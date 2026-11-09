"""Dormant account-scoped single Review read; shared registration belongs to T1."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings, is_review_shadow_enabled
from app.control_plane.auth import ActorContext, actor_from_request
from app.infra.db import get_engine, set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)
from app.reviews.canonical_contract import (
    ExternalReviewIdentity,
    ReviewNormalizationError,
)
from app.reviews.canonical_repository import ReviewFactsRepository, ReviewOwner
from app.reviews.ingestion_contract import ReviewRepositoryError

_STATUS = {
    "REVIEW_READ_AUTHENTICATION_REQUIRED": 401,
    "REVIEW_READ_DENIED": 403,
    "REVIEW_READ_DISABLED": 409,
    "REVIEW_READ_CONFLICT": 409,
    "REVIEW_READ_INVALID": 400,
    "REVIEW_READ_NOT_FOUND": 404,
    "REVIEW_READ_STORAGE_UNAVAILABLE": 503,
    "REVIEW_READ_CONFIGURATION_INVALID": 503,
}


class ReviewReadError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in _STATUS else "REVIEW_READ_STORAGE_UNAVAILABLE"
        super().__init__(self.code)


class CanonicalReviewFactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["canonical-review-fact-v1"] = "canonical-review-fact-v1"
    organization_id: int
    marketplace_account_id: int
    marketplace: Literal["wb"] = "wb"
    external_review_id: str
    review_id: UUID
    current_observation_id: UUID
    # PostgreSQL BIGINT versions travel losslessly even beyond JavaScript integers.
    version: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
    revision: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
    text: str | None = Field(repr=False)
    answered: bool
    can_answer: bool | None
    source_order_state: Literal["current", "ambiguous"]
    content_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    external_product_id: str | None
    source_created_at: datetime
    source_updated_at: datetime | None
    source_schema_version: str
    normalization_version: str


def read_canonical_wb_fact(
    engine: Engine, *, actor: ActorContext, settings: Settings,
    marketplace_account_id: int, external_review_id: str,
) -> CanonicalReviewFactResponse:
    try:
        if type(actor) is not ActorContext or not actor.session_id:
            raise ReviewReadError("REVIEW_READ_DENIED")
        identity = ExternalReviewIdentity(
            actor.organization_id, marketplace_account_id, "wb", external_review_id
        )
        try:
            enabled = is_review_shadow_enabled(
                settings, organization_id=actor.organization_id,
                marketplace_account_id=marketplace_account_id,
            )
        except RuntimeError:
            raise ReviewReadError("REVIEW_READ_CONFIGURATION_INVALID") from None
        if not enabled:
            raise ReviewReadError("REVIEW_READ_DISABLED")
        if not isinstance(engine, Engine) or engine.dialect.name != "postgresql":
            raise ReviewReadError("REVIEW_READ_STORAGE_UNAVAILABLE")
        with Session(engine) as session, session.begin():
            set_tenant_context(session, actor.organization_id)
            member = session.scalar(select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id,
            ))
            account = session.execute(select(
                MarketplaceAccountRow.external_account_id, MarketplaceAccountRow.credential_ref,
            ).where(
                MarketplaceAccountRow.organization_id == actor.organization_id,
                MarketplaceAccountRow.marketplace_account_id == marketplace_account_id,
                MarketplaceAccountRow.marketplace == "wb",
            )).one_or_none()
            if member is None or account is None:
                raise ReviewReadError("REVIEW_READ_DENIED")
            expected = ExpectedAccountBinding(
                marketplace_account_id, "wb", account.external_account_id, account.credential_ref
            )
            guard = acquire_publication_guard(
                session,
                principal=UserSessionPrincipal(actor.organization_id, actor.user_id, member, actor.session_id),
                required_permissions=frozenset({"reviews:read"}),
                accounts=(expected,), authorities=(),
            )
            guard.revalidate_before_write()
            repository = ReviewFactsRepository(
                session.connection(),
                ReviewOwner(actor.organization_id, marketplace_account_id, "wb",
                            expected.external_account_id, expected.credential_ref),
                command_savepoints=False,
            )
            current = repository.get_fact(identity.external_review_id)
            if current is None:
                raise ReviewReadError("REVIEW_READ_NOT_FOUND")
            result = CanonicalReviewFactResponse(
                organization_id=actor.organization_id, marketplace_account_id=marketplace_account_id,
                external_review_id=identity.external_review_id, review_id=current.review_id,
                current_observation_id=current.current_observation_id,
                version=str(current.version), revision=str(current.revision), text=current.text,
                answered=current.answered, can_answer=current.can_answer,
                source_order_state=current.source_order_state, content_checksum=current.content_checksum,
                external_product_id=current.external_product_id, source_created_at=current.source_created_at,
                source_updated_at=current.source_updated_at, source_schema_version=current.source_schema_version,
                normalization_version=current.normalization_version,
            )
            guard.revalidate_before_write()
        return result
    except ReviewNormalizationError:
        raise ReviewReadError("REVIEW_READ_INVALID") from None
    except PublicationGuardError as error:
        code = "REVIEW_READ_STORAGE_UNAVAILABLE" if error.code in {
            "publication_context_invalid", "publication_persistence_failed"
        } else "REVIEW_READ_DENIED"
        raise ReviewReadError(code) from None
    except ReviewRepositoryError as error:
        code = "REVIEW_READ_CONFLICT" if error.code in {
            "REVIEW_HISTORY_BINDING_CONFLICT", "REVIEW_SCOPE_DENIED"
        } else "REVIEW_READ_STORAGE_UNAVAILABLE"
        raise ReviewReadError(code) from None
    except (SQLAlchemyError, ValidationError):
        raise ReviewReadError("REVIEW_READ_STORAGE_UNAVAILABLE") from None


def _http_error(code):
    return HTTPException(status_code=_STATUS[code], detail={"code": code})


def get_read_actor(request: Request) -> ActorContext:
    try:
        return actor_from_request(request)
    except HTTPException:
        raise _http_error("REVIEW_READ_AUTHENTICATION_REQUIRED") from None
    except SQLAlchemyError:
        raise _http_error("REVIEW_READ_STORAGE_UNAVAILABLE") from None


def get_read_engine() -> Engine:
    try:
        return get_engine()
    except (SQLAlchemyError, RuntimeError):
        raise _http_error("REVIEW_READ_STORAGE_UNAVAILABLE") from None


def get_read_settings() -> Settings:
    try:
        return get_settings()
    except RuntimeError:
        raise _http_error("REVIEW_READ_CONFIGURATION_INVALID") from None


router = APIRouter(prefix="/api/v2/reviews/wb", tags=["canonical-reviews"])


@router.get("/fact", response_model=CanonicalReviewFactResponse)
def canonical_review_fact(
    marketplace_account_id: Annotated[int, Query(ge=1, le=2**31 - 1)],
    external_review_id: Annotated[str, Query(min_length=1)],
    actor: Annotated[ActorContext, Depends(get_read_actor)],
    engine: Annotated[Engine, Depends(get_read_engine)],
    settings: Annotated[Settings, Depends(get_read_settings)],
) -> CanonicalReviewFactResponse:
    try:
        return read_canonical_wb_fact(
            engine, actor=actor, settings=settings, marketplace_account_id=marketplace_account_id,
            external_review_id=external_review_id,
        )
    except ReviewReadError as error:
        raise _http_error(error.code) from None
