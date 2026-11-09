"""Owned local PostgreSQL only; no providers, services or credential material."""
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.platform.integrations.wb_price_quota import (
    WbPriceQuotaError,
    WbPriceQuotaService,
)
from app.platform.integrations.worker_identity import ExecutorRoleIdentity
from tests import test_orders_schema_candidate as candidate
from tests.test_orders_schema_integration import runtime_script

cluster = candidate.cluster
IDS = count(82000)


@pytest.fixture(scope="module")
def quota_db(cluster):
    names = {kind: "quota_" + kind + "_" + uuid4().hex for kind in ("reader", "executor", "api", "dispatch", "beat")}
    with candidate.disposable_database(cluster, tuple(names.values())) as database:
        migrated = candidate.migrate(database.url, "upgrade", "20260910_0083")
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        engines = {kind: create_engine(owner.url.set(username=name), hide_parameters=True) for kind, name in names.items()}
        try:
            with owner.begin() as connection:
                connection.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{names["api"]}"')
                defaults_before = connection.execute(text("SELECT defaclrole,defaclnamespace,defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid")).all()
            migrated = candidate.migrate(database.url, "upgrade", "20260910_0084")
            assert migrated.returncode == 0, migrated.stderr
            with owner.connect() as connection:
                assert connection.execute(text("SELECT defaclrole,defaclnamespace,defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid")).all() == defaults_before
                assert not connection.scalar(text("SELECT has_table_privilege(:role,'wb_price_quota','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"), {"role": names["api"]})
            reapplied = runtime_script(owner, names["api"])
            assert reapplied.returncode == 0, reapplied.stderr
            script = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
            for before, after in {"satorna_wb_live": database.name, "wb_live_owner": database.owner,
                                  "wb_live_worker": names["reader"], "wb_live_api": names["api"],
                                  "wb_live_dispatch": names["dispatch"]}.items():
                script = script.replace(before, after)
            script += f"\n\\set executor_role {names['executor']}\n\\set api_runtime_role {names['api']}\n"
            script += (candidate.ROOT / "ops/repricer-executor-grants.sql").read_text()
            result = candidate.subprocess.run(
                ["psql", "-X", "-w", "-v", "ON_ERROR_STOP=1", "-h", database.host,
                 "-p", str(database.port), "-U", database.owner, "-d", database.name],
                input=script, env=candidate.isolated_environment(), capture_output=True, text=True, timeout=60, check=False)
            assert result.returncode == 0, result.stderr
            yield SimpleNamespace(owner=owner, engines=engines, names=names, database=database)
        finally:
            for engine in engines.values():
                engine.dispose()
            owner.dispose()


@pytest.fixture
def scope(quota_db):
    org = next(IDS)
    account = org * 10
    with quota_db.owner.begin() as connection:
        connection.execute(text("INSERT INTO lk_organizations(organization_id,slug,name) VALUES(:org,:slug,'Synthetic')"),
                           {"org": org, "slug": f"quota-{org}"})
        for offset, provider in ((0, "wb"), (1, "wb"), (2, "avito")):
            connection.execute(text("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES(:account,:org,:provider,:external,'connected')"),
                               {"account": account + offset, "org": org, "provider": provider, "external": f"synthetic-{account + offset}"})
    return org, account


def service(db, kind="reader", *, session_class=Session, identity_kind=None):
    return WbPriceQuotaService(session_factory=sessionmaker(bind=db.engines[kind], class_=session_class),
        identity=ExecutorRoleIdentity(db.names[identity_kind or kind], db.names["api"]))


def admit(svc, scope, *, post=False):
    return svc.admit_request(*scope, "POST" if post else "GET",
                             "/api/v2/upload/task" if post else "/api/v2/list/goods/filter", 900)


def deadline(db, scope):
    with db.owner.connect() as connection:
        return connection.scalar(text("SELECT next_allowed_at FROM wb_price_quota WHERE organization_id=:org AND marketplace_account_id=:account"),
                                 dict(zip(("org", "account"), scope)))


def test_postgres_reader_executor_concurrent_single_admission(quota_db, scope):
    barrier = Barrier(2)
    def run(kind):
        barrier.wait(timeout=10)
        return admit(service(quota_db, kind), scope, post=kind == "executor")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, ("reader", "executor"))) == [False, True]
    assert quota_db.names["reader"] != quota_db.names["executor"]
    assert deadline(quota_db, scope) is not None


