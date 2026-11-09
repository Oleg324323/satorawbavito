"""Private in-root history projection SQL; no independent authority or commit."""
import hashlib
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    Integer,
    MetaData,
    Text,
    Uuid,
    insert,
    select,
    text,
    update,
)

from app.orders.bindings import validate_run_binding
from app.orders.history_bridge import HistoryPageEvidence, decode_history_chunk
from app.platform.integrations import user_orders_job_store as db
from app.platform.integrations.user_orders_job_contract import (
    OrdersJobError,
    OrdersJobLocator,
    integer,
)
from app.platform.integrations.wb_history_projection_contract import (
    BINDING,
    OPERATION,
    SOURCE_CONTRACT,
    HistoryProjectionReceipt,
    freeze_history_selection,
    history_header_bytes,
)

_metadata = MetaData()
jobs = db.jobs.to_metadata(_metadata)
for name, kind in (("history_job_id", Uuid), ("history_run_id", Uuid), ("history_selection_digest", Text),
        ("history_page_count", Integer), ("history_terminal_page_id", Uuid), ("history_cursor_page", Integer),
        ("history_cursor_ordinal", Integer), ("history_progress_version", BigInteger)):
    jobs.append_column(Column(name, kind))
audit = db.audit.to_metadata(_metadata)
for name, kind in (("history_page_index", Integer), ("history_page_id", Uuid), ("history_first_ordinal", Integer),
        ("history_next_ordinal", Integer), ("history_input_checksum", Text), ("history_result_sync_run_id", BigInteger),
        ("history_reconciliation_count", Integer)):
    audit.append_column(Column(name, kind))


def params(locator):
    return {"org": locator.organization_id, "account": locator.marketplace_account_id, "job": locator.job_id}


def _verify_parent_baseline(before, after, *, run_id, replayed):
    """Compare only this bounded transaction's prestate, never past receipt state."""
    expected = {row[0]: row[1:] for row in before}
    actual = {row["external_order_id"]: row for row in after}
    if (len(before) > 1000 or len(expected) != len(before) or len(actual) != len(after)
            or expected.keys() != actual.keys()):
        raise OrdersJobError("JOB_FENCE_INVALID")
    for external, prior in expected.items():
        row = actual[external]
        current = tuple(row[key] for key in ("order_id", "version", "last_seen_sync_run_id", "prior_observation_id"))
        if replayed:
            if current != prior:
                raise OrdersJobError("JOB_FENCE_INVALID")
            continue
        prestate = tuple(row[key] for key in ("history_pre_order_version", "history_pre_sync_run_id", "history_pre_observation_id"))
        outcome = row["history_outcome"]
        if prior[0] is None or (prior[1:] == (1, None, None) and outcome == "initial_projection"):
            if (outcome != "initial_projection" or prestate != (1, None, None)
                    or (prior[0] is not None and current[0] != prior[0])
                    or current[0] is None or current[1:3] != (2, run_id) or current[3] is None):
                raise OrdersJobError("JOB_FENCE_INVALID")
        elif (current != prior or prestate != prior[1:]
                or outcome not in {"semantic_replay", "reconciliation_required"}):
            raise OrdersJobError("JOB_FENCE_INVALID")


_PARENT_STATE = """SELECT o.external_order_id,o.order_id,o.version,o.last_seen_sync_run_id,
 prior.observation_id AS prior_observation_id
 FROM public.marketplace_orders o LEFT JOIN public.order_sync_memberships prior
 ON prior.organization_id=o.organization_id AND prior.marketplace_account_id=o.marketplace_account_id
 AND prior.order_id=o.order_id AND prior.sync_run_id=o.last_seen_sync_run_id AND prior.order_item_id IS NULL
 WHERE o.organization_id=:org AND o.marketplace_account_id=:account AND o.marketplace='wb'
 AND o.external_order_id=ANY(CAST(:external_ids AS text[]))"""


