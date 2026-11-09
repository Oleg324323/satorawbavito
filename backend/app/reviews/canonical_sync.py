"""Canonical-only manual WB page ingestion. Never imports the legacy Review store."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, is_review_shadow_enabled
from app.control_plane.auth import ActorContext
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import (
    CredentialStoreError,
    MarketplaceAccountCredentialOwner,
    resolve_marketplace_credential_for_fetch,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)
from app.reviews.canonical_wb_fetch import CanonicalWbFetchError
from app.reviews.canonical_wb_fetch import fetch_canonical_feedbacks as fetch_feedbacks
from app.reviews.shadow_service import (
    ReviewShadowError,
    begin_review_shadow,
    publish_received_review_rows,
)
from app.wb_api.feedbacks_runtime import WbFeedbacksFetchError


class ReviewCanonicalSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    marketplace_account_id: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
    request_id: UUID
    is_answered: StrictBool
    nm_id: Annotated[StrictInt, Field(ge=1, le=2**53 - 1)] | None = None
    take: Annotated[StrictInt, Field(ge=1, le=5000)] = 100
    skip: Annotated[StrictInt, Field(ge=0, le=2**31 - 1)] = 0
    order: Literal["dateAsc", "dateDesc"] = "dateDesc"
    date_from: Annotated[StrictInt, Field(ge=0, le=253402300799)] | None = None
    date_to: Annotated[StrictInt, Field(ge=0, le=253402300799)] | None = None

    @model_validator(mode="after")
    def ordered_dates(self):
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from >= self.date_to
        ):
            raise ValueError("Invalid Review date interval")
        return self


class ReviewCanonicalSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sync_run_id: UUID
    manifest_checksum: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    observed_count: Annotated[int, Field(ge=0)]
    completeness: Literal["partial"] = "partial"


class ReviewCanonicalSyncError(ValueError):
    def __init__(self, code: str):
        self.code = (
            code
            if code
            in {
                "REVIEW_SYNC_DISABLED",
                "REVIEW_SYNC_DENIED",
                "REVIEW_SYNC_INVALID",
                "REVIEW_SYNC_CONFLICT",
                "REVIEW_SYNC_CREDENTIAL_UNAVAILABLE",
                "REVIEW_SYNC_STORAGE_UNAVAILABLE",
                "REVIEW_SYNC_PROVIDER_UNAVAILABLE",
                "REVIEW_SYNC_CONFIGURATION_INVALID",
            }
            else "REVIEW_SYNC_INVALID"
        )
        super().__init__(self.code)


def _authorize_before_fetch(engine, actor, account_id):
    if type(actor) is not ActorContext or not actor.session_id:
        raise ReviewCanonicalSyncError("REVIEW_SYNC_DENIED")
    if not isinstance(engine, Engine) or engine.dialect.name != "postgresql":
        raise ReviewCanonicalSyncError("REVIEW_SYNC_STORAGE_UNAVAILABLE")
    with Session(engine) as session, session.begin():
        set_tenant_context(session, actor.organization_id)
        member = session.scalar(
            select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id,
            )
        )
        account = session.execute(
            select(
                MarketplaceAccountRow.external_account_id,
                MarketplaceAccountRow.credential_ref,
            ).where(
                MarketplaceAccountRow.organization_id == actor.organization_id,
                MarketplaceAccountRow.marketplace_account_id == account_id,
                MarketplaceAccountRow.marketplace == "wb",
            )
        ).one_or_none()
        if member is None or account is None:
            raise ReviewCanonicalSyncError("REVIEW_SYNC_DENIED")
        expected = ExpectedAccountBinding(
            account_id, "wb", account.external_account_id, account.credential_ref
        )
        guard = acquire_publication_guard(
            session,
            principal=UserSessionPrincipal(
                actor.organization_id, actor.user_id, member, actor.session_id
            ),
            required_permissions=frozenset({"reviews:write"}),
            accounts=(expected,),
            authorities=(),
        )
        guard.revalidate_before_write()
    return expected


def sync_canonical_wb_page(
    engine: Engine,
    *,
    actor: ActorContext,
    settings: Settings,
    payload: ReviewCanonicalSyncRequest,
) -> ReviewCanonicalSyncResponse:
    try:
        if (
            type(actor) is not ActorContext
            or type(payload) is not ReviewCanonicalSyncRequest
        ):
            raise ReviewCanonicalSyncError("REVIEW_SYNC_INVALID")
        try:
            selected = is_review_shadow_enabled(
                settings,
                organization_id=actor.organization_id,
                marketplace_account_id=payload.marketplace_account_id,
            )
        except RuntimeError:
            raise ReviewCanonicalSyncError(
                "REVIEW_SYNC_CONFIGURATION_INVALID"
            ) from None
        if not selected:
            raise ReviewCanonicalSyncError("REVIEW_SYNC_DISABLED")
        expected = _authorize_before_fetch(
            engine, actor, payload.marketplace_account_id
        )
        fetched = resolve_marketplace_credential_for_fetch(
            MarketplaceAccountCredentialOwner(
                actor.organization_id, payload.marketplace_account_id, "wb"
            ),
            "wb_api",
        )
        if (
            fetched.binding.external_account_id != expected.external_account_id
            or fetched.binding.credential_ref != expected.credential_ref
        ):
            raise ReviewCanonicalSyncError("REVIEW_SYNC_DENIED")
        request = payload.model_dump(mode="json", exclude={"request_id"})
        checksum = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        ticket = begin_review_shadow(
            engine,
            actor=actor,
            binding=fetched.binding,
            source_run_id="http:" + str(payload.request_id),
            request_checksum=checksum,
        )
        secret = fetched.secret.reveal()
        token = secret.get("token") if type(secret) is dict else None
        if type(token) is not str or not token.strip():
            raise ReviewCanonicalSyncError("REVIEW_SYNC_CREDENTIAL_UNAVAILABLE")
        # Nonempty exact paired override prevents ambient/user-token fallback.
        rows = tuple(
            fetch_feedbacks(
                scenario="complete",
                wb_token=token,
                is_answered=payload.is_answered,
                nm_id=payload.nm_id,
                take=payload.take,
                skip=payload.skip,
                order=payload.order,
                date_from_epoch=payload.date_from,
                date_to_epoch=payload.date_to,
            )
        )
        coverage = {
            "from": None,
            "to": None,
            "streams": [
                {
                    "name": "answered" if payload.is_answered else "unanswered",
                    "terminalReached": False,
                }
            ],
            "pagesObserved": 1,
            "providerEndReached": False,
        }
        receipt = publish_received_review_rows(
            engine, ticket=ticket, rows=rows, coverage=coverage
        )
        return ReviewCanonicalSyncResponse(
            sync_run_id=receipt.sync_run_id,
            manifest_checksum=receipt.manifest_checksum,
            observed_count=receipt.observed_count,
        )
    except ReviewShadowError as error:
        code = {
            "REVIEW_SHADOW_DENIED": "REVIEW_SYNC_DENIED",
            "REVIEW_SHADOW_AUTHORITY_CHANGED": "REVIEW_SYNC_DENIED",
            "REVIEW_SHADOW_EXPIRED": "REVIEW_SYNC_DENIED",
            "REVIEW_SHADOW_CONFLICT": "REVIEW_SYNC_CONFLICT",
            "REVIEW_SHADOW_INVALID": "REVIEW_SYNC_INVALID",
        }.get(error.code, "REVIEW_SYNC_STORAGE_UNAVAILABLE")
        raise ReviewCanonicalSyncError(code) from None
    except PublicationGuardError as error:
        code = (
            "REVIEW_SYNC_STORAGE_UNAVAILABLE"
            if error.code
            in {
                "publication_context_invalid",
                "publication_persistence_failed",
            }
            else "REVIEW_SYNC_DENIED"
        )
        raise ReviewCanonicalSyncError(code) from None
    except CredentialStoreError as error:
        code = (
            "REVIEW_SYNC_STORAGE_UNAVAILABLE"
            if error.code == "credential_persistence_failed"
            else "REVIEW_SYNC_CREDENTIAL_UNAVAILABLE"
        )
        raise ReviewCanonicalSyncError(code) from None
    except (WbFeedbacksFetchError, CanonicalWbFetchError):
        raise ReviewCanonicalSyncError("REVIEW_SYNC_PROVIDER_UNAVAILABLE") from None
    except SQLAlchemyError:
        raise ReviewCanonicalSyncError("REVIEW_SYNC_STORAGE_UNAVAILABLE") from None
