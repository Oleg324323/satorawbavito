"""Live user/session authorization held through a caller-owned root transaction.

Trusted services construct the principal and fixed permissions; these values are
not authentication tokens. Every authority used for fetch must be supplied by the
service. Empty authorities mean a credential-independent operation. The guard
cannot infer an unreported fetch. Acquire before domain locks and perform all
provider I/O before acquiring. Callers roll back every failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from weakref import ref

from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkSessionRow, LkUserRow
from app.cabinet.permissions import (
    PRODUCTION_PERMISSION_KEYS,
    PRODUCTION_READ_PERMISSIONS,
    permissions_from_profile,
)
from app.infra.db import set_tenant_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountIngestionTokenRow,
    MarketplaceAccountRow,
)

_CODES = frozenset({
    "publication_context_invalid", "publication_access_denied", "publication_binding_changed",
    "publication_authority_invalid", "publication_expired", "publication_persistence_failed",
})
_KINDS = {"wb_api": "wb", "avito_oauth_client": "avito", "avito_oauth_access": "avito"}
_STATE = "_satorna_publication_guard"


class PublicationGuardError(ValueError):
    """Only an allowlisted code is exposed, including for malformed error inputs."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _CODES else "publication_persistence_failed"
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"PublicationGuardError(code={self.code!r})"


def _integer(value: object, maximum: int = 2**31 - 1) -> bool:
    return type(value) is int and 0 < value <= maximum


