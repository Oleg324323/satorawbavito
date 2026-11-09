"""Synthetic SQL participant; trusted GUC simulation is not authentication."""
# Root transaction boundaries, not savepoints, are the behavior under test.
# ruff: noqa: SIM117

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_sku_override_schema as schema

cluster = candidate.cluster
db = schema.db
V, H, A = schema.TABLES
scope = schema.scope
OWNER = "organization_id=:organization_id AND marketplace_account_id=:marketplace_account_id AND catalog_sku_id=:catalog_sku_id"


class Conflict(Exception):
    pass


class InjectedFailure(Exception):
    pass


def fresh(db):
    with db[0].begin() as c:
        return c.execute(text("INSERT INTO catalog_skus(catalog_sku_id,organization_id,code) SELECT coalesce(max(catalog_sku_id),0)+1,7,:code FROM catalog_skus RETURNING catalog_sku_id"), {"code": "sku-override-" + uuid4().hex}).scalar_one()


def prepare(c, sku, expected=0, **overrides):
    p = schema.params(**{"catalog_sku_id": sku, "expected_version": expected,
                        "command_id": str(uuid4()), **overrides})
    p["canonical_request_bytes"] = schema.sql_bytes(c, p)
    p["request_checksum"] = hashlib.sha256(p["canonical_request_bytes"]).hexdigest()
    p["revision"] = expected + 1
    p["parent_revision"] = expected or None
    p["at"] = c.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    p["event_kind"] = "created" if expected == 0 else "replaced"
    return p


def insert_version(c, p):
    keys = ("organization_id", "marketplace_account_id", "catalog_sku_id", "actor_membership_id", "command_id", "revision", "parent_revision", "canonical_request_bytes", "request_checksum") + schema.FIELDS
    binds = [f"CAST(:{k} AS {schema.TYPES[k]})" if k in schema.TYPES else f":{k}" for k in keys]
    c.execute(text(f"INSERT INTO {V} (" + ",".join(keys) + ",marketplace,created_at) VALUES (" + ",".join(binds) + ",'wb',:at)"), p)


def head(c, p):
    if p["expected_version"] == 0:
        row = c.execute(text(f"INSERT INTO {H}(organization_id,marketplace_account_id,catalog_sku_id,version,current_revision,updated_at) VALUES(:organization_id,:marketplace_account_id,:catalog_sku_id,:revision,:revision,:at) RETURNING version"), p).one()
    else:
        row = c.execute(text(f"UPDATE {H} SET version=:revision,current_revision=:revision,updated_at=:at WHERE {OWNER} AND version=:expected_version RETURNING version"), p).one_or_none()
    if row is None or row[0] != p["revision"]:
        raise Conflict("sku_override_cas_conflict")


def audit(c, p):
    c.execute(text(f"INSERT INTO {A}(organization_id,marketplace_account_id,catalog_sku_id,before_revision,after_revision,command_id,actor_membership_id,event_kind,occurred_at) VALUES(:organization_id,:marketplace_account_id,:catalog_sku_id,:parent_revision,:revision,:command_id,:actor_membership_id,:event_kind,:at)"), p)


def participant(c, p, *, fault=None, expected_binding=None):
    # Exact WB account lock precedes command/head reads. Real service auth and
    # current offer mapping are T2 obligations, deliberately not faked here.
    account = c.execute(text("SELECT external_account_id FROM marketplace_accounts WHERE organization_id=:organization_id AND marketplace_account_id=:marketplace_account_id AND marketplace='wb' FOR UPDATE"), p).one_or_none()
    if account is None or (expected_binding is not None and account[0] != expected_binding):
        raise Conflict("sku_override_account_binding_changed")
    old = c.execute(text(f"SELECT revision,actor_membership_id,canonical_request_bytes FROM {V} WHERE {OWNER} AND command_id=:command_id"), p).one_or_none()
    if old:
        if old[1] != p["actor_membership_id"] or bytes(old[2]) != p["canonical_request_bytes"]:
            raise Conflict("sku_override_command_conflict")
        return old[0]
    current = c.execute(text(f"SELECT version FROM {H} WHERE {OWNER} FOR UPDATE"), p).scalar_one_or_none()
    if (current or 0) != p["expected_version"]:
        raise Conflict("sku_override_cas_conflict")
    insert_version(c, p)
    if fault == "version":
        raise InjectedFailure()
    head(c, p)
    if fault == "head":
        raise InjectedFailure()
    audit(c, p)
    if fault == "audit":
        raise InjectedFailure()
    return p["revision"]


def counts(c, sku):
    return tuple(c.execute(text(f"SELECT count(*) FROM {t} WHERE catalog_sku_id=:sku"), {"sku": sku}).scalar_one() for t in (V, H, A))


