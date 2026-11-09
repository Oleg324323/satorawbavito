"""Exercise the active Orders revision only in fresh disposable PostgreSQL databases."""
# Separate contexts expose the transaction commit boundary under assertion.
# ruff: noqa: SIM117

import getpass
import os
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ops.release_gate import safe_environment

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "schema_candidates" / "orders_v1"
TABLES = (
    "order_sync_runs",
    "marketplace_orders",
    "marketplace_order_items",
    "order_observations",
    "order_status_observations",
    "order_lifecycle_events",
    "order_deadlines",
    "order_sync_coverage",
    "order_sync_memberships",
    "order_read_snapshots",
    "order_read_snapshot_rows",
)
ROLE = "orders_candidate_runtime_" + uuid4().hex[:12]


def orders_revision():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_revision("20260909_0062").module


def run_revision(connection, action):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(orders_revision(), action)()


def test_candidate_is_embedded_in_actual_revision():
    migration = orders_revision()
    assert migration.UPGRADE_SQL == (BUNDLE / "upgrade.sql").read_text()
    assert migration.DOWNGRADE_SQL == (BUNDLE / "downgrade.sql").read_text()
    assert migration.down_revision == "20260908_0061"


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    binaries = {name: shutil.which(name) for name in ("initdb", "pg_ctl", "createdb")}
    assert all(binaries.values()), "Disposable PostgreSQL binaries required"
    if os.environ.get("ORDERS_TEST_USE_LOCAL_CLUSTER") == "1":
        # Explicit fallback: only local Unix maintenance socket; never an application URL.
        yield binaries, None, 5432, getpass.getuser()
        return
    root = tmp_path_factory.mktemp("orders-schema-postgres")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    owner = "orders_candidate_owner"

    def run(args):
        subprocess.run(args, check=True, capture_output=True, text=True)

    run(
        [
            binaries["initdb"],
            "-D",
            str(root / "data"),
            "-A",
            "trust",
            "-U",
            owner,
            "--no-locale",
            "-E",
            "UTF8",
        ]
    )
    try:
        run(
            [
                binaries["pg_ctl"], "-D", str(root / "data"),
                "-l", str(root / "server.log"),
                "-o", f"-p {port} -h '' -k {root} -c fsync=off",
                "-w", "start",
            ]
        )
        yield binaries, root, port, owner
    finally:
        run([binaries["pg_ctl"], "-D", str(root / "data"), "-m", "fast", "-w", "stop"])


def isolated_environment():
    environment = safe_environment()
    environment.update(PGPASSFILE="/dev/null", PGSERVICEFILE="/dev/null", NETRC="/dev/null")
    return environment


def migrate(url, action, revision):
    environment = isolated_environment()
    environment["VELLA_DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", action, revision], cwd=ROOT,
        env=environment, capture_output=True, text=True, timeout=60, check=False,
    )


@contextmanager
def disposable_database(cluster, roles=()):
    _, root, port, owner = cluster
    name = "orders_test_" + uuid4().hex
    host = "/tmp" if root is None else str(root)
    admin = psycopg.connect(
        host=host, port=port, dbname="postgres", user=owner, autocommit=True,
        passfile="/dev/null",
    )
    attempted_db = False
    attempted_roles = []
    try:
        assert admin.execute("SELECT inet_server_addr() IS NULL").fetchone() == (True,)
        assert not admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        attempted_db = True
        admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
        for role in roles:
            assert not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            attempted_roles.append(role)
            admin.execute(psycopg.sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
            ).format(psycopg.sql.Identifier(role)))
        url = f"postgresql+psycopg://{owner}@/{name}?host={host}&port={port}"
        yield SimpleNamespace(name=name, host=host, port=port, owner=owner, url=url, admin=admin)
    finally:
        try:
            if attempted_db:
                admin.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    psycopg.sql.Identifier(name)))
            for role in reversed(attempted_roles):
                admin.execute(psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role)))
            assert not admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            for role in attempted_roles:
                assert not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            print(f"Orders cleanup verified: database={name}; roles={','.join(attempted_roles)}")
        finally:
            admin.close()