def capture_parent_baseline(session, snapshot, plan):
    """Called only after the genuine account/job guard; lock at most one chunk."""
    external = tuple(sorted(row.observation.identity.external_order_id for row in plan.rows))
    if len(external) > 1000 or len(set(external)) != len(external):
        raise OrdersJobError("JOB_FENCE_INVALID")
    values = {**params(snapshot.locator), "external_ids": list(external)}
    rows = session.execute(text(_PARENT_STATE + " ORDER BY o.order_id LIMIT 1001 FOR UPDATE OF o"), values).mappings().all()
    found = {row["external_order_id"]: row for row in rows}
    if len(found) != len(rows) or not found.keys() <= set(external):
        raise OrdersJobError("JOB_FENCE_INVALID")
    result = []
    for identity in external:
        row = found.get(identity)
        if row is None:
            result.append((identity, None, None, None, None))
        else:
            if (row["last_seen_sync_run_id"] is None) != (row["prior_observation_id"] is None):
                raise OrdersJobError("JOB_FENCE_INVALID")
            result.append((identity, row["order_id"], row["version"], row["last_seen_sync_run_id"], row["prior_observation_id"]))
    return tuple(result)


def verify_parent_baseline(session, locator, baseline, *, run_id, replayed):
    values = {**params(locator), "external_ids": [row[0] for row in baseline], "run": run_id}
    rows = session.execute(text("WITH current_parent AS (" + _PARENT_STATE + " ORDER BY o.order_id LIMIT 1001) "
        "SELECT p.*,m.history_pre_order_version,m.history_pre_sync_run_id,m.history_pre_observation_id,m.history_outcome "
        "FROM current_parent p LEFT JOIN public.order_sync_memberships m ON m.organization_id=:org "
        "AND m.marketplace_account_id=:account AND m.order_id=p.order_id AND m.sync_run_id=:run "
        "AND m.order_item_id IS NULL ORDER BY p.order_id LIMIT 1001"), values).mappings().all()
    _verify_parent_baseline(baseline, rows, run_id=run_id, replayed=replayed)


def require_participant_run(session, snapshot, plan, run_id, *, staging):
    row = session.execute(text("SELECT marketplace,source_kind,adapter_version,mapping_version,source_contract_version,"
        "source_run_key,source_snapshot,state,manifest_state,payload_checksum FROM public.order_sync_runs "
        "WHERE organization_id=:org AND marketplace_account_id=:account AND sync_run_id=:run FOR UPDATE"),
        {**params(snapshot.locator), "run": run_id}).mappings().one_or_none()
    if (row is None or tuple(row[key] for key in ("marketplace", "source_kind", "adapter_version", "mapping_version", "source_contract_version"))
            != (BINDING.provider, BINDING.source_kind, BINDING.adapter_version, BINDING.mapping_version, SOURCE_CONTRACT)
            or row["source_run_key"] != plan.source_run_key
            or row["source_snapshot"] != f"wb-history-run-v1:{plan.page.job_id}:{plan.page.run_id}"
            or (staging and row["state"] != "staging")
            or (not staging and (row["state"] != "partial" or row["manifest_state"] != "partial"
                or row["payload_checksum"] != plan.input_checksum))):
        raise OrdersJobError("JOB_CONFLICT")


def read_job_row(session, locator, *, lock=False):
    stmt = select(jobs).where(*db.where(jobs, locator))
    return session.execute(stmt.with_for_update() if lock else stmt).mappings().one()


def page_evidence(row):
    receipt = row["receipt"]
    try:
        return HistoryPageEvidence(row["organization_id"], row["marketplace_account_id"], str(row["job_id"]),
            str(row["page_id"]), str(row["run_id"]), str(row["credential_id"]), row["credential_generation"],
            row["account_incarnation"], row["request_checksum"], receipt["raw_checksum"], row["input_date_from"],
            receipt["next_date_from"], receipt["row_count"], receipt["terminal"], row["published_at"], row["state"])
    except (ValueError, TypeError, KeyError):
        raise OrdersJobError("JOB_CONFLICT") from None


