"""Exact unbounded Orders identities on fresh, disposable PostgreSQL only."""
# Separate transaction boundaries are part of the assertions.
# ruff: noqa: SIM117

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import Script, ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster
OWNER = {"organization_id": 91001, "marketplace_account_id": 91101}
SOURCE = "avito-order-management"
CASES = (
    ("order_sync_runs", "source_run_key", "sync_run_id"),
    ("marketplace_orders", "external_order_id", "order_id"),
    ("marketplace_order_items", "source_line_key", "order_item_id"),
    ("order_observations", "adapter_version", "observation_id"),
    ("order_observations", "adapter_version", "observation_id"),
    ("order_lifecycle_events", "source_event_key", "lifecycle_event_id"),
    ("order_deadlines", "deadline_kind", "deadline_id"),
    ("order_sync_coverage", "coverage_kind", "coverage_id"),
)
LOGICAL = (
    "order_sync_runs_organization_id_marketplace_account_id_sour_key",
    "marketplace_orders_organization_id_marketplace_account_id_e_key",
    "marketplace_order_items_organization_id_marketplace_account_key",
    "uq_orders_observation_order_replay",
    "uq_orders_observation_item_replay",
    "order_lifecycle_events_organization_id_marketplace_account__key",
    "order_deadlines_organization_id_marketplace_account_id_obse_key",
    "order_sync_coverage_organization_id_marketplace_account_id__key",
)


def long_key(label):
    return label + "".join(hashlib.sha256(str(i).encode("ascii")).hexdigest() for i in range(64))


def insert(c, table, values, returning):
    return c.execute(text(f"INSERT INTO {table} ({','.join(values)}) "
                          f"VALUES ({','.join(':' + key for key in values)}) RETURNING {returning}"),
                     values).scalar_one()


def make_run(c, **changes):
    values = dict(OWNER, marketplace="avito", source_kind=SOURCE, source_run_key=uuid4().hex,
                  adapter_version="v1", mapping_version="v1", source_contract_version="v1",
                  source_snapshot="synthetic")
    values.update(changes)
    return insert(c, "order_sync_runs", values, "sync_run_id")


def parents(c, index, key):
    """Create parents before the direct guarded insert, including long adapter run."""
    table, column, returning = CASES[index]
    if index == 0:
        values = dict(OWNER, marketplace="avito", source_kind=SOURCE,
                      adapter_version="v1", mapping_version="v1", source_contract_version="v1",
                      source_snapshot="synthetic")
    elif index == 1:
        values = dict(OWNER, marketplace="avito")
    else:
        rid = make_run(c, adapter_version=key if index in (3, 4) else "v1")
        oid = candidate.order(c)
        if index == 2:
            values = dict(OWNER, order_id=oid, quantity=1, resolution_version="v1")
        elif index in (3, 4):
            values = dict(OWNER, sync_run_id=rid, order_id=oid, source_kind=SOURCE,
                          payload_checksum="a" * 64, normalized_evidence="{}",
                          observed_at="2026-09-09T00:00:00Z",
                          order_item_id=candidate.item(c, oid) if index == 4 else None)
        elif index in (5, 6):
            obs = candidate.observation(c, oid, rid)
            values = dict(OWNER, order_id=oid, observation_id=obs,
                          observed_at="2026-09-09T00:00:00Z")
            if index == 5:
                values.update(event_kind="status_changed", evidence="{}")
            else:
                values.update(source_deadline_at="2026-09-10T00:00:00Z",
                              timezone="UTC", evidence_source=SOURCE)
        else:
            values = dict(OWNER, sync_run_id=rid, is_complete=False)
    values[column] = key
    return table, column, returning, values


def setup(owner, role):
    with owner.begin() as c:
        c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
        c.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {role}")
        c.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
        c.exec_driver_sql("INSERT INTO lk_organizations(organization_id,slug,name) VALUES "
                         "(91001,'exact-one','Synthetic'),(91002,'exact-two','Synthetic')")
        c.exec_driver_sql("""INSERT INTO marketplace_accounts
            (marketplace_account_id,organization_id,marketplace,external_account_id,status)
            VALUES (91101,91001,'avito','synthetic-a','connected'),
                   (91102,91001,'avito','synthetic-b','connected'),
                   (91201,91002,'wb','synthetic-c','connected')""")


