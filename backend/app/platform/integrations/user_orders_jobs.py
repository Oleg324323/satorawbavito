"""Trusted, session-bound Orders job authority. Local source, UNVERIFIED.

Composition roots supply authenticated ActorContext, finite policy, exact source
bindings, Session factory and any synthetic/domain participant. No handlers,
routers, Celery tasks, schedulers, providers or operational defaults registered.
"""
from __future__ import annotations

from contextlib import contextmanager
from weakref import ref
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkSessionRow, LkUserRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.credential_store import (
    CredentialStoreError, MarketplaceAccountCredentialOwner, ResolvedCredentialForFetch,
    resolve_marketplace_credential_for_fetch,
)
from app.platform.integrations.orm import MarketplaceAccountCredentialRow, MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding, ExpectedCredential, PublicationGuardError, UserSessionPrincipal,
    _physical_connection, _register_user_orders_fence, _scope, acquire_publication_guard,
)
from app.platform.integrations import user_orders_job_store as db
from app.platform.integrations.user_orders_job_contract import (
    _COMMITTED, ClaimedUserOrdersJob, OrdersExecutionPolicy, OrdersJobError, OrdersJobLocator,
    OrdersJobRequest, RETRY_REASONS, TERMINAL, TrustedOrdersSourceBinding, integer, timestamp, uuid4_value,
)


@contextmanager
def _root(factory):
    """Own a clean physical PostgreSQL root. Unknown commit means readback only."""
    session = factory()
    if (not isinstance(session, Session) or session.in_transaction() or not session.is_active
            or session.new or session.dirty or session.deleted):
        # Never close/rollback a foreign active transaction returned by a bad factory.
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    try:
        session.begin()
        if _physical_connection(session).get_isolation_level() != "READ COMMITTED":
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        try:
            yield session
        except SQLAlchemyError:
            raise OrdersJobError("JOB_PERSISTENCE_FAILED") from None
        try:
            session.commit()
        except SQLAlchemyError:
            # This includes uncertain physical COMMIT and conservatively handles
            # deferred constraint failures. It is never a refetch authorization.
            raise OrdersJobError("READBACK_REQUIRED") from None
    except PublicationGuardError:
        _rollback(session)
        raise OrdersJobError("JOB_AUTHORITY_DENIED") from None
    except SQLAlchemyError:
        _rollback(session)
        raise OrdersJobError("JOB_PERSISTENCE_FAILED") from None
    except Exception:
        _rollback(session)
        raise
    finally:
        try:
            session.close()
        except SQLAlchemyError:
            pass


def _rollback(session):
    # Never replace the safe readback-required outcome with a connection error.
    try:
        session.rollback()
    except SQLAlchemyError:
        pass


def _actor_principal(session, actor, organization_id):
    if type(actor) is not ActorContext or actor.organization_id != organization_id or actor.session_id is None:
        raise OrdersJobError("JOB_ACCESS_DENIED")
    membership = session.scalar(select(IamMembershipRow.membership_id).where(
        IamMembershipRow.organization_id == organization_id, IamMembershipRow.user_id == actor.user_id))
    if membership is None:
        raise OrdersJobError("JOB_ACCESS_DENIED")
    # This unlocked locator proves nothing yet: acquire_publication_guard checks
    # the exact canonical principal again, under the required lock order.
    return UserSessionPrincipal(organization_id, actor.user_id, membership, actor.session_id)


def _account_metadata(session, organization_id, account_id):
    a = MarketplaceAccountRow
    row = session.execute(select(a.marketplace, a.external_account_id, a.credential_ref).where(
        a.organization_id == organization_id, a.marketplace_account_id == account_id)).one_or_none()
    if row is None:
        raise OrdersJobError("JOB_ACCESS_DENIED")
    return ExpectedAccountBinding(account_id, row.marketplace, row.external_account_id, row.credential_ref)


def _credential_metadata(session, organization_id, account, kind):
    c = MarketplaceAccountCredentialRow
    row = session.execute(select(c.credential_id, c.generation, c.payload_schema_version, c.expires_at).where(
        c.organization_id == organization_id, c.marketplace_account_id == account.marketplace_account_id,
        c.provider == account.provider, c.credential_kind == kind, c.revoked_at.is_(None))).one_or_none()
    if row is None:
        raise OrdersJobError("JOB_AUTHORITY_DENIED")
    return ExpectedCredential(account.marketplace_account_id, row.credential_id, kind, row.generation, row.payload_schema_version, row.expires_at)


def _require_original(snapshot, principal):
    if snapshot.principal != principal:
        raise OrdersJobError("JOB_ACCESS_DENIED")


def _require_legacy_operation(snapshot):
    if snapshot.row["operation_kind"] != "orders.sync.v1":
        raise OrdersJobError("SOURCE_CONTRACT_UNAVAILABLE")


