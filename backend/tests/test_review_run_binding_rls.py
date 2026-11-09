"""Native runtime RLS, from-INSERT immutability and observed account races."""
# Transaction boundaries are part of the assertions.
# ruff: noqa: SIM117

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as legacy
from tests import test_review_run_binding_migration as binding
from tests.test_review_lossless_migration import migrated

cluster = candidate.cluster


def old_writer(c, role):
    """The historical run writer; parent authority is explicit fixture setup."""
    columns = (*legacy.RUN_COLUMNS, "source_run_id_utf8", "coverage_utf8")
    c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
    c.exec_driver_sql(f"GRANT SELECT,UPDATE ON review_sync_runs_v2 TO {role}")
    c.exec_driver_sql(f"GRANT INSERT ({','.join(columns)}) ON review_sync_runs_v2 TO {role}")
    c.exec_driver_sql(f"GRANT USAGE ON SEQUENCE review_sync_runs_v2_run_sequence_seq TO {role}")
    c.exec_driver_sql(f"GRANT SELECT,UPDATE ON marketplace_accounts TO {role}")
    c.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION review_strict_utf8(bytea),review_coverage_json_object_utf8(bytea) TO {role}")


@pytest.fixture(scope="module")
def db(cluster):
    role = "review_binding_runtime_" + uuid4().hex
    with migrated(cluster, "20260909_0067", (role,)) as database:
        with database.owner.begin() as c:
            old_writer(c, role)
        result = candidate.migrate(database.database.url, "upgrade", binding.REVISION)
        assert result.returncode == 0, result.stderr
        runtime = create_engine(database.owner.url.set(username=role), hide_parameters=True)
        try:
            yield database.owner, runtime
        finally:
            runtime.dispose()


def read_stamp(c, rid):
    return dict(c.execute(text(f"SELECT {','.join(binding.FIELDS)} FROM review_sync_runs_v2 WHERE sync_run_id=:id"), {"id": rid}).mappings().one())


def safe(error, message):
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.message_primary == message
    assert error.value.orig.diag.message_detail is None
    assert "SYNTHETIC_CANARY" not in str(error.value.orig)


@pytest.mark.parametrize("state", ["running", "partial", "complete", "failed"])
@pytest.mark.parametrize("field", binding.FIELDS)
def test_fields_immutable_from_insert_empty_and_terminal(db, state, field):
    owner, runtime = db
    with runtime.begin() as c:
        candidate.scope(c)
        rid, _ = legacy.run(c, **binding.stamp())
        if state != "running":
            c.execute(text("UPDATE review_sync_runs_v2 SET status=:state,completed_at=now(),manifest_checksum=:hash WHERE sync_run_id=:id"), {"state": state, "hash": "a" * 64, "id": rid})
    value = {binding.FIELDS[0]: 2, binding.FIELDS[1]: "SYNTHETIC_CANARY_EXTERNAL",
        binding.FIELDS[2]: "SYNTHETIC_CANARY_REF", binding.FIELDS[3]: b"SYNTHETIC_CANARY_BYTES",
        binding.FIELDS[4]: "SYNTHETIC_CANARY_HASH"}[field]
    # Privileged ordinary DML and actual runtime both hit the immutable trigger.
    for engine in (owner, runtime):
        with pytest.raises(DBAPIError) as error, engine.begin() as c:
            candidate.scope(c)
            c.execute(text(f"UPDATE review_sync_runs_v2 SET {field}=:value WHERE sync_run_id=:id"), {"value": value, "id": rid})
        safe(error, "review_run_binding_immutable")
    with runtime.begin() as c:
        candidate.scope(c)
        assert read_stamp(c, rid) == binding.stamp()


@pytest.mark.parametrize("clear", [False, True])
def test_no_legacy_promotion_or_bound_clearing(db, clear):
    runtime = db[1]
    with runtime.begin() as c:
        candidate.scope(c)
        rid, _ = legacy.run(c, **(binding.stamp() if clear else {}))
    values = dict.fromkeys(binding.FIELDS) if clear else binding.stamp()
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        candidate.scope(c)
        c.execute(text("UPDATE review_sync_runs_v2 SET " + ",".join(f"{f}=:{f}" for f in values) + " WHERE sync_run_id=:id"), dict(values, id=rid))
    safe(error, "review_run_binding_immutable")


def test_unchanged_legacy_and_bound_updates_without_account_inversion(db):
    owner, runtime = db
    ids = []
    with runtime.begin() as c:
        candidate.scope(c)
        for fields in ({}, binding.stamp()):
            ids.append(legacy.run(c, **fields)[0])
    with owner.begin() as holder:
        holder.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=91101 FOR UPDATE")
        for rid in ids:
            for assignment in ("observed_count=2", "observed_count=observed_count"):
                with runtime.begin() as c:
                    candidate.scope(c)
                    c.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                    assert c.execute(text(f"UPDATE review_sync_runs_v2 SET {assignment} WHERE sync_run_id=:id"), {"id": rid}).rowcount == 1