@contextmanager
def migrated_database(cluster, target):
    role = "orders_exact_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        result = candidate.migrate(database.url, "upgrade", target)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            setup(owner, role)
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


@pytest.fixture(scope="module")
def db(cluster):
    """Historical feature and catalog contract remains bound to 0064."""
    with migrated_database(cluster, "20260909_0064") as engines:
        yield engines


@pytest.fixture(scope="module")
def latest_db(cluster):
    """Only the actual current runtime script consumes the latest schema."""
    with migrated_database(cluster, "head") as engines:
        yield engines


@pytest.mark.parametrize("index", range(8))
def test_long_exact_identity_roundtrip(db, index):
    _, runtime = db
    key = long_key("synthetic:")
    state = None
    try:
        with runtime.begin() as c:
            candidate.scope(c)
            table, column, returning, values = parents(c, index, key)
            identity = insert(c, table, values, returning)
            assert c.execute(text(f"SELECT {column} FROM {table} WHERE {returning}=:id"),
                             {"id": identity}).scalar_one() == key
    except DBAPIError as error:
        state = error.orig.sqlstate
    assert state is None, f"long exact identity rejected: SQLSTATE {state}"


def safe_error(error, state, constraint=None, key=None):
    assert error.value.orig.sqlstate == state
    if constraint:
        assert error.value.orig.diag.constraint_name == constraint
        assert error.value.orig.diag.message_primary == "orders_identity_conflict"
    if key:
        assert key not in str(error.value.orig)
        assert key[:40] not in str(error.value.orig)


@pytest.mark.parametrize("index", range(8))
def test_distinct_keys_and_exact_duplicate_have_safe_logical_conflict(db, index):
    _, runtime = db
    key = long_key(uuid4().hex + ":A:")
    with runtime.begin() as c:
        candidate.scope(c)
        table, column, returning, values = parents(c, index, key)
        first = insert(c, table, values, returning)
        different = dict(values, **{column: key.replace(":A:", ":a:")})
        if index in (3, 4):
            different["sync_run_id"] = make_run(c, adapter_version=different[column])
        assert insert(c, table, different, returning) != first
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        insert(c, table, values, returning)
    safe_error(error, "23505", LOGICAL[index], key)


def test_observation_null_scope_checksum_and_run_independent_replay(db):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        table, _, pk, values = parents(c, 3, long_key(uuid4().hex))
        first = insert(c, table, values, pk)
        iid = candidate.item(c, values["order_id"])
        other_iid = candidate.item(c, values["order_id"])
        assert insert(c, table, dict(values, order_item_id=iid), pk) != first
        insert(c, table, dict(values, order_item_id=other_iid), pk)
        changed = dict(values, payload_checksum="b" * 64, normalized_evidence='{"status":"changed"}')
        changed_id = insert(c, table, changed, pk)
        assert c.execute(text("SELECT normalized_evidence FROM order_observations WHERE observation_id=:id"),
                         {"id": changed_id}).scalar_one() == {"status": "changed"}
        another_run = make_run(c, adapter_version=values["adapter_version"])
    for replay in (dict(values, sync_run_id=another_run), dict(values, order_item_id=iid)):
        with pytest.raises(DBAPIError) as error, runtime.begin() as c:
            candidate.scope(c)
            insert(c, table, replay, pk)
        safe_error(error, "23505", LOGICAL[4 if replay["order_item_id"] else 3])


@pytest.mark.parametrize("index", range(2, 8))
def test_identical_text_under_distinct_parent_is_not_collapsed(db, index):
    _, runtime = db
    key = long_key(uuid4().hex)
    with runtime.begin() as c:
        candidate.scope(c)
        table, _, pk, first = parents(c, index, key)
        _, _, _, second = parents(c, index, key)
        assert insert(c, table, first, pk) != insert(c, table, second, pk)


@pytest.mark.parametrize("index", [3, 4, 5, 6])
def test_source_and_observation_parts_of_logical_identity_remain_distinct(db, index):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        table, _, pk, values = parents(c, index, long_key(uuid4().hex))
        first = insert(c, table, values, pk)
        if index in (3, 4):
            values["source_kind"] = "avito-browser"
            values["sync_run_id"] = make_run(c, source_kind="avito-browser", adapter_version=values["adapter_version"])
        else:
            values["observation_id"] = candidate.observation(c, values["order_id"], make_run(c), checksum="b" * 64)
        assert insert(c, table, values, pk) != first


