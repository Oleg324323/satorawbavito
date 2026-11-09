"""Literal representation/merge tests only; no DB, verified evidence or delivery."""

import hashlib
import json
from pathlib import Path

import pytest

from app.notification_storage_payloads import (
    encode_notification_event,
    encode_notification_receipt,
    encode_notification_visible_action,
    merge_notification_receipt,
)
from app.reviews.send_payloads import (
    encode_review_answer_evidence,
    encode_review_enqueue,
    encode_review_send_audit,
)
from app.reviews.storage_payloads import StoragePayloadError, encode_review_send

VECTORS = json.loads((Path(__file__).parent / "fixtures/reviews/send-in-app-storage-v1-golden.json").read_text())["vectors"]
ENCODERS = {"sendRequest": encode_review_send, "reconciliationExact": encode_review_answer_evidence,
    "dispatchIncomplete": encode_review_answer_evidence, "sendAudit": encode_review_send_audit,
    "enqueue": encode_review_enqueue, "notificationEvent": encode_notification_event,
    "readReceipt": encode_notification_receipt, "visibleAction": encode_notification_visible_action}


def value(name):
    return json.loads(VECTORS[name]["canonicalUtf8Json"])


@pytest.mark.parametrize("name", ENCODERS)
def test_literal_canonical_bytes_and_sha(name):
    expected = VECTORS[name]["canonicalUtf8Json"].encode("utf-8")
    actual = ENCODERS[name](value(name))
    assert actual.canonical_bytes == expected
    assert actual.checksum == VECTORS[name]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert ENCODERS[name](dict(reversed(list(value(name).items())))) == actual


@pytest.mark.parametrize("name", ENCODERS)
def test_unknown_keys_never_enter_storage_payload(name):
    data = value(name)
    data["rawProviderResponse"] = "SYNTHETIC private"
    with pytest.raises(StoragePayloadError):
        ENCODERS[name](data)


@pytest.mark.parametrize("changes", [
    {"outcome": "incomplete"}, {"outcome": "sent"}, {"providerAnswerId": None},
    {"answerChecksum": None}, {"providerAnswerId": "  "}, {"providerAnswerId": "x" * 513},
    {"readId": None}, {"reconciliationStartedAt": None},
    {"observedAt": "2026-09-09T11:00:00.000000Z"},
    {"evidenceKind": "dispatch_ack"}, {"organizationId": True},
    {"marketplaceAccountId": 2**31}, {"verifierVersion": "unsafe version"},
])
def test_invalid_evidence_shape_rejected(changes):
    with pytest.raises(StoragePayloadError):
        encode_review_answer_evidence(dict(value("reconciliationExact"), **changes))


@pytest.mark.parametrize("kind,before,after,reason,actor,attempt", [
    ("send.created", None, "queued", None, "membership", False),
    ("send.claimed", "queued", "leased", None, "worker", True),
    ("send.lease_renewed", "leased", "leased", None, "worker", True),
    ("send.dispatched", "leased", "leased", None, "worker", True),
    ("send.reclaimed", "leased", "queued", "LEASE_EXPIRED_UNDISPATCHED", "worker", True),
    ("send.ambiguous", "leased", "ambiguous", "RESULT_UNKNOWN", "worker", True),
    ("send.sent", "leased", "sent", "VERIFIED_EXACT_ANSWER", "worker", True),
    ("send.sent", "ambiguous", "sent", "VERIFIED_EXACT_ANSWER", "membership", True),
    ("send.conflict", "ambiguous", "conflict", "VERIFIED_DIFFERENT_ANSWER", "membership", True),
    ("send.blocked", "queued", "blocked", "ACCESS_REVOKED", "worker", False),
    ("send.blocked", "leased", "blocked", "CREDENTIAL_UNAVAILABLE", "worker", True),
    ("send.cancelled", "queued", "cancelled", "USER_CANCELLED", "membership", False),
])
def test_exact_send_audit_matrix(kind, before, after, reason, actor, attempt):
    data = value("sendAudit")
    data.update(eventKind=kind, beforeState=before, afterState=after, reasonCode=reason,
        actorKind=actor, actorMembershipId=7 if actor == "membership" else None,
        attemptId=data["attemptId"] if attempt else None)
    if kind == "send.created":
        request = value("sendRequest")
        data.update(aggregateVersion=1, draftId=request["draftId"], decisionId=request["decisionId"])
    encode_review_send_audit(data)
    with pytest.raises(StoragePayloadError):
        encode_review_send_audit(dict(data, policyId=data["commandId"]))
    with pytest.raises(StoragePayloadError):
        encode_review_send_audit(dict(data, afterState="guessed_success"))


@pytest.mark.parametrize("changes", [
    {"title": "private customer text"}, {"details": "raw provider data"}, {"dedupeKey": "a" * 64},
    {"scope": "organization"}, {"producer": "platform"}, {"kind": "system_attention"},
    {"marketplaceAccountId": None}, {"sourceVersion": True},
])
def test_notification_display_is_fixed_not_free_payload(changes):
    with pytest.raises(StoragePayloadError):
        encode_notification_event(dict(value("notificationEvent"), **changes))


def test_receipt_first_write_wins_independently_and_never_reopens():
    original = value("readReceipt")
    later = "2026-09-10T12:00:00.000000Z"
    dismiss = dict(original, readAt=None, dismissedAt=later)
    merged = json.loads(merge_notification_receipt(original, dismiss).canonical_bytes)
    assert merged["readAt"] == original["readAt"] and merged["dismissedAt"] == later
    earlier = dict(original, readAt="2026-09-01T00:00:00.000000Z", dismissedAt=later)
    assert json.loads(merge_notification_receipt(merged, earlier).canonical_bytes) == merged
    assert merge_notification_receipt(None, original) == encode_notification_receipt(original)
    assert json.loads(merge_notification_receipt(dismiss, original).canonical_bytes) == merged
    assert original["dismissedAt"] is None  # inputs not mutated
    for key, val in (("recipientMembershipId", 8), ("marketplaceAccountId", 12), ("organizationId", 2)):
        with pytest.raises(StoragePayloadError):
            merge_notification_receipt(original, dict(dismiss, **{key: val}))
    with pytest.raises(StoragePayloadError):
        encode_notification_receipt(dict(original, readAt=None))


def test_visible_action_exact_list_not_watermark_or_all_future():
    original = value("visibleAction")
    for changes in ({"eventIds": []}, {"eventIds": original["eventIds"] * 2},
                    {"action": "reopen"}, {"recipientMembershipId": True}):
        with pytest.raises(StoragePayloadError):
            encode_notification_visible_action(dict(original, **changes))
    for extra in ({"highWater": "latest"}, {"all": True}):
        with pytest.raises(StoragePayloadError):
            encode_notification_visible_action(dict(original, **extra))