def test_postgres_accounts_independent_and_metadata_only(quota_db, scope):
    statements = []
    def capture(_c, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(quota_db.engines["reader"], "before_cursor_execute", capture)
    try:
        assert admit(service(quota_db), scope)
        assert admit(service(quota_db), (scope[0], scope[1] + 1))
    finally:
        event.remove(quota_db.engines["reader"], "before_cursor_execute", capture)
    assert not any(word in " ".join(statements) for word in ("ciphertext", "credential_ref", "marketplace_account_credentials"))


def test_postgres_long_feedback_restart_and_out_of_order_max(quota_db, scope):
    with quota_db.owner.connect() as connection:
        future, earlier = connection.execute(text("SELECT clock_timestamp()+interval '3600 seconds',clock_timestamp()+interval '900 seconds'")).one()
    assert service(quota_db).record_cooldown(*scope, future)
    assert service(quota_db, "executor").record_cooldown(*scope, earlier)
    assert service(quota_db).record_cooldown(*scope, future)
    assert deadline(quota_db, scope) == future
    assert not admit(service(quota_db), scope)
    assert not admit(service(quota_db, "executor"), scope, post=True)


@pytest.mark.parametrize("kind", ["api", "dispatch", "beat"])
def test_postgres_actual_wrong_login_denied(quota_db, scope, kind):
    with pytest.raises(WbPriceQuotaError, match="^WB_PRICE_QUOTA_DENIED$"):
        admit(service(quota_db, kind, identity_kind="reader"), scope)
    assert deadline(quota_db, scope) is None


@pytest.mark.parametrize("wrong", ["organization", "provider"])
def test_postgres_wrong_scope_denied(quota_db, scope, wrong):
    attempted = (scope[0] + 500000, scope[1]) if wrong == "organization" else (scope[0], scope[1] + 2)
    with pytest.raises(WbPriceQuotaError, match="^WB_PRICE_QUOTA_DENIED$"):
        admit(service(quota_db), attempted)
    assert deadline(quota_db, attempted) is None


def test_postgres_commit_ack_unknown_preserves_reservation(quota_db, scope):
    class UnknownCommit(Session):
        def commit(self):
            super().commit()
            raise RuntimeError("synthetic-commit-ack-unknown")
    with pytest.raises(WbPriceQuotaError, match="^WB_PRICE_QUOTA_UNAVAILABLE$"):
        admit(service(quota_db, session_class=UnknownCommit), scope)
    assert not admit(service(quota_db, "executor"), scope, post=True)


def test_postgres_acl_rls_and_finite_constraint(quota_db, scope):
    assert admit(service(quota_db), scope)
    with quota_db.owner.connect() as connection:
        assert connection.execute(text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='wb_price_quota'::regclass")).one() == (True, True)
        for kind in ("reader", "executor"):
            assert connection.scalar(text("SELECT has_table_privilege(:role,'wb_price_quota','SELECT,INSERT')"), {"role": quota_db.names[kind]})
            assert not connection.scalar(text("SELECT has_table_privilege(:role,'wb_price_quota','UPDATE,DELETE,TRUNCATE')"), {"role": quota_db.names[kind]})
            assert not connection.scalar(text("SELECT has_column_privilege(:role,'wb_price_quota','created_at','UPDATE')"), {"role": quota_db.names[kind]})
        for kind in ("api", "dispatch", "beat"):
            assert not connection.scalar(text("SELECT has_table_privilege(:role,'wb_price_quota','SELECT,INSERT,UPDATE,DELETE')"), {"role": quota_db.names[kind]})
    with quota_db.engines["reader"].begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM wb_price_quota")) == 0
    with quota_db.engines["reader"].begin() as connection:
        connection.execute(text("SELECT set_config('app.organization_id',:org,true),set_config('app.marketplace_account_id',:account,true)"),
                           {"org": str(scope[0]), "account": str(scope[1] + 1)})
        assert connection.scalar(text("SELECT count(*) FROM wb_price_quota")) == 0
        with pytest.raises(DBAPIError):
            connection.execute(text("INSERT INTO wb_price_quota VALUES(:org,:account,'wb','prices_discounts',clock_timestamp(),clock_timestamp(),clock_timestamp())"),
                               {"org": scope[0], "account": scope[1]})
    with quota_db.engines["reader"].begin() as connection:
        connection.execute(text("SELECT set_config('app.organization_id',:org,true),set_config('app.marketplace_account_id',:account,true)"),
                           {"org": str(scope[0]), "account": str(scope[1])})
        with pytest.raises(DBAPIError):
            connection.execute(text("UPDATE wb_price_quota SET next_allowed_at='infinity'"))


def test_postgres_used_downgrade_fails_closed(quota_db, scope):
    assert admit(service(quota_db), scope)
    result = candidate.migrate(quota_db.database.url, "downgrade", "20260910_0083")
    assert result.returncode != 0 and "WB_PRICE_QUOTA_DOWNGRADE_HAS_DATA" in result.stderr
    with quota_db.owner.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260910_0084"
    assert deadline(quota_db, scope) is not None


def test_postgres_unused_downgrade(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", "20260910_0084")
        assert result.returncode == 0, result.stderr
        result = candidate.migrate(database.url, "downgrade", "20260910_0083")
        assert result.returncode == 0, result.stderr
        result = candidate.migrate(database.url, "upgrade", "20260910_0084")
        assert result.returncode == 0, result.stderr