@pytest.mark.parametrize("target", ["20260909_0067", binding.REVISION])
def test_repeated_update_native_fk_recheck_has_base_parity(cluster, target):
    """Characterization: a second tuple version in one tx rechecks the old FK.

    This preexisting KEY SHARE is distinct from a new account FOR UPDATE in a
    binding row trigger. Keep this limitation visible instead of claiming that
    every possible run UPDATE is account-lock-free.
    """
    role = "review_binding_fk_" + uuid4().hex
    with migrated(cluster, "20260909_0067", (role,)) as database:
        owner = database.owner
        with owner.begin() as c:
            old_writer(c, role)
        if target != "20260909_0067":
            result = candidate.migrate(database.database.url, "upgrade", target)
            assert result.returncode == 0, result.stderr
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with runtime.begin() as c:
                candidate.scope(c)
                rid, _ = legacy.run(c)
            with owner.begin() as holder:
                holder.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=91101 FOR UPDATE")
                with pytest.raises(DBAPIError) as error, runtime.begin() as c:
                    candidate.scope(c)
                    c.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
                    c.execute(text("UPDATE review_sync_runs_v2 SET observed_count=observed_count WHERE sync_run_id=:id"), {"id": rid})
                    c.execute(text("UPDATE review_sync_runs_v2 SET observed_count=2 WHERE sync_run_id=:id"), {"id": rid})
                assert error.value.orig.sqlstate == "55P03"
                assert "FOR KEY SHARE" in error.value.orig.diag.context
        finally:
            runtime.dispose()


@pytest.mark.parametrize("context", [None, 91002])
def test_runtime_org_force_rls(db, context):
    owner, runtime = db
    with owner.connect() as c:
        assert c.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=:r"), {"r": runtime.url.username}).one() == (False, False)
        assert c.exec_driver_sql("SELECT relrowsecurity,relforcerowsecurity,pg_get_userbyid(relowner) FROM pg_class WHERE oid='review_sync_runs_v2'::regclass").one() == (True, True, owner.url.username)
        assert c.exec_driver_sql("SELECT inet_server_addr() IS NULL").scalar_one()
        print("Review binding PostgreSQL:", c.exec_driver_sql("SHOW server_version").scalar_one(), "; Unix socket")
    with pytest.raises(DBAPIError), runtime.begin() as c:
        if context:
            candidate.scope(c, context)
        legacy.run(c, **binding.stamp())
    with runtime.begin() as c:
        if context:
            candidate.scope(c, context)
        assert c.exec_driver_sql("SELECT count(*) FROM review_sync_runs_v2 WHERE organization_id=91001").scalar_one() == 0


def test_same_org_second_account_read_is_preserved(db):
    with db[1].begin() as c:
        candidate.scope(c)
        expected = binding.stamp(account=91102, external="synthetic-b")
        rid, _ = legacy.run(c, marketplace_account_id=91102, **expected)
        c.exec_driver_sql("SELECT set_config('app.marketplace_account_id','91101',true)")
        assert read_stamp(c, rid) == expected


@pytest.mark.parametrize("change", ["external", "ref", "provider", "missing", "wrong-org"])
def test_current_account_mismatch_safe(db, change):
    values = binding.stamp()
    changes = {}
    if change == "external":
        values = binding.stamp(external="SYNTHETIC_CANARY_EXTERNAL")
    elif change == "ref":
        values = binding.stamp(ref="SYNTHETIC_CANARY_REF")
    elif change == "provider":
        values = binding.stamp(provider="wb")
        changes["marketplace"] = "wb"
    elif change == "missing":
        changes["marketplace_account_id"] = 99999
    else:
        changes["organization_id"] = 91002
    with pytest.raises(DBAPIError) as error, db[0].begin() as c:
        legacy.run(c, **changes, **values)
    safe(error, "review_run_binding_mismatch")


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_only_read_committed(db, isolation):
    with pytest.raises(DBAPIError) as error, db[1].connect().execution_options(isolation_level=isolation) as c:
        with c.begin():
            candidate.scope(c)
            legacy.run(c, **binding.stamp())
    assert error.value.orig.sqlstate == "25000"
    assert error.value.orig.diag.message_primary == "review_run_binding_isolation_invalid"


def observed_wait(owner, waiter, holder):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with owner.connect() as c:
            if holder in c.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}).scalar_one():
                return
        sleep(0.01)
    pytest.fail("Expected physical account/advisory lock blocker was not observed")


