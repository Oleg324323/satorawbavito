"""Account-owned Avito ingestion-token lifecycle boundary."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, defer

from app.cabinet.orm import LkAuditEventRow
from app.infra.db import get_session_factory, set_tenant_context
from app.platform.integrations.credential_store import MarketplaceAccountCredentialOwner
from app.platform.integrations.orm import (
    MarketplaceAccountIngestionTokenRow,
    MarketplaceAccountRow,
)

INGESTION_TOKEN_SCOPE = "avito.browser_snapshot.write"
_TOKEN_PREFIX = "sat1"
_MAX_POSTGRES_INTEGER = (1 << 31) - 1
_SECRET_BYTES = 32
_SECRET_LENGTH = 43
_MIN_BEARER_LENGTH = len(_TOKEN_PREFIX) + 3 + 1 + 32 + _SECRET_LENGTH
_MAX_BEARER_LENGTH = (
    len(_TOKEN_PREFIX) + 3 + len(str(_MAX_POSTGRES_INTEGER)) + 32 + _SECRET_LENGTH
)
_UUID_HEX = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_URLSAFE_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_REVOCATION_REASONS = frozenset(
    {
        "token_rotated",
        "account_disconnected",
        "security_incident",
        "operator_revoked",
        "expired",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "ingestion_token_account_unavailable",
        "ingestion_token_already_revealed",
        "ingestion_token_contract_invalid",
        "ingestion_token_invalid",
        "ingestion_token_reason_invalid",
        "ingestion_token_serialization_refused",
        "ingestion_token_unavailable",
    }
)


class IngestionTokenStoreError(ValueError):
    """Fail-closed token-store error with a non-disclosing public form."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "ingestion_token_unavailable"
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"IngestionTokenStoreError(code={self.code!r})"


@dataclass(frozen=True)
class IngestionTokenMetadata:
    token_id: UUID
    organization_id: int
    marketplace_account_id: int
    provider: str
    scope: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revocation_reason_code: str | None
    last_used_at: datetime | None
    status: Literal["active", "expired", "revoked"]


@dataclass(frozen=True)
class VerifiedIngestionToken:
    token_id: UUID
    owner: MarketplaceAccountCredentialOwner
    scope: str
    expires_at: datetime
    account_binding: IngestionAccountBinding


@dataclass(frozen=True, slots=True, repr=False)
class IngestionAccountBinding:
    marketplace_account_id: int
    provider: str
    external_account_id: str
    credential_ref: str | None
    binding_version: int
    binding_schema_version: int = 1


class IssuedIngestionToken:
    """A one-time bearer reveal paired with metadata safe for serialization."""

    __slots__ = ("_metadata", "_raw_bearer")

    def __init__(self, metadata: IngestionTokenMetadata, raw_bearer: str) -> None:
        self._metadata = metadata
        self._raw_bearer: str | None = raw_bearer

    @property
    def metadata(self) -> IngestionTokenMetadata:
        return self._metadata

    def reveal(self) -> str:
        raw_bearer = self._raw_bearer
        if raw_bearer is None:
            raise IngestionTokenStoreError("ingestion_token_already_revealed")
        self._raw_bearer = None
        return raw_bearer

    def __repr__(self) -> str:
        return "<IssuedIngestionToken redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol: int):
        raise IngestionTokenStoreError("ingestion_token_serialization_refused")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_owner(owner: MarketplaceAccountCredentialOwner) -> None:
    if (
        not isinstance(owner, MarketplaceAccountCredentialOwner)
        or not _positive_int(owner.organization_id)
        or not _positive_int(owner.marketplace_account_id)
        or owner.organization_id > _MAX_POSTGRES_INTEGER
        or owner.marketplace_account_id > _MAX_POSTGRES_INTEGER
        or owner.provider != "avito"
    ):
        raise IngestionTokenStoreError("ingestion_token_contract_invalid")


def _validate_expiry(expires_at: datetime, now: datetime) -> datetime:
    if (
        not isinstance(expires_at, datetime)
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
    ):
        raise IngestionTokenStoreError("ingestion_token_contract_invalid")
    try:
        normalized = expires_at.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise IngestionTokenStoreError("ingestion_token_contract_invalid") from None
    if normalized <= now:
        raise IngestionTokenStoreError("ingestion_token_contract_invalid")
    return normalized


