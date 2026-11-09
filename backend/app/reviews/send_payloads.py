"""Exact send storage representation, NOT verified evidence or delivery authority.

The repository must establish scoped references, current attempt/token/version,
live authority and evidence authenticity. Encoding an outcome does not prove it.
"""

from app.reviews.storage_payloads import (
    _checksum,
    _finish,
    _integer,
    _label,
    _object,
    _owner,
    _require,
    _timestamp,
    _uuid,
)

_BLOCKED = {"ACCESS_REVOKED", "SOURCE_CHANGED", "POLICY_CHANGED", "NOT_ANSWERABLE", "CREDENTIAL_UNAVAILABLE"}


def _account(value):
    _owner(value)
    for key in ("organizationId", "marketplaceAccountId"):
        _require(value[key] <= 2**31 - 1)


def encode_review_answer_evidence(payload):
    value = _object(payload, {"evidenceVersion", "evidenceKind", "evidenceId", "organizationId",
        "marketplaceAccountId", "marketplace", "reviewId", "commandId", "attemptId", "outcome",
        "readId", "reconciliationStartedAt", "observedAt", "providerAnswerId", "answerChecksum", "verifierVersion"})
    _require(value["evidenceVersion"] == "review-answer-evidence-v1")
    _account(value)
    for key in ("evidenceId", "reviewId", "commandId", "attemptId"):
        _uuid(value[key])
    _label(value["verifierVersion"])
    observed = _timestamp(value["observedAt"])
    if value["evidenceKind"] == "dispatch_ack":
        _require(value["readId"] is None and value["reconciliationStartedAt"] is None)
    else:
        _require(value["evidenceKind"] == "reconciliation_read")
        _uuid(value["readId"])
        _require(_timestamp(value["reconciliationStartedAt"]) <= observed)
    answer_id, checksum = value["providerAnswerId"], value["answerChecksum"]
    if answer_id is not None:
        _require(type(answer_id) is str and bool(answer_id.strip()) and len(answer_id) <= 512)
    if checksum is not None:
        _checksum(checksum)
    if value["outcome"] == "incomplete":
        _require(answer_id is None or checksum is None)
    else:
        _require(value["outcome"] in ("exact", "different") and answer_id is not None and checksum is not None)
    # dispatch<=read_start/observed<=DBnow and outcome-vs-command checksum are
    # cross-row trusted service checks, never guessed from the supplied envelope.
    return _finish(value)


def encode_review_send_audit(payload):
    value = _object(payload, {"schemaVersion", "organizationId", "marketplaceAccountId", "marketplace",
        "eventId", "aggregateId", "aggregateVersion", "eventKind", "occurredAt", "actorKind",
        "actorMembershipId", "commandId", "draftId", "policyId", "decisionId", "attemptId",
        "beforeState", "afterState", "reasonCode"})
    _require(value["schemaVersion"] == "review-audit-v1")
    _account(value)
    for key in ("eventId", "aggregateId", "commandId"):
        _uuid(value[key])
    _require(value["aggregateId"] == value["commandId"])
    _integer(value["aggregateVersion"])
    _timestamp(value["occurredAt"])
    kind, before, after, reason = (value[key] for key in ("eventKind", "beforeState", "afterState", "reasonCode"))
    _require(type(kind) is str and kind in {
        "send.created", "send.claimed", "send.lease_renewed", "send.dispatched", "send.reclaimed",
        "send.ambiguous", "send.sent", "send.conflict", "send.blocked", "send.cancelled"})
    refs = {"commandId"}
    if kind == "send.created":
        refs |= {"draftId", "decisionId"}
        _require(value["aggregateVersion"] == 1 and before is None and after == "queued" and reason is None)
    else:
        _require(value["aggregateVersion"] >= 2)
        if kind not in {"send.cancelled", "send.blocked"} or before == "leased":
            refs.add("attemptId")
        matrix = {
            "send.claimed": ({"queued"}, "leased", {None}),
            "send.lease_renewed": ({"leased"}, "leased", {None}),
            "send.dispatched": ({"leased"}, "leased", {None}),
            "send.reclaimed": ({"leased"}, "queued", {"LEASE_EXPIRED_UNDISPATCHED"}),
            "send.ambiguous": ({"leased"}, "ambiguous", {"RESULT_UNKNOWN"}),
            "send.sent": ({"leased", "ambiguous"}, "sent", {"VERIFIED_EXACT_ANSWER"}),
            "send.conflict": ({"leased", "ambiguous"}, "conflict", {"VERIFIED_DIFFERENT_ANSWER"}),
            "send.blocked": ({"queued", "leased"}, "blocked", _BLOCKED),
            "send.cancelled": ({"queued"}, "cancelled", {"USER_CANCELLED"}),
        }
        allowed_before, expected_after, reasons = matrix[kind]
        _require((before is None or type(before) is str) and (reason is None or type(reason) is str))
        _require(before in allowed_before and after == expected_after and reason in reasons)
    for key in ("commandId", "draftId", "policyId", "decisionId", "attemptId"):
        if key in refs:
            _uuid(value[key])
        else:
            _require(value[key] is None)
    actors = {"membership"} if kind in {"send.created", "send.cancelled"} else (
        {"membership", "worker"} if kind in {"send.sent", "send.conflict"} else {"worker"})
    _require(type(value["actorKind"]) is str and value["actorKind"] in actors)
    if value["actorKind"] == "membership":
        _integer(value["actorMembershipId"])
        _require(value["actorMembershipId"] <= 2**31 - 1)
    else:
        _require(value["actorMembershipId"] is None)
    # Membership closing outcomes additionally require authenticated reconciliation
    # evidence. A caller cannot label itself worker or use a fabricated verified flag.
    return _finish(value)


def encode_review_enqueue(payload):
    value = _object(payload, {"schemaVersion", "organizationId", "marketplaceAccountId", "marketplace",
                              "commandId", "commandVersion", "eventKind"})
    _require(value["schemaVersion"] == "review-enqueue-v1" and value["eventKind"] == "send.ready")
    _account(value)
    _uuid(value["commandId"])
    _integer(value["commandVersion"])
    return _finish(value)
