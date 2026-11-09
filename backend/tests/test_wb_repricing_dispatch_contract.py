import inspect
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from app.modules.wb_repricing import (
    SAFE_APPLY_ERROR_CODES,
    ApprovalConflictError,
    ApprovalStatus,
    ApprovalValidationError,
    PriceApprovalSnapshot,
    build_action_key,
    claim_approval,
)
from app.modules.wb_repricing_dispatch import (
    ApplyOutcome,
    ApprovalAuditEvent,
    AttemptStatus,
    AuditActorKind,
    AuditKind,
    CanonicalApplyRequest,
    bind_request,
    build_dispatch_key,
    finish_attempt,
    mark_dispatched,
    reserve_attempt,
)
from app.modules.wb_repricing_repository import (
    ApprovalRepositoryScope,
    AuthenticatedApprovalActor,
    PriceApprovalRepository,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)
SCOPE = ApprovalRepositoryScope(7, 42, "approval-1")
ACTOR = AuthenticatedApprovalActor(7, 314)
ATTEMPT_ID = "12345678-1234-4234-8234-123456789abc"


def request(**kwargs):
    return CanonicalApplyRequest(SCOPE, 99, 123, "FBBT_42", **{
        "price_kopecks": 129950, "discount_pct": 0, **kwargs,
    })


def approval(req=None):
    req = req or request()
    return PriceApprovalSnapshot(
        7, "42", "approval-1", "99", 123, "FBBT_42", req.price_kopecks,
        req.checksum, ApprovalStatus.pending, 0,
        build_action_key(7, "42", "approval-1", req.checksum), NOW, NOW,
    )


def reserved():
    req = request()
    applying = claim_approval(approval(req), 0, "314", NOW)
    return applying, reserve_attempt(applying, req, 1, ACTOR, ATTEMPT_ID, NOW)


def test_canonical_request_binds_exact_typed_intent_and_wire_bytes():
    req = request()
    expected = (b'{"accountId":42,"approvalId":"approval-1","articleId":"FBBT_42",'
                b'"catalogSkuId":99,"discountPct":0,"minPriceKopecks":null,"nmId":123,'
                b'"organizationId":7,"priceKopecks":129950,"schema":"wb-price-apply/v1",'
                b'"sizeId":null}')
    assert req.canonical_bytes == expected
    assert req.checksum == sha256(expected).hexdigest()
    assert req.provider_bytes == b'{"data":[{"discount":0,"nmID":123,"price":1300}]}'
    original = approval(req)
    assert bind_request(original, req) is original
    with pytest.raises(FrozenInstanceError):
        req.price_kopecks = 1


@pytest.mark.parametrize("value,want", [(129949,1299),(129950,1300),(129951,1300)])
def test_upload_rounding_preserves_existing_half_up_boundary(value, want):
    import json
    assert json.loads(request(price_kopecks=value).provider_bytes)["data"][0]["price"] == want


def test_optional_size_minimum_and_discount_are_not_lost_in_request_binding():
    req = request(size_id=321, min_price_kopecks=120050, discount_pct=12)
    assert req.provider_bytes == b'{"data":[{"discount":12,"minPrice":1201,"nmID":123,"price":1300,"sizeID":321}]}'
    assert req.checksum != request().checksum


@pytest.mark.parametrize("field,value", [
    ("price_kopecks", True), ("price_kopecks", 1.0), ("price_kopecks", 0),
    ("price_kopecks", 49), ("discount_pct", True), ("discount_pct", 100),
    ("discount_pct", -1), ("size_id", "321"), ("min_price_kopecks", 1),
    ("article_id", "x" * 5000),
])
def test_invalid_or_oversized_request_is_rejected(field,value):
    with pytest.raises(ApprovalValidationError):
        replace(request(), **{field:value})


@pytest.mark.parametrize("field,value", [
    ("scope", ApprovalRepositoryScope(7,43,"approval-1")),
    ("catalog_sku_id", 100), ("nm_id", 124), ("article_id", "FBBT_43"),
    ("discount_pct", 1), ("size_id", 321), ("min_price_kopecks", 120000),
])
def test_same_approval_cannot_bind_changed_payload_or_scope(field,value):
    with pytest.raises(ApprovalValidationError):
        bind_request(approval(), replace(request(), **{field:value}))


