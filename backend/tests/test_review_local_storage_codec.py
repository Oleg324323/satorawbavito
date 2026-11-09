"""Frozen literals and exact PostgreSQL scalar bytes; no runtime T4 imports."""

import hashlib
import json
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_local_storage_schema as storage

cluster = candidate.cluster
db = storage.db
FIXTURES = candidate.ROOT / "tests/fixtures"
FIXTURE_HASHES = {
    "t1_review_storage_v1_golden.json": "33bb1848950ecb5214b279c84869164bcf87af2606a1dcbf051496ec416537b2",
    "t1_review_local_audit_v1_golden.json": "a6230858b279ba0f1cacc643c2e2533db2bdc33cd16383a05bf44270bb3eafa0",
    "t1_review_local_command_v1_golden.json": "e8bf64f7ca4ba93aeda673bd27f62f36725601bf2ad00ea9309ddef5dc9b7169",
    "t1_review_account_binding_v1_golden.json": "ee563c3fdd1e5c0799e7f42a33051c5fba3e7752e3a89db3a608a9cbe77fe00f",
}
STORAGE = json.loads((FIXTURES / "t1_review_storage_v1_golden.json").read_text())
AUDIT = json.loads((FIXTURES / "t1_review_local_audit_v1_golden.json").read_text())
COMMANDS = json.loads((FIXTURES / "t1_review_local_command_v1_golden.json").read_text())
BINDINGS = json.loads((FIXTURES / "t1_review_account_binding_v1_golden.json").read_text())
WHITESPACE = (*range(9, 14), *range(28, 33), 133, 160, 5760,
              *range(8192, 8203), 8232, 8233, 8239, 8287, 12288)


def composite(table, parameter):
    """Test adapter only: PostgreSQL converts literal fields to actual row types."""
    return f"jsonb_populate_record(NULL::public.{table},CAST(:{parameter} AS jsonb))"


def json_row(row):
    return json.dumps(row, default=lambda value: "\\x" + value.hex() if isinstance(value, bytes)
                      else str(value), ensure_ascii=True)


def literal_policy(p):
    return {"organization_id": p["organizationId"], "marketplace_account_id": p["marketplaceAccountId"],
            "marketplace": p["marketplace"], "policy_id": p["policyId"], "version": p["version"],
            "approval_mode": p["approvalMode"], "template_version": p["templateVersion"],
            "model_version": p["modelVersion"]}


def literal_generation(g):
    return {"generation_id": g["generationId"], "generation_mode": g["mode"],
            "source_observation_id": g["sourceObservationId"], "source_checksum": g["sourceChecksum"],
            "policy_id": g["policyId"], "policy_version": g["policyVersion"],
            "policy_checksum": g["policyChecksum"], "template_version": g["templateVersion"],
            "model_version": g["modelVersion"], "actor_membership_id": g["actorMembershipId"],
            "generation_started_at": g["startedAt"], "generation_completed_at": g["completedAt"],
            "previous_draft_id": g.get("previousDraftId")}


def literal_result(v):
    return {"local_command_id": v["localCommandId"], "operation_kind": v["operationKind"],
            "audit_event_id": v["auditEventId"], "completed_at": v["completedAt"],
            "result_policy_id": v["policyId"], "result_policy_version": v["policyVersion"],
            "result_draft_id": v["draftId"], "result_draft_revision": v["draftRevision"],
            "result_decision_id": v["decisionId"], "result_head_id": v["headId"],
            "result_head_version": v["headVersion"]}


def literal_audit(v):
    names = {"organizationId": "organization_id", "marketplaceAccountId": "marketplace_account_id",
             "marketplace": "marketplace", "eventId": "event_id", "aggregateId": "aggregate_id",
             "aggregateVersion": "aggregate_version", "eventKind": "event_kind", "occurredAt": "occurred_at",
             "actorMembershipId": "actor_membership_id", "policyId": "policy_id", "draftId": "draft_id",
             "decisionId": "decision_id", "beforeState": "before_state", "afterState": "after_state"}
    return {target: v[source] for source, target in names.items()}


def assert_literal(actual, vector):
    expected = vector["canonicalUtf8Json"].encode()
    assert bytes(actual) == expected
    assert len(actual) == len(expected)
    assert hashlib.sha256(actual).hexdigest() == vector["sha256"]