@pytest.fixture(scope="module")
def db(cluster):
    with disposable_database(cluster, (ROLE,)) as database:
        result = migrate(database.url, "upgrade", "20260909_0062")
        assert result.returncode == 0, result.stderr
        engine = create_engine(database.url)
        runtime_url = f"postgresql+psycopg://{ROLE}@/{database.name}?host={database.host}&port={database.port}"
        runtime = create_engine(runtime_url)
        try:
            with engine.begin() as c:
                # Deliberately broad only in this isolated fixture: history triggers
                # must remain a second defense independently of runtime ACLs.
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
                c.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {ROLE}")
                c.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
                c.exec_driver_sql(
                    "INSERT INTO lk_organizations (organization_id, slug, name) VALUES (91001,'orders-one','Synthetic one'),(91002,'orders-two','Synthetic two')"
                )
                c.exec_driver_sql("""INSERT INTO marketplace_accounts
                  (marketplace_account_id, organization_id, marketplace, external_account_id, status)
                  VALUES (91101,91001,'avito','synthetic-a','connected'),
                         (91102,91001,'avito','synthetic-b','connected'),
                         (91201,91002,'wb','synthetic-c','connected')""")
            yield engine, runtime
        finally:
            runtime.dispose()
            engine.dispose()


def scope(c, org=91001):
    c.execute(
        text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(org)}
    )


def order(c, account=91101, org=91001, external=None):
    return c.execute(
        text("""INSERT INTO marketplace_orders
        (organization_id, marketplace_account_id, marketplace, external_order_id)
        VALUES (:org,:account,:marketplace,:external) RETURNING order_id"""),
        {
            "org": org,
            "account": account,
            "marketplace": "wb" if account == 91201 else "avito",
            "external": external or uuid4().hex,
        },
    ).scalar_one()


def run(c, account=91101, org=91001):
    return c.execute(
        text("""INSERT INTO order_sync_runs
        (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,
         adapter_version,mapping_version,source_contract_version,source_snapshot)
        VALUES (:org,:account,:marketplace,:source,:key,'v1','v1','v1','snapshot')
        RETURNING sync_run_id"""),
        {
            "org": org,
            "account": account,
            "marketplace": "wb" if account == 91201 else "avito",
            "source": "wb-statistics-supplier-orders"
            if account == 91201
            else "avito-order-management",
            "key": uuid4().hex,
        },
    ).scalar_one()


def observation(c, oid, rid, checksum="a" * 64, revision=None, event=None):
    return c.execute(
        text("""INSERT INTO order_observations
        (organization_id,marketplace_account_id,sync_run_id,order_id,source_kind,
         adapter_version,source_revision,source_event_id,payload_checksum,normalized_evidence,observed_at)
        VALUES (91001,91101,:run,:order,'avito-order-management','v1',:revision,:event,:checksum,'{}',now())
        RETURNING observation_id"""),
        {
            "run": rid,
            "order": oid,
            "checksum": checksum,
            "revision": revision,
            "event": event,
        },
    ).scalar_one()


def test_forced_rls_default_deny_and_transaction_context_reset(db):
    owner, runtime = db
    with owner.connect() as c:
        rows = c.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:names)"
            ),
            {"names": list(TABLES)},
        ).all()
        assert len(rows) == len(TABLES) and all(a and b for _, a, b in rows)
        assert (
            c.execute(
                text(
                    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=:role"
                ),
                {"role": ROLE},
            ).scalar_one()
            is False
        )
    with runtime.begin() as c:
        scope(c)
        order(c)
    with runtime.connect() as c:
        for table in TABLES:
            assert c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
    with pytest.raises(DBAPIError, match="row-level security"):
        with runtime.begin() as c:
            order(c)
    with pytest.raises(DBAPIError, match="row-level security"):
        with runtime.begin() as c:
            scope(c, 91002)
            order(c)


def test_same_external_id_across_tenants_and_accounts(db):
    _, runtime = db
    key = "000" + uuid4().hex
    for org, account in [(91001, 91101), (91001, 91102), (91002, 91201)]:
        with runtime.begin() as c:
            scope(c, org)
            order(c, account, org, key)
            assert c.execute(
                text(
                    "SELECT external_order_id FROM marketplace_orders WHERE external_order_id=:key"
                ),
                {"key": key},
            ).scalars().all() == [key] * (2 if account == 91102 else 1)


