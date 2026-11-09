from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.reviews.canonical_contract import ReviewMarketplace, normalize_wb_review
from app.reviews.decision_contract import (
    ReviewActorScope, ReviewDecisionError, ReviewDecisionKind,
    ReviewPolicyVersion, ReviewSourceEvidence, build_review_draft,
    decide_review_draft, validate_review_draft_publication, validate_review_send,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
CUSTOMER = "synthetic-private-customer-text"
ANSWER = "synthetic-private-approved-answer"


def source(**overrides):
    payload = dict(id="wb-007", nmId=101, createdDate=NOW,
                   text=CUSTOMER, rating=5, isAnswered=False, canAnswer=True)
    payload.update(overrides)
    fact = normalize_wb_review(payload, 1, 11, "synthetic-run", NOW)
    return ReviewSourceEvidence(UUID(int=1), fact, "current")


def policy():
    return ReviewPolicyVersion(1, 11, ReviewMarketplace.WB, UUID(int=2), 3,
                               "a" * 64, "template-v2", "fake-model-v1")


def actor(membership_id=7, permissions=frozenset({"reviews:approve", "reviews:send"})):
    return ReviewActorScope(1, membership_id, True, frozenset({11}), permissions)


def draft(src=None, pol=None, **kwargs):
    return build_review_draft(source=src or source(), policy=pol or policy(),
                              draft_id=kwargs.get("draft_id", UUID(int=3)),
                              expected_version=kwargs.get("expected_version", 0),
                              text=kwargs.get("text", ANSWER), created_at=NOW)


def decision(d=None, **kwargs):
    d = d or draft()
    return decide_review_draft(d, source=kwargs.get("source", source()),
                               policy=kwargs.get("policy", policy()),
                               actor=kwargs.get("actor", actor()),
                               current_draft_id=kwargs.get("current_draft_id", d.draft_id),
                               current_draft_version=kwargs.get("current_draft_version", d.revision),
                               decision_id=UUID(int=4),
                               kind=kwargs.get("kind", ReviewDecisionKind.approved),
                               decided_at=NOW + timedelta(seconds=1))


def send(d=None, approval=None, **kwargs):
    d = d or draft()
    approval = approval or decision(d)
    return validate_review_send(d, approval, source=kwargs.get("source", source()),
                                policy=kwargs.get("policy", policy()),
                                approver=kwargs.get("approver", actor()),
                                sender=kwargs.get("sender", actor(8)),
                                current_draft_id=kwargs.get("current_draft_id", d.draft_id),
                                current_draft_version=kwargs.get("current_draft_version", d.revision),
                                current_decision_id=kwargs.get("current_decision_id", approval.decision_id),
                                now=NOW + timedelta(seconds=2))


def test_exact_immutable_approval_validates_without_sending_or_mutating():
    d = draft()
    a = decision(d)
    assert send(d, a) is None
    assert d.text == ANSWER
    assert a.draft_id == d.draft_id
    assert a.draft_revision == 1
    assert a.actor_membership_id == 7
    with pytest.raises(FrozenInstanceError):
        d.text = "another answer"


@pytest.mark.parametrize("kwargs,code", [
    ({"current_draft_id": UUID(int=50)}, "REVIEW_STALE_DRAFT"),
    ({"current_draft_version": 2}, "REVIEW_STALE_DRAFT"),
    ({"source": replace(source(), observation_id=UUID(int=51))}, "REVIEW_SOURCE_CHANGED"),
    ({"source": source(text="changed")}, "REVIEW_SOURCE_CHANGED"),
    ({"source": replace(source(), ordering_state="ambiguous")}, "REVIEW_SOURCE_AMBIGUOUS"),
    ({"policy": replace(policy(), version=4)}, "REVIEW_POLICY_CHANGED"),
    ({"policy": replace(policy(), checksum="b" * 64)}, "REVIEW_POLICY_CHANGED"),
    ({"policy": replace(policy(), template_version="template-v3")}, "REVIEW_POLICY_CHANGED"),
    ({"policy": replace(policy(), model_version="fake-model-v2")}, "REVIEW_POLICY_CHANGED"),
])
def test_stale_approval_context_is_rejected(kwargs, code):
    with pytest.raises(ReviewDecisionError) as error:
        send(**kwargs)
    assert error.value.code == code


@pytest.mark.parametrize("overrides", [dict(isAnswered=True), dict(canAnswer=False), dict(canAnswer=None)])
def test_ineligible_source_cannot_be_approved(overrides):
    src = source(**overrides)
    with pytest.raises(ReviewDecisionError, match="REVIEW_NOT_ANSWERABLE"):
        decision(draft(src), source=src)


@pytest.mark.parametrize("who", ["approver", "sender"])
@pytest.mark.parametrize("change", ["revoked", "organization", "account", "permission"])
def test_send_rechecks_both_live_memberships(who, change):
    value = actor(7 if who == "approver" else 8)
    value = replace(value, **{
        "revoked": {"active": False}, "organization": {"organization_id": 2},
        "account": {"allowed_account_ids": frozenset({12})},
        "permission": {"permissions": frozenset()},
    }[change])
    with pytest.raises(ReviewDecisionError, match="REVIEW_ACCESS_DENIED"):
        send(**{who: value})


def test_a_different_active_approver_cannot_substitute_the_revoked_original():
    with pytest.raises(ReviewDecisionError, match="REVIEW_APPROVER_MISMATCH"):
        send(approver=actor(9))


def test_rejection_is_not_send_authority():
    d = draft()
    rejected = decision(d, kind=ReviewDecisionKind.rejected)
    with pytest.raises(ReviewDecisionError, match="REVIEW_APPROVAL_REQUIRED"):
        send(d, rejected)


def test_old_approval_cannot_bypass_a_newer_rejection_or_revocation():
    with pytest.raises(ReviewDecisionError, match="REVIEW_APPROVAL_SUPERSEDED"):
        send(current_decision_id=UUID(int=99))


def test_changed_text_with_reused_draft_id_cannot_reuse_approval():
    original = draft()
    edited = draft(text="synthetic-new-answer")
    with pytest.raises(ReviewDecisionError, match="REVIEW_APPROVAL_MISMATCH"):
        send(edited, decision(original))


def test_naive_send_time_fails_closed():
    d = draft()
    a = decision(d)
    with pytest.raises(ReviewDecisionError, match="REVIEW_INPUT_INVALID"):
        validate_review_send(d, a, source=source(), policy=policy(), approver=actor(), sender=actor(8),
                             current_draft_id=d.draft_id, current_draft_version=1,
                             current_decision_id=a.decision_id, now=NOW.replace(tzinfo=None))


def test_same_text_regeneration_does_not_reuse_old_approval():
    first = draft()
    old_approval = decision(first)
    second = draft(draft_id=UUID(int=5), expected_version=1)
    assert first.text_checksum == second.text_checksum
    with pytest.raises(ReviewDecisionError, match="REVIEW_APPROVAL_MISMATCH"):
        send(second, old_approval)


def test_publication_predicate_rejects_a_late_generation_after_another_won():
    first = draft()
    second = draft(draft_id=UUID(int=5))
    assert validate_review_draft_publication(first, source=source(), policy=policy(), current_version=0) is None
    with pytest.raises(ReviewDecisionError, match="REVIEW_STALE_DRAFT"):
        validate_review_draft_publication(second, source=source(), policy=policy(), current_version=1)
    # This is only a predicate: the future repository must atomically persist CAS.
    assert second.revision == 1


def test_policy_cannot_cross_account_or_provider():
    for pol in (replace(policy(), marketplace_account_id=12),
                replace(policy(), organization_id=2), replace(policy(), marketplace=ReviewMarketplace.AVITO)):
        with pytest.raises(ReviewDecisionError, match="REVIEW_SCOPE_MISMATCH"):
            draft(pol=pol)


def test_untrusted_fact_checksum_is_recomputed_before_acceptance():
    src = source()
    with pytest.raises(ReviewDecisionError, match="REVIEW_SOURCE_INVALID"):
        replace(src, fact=replace(src.fact, text="forged text"))


@pytest.mark.parametrize("value", ["", "   ", None, 42])
def test_empty_or_non_text_generation_is_not_a_draft(value):
    with pytest.raises(ReviewDecisionError, match="REVIEW_INPUT_INVALID"):
        draft(text=value)


@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_generation_version_is_strict(value):
    with pytest.raises(ReviewDecisionError, match="REVIEW_INPUT_INVALID"):
        draft(expected_version=value)


def test_naive_and_reversed_decision_times_are_rejected():
    d = draft()
    for when in (NOW.replace(tzinfo=None), NOW - timedelta(seconds=1)):
        with pytest.raises(ReviewDecisionError, match="REVIEW_INPUT_INVALID"):
            decide_review_draft(d, source=source(), policy=policy(), actor=actor(),
                                current_draft_id=d.draft_id, current_draft_version=1,
                                decision_id=UUID(int=6), kind=ReviewDecisionKind.approved, decided_at=when)


def test_private_source_and_answer_never_appear_in_repr_or_errors():
    src, d = source(), draft()
    values = repr((src, d, decision(d)))
    assert CUSTOMER not in values and ANSWER not in values
    with pytest.raises(ReviewDecisionError) as error:
        draft(text=None)
    assert CUSTOMER not in str(error.value) and ANSWER not in str(error.value)
