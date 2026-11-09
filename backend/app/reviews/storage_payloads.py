"""Canonical storage payloads for T4's local v1 contracts; no database or auth.

These serializers validate representation, not FK ownership, policy activation,
approval or idempotency uniqueness. The repository must compare immutable bytes
as well as checksums and revalidate scoped bindings inside its transaction.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.reviews.canonical_contract import ExternalReviewIdentity


class StoragePayloadError(ValueError):
    def __init__(self):
        super().__init__("REVIEW_STORAGE_PAYLOAD_INVALID")


@dataclass(frozen=True, slots=True)
class EncodedStoragePayload:
    canonical_bytes: bytes = field(repr=False)
    checksum: str


def _require(value):
    if not value:
        raise StoragePayloadError()


def _object(payload, keys):
    _require(type(payload) is dict and payload.keys() == keys)
    return dict(payload)


def _integer(value):
    _require(type(value) is int and value > 0)


def _uuid(value):
    _require(isinstance(value, str))
    try:
        parsed = UUID(value)
    except ValueError:
        raise StoragePayloadError() from None
    _require(parsed.int != 0 and str(parsed) == value)


def _label(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value))


def _checksum(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))


def _owner(payload):
    _integer(payload["organizationId"])
    _integer(payload["marketplaceAccountId"])
    _require(type(payload["marketplace"]) is str and payload["marketplace"] in {"wb", "avito"})


def _timestamp(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value))
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise StoragePayloadError() from None


def _finish(payload):
    try:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise StoragePayloadError() from None
    return EncodedStoragePayload(raw, hashlib.sha256(raw).hexdigest())


def encode_review_policy(payload: object) -> EncodedStoragePayload:
    value = _object(payload, {"schemaVersion", "organizationId", "marketplaceAccountId",
                              "marketplace", "policyId", "version", "approvalMode",
                              "templateVersion", "modelVersion"})
    _require(value["schemaVersion"] == "review-policy-v1" and value["approvalMode"] == "manual")
    _owner(value)
    _uuid(value["policyId"])
    _integer(value["version"])
    _label(value["templateVersion"])
    _label(value["modelVersion"])
    return _finish(value)


def encode_review_generation(payload: object) -> EncodedStoragePayload:
    _require(type(payload) is dict)
    mode = payload.get("mode")
    _require(type(mode) is str and mode in {"fake", "manual", "manual_edit"})
    keys = {"schemaVersion", "generationId", "sourceObservationId", "sourceChecksum",
            "policyId", "policyVersion", "policyChecksum", "templateVersion", "modelVersion",
            "mode", "actorMembershipId", "startedAt", "completedAt"}
    if mode == "manual_edit":
        keys.add("previousDraftId")
    value = _object(payload, keys)
    _require(value["schemaVersion"] == "review-generation-v1")
    for key in ("generationId", "sourceObservationId", "policyId"):
        _uuid(value[key])
    if mode == "manual_edit":
        _uuid(value["previousDraftId"])
    for key in ("sourceChecksum", "policyChecksum"):
        _checksum(value[key])
    for key in ("policyVersion", "actorMembershipId"):
        _integer(value[key])
    for key in ("templateVersion", "modelVersion"):
        _label(value[key])
    _require(_timestamp(value["completedAt"]) >= _timestamp(value["startedAt"]))
    return _finish(value)


def encode_review_send(payload: object) -> EncodedStoragePayload:
    value = _object(payload, {"schemaVersion", "operationKind", "organizationId",
                              "marketplaceAccountId", "marketplace", "externalReviewId",
                              "draftId", "draftRevision", "decisionId", "bindingChecksum", "textChecksum"})
    _require(value["schemaVersion"] == "review-send-request-v1"
             and value["operationKind"] == "review.answer.create.v1")
    _owner(value)
    _require(isinstance(value["externalReviewId"], str))
    try:
        identity = ExternalReviewIdentity(value["organizationId"], value["marketplaceAccountId"],
                                          value["marketplace"], value["externalReviewId"])
    except ValueError:
        raise StoragePayloadError() from None
    _require(identity.external_review_id == value["externalReviewId"])
    for key in ("draftId", "decisionId"):
        _uuid(value[key])
    _integer(value["draftRevision"])
    _checksum(value["bindingChecksum"])
    _checksum(value["textChecksum"])
    return _finish(value)


def encode_review_local_audit(payload: object) -> EncodedStoragePayload:
    """Encode only local membership events from the documented v1 proposal.

    Representation checks cannot prove actor authority, actual before-state,
    scoped references, immutable witnesses or transactionally atomic publication.
    Send/worker audit events deliberately require a separate lifecycle boundary.
    """
    value = _object(payload, {
        "schemaVersion", "organizationId", "marketplaceAccountId", "marketplace",
        "eventId", "aggregateId", "aggregateVersion", "eventKind", "occurredAt",
        "actorKind", "actorMembershipId", "commandId", "draftId", "policyId",
        "decisionId", "attemptId", "beforeState", "afterState", "reasonCode",
    })
    _require(value["schemaVersion"] == "review-audit-v1")
    _owner(value)
    for key in ("organizationId", "marketplaceAccountId", "actorMembershipId"):
        _integer(value[key])
        _require(value[key] <= 2147483647)
    _require(value["actorKind"] == "membership" and value["reasonCode"] is None)
    _uuid(value["eventId"])
    _uuid(value["aggregateId"])
    _integer(value["aggregateVersion"])
    _timestamp(value["occurredAt"])
    kind = value["eventKind"]
    _require(type(kind) is str and kind in {
        "policy.created", "policy.selected", "draft.published",
        "decision.approved", "decision.rejected",
    })
    refs = {
        "policy.created": {"policyId"},
        "policy.selected": {"policyId"},
        "draft.published": {"policyId", "draftId"},
        "decision.approved": {"draftId", "decisionId"},
        "decision.rejected": {"draftId", "decisionId"},
    }[kind]
    for key in ("commandId", "draftId", "policyId", "decisionId", "attemptId"):
        if key in refs:
            _uuid(value[key])
        else:
            _require(value[key] is None)
    before, after, version = (
        value["beforeState"], value["afterState"], value["aggregateVersion"]
    )
    _require(before is None or type(before) is str)
    _require(after is None or type(after) is str)
    if before == "decision_current":
        # Version 1 starts with a draft; the earliest decision is version 2.
        _require(version >= 3)
    if kind == "policy.created":
        _require(before is None and after is None)
        _require(value["aggregateId"] == value["policyId"])
    elif kind == "policy.selected":
        _require(after == "policy_selected")
        _require(before is None if version == 1 else before == "policy_selected")
    elif kind == "draft.published":
        _require(after == "draft_current")
        _require(before is None if version == 1 else before in {"draft_current", "decision_current"})
    else:
        _require(version >= 2 and after == "decision_current")
        _require(before in {"draft_current", "decision_current"})
    return _finish(value)