def test_first_next_multistep_replay_and_scoped_namespaces(db):
    sku = fresh(db)
    with db[1].begin() as c:
        scope(c)
        first = prepare(c, sku, p_min_kopecks=2**80)
        assert participant(c, first) == 1
    with db[1].begin() as c:
        scope(c)
        assert participant(c, first) == 1
        assert participant(c, prepare(c, sku, 1, min_margin_pct="-123.4500")) == 2
        assert participant(c, prepare(c, sku, 2, price_step_pct="0.12345678901234567890123456789")) == 3
    with db[1].begin() as c:
        scope(c)
        assert participant(c, first) == 1
    with db[0].connect() as c:
        assert counts(c, sku) == (3, 1, 3)
        assert c.execute(text(f"SELECT p_min_kopecks FROM {V} WHERE catalog_sku_id=:sku AND revision=1"), {"sku": sku}).scalar_one() == 2**80
    # Same UUID is independent under each full-owner namespace.
    with db[1].begin() as c:
        scope(c, account=43)
        other = prepare(c, sku, marketplace_account_id=43, command_id=first["command_id"])
        assert participant(c, other) == 1
    with db[0].connect() as c:
        assert counts(c, sku) == (4, 2, 4)


@pytest.mark.parametrize("change", [{"actor_membership_id": 78}, {"automation_enabled": False}])
def test_replay_changed_actor_or_content_is_conflict(db, change):
    sku = fresh(db)
    with db[1].begin() as c:
        scope(c)
        p = prepare(c, sku)
        participant(c, p)
    with pytest.raises(Conflict), db[1].begin() as c:
        scope(c)
        changed = {**p, **change}
        changed["canonical_request_bytes"] = schema.sql_bytes(c, changed)
        participant(c, changed)
    with db[0].connect() as c:
        assert counts(c, sku) == (1, 1, 1)


def test_all_typed_fields_accept_recomputed_bytes_without_business_clamps(db):
    sku = fresh(db)
    with db[1].begin() as c:
        scope(c)
        values = schema.VECTORS[2]["inputs"]["values"]
        p = prepare(c, sku, **{**values, "p_min_kopecks": 200, "p_max_kopecks": 100})
        lifecycle_result = participant(c, p)
        assert lifecycle_result == 1
    with db[0].connect() as c:
        actual = c.execute(text(f"SELECT {','.join(schema.FIELDS)} FROM {V} WHERE catalog_sku_id=:sku"), {"sku": sku}).one()
        assert tuple(actual) == tuple(p[key] for key in schema.FIELDS)


@pytest.mark.parametrize("bad", [{"request_checksum": "A" * 64}, {"request_checksum": "0" * 64}, {"canonical_request_bytes": b"{}"}, {"created_at": "infinity"}])
def test_checksum_bytes_and_finite_clock_cannot_be_forged(db, bad):
    sku = fresh(db)
    with db[0].connect() as c:
        transaction = c.begin()
        scope(c)
        p = prepare(c, sku)
        p.update(bad)
        if "created_at" in bad:
            p["at"] = bad["created_at"]
        try:
            with pytest.raises(DBAPIError):
                insert_version(c, p)
        finally:
            transaction.rollback()


@pytest.mark.parametrize("omit_first_audit", [False, True])
def test_every_sku_in_same_root_has_its_own_continuous_witness(db, omit_first_audit):
    first_sku, second_sku = fresh(db), fresh(db)

    def transact():
        with db[1].begin() as c:
            scope(c)
            p = prepare(c, first_sku)
            insert_version(c, p)
            head(c, p)
            if not omit_first_audit:
                audit(c, p)
            participant(c, prepare(c, second_sku))

    if omit_first_audit:
        with pytest.raises(DBAPIError):
            transact()
    else:
        transact()
    with db[0].connect() as c:
        want = (0, 0, 0) if omit_first_audit else (1, 1, 1)
        assert counts(c, first_sku) == counts(c, second_sku) == want


@pytest.mark.parametrize("fault", ["version", "head", "audit", "commit"])
def test_root_failure_at_every_stage_leaves_no_rows(db, fault):
    sku = fresh(db)
    expected_error = DBAPIError if fault == "commit" else InjectedFailure
    with pytest.raises(expected_error), db[1].begin() as c:
        scope(c)
        participant(c, prepare(c, sku), fault=fault)
        if fault == "commit":
            # Deferrable orphan reaches physical COMMIT, where it must fail.
            insert_version(c, prepare(c, sku, 1))
    with db[0].connect() as c:
        assert counts(c, sku) == (0, 0, 0)


