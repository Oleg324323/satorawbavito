"""Physical transitions and a test-only SQL participant; no service/auth claims."""
# Separate contexts make connection, transaction and worker lifetimes explicit.
# ruff: noqa: SIM117

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster
W = "production_work_items"
R = "production_assignment_receipts"
H = "production_assignment_history"
TABLES = (W, R, H)
RESULT_FIELDS = (
    "work_item_id",
    "version",
    "catalog_sku_id",
    "required_quantity",
    "planned_quantity",
    "remaining_quantity",
    "source_item_version",
)


def scope(c, org=91001, account=91101):
    for setting, value in (
        ("app.organization_id", org),
        ("app.marketplace_account_id", account),
    ):
        c.execute(
            text("SELECT set_config(:setting,:value,true)"),
            {"setting": setting, "value": str(value)},
        )


def seed(c):
    c.exec_driver_sql(
        "INSERT INTO lk_organizations(organization_id,slug,name) VALUES(91001,'production-a','Synthetic'),(91002,'production-b','Synthetic')"
    )
    c.exec_driver_sql("""INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status)
        VALUES(91101,91001,'avito','synthetic-a','connected'),(91102,91001,'wb','synthetic-b','connected'),(91201,91002,'wb','synthetic-c','connected')""")
    c.exec_driver_sql(
        "INSERT INTO catalog_skus(catalog_sku_id,organization_id,code) VALUES(91301,91001,'one'),(91302,91001,'two'),(91401,91002,'foreign')"
    )
    c.exec_driver_sql("""INSERT INTO lk_users(user_id,organization_id,email,password_hash,full_name,permission_profile)
        VALUES('production-one',91001,'p1@invalid','synthetic','Synthetic','admin'),('production-two',91001,'p2@invalid','synthetic','Synthetic','admin'),('production-three',91002,'p3@invalid','synthetic','Synthetic','admin')""")
    c.exec_driver_sql("""INSERT INTO iam_memberships(membership_id,organization_id,user_id,role,permissions,scope_mode,allowed_account_ids,is_active)
        VALUES(91501,91001,'production-one','admin','[]','all','[]',true),(91502,91001,'production-two','admin','[]','all','[]',true),(91601,91002,'production-three','admin','[]','all','[]',true)""")


@pytest.fixture(scope="module")
def db(cluster):
    role = "production_runtime_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        migration = candidate.migrate(database.url, "upgrade", "20260909_0069")
        assert migration.returncode == 0, migration.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
                # Historical fixture is pinned to0069 and cannot invoke a future
                # runtime script requiring successor schema. RLS file tests that
                # actual script independently against latest head.
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
                c.exec_driver_sql(
                    f"GRANT SELECT,INSERT,UPDATE ON marketplace_orders,marketplace_order_items,{W} TO {role}"
                )
                c.exec_driver_sql(
                    f"GRANT SELECT,UPDATE ON marketplace_accounts TO {role}"
                )
                c.exec_driver_sql(
                    f"GRANT SELECT ON catalog_skus,iam_memberships TO {role}"
                )
                c.exec_driver_sql(f"GRANT SELECT,INSERT ON {R},{H} TO {role}")
                c.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION production_exact_text(text),production_ascii_json_string(text),production_assignment_bytes(bigint,bigint,integer,text,text) TO {role}"
                )
                for table, identity in (
                    (W, "work_item_id"),
                    (R, "receipt_id"),
                    (H, "assignment_event_id"),
                    ("marketplace_orders", "order_id"),
                    ("marketplace_order_items", "order_item_id"),
                ):
                    sequence = c.execute(
                        text("SELECT pg_get_serial_sequence(:t,:i)"),
                        {"t": table, "i": identity},
                    ).scalar_one()
                    c.exec_driver_sql(f"GRANT USAGE ON SEQUENCE {sequence} TO {role}")
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def source(c, org=91001, account=91101, quantity=3):
    provider = "avito" if account == 91101 else "wb"
    oid = c.execute(
        text("""INSERT INTO marketplace_orders
        (organization_id,marketplace_account_id,marketplace,external_order_id)
        VALUES(:org,:account,:provider,:external) RETURNING order_id"""),
        {"org": org, "account": account, "provider": provider, "external": uuid4().hex},
    ).scalar_one()
    iid = candidate.item(
        c, oid, organization_id=org, marketplace_account_id=account, quantity=quantity
    )
    return oid, iid