def _parse_bearer(raw_bearer: str) -> tuple[int, UUID, str]:
    if (
        not isinstance(raw_bearer, str)
        or len(raw_bearer) < _MIN_BEARER_LENGTH
        or len(raw_bearer) > _MAX_BEARER_LENGTH
    ):
        raise IngestionTokenStoreError("ingestion_token_invalid")
    parts = raw_bearer.split(".")
    if len(parts) != 4:
        raise IngestionTokenStoreError("ingestion_token_invalid")
    prefix, organization_text, token_text, secret = parts
    if (
        prefix != _TOKEN_PREFIX
        or not organization_text.isascii()
        or not organization_text.isdecimal()
        or organization_text.startswith("0")
        or len(organization_text) > len(str(_MAX_POSTGRES_INTEGER))
        or _UUID_HEX.fullmatch(token_text) is None
        or _URLSAFE_SECRET.fullmatch(secret) is None
    ):
        raise IngestionTokenStoreError("ingestion_token_invalid")
    organization_id = int(organization_text)
    if organization_id < 1 or organization_id > _MAX_POSTGRES_INTEGER:
        raise IngestionTokenStoreError("ingestion_token_invalid")
    return organization_id, UUID(hex=token_text), secret


def _new_session() -> Session:
    try:
        return get_session_factory()()
    except SQLAlchemyError:
        raise IngestionTokenStoreError("ingestion_token_unavailable") from None


def _rollback_safely(session: Session) -> None:
    try:
        session.rollback()
    except SQLAlchemyError:
        pass


def _close_safely(session: Session) -> None:
    try:
        session.close()
    except SQLAlchemyError:
        pass