def test_policy_literal_sql_parity(db):
    row = literal_policy(json.loads(STORAGE["policy"]["canonicalUtf8Json"]))
    with db[0].connect() as c:
        actual = c.execute(text("SELECT review_local_policy_bytes(" + composite("review_policy_versions", "p") + ")"),
                           {"p": json_row(row)}).scalar_one()
    assert_literal(actual, STORAGE["policy"])


@pytest.mark.parametrize("name", ["fakeGeneration", "manualGeneration"])
def test_generation_literal_sql_parity(db, name):
    row = literal_generation(json.loads(STORAGE[name]["canonicalUtf8Json"]))
    with db[0].connect() as c:
        actual = c.execute(text("SELECT review_local_generation_bytes(" + composite("review_draft_revisions", "d") + ")"),
                           {"d": json_row(row)}).scalar_one()
    assert_literal(actual, STORAGE[name])


def test_decision_binding_literal_sql_parity(db):
    v = json.loads(STORAGE["decisionBinding"]["canonicalUtf8Json"])
    row = dict(zip(("organization_id", "marketplace_account_id", "marketplace"), v["owner"][:3], strict=True))
    row.update(zip(("draft_id", "revision", "text_checksum"), v["draft"], strict=True))
    row.update(zip(("policy_id", "policy_version", "policy_checksum", "template_version", "model_version"), v["policy"], strict=True))
    row.update(zip(("source_observation_id", "source_checksum"), v["source"], strict=True))
    with db[0].connect() as c:
        actual = c.execute(text("SELECT review_local_decision_binding_bytes(" + composite("review_draft_revisions", "d") + ",CAST(:external AS bytea))"),
                           {"d": json_row(row), "external": v["owner"][3].encode()}).scalar_one()
    assert_literal(actual, STORAGE["decisionBinding"])


def test_audit_literal_sql_parity(db):
    row = literal_audit(json.loads(AUDIT["canonicalUtf8Json"]))
    with db[0].connect() as c:
        actual = c.execute(text("SELECT review_local_audit_bytes(" + composite("review_local_audit", "a") + ")"),
                           {"a": json_row(row)}).scalar_one()
    assert_literal(actual, AUDIT)


@pytest.mark.parametrize("vector", [v for v in COMMANDS if v["type"] == "result"], ids=lambda v: v["name"])
def test_result_literal_sql_parity(db, vector):
    row = literal_result(json.loads(vector["canonicalUtf8Json"]))
    with db[0].connect() as c:
        actual = c.execute(text("SELECT review_local_result_bytes(" + composite("review_local_command_receipts", "r") + ")"),
                           {"r": json_row(row)}).scalar_one()
    assert_literal(actual, vector)


def request_projections(v):
    r = {"organization_id": v["organizationId"], "marketplace_account_id": v["marketplaceAccountId"],
         "marketplace": v["marketplace"], "local_command_id": v["localCommandId"],
         "actor_membership_id": v["actorMembershipId"], "operation_kind": v["operationKind"]}
    p, d, decision, external = {}, {}, {}, None
    data, kind = v["input"], v["operationKind"]
    if kind == "review.policy.create.v1":
        p = literal_policy(data["policy"])
    elif kind == "review.policy.select.v1":
        p = {"policy_id": data["policyId"], "version": data["policyVersion"],
             "policy_checksum": data["policyChecksum"]}
        r["expected_head_version"] = data["expectedHeadVersion"]
    else:
        r.update({"review_id": data["reviewId"], "expected_head_version": data["expectedHeadVersion"],
                  "expected_policy_head_id": data["expectedPolicyHeadId"],
                  "expected_policy_head_version": data["expectedPolicyHeadVersion"]})
        external = data["externalReviewId"].encode()
        d = {"draft_id": data["draftId"]}
        if kind == "review.draft.publish.v1":
            r["expected_draft_revision"] = data["expectedDraftRevision"]
            d.update(literal_generation(data["generation"]))
            d["text_utf8"] = data["text"].encode()
        else:
            d.update({"revision": data["draftRevision"], "binding_checksum": data["bindingChecksum"],
                      "source_observation_id": data["sourceObservationId"]})
            decision["decision_kind"] = data["decisionKind"]
    return {"r": json_row(r), "p": json_row(p), "d": json_row(d),
            "q": json_row(decision), "external": external}


