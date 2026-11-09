from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkAuditEventRow
from app.config import get_settings, load_marketplace_credential_keyring
from app.infra.db import get_session_factory, set_marketplace_account_context, set_tenant_context
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountRow,
)
from app.platform.integrations.publication_guard import _physical_connection
from app.platform.integrations.worker_identity import (
    ExecutorIdentityDenied,
    ExecutorRoleIdentity,
    verify_executor_login,
)
from app.security.marketplace_credentials import (
    CredentialCryptoError,
    CredentialIdentity,
    CredentialKeyring,
    DecryptedCredential,
    EncryptedCredential,
    decrypt_credential,
    encrypt_credential,
)

_REVOCATION_REASONS = frozenset(
    {
        "provider_rotated",
        "credential_replaced",
        "account_disconnected",
        "security_incident",
        "operator_revoked",
        "expired",
    }
)
_CREDENTIAL_KINDS_BY_PROVIDER = {
    "wb": frozenset({"wb_api"}),
    "avito": frozenset({"avito_oauth_client", "avito_oauth_access"}),
}
_SAFE_ERROR_CODES = frozenset(
    {
        "credential_account_not_found",
        "credential_account_identity_mismatch",
        "credential_concurrent_update",
        "credential_configuration_invalid",
        "credential_contract_invalid",
        "credential_expired",
        "credential_missing",
        "credential_persistence_failed",
        "credential_reason_invalid",
    }
)


class CredentialStoreError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "credential_persistence_failed"
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"CredentialStoreError(code={self.code!r})"


@dataclass(frozen=True)
class MarketplaceAccountCredentialOwner:
    organization_id: int
    marketplace_account_id: int
    provider: str


@dataclass(frozen=True)
class CredentialFetchBinding:
    """Safe evidence captured from the same statement as the fetched secret."""

    owner: MarketplaceAccountCredentialOwner
    external_account_id: str
    credential_ref: str | None
    credential_identity: CredentialIdentity


class ResolvedCredentialForFetch:
    """Explicit secret access; generic copies/serialization must fail closed."""

    __slots__ = ("__binding", "__secret")

    def __init__(self, secret: DecryptedCredential, binding: CredentialFetchBinding) -> None:
        self.__secret = secret
        self.__binding = binding

    @property
    def secret(self) -> DecryptedCredential:
        return self.__secret

    @property
    def binding(self) -> CredentialFetchBinding:
        return self.__binding

    @staticmethod
    def _refuse_serialization() -> NoReturn:
        raise TypeError("credential_contract_invalid")

    def __iter__(self) -> NoReturn:
        self._refuse_serialization()

    def __copy__(self) -> NoReturn:
        self._refuse_serialization()

    def __deepcopy__(self, memo: object) -> NoReturn:
        self._refuse_serialization()

    def __reduce__(self) -> NoReturn:
        self._refuse_serialization()

    def __reduce_ex__(self, protocol: int) -> NoReturn:
        self._refuse_serialization()

    def __repr__(self) -> str:
        return "<ResolvedCredentialForFetch redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class CredentialMetadata:
    credential_id: UUID
    organization_id: int
    marketplace_account_id: int
    provider: str
    credential_kind: str
    generation: int
    expires_at: datetime | None
    revoked_at: datetime | None
    revocation_reason_code: str | None
    created_at: datetime
    updated_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_keyring() -> CredentialKeyring:
    try:
        return load_marketplace_credential_keyring(get_settings())
    except RuntimeError:
        raise CredentialStoreError("credential_configuration_invalid") from None


def _validate_owner(owner: MarketplaceAccountCredentialOwner) -> None:
    if (
        not isinstance(owner.organization_id, int)
        or isinstance(owner.organization_id, bool)
        or owner.organization_id < 1
        or not isinstance(owner.marketplace_account_id, int)
        or isinstance(owner.marketplace_account_id, bool)
        or owner.marketplace_account_id < 1
        or owner.provider not in {"wb", "avito"}
    ):
        raise CredentialStoreError("credential_contract_invalid")


def _validate_kind(
    owner: MarketplaceAccountCredentialOwner,
    kind: str,
) -> None:
    if kind not in _CREDENTIAL_KINDS_BY_PROVIDER.get(owner.provider, frozenset()):
        raise CredentialStoreError("credential_contract_invalid")


