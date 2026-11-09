"""Dormant canonical-only Review router. Shared registration belongs to T1."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.control_plane.auth import ActorContext, actor_from_request
from app.infra.db import get_engine
from app.reviews.canonical_sync import (
    ReviewCanonicalSyncError,
    ReviewCanonicalSyncRequest,
    ReviewCanonicalSyncResponse,
    sync_canonical_wb_page,
)

router = APIRouter(prefix="/api/v2/reviews/wb", tags=["canonical-reviews"])


def _error(status, code):
    return HTTPException(status_code=status, detail={"code": code})


def get_review_actor(request: Request) -> ActorContext:
    try:
        return actor_from_request(request)
    except HTTPException:
        raise _error(401, "REVIEW_SYNC_AUTHENTICATION_REQUIRED") from None
    except SQLAlchemyError:
        raise _error(503, "REVIEW_SYNC_STORAGE_UNAVAILABLE") from None


def get_review_engine() -> Engine:
    try:
        return get_engine()
    except SQLAlchemyError:
        raise _error(503, "REVIEW_SYNC_STORAGE_UNAVAILABLE") from None
    except RuntimeError:
        raise _error(503, "REVIEW_SYNC_CONFIGURATION_INVALID") from None


def get_review_settings() -> Settings:
    try:
        return get_settings()
    except RuntimeError:
        raise _error(503, "REVIEW_SYNC_CONFIGURATION_INVALID") from None


@router.post("/sync", response_model=ReviewCanonicalSyncResponse)
def canonical_review_sync(
    payload: ReviewCanonicalSyncRequest,
    actor: Annotated[ActorContext, Depends(get_review_actor)],
    engine: Annotated[Engine, Depends(get_review_engine)],
    settings: Annotated[Settings, Depends(get_review_settings)],
) -> ReviewCanonicalSyncResponse:
    try:
        return sync_canonical_wb_page(
            engine, actor=actor, settings=settings, payload=payload
        )
    except ReviewCanonicalSyncError as error:
        status = {
            "REVIEW_SYNC_DISABLED": 409,
            "REVIEW_SYNC_CONFLICT": 409,
            "REVIEW_SYNC_CREDENTIAL_UNAVAILABLE": 409,
            "REVIEW_SYNC_DENIED": 403,
            "REVIEW_SYNC_INVALID": 400,
            "REVIEW_SYNC_PROVIDER_UNAVAILABLE": 502,
        }.get(error.code, 503)
        raise _error(status, error.code) from None