def test_legacy_checksum_is_blocked_without_rewriting_snapshot():
    legacy = replace(approval(), request_checksum="a"*64,
                     action_key=build_action_key(7,"42","approval-1","a"*64))
    with pytest.raises(ApprovalValidationError):
        bind_request(legacy, request())
    assert legacy.request_checksum == "a"*64


def test_reserve_and_dispatch_use_attempt_version_not_approval_version():
    applying, attempt = reserved()
    marked = mark_dispatched(applying, attempt, 1, 0, ACTOR, NOW)
    assert attempt.status is AttemptStatus.reserved and attempt.version == 0
    assert marked.status is AttemptStatus.dispatched and marked.version == 1
    assert marked.dispatch_at == NOW
    assert applying.version == 1
    assert marked.dispatch_key == build_dispatch_key(SCOPE, applying.action_key, ATTEMPT_ID)
    with pytest.raises(ApprovalConflictError):
        mark_dispatched(applying, marked, 1, 1, ACTOR, NOW)
    # Calling a pure function twice on the old value is NOT shared-state CAS proof.


@pytest.mark.parametrize("changed", [
    ApprovalRepositoryScope(8,42,"approval-1"),
    ApprovalRepositoryScope(7,43,"approval-1"),
    ApprovalRepositoryScope(7,42,"approval-2"),
])
def test_dispatch_key_is_account_and_approval_scoped(changed):
    assert build_dispatch_key(SCOPE,"a"*64,ATTEMPT_ID) != build_dispatch_key(changed,"a"*64,ATTEMPT_ID)


@pytest.mark.parametrize("invalid", ["attempt-1", "", ATTEMPT_ID.upper(), "12345678-1234-1234-8234-123456789abc"])
def test_attempt_id_requires_canonical_server_uuid4(invalid):
    applying, _ = reserved()
    with pytest.raises(ApprovalValidationError):
        reserve_attempt(applying,request(),1,ACTOR,invalid,NOW)


@pytest.mark.parametrize("command", ["reserve","dispatch","outcome"])
def test_stale_approval_version_blocks_every_attempt_command(command):
    applying, attempt = reserved()
    with pytest.raises(ApprovalConflictError):
        if command == "reserve":
            reserve_attempt(applying,request(),0,ACTOR,ATTEMPT_ID,NOW)
        elif command == "dispatch":
            mark_dispatched(applying,attempt,0,0,ACTOR,NOW)
        else:
            finish_attempt(applying,attempt,0,0,ApplyOutcome(AttemptStatus.failed,safe_error_code="INTERNAL_APPLY_ERROR"),NOW)


@pytest.mark.parametrize("command", ["reserve","dispatch"])
def test_foreign_or_different_claim_actor_is_blocked(command):
    applying, attempt = reserved()
    for actor in (AuthenticatedApprovalActor(8,314), AuthenticatedApprovalActor(7,315)):
        with pytest.raises(ApprovalValidationError):
            if command == "reserve":
                reserve_attempt(applying,request(),1,actor,ATTEMPT_ID,NOW)
            else:
                mark_dispatched(applying,attempt,1,0,actor,NOW)


@pytest.mark.parametrize("status", [AttemptStatus.applied,AttemptStatus.failed,AttemptStatus.ambiguous])
def test_post_dispatch_outcomes_are_terminal_and_keep_marker(status):
    applying, attempt = reserved()
    marked = mark_dispatched(applying,attempt,1,0,ACTOR,NOW)
    outcome = (ApplyOutcome(status,wb_upload_id="upload-1",result_code="WB_APPLY_ACCEPTED")
               if status is AttemptStatus.applied else
               ApplyOutcome(status,safe_error_code="WB_APPLY_REJECTED" if status is AttemptStatus.failed else "WB_RESULT_UNAVAILABLE"))
    result = finish_attempt(applying,marked,1,1,outcome,NOW)
    assert result.approval.version == 2
    assert result.attempt.version == 2
    assert result.attempt.dispatch_at == NOW
    assert result.approval.status.value == status.value
    with pytest.raises(ApprovalConflictError):
        finish_attempt(result.approval,result.attempt,2,2,outcome,NOW)


