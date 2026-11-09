"""Token-only authorization in the durable sink's physical root; no user principal.

The sink begins a fresh Session root, acquires this handle before domain locks,
requires participation on that exact Session, writes and commits. The existing
shared final listener flushes first and then rechecks this token/account/DB clock.
There is deliberately no registry for domain classes that do not exist yet.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.infra.db import MarketplaceAccountContextError, set_marketplace_account_context
from app.platform.integrations.credential_store import MarketplaceAccountCredentialOwner
from app.platform.integrations.ingestion_tokens import (
    INGESTION_TOKEN_SCOPE, IngestionTokenStoreError, _database_now,
    _verify_ingestion_token_in_session,
)
from app.platform.integrations.orm import MarketplaceAccountIngestionTokenRow, MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding, PublicationGuardError, _STATE, _initialize_publication_root,
    _install_listeners, _publication_root_context, _require_clean_publication_root,
    _revalidate_publication_root,
)


class IngestionPublicationGuard:
    """Safe metadata only. Neither bearer nor verifier is retained in this handle."""

    def __init__(self, session: Session):
        _initialize_publication_root(self, session)
        self._verified = None

    def __repr__(self):
        return "<IngestionPublicationGuard>"

    @property
    def owner(self) -> MarketplaceAccountCredentialOwner:
        return self._verified.owner

    @property
    def account_binding(self) -> ExpectedAccountBinding:
        binding = self._verified.account_binding
        return ExpectedAccountBinding(binding.marketplace_account_id, binding.provider,
                                      binding.external_account_id, binding.credential_ref)

    @property
    def binding_version(self) -> int:
        return self._verified.account_binding.binding_version

    @property
    def token_id(self) -> UUID:
        return self._verified.token_id

    @property
    def expires_at(self) -> datetime:
        return self._verified.expires_at

    def _context(self):
        if self._verified is None:
            raise PublicationGuardError("publication_authority_invalid")
        session = _publication_root_context(self, self.owner.organization_id)
        expected = (self._transaction, self.owner.organization_id, self.owner.marketplace_account_id)
        if session.info.get("satorna_marketplace_account_context") != expected:
            raise PublicationGuardError("publication_context_invalid")
        actual = session.scalar(text("SELECT current_setting('app.marketplace_account_id', true)"))
        if actual != str(self.owner.marketplace_account_id):
            raise PublicationGuardError("publication_context_invalid")
        return session

    def require_participation(self, session: Session) -> IngestionPublicationGuard:
        """Admit a domain participant to this exact root; grants no extra scope.

        The participant may neither commit early, open another Session, install a
        second guard nor use this handle as authorization after its root ends.
        """
        try:
            if self._session_ref() is not session:
                raise PublicationGuardError("publication_context_invalid")
            self.revalidate_before_write()
            return self
        except Exception:
            self._failed = True
            raise

    def revalidate_before_write(self) -> datetime:
        return _revalidate_publication_root(self)

    def _validate(self):
        session = self._context()
        owner, binding = self.owner, self._verified.account_binding
        a = MarketplaceAccountRow
        account = session.execute(select(a.external_account_id, a.credential_ref, a.status,
            a.ingestion_binding_version).where(a.organization_id == owner.organization_id,
                a.marketplace_account_id == owner.marketplace_account_id, a.marketplace == "avito"
            ).with_for_update()).one_or_none()
        if account is None or tuple(account) != (
                binding.external_account_id, binding.credential_ref, "connected", binding.binding_version):
            raise PublicationGuardError("publication_binding_changed")
        t = MarketplaceAccountIngestionTokenRow
        token = session.execute(select(t.organization_id, t.marketplace_account_id, t.provider, t.scope,
            t.expires_at, t.revoked_at, t.binding_schema_version, t.binding_external_account_id,
            t.binding_credential_ref, t.binding_version).where(t.organization_id == owner.organization_id,
                t.token_id == self.token_id).with_for_update()).one_or_none()
        if token is None or tuple(token) != (
                owner.organization_id, owner.marketplace_account_id, "avito", INGESTION_TOKEN_SCOPE,
                self.expires_at, None, 1, binding.external_account_id, binding.credential_ref, binding.binding_version):
            raise PublicationGuardError("publication_authority_invalid")
        now = _database_now(session)
        if self.expires_at <= now:
            raise PublicationGuardError("publication_expired")
        return now


def acquire_ingestion_publication_guard(session: Session, *, raw_bearer: str) -> IngestionPublicationGuard:
    """Verify in an existing clean PostgreSQL READ COMMITTED physical root.

    No owner, account or permissions come from the caller. The tenant locator is
    used only to restrict RLS lookup. Account context follows successful verification.
    Every failed acquisition poisons the installed root and requires rollback.
    """
    guard = None
    try:
        _require_clean_publication_root(session)
        guard = IngestionPublicationGuard(session)
        _install_listeners(session)
        setattr(session, _STATE, guard)
        with session.no_autoflush:
            guard._verified = _verify_ingestion_token_in_session(session, raw_bearer=raw_bearer)
            set_marketplace_account_context(session, organization_id=guard.owner.organization_id,
                                            marketplace_account_id=guard.owner.marketplace_account_id)
            now = guard.revalidate_before_write()
            session.execute(text("UPDATE marketplace_account_ingestion_tokens SET last_used_at=:now "
                                 "WHERE organization_id=:org AND token_id=:token"),
                            {"now": now, "org": guard.owner.organization_id, "token": guard.token_id})
        return guard
    except (IngestionTokenStoreError, MarketplaceAccountContextError):
        if guard is not None:
            guard._failed = True
        raise PublicationGuardError("publication_authority_invalid") from None
    except SQLAlchemyError:
        if guard is not None:
            guard._failed = True
        raise PublicationGuardError("publication_persistence_failed") from None
    except Exception:
        if guard is not None:
            guard._failed = True
        raise
    finally:
        raw_bearer = None