@pytest.mark.parametrize("change", ["adapter", "source", "account", "organization", "item_owner", "provider"])
def test_source_adapter_and_ownership_cannot_be_substituted(db, change):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        table, _, pk, values = parents(c, 3, long_key(uuid4().hex))
        if change == "adapter":
            values["adapter_version"] += ":different"
        elif change == "source":
            values["source_kind"] = "avito-browser"
        elif change == "account":
            values["sync_run_id"] = candidate.run(c, account=91102)
        elif change == "organization":
            candidate.scope(c, 91002)
            values["sync_run_id"] = candidate.run(c, account=91201, org=91002)
        elif change == "item_owner":
            values["order_item_id"] = candidate.item(c, candidate.order(c))
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        if change == "provider":
            make_run(c, marketplace="wb")
        else:
            insert(c, table, values, pk)
    assert error.value.orig.sqlstate in {"23503", "23514"}


@pytest.mark.parametrize("context", [None, 91002])
def test_missing_and_wrong_context_fail_closed(db, context):
    _, runtime = db
    with runtime.begin() as c:
        if context is not None:
            candidate.scope(c, context)
        assert c.exec_driver_sql("SELECT count(*) FROM marketplace_orders WHERE organization_id=91001").scalar_one() == 0
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        if context is not None:
            candidate.scope(c, context)
        candidate.order(c)
    safe_error(error, "42501")


@pytest.mark.parametrize("index", range(8))
def test_immutable_keys_cannot_bypass_insert_guard(db, index):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        table, column, pk, values = parents(c, index, long_key(uuid4().hex))
        identity = insert(c, table, values, pk)
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        extra = ",version=version+1" if index in (1, 2) else ""
        c.execute(text(f"UPDATE {table} SET {column}=:key{extra} WHERE {pk}=:id"),
                  {"key": long_key("changed:"), "id": identity})
    assert "immutable" in error.value.orig.diag.message_primary.lower()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
@pytest.mark.parametrize("index", range(8))
def test_non_rc_insert_rejected_without_partial_rows(db, isolation, index):
    owner, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        table, _, pk, values = parents(c, index, long_key(uuid4().hex))
    before = rows(owner)
    with pytest.raises(DBAPIError) as error, runtime.connect().execution_options(isolation_level=isolation) as c:
        with c.begin():
            candidate.scope(c)
            c.exec_driver_sql("SELECT count(*) FROM marketplace_orders")
            insert(c, table, values, pk)
    safe_error(error, "25000")
    assert error.value.orig.diag.message_primary == "orders_isolation_invalid"
    assert rows(owner) == before


