"""Runtime permissions, immutable writes and observed two-session account races."""
# Explicit transaction scopes are proof boundaries.
# ruff: noqa: SIM117

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_exact_text_migration as exact
from tests import test_orders_run_binding_migration as binding
from tests import test_orders_schema_candidate as candidate
from tests.test_orders_schema_integration import runtime_script

cluster = candidate.cluster


@pytest.fixture(scope="module")
def db(cluster):
    role = "orders_binding_runtime_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        assert candidate.migrate(database.url, "upgrade", "20260909_0066").returncode == 0
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            exact.setup(owner, role)
            assert candidate.migrate(database.url, "upgrade", binding.REVISION).returncode == 0
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


@pytest.fixture(scope="module")
def latest_db(cluster):
    """Only the current runtime script follows head; binding feature stays 0067."""
    with exact.migrated_database(cluster, "head") as engines:
        yield engines


def read_stamp(c, rid):
    return dict(c.execute(text(f"SELECT {','.join(binding.FIELDS)} FROM order_sync_runs WHERE sync_run_id=:id"),
                          {"id": rid}).mappings().one())


def assert_safe(error, message):
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.message_primary == message
    assert "SYNTHETIC_CANARY" not in str(error.value.orig)
    assert error.value.orig.diag.message_detail is None


@pytest.mark.parametrize("state", ["staging", "partial", "complete"])
@pytest.mark.parametrize("field", binding.FIELDS)
def test_bound_fields_immutable_from_insert_in_every_state(db, state, field):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        rid = exact.make_run(c, **binding.stamp())
        if state == "complete":
            c.execute(text("UPDATE order_sync_runs SET state='complete',manifest_state='complete',completed_at=now(),page_count=1,expected_order_count=0,payload_checksum=:checksum WHERE sync_run_id=:id"),
                      {"id": rid, "checksum": "a" * 64})
        elif state == "partial":
            c.execute(text("UPDATE order_sync_runs SET state='partial',completed_at=now() WHERE sync_run_id=:id"), {"id": rid})
    value = {binding.FIELDS[0]: 2, binding.FIELDS[1]: "SYNTHETIC_CANARY_EXTERNAL",
             binding.FIELDS[2]: "SYNTHETIC_CANARY_REF", binding.FIELDS[3]: b"SYNTHETIC_CANARY_BYTES",
             binding.FIELDS[4]: "SYNTHETIC_CANARY_CHECKSUM"}[field]
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        c.execute(text(f"UPDATE order_sync_runs SET {field}=:value WHERE sync_run_id=:id"), {"id": rid, "value": value})
    assert_safe(error, "orders_run_binding_immutable")
    with runtime.begin() as c:
        candidate.scope(c)
        assert read_stamp(c, rid) == binding.stamp()


@pytest.mark.parametrize("direction", ["bind_legacy", "clear_bound"])
def test_legacy_cannot_be_relabelled_and_bound_cannot_be_cleared(db, direction):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        rid = exact.make_run(c, **(binding.stamp() if direction == "clear_bound" else {}))
    fields = {f: None for f in binding.FIELDS} if direction == "clear_bound" else binding.stamp()
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        c.execute(text("UPDATE order_sync_runs SET " + ",".join(f"{f}=:{f}" for f in fields) + " WHERE sync_run_id=:id"), dict(fields, id=rid))
    assert_safe(error, "orders_run_binding_immutable")


def test_unbound_updates_and_unchanged_bound_updates_preserve_old_contract(db):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        for stamp in ({}, binding.stamp()):
            rid = exact.make_run(c, **stamp)
            c.execute(text("UPDATE order_sync_runs SET page_count=2 WHERE sync_run_id=:id"), {"id": rid})
            expected = stamp or dict.fromkeys(binding.FIELDS)
            assert read_stamp(c, rid) == expected
            c.execute(text("UPDATE order_sync_runs SET state='partial',completed_at=now() WHERE sync_run_id=:id"), {"id": rid})
    with pytest.raises(DBAPIError), runtime.begin() as c:
        candidate.scope(c)
        c.execute(text("UPDATE order_sync_runs SET page_count=3 WHERE sync_run_id=:id"), {"id": rid})


