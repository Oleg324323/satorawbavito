"""Dormant, explicitly composed history projection using real Orders authority."""
from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import select, text

from app.cabinet.orm import LkSessionRow
from app.modules.orders import OrderContractValidationError
from app.orders.publication_service import OrdersPublicationResult
from app.platform.integrations import user_orders_job_store as db
from app.platform.integrations import user_orders_jobs as authority
from app.platform.integrations import wb_history_projection_store as store
from app.platform.integrations.publication_guard import (
    PublicationGuardError,
    acquire_publication_guard,
)
from app.platform.integrations.user_orders_job_contract import (
    OrdersCredentialDependency,
    OrdersJobError,
    OrdersJobLocator,
    integer,
    timestamp,
    uuid4_value,
)
from app.platform.integrations.wb_history_projection_contract import (
    BINDING,
    OPERATION,
    SOURCE_CONTRACT,
    HistoryProjectionDependencies,
)
from app.platform.integrations.wb_history_projection_role import (
    HistoryProjectionRoleIdentity,
    inspect_projection_role,
)


class WbHistoryProjectionHandle:
    """Nonserializable wrapper; only an actually registered Orders handle works."""
    __slots__ = ("_base", "_page", "_plan", "_rows")

    def __init__(self, base):
        if type(base) is not authority.UserOrdersPublicationHandle:
            raise OrdersJobError("JOB_FENCE_INVALID")
        self._base = base
        self.require_root(base._session_ref())
        if base._history_baseline is not None:
            raise OrdersJobError("JOB_FENCE_INVALID")
        self._page, self._rows, self._plan = store.capture_chunk(base._session_ref(), base._snapshot)
        baseline = store.capture_parent_baseline(base._session_ref(), base._snapshot, self._plan)
        base._history_baseline = (base._snapshot.locator, self._plan, baseline)

    def __repr__(self):
        return "<WbHistoryProjectionHandle redacted>"

    def __reduce_ex__(self, protocol):
        raise TypeError("JOB_CONTRACT_INVALID")

    def require_root(self, session):
        base = self._base
        try:
            if (session is None or session is not base._session_ref() or not base._publication
                    or base._consumed or base._attempt is None or base._snapshot.row["operation_kind"] != OPERATION):
                raise OrdersJobError("JOB_FENCE_INVALID")
            base.revalidate_before_write()
        except Exception:
            base._guard._failed = True
            raise

    def revalidate_before_write(self):
        self.require_root(self._base._session_ref())

    @property
    def page(self):
        self.revalidate_before_write()
        return self._page

    @property
    def first_ordinal(self):
        self.revalidate_before_write()
        return self._plan.first_ordinal

    def captured_rows(self):
        self.revalidate_before_write()
        return deepcopy(self._rows)

    @property
    def input_checksum(self):
        self.revalidate_before_write()
        return self._plan.input_checksum

    @property
    def source_run_key(self):
        self.revalidate_before_write()
        return self._plan.source_run_key

    @property
    def source_snapshot(self):
        self.revalidate_before_write()
        return f"wb-history-run-v1:{self._page.job_id}:{self._page.run_id}"

    @property
    def source_contract_version(self):
        self.revalidate_before_write()
        return SOURCE_CONTRACT

    @property
    def account_binding(self):
        self.revalidate_before_write()
        return self._base.account_binding

    @property
    def initiating_user_id(self):
        self.revalidate_before_write()
        return self._base.initiating_user_id


@dataclass(frozen=True, slots=True, repr=False)
class HistoryProjectionProgress:
    job: object
    continuation_claim: object | None
    publication: OrdersPublicationResult


def _validate_history_final(base):
    """Registered fence hook; intermediate participant reads may be unsealed."""
    if type(base) is not authority.UserOrdersPublicationHandle:
        raise OrdersJobError("JOB_FENCE_INVALID")
    if base._guard._finalizing and (not base._consumed or base._history_seal is None):
        raise OrdersJobError("JOB_FENCE_INVALID")
    if base._history_seal is not None:
        if base._history_baseline is None:
            raise OrdersJobError("JOB_FENCE_INVALID")
        locator, _plan, baseline = base._history_baseline
        if locator != base._snapshot.locator:
            raise OrdersJobError("JOB_FENCE_INVALID")
        run_id, replayed = base._history_seal
        store.verify_parent_baseline(base._session_ref(), locator, baseline, run_id=run_id, replayed=replayed)


def _history_helper_call(session, handle, callback):
    if type(handle) is not WbHistoryProjectionHandle:
        raise OrdersJobError("JOB_FENCE_INVALID")
    def guarded_call():
        try:
            handle.require_root(session)
            result = callback()
            handle.require_root(session)
            return result
        except Exception:
            handle._base._guard._failed = True
            raise
    return _bounded_call(guarded_call)