@pytest.mark.parametrize("field", schema.FIELDS + ("organization_id", "marketplace_account_id", "catalog_sku_id", "actor_membership_id", "command_id", "revision"))
def test_typed_column_mutation_with_original_bytes_is_rejected(db, field):
    sku = fresh(db)
    with db[0].connect() as c:
        tx = c.begin()
        scope(c)
        p = prepare(c, sku)
        value = False if field in schema.BOOL else "manual" if field == "basket_norm_mode" else str(uuid4()) if field == "command_id" else 1
        if field == "organization_id":
            value = 8
        elif field == "marketplace_account_id":
            value = 43
        elif field == "catalog_sku_id":
            value = 101
        elif field == "actor_membership_id":
            value = 78
        elif field == "revision":
            value = 2
        p[field] = value
        try:
            # Require failure in this INSERT, before any deferred orphan check
            # can conceal a missing canonical CHECK. Owner/identity cases also
            # have independent context/range guards; fields exercise the codec.
            with pytest.raises(DBAPIError):
                insert_version(c, p)
        finally:
            tx.rollback()
    with db[0].connect() as c:
        assert counts(c, sku) == (0, 0, 0)


@pytest.mark.parametrize("bad", [{"marketplace_account_id": 45}, {"catalog_sku_id": 100}, {"actor_membership_id": 79}])
def test_provider_sku_and_member_parent_scope_rejected(db, bad):
    sku = fresh(db)
    with pytest.raises(DBAPIError), db[0].begin() as c:
        scope(c, account=bad.get("marketplace_account_id", 42))
        p = prepare(c, bad.get("catalog_sku_id", sku), **{k: v for k, v in bad.items() if k != "catalog_sku_id"})
        insert_version(c, p)


@pytest.mark.parametrize("fault", ["revision_only", "head_only", "audit_only", "missing_audit", "wrong_actor", "wrong_command", "wrong_time", "wrong_event", "missing_intermediate_audit", "forged_intermediate_time", "scope_changed", "head_skipped", "owner_changed", "time_backwards"])
def test_reciprocal_and_captured_witnesses_reject_forgery(db, fault):
    sku = fresh(db)
    with pytest.raises(DBAPIError), db[0].begin() as c:
        scope(c)
        first = prepare(c, sku)
        if fault == "revision_only":
            insert_version(c, first)
        elif fault == "head_only":
            head(c, first)
        elif fault == "audit_only":
            audit(c, first)
        else:
            insert_version(c, first)
            head(c, {**first, "at": first["at"] - timedelta(seconds=1)} if fault == "forged_intermediate_time" else first)
            if fault not in ("missing_audit", "missing_intermediate_audit"):
                changes = {"wrong_actor": {"actor_membership_id": 78}, "wrong_command": {"command_id": str(uuid4())}, "wrong_time": {"at": first["at"] + timedelta(seconds=1)}, "wrong_event": {"event_kind": "replaced"}}.get(fault, {})
                audit(c, {**first, **changes})
            if fault in ("missing_intermediate_audit", "forged_intermediate_time", "head_skipped", "owner_changed", "time_backwards"):
                second = prepare(c, sku, 1)
                insert_version(c, second)
                if fault == "head_skipped":
                    second["revision"] = 3
                if fault == "time_backwards":
                    second["at"] = first["at"] - timedelta(seconds=1)
                if fault == "owner_changed":
                    c.execute(text(f"UPDATE {H} SET catalog_sku_id=101,version=2,current_revision=2 WHERE {OWNER}"), second)
                else:
                    head(c, second)
                audit(c, second)
            if fault == "scope_changed":
                scope(c, account=43)
    with db[0].connect() as c:
        assert counts(c, sku) == (0, 0, 0)


@pytest.mark.parametrize("table,operation", [(t, op) for t in schema.TABLES for op in ("DELETE", "TRUNCATE", "UPDATE") if (t, op) != (H, "UPDATE")])
def test_append_only_statements_even_no_matching_rows(db, table, operation):
    statement = f"{operation} {table}" if operation == "TRUNCATE" else f"DELETE FROM {table} WHERE false" if operation == "DELETE" else f"UPDATE {table} SET organization_id=organization_id WHERE false"
    with pytest.raises(DBAPIError), db[0].begin() as c:
        scope(c)
        c.exec_driver_sql(statement)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_only_read_committed_admitted(db, isolation):
    sku = fresh(db)
    with pytest.raises(DBAPIError), db[0].connect().execution_options(isolation_level=isolation) as c:
        with c.begin():
            scope(c)
            insert_version(c, prepare(c, sku))


def blocked_by(owner, waiter, blocker):
    deadline = monotonic() + 5
    with owner.connect() as c:
        while monotonic() < deadline:
            if c.execute(text("SELECT :blocker=ANY(pg_blocking_pids(:waiter))"), {"blocker": blocker, "waiter": waiter}).scalar_one():
                return
    pytest.fail("Exact own PostgreSQL lock wait not observed within five seconds")