@pytest.mark.parametrize("context", [None, "", "bad", "91002"])
def test_runtime_org_scope_denial(db, context):
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        if context is not None:
            candidate.scope(c, context)
        exact.make_run(c, **binding.stamp())
    assert error.value.orig.sqlstate in ("42501", "22P02", "23514")
    if context != "bad":
        with runtime.begin() as c:
            if context is not None:
                candidate.scope(c, context)
            assert c.exec_driver_sql("SELECT count(*) FROM order_sync_runs WHERE organization_id=91001").scalar_one() == 0


def test_same_org_second_account_read_retains_existing_org_rls(db):
    _, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        rid = exact.make_run(c, marketplace_account_id=91102,
                             **binding.stamp(account=91102, external="synthetic-b"))
        c.exec_driver_sql("SELECT set_config('app.marketplace_account_id','91101',true)")
        assert read_stamp(c, rid) == binding.stamp(account=91102, external="synthetic-b")


@pytest.mark.parametrize("change", ["external", "ref", "provider", "missing"])
def test_bound_insert_current_account_mismatch_is_safe(db, change):
    owner, _ = db
    values = binding.stamp(external="SYNTHETIC_CANARY_EXTERNAL", ref="SYNTHETIC_CANARY_REF")
    with pytest.raises(DBAPIError) as error, owner.begin() as c:
        if change == "external":
            values = binding.stamp(external="SYNTHETIC_CANARY_EXTERNAL")
        elif change == "ref":
            values = binding.stamp(ref="SYNTHETIC_CANARY_REF")
        elif change == "provider":
            values = binding.stamp(provider="wb")
        exact.make_run(c, marketplace="wb" if change == "provider" else "avito",
                       marketplace_account_id=99999 if change == "missing" else 91101,
                       **values)
    assert_safe(error, "orders_run_binding_mismatch")


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_bound_insert_requires_read_committed(db, isolation):
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.connect().execution_options(isolation_level=isolation) as c:
        with c.begin():
            candidate.scope(c)
            exact.make_run(c, **binding.stamp())
    assert error.value.orig.sqlstate == "25000"
    assert error.value.orig.diag.message_primary == "orders_isolation_invalid"


def observed_wait(owner, waiter_pid, blocker_pid):
    until = monotonic() + 5
    while monotonic() < until:
        with owner.connect() as observer:
            pids = observer.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}).scalar_one()
        if blocker_pid in pids:
            return
        sleep(0.01)
    pytest.fail("physical waiter did not block on the expected account-lock holder")


