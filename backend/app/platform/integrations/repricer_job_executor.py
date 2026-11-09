"""Dormant repricer root owners and exact action handles. IMPLEMENTED / UNVERIFIED.

Trusted bootstrap supplies factories, role identity, policy and the existing paired
credential resolver. T2 supplies its unchanged domain participant in these physical
roots. No provider, queue handler, scheduler or API registration is installed.
"""
from __future__ import annotations

from contextlib import contextmanager
from weakref import ref

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkSessionRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import MarketplaceAccountCredentialOwner, ResolvedCredentialForFetch
from app.platform.integrations.orm import MarketplaceAccountCredentialRow, MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding, ExpectedCredential, PublicationGuardError, UserSessionPrincipal,
    _physical_connection, _register_repricer_fence, _install_repricer_closing_guard, acquire_publication_guard,
)
from app.platform.integrations import repricer_job_store as db
from app.platform.integrations.repricer_job_contract import (
    _COMMITTED, ClosingAction, CommittedRepricerDispatch, InitiationAction, Redacted,
    RepricerApprovalBinding, RepricerAuthorityPolicy, RepricerExpectedState, RepricerJobError,
    RepricerJobLocator, RepricerReadbackRequired, RepricerUploadObservation, timestamp, version,
)
from app.platform.integrations.worker_identity import ExecutorIdentityDenied, ExecutorRoleIdentity, verify_executor_login


def _rollback(session):
    try:
        session.rollback()
    except Exception:
        pass


@contextmanager
def _root(factory, *, locator=None, approval=None):
    try:
        session = factory()
    except Exception:
        raise RepricerJobError("REPRICER_PERSISTENCE_FAILED") from None
    if (not isinstance(session, Session) or session.in_transaction() or not session.is_active
            or session.new or session.dirty or session.deleted):
        # Do not commit, close or roll back a foreign root returned by a bad factory.
        raise RepricerJobError("REPRICER_FENCE_INVALID")
    try:
        session.begin()
        if _physical_connection(session).get_isolation_level() != "READ COMMITTED":
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        yield session
        try:
            session.commit()
        except (RepricerJobError, PublicationGuardError, ExecutorIdentityDenied):
            raise
        except Exception:
            raise RepricerReadbackRequired(locator, approval) from None
    except (PublicationGuardError, ExecutorIdentityDenied):
        _rollback(session)
        raise RepricerJobError("REPRICER_AUTHORITY_DENIED") from None
    except RepricerJobError:
        _rollback(session)
        raise
    except Exception:
        _rollback(session)
        raise RepricerJobError("REPRICER_PERSISTENCE_FAILED") from None
    finally:
        try:
            session.close()
        except Exception:
            pass


def _actor_principal(session, actor, organization_id):
    if type(actor) is not ActorContext or actor.organization_id != organization_id or actor.session_id is None:
        raise RepricerJobError("REPRICER_ACCESS_DENIED")
    membership = session.scalar(select(IamMembershipRow.membership_id).where(
        IamMembershipRow.organization_id == organization_id, IamMembershipRow.user_id == actor.user_id))
    if membership is None:
        raise RepricerJobError("REPRICER_ACCESS_DENIED")
    return UserSessionPrincipal(organization_id,actor.user_id,membership,actor.session_id)


def _metadata(session, binding):
    a, c = MarketplaceAccountRow, MarketplaceAccountCredentialRow
    account = session.execute(select(a.external_account_id,a.credential_ref).where(
        a.organization_id == binding.organization_id,a.marketplace_account_id == binding.marketplace_account_id,
        a.marketplace == "wb",a.status == "connected")).one_or_none()
    credential = session.execute(select(c.credential_id,c.generation,c.payload_schema_version,c.expires_at).where(
        c.organization_id == binding.organization_id,c.marketplace_account_id == binding.marketplace_account_id,
        c.provider == "wb",c.credential_kind == "wb_api",c.revoked_at.is_(None))).one_or_none()
    if account is None or credential is None:
        raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
    return (ExpectedAccountBinding(binding.marketplace_account_id,"wb",*account),
            ExpectedCredential(binding.marketplace_account_id,credential.credential_id,"wb_api",
                credential.generation,credential.payload_schema_version,credential.expires_at))