def _validate_operation_snapshot(session, snapshot):
    if snapshot.row["operation_kind"] == "orders.wb-history.project.v1":
        from app.platform.integrations.wb_history_projection_store import validate_snapshot
        validate_snapshot(session, snapshot)


def _require_claim(snapshot, attempt, claim, now, *, live_lease=True):
    if type(claim) is not ClaimedUserOrdersJob:
        raise OrdersJobError("JOB_FENCE_INVALID")
    claim.__post_init__()
    r = snapshot.row
    if (snapshot.locator != claim.locator or r["state"] != "running" or r["version"] != claim.expected_job_version
            or r["current_attempt_id"] != claim.attempt_id or attempt is None or attempt["state"] != "claimed"
            or attempt["attempt_id"] != claim.attempt_id or attempt["claimant_token"] != claim.claimant_token
            or attempt["version"] != claim.expected_attempt_version or r["authority_expires_at"] <= now
            or (live_lease and attempt["lease_expires_at"] <= now)):
        raise OrdersJobError("JOB_FENCE_INVALID")


def _committed(snapshot, attempt):
    return ClaimedUserOrdersJob(snapshot.locator, attempt["attempt_id"], attempt["claimant_token"], snapshot.row["version"],
        attempt["version"], attempt["lease_expires_at"], snapshot.row["authority_expires_at"], _COMMITTED)


class UserOrdersPublicationHandle:
    """Trusted, transaction-bound participant. Never serialize or reuse.

    Participant publishes a validated Orders snapshot/run in this same Session,
    then calls complete(result_sync_run_id=...). This handle holds all preceding
    user/account/job/attempt locks and final checks through the caller's commit.
    """
    __slots__ = ("_session_ref", "_guard", "_snapshot", "_attempt", "_consumed", "_deadline_required", "_publication",
        "_history_baseline", "_history_seal", "_history_role_identity")

    def __init__(self, session, guard, snapshot, attempt, *, publication=False, deadline_required=True, _history_role_identity=None):
        self._session_ref = ref(session)
        self._guard = guard
        self._snapshot, self._attempt = snapshot, attempt
        self._consumed = False
        self._deadline_required = deadline_required
        self._publication = publication
        self._history_baseline = self._history_seal = None
        self._history_role_identity = _history_role_identity
        _register_user_orders_fence(guard, self)

    def __repr__(self):
        return "<UserOrdersPublicationHandle redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("JOB_CONTRACT_INVALID")

    @property
    def request(self):
        self._validate_final()
        return self._snapshot.request

    @property
    def source_run_key(self):
        self._validate_final()
        _require_legacy_operation(self._snapshot)
        if self._attempt is None:
            raise OrdersJobError("JOB_FENCE_INVALID")
        return f"orders-job-v1:{self._snapshot.locator.job_id}:{self._attempt['attempt_id']}"

    @property
    def account_binding(self):
        self._validate_final()
        return self._snapshot.account

    @property
    def initiating_user_id(self):
        """Immutable audit origin only; not a worker principal or permission."""
        self._validate_final()
        return self._snapshot.row["initiator_user_id"]

    def revalidate_before_write(self):
        """Public domain-participant check after waits, without committing."""
        self._guard.revalidate_before_write()
        return self._validate_final()

    def _validate_final(self):
        try:
            session = self._guard._context()
            if session is not self._session_ref():
                raise OrdersJobError("JOB_FENCE_INVALID")
            account_context = session.scalar(text("SELECT current_setting('app.marketplace_account_id',true)"))
            if account_context != str(self._snapshot.locator.marketplace_account_id):
                raise OrdersJobError("JOB_FENCE_INVALID")
            current = db.read_job(session, self._snapshot.locator, lock=True)
            attempt = db.read_attempt(session, current)
            if not self._snapshot.same_delegation(current) or current.row != self._snapshot.row or attempt != self._attempt:
                raise OrdersJobError("JOB_FENCE_INVALID")
            _validate_operation_snapshot(session, current)
            if current.row["operation_kind"] == "orders.wb-history.project.v1" and self._guard._finalizing:
                from app.platform.integrations.wb_history_projection_role import inspect_projection_role
                inspect_projection_role(session, self._history_role_identity)
            if current.row["operation_kind"] == "orders.wb-history.project.v1" and self._publication:
                from app.platform.integrations.wb_history_projection import _validate_history_final
                _validate_history_final(self)
            now = db.now(session)  # after every potential domain/fence lock wait
            if self._deadline_required and (current.row["authority_expires_at"] <= now or
                    (attempt is not None and attempt["lease_expires_at"] <= now)):
                raise OrdersJobError("JOB_FENCE_INVALID")
            return now
        except Exception:
            self._guard._failed = True
            raise

    def complete(self, *, result_sync_run_id):
        """Seal the exact existing completed run, never manufacture source evidence."""
        if not self._publication or self._consumed or self._attempt is None:
            self._guard._failed = True
            raise OrdersJobError("JOB_FENCE_INVALID")
        try:
            _require_legacy_operation(self._snapshot)
            integer(result_sync_run_id, 2**63 - 1)
            session = self._guard._context()
            self._guard.revalidate_before_write()
            self._validate_final()
            r = self._snapshot.row
            # Domain participant owns flush, but completion also flushes its ORM
            # work before examining the authoritative run; final hook flushes again.
            session.flush()
            run = session.execute(text("""SELECT marketplace,source_kind,adapter_version,mapping_version,
                source_contract_version,source_run_key,requested_from,requested_to,state,manifest_state,
                completed_at,account_binding_schema_version,account_binding_external_account_id,
                account_binding_credential_ref FROM public.order_sync_runs
                WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run FOR UPDATE"""),
                {"org": r["organization_id"], "account": r["marketplace_account_id"], "run": result_sync_run_id}).mappings().one_or_none()
            now = self._validate_final()
            if (run is None or tuple(run[k] for k in ("marketplace", "source_kind", "adapter_version", "mapping_version", "source_contract_version"))
                    != tuple(r[k] for k in ("provider", "source_kind", "adapter_version", "mapping_version", "source_contract_version"))
                    or run["source_run_key"] != self.source_run_key or run["state"] not in {"complete", "partial"}
                    or run["completed_at"] is None or run["completed_at"] > now
                    or run["requested_from"] is not None or run["requested_to"] is not None
                    or run["account_binding_schema_version"] != 1
                    or run["account_binding_external_account_id"] != r["expected_external_account_id"]
                    or run["account_binding_credential_ref"] != r["expected_credential_ref"]
                    or (run["state"] == "complete" and run["manifest_state"] != "complete")):
                raise OrdersJobError("JOB_CONFLICT")
            self._snapshot, self._attempt = db.transition(session, self._snapshot, self._attempt,
                event="job.succeeded", state="succeeded", attempt_state="succeeded", at=now,
                result_sync_run_id=result_sync_run_id, result_coverage_state=run["state"])
            self._consumed = True
            # Expected successful own transition remains fenced until final commit.
            self._validate_final()
            return db.view(self._snapshot)
        except Exception:
            self._guard._failed = True
            raise