def capture_selection(session, *, principal, account, credential, history_job_id, history_run_id, maximum):
    incarnation = session.scalar(text("SELECT ingestion_binding_version FROM marketplace_accounts "
        "WHERE organization_id=:org AND marketplace_account_id=:account"),
        {"org": principal.organization_id, "account": account.marketplace_account_id})
    rows = session.execute(text("SELECT * FROM wb_live_history_pages WHERE organization_id=:org "
        "AND marketplace_account_id=:account AND job_id=:job AND run_id=:run "
        "AND source='wb-statistics-supplier-orders' AND state='published' ORDER BY page_id LIMIT :maximum"),
        {"org": principal.organization_id, "account": account.marketplace_account_id, "job": history_job_id,
            "run": history_run_id, "maximum": maximum + 1}).mappings().all()
    selection = freeze_history_selection(organization_id=principal.organization_id,
        marketplace_account_id=account.marketplace_account_id, history_job_id=history_job_id,
        history_run_id=history_run_id, credential_id=credential.credential_id,
        credential_generation=credential.generation, account_incarnation=incarnation,
        pages=tuple(page_evidence(row) for row in rows), max_selection_pages=maximum)
    initial = session.scalar(text("SELECT EXISTS(SELECT FROM wb_live_history_requests WHERE organization_id=:org "
        "AND marketplace_account_id=:account AND job_id=:job AND source=:source AND date_from=:initial)"),
        {"org": principal.organization_id, "account": account.marketplace_account_id, "job": history_job_id,
            "source": BINDING.source_kind, "initial": selection.pages[0].input_date_from})
    if not initial:
        raise OrdersJobError("JOB_CONFLICT")
    return selection


def create(session, *, selection, principal, account, credential, policy, deadline, at, idempotency_key):
    request = selection.request
    locator = OrdersJobLocator(request.organization_id, request.marketplace_account_id, uuid4())
    values = {**db.owner(locator), "provider": "wb", "operation_kind": OPERATION, "required_permission": "sync:run",
        "initiator_user_id": principal.user_id, "initiator_membership_id": principal.membership_id,
        "initiator_session_id": principal.session_id, "idempotency_key": idempotency_key,
        "request_schema_version": 1, "request_bytes": request.canonical_bytes, "request_checksum": request.checksum,
        "source_kind": BINDING.source_kind, "adapter_version": BINDING.adapter_version,
        "mapping_version": BINDING.mapping_version, "source_contract_version": SOURCE_CONTRACT,
        "source_date_from": None, "source_statuses": None, "source_limit": None, "source_page": None,
        "requested_from": None, "requested_to": None, "expected_external_account_id": account.external_account_id,
        "expected_credential_ref": account.credential_ref, "created_at": at, "authority_expires_at": deadline,
        "policy_reference": policy.reference, "policy_version": policy.version, "max_attempts": policy.max_attempts,
        "lease_seconds": policy.lease_seconds, "retry_backoff_seconds": list(policy.retry_backoff_seconds),
        "state": "queued", "version": 1, "attempt_count": 0, "current_attempt_id": None, "next_attempt_at": at,
        "completed_at": None, "safe_reason": None, "result_sync_run_id": None, "result_coverage_state": None,
        "history_cursor_page": 0, "history_cursor_ordinal": 0, "history_progress_version": 0}
    for name in ("history_job_id", "history_run_id", "history_selection_digest", "history_page_count", "history_terminal_page_id"):
        values[name] = getattr(request, name)
    session.execute(insert(jobs).values(**values))
    session.execute(insert(db.authorities).values(**db.owner(locator), credential_id=credential.credential_id,
        credential_kind=credential.credential_kind, provider="wb", generation=credential.generation,
        payload_schema_version=credential.payload_schema_version, expires_at=credential.expires_at))
    session.execute(text("INSERT INTO user_orders_history_selection_pages(organization_id,marketplace_account_id,job_id,"
        "page_index,history_job_id,history_source,history_page_id,history_run_id,header_checksum) "
        "VALUES(:org,:account,:job,:index,:history_job,:source,:page,:run,:digest)"),
        [{**params(locator), "index": index, "history_job": request.history_job_id, "source": BINDING.source_kind,
            "page": UUID(page.page_id), "run": request.history_run_id, "digest": digest}
            for index, (page, digest) in enumerate(zip(selection.pages, selection.header_checksums, strict=True))])
    snapshot = db.read_job(session, locator)
    db._audit(session, snapshot, before=None, attempt_before=None, attempt_after=None,
        event="job.created", at=at, membership_id=principal.membership_id)
    return snapshot


