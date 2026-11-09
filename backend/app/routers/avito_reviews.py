from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.reviews import AvitoReviewsFetchRequest, AvitoReviewsUpstreamError, AvitoReviewRow, build_avito_reviews_client
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.repricer_cache.store import get_source_cache, save_source_cache
from app.reviews.openai_client import generate_openai_review_reply
from app.reviews.schemas import ReviewFeedbackView, ReviewModerationDecision
from app.reviews.service import get_sync_settings


router = APIRouter(tags=["avito-reviews"])


class AvitoReviewAnswerPayload(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AvitoReviewDraftGeneratePayload(BaseModel):
    brandVoiceId: str = Field(default="avito", min_length=1)
    ratingOverride: int | None = Field(default=None, ge=1, le=5)
    promptInstruction: str | None = Field(default=None, max_length=8000)
    regenerate: bool = False
    review: AvitoReviewRow


def _cache_key(limit: int, offset: int) -> str:
    return f"avito_reviews:{limit}:{offset}"


def _summary(reviews: list[AvitoReviewRow], total: int) -> dict[str, Any]:
    unanswered = sum(1 for row in reviews if row.answer is None and row.canAnswer)
    low_rating = sum(1 for row in reviews if row.score <= 3)
    answered = sum(1 for row in reviews if row.answer is not None)
    return {
        "total": total,
        "shown": len(reviews),
        "unanswered": unanswered,
        "answered": answered,
        "lowRating": low_rating,
        "canAnswer": sum(1 for row in reviews if row.canAnswer),
    }


def _response_payload(
    *,
    status: str,
    reviews: list[AvitoReviewRow],
    total: int,
    rating: Any | None,
    limit: int,
    offset: int,
    cache_status: str = "fresh",
    diagnostics: dict[str, Any] | None = None,
    error: Any | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": status,
        "summary": _summary(reviews, total),
        "rating": rating.model_dump(mode="json") if hasattr(rating, "model_dump") else rating,
        "reviews": [review.model_dump(mode="json") for review in reviews],
        "pagination": {"limit": limit, "offset": offset, "total": total},
        "source": {
            "mode": "live",
            "cache": {"status": cache_status, "savedAt": datetime.now(timezone.utc).isoformat()},
            "api": {
                "baseUrl": settings.avito_api_base_url,
                "ratingEndpoint": "GET /ratings/v1/info",
                "reviewsEndpoint": "GET /ratings/v1/reviews",
                "answerEndpoint": "POST /ratings/v1/answers",
                "deleteAnswerEndpoint": "DELETE /ratings/v1/answers/{answer_id}",
                "scopes": ["ratings:read", "ratings:write"],
            },
            "diagnostics": diagnostics,
            "error": error,
        },
    }


def _cache_hit_payload(cached: dict[str, Any]) -> dict[str, Any]:
    payload = dict(cached)
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "hit"
    source["cache"] = cache
    source["error"] = None
    payload["source"] = source
    return payload


def _credentials_or_error(request: Request, permission: str = "cabinet:read"):
    actor = actor_from_request(request)
    if not has_permission(actor, permission):
        raise HTTPException(status_code=403, detail=f"NO_ACCESS:{permission}")
    credentials = get_user_avito_credentials_secret(actor.user_id) or get_organization_avito_credentials_secret(actor.organization_id)
    if credentials is None:
        raise HTTPException(status_code=409, detail="AVITO_CREDENTIALS_REQUIRED")
    settings = get_settings()
    try:
        access_token = resolve_user_avito_access_token(
            user_id=actor.user_id,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="AVITO_OAUTH_FAILED") from exc
    return actor, settings, access_token


def _avito_review_to_feedback(review: AvitoReviewRow) -> ReviewFeedbackView:
    now = datetime.now(timezone.utc)
    created_at = now
    if review.createdAt:
        try:
            created_at = datetime.fromisoformat(review.createdAt)
        except ValueError:
            created_at = now
    item_id = 1
    if review.itemId:
        try:
            item_id = max(1, int(review.itemId))
        except ValueError:
            item_id = 1
    return ReviewFeedbackView(
        feedbackId=review.reviewId,
        nmId=item_id,
        imtId=None,
        brandName="Avito",
        productName=review.itemTitle or f"Объявление Avito {review.itemId or review.reviewId}",
        createdDate=created_at,
        text=review.text,
        pros="",
        cons="",
        answerText=review.answer.text if review.answer is not None else None,
        isAnswered=review.answer is not None,
        rating=review.score,
        syncedAt=now,
        sourceStatus=review.sourceStatus,
    )


def _moderation_for_avito_review(review: AvitoReviewRow, rating: int) -> ReviewModerationDecision:
    reasons: list[str] = []
    state = "clean"
    if rating <= 3:
        state = "manual_review"
        reasons.append("low_rating")
    if review.answer is not None:
        state = "manual_review"
        reasons.append("already_answered")
    if not review.text.strip():
        state = "manual_review"
        reasons.append("empty_text")
    return ReviewModerationDecision(state=state, reasons=reasons, matchedStopTopicIds=[], matchedRuleIds=[])  # type: ignore[arg-type]


def _avito_draft_payload(*, actor: Any, review: AvitoReviewRow, payload: AvitoReviewDraftGeneratePayload) -> dict[str, Any]:
    sync_settings = get_sync_settings(actor.organization_id, getattr(actor, "actor_id", actor.user_id))
    ai_prompt = sync_settings.aiPrompt.strip()
    if not ai_prompt:
        raise HTTPException(status_code=409, detail="REVIEW_AI_PROMPT_REQUIRED")
    rating = payload.ratingOverride or review.score or 5
    feedback = _avito_review_to_feedback(review)
    moderation = _moderation_for_avito_review(review, rating)
    avito_tone = getattr(sync_settings.reviewSettings, "avitoTone", "Спокойный, уверенный тон продавца Avito")
    template_text = (
        "Сформируй короткий человечный ответ на отзыв Avito. "
        "Не обещай компенсации, скидки или действия вне правил площадки. "
        "Если отзыв негативный, признай опыт клиента и предложи решить вопрос в диалоге."
    )
    if payload.promptInstruction and payload.promptInstruction.strip():
        template_text = f"{payload.promptInstruction.strip()}\n\nБазовые ограничения Avito:\n{template_text}"
    ai_result = generate_openai_review_reply(
        feedback=feedback,
        brand_voice_id=payload.brandVoiceId,
        rating=rating,
        template_text=template_text,
        moderation=moderation,
        system_prompt=(
            f"{ai_prompt}\n\n"
            "Контекст: это отзыв Avito, ответ будет отправляться через Avito Ratings API после ручной проверки.\n"
            f"Тон Avito по умолчанию: {avito_tone}\n"
            "Настройки автоответов из интерфейса:\n"
            f"{sync_settings.reviewSettings.model_dump_json()}"
        ),
    )
    if ai_result.requiresApproval and moderation.state == "clean":
        moderation = moderation.model_copy(update={"state": "manual_review", "reasons": list(dict.fromkeys([*moderation.reasons, *ai_result.reasons]))})
    now = datetime.now(timezone.utc).isoformat()
    draft_id = f"avito-draft-{uuid4().hex}"
    return {
        "draft": {
            "draftId": draft_id,
            "feedbackId": review.reviewId,
            "reviewId": review.reviewId,
            "rating": rating,
            "brandVoiceId": payload.brandVoiceId,
            "approvalState": "required" if moderation.state != "clean" or rating < 4 else "not_required",
            "externalSendAllowed": review.canAnswer,
            "sendState": "ready_to_send" if review.canAnswer else "blocked",
            "generatedText": ai_result.replyText,
            "moderation": moderation.model_dump(mode="json"),
            "promptTraceId": draft_id,
            "createdByActorId": getattr(actor, "actor_id", actor.user_id),
            "createdAt": now,
            "updatedAt": now,
            "source": {"generationSource": ai_result.generationSource, "model": ai_result.model, "riskLevel": ai_result.riskLevel},
        }
    }


@router.get("/api/v1/avito/reviews")
def get_avito_reviews(
    request: Request,
    limit: int = Query(default=50, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    credentials = get_user_avito_credentials_secret(actor.user_id) or get_organization_avito_credentials_secret(actor.organization_id)
    if credentials is None:
        raise HTTPException(status_code=409, detail="AVITO_CREDENTIALS_REQUIRED")
    settings = get_settings()
    try:
        access_token = resolve_user_avito_access_token(
            user_id=actor.user_id,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="AVITO_OAUTH_FAILED") from exc

    source_key = scoped_avito_cache_key(_cache_key(limit, offset), access_token)
    cached = get_source_cache(actor.organization_id, source_key, slim=False) or None
    if not force_refresh and isinstance(cached, dict) and cached.get("reviews"):
        return _cache_hit_payload(cached)

    client = build_avito_reviews_client(access_token=access_token, base_url=settings.avito_api_base_url, timeout_seconds=settings.avito_api_timeout_seconds)
    result = client.fetch_reviews(AvitoReviewsFetchRequest(limit=limit, offset=offset))
    payload = _response_payload(
        status=result.status,
        reviews=result.reviews,
        total=result.total,
        rating=result.rating,
        limit=limit,
        offset=offset,
        diagnostics=result.diagnostics,
        error=result.error.model_dump(mode="json") if hasattr(result.error, "model_dump") else result.error,
    )
    if result.status != "blocked":
        save_source_cache(actor.organization_id, source_key, payload)
    return payload


@router.post("/api/v1/avito/reviews/{review_id}/answer")
def post_avito_review_answer(review_id: str, payload: AvitoReviewAnswerPayload, request: Request) -> dict[str, Any]:
    _actor, settings, access_token = _credentials_or_error(request, permission="reviews:send")
    client = build_avito_reviews_client(access_token=access_token, base_url=settings.avito_api_base_url, timeout_seconds=settings.avito_api_timeout_seconds)
    try:
        answer = client.create_answer(review_id=review_id, message=payload.message.strip())
    except AvitoReviewsUpstreamError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.error.model_dump(mode="json")) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "answer": answer.model_dump(mode="json")}


@router.delete("/api/v1/avito/reviews/answers/{answer_id}")
def delete_avito_review_answer(answer_id: str, request: Request) -> dict[str, Any]:
    _actor, settings, access_token = _credentials_or_error(request, permission="reviews:send")
    client = build_avito_reviews_client(access_token=access_token, base_url=settings.avito_api_base_url, timeout_seconds=settings.avito_api_timeout_seconds)
    try:
        ok = client.delete_answer(answer_id=answer_id)
    except AvitoReviewsUpstreamError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.error.model_dump(mode="json")) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": ok}


@router.post("/api/v1/avito/reviews/{review_id}/drafts/generate")
def generate_avito_review_draft(review_id: str, payload: AvitoReviewDraftGeneratePayload, request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "reviews:write"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:reviews:write")
    review = payload.review.model_copy(update={"reviewId": review_id})
    return _avito_draft_payload(actor=actor, review=review, payload=payload)