@pytest.mark.parametrize("winner", ["insert", "rebind"])
@pytest.mark.parametrize("new_external,old_ref,new_ref", [("synthetic-rebound", None, None), ("synthetic-a", None, ""), ("synthetic-a", "", None)])
def test_both_actual_lock_winners(db, winner, new_external, old_ref, new_ref):
    owner, runtime = db
    ready, started = Queue(), Event()
    key = uuid4().hex
    expected = binding.stamp(ref=old_ref)
    with owner.begin() as c:
        c.execute(text("UPDATE marketplace_accounts SET credential_ref=:r WHERE marketplace_account_id=91101"), {"r": old_ref})
    def waiter():
        try:
            with runtime.begin() as c:
                candidate.scope(c)
                c.exec_driver_sql("SET LOCAL lock_timeout='10s'")
                ready.put(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                started.set()
                if winner == "rebind":
                    legacy.run(c, source_run_id=key, **expected)
                else:
                    c.execute(text("UPDATE marketplace_accounts SET external_account_id=:e,credential_ref=:r WHERE marketplace_account_id=91101"), {"e": new_external, "r": new_ref})
            return "committed"
        except DBAPIError as error:
            assert error.orig.sqlstate == "23514"
            assert error.orig.diag.message_primary == "review_run_binding_mismatch"
            return "mismatch"
    try:
        # Holder rollback/commit precedes executor join on every failure path.
        with ThreadPoolExecutor(max_workers=1) as pool:
            with runtime.begin() as holder:
                candidate.scope(holder)
                pid = holder.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                if winner == "rebind":
                    holder.execute(text("UPDATE marketplace_accounts SET external_account_id=:e,credential_ref=:r WHERE marketplace_account_id=91101"), {"e": new_external, "r": new_ref})
                else:
                    rid, _ = legacy.run(holder, source_run_id=key, **expected)
                future = pool.submit(waiter)
                assert started.wait(5)
                other = ready.get(timeout=5)
                assert other != pid
                observed_wait(owner, other, pid)
            assert future.result(timeout=10) == ("mismatch" if winner == "rebind" else "committed")
        with owner.connect() as c:
            assert c.exec_driver_sql("SELECT external_account_id,credential_ref FROM marketplace_accounts WHERE marketplace_account_id=91101").one() == (new_external, new_ref)
            assert c.execute(text("SELECT count(*) FROM review_sync_runs_v2 WHERE source_run_id=:key"), {"key": key}).scalar_one() == (1 if winner == "insert" else 0)
            if winner == "insert":
                assert read_stamp(c, rid) == expected
    finally:
        with owner.begin() as c:
            c.exec_driver_sql("UPDATE marketplace_accounts SET external_account_id='synthetic-a',credential_ref=NULL WHERE marketplace_account_id=91101")


def test_missing_lock_then_created_row_cannot_overwrite_found(db):
    """Disposable-only pause preserves FOUND between PERFORM and its guard."""
    owner, runtime = db
    account, key, lock = 91999, uuid4().hex, int(uuid4().hex[:15], 16)
    ready = Queue()
    expected = binding.stamp(account=account, external="synthetic-created")
    with owner.begin() as c:
        original = c.exec_driver_sql("SELECT pg_get_functiondef('public.review_run_binding_insert_guard()'::regprocedure)").scalar_one()
        anchor = 'AND marketplace COLLATE "C"=NEW.marketplace COLLATE "C" FOR UPDATE;'
        assert original.count(anchor) == 1
        assert c.exec_driver_sql(f"SELECT count(*) FROM marketplace_accounts WHERE marketplace_account_id={account}").scalar_one() == 0
        instrumented = original.replace("DECLARE actual record;", "DECLARE actual record; test_found boolean; test_pause text;")
        instrumented = instrumented.replace(anchor, anchor + f"""
          test_found := FOUND;
          test_pause := pg_catalog.pg_advisory_xact_lock({lock}::bigint)::text;
          IF FOUND IS DISTINCT FROM test_found OR test_found THEN
            RAISE EXCEPTION 'test_expected_preserved_missing_found';
          END IF;
        """)
        c.exec_driver_sql(instrumented)
    def insert():
        try:
            with runtime.begin() as c:
                candidate.scope(c)
                c.exec_driver_sql("SET LOCAL lock_timeout='10s'")
                ready.put(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                legacy.run(c, marketplace_account_id=account, source_run_id=key, **expected)
            return "admitted"
        except DBAPIError as error:
            assert error.orig.sqlstate == "23514"
            assert error.orig.diag.message_primary == "review_run_binding_mismatch"
            return "rejected"
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with owner.begin() as gate:
                gate.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
                gate_pid = gate.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                future = pool.submit(insert)
                insert_pid = ready.get(timeout=5)
                observed_wait(owner, insert_pid, gate_pid)
                with owner.begin() as creator:
                    creator.execute(text("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status) VALUES (:id,91001,'avito','synthetic-created','connected')"), {"id": account})
            assert future.result(timeout=10) == "rejected"
        with owner.connect() as c:
            assert c.execute(text("SELECT count(*) FROM review_sync_runs_v2 WHERE source_run_id=:key"), {"key": key}).scalar_one() == 0
    finally:
        with owner.begin() as c:
            c.exec_driver_sql(original)
    with runtime.begin() as c:
        candidate.scope(c)
        rid, _ = legacy.run(c, marketplace_account_id=account, **expected)
        assert read_stamp(c, rid) == expected
