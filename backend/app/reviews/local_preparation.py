"""Prepare once, retain exact bytes, then publish; no provider or storage I/O.

This is not authority. Context can become stale immediately; the live service
rechecks source, policy epoch, actor and predecessor inside its publication root.
Never regenerate a prepared command merely because its HTTP response was lost.
"""

from datetime import UTC, datetime
from uuid import UUID

from app.reviews.local_command_payloads import encode_review_local_request
from app.reviews.storage_payloads import StoragePayloadError


def prepare_local_review_draft(context: dict, *, local_command_id: UUID, draft_id: UUID,
                               generation_id: UUID, mode: str, text: str | None = None,
                               prepared_at: datetime | None = None):
    """Return immutable command bytes for fake, first manual draft, or manual edit.

The fake answer is visibly synthetic, not an LLM answer and never automatically
approved. Manual edit requires the immediately current predecessor, even when
text is unchanged. Call once per explicit user action, not on transport retries.
Manual is first human text only; template/model labels capture selected policy
metadata and do not claim AI generation. A missing policy is never manufactured.
"""
    try:
        if context["schemaVersion"] != "review-local-context-v1" or mode not in {"fake", "manual", "manual_edit"}:
            raise StoragePayloadError()
        policy, ph, review, wh = context["policy"], context["policyHead"], context["review"], context["workflowHead"]
        if policy is None or ph is None or review is None:
            raise StoragePayloadError()
        if mode == "manual_edit" and (context["draft"] is None or wh is None or type(text) is not str):
            raise StoragePayloadError()
        if mode == "manual" and (context["draft"] is not None or wh is not None or type(text) is not str):
            raise StoragePayloadError()
        if mode == "fake" and text is not None:
            raise StoragePayloadError()
        instant = prepared_at or datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise StoragePayloadError()
        at = instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        generation = {"schemaVersion": "review-generation-v1", "generationId": str(generation_id),
            "sourceObservationId": review["sourceObservationId"], "sourceChecksum": review["sourceChecksum"],
            "policyId": policy["policyId"], "policyVersion": policy["version"], "policyChecksum": ph["policyChecksum"],
            "templateVersion": policy["templateVersion"], "modelVersion": policy["modelVersion"], "mode": mode,
            "actorMembershipId": context["actorMembershipId"], "startedAt": at, "completedAt": at}
        if mode == "manual_edit":
            generation["previousDraftId"] = context["draft"]["draftId"]
        return encode_review_local_request({"schemaVersion": "review-local-command-v1",
            "organizationId": context["organizationId"], "marketplaceAccountId": context["marketplaceAccountId"],
            "marketplace": context["marketplace"], "localCommandId": str(local_command_id),
            "actorMembershipId": context["actorMembershipId"], "operationKind": "review.draft.publish.v1",
            "input": {"reviewId": review["reviewId"], "externalReviewId": review["externalReviewId"], "draftId": str(draft_id),
                "expectedHeadVersion": 0 if wh is None else wh["version"],
                "expectedDraftRevision": 0 if wh is None else context["draft"]["revision"],
                "expectedPolicyHeadId": ph["headId"], "expectedPolicyHeadVersion": ph["version"],
                "generation": generation, "text": "[FAKE — не отправлять] Тестовый черновик ответа." if mode == "fake" else text}})
    except (KeyError, TypeError, AttributeError):
        raise StoragePayloadError() from None
