"""Local audit representation only; never supplies authorization or DB witnesses."""

import json
from pathlib import Path

import pytest

from app.reviews.storage_payloads import (
    StoragePayloadError,
    encode_review_local_audit,
)


def test_frozen_local_audit_bytes_and_hash():
    case = json.loads(
        (
            Path(__file__).parent / "fixtures/reviews/local-audit-v1-golden.json"
        ).read_text()
    )
    encoded = encode_review_local_audit(json.loads(case["canonicalUtf8Json"]))
    assert encoded.canonical_bytes == case["canonicalUtf8Json"].encode("utf-8")
    assert encoded.checksum == case["sha256"]


def envelope(kind="policy.created"):
    return {
        "schemaVersion": "review-audit-v1",
        "organizationId": 1,
        "marketplaceAccountId": 11,
        "marketplace": "wb",
        "eventId": "00000000-0000-4000-8000-000000000001",
        "aggregateId": "00000000-0000-4000-8000-000000000002",
        "aggregateVersion": 1,
        "eventKind": kind,
        "occurredAt": "2026-09-09T00:00:00.123456Z",
        "actorKind": "membership",
        "actorMembershipId": 7,
        "commandId": None,
        "draftId": None,
        "policyId": "00000000-0000-4000-8000-000000000002",
        "decisionId": None,
        "attemptId": None,
        "beforeState": None,
        "afterState": None,
        "reasonCode": None,
    }


@pytest.mark.parametrize(
    "kind,version,before,after,refs",
    [
        ("policy.created", 9223372036854775808, None, None, ("policyId",)),
        ("policy.selected", 1, None, "policy_selected", ("policyId",)),
        ("policy.selected", 2, "policy_selected", "policy_selected", ("policyId",)),
        ("draft.published", 1, None, "draft_current", ("policyId", "draftId")),
        (
            "draft.published",
            2,
            "draft_current",
            "draft_current",
            ("policyId", "draftId"),
        ),
        (
            "draft.published",
            3,
            "decision_current",
            "draft_current",
            ("policyId", "draftId"),
        ),
        (
            "decision.approved",
            2,
            "draft_current",
            "decision_current",
            ("draftId", "decisionId"),
        ),
        (
            "decision.approved",
            3,
            "decision_current",
            "decision_current",
            ("draftId", "decisionId"),
        ),
        (
            "decision.rejected",
            2,
            "draft_current",
            "decision_current",
            ("draftId", "decisionId"),
        ),
        (
            "decision.rejected",
            3,
            "decision_current",
            "decision_current",
            ("draftId", "decisionId"),
        ),
    ],
)
def test_closed_local_transition_matrix_keeps_explicit_nulls(
    kind, version, before, after, refs
):
    value = envelope(kind)
    value.update(aggregateVersion=version, beforeState=before, afterState=after)
    for key in ("policyId", "draftId", "decisionId"):
        value[key] = "00000000-0000-4000-8000-000000000002" if key in refs else None
    encoded = encode_review_local_audit(value)
    assert json.loads(encoded.canonical_bytes) == value
    assert encode_review_local_audit(dict(reversed(list(value.items())))) == encoded
    assert b'"commandId":null' in encoded.canonical_bytes
    assert b'"attemptId":null' in encoded.canonical_bytes


@pytest.mark.parametrize(
    "key,bad",
    [
        ("organizationId", True),
        ("organizationId", 2147483648),
        ("marketplaceAccountId", 0),
        ("marketplaceAccountId", "11"),
        ("marketplace", "other"),
        ("actorMembershipId", None),
        ("actorMembershipId", 2147483648),
        ("actorMembershipId", False),
        ("actorKind", "worker"),
        ("eventKind", "send.created"),
        ("eventKind", []),
        ("eventId", "not-a-uuid"),
        ("aggregateId", "00000000-0000-4000-8000-000000000003"),
        ("aggregateVersion", 0),
        ("aggregateVersion", 1.5),
        ("aggregateVersion", True),
        ("occurredAt", "2026-09-09T00:00:00Z"),
        ("reasonCode", "raw customer content"),
        ("beforeState", "draft_current"),
        ("afterState", "decision_current"),
        ("beforeState", []),
        ("policyId", None),
        ("draftId", "00000000-0000-4000-8000-000000000003"),
        ("commandId", "00000000-0000-4000-8000-000000000003"),
        ("attemptId", "00000000-0000-4000-8000-000000000003"),
        ("schemaVersion", "review-audit-v2"),
    ],
)
def test_malformed_unscoped_or_nonlocal_fields_fail_with_safe_error(key, bad):
    value = envelope()
    value[key] = bad
    with pytest.raises(StoragePayloadError, match="^REVIEW_STORAGE_PAYLOAD_INVALID$"):
        encode_review_local_audit(value)


@pytest.mark.parametrize(
    "omit", ["attemptId", "reasonCode", "actorMembershipId", "beforeState"]
)
def test_explicit_null_fields_cannot_be_omitted(omit):
    value = envelope()
    del value[omit]
    with pytest.raises(StoragePayloadError):
        encode_review_local_audit(value)


def test_free_metadata_is_never_admitted():
    value = envelope()
    value["metadata"] = {"text": "synthetic secret"}
    with pytest.raises(StoragePayloadError):
        encode_review_local_audit(value)


@pytest.mark.parametrize(
    "kind,before,after,version",
    [
        ("policy.selected", None, "policy_selected", 2),
        ("policy.selected", "policy_selected", "policy_selected", 1),
        ("draft.published", None, "draft_current", 2),
        ("draft.published", "draft_current", "draft_current", 1),
        ("decision.approved", None, "decision_current", 1),
        ("decision.rejected", "draft_current", "decision_current", 1),
        ("draft.published", "decision_current", "draft_current", 2),
        ("decision.approved", "decision_current", "decision_current", 2),
        ("decision.rejected", "decision_current", "decision_current", 2),
    ],
)
def test_head_initialization_and_later_versions_cannot_be_confused(
    kind, before, after, version
):
    value = envelope(kind)
    value.update(beforeState=before, afterState=after, aggregateVersion=version)
    if kind.startswith(("draft.", "decision.")):
        value["draftId"] = value["aggregateId"]
    if kind.startswith("decision."):
        value["policyId"] = None
        value["decisionId"] = value["aggregateId"]
    with pytest.raises(StoragePayloadError):
        encode_review_local_audit(value)
