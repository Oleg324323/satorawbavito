from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.cabinet.store import get_user_wb_token_secret
from app.config import get_settings
from app.contracts.envelopes import DataEnvelope
from app.control_plane.auth import actor_from_request
from app.control_plane.store import assert_permission_or_audit, record_audit_event
from app.repricer_sprint_b import (
    ApplyDraftRequest,
    ApplyJobView,
    ApproveDraftRequest,
    PriceDraftCreateRequest,
    PriceDraftView,
    PriceRecommendationRequest,
    PriceRecommendationResponse,
    RetryApplyJobRequest,
    apply_approved_draft,
    approve_draft,
    build_price_recommendation,
    create_price_draft,
    get_apply_job_or_404,
    get_draft_or_404,
    retry_apply_job,
)
from app.wb_api.client import RateLimitedWbApiClient, build_wb_client, build_wb_statistics_client


router = APIRouter(tags=["wb-repricer-sprint-b"])


def _actor_wb_token(request: Request) -> str:
    actor = actor_from_request(request)
    token = (get_user_wb_token_secret(actor.user_id) or "").strip()
    if not token:
        raise HTTPException(status_code=409, detail="WB_TOKEN_REQUIRED")
    return token


def _build_price_client(scenario: str, wb_token: str | None = None) -> RateLimitedWbApiClient:
    settings = get_settings()
    if settings.wb_api_mode == "real" and not wb_token:
        raise HTTPException(status_code=409, detail="WB_TOKEN_REQUIRED")
    token_override = wb_token if settings.wb_api_mode == "real" else None
    return RateLimitedWbApiClient(inner=build_wb_client(scenario, token_override=token_override))


def _build_statistics_client(scenario: str, wb_token: str | None = None) -> RateLimitedWbApiClient:
    settings = get_settings()
    if settings.wb_api_mode == "real" and not wb_token:
        raise HTTPException(status_code=409, detail="WB_TOKEN_REQUIRED")
    token_override = wb_token if settings.wb_api_mode == "real" else None
    return RateLimitedWbApiClient(inner=build_wb_statistics_client(scenario, token_override=token_override))


@router.post("/api/v1/wb-repricer/recommendation", response_model=DataEnvelope[PriceRecommendationResponse])
def post_price_recommendation(request: Request, payload: PriceRecommendationRequest) -> DataEnvelope[PriceRecommendationResponse]:
    wb_token = _actor_wb_token(request)
    client = _build_price_client(payload.scenario, wb_token)
    orders_client = _build_statistics_client(payload.scenario, wb_token)
    return DataEnvelope(data=build_price_recommendation(client=client, payload=payload, orders_client=orders_client))


@router.post("/api/v1/wb-repricer/drafts", response_model=DataEnvelope[PriceDraftView])
def post_create_price_draft(request: Request, payload: PriceDraftCreateRequest) -> DataEnvelope[PriceDraftView]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="price:send",
        action="price.draft.create",
        object_type="price_draft",
        object_id=payload.articleId,
        reason="actor cannot create price drafts",
    )
    client = _build_price_client(payload.scenario, _actor_wb_token(request))
    orders_client = _build_statistics_client(payload.scenario, _actor_wb_token(request))
    draft = create_price_draft(
        actor_id=actor.actor_id,
        actor_role=actor.role,
        client=client,
        payload=payload,
        orders_client=orders_client,
        organization_id=actor.organization_id,
    )
    record_audit_event(
        actor=actor,
        action="price.draft.create",
        object_type="price_draft",
        object_id=draft.draftId,
        before_state=None,
        after_state=draft.model_dump(mode="json"),
        reason=payload.reason,
        approval_ref=None,
        evidence_refs=["WB-23", "FORMULA_VERSION", draft.formulaVersion],
    )
    return DataEnvelope(data=draft)


@router.get("/api/v1/wb-repricer/drafts/{draftId}", response_model=DataEnvelope[PriceDraftView])
def get_price_draft(request: Request, draftId: str) -> DataEnvelope[PriceDraftView]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="price:send",
        action="price.draft.get",
        object_type="price_draft",
        object_id=draftId,
        reason="actor cannot read price draft",
    )
    return DataEnvelope(data=get_draft_or_404(draftId, actor.organization_id))