def validate_snapshot(session, snapshot):
    """Extra new-operation incarnation proof behind the existing live guard."""
    _ = snapshot.request
    row = session.execute(text("SELECT h.credential_id,h.credential_generation,h.account_incarnation,"
        "a.ingestion_binding_version FROM user_orders_history_selection_pages p JOIN wb_live_history_pages h ON "
        "(h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)="
        "(p.organization_id,p.marketplace_account_id,p.history_job_id,p.history_source,p.history_page_id) "
        "JOIN marketplace_accounts a ON (a.organization_id,a.marketplace_account_id)=(p.organization_id,p.marketplace_account_id) "
        "WHERE p.organization_id=:org AND p.marketplace_account_id=:account AND p.job_id=:job AND p.page_index=0 "
        "AND h.state='published' AND h.run_id=p.history_run_id"), params(snapshot.locator)).one_or_none()
    if row is None or row[0] != snapshot.dependency.credential_id or row[1] != snapshot.dependency.generation or row[2] != row[3]:
        raise OrdersJobError("JOB_FENCE_INVALID")


def capture_chunk(session, snapshot):
    r = snapshot.row
    if not 0 <= r["history_cursor_page"] < r["history_page_count"]:
        raise OrdersJobError("JOB_FENCE_INVALID")
    row = session.execute(text("SELECT h.*,p.header_checksum FROM user_orders_history_selection_pages p "
        "JOIN wb_live_history_pages h ON (h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)="
        "(p.organization_id,p.marketplace_account_id,p.history_job_id,p.history_source,p.history_page_id) "
        "WHERE p.organization_id=:org AND p.marketplace_account_id=:account AND p.job_id=:job AND p.page_index=:index "
        "AND h.run_id=p.history_run_id AND h.state='published'"),
        {**params(snapshot.locator), "index": r["history_cursor_page"]}).mappings().one_or_none()
    if row is None:
        raise OrdersJobError("JOB_FENCE_INVALID")
    page = page_evidence(row)
    if hashlib.sha256(history_header_bytes(page)).hexdigest() != row["header_checksum"]:
        raise OrdersJobError("JOB_CONFLICT")
    first = r["history_cursor_ordinal"]
    rows = session.execute(text("SELECT ordinal,srid,nm_id,barcode,semantic_checksum,source_row_checksum,"
        "observation,source_revision,effective_at FROM wb_live_history_rows WHERE organization_id=:org "
        "AND marketplace_account_id=:account AND job_id=:job AND source=:source AND page_id=:page "
        "AND ordinal>=:first AND ordinal<:stop ORDER BY ordinal LIMIT 1000"),
        {"org": page.organization_id, "account": page.marketplace_account_id, "job": UUID(page.job_id),
            "source": BINDING.source_kind, "page": UUID(page.page_id), "first": first,
            "stop": min(first + 1000, page.row_count)}).mappings().all()
    copied = tuple(dict(row) for row in rows)
    try:
        plan = decode_history_chunk(page=page, first_ordinal=first, rows=copied)
    except ValueError:
        raise OrdersJobError("JOB_CONFLICT") from None
    return page, copied, plan