def create(c, *, org=91001, account=91101, binding=None, **overrides):
    oid, iid = binding or source(c, org, account)
    values = {
        "organization_id": org,
        "marketplace_account_id": account,
        "order_id": oid,
        "order_item_id": iid,
        "source_item_version": 1,
        "required_quantity": 3,
    }
    values.update(overrides)
    return dict(
        c.execute(
            text(
                f"INSERT INTO {W} ({','.join(values)}) VALUES ({','.join(':' + key for key in values)}) RETURNING *"
            ),
            values,
        )
        .mappings()
        .one()
    )


def canonical(command):
    return json.dumps(
        {"command": command, "schema_version": 1},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def frozen(row):
    return {key: row[key] for key in RESULT_FIELDS}


def prepare(c, row, *, key=None, sku=91301, actor=91501, reason="synthetic reason"):
    c.execute(
        text(
            "SELECT 1 FROM marketplace_accounts WHERE organization_id=:org AND marketplace_account_id=:account FOR UPDATE"
        ),
        {"org": row["organization_id"], "account": row["marketplace_account_id"]},
    ).scalar_one()
    c.execute(
        text(f"SELECT 1 FROM {W} WHERE work_item_id=:id FOR UPDATE"),
        {"id": row["work_item_id"]},
    ).scalar_one()
    command = {
        "work_item_id": row["work_item_id"],
        "expected_version": row["version"],
        "catalog_sku_id": sku,
        "idempotency_key": key or uuid4().hex,
        "reason": reason,
    }
    payload = canonical(command)
    result = dict(frozen(row), version=row["version"] + 1, catalog_sku_id=sku)
    instant = c.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    return {
        "organization_id": row["organization_id"],
        "marketplace_account_id": row["marketplace_account_id"],
        "work_item_id": row["work_item_id"],
        "idempotency_key": command["idempotency_key"],
        "request_schema_version": 1,
        "request_payload": command,
        "canonical_request_bytes": payload,
        "request_checksum": hashlib.sha256(payload).hexdigest(),
        "actor_membership_id": actor,
        "result_version": result["version"],
        "result_schema_version": 1,
        "result_payload": result,
        "created_at": instant,
    }


def receipt(c, values):
    args = {
        k: json.dumps(v) if k in ("request_payload", "result_payload") else v
        for k, v in values.items()
    }
    slots = [
        f"CAST(:{k} AS jsonb)"
        if k in ("request_payload", "result_payload")
        else ":" + k
        for k in values
    ]
    return c.execute(
        text(
            f"INSERT INTO {R} ({','.join(values)}) VALUES ({','.join(slots)}) RETURNING receipt_id"
        ),
        args,
    ).scalar_one()


def history(c, row, values, receipt_id, **overrides):
    args = {
        key: values[key]
        for key in (
            "organization_id",
            "marketplace_account_id",
            "work_item_id",
            "actor_membership_id",
        )
    }
    args.update(
        receipt_id=receipt_id,
        event_kind="manual_assignment",
        from_version=row["version"],
        to_version=values["result_version"],
        previous_catalog_sku_id=row["catalog_sku_id"],
        catalog_sku_id=values["request_payload"]["catalog_sku_id"],
        reason=values["request_payload"]["reason"],
        occurred_at=values["created_at"],
    )
    args.update(overrides)
    return c.execute(
        text(
            f"INSERT INTO {H} ({','.join(args)}) VALUES ({','.join(':' + k for k in args)}) RETURNING assignment_event_id"
        ),
        args,
    ).scalar_one()


def mutation(c, row, values, receipt_id, **overrides):
    args = {
        "catalog_sku_id": values["request_payload"]["catalog_sku_id"],
        "version": values["result_version"],
        "updated_at": values["created_at"],
        "current_assignment_receipt_id": receipt_id,
    }
    args.update(overrides)
    return dict(
        c.execute(
            text(
                f"UPDATE {W} SET {','.join(k + '=:' + k for k in args)} WHERE work_item_id=:id AND version=:old_version RETURNING *"
            ),
            dict(args, id=row["work_item_id"], old_version=row["version"]),
        )
        .mappings()
        .one()
    )


def assign(c, row, **kwargs):
    values = prepare(c, row, **kwargs)
    rid = receipt(c, values)
    after = mutation(c, row, values, rid)
    history(c, row, values, rid)
    return after, values


def counts(c, wid):
    return tuple(
        c.execute(
            text(f"SELECT count(*) FROM {table} WHERE work_item_id=:id"), {"id": wid}
        ).scalar_one()
        for table in TABLES
    )


def fresh(db):
    _, runtime = db
    with runtime.begin() as c:
        scope(c)
        return create(c)


def assert_rolled_back(db, before):
    owner, _ = db
    with owner.connect() as c:
        assert (
            dict(
                c.execute(
                    text(f"SELECT * FROM {W} WHERE work_item_id=:id"),
                    {"id": before["work_item_id"]},
                )
                .mappings()
                .one()
            )
            == before
        )
        assert counts(c, before["work_item_id"]) == (1, 0, 0)


def test_single_multiple_and_same_sku_transitions(db):
    owner, runtime = db
    with runtime.begin() as c:
        scope(c)
        row = create(c)
        for sku in (91301, 91302, 91302):
            row, _ = assign(c, row, sku=sku)
    with owner.connect() as c:
        assert row["version"] == 4
        assert counts(c, row["work_item_id"]) == (1, 3, 3)


def test_both_providers_and_scoped_key_reuse(db):
    _, runtime = db
    for org, account, sku, actor in (
        (91001, 91101, 91301, 91501),
        (91001, 91102, 91301, 91501),
        (91002, 91201, 91401, 91601),
    ):
        with runtime.begin() as c:
            scope(c, org, account)
            row, values = assign(
                c,
                create(c, org=org, account=account),
                key="same-key-different-owner",
                sku=sku,
                actor=actor,
            )
            assert (
                row["version"] == 2
                and values["result_payload"]["catalog_sku_id"] == sku
            )


def test_cross_account_source_and_missing_account_fail_closed(db):
    owner, runtime = db
    with runtime.begin() as c:
        scope(c)
        binding = source(c)
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c, 91001, 91102)
        create(c, account=91102, binding=binding)
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.message_primary == "production_source_mismatch"
    with pytest.raises(DBAPIError) as missing, owner.begin() as c:
        scope(c, 91001, 91999)
        create(c, account=91999, binding=binding)
    assert missing.value.orig.sqlstate == "23514"
    assert missing.value.orig.diag.message_primary == "production_account_missing"