def _parse_access_expiry(kind: str, plaintext: Mapping[str, Any]) -> datetime | None:
    if kind != "avito_oauth_access":
        return None
    raw = plaintext.get("expiresAt") if isinstance(plaintext, Mapping) else None
    if not isinstance(raw, str):
        raise CredentialStoreError("credential_contract_invalid")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise CredentialStoreError("credential_contract_invalid") from None
    return parsed


def _account(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
    *,
    lock: bool,
    expected_external_account_id: str | None = None,
) -> MarketplaceAccountRow:
    query = select(MarketplaceAccountRow).where(
        MarketplaceAccountRow.organization_id == owner.organization_id,
        MarketplaceAccountRow.marketplace_account_id == owner.marketplace_account_id,
        MarketplaceAccountRow.marketplace == owner.provider,
    )
    if lock:
        query = query.with_for_update()
    account = session.scalar(query)
    if account is None:
        raise CredentialStoreError("credential_account_not_found")
    if (
        expected_external_account_id is not None
        and account.external_account_id != expected_external_account_id
    ):
        raise CredentialStoreError("credential_account_identity_mismatch")
    return account


def _active_row(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
    kind: str,
    *,
    lock: bool,
) -> MarketplaceAccountCredentialRow | None:
    query = (
        select(MarketplaceAccountCredentialRow)
        .where(
            MarketplaceAccountCredentialRow.organization_id == owner.organization_id,
            MarketplaceAccountCredentialRow.marketplace_account_id == owner.marketplace_account_id,
            MarketplaceAccountCredentialRow.provider == owner.provider,
            MarketplaceAccountCredentialRow.credential_kind == kind,
            MarketplaceAccountCredentialRow.revoked_at.is_(None),
        )
        .order_by(MarketplaceAccountCredentialRow.generation.desc())
    )
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _latest_row(
    session: Session,
    owner: MarketplaceAccountCredentialOwner,
    kind: str,
) -> MarketplaceAccountCredentialRow | None:
    return session.scalar(
        select(MarketplaceAccountCredentialRow)
        .where(
            MarketplaceAccountCredentialRow.organization_id == owner.organization_id,
            MarketplaceAccountCredentialRow.marketplace_account_id
            == owner.marketplace_account_id,
            MarketplaceAccountCredentialRow.provider == owner.provider,
            MarketplaceAccountCredentialRow.credential_kind == kind,
        )
        .order_by(
            MarketplaceAccountCredentialRow.generation.desc(),
            MarketplaceAccountCredentialRow.created_at.desc(),
        )
    )


def _identity(row: MarketplaceAccountCredentialRow) -> CredentialIdentity:
    return CredentialIdentity(
        organization_id=int(row.organization_id),
        marketplace_account_id=int(row.marketplace_account_id),
        provider=row.provider,
        credential_kind=row.credential_kind,
        payload_schema_version=int(row.payload_schema_version),
        credential_id=row.credential_id,
        generation=int(row.generation),
        expires_at=_as_utc(row.expires_at),
    )


def _encrypted(row: MarketplaceAccountCredentialRow) -> EncryptedCredential:
    return EncryptedCredential(
        algorithm=row.algorithm,
        key_version=int(row.key_version),
        aad_version=int(row.aad_version),
        nonce=bytes(row.nonce),
        ciphertext=bytes(row.ciphertext),
    )


