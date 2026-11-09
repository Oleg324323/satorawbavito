"""Dormant manual original-caller one-page orchestration. No router/worker."""

import hashlib
import json
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import Settings, is_review_shadow_enabled
from app.control_plane.auth import ActorContext
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner,
    _resolve_fetch_in_session,
)
from app.reviews.canonical_avito_fetch import (
    BoundedAvitoReviewSourceClient,
    validate_offset,
)
from app.reviews.shadow_service import (
    ReviewShadowError,
    _avito_incarnation,
    _avito_source_binding,
    _avito_source_guard,
    _engine,
    begin_avito_review_shadow,
    publish_received_avito_review_rows,
)
from app.wb_live.auth import require_live_actor


class AvitoReviewSyncError(ValueError):
    def __init__(self, code="AVITO_REVIEW_SYNC_UNAVAILABLE"):
        self.code = (
            code
            if code
            in {
                "AVITO_REVIEW_SYNC_UNAVAILABLE",
                "AVITO_REVIEW_SYNC_INVALID",
                "AVITO_REVIEW_SYNC_DISABLED",
                "AVITO_REVIEW_SYNC_AUTHORITY_CHANGED",
                "AVITO_REVIEW_SYNC_CONFLICT",
            }
            else "AVITO_REVIEW_SYNC_UNAVAILABLE"
        )
        super().__init__(self.code)


def sync_canonical_avito_page(
    engine,
    *,
    actor,
    settings,
    marketplace_account_id,
    offset,
    request_id,
    keyring_loader,
    client_factory=BoundedAvitoReviewSourceClient,
):
    """Resolve/commit, reserve/commit, one GET, publish/commit; never recapture.

    Explicit same-key invocation may re-fetch a GET and then replay/conflict.
    Running reservations survive crashes; no recovery worker is implied.
    """
    failure = "AVITO_REVIEW_SYNC_UNAVAILABLE"
    try:
        if (
            type(actor) is not ActorContext
            or not actor.session_id
            or type(actor.organization_id) is not int
            or not 0 < actor.organization_id < 2**31
            or type(marketplace_account_id) is not int
            or not 0 < marketplace_account_id < 2**31
            or type(offset) is not int
            or not 0 <= offset < 2**63
            or type(request_id) is not UUID
            or request_id.version != 4
            or type(settings) is not Settings
            or not callable(keyring_loader)
            or not callable(client_factory)
        ):
            raise AvitoReviewSyncError("AVITO_REVIEW_SYNC_INVALID")
        validate_offset(offset)
        if not is_review_shadow_enabled(
            settings,
            organization_id=actor.organization_id,
            marketplace_account_id=marketplace_account_id,
        ):
            raise AvitoReviewSyncError("AVITO_REVIEW_SYNC_DISABLED")
        descriptor = {
            "schemaVersion": "avito-review-page-request-v1",
            "decoderVersion": "avito-review-row-v1",
            "organizationId": actor.organization_id,
            "marketplaceAccountId": marketplace_account_id,
            "provider": "avito",
            "method": "GET",
            "path": "/ratings/v1/reviews",
            "limit": 50,
            "offset": offset,
        }
        checksum = hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        with Session(_engine(engine), autoflush=False) as session, session.begin():
            principal, _ = require_live_actor(
                session,
                actor,
                permission="reviews:write",
                account_id=marketplace_account_id,
            )
            owner = MarketplaceAccountCredentialOwner(
                actor.organization_id, marketplace_account_id, "avito"
            )
            # Capture before the paired SELECT/decrypt, not after it: even a
            # descriptor roundtrip during resolution must invalidate this root.
            incarnation = _avito_incarnation(session, principal, owner)
            resolved = _resolve_fetch_in_session(
                session,
                owner,
                "avito_oauth_access",
                keyring_loader(),
            )
            account, authority = _avito_source_binding(actor, resolved.binding)
            _avito_source_guard(session, principal, account, authority, incarnation)
        ticket = begin_avito_review_shadow(
            engine,
            actor=actor,
            settings=settings,
            binding=resolved.binding,
            source_run_id="avito-page:" + str(request_id),
            request_checksum=checksum,
        )
        if (
            ticket.principal,
            ticket.account,
            ticket.authority,
            ticket.account_incarnation,
        ) != (principal, account, authority, incarnation):
            raise AvitoReviewSyncError("AVITO_REVIEW_SYNC_AUTHORITY_CHANGED")
        rows = client_factory(resolved).fetch_page(offset=offset)
        return publish_received_avito_review_rows(engine, ticket=ticket, rows=rows)
    except AvitoReviewSyncError as error:
        failure = error.code
    except ReviewShadowError as error:
        failure = (
            "AVITO_REVIEW_SYNC_CONFLICT"
            if error.code == "REVIEW_SHADOW_CONFLICT"
            else "AVITO_REVIEW_SYNC_UNAVAILABLE"
        )
    except Exception:  # noqa: BLE001, S110 -- caller never receives credentials/raw DB/HTTP cause.
        pass
    raise AvitoReviewSyncError(failure)