@pytest.mark.parametrize(
    "bad",
    [
        {"source_item_version": 2},
        {"required_quantity": 4},
        {"planned_quantity": 1},
        {"catalog_sku_id": 91301},
        {"version": 2},
        {"current_assignment_receipt_id": 99},
        {"source_item_version": 0},
        {"required_quantity": 0},
        {"version": -1},
    ],
)
def test_initial_shape_and_source_mismatch_reject(db, bad):
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        create(c, **bad)
    assert error.value.orig.sqlstate in ("23514", "23503")


def test_same_transaction_source_mutation_invalidates_creation(db):
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        row = create(c)
        c.execute(
            text(
                "UPDATE marketplace_order_items SET quantity=4,version=version+1 WHERE order_item_id=:id"
            ),
            {"id": row["order_item_id"]},
        )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "bad",
    [
        {"required_quantity": 4},
        {"planned_quantity": 1},
        {"source_item_version": 2},
        {"catalog_sku_id": None},
        {"version": 3},
        {"current_assignment_receipt_id": None},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"updated_at": "infinity"},
    ],
)
def test_update_cannot_refresh_unassign_or_change_identity(db, bad):
    row = fresh(db)
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        values = prepare(c, row)
        rid = receipt(c, values)
        mutation(c, row, values, rid, **bad)
        history(c, row, values, rid)
    assert error.value.orig.sqlstate in ("23514", "23503")
    assert_rolled_back(db, row)


