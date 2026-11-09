"""Verified seller discovery and encrypted account creation in one PG commit."""
from uuid import UUID
from sqlalchemy import func, select, text
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.credential_store import MarketplaceAccountCredentialOwner, _put_marketplace_credential_in_session
from app.platform.integrations.wb_credentials import fetch_wb_seller_id
from app.wb_live.auth import require_live_actor
from app.wb_live.contracts import WbLiveError
from app.wb_live.repository import WbLiveRepository, _safe

def account_view(row):
    return {"marketplaceAccountId": row.marketplace_account_id, "provider": row.marketplace,
        "externalAccountId": row.external_account_id, "displayName": row.display_name,
        "status": row.status}

class WbAccountConnection:
    def __init__(self, *, session_factory, keyring_loader, seller_verifier=fetch_wb_seller_id):
        self._repo = WbLiveRepository(session_factory, keyring_loader)
        self._verify = seller_verifier

    @_safe
    def list_accounts(self, actor):
        with self._repo._session() as s:
            _, membership = require_live_actor(s, actor, permission="cabinet:read")
            ids = membership.allowed_account_ids
            rows = s.execute(text("SELECT marketplace_account_id,marketplace,external_account_id,display_name,status "
                "FROM marketplace_accounts WHERE organization_id=:org AND marketplace='wb' ORDER BY marketplace_account_id"),
                {"org": actor.organization_id})
            return [account_view(r) for r in rows if membership.scope_mode == "all" or str(r.marketplace_account_id) in {str(v) for v in ids}]

    @_safe
    def connect(self, actor, *, token, display_name=None):
        if type(token) is not str or not 8 <= len(token.encode("utf-8")) <= 8192 or any(c.isspace() for c in token):
            raise WbLiveError("WB_RESPONSE_INVALID")
        if display_name is not None and (type(display_name) is not str or not 1 <= len(display_name.strip()) <= 128):
            raise WbLiveError("WB_RESPONSE_INVALID")
        with self._repo._session() as s:
            _, m = require_live_actor(s, actor)
            if m.scope_mode != "all":
                raise WbLiveError("WB_ACCESS_DENIED")
        keyring = self._repo._keyring()
        # All HTTP happens outside any database transaction; no plaintext persists.
        try:
            seller_id = str(UUID(self._verify(token)))
            if UUID(seller_id).version != 4:
                raise ValueError()
        except Exception:
            raise WbLiveError("WB_ACCESS_DENIED") from None
        with self._repo._session() as s:
            principal, m = require_live_actor(s, actor)
            if m.scope_mode != "all":
                raise WbLiveError("WB_ACCESS_DENIED")
            # Serializes only this verified org/seller creation, never provider HTTP.
            s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity,0))"),
                {"identity": f"wb-live:{actor.organization_id}:{seller_id}"})
            a = s.scalar(select(MarketplaceAccountRow).where(MarketplaceAccountRow.organization_id == actor.organization_id,
                MarketplaceAccountRow.marketplace == "wb", MarketplaceAccountRow.external_account_id == seller_id).with_for_update())
            if a is None:
                a = MarketplaceAccountRow(organization_id=actor.organization_id, marketplace="wb", external_account_id=seller_id,
                    status="connected", ingestion_binding_version=1)
                s.add(a)
                s.flush()
            else:
                a.status = "connected"
                s.flush()
            if display_name is not None:
                s.execute(text("UPDATE marketplace_accounts SET display_name=:name WHERE organization_id=:org AND marketplace_account_id=:account"),
                    {"name": display_name.strip(), "org": actor.organization_id, "account": a.marketplace_account_id})
            metadata = _put_marketplace_credential_in_session(s,
                MarketplaceAccountCredentialOwner(actor.organization_id, a.marketplace_account_id, "wb"), "wb_api", {"token": token},
                keyring=keyring, now=s.scalar(select(func.clock_timestamp())), actor_user_id=actor.user_id,
                expected_external_account_id=seller_id)
            s.flush()
            # Recheck actual membership and login expiry immediately before commit.
            require_live_actor(s, actor, account_id=a.marketplace_account_id)
            row = s.execute(text("SELECT marketplace_account_id,marketplace,external_account_id,display_name,status FROM marketplace_accounts "
                "WHERE organization_id=:org AND marketplace_account_id=:account"),
                {"org": actor.organization_id, "account": a.marketplace_account_id}).one()
            return account_view(row), metadata
