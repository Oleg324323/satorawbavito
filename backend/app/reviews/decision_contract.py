"""Pure Reviews predicates, not persistence, authentication or send authority.

The application must load these values from scoped durable storage, revalidate
memberships and atomically CAS the draft/decision/audit in its transaction.
Never deserialize an HTTP ``approved=true`` into a trusted decision. Successful
validation here neither reserves a command nor permits an external call by itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID

from app.reviews.canonical_contract import (
    ExternalReviewIdentity, NormalizedReviewFact, ReviewMarketplace,
    review_fact_checksum,
)


class ReviewDecisionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str = "REVIEW_INPUT_INVALID") -> None:
    if not condition:
        raise ReviewDecisionError(code)


def _integer(value: object, minimum: int = 1) -> None:
    _require(type(value) is int and value >= minimum)


def _uuid(value: object) -> None:
    _require(isinstance(value, UUID) and value.int != 0)


def _checksum(value: object) -> None:
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _label(value: object) -> None:
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value) is not None)


def _time(value: object) -> None:
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None)


def _text(value: object) -> None:
    _require(isinstance(value, str) and bool(value.strip()))
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ReviewDecisionError("REVIEW_INPUT_INVALID") from None


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewSourceEvidence:
    observation_id: UUID
    fact: NormalizedReviewFact = field(repr=False)
    ordering_state: str

    def __post_init__(self) -> None:
        _uuid(self.observation_id)
        _require(isinstance(self.fact, NormalizedReviewFact), "REVIEW_SOURCE_INVALID")
        _require(self.ordering_state in {"current", "ambiguous"})
        try:
            valid = self.fact.content_checksum == review_fact_checksum(self.fact)
        except (ValueError, TypeError, UnicodeError):
            raise ReviewDecisionError("REVIEW_SOURCE_INVALID") from None
        _require(valid, "REVIEW_SOURCE_INVALID")


@dataclass(frozen=True, slots=True)
class ReviewPolicyVersion:
    organization_id: int
    marketplace_account_id: int
    marketplace: ReviewMarketplace
    policy_id: UUID
    version: int
    checksum: str
    template_version: str
    model_version: str

    def __post_init__(self) -> None:
        _integer(self.organization_id)
        _integer(self.marketplace_account_id)
        _require(isinstance(self.marketplace, ReviewMarketplace))
        _uuid(self.policy_id)
        _integer(self.version)
        _checksum(self.checksum)
        _label(self.template_version)
        _label(self.model_version)


@dataclass(frozen=True, slots=True)
class ReviewActorScope:
    """Fresh server-loaded membership projection, not a user-supplied claim."""

    organization_id: int
    membership_id: int
    active: bool
    allowed_account_ids: frozenset[int]
    permissions: frozenset[str]

    def __post_init__(self) -> None:
        _integer(self.organization_id)
        _integer(self.membership_id)
        _require(type(self.active) is bool)
        _require(isinstance(self.allowed_account_ids, frozenset))
        for account_id in self.allowed_account_ids:
            _integer(account_id)
        _require(isinstance(self.permissions, frozenset))
        for permission in self.permissions:
            _label(permission)


@dataclass(frozen=True, slots=True)
class ReviewDraftRevision:
    identity: ExternalReviewIdentity
    draft_id: UUID
    revision: int
    source_observation_id: UUID
    source_checksum: str
    policy: ReviewPolicyVersion
    text: str = field(repr=False)
    text_checksum: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require(isinstance(self.identity, ExternalReviewIdentity))
        _uuid(self.draft_id)
        _integer(self.revision)
        _uuid(self.source_observation_id)
        _checksum(self.source_checksum)
        _require(isinstance(self.policy, ReviewPolicyVersion))
        _policy_scope(self.identity, self.policy)
        _text(self.text)
        _checksum(self.text_checksum)
        _require(hashlib.sha256(self.text.encode("utf-8")).hexdigest() == self.text_checksum)
        _time(self.created_at)

    @property
    def binding_checksum(self) -> str:
        return _digest({
            "contract": "review-decision-v1",
            "owner": [self.identity.organization_id, self.identity.marketplace_account_id,
                      self.identity.marketplace.value, self.identity.external_review_id],
            "draft": [str(self.draft_id), self.revision, self.text_checksum],
            "source": [str(self.source_observation_id), self.source_checksum],
            "policy": [str(self.policy.policy_id), self.policy.version, self.policy.checksum,
                       self.policy.template_version, self.policy.model_version],
        })


class ReviewDecisionKind(str, Enum):
    approved = "approved"
    rejected = "rejected"


@dataclass(frozen=True, slots=True)
class ReviewApprovalDecision:
    decision_id: UUID
    identity: ExternalReviewIdentity
    draft_id: UUID
    draft_revision: int
    binding_checksum: str
    actor_membership_id: int
    kind: ReviewDecisionKind
    decided_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.decision_id)
        _require(isinstance(self.identity, ExternalReviewIdentity))
        _uuid(self.draft_id)
        _integer(self.draft_revision)
        _checksum(self.binding_checksum)
        _integer(self.actor_membership_id)
        _require(isinstance(self.kind, ReviewDecisionKind))
        _time(self.decided_at)


def _policy_scope(identity: ExternalReviewIdentity, policy: ReviewPolicyVersion) -> None:
    _require((identity.organization_id, identity.marketplace_account_id, identity.marketplace)
             == (policy.organization_id, policy.marketplace_account_id, policy.marketplace),
             "REVIEW_SCOPE_MISMATCH")


def _access(actor: ReviewActorScope, identity: ExternalReviewIdentity, permission: str) -> None:
    _require(isinstance(actor, ReviewActorScope))
    _require(actor.active and actor.organization_id == identity.organization_id
             and identity.marketplace_account_id in actor.allowed_account_ids
             and permission in actor.permissions, "REVIEW_ACCESS_DENIED")


def _context(draft: ReviewDraftRevision, source: ReviewSourceEvidence, policy: ReviewPolicyVersion) -> None:
    _require(isinstance(draft, ReviewDraftRevision) and isinstance(source, ReviewSourceEvidence)
             and isinstance(policy, ReviewPolicyVersion))
    _require(source.ordering_state == "current", "REVIEW_SOURCE_AMBIGUOUS")
    _require(source.fact.identity == draft.identity
             and source.observation_id == draft.source_observation_id
             and source.fact.content_checksum == draft.source_checksum, "REVIEW_SOURCE_CHANGED")
    _require(policy == draft.policy, "REVIEW_POLICY_CHANGED")


def _answerable(source: ReviewSourceEvidence) -> None:
    _require(not source.fact.answered and source.fact.can_answer is True, "REVIEW_NOT_ANSWERABLE")


def _current(draft: ReviewDraftRevision, current_draft_id: UUID, current_draft_version: int) -> None:
    _uuid(current_draft_id)
    _integer(current_draft_version, 0)
    _require(current_draft_id == draft.draft_id and current_draft_version == draft.revision,
             "REVIEW_STALE_DRAFT")


def build_review_draft(*, source: ReviewSourceEvidence, policy: ReviewPolicyVersion,
                       draft_id: UUID, expected_version: int, text: str,
                       created_at: datetime) -> ReviewDraftRevision:
    """Record an output (including fake LLM output); no generation or persistence."""
    _require(isinstance(source, ReviewSourceEvidence) and isinstance(policy, ReviewPolicyVersion))
    _integer(expected_version, 0)
    _text(text)
    return ReviewDraftRevision(source.fact.identity, draft_id, expected_version + 1,
                               source.observation_id, source.fact.content_checksum, policy,
                               text, hashlib.sha256(text.encode("utf-8")).hexdigest(), created_at)


def validate_review_draft_publication(draft: ReviewDraftRevision, *, source: ReviewSourceEvidence,
                                      policy: ReviewPolicyVersion, current_version: int) -> None:
    """Predicate to repeat inside the future repository's atomic CAS transaction."""
    _context(draft, source, policy)
    _integer(current_version, 0)
    _require(draft.revision == current_version + 1, "REVIEW_STALE_DRAFT")