@pytest.mark.parametrize(
    "fault",
    [
        "no_history",
        "no_mutation",
        "no_receipt",
        "actor",
        "reason",
        "previous_sku",
        "time",
        "result_quantity",
        "result_source",
        "result_extra",
        "result_missing",
        "result_version",
        "result_work_item",
        "result_sku",
        "result_planned",
        "result_remaining",
        "history_version",
        "target_sku",
        "foreign_actor",
        "foreign_sku",
        "deferred",
        "physical_commit",
    ],
)
def test_forged_or_failed_transition_rolls_back_all_effects(db, fault):
    row = fresh(db)
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        values = prepare(c, row)
        if fault.startswith("result_"):
            field = {
                "quantity": "required_quantity",
                "source": "source_item_version",
                "version": "version",
                "work_item": "work_item_id",
                "sku": "catalog_sku_id",
                "planned": "planned_quantity",
                "remaining": "remaining_quantity",
            }.get(fault[7:])
            if field:
                values["result_payload"][field] += 1
            elif fault == "result_extra":
                values["result_payload"]["extra"] = 1
            else:
                del values["result_payload"]["version"]
        if fault == "foreign_actor":
            values["actor_membership_id"] = 91601
        if fault == "foreign_sku":
            values = prepare(c, row, sku=91401)
        rid = receipt(c, values) if fault != "no_receipt" else 9223372036854775806
        if fault != "no_mutation":
            mutation(c, row, values, rid)
        if fault not in ("no_history", "no_receipt"):
            overrides = {
                "actor": {"actor_membership_id": 91502},
                "reason": {"reason": "changed"},
                "previous_sku": {"previous_catalog_sku_id": 91302},
                "time": {"occurred_at": "2020-01-01T00:00:00Z"},
                "history_version": {"from_version": 2, "to_version": 3},
                "target_sku": {"catalog_sku_id": 91302},
            }.get(fault, {})
            history(c, row, values, rid, **overrides)
        if fault == "deferred":
            # A physical FK failure is queued until the real transaction boundary.
            c.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")
            c.exec_driver_sql(
                "CREATE TEMP TABLE production_fault_parent (id integer PRIMARY KEY)"
            )
            c.exec_driver_sql(
                "CREATE TEMP TABLE production_fault (id integer REFERENCES production_fault_parent DEFERRABLE INITIALLY DEFERRED)"
            )
            c.exec_driver_sql("INSERT INTO production_fault VALUES(-1)")
        elif fault == "physical_commit":
            c.exec_driver_sql("SELECT 1/0")
    assert error.value.orig.sqlstate in ("23514", "23503", "22012")
    assert_rolled_back(db, row)


@pytest.mark.parametrize("value", [True, "1", 1.0, None, -1, 0, 2**63, {"x": 1}, []])
@pytest.mark.parametrize(
    "field", ["work_item_id", "expected_version", "catalog_sku_id"]
)
def test_request_numeric_types_and_ranges_precede_casts(db, field, value):
    row = fresh(db)
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        values = prepare(c, row)
        values["request_payload"][field] = value
        receipt(c, values)
    assert error.value.orig.sqlstate == "23514"
    assert_rolled_back(db, row)


@pytest.mark.parametrize(
    "fault",
    [
        "extra",
        "missing",
        "array",
        "wrong_key",
        "bad_reason",
        "bad_checksum",
        "uppercase_checksum",
        "trailing_bytes",
        "exponent_bytes",
        "fraction_bytes",
        "bool_bytes",
        "string_bytes",
        "request_schema",
        "result_schema",
    ],
)
def test_exact_request_shape_bytes_and_versions(db, fault):
    row = fresh(db)
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        values = prepare(c, row)
        if fault == "extra":
            values["request_payload"]["extra"] = 1
        elif fault == "missing":
            del values["request_payload"]["reason"]
        elif fault == "array":
            values["request_payload"] = []
        elif fault == "wrong_key":
            values["idempotency_key"] += "changed"
        elif fault == "bad_reason":
            values["request_payload"]["reason"] = " leading"
        elif fault == "bad_checksum":
            values["request_checksum"] = "0" * 64
        elif fault == "uppercase_checksum":
            values["request_checksum"] = values["request_checksum"].upper()
        elif fault.endswith("_bytes"):
            raw = values["canonical_request_bytes"]
            replacement = {
                "exponent_bytes": b'"expected_version":1e0',
                "fraction_bytes": b'"expected_version":1.0',
                "bool_bytes": b'"expected_version":true',
                "string_bytes": b'"expected_version":"1"',
            }.get(fault)
            values["canonical_request_bytes"] = (
                raw.replace(b'"expected_version":1', replacement)
                if replacement
                else raw + b" "
            )
            values["request_checksum"] = hashlib.sha256(
                values["canonical_request_bytes"]
            ).hexdigest()
        else:
            values[fault + "_version"] = 2
        receipt(c, values)
    assert error.value.orig.sqlstate == "23514"
    assert_rolled_back(db, row)


