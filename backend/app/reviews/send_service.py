"""Dormant guarded Review service composition. No registration or provider I/O.

Exact T1 roots authorize/commit; these callbacks enforce T4 source/policy/draft
semantics. Evidence entrypoints are TRUSTED adapter inputs, never HTTP payloads.
"""

import json
from dataclasses import dataclass
from datetime import UTC
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.notification_repository import NotificationRepository
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.review_job_authority import (
    ReviewJobCommands,
    ReviewJobExecutor,
    ReviewReconciliation,
)
from app.platform.integrations.review_job_contract import (
    ReviewAction,
    ReviewJobError,
    ReviewSendIntent,
)
from app.reviews.canonical_contract import ExternalReviewIdentity, ReviewMarketplace
from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.decision_contract import ReviewDraftRevision, ReviewPolicyVersion
from app.reviews.historical_binding import ReviewBindingDescriptor
from app.reviews.local_repository import ReviewLocalRepository, _encoded
from app.reviews.local_tables import AUDIT as LOCAL_AUDIT
from app.reviews.local_tables import DECISION, DRAFT, POLICY, POLICY_HEAD, WORKFLOW_HEAD
from app.reviews.send_payloads import encode_review_answer_evidence
from app.reviews.send_repository import ReviewSendRepository
from app.reviews.send_tables import COMMAND
from app.reviews.storage_payloads import (
    encode_review_generation,
    encode_review_local_audit,
    encode_review_policy,
    encode_review_send,
)


class ReviewDomainBlocked(ReviewJobError):
    def __init__(self, reason):
        self.reason = reason if reason in {"SOURCE_CHANGED", "POLICY_CHANGED", "NOT_ANSWERABLE", "ACCESS_REVOKED"} else "SOURCE_CHANGED"
        super().__init__("REVIEW_CONFLICT")


def _require(condition, reason="SOURCE_CHANGED"):
    if not condition:
        raise ReviewDomainBlocked(reason)


def _intent_payload(intent):
    _require(type(intent) is ReviewSendIntent)
    value = json.loads(intent.request_payload)
    encoded = encode_review_send(value)
    _require(encoded.canonical_bytes == intent.request_payload and encoded.checksum == intent.request_checksum)
    _require((value["organizationId"], value["marketplaceAccountId"], value["marketplace"],
        UUID(value["draftId"]), value["draftRevision"], UUID(value["decisionId"]), value["bindingChecksum"], value["textChecksum"])
        == (intent.locator.organization_id, intent.locator.marketplace_account_id, intent.locator.marketplace,
            intent.draft_id, intent.draft_revision, intent.decision_id, intent.binding_checksum, intent.text_checksum))
    return value


def _binding(locator, account):
    return ReviewBindingDescriptor(locator.organization_id, locator.marketplace_account_id,
        locator.marketplace, account.external_account_id, account.credential_ref)


def _closing_binding(session, locator):
    # Concrete closing handle already authenticates the executor and locks account.
    row = session.execute(select(MarketplaceAccountRow.external_account_id, MarketplaceAccountRow.credential_ref).where(
        MarketplaceAccountRow.organization_id == locator.organization_id,
        MarketplaceAccountRow.marketplace_account_id == locator.marketplace_account_id,
        MarketplaceAccountRow.marketplace == locator.marketplace)).one_or_none()
    _require(row is not None, "ACCESS_REVOKED")
    return _binding(locator, row)


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedReviewText:
    external_review_id: str
    text: str
    text_checksum: str

    def __repr__(self):
        return "<ApprovedReviewText redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("REVIEW_CONTRACT_INVALID")

    def __copy__(self):
        raise TypeError("REVIEW_CONTRACT_INVALID")

    def __deepcopy__(self, memo):
        raise TypeError("REVIEW_CONTRACT_INVALID")


