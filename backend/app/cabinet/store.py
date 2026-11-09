from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from hmac import compare_digest
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import (
    LkAuditEventRow,
    LkIntegrationRow,
    LkOrganizationRow,
    LkSessionRow,
    LkUserAvitoCredentialsRow,
    LkUserWbTokenRow,
    LkUserPermissionRow,
    LkUserPreferenceRow,
    LkUserRow,
)
from app.cabinet.permissions import normalize_permission_profile, permissions_from_profile
from app.cabinet.schemas import (
    AuditEventView,
    CabinetMeView,
    IntegrationStatus,
    IntegrationView,
    OrganizationView,
    SessionView,
    TeamUserView,
    UserAvitoCredentialsView,
    UserWbTokenView,
    UserPreferencesView,
)
from app.infra.db import get_engine, get_session_factory, set_tenant_context
from app.config import get_settings
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.wb_credentials import invalidate_wb_credential_bindings


@dataclass(frozen=True)
class UserAuthRecord:
    user_id: str
    organization_id: int
    email: str
    password_hash: str
    full_name: str
    permission_profile: str
    permissions: frozenset[str]
    is_active: bool


@dataclass(frozen=True)
class SessionAuthRecord:
    session_id: str
    user_id: str
    organization_id: int
    issued_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None
    user_agent: str | None
    ip_address: str | None
    refresh_token_hash: str | None
    refresh_token_expires_at: datetime | None
    previous_refresh_token_hash: str | None = None
    previous_refresh_token_expires_at: datetime | None = None


@dataclass(frozen=True)
class AvitoCredentialsSecret:
    client_id: str
    client_secret: str
    cached_access_token: str | None
    access_token_expires_at: datetime | None


@dataclass
class _MemoryUser:
    user_id: str
    organization_id: int
    email: str
    password_hash: str
    full_name: str
    permission_profile: str
    permissions: set[str]
    is_active: bool
    created_at: datetime


@dataclass
class _MemorySession:
    session_id: str
    user_id: str
    organization_id: int
    issued_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None
    user_agent: str | None
    ip_address: str | None
    refresh_token_hash: str | None
    refresh_token_expires_at: datetime | None
    previous_refresh_token_hash: str | None = None
    previous_refresh_token_expires_at: datetime | None = None


@dataclass
class _MemoryOrg:
    organization_id: int
    slug: str
    name: str
    created_at: datetime


@dataclass
class _MemoryState:
    organizations: dict[int, _MemoryOrg] = field(default_factory=dict)
    users: dict[str, _MemoryUser] = field(default_factory=dict)
    users_by_email: dict[str, str] = field(default_factory=dict)
    sessions: dict[str, _MemorySession] = field(default_factory=dict)
    integrations: dict[tuple[int, str], IntegrationView] = field(default_factory=dict)
    user_wb_tokens: dict[str, UserWbTokenView] = field(default_factory=dict)
    user_wb_token_secrets: dict[str, str] = field(default_factory=dict)
    user_avito_credentials: dict[str, UserAvitoCredentialsView] = field(default_factory=dict)
    user_avito_credential_secrets: dict[str, AvitoCredentialsSecret] = field(default_factory=dict)
    preferences: dict[str, UserPreferencesView] = field(default_factory=dict)
    audit_events: list[AuditEventView] = field(default_factory=list)
    next_org_id: int = 1
    next_audit_event_id: int = 1


_MEMORY = _MemoryState()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return clean or "company"


def _run_db(db_fn):
    try:
        engine = get_engine()
        if get_settings().wb_live_sync_enabled and engine.dialect.name != "postgresql":
            raise HTTPException(503, detail={"code": "WB_LIVE_UNAVAILABLE"})
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        session_factory = get_session_factory()
        with session_factory() as session:
            if get_settings().wb_live_sync_enabled:
                event.listen(session, "after_begin", _live_utc_transaction)
            return db_fn(session)
    except SQLAlchemyError:
        if get_settings().wb_live_sync_enabled:
            raise HTTPException(503, detail={"code": "WB_LIVE_UNAVAILABLE"}) from None
        if _requires_persistent_storage():
            raise HTTPException(503, detail={
                "code": "CABINET_STORAGE_UNAVAILABLE",
                "message": "База данных временно занята или недоступна. Повторите запрос; сохранённые данные не потеряны.",
            }) from None
        return None


def _requires_persistent_storage() -> bool:
    settings = get_settings()
    return (
        getattr(settings, "cabinet_require_persistent_storage", False)
        or getattr(settings, "environment", "local").lower() not in {"local", "dev", "development", "test"}
    )


def _live_utc_transaction(session, transaction, connection):
    # DB timestamps must satisfy the existing UTC wire contract, including reads
    # in the new transaction opened by refresh() after a registration commit.
    connection.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")


def _default_notification_settings() -> dict[str, Any]:
    return {
        "email": {"enabled": True, "dailyDigest": True, "criticalAlerts": True},
        "telegram": {"enabled": False, "chatId": None},
    }


def _default_export_settings() -> dict[str, Any]:
    return {
        "defaultFormat": "xlsx",
        "includeFinance": True,
        "autoExportSchedule": "disabled",
    }


def _sync_membership(
    session: Session, user: LkUserRow, permissions: Iterable[str]
) -> None:
    set_tenant_context(session, user.organization_id)
    membership = session.scalar(
        select(IamMembershipRow).where(
            IamMembershipRow.organization_id == user.organization_id,
            IamMembershipRow.user_id == user.user_id,
        )
    )
    resolved_permissions = sorted(permissions)
    if membership is None:
        session.add(
            IamMembershipRow(
                organization_id=user.organization_id,
                user_id=user.user_id,
                role=user.permission_profile,
                permissions=resolved_permissions,
                scope_mode="all",
                allowed_account_ids=[],
                is_active=user.is_active,
            )
        )
        return
    membership.role = user.permission_profile
    membership.permissions = resolved_permissions
    membership.is_active = user.is_active


def _team_user_view(
    *,
    user_id: str,
    organization_id: int,
    email: str,
    full_name: str,
    permission_profile: str,
    permissions: list[str],
    is_active: bool,
    created_at: datetime,
) -> TeamUserView:
    return TeamUserView(
        userId=user_id,
        organizationId=organization_id,
        email=email,
        fullName=full_name,
        permissionProfile=permission_profile,
        permissions=sorted(set(permissions)),
        isActive=is_active,
        createdAt=created_at,
    )


def _mask_wb_token(raw_token: str) -> str:
    cleaned = raw_token.strip()
    if not cleaned:
        return "wb:****"
    tail = cleaned[-4:] if len(cleaned) >= 4 else cleaned
    return f"wb:****{tail}"


def _mask_avito_secret(label: str, raw_value: str) -> str:
    cleaned = raw_value.strip()
    if not cleaned:
        return f"{label}:****"
    tail = cleaned[-4:] if len(cleaned) >= 4 else cleaned
    return f"{label}:****{tail}"