class UserOrdersJobs:
    """Trusted application composition; constructor inputs are never request fields.

    Empty trusted_sources leaves creation/claim/fetch/publication unavailable.
    Supplying reviewed selector bindings still does not register a provider handler.
    Session factory must create a fresh, Engine-bound PostgreSQL Session each time.
    """
    def __init__(self, *, session_factory, trusted_sources=(), credential_resolver=resolve_marketplace_credential_for_fetch, _history_role_identity=None):
        if not callable(session_factory) or not callable(credential_resolver) or type(trusted_sources) is not tuple:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        for source in trusted_sources:
            if type(source) is not TrustedOrdersSourceBinding:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
            source.__post_init__()
        if len(set(trusted_sources)) != len(trusted_sources):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        self._factory, self._sources, self._resolver = session_factory, trusted_sources, credential_resolver
        self._history_role_identity = _history_role_identity

    def _make_handle(self, session, guard, snapshot, attempt, **options):
        return UserOrdersPublicationHandle(session, guard, snapshot, attempt,
            _history_role_identity=self._history_role_identity, **options)

    def _source(self, binding):
        if binding not in self._sources:
            raise OrdersJobError("SOURCE_CONTRACT_UNAVAILABLE")

    def _worker(self, session, locator, *, permission="sync:run", source_required=True):
        if type(locator) is not OrdersJobLocator:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        locator.__post_init__()
        db.scope(session, locator.organization_id, locator.marketplace_account_id)
        original = db.read_job(session, locator)
        if original.row["operation_kind"] == "orders.wb-history.project.v1":
            from app.platform.integrations.wb_history_projection_role import inspect_projection_role
            inspect_projection_role(session, self._history_role_identity)
        if source_required:
            self._source(original.binding)
        guard = acquire_publication_guard(session, principal=original.principal, required_permissions=frozenset({permission}),
            accounts=(original.account,), authorities=(original.credential,))
        locked = db.read_job(session, locator, lock=True)
        if not original.same_delegation(locked):
            raise OrdersJobError("JOB_FENCE_INVALID")
        attempt = db.read_attempt(session, locked)
        _validate_operation_snapshot(session, locked)
        return guard, locked, attempt, db.now(session)

    def create(self, *, authenticated_actor, request, idempotency_key, execution_policy, authority_expires_at):
        if type(request) is not OrdersJobRequest or type(execution_policy) is not OrdersExecutionPolicy:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        request.__post_init__()
        if request.binding.source_contract_version == "wb-history-positive-partial-v1":
            raise OrdersJobError("SOURCE_CONTRACT_UNAVAILABLE")
        execution_policy.__post_init__()
        uuid4_value(idempotency_key)
        deadline = timestamp(authority_expires_at)
        self._source(request.binding)
        with _root(self._factory) as session:
            db.scope(session, request.organization_id, request.marketplace_account_id)
            principal = _actor_principal(session, authenticated_actor, request.organization_id)
            account = _account_metadata(session, request.organization_id, request.marketplace_account_id)
            if account.provider != request.binding.provider:
                raise OrdersJobError("JOB_ACCESS_DENIED")
            credential = _credential_metadata(session, request.organization_id, account, request.binding.credential_kind)
            guard = acquire_publication_guard(session, principal=principal, required_permissions=frozenset({"sync:run"}),
                accounts=(account,), authorities=(credential,))
            existing = db.find_idempotency(session, organization_id=request.organization_id,
                marketplace_account_id=request.marketplace_account_id, membership_id=principal.membership_id,
                idempotency_key=idempotency_key)
            if existing is not None:
                snapshot = db.read_job(session, OrdersJobLocator(request.organization_id, request.marketplace_account_id, existing), lock=True)
                _require_original(snapshot, principal)
                if (snapshot.request.canonical_bytes != request.canonical_bytes or snapshot.policy != execution_policy
                        or snapshot.account != account or snapshot.credential != credential or snapshot.row["authority_expires_at"] != deadline):
                    raise OrdersJobError("JOB_CONFLICT")
                result = db.view(snapshot)
            else:
                now = guard.revalidate_before_write()
                session_expiry = session.scalar(select(LkSessionRow.expires_at).where(LkSessionRow.session_id == principal.session_id))
                if deadline <= now or deadline > session_expiry or (credential.expires_at is not None and deadline > credential.expires_at) or execution_policy.lease_end(now) > deadline:
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
                snapshot = db.create(session, request=request, policy=execution_policy, principal=principal, account=account,
                    credential=credential, idempotency_key=idempotency_key, deadline=deadline, created_at=now, job_id=uuid4())
                self._make_handle(session, guard, snapshot, None)
                result = db.view(snapshot)
        return result

    def status(self, *, authenticated_actor, locator):
        with _root(self._factory) as session:
            if self._history_role_identity is not None:
                from app.platform.integrations.wb_history_projection_role import inspect_projection_role
                inspect_projection_role(session, self._history_role_identity)
            db.scope(session, locator.organization_id, locator.marketplace_account_id)
            principal = _actor_principal(session, authenticated_actor, locator.organization_id)
            account = _account_metadata(session, locator.organization_id, locator.marketplace_account_id)
            guard = acquire_publication_guard(session, principal=principal, required_permissions=frozenset({"cabinet:read"}), accounts=(account,), authorities=())
            snapshot = db.read_job(session, locator)
            _require_original(snapshot, principal)
            if snapshot.row["operation_kind"] == "orders.wb-history.project.v1":
                from app.platform.integrations.wb_history_projection_role import inspect_projection_role
                inspect_projection_role(session, self._history_role_identity)
                snapshot = db.read_job(session, locator, lock=True)
                _require_original(snapshot, principal)
                self._make_handle(session, guard, snapshot, db.read_attempt(session, snapshot), deadline_required=False)
            result = db.view(snapshot)
        return result

    def replay(self, *, authenticated_actor, request, idempotency_key, execution_policy, authority_expires_at):
        """Authorized exact original command read; no attempt or delivery side effect."""
        if type(request) is not OrdersJobRequest or type(execution_policy) is not OrdersExecutionPolicy:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        request.__post_init__()
        if request.binding.source_contract_version == "wb-history-positive-partial-v1":
            raise OrdersJobError("SOURCE_CONTRACT_UNAVAILABLE")
        execution_policy.__post_init__()
        uuid4_value(idempotency_key)
        deadline = timestamp(authority_expires_at)
        with _root(self._factory) as session:
            db.scope(session, request.organization_id, request.marketplace_account_id)
            principal = _actor_principal(session, authenticated_actor, request.organization_id)
            account = _account_metadata(session, request.organization_id, request.marketplace_account_id)
            acquire_publication_guard(session, principal=principal, required_permissions=frozenset({"cabinet:read"}), accounts=(account,), authorities=())
            job_id = db.find_idempotency(session, organization_id=request.organization_id, marketplace_account_id=request.marketplace_account_id,
                membership_id=principal.membership_id, idempotency_key=idempotency_key)
            if job_id is None:
                raise OrdersJobError("JOB_NOT_FOUND")
            snapshot = db.read_job(session, OrdersJobLocator(request.organization_id, request.marketplace_account_id, job_id))
            _require_original(snapshot, principal)
            if (snapshot.request.canonical_bytes != request.canonical_bytes or snapshot.policy != execution_policy
                    or snapshot.account != account or snapshot.row["authority_expires_at"] != deadline):
                raise OrdersJobError("JOB_CONFLICT")
            result = db.view(snapshot)
        return result

    def cancel(self, *, authenticated_actor, locator, revoke=False):
        if type(revoke) is not bool:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        with _root(self._factory) as session:
            db.scope(session, locator.organization_id, locator.marketplace_account_id)
            principal = _actor_principal(session, authenticated_actor, locator.organization_id)
            account = _account_metadata(session, locator.organization_id, locator.marketplace_account_id)
            guard = acquire_publication_guard(session, principal=principal, required_permissions=frozenset({"cabinet:read"}), accounts=(account,), authorities=())
            snapshot = db.read_job(session, locator, lock=True)
            _require_original(snapshot, principal)
            history_operation = snapshot.row["operation_kind"] == "orders.wb-history.project.v1"
            if history_operation:
                from app.platform.integrations.wb_history_projection_role import inspect_projection_role
                inspect_projection_role(session, self._history_role_identity)
            attempt = db.read_attempt(session, snapshot)
            if snapshot.row["state"] not in TERMINAL:
                state, reason = ("revoked", "AUTHORITY_REVOKED") if revoke else ("cancelled", "USER_CANCELLED")
                snapshot, attempt = db.transition(session, snapshot, attempt, event="job." + state, state=state,
                    attempt_state=state if attempt is not None else None, at=db.now(session), reason=reason, membership_id=principal.membership_id)
                self._make_handle(session, guard, snapshot, attempt, deadline_required=False)
            elif history_operation:
                self._make_handle(session, guard, snapshot, attempt, deadline_required=False)
            result = db.view(snapshot)
        return result

    def claim(self, *, organization_id, marketplace_account_id, job_id):
        """Exactly the three trusted queue locator fields; duplicates never refetch."""
        locator = OrdersJobLocator(organization_id, marketplace_account_id, job_id)
        committed = None
        with _root(self._factory) as session:
            guard, snapshot, attempt, now = self._worker(session, locator)
            r = snapshot.row
            if r["state"] != "queued":
                if r["operation_kind"] == "orders.wb-history.project.v1":
                    self._make_handle(session, guard, snapshot, attempt, deadline_required=False)
                result = db.view(snapshot)
            elif r["next_attempt_at"] > now:
                if r["operation_kind"] == "orders.wb-history.project.v1":
                    self._make_handle(session, guard, snapshot, attempt, deadline_required=False)
                result = db.view(snapshot)
            elif r["attempt_count"] >= r["max_attempts"]:
                raise OrdersJobError("JOB_NOT_CLAIMABLE")
            elif snapshot.policy.lease_end(now) > r["authority_expires_at"]:
                snapshot, attempt = db.transition(session, snapshot, None, event="job.expired", state="expired", at=now, reason="AUTHORITY_EXPIRED")
                self._make_handle(session, guard, snapshot, attempt, deadline_required=False)
                result = db.view(snapshot)
            else:
                snapshot, attempt = db.transition(session, snapshot, None, event="job.claimed", state="running", at=now,
                    lease_expires_at=snapshot.policy.lease_end(now))
                self._make_handle(session, guard, snapshot, attempt)
                committed = snapshot, attempt
        # Physical root committed and closed before the usable claim is minted.
        return _committed(*committed) if committed is not None else result

    def renew(self, *, claim):
        committed = None
        with _root(self._factory) as session:
            guard, snapshot, attempt, now = self._worker(session, claim.locator)
            _require_claim(snapshot, attempt, claim, now)
            end = snapshot.policy.lease_end(now)
            if end <= attempt["lease_expires_at"]:
                raise OrdersJobError("JOB_FENCE_INVALID")
            if end > snapshot.row["authority_expires_at"]:
                snapshot, attempt = db.transition(session, snapshot, attempt, event="job.expired", state="expired",
                    attempt_state="expired", at=now, reason="AUTHORITY_EXPIRED")
                self._make_handle(session, guard, snapshot, attempt, deadline_required=False)
                result = db.view(snapshot)
            else:
                snapshot, attempt = db.transition(session, snapshot, attempt, event="job.lease_renewed", state="running",
                    attempt_state="claimed", at=now, lease_expires_at=end)
                self._make_handle(session, guard, snapshot, attempt)
                committed = snapshot, attempt
        return _committed(*committed) if committed is not None else result

    def fail_read(self, *, claim, safe_reason):
        """After complete publication rollback only. Never accepts raw exceptions."""
        if type(safe_reason) is not str or safe_reason not in RETRY_REASONS | {"PERMANENT_SOURCE_FAILURE"}:
            safe_reason = "PERMANENT_SOURCE_FAILURE"
        with _root(self._factory) as session:
            guard, snapshot, attempt, now = self._worker(session, claim.locator)
            _require_claim(snapshot, attempt, claim, now)
            snapshot, attempt = self._end_attempt(session, snapshot, attempt, now, reason=safe_reason, abandoned=False)
            self._make_handle(session, guard, snapshot, db.read_attempt(session, snapshot), deadline_required=False)
            result = db.view(snapshot)
        return result

    def reclaim(self, *, organization_id, marketplace_account_id, job_id, expected_job_version, expected_attempt_version):
        """Trusted recovery CAS of expired lease; this never performs a source read."""
        integer(expected_job_version, 2**63 - 1)
        integer(expected_attempt_version, 2**63 - 1)
        locator = OrdersJobLocator(organization_id, marketplace_account_id, job_id)
        with _root(self._factory) as session:
            guard, snapshot, attempt, now = self._worker(session, locator)
            if (snapshot.row["state"] != "running" or snapshot.row["version"] != expected_job_version or attempt is None
                    or attempt["version"] != expected_attempt_version or attempt["state"] != "claimed" or attempt["lease_expires_at"] > now):
                raise OrdersJobError("JOB_FENCE_INVALID")
            snapshot, attempt = self._end_attempt(session, snapshot, attempt, now, reason="LEASE_EXPIRED", abandoned=True)
            self._make_handle(session, guard, snapshot, db.read_attempt(session, snapshot), deadline_required=False)
            result = db.view(snapshot)
        return result

    def _end_attempt(self, session, snapshot, attempt, now, *, reason, abandoned):
        r = snapshot.row
        state, event, next_due = "failed", "job.failed", None
        attempt_state = "abandoned" if abandoned else "failed"
        if r["authority_expires_at"] <= now:
            state, event, reason, attempt_state = "expired", "job.expired", "AUTHORITY_EXPIRED", "expired"
        elif r["attempt_count"] >= r["max_attempts"] and reason != "PERMANENT_SOURCE_FAILURE":
            reason = "RETRY_BUDGET_EXHAUSTED"
        elif reason != "PERMANENT_SOURCE_FAILURE":
            due = snapshot.policy.next_due(now, r["attempt_count"])
            if snapshot.policy.lease_end(due) > r["authority_expires_at"]:
                state, event, reason, attempt_state = "expired", "job.expired", "AUTHORITY_EXPIRED", "expired"
            else:
                state, event, next_due = "queued", "job.reclaimed" if abandoned else "job.retry_scheduled", due
        return db.transition(session, snapshot, attempt, event=event, state=state, attempt_state=attempt_state, at=now,
            reason=reason, next_attempt_at=next_due)

    def acquire_publication_guard(self, session, *, claim):
        """Only publication receives a caller-owned clean existing Session root."""
        if (not isinstance(session, Session) or not session.in_transaction() or not session.is_active
                or session.in_nested_transaction() or session.new or session.dirty or session.deleted):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        try:
            guard, snapshot, attempt, now = self._worker(session, claim.locator)
            _require_claim(snapshot, attempt, claim, now)
            return self._make_handle(session, guard, snapshot, attempt, publication=True)
        except Exception:
            # Caller must roll back; the existing guard poisons its own failures.
            guard_state = getattr(session, "_satorna_publication_guard", None)
            if guard_state is not None:
                guard_state._failed = True
            raise

    def publish(self, session, *, claim, participant):
        """Trusted injected participant(session, handle) returns its real run ID.

        The caller still owns commit/rollback. Any participant exception poisons
        the root, including when a caller catches the bounded service error.
        """
        if not callable(participant):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        handle = self.acquire_publication_guard(session, claim=claim)
        try:
            _require_legacy_operation(handle._snapshot)
            result_sync_run_id = participant(session, handle)
            return handle.complete(result_sync_run_id=result_sync_run_id)
        except OrdersJobError:
            handle._guard._failed = True
            raise
        except Exception:
            handle._guard._failed = True
            raise OrdersJobError("JOB_PERSISTENCE_FAILED") from None

    def resolve_fetch(self, *, claim):
        """Resolve exact paired metadata outside publication locks; secret wrapper only.

        A fresh live persisted fence precedes resolution. Remote reads naturally
        have an authorization-to-I/O gap; publication rechecks the whole chain.
        """
        with _root(self._factory) as session:
            guard, snapshot, attempt, now = self._worker(session, claim.locator)
            _require_claim(snapshot, attempt, claim, now)
            _require_legacy_operation(snapshot)
            self._make_handle(session, guard, snapshot, attempt)
            expected_account, expected_credential = snapshot.account, snapshot.credential
        owner = MarketplaceAccountCredentialOwner(claim.locator.organization_id, claim.locator.marketplace_account_id, expected_account.provider)
        try:
            resolved = self._resolver(owner, expected_credential.kind)
            if type(resolved) is not ResolvedCredentialForFetch:
                raise OrdersJobError("JOB_AUTHORITY_DENIED")
            binding, identity = resolved.binding, resolved.binding.credential_identity
            if (binding.owner != owner or binding.external_account_id != expected_account.external_account_id
                    or binding.credential_ref != expected_account.credential_ref
                    or (identity.organization_id, identity.marketplace_account_id, identity.provider, identity.credential_kind,
                        identity.credential_id, identity.generation, identity.payload_schema_version, identity.expires_at)
                    != (owner.organization_id, owner.marketplace_account_id, owner.provider, expected_credential.kind,
                        expected_credential.credential_id, expected_credential.generation, expected_credential.payload_schema_version, expected_credential.expires_at)):
                raise OrdersJobError("JOB_AUTHORITY_DENIED")
            return resolved.secret
        except (CredentialStoreError, SQLAlchemyError):
            raise OrdersJobError("JOB_AUTHORITY_DENIED") from None
        except OrdersJobError:
            raise
        except Exception:
            raise OrdersJobError("JOB_AUTHORITY_DENIED") from None

    def request_for_fetch(self, *, claim):
        """Exact persisted selectors for trusted acquisition, after releasing locks."""
        with _root(self._factory) as session:
            guard, snapshot, attempt, now = self._worker(session, claim.locator)
            _require_claim(snapshot, attempt, claim, now)
            _require_legacy_operation(snapshot)
            self._make_handle(session, guard, snapshot, attempt)
            request = snapshot.request
        return request

    def execute(self, *, locator):
        """Deliberately no default source handler; no fetch or claim side effect."""
        raise OrdersJobError("SOURCE_CONTRACT_UNAVAILABLE")

    def close_denied(self, *, organization_id, marketplace_account_id, job_id):
        """State-closing only. Derive fresh denial internally; no reason parameter.

        May run after a failed authority transaction has rolled back. Locks only
        job then attempt; later metadata observations are SELECT without locks.
        Cannot publish, fetch, renew, adopt identities, or return command content.
        """
        locator = OrdersJobLocator(organization_id, marketplace_account_id, job_id)
        with _root(self._factory) as session:
            db.scope(session, organization_id, marketplace_account_id)
            snapshot = db.read_job(session, locator, lock=True)
            _require_legacy_operation(snapshot)
            attempt = db.read_attempt(session, snapshot)
            if snapshot.row["state"] not in TERMINAL:
                denial = self._observed_denial(session, snapshot)
                if denial is None:
                    raise OrdersJobError("JOB_AUTHORITY_DENIED")
                state, reason = denial
                snapshot, attempt = db.transition(session, snapshot, attempt, event="job." + state, state=state,
                    attempt_state=state if attempt is not None else None, at=db.now(session), reason=reason)
            result = db.view(snapshot).delivery_result()
        return result

    def _observed_denial(self, session, snapshot):
        """Allowlisted observations only. No caller exception/reason is evidence."""
        p, account, credential = snapshot.principal, snapshot.account, snapshot.credential
        u = session.execute(select(LkUserRow.organization_id, LkUserRow.is_active).where(LkUserRow.user_id == p.user_id)).one_or_none()
        m = session.execute(select(IamMembershipRow.organization_id, IamMembershipRow.user_id, IamMembershipRow.is_active,
            IamMembershipRow.role, IamMembershipRow.permissions, IamMembershipRow.scope_mode, IamMembershipRow.allowed_account_ids)
            .where(IamMembershipRow.membership_id == p.membership_id)).one_or_none()
        login = session.execute(select(LkSessionRow.user_id, LkSessionRow.revoked_at, LkSessionRow.expires_at).where(LkSessionRow.session_id == p.session_id)).one_or_none()
        if u is None or u.organization_id != p.organization_id or not u.is_active or m is None or m.organization_id != p.organization_id or m.user_id != p.user_id or not m.is_active or login is None or login.user_id != p.user_id or login.revoked_at is not None:
            return "revoked", "AUTHORITY_REVOKED"
        a = MarketplaceAccountRow
        actual = session.execute(select(a.marketplace, a.external_account_id, a.credential_ref, a.status).where(
            a.organization_id == p.organization_id, a.marketplace_account_id == account.marketplace_account_id)).one_or_none()
        if actual is None or actual.status != "connected":
            return "blocked", "ACCOUNT_DISCONNECTED"
        if tuple(actual[:3]) != (account.provider, account.external_account_id, account.credential_ref):
            return "blocked", "BINDING_CHANGED"
        c = MarketplaceAccountCredentialRow
        actual_c = session.execute(select(c.organization_id, c.marketplace_account_id, c.provider, c.credential_kind,
            c.generation, c.payload_schema_version, c.expires_at, c.revoked_at).where(c.credential_id == credential.credential_id)).one_or_none()
        if actual_c is None or tuple(actual_c[:7]) != (p.organization_id, account.marketplace_account_id, account.provider,
            credential.kind, credential.generation, credential.payload_schema_version, credential.expires_at):
            return "blocked", "BINDING_CHANGED"
        if actual_c.revoked_at is not None:
            return "revoked", "AUTHORITY_REVOKED"
        now = db.now(session)
        if snapshot.row["authority_expires_at"] <= now or login.expires_at <= now or (credential.expires_at is not None and credential.expires_at <= now):
            return "expired", "AUTHORITY_EXPIRED"
        try:
            _scope(m, (), frozenset({"sync:run"}))
        except PublicationGuardError:
            return "blocked", "PERMISSION_DENIED"
        try:
            _scope(m, (account,), frozenset())
        except PublicationGuardError:
            return "blocked", "ACCOUNT_SCOPE_DENIED"
        if snapshot.binding not in self._sources:
            return "blocked", "SOURCE_CONTRACT_UNAVAILABLE"
        return None

    def readback(self, *, organization_id, marketplace_account_id, job_id):
        """Safe worker state only after uncertain claim/success commit. Never fetch."""
        locator = OrdersJobLocator(organization_id, marketplace_account_id, job_id)
        with _root(self._factory) as session:
            db.scope(session, organization_id, marketplace_account_id)
            # Content-free trusted housekeeping: no newly minted claim/token.
            snapshot = db.read_job(session, locator)
            _require_legacy_operation(snapshot)
            result = db.view(snapshot).delivery_result()
        return result

    def deliver_existing(self, *, authenticated_actor, locator, deliver):
        """Fresh authorized read commits before invoking injected locator transport."""
        view = self.status(authenticated_actor=authenticated_actor, locator=locator)
        if view.state != "queued":
            return view.delivery_result()
        if not callable(deliver):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        try:
            deliver(locator.queue_payload())
        except Exception:
            raise OrdersJobError("DELIVERY_UNCONFIRMED") from None
        return view.delivery_result()

    def create_then_deliver(self, *, authenticated_actor, request, idempotency_key, execution_policy, authority_expires_at, deliver):
        view = self.create(authenticated_actor=authenticated_actor, request=request, idempotency_key=idempotency_key,
            execution_policy=execution_policy, authority_expires_at=authority_expires_at)
        locator = OrdersJobLocator(request.organization_id, request.marketplace_account_id, view.job_id)
        return self.deliver_existing(authenticated_actor=authenticated_actor, locator=locator, deliver=deliver)

    def recover_delivery(self, *, organization_id, marketplace_account_id, limit, deliver):
        """Bounded explicit recovery, no scheduler/outbox/exactly-once promise.

        Select existing rows within one explicit org/account. Each candidate's
        stored authority is freshly guarded and physically committed before send.
        """
        integer(limit, 1000)
        if not callable(deliver):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        if any(source.source_contract_version == "wb-history-positive-partial-v1" for source in self._sources):
            raise OrdersJobError("SOURCE_CONTRACT_UNAVAILABLE")
        with _root(self._factory) as session:
            db.scope(session, organization_id, marketplace_account_id)
            ids = session.scalars(select(db.jobs.c.job_id).where(db.jobs.c.organization_id == organization_id,
                db.jobs.c.marketplace_account_id == marketplace_account_id, db.jobs.c.state == "queued",
                db.jobs.c.operation_kind == "orders.sync.v1",
                db.jobs.c.next_attempt_at <= db.now(session)).order_by(db.jobs.c.next_attempt_at, db.jobs.c.job_id).limit(limit)).all()
        delivered = 0
        for job_id in ids:
            locator = OrdersJobLocator(organization_id, marketplace_account_id, job_id)
            with _root(self._factory) as session:
                guard, snapshot, attempt, now = self._worker(session, locator)
                ready = snapshot.row["state"] == "queued" and snapshot.row["next_attempt_at"] <= now and snapshot.policy.lease_end(now) <= snapshot.row["authority_expires_at"]
                if ready:
                    self._make_handle(session, guard, snapshot, None)
            if ready:
                try:
                    deliver(locator.queue_payload())
                except Exception:
                    raise OrdersJobError("DELIVERY_UNCONFIRMED") from None
                delivered += 1
        return {"delivered": delivered}


# Explicit function facade; no hidden global service, session, binding or policy.
def create_user_orders_job(service, *, authenticated_actor, request, idempotency_key,
                           execution_policy, authority_expires_at):
    return service.create(authenticated_actor=authenticated_actor, request=request, idempotency_key=idempotency_key,
        execution_policy=execution_policy, authority_expires_at=authority_expires_at)


def claim_user_orders_job(service, *, organization_id, marketplace_account_id, job_id):
    return service.claim(organization_id=organization_id, marketplace_account_id=marketplace_account_id, job_id=job_id)


def acquire_user_orders_job_publication_guard(session, *, service, claim):
    return service.acquire_publication_guard(session, claim=claim)