def _current_draft(session, handle):
    """Only SELECT on immutable/source rows; account lock prevents writer drift."""
    intent, capture = handle.intent, handle.capture
    value = _intent_payload(intent)
    binding = _binding(intent.locator, capture.account)
    local = ReviewLocalRepository(session.connection(), binding)
    facts = ReviewFactsRepository(session.connection(), ReviewOwner(binding.organization_id,
        binding.marketplace_account_id, binding.marketplace, binding.external_account_id,
        binding.credential_ref), command_savepoints=False)
    source = facts.get_fact(value["externalReviewId"])
    _require(source is not None and source.review_id == intent.review_id and source.source_order_state == "current")
    _require(not source.answered and source.can_answer is True, "NOT_ANSWERABLE")
    head = local.get(WORKFLOW_HEAD, review_id=intent.review_id)
    draft = local.get(DRAFT, review_id=intent.review_id, draft_id=intent.draft_id)
    decision = local.get(DECISION, review_id=intent.review_id, decision_id=intent.decision_id)
    _require(head is not None and draft is not None and decision is not None)
    _require((head["current_draft_id"], head["current_draft_revision"], head["current_decision_id"])
             == (intent.draft_id, intent.draft_revision, intent.decision_id))
    _require((draft["source_observation_id"], draft["source_checksum"])
             == (source.current_observation_id, source.content_checksum))
    _require((decision["draft_id"], decision["draft_revision"], decision["binding_checksum"],
              decision["decision_kind"], decision["actor_membership_id"])
             == (intent.draft_id, intent.draft_revision, intent.binding_checksum, "approved", handle.approver_membership_id))
    ph = local.get(POLICY_HEAD)
    _require(ph is not None, "POLICY_CHANGED")
    policy = local.get(POLICY, policy_id=ph["current_policy_id"], version=ph["current_policy_version"])
    epoch = local.get(LOCAL_AUDIT, aggregate_kind="policy_head", aggregate_id=ph["head_id"], aggregate_version=ph["version"])
    _require(policy is not None and epoch is not None, "POLICY_CHANGED")
    _encoded(policy, "policy", encode_review_policy)
    _encoded(epoch, "audit", encode_review_local_audit)
    _encoded(draft, "generation", encode_review_generation)
    _require((draft["policy_head_id"], draft["policy_head_version"], draft["policy_selection_event_id"])
             == (ph["head_id"], ph["version"], epoch["event_id"]), "POLICY_CHANGED")
    _require(epoch["event_kind"] == "policy.selected"
             and (epoch["policy_id"], epoch["policy_version"]) == (policy["policy_id"], policy["version"])
             and ph["current_policy_checksum"] == policy["policy_checksum"], "POLICY_CHANGED")
    _require((draft["policy_id"], draft["policy_version"], draft["policy_checksum"], draft["template_version"], draft["model_version"])
             == (policy["policy_id"], policy["version"], policy["policy_checksum"], policy["template_version"], policy["model_version"]),
             "POLICY_CHANGED")
    identity = ExternalReviewIdentity(binding.organization_id, binding.marketplace_account_id,
                                     binding.marketplace, value["externalReviewId"])
    p = ReviewPolicyVersion(binding.organization_id, binding.marketplace_account_id, ReviewMarketplace(binding.marketplace),
        policy["policy_id"], int(policy["version"]), policy["policy_checksum"], policy["template_version"], policy["model_version"])
    current = ReviewDraftRevision(identity, draft["draft_id"], int(draft["revision"]), draft["source_observation_id"],
        draft["source_checksum"], p, bytes(draft["text_utf8"]).decode("utf-8"), draft["text_checksum"], draft["created_at"])
    _require(current.revision == intent.draft_revision and current.text_checksum == intent.text_checksum
             and current.binding_checksum == draft["binding_checksum"] == intent.binding_checksum)
    now = session.scalar(select(func.clock_timestamp()))
    _require(draft["created_at"] <= decision["decided_at"] <= now)
    return binding, ApprovedReviewText(value["externalReviewId"], current.text, current.text_checksum)