def _append_audit_event(
    *,
    organization_id: int,
    actor_user_id: str | None,
    action: str,
    object_type: str,
    object_id: str,
    details: dict[str, Any] | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditEventView:
    resolved_after_state = after_state if after_state is not None else details

    def _db(session: Session) -> AuditEventView:
        row = LkAuditEventRow(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            details=details,
            before_state=before_state,
            after_state=resolved_after_state,
            reason=reason,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return AuditEventView(
            eventId=row.event_id,
            organizationId=row.organization_id,
            actorUserId=row.actor_user_id,
            action=row.action,
            objectType=row.object_type,
            objectId=row.object_id,
            details=row.details,
            beforeState=row.before_state,
            afterState=row.after_state,
            reason=row.reason,
            ipAddress=row.ip_address,
            userAgent=row.user_agent,
            createdAt=row.created_at,
        )

    result = _run_db(_db)
    if result is not None:
        return result

    event = AuditEventView(
        eventId=_MEMORY.next_audit_event_id,
        organizationId=organization_id,
        actorUserId=actor_user_id,
        action=action,
        objectType=object_type,
        objectId=object_id,
        details=details,
        beforeState=before_state,
        afterState=resolved_after_state,
        reason=reason,
        ipAddress=ip_address,
        userAgent=user_agent,
        createdAt=_utc_now(),
    )
    _MEMORY.next_audit_event_id += 1
    _MEMORY.audit_events.append(event)
    return event


def _ensure_defaults(session: Session | None = None) -> None:
    if getattr(get_settings(), 'environment', 'local').lower() not in {'local', 'dev', 'development', 'test'}:
        if session is None:
            raise HTTPException(503, detail={"code": "CABINET_STORAGE_UNAVAILABLE"})
        return  # Never seed demo users or known passwords on the VPS.
    if get_settings().wb_live_sync_enabled:
        if session is None:
            raise HTTPException(503, detail={"code": "WB_LIVE_UNAVAILABLE"})
        return
    defaults = [
        ("viewer@vella.local", "Viewer Pass", "viewer", "pbkdf2_sha256$120000$viewer-v1$X8nL6oW3xibOEeoflBcI5RBmXfOMtyt3VrUhfA0eK3k"),
        ("editor@vella.local", "Settings Editor", "settings_editor", "pbkdf2_sha256$120000$editor-v1$cqH317m8tJt__yOEu-lHGapdxEFgVAjyGPC6c225q8M"),
        ("sender@vella.local", "Price Sender", "price_sender", "pbkdf2_sha256$120000$sender-v1$jOYIhmgrImOnHtVRQhP_uLubodUmeMCX7c5KkMizCQE"),
        ("finance@vella.local", "Finance Viewer", "finance_viewer", "pbkdf2_sha256$120000$finance-v1$xcvf9lT5mTxwmnGu8VM5AOXpjlBUCfZBo4VJYyzTY0Y"),
        ("admin@vella.local", "Admin", "admin", "pbkdf2_sha256$120000$admin-v1$H9G5JJBi1jUCqp6y_62-YOW4FOqha8pKgWrBiEXVrJE"),
    ]

    if session is not None:
        any_user = session.scalar(select(LkUserRow.user_id).limit(1))
        if any_user:
            return

        org = LkOrganizationRow(slug="vella-local", name="Vella Local")
        session.add(org)
        session.flush()

        for email, full_name, profile, password_hash in defaults:
            user = LkUserRow(
                user_id=email,
                organization_id=org.organization_id,
                email=email,
                password_hash=password_hash,
                full_name=full_name,
                permission_profile=profile,
                is_active=True,
            )
            session.add(user)
            session.flush()
            for permission in permissions_from_profile(profile):
                session.add(LkUserPermissionRow(user_id=user.user_id, permission=permission))
            _sync_membership(session, user, permissions_from_profile(profile))
            session.add(
                LkUserPreferenceRow(
                    user_id=user.user_id,
                    notification_settings=_default_notification_settings(),
                    export_settings=_default_export_settings(),
                    timezone="UTC",
                )
            )

        for provider in ("wb", "avito"):
            session.add(
                LkIntegrationRow(
                    organization_id=org.organization_id,
                    provider=provider,
                    status="disconnected",
                    external_account_id=None,
                    token_ref=None,
                    metadata_payload={},
                    updated_by_user_id="admin@vella.local",
                )
            )
        session.commit()
        return

    if _MEMORY.users:
        return
    now = _utc_now()
    org = _MemoryOrg(organization_id=_MEMORY.next_org_id, slug="vella-local", name="Vella Local", created_at=now)
    _MEMORY.organizations[org.organization_id] = org
    _MEMORY.next_org_id += 1

    for email, full_name, profile, password_hash in defaults:
        user = _MemoryUser(
            user_id=email,
            organization_id=org.organization_id,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            permission_profile=profile,
            permissions=set(permissions_from_profile(profile)),
            is_active=True,
            created_at=now,
        )
        _MEMORY.users[user.user_id] = user
        _MEMORY.users_by_email[email] = user.user_id
        _MEMORY.preferences[user.user_id] = UserPreferencesView(
            userId=user.user_id,
            notificationSettings=_default_notification_settings(),
            exportSettings=_default_export_settings(),
            timezone="UTC",
            updatedAt=now,
        )

    _MEMORY.integrations[(org.organization_id, "wb")] = IntegrationView(
        integrationId=1,
        organizationId=org.organization_id,
        provider="wb",
        status="disconnected",
        externalAccountId=None,
        tokenRef=None,
        metadata={},
        updatedByUserId="admin@vella.local",
        updatedAt=now,
    )
    _MEMORY.integrations[(org.organization_id, "avito")] = IntegrationView(
        integrationId=2,
        organizationId=org.organization_id,
        provider="avito",
        status="disconnected",
        externalAccountId=None,
        tokenRef=None,
        metadata={},
        updatedByUserId="admin@vella.local",
        updatedAt=now,
    )


def ensure_defaults() -> None:
    def _db(session: Session) -> bool:
        _ensure_defaults(session)
        return True

    result = _run_db(_db)
    if result is not None:
        return
    _ensure_defaults(None)


def register_organization_owner(*, email: str, password_hash: str, full_name: str, company_name: str) -> tuple[OrganizationView, TeamUserView]:
    email_normalized = email.strip().lower()
    profile = "admin"
    permissions = sorted(permissions_from_profile(profile))

    def _db(session: Session) -> tuple[OrganizationView, TeamUserView]:
        _ensure_defaults(session)
        exists = session.scalar(select(LkUserRow.user_id).where(LkUserRow.email == email_normalized))
        if exists:
            raise HTTPException(status_code=409, detail="EMAIL_ALREADY_REGISTERED")

        slug_base = _slugify(company_name)
        slug = slug_base
        index = 1
        while session.scalar(select(LkOrganizationRow.organization_id).where(LkOrganizationRow.slug == slug)) is not None:
            index += 1
            slug = f"{slug_base}-{index}"

        org = LkOrganizationRow(slug=slug, name=company_name.strip())
        session.add(org)
        session.flush()

        user_id = secrets.token_hex(12)
        user = LkUserRow(
            user_id=user_id,
            organization_id=org.organization_id,
            email=email_normalized,
            password_hash=password_hash,
            full_name=full_name.strip(),
            permission_profile=profile,
            is_active=True,
        )
        session.add(user)
        session.flush()

        for permission in permissions:
            session.add(LkUserPermissionRow(user_id=user.user_id, permission=permission))
        _sync_membership(session, user, permissions)

        session.add(
            LkUserPreferenceRow(
                user_id=user.user_id,
                notification_settings=_default_notification_settings(),
                export_settings=_default_export_settings(),
                timezone="UTC",
            )
        )

        for provider in ("wb", "avito"):
            session.add(
                LkIntegrationRow(
                    organization_id=org.organization_id,
                    provider=provider,
                    status="disconnected",
                    metadata_payload={},
                    updated_by_user_id=user.user_id,
                )
            )

        session.flush()
        session.commit()
        session.refresh(org)
        session.refresh(user)

        org_view = OrganizationView(
            organizationId=org.organization_id,
            slug=org.slug,
            name=org.name,
            createdAt=org.created_at,
        )
        user_view = _team_user_view(
            user_id=user.user_id,
            organization_id=org.organization_id,
            email=user.email,
            full_name=user.full_name,
            permission_profile=user.permission_profile,
            permissions=permissions,
            is_active=user.is_active,
            created_at=user.created_at,
        )
        return org_view, user_view

    result = _run_db(_db)
    if result is not None:
        org_view, user_view = result
        _append_audit_event(
            organization_id=org_view.organizationId,
            actor_user_id=user_view.userId,
            action="auth.register",
            object_type="lk_user",
            object_id=user_view.userId,
            details={"email": user_view.email},
        )
        return org_view, user_view

    _ensure_defaults(None)
    if email_normalized in _MEMORY.users_by_email:
        raise HTTPException(status_code=409, detail="EMAIL_ALREADY_REGISTERED")

    org_id = _MEMORY.next_org_id
    _MEMORY.next_org_id += 1
    now = _utc_now()
    slug_base = _slugify(company_name)
    slug = slug_base
    suffix = 1
    existing_slugs = {org.slug for org in _MEMORY.organizations.values()}
    while slug in existing_slugs:
        suffix += 1
        slug = f"{slug_base}-{suffix}"
    org = _MemoryOrg(organization_id=org_id, slug=slug, name=company_name.strip(), created_at=now)
    _MEMORY.organizations[org_id] = org

    user_id = secrets.token_hex(12)
    user = _MemoryUser(
        user_id=user_id,
        organization_id=org_id,
        email=email_normalized,
        password_hash=password_hash,
        full_name=full_name.strip(),
        permission_profile=profile,
        permissions=set(permissions),
        is_active=True,
        created_at=now,
    )
    _MEMORY.users[user_id] = user
    _MEMORY.users_by_email[email_normalized] = user_id
    _MEMORY.preferences[user_id] = UserPreferencesView(
        userId=user_id,
        notificationSettings=_default_notification_settings(),
        exportSettings=_default_export_settings(),
        timezone="UTC",
        updatedAt=now,
    )

    for provider in ("wb", "avito"):
        _MEMORY.integrations[(org_id, provider)] = IntegrationView(
            integrationId=len(_MEMORY.integrations) + 1,
            organizationId=org_id,
            provider=provider,  # type: ignore[arg-type]
            status="disconnected",
            metadata={},
            updatedByUserId=user_id,
            updatedAt=now,
        )

    _append_audit_event(
        organization_id=org_id,
        actor_user_id=user_id,
        action="auth.register",
        object_type="lk_user",
        object_id=user_id,
        details={"email": email_normalized},
    )

    return (
        OrganizationView(organizationId=org_id, slug=slug, name=org.name, createdAt=org.created_at),
        _team_user_view(
            user_id=user.user_id,
            organization_id=org_id,
            email=user.email,
            full_name=user.full_name,
            permission_profile=user.permission_profile,
            permissions=sorted(user.permissions),
            is_active=user.is_active,
            created_at=user.created_at,
        ),
    )


def get_user_auth_record_by_email(email: str) -> UserAuthRecord | None:
    email_normalized = email.strip().lower()

    def _db(session: Session) -> UserAuthRecord | None:
        _ensure_defaults(session)
        user = session.scalar(select(LkUserRow).where(LkUserRow.email == email_normalized))
        if user is None:
            return None
        permissions = session.scalars(select(LkUserPermissionRow.permission).where(LkUserPermissionRow.user_id == user.user_id)).all()
        return UserAuthRecord(
            user_id=user.user_id,
            organization_id=user.organization_id,
            email=user.email,
            password_hash=user.password_hash,
            full_name=user.full_name,
            permission_profile=user.permission_profile,
            permissions=frozenset(permissions),
            is_active=user.is_active,
        )

    result = _run_db(_db)
    if get_settings().wb_live_sync_enabled or _requires_persistent_storage():
        return result
    if result is not None:
        return result

    _ensure_defaults(None)
    user_id = _MEMORY.users_by_email.get(email_normalized)
    if not user_id:
        return None
    user = _MEMORY.users[user_id]
    return UserAuthRecord(
        user_id=user.user_id,
        organization_id=user.organization_id,
        email=user.email,
        password_hash=user.password_hash,
        full_name=user.full_name,
        permission_profile=user.permission_profile,
        permissions=frozenset(user.permissions),
        is_active=user.is_active,
    )


def create_session_for_user(
    *,
    user: UserAuthRecord,
    refresh_ttl_seconds: int,
    refresh_token_hash: str,
    user_agent: str | None,
    ip_address: str | None,
) -> SessionAuthRecord:
    issued_at = _utc_now()
    expires_at = issued_at + timedelta(seconds=max(60, refresh_ttl_seconds))
    session_id = secrets.token_urlsafe(24)

    def _db(session: Session) -> SessionAuthRecord:
        row = LkSessionRow(
            session_id=session_id,
            user_id=user.user_id,
            issued_at=issued_at,
            expires_at=expires_at,
            last_seen_at=issued_at,
            revoked_at=None,
            revoked_reason=None,
            user_agent=user_agent,
            ip_address=ip_address,
            refresh_token_hash=refresh_token_hash,
            refresh_token_expires_at=expires_at,
        )
        session.add(row)
        session.commit()
        return SessionAuthRecord(
            session_id=row.session_id,
            user_id=row.user_id,
            organization_id=user.organization_id,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            last_seen_at=row.last_seen_at,
            revoked_at=row.revoked_at,
            revoked_reason=row.revoked_reason,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            refresh_token_hash=row.refresh_token_hash,
            refresh_token_expires_at=row.refresh_token_expires_at,
        )

    result = _run_db(_db)
    if result is not None:
        _append_audit_event(
            organization_id=user.organization_id,
            actor_user_id=user.user_id,
        action="auth.login",
        object_type="lk_session",
        object_id=result.session_id,
        details={"email": user.email},
        reason="credentials accepted",
        ip_address=ip_address,
        user_agent=user_agent,
    )
        return result

    _ensure_defaults(None)
    mem = _MemorySession(
        session_id=session_id,
        user_id=user.user_id,
        organization_id=user.organization_id,
        issued_at=issued_at,
        expires_at=expires_at,
        last_seen_at=issued_at,
        revoked_at=None,
        revoked_reason=None,
        user_agent=user_agent,
        ip_address=ip_address,
        refresh_token_hash=refresh_token_hash,
        refresh_token_expires_at=expires_at,
    )
    _MEMORY.sessions[session_id] = mem
    _append_audit_event(
        organization_id=user.organization_id,
        actor_user_id=user.user_id,
        action="auth.login",
        object_type="lk_session",
        object_id=session_id,
        details={"email": user.email},
        reason="credentials accepted",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return SessionAuthRecord(
        session_id=session_id,
        user_id=user.user_id,
        organization_id=user.organization_id,
        issued_at=issued_at,
        expires_at=expires_at,
        last_seen_at=issued_at,
        revoked_at=None,
        revoked_reason=None,
        user_agent=user_agent,
        ip_address=ip_address,
        refresh_token_hash=refresh_token_hash,
        refresh_token_expires_at=expires_at,
    )


def resolve_active_session(*, session_id: str, user_id: str) -> tuple[UserAuthRecord, SessionAuthRecord] | None:
    now = _utc_now()

    def _db(session: Session) -> tuple[UserAuthRecord, SessionAuthRecord] | None:
        row = session.get(LkSessionRow, session_id)
        if row is None or row.user_id != user_id:
            return None
        if row.revoked_at is not None or row.expires_at <= now:
            return None

        user = session.get(LkUserRow, user_id)
        if user is None or not user.is_active:
            return None

        row.last_seen_at = now
        session.flush()

        permissions = session.scalars(select(LkUserPermissionRow.permission).where(LkUserPermissionRow.user_id == user_id)).all()
        session.commit()

        return (
            UserAuthRecord(
                user_id=user.user_id,
                organization_id=user.organization_id,
                email=user.email,
                password_hash=user.password_hash,
                full_name=user.full_name,
                permission_profile=user.permission_profile,
                permissions=frozenset(permissions),
                is_active=user.is_active,
            ),
            SessionAuthRecord(
                session_id=row.session_id,
                user_id=row.user_id,
                organization_id=user.organization_id,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                last_seen_at=now,
                revoked_at=row.revoked_at,
                revoked_reason=row.revoked_reason,
                user_agent=row.user_agent,
                ip_address=row.ip_address,
                refresh_token_hash=row.refresh_token_hash,
                refresh_token_expires_at=row.refresh_token_expires_at,
            ),
        )

    result = _run_db(_db)
    if get_settings().wb_live_sync_enabled or _requires_persistent_storage():
        return result
    if result is not None:
        return result

    _ensure_defaults(None)
    row = _MEMORY.sessions.get(session_id)
    if row is None or row.user_id != user_id:
        return None
    if row.revoked_at is not None or row.expires_at <= now:
        return None

    user = _MEMORY.users.get(user_id)
    if user is None or not user.is_active:
        return None

    row.last_seen_at = now
    return (
        UserAuthRecord(
            user_id=user.user_id,
            organization_id=user.organization_id,
            email=user.email,
            password_hash=user.password_hash,
            full_name=user.full_name,
            permission_profile=user.permission_profile,
            permissions=frozenset(user.permissions),
            is_active=user.is_active,
        ),
        SessionAuthRecord(
            session_id=row.session_id,
            user_id=row.user_id,
            organization_id=row.organization_id,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            last_seen_at=row.last_seen_at,
            revoked_at=row.revoked_at,
            revoked_reason=row.revoked_reason,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            refresh_token_hash=row.refresh_token_hash,
            refresh_token_expires_at=row.refresh_token_expires_at,
        ),
    )


def resolve_refresh_session_by_token_hash(refresh_token_hash: str) -> tuple[UserAuthRecord, SessionAuthRecord] | None:
    now = _utc_now()

    def _db(session: Session) -> tuple[UserAuthRecord, SessionAuthRecord] | None:
        row = session.scalar(select(LkSessionRow).where(LkSessionRow.refresh_token_hash == refresh_token_hash))
        matched_previous = False
        if row is None:
            row = session.scalar(select(LkSessionRow).where(LkSessionRow.previous_refresh_token_hash == refresh_token_hash))
            matched_previous = row is not None
        if row is None:
            return None
        if row.revoked_at is not None:
            return None
        token_expires_at = row.previous_refresh_token_expires_at if matched_previous else row.refresh_token_expires_at
        if token_expires_at is None or token_expires_at <= now:
            return None

        user = session.get(LkUserRow, row.user_id)
        if user is None or not user.is_active:
            return None

        permissions = session.scalars(select(LkUserPermissionRow.permission).where(LkUserPermissionRow.user_id == row.user_id)).all()
        return (
            UserAuthRecord(
                user_id=user.user_id,
                organization_id=user.organization_id,
                email=user.email,
                password_hash=user.password_hash,
                full_name=user.full_name,
                permission_profile=user.permission_profile,
                permissions=frozenset(permissions),
                is_active=user.is_active,
            ),
            SessionAuthRecord(
                session_id=row.session_id,
                user_id=row.user_id,
                organization_id=user.organization_id,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                last_seen_at=row.last_seen_at,
                revoked_at=row.revoked_at,
                revoked_reason=row.revoked_reason,
                user_agent=row.user_agent,
                ip_address=row.ip_address,
                refresh_token_hash=row.refresh_token_hash,
                refresh_token_expires_at=row.refresh_token_expires_at,
                previous_refresh_token_hash=row.previous_refresh_token_hash,
                previous_refresh_token_expires_at=row.previous_refresh_token_expires_at,
            ),
        )

    result = _run_db(_db)
    if get_settings().wb_live_sync_enabled or _requires_persistent_storage():
        return result
    if result is not None:
        return result

    _ensure_defaults(None)
    for row in _MEMORY.sessions.values():
        matched_previous = row.previous_refresh_token_hash == refresh_token_hash
        if row.refresh_token_hash != refresh_token_hash and not matched_previous:
            continue
        if row.revoked_at is not None:
            continue
        token_expires_at = row.previous_refresh_token_expires_at if matched_previous else row.refresh_token_expires_at
        if token_expires_at is None or token_expires_at <= now:
            continue
        user = _MEMORY.users.get(row.user_id)
        if user is None or not user.is_active:
            continue
        return (
            UserAuthRecord(
                user_id=user.user_id,
                organization_id=user.organization_id,
                email=user.email,
                password_hash=user.password_hash,
                full_name=user.full_name,
                permission_profile=user.permission_profile,
                permissions=frozenset(user.permissions),
                is_active=user.is_active,
            ),
            SessionAuthRecord(
                session_id=row.session_id,
                user_id=row.user_id,
                organization_id=row.organization_id,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                last_seen_at=row.last_seen_at,
                revoked_at=row.revoked_at,
                revoked_reason=row.revoked_reason,
                user_agent=row.user_agent,
                ip_address=row.ip_address,
                refresh_token_hash=row.refresh_token_hash,
                refresh_token_expires_at=row.refresh_token_expires_at,
                previous_refresh_token_hash=row.previous_refresh_token_hash,
                previous_refresh_token_expires_at=row.previous_refresh_token_expires_at,
            ),
        )
    return None


def rotate_refresh_token(
    *,
    session_id: str,
    user_id: str,
    new_refresh_token_hash: str,
    refresh_ttl_seconds: int,
    rotation_grace_seconds: int,
    ip_address: str | None,
    user_agent: str | None,
) -> SessionAuthRecord | None:
    now = _utc_now()
    new_expires_at = now + timedelta(seconds=max(60, refresh_ttl_seconds))

    def _db(session: Session) -> SessionAuthRecord | None:
        row = session.get(LkSessionRow, session_id)
        if row is None or row.user_id != user_id:
            return None
        if row.revoked_at is not None:
            return None
        user = session.get(LkUserRow, row.user_id)
        if user is None:
            return None
        row.previous_refresh_token_hash = row.refresh_token_hash
        row.previous_refresh_token_expires_at = now + timedelta(seconds=max(0, rotation_grace_seconds))
        row.refresh_token_hash = new_refresh_token_hash
        row.refresh_token_expires_at = new_expires_at
        row.expires_at = new_expires_at
        row.last_seen_at = now
        if ip_address:
            row.ip_address = ip_address
        if user_agent:
            row.user_agent = user_agent
        session.commit()
        return SessionAuthRecord(
            session_id=row.session_id,
            user_id=row.user_id,
            organization_id=user.organization_id,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            last_seen_at=row.last_seen_at,
            revoked_at=row.revoked_at,
            revoked_reason=row.revoked_reason,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            refresh_token_hash=row.refresh_token_hash,
            refresh_token_expires_at=row.refresh_token_expires_at,
            previous_refresh_token_hash=row.previous_refresh_token_hash,
            previous_refresh_token_expires_at=row.previous_refresh_token_expires_at,
        )

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    row = _MEMORY.sessions.get(session_id)
    if row is None or row.user_id != user_id:
        return None
    if row.revoked_at is not None:
        return None
    row.previous_refresh_token_hash = row.refresh_token_hash
    row.previous_refresh_token_expires_at = now + timedelta(seconds=max(0, rotation_grace_seconds))
    row.refresh_token_hash = new_refresh_token_hash
    row.refresh_token_expires_at = new_expires_at
    row.expires_at = new_expires_at
    row.last_seen_at = now
    if ip_address:
        row.ip_address = ip_address
    if user_agent:
        row.user_agent = user_agent
    return SessionAuthRecord(
        session_id=row.session_id,
        user_id=row.user_id,
        organization_id=row.organization_id,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
        revoked_reason=row.revoked_reason,
        user_agent=row.user_agent,
        ip_address=row.ip_address,
        refresh_token_hash=row.refresh_token_hash,
        refresh_token_expires_at=row.refresh_token_expires_at,
        previous_refresh_token_hash=row.previous_refresh_token_hash,
        previous_refresh_token_expires_at=row.previous_refresh_token_expires_at,
    )


def revoke_session(
    *,
    session_id: str,
    actor_user_id: str,
    organization_id: int,
    reason: str = "manual_logout",
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionView:
    now = _utc_now()

    def _db(session: Session) -> SessionView:
        row = session.get(LkSessionRow, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
        row.revoked_at = now
        row.revoked_reason = reason
        row.refresh_token_hash = None
        row.refresh_token_expires_at = None
        row.last_seen_at = now
        session.commit()
        return SessionView(
            sessionId=row.session_id,
            userId=row.user_id,
            issuedAt=row.issued_at,
            expiresAt=row.expires_at,
            lastSeenAt=row.last_seen_at,
            revokedAt=row.revoked_at,
            revokedReason=row.revoked_reason,
            userAgent=row.user_agent,
            ipAddress=row.ip_address,
        )

    result = _run_db(_db)
    if result is not None:
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="session.revoke",
            object_type="lk_session",
            object_id=session_id,
            details=None,
            reason=reason,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return result

    _ensure_defaults(None)
    row = _MEMORY.sessions.get(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
    row.revoked_at = now
    row.revoked_reason = reason
    row.refresh_token_hash = None
    row.refresh_token_expires_at = None
    row.last_seen_at = now
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="session.revoke",
        object_type="lk_session",
        object_id=session_id,
        details=None,
        reason=reason,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return SessionView(
        sessionId=row.session_id,
        userId=row.user_id,
        issuedAt=row.issued_at,
        expiresAt=row.expires_at,
        lastSeenAt=row.last_seen_at,
        revokedAt=row.revoked_at,
        revokedReason=row.revoked_reason,
        userAgent=row.user_agent,
        ipAddress=row.ip_address,
    )


def list_user_sessions(user_id: str) -> list[SessionView]:
    now = _utc_now()

    def _db(session: Session) -> list[SessionView]:
        rows = session.scalars(select(LkSessionRow).where(LkSessionRow.user_id == user_id).order_by(LkSessionRow.issued_at.desc())).all()
        return [
            SessionView(
                sessionId=row.session_id,
                userId=row.user_id,
                issuedAt=row.issued_at,
                expiresAt=row.expires_at,
                lastSeenAt=row.last_seen_at,
                revokedAt=row.revoked_at,
                revokedReason=row.revoked_reason,
                userAgent=row.user_agent,
                ipAddress=row.ip_address,
            )
            for row in rows
            if row.expires_at > now
        ]

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    rows = [row for row in _MEMORY.sessions.values() if row.user_id == user_id and row.expires_at > now]
    rows.sort(key=lambda row: row.issued_at, reverse=True)
    return [
        SessionView(
            sessionId=row.session_id,
            userId=row.user_id,
            issuedAt=row.issued_at,
            expiresAt=row.expires_at,
            lastSeenAt=row.last_seen_at,
            revokedAt=row.revoked_at,
            revokedReason=row.revoked_reason,
            userAgent=row.user_agent,
            ipAddress=row.ip_address,
        )
        for row in rows
    ]


def list_team_users(organization_id: int) -> list[TeamUserView]:
    def _db(session: Session) -> list[TeamUserView]:
        rows = session.scalars(select(LkUserRow).where(LkUserRow.organization_id == organization_id).order_by(LkUserRow.created_at.asc())).all()
        users: list[TeamUserView] = []
        for row in rows:
            permissions = session.scalars(select(LkUserPermissionRow.permission).where(LkUserPermissionRow.user_id == row.user_id)).all()
            users.append(
                _team_user_view(
                    user_id=row.user_id,
                    organization_id=row.organization_id,
                    email=row.email,
                    full_name=row.full_name,
                    permission_profile=row.permission_profile,
                    permissions=permissions,
                    is_active=row.is_active,
                    created_at=row.created_at,
                )
            )
        return users

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    rows = [row for row in _MEMORY.users.values() if row.organization_id == organization_id]
    rows.sort(key=lambda row: row.created_at)
    return [
        _team_user_view(
            user_id=row.user_id,
            organization_id=row.organization_id,
            email=row.email,
            full_name=row.full_name,
            permission_profile=row.permission_profile,
            permissions=sorted(row.permissions),
            is_active=row.is_active,
            created_at=row.created_at,
        )
        for row in rows
    ]


def create_team_user(
    *,
    organization_id: int,
    email: str,
    password_hash: str,
    full_name: str,
    permission_profile: str,
    actor_user_id: str,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> TeamUserView:
    normalized_email = email.strip().lower()
    profile = normalize_permission_profile(permission_profile)
    permissions = sorted(permissions_from_profile(profile))

    def _db(session: Session) -> TeamUserView:
        _ensure_defaults(session)
        exists = session.scalar(select(LkUserRow.user_id).where(LkUserRow.email == normalized_email))
        if exists:
            raise HTTPException(status_code=409, detail="EMAIL_ALREADY_REGISTERED")

        user = LkUserRow(
            user_id=secrets.token_hex(12),
            organization_id=organization_id,
            email=normalized_email,
            password_hash=password_hash,
            full_name=full_name.strip(),
            permission_profile=profile,
            is_active=True,
        )
        session.add(user)
        session.flush()

        session.execute(delete(LkUserPermissionRow).where(LkUserPermissionRow.user_id == user.user_id))
        for permission in permissions:
            session.add(LkUserPermissionRow(user_id=user.user_id, permission=permission))
        _sync_membership(session, user, permissions)

        session.add(
            LkUserPreferenceRow(
                user_id=user.user_id,
                notification_settings=_default_notification_settings(),
                export_settings=_default_export_settings(),
                timezone="UTC",
            )
        )

        session.commit()
        session.refresh(user)

        return _team_user_view(
            user_id=user.user_id,
            organization_id=user.organization_id,
            email=user.email,
            full_name=user.full_name,
            permission_profile=user.permission_profile,
            permissions=permissions,
            is_active=user.is_active,
            created_at=user.created_at,
        )

    result = _run_db(_db)
    if result is not None:
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="team.user.create",
            object_type="lk_user",
            object_id=result.userId,
            details={"email": result.email, "permissionProfile": result.permissionProfile},
            reason=reason or "team member created",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return result

    _ensure_defaults(None)
    if normalized_email in _MEMORY.users_by_email:
        raise HTTPException(status_code=409, detail="EMAIL_ALREADY_REGISTERED")
    user_id = secrets.token_hex(12)
    now = _utc_now()
    record = _MemoryUser(
        user_id=user_id,
        organization_id=organization_id,
        email=normalized_email,
        password_hash=password_hash,
        full_name=full_name.strip(),
        permission_profile=profile,
        permissions=set(permissions),
        is_active=True,
        created_at=now,
    )
    _MEMORY.users[user_id] = record
    _MEMORY.users_by_email[normalized_email] = user_id
    _MEMORY.preferences[user_id] = UserPreferencesView(
        userId=user_id,
        notificationSettings=_default_notification_settings(),
        exportSettings=_default_export_settings(),
        timezone="UTC",
        updatedAt=now,
    )
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="team.user.create",
        object_type="lk_user",
        object_id=user_id,
        details={"email": normalized_email, "permissionProfile": profile},
        reason=reason or "team member created",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return _team_user_view(
        user_id=record.user_id,
        organization_id=record.organization_id,
        email=record.email,
        full_name=record.full_name,
        permission_profile=record.permission_profile,
        permissions=sorted(record.permissions),
        is_active=record.is_active,
        created_at=record.created_at,
    )


def update_team_user_permission_profile(
    *,
    organization_id: int,
    user_id: str,
    permission_profile: str,
    actor_user_id: str,
    reason: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> TeamUserView:
    normalized_profile = normalize_permission_profile(permission_profile)
    new_permissions = sorted(permissions_from_profile(normalized_profile))

    def _db(session: Session) -> TeamUserView:
        user = session.get(LkUserRow, user_id)
        if user is None or user.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="TEAM_USER_NOT_FOUND")
        before_state = {"permissionProfile": user.permission_profile}
        user.permission_profile = normalized_profile
        session.execute(delete(LkUserPermissionRow).where(LkUserPermissionRow.user_id == user.user_id))
        for permission in new_permissions:
            session.add(LkUserPermissionRow(user_id=user.user_id, permission=permission))
        _sync_membership(session, user, new_permissions)
        session.commit()
        session.refresh(user)
        view = _team_user_view(
            user_id=user.user_id,
            organization_id=user.organization_id,
            email=user.email,
            full_name=user.full_name,
            permission_profile=user.permission_profile,
            permissions=new_permissions,
            is_active=user.is_active,
            created_at=user.created_at,
        )
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="team.user.permission_profile.update",
            object_type="lk_user",
            object_id=user.user_id,
            before_state=before_state,
            after_state={"permissionProfile": view.permissionProfile},
            reason=reason or "permission profile updated",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return view

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    user = _MEMORY.users.get(user_id)
    if user is None or user.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="TEAM_USER_NOT_FOUND")
    before_state = {"permissionProfile": user.permission_profile}
    user.permission_profile = normalized_profile
    user.permissions = set(new_permissions)
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="team.user.permission_profile.update",
        object_type="lk_user",
        object_id=user.user_id,
        before_state=before_state,
        after_state={"permissionProfile": user.permission_profile},
        reason=reason or "permission profile updated",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return _team_user_view(
        user_id=user.user_id,
        organization_id=user.organization_id,
        email=user.email,
        full_name=user.full_name,
        permission_profile=user.permission_profile,
        permissions=sorted(user.permissions),
        is_active=user.is_active,
        created_at=user.created_at,
    )


def list_integrations(organization_id: int) -> list[IntegrationView]:
    def _db(session: Session) -> list[IntegrationView]:
        rows = session.scalars(
            select(LkIntegrationRow).where(LkIntegrationRow.organization_id == organization_id).order_by(LkIntegrationRow.provider.asc())
        ).all()
        return [
            IntegrationView(
                integrationId=row.integration_id,
                organizationId=row.organization_id,
                provider=row.provider,  # type: ignore[arg-type]
                status=row.status,  # type: ignore[arg-type]
                externalAccountId=row.external_account_id,
                tokenRef=row.token_ref,
                metadata=row.metadata_payload,
                updatedByUserId=row.updated_by_user_id,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    rows = [value for key, value in _MEMORY.integrations.items() if key[0] == organization_id]
    rows.sort(key=lambda row: row.provider)
    return rows


def upsert_integration(
    *,
    organization_id: int,
    provider: str,
    status: IntegrationStatus,
    external_account_id: str | None,
    token_ref: str | None,
    metadata: dict,
    actor_user_id: str,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> IntegrationView:
    provider_value = provider.lower()
    if provider_value not in {"wb", "avito"}:
        raise HTTPException(status_code=400, detail=f"UNSUPPORTED_PROVIDER:{provider}")

    now = _utc_now()

    def _db(session: Session) -> IntegrationView:
        row = session.scalar(
            select(LkIntegrationRow).where(
                LkIntegrationRow.organization_id == organization_id,
                LkIntegrationRow.provider == provider_value,
            )
        )
        if row is None:
            row = LkIntegrationRow(
                organization_id=organization_id,
                provider=provider_value,
                status=status,
                external_account_id=external_account_id,
                token_ref=token_ref,
                metadata_payload=metadata,
                updated_by_user_id=actor_user_id,
            )
            session.add(row)
        else:
            row.status = status
            row.external_account_id = external_account_id
            row.token_ref = token_ref
            row.metadata_payload = metadata
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
        session.commit()
        session.refresh(row)
        return IntegrationView(
            integrationId=row.integration_id,
            organizationId=row.organization_id,
            provider=row.provider,  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            externalAccountId=row.external_account_id,
            tokenRef=row.token_ref,
            metadata=row.metadata_payload,
            updatedByUserId=row.updated_by_user_id,
            updatedAt=row.updated_at,
        )

    result = _run_db(_db)
    if result is not None:
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="integration.upsert",
            object_type="lk_integration",
            object_id=f"{provider_value}",
            details={"status": status},
            reason=reason or "integration state changed",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return result

    _ensure_defaults(None)
    current = _MEMORY.integrations.get((organization_id, provider_value))
    integration_id = current.integrationId if current else len(_MEMORY.integrations) + 1
    value = IntegrationView(
        integrationId=integration_id,
        organizationId=organization_id,
        provider=provider_value,  # type: ignore[arg-type]
        status=status,
        externalAccountId=external_account_id,
        tokenRef=token_ref,
        metadata=metadata,
        updatedByUserId=actor_user_id,
        updatedAt=now,
    )
    _MEMORY.integrations[(organization_id, provider_value)] = value
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="integration.upsert",
        object_type="lk_integration",
        object_id=f"{provider_value}",
        details={"status": status},
        reason=reason or "integration state changed",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return value


def get_user_wb_token(user_id: str) -> UserWbTokenView:
    def _db(session: Session) -> UserWbTokenView:
        row = session.scalar(select(LkUserWbTokenRow).where(LkUserWbTokenRow.user_id == user_id))
        if row is None:
            return UserWbTokenView(userId=user_id, hasToken=False, tokenMasked=None, updatedAt=None)
        return UserWbTokenView(
            userId=row.user_id,
            hasToken=True,
            tokenMasked=row.token_masked,
            updatedAt=(
                row.updated_at.astimezone(timezone.utc)
                if row.updated_at.utcoffset() is not None
                else row.updated_at
            ),
        )

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    token = _MEMORY.user_wb_tokens.get(user_id)
    if token is None:
        return UserWbTokenView(userId=user_id, hasToken=False, tokenMasked=None, updatedAt=None)
    return token


def get_user_wb_token_secret(user_id: str) -> str | None:
    def _db(session: Session) -> str | None:
        row = session.scalar(select(LkUserWbTokenRow).where(LkUserWbTokenRow.user_id == user_id))
        if row is None:
            return None
        return row.wb_token

    result = _run_db(_db)
    if result is not None or _requires_persistent_storage():
        return result

    _ensure_defaults(None)
    return _MEMORY.user_wb_token_secrets.get(user_id)


def get_organization_wb_token_secret(organization_id: int) -> str | None:
    def _db(session: Session) -> str | None:
        row = session.scalar(
            select(LkUserWbTokenRow)
            .join(LkUserRow, LkUserRow.user_id == LkUserWbTokenRow.user_id)
            .where(
                LkUserWbTokenRow.organization_id == organization_id,
                LkUserRow.is_active.is_(True),
            )
            .order_by(LkUserWbTokenRow.updated_at.desc(), LkUserWbTokenRow.created_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        return row.wb_token

    result = _run_db(_db)
    if result is not None or _requires_persistent_storage():
        return result

    _ensure_defaults(None)
    candidates: list[tuple[datetime, str]] = []
    for user_id, token_view in _MEMORY.user_wb_tokens.items():
        user = _MEMORY.users.get(user_id)
        secret = _MEMORY.user_wb_token_secrets.get(user_id)
        if user is None or not user.is_active or user.organization_id != organization_id or not secret:
            continue
        candidates.append((token_view.updatedAt or datetime.min.replace(tzinfo=timezone.utc), secret))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def get_user_avito_credentials(user_id: str) -> UserAvitoCredentialsView:
    def _db(session: Session) -> UserAvitoCredentialsView:
        row = session.scalar(select(LkUserAvitoCredentialsRow).where(LkUserAvitoCredentialsRow.user_id == user_id))
        if row is None:
            return UserAvitoCredentialsView(userId=user_id, hasCredentials=False, updatedAt=None)
        return UserAvitoCredentialsView(
            userId=row.user_id,
            hasCredentials=True,
            clientIdMasked=row.client_id_masked,
            clientSecretMasked=row.client_secret_masked,
            accessTokenExpiresAt=row.access_token_expires_at,
            updatedAt=row.updated_at,
        )

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    value = _MEMORY.user_avito_credentials.get(user_id)
    if value is None:
        return UserAvitoCredentialsView(userId=user_id, hasCredentials=False, updatedAt=None)
    return value


def get_user_avito_credentials_secret(user_id: str) -> AvitoCredentialsSecret | None:
    def _db(session: Session) -> AvitoCredentialsSecret | None:
        row = session.scalar(select(LkUserAvitoCredentialsRow).where(LkUserAvitoCredentialsRow.user_id == user_id))
        if row is None:
            return None
        return AvitoCredentialsSecret(
            client_id=row.client_id,
            client_secret=row.client_secret,
            cached_access_token=row.cached_access_token,
            access_token_expires_at=row.access_token_expires_at,
        )

    result = _run_db(_db)
    if result is not None or _requires_persistent_storage():
        return result

    _ensure_defaults(None)
    return _MEMORY.user_avito_credential_secrets.get(user_id)


def get_organization_avito_credentials_secret(organization_id: int) -> AvitoCredentialsSecret | None:
    def _db(session: Session) -> AvitoCredentialsSecret | None:
        row = session.scalar(
            select(LkUserAvitoCredentialsRow)
            .join(LkUserRow, LkUserRow.user_id == LkUserAvitoCredentialsRow.user_id)
            .where(
                LkUserAvitoCredentialsRow.organization_id == organization_id,
                LkUserRow.is_active.is_(True),
            )
            .order_by(LkUserAvitoCredentialsRow.updated_at.desc(), LkUserAvitoCredentialsRow.created_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        return AvitoCredentialsSecret(
            client_id=row.client_id,
            client_secret=row.client_secret,
            cached_access_token=row.cached_access_token,
            access_token_expires_at=row.access_token_expires_at,
        )

    result = _run_db(_db)
    if result is not None or _requires_persistent_storage():
        return result

    _ensure_defaults(None)
    candidates: list[tuple[datetime, AvitoCredentialsSecret]] = []
    for user_id, view in _MEMORY.user_avito_credentials.items():
        user = _MEMORY.users.get(user_id)
        secret = _MEMORY.user_avito_credential_secrets.get(user_id)
        if user is None or not user.is_active or user.organization_id != organization_id or secret is None:
            continue
        candidates.append((view.updatedAt or datetime.min.replace(tzinfo=timezone.utc), secret))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def upsert_user_avito_credentials(
    *,
    user_id: str,
    organization_id: int,
    actor_user_id: str,
    client_id: str,
    client_secret: str,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> UserAvitoCredentialsView:
    normalized_client_id = client_id.strip()
    normalized_client_secret = client_secret.strip()
    if len(normalized_client_id) < 4:
        raise HTTPException(status_code=400, detail="AVITO_CLIENT_ID_TOO_SHORT")
    if len(normalized_client_secret) < 8:
        raise HTTPException(status_code=400, detail="AVITO_CLIENT_SECRET_TOO_SHORT")
    now = _utc_now()
    client_id_masked = _mask_avito_secret("avito-client", normalized_client_id)
    client_secret_masked = _mask_avito_secret("avito-secret", normalized_client_secret)

    def _db(session: Session) -> UserAvitoCredentialsView:
        user = session.get(LkUserRow, user_id)
        if user is None or user.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

        row = session.scalar(select(LkUserAvitoCredentialsRow).where(LkUserAvitoCredentialsRow.user_id == user_id))
        before_state = {
            "hasCredentials": row is not None,
            "clientIdMasked": row.client_id_masked if row else None,
            "clientSecretMasked": row.client_secret_masked if row else None,
        }
        if row is None:
            row = LkUserAvitoCredentialsRow(
                user_id=user_id,
                organization_id=organization_id,
                client_id=normalized_client_id,
                client_secret=normalized_client_secret,
                client_id_masked=client_id_masked,
                client_secret_masked=client_secret_masked,
                created_by_user_id=actor_user_id,
                updated_by_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.client_id = normalized_client_id
            row.client_secret = normalized_client_secret
            row.client_id_masked = client_id_masked
            row.client_secret_masked = client_secret_masked
            row.cached_access_token = None
            row.access_token_expires_at = None
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
        session.commit()
        session.refresh(row)
        view = UserAvitoCredentialsView(
            userId=row.user_id,
            hasCredentials=True,
            clientIdMasked=row.client_id_masked,
            clientSecretMasked=row.client_secret_masked,
            accessTokenExpiresAt=row.access_token_expires_at,
            updatedAt=row.updated_at,
        )
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="integration.avito_credentials.upsert",
            object_type="lk_user_avito_credentials",
            object_id=user_id,
            before_state=before_state,
            after_state=view.model_dump(mode="json"),
            reason=reason or "avito credentials created or updated",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return view

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    user = _MEMORY.users.get(user_id)
    if user is None or user.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

    previous = _MEMORY.user_avito_credentials.get(user_id)
    before_state = previous.model_dump(mode="json") if previous is not None else {"hasCredentials": False}
    view = UserAvitoCredentialsView(
        userId=user_id,
        hasCredentials=True,
        clientIdMasked=client_id_masked,
        clientSecretMasked=client_secret_masked,
        accessTokenExpiresAt=None,
        updatedAt=now,
    )
    _MEMORY.user_avito_credentials[user_id] = view
    _MEMORY.user_avito_credential_secrets[user_id] = AvitoCredentialsSecret(
        client_id=normalized_client_id,
        client_secret=normalized_client_secret,
        cached_access_token=None,
        access_token_expires_at=None,
    )
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="integration.avito_credentials.upsert",
        object_type="lk_user_avito_credentials",
        object_id=user_id,
        before_state=before_state,
        after_state=view.model_dump(mode="json"),
        reason=reason or "avito credentials created or updated",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return view


def cache_user_avito_access_token(user_id: str, access_token: str, expires_at: datetime) -> None:
    def _db(session: Session) -> None:
        row = session.scalar(select(LkUserAvitoCredentialsRow).where(LkUserAvitoCredentialsRow.user_id == user_id))
        if row is None:
            return
        row.cached_access_token = access_token
        row.access_token_expires_at = expires_at
        session.commit()

    result = _run_db(_db)
    if result is not None:
        return

    secret = _MEMORY.user_avito_credential_secrets.get(user_id)
    if secret is None:
        return
    _MEMORY.user_avito_credential_secrets[user_id] = AvitoCredentialsSecret(
        client_id=secret.client_id,
        client_secret=secret.client_secret,
        cached_access_token=access_token,
        access_token_expires_at=expires_at,
    )
    view = _MEMORY.user_avito_credentials.get(user_id)
    if view is not None:
        _MEMORY.user_avito_credentials[user_id] = view.model_copy(update={"accessTokenExpiresAt": expires_at})


def upsert_user_wb_token(
    *,
    user_id: str,
    organization_id: int,
    actor_user_id: str,
    wb_token: str,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> UserWbTokenView:
    if get_settings().wb_live_sync_enabled:
        raise HTTPException(409, detail={"code": "WB_USE_ACCOUNT_CONNECTION"})
    normalized = wb_token.strip()
    if len(normalized) < 8:
        raise HTTPException(status_code=400, detail="WB_TOKEN_TOO_SHORT")
    now = _utc_now()
    masked = _mask_wb_token(normalized)
    invalidated_bindings = 0

    def _db(session: Session) -> UserWbTokenView:
        nonlocal invalidated_bindings
        user = session.get(LkUserRow, user_id)
        if user is None or user.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

        row = session.scalar(select(LkUserWbTokenRow).where(LkUserWbTokenRow.user_id == user_id))
        before_state = {"hasToken": row is not None, "tokenMasked": row.token_masked if row else None}
        if row is None:
            row = LkUserWbTokenRow(
                user_id=user_id,
                organization_id=organization_id,
                wb_token=normalized,
                token_masked=masked,
                created_by_user_id=actor_user_id,
                updated_by_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            if not compare_digest(row.wb_token.encode(), normalized.encode()):
                invalidated_bindings = invalidate_wb_credential_bindings(
                    session, organization_id, row.token_id
                )
            row.wb_token = normalized
            row.token_masked = masked
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
        session.commit()
        session.refresh(row)
        view = UserWbTokenView(
            userId=row.user_id,
            hasToken=True,
            tokenMasked=row.token_masked,
            updatedAt=row.updated_at,
        )
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="integration.wb_token.upsert",
            object_type="lk_user_wb_token",
            object_id=user_id,
            details={"credentialBindingsInvalidated": invalidated_bindings},
            before_state=before_state,
            after_state=view.model_dump(mode="json"),
            reason=reason or "wb token created or updated",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return view

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    user = _MEMORY.users.get(user_id)
    if user is None or user.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

    previous = _MEMORY.user_wb_tokens.get(user_id)
    before_state = previous.model_dump(mode="json") if previous is not None else {"hasToken": False, "tokenMasked": None}
    view = UserWbTokenView(
        userId=user_id,
        hasToken=True,
        tokenMasked=masked,
        updatedAt=now,
    )
    _MEMORY.user_wb_tokens[user_id] = view
    _MEMORY.user_wb_token_secrets[user_id] = normalized
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="integration.wb_token.upsert",
        object_type="lk_user_wb_token",
        object_id=user_id,
        before_state=before_state,
        after_state=view.model_dump(mode="json"),
        reason=reason or "wb token created or updated",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return view


def delete_user_wb_token(
    *,
    user_id: str,
    organization_id: int,
    actor_user_id: str,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> UserWbTokenView:
    if get_settings().wb_live_sync_enabled:
        raise HTTPException(409, detail={"code": "WB_USE_ACCOUNT_CONNECTION"})
    invalidated_bindings = 0

    def _db(session: Session) -> UserWbTokenView:
        nonlocal invalidated_bindings
        user = session.get(LkUserRow, user_id)
        if user is None or user.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

        row = session.scalar(select(LkUserWbTokenRow).where(LkUserWbTokenRow.user_id == user_id))
        before_state = {"hasToken": row is not None, "tokenMasked": row.token_masked if row else None}
        if row is not None:
            invalidated_bindings = invalidate_wb_credential_bindings(
                session, organization_id, row.token_id
            )
            session.delete(row)
            session.commit()
        after_state = UserWbTokenView(userId=user_id, hasToken=False, tokenMasked=None, updatedAt=None)
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="integration.wb_token.delete",
            object_type="lk_user_wb_token",
            object_id=user_id,
            details={"credentialBindingsInvalidated": invalidated_bindings},
            before_state=before_state,
            after_state=after_state.model_dump(mode="json"),
            reason=reason or "wb token deleted",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return after_state

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    user = _MEMORY.users.get(user_id)
    if user is None or user.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

    before = _MEMORY.user_wb_tokens.pop(user_id, None)
    _MEMORY.user_wb_token_secrets.pop(user_id, None)
    before_state = before.model_dump(mode="json") if before is not None else {"hasToken": False, "tokenMasked": None}
    after_state = UserWbTokenView(userId=user_id, hasToken=False, tokenMasked=None, updatedAt=None)
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="integration.wb_token.delete",
        object_type="lk_user_wb_token",
        object_id=user_id,
        before_state=before_state,
        after_state=after_state.model_dump(mode="json"),
        reason=reason or "wb token deleted",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return after_state


def delete_user_avito_credentials(
    *,
    user_id: str,
    organization_id: int,
    actor_user_id: str,
    reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> UserAvitoCredentialsView:
    def _db(session: Session) -> UserAvitoCredentialsView:
        user = session.get(LkUserRow, user_id)
        if user is None or user.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

        row = session.scalar(select(LkUserAvitoCredentialsRow).where(LkUserAvitoCredentialsRow.user_id == user_id))
        before_state = {
            "hasCredentials": row is not None,
            "clientIdMasked": row.client_id_masked if row else None,
            "clientSecretMasked": row.client_secret_masked if row else None,
        }
        if row is not None:
            session.delete(row)
            session.commit()
        after_state = UserAvitoCredentialsView(userId=user_id, hasCredentials=False, updatedAt=None)
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="integration.avito_credentials.delete",
            object_type="lk_user_avito_credentials",
            object_id=user_id,
            before_state=before_state,
            after_state=after_state.model_dump(mode="json"),
            reason=reason or "avito credentials deleted",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return after_state

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    user = _MEMORY.users.get(user_id)
    if user is None or user.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

    before = _MEMORY.user_avito_credentials.pop(user_id, None)
    _MEMORY.user_avito_credential_secrets.pop(user_id, None)
    before_state = before.model_dump(mode="json") if before is not None else {"hasCredentials": False}
    after_state = UserAvitoCredentialsView(userId=user_id, hasCredentials=False, updatedAt=None)
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="integration.avito_credentials.delete",
        object_type="lk_user_avito_credentials",
        object_id=user_id,
        before_state=before_state,
        after_state=after_state.model_dump(mode="json"),
        reason=reason or "avito credentials deleted",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return after_state


def get_preferences(user_id: str) -> UserPreferencesView:
    def _db(session: Session) -> UserPreferencesView:
        row = session.scalar(select(LkUserPreferenceRow).where(LkUserPreferenceRow.user_id == user_id))
        if row is None:
            now = _utc_now()
            row = LkUserPreferenceRow(
                user_id=user_id,
                notification_settings=_default_notification_settings(),
                export_settings=_default_export_settings(),
                timezone="UTC",
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
        return UserPreferencesView(
            userId=row.user_id,
            notificationSettings=row.notification_settings,
            exportSettings=row.export_settings,
            timezone=row.timezone,
            updatedAt=row.updated_at,
        )

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    if user_id not in _MEMORY.preferences:
        _MEMORY.preferences[user_id] = UserPreferencesView(
            userId=user_id,
            notificationSettings=_default_notification_settings(),
            exportSettings=_default_export_settings(),
            timezone="UTC",
            updatedAt=_utc_now(),
        )
    return _MEMORY.preferences[user_id]


def update_preferences(
    *,
    user_id: str,
    organization_id: int,
    actor_user_id: str,
    notification_settings: dict,
    export_settings: dict,
    timezone_value: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> UserPreferencesView:
    now = _utc_now()
    canonical = get_settings().canonical_notifications_enabled
    if canonical:
        unavailable = False
        try:
            unavailable = get_engine().dialect.name != "postgresql"
        except Exception:  # noqa: BLE001 - engine/configuration diagnostics are private.
            unavailable = True
        if unavailable:
            raise HTTPException(503, detail={"code": "PREFERENCES_UNAVAILABLE"})

    def _db(session: Session) -> UserPreferencesView:
        statement = select(LkUserPreferenceRow).where(LkUserPreferenceRow.user_id == user_id)
        if canonical:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if canonical:
            if row is None:
                raise HTTPException(503, detail={"code": "PREFERENCES_UNAVAILABLE"})
            if row.notification_settings != notification_settings:
                raise HTTPException(409, detail={"code": "PREFERENCES_CANONICAL_REQUIRED"})
        if row is None:
            row = LkUserPreferenceRow(
                user_id=user_id,
                notification_settings=notification_settings,
                export_settings=export_settings,
                timezone=timezone_value,
                updated_at=now,
            )
            session.add(row)
        else:
            row.notification_settings = notification_settings
            row.export_settings = export_settings
            row.timezone = timezone_value
            row.updated_at = now
        if canonical:
            session.add(LkAuditEventRow(
                organization_id=organization_id, actor_user_id=actor_user_id,
                action="preferences.update", object_type="lk_preferences", object_id=user_id,
                reason="preferences updated",
            ))
        session.commit()
        session.refresh(row)
        return UserPreferencesView(
            userId=row.user_id,
            notificationSettings=row.notification_settings,
            exportSettings=row.export_settings,
            timezone=row.timezone,
            updatedAt=row.updated_at.astimezone(UTC) if canonical else row.updated_at,
        )

    result = _run_db(_db)
    if canonical and result is None:
        # _run_db's legacy outage fallback must never accept this activated writer.
        raise HTTPException(503, detail={"code": "PREFERENCES_READBACK_REQUIRED"})
    if canonical:
        return result
    if result is not None:
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="preferences.update",
            object_type="lk_preferences",
            object_id=user_id,
            details={"timezone": timezone_value},
            reason="preferences updated",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return result

    _ensure_defaults(None)
    value = UserPreferencesView(
        userId=user_id,
        notificationSettings=notification_settings,
        exportSettings=export_settings,
        timezone=timezone_value,
        updatedAt=now,
    )
    _MEMORY.preferences[user_id] = value
    _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="preferences.update",
        object_type="lk_preferences",
        object_id=user_id,
        details={"timezone": timezone_value},
        reason="preferences updated",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return value


def list_audit_events(
    organization_id: int,
    limit: int = 100,
    offset: int = 0,
    action_prefix: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    actor_user_id: str | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> tuple[list[AuditEventView], int]:
    def _db(session: Session) -> tuple[list[AuditEventView], int]:
        stmt = select(LkAuditEventRow).where(LkAuditEventRow.organization_id == organization_id).order_by(LkAuditEventRow.event_id.desc())
        if action_prefix:
            stmt = stmt.where(LkAuditEventRow.action.like(f"{action_prefix}%"))
        if object_type:
            stmt = stmt.where(LkAuditEventRow.object_type == object_type)
        if object_id:
            stmt = stmt.where(LkAuditEventRow.object_id == object_id)
        if actor_user_id:
            stmt = stmt.where(LkAuditEventRow.actor_user_id == actor_user_id)
        if created_from:
            start = datetime.combine(created_from, datetime.min.time(), tzinfo=timezone.utc)
            stmt = stmt.where(LkAuditEventRow.created_at >= start)
        if created_to:
            end = datetime.combine(created_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
            stmt = stmt.where(LkAuditEventRow.created_at < end)
        rows = session.scalars(stmt).all()
        items = [
            AuditEventView(
                eventId=row.event_id,
                organizationId=row.organization_id,
                actorUserId=row.actor_user_id,
                action=row.action,
                objectType=row.object_type,
                objectId=row.object_id,
                details=row.details,
                beforeState=row.before_state,
                afterState=row.after_state,
                reason=row.reason,
                ipAddress=row.ip_address,
                userAgent=row.user_agent,
                createdAt=row.created_at,
            )
            for row in rows
        ]
        total = len(items)
        return items[offset : offset + limit], total

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    rows = [row for row in _MEMORY.audit_events if row.organizationId == organization_id]
    if action_prefix:
        rows = [row for row in rows if row.action.startswith(action_prefix)]
    if object_type:
        rows = [row for row in rows if row.objectType == object_type]
    if object_id:
        rows = [row for row in rows if row.objectId == object_id]
    if actor_user_id:
        rows = [row for row in rows if row.actorUserId == actor_user_id]
    if created_from:
        rows = [row for row in rows if row.createdAt.date() >= created_from]
    if created_to:
        rows = [row for row in rows if row.createdAt.date() <= created_to]
    rows.sort(key=lambda row: row.eventId, reverse=True)
    total = len(rows)
    return rows[offset : offset + limit], total


def revoke_other_sessions(user_id: str, current_session_id: str | None, actor_user_id: str, organization_id: int) -> int:
    now = _utc_now()

    def _db(session: Session) -> int:
        rows = session.scalars(select(LkSessionRow).where(LkSessionRow.user_id == user_id)).all()
        count = 0
        for row in rows:
            if current_session_id and row.session_id == current_session_id:
                continue
            if row.revoked_at is not None:
                continue
            if row.expires_at <= now:
                continue
            row.revoked_at = now
            row.revoked_reason = "logout_other_devices"
            row.refresh_token_hash = None
            row.refresh_token_expires_at = None
            row.last_seen_at = now
            count += 1
        session.commit()
        return count

    result = _run_db(_db)
    if result is not None:
        if result > 0:
            _append_audit_event(
                organization_id=organization_id,
                actor_user_id=actor_user_id,
                action="session.revoke_others",
                object_type="lk_session",
                object_id=user_id,
                details={"revokedCount": result},
                reason="logout other devices",
            )
        return result

    _ensure_defaults(None)
    count = 0
    for row in _MEMORY.sessions.values():
        if row.user_id != user_id:
            continue
        if current_session_id and row.session_id == current_session_id:
            continue
        if row.revoked_at is not None:
            continue
        if row.expires_at <= now:
            continue
        row.revoked_at = now
        row.revoked_reason = "logout_other_devices"
        row.refresh_token_hash = None
        row.refresh_token_expires_at = None
        row.last_seen_at = now
        count += 1
    if count > 0:
        _append_audit_event(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action="session.revoke_others",
            object_type="lk_session",
            object_id=user_id,
            details={"revokedCount": count},
            reason="logout other devices",
        )
    return count


def get_user_and_org_views(user_id: str) -> tuple[TeamUserView, OrganizationView]:
    def _db(session: Session) -> tuple[TeamUserView, OrganizationView]:
        user = session.get(LkUserRow, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
        org = session.get(LkOrganizationRow, user.organization_id)
        if org is None:
            raise HTTPException(status_code=404, detail="ORG_NOT_FOUND")
        permissions = session.scalars(select(LkUserPermissionRow.permission).where(LkUserPermissionRow.user_id == user_id)).all()
        return (
            _team_user_view(
                user_id=user.user_id,
                organization_id=user.organization_id,
                email=user.email,
                full_name=user.full_name,
                permission_profile=user.permission_profile,
                permissions=permissions,
                is_active=user.is_active,
                created_at=user.created_at,
            ),
            OrganizationView(
                organizationId=org.organization_id,
                slug=org.slug,
                name=org.name,
                createdAt=org.created_at,
            ),
        )

    result = _run_db(_db)
    if result is not None:
        return result

    _ensure_defaults(None)
    user = _MEMORY.users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
    org = _MEMORY.organizations.get(user.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="ORG_NOT_FOUND")
    return (
        _team_user_view(
            user_id=user.user_id,
            organization_id=user.organization_id,
            email=user.email,
            full_name=user.full_name,
            permission_profile=user.permission_profile,
            permissions=sorted(user.permissions),
            is_active=user.is_active,
            created_at=user.created_at,
        ),
        OrganizationView(
            organizationId=org.organization_id,
            slug=org.slug,
            name=org.name,
            createdAt=org.created_at,
        ),
    )


def build_cabinet_me(*, user_id: str, current_session_id: str | None = None) -> CabinetMeView:
    user_view, org_view = get_user_and_org_views(user_id)
    preferences = get_preferences(user_id)
    active_session: SessionView | None = None
    if current_session_id:
        sessions = list_user_sessions(user_id)
        for row in sessions:
            if row.sessionId == current_session_id:
                active_session = row
                break
    return CabinetMeView(
        organization=org_view,
        user=user_view,
        activeSession=active_session,
        preferences=preferences,
    )


def record_audit_event(
    *,
    organization_id: int,
    actor_user_id: str | None,
    action: str,
    object_type: str,
    object_id: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditEventView:
    return _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        details=details,
        before_state=before_state,
        after_state=after_state,
        reason=reason,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def record_login_failure(email: str, reason: str, ip_address: str | None, user_agent: str | None) -> AuditEventView:
    ensure_defaults()
    user = get_user_auth_record_by_email(email)
    organization_id = user.organization_id if user else 1
    actor_user_id = user.user_id if user else None
    return _append_audit_event(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="auth.login.failed",
        object_type="lk_user",
        object_id=(user.user_id if user else email.lower()),
        details={"email": email.lower()},
        reason=reason,
        ip_address=ip_address,
        user_agent=user_agent,
    )
