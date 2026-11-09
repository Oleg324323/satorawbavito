"""General runtime reapplication must not turn API intent into WB publication.

Only the existing owned Unix PostgreSQL allocator is used. These are actual
API/worker login tests, not production rollout or provider acceptance.
"""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.wb_live.repository import WbLiveRepository
from tests import test_orders_schema_candidate as candidate
from tests import test_wb_live_history_postgres as history
from tests import test_wb_live_platform as platform
from tests.test_orders_schema_integration import runtime_script

cluster = candidate.cluster
pg_store = platform.pg_store
data = platform.data
live = platform.live


@pytest.fixture(scope="module")
def pg_database(cluster):
    api, worker, dispatcher = ("wb_acl_" + uuid4().hex for _ in range(3))
    with candidate.disposable_database(cluster, (api, worker, dispatcher)) as database:
        result = candidate.migrate(database.url, "upgrade", "20260910_0084")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=api), hide_parameters=True)
        try:
            # Model a previously over-granted named role, including column ACLs
            # which REVOKE ALL ON TABLE alone cannot remove.
            with owner.begin() as connection:
                connection.exec_driver_sql(f'GRANT UPDATE(checkpoint) ON wb_live_sync_sources TO "{api}"')
                connection.exec_driver_sql(f'GRANT INSERT(ordinal) ON wb_live_history_rows TO "{api}"')
                connection.exec_driver_sql(f'GRANT UPDATE(credential_id) ON wb_live_sync_jobs TO "{worker}"')
                connection.exec_driver_sql(f'GRANT UPDATE(nm_id) ON wb_live_products TO "{worker}"')
                connection.exec_driver_sql(f'GRANT SELECT(ordinal) ON wb_live_history_rows TO "{dispatcher}"')
            script = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
            for old, new in (("satorna_wb_live", database.name), ("wb_live_owner", database.owner),
                             ("wb_live_worker", worker), ("wb_live_api", api),
                             ("wb_live_dispatch", dispatcher)):
                script = script.replace(old, new)
            result = candidate.subprocess.run(
                ["psql", "-X", "-w", "-v", "ON_ERROR_STOP=1", "-h", database.host,
                 "-p", str(database.port), "-U", database.owner, "-d", database.name],
                input=script, env=candidate.isolated_environment(), capture_output=True,
                text=True, timeout=60, check=False,
            )
            assert result.returncode == 0, result.stderr
            # The general script must be safe even when it runs LAST, twice.
            for _ in range(2):
                result = runtime_script(owner, api)
                assert result.returncode == 0, result.stderr
            runtime._wb_live_api_role = runtime._history_api_role = api
            runtime._wb_live_worker_role = runtime._history_worker_role = worker
            runtime._wb_live_dispatch_role = dispatcher
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def test_actual_api_cannot_publish_or_rewrite_ownership_after_general_grants(live):
    d = live
    platform.start(d)
    statements = (
        "UPDATE wb_live_products SET title=title WHERE false",
        "INSERT INTO wb_live_products SELECT * FROM wb_live_products WHERE false",
        "UPDATE wb_live_product_sizes SET price_kopecks=price_kopecks WHERE false",
        "INSERT INTO wb_live_pages SELECT * FROM wb_live_pages WHERE false",
        "SELECT * FROM wb_live_history_pages WHERE false",
        "SELECT * FROM wb_live_history_rows WHERE false",
        "INSERT INTO wb_live_history_rows(ordinal) SELECT 0 WHERE false",
        "UPDATE wb_live_sync_sources SET checkpoint=checkpoint WHERE false",
        "UPDATE wb_live_sync_sources SET processed=processed,revision=revision,run_id=run_id WHERE false",
        "UPDATE wb_live_sync_jobs SET credential_id=credential_id,user_id=user_id WHERE false",
        "UPDATE wb_live_sync_requests SET idempotency_key=idempotency_key WHERE false",
        "DELETE FROM wb_live_history_requests WHERE false",
        "SELECT * FROM wb_live_due_jobs(1)",
    )
    for statement in statements:
        with pytest.raises(DBAPIError) as denied, d.factory.begin() as session:
            session.execute(text("SELECT set_config('app.organization_id',:org,true),"
                                 "set_config('app.marketplace_account_id',:org,true)"), {"org": str(d.org)})
            session.execute(text(statement))
        assert denied.value.orig.sqlstate == "42501", statement
    with d.engine.connect() as connection:
        api = d.factory.kw["bind"].url.username
        for table, column in (("wb_live_sync_sources", "checkpoint"), ("wb_live_history_rows", "ordinal")):
            privilege = "UPDATE" if column == "checkpoint" else "INSERT"
            assert not connection.scalar(text("SELECT has_column_privilege(:r,:t,:c,:p)"),
                                         {"r": api, "t": table, "c": column, "p": privilege})
        worker = d.factory.kw["bind"]._wb_live_worker_role
        dispatcher = d.factory.kw["bind"]._wb_live_dispatch_role
        assert not connection.scalar(text("SELECT has_column_privilege(:r,'wb_live_sync_jobs','credential_id','UPDATE')"), {"r": worker})
        assert not connection.scalar(text("SELECT has_column_privilege(:r,'wb_live_products','nm_id','UPDATE')"), {"r": worker})
        assert not connection.scalar(text("SELECT has_column_privilege(:r,'wb_live_history_rows','ordinal','SELECT')"), {"r": dispatcher})
        for role, allowed in ((api, False), (worker, True), (dispatcher, True)):
            assert connection.scalar(text("SELECT has_function_privilege(:r,'wb_live_due_jobs(integer)','EXECUTE')"), {"r": role}) is allowed
            assert not connection.scalar(text("SELECT has_function_privilege(:r,'wb_live_history_immutable()','EXECUTE')"), {"r": role})
            assert connection.execute(text("SELECT rolsuper,rolbypassrls,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=:r"), {"r": role}).one() == (False, False, False, False)
            assert not connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=:r))"), {"r": role})
        assert connection.scalar(text("SELECT pg_get_userbyid(proowner)=session_user AND prosecdef FROM pg_proc WHERE oid='wb_live_due_jobs(integer)'::regprocedure"))
        assert connection.scalar(text("SELECT proconfig FROM pg_proc WHERE oid='wb_live_due_jobs(integer)'::regprocedure")) == ["search_path=pg_catalog, public"]