class ReviewSendService:
    def __init__(self, *, commands: ReviewJobCommands, executor: ReviewJobExecutor,
                 reconciliation: ReviewReconciliation):
        _require(type(commands) is ReviewJobCommands and type(executor) is ReviewJobExecutor
                 and type(reconciliation) is ReviewReconciliation)
        self.commands, self.executor, self.reconciliation = commands, executor, reconciliation

    def create(self, *, authenticated_actor, intent, policy, authority_expires_at):
        payload = _intent_payload(intent)

        def participant(session, handle):
            handle.require_participation(session, ReviewAction.CREATE)
            binding, _ = _current_draft(session, handle)
            ReviewSendRepository(session.connection(), binding).create(
                command_id=intent.locator.command_id, review_id=intent.review_id, idempotency_key=intent.idempotency_key,
                actor_membership_id=handle.principal.membership_id, intent=payload, authority=handle.authority_values)

        return self.commands.create(authenticated_actor=authenticated_actor, intent=intent, policy=policy,
                                    authority_expires_at=authority_expires_at, participant=participant)

    def cancel(self, *, authenticated_actor, locator, expected):
        """Exact queued CAS by the original sender's currently authorized session."""
        def participant(session, handle):
            handle.require_participation(session, ReviewAction.CANCEL)
            repository = ReviewSendRepository(session.connection(), _binding(locator, handle.capture.account))
            repository.transition(command_id=locator.command_id, expected_version=handle.expected.version,
                expected_attempt_id=None, lease_token=None, event_kind="send.cancelled",
                actor_kind="membership", actor_membership_id=handle.principal.membership_id,
                reason="USER_CANCELLED")

        return self.commands.cancel(authenticated_actor=authenticated_actor, locator=locator,
                                    expected=expected, participant=participant)

    def _initiation(self, action, *, locator, expected, resolved_credential=None):
        approved = []
        event = {ReviewAction.CLAIM: "send.claimed", ReviewAction.RENEW: "send.lease_renewed",
                 ReviewAction.RECLAIM: "send.reclaimed", ReviewAction.DISPATCH: "send.dispatched"}[action]

        def participant(session, handle):
            handle.require_participation(session, action)
            binding, text_value = _current_draft(session, handle)
            repository = ReviewSendRepository(session.connection(), binding)
            repository.transition(command_id=locator.command_id, expected_version=expected.version,
                expected_attempt_id=expected.attempt_id, lease_token=expected.lease_token, event_kind=event,
                actor_kind="worker", actor_membership_id=None,
                reason="LEASE_EXPIRED_UNDISPATCHED" if action is ReviewAction.RECLAIM else None)
            approved.append(text_value)

        method = {ReviewAction.CLAIM: self.executor.claim, ReviewAction.RENEW: self.executor.renew,
                  ReviewAction.RECLAIM: self.executor.reclaim, ReviewAction.DISPATCH: self.executor.mark_dispatch}[action]
        kwargs = {} if action is not ReviewAction.DISPATCH else {"resolved_credential": resolved_credential}
        result = method(locator=locator, expected=expected, participant=participant, **kwargs)
        # Callback output is private and only returned AFTER the shared physical commit.
        return (result, approved[0]) if action is ReviewAction.DISPATCH else result

    def claim(self, *, locator, expected):
        return self._initiation(ReviewAction.CLAIM, locator=locator, expected=expected)

    def renew(self, *, locator, expected):
        return self._initiation(ReviewAction.RENEW, locator=locator, expected=expected)

    def reclaim(self, *, locator, expected):
        return self._initiation(ReviewAction.RECLAIM, locator=locator, expected=expected)

    def mark_dispatch(self, *, locator, expected, resolved_credential):
        return self._initiation(ReviewAction.DISPATCH, locator=locator, expected=expected, resolved_credential=resolved_credential)

    def _closing(self, action, *, locator, expected, evidence=None, reason=None):
        _require(action not in {ReviewAction.ACK, ReviewAction.APPEND_ACK} or evidence is not None)
        # Capture immutable validated adapter bytes before entering the callback.
        frozen = None if evidence is None else encode_review_answer_evidence(evidence).canonical_bytes

        def participant(session, handle):
            handle.require_participation(session, action)
            binding = _closing_binding(session, locator)
            repository = ReviewSendRepository(session.connection(), binding)
            item = None if frozen is None else json.loads(frozen)
            if item is not None:
                _require(item["evidenceKind"] == "dispatch_ack"
                         and UUID(item["commandId"]) == locator.command_id and UUID(item["attemptId"]) == expected.attempt_id)
                repository.append_evidence(item)
            if action is ReviewAction.APPEND_ACK:
                return
            event, code = {ReviewAction.BLOCK: ("send.blocked", reason),
                           ReviewAction.AMBIGUOUS: ("send.ambiguous", "RESULT_UNKNOWN")}.get(action, (None, None))
            if action is ReviewAction.ACK:
                _require(item is not None and item["outcome"] in {"exact", "different"})
                event, code = (("send.sent", "VERIFIED_EXACT_ANSWER") if item["outcome"] == "exact"
                               else ("send.conflict", "VERIFIED_DIFFERENT_ANSWER"))
            row, audit = repository.transition(command_id=locator.command_id, expected_version=expected.version,
                expected_attempt_id=expected.attempt_id, lease_token=expected.lease_token, event_kind=event,
                actor_kind="worker", actor_membership_id=None, reason=code,
                evidence_id=None if item is None else UUID(item["evidenceId"]))
            if action in {ReviewAction.BLOCK, ReviewAction.AMBIGUOUS}:
                NotificationRepository(session.connection(), binding).publish(review_id=row["review_id"],
                    entity_id=locator.command_id, source_version=int(row["version"]),
                    kind="send_blocked" if action is ReviewAction.BLOCK else "send_ambiguous",
                    source_audit_event_id=audit["event_id"])

        method = {ReviewAction.BLOCK: self.executor.block_before_dispatch, ReviewAction.AMBIGUOUS: self.executor.mark_ambiguous,
                  ReviewAction.ACK: self.executor.close_ack, ReviewAction.APPEND_ACK: self.executor.append_ack}[action]
        return method(locator=locator, expected=expected, participant=participant)

    def block(self, *, locator, expected, reason):
        _require(reason in {"ACCESS_REVOKED", "SOURCE_CHANGED", "POLICY_CHANGED", "NOT_ANSWERABLE", "CREDENTIAL_UNAVAILABLE"})
        return self._closing(ReviewAction.BLOCK, locator=locator, expected=expected, reason=reason)

    def mark_ambiguous(self, *, locator, expected):
        return self._closing(ReviewAction.AMBIGUOUS, locator=locator, expected=expected)

    def close_ack(self, *, locator, expected, evidence):
        return self._closing(ReviewAction.ACK, locator=locator, expected=expected, evidence=evidence)

    def append_ack(self, *, locator, expected, evidence):
        return self._closing(ReviewAction.APPEND_ACK, locator=locator, expected=expected, evidence=evidence)

    def observe_ack(self, *, locator, expected, observation, verifier_version, retain_only=False):
        """Trusted transport observation → DB-timed evidence, not a supplied outcome.

        retain_only explicitly stores late ACK without granting closure. Incomplete
        ACK likewise stores only evidence; mark_ambiguous is a separate exact action.
        """
        from app.reviews.send_driver import ReviewAnswerObservation

        _require(type(observation) is ReviewAnswerObservation)
        observation.__post_init__()
        complete = observation.provider_answer_id is not None and observation.answer_text is not None
        action = ReviewAction.APPEND_ACK if retain_only or not complete else ReviewAction.ACK

        def participant(session, handle):
            handle.require_participation(session, action)
            repository = ReviewSendRepository(session.connection(), _closing_binding(session, locator))
            command = repository.get(COMMAND, command_id=locator.command_id)
            _require(command is not None)
            checksum = None if observation.answer_text is None else sha256(observation.answer_text.encode("utf-8")).hexdigest()
            outcome = "incomplete" if not complete else "exact" if checksum == command["text_checksum"] else "different"
            payload = {"evidenceVersion": "review-answer-evidence-v1", "evidenceKind": "dispatch_ack",
                "organizationId": locator.organization_id, "marketplaceAccountId": locator.marketplace_account_id,
                "marketplace": locator.marketplace, "evidenceId": str(uuid4()), "reviewId": str(command["review_id"]),
                "commandId": str(locator.command_id), "attemptId": str(expected.attempt_id),
                "outcome": outcome, "readId": None, "reconciliationStartedAt": None,
                "observedAt": session.scalar(select(func.clock_timestamp())).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "providerAnswerId": observation.provider_answer_id, "answerChecksum": checksum, "verifierVersion": verifier_version}
            repository.append_evidence(payload)
            if action is ReviewAction.ACK:
                exact = outcome == "exact"
                repository.transition(command_id=locator.command_id, expected_version=expected.version,
                    expected_attempt_id=expected.attempt_id, lease_token=expected.lease_token,
                    event_kind="send.sent" if exact else "send.conflict", actor_kind="worker", actor_membership_id=None,
                    reason="VERIFIED_EXACT_ANSWER" if exact else "VERIFIED_DIFFERENT_ANSWER",
                    evidence_id=UUID(payload["evidenceId"]))

        method = self.executor.append_ack if action is ReviewAction.APPEND_ACK else self.executor.close_ack
        return method(locator=locator, expected=expected, participant=participant)

    def publish_reconciliation(self, *, authenticated_actor, read_authority, evidence):
        frozen = encode_review_answer_evidence(evidence).canonical_bytes
        return self._publish_reconciliation(authenticated_actor=authenticated_actor,
            read_authority=read_authority, build_evidence=lambda session, read: json.loads(frozen))

    def observe_reconciliation(self, *, authenticated_actor, read_authority, observation, verifier_version):
        """Trusted fresh GET observation, scoped to the sealed captured intent."""
        from app.reviews.send_driver import ReviewAnswerObservation

        _require(type(observation) is ReviewAnswerObservation)
        observation.__post_init__()

        def build_evidence(session, read):
            _intent_payload(read.intent)
            complete = observation.provider_answer_id is not None and observation.answer_text is not None
            checksum = None if observation.answer_text is None else sha256(observation.answer_text.encode("utf-8")).hexdigest()
            outcome = "incomplete" if not complete else "exact" if checksum == read.intent.text_checksum else "different"
            return {"evidenceVersion": "review-answer-evidence-v1", "evidenceKind": "reconciliation_read",
                "organizationId": read.locator.organization_id, "marketplaceAccountId": read.locator.marketplace_account_id,
                "marketplace": read.locator.marketplace, "evidenceId": str(uuid4()), "reviewId": str(read.intent.review_id),
                "commandId": str(read.locator.command_id), "attemptId": str(read.expected.attempt_id),
                "outcome": outcome, "readId": str(read.read_id),
                "reconciliationStartedAt": read.started_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "observedAt": session.scalar(select(func.clock_timestamp())).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "providerAnswerId": observation.provider_answer_id, "answerChecksum": checksum, "verifierVersion": verifier_version}

        return self._publish_reconciliation(authenticated_actor=authenticated_actor,
            read_authority=read_authority, build_evidence=build_evidence)

    def _publish_reconciliation(self, *, authenticated_actor, read_authority, build_evidence):

        def participant(session, handle):
            handle.require_participation(session, ReviewAction.RECONCILE)
            read = handle.read_authority
            item = json.loads(encode_review_answer_evidence(build_evidence(session, read)).canonical_bytes)
            _require(item["evidenceKind"] == "reconciliation_read" and UUID(item["readId"]) == read.read_id
                     and UUID(item["commandId"]) == read.locator.command_id and UUID(item["attemptId"]) == read.expected.attempt_id)
            repository = ReviewSendRepository(session.connection(), _binding(read.locator, read.account))
            repository.append_evidence(item)
            if item["outcome"] == "incomplete":
                return  # A member cannot invent worker-only send.ambiguous.
            exact = item["outcome"] == "exact"
            repository.transition(command_id=read.locator.command_id, expected_version=read.expected.version,
                expected_attempt_id=read.expected.attempt_id, lease_token=read.expected.lease_token,
                event_kind="send.sent" if exact else "send.conflict", actor_kind="membership",
                actor_membership_id=handle.principal.membership_id,
                reason="VERIFIED_EXACT_ANSWER" if exact else "VERIFIED_DIFFERENT_ANSWER",
                evidence_id=UUID(item["evidenceId"]))

        return self.reconciliation.publish(authenticated_actor=authenticated_actor,
                                            read_authority=read_authority, participant=participant)