def apply_history_initial_parent(session, *, handle: WbHistoryProjectionHandle, sync_run_id: int, order_id: int) -> int:
    def apply():
        integer(sync_run_id, 2**63 - 1)
        integer(order_id, 2**63 - 1)
        store.require_participant_run(session, handle._base._snapshot, handle._plan, sync_run_id, staging=True)
        version = session.scalar(text("SELECT public.wb_history_projection_apply_parent(:org,:account,:run,:order)"),
            {**store.params(handle._base._snapshot.locator), "run": sync_run_id, "order": order_id})
        integer(version, 2**63 - 1)
        return version
    return _history_helper_call(session, handle, apply)


def load_history_reconciliation_count(session, *, handle: WbHistoryProjectionHandle, sync_run_id: int) -> int:
    def count():
        integer(sync_run_id, 2**63 - 1)
        store.require_participant_run(session, handle._base._snapshot, handle._plan, sync_run_id, staging=False)
        row = session.execute(text("SELECT count(*) AS total,count(*) FILTER(WHERE history_decision_version=1 "
            "AND history_outcome IN ('initial_projection','semantic_replay','reconciliation_required')) AS proven,"
            "count(*) FILTER(WHERE history_decision_version=1 AND history_outcome='reconciliation_required') AS reconciliation "
            "FROM public.order_sync_memberships WHERE organization_id=:org AND marketplace_account_id=:account "
            "AND sync_run_id=:run AND order_item_id IS NULL"),
            {**store.params(handle._base._snapshot.locator), "run": sync_run_id}).one()
        if row.total != len(handle._plan.rows) or row.proven != row.total:
            raise OrdersJobError("JOB_CONFLICT")
        return row.reconciliation
    return _history_helper_call(session, handle, count)


def load_history_projection_receipt(session, *, organization_id, marketplace_account_id, sync_run_id):
    """Past-commit provenance only; caller retains its own current read guard."""
    return _bounded_call(lambda: store.read_receipt(session, organization_id=organization_id,
        marketplace_account_id=marketplace_account_id, sync_run_id=sync_run_id))


def _no_credential_resolution(*args, **kwargs):
    raise OrdersJobError("SOURCE_CONTRACT_UNAVAILABLE")


def _bounded_call(callback):
    """Drop caller exception objects/chains; raising happens outside handlers."""
    try:
        return callback()
    except OrdersJobError as error:
        code = OrdersJobError(error.code).code
    except PublicationGuardError:
        code = "JOB_AUTHORITY_DENIED"
    except OrderContractValidationError:
        code = "JOB_CONFLICT"
    except Exception:  # noqa: BLE001 -- deliberately sanitize the whole trusted participant/store boundary.
        code = "JOB_PERSISTENCE_FAILED"
    raise OrdersJobError(code)


def _deadline_call(callback, values):
    try:
        return timestamp(callback(**values))
    except Exception:  # noqa: BLE001 -- no callback object or chain is exposed at this boundary.
        code = "JOB_CONTRACT_INVALID"
    raise OrdersJobError(code)