def _live_guard(session, principal, account, credential):
    return acquire_publication_guard(session,principal=principal,required_permissions=frozenset({"price:send"}),
        accounts=(account,),authorities=(credential,))


def _state(snapshot, a, t, expected, *, marker=False):
    if type(expected) is not RepricerExpectedState:
        raise RepricerJobError("REPRICER_CONTRACT_INVALID")
    expected.__post_init__()
    if expected.approval != snapshot.binding or db.expected_state(snapshot.binding,a,t) != expected:
        raise RepricerJobError("REPRICER_CONFLICT")
    member = snapshot.row["initiator_membership_id"]
    if a["claimed_by_membership_id"] not in (None,member):
        raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
    if t is not None:
        if ((t["action_key"],t["request_checksum"],t["claimed_by_membership_id"],a["claimed_by_membership_id"])
                != (snapshot.binding.action_key,snapshot.binding.request_checksum,member,member)):
            raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
    if marker and (t is None or t["dispatch_at"] is None or t["dispatched_audit_id"] is None or t["version"] < 1):
        raise RepricerJobError("REPRICER_FENCE_INVALID")


def _locked(session, snapshot):
    current = db.read_job(session,snapshot.locator)
    if current != snapshot:
        raise RepricerJobError("REPRICER_CONFLICT")
    a = db.approval(session,snapshot.binding)
    t = db.attempt(session,snapshot.binding)
    return a,t


def _initiation_precondition(action, a, t):
    if action is InitiationAction.CLAIM:
        valid = a["status"] == "pending" and t is None and a["claimed_by_membership_id"] is None
    elif action is InitiationAction.RESERVE:
        valid = a["status"] == "applying" and t is None
    elif action in (InitiationAction.DISPATCH,InitiationAction.FETCH):
        valid = a["status"] == "applying" and t is not None and t["status"] == "reserved" and t["version"] == 0 and t["dispatch_at"] is None
    elif action is InitiationAction.BEFORE_PROVIDER_IO:
        valid = a["status"] == "applying" and t is not None and t["status"] == "dispatched" and t["version"] == 1 and t["dispatch_at"] is not None
    else:
        valid = False
    if not valid:
        raise RepricerJobError("REPRICER_CONFLICT")