@router.post("/api/v1/wb-repricer/drafts/{draftId}/approve", response_model=DataEnvelope[PriceDraftView])
def post_approve_price_draft(request: Request, draftId: str, payload: ApproveDraftRequest) -> DataEnvelope[PriceDraftView]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="price:send",
        action="price.draft.approve",
        object_type="price_draft",
        object_id=draftId,
        reason="actor cannot approve price draft",
    )
    before = get_draft_or_404(draftId, actor.organization_id)
    approved = approve_draft(draft_id=draftId, actor_id=actor.actor_id, request=payload)
    record_audit_event(
        actor=actor,
        action="price.draft.approve",
        object_type="price_draft",
        object_id=draftId,
        before_state=before.model_dump(mode="json"),
        after_state=approved.model_dump(mode="json"),
        reason=payload.reason,
        approval_ref=payload.approvalRef,
        evidence_refs=["approval_required", "WB-22", "WB-23"],
    )
    return DataEnvelope(data=approved)


@router.post("/api/v1/wb-repricer/drafts/{draftId}/apply", response_model=DataEnvelope[ApplyJobView])
def post_apply_price_draft(request: Request, draftId: str, payload: ApplyDraftRequest) -> DataEnvelope[ApplyJobView]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="price:send",
        action="price.apply.commit",
        object_type="price_draft",
        object_id=draftId,
        reason="actor cannot commit approved draft",
    )
    settings = get_settings()
    client = _build_price_client(payload.scenario, _actor_wb_token(request))
    before = get_draft_or_404(draftId, actor.organization_id)
    job = apply_approved_draft(
        draft_id=draftId,
        client=client,
        real_apply_enabled=settings.real_price_apply_enabled,
        local_apply_enabled=settings.repricer_local_price_apply_enabled,
        request=payload,
    )
    after = get_draft_or_404(draftId)
    record_audit_event(
        actor=actor,
        action="price.apply.commit",
        object_type="price_apply_job",
        object_id=job.jobId,
        before_state=before.model_dump(mode="json"),
        after_state={
            "draft": after.model_dump(mode="json"),
            "applyJob": job.model_dump(mode="json"),
        },
        reason="apply approved draft to WB endpoint",
        approval_ref=after.approvalRef,
        evidence_refs=["WB-22", "WB-23", "row_level_errors"],
    )
    return DataEnvelope(data=job)


@router.get("/api/v1/wb-repricer/jobs/{jobId}", response_model=DataEnvelope[ApplyJobView])
def get_price_apply_job(request: Request, jobId: str) -> DataEnvelope[ApplyJobView]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="price:send",
        action="price.apply.job.get",
        object_type="price_apply_job",
        object_id=jobId,
        reason="actor cannot read price apply job",
    )
    return DataEnvelope(data=get_apply_job_or_404(jobId, actor.organization_id))


@router.post("/api/v1/wb-repricer/jobs/{jobId}/retry", response_model=DataEnvelope[ApplyJobView])
def post_retry_price_apply_job(request: Request, jobId: str, payload: RetryApplyJobRequest) -> DataEnvelope[ApplyJobView]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="price:send",
        action="price.apply.job.retry",
        object_type="price_apply_job",
        object_id=jobId,
        reason="actor cannot retry price apply job",
    )
    settings = get_settings()
    client = _build_price_client(payload.scenario, _actor_wb_token(request))
    before = get_apply_job_or_404(jobId, actor.organization_id)
    retried = retry_apply_job(
        job_id=jobId,
        client=client,
        real_apply_enabled=settings.real_price_apply_enabled,
        local_apply_enabled=settings.repricer_local_price_apply_enabled,
        request=payload,
    )
    record_audit_event(
        actor=actor,
        action="price.apply.job.retry",
        object_type="price_apply_job",
        object_id=jobId,
        before_state=before.model_dump(mode="json"),
        after_state=retried.model_dump(mode="json"),
        reason="manual retry with backoff policy",
        approval_ref=None,
        evidence_refs=["retry_backoff", "WB-22"],
    )
    return DataEnvelope(data=retried)