class WbHistoryProjectionJobs:
    def __init__(self, *, session_factory, execution_policy, authority_deadline_provider, max_selection_pages, role_identity):
        if type(role_identity) is not HistoryProjectionRoleIdentity:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        self._role_identity = role_identity
        self._dependencies = HistoryProjectionDependencies(session_factory, execution_policy,
            authority_deadline_provider, max_selection_pages)
        self._jobs = authority.UserOrdersJobs(session_factory=session_factory, trusted_sources=(BINDING,),
            credential_resolver=_no_credential_resolution, _history_role_identity=role_identity)

    def __repr__(self):
        return "<WbHistoryProjectionJobs redacted>"

    def create(self, *, authenticated_actor, organization_id, marketplace_account_id,
            history_job_id, history_run_id, idempotency_key):
        return _bounded_call(lambda: self._create(authenticated_actor=authenticated_actor, organization_id=organization_id,
            marketplace_account_id=marketplace_account_id, history_job_id=history_job_id, history_run_id=history_run_id,
            idempotency_key=idempotency_key))

    def _create(self, *, authenticated_actor, organization_id, marketplace_account_id,
            history_job_id, history_run_id, idempotency_key):
        integer(organization_id)
        integer(marketplace_account_id)
        for value in (history_job_id, history_run_id, idempotency_key):
            uuid4_value(value)
        deps = self._dependencies
        with authority._root(deps.session_factory) as session:
            inspect_projection_role(session, self._role_identity)
            db.scope(session, organization_id, marketplace_account_id)
            principal = authority._actor_principal(session, authenticated_actor, organization_id)
            account = authority._account_metadata(session, organization_id, marketplace_account_id)
            if account.provider != "wb":
                raise OrdersJobError("JOB_ACCESS_DENIED")
            credential = authority._credential_metadata(session, organization_id, account, "wb_api")
            dependency = OrdersCredentialDependency(organization_id, marketplace_account_id, "wb",
                credential.credential_id, credential.kind, credential.generation,
                credential.payload_schema_version, credential.expires_at)
            guard = acquire_publication_guard(session, principal=principal, required_permissions=frozenset({"sync:run"}),
                accounts=(account,), authorities=(credential,))
            existing = db.find_idempotency(session, organization_id=organization_id,
                marketplace_account_id=marketplace_account_id, membership_id=principal.membership_id,
                idempotency_key=idempotency_key, operation_kind=OPERATION)
            if existing is not None:
                snapshot = db.read_job(session, OrdersJobLocator(organization_id, marketplace_account_id, existing), lock=True)
                authority._require_original(snapshot, principal)
                if (snapshot.account != account or snapshot.dependency != dependency
                        or snapshot.request.history_job_id != history_job_id or snapshot.request.history_run_id != history_run_id):
                    raise OrdersJobError("JOB_CONFLICT")
                store.validate_snapshot(session, snapshot)
                # Readback never refreshes the stored deadline or policy.
                result = db.view(snapshot)
            else:
                selection = store.capture_selection(session, principal=principal, account=account, credential=dependency,
                    history_job_id=history_job_id, history_run_id=history_run_id, maximum=deps.max_selection_pages)
                now = guard.revalidate_before_write()
                expiry = session.scalar(select(LkSessionRow.expires_at).where(LkSessionRow.session_id == principal.session_id))
                deadline = _deadline_call(deps.authority_deadline_provider,
                    {"principal": principal, "account": account, "credential": dependency,
                        "session_expires_at": expiry, "database_now": now})
                now = guard.revalidate_before_write()
                if deadline <= now or deadline > expiry or deps.execution_policy.lease_end(now) > deadline:
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
                snapshot = store.create(session, selection=selection, principal=principal, account=account,
                    credential=dependency, policy=deps.execution_policy, deadline=deadline, at=now, idempotency_key=idempotency_key)
                result = db.view(snapshot)
            authority.UserOrdersPublicationHandle(session, guard, snapshot, db.read_attempt(session, snapshot),
                deadline_required=existing is None, _history_role_identity=self._role_identity)
            inspect_projection_role(session, self._role_identity)
        return result

    def claim(self, *, organization_id, marketplace_account_id, job_id):
        return _bounded_call(lambda: self._jobs.claim(organization_id=organization_id, marketplace_account_id=marketplace_account_id, job_id=job_id))

    def status(self, *, authenticated_actor, locator):
        return _bounded_call(lambda: self._jobs.status(authenticated_actor=authenticated_actor, locator=locator))

    def renew(self, *, claim):
        return _bounded_call(lambda: self._jobs.renew(claim=claim))

    def publish_chunk(self, *, claim, participant):
        if not callable(participant):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        return _bounded_call(lambda: self._publish_chunk(claim=claim, participant=participant))

    def _publish_chunk(self, *, claim, participant):
        with authority._root(self._dependencies.session_factory) as session:
            base = self._jobs.acquire_publication_guard(session, claim=claim)
            def publish_in_root():
                handle = WbHistoryProjectionHandle(base)
                result = participant(session, handle=handle)
                if type(result) is not OrdersPublicationResult:
                    raise OrdersJobError("JOB_CONFLICT")
                handle.require_root(session)
                session.flush()
                integer(result.run_id, 2**63 - 1)
                if type(result.replayed) is not bool:
                    raise OrdersJobError("JOB_CONFLICT")
                base._history_seal = (result.run_id, result.replayed)
                now = base._validate_final()
                snapshot, attempt = store.advance(session, base._snapshot, base._attempt,
                    plan=handle._plan, result=result, at=now)
                base._snapshot, base._attempt, base._consumed = snapshot, attempt, True
                base._validate_final()
                view = db.view(snapshot)
                return result, snapshot, attempt, view
            try:
                result, snapshot, attempt, view = _bounded_call(publish_in_root)
            except OrdersJobError:
                base._guard._failed = True
                raise
        continuation = authority._committed(snapshot, attempt) if snapshot.row["state"] == "running" else None
        return HistoryProjectionProgress(view, continuation, result)
