"""Private local SQL store. Call only behind user_orders_jobs authority boundary.

No independent sessions, providers, decryption, or commits. New tables are Core
descriptors only: importing this module performs no schema or configuration I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import (ARRAY, BigInteger, Column, Date, DateTime, Integer, LargeBinary,
                        MetaData, SmallInteger, Table, Text, Uuid, insert, select, text, update)

from app.infra.db import set_tenant_context
from app.platform.integrations.publication_guard import ExpectedAccountBinding, ExpectedCredential, UserSessionPrincipal
from app.platform.integrations.user_orders_job_contract import (
    AvitoOrdersSourceRequest, OrdersCredentialDependency, OrdersExecutionPolicy, OrdersJobError, OrdersJobLocator,
    OrdersJobRequest, StoredOrdersJobView, TrustedOrdersSourceBinding, integer,
)

_metadata = MetaData()


def _table(name, spec):
    return Table(name, _metadata, *(Column(n, t) for n, t in spec), schema="public")


_owner = [("organization_id", Integer), ("marketplace_account_id", Integer), ("job_id", Uuid)]
jobs = _table("user_orders_jobs", _owner + [
    ("provider", Text), ("operation_kind", Text), ("required_permission", Text),
    ("initiator_user_id", Text), ("initiator_membership_id", Integer), ("initiator_session_id", Text),
    ("idempotency_key", Uuid), ("request_schema_version", SmallInteger), ("request_bytes", LargeBinary),
    ("request_checksum", Text), ("source_kind", Text), ("adapter_version", Text), ("mapping_version", Text),
    ("source_contract_version", Text), ("source_date_from", Date), ("source_statuses", ARRAY(Text)),
    ("source_limit", Integer), ("source_page", BigInteger), ("requested_from", DateTime(timezone=True)),
    ("requested_to", DateTime(timezone=True)), ("expected_external_account_id", Text), ("expected_credential_ref", Text),
    ("created_at", DateTime(timezone=True)), ("authority_expires_at", DateTime(timezone=True)),
    ("policy_reference", Text), ("policy_version", Integer), ("max_attempts", Integer), ("lease_seconds", Integer),
    ("retry_backoff_seconds", ARRAY(Integer)), ("state", Text), ("version", BigInteger), ("attempt_count", Integer),
    ("current_attempt_id", Uuid), ("next_attempt_at", DateTime(timezone=True)), ("completed_at", DateTime(timezone=True)),
    ("safe_reason", Text), ("result_sync_run_id", BigInteger), ("result_coverage_state", Text)])
authorities = _table("user_orders_job_authorities", _owner + [
    ("credential_id", Uuid), ("credential_kind", Text), ("provider", Text), ("generation", BigInteger),
    ("payload_schema_version", SmallInteger), ("expires_at", DateTime(timezone=True))])
attempts = _table("user_orders_job_attempts", _owner + [
    ("attempt_id", Uuid), ("attempt_number", Integer), ("claimant_token", Uuid), ("state", Text),
    ("version", BigInteger), ("job_version_after", BigInteger), ("claimed_at", DateTime(timezone=True)),
    ("lease_expires_at", DateTime(timezone=True)), ("finished_at", DateTime(timezone=True)), ("safe_reason", Text),
    ("result_sync_run_id", BigInteger), ("result_coverage_state", Text)])
audit = _table("user_orders_job_audit", _owner + [
    ("event_id", Uuid), ("job_version_after", BigInteger), ("event_kind", Text), ("occurred_at", DateTime(timezone=True)),
    ("actor_kind", Text), ("actor_membership_id", Integer), ("attempt_id", Uuid), ("before_state", Text),
    ("after_state", Text), ("reason", Text), ("attempt_version_before", BigInteger), ("attempt_version_after", BigInteger),
    ("attempt_state_before", Text), ("attempt_state_after", Text), ("attempt_count_after", Integer),
    ("next_attempt_at", DateTime(timezone=True)), ("lease_expires_at", DateTime(timezone=True)),
    ("result_sync_run_id", BigInteger), ("result_coverage_state", Text)])
_MUTABLE = frozenset({"state", "version", "attempt_count", "current_attempt_id", "next_attempt_at",
                      "completed_at", "safe_reason", "result_sync_run_id", "result_coverage_state"})


def scope(session, organization_id, marketplace_account_id):
    integer(organization_id)
    integer(marketplace_account_id)
    for key, value in (("app.organization_id", organization_id), ("app.marketplace_account_id", marketplace_account_id)):
        current = session.scalar(text("SELECT current_setting(:key,true)"), {"key": key})
        if current not in (None, "", str(value)):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
    set_tenant_context(session, organization_id)
    session.execute(text("SELECT set_config('app.marketplace_account_id',:account,true)"), {"account": str(marketplace_account_id)})


def now(session):
    return session.scalar(text("SELECT clock_timestamp()"))


def owner(locator):
    return {"organization_id": locator.organization_id, "marketplace_account_id": locator.marketplace_account_id,
            "job_id": locator.job_id}


def where(table, locator):
    return tuple(table.c[key] == value for key, value in owner(locator).items())


@dataclass(frozen=True, slots=True, repr=False)
class JobSnapshot:
    """Immutable locator/delegation snapshot, private to the service."""
    row: object
    authority: object

    def __repr__(self):
        return "<JobSnapshot redacted>"

    @property
    def locator(self):
        return OrdersJobLocator(self.row["organization_id"], self.row["marketplace_account_id"], self.row["job_id"])

    @property
    def principal(self):
        r = self.row
        return UserSessionPrincipal(r["organization_id"], r["initiator_user_id"], r["initiator_membership_id"], r["initiator_session_id"])

    @property
    def account(self):
        r = self.row
        return ExpectedAccountBinding(r["marketplace_account_id"], r["provider"], r["expected_external_account_id"], r["expected_credential_ref"])

    @property
    def dependency(self):
        r = self.authority
        return OrdersCredentialDependency(r["organization_id"], r["marketplace_account_id"], r["provider"],
            r["credential_id"], r["credential_kind"], r["generation"], r["payload_schema_version"], r["expires_at"])

    @property
    def credential(self):
        r = self.dependency
        return ExpectedCredential(r.marketplace_account_id, r.credential_id, r.credential_kind, r.generation, r.payload_schema_version, r.expires_at)

    @property
    def binding(self):
        r = self.row
        return TrustedOrdersSourceBinding(r["provider"], r["source_kind"], r["adapter_version"], r["mapping_version"], r["source_contract_version"])

    @property
    def request(self):
        if self.row["operation_kind"] == "orders.wb-history.project.v1":
            from app.platform.integrations.wb_history_projection_contract import BINDING, WbHistoryProjectionRequest
            if self.binding != BINDING:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
            request = WbHistoryProjectionRequest.from_bytes(bytes(self.row["request_bytes"]))
            if (request.organization_id, request.marketplace_account_id, request.checksum) != (
                    self.row["organization_id"], self.row["marketplace_account_id"], self.row["request_checksum"]):
                raise OrdersJobError("JOB_CONTRACT_INVALID")
            for name in ("history_job_id", "history_run_id", "history_selection_digest", "history_page_count", "history_terminal_page_id"):
                if self.row[name] != getattr(request, name):
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
            return request
        return OrdersJobRequest.from_bytes(bytes(self.row["request_bytes"]), trusted_binding=self.binding)

    @property
    def policy(self):
        r = self.row
        return OrdersExecutionPolicy(r["policy_reference"], r["policy_version"], r["max_attempts"], r["lease_seconds"], tuple(r["retry_backoff_seconds"]))

    def same_delegation(self, other):
        mutable = _MUTABLE
        if self.row["operation_kind"] == "orders.wb-history.project.v1":
            mutable = mutable | {"history_cursor_page", "history_cursor_ordinal", "history_progress_version"}
        return ({k: v for k, v in self.row.items() if k not in mutable} ==
                {k: v for k, v in other.row.items() if k not in mutable} and self.authority == other.authority)


def read_job(session, locator, *, lock=False):
    stmt = select(jobs).where(*where(jobs, locator))
    if lock:
        stmt = stmt.with_for_update()
    row = session.execute(stmt).mappings().one_or_none()
    if row is None:
        raise OrdersJobError("JOB_NOT_FOUND")
    if row["operation_kind"] == "orders.wb-history.project.v1":
        from app.platform.integrations.wb_history_projection_store import read_job_row
        row = read_job_row(session, locator, lock=lock)
    dependencies = session.execute(select(authorities).where(*where(authorities, locator))).mappings().all()
    if len(dependencies) != 1:
        raise OrdersJobError("JOB_AUTHORITY_DENIED")
    return JobSnapshot(row, dependencies[0])


def read_attempt(session, snapshot, *, lock=True):
    if snapshot.row["current_attempt_id"] is None:
        return None
    stmt = select(attempts).where(*where(attempts, snapshot.locator), attempts.c.attempt_id == snapshot.row["current_attempt_id"])
    if lock:
        stmt = stmt.with_for_update()
    result = session.execute(stmt).mappings().one_or_none()
    if result is None:
        raise OrdersJobError("JOB_FENCE_INVALID")
    return result


def view(snapshot):
    r = snapshot.row
    return StoredOrdersJobView(*(r[name] for name in ("job_id", "state", "version", "attempt_count", "next_attempt_at",
        "completed_at", "safe_reason", "result_sync_run_id", "result_coverage_state")))


def find_idempotency(session, *, organization_id, marketplace_account_id, membership_id, idempotency_key,
        operation_kind="orders.sync.v1"):
    return session.scalar(select(jobs.c.job_id).where(jobs.c.organization_id == organization_id,
        jobs.c.marketplace_account_id == marketplace_account_id, jobs.c.initiator_membership_id == membership_id,
        jobs.c.operation_kind == operation_kind, jobs.c.idempotency_key == idempotency_key))


def create(session, *, request, policy, principal, account, credential, idempotency_key, deadline, created_at, job_id):
    source, binding = request.source_request, request.binding
    locator = OrdersJobLocator(request.organization_id, request.marketplace_account_id, job_id)
    avito = type(source) is AvitoOrdersSourceRequest
    values = {**owner(locator), "provider": binding.provider, "operation_kind": "orders.sync.v1", "required_permission": "sync:run",
        "initiator_user_id": principal.user_id, "initiator_membership_id": principal.membership_id, "initiator_session_id": principal.session_id,
        "idempotency_key": idempotency_key, "request_schema_version": 1, "request_bytes": request.canonical_bytes, "request_checksum": request.checksum,
        "source_kind": binding.source_kind, "adapter_version": binding.adapter_version, "mapping_version": binding.mapping_version,
        "source_contract_version": binding.source_contract_version, "source_date_from": source.date_from,
        "source_statuses": list(source.statuses) if avito else None, "source_limit": source.limit if avito else None,
        "source_page": source.page if avito else None, "requested_from": None, "requested_to": None,
        "expected_external_account_id": account.external_account_id, "expected_credential_ref": account.credential_ref,
        "created_at": created_at, "authority_expires_at": deadline, "policy_reference": policy.reference, "policy_version": policy.version,
        "max_attempts": policy.max_attempts, "lease_seconds": policy.lease_seconds, "retry_backoff_seconds": list(policy.retry_backoff_seconds),
        "state": "queued", "version": 1, "attempt_count": 0, "current_attempt_id": None, "next_attempt_at": created_at,
        "completed_at": None, "safe_reason": None, "result_sync_run_id": None, "result_coverage_state": None}
    session.execute(insert(jobs).values(**values))
    session.execute(insert(authorities).values(**owner(locator), credential_id=credential.credential_id,
        credential_kind=credential.kind, provider=binding.provider, generation=credential.generation,
        payload_schema_version=credential.payload_schema_version, expires_at=credential.expires_at))
    snapshot = read_job(session, locator)
    _audit(session, snapshot, before=None, attempt_before=None, attempt_after=None,
           event="job.created", at=created_at, membership_id=principal.membership_id)
    return snapshot


def _audit(session, snapshot, *, before, attempt_before, attempt_after, event, at, membership_id=None):
    r, a, b = snapshot.row, attempt_after, attempt_before
    session.execute(insert(audit).values(**owner(snapshot.locator), event_id=uuid4(), job_version_after=r["version"],
        event_kind=event, occurred_at=at, actor_kind="membership" if membership_id is not None else "delegated_worker",
        actor_membership_id=membership_id, attempt_id=a["attempt_id"] if a else None,
        before_state=before.row["state"] if before else None, after_state=r["state"], reason=r["safe_reason"],
        attempt_version_before=b["version"] if b else None, attempt_version_after=a["version"] if a else None,
        attempt_state_before=b["state"] if b else None, attempt_state_after=a["state"] if a else None,
        attempt_count_after=r["attempt_count"], next_attempt_at=r["next_attempt_at"], lease_expires_at=a["lease_expires_at"] if a else None,
        result_sync_run_id=r["result_sync_run_id"], result_coverage_state=r["result_coverage_state"]))


def transition(session, snapshot, attempt, *, event, state, at, reason=None, membership_id=None,
               next_attempt_at=None, lease_expires_at=None, attempt_state=None, result_sync_run_id=None, result_coverage_state=None):
    """One exact version CAS, attempt transition and audit; caller holds locks."""
    r = snapshot.row
    new_version = integer(r["version"] + 1, 2**63 - 1)
    a = attempt
    count = r["attempt_count"]
    pointer = r["current_attempt_id"]
    if event == "job.claimed":
        count += 1
        a = {**owner(snapshot.locator), "attempt_id": uuid4(), "attempt_number": count, "claimant_token": uuid4(),
             "state": "claimed", "version": 1, "job_version_after": new_version, "claimed_at": at,
             "lease_expires_at": lease_expires_at, "finished_at": None, "safe_reason": None,
             "result_sync_run_id": None, "result_coverage_state": None}
        session.execute(insert(attempts).values(**a))
        pointer = a["attempt_id"]
    elif attempt is not None:
        a = dict(attempt)
        a.update(state=attempt_state, version=integer(attempt["version"] + 1, 2**63 - 1), job_version_after=new_version,
            lease_expires_at=lease_expires_at if lease_expires_at is not None else attempt["lease_expires_at"],
            finished_at=None if attempt_state == "claimed" else at, safe_reason=reason,
            result_sync_run_id=result_sync_run_id, result_coverage_state=result_coverage_state)
        changes = {key: a[key] for key in ("state", "version", "job_version_after", "lease_expires_at", "finished_at", "safe_reason", "result_sync_run_id", "result_coverage_state")}
        result = session.execute(update(attempts).where(*where(attempts, snapshot.locator),
            attempts.c.attempt_id == attempt["attempt_id"], attempts.c.claimant_token == attempt["claimant_token"],
            attempts.c.version == attempt["version"], attempts.c.state == "claimed").values(**changes))
        if result.rowcount != 1:
            raise OrdersJobError("JOB_FENCE_INVALID")
    if state == "queued":
        pointer = None
    values = {"state": state, "version": new_version, "attempt_count": count, "current_attempt_id": pointer,
              "next_attempt_at": next_attempt_at, "completed_at": None if state in {"queued", "running"} else at,
              "safe_reason": reason, "result_sync_run_id": result_sync_run_id, "result_coverage_state": result_coverage_state}
    result = session.execute(update(jobs).where(*where(jobs, snapshot.locator), jobs.c.version == r["version"],
        jobs.c.state == r["state"], jobs.c.current_attempt_id.is_not_distinct_from(r["current_attempt_id"])).values(**values))
    if result.rowcount != 1:
        raise OrdersJobError("JOB_FENCE_INVALID")
    after = read_job(session, snapshot.locator)
    _audit(session, after, before=snapshot, attempt_before=attempt, attempt_after=a, event=event, at=at, membership_id=membership_id)
    return after, a