class RepricerInitiationHandle(Redacted):
    """Exact root-bound initiation participation, never transferable send authority."""
    __slots__ = ("_session_ref","_guard","_snapshot","_action","_expected","_a","_t",
                 "_policy","_identity","_sealed","_creation")

    def __init__(self, session, guard, snapshot, action, expected, a, t, *, policy, identity=None, creation=False):
        self._session_ref, self._guard, self._snapshot = ref(session),guard,snapshot
        self._action,self._expected,self._a,self._t = action,expected,a,t
        self._policy,self._identity,self._sealed,self._creation = policy,identity,False,creation
        _register_repricer_fence(guard,self)

    @property
    def action(self):
        self._guard._context()
        return self._action

    @property
    def expected(self):
        self._guard._context()
        return self._expected

    @property
    def initiator_membership_id(self):
        self._guard._context()
        return self._snapshot.row["initiator_membership_id"]

    def require_participation(self, session, action):
        if session is not self._session_ref() or action is not self._action or self._sealed or self._creation:
            self._guard._failed = True
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        self.revalidate_before_write()

    def revalidate_before_write(self):
        self._guard.revalidate_before_write()
        return self._validate_final()

    def _validate_final(self):
        try:
            session = self._guard._context()
            if session is not self._session_ref():
                raise RepricerJobError("REPRICER_FENCE_INVALID")
            if self._identity is not None:
                verify_executor_login(session,identity=self._identity)
            if session.scalar(text("SELECT current_setting('app.marketplace_account_id',true)")) != str(self._snapshot.locator.marketplace_account_id):
                raise RepricerJobError("REPRICER_FENCE_INVALID")
            a,t = _locked(session,self._snapshot)
            if a != self._a or t != self._t or self._snapshot.policy != self._policy:
                raise RepricerJobError("REPRICER_FENCE_INVALID")
            if self._snapshot.authority["authority_expires_at"] <= db.now(session):
                raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
            if self._guard._finalizing and not self._sealed:
                raise RepricerJobError("REPRICER_FENCE_INVALID")
            return db.now(session)
        except Exception:
            self._guard._failed = True
            raise

    def _seal(self):
        """Inspect T2's persisted result; T1 never copies a domain transition."""
        session = self._guard._context()
        session.flush()
        a,t = _locked(session,self._snapshot)
        old_a,old_t = self._a,self._t
        if self._creation or self._action in (InitiationAction.FETCH,InitiationAction.BEFORE_PROVIDER_IO):
            valid = a == old_a and t == old_t
        elif self._action is InitiationAction.CLAIM:
            valid = (a["status"] == "applying" and version(a["version"]) == version(old_a["version"])+1
                and a["claimed_by_membership_id"] == self.initiator_membership_id and t is None)
        elif self._action is InitiationAction.RESERVE:
            valid = (a == old_a and t is not None and t["status"] == "reserved" and t["version"] == 0
                and t["claim_version"] == a["version"] and t["dispatch_at"] is None)
        elif self._action is InitiationAction.DISPATCH:
            valid = (a == old_a and t is not None and t["attempt_id"] == old_t["attempt_id"]
                and t["dispatch_key"] == old_t["dispatch_key"] and t["claim_version"] == old_t["claim_version"]
                and t["status"] == "dispatched" and t["version"] == 1 and t["dispatch_at"] is not None
                and t["dispatched_audit_id"] is not None)
        else:
            valid = False
        if not valid:
            self._guard._failed = True
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        _state(self._snapshot,a,t,db.expected_state(self._snapshot.binding,a,t))
        self._a,self._t,self._sealed = a,t,True
        self.revalidate_before_write()