REQUEST_SQL = "SELECT review_local_request_bytes(" + ",".join((
    composite("review_local_command_receipts", "r"), composite("review_policy_versions", "p"),
    composite("review_draft_revisions", "d"), composite("review_decisions", "q"),
    "CAST(:external AS bytea)")) + ")"


@pytest.mark.parametrize("vector", [v for v in COMMANDS if v["type"] == "request"], ids=lambda v: v["name"])
def test_request_literal_sql_parity(db, vector):
    with db[0].connect() as c:
        actual = c.execute(text(REQUEST_SQL), request_projections(json.loads(vector["canonicalUtf8Json"]))).scalar_one()
    assert_literal(actual, vector)


REQUEST_VECTORS = [v for v in COMMANDS if v["type"] == "request"]
REQUEST_EXPECTATIONS = ("review_id", "expected_head_version", "expected_draft_revision",
                        "expected_policy_head_id", "expected_policy_head_version")


@pytest.mark.parametrize("vector", REQUEST_VECTORS, ids=lambda v: v["name"])
@pytest.mark.parametrize("field", REQUEST_EXPECTATIONS)
def test_request_complete_expectation_null_matrix(db, vector, field):
    params = request_projections(json.loads(vector["canonicalUtf8Json"]))
    row = json.loads(params["r"])
    row[field] = ("00000000-0000-4000-8000-000000000001" if field.endswith("_id") else 1) if row.get(field) is None else None
    params["r"] = json_row(row)
    with db[0].connect() as c:
        tx = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text(REQUEST_SQL), params)
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_invalid"
        finally:
            tx.rollback()


@pytest.mark.parametrize("vector", REQUEST_VECTORS, ids=lambda v: v["name"])
@pytest.mark.parametrize("field,value,state", [
    ("organization_id", None, "23514"), ("marketplace_account_id", None, "23514"),
    ("actor_membership_id", None, "23514"), ("local_command_id", None, "23514"),
    ("operation_kind", None, "23514"), ("marketplace", None, "23514"),
    ("organization_id", 0, "23514"), ("actor_membership_id", -1, "23514"),
    ("marketplace", "other", "23514"), ("operation_kind", "unknown", "23514"),
    ("local_command_id", "00000000-0000-0000-0000-000000000000", "23514"),
    ("local_command_id", "not-a-uuid", "22P02"),
    ("actor_membership_id", True, "22P02"),
    ("marketplace_account_id", 2147483648, "22003"),
])
def test_request_envelope_required_types_and_values(db, vector, field, value, state):
    params = request_projections(json.loads(vector["canonicalUtf8Json"]))
    row = json.loads(params["r"])
    row[field] = value
    params["r"] = json_row(row)
    with db[0].connect() as c:
        tx = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text(REQUEST_SQL), params)
            assert caught.value.orig.sqlstate == state
        finally:
            tx.rollback()


@pytest.mark.parametrize("case", ["blank", "empty", "actor", "zero_head_only", "zero_revision_only", "negative_head", "fractional_revision", "zero_policy_epoch"])
def test_request_publication_semantic_input_matrix(db, case):
    vector = next(v for v in REQUEST_VECTORS if json.loads(v["canonicalUtf8Json"])["operationKind"] == "review.draft.publish.v1")
    params = request_projections(json.loads(vector["canonicalUtf8Json"]))
    r, d = json.loads(params["r"]), json.loads(params["d"])
    if case in {"blank", "empty"}:
        d["text_utf8"] = b" \t\n" if case == "blank" else b""
    elif case == "actor":
        d["actor_membership_id"] = r["actor_membership_id"] + 1
    elif case == "zero_head_only":
        r.update(expected_head_version=0, expected_draft_revision=1)
    elif case == "zero_revision_only":
        r.update(expected_head_version=1, expected_draft_revision=0)
    elif case == "negative_head":
        r["expected_head_version"] = -1
    elif case == "fractional_revision":
        r["expected_draft_revision"] = "1.25"
    else:
        r["expected_policy_head_version"] = 0
    params.update(r=json_row(r), d=json_row(d))
    with db[0].connect() as c:
        tx = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text(REQUEST_SQL), params)
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_invalid"
        finally:
            tx.rollback()