def test_pre_dispatch_failure_closes_without_marker_and_uncertainty_cannot_be_failed():
    applying, attempt = reserved()
    result = finish_attempt(applying,attempt,1,0,ApplyOutcome(AttemptStatus.failed,safe_error_code="WB_APPLY_AUTHORIZATION_FAILED"),NOW)
    assert result.attempt.version == 1 and result.attempt.dispatch_at is None
    marked = mark_dispatched(applying,attempt,1,0,ACTOR,NOW)
    with pytest.raises(ApprovalValidationError):
        finish_attempt(applying,marked,1,1,ApplyOutcome(AttemptStatus.failed,safe_error_code="WB_APPLY_TIMEOUT"),NOW)


@pytest.mark.parametrize("outcome", [
    (AttemptStatus.applied,{}),
    (AttemptStatus.ambiguous,{"safe_error_code":"TimeoutError_secret"}),
    (AttemptStatus.failed,{"safe_error_code":"raw exception text"}),
    (AttemptStatus.dispatched,{}),
    (AttemptStatus.applied,{"wb_upload_id":"u","result_code":"OK","safe_error_code":"INTERNAL_APPLY_ERROR"}),
])
def test_outcome_rejects_raw_errors_and_inconsistent_metadata(outcome):
    status,kwargs=outcome
    with pytest.raises(ApprovalValidationError):
        ApplyOutcome(status,**kwargs)


def test_legacy_stored_safe_code_remains_readable_but_not_a_new_failure_command():
    applying, _ = reserved()
    legacy = replace(applying,status=ApprovalStatus.failed,safe_error_code="LEGACY_SAFE_CODE")
    assert legacy.safe_error_code == "LEGACY_SAFE_CODE"
    assert "LEGACY_SAFE_CODE" not in SAFE_APPLY_ERROR_CODES
    with pytest.raises(ApprovalValidationError):
        ApplyOutcome(AttemptStatus.failed,safe_error_code=legacy.safe_error_code)


def test_audit_requires_membership_for_human_and_null_for_worker():
    event = ApprovalAuditEvent(SCOPE,AuditKind.claimed,AuditActorKind.membership,314,
                              ApprovalStatus.pending,ApprovalStatus.applying,0,1,NOW)
    assert event.actor_membership_id == 314
    with pytest.raises(ApprovalValidationError):
        replace(event,actor_membership_id=None)
    with pytest.raises(ApprovalValidationError):
        replace(event,actor_kind=AuditActorKind.worker)
    with pytest.raises(ApprovalValidationError):
        replace(event,after_status=ApprovalStatus.applied)


def test_repository_has_explicit_scoped_reserve_and_dispatch_commands():
    for method in ("create_intent","reserve_attempt","mark_dispatch","record_attempt_outcome","reject","block"):
        params = inspect.signature(getattr(PriceApprovalRepository,method)).parameters
        assert "scope" in params
    assert "attempt_id" not in inspect.signature(PriceApprovalRepository.reserve_attempt).parameters
    assert "expected_attempt_version" in inspect.signature(PriceApprovalRepository.mark_dispatch).parameters


@pytest.mark.parametrize("now", [NOW.replace(tzinfo=None), NOW-timedelta(seconds=1)])
def test_attempt_time_cannot_be_naive_or_go_backwards(now):
    applying, attempt=reserved()
    with pytest.raises(ApprovalValidationError):
        mark_dispatched(applying,attempt,1,0,ACTOR,now)


