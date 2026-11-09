from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.permissions import permissions_from_profile
from app.control_plane.auth import ActorContext, actor_from_request, has_permission
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner,
)
from app.platform.integrations.orm import MarketplaceAccountRow


@dataclass(frozen=True)
class MarketplaceAccountCredentialAccess:
    owner: MarketplaceAccountCredentialOwner
    external_account_id: str


def get_marketplace_credential_actor(request: Request) -> ActorContext:
    return actor_from_request(request)


def require_marketplace_credential_write(actor: ActorContext) -> None:
    if not has_permission(actor, "integrations:write"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "NO_ACCESS",
                "message": "NO_ACCESS:integrations:write",
            },
        )


def require_marketplace_account_credential_access(
    session: Session,
    actor: ActorContext,
    *,
    marketplace_account_id: int,
    provider: str,
) -> MarketplaceAccountCredentialAccess:
    if provider not in {"wb", "avito"}:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MARKETPLACE_PROVIDER_UNSUPPORTED",
                "message": "Marketplace provider is unsupported",
            },
        )
    try:
        set_tenant_context(session, actor.organization_id)
        membership = session.scalar(
            select(IamMembershipRow).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id,
                IamMembershipRow.is_active.is_(True),
            )
        )
        if membership is None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "MEMBERSHIP_INACTIVE",
                    "message": "Active organization membership is required",
                },
            )
        try:
            current_permissions = {
                value
                for value in membership.permissions
                if isinstance(value, str)
            } | set(permissions_from_profile(membership.role))
        except (AttributeError, TypeError, ValueError):
            current_permissions = set()
        if "integrations:write" not in current_permissions:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "NO_ACCESS",
                    "message": "NO_ACCESS:integrations:write",
                },
            )
        allowed_account_ids: set[int] = set()
        for value in membership.allowed_account_ids:
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                allowed_account_ids.add(value)
            elif (
                isinstance(value, str)
                and value.isascii()
                and value.isdecimal()
                and int(value) > 0
            ):
                allowed_account_ids.add(int(value))
        if (
            membership.scope_mode != "all"
            and marketplace_account_id not in allowed_account_ids
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ACCOUNT_SCOPE_DENIED",
                    "message": "Marketplace account is outside membership scope",
                },
            )
        account = session.scalar(
            select(MarketplaceAccountRow).where(
                MarketplaceAccountRow.organization_id == actor.organization_id,
                MarketplaceAccountRow.marketplace_account_id
                == marketplace_account_id,
            )
        )
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CREDENTIAL_STORAGE_UNAVAILABLE",
                "message": "Credential storage is unavailable",
            },
        ) from None
    if account is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MARKETPLACE_ACCOUNT_NOT_FOUND",
                "message": "Marketplace account was not found",
            },
        )
    if account.marketplace != provider:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MARKETPLACE_ACCOUNT_PROVIDER_MISMATCH",
                "message": "Marketplace account provider does not match",
            },
        )
    return MarketplaceAccountCredentialAccess(
        owner=MarketplaceAccountCredentialOwner(
            organization_id=actor.organization_id,
            marketplace_account_id=marketplace_account_id,
            provider=provider,
        ),
        external_account_id=account.external_account_id,
    )
