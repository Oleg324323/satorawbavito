"""Exact local Review command/result bytes, not auth, replay or DB admission.

Account binding evidence remains a separate immutable receipt descriptor. These
encoders cannot prove epoch witnesses, foreign keys, head CAS or atomic audit.
Prepared generation is input; encoding never generates or dispatches anything.
"""

from app.reviews.canonical_contract import (
    ExternalReviewIdentity,
    ReviewNormalizationError,
)
from app.reviews.storage_payloads import (
    EncodedStoragePayload,
    StoragePayloadError,
    _checksum,
    _finish,
    _integer,
    _object,
    _owner,
    _require,
    _timestamp,
    _uuid,
    encode_review_generation,
    encode_review_policy,
)

_INPUT_KEYS = {
    "review.policy.create.v1": {"policy"},
    "review.policy.select.v1": {
        "policyId", "policyVersion", "policyChecksum", "expectedHeadVersion",
    },
    "review.draft.publish.v1": {
        "reviewId", "externalReviewId", "draftId", "expectedHeadVersion",
        "expectedDraftRevision", "expectedPolicyHeadId", "expectedPolicyHeadVersion",
        "generation", "text",
    },
    "review.decision.record.v1": {
        "reviewId", "externalReviewId", "draftId", "draftRevision", "bindingChecksum",
        "sourceObservationId", "expectedHeadVersion", "expectedPolicyHeadId",
        "expectedPolicyHeadVersion", "decisionKind",
    },
}
_RESULT_REFS = {
    "review.policy.create.v1": {"policyId", "policyVersion"},
    "review.policy.select.v1": {"policyId", "policyVersion", "headId", "headVersion"},
    "review.draft.publish.v1": {
        "policyId", "policyVersion", "draftId", "draftRevision", "headId", "headVersion",
    },
    "review.decision.record.v1": {
        "draftId", "draftRevision", "decisionId", "headId", "headVersion",
    },
}
_ALL_REFS = {"policyId", "policyVersion", "draftId", "draftRevision", "decisionId", "headId", "headVersion"}


def _kind(value):
    _require(type(value) is str and value in _INPUT_KEYS)
    return value


def _nonnegative(value):
    _require(type(value) is int and value >= 0)


def _review_input(envelope, data):
    for key in ("reviewId", "draftId", "expectedPolicyHeadId"):
        _uuid(data[key])
    _integer(data["expectedPolicyHeadVersion"])
    try:
        identity = ExternalReviewIdentity(
            envelope["organizationId"], envelope["marketplaceAccountId"],
            envelope["marketplace"], data["externalReviewId"],
        )
    except ReviewNormalizationError:
        raise StoragePayloadError() from None
    _require(type(data["externalReviewId"]) is str
             and identity.external_review_id == data["externalReviewId"])


def encode_review_local_request(payload: object) -> EncodedStoragePayload:
    value = _object(payload, {
        "schemaVersion", "organizationId", "marketplaceAccountId", "marketplace",
        "localCommandId", "actorMembershipId", "operationKind", "input",
    })
    _require(value["schemaVersion"] == "review-local-command-v1")
    _owner(value)
    for key in ("organizationId", "marketplaceAccountId", "actorMembershipId"):
        _integer(value[key])
        _require(value[key] <= 2**31 - 1)
    _uuid(value["localCommandId"])
    kind = _kind(value["operationKind"])
    data = _object(value["input"], _INPUT_KEYS[kind])
    if kind == "review.policy.create.v1":
        encode_review_policy(data["policy"])
        _require(all(data["policy"][key] == value[key]
                     for key in ("organizationId", "marketplaceAccountId", "marketplace")))
    elif kind == "review.policy.select.v1":
        _uuid(data["policyId"])
        _integer(data["policyVersion"])
        _checksum(data["policyChecksum"])
        _nonnegative(data["expectedHeadVersion"])
    elif kind == "review.draft.publish.v1":
        _review_input(value, data)
        _nonnegative(data["expectedHeadVersion"])
        _nonnegative(data["expectedDraftRevision"])
        _require((data["expectedHeadVersion"] == 0) == (data["expectedDraftRevision"] == 0))
        encode_review_generation(data["generation"])
        if data["generation"]["mode"] == "manual":
            _require(data["expectedHeadVersion"] == data["expectedDraftRevision"] == 0)
        _require(data["generation"]["actorMembershipId"] == value["actorMembershipId"])
        _require(type(data["text"]) is str and bool(data["text"].strip()))
    else:
        _review_input(value, data)
        _uuid(data["sourceObservationId"])
        _integer(data["draftRevision"])
        _integer(data["expectedHeadVersion"])
        _checksum(data["bindingChecksum"])
        _require(type(data["decisionKind"]) is str and data["decisionKind"] in {"approved", "rejected"})
    return _finish(value)


def encode_review_local_result(payload: object) -> EncodedStoragePayload:
    value = _object(payload, {
        "schemaVersion", "localCommandId", "operationKind", "auditEventId", "completedAt",
        *_ALL_REFS,
    })
    _require(value["schemaVersion"] == "review-local-command-result-v1")
    kind = _kind(value["operationKind"])
    _uuid(value["localCommandId"])
    _uuid(value["auditEventId"])
    _timestamp(value["completedAt"])
    for key in _ALL_REFS:
        if key not in _RESULT_REFS[kind]:
            _require(value[key] is None)
        elif key.endswith("Id"):
            _uuid(value[key])
        else:
            _integer(value[key])
    if kind == "review.decision.record.v1":
        # A decision needs an existing draft/head (expected version >= 1).
        _require(value["headVersion"] >= 2)
    return _finish(value)
