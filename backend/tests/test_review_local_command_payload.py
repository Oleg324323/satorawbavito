"""Closed local intent/result bytes; no DB, providers or SQL admission claims."""

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.reviews.storage_payloads import StoragePayloadError

CASES = json.loads((Path(__file__).parent / "fixtures/reviews/local-command-v1-golden.json").read_text())


def encoder(kind):
    assert importlib.util.find_spec("app.reviews.local_command_payloads"), "Local command codec missing"
    module = importlib.import_module("app.reviews.local_command_payloads")
    return getattr(module, f"encode_review_local_{kind}")


def value(name, kind="request"):
    return json.loads(next(case["canonicalUtf8Json"] for case in CASES
                           if case["name"] == name and case["type"] == kind))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"] + "-" + c["type"])
def test_literal_golden_bytes_preserve_versions_epoch_text_and_nulls(case):
    raw = case["canonicalUtf8Json"].encode("utf-8")
    payload = json.loads(raw)
    before = json.loads(raw)
    result = encoder(case["type"])(payload)
    assert result.canonical_bytes == raw
    assert result.checksum == case["sha256"] == hashlib.sha256(raw).hexdigest()
    assert encoder(case["type"])(dict(reversed(list(payload.items())))) == result
    assert payload == before
    assert "canonical_bytes" not in repr(result)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"] + "-" + c["type"])
def test_exact_top_level_keyset_no_defaulted_or_extra_fields(case):
    payload = json.loads(case["canonicalUtf8Json"])
    call = encoder(case["type"])
    for key in payload:
        malformed = dict(payload)
        del malformed[key]
        with pytest.raises(StoragePayloadError):
            call(malformed)
    with pytest.raises(StoragePayloadError):
        call({**payload, "approved": True})


@pytest.mark.parametrize("name", ["policy-create", "policy-select", "draft-publish", "decision-record"])
def test_exact_operation_input_keyset(name):
    payload = value(name)
    call = encoder("request")
    for key in payload["input"]:
        malformed = value(name)
        del malformed["input"][key]
        with pytest.raises(StoragePayloadError):
            call(malformed)
    payload["input"]["credential"] = "synthetic-private"
    with pytest.raises(StoragePayloadError, match="^REVIEW_STORAGE_PAYLOAD_INVALID$"):
        call(payload)


@pytest.mark.parametrize("field,bad", [
    ("actorMembershipId", True), ("actorMembershipId", 2**31), ("organizationId", 0),
    ("marketplaceAccountId", 1.0), ("marketplace", "amazon"),
    ("localCommandId", "00000000-0000-0000-0000-000000000000"),
    ("operationKind", []), ("operationKind", "review.answer.create.v1"),
    ("schemaVersion", "future"), ("input", None),
])
def test_request_rejects_invalid_envelope_without_echo(field, bad):
    payload = value("draft-publish")
    payload[field] = bad
    with pytest.raises(StoragePayloadError, match="^REVIEW_STORAGE_PAYLOAD_INVALID$"):
        encoder("request")(payload)


@pytest.mark.parametrize("name,path,bad", [
    ("policy-create", ("policy", "organizationId"), 4),
    ("policy-create", ("policy", "approvalMode"), "auto"),
    ("policy-select", ("expectedHeadVersion",), -1),
    ("policy-select", ("policyVersion",), True),
    ("policy-select", ("policyChecksum",), "A" * 64),
    ("draft-publish", ("expectedDraftRevision",), 1),
    ("draft-publish", ("expectedHeadVersion",), 1),
    ("draft-publish", ("expectedPolicyHeadId",), "bad"),
    ("draft-publish", ("expectedPolicyHeadVersion",), 0),
    ("draft-publish", ("externalReviewId",), " padded "),
    ("draft-publish", ("text",), " \t\n"),
    ("draft-publish", ("text",), "bad\ud800"),
    ("draft-publish", ("generation", "actorMembershipId"), 4),
    ("draft-publish", ("generation", "completedAt"), "2026-09-09T11:00:00.000000Z"),
    ("draft-publish", ("generation", "previousDraftId"), None),
    ("decision-record", ("decisionKind",), "sent"),
    ("decision-record", ("expectedHeadVersion",), 0),
    ("decision-record", ("expectedPolicyHeadVersion",), 1.5),
    ("decision-record", ("sourceObservationId",), "bad"),
    ("decision-record", ("draftRevision",), 0),
])
def test_typed_input_rejects_lossy_or_mismatched_values(name, path, bad):
    payload = value(name)
    target = payload["input"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    with pytest.raises(StoragePayloadError, match="^REVIEW_STORAGE_PAYLOAD_INVALID$"):
        encoder("request")(payload)


@pytest.mark.parametrize("name", ["policy-create", "policy-select", "draft-publish", "decision-record"])
def test_result_ref_nullability_is_operation_specific(name):
    payload = value(name, "result")
    for key in ("policyId", "policyVersion", "draftId", "draftRevision", "decisionId", "headId", "headVersion"):
        malformed = dict(payload)
        malformed[key] = ("10000000-0000-4000-8000-000000000099" if key.endswith("Id") else 1) if payload[key] is None else None
        with pytest.raises(StoragePayloadError):
            encoder("result")(malformed)


@pytest.mark.parametrize("field,bad", [
    ("operationKind", {}), ("completedAt", "2026-09-09T12:00:02Z"),
    ("headVersion", 1.0), ("headVersion", 1), ("draftRevision", True), ("auditEventId", "bad"),
])
def test_result_invalid_values_are_safe(field, bad):
    payload = value("decision-record", "result")
    payload[field] = bad
    with pytest.raises(StoragePayloadError, match="^REVIEW_STORAGE_PAYLOAD_INVALID$"):
        encoder("result")(payload)


def test_manual_edit_and_rejection_are_explicit_valid_variants():
    draft = value("draft-publish")
    draft["input"]["expectedHeadVersion"] = 5
    draft["input"]["expectedDraftRevision"] = 2
    draft["input"]["generation"]["mode"] = "manual_edit"
    draft["input"]["generation"]["previousDraftId"] = "10000000-0000-4000-8000-000000000099"
    assert json.loads(encoder("request")(draft).canonical_bytes) == draft
    decision = value("decision-record")
    decision["input"]["decisionKind"] = "rejected"
    assert json.loads(encoder("request")(decision).canonical_bytes)["input"]["decisionKind"] == "rejected"