@pytest.mark.parametrize("kind,after,attempt_before,attempt_after,metadata", [
    (AuditKind.reserved,ApprovalStatus.applying,None,0,{}),
    (AuditKind.dispatched,ApprovalStatus.applying,0,1,{}),
    (AuditKind.applied,ApprovalStatus.applied,1,2,{"wb_upload_id":"u","result_code":"OK"}),
    (AuditKind.failed,ApprovalStatus.failed,0,1,{"safe_error_code":"INTERNAL_APPLY_ERROR"}),
    (AuditKind.failed,ApprovalStatus.failed,1,2,{"safe_error_code":"WB_APPLY_REJECTED"}),
    (AuditKind.ambiguous,ApprovalStatus.ambiguous,1,2,{"safe_error_code":"WB_RESULT_UNAVAILABLE"}),
])
def test_audit_attempt_matrix_preserves_state_and_version_semantics(kind,after,attempt_before,attempt_after,metadata):
    member = kind in (AuditKind.reserved,AuditKind.dispatched)
    event = ApprovalAuditEvent(SCOPE,kind,AuditActorKind.membership if member else AuditActorKind.worker,
                              314 if member else None,ApprovalStatus.applying,after,1,1 if member else 2,NOW,
                              ATTEMPT_ID,attempt_before,attempt_after,**metadata)
    with pytest.raises(ApprovalValidationError):
        replace(event,after_version=event.after_version+1)
    with pytest.raises(ApprovalValidationError):
        replace(event,attempt_id=None)
    with pytest.raises(ApprovalValidationError):
        replace(event,reason_code="UNRELATED_REASON")


def test_audit_cannot_call_post_dispatch_timeout_a_known_failure():
    with pytest.raises(ApprovalValidationError):
        ApprovalAuditEvent(SCOPE,AuditKind.failed,AuditActorKind.worker,None,
                           ApprovalStatus.applying,ApprovalStatus.failed,1,2,NOW,
                           ATTEMPT_ID,1,2,safe_error_code="WB_APPLY_TIMEOUT")


@pytest.mark.parametrize("kind,status", [(AuditKind.rejected,ApprovalStatus.rejected),
                                        (AuditKind.blocked,ApprovalStatus.blocked)])
def test_decision_audit_requires_reason_and_no_attempt(kind,status):
    event=ApprovalAuditEvent(SCOPE,kind,AuditActorKind.membership,314,
                            ApprovalStatus.pending,status,0,1,NOW,reason_code="GUARD_BLOCKED")
    with pytest.raises(ApprovalValidationError):
        replace(event,reason_code=None)
    with pytest.raises(ApprovalValidationError):
        replace(event,attempt_id=ATTEMPT_ID)


def test_create_import_audit_distinguish_unknown_legacy_actor():
    event=ApprovalAuditEvent(SCOPE,AuditKind.created,AuditActorKind.membership,314,
                            None,ApprovalStatus.pending,None,0,NOW)
    with pytest.raises(ApprovalValidationError):
        replace(event,before_version=0)
    imported=replace(event,kind=AuditKind.imported,actor_kind=AuditActorKind.backfill,
                     actor_membership_id=None,after_status=ApprovalStatus.failed,after_version=10)
    assert imported.actor_membership_id is None
    with pytest.raises(ApprovalValidationError):
        replace(imported,actor_membership_id=314)


def test_hydrated_attempt_cannot_bypass_failure_matrix_or_enum_validation():
    applying, attempt=reserved()
    marked=mark_dispatched(applying,attempt,1,0,ACTOR,NOW)
    with pytest.raises(ApprovalValidationError):
        replace(marked,status=AttemptStatus.failed,version=2,finished_at=NOW,
                outcome=ApplyOutcome(AttemptStatus.failed,safe_error_code="WB_APPLY_TIMEOUT"))
    with pytest.raises(ApprovalValidationError):
        ApplyOutcome("failed",safe_error_code="INTERNAL_APPLY_ERROR")
    with pytest.raises(ApprovalValidationError):
        replace(attempt,status="reserved")
    with pytest.raises(ApprovalValidationError):
        replace(attempt,updated_at=NOW+timedelta(seconds=1))


def test_attempt_cannot_be_attached_to_other_claim_and_stale_attempt_conflicts():
    applying, attempt=reserved()
    with pytest.raises(ApprovalValidationError):
        mark_dispatched(replace(applying,claimed_by_membership_id="315"),attempt,1,0,ACTOR,NOW)
    with pytest.raises(ApprovalConflictError):
        mark_dispatched(applying,attempt,1,1,ACTOR,NOW)
    with pytest.raises(ApprovalValidationError):
        replace(attempt,action_key="a"*64)
