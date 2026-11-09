"""Live IAM/session checks shared by account discovery, sync and reads."""
from sqlalchemy import Engine, func, select
from app.cabinet.orm import LkUserRow, LkSessionRow
from app.infra.db import set_tenant_context, set_marketplace_account_context
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    UserSessionPrincipal, ExpectedAccountBinding, ExpectedCredential, PublicationGuardError, PublicationGuard,
    _scope, acquire_publication_guard, _install_listeners, _STATE, _require_clean_publication_root,
)
from app.wb_live.contracts import WbLiveError

def _require_live_actor_records(session, actor, *, permission, account_id, database_clock):
    """Shared access policy; caller owns dialect admission and transaction locking."""
    if not actor.session_id:
        raise WbLiveError("WB_ACCESS_DENIED")
    u = session.scalar(select(LkUserRow).where(LkUserRow.user_id == actor.user_id).with_for_update(read=True))
    m = session.scalar(select(IamMembershipRow).where(
        IamMembershipRow.organization_id == actor.organization_id,
        IamMembershipRow.user_id == actor.user_id).with_for_update(read=True))
    login = session.scalar(select(LkSessionRow).where(LkSessionRow.session_id == actor.session_id).with_for_update(read=True))
    now = database_clock(session)
    if (u is None or u.organization_id != actor.organization_id or not u.is_active or m is None or not m.is_active
            or login is None or login.user_id != actor.user_id or login.revoked_at is not None or login.expires_at <= now):
        raise WbLiveError("WB_ACCESS_DENIED")
    scopes = () if account_id is None else (type("AccountScope", (), {"marketplace_account_id": account_id})(),)
    try:
        _scope(m, scopes, frozenset({permission}))
    except PublicationGuardError:
        raise WbLiveError("WB_ACCESS_DENIED") from None
    return UserSessionPrincipal(actor.organization_id, actor.user_id, m.membership_id, actor.session_id), m


def require_live_actor(session, actor, *, permission="integrations:write", account_id=None):
    """Root transaction required. Shared metadata locks survive until commit."""
    if (not isinstance(session.get_bind(), Engine) or session.get_bind().dialect.name != "postgresql"
            or not actor.session_id):
        raise WbLiveError("WB_ACCESS_DENIED")
    set_tenant_context(session, actor.organization_id)
    return _require_live_actor_records(session, actor, permission=permission, account_id=account_id,
        database_clock=lambda s: s.scalar(select(func.clock_timestamp())))

def acquire_read_context(session, actor, *, marketplace_account_id):
    """Returns a live publication guard; caller commits after bounded read."""
    principal, _ = require_live_actor(session, actor, permission="catalog:read", account_id=marketplace_account_id)
    a = session.scalar(select(MarketplaceAccountRow).where(
        MarketplaceAccountRow.organization_id == actor.organization_id,
        MarketplaceAccountRow.marketplace_account_id == marketplace_account_id,
        MarketplaceAccountRow.marketplace == "wb"))
    if a is None:
        raise WbLiveError("WB_ACCESS_DENIED")
    guard = acquire_publication_guard(session, principal=principal, required_permissions=frozenset({"catalog:read"}),
        accounts=(ExpectedAccountBinding(marketplace_account_id, "wb", a.external_account_id, a.credential_ref),), authorities=())
    set_marketplace_account_context(session, organization_id=actor.organization_id, marketplace_account_id=marketplace_account_id)
    return guard

class _WbReadJobGuard(PublicationGuard):
    """Persisted read subscription; initiation login is evidence, not a lease.

    This private composition grants only a WB live repository root. Current user,
    membership, account scope and paired credential remain locked and rechecked.
    It does not change the user/publication or external-operation guard.
    """
    def __init__(self, session, job):
        self._job_id = job.job_id
        self._binding = (job.organization_id, job.marketplace_account_id, job.credential_id,
            job.credential_generation, job.account_incarnation, job.external_account_id, job.credential_ref,
            job.user_id, job.membership_id, job.session_id)
        super().__init__(session, UserSessionPrincipal(job.organization_id, job.user_id, job.membership_id, job.session_id),
            frozenset({"integrations:write"}),
            (ExpectedAccountBinding(job.marketplace_account_id, "wb", job.external_account_id, job.credential_ref),),
            (ExpectedCredential(job.marketplace_account_id, job.credential_id, "wb_api", job.credential_generation, 1, None),), ())

    def _validate(self):
        from app.wb_live.orm import WbLiveSyncJobRow as J
        s = self._context()
        self._validate_membership(s)
        now = self._validate_accounts(s, [])
        binding = s.execute(select(J.organization_id, J.marketplace_account_id, J.credential_id,
            J.credential_generation, J.account_incarnation, J.external_account_id, J.credential_ref,
            J.user_id, J.membership_id, J.session_id).where(J.job_id == self._job_id,
                J.organization_id == self._principal.organization_id).with_for_update()).one_or_none()
        incarnation = s.scalar(select(MarketplaceAccountRow.ingestion_binding_version).where(
            MarketplaceAccountRow.organization_id == self._binding[0], MarketplaceAccountRow.marketplace_account_id == self._binding[1]))
        if binding is None or tuple(binding) != self._binding or incarnation != self._binding[4]:
            raise PublicationGuardError("publication_binding_changed")
        return now

def acquire_read_job_guard(session, job):
    _require_clean_publication_root(session)
    guard = _WbReadJobGuard(session, job)
    _install_listeners(session)
    setattr(session, _STATE, guard)
    guard.revalidate_before_write()
    return guard