def advance(session, snapshot, attempt, *, plan, result, at):
    """Same-root canonical result verification and witnessed progress CAS."""
    integer(result.run_id, 2**63 - 1)
    integer(result.reconciliation_count, zero=True)
    if result.state != "partial" or type(result.replayed) is not bool:
        raise OrdersJobError("JOB_CONFLICT")
    p = {**params(snapshot.locator), "run": result.run_id}
    run = session.execute(text("SELECT * FROM order_sync_runs WHERE organization_id=:org "
        "AND marketplace_account_id=:account AND sync_run_id=:run FOR UPDATE"), p).mappings().one_or_none()
    source_snapshot = f"wb-history-run-v1:{plan.page.job_id}:{plan.page.run_id}"
    if (run is None or tuple(run[k] for k in ("marketplace", "source_kind", "adapter_version", "mapping_version",
            "source_contract_version", "source_run_key", "source_snapshot", "payload_checksum", "state", "manifest_state"))
            != ("wb", BINDING.source_kind, BINDING.adapter_version, BINDING.mapping_version, SOURCE_CONTRACT,
                plan.source_run_key, source_snapshot, plan.input_checksum, "partial", "partial")
            or run["requested_from"] is not None or run["requested_to"] is not None or run["expected_order_count"] is not None
            or run["page_count"] != 1 or run["order_count"] != len(plan.rows) or run["item_count"] != len(plan.rows)
            or run["completed_at"] is None or not run["started_at"] <= run["completed_at"] <= at):
        raise OrdersJobError("JOB_CONFLICT")
    validate_run_binding(snapshot.locator.organization_id, snapshot.account,
        schema_version=run["account_binding_schema_version"], external_account_id=run["account_binding_external_account_id"],
        credential_ref=run["account_binding_credential_ref"], payload=run["account_binding_payload"], checksum=run["account_binding_checksum"])
    physical = session.scalar(text("SELECT encode(sha256(public.wb_history_projection_chunk_bytes(:org,:account,:history_job,:page,:first)),'hex')"),
        {**p, "history_job": UUID(plan.page.job_id), "page": UUID(plan.page.page_id), "first": plan.first_ordinal})
    if physical != plan.input_checksum:
        raise OrdersJobError("JOB_CONFLICT")
    r = snapshot.row
    final = plan.page.terminal
    moved_page = r["history_cursor_page"] + (1 if plan.next_ordinal == plan.page.row_count else 0)
    moved_ordinal = 0 if moved_page != r["history_cursor_page"] else plan.next_ordinal
    if final and moved_page != r["history_page_count"]:
        raise OrdersJobError("JOB_CONFLICT")
    version = integer(r["version"] + 1, 2**63 - 1)
    a = dict(attempt)
    a.update(state="succeeded" if final else "claimed", version=integer(a["version"] + 1, 2**63 - 1),
        job_version_after=version, finished_at=at if final else None, safe_reason=None,
        result_sync_run_id=result.run_id if final else None, result_coverage_state="partial" if final else None)
    changed = session.execute(update(db.attempts).where(*db.where(db.attempts, snapshot.locator),
        db.attempts.c.attempt_id == attempt["attempt_id"], db.attempts.c.version == attempt["version"],
        db.attempts.c.claimant_token == attempt["claimant_token"], db.attempts.c.state == "claimed").values(
        **{key: a[key] for key in ("state", "version", "job_version_after", "finished_at", "safe_reason", "result_sync_run_id", "result_coverage_state")}))
    if changed.rowcount != 1:
        raise OrdersJobError("JOB_FENCE_INVALID")
    changed = session.execute(update(jobs).where(*db.where(jobs, snapshot.locator), jobs.c.version == r["version"],
        jobs.c.state == "running", jobs.c.current_attempt_id == attempt["attempt_id"],
        jobs.c.history_progress_version == r["history_progress_version"]).values(state="succeeded" if final else "running",
        version=version, completed_at=at if final else None, result_sync_run_id=result.run_id if final else None,
        result_coverage_state="partial" if final else None, history_cursor_page=moved_page,
        history_cursor_ordinal=moved_ordinal, history_progress_version=r["history_progress_version"] + 1))
    if changed.rowcount != 1:
        raise OrdersJobError("JOB_FENCE_INVALID")
    after = db.read_job(session, snapshot.locator)
    _insert_receipt(session, before=snapshot, after=after, old_attempt=attempt, attempt=a, plan=plan, result=result, at=at)
    return after, a


def _insert_receipt(session, *, before, after, old_attempt, attempt, plan, result, at):
    r = after.row
    session.execute(insert(audit).values(**db.owner(after.locator), event_id=uuid4(), job_version_after=r["version"],
        event_kind="job.chunk_committed", occurred_at=at, actor_kind="delegated_worker", actor_membership_id=None,
        attempt_id=attempt["attempt_id"], before_state=before.row["state"], after_state=r["state"], reason=None,
        attempt_version_before=old_attempt["version"], attempt_version_after=attempt["version"],
        attempt_state_before="claimed", attempt_state_after=attempt["state"], attempt_count_after=r["attempt_count"],
        next_attempt_at=None, lease_expires_at=attempt["lease_expires_at"], result_sync_run_id=r["result_sync_run_id"],
        result_coverage_state=r["result_coverage_state"], history_page_index=before.row["history_cursor_page"],
        history_page_id=UUID(plan.page.page_id), history_first_ordinal=plan.first_ordinal,
        history_next_ordinal=plan.next_ordinal, history_input_checksum=plan.input_checksum,
        history_result_sync_run_id=result.run_id, history_reconciliation_count=result.reconciliation_count))


