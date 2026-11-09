"""Committed metadata-only discovery; no credential loading or action authority."""

from sqlalchemy import Engine, bindparam, event, text
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.wb_live.auth import require_live_actor
from app.wb_live.contracts import WbLiveError


class AccountDiscoveryError(ValueError):
    def __init__(self, code="ACCOUNT_DISCOVERY_UNAVAILABLE"):
        self.code = (
            code
            if code
            in {
                "ACCOUNT_DISCOVERY_UNAVAILABLE",
                "ACCOUNT_DISCOVERY_DISABLED",
                "ACCOUNT_DISCOVERY_ACCESS_DENIED",
                "ACCOUNT_DISCOVERY_INVALID_REQUEST",
            }
            else "ACCOUNT_DISCOVERY_UNAVAILABLE"
        )
        super().__init__(self.code)


def _metadata(session, actor, provider, membership):
    # require_live_actor validates ALL scope elements via the existing _scope,
    # including malformed/bool IDs. Normalize only after that validation.
    params = {"org": actor.organization_id}
    predicate = "organization_id=:org AND marketplace IN ('wb','avito')"
    if provider is not None:
        predicate += " AND marketplace=:provider"
        params["provider"] = provider
    if membership.scope_mode != "all":
        predicate += " AND marketplace_account_id IN :allowed"
        params["allowed"] = tuple(
            int(value) for value in membership.allowed_account_ids
        )
    statement = text(
        "SELECT marketplace_account_id,marketplace,external_account_id,display_name,status "
        "FROM marketplace_accounts WHERE "
        + predicate
        + " ORDER BY marketplace_account_id FOR SHARE"
    )
    if "allowed" in params:
        statement = statement.bindparams(bindparam("allowed", expanding=True))
    return tuple(tuple(row) for row in session.execute(statement, params))


class MarketplaceAccountDiscoveryService:
    def __init__(self, *, engine, enabled=False):
        if (
            not isinstance(engine, Engine)
            or engine.dialect.name != "postgresql"
            or type(enabled) is not bool
        ):
            raise AccountDiscoveryError()
        self._engine, self._enabled = engine, enabled

    def list_accounts(self, actor, *, provider=None):
        if not self._enabled:
            raise AccountDiscoveryError("ACCOUNT_DISCOVERY_DISABLED")
        if provider is not None and (
            type(provider) is not str or provider not in ("wb", "avito")
        ):
            raise AccountDiscoveryError("ACCOUNT_DISCOVERY_INVALID_REQUEST")
        if (
            type(actor) is not ActorContext
            or type(actor.organization_id) is not int
            or not 0 < actor.organization_id < 2**31
            or not actor.session_id
        ):
            raise AccountDiscoveryError("ACCOUNT_DISCOVERY_ACCESS_DENIED")
        failure = "ACCOUNT_DISCOVERY_UNAVAILABLE"
        try:
            # Owned fresh Session, no caller transaction/connection or nested root.
            with Session(self._engine) as session, session.begin():
                principal, membership = require_live_actor(
                    session, actor, permission="cabinet:read"
                )
                rows = _metadata(session, actor, provider, membership)

                def closing_check(current):
                    if tuple(current.dispatch.before_commit)[-1:] != (closing_check,):
                        raise WbLiveError("WB_ACCESS_DENIED")
                    if (
                        current.new
                        or current.dirty
                        or current.deleted
                        or current.in_nested_transaction()
                    ):
                        raise WbLiveError("WB_ACCESS_DENIED")
                    current.expire_all()
                    closing_principal, closing_membership = require_live_actor(
                        current, actor, permission="cabinet:read"
                    )
                    if (
                        closing_principal != principal
                        or _metadata(current, actor, provider, closing_membership)
                        != rows
                    ):
                        raise WbLiveError("WB_ACCESS_DENIED")
                    if tuple(current.dispatch.before_commit)[-1:] != (closing_check,):
                        raise WbLiveError("WB_ACCESS_DENIED")

                event.listen(session, "before_commit", closing_check)
                # before_commit performs final live expiry/scope recheck.
                # IAM and metadata SHARE locks survive until physical commit.
            # No result escapes before physical commit and session close.
            return [
                {
                    "marketplaceAccountId": row[0],
                    "provider": row[1],
                    "externalAccountId": row[2],
                    "displayName": row[3],
                    "status": row[4],
                }
                for row in rows
            ]
        except WbLiveError as error:
            if error.code == "WB_ACCESS_DENIED":
                failure = "ACCOUNT_DISCOVERY_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 -- discard SQL/detail and never use a fallback.
            failure = "ACCOUNT_DISCOVERY_UNAVAILABLE"
        raise AccountDiscoveryError(failure)