class Conflict(Exception):
    pass


def participant(c, wid, expected, key, *, sku=91301):
    """Test-only caller: simulated authorized scope, account lock, replay, then CAS."""
    scope(c)
    locked = c.exec_driver_sql(
        "SELECT marketplace_account_id FROM marketplace_accounts WHERE organization_id=91001 AND marketplace_account_id=91101 FOR UPDATE"
    ).scalar_one()
    assert locked == 91101
    row = dict(
        c.execute(
            text(
                f"SELECT * FROM {W} WHERE organization_id=91001 AND marketplace_account_id=91101 AND work_item_id=:id FOR UPDATE"
            ),
            {"id": wid},
        )
        .mappings()
        .one()
    )
    command = {
        "work_item_id": wid,
        "expected_version": expected,
        "catalog_sku_id": sku,
        "idempotency_key": key,
        "reason": "synthetic reason",
    }
    old = c.execute(
        text(
            f'SELECT canonical_request_bytes,result_payload FROM {R} WHERE organization_id=91001 AND marketplace_account_id=91101 AND work_item_id=:id AND idempotency_key COLLATE "C"=:key'
        ),
        {"id": wid, "key": key},
    ).one_or_none()
    if old is not None:
        if bytes(old[0]) != canonical(command):
            raise Conflict("IDEMPOTENCY_CONFLICT")
        return old[1]
    if row["version"] != expected:
        raise Conflict("VERSION_CONFLICT")
    _, values = assign(c, row, key=key, sku=sku)
    return values["result_payload"]


def test_replay_returns_original_result_after_later_transition_without_writes(db):
    row = fresh(db)
    owner, runtime = db
    key = uuid4().hex
    with runtime.begin() as c:
        original = participant(c, row["work_item_id"], 1, key)
    with runtime.begin() as c:
        participant(c, row["work_item_id"], 2, uuid4().hex, sku=91302)
    with runtime.begin() as c:
        assert participant(c, row["work_item_id"], 1, key) == original
    with pytest.raises(Conflict, match="IDEMPOTENCY_CONFLICT"), runtime.begin() as c:
        participant(c, row["work_item_id"], 1, key, sku=91302)
    with owner.connect() as c:
        assert counts(c, row["work_item_id"]) == (1, 2, 2)


def blocked_by(owner, waiter, blocker):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with owner.connect() as c:
            if c.execute(
                text("SELECT :blocker=ANY(pg_blocking_pids(:waiter))"),
                {"blocker": blocker, "waiter": waiter},
            ).scalar_one():
                return
        sleep(0.01)
    raise AssertionError("Expected actual PostgreSQL blocking was not observed")