@pytest.mark.parametrize("index", range(8))
def test_account_wait_refreshes_snapshot_before_direct_insert(db, index):
    owner, runtime = db
    key = long_key(uuid4().hex)
    with runtime.begin() as c:
        candidate.scope(c)
        table, _, pk, values = parents(c, index, key)
    ready = Queue()

    def waiter():
        with runtime.begin() as c:
            candidate.scope(c)
            c.exec_driver_sql("SET LOCAL lock_timeout='10s'")
            c.exec_driver_sql(f"SELECT count(*) FROM {table}")
            ready.put(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            with pytest.raises(DBAPIError) as error:
                insert(c, table, values, pk)
            safe_error(error, "23505", LOGICAL[index], key)
            c.rollback()

    with runtime.connect() as winner, ThreadPoolExecutor(max_workers=1) as pool:
        with winner.begin():
            candidate.scope(winner)
            winner.exec_driver_sql(f"SELECT count(*) FROM {table}")
            # Hold the account directly; parents already committed. Both INSERTs
            # themselves run the guard, with no preparatory helper holding it.
            winner.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE organization_id=91001 AND marketplace_account_id=91101 FOR UPDATE")
            future = pool.submit(waiter)
            waiter_pid = ready.get(timeout=5)
            until = monotonic() + 5
            waiting = False
            while monotonic() < until:
                with owner.connect() as observer:
                    waiting = observer.execute(text("SELECT cardinality(pg_blocking_pids(:pid))>0"),
                                               {"pid": waiter_pid}).scalar_one()
                if waiting:
                    break
                sleep(0.01)
            assert waiting, "second physical connection did not wait on the account lock"
            insert(winner, table, values, pk)
        future.result(timeout=10)


def rows(owner):
    with owner.connect() as c:
        return {table: c.exec_driver_sql(f"SELECT to_jsonb(t) FROM {table} t ORDER BY {list_pk(table)}").all()
                for table in candidate.TABLES}


def list_pk(table):
    return {"order_sync_runs": "sync_run_id", "marketplace_orders": "order_id",
            "marketplace_order_items": "order_item_id", "order_observations": "observation_id",
            "order_status_observations": "status_observation_id", "order_lifecycle_events": "lifecycle_event_id",
            "order_deadlines": "deadline_id", "order_sync_coverage": "coverage_id",
            "order_sync_memberships": "membership_id", "order_read_snapshots": "snapshot_id",
            "order_read_snapshot_rows": "snapshot_row_id"}[table]


def test_duplicate_rolls_back_complete_evidence_transaction(db):
    owner, runtime = db
    before = rows(owner)
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        complete_evidence(c)
        for index in range(8):
            table, _, pk, values = parents(c, index, long_key(uuid4().hex))
            insert(c, table, values, pk)
        insert(c, table, values, pk)
    safe_error(error, "23505", LOGICAL[7])
    assert rows(owner) == before


def schema(c):
    return {
        "constraints": c.execute(text("SELECT conrelid::regclass::text,conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid::regclass::text=ANY(:tables) ORDER BY 1,2"), {"tables": list(candidate.TABLES)}).all(),
        "indexes": c.execute(text("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE tablename=ANY(:tables) ORDER BY 1,2"), {"tables": list(candidate.TABLES)}).all(),
        "access": c.execute(text("SELECT relname,relowner,relacl::text,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=ANY(:tables) ORDER BY 1"), {"tables": list(candidate.TABLES)}).all(),
        "policies": c.execute(text("SELECT tablename,policyname,roles,cmd,qual,with_check FROM pg_policies WHERE tablename=ANY(:tables) ORDER BY 1,2"), {"tables": list(candidate.TABLES)}).all(),
        "triggers": c.execute(text("SELECT tgrelid::regclass::text,tgname,pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal AND tgrelid::regclass::text=ANY(:tables) ORDER BY 1,2"), {"tables": list(candidate.TABLES)}).all(),
        "sequences": c.execute(text("SELECT s.relname,s.relowner,s.relacl::text FROM pg_class s JOIN pg_depend d ON d.objid=s.oid AND d.classid='pg_class'::regclass AND d.deptype='i' JOIN pg_class t ON t.oid=d.refobjid WHERE s.relkind='S' AND t.relname=ANY(:tables) ORDER BY 1"), {"tables": list(candidate.TABLES)}).all(),
    }


def migration_scripts():
    config = Config(str(candidate.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(candidate.ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def revision():
    return migration_scripts().get_revision("20260909_0064").module


def assert_orders_ancestry(scripts):
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert "20260909_0064" in {
        item.revision for item in scripts.iterate_revisions(heads[0], "base")
    }


def test_current_chain_includes_orders_amendment():
    assert_orders_ancestry(migration_scripts())


def test_historical_revision_accepts_synthetic_successor(monkeypatch):
    # Only the in-memory revision map changes: no migration files, env.py or DB.
    scripts = migration_scripts()
    expected = scripts.get_revision("20260909_0064").module
    successor = Script(SimpleNamespace(down_revision=scripts.get_current_head(),
                       branch_labels=None, depends_on=None), "synthetic_orders_successor", "<in-memory>")
    scripts.revision_map.add_revision(successor)
    monkeypatch.setattr(ScriptDirectory, "from_config", lambda config: scripts)
    assert revision() is expected
    assert_orders_ancestry(migration_scripts())


def test_empty_roundtrip_restores_exact_schema_and_acl(cluster):
    role = "orders_roundtrip_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        assert candidate.migrate(database.url, "upgrade", "20260909_0063").returncode == 0
        engine = create_engine(database.url)
        try:
            setup(engine, role)
            with engine.connect() as c:
                original = schema(c)
            assert candidate.migrate(database.url, "upgrade", "20260909_0064").returncode == 0
            with engine.connect() as c:
                upgraded = schema(c)
                assert upgraded["access"] == original["access"]
                assert upgraded["policies"] == original["policies"]
                assert upgraded["sequences"] == original["sequences"]
            assert candidate.migrate(database.url, "downgrade", "20260909_0063").returncode == 0
            with engine.connect() as c:
                assert schema(c) == original
            assert candidate.migrate(database.url, "upgrade", "20260909_0064").returncode == 0
            with engine.connect() as c:
                assert schema(c) == upgraded
        finally:
            engine.dispose()


def test_populated_upgrade_preserves_rows_and_nonempty_downgrade_is_atomic(cluster):
    role = "orders_parity_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        assert candidate.migrate(database.url, "upgrade", "20260909_0063").returncode == 0
        engine = create_engine(database.url)
        try:
            setup(engine, role)
            with engine.begin() as c:
                complete_evidence(c)
                for index in range(8):
                    table, _, pk, values = parents(c, index, uuid4().hex)
                    insert(c, table, values, pk)
            original = rows(engine)
            assert all(original.values()), "parity must include all eleven Orders relations"
            assert candidate.migrate(database.url, "upgrade", "20260909_0064").returncode == 0
            assert rows(engine) == original
            with engine.connect() as c:
                before = schema(c)
            result = candidate.migrate(database.url, "downgrade", "20260909_0063")
            assert result.returncode != 0 and "orders_downgrade_nonempty" in result.stderr
            assert rows(engine) == original
            with engine.connect() as c:
                assert schema(c) == before
                assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260909_0064"
            # FORCE RLS plus row_security=off must fail closed even when an
            # owner-level actor cannot see the populated organization.
            with engine.begin() as c:
                for table in candidate.TABLES:
                    c.exec_driver_sql(f"ALTER TABLE {table} OWNER TO {role}")
            with engine.connect() as c:
                before = schema(c)
            with pytest.raises(DBAPIError) as error, engine.begin() as c:
                c.exec_driver_sql(f"SET LOCAL ROLE {role}")
                candidate.scope(c, 91002)
                assert c.exec_driver_sql("SELECT count(*) FROM marketplace_orders").scalar_one() == 0
                with Operations.context(MigrationContext.configure(c)):
                    revision().downgrade()
            safe_error(error, "42501")
            with engine.connect() as c:
                assert schema(c) == before
                assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260909_0064"
            assert rows(engine) == original
        finally:
            engine.dispose()


def complete_evidence(c):
    rid, oid = make_run(c), candidate.order(c)
    iid = candidate.item(c, oid)
    obs = candidate.observation(c, oid, rid)
    common = dict(OWNER, order_id=oid, observation_id=obs, observed_at="2026-09-09T00:00:00Z")
    insert(c, "order_status_observations", dict(common, mapping_version="v1", evidence_source=SOURCE), "status_observation_id")
    insert(c, "order_lifecycle_events", dict(common, source_event_key=uuid4().hex, event_kind="status_changed", evidence='{"kind":"synthetic"}'), "lifecycle_event_id")
    insert(c, "order_deadlines", dict(common, deadline_kind="synthetic", source_deadline_at="2026-09-10T00:00:00Z", timezone="UTC", evidence_source=SOURCE), "deadline_id")
    insert(c, "order_sync_coverage", dict(OWNER, sync_run_id=rid, coverage_kind="synthetic", is_complete=False), "coverage_id")
    insert(c, "order_sync_memberships", dict(common, sync_run_id=rid, coverage_role="observed"), "membership_id")
    snapshot = insert(c, "order_read_snapshots", {"organization_id": 91001, "high_water_mark": "synthetic",
                      "account_scope": [91101], "query_checksum": "a" * 64, "account_coverage": "[]",
                      "coverage_state": "partial", "row_count": 1}, "snapshot_id")
    insert(c, "order_read_snapshot_rows", dict(OWNER, snapshot_id=snapshot, order_id=oid, order_item_id=iid,
           observation_id=obs, position=1, row_version=1, payload_schema_version=1,
           row_payload='{"synthetic":true}'), "snapshot_row_id")


@pytest.mark.parametrize("field,value", [("adapter_version", "different"), ("source_kind", "avito-browser")])
def test_run_source_binding_is_immutable(db, field, value):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        rid = make_run(c)
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        c.execute(text(f"UPDATE order_sync_runs SET {field}=:value WHERE sync_run_id=:id"),
                  {"value": value, "id": rid})
    assert "immutable" in error.value.orig.diag.message_primary.lower()


def test_same_exact_key_is_distinct_across_owner_account_and_source(db):
    _, runtime = db
    key = long_key(uuid4().hex)
    with runtime.begin() as c:
        candidate.scope(c)
        first = candidate.order(c, external=key)
        assert candidate.order(c, account=91102, external=key) != first
        run_a = make_run(c, source_run_key=key)
        assert make_run(c, source_run_key=key, source_kind="avito-browser") != run_a
        assert make_run(c, source_run_key=key, marketplace_account_id=91102) != run_a
        candidate.scope(c, 91002)
        assert candidate.order(c, account=91201, org=91002, external=key) != first
        assert make_run(c, organization_id=91002, marketplace_account_id=91201,
                        marketplace="wb", source_kind="wb-statistics-supplier-orders",
                        source_run_key=key) != run_a


def test_remaining_text_index_keys_are_only_finite_source_literals(db):
    owner, _ = db
    with owner.connect() as c:
        indexed = c.execute(text("""SELECT t.relname,a.attname
            FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
            JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(i.indkey)
            WHERE t.relname=ANY(:tables) AND a.atttypid='text'::regtype
            ORDER BY 1,2"""), {"tables": list(candidate.TABLES)}).all()
        assert indexed == [("order_sync_runs", "source_kind")]
        functions = c.execute(text("SELECT provolatile,prosecdef FROM pg_proc WHERE proname LIKE :prefix"),
                              {"prefix": "orders_exact_insert_%"}).all()
        assert len(functions) == 7 and all(row == ("v", False) for row in functions)
        assert not c.execute(text("SELECT EXISTS (SELECT 1 FROM pg_index WHERE indrelid::regclass::text=ANY(:tables) AND indexprs IS NOT NULL)"),
                             {"tables": list(candidate.TABLES)}).scalar_one()


@pytest.mark.parametrize("drift", ["unique", "foreign_key", "partial_index"])
def test_upgrade_refuses_changed_old_shape_atomically(cluster, drift):
    with candidate.disposable_database(cluster) as database:
        assert candidate.migrate(database.url, "upgrade", "20260909_0063").returncode == 0
        engine = create_engine(database.url)
        try:
            with engine.begin() as c:
                if drift == "unique":
                    c.exec_driver_sql(f"ALTER TABLE marketplace_orders DROP CONSTRAINT {LOGICAL[1]}")
                    c.exec_driver_sql(f"ALTER TABLE marketplace_orders ADD CONSTRAINT {LOGICAL[1]} UNIQUE(organization_id,external_order_id)")
                elif drift == "foreign_key":
                    name = "order_observations_organization_id_marketplace_account_id__fkey"
                    c.exec_driver_sql(f"ALTER TABLE order_observations DROP CONSTRAINT {name}")
                    c.exec_driver_sql(f"ALTER TABLE order_observations ADD CONSTRAINT {name} FOREIGN KEY(sync_run_id) REFERENCES order_sync_runs(sync_run_id)")
                else:
                    c.exec_driver_sql(f"DROP INDEX {LOGICAL[3]}")
                    c.exec_driver_sql(f"CREATE UNIQUE INDEX {LOGICAL[3]} ON order_observations(organization_id,marketplace_account_id,order_id,source_kind,adapter_version,payload_checksum)")
                before = schema(c)
            result = candidate.migrate(database.url, "upgrade", "20260909_0064")
            assert result.returncode != 0 and "orders_schema_drift" in result.stderr
            with engine.connect() as c:
                assert schema(c) == before
                assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260909_0063"
        finally:
            engine.dispose()


def test_latest_runtime_grants_support_guarded_exact_inserts(latest_db):
    from tests.test_orders_schema_integration import (
        assert_narrow_privileges,
        runtime_script,
    )

    owner, runtime = latest_db
    result = runtime_script(owner, runtime.url.username)
    assert result.returncode == 0, result.stderr
    with owner.connect() as c:
        assert_narrow_privileges(c, runtime.url.username)
        accounts_before = c.exec_driver_sql("SELECT to_jsonb(a) FROM marketplace_accounts a ORDER BY marketplace_account_id").all()
    key = long_key(uuid4().hex)
    with runtime.begin() as c:
        candidate.scope(c)
        candidate.order(c, external=key)
        for index in range(8):
            table, _, pk, values = parents(c, index, long_key(uuid4().hex))
            insert(c, table, values, pk)
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        candidate.order(c, external=key)
    safe_error(error, "23505", LOGICAL[1], key)
    with owner.connect() as c:
        assert c.exec_driver_sql("SELECT to_jsonb(a) FROM marketplace_accounts a ORDER BY marketplace_account_id").all() == accounts_before