@pytest.mark.parametrize("field", ["organization_id", "marketplace_account_id", "marketplace", "policy_id",
                                  "version", "approval_mode", "template_version", "model_version"])
def test_policy_required_typed_field_nulls_rejected(db, field):
    row = literal_policy(json.loads(STORAGE["policy"]["canonicalUtf8Json"]))
    row[field] = None
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text("SELECT review_local_policy_bytes(" + composite("review_policy_versions", "p") + ")"),
                          {"p": json_row(row)})
            assert caught.value.orig.sqlstate == "23514"
        finally:
            transaction.rollback()


@pytest.mark.parametrize("field,value", [
    ("organization_id", 0), ("marketplace_account_id", -1), ("marketplace", "other"),
    ("policy_id", "00000000-0000-0000-0000-000000000000"), ("version", "NaN"),
    ("version", "1.2"), ("approval_mode", "automatic"), ("template_version", "has space"),
    ("model_version", "x" * 129), ("model_version", ""),
    ("template_version", "valid\n"), ("template_version", "valid\r"),
    ("model_version", "café"), ("model_version", "valid\r\n"),
])
def test_policy_typed_values_rejected(db, field, value):
    row = literal_policy(json.loads(STORAGE["policy"]["canonicalUtf8Json"]))
    row[field] = value
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text("SELECT review_local_policy_bytes(" + composite("review_policy_versions", "p") + ")"),
                          {"p": json_row(row)})
            assert caught.value.orig.sqlstate == "23514"
        finally:
            transaction.rollback()


@pytest.mark.parametrize("name,field,value", [
    ("fakeGeneration", "previous_draft_id", "00000000-0000-4000-8000-000000000001"),
    ("manualGeneration", "previous_draft_id", None),
    ("fakeGeneration", "generation_completed_at", "2026-09-08T00:00:00Z"),
    ("fakeGeneration", "generation_started_at", None),
    ("fakeGeneration", "actor_membership_id", 0),
    ("fakeGeneration", "generation_mode", "real"),
    ("fakeGeneration", "source_checksum", "A" * 64),
])
def test_generation_fixed_mode_and_timestamp_matrix(db, name, field, value):
    row = literal_generation(json.loads(STORAGE[name]["canonicalUtf8Json"]))
    row[field] = value
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text("SELECT review_local_generation_bytes(" + composite("review_draft_revisions", "d") + ")"),
                          {"d": json_row(row)})
            assert caught.value.orig.sqlstate == "23514"
        finally:
            transaction.rollback()


RESULT_NULL_CASES = [(vector, field) for vector in COMMANDS if vector["type"] == "result"
                     for field in ("policyId", "policyVersion", "draftId", "draftRevision", "decisionId", "headId", "headVersion")]


@pytest.mark.parametrize("vector,field", RESULT_NULL_CASES, ids=[v["name"] + "-" + f for v, f in RESULT_NULL_CASES])
def test_result_complete_null_matrix(db, vector, field):
    value = json.loads(vector["canonicalUtf8Json"])
    value[field] = ("00000000-0000-4000-8000-000000000001" if field.endswith("Id") else 1) if value[field] is None else None
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text("SELECT review_local_result_bytes(" + composite("review_local_command_receipts", "r") + ")"),
                          {"r": json_row(literal_result(value))})
            assert caught.value.orig.sqlstate == "23514"
        finally:
            transaction.rollback()


@pytest.mark.parametrize("name,digest", FIXTURE_HASHES.items())
def test_literal_fixture_hash(name, digest):
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("value,want", [
    ("1.000", "1"), ("9223372036854775808.000", "9223372036854775808"),
    ("1e40", "10000000000000000000000000000000000000000"), ("-0.00", "0"),
])
def test_integer_scale_free_bytes(db, value, want):
    with db[0].connect() as c:
        assert c.execute(text("SELECT review_local_integer_text(CAST(:v AS numeric),true)"),
                         {"v": Decimal(value)}).scalar_one() == want