def test_cross_account_parent_and_run_links_are_rejected(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        oid = order(c)
        rid = run(c, account=91102)
    with pytest.raises(IntegrityError), runtime.begin() as c:
        scope(c)
        observation(c, oid, rid)
    with pytest.raises(IntegrityError):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text("""INSERT INTO marketplace_order_items
              (organization_id,marketplace_account_id,order_id,source_line_key,quantity,resolution_version)
              VALUES (91001,91102,:oid,'line',1,'v1')"""),
                {"oid": oid},
            )


def test_replay_key_keeps_entities_and_event_revisions_distinct(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        oid, other, rid = order(c), order(c), run(c)
        first = observation(c, oid, rid, event="event")
        assert observation(c, other, rid, event="event") != first
        assert (
            observation(c, oid, rid, checksum="b" * 64, revision="v2", event="event")
            != first
        )
    with pytest.raises(IntegrityError), runtime.begin() as c:
        scope(c)
        observation(c, oid, rid, event="event")


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_history_is_immutable_even_after_broad_grants(db, operation):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        oid, rid = order(c), run(c)
        observation(c, oid, rid)
    statement = {
        "UPDATE": "UPDATE order_observations SET normalized_evidence='{}'",
        "DELETE": "DELETE FROM order_observations",
        "TRUNCATE": "TRUNCATE order_observations CASCADE",
    }[operation]
    with pytest.raises(DBAPIError, match="immutable"), runtime.begin() as c:
        scope(c)
        c.exec_driver_sql(statement)


def test_nonempty_downgrade_preserves_every_table(db):
    owner, runtime = db
    with runtime.begin() as c:
        scope(c)
        order(c)
    with owner.connect() as c:
        before = {table: c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() for table in TABLES}
    url = owner.url
    # Keep the Unix path literal: Alembic Config treats percent-encoded / as interpolation.
    result = migrate(f"postgresql+psycopg://{url.username}@/{url.database}?host={url.query['host']}&port={url.query['port']}", "downgrade", "20260908_0061")
    assert result.returncode != 0 and "nonempty" in result.stderr
    with owner.connect() as c:
        assert all(
            c.execute(text("SELECT to_regclass(:name)"), {"name": table}).scalar_one()
            for table in TABLES
        )
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260909_0062"
        assert {table: c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() for table in TABLES} == before


def test_concurrent_replay_uniqueness_uses_two_postgres_sessions(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        oid, rid = order(c), run(c)
    barrier = Barrier(2)

    def insert():
        with runtime.connect() as c:
            with c.begin():
                scope(c)
                c.exec_driver_sql("SET LOCAL lock_timeout='4s'")
                pid = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                barrier.wait(timeout=5)
                try:
                    observation(c, oid, rid, checksum="c" * 64)
                except IntegrityError:
                    return pid, "duplicate"
            return pid, "inserted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(insert) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert len({pid for pid, _ in results}) == 2
    assert sorted(state for _, state in results) == ["duplicate", "inserted"]


def item(c, oid, **values):
    fields = {
        "organization_id": 91001,
        "marketplace_account_id": 91101,
        "order_id": oid,
        "source_line_key": uuid4().hex,
        "quantity": 1,
        "resolution_version": "v1",
    }
    fields.update(values)
    cols = ",".join(fields)
    params = ",".join(":" + key for key in fields)
    return c.execute(
        text(
            f"INSERT INTO marketplace_order_items ({cols}) VALUES ({params}) RETURNING order_item_id"
        ),
        fields,
    ).scalar_one()


@pytest.mark.parametrize(
    "bad", ["\tstatus", "status\n", "\u00a0status", "status\u2003", "\u3000status", ""]
)
def test_exact_provider_status_rejects_python_edge_whitespace(db, bad):
    _, runtime = db
    with pytest.raises(IntegrityError):
        with runtime.begin() as c:
            scope(c)
            oid = order(c)
            c.execute(
                text(
                    "UPDATE marketplace_orders SET raw_status=:bad,version=version+1 WHERE order_id=:oid"
                ),
                {"bad": bad, "oid": oid},
            )


def test_wb_nullable_unknown_status_and_revision_roundtrip(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c, 91002)
        oid = order(c, 91201, 91002)
        assert c.execute(
            text(
                "SELECT raw_status,canonical_status,mapping_state FROM marketplace_orders WHERE order_id=:oid"
            ),
            {"oid": oid},
        ).one() == (None, None, "unmapped")
    with runtime.begin() as c:
        scope(c)
        oid, rid = order(c), run(c)
        obs = observation(c, oid, rid)
        assert c.execute(
            text(
                "SELECT source_revision,source_effective_at FROM order_observations WHERE observation_id=:id"
            ),
            {"id": obs},
        ).one() == (None, None)


@pytest.mark.parametrize(
    "fields",
    [
        {"quantity": 0},
        {"occurrence_index": -1},
        {"resolution_state": "resolved"},
        {"resolution_state": "manual_override"},
        {"marketplace_offer_id": 1},
        {"version": 0},
    ],
)
def test_invalid_item_state_is_rejected(db, fields):
    _, runtime = db
    with pytest.raises(IntegrityError), runtime.begin() as c:
        scope(c)
        item(c, order(c), **fields)


def test_catalog_account_product_pair_and_resolution_constraints(db):
    owner, runtime = db
    with owner.begin() as c:
        c.exec_driver_sql("""INSERT INTO catalog_skus(catalog_sku_id,organization_id,code)
            VALUES (91301,91001,'synthetic-one'),(91302,91002,'synthetic-two');
            INSERT INTO marketplace_products(marketplace_product_id,organization_id,marketplace_account_id,external_product_id)
            VALUES (91401,91001,91101,'p1'),(91402,91001,91101,'p2'),(91403,91001,91102,'p3');
            INSERT INTO marketplace_offers(marketplace_offer_id,organization_id,marketplace_account_id,marketplace_product_id,external_offer_key)
            VALUES (91501,91001,91101,91401,'offer1');""")
    with pytest.raises(IntegrityError):
        with owner.begin() as c:
            c.exec_driver_sql("""INSERT INTO marketplace_offers(organization_id,marketplace_account_id,marketplace_product_id,external_offer_key)
                VALUES (91001,91102,91401,'bad-account')""")
    for fields in [
        {"marketplace_product_id": 91403},
        {"marketplace_product_id": 91402, "marketplace_offer_id": 91501},
        {"resolution_state": "unmapped", "catalog_sku_id": 91301},
        {"resolution_state": "resolved", "catalog_sku_id": 91301},
        {"resolution_state": "manual_override", "catalog_sku_id": 91302},
    ]:
        with pytest.raises(IntegrityError), runtime.begin() as c:
            scope(c)
            item(c, order(c), **fields)
    with runtime.begin() as c:
        scope(c)
        item(c, order(c), resolution_state="manual_override", catalog_sku_id=91301)
        item(
            c,
            order(c),
            resolution_state="resolved",
            catalog_sku_id=91301,
            marketplace_product_id=91401,
            marketplace_offer_id=91501,
        )


def test_order_item_observation_parent_cannot_disagree(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        oid, other, rid = order(c), order(c), run(c)
        iid = item(c, other)
        obs = observation(c, oid, rid)
    with pytest.raises(IntegrityError):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text("""INSERT INTO order_status_observations
                (organization_id,marketplace_account_id,order_id,order_item_id,observation_id,
                 mapping_version,evidence_source,observed_at)
                VALUES(91001,91101,:oid,:iid,:obs,'v1','avito-order-management',now())"""),
                {"oid": oid, "iid": iid, "obs": obs},
            )


def test_complete_run_requires_manifest_metadata_and_terminal_is_immutable(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        rid = run(c)
    with pytest.raises(IntegrityError):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text(
                    "UPDATE order_sync_runs SET state='complete',completed_at=now() WHERE sync_run_id=:id"
                ),
                {"id": rid},
            )
    with runtime.begin() as c:
        scope(c)
        c.execute(
            text("""UPDATE order_sync_runs SET state='complete',manifest_state='complete',
            completed_at=now(),page_count=1,expected_order_count=0,payload_checksum=:checksum
            WHERE sync_run_id=:id"""),
            {"id": rid, "checksum": "f" * 64},
        )
    with pytest.raises(DBAPIError, match="immutable"):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text("UPDATE order_sync_runs SET page_count=2 WHERE sync_run_id=:id"),
                {"id": rid},
            )


def test_snapshot_rows_remain_frozen_across_current_projection_changes(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        oid, rid = order(c), run(c)
        iid = item(c, oid)
        obs = observation(c, oid, rid)
        snapshot = c.execute(
            text("""INSERT INTO order_read_snapshots
            (organization_id,high_water_mark,account_scope,query_checksum,account_coverage,coverage_state,row_count)
            VALUES(91001,'watermark',ARRAY[91101],:checksum,'[]','partial',1)
            RETURNING snapshot_id"""),
            {"checksum": "e" * 64},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO order_read_snapshot_rows
            (organization_id,marketplace_account_id,snapshot_id,order_id,order_item_id,observation_id,
             position,row_version,payload_schema_version,row_payload)
            VALUES(91001,91101,:snapshot,:oid,:iid,:obs,1,1,1,jsonb_build_object('quantity',1))"""),
            {"snapshot": snapshot, "oid": oid, "iid": iid, "obs": obs},
        )
    with runtime.begin() as c:
        scope(c)
        c.execute(
            text(
                "UPDATE marketplace_order_items SET quantity=2,version=version+1 WHERE order_item_id=:iid"
            ),
            {"iid": iid},
        )
        assert c.execute(
            text(
                "SELECT row_payload FROM order_read_snapshot_rows WHERE snapshot_id=:id"
            ),
            {"id": snapshot},
        ).scalar_one() == {"quantity": 1}
    with pytest.raises(DBAPIError, match="immutable"):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text(
                    "UPDATE order_read_snapshot_rows SET row_payload='{}' WHERE snapshot_id=:id"
                ),
                {"id": snapshot},
            )


def test_current_projection_cas_has_exactly_one_winner(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        iid = item(c, order(c))
    barrier = Barrier(2)

    def change(quantity):
        with runtime.begin() as c:
            scope(c)
            c.exec_driver_sql("SET LOCAL lock_timeout='4s'")
            pid = c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            barrier.wait(timeout=5)
            rows = (
                c.execute(
                    text("""UPDATE marketplace_order_items SET quantity=:quantity,version=version+1
                WHERE order_item_id=:iid AND version=1 RETURNING version"""),
                    {"quantity": quantity, "iid": iid},
                )
                .scalars()
                .all()
            )
            return pid, rows

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(change, q) for q in [2, 3]]
        results = [f.result(timeout=10) for f in futures]
    assert len({pid for pid, _ in results}) == 2
    assert sorted(rows for _, rows in results) == [[], [2]]


def test_downgrade_fails_closed_for_nonbypass_owner_with_hidden_rows(db):
    owner, runtime = db
    with runtime.begin() as c:
        scope(c, 91002)
        order(c, 91201, 91002)
    hidden_owner = "orders_hidden_" + uuid4().hex
    with owner.connect() as c:
        before = {table: c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() for table in TABLES}
    with pytest.raises(DBAPIError, match="row-level security"):
        with owner.begin() as c:
            c.exec_driver_sql(
                f"CREATE ROLE {hidden_owner} NOSUPERUSER NOBYPASSRLS"
            )
            for table in TABLES:
                c.exec_driver_sql(
                    f"ALTER TABLE {table} OWNER TO {hidden_owner}"
                )
            c.exec_driver_sql(f"SET LOCAL ROLE {hidden_owner}")
            run_revision(c, "downgrade")
    with owner.connect() as c:
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260909_0062"
        assert {table: c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() for table in TABLES} == before
        assert c.execute(text("SELECT count(*) FROM pg_roles WHERE rolname=:role"), {"role": hidden_owner}).scalar_one() == 0


def test_observation_source_must_match_run(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        oid, rid = order(c), run(c)
    with pytest.raises(IntegrityError):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text("""INSERT INTO order_observations
                (organization_id,marketplace_account_id,sync_run_id,order_id,source_kind,
                 adapter_version,payload_checksum,normalized_evidence,observed_at)
                VALUES(91001,91101,:rid,:oid,'wb-statistics-supplier-orders','v1',:checksum,'{}',now())"""),
                {"rid": rid, "oid": oid, "checksum": "a" * 64},
            )


def test_snapshot_requires_exact_row_count_at_commit_and_cannot_append_later(db):
    _, runtime = db
    with pytest.raises(DBAPIError, match="snapshot"):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text("""INSERT INTO order_read_snapshots
                (organization_id,high_water_mark,account_scope,query_checksum,account_coverage,coverage_state,row_count)
                VALUES(91001,'test',ARRAY[91101],:checksum,'[]','partial',1)"""),
                {"checksum": "a" * 64},
            )
    with runtime.begin() as c:
        scope(c)
        snapshot = c.execute(
            text("""INSERT INTO order_read_snapshots
            (organization_id,high_water_mark,account_scope,query_checksum,account_coverage,coverage_state,row_count)
            VALUES(91001,'test',ARRAY[91101],:checksum,'[]','partial',0) RETURNING snapshot_id"""),
            {"checksum": "a" * 64},
        ).scalar_one()
        oid, rid = order(c), run(c)
        iid = item(c, oid)
        obs = observation(c, oid, rid)
    with pytest.raises(DBAPIError, match="snapshot"):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text("""INSERT INTO order_read_snapshot_rows
                (organization_id,marketplace_account_id,snapshot_id,order_id,order_item_id,observation_id,
                 position,row_version,payload_schema_version,row_payload)
                VALUES(91001,91101,:snapshot,:oid,:iid,:obs,1,1,1,'{}')"""),
                {"snapshot": snapshot, "oid": oid, "iid": iid, "obs": obs},
            )


@pytest.mark.parametrize("target", ["wrong_account", "wrong_item"])
def test_snapshot_rejects_account_scope_and_item_observation_mismatch(db, target):
    _, runtime = db
    with pytest.raises(DBAPIError, match="snapshot"):
        with runtime.begin() as c:
            scope(c)
            oid, rid = order(c), run(c)
            iid, other = item(c, oid), item(c, oid)
            obs = c.execute(
                text("""INSERT INTO order_observations
                (organization_id,marketplace_account_id,sync_run_id,order_id,order_item_id,
                 source_kind,adapter_version,payload_checksum,normalized_evidence,observed_at)
                VALUES(91001,91101,:rid,:oid,:iid,'avito-order-management','v1',:checksum,'{}',now())
                RETURNING observation_id"""),
                {"rid": rid, "oid": oid, "iid": iid, "checksum": "a" * 64},
            ).scalar_one()
            snapshot = c.execute(
                text("""INSERT INTO order_read_snapshots
                (organization_id,high_water_mark,account_scope,query_checksum,account_coverage,coverage_state,row_count)
                VALUES(91001,'test',:accounts,:checksum,'[]','partial',1) RETURNING snapshot_id"""),
                {
                    "accounts": [91102] if target == "wrong_account" else [91101],
                    "checksum": "a" * 64,
                },
            ).scalar_one()
            c.execute(
                text("""INSERT INTO order_read_snapshot_rows
                (organization_id,marketplace_account_id,snapshot_id,order_id,order_item_id,observation_id,
                 position,row_version,payload_schema_version,row_payload)
                VALUES(91001,91101,:snapshot,:oid,:iid,:obs,1,1,1,'{}')"""),
                {
                    "snapshot": snapshot,
                    "oid": oid,
                    "iid": other if target == "wrong_item" else iid,
                    "obs": obs,
                },
            )


@pytest.mark.parametrize("accounts", [[None], [91101, None], [0], [-1], []])
def test_snapshot_scope_cannot_contain_null_or_nonpositive_accounts(db, accounts):
    _, runtime = db
    with pytest.raises(IntegrityError):
        with runtime.begin() as c:
            scope(c)
            c.execute(
                text("""INSERT INTO order_read_snapshots
                (organization_id,high_water_mark,account_scope,query_checksum,account_coverage,coverage_state,row_count)
                VALUES(91001,'test',:accounts,:checksum,'[]','partial',0)"""),
                {"accounts": accounts, "checksum": "a" * 64},
            )


def test_actual_single_head_alembic_roundtrip(cluster):
    with disposable_database(cluster) as database:
        for action, revision in [("upgrade", "20260909_0062"), ("downgrade", "20260908_0061"), ("upgrade", "20260909_0062")]:
            result = migrate(database.url, action, revision)
            assert result.returncode == 0, result.stderr
        engine = create_engine(database.url)
        try:
            with engine.connect() as c:
                assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260909_0062"
                assert c.exec_driver_sql("SELECT count(*) FROM order_sync_runs").scalar_one() == 0
        finally:
            engine.dispose()