def _account(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
    *,
    lock: bool,
) -> MarketplaceAccountRow | None:
    query = select(MarketplaceAccountRow).where(
        MarketplaceAccountRow.organization_id == owner.organization_id,
        MarketplaceAccountRow.marketplace_account_id == owner.marketplace_account_id,
        MarketplaceAccountRow.marketplace == owner.provider,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _require_connected_account(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
    *,
    lock: bool,
    verifier: bool = False,
) -> MarketplaceAccountRow:
    account = _account(session, owner, lock=lock)
    if account is None or account.status != "connected":
        code = (
            "ingestion_token_invalid"
            if verifier
            else "ingestion_token_account_unavailable"
        )
        raise IngestionTokenStoreError(code)
    return account


def _require_existing_account(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
    *,
    lock: bool,
) -> MarketplaceAccountRow:
    account = _account(session, owner, lock=lock)
    if account is None:
        raise IngestionTokenStoreError("ingestion_token_account_unavailable")
    return account


def _owner_rows(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
    *,
    active_only: bool,
    lock: bool,
) -> list[MarketplaceAccountIngestionTokenRow]:
    query = select(MarketplaceAccountIngestionTokenRow).where(
        MarketplaceAccountIngestionTokenRow.organization_id == owner.organization_id,
        MarketplaceAccountIngestionTokenRow.marketplace_account_id
        == owner.marketplace_account_id,
        MarketplaceAccountIngestionTokenRow.provider == "avito",
        MarketplaceAccountIngestionTokenRow.scope == INGESTION_TOKEN_SCOPE,
    ).options(defer(MarketplaceAccountIngestionTokenRow.verifier))
    if active_only:
        query = query.where(MarketplaceAccountIngestionTokenRow.revoked_at.is_(None))
    query = query.order_by(
        MarketplaceAccountIngestionTokenRow.issued_at,
        MarketplaceAccountIngestionTokenRow.token_id,
    )
    if lock:
        query = query.with_for_update()
    return list(session.scalars(query).all())


def _token_row(
    session: Session,
    *,
    organization_id: int,
    token_id: UUID,
    lock: bool,
) -> MarketplaceAccountIngestionTokenRow | None:
    query = select(MarketplaceAccountIngestionTokenRow).where(
        MarketplaceAccountIngestionTokenRow.organization_id == organization_id,
        MarketplaceAccountIngestionTokenRow.token_id == token_id,
        MarketplaceAccountIngestionTokenRow.provider == "avito",
        MarketplaceAccountIngestionTokenRow.scope == INGESTION_TOKEN_SCOPE,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalar(query)


def _latest_owner_row(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
) -> MarketplaceAccountIngestionTokenRow | None:
    return session.scalar(
        select(MarketplaceAccountIngestionTokenRow)
        .options(defer(MarketplaceAccountIngestionTokenRow.verifier))
        .where(
            MarketplaceAccountIngestionTokenRow.organization_id
            == owner.organization_id,
            MarketplaceAccountIngestionTokenRow.marketplace_account_id
            == owner.marketplace_account_id,
            MarketplaceAccountIngestionTokenRow.provider == "avito",
            MarketplaceAccountIngestionTokenRow.scope == INGESTION_TOKEN_SCOPE,
        )
        .order_by(
            MarketplaceAccountIngestionTokenRow.revoked_at.is_(None).desc(),
            MarketplaceAccountIngestionTokenRow.issued_at.desc(),
            MarketplaceAccountIngestionTokenRow.token_id.desc(),
        )
        .limit(1)
    )


def _metadata(
    row: MarketplaceAccountIngestionTokenRow, *, now: datetime
) -> IngestionTokenMetadata:
    issued_at = _as_utc(row.issued_at)
    expires_at = _as_utc(row.expires_at)
    revoked_at = _as_utc(row.revoked_at)
    last_used_at = _as_utc(row.last_used_at)
    if issued_at is None or expires_at is None:
        raise IngestionTokenStoreError("ingestion_token_unavailable")
    if revoked_at is not None:
        status: Literal["active", "expired", "revoked"] = "revoked"
    elif expires_at <= now:
        status = "expired"
    else:
        status = "active"
    return IngestionTokenMetadata(
        token_id=row.token_id,
        organization_id=int(row.organization_id),
        marketplace_account_id=int(row.marketplace_account_id),
        provider=row.provider,
        scope=row.scope,
        issued_at=issued_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
        revocation_reason_code=row.revocation_reason_code,
        last_used_at=last_used_at,
        status=status,
    )


def _audit(
    session: Session,
    row: MarketplaceAccountIngestionTokenRow,
    *,
    operation: Literal["issue", "revoke"],
    result_code: str,
    actor_user_id: str | None,
) -> None:
    session.add(
        LkAuditEventRow(
            organization_id=int(row.organization_id),
            actor_user_id=actor_user_id,
            action=f"integration.marketplace_ingestion_token.{operation}",
            object_type="marketplace_account_ingestion_token",
            object_id=str(row.token_id),
            details={
                "marketplaceAccountId": int(row.marketplace_account_id),
                "operation": operation,
                "provider": "avito",
                "resultCode": result_code,
                "scope": INGESTION_TOKEN_SCOPE,
            },
        )
    )


def _database_now(session: Session) -> datetime:
    now = session.scalar(select(func.clock_timestamp()))
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise IngestionTokenStoreError("ingestion_token_unavailable")
    return now.astimezone(timezone.utc)


def _captured_binding(account: MarketplaceAccountRow) -> IngestionAccountBinding:
    from app.platform.integrations.publication_guard import ExpectedAccountBinding, _integer

    ExpectedAccountBinding(account.marketplace_account_id, account.marketplace,
                           account.external_account_id, account.credential_ref)
    if not _integer(account.ingestion_binding_version, 2**63 - 1):
        raise IngestionTokenStoreError("ingestion_token_account_unavailable")
    return IngestionAccountBinding(account.marketplace_account_id, account.marketplace,
                                   account.external_account_id, account.credential_ref,
                                   account.ingestion_binding_version)


def _issue_ingestion_token_in_session(
    session: Session, owner: MarketplaceAccountCredentialOwner, *, expires_at: datetime,
    actor_user_id: str | None = None,
) -> IssuedIngestionToken:
    """Private participant: caller owns authentication, root, policy and commit.

    The one-time wrapper must not escape that root until its commit succeeded.
    """
    _validate_owner(owner)
    account = _require_connected_account(session, owner, lock=True)
    binding = _captured_binding(account)
    rows = _owner_rows(session, owner, active_only=True, lock=True)
    now = _database_now(session)
    normalized_expiry = _validate_expiry(expires_at, now)
    for current in rows:
        current.revoked_at = now
        current.revocation_reason_code = "token_rotated"
        _audit(session, current, operation="revoke", result_code="token_rotated", actor_user_id=actor_user_id)
    if rows:
        session.flush()
    secret = raw_bearer = None
    row = None
    try:
        secret = secrets.token_urlsafe(_SECRET_BYTES)
        if _URLSAFE_SECRET.fullmatch(secret) is None:
            raise IngestionTokenStoreError("ingestion_token_unavailable")
        token_id = uuid4()
        row = MarketplaceAccountIngestionTokenRow(
            token_id=token_id, organization_id=owner.organization_id,
            marketplace_account_id=owner.marketplace_account_id, provider="avito",
            verifier=hashlib.sha256(secret.encode("ascii")).digest(), scope=INGESTION_TOKEN_SCOPE,
            issued_at=now, expires_at=normalized_expiry, binding_schema_version=1,
            binding_external_account_id=binding.external_account_id,
            binding_credential_ref=binding.credential_ref, binding_version=binding.binding_version,
        )
        session.add(row)
        session.flush()
        _audit(session, row, operation="issue", result_code="issued", actor_user_id=actor_user_id)
        metadata = _metadata(row, now=now)
        # Do not leave a verifier-bearing ORM object attached to the Session.
        session.expunge(row)
        raw_bearer = f"{_TOKEN_PREFIX}.{owner.organization_id}.{token_id.hex}.{secret}"
        return IssuedIngestionToken(metadata, raw_bearer)
    finally:
        secret = raw_bearer = row = None


def issue_ingestion_token(
    owner: MarketplaceAccountCredentialOwner,
    *,
    expires_at: datetime,
    actor_user_id: str | None = None,
) -> IssuedIngestionToken:
    _validate_owner(owner)
    session = _new_session()
    issued = None
    committed = False
    try:
        set_tenant_context(session, owner.organization_id)
        issued = _issue_ingestion_token_in_session(session, owner, expires_at=expires_at, actor_user_id=actor_user_id)
        session.commit()
        committed = True
        return issued
    except IngestionTokenStoreError:
        _rollback_safely(session)
        raise
    except (IntegrityError, SQLAlchemyError):
        _rollback_safely(session)
        raise IngestionTokenStoreError("ingestion_token_unavailable") from None
    finally:
        if not committed and issued is not None:
            issued._raw_bearer = None
        _close_safely(session)


def _verify_ingestion_token_in_session(session: Session, *, raw_bearer: str) -> VerifiedIngestionToken:
    """The sole bearer verifier. Locator is untrusted until locked comparison.

    No verifier-bearing ORM rows enter the identity map. Token-only admission and
    the standalone verifier both deny legacy unbound tokens without rebinding.
    """
    secret = candidate_verifier = stored_verifier = token = None
    try:
        organization_id, token_id, secret = _parse_bearer(raw_bearer)
        candidate_verifier = hashlib.sha256(secret.encode("ascii")).digest()
        root = session.get_transaction()
        marker = session.info.get("satorna_tenant_context")
        if marker is not None and (type(marker) is not tuple or len(marker) != 2
                or (marker[0] is root and marker != (root, organization_id))):
            raise IngestionTokenStoreError("ingestion_token_invalid")
        current = session.scalar(text("SELECT current_setting('app.organization_id', true)"))
        if current not in (None, "", str(organization_id)):
            raise IngestionTokenStoreError("ingestion_token_invalid")
        # This is only an RLS locator restriction, not verified account context.
        set_tenant_context(session, organization_id)
        t = MarketplaceAccountIngestionTokenRow
        account_id = session.scalar(select(t.marketplace_account_id).where(
            t.organization_id == organization_id, t.token_id == token_id,
            t.provider == "avito", t.scope == INGESTION_TOKEN_SCOPE))
        if account_id is None:
            raise IngestionTokenStoreError("ingestion_token_invalid")
        owner = MarketplaceAccountCredentialOwner(organization_id, int(account_id), "avito")
        _validate_owner(owner)
        account = _require_connected_account(session, owner, lock=True, verifier=True)
        binding = _captured_binding(account)
        token = session.execute(select(t.marketplace_account_id, t.verifier, t.expires_at, t.revoked_at,
            t.binding_schema_version, t.binding_external_account_id, t.binding_credential_ref, t.binding_version
        ).where(t.organization_id == organization_id, t.token_id == token_id,
                t.provider == "avito", t.scope == INGESTION_TOKEN_SCOPE).with_for_update()).one_or_none()
        if token is None:
            raise IngestionTokenStoreError("ingestion_token_invalid")
        stored_verifier = bytes(token.verifier)
        secret_matches = hmac.compare_digest(stored_verifier, candidate_verifier)
        expires_at = _as_utc(token.expires_at)
        now = _database_now(session)
        if (not secret_matches or len(stored_verifier) != 32 or token.revoked_at is not None
                or expires_at is None or expires_at <= now
                or token.marketplace_account_id != owner.marketplace_account_id
                or (token.binding_schema_version, token.binding_external_account_id,
                    token.binding_credential_ref, token.binding_version) !=
                   (1, binding.external_account_id, binding.credential_ref, binding.binding_version)):
            raise IngestionTokenStoreError("ingestion_token_invalid")
        return VerifiedIngestionToken(token_id, owner, INGESTION_TOKEN_SCOPE, expires_at, binding)
    finally:
        raw_bearer = secret = candidate_verifier = stored_verifier = token = None


def verify_ingestion_token(raw_bearer: str) -> VerifiedIngestionToken:
    session = _new_session()
    try:
        with session.no_autoflush:
            verified = _verify_ingestion_token_in_session(session, raw_bearer=raw_bearer)
        now = _database_now(session)
        session.execute(text("UPDATE marketplace_account_ingestion_tokens SET last_used_at=:now "
                             "WHERE organization_id=:org AND token_id=:token"),
                        {"now": now, "org": verified.owner.organization_id, "token": verified.token_id})
        session.commit()
        return verified
    except IngestionTokenStoreError:
        _rollback_safely(session)
        raise
    except (IntegrityError, SQLAlchemyError):
        _rollback_safely(session)
        raise IngestionTokenStoreError("ingestion_token_unavailable") from None
    finally:
        raw_bearer = None
        _close_safely(session)


def _revoke_ingestion_tokens_in_session(session, owner, reason_code, *, actor_user_id=None):
    _validate_owner(owner)
    if type(reason_code) is not str or reason_code not in _REVOCATION_REASONS:
        raise IngestionTokenStoreError("ingestion_token_reason_invalid")
    _require_existing_account(session, owner, lock=True)
    rows = _owner_rows(session, owner, active_only=True, lock=True)
    now = _database_now(session)
    for row in rows:
        row.revoked_at = now
        row.revocation_reason_code = reason_code
        _audit(session, row, operation="revoke", result_code=reason_code, actor_user_id=actor_user_id)
    if rows:
        session.flush()
    return tuple(_metadata(row, now=now) for row in rows)


def _get_ingestion_token_status_in_session(session, owner):
    _validate_owner(owner)
    _require_existing_account(session, owner, lock=False)
    row = _latest_owner_row(session, owner)
    return None if row is None else _metadata(row, now=_database_now(session))


def revoke_ingestion_tokens(
    owner: MarketplaceAccountCredentialOwner,
    reason_code: str,
    *,
    actor_user_id: str | None = None,
) -> tuple[IngestionTokenMetadata, ...]:
    _validate_owner(owner)
    if reason_code not in _REVOCATION_REASONS:
        raise IngestionTokenStoreError("ingestion_token_reason_invalid")
    session = _new_session()
    try:
        set_tenant_context(session, owner.organization_id)
        metadata = _revoke_ingestion_tokens_in_session(session, owner, reason_code, actor_user_id=actor_user_id)
        session.commit()
        return metadata
    except IngestionTokenStoreError:
        _rollback_safely(session)
        raise
    except (IntegrityError, SQLAlchemyError):
        _rollback_safely(session)
        raise IngestionTokenStoreError("ingestion_token_unavailable") from None
    finally:
        _close_safely(session)


def get_ingestion_token_status(
    owner: MarketplaceAccountCredentialOwner,
) -> IngestionTokenMetadata | None:
    _validate_owner(owner)
    session = _new_session()
    try:
        set_tenant_context(session, owner.organization_id)
        return _get_ingestion_token_status_in_session(session, owner)
    except IngestionTokenStoreError:
        _rollback_safely(session)
        raise
    except SQLAlchemyError:
        _rollback_safely(session)
        raise IngestionTokenStoreError("ingestion_token_unavailable") from None
    finally:
        _close_safely(session)
