"""Reject unrepresentable storage text before hashing/transition; no DB needed."""

import hashlib
import json
import traceback
from dataclasses import replace

import pytest

from app.modules.wb_repricing import (
    ApprovalStatus,
    ApprovalValidationError,
    block_approval,
    build_action_key,
    claim_approval,
    record_apply_success,
    reject_approval,
)
from app.modules.wb_repricing_dispatch import (
    ApplyOutcome,
    ApprovalAuditEvent,
    AttemptStatus,
    AuditActorKind,
    AuditKind,
    build_dispatch_key,
    reserve_attempt,
)
from app.modules.wb_repricing_repository import ApprovalRepositoryScope
from tests.test_wb_repricing_approval_domain import _snapshot as make_snapshot
from tests.test_wb_repricing_dispatch_contract import (
    ACTOR,
    ATTEMPT_ID,
    NOW,
    SCOPE,
    request,
    reserved,
)


@pytest.mark.parametrize("bad", ["secret" + chr(code) + "tail" for code in (0, 0xD800, 0xDFFF)])
@pytest.mark.parametrize("entry", [
    "key_account", "key_approval", "scope", "request_article", "outcome_upload",
    "snapshot_account", "snapshot_approval", "snapshot_catalog", "snapshot_article",
    "snapshot_claimed", "snapshot_decided", "snapshot_upload", "claim_actor",
    "reject_actor", "block_actor", "success_upload", "audit_upload",
])
def test_public_text_inputs_reject_unrepresentable_unicode_without_reflection(bad, entry):
    pending = make_snapshot()
    applying = make_snapshot(status=ApprovalStatus.applying)
    with pytest.raises(ApprovalValidationError) as caught:
        if entry == "key_account":
            build_action_key(7, bad, "approval-1", "a" * 64)
        elif entry == "key_approval":
            build_action_key(7, "42", bad, "a" * 64)
        elif entry == "scope":
            ApprovalRepositoryScope(7, 42, bad)
        elif entry == "request_article":
            replace(request(), article_id=bad)
        elif entry == "outcome_upload":
            ApplyOutcome(AttemptStatus.applied, wb_upload_id=bad, result_code="ACCEPTED")
        elif entry.startswith("snapshot_"):
            field, status = {
                "snapshot_account": ("marketplace_account_id", ApprovalStatus.pending),
                "snapshot_approval": ("approval_id", ApprovalStatus.pending),
                "snapshot_catalog": ("catalog_sku_id", ApprovalStatus.pending),
                "snapshot_article": ("article_id", ApprovalStatus.pending),
                "snapshot_claimed": ("claimed_by_membership_id", ApprovalStatus.applying),
                "snapshot_decided": ("decided_by_membership_id", ApprovalStatus.rejected),
                "snapshot_upload": ("wb_upload_id", ApprovalStatus.applied),
            }[entry]
            original = make_snapshot(status=status)
            kwargs = {field: bad}
            if field in ("marketplace_account_id", "approval_id"):
                # Construct matching legacy hash independently: constructor must
                # reject text, not merely notice a mismatched action key.
                values = [original.organization_id, original.marketplace_account_id,
                          original.approval_id, original.request_checksum]
                values[1 if field == "marketplace_account_id" else 2] = bad
                kwargs["action_key"] = hashlib.sha256(json.dumps(
                    values, ensure_ascii=True, separators=(",", ":")
                ).encode()).hexdigest()
            replace(original, **kwargs)
        elif entry == "claim_actor":
            claim_approval(pending, pending.version, bad, pending.updated_at)
        elif entry == "reject_actor":
            reject_approval(pending, pending.version, bad, "MANUAL", pending.updated_at)
        elif entry == "block_actor":
            block_approval(pending, pending.version, bad, "MANUAL", pending.updated_at)
        elif entry == "success_upload":
            record_apply_success(applying, applying.version, bad, "ACCEPTED", applying.updated_at)
        elif entry == "audit_upload":
            ApprovalAuditEvent(
                SCOPE, AuditKind.applied, AuditActorKind.worker, None,
                ApprovalStatus.applying, ApprovalStatus.applied, 1, 2, NOW,
                attempt_id=ATTEMPT_ID, before_attempt_version=1, after_attempt_version=2,
                wb_upload_id=bad, result_code="ACCEPTED",
            )
        else:
            pytest.fail("uncovered input")
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("valid", ["Артикул-Ё", "a\u0308", "\U0001f680", "x" * 10000])
def test_valid_unicode_is_not_normalized_truncated_or_rehashed(valid):
    original = make_snapshot(article_id=valid, approval_id=valid)
    wire = json.dumps([original.organization_id, original.marketplace_account_id,
                       valid, original.request_checksum], ensure_ascii=True,
                      separators=(",", ":")).encode()
    assert original.action_key == hashlib.sha256(wire).hexdigest()
    assert original.article_id == valid
    assert ApprovalRepositoryScope(7, 42, valid).approval_id == valid
    assert ApplyOutcome(AttemptStatus.applied, wb_upload_id=valid,
                        result_code="ACCEPTED").wb_upload_id == valid


def test_canonical_cyrillic_request_preserves_exact_existing_json_bytes():
    req = replace(request(), article_id="Ё")
    expected = (b'{"accountId":42,"approvalId":"approval-1","articleId":"\\u0401",'
                b'"catalogSkuId":99,"discountPct":0,"minPriceKopecks":null,"nmId":123,'
                b'"organizationId":7,"priceKopecks":129950,"schema":"wb-price-apply/v1",'
                b'"sizeId":null}')
    assert req.canonical_bytes == expected
    assert req.checksum == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("code", [0, 0xD800, 0xDFFF, ord("z")])
@pytest.mark.parametrize("entry", ["key", "reserve", "hydrate", "audit"])
def test_invalid_attempt_identity_never_exposes_uuid_parser_exception(code, entry):
    bad = "secret" + chr(code) + "a" * 25
    applying, attempt = reserved()
    with pytest.raises(ApprovalValidationError) as caught:
        if entry == "key":
            build_dispatch_key(SCOPE, attempt.action_key, bad)
        elif entry == "reserve":
            reserve_attempt(applying, request(), 1, ACTOR, bad, NOW)
        elif entry == "hydrate":
            replace(attempt, attempt_id=bad)
        else:
            ApprovalAuditEvent(
                SCOPE, AuditKind.reserved, AuditActorKind.membership, 314,
                ApprovalStatus.applying, ApprovalStatus.applying, 1, 1, NOW,
                attempt_id=bad, before_attempt_version=None, after_attempt_version=0,
            )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in "".join(traceback.format_exception(caught.value))
