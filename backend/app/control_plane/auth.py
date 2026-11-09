from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from app.cabinet.permissions import permissions_from_profile
from app.cabinet.store import (
    UserAuthRecord,
    create_session_for_user,
    upsert_user_wb_token,
    ensure_defaults,
    get_user_auth_record_by_email,
    record_audit_event,
    record_login_failure,
    register_organization_owner,
    resolve_active_session,
    resolve_refresh_session_by_token_hash,
    revoke_session,
    rotate_refresh_token,
)
from app.config import get_settings


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    user_id: str
    organization_id: int
    permission_profile: str
    permissions: frozenset[str]
    email: str | None = None
    session_id: str | None = None

    @property
    def role(self) -> str:
        return self.permission_profile


@dataclass(frozen=True)
class AccessTokenBundle:
    access_token: str
    expires_in: int


@dataclass(frozen=True)
class AuthTokenBundle:
    access_token: str
    refresh_token: str
    expires_in: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _hash_password(password: str, salt: str, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return _b64url_encode(digest)


def _hash_secret_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    iterations = 120000
    salt = _b64url_encode(hashlib.sha256(f"{password}:{_utc_now().timestamp()}".encode("utf-8")).digest())[:16]
    digest = _hash_password(password=password, salt=salt, iterations=iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _verify_password(password: str, encoded_hash: str) -> bool:
    if encoded_hash.startswith("plain:"):
        return hmac.compare_digest(password, encoded_hash.removeprefix("plain:"))

    parts = encoded_hash.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
    except ValueError:
        return False
    salt = parts[2]
    expected = parts[3]
    actual = _hash_password(password=password, salt=salt, iterations=iterations)
    return hmac.compare_digest(actual, expected)


def _issue_access_token(*, actor: ActorContext) -> AccessTokenBundle:
    settings = get_settings()
    now = _utc_now()
    expires_in = max(60, settings.auth_access_ttl_seconds)
    payload = {
        "ver": 2,
        "sub": actor.user_id,
        "sid": actor.session_id,
        "org": actor.organization_id,
        "email": actor.email,
        "permissionProfile": actor.permission_profile,
        "permissions": sorted(actor.permissions),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _sign(payload_b64, settings.auth_secret)
    return AccessTokenBundle(access_token=f"{payload_b64}.{signature}", expires_in=expires_in)


def _decode_token_payload(raw_token: str) -> dict:
    settings = get_settings()
    parts = raw_token.split(".")
    if len(parts) != 2:
        raise ValueError("MALFORMED_TOKEN")
    payload_b64, signature = parts
    expected_signature = _sign(payload_b64, settings.auth_secret)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("BAD_SIGNATURE")

    payload_raw = _b64url_decode(payload_b64)
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("BAD_PAYLOAD") from exc

    required_fields = ("sub", "sid", "exp")
    if not all(field in payload for field in required_fields):
        raise ValueError("MISSING_CLAIMS")

    exp = payload["exp"]
    if not isinstance(exp, int) or int(_utc_now().timestamp()) >= exp:
        raise ValueError("TOKEN_EXPIRED")

    return payload


def _actor_from_user_record(user: UserAuthRecord, session_id: str | None = None) -> ActorContext:
    effective_permissions = frozenset(set(user.permissions) | set(permissions_from_profile(user.permission_profile)))
    return ActorContext(
        actor_id=user.user_id,
        user_id=user.user_id,
        organization_id=user.organization_id,
        permission_profile=user.permission_profile,
        permissions=effective_permissions,
        email=user.email,
        session_id=session_id,
    )


def _create_refresh_token_value() -> str:
    return secrets.token_urlsafe(64)


def _issue_tokens_for_user(*, user: UserAuthRecord, user_agent: str | None, ip_address: str | None) -> AuthTokenBundle:
    settings = get_settings()
    refresh_token = _create_refresh_token_value()
    refresh_token_hash = _hash_secret_token(refresh_token)
    session = create_session_for_user(
        user=user,
        refresh_ttl_seconds=settings.auth_refresh_ttl_seconds,
        refresh_token_hash=refresh_token_hash,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    actor = _actor_from_user_record(user, session_id=session.session_id)
    access = _issue_access_token(actor=actor)
    return AuthTokenBundle(access_token=access.access_token, refresh_token=refresh_token, expires_in=access.expires_in)


def register_with_email_password(
    *,
    email: str,
    password: str,
    full_name: str,
    company_name: str,
    wb_token: str | None,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[ActorContext, AuthTokenBundle]:
    if get_settings().wb_live_sync_enabled and wb_token:
        raise HTTPException(409, detail={"code": "WB_USE_ACCOUNT_CONNECTION"})
    ensure_defaults()
    password_hash = hash_password(password)
    _org, user_view = register_organization_owner(
        email=email,
        password_hash=password_hash,
        full_name=full_name,
        company_name=company_name,
    )
    auth_record = get_user_auth_record_by_email(user_view.email)
    if auth_record is None:
        raise HTTPException(status_code=500, detail="REGISTERED_USER_NOT_FOUND")
    tokens = _issue_tokens_for_user(user=auth_record, user_agent=user_agent, ip_address=ip_address)
    session_info = resolve_refresh_session_by_token_hash(_hash_secret_token(tokens.refresh_token))
    actor = _actor_from_user_record(auth_record, session_id=session_info[1].session_id if session_info else None)
    if wb_token and wb_token.strip():
        upsert_user_wb_token(
            user_id=auth_record.user_id,
            organization_id=auth_record.organization_id,
            actor_user_id=auth_record.user_id,
            wb_token=wb_token,
            reason="registration onboarding",
            ip_address=ip_address,
            user_agent=user_agent,
        )
    return actor, tokens


def login_with_email_password(*, email: str, password: str, user_agent: str | None, ip_address: str | None) -> AuthTokenBundle:
    ensure_defaults()
    user = get_user_auth_record_by_email(email)
    if user is None or not user.is_active:
        record_login_failure(email=email, reason="invalid_credentials", ip_address=ip_address, user_agent=user_agent)
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
    if not _verify_password(password=password, encoded_hash=user.password_hash):
        record_login_failure(email=email, reason="invalid_credentials", ip_address=ip_address, user_agent=user_agent)
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
    return _issue_tokens_for_user(user=user, user_agent=user_agent, ip_address=ip_address)


def refresh_access_token(*, refresh_token: str | None, user_agent: str | None, ip_address: str | None) -> AuthTokenBundle:
    if not refresh_token:
        raise HTTPException(status_code=401, detail="REFRESH_TOKEN_REQUIRED")

    refresh_hash = _hash_secret_token(refresh_token)
    resolved = resolve_refresh_session_by_token_hash(refresh_hash)
    if resolved is None:
        raise HTTPException(status_code=401, detail="INVALID_REFRESH_TOKEN")

    user, session = resolved
    new_refresh_token = _create_refresh_token_value()
    new_refresh_hash = _hash_secret_token(new_refresh_token)
    rotated = rotate_refresh_token(
        session_id=session.session_id,
        user_id=user.user_id,
        new_refresh_token_hash=new_refresh_hash,
        refresh_ttl_seconds=get_settings().auth_refresh_ttl_seconds,
        rotation_grace_seconds=get_settings().auth_refresh_rotation_grace_seconds,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    if rotated is None:
        raise HTTPException(status_code=401, detail="SESSION_INACTIVE")

    actor = _actor_from_user_record(user, session_id=rotated.session_id)
    access = _issue_access_token(actor=actor)
    record_audit_event(
        organization_id=user.organization_id,
        actor_user_id=user.user_id,
        action="auth.refresh",
        object_type="lk_session",
        object_id=rotated.session_id,
        reason="refresh token rotated",
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return AuthTokenBundle(access_token=access.access_token, refresh_token=new_refresh_token, expires_in=access.expires_in)


def logout_current_session(*, actor: ActorContext, ip_address: str | None, user_agent: str | None) -> None:
    if not actor.session_id:
        raise HTTPException(status_code=401, detail="SESSION_NOT_FOUND")
    revoke_session(
        session_id=actor.session_id,
        actor_user_id=actor.user_id,
        organization_id=actor.organization_id,
        reason="auth.logout",
        ip_address=ip_address,
        user_agent=user_agent,
    )


def actor_from_request(request: Request) -> ActorContext:
    authorization = request.headers.get("authorization")
    if not authorization:
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="INVALID_AUTH_SCHEME")

    return actor_from_access_token(token.strip())


def actor_from_access_token(access_token: str) -> ActorContext:
    try:
        payload = _decode_token_payload(access_token.strip())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"INVALID_AUTH_TOKEN:{exc}") from exc

    session_id = str(payload["sid"])
    user_id = str(payload["sub"])
    resolved = resolve_active_session(session_id=session_id, user_id=user_id)
    if resolved is None:
        raise HTTPException(status_code=401, detail="SESSION_INACTIVE")
    user, _session = resolved

    return ActorContext(
        actor_id=user.user_id,
        user_id=user.user_id,
        organization_id=user.organization_id,
        permission_profile=user.permission_profile,
        permissions=frozenset(set(user.permissions) | set(permissions_from_profile(user.permission_profile))),
        email=user.email,
        session_id=session_id,
    )


def has_permission(subject: ActorContext | str, permission: str) -> bool:
    if isinstance(subject, ActorContext):
        return permission in subject.permissions
    try:
        return permission in permissions_from_profile(subject)
    except ValueError:
        return False