def decide_review_draft(draft: ReviewDraftRevision, *, source: ReviewSourceEvidence,
                        policy: ReviewPolicyVersion, actor: ReviewActorScope,
                        current_draft_id: UUID, current_draft_version: int,
                        decision_id: UUID, kind: ReviewDecisionKind,
                        decided_at: datetime) -> ReviewApprovalDecision:
    _context(draft, source, policy)
    _current(draft, current_draft_id, current_draft_version)
    _access(actor, draft.identity, "reviews:approve")
    _time(decided_at)
    _require(decided_at >= draft.created_at)
    _require(isinstance(kind, ReviewDecisionKind))
    if kind is ReviewDecisionKind.approved:
        _answerable(source)
    return ReviewApprovalDecision(decision_id, draft.identity, draft.draft_id, draft.revision,
                                  draft.binding_checksum, actor.membership_id, kind, decided_at)


def validate_review_send(draft: ReviewDraftRevision, approval: ReviewApprovalDecision, *,
                         source: ReviewSourceEvidence, policy: ReviewPolicyVersion,
                         approver: ReviewActorScope, sender: ReviewActorScope,
                         current_draft_id: UUID, current_draft_version: int,
                         current_decision_id: UUID,
                         now: datetime) -> None:
    """No send. Caller supplies the latest decision ID from locked durable state.

    Account binding/status, approval revocation/current-decision pointer, command
    uniqueness, lease and dispatch/ambiguity gates belong to the DB/worker layer.
    """
    _context(draft, source, policy)
    _current(draft, current_draft_id, current_draft_version)
    _require(isinstance(approval, ReviewApprovalDecision))
    _uuid(current_decision_id)
    _require(approval.decision_id == current_decision_id, "REVIEW_APPROVAL_SUPERSEDED")
    _require(approval.kind is ReviewDecisionKind.approved, "REVIEW_APPROVAL_REQUIRED")
    _require(approval.identity == draft.identity and approval.draft_id == draft.draft_id
             and approval.draft_revision == draft.revision
             and approval.binding_checksum == draft.binding_checksum, "REVIEW_APPROVAL_MISMATCH")
    _access(approver, draft.identity, "reviews:approve")
    _access(sender, draft.identity, "reviews:send")
    _require(approver.membership_id == approval.actor_membership_id, "REVIEW_APPROVER_MISMATCH")
    _answerable(source)
    _time(now)
    _require(draft.created_at <= approval.decided_at <= now)