class RepricerClosingHandle(Redacted):
    """Separate exact root guard: local evidence/outcomes only, no fetch or initiation."""
    __slots__ = ("_session_ref","_transaction","_snapshot","_identity","_action","_expected",
                 "_a","_t","_failed","_ended","_finalizing","_orders_fences","_repricer_fences","_sealed")

    def __init__(self, session, snapshot, identity, action, expected, a, t):
        self._session_ref,self._transaction = ref(session),session.get_transaction()
        self._snapshot,self._identity,self._action,self._expected = snapshot,identity,action,expected
        self._a,self._t = a,t
        self._failed,self._ended,self._finalizing,self._sealed = False,False,False,False
        self._orders_fences,self._repricer_fences = (),()
        _install_repricer_closing_guard(session,self)

    @property
    def action(self):
        self._context()
        return self._action

    @property
    def expected(self):
        self._context()
        return self._expected

    def require_participation(self, session, action):
        if session is not self._session_ref() or action is not ClosingAction.OUTCOME or action is not self._action or self._sealed:
            self._failed = True
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        self.revalidate_before_write()

    def _context(self):
        session = self._session_ref()
        if (session is None or self._failed or self._ended or not session.is_active
                or session.get_transaction() is not self._transaction or not self._transaction.is_active
                or session.in_nested_transaction() or getattr(session,"_satorna_publication_guard",None) is not self):
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        _physical_connection(session)
        marker = (self._transaction,self._snapshot.locator.organization_id)
        if session.info.get("satorna_tenant_context") != marker:
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        actual = session.execute(text("""SELECT current_setting('app.organization_id',true),
            current_setting('app.marketplace_account_id',true),current_setting('transaction_isolation')""")).one()
        if tuple(actual) != (str(self._snapshot.locator.organization_id),str(self._snapshot.locator.marketplace_account_id),"read committed"):
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        return session

    def revalidate_before_write(self):
        try:
            session = self._context()
            verify_executor_login(session,identity=self._identity)
            a,t = _locked(session,self._snapshot)
            if a != self._a or t != self._t or (self._finalizing and not self._sealed):
                raise RepricerJobError("REPRICER_FENCE_INVALID")
            _state(self._snapshot,a,t,db.expected_state(self._snapshot.binding,a,t),
                marker=self._action is ClosingAction.RECEIPT)
            return db.now(session)
        except Exception:
            self._failed = True
            raise

    def _seal(self):
        session = self._context()
        session.flush()
        a,t = _locked(session,self._snapshot)
        if self._action is ClosingAction.OUTCOME:
            old = self._t
            valid = (old is not None and old["status"] in {"reserved","dispatched"} and t is not None
                and t["attempt_id"] == old["attempt_id"] and t["dispatch_key"] == old["dispatch_key"]
                and t["dispatch_at"] == old["dispatch_at"] and t["dispatched_audit_id"] == old["dispatched_audit_id"]
                and t["claim_version"] == old["claim_version"] and t["version"] == old["version"]+1
                and a["status"] == t["status"] and a["status"] in {"applied","failed","ambiguous"}
                and version(a["version"]) == version(self._a["version"])+1
                and (old["dispatch_at"] is not None or t["status"] == "failed"))
        else:
            valid = a == self._a and t == self._t
        if not valid:
            self._failed = True
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        self._a,self._t,self._sealed = a,t,True
        self.revalidate_before_write()


class RepricerJobCommands(Redacted):
    """Authenticated user creation/readback; no executor connection or receipt INSERT."""
    def __init__(self, *, session_factory):
        if not callable(session_factory):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        self._factory = session_factory

    def create(self, *, authenticated_actor, approval, policy, authority_expires_at):
        if type(approval) is not RepricerApprovalBinding or type(policy) is not RepricerAuthorityPolicy:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        approval.__post_init__()
        policy.__post_init__()
        expires = timestamp(authority_expires_at)
        with _root(self._factory,approval=approval) as session:
            db.scope(session,approval.organization_id,approval.marketplace_account_id)
            principal = _actor_principal(session,authenticated_actor,approval.organization_id)
            account,credential = _metadata(session,approval)
            guard = _live_guard(session,principal,account,credential)
            existing = db.find_by_approval(session,approval)
            if existing is not None:
                if (existing.binding != approval or existing.principal != principal or existing.account != account
                        or existing.credential != credential or existing.policy != policy
                        or existing.authority["authority_expires_at"] != expires):
                    raise RepricerJobError("REPRICER_CONFLICT")
                # Historical replay precedes pending/deadline-new-row checks. It
                # returns original evidence only; live guard still fences commit.
                result = existing.view()
            else:
                a = db.approval(session,approval)
                if a["status"] != "pending":
                    raise RepricerJobError("REPRICER_CONFLICT")
                login_expiry = session.scalar(select(LkSessionRow.expires_at).where(LkSessionRow.session_id == principal.session_id))
                if login_expiry is None or expires > login_expiry or expires <= db.now(session):
                    raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
                snapshot = db.create_job(session,approval,principal,account,credential,policy,expires)
                handle = RepricerInitiationHandle(session,guard,snapshot,None,db.expected_state(approval,a,None),a,None,
                    policy=policy,creation=True)
                handle._seal()
                result = snapshot.view()
        return result

    def readback(self, *, authenticated_actor, approval):
        if type(approval) is not RepricerApprovalBinding:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        with _root(self._factory,approval=approval) as session:
            db.scope(session,approval.organization_id,approval.marketplace_account_id)
            principal = _actor_principal(session,authenticated_actor,approval.organization_id)
            account,credential = _metadata(session,approval)
            _live_guard(session,principal,account,credential)
            snapshot = db.find_by_approval(session,approval)
            if snapshot is None:
                raise RepricerJobError("REPRICER_NOT_FOUND")
            if snapshot.principal != principal or snapshot.binding != approval:
                raise RepricerJobError("REPRICER_ACCESS_DENIED")
            result = snapshot.view()
        return result