def _metadata(row: MarketplaceAccountCredentialRow) -> CredentialMetadata:
    return CredentialMetadata(
        credential_id=row.credential_id,
        organization_id=int(row.organization_id),
        marketplace_account_id=int(row.marketplace_account_id),
        provider=row.provider,
        credential_kind=row.credential_kind,
        generation=int(row.generation),
        expires_at=_as_utc(row.expires_at),
        revoked_at=_as_utc(row.revoked_at),
        revocation_reason_code=row.revocation_reason_code,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _audit(
    session: Session,
    row: MarketplaceAccountCredentialRow,
    *,
    operation: str,
    actor_user_id: str | None = None,
) -> None:
    session.add(
        LkAuditEventRow(
            organization_id=row.organization_id,
            actor_user_id=actor_user_id,
            action=f"integration.marketplace_credential.{operation}",
            object_type="marketplace_account_credential",
            object_id=str(row.credential_id),
            details={
                "credentialKind": row.credential_kind,
                "generation": int(row.generation),
                "marketplaceAccountId": int(row.marketplace_account_id),
                "operation": operation,
                "provider": row.provider,
                "resultCode": "ready",
            },
        )
    )


def _translate_persistence_error() -> CredentialStoreError:
    return CredentialStoreError("credential_persistence_failed")


def require_marketplace_credential_store_ready() -> None:
    """Fail closed before provider validation when the keyring is unavailable."""

    _load_keyring()


def _credential_transaction_inputs(session, account_identity, kind, now=None):
    """Validate participant inputs, not caller authorization or root ownership."""
    if not isinstance(session, Session) or not session.is_active:
        raise CredentialStoreError("credential_contract_invalid")
    if type(account_identity) is not MarketplaceAccountCredentialOwner:
        raise CredentialStoreError("credential_contract_invalid")
    _validate_owner(account_identity)
    _validate_kind(account_identity, kind)
    if now is not None and (
        type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None
    ):
        raise CredentialStoreError("credential_contract_invalid")


def _put_marketplace_credential_in_session(
    session: Session,
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
    plaintext: Mapping[str, Any],
    *,
    keyring: CredentialKeyring,
    now: datetime,
    actor_user_id: str | None = None,
    expected_external_account_id: str | None = None,
) -> CredentialMetadata:
    """Existing write participant; caller owns context, live guard and commit.

    No key load/session creation/commit/rollback. Returned metadata is provisional
    until the caller commits. Account state need not be connected for credential
    setup/revocation; this primitive does not use the publication fetch guard.
    """
    _credential_transaction_inputs(session, account_identity, kind, now)
    if now is None:
        raise CredentialStoreError("credential_contract_invalid")
    if type(keyring) is not CredentialKeyring:
        raise CredentialStoreError("credential_configuration_invalid")
    if expected_external_account_id is not None and (
        not isinstance(expected_external_account_id, str)
        or not expected_external_account_id
    ):
        raise CredentialStoreError("credential_contract_invalid")
    expires_at = _parse_access_expiry(kind, plaintext)
    _account(session, account_identity, lock=True,
             expected_external_account_id=expected_external_account_id)
    current = _active_row(session, account_identity, kind, lock=True)
    latest = current or _latest_row(session, account_identity, kind)
    generation = int(latest.generation) + 1 if latest is not None else 1
    if current is not None:
        current.revoked_at = now
        current.revocation_reason_code = "credential_replaced"
        current.updated_at = now
        session.flush()
    identity = CredentialIdentity(
        organization_id=account_identity.organization_id,
        marketplace_account_id=account_identity.marketplace_account_id,
        provider=account_identity.provider, credential_kind=kind,
        payload_schema_version=1, credential_id=uuid4(), generation=generation,
        expires_at=expires_at,
    )
    row = _insert_verified_credential_in_session(
        session, identity, plaintext, keyring=keyring, now=now,
    )
    _audit(session, row, operation="put", actor_user_id=actor_user_id)
    return _metadata(row)


def _insert_verified_credential_in_session(
    session: Session,
    identity: CredentialIdentity,
    plaintext: Mapping[str, Any],
    *,
    keyring: CredentialKeyring,
    now: datetime,
) -> MarketplaceAccountCredentialRow:
    """Store-only insert/readback primitive, before caller audit and commit.

    Caller owns identity/generation, tenant/auth/account locks and rollback. The
    ORM result is private to this store; no ciphertext/ORM export to consumers.
    """
    if (type(identity) is not CredentialIdentity or type(keyring) is not CredentialKeyring
            or not isinstance(session, Session) or not session.is_active
            or not session.in_transaction() or session.in_nested_transaction()
            or type(now) is not datetime or now.utcoffset() is None):
        raise CredentialStoreError("credential_contract_invalid")
    if (any(type(value) is not int or value <= 0 for value in (
            identity.organization_id, identity.marketplace_account_id,
            identity.payload_schema_version, identity.generation,
        )) or type(identity.credential_id) is not UUID
            or type(identity.provider) is not str or type(identity.credential_kind) is not str
            or not isinstance(plaintext, Mapping)):
        raise CredentialStoreError("credential_contract_invalid")
    root = session.get_transaction()
    connection = session.connection()
    physical_root = connection.get_transaction()

    def same_root():
        if (session.get_transaction() is not root or not root.is_active
                or not session.is_active or session.in_nested_transaction()
                or session.connection() is not connection or connection.in_nested_transaction()
                or connection.get_transaction() is not physical_root
                or physical_root is None or not physical_root.is_active):
            raise CredentialStoreError("credential_contract_invalid")
        if connection.dialect.name == "postgresql":
            if (_physical_connection(session) is not connection
                    or connection.get_isolation_level() != "READ COMMITTED"):
                raise CredentialStoreError("credential_contract_invalid")

    same_root()
    # Freeze only the already required in-memory comparison; never log/serialize
    # this payload or put it into audit. The existing crypto validates its schema.
    try:
        expected = dict(plaintext)
    except Exception:
        raise CredentialCryptoError("credential_contract_invalid") from None
    if any(type(key) is not str or type(value) is not str for key, value in expected.items()):
        raise CredentialCryptoError("credential_contract_invalid")
    encrypted = encrypt_credential(identity, expected, keyring)
    if decrypt_credential(identity, encrypted, keyring).reveal() != expected:
        raise CredentialCryptoError("credential_auth_failed")
    row = MarketplaceAccountCredentialRow(
        credential_id=identity.credential_id, organization_id=identity.organization_id,
        marketplace_account_id=identity.marketplace_account_id, provider=identity.provider,
        credential_kind=identity.credential_kind, algorithm=encrypted.algorithm,
        key_version=encrypted.key_version, aad_version=encrypted.aad_version,
        payload_schema_version=identity.payload_schema_version, nonce=encrypted.nonce,
        ciphertext=encrypted.ciphertext, generation=identity.generation,
        expires_at=identity.expires_at, created_at=now, updated_at=now,
    )
    session.add(row)
    session.flush()
    same_root()
    session.expire(row)
    # Force a real SELECT, not an identity-map hit. Frozen scope comes from the
    # requested identity, never from potentially corrupted/reassigned row fields.
    with session.no_autoflush:
        persisted = session.scalar(
            select(MarketplaceAccountCredentialRow).where(
                MarketplaceAccountCredentialRow.credential_id == identity.credential_id,
                MarketplaceAccountCredentialRow.organization_id == identity.organization_id,
                MarketplaceAccountCredentialRow.marketplace_account_id == identity.marketplace_account_id,
                MarketplaceAccountCredentialRow.provider == identity.provider,
                MarketplaceAccountCredentialRow.credential_kind == identity.credential_kind,
            ).execution_options(populate_existing=True)
        )
    same_root()
    if persisted is None:
        raise CredentialCryptoError("credential_auth_failed")
    # Check raw integer types before existing reconstruction helpers normalize.
    if any(type(getattr(persisted, name)) is not int for name in (
        "organization_id", "marketplace_account_id", "payload_schema_version",
        "generation", "key_version", "aad_version",
    )):
        raise CredentialCryptoError("credential_auth_failed")
    try:
        persisted_identity = _identity(persisted)
        persisted_encrypted = _encrypted(persisted)
    except (TypeError, ValueError, OverflowError):
        raise CredentialCryptoError("credential_auth_failed") from None
    if (persisted_identity != identity or persisted_encrypted != encrypted
            or persisted.revoked_at is not None or persisted.revocation_reason_code is not None
            or _as_utc(persisted.created_at) != now or _as_utc(persisted.updated_at) != now):
        raise CredentialCryptoError("credential_auth_failed")
    actual = decrypt_credential(persisted_identity, persisted_encrypted, keyring).reveal()
    if (type(actual) is not dict or actual.keys() != expected.keys()
            or any(type(actual[key]) is not str or actual[key] != value for key, value in expected.items())):
        raise CredentialCryptoError("credential_auth_failed")
    same_root()
    return persisted


def put_marketplace_credential(
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
    plaintext: Mapping[str, Any],
    *,
    actor_user_id: str | None = None,
    expected_external_account_id: str | None = None,
) -> CredentialMetadata:
    _validate_owner(account_identity)
    _validate_kind(account_identity, kind)
    if expected_external_account_id is not None and (
        not isinstance(expected_external_account_id, str) or not expected_external_account_id
    ):
        raise CredentialStoreError("credential_contract_invalid")
    # Retain public validation-before-key-load error precedence.
    _parse_access_expiry(kind, plaintext)
    keyring = _load_keyring()
    now = _utc_now()
    with get_session_factory()() as session:
        try:
            set_tenant_context(session, account_identity.organization_id)
            result = _put_marketplace_credential_in_session(
                session, account_identity, kind, plaintext, keyring=keyring, now=now,
                actor_user_id=actor_user_id,
                expected_external_account_id=expected_external_account_id,
            )
            # Preserve the public wrapper's existing post-commit ORM refresh
            # semantics; the private participant itself exposes only metadata.
            row = session.get(MarketplaceAccountCredentialRow, result.credential_id)
            session.commit()
            return _metadata(row)
        except (CredentialCryptoError, CredentialStoreError):
            session.rollback()
            raise
        except (IntegrityError, SQLAlchemyError):
            session.rollback()
            raise _translate_persistence_error() from None


def _resolve_marketplace_credential_in_session(
    session: Session,
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
    *,
    keyring: CredentialKeyring,
    now: datetime,
) -> DecryptedCredential:
    """Existing active-row verification in the caller's root, not fetch authority.

    Disconnected account status does not bypass missing/expiry/crypto validation.
    The caller supplies trusted evaluation time and owns context and final guard.
    """
    _credential_transaction_inputs(session, account_identity, kind, now)
    if now is None:
        raise CredentialStoreError("credential_contract_invalid")
    if type(keyring) is not CredentialKeyring:
        raise CredentialStoreError("credential_configuration_invalid")
    row = _active_credential_for_resolution(session, account_identity, kind)
    return _decrypt_active_credential(row, keyring=keyring, now=now)


def _active_credential_for_resolution(session, account_identity, kind):
    """Store-internal row only: one scoped lookup, no clock or crypto."""
    _account(session, account_identity, lock=False)
    row = _active_row(session, account_identity, kind, lock=False)
    if row is None:
        raise CredentialStoreError("credential_missing")
    return row


def _decrypt_active_credential(row, *, keyring, now):
    """Store-internal shared expiry/decrypt step at explicit evaluation time."""
    expires_at = _as_utc(row.expires_at)
    if expires_at is not None and expires_at <= now:
        raise CredentialStoreError("credential_expired")
    return decrypt_credential(_identity(row), _encrypted(row), keyring)


def resolve_marketplace_credential(
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
) -> DecryptedCredential:
    _validate_owner(account_identity)
    _validate_kind(account_identity, kind)
    keyring = _load_keyring()
    with get_session_factory()() as session:
        try:
            set_tenant_context(session, account_identity.organization_id)
            row = _active_credential_for_resolution(session, account_identity, kind)
            # Preserve legacy timing: read active row BEFORE sampling expiry time.
            return _decrypt_active_credential(row, keyring=keyring, now=_utc_now())
        except (CredentialCryptoError, CredentialStoreError):
            raise
        except SQLAlchemyError:
            raise _translate_persistence_error() from None


def _validate_fetch(account_identity, kind):
    if (
        not isinstance(account_identity, MarketplaceAccountCredentialOwner)
        or not isinstance(account_identity.provider, str)
        or not isinstance(kind, str)
    ):
        raise CredentialStoreError("credential_contract_invalid")
    _validate_owner(account_identity)
    _validate_kind(account_identity, kind)


def _resolve_fetch_in_session(session, account_identity, kind, keyring):
    """One paired query/crypto implementation; caller owns context and root."""
    account = MarketplaceAccountRow
    credential = MarketplaceAccountCredentialRow
    query = (
        select(account.external_account_id, account.credential_ref, credential)
        .outerjoin(
            credential,
            (credential.organization_id == account.organization_id)
            & (credential.marketplace_account_id == account.marketplace_account_id)
            & (credential.provider == account.marketplace)
            & (credential.credential_kind == kind)
            & credential.revoked_at.is_(None),
        )
        .where(
            account.organization_id == account_identity.organization_id,
            account.marketplace_account_id == account_identity.marketplace_account_id,
            account.marketplace == account_identity.provider,
            account.status == "connected",
        )
    )
    selected = session.execute(query).one_or_none()
    if selected is None:
        raise CredentialStoreError("credential_account_not_found")
    external_account_id, credential_ref, row = selected
    if row is None:
        raise CredentialStoreError("credential_missing")
    identity = _identity(row)
    if identity.expires_at is not None and identity.expires_at <= _utc_now():
        raise CredentialStoreError("credential_expired")
    secret = decrypt_credential(identity, _encrypted(row), keyring)
    return ResolvedCredentialForFetch(
        secret=secret,
        binding=CredentialFetchBinding(
            owner=account_identity,
            external_account_id=external_account_id,
            credential_ref=credential_ref,
            credential_identity=identity,
        ),
    )


def resolve_marketplace_credential_for_fetch(
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
) -> ResolvedCredentialForFetch:
    """Capture account and credential in one snapshot; close before provider I/O.

    Existing public path retains its configured pool/key loader and SQLite
    compatibility. Dedicated executors use the explicit factory below instead.
    """
    _validate_fetch(account_identity, kind)
    keyring = _load_keyring()
    with get_session_factory()() as session:
        try:
            set_tenant_context(session, account_identity.organization_id)
            return _resolve_fetch_in_session(session, account_identity, kind, keyring)
        except (CredentialCryptoError, CredentialStoreError):
            raise
        except SQLAlchemyError:
            raise _translate_persistence_error() from None


class _ExecutorCredentialResolver:
    __slots__ = ("_factory", "_keyring_loader", "_identity")

    def __init__(self, *, session_factory, keyring_loader, identity):
        if (not callable(session_factory) or not callable(keyring_loader)
                or type(identity) is not ExecutorRoleIdentity):
            raise CredentialStoreError("credential_configuration_invalid")
        self._factory = session_factory
        self._keyring_loader = keyring_loader
        self._identity = identity

    def __repr__(self):
        return "<ExecutorCredentialResolver redacted>"

    __str__ = __repr__

    def __copy__(self):
        raise TypeError("credential_contract_invalid")

    def __deepcopy__(self, memo):
        raise TypeError("credential_contract_invalid")

    def __reduce__(self):
        raise TypeError("credential_contract_invalid")

    def __reduce_ex__(self, protocol):
        raise TypeError("credential_contract_invalid")

    def __call__(self, account_identity: MarketplaceAccountCredentialOwner,
                 kind: str) -> ResolvedCredentialForFetch:
        _validate_fetch(account_identity, kind)
        try:
            session = self._factory()
        except Exception:
            raise _translate_persistence_error() from None
        # Do not touch ownership/lifecycle of a foreign or joined root. Engine
        # inspection does not open a connection; rejected factories own cleanup.
        try:
            valid = (isinstance(session, Session) and session.is_active
                     and not session.in_transaction() and not session.in_nested_transaction()
                     and not session.new and not session.dirty and not session.deleted
                     and isinstance(session.get_bind(), Engine)
                     and session.get_bind().dialect.name == "postgresql")
        except Exception:
            valid = False
        if not valid:
            raise CredentialStoreError("credential_contract_invalid")
        try:
            session.begin()
            connection = _physical_connection(session)
            root, physical_root = session.get_transaction(), connection.get_transaction()

            def verify_same_root():
                if (session.get_transaction() is not root or not root.is_active
                        or session.new or session.dirty or session.deleted
                        or _physical_connection(session) is not connection
                        or connection.get_transaction() is not physical_root):
                    raise CredentialStoreError("credential_contract_invalid")
                verify_executor_login(session, identity=self._identity)

            verify_same_root()  # Concrete physical-login check BEFORE key/ciphertext.
            try:
                keyring = self._keyring_loader()
                if type(keyring) is not CredentialKeyring:
                    raise CredentialStoreError("credential_configuration_invalid")
            except Exception:
                raise CredentialStoreError("credential_configuration_invalid") from None
            verify_same_root()
            set_marketplace_account_context(
                session, organization_id=account_identity.organization_id,
                marketplace_account_id=account_identity.marketplace_account_id,
            )
            with session.no_autoflush:
                result = _resolve_fetch_in_session(session, account_identity, kind, keyring)
            verify_same_root()
        except ExecutorIdentityDenied:
            raise CredentialStoreError("credential_configuration_invalid") from None
        except (CredentialCryptoError, CredentialStoreError):
            raise
        except Exception:
            raise _translate_persistence_error() from None
        finally:
            # No writes/commit, no read-closure "commit ambiguity". Both cleanup
            # operations are attempted; any failure prevents returning authority.
            cleanup_failed = False
            try:
                session.rollback()
            except Exception:
                cleanup_failed = True
            try:
                session.close()
            except Exception:
                cleanup_failed = True
            if cleanup_failed:
                raise _translate_persistence_error() from None
        return result


def make_executor_credential_resolver(*, session_factory, keyring_loader, identity):
    """Bind explicit trusted bootstrap dependencies, without DB/key I/O.

    This verifies pool/crypto identity, NOT job/user/domain authorization. Never
    register this as a fallback for an unresolved job credential generation.
    """
    return _ExecutorCredentialResolver(
        session_factory=session_factory, keyring_loader=keyring_loader, identity=identity,
    )


def _get_marketplace_credential_metadata_in_session(
    session: Session,
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
) -> CredentialMetadata | None:
    """Existing scoped status read; no authentication, session or transaction owner."""
    _credential_transaction_inputs(session, account_identity, kind)
    _account(session, account_identity, lock=False)
    row = _latest_row(session, account_identity, kind)
    return _metadata(row) if row is not None else None


def get_marketplace_credential_metadata(
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
) -> CredentialMetadata | None:
    _validate_owner(account_identity)
    _validate_kind(account_identity, kind)
    with get_session_factory()() as session:
        try:
            set_tenant_context(session, account_identity.organization_id)
            return _get_marketplace_credential_metadata_in_session(session, account_identity, kind)
        except CredentialStoreError:
            raise
        except SQLAlchemyError:
            raise _translate_persistence_error() from None


def _revoke_marketplace_credential_in_session(
    session: Session,
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
    reason_code: str,
    *,
    now: datetime,
    actor_user_id: str | None = None,
) -> CredentialMetadata:
    """Existing account-before-row revoke; metadata provisional until caller commit."""
    _credential_transaction_inputs(session, account_identity, kind, now)
    if now is None:
        raise CredentialStoreError("credential_contract_invalid")
    if reason_code not in _REVOCATION_REASONS:
        raise CredentialStoreError("credential_reason_invalid")
    _account(session, account_identity, lock=True)
    row = _active_row(session, account_identity, kind, lock=True)
    if row is None:
        raise CredentialStoreError("credential_missing")
    row.revoked_at = now
    row.revocation_reason_code = reason_code
    row.updated_at = now
    session.flush()
    _audit(session, row, operation="revoke", actor_user_id=actor_user_id)
    return _metadata(row)


def revoke_marketplace_credential(
    account_identity: MarketplaceAccountCredentialOwner,
    kind: str,
    reason_code: str,
    *,
    actor_user_id: str | None = None,
) -> CredentialMetadata:
    _validate_owner(account_identity)
    _validate_kind(account_identity, kind)
    if reason_code not in _REVOCATION_REASONS:
        raise CredentialStoreError("credential_reason_invalid")
    now = _utc_now()
    with get_session_factory()() as session:
        try:
            set_tenant_context(session, account_identity.organization_id)
            result = _revoke_marketplace_credential_in_session(
                session, account_identity, kind, reason_code, now=now,
                actor_user_id=actor_user_id,
            )
            row = session.get(MarketplaceAccountCredentialRow, result.credential_id)
            session.commit()
            return _metadata(row)
        except CredentialStoreError:
            session.rollback()
            raise
        except (IntegrityError, SQLAlchemyError):
            session.rollback()
            raise _translate_persistence_error() from None


def reencrypt_credential(
    credential_id: UUID,
    expected_generation: int,
    target_key_version: int,
    *,
    account_identity: MarketplaceAccountCredentialOwner | None = None,
) -> CredentialMetadata:
    """Owned transaction for trusted administration; account scope is not authentication."""
    if (
        not isinstance(account_identity, MarketplaceAccountCredentialOwner)
        or type(account_identity.organization_id) is not int
        or not 1 <= account_identity.organization_id <= 2147483647
        or type(account_identity.marketplace_account_id) is not int
        or not 1 <= account_identity.marketplace_account_id <= 2147483647
        or type(account_identity.provider) is not str
        or account_identity.provider not in {"wb", "avito"}
        or not isinstance(credential_id, UUID)
        or not isinstance(expected_generation, int)
        or isinstance(expected_generation, bool)
        or not 1 <= expected_generation < 9223372036854775807
        or not isinstance(target_key_version, int)
        or isinstance(target_key_version, bool)
        or not 1 <= target_key_version <= 2147483647
    ):
        raise CredentialStoreError("credential_contract_invalid")
    keyring = _load_keyring()
    target_key = keyring.key_for(target_key_version)
    target_keyring = CredentialKeyring(
        current_key_version=target_key_version,
        keys={target_key_version: target_key},
    )
    try:
        with get_session_factory()() as session, session.begin():
            bind = session.get_bind()
            if bind.dialect.name == "postgresql":
                if not isinstance(bind, Engine):
                    raise CredentialStoreError("credential_configuration_invalid")
                connection = session.connection()
                if (
                    getattr(connection.connection.dbapi_connection, "autocommit", None) is not False
                    or connection.get_isolation_level() != "READ COMMITTED"
                ):
                    raise CredentialStoreError("credential_configuration_invalid")
            set_tenant_context(session, account_identity.organization_id)
            _account(session, account_identity, lock=True)
            row = session.scalar(
                select(MarketplaceAccountCredentialRow)
                .where(
                    MarketplaceAccountCredentialRow.organization_id == account_identity.organization_id,
                    MarketplaceAccountCredentialRow.marketplace_account_id == account_identity.marketplace_account_id,
                    MarketplaceAccountCredentialRow.provider == account_identity.provider,
                    MarketplaceAccountCredentialRow.credential_id == credential_id,
                    MarketplaceAccountCredentialRow.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                raise CredentialStoreError("credential_missing")
            if int(row.generation) != expected_generation:
                raise CredentialStoreError("credential_concurrent_update")
            now = _utc_now()
            expires_at = _as_utc(row.expires_at)
            if expires_at is not None and expires_at <= now:
                raise CredentialStoreError("credential_expired")
            old_payload = decrypt_credential(_identity(row), _encrypted(row), keyring).reveal()
            new_identity = CredentialIdentity(
                organization_id=int(row.organization_id),
                marketplace_account_id=int(row.marketplace_account_id),
                provider=row.provider,
                credential_kind=row.credential_kind,
                payload_schema_version=int(row.payload_schema_version),
                credential_id=row.credential_id,
                generation=expected_generation + 1,
                expires_at=_as_utc(row.expires_at),
            )
            encrypted = encrypt_credential(new_identity, old_payload, target_keyring)
            verified = decrypt_credential(new_identity, encrypted, target_keyring).reveal()
            if verified != old_payload:
                raise CredentialCryptoError("credential_auth_failed")
            result = session.execute(
                update(MarketplaceAccountCredentialRow)
                .where(
                    MarketplaceAccountCredentialRow.organization_id == account_identity.organization_id,
                    MarketplaceAccountCredentialRow.marketplace_account_id == account_identity.marketplace_account_id,
                    MarketplaceAccountCredentialRow.provider == account_identity.provider,
                    MarketplaceAccountCredentialRow.credential_id == credential_id,
                    MarketplaceAccountCredentialRow.generation == expected_generation,
                    MarketplaceAccountCredentialRow.revoked_at.is_(None),
                )
                .values(
                    algorithm=encrypted.algorithm,
                    key_version=encrypted.key_version,
                    aad_version=encrypted.aad_version,
                    nonce=encrypted.nonce,
                    ciphertext=encrypted.ciphertext,
                    generation=new_identity.generation,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if int(result.rowcount or 0) != 1:
                raise CredentialStoreError("credential_concurrent_update")
            session.expire(row)
            session.refresh(row)
            _audit(session, row, operation="reencrypt")
            metadata = _metadata(row)
        return metadata
    except (IntegrityError, SQLAlchemyError):
        raise _translate_persistence_error() from None