def test_api_intent_retry_and_worker_publication_remain_separate(live):
    d = live
    locator = platform.start(d)
    assert platform.start(d) == locator
    engine = d.factory.kw["bind"]
    worker = create_engine(engine.url.set(username=engine._wb_live_worker_role), hide_parameters=True)
    api_repo = d.repo
    try:
        d.repo = WbLiveRepository(sessionmaker(worker, expire_on_commit=False), d.keys)
        assert locator in d.repo.due_jobs()
        lease = d.repo.claim_batch(locator)
        assert lease.source == "content"
        assert d.repo.resolve_for_fetch(lease).secret.reveal()["token"] == "synthetic-guard"
        assert platform.commit(d, lease)
        prices = d.repo.claim_batch(locator)
        with d.engine.begin() as connection:
            connection.execute(text("UPDATE wb_live_sync_sources SET attempt=8 WHERE organization_id=:org AND source='prices'"), {"org": d.org})
        assert d.repo.fail_batch(prices, error_code="WB_RETRY_EXHAUSTED")
        d.repo = api_repo
        assert platform.start(d, "synthetic-acl-retry") == locator
        view = d.repo.status(d.actor, d.org)
        content = next(source for source in view["sources"] if source["source"] == "content")
        assert content["processed"] == 1
    finally:
        d.repo = api_repo
        worker.dispose()


# Reuse unchanged behavioral cases under this distinct final general API-role
# fixture. Their owner-only setup remains synthetic; actual operations use API
# and worker engines, including lock-bearing credential and history paths.
test_actual_worker_history_eof_and_replay = history.test_real_worker_staging_eof_ack_and_immutability
test_actual_api_verified_connection_and_rotation = platform.test_verified_connection_encrypted_and_rechecked_after_io
test_actual_api_registration_and_session = platform.test_live_registration_login_are_postgres_only
test_actual_api_no_context_and_wrong_account = platform.test_current_head_forced_rls_no_context_denies_rows