@pytest.mark.parametrize("winner", [0, 1])
def test_two_sessions_actual_account_wait_and_both_cas_winners(db, winner):
    sku = fresh(db)
    with db[1].begin() as c:
        scope(c)
        participant(c, prepare(c, sku))
    with db[1].connect() as first, db[1].connect() as second:
        connections = (first, second)
        win, lose = connections[winner], connections[1 - winner]
        with win.begin():
            scope(win)
            win_pid = win.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            win.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE organization_id=7 AND marketplace_account_id=42 FOR UPDATE")
            started = Event()
            waiter = {}

            def worker():
                with lose.begin():
                    scope(lose)
                    waiter["pid"] = lose.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                    p = prepare(lose, sku, 1)
                    started.set()
                    return participant(lose, p)

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker)
                try:
                    assert started.wait(5)
                    blocked_by(db[0], waiter["pid"], win_pid)
                    participant(win, prepare(win, sku, 1))
                    win.commit()
                    with pytest.raises(Conflict):
                        future.result(timeout=5)
                finally:
                    if win.in_transaction():
                        win.rollback()
    with db[0].connect() as c:
        assert counts(c, sku) == (2, 1, 2)


@pytest.mark.parametrize("change", ["provider", "delete", "binding"])
def test_waited_account_change_is_rechecked(db, change):
    sku = fresh(db)
    with db[0].begin() as c:
        account = c.execute(text("INSERT INTO marketplace_accounts(organization_id,marketplace,external_account_id,status) VALUES(7,'wb',:external,'connected') RETURNING marketplace_account_id"), {"external": "wait-" + uuid4().hex}).scalar_one()
    with db[0].connect() as blocker, db[1].connect() as waiter:
        transaction = blocker.begin()
        pid = blocker.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        old_binding = blocker.execute(text("SELECT external_account_id FROM marketplace_accounts WHERE marketplace_account_id=:a FOR UPDATE"), {"a": account}).scalar_one()
        ready = Event()
        own = {}

        def worker():
            with waiter.begin():
                scope(waiter, account=account)
                own["pid"] = waiter.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                p = prepare(waiter, sku, marketplace_account_id=account)
                ready.set()
                if change == "binding":
                    participant(waiter, p, expected_binding=old_binding)
                else:
                    # Direct DML proves the statement guard itself rechecks FOUND.
                    insert_version(waiter, p)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                assert ready.wait(5)
                blocked_by(db[0], own["pid"], pid)
                statement = "DELETE FROM marketplace_accounts WHERE marketplace_account_id=:a" if change == "delete" else "UPDATE marketplace_accounts SET marketplace='avito' WHERE marketplace_account_id=:a" if change == "provider" else "UPDATE marketplace_accounts SET external_account_id=external_account_id||'-rebound' WHERE marketplace_account_id=:a"
                blocker.execute(text(statement), {"a": account})
                transaction.commit()
                with pytest.raises(Conflict if change == "binding" else DBAPIError):
                    future.result(timeout=5)
            finally:
                if transaction.is_active:
                    transaction.rollback()
    with db[0].connect() as c:
        assert counts(c, sku) == (0, 0, 0)


def test_statement_account_lock_precedes_head_tuple_and_other_account_progresses(db):
    sku, other_sku = fresh(db), fresh(db)
    with db[1].begin() as c:
        scope(c)
        participant(c, prepare(c, sku))
    with db[0].connect() as blocker, db[0].connect() as tuple_holder, db[0].connect() as waiter:
        blocker.begin()
        pid = blocker.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        blocker.exec_driver_sql("SELECT 1 FROM marketplace_accounts WHERE organization_id=7 AND marketplace_account_id=42 FOR UPDATE")
        tuple_holder.begin()
        tuple_holder.execute(text(f"SELECT 1 FROM {H} WHERE catalog_sku_id=:sku FOR UPDATE"), {"sku": sku})
        ready = Event()
        own = {}

        def worker():
            with waiter.begin():
                scope(waiter)
                own["pid"] = waiter.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                ready.set()
                waiter.execute(text(f"UPDATE {H} SET version=version+1,current_revision=current_revision+1 WHERE catalog_sku_id=:sku"), {"sku": sku})

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                assert ready.wait(5)
                blocked_by(db[0], own["pid"], pid)
                # No FK column changes: this wait is the BEFORE STATEMENT guard.
                with db[1].begin() as c:
                    scope(c, account=43)
                    participant(c, prepare(c, other_sku, marketplace_account_id=43))
                tuple_holder.rollback()
                blocker.rollback()
                with pytest.raises(DBAPIError):
                    future.result(timeout=5)
            finally:
                if tuple_holder.in_transaction():
                    tuple_holder.rollback()
                if blocker.in_transaction():
                    blocker.rollback()
    with db[0].connect() as c:
        assert counts(c, sku) == (1, 1, 1)
        assert counts(c, other_sku) == (1, 1, 1)
