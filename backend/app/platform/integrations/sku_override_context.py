"""Fresh self/account metadata for the SKU override HTTP adapter.

This committed snapshot is not an authority token. SkuOverrideService must still
revalidate the actual session, permissions, binding and SKU mapping in its own
transaction. No credential payload, token, generic engine or fallback is used.
"""
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.infra.db import set_tenant_context
from app.modules.wb_repricing_override_service import OverridePersistenceError
from app.modules.wb_repricing_overrides import OverrideCommandValidationError
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
    acquire_publication_guard,
)


def _identifier(value):
    return type(value) is int and 0 < value < 2**31


def _identity_text(value):
    return (type(value) is str and 0 < len(value) <= 64 and bool(value.strip())
            and "\x00" not in value and not any(0xD800 <= ord(char) <= 0xDFFF for char in value))


class SkuOverrideContextResolver:
    def __init__(self, *, engine):
        if not isinstance(engine, Engine) or engine.dialect.name != "postgresql":
            raise OverridePersistenceError()
        self._engine = engine

    def __call__(self, actor, account_id):
        if not _identifier(account_id):
            raise OverrideCommandValidationError()
        if (type(actor) is not ActorContext or not _identifier(actor.organization_id)
                or not _identity_text(actor.user_id) or not _identity_text(actor.session_id)):
            raise PublicationGuardError("publication_access_denied")
        session, result, code = None, None, None
        try:
            session = Session(self._engine, autoflush=False)
            session.begin()
            set_tenant_context(session, actor.organization_id)
            member = session.scalar(select(IamMembershipRow.membership_id).where(
                IamMembershipRow.organization_id == actor.organization_id,
                IamMembershipRow.user_id == actor.user_id, IamMembershipRow.is_active.is_(True)))
            account = session.execute(select(MarketplaceAccountRow.external_account_id,
                MarketplaceAccountRow.credential_ref).where(
                    MarketplaceAccountRow.organization_id == actor.organization_id,
                    MarketplaceAccountRow.marketplace_account_id == account_id,
                    MarketplaceAccountRow.marketplace == "wb")).one_or_none()
            if member is None or account is None:
                raise PublicationGuardError("publication_access_denied")
            principal = UserSessionPrincipal(actor.organization_id, actor.user_id, member, actor.session_id)
            binding = ExpectedAccountBinding(account_id, "wb", account.external_account_id, account.credential_ref)
            guard = acquire_publication_guard(session, principal=principal,
                required_permissions=frozenset({"settings:read"}), accounts=(binding,), authorities=())
            guard.revalidate_before_write()
            session.commit()  # Existing physical-root listener performs the closing live check.
            result = principal, binding
        except PublicationGuardError as error:
            code = "denied" if error.code in {"publication_access_denied", "publication_expired",
                                              "publication_binding_changed"} else "unavailable"
        except Exception:  # noqa: BLE001 - never expose SQL/driver/configuration diagnostics.
            code = "unavailable"
        finally:
            if session is not None:
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001 - still close after cleanup failure.
                    code = "unavailable"
                try:
                    session.close()
                except Exception:  # noqa: BLE001 - do not return an unverified snapshot.
                    code = "unavailable"
        if code == "denied":
            raise PublicationGuardError("publication_access_denied")
        if code is not None:
            raise OverridePersistenceError()
        return result