class RepricerJobExecutor(Redacted):
    def __init__(self, *, executor_session_factory, identity, policy, credential_resolver):
        """All dependencies are explicit server-bootstrap inputs, never queue fields.

        credential_resolver must be the existing paired credential store composed
        with the dedicated executor factory/keyring. This wrapper cannot attest
        the resolver's separately opened internal connection; activation must.
        """
        if (not callable(executor_session_factory) or type(identity) is not ExecutorRoleIdentity
                or type(policy) is not RepricerAuthorityPolicy or not callable(credential_resolver)):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        policy.__post_init__()
        self._factory,self._identity,self._policy,self._resolver = executor_session_factory,identity,policy,credential_resolver

    def _initiate(self, session, locator, expected, action):
        if type(locator) is not RepricerJobLocator:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        locator.__post_init__()
        verify_executor_login(session,identity=self._identity)
        db.scope(session,locator.organization_id,locator.marketplace_account_id)
        snapshot = db.read_job(session,locator)  # nonlocking immutable origin locator
        if snapshot.policy != self._policy:
            raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
        guard = _live_guard(session,snapshot.principal,snapshot.account,snapshot.credential)
        a,t = _locked(session,snapshot)  # account is already locked by live guard
        _state(snapshot,a,t,expected,marker=action is InitiationAction.BEFORE_PROVIDER_IO)
        _initiation_precondition(action,a,t)
        handle = RepricerInitiationHandle(session,guard,snapshot,action,expected,a,t,policy=self._policy,identity=self._identity)
        handle.revalidate_before_write()
        return handle

    def _participate(self, locator, expected, action, participant, resolved=None):
        if not callable(participant):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        with _root(self._factory,locator=locator) as session:
            handle = self._initiate(session,locator,expected,action)
            if action is InitiationAction.DISPATCH:
                self._paired(handle._snapshot,resolved)
            try:
                participant(session,handle)
                handle._seal()
                result = db.readback(session,handle._snapshot,handle._a,handle._t)
            except Exception:
                handle._guard._failed = True
                raise
        if action is InitiationAction.DISPATCH:
            return CommittedRepricerDispatch(locator,result.expected,_COMMITTED)
        return result

    def claim(self, *, locator, expected, participant):
        return self._participate(locator,expected,InitiationAction.CLAIM,participant)

    def reserve(self, *, locator, expected, participant):
        return self._participate(locator,expected,InitiationAction.RESERVE,participant)

    def mark_dispatch(self, *, locator, expected, resolved_credential, participant):
        """Mint an in-process marker result only after the actual root commits."""
        return self._participate(locator,expected,InitiationAction.DISPATCH,participant,resolved_credential)

    def _paired(self, snapshot, resolved):
        if type(resolved) is not ResolvedCredentialForFetch:
            raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
        expected_account,expected = snapshot.account,snapshot.credential
        binding,c = resolved.binding,resolved.binding.credential_identity
        owner = MarketplaceAccountCredentialOwner(snapshot.locator.organization_id,snapshot.locator.marketplace_account_id,"wb")
        if (binding.owner != owner or binding.external_account_id != expected_account.external_account_id
                or binding.credential_ref != expected_account.credential_ref
                or (c.organization_id,c.marketplace_account_id,c.provider,c.credential_kind,c.credential_id,
                    c.generation,c.payload_schema_version,c.expires_at)
                != (owner.organization_id,owner.marketplace_account_id,"wb","wb_api",expected.credential_id,
                    expected.generation,expected.payload_schema_version,expected.expires_at)):
            raise RepricerJobError("REPRICER_AUTHORITY_DENIED")
        return owner

    def resolve_fetch(self, *, locator, expected):
        """Valid reserved initiation only. Existing store resolves outside locks.

        Returns the paired redacted wrapper, never plaintext in result transport.
        This observation does not authorize provider I/O; mark + final guard follow.
        """
        with _root(self._factory,locator=locator) as session:
            handle = self._initiate(session,locator,expected,InitiationAction.FETCH)
            handle._seal()
            snapshot = handle._snapshot
        try:
            owner = MarketplaceAccountCredentialOwner(locator.organization_id,locator.marketplace_account_id,"wb")
            resolved = self._resolver(owner,"wb_api")
            self._paired(snapshot,resolved)
            return resolved
        except Exception:
            raise RepricerJobError("REPRICER_AUTHORITY_DENIED") from None

    def before_provider_io(self, *, dispatch, resolved_credential):
        """Fresh final check immediately before trusted T2 POST, once per marker.

        Does not perform I/O or return a reusable authorization. Distributed
        revocation between this physical commit and POST remains unavoidable.
        Any error, lost result or crash consumes this in-process opportunity;
        recover with readback/ambiguity, never recreate it from the stored marker.
        """
        if type(dispatch) is not CommittedRepricerDispatch:
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        dispatch._consume()
        with _root(self._factory,locator=dispatch.locator) as session:
            handle = self._initiate(session,dispatch.locator,dispatch.expected,InitiationAction.BEFORE_PROVIDER_IO)
            self._paired(handle._snapshot,resolved_credential)
            handle._seal()

    def _closing(self, session, locator, expected, action):
        if type(locator) is not RepricerJobLocator:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        locator.__post_init__()
        verify_executor_login(session,identity=self._identity)
        db.scope(session,locator.organization_id,locator.marketplace_account_id)
        db.account_lock(session,locator)
        snapshot = db.read_job(session,locator)
        a,t = _locked(session,snapshot)
        if action is ClosingAction.READBACK:
            expected = db.expected_state(snapshot.binding,a,t)
        _state(snapshot,a,t,expected,marker=action is ClosingAction.RECEIPT)
        if action is ClosingAction.OUTCOME and (a["status"] != "applying" or t is None or t["status"] not in {"reserved","dispatched"}):
            raise RepricerJobError("REPRICER_CONFLICT")
        return RepricerClosingHandle(session,snapshot,self._identity,action,expected,a,t)

    def close_outcome(self, *, locator, expected, participant):
        """Trusted T2 existing terminal outcome only; never synthesizes rejection."""
        if not callable(participant):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        with _root(self._factory,locator=locator) as session:
            handle = self._closing(session,locator,expected,ClosingAction.OUTCOME)
            try:
                participant(session,handle)
                handle._seal()
                result = db.readback(session,handle._snapshot,handle._a,handle._t)
            except Exception:
                handle._failed = True
                raise
        return result

    def publish_receipt(self, *, locator, expected, observation):
        """Clean NEW physical root observes the already committed dispatch marker.

        Same evidence replays; different ID conflicts. There is no participant or
        approval/attempt/cache mutation in this transaction. Clock domains remain
        separate; a late receipt may be attached to terminal ambiguous evidence.
        """
        if type(observation) is not RepricerUploadObservation:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        observation.__post_init__()
        with _root(self._factory,locator=locator) as session:
            handle = self._closing(session,locator,expected,ClosingAction.RECEIPT)
            result = db.insert_receipt(session,handle._snapshot,handle._t,observation)
            handle._seal()
        return result

    def readback(self, *, locator):
        """Durable scoped state/receipt after uncertainty. Never mints a send handle."""
        with _root(self._factory,locator=locator) as session:
            handle = self._closing(session,locator,None,ClosingAction.READBACK)
            result = db.readback(session,handle._snapshot,handle._a,handle._t)
            handle._seal()
        return result