def test_creation_source_lock_keeps_quantity_version_coherent(db):
    owner, runtime = db
    with runtime.begin() as c:
        scope(c)
        binding = source(c)
    ready, state = Event(), {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.connect() as first:
            with first.begin():
                scope(first)
                row = create(first, binding=binding)
                blocker = first.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()

                def change_source():
                    with runtime.begin() as c:
                        scope(c)
                        c.exec_driver_sql("SET LOCAL lock_timeout='8s'")
                        state["pid"] = c.exec_driver_sql(
                            "SELECT pg_backend_pid()"
                        ).scalar_one()
                        ready.set()
                        return c.execute(
                            text(
                                "UPDATE marketplace_order_items SET quantity=4,version=version+1 WHERE order_item_id=:id RETURNING quantity,version"
                            ),
                            {"id": binding[1]},
                        ).one()

                future = pool.submit(change_source)
                assert ready.wait(5)
                blocked_by(owner, state["pid"], blocker)
            assert future.result(timeout=10) == (4, 2)
    with owner.connect() as c:
        assert c.execute(
            text(
                f"SELECT required_quantity,source_item_version FROM {W} WHERE work_item_id=:id"
            ),
            {"id": row["work_item_id"]},
        ).one() == (3, 1)


def test_statement_account_lock_precedes_work_item_tuple_lock(db):
    row = fresh(db)
    owner, runtime = db
    ready, state = Event(), {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.connect() as first:
            with first.begin():
                scope(first)
                first.exec_driver_sql(
                    "SELECT 1 FROM marketplace_accounts WHERE organization_id=91001 AND marketplace_account_id=91101 FOR UPDATE"
                )
                blocker = first.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()

                def raw_mutation():
                    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
                        scope(c)
                        c.exec_driver_sql("SET LOCAL lock_timeout='8s'")
                        state["pid"] = c.exec_driver_sql(
                            "SELECT pg_backend_pid()"
                        ).scalar_one()
                        ready.set()
                        c.execute(
                            text(
                                f"UPDATE {W} SET version=version+1 WHERE work_item_id=:id"
                            ),
                            {"id": row["work_item_id"]},
                        )
                    return error.value.orig.sqlstate

                future = pool.submit(raw_mutation)
                assert ready.wait(5)
                blocked_by(owner, state["pid"], blocker)
                # A blocked statement must not already own the target tuple.
                with owner.begin() as c:
                    assert (
                        c.execute(
                            text(
                                f"SELECT work_item_id FROM {W} WHERE work_item_id=:id FOR UPDATE NOWAIT"
                            ),
                            {"id": row["work_item_id"]},
                        ).scalar_one()
                        == row["work_item_id"]
                    )
            assert future.result(timeout=10) == "23514"
    assert_rolled_back(db, row)


def test_raw_duplicate_key_even_different_version_rejected(db):
    row = fresh(db)
    _, runtime = db
    key = "long-" + "".join(
        hashlib.sha256(str(i).encode()).hexdigest() for i in range(160)
    )
    with runtime.begin() as c:
        scope(c)
        after, _ = assign(c, row, key=key)
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        assign(c, after, key=key)
    assert error.value.orig.sqlstate == "23505"


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_non_read_committed_mutations_reject(db, isolation):
    owner, _ = db
    with owner.begin() as c:
        scope(c)
        binding = source(c)
    with (
        pytest.raises(DBAPIError) as error,
        owner.connect().execution_options(isolation_level=isolation) as c,
    ):
        with c.begin():
            scope(c)
            create(c, binding=binding)
    assert error.value.orig.sqlstate == "25000"


@pytest.mark.parametrize(
    "mode",
    [
        "same_version",
        "same_key",
        "changed_body",
        "long_same_key",
        "long_changed_body",
        "long_distinct_keys",
    ],
)
def test_two_actual_sessions_serialize_cas_and_exact_idempotency(db, mode):
    row = fresh(db)
    owner, runtime = db
    prefix = (
        "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(160))
        if mode.startswith("long")
        else uuid4().hex
    )
    second_key = (
        prefix + "other" if mode in ("same_version", "long_distinct_keys") else prefix
    )
    second_sku = 91302 if "changed_body" in mode else 91301
    ready = Event()
    state = {}
    with ThreadPoolExecutor(max_workers=1) as pool, runtime.connect() as first:
        with first.begin():
            scope(first)
            blocker = first.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            original = participant(first, row["work_item_id"], 1, prefix)

            def other():
                try:
                    with runtime.begin() as c:
                        c.exec_driver_sql("SET LOCAL lock_timeout='8s'")
                        state["pid"] = c.exec_driver_sql(
                            "SELECT pg_backend_pid()"
                        ).scalar_one()
                        ready.set()
                        return participant(
                            c, row["work_item_id"], 1, second_key, sku=second_sku
                        )
                except Conflict as error:
                    return str(error)

            future = pool.submit(other)
            assert ready.wait(5)
            assert state["pid"] != blocker
            blocked_by(owner, state["pid"], blocker)
        outcome = future.result(timeout=10)
    if mode in ("same_key", "long_same_key"):
        assert outcome == original
    else:
        assert outcome == (
            "IDEMPOTENCY_CONFLICT" if "changed_body" in mode else "VERSION_CONFLICT"
        )
    with owner.connect() as c:
        assert counts(c, row["work_item_id"]) == (1, 1, 1)