@pytest.mark.parametrize("winner", ["rebind", "insert"])
def test_actual_rebind_insert_lock_winners(db, winner):
    owner, runtime = db
    ready = Queue()
    key = uuid4().hex
    def waiter():
        try:
            with runtime.begin() as c:
                candidate.scope(c)
                c.exec_driver_sql("SET LOCAL lock_timeout='10s'")
                # Establish old statement snapshot before holder commits.
                c.exec_driver_sql("SELECT external_account_id FROM marketplace_accounts WHERE marketplace_account_id=91101")
                ready.put(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                if winner == "rebind":
                    exact.make_run(c, source_run_key=key, **binding.stamp())
                else:
                    c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='synthetic-rebound',credential_ref='synthetic-new-ref' WHERE marketplace_account_id=91101")
            return "committed"
        except DBAPIError as error:
            assert error.orig.sqlstate == "23514"
            assert error.orig.diag.message_primary == "orders_run_binding_mismatch"
            return "mismatch"

    try:
        with runtime.connect() as holder, ThreadPoolExecutor(max_workers=1) as pool:
            with holder.begin():
                candidate.scope(holder)
                pid = holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                if winner == "rebind":
                    holder.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='synthetic-rebound',credential_ref='synthetic-new-ref' WHERE marketplace_account_id=91101")
                else:
                    rid = exact.make_run(holder, source_run_key=key, **binding.stamp())
                    holder.execute(text("UPDATE order_sync_runs SET state='partial',completed_at=now() WHERE sync_run_id=:id"), {"id": rid})
                future = pool.submit(waiter)
                waiter_pid = ready.get(timeout=5)
                assert waiter_pid != pid
                observed_wait(owner, waiter_pid, pid)
            assert future.result(timeout=10) == ("mismatch" if winner == "rebind" else "committed")
        with owner.connect() as c:
            assert c.exec_driver_sql("SELECT external_account_id,credential_ref FROM marketplace_accounts WHERE marketplace_account_id=91101").one() == ("synthetic-rebound", "synthetic-new-ref")
            count = c.execute(text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"), {"key": key}).scalar_one()
            assert count == (0 if winner == "rebind" else 1)
            if winner == "insert":
                assert read_stamp(c, rid) == binding.stamp()
    finally:
        with owner.begin() as c:
            c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='synthetic-a',credential_ref=NULL WHERE marketplace_account_id=91101")


def test_binding_update_does_not_acquire_account_lock(db):
    owner, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        rid = exact.make_run(c, **binding.stamp())
    with owner.begin() as holder:
        holder.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=91101 FOR UPDATE")
        with runtime.begin() as c:
            candidate.scope(c)
            c.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            c.execute(text("UPDATE order_sync_runs SET page_count=1 WHERE sync_run_id=:id"), {"id": rid})


def test_account_created_after_missing_lock_lookup_is_not_admitted(db):
    """Deterministically expose the missing-row window in the installed guard.

    Only this disposable database's function is temporarily instrumented: an
    assignment waits on an advisory lock immediately after FOR UPDATE. Unlike
    PERFORM, assignment preserves FOUND; assertions inside the injected body
    verify that fact and that the lookup really missed. Production has no pause.
    """
    owner, runtime = db
    account = 91103
    key = uuid4().hex
    advisory_key = int(uuid4().hex[:15], 16)
    ready = Queue()
    expected = binding.stamp(account=account, external="synthetic-created-during-insert")
    with owner.begin() as c:
        assert c.execute(text("SELECT count(*) FROM marketplace_accounts WHERE marketplace_account_id=:id"), {"id": account}).scalar_one() == 0
        original = c.exec_driver_sql("SELECT pg_get_functiondef('public.orders_run_binding_insert_guard()'::regprocedure)").scalar_one()
        anchor = "AND marketplace_account_id=NEW.marketplace_account_id FOR UPDATE;"
        assert original.count(anchor) == 1
        assert original.count("DECLARE actual record;") == 1
        instrumented = original.replace("DECLARE actual record;", "DECLARE actual record; binding_test_found boolean; binding_test_pause text;", 1)
        instrumented = instrumented.replace(anchor, anchor + f"""
          binding_test_found := FOUND;
          binding_test_pause := pg_catalog.pg_advisory_xact_lock({advisory_key}::bigint)::text;
          IF FOUND IS DISTINCT FROM binding_test_found THEN
            RAISE EXCEPTION 'binding_test_found_changed';
          END IF;
          IF binding_test_found THEN
            RAISE EXCEPTION 'binding_test_expected_missing';
          END IF;
        """, 1)
        c.exec_driver_sql(instrumented)

    def insert_during_creation():
        try:
            with runtime.begin() as c:
                candidate.scope(c)
                c.exec_driver_sql("SET LOCAL lock_timeout='10s'")
                ready.put(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                exact.make_run(c, marketplace_account_id=account, source_run_key=key, **expected)
            return "admitted"
        except DBAPIError as error:
            assert error.orig.sqlstate == "23514"
            assert error.orig.diag.message_primary == "orders_run_binding_mismatch"
            return "rejected"

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with owner.begin() as gate:
                gate.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key})
                gate_pid = gate.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                future = pool.submit(insert_during_creation)
                insert_pid = ready.get(timeout=5)
                assert insert_pid != gate_pid
                observed_wait(owner, insert_pid, gate_pid)
                # The blocked assignment proves the row-lock lookup already
                # missed. Commit matching metadata before releasing that pause.
                with owner.begin() as creator:
                    assert creator.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() not in (insert_pid, gate_pid)
                    creator.execute(text("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES (:id,91001,'avito','synthetic-created-during-insert','connected')"), {"id": account})
            outcome = future.result(timeout=10)
        assert outcome == "rejected", "fresh SELECT admitted an account missed by the lock lookup"
        with owner.connect() as c:
            assert c.execute(text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"), {"key": key}).scalar_one() == 0
    finally:
        with owner.begin() as c:
            c.exec_driver_sql(original)
    # Once visible at the actual lookup, this account is a valid positive control.
    with runtime.begin() as c:
        candidate.scope(c)
        rid = exact.make_run(c, marketplace_account_id=account, **expected)
        assert read_stamp(c, rid) == expected


def privileges(c):
    return {
        "tables": c.exec_driver_sql("SELECT oid,relacl::text FROM pg_class WHERE relnamespace='public'::regnamespace ORDER BY oid").all(),
        "columns": c.exec_driver_sql("SELECT attrelid,attnum,attacl::text FROM pg_attribute WHERE attacl IS NOT NULL ORDER BY 1,2").all(),
        "defaults": c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all(),
        "functions": c.execute(text("SELECT oid,proacl::text FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname NOT LIKE 'orders_binding_%' AND proname NOT LIKE 'orders_run_binding_%' ORDER BY oid")).all(),
    }


def test_narrow_helper_acl_preserves_old_table_column_default_rights(cluster):
    roles = ["orders_binding_acl_" + uuid4().hex for _ in range(4)]
    writer, reader, column_writer, stranger = roles
    with candidate.disposable_database(cluster, roles) as database:
        assert candidate.migrate(database.url, "upgrade", "20260909_0066").returncode == 0
        owner = create_engine(database.url, hide_parameters=True)
        try:
            exact.setup(owner, writer)
            with owner.begin() as c:
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {reader},{column_writer},{stranger}")
                c.exec_driver_sql(f"GRANT SELECT (sync_run_id) ON order_sync_runs TO {reader}")
                c.exec_driver_sql(f"GRANT UPDATE (page_count) ON order_sync_runs TO {column_writer}")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO {stranger} WITH GRANT OPTION")
                before = privileges(c)
            result = candidate.migrate(database.url, "upgrade", binding.REVISION)
            assert result.returncode == 0, result.stderr
            with owner.connect() as c:
                assert privileges(c) == before
                for role in roles:
                    for helper in binding.HELPERS:
                        assert c.execute(text("SELECT has_function_privilege(:role,:helper,'EXECUTE')"),
                                         {"role": role, "helper": "public." + helper}).scalar_one() == (role in (writer, column_writer))
                        assert not c.execute(text("SELECT has_function_privilege(:role,:helper,'EXECUTE WITH GRANT OPTION')"),
                                             {"role": role, "helper": "public." + helper}).scalar_one()
                    for helper in ("orders_run_binding_insert_guard()", "orders_run_binding_update_guard()"):
                        assert not c.execute(text("SELECT has_function_privilege(:role,:helper,'EXECUTE')"),
                                             {"role": role, "helper": "public." + helper}).scalar_one()
                assert not c.execute(text("SELECT has_column_privilege(:role,'order_sync_runs','account_binding_payload','SELECT')"), {"role": reader}).scalar_one()
            with owner.begin() as c:
                rid = exact.make_run(c, **binding.stamp())
                c.exec_driver_sql(f"SET LOCAL ROLE {reader}")
                candidate.scope(c)
                assert c.execute(text("SELECT sync_run_id FROM order_sync_runs WHERE sync_run_id=:id"), {"id": rid}).scalar_one() == rid
            with pytest.raises(DBAPIError) as error, owner.begin() as c:
                c.exec_driver_sql(f"SET LOCAL ROLE {stranger}")
                c.exec_driver_sql("SELECT public.orders_binding_ascii_string('synthetic')")
            assert error.value.orig.sqlstate == "42501"
            with owner.begin() as c:
                c.exec_driver_sql(f"SET LOCAL ROLE {column_writer}")
                candidate.scope(c)
                assert c.exec_driver_sql("UPDATE order_sync_runs SET page_count=7").rowcount == 1
            with owner.connect() as c:
                assert c.execute(text("SELECT page_count FROM order_sync_runs WHERE sync_run_id=:id"), {"id": rid}).scalar_one() == 7
                assert read_stamp(c, rid) == binding.stamp()
        finally:
            owner.dispose()


def test_actual_runtime_script_new_helper_grants_and_atomic_failure(latest_db):
    owner, runtime = latest_db
    result = runtime_script(owner, runtime.url.username)
    assert result.returncode == 0, result.stderr
    with runtime.begin() as c:
        candidate.scope(c)
        rid = exact.make_run(c, **binding.stamp())
        assert read_stamp(c, rid) == binding.stamp()
    with owner.connect() as c:
        before = privileges(c)
        new_acl = c.execute(text("SELECT oid,proacl::text FROM pg_proc WHERE proname LIKE 'orders_binding_%' OR proname LIKE 'orders_run_binding_%' ORDER BY oid")).all()
    result = runtime_script(owner, runtime.url.username, fail_after_broad=True)
    assert result.returncode != 0 and "division by zero" in result.stderr
    with owner.connect() as c:
        assert privileges(c) == before
        assert c.execute(text("SELECT oid,proacl::text FROM pg_proc WHERE proname LIKE 'orders_binding_%' OR proname LIKE 'orders_run_binding_%' ORDER BY oid")).all() == new_acl