@pytest.mark.parametrize("value,allow_zero", [
    (None, True), ("NaN", True), ("Infinity", True), ("-Infinity", True),
    ("-1", True), ("0.1", True), ("0", False), ("1.00001", False),
])
def test_invalid_integer_fails_closed(db, value, allow_zero):
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text("SELECT review_local_integer_text(CAST(:v AS numeric),:z)"),
                          {"v": value, "z": allow_zero})
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_invalid"
        finally:
            transaction.rollback()


@pytest.mark.parametrize("value", [*(chr(i) for i in range(32)), "\x7f", "\x85", "\u2028",
    '"\\', "é", "é", "😀", "\U0010ffff", "", " leading trailing ", r"\u0000\n"])
def test_utf8_scalar_exact_bytes(db, value):
    expected = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    with db[0].connect() as c:
        actual = c.execute(text("SELECT review_local_utf8_json_string(CAST(:v AS bytea))"),
                           {"v": value.encode()}).scalar_one()
    assert bytes(actual) == expected


@pytest.mark.parametrize("code", WHITESPACE)
def test_each_python_whitespace_scalar_is_blank(db, code):
    with db[0].connect() as c:
        assert c.execute(text("SELECT review_local_nonblank_utf8(CAST(:v AS bytea))"),
                         {"v": chr(code).encode()}).scalar_one() is False


@pytest.mark.parametrize("value,want", [
    (b"", False), ("".join(map(chr, WHITESPACE)).encode(), False),
    (b"\x00", True), (b"\x01", True), (b" \x00 ", True), ("　é 🚀".encode(), True),
])
def test_nonblank_exact_control_and_unicode(db, value, want):
    with db[0].connect() as c:
        assert c.execute(text("SELECT review_local_nonblank_utf8(CAST(:v AS bytea))"),
                         {"v": value}).scalar_one() is want


@pytest.mark.parametrize("value", [None, b"\xff", b"\xc0\x80", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\x00\xc3"])
def test_invalid_utf8_scalar_raises_safe_error(db, value):
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text("SELECT review_local_utf8_json_string(CAST(:v AS bytea))"), {"v": value})
            assert caught.value.orig.sqlstate == "23514"
            assert caught.value.orig.diag.message_primary == "review_local_invalid"
        finally:
            transaction.rollback()


@pytest.mark.parametrize("zone", ["UTC", "Pacific/Chatham", "America/New_York"])
def test_timestamp_is_utc_with_six_digits(db, zone):
    with db[0].begin() as c:
        c.execute(text("SELECT set_config('TimeZone',:z,true)"), {"z": zone})
        assert c.execute(text("SELECT review_local_timestamp_text(CAST(:v AS timestamptz))"),
                         {"v": "2026-09-09T03:00:00.123456+03:00"}).scalar_one() == "2026-09-09T00:00:00.123456Z"


@pytest.mark.parametrize("value", [None, "infinity", "-infinity", "10000-01-01 00:00:00+00", "0001-01-01 00:00:00+00 BC"])
def test_unrepresentable_timestamp_rejected(db, value):
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                c.execute(text("SELECT review_local_timestamp_text(CAST(:v AS timestamptz))"), {"v": value})
            assert caught.value.orig.sqlstate == "23514"
        finally:
            transaction.rollback()


@pytest.mark.parametrize("vector", BINDINGS, ids=lambda v: v["name"])
def test_existing_binding_codec_literal_or_physical_nul_refusal(db, vector):
    d = vector["descriptor"]
    params = {"o": d["organizationId"], "a": d["marketplaceAccountId"], "p": d["marketplace"],
              "e": d["externalAccountId"].encode(),
              "r": None if d["credentialRef"] is None else d["credentialRef"].encode()}
    sql = text("SELECT review_run_binding_bytes(:o,:a,:p,convert_from(:e,'UTF8'),convert_from(:r,'UTF8'))")
    with db[0].connect() as c:
        transaction = c.begin()
        try:
            if "pure-nul" in vector["name"]:
                with pytest.raises(DBAPIError) as caught:
                    c.execute(sql, params)
                assert caught.value.orig.sqlstate == "22021"
            else:
                actual = bytes(c.execute(sql, params).scalar_one())
                assert actual == vector["canonicalAscii"].encode("ascii")
                assert len(actual) == len(vector["canonicalAscii"].encode("ascii"))
                assert hashlib.sha256(actual).hexdigest() == vector["sha256"]
        finally:
            transaction.rollback()