def read_receipt(session, *, organization_id, marketplace_account_id, sync_run_id):
    """One snapshot and one bounded header; no history-row materialization."""
    for value, maximum in ((organization_id, 2**31 - 1), (marketplace_account_id, 2**31 - 1), (sync_run_id, 2**63 - 1)):
        integer(value, maximum)
    row = session.execute(text("""WITH chosen AS (
 SELECT h.*,p.header_checksum,a.history_first_ordinal,a.history_next_ordinal,a.history_input_checksum,
 a.history_reconciliation_count,r.sync_run_id,r.source_run_key,r.source_snapshot,r.source_contract_version,
 r.payload_checksum,r.state AS run_state,r.manifest_state,r.page_count,r.order_count,r.item_count,r.expected_order_count,
 r.requested_from,r.requested_to
 FROM user_orders_job_audit a JOIN user_orders_history_selection_pages p
 ON (a.organization_id,a.marketplace_account_id,a.job_id,a.history_page_index,a.history_page_id)=
 (p.organization_id,p.marketplace_account_id,p.job_id,p.page_index,p.history_page_id)
 JOIN wb_live_history_pages h ON (h.organization_id,h.marketplace_account_id,h.job_id,h.source,h.page_id)=
 (p.organization_id,p.marketplace_account_id,p.history_job_id,p.history_source,p.history_page_id) AND h.run_id=p.history_run_id
 JOIN order_sync_runs r ON (r.organization_id,r.marketplace_account_id,r.sync_run_id)=
 (a.organization_id,a.marketplace_account_id,a.history_result_sync_run_id)
 WHERE a.organization_id=:org AND a.marketplace_account_id=:account AND a.history_result_sync_run_id=:run
 AND a.event_kind='job.chunk_committed' AND h.state='published' AND r.marketplace='wb'
 AND r.source_kind='wb-statistics-supplier-orders' AND r.adapter_version='wb-statistics-orders-stream-v1'
 AND r.mapping_version='wb-statistics-status-v1' AND r.source_contract_version='wb-history-positive-partial-v1'
 ORDER BY a.job_id,a.history_page_index,a.history_first_ordinal LIMIT 1)
 SELECT chosen.*,EXISTS(SELECT FROM user_orders_job_audit other JOIN user_orders_history_selection_pages p
 ON (other.organization_id,other.marketplace_account_id,other.job_id,other.history_page_index,other.history_page_id)=
 (p.organization_id,p.marketplace_account_id,p.job_id,p.page_index,p.history_page_id)
 WHERE other.organization_id=:org AND other.marketplace_account_id=:account AND other.history_result_sync_run_id=:run
 AND other.event_kind='job.chunk_committed' AND
 (p.history_job_id,p.history_run_id,p.history_page_id,p.header_checksum,other.history_first_ordinal,other.history_next_ordinal,
  other.history_input_checksum,other.history_reconciliation_count) IS DISTINCT FROM
 (chosen.job_id,chosen.run_id,chosen.page_id,chosen.header_checksum,chosen.history_first_ordinal,chosen.history_next_ordinal,
  chosen.history_input_checksum,chosen.history_reconciliation_count)) AS disagreement FROM chosen"""),
        {"org": organization_id, "account": marketplace_account_id, "run": sync_run_id}).mappings().one_or_none()
    if row is None or row["disagreement"]:
        raise OrdersJobError("JOB_CONFLICT")
    page = page_evidence(row)
    first, stop = row["history_first_ordinal"], row["history_next_ordinal"]
    if (hashlib.sha256(history_header_bytes(page)).hexdigest() != row["header_checksum"]
            or stop != min(first + 1000, page.row_count) or first > page.row_count
            or (first == page.row_count and page.row_count != 0)
            or row["payload_checksum"] != row["history_input_checksum"]
            or row["run_state"] != "partial" or row["manifest_state"] != "partial"
            or row["page_count"] != 1 or row["order_count"] != stop - first or row["item_count"] != stop - first
            or row["expected_order_count"] is not None or row["requested_from"] is not None or row["requested_to"] is not None):
        raise OrdersJobError("JOB_CONFLICT")
    return HistoryProjectionReceipt(organization_id, marketplace_account_id, sync_run_id,
        page.job_id, page.run_id, page.page_id, first, stop, row["history_input_checksum"],
        row["source_run_key"], row["source_snapshot"], row["source_contract_version"], page.credential_id,
        page.credential_generation, page.account_incarnation, row["history_reconciliation_count"], "partial")
