"""Committed prefixes, captured witnesses and real account-serialized races."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests import test_repricer_approvals_schema as schema
from tests.test_marketplace_credential_fetch_postgres import wait_blocked

cluster = schema.cluster
db = schema.db

A = "wb_repricer_price_approvals"
T = "wb_repricer_price_apply_attempts"
E = "wb_repricer_price_approval_audit"
NOW = datetime(2026, 9, 9, tzinfo=UTC)


def scope(c, org=7, account=42):
    c.execute(
        text(
            "SELECT set_config('app.organization_id',:org,true),set_config('app.marketplace_account_id',:account,true)"
        ),
        {"org": str(org), "account": str(account)},
    )


def insert(c, table, values):
    return dict(
        c.execute(
            text(
                f"INSERT INTO public.{table} ({','.join(values)}) VALUES ({','.join(':' + k for k in values)}) RETURNING *"
            ),
            values,
        )
        .mappings()
        .one()
    )


def update(c, table, key, value, changes, predicate="", extra=None):
    return (
        c.execute(
            text(
                f"UPDATE public.{table} SET {','.join(k + '=:' + k for k in changes)} WHERE {key}=:identity {predicate} RETURNING *"
            ),
            dict(changes, identity=value, **(extra or {})),
        )
        .mappings()
        .all()
    )


def event(
    row, kind, *, before=None, actor=77, attempt=None, before_attempt=None, **changes
):
    values = {
        "audit_event_id": uuid4(),
        "organization_id": row["organization_id"],
        "marketplace_account_id": row["marketplace_account_id"],
        "approval_row_id": row["approval_row_id"],
        "event_kind": kind,
        "actor_kind": "membership",
        "actor_membership_id": actor,
        "before_status": None if before is None else before["status"],
        "after_status": row["status"],
        "before_version": None if before is None else before["version"],
        "after_version": row["version"],
        "occurred_at": row["updated_at"],
        "attempt_id": None,
        "before_attempt_version": None,
        "after_attempt_version": None,
        "reason_code": row.get("reason_code"),
        "safe_error_code": row.get("safe_error_code"),
        "wb_upload_id": row.get("wb_upload_id"),
        "result_code": row.get("result_code"),
    }
    if attempt is not None:
        values.update(
            attempt_id=attempt["attempt_id"],
            before_attempt_version=None
            if before_attempt is None
            else before_attempt["version"],
            after_attempt_version=attempt["version"],
            occurred_at=attempt["updated_at"],
        )
    values.update(changes)
    return values


def create(
    c,
    *,
    legacy=False,
    approval=None,
    catalog=None,
    article="Футболка🚀",
    size_id=None,
    min_price_kopecks=None,
    changes=None,
    audit_changes=None,
    emit=True,
):
    body = {
        "accountId": 42,
        "approvalId": approval or uuid4().hex,
        "articleId": article,
        "catalogSkuId": catalog,
        "discountPct": 0,
        "minPriceKopecks": json_numeric(min_price_kopecks),
        "nmId": 2**70,
        "organizationId": 7,
        "priceKopecks": 2**72,
        "schema": "wb-price-apply/v1",
        "sizeId": json_numeric(size_id),
    }
    raw = json.dumps(
        body, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    checksum = hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256(
        json.dumps(
            [7, "42", body["approvalId"], checksum],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    row = {
        "approval_row_id": uuid4(),
        "organization_id": 7,
        "marketplace_account_id": 42,
        "marketplace": "wb",
        "approval_id": body["approvalId"],
        "catalog_sku_id": catalog,
        "nm_id": body["nmId"],
        "article_id": body["articleId"],
        "recommended_price_kopecks": body["priceKopecks"],
        "request_checksum": checksum,
        "action_key": key,
        "created_at": NOW,
        "updated_at": NOW,
        "request_format": "legacy" if legacy else "wb-price-apply/v1",
        "canonical_request_bytes": None if legacy else raw,
        "discount_pct": None if legacy else 0,
        "size_id": None if legacy else size_id,
        "min_price_kopecks": None if legacy else min_price_kopecks,
        "status": "pending",
        "version": 0,
        "created_audit_id": uuid4(),
    }
    row.update(changes or {})
    result = insert(c, A, row)
    audit = event(
        result,
        "approval.imported" if legacy else "approval.created",
        audit_event_id=row["created_audit_id"],
        occurred_at=row["created_at"],
    )
    if legacy:
        audit.update(
            actor_kind="backfill",
            actor_membership_id=None,
            reason_code=None,
            safe_error_code=None,
            wb_upload_id=None,
            result_code=None,
        )
    audit.update(audit_changes or {})
    if emit:
        insert(c, E, audit)
    return result


def json_numeric(value):
    if not isinstance(value, Decimal):
        return value
    if value.is_finite() and value == value.to_integral_value():
        return int(value)
    return float(value)


def create_v1_numeric_boundary(c, *, field, value):
    assert field in ("size_id", "min_price_kopecks")
    return create(c, **{field: value})


def optional_numeric_failure(engine, *, field, value):
    try:
        with engine.begin() as c:
            scope(c)
            create_v1_numeric_boundary(c, field=field, value=value)
    except DBAPIError as error:
        return (
            error.orig.sqlstate,
            error.orig.diag.message_primary,
            error.orig.diag.constraint_name,
        )
    return None


def assert_optional_numeric_invalid(engine, *, field, value):
    assert optional_numeric_failure(engine, field=field, value=value) == (
        "P0001",
        "repricer_numeric_invalid",
        None,
    )


def claim(c, row, *, emit=True, audit_changes=None):
    result = update(
        c,
        A,
        "approval_row_id",
        row["approval_row_id"],
        {
            "status": "applying",
            "version": row["version"] + 1,
            "claimed_by_membership_id": 77,
            "updated_at": NOW + timedelta(seconds=1),
            "claimed_audit_id": uuid4(),
        },
        "AND status='pending' AND version=:expected",
        {"expected": row["version"]},
    )
    if not result:
        return None
    result = dict(result[0])
    if emit:
        insert(
            c,
            E,
            event(
                result,
                "approval.claimed",
                before=row,
                audit_event_id=result["claimed_audit_id"],
                **(audit_changes or {}),
            ),
        )
    return result


def reserve(c, row, *, emit=True, changes=None):
    identity = uuid4()
    key = hashlib.sha256(
        json.dumps(
            [
                "wb-price-dispatch/v1",
                7,
                42,
                row["approval_id"],
                row["action_key"],
                str(identity),
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    values = {
        "attempt_id": identity,
        "organization_id": 7,
        "marketplace_account_id": 42,
        "approval_row_id": row["approval_row_id"],
        "action_key": row["action_key"],
        "request_checksum": row["request_checksum"],
        "dispatch_key": key,
        "claim_version": row["version"],
        "claimed_by_membership_id": 77,
        "status": "reserved",
        "version": 0,
        "reserved_at": NOW + timedelta(seconds=2),
        "updated_at": NOW + timedelta(seconds=2),
        "reserved_audit_id": uuid4(),
    }
    values.update(changes or {})
    result = insert(c, T, values)
    if emit:
        insert(
            c,
            E,
            event(
                row,
                "attempt.reserved",
                before=row,
                attempt=result,
                audit_event_id=result["reserved_audit_id"],
            ),
        )
    return result


def dispatch(c, row, attempt, *, emit=True):
    result = update(
        c,
        T,
        "attempt_id",
        attempt["attempt_id"],
        {
            "status": "dispatched",
            "version": 1,
            "dispatch_at": NOW + timedelta(seconds=3),
            "updated_at": NOW + timedelta(seconds=3),
            "dispatched_audit_id": uuid4(),
        },
        "AND status='reserved' AND version=0 AND dispatch_at IS NULL",
    )
    if not result:
        return None
    result = dict(result[0])
    if emit:
        insert(
            c,
            E,
            event(
                row,
                "attempt.dispatched",
                before=row,
                attempt=result,
                before_attempt=attempt,
                audit_event_id=result["dispatched_audit_id"],
            ),
        )
    return result


def outcome(c, row, attempt, *, status="applied", error=None, emit=True):
    witness = uuid4()
    metadata = {
        "safe_error_code": error,
        "wb_upload_id": "upload-🚀" if status == "applied" else None,
        "result_code": "Confirmed" if status == "applied" else None,
    }
    common = dict(
        metadata,
        status=status,
        updated_at=NOW + timedelta(seconds=4),
        outcome_audit_id=witness,
    )
    after_attempt = dict(
        update(
            c,
            T,
            "attempt_id",
            attempt["attempt_id"],
            dict(
                common, version=attempt["version"] + 1, finished_at=common["updated_at"]
            ),
        )[0]
    )
    after = dict(
        update(
            c,
            A,
            "approval_row_id",
            row["approval_row_id"],
            dict(common, version=row["version"] + 1),
        )[0]
    )
    if emit:
        insert(
            c,
            E,
            event(
                after,
                "apply.succeeded" if status == "applied" else "apply." + status,
                before=row,
                attempt=after_attempt,
                before_attempt=attempt,
                actor_kind="repricer_worker",
                actor_membership_id=None,
                audit_event_id=witness,
            ),
        )
    return after, after_attempt


def test_committed_prefixes_and_shared_outcome(db):
    with db.begin() as c:
        scope(c)
        row = create(c)
    with db.begin() as c:
        scope(c)
        row = claim(c, row)
    with db.begin() as c:
        scope(c)
        attempt = reserve(c, row)
    with db.begin() as c:
        scope(c)
        attempt = dispatch(c, row, attempt)
    with db.begin() as c:
        scope(c)
        after, terminal = outcome(c, row, attempt)
    assert after["version"] == 2 and terminal["claim_version"] == 1
    assert after["outcome_audit_id"] == terminal["outcome_audit_id"]


@pytest.mark.parametrize("end", ["applied", "failed_pre", "failed_post", "ambiguous"])
def test_multistep_transaction_captures_intermediate_states(db, end):
    with db.begin() as c:
        scope(c)
        row = claim(c, create(c))
        attempt = reserve(c, row)
        if end != "failed_pre":
            attempt = dispatch(c, row, attempt)
        status = "failed" if end.startswith("failed") else end
        error = (
            None
            if status == "applied"
            else "WB_APPLY_REJECTED"
            if end == "failed_post"
            else "WB_APPLY_TIMEOUT"
        )
        outcome(c, row, attempt, status=status, error=error)


@pytest.mark.parametrize("step", ["create", "claim", "reserve", "dispatch", "outcome"])
def test_missing_witness_event_rolls_back_all_work(db, step):
    identity = uuid4().hex
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        row = create(c, approval=identity, emit=step != "create")
        row = claim(c, row, emit=step != "claim")
        attempt = reserve(c, row, emit=step != "reserve")
        attempt = dispatch(c, row, attempt, emit=step != "dispatch")
        outcome(c, row, attempt, emit=step != "outcome")
    with db.begin() as c:
        assert (
            c.execute(
                text(f"SELECT count(*) FROM {A} WHERE approval_id=:id"),
                {"id": identity},
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    "change",
    [
        {"actor_membership_id": 78},
        {"occurred_at": NOW},
        {"after_version": 9},
        {"before_status": "applied"},
        {"result_code": "Ghost"},
    ],
)
def test_captured_claim_event_rejects_mismatches(db, change):
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        claim(c, create(c), audit_changes=change)


def test_ghost_audit_and_metadata_only_witness_fill_deny(db):
    with db.begin() as c:
        scope(c)
        row = claim(c, create(c))
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        insert(
            c,
            E,
            event(
                row, "approval.claimed", before=dict(row, status="pending", version=0)
            ),
        )
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        update(
            c,
            A,
            "approval_row_id",
            row["approval_row_id"],
            {"outcome_audit_id": uuid4()},
        )


@pytest.mark.parametrize(
    "status,metadata",
    [
        ("pending", {}),
        ("applying", {"claimed_by_membership_id": 77}),
        (
            "applied",
            {
                "claimed_by_membership_id": 77,
                "wb_upload_id": "legacy-upload",
                "result_code": None,
            },
        ),
        (
            "failed",
            {"claimed_by_membership_id": 77, "safe_error_code": "Legacy_Failure"},
        ),
        (
            "ambiguous",
            {"claimed_by_membership_id": 77, "safe_error_code": "OldUnknown"},
        ),
        ("rejected", {"decided_by_membership_id": 77, "reason_code": "OldReason"}),
        ("blocked", {"decided_by_membership_id": 77, "reason_code": "OldBlock"}),
    ],
)
def test_privileged_import_preserves_historical_states_and_huge_version(
    db, status, metadata
):
    number = Decimal("12345678901234567890" * 400)
    identity = (
        "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(500))
        + uuid4().hex
    )
    with db.begin() as c:
        scope(c)
        row = create(
            c,
            legacy=True,
            approval=identity,
            changes=dict(metadata, status=status, version=number),
        )
    assert row["version"] == number and row["approval_id"] == identity
    with db.begin() as c:
        assert (
            c.execute(
                text(f"SELECT count(*) FROM {T} WHERE approval_row_id=:id"),
                {"id": row["approval_row_id"]},
            ).scalar_one()
            == 0
        )
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        update(c, A, "approval_row_id", row["approval_row_id"], {"version": number + 1})


@pytest.mark.parametrize(
    "field,constraint",
    [
        ("nm_id", "wb_repricer_price_approvals_nm_id_check"),
        (
            "recommended_price_kopecks",
            "wb_repricer_price_approvals_recommended_price_kopecks_check",
        ),
        ("version", "wb_repricer_price_approvals_version_check"),
    ],
)
@pytest.mark.parametrize(
    "value", [Decimal("1.5"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_required_numeric_rejects_fraction_and_nonfinite_at_column_guard(
    db, field, constraint, value
):
    with pytest.raises(DBAPIError) as captured, db.begin() as c:
        scope(c)
        create(c, legacy=True, changes={field: value})
    assert captured.value.orig.sqlstate == "23514"
    assert captured.value.orig.diag.constraint_name == constraint


@pytest.mark.parametrize("field", ["size_id", "min_price_kopecks"])
@pytest.mark.parametrize(
    "value", [Decimal("1.5"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_v1_optional_numeric_rejects_fraction_and_nonfinite_at_numeric_helper(
    db, field, value
):
    assert_optional_numeric_invalid(db, field=field, value=value)


@pytest.mark.parametrize(
    "size_id,min_price_kopecks",
    [(1, 50), (2**70, 2**72)],
)
def test_v1_optional_numeric_positive_values_commit_exactly(
    db, size_id, min_price_kopecks
):
    with db.begin() as c:
        scope(c)
        row = create(
            c, size_id=size_id, min_price_kopecks=min_price_kopecks
        )
    with db.begin() as c:
        scope(c)
        stored = c.execute(
            text(
                f"SELECT size_id,min_price_kopecks FROM {A} "
                "WHERE approval_row_id=:approval_row_id"
            ),
            {"approval_row_id": row["approval_row_id"]},
        ).one()
        audit_count = c.execute(
            text(
                f"SELECT count(*) FROM {E} "
                "WHERE approval_row_id=:approval_row_id AND event_kind='approval.created'"
            ),
            {"approval_row_id": row["approval_row_id"]},
        ).scalar_one()
    assert stored == (Decimal(size_id), Decimal(min_price_kopecks))
    assert audit_count == 1


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("size_id", 0, "wb_repricer_price_approvals_size_id_check"),
        (
            "min_price_kopecks",
            49,
            "wb_repricer_price_approvals_min_price_kopecks_check",
        ),
    ],
)
def test_v1_optional_numeric_range_denials_use_exact_column_guard(
    db, field, value, constraint
):
    failure = optional_numeric_failure(db, field=field, value=value)
    assert failure is not None
    assert failure[0] == "23514"
    assert failure[2] == constraint


def test_optional_numeric_diagnostic_detects_relaxed_helper(cluster):
    with schema.candidate.disposable_database(cluster) as database:
        result = schema.candidate.migrate(
            database.url, "upgrade", "20260909_0066"
        )
        assert result.returncode == 0, result.stderr
        owner = schema.create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                schema.seed(c)
                c.exec_driver_sql(
                    """CREATE OR REPLACE FUNCTION public.repricer_integral_finite(v numeric)
                    RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=pg_catalog
                    AS $$ SELECT v IS NOT NULL $$"""
                )
            with pytest.raises(AssertionError):
                assert_optional_numeric_invalid(
                    owner, field="size_id", value=Decimal("1.5")
                )
        finally:
            owner.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "whitespace",
        "order",
        "duplicate",
        "unknown",
        "missing",
        "float",
        "boolean",
        "string",
        "escape",
        "typed",
    ],
)
def test_request_reconstruction_rejects_lexical_and_typed_changes_even_rehashed(
    db, mutation
):
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        scope(c)
        row = create(c)
        raw = bytes(row["canonical_request_bytes"])
        replacements = {
            "whitespace": lambda b: b + b" ",
            "order": lambda b: b.replace(b'{"accountId":42,', b"{").replace(
                b'"sizeId":null}', b'"sizeId":null,"accountId":42}'
            ),
            "duplicate": lambda b: b.replace(b"{", b'{"accountId":42,', 1),
            "unknown": lambda b: b.replace(b"{", b'{"unknown":1,', 1),
            "missing": lambda b: b.replace(b'"sizeId":null,', b"").replace(
                b',"sizeId":null', b""
            ),
            "float": lambda b: b.replace(b'"accountId":42', b'"accountId":42.0'),
            "boolean": lambda b: b.replace(b'"accountId":42', b'"accountId":true'),
            "string": lambda b: b.replace(b'"accountId":42', b'"accountId":"42"'),
            "escape": lambda b: b.replace(b"\\u0424", b"\\u0424".upper()),
            "typed": lambda b: b,
        }
        raw = replacements[mutation](raw)
        checksum = hashlib.sha256(raw).hexdigest()
        identity = uuid4().hex
        raw = raw.replace(row["approval_id"].encode(), identity.encode())
        checksum = hashlib.sha256(raw).hexdigest()
        key = hashlib.sha256(
            json.dumps([7, "42", identity, checksum], separators=(",", ":")).encode()
        ).hexdigest()
        create(
            c,
            approval=identity,
            changes=dict(
                canonical_request_bytes=raw,
                request_checksum=checksum,
                action_key=key,
                **({"nm_id": 999} if mutation == "typed" else {}),
            ),
        )
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == "repricer_request_exact"


@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_non_read_committed_mutation_denied(db, level):
    with (
        pytest.raises(DBAPIError) as error,
        db.execution_options(isolation_level=level).begin() as c,
    ):
        scope(c)
        create(c)
    assert error.value.orig.diag.message_primary == "repricer_isolation_invalid"


@pytest.mark.parametrize(
    "operation", ["identity", "claim", "reserve", "marker", "fail_vs_marker"]
)
def test_real_account_wait_precedes_domain_tuple_and_sees_committed_winner(
    db, operation
):
    with db.begin() as c:
        scope(c)
        row = create(c)
        if operation in ("reserve", "marker", "fail_vs_marker"):
            row = claim(c, row)
        attempt = reserve(c, row) if operation in ("marker", "fail_vs_marker") else None
    long_id = (
        "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(500))
        + uuid4().hex
    )
    started = Event()
    waiter = []

    def loser():
        try:
            with db.begin() as c:
                scope(c)
                waiter.append(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                started.set()
                if operation == "identity":
                    return create(c, legacy=True, approval=long_id)
                if operation == "claim":
                    return claim(c, row)
                if operation == "reserve":
                    return reserve(c, row)
                return dispatch(c, row, attempt)
        except DBAPIError as error:
            return error.orig.diag.message_primary

    with db.connect() as winner, ThreadPoolExecutor(max_workers=1) as pool:
        transaction = winner.begin()
        try:
            scope(winner)
            winner_pid = winner.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            winner.exec_driver_sql(
                "SELECT marketplace_account_id FROM marketplace_accounts WHERE organization_id=7 AND marketplace_account_id=42 FOR UPDATE"
            )
            future = pool.submit(loser)
            assert started.wait(5)
            with db.connect() as observer:
                wait_blocked(observer, waiter[0], winner_pid)
                # Lock acquisition on the account happens before touching domain tuples.
                assert (
                    observer.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='tuple' AND relation IN (CAST(:a AS regclass),CAST(:t AS regclass))"
                        ),
                        {"pid": waiter[0], "a": A, "t": T},
                    ).scalar_one()
                    == 0
                )
            if operation == "identity":
                create(winner, legacy=True, approval=long_id)
            elif operation == "claim":
                claim(winner, row)
            elif operation == "reserve":
                reserve(winner, row)
            elif operation == "marker":
                dispatch(winner, row, attempt)
            else:
                outcome(winner, row, attempt, status="failed", error="WB_APPLY_TIMEOUT")
            transaction.commit()
        finally:
            if transaction.is_active:
                transaction.rollback()
        lost = future.result(timeout=8)
    if operation == "identity":
        assert lost == "repricer_identity_conflict"
    elif operation == "reserve":
        assert isinstance(lost, str) and "unique constraint" in lost
    else:
        assert lost is None
    with db.begin() as c:
        assert (
            c.execute(
                text(
                    f"SELECT count(*) FROM {E} WHERE approval_row_id=:id AND event_kind=:kind"
                ),
                {
                    "id": row["approval_row_id"],
                    "kind": {
                        "identity": "approval.created",
                        "claim": "approval.claimed",
                        "reserve": "attempt.reserved",
                        "marker": "attempt.dispatched",
                        "fail_vs_marker": "apply.failed",
                    }[operation],
                },
            ).scalar_one()
            == 1
        )


def test_exact_4096_byte_limit_and_distinct_long_legacy_ids(db):
    with db.begin() as c:
        scope(c)
        first = create(c, approval="x")
        overhead = len(first["canonical_request_bytes"]) - 1
        accepted = create(c, approval="x" * (4096 - overhead))
        assert len(accepted["canonical_request_bytes"]) == 4096
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        scope(c)
        create(c, approval="x" * (4097 - overhead))
    assert error.value.orig.diag.constraint_name == "repricer_request_exact"
    long_id = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(500))
    with db.begin() as c:
        scope(c)
        a = create(c, legacy=True, approval=long_id + "A")
        b = create(c, legacy=True, approval=long_id + "B")
    assert a["approval_id"] != b["approval_id"]


@pytest.mark.parametrize("value", ["a\x01z", "a\x7fz", "a\nz"])
def test_v1_article_control_restriction_preserves_legacy_text(db, value):
    with db.begin() as c:
        scope(c)
        assert create(c, legacy=True, article=value)["article_id"] == value
    with pytest.raises(DBAPIError) as error, db.begin() as c:
        scope(c)
        create(c, article=value)
    assert error.value.orig.diag.constraint_name == "repricer_request_exact"


@pytest.mark.parametrize("end", ["applied", "failed", "ambiguous"])
def test_terminal_marker_and_frozen_binding_are_immutable(db, end):
    with db.begin() as c:
        scope(c)
        row = claim(c, create(c))
        attempt = dispatch(c, row, reserve(c, row))
        row, attempt = outcome(
            c,
            row,
            attempt,
            status=end,
            error=None
            if end == "applied"
            else "WB_APPLY_REJECTED"
            if end == "failed"
            else "WB_APPLY_TIMEOUT",
        )
    for table, key, value, changes in [
        (
            A,
            "approval_row_id",
            row["approval_row_id"],
            {"status": "applying", "version": 3},
        ),
        (T, "attempt_id", attempt["attempt_id"], {"dispatch_at": None}),
        (T, "attempt_id", attempt["attempt_id"], {"claim_version": 2}),
        (
            E,
            "audit_event_id",
            row["created_audit_id"],
            {"event_kind": "approval.imported"},
        ),
    ]:
        with pytest.raises(DBAPIError) as error, db.begin() as c:
            scope(c)
            update(c, table, key, value, changes)
        assert error.value.orig.diag.message_primary.startswith("repricer_")
    with db.begin() as c:
        scope(c)
        assert dispatch(c, row, attempt) is None


@pytest.mark.parametrize(
    "mismatch", ["claimant", "claim_version", "key", "checksum", "reserved_time"]
)
def test_attempt_binding_mismatch_cannot_commit(db, mismatch):
    changes = {
        "claimant": {"claimed_by_membership_id": 78},
        "claim_version": {"claim_version": 2},
        "key": {"action_key": "a" * 64},
        "checksum": {"request_checksum": "b" * 64},
        "reserved_time": {"reserved_at": NOW, "updated_at": NOW},
    }[mismatch]
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        row = claim(c, create(c))
        reserve(c, row, changes=changes)


@pytest.mark.parametrize(
    "error",
    [
        "INTERNAL_APPLY_ERROR",
        "WB_APPLY_AUTHORIZATION_FAILED",
        "WB_APPLY_RATE_LIMITED",
        "WB_APPLY_TIMEOUT",
        "WB_APPLY_TRANSPORT_ERROR",
        "WB_APPLY_VALIDATION_FAILED",
        "WB_RESULT_UNAVAILABLE",
    ],
)
def test_post_marker_failed_requires_exact_rejection(db, error):
    with pytest.raises(DBAPIError) as caught, db.begin() as c:
        scope(c)
        row = claim(c, create(c))
        attempt = dispatch(c, row, reserve(c, row))
        outcome(c, row, attempt, status="failed", error=error)
    assert caught.value.orig.diag.constraint_name == "repricer_attempt_state"


def test_noop_exact_read_replay_and_zero_row_cas_do_not_append(db):
    with db.begin() as c:
        scope(c)
        row = claim(c, create(c))
        attempt = reserve(c, row)
    with db.begin() as c:
        scope(c)
        before = c.execute(
            text(f"SELECT count(*) FROM {E} WHERE approval_row_id=:id"),
            {"id": row["approval_row_id"]},
        ).scalar_one()
        assert claim(c, dict(row, status="pending", version=0)) is None
        replay = (
            c.execute(
                text(
                    f"SELECT * FROM {A} WHERE organization_id=7 AND marketplace_account_id=42 AND approval_id=:id"
                ),
                {"id": row["approval_id"]},
            )
            .mappings()
            .one()
        )
        assert bytes(replay["canonical_request_bytes"]) == bytes(
            row["canonical_request_bytes"]
        )
        assert (
            c.execute(
                text(
                    f"SELECT attempt_id FROM {T} WHERE organization_id=7 AND marketplace_account_id=42 AND approval_row_id=:id"
                ),
                {"id": row["approval_row_id"]},
            ).scalar_one()
            == attempt["attempt_id"]
        )
        assert (
            c.execute(
                text(f"SELECT count(*) FROM {E} WHERE approval_row_id=:id"),
                {"id": row["approval_row_id"]},
            ).scalar_one()
            == before
            == 3
        )


@pytest.mark.parametrize(
    "kind",
    [
        "approval.created",
        "approval.claimed",
        "attempt.reserved",
        "attempt.dispatched",
        "apply.succeeded",
    ],
)
@pytest.mark.parametrize(
    "field", ["occurred_at", "after_version", "actor_kind", "result_code", "attempt_id"]
)
def test_every_captured_event_rejects_misbound_metadata(db, monkeypatch, kind, field):
    import sys

    original = insert

    def corrupt(c, table, values):
        if table == E and values["event_kind"] == kind:
            values = dict(values)
            values[field] = {
                "occurred_at": NOW + timedelta(days=1),
                "after_version": 99,
                "actor_kind": "backfill",
                "result_code": "Injected",
                "attempt_id": uuid4(),
            }[field]
        return original(c, table, values)

    monkeypatch.setattr(sys.modules[__name__], "insert", corrupt)
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        row = claim(c, create(c))
        outcome(c, row, dispatch(c, row, reserve(c, row)))


@pytest.mark.parametrize("target", ["rejected", "blocked"])
def test_pending_decision_owns_single_exact_audit(db, target):
    with db.begin() as c:
        scope(c)
        row = create(c)
        after = dict(
            update(
                c,
                A,
                "approval_row_id",
                row["approval_row_id"],
                {
                    "status": target,
                    "version": 1,
                    "decided_by_membership_id": 77,
                    "reason_code": "Policy",
                    "updated_at": NOW + timedelta(seconds=1),
                    "decision_audit_id": uuid4(),
                },
            )[0]
        )
        insert(
            c,
            E,
            event(
                after,
                "approval." + target,
                before=row,
                audit_event_id=after["decision_audit_id"],
            ),
        )
    with pytest.raises(DBAPIError), db.begin() as c:
        scope(c)
        reserve(c, after)