def _text(value: object, limit: int) -> bool:
    return (type(value) is str and 0 < len(value) <= limit and bool(value.strip())
            and "\x00" not in value and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def _aware(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True, slots=True, repr=False)
class UserSessionPrincipal:
    organization_id: int
    user_id: str
    membership_id: int
    session_id: str

    def __post_init__(self) -> None:
        if not (_integer(self.organization_id) and _integer(self.membership_id)
                and _text(self.user_id, 64) and _text(self.session_id, 64)):
            raise PublicationGuardError("publication_context_invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ExpectedAccountBinding:
    marketplace_account_id: int
    provider: str
    external_account_id: str
    credential_ref: str | None

    def __post_init__(self) -> None:
        if not (_integer(self.marketplace_account_id) and type(self.provider) is str
                and self.provider in {"wb", "avito"} and _text(self.external_account_id, 128)
                and (self.credential_ref is None or _text(self.credential_ref, 255))):
            raise PublicationGuardError("publication_binding_changed")


@dataclass(frozen=True, slots=True, repr=False)
class ExpectedCredential:
    marketplace_account_id: int
    credential_id: UUID
    kind: str
    generation: int
    payload_schema_version: int
    expires_at: datetime | None

    def __post_init__(self) -> None:
        if not (_integer(self.marketplace_account_id) and type(self.credential_id) is UUID
                and type(self.kind) is str and self.kind in _KINDS
                and _integer(self.generation, 2**63 - 1)
                and type(self.payload_schema_version) is int and self.payload_schema_version == 1
                and (_aware(self.expires_at) if self.kind == "avito_oauth_access" else self.expires_at is None)):
            raise PublicationGuardError("publication_authority_invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ExpectedIngestionToken:
    marketplace_account_id: int
    token_id: UUID
    scope: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not (_integer(self.marketplace_account_id) and type(self.token_id) is UUID
                and type(self.scope) is str and self.scope == "avito.browser_snapshot.write"
                and _aware(self.expires_at)):
            raise PublicationGuardError("publication_authority_invalid")


def _contracts(principal, required_permissions, accounts, authorities):
    if type(principal) is not UserSessionPrincipal:
        raise PublicationGuardError("publication_context_invalid")
    principal.__post_init__()
    if (type(required_permissions) is not frozenset or not required_permissions
            or not all(_text(p, 64) for p in required_permissions)):
        raise PublicationGuardError("publication_context_invalid")
    if (required_permissions & (PRODUCTION_PERMISSION_KEYS - PRODUCTION_READ_PERMISSIONS)
            and not PRODUCTION_READ_PERMISSIONS <= required_permissions):
        raise PublicationGuardError("publication_context_invalid")
    self_preferences = required_permissions in (
        frozenset({"preferences:read"}), frozenset({"preferences:write"}))
    if (type(accounts) not in (tuple, list)
            or (not accounts and not (self_preferences and type(authorities) in (tuple, list) and not authorities))):
        raise PublicationGuardError("publication_binding_changed")
    account_map = {}
    for account in accounts:
        if type(account) is not ExpectedAccountBinding:
            raise PublicationGuardError("publication_binding_changed")
        account.__post_init__()
        if account.marketplace_account_id in account_map:
            raise PublicationGuardError("publication_binding_changed")
        account_map[account.marketplace_account_id] = account
    if type(authorities) not in (tuple, list):
        raise PublicationGuardError("publication_authority_invalid")
    seen, ids = set(), set()
    for authority in authorities:
        if type(authority) not in (ExpectedCredential, ExpectedIngestionToken):
            raise PublicationGuardError("publication_authority_invalid")
        authority.__post_init__()
        account = account_map.get(authority.marketplace_account_id)
        credential = type(authority) is ExpectedCredential
        provider = _KINDS[authority.kind] if credential else "avito"
        identity = authority.credential_id if credential else authority.token_id
        key = (authority.marketplace_account_id, authority.kind if credential else identity)
        if account is None or account.provider != provider or key in seen or identity in ids:
            raise PublicationGuardError("publication_authority_invalid")
        seen.add(key)
        ids.add(identity)
    return (tuple(sorted(account_map.values(), key=lambda a: a.marketplace_account_id)),
            tuple(sorted((a for a in authorities if type(a) is ExpectedCredential),
                         key=lambda a: (a.marketplace_account_id, a.kind, a.credential_id))),
            tuple(sorted((a for a in authorities if type(a) is ExpectedIngestionToken),
                         key=lambda a: (a.marketplace_account_id, a.token_id))))


def _scope(membership, accounts, required_permissions):
    permissions, ids = membership.permissions, membership.allowed_account_ids
    if (type(permissions) is not list or not all(_text(p, 64) for p in permissions)
            or type(ids) is not list or membership.scope_mode not in {"all", "selected", "restricted"}):
        raise PublicationGuardError("publication_access_denied")
    allowed = set()
    for value in ids:
        if _integer(value):
            allowed.add(value)
        elif (type(value) is str and value.isascii() and value.isdecimal()
              and len(value.lstrip("0")) <= 10 and _integer(int(value.lstrip("0") or "0"))):
            allowed.add(int(value.lstrip("0") or "0"))
        else:
            raise PublicationGuardError("publication_access_denied")
    try:
        effective = frozenset(permissions) | permissions_from_profile(membership.role)
    except (AttributeError, TypeError, ValueError):
        raise PublicationGuardError("publication_access_denied") from None
    if not required_permissions <= effective or (membership.scope_mode != "all" and
            not {a.marketplace_account_id for a in accounts} <= allowed):
        raise PublicationGuardError("publication_access_denied")


def _physical_connection(session):
    # A joined Connection root can outlive Session.commit and its final check.
    bind = session.get_bind()
    if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
        raise PublicationGuardError("publication_context_invalid")
    connection = session.connection()
    root = connection.get_transaction()
    # Logical Session roots and READ COMMITTED do not rule out AUTOCOMMIT;
    # Connection SAVEPOINTs also bypass Session nested-transaction events.
    if (root is None or not root.is_active or connection.in_nested_transaction()
            or getattr(connection.connection.dbapi_connection, "autocommit", None) is not False):
        raise PublicationGuardError("publication_context_invalid")
    return connection


def _require_clean_publication_root(session):
    """Shared admission only; no authorization, identity or domain callbacks."""
    existing = getattr(session, _STATE, None)
    if existing is not None:
        existing._failed = True
    try:
        if (not isinstance(session, Session) or not session.in_transaction() or not session.is_active
                or session.in_nested_transaction() or session.new or session.dirty or session.deleted
                or existing is not None
                or _physical_connection(session).get_isolation_level() != "READ COMMITTED"):
            raise PublicationGuardError("publication_context_invalid")
        return
    except SQLAlchemyError:
        pass
    # Raise after leaving the handler so driver diagnostics are not retained as
    # an exception context by any caller of this shared admission boundary.
    raise PublicationGuardError("publication_persistence_failed")


def _initialize_publication_root(guard, session):
    guard._session_ref = ref(session)
    guard._transaction = session.get_transaction()
    guard._physical_root = _physical_connection(session).get_transaction()
    guard._failed = guard._ended = guard._finalizing = False
    guard._orders_fences = guard._repricer_fences = guard._review_fences = ()


def _publication_root_context(guard, organization_id):
    session = guard._session_ref()
    if (session is None or guard._ended or guard._failed or not session.is_active
            or session.get_transaction() is not guard._transaction
            or guard._transaction is None or not guard._transaction.is_active
            or session.in_nested_transaction() or getattr(session, _STATE, None) is not guard):
        raise PublicationGuardError("publication_context_invalid")
    if _physical_connection(session).get_transaction() is not guard._physical_root:
        raise PublicationGuardError("publication_context_invalid")
    if session.info.get("satorna_tenant_context") != (guard._transaction, organization_id):
        raise PublicationGuardError("publication_context_invalid")
    actual = session.execute(text(
        "SELECT current_setting('app.organization_id', true), current_setting('transaction_isolation')"
    )).one()
    if actual != (str(organization_id), "read committed"):
        raise PublicationGuardError("publication_context_invalid")
    return session


def _revalidate_publication_root(guard):
    try:
        session = guard._session_ref()
        if session is None:
            raise PublicationGuardError("publication_context_invalid")
        with session.no_autoflush:
            return guard._validate()
    except PublicationGuardError:
        guard._failed = True
        raise
    except SQLAlchemyError:
        guard._failed = True
        raise PublicationGuardError("publication_persistence_failed") from None
    except Exception:
        guard._failed = True
        raise


class PublicationGuard:
    """Transaction-bound handle; no credential payloads or independent sessions."""

    def __init__(self, session, principal, permissions, accounts, credentials, tokens):
        _initialize_publication_root(self, session)
        self._principal = principal
        self._permissions = permissions
        self._accounts, self._credentials, self._tokens = accounts, credentials, tokens
        self._failed = False
        self._ended = False
        self._orders_fences = ()
        self._repricer_fences = ()
        self._review_fences = ()
        self._review_approver = None
        self._finalizing = False

    def __repr__(self):
        return "<PublicationGuard>"

    def _context(self):
        return _publication_root_context(self, self._principal.organization_id)

    def revalidate_before_write(self) -> datetime:
        """Refresh exact locked metadata and DB time; does not flush caller writes."""
        return _revalidate_publication_root(self)

    def _validate(self):
        session = self._context()
        p = self._principal
        if self._review_approver is not None:
            self._validate_review_memberships(session)
        else:
            self._validate_membership(session)
        login = session.execute(select(LkSessionRow.session_id, LkSessionRow.user_id,
            LkSessionRow.revoked_at, LkSessionRow.expires_at).where(
                LkSessionRow.session_id == p.session_id).with_for_update(read=True)).one_or_none()
        if login is None or login.user_id != p.user_id or login.revoked_at is not None:
            raise PublicationGuardError("publication_access_denied")
        return self._validate_accounts(session, [login.expires_at])

    def _validate_membership(self, session):
        p = self._principal
        user = session.execute(select(LkUserRow.user_id, LkUserRow.organization_id, LkUserRow.is_active).where(
            LkUserRow.user_id == p.user_id).with_for_update(read=True)).one_or_none()
        if user is None or user.organization_id != p.organization_id or user.is_active is not True:
            raise PublicationGuardError("publication_access_denied")
        m = IamMembershipRow
        membership = session.execute(select(m.membership_id, m.organization_id, m.user_id, m.is_active,
            m.role, m.permissions, m.scope_mode, m.allowed_account_ids).where(
                m.membership_id == p.membership_id).with_for_update(read=True)).one_or_none()
        if (membership is None or membership.organization_id != p.organization_id
                or membership.user_id != p.user_id or membership.is_active is not True):
            raise PublicationGuardError("publication_access_denied")
        _scope(membership, self._accounts, self._permissions)

    def _validate_review_memberships(self, session):
        # Only the Review creation/execution constructor installs this exact
        # dependency. No additional principal or caller-selected permissions.
        from app.platform.integrations.review_job_store import approver

        intent, approver_member, approver_user = self._review_approver
        p = self._principal
        users = {p.user_id, approver_user}
        for user_id in sorted(users):
            u = session.execute(select(LkUserRow.organization_id, LkUserRow.is_active).where(
                LkUserRow.user_id == user_id).with_for_update(read=True)).one_or_none()
            if u is None or tuple(u) != (p.organization_id, True):
                raise PublicationGuardError("publication_access_denied")
        members = {p.membership_id: (p.user_id, frozenset({"reviews:send"}))}
        if approver_member == p.membership_id:
            if approver_user != p.user_id:
                raise PublicationGuardError("publication_access_denied")
            members[p.membership_id] = (p.user_id, frozenset({"reviews:send", "reviews:approve"}))
        else:
            members[approver_member] = (approver_user, frozenset({"reviews:approve"}))
        m = IamMembershipRow
        for member_id in sorted(members):
            user_id, permissions = members[member_id]
            membership = session.execute(select(m.membership_id, m.organization_id, m.user_id, m.is_active,
                m.role, m.permissions, m.scope_mode, m.allowed_account_ids).where(
                    m.membership_id == member_id).with_for_update(read=True)).one_or_none()
            if (membership is None or membership.organization_id != p.organization_id
                    or membership.user_id != user_id or membership.is_active is not True):
                raise PublicationGuardError("publication_access_denied")
            _scope(membership, self._accounts, permissions)
        if approver(session, intent) != (approver_member, approver_user):
            raise PublicationGuardError("publication_access_denied")

    def _validate_accounts(self, session, expiries):
        p = self._principal
        a = MarketplaceAccountRow
        locked_accounts = {}
        for expected in self._accounts:
            account = session.execute(select(a.organization_id, a.marketplace, a.external_account_id,
                a.credential_ref, a.status, a.ingestion_binding_version).where(a.marketplace_account_id == expected.marketplace_account_id
                ).with_for_update()).one_or_none()
            if account is None or tuple(account)[:5] != (p.organization_id, expected.provider,
                    expected.external_account_id, expected.credential_ref, "connected"):
                raise PublicationGuardError("publication_binding_changed")
            locked_accounts[expected.marketplace_account_id] = account
        c = MarketplaceAccountCredentialRow
        for expected in self._credentials:
            credential = session.execute(select(c.organization_id, c.marketplace_account_id, c.provider,
                c.credential_kind, c.generation, c.payload_schema_version, c.expires_at, c.revoked_at).where(
                    c.credential_id == expected.credential_id).with_for_update(read=True)).one_or_none()
            if credential is None or tuple(credential) != (p.organization_id, expected.marketplace_account_id,
                    _KINDS[expected.kind], expected.kind, expected.generation,
                    expected.payload_schema_version, expected.expires_at, None):
                raise PublicationGuardError("publication_authority_invalid")
            if expected.expires_at is not None:
                expiries.append(credential.expires_at)
        t = MarketplaceAccountIngestionTokenRow
        for expected in self._tokens:
            token = session.execute(select(t.organization_id, t.marketplace_account_id, t.provider, t.scope,
                t.expires_at, t.revoked_at, t.binding_schema_version, t.binding_external_account_id,
                t.binding_credential_ref, t.binding_version).where(t.token_id == expected.token_id).with_for_update(read=True)).one_or_none()
            if token is None or tuple(token)[:6] != (p.organization_id, expected.marketplace_account_id,
                                                 "avito", expected.scope, expected.expires_at, None):
                raise PublicationGuardError("publication_authority_invalid")
            # Legacy all-NULL is retained ONLY for the pre-rollout, independently
            # user-authenticated entry. New bound rows cannot bypass incarnation.
            binding = tuple(token)[6:]
            account = locked_accounts[expected.marketplace_account_id]
            if binding != (None, None, None, None) and binding != (
                    1, account.external_account_id, account.credential_ref, account.ingestion_binding_version):
                raise PublicationGuardError("publication_binding_changed")
            expiries.append(token.expires_at)
        now = session.scalar(select(func.clock_timestamp()))
        if any(not _aware(expiry) or expiry <= now for expiry in expiries):
            raise PublicationGuardError("publication_expired")
        return now


def _before_commit(session):
    guard = getattr(session, _STATE, None)
    if guard is None:
        return
    try:
        if guard._finalizing:
            raise PublicationGuardError("publication_context_invalid")
        guard._finalizing = True
        # Dispatch iteration is the effective class-then-instance call order,
        # not registration time. No callback may run after final validation,
        # even if its author intended a read-only observer. Inspect, never edit,
        # the collection while SQLAlchemy is dispatching it.
        if tuple(session.dispatch.before_commit)[-1:] != (_before_commit,):
            raise PublicationGuardError("publication_context_invalid")
        with session.no_autoflush:
            guard._context()
        session.flush()
        # after_flush_postexec can queue another flush. Do not let SQLAlchemy's
        # subsequent commit flush loop publish that work after our final check.
        if session.new or session.dirty or session.deleted:
            raise PublicationGuardError("publication_context_invalid")
        guard.revalidate_before_write()
        for fence in guard._orders_fences:
            fence._validate_final()
        for fence in guard._repricer_fences:
            fence._validate_final()
        for fence in getattr(guard, "_review_fences", ()):
            fence._validate_final()
        if session.new or session.dirty or session.deleted:
            raise PublicationGuardError("publication_context_invalid")
    except PublicationGuardError:
        guard._failed = True
        raise
    except SQLAlchemyError:
        guard._failed = True
        raise PublicationGuardError("publication_persistence_failed") from None
    except Exception:
        # A job fence failure poisons this root even if a caller catches it.
        guard._failed = True
        raise


def _register_user_orders_fence(guard, fence):
    """Private, exact-class Orders extension; never an arbitrary callback bus.

    Same root only, before finalization, once. The normal listener remains the
    final before_commit listener and validates these fences after flush and auth.
    """
    from app.platform.integrations.user_orders_jobs import UserOrdersPublicationHandle

    try:
        if (type(guard) is not PublicationGuard or type(fence) is not UserOrdersPublicationHandle
                or guard._finalizing or guard._orders_fences or guard._repricer_fences or guard._review_fences or fence._guard is not guard
                or fence._session_ref() is not guard._context()):
            raise PublicationGuardError("publication_context_invalid")
        guard._orders_fences = (fence,)
    except Exception:
        guard._failed = True
        raise


def _register_repricer_fence(guard, fence):
    """Private exact-class repricer extension; not a public callback registry."""
    from app.platform.integrations.repricer_job_executor import RepricerInitiationHandle

    try:
        if (type(guard) is not PublicationGuard or type(fence) is not RepricerInitiationHandle
                or guard._finalizing or guard._orders_fences or guard._repricer_fences or guard._review_fences
                or fence._guard is not guard or fence._session_ref() is not guard._context()):
            raise PublicationGuardError("publication_context_invalid")
        guard._repricer_fences = (fence,)
    except Exception:
        guard._failed = True
        raise


def _install_repricer_closing_guard(session, handle):
    """Exact closing type only. Never fabricates or weakens a user principal."""
    from app.platform.integrations.repricer_job_executor import RepricerClosingHandle

    if (type(handle) is not RepricerClosingHandle or not isinstance(session, Session)
            or not session.in_transaction() or not session.is_active or session.in_nested_transaction()
            or session.new or session.dirty or session.deleted or getattr(session, _STATE, None) is not None
            or handle._session_ref() is not session or handle._transaction is not session.get_transaction()
            or _physical_connection(session).get_isolation_level() != "READ COMMITTED"):
        raise PublicationGuardError("publication_context_invalid")
    _install_listeners(session)
    setattr(session, _STATE, handle)
    try:
        handle.revalidate_before_write()
    except Exception:
        handle._failed = True
        raise


def _install_listeners(session):
    for name, listener in (("before_commit", _before_commit),
                           ("after_transaction_create", _transaction_created),
                           ("after_transaction_end", _transaction_ended)):
        if not event.contains(session, name, listener):
            event.listen(session, name, listener)


def _install_credential_management_guard(session, guard):
    """Fixed admin root only; never relax the public connected fetch contract."""
    from app.platform.integrations.credential_management import _CredentialManagementGuard

    _require_clean_publication_root(session)
    if (type(guard) is not _CredentialManagementGuard
            or guard._session_ref() is not session
            or guard._transaction is not session.get_transaction()
            or guard._physical_root is not _physical_connection(session).get_transaction()):
        raise PublicationGuardError("publication_context_invalid")
    _install_listeners(session)
    setattr(session, _STATE, guard)
    guard.revalidate_before_write()


def _register_review_fence(guard, fence):
    from app.platform.integrations.review_job_authority import ReviewPublicationHandle

    try:
        if (type(guard) is not PublicationGuard or type(fence) is not ReviewPublicationHandle
                or guard._finalizing or guard._orders_fences or guard._repricer_fences or guard._review_fences
                or fence._guard is not guard or fence._session_ref() is not guard._context()):
            raise PublicationGuardError("publication_context_invalid")
        guard._review_fences = (fence,)
    except Exception:
        guard._failed = True
        raise


def _install_review_closing_guard(session, handle):
    from app.platform.integrations.review_job_authority import ReviewClosingHandle

    if (type(handle) is not ReviewClosingHandle or not isinstance(session, Session)
            or not session.in_transaction() or not session.is_active or session.in_nested_transaction()
            or session.new or session.dirty or session.deleted or getattr(session, _STATE, None) is not None
            or handle._session_ref() is not session or handle._transaction is not session.get_transaction()
            or _physical_connection(session).get_isolation_level() != "READ COMMITTED"):
        raise PublicationGuardError("publication_context_invalid")
    _install_listeners(session)
    setattr(session, _STATE, handle)
    try:
        handle.revalidate_before_write()
    except Exception:
        handle._failed = True
        raise


def _acquire_review_publication_guard(session, *, principal, intent, account, credential):
    """Review-only sorted sender/current-approver admission. Public API unchanged."""
    from app.platform.integrations.review_job_contract import ReviewSendIntent
    from app.platform.integrations.review_job_store import approver

    if type(intent) is not ReviewSendIntent:
        raise PublicationGuardError("publication_context_invalid")
    intent.__post_init__()
    permissions = frozenset({"reviews:send"})
    accounts, credentials, tokens = _contracts(principal, permissions, (account,), (credential,))
    if (principal.organization_id != intent.locator.organization_id
            or account.marketplace_account_id != intent.locator.marketplace_account_id
            or account.provider != intent.locator.marketplace
            or credential.kind != ("wb_api" if account.provider == "wb" else "avito_oauth_access")
            or not isinstance(session, Session) or not session.in_transaction() or not session.is_active
            or session.in_nested_transaction() or session.new or session.dirty or session.deleted
            or getattr(session, _STATE, None) is not None):
        raise PublicationGuardError("publication_context_invalid")
    with session.no_autoflush:
        if _physical_connection(session).get_isolation_level() != "READ COMMITTED":
            raise PublicationGuardError("publication_context_invalid")
        # scope() is the Review root owner's required context setup. Do not
        # overwrite foreign context before the exact guard has inspected it.
        marker = (session.get_transaction(), principal.organization_id)
        if session.info.get("satorna_tenant_context") != marker:
            raise PublicationGuardError("publication_context_invalid")
        member_id, user_id = approver(session, intent)
        guard = PublicationGuard(session, principal, permissions, accounts, credentials, tokens)
        guard._review_approver = (intent, member_id, user_id)
        _install_listeners(session)
        setattr(session, _STATE, guard)
        try:
            guard.revalidate_before_write()
        except Exception:
            guard._failed = True
            raise
        return guard


def _transaction_created(session, transaction):
    guard = getattr(session, _STATE, None)
    # Flush creates an internal, non-nested child transaction; SAVEPOINT does not
    # belong to the publication protocol and must poison the root even if caught.
    if guard is not None and transaction.nested:
        guard._failed = True
        raise PublicationGuardError("publication_context_invalid")


def _transaction_ended(session, transaction):
    guard = getattr(session, _STATE, None)
    if guard is not None and transaction is guard._transaction:
        guard._ended = True
        guard._transaction = None
        setattr(session, _STATE, None)


def acquire_publication_guard(session: Session, *, principal: UserSessionPrincipal,
                              required_permissions: frozenset[str], accounts, authorities) -> PublicationGuard:
    """Acquire one metadata fence in an existing clean PostgreSQL RC transaction."""
    accounts, credentials, tokens = _contracts(principal, required_permissions, accounts, authorities)
    _require_clean_publication_root(session)
    try:
        with session.no_autoflush:
            if _physical_connection(session).get_isolation_level() != "READ COMMITTED":
                raise PublicationGuardError("publication_context_invalid")
            transaction = session.get_transaction()
            marker = session.info.get("satorna_tenant_context")
            if marker is not None and (type(marker) is not tuple or len(marker) != 2):
                raise PublicationGuardError("publication_context_invalid")
            if marker is not None and marker[0] is transaction and marker != (transaction, principal.organization_id):
                raise PublicationGuardError("publication_context_invalid")
            current = session.scalar(text("SELECT current_setting('app.organization_id', true)"))
            if current not in (None, "", str(principal.organization_id)):
                raise PublicationGuardError("publication_context_invalid")
            set_tenant_context(session, principal.organization_id)
            guard = PublicationGuard(session, principal, required_permissions, accounts, credentials, tokens)
            # Functions capture neither this transaction nor the session. Install
            # once per Session; root completion clears state without modifying a
            # listener collection during event dispatch or affecting other sessions.
            _install_listeners(session)
            setattr(session, _STATE, guard)
            guard.revalidate_before_write()
            return guard
    except SQLAlchemyError:
        raise PublicationGuardError("publication_persistence_failed") from None
