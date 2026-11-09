"""PostgreSQL commits are job delivery evidence. Broker delivery is advisory."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.infra.db import set_tenant_context, set_marketplace_account_context
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner, _load_keyring, _resolve_fetch_in_session,
    _get_marketplace_credential_metadata_in_session, CredentialStoreError,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.integrations.publication_guard import (
    UserSessionPrincipal, ExpectedAccountBinding, ExpectedCredential,
    acquire_publication_guard, PublicationGuardError,
)
from app.wb_live.auth import require_live_actor, acquire_read_job_guard
from app.wb_live.contracts import ACTIVE_STATES, ERROR_CODES, SOURCES, BatchLease, JobLocator, WbLiveError
from app.wb_live.orm import WbLiveSyncJobRow as Job, WbLiveSyncSourceRow as Source
from app.wb_live.orm import WbLiveProductRow as Product, WbLiveProductSizeRow as Size
from app.wb_live import history_repository as history

LEASE_SECONDS = 120
MAX_ATTEMPTS = 8

def _safe(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except WbLiveError as e:
            if e.code == "WB_LEASE_LOST" and fn.__name__ in {"commit_batch", "defer_batch", "fail_batch", "stage_history_rows", "commit_history_page"}:
                return False
            raise
        except PublicationGuardError as e:
            code = "WB_ACCESS_DENIED" if e.code in {"publication_access_denied", "publication_expired"} else "WB_BINDING_CHANGED"
        except CredentialStoreError:
            code = "WB_BINDING_CHANGED"
        except SQLAlchemyError:
            code = "WB_LIVE_UNAVAILABLE"
        raise WbLiveError(code)
    return call

def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WbLiveError("WB_RESPONSE_INVALID")
    return value.astimezone(timezone.utc)

def _checkpoint(source, value):
    if type(value) is not dict or len(json.dumps(value)) > 4096:
        raise WbLiveError("WB_RESPONSE_INVALID")
    if not value:
        return {}
    if source == "prices":
        if set(value) != {"offset"} or type(value["offset"]) is not int or value["offset"] < 0:
            raise WbLiveError("WB_RESPONSE_INVALID")
    else:
        if set(value) != {"updatedAt", "nmID"} or type(value["nmID"]) is not int or value["nmID"] < 0:
            raise WbLiveError("WB_RESPONSE_INVALID")
        _parse_time(value["updatedAt"])
    return dict(value)

def _parse_time(value):
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError, AttributeError):
        raise WbLiveError("WB_RESPONSE_INVALID") from None

class WbLiveRepository:
    def __init__(self, session_factory, keyring_loader=_load_keyring):
        self._factory, self._keyring = session_factory, keyring_loader

    @_safe
    def begin_history_page(self, lease, *, request_checksum):
        return history.begin_history_page(self, lease, request_checksum=request_checksum)

    @_safe
    def stage_history_rows(self, lease, *, page_id, first_ordinal, rows):
        return history.stage_history_rows(self, lease, page_id=page_id, first_ordinal=first_ordinal, rows=rows)

    @_safe
    def commit_history_page(self, lease, *, page_id, end, next_due_at):
        return history.commit_history_page(self, lease, page_id=page_id, end=end, next_due_at=next_due_at)

    @contextmanager
    def _session(self, locator=None):
        with self._factory() as s:
            if not isinstance(s.get_bind(), Engine) or s.get_bind().dialect.name != "postgresql":
                raise WbLiveError()
            with s.begin():
                if locator is not None:
                    set_tenant_context(s, locator.org_id)
                    set_marketplace_account_context(s, organization_id=locator.org_id, marketplace_account_id=locator.account_id)
                yield s

    def _job(self, s, locator, *, lock=False):
        q = select(Job).where(Job.organization_id == locator.org_id, Job.marketplace_account_id == locator.account_id,
                             Job.job_id == UUID(locator.job_id))
        if lock:
            q = q.with_for_update().execution_options(populate_existing=True)
        row = s.scalar(q)
        if row is None:
            raise WbLiveError("WB_SYNC_CONFLICT")
        return row

    def _guard(self, s, job, *, credential=True):
        return acquire_read_job_guard(s, job)

    def _actor_guard(self, s, actor, account_id, *, permission="integrations:write"):
        principal, _ = require_live_actor(s, actor, account_id=account_id, permission=permission)
        a = s.scalar(select(MarketplaceAccountRow).where(MarketplaceAccountRow.organization_id == actor.organization_id,
            MarketplaceAccountRow.marketplace_account_id == account_id, MarketplaceAccountRow.marketplace == "wb"))
        if a is None:
            raise WbLiveError("WB_ACCESS_DENIED")
        guard = acquire_publication_guard(s, principal=principal, required_permissions=frozenset({permission}),
            accounts=(ExpectedAccountBinding(account_id, "wb", a.external_account_id, a.credential_ref),), authorities=())
        set_marketplace_account_context(s, organization_id=actor.organization_id, marketplace_account_id=account_id)
        return principal, a, guard

    def _sources(self, s, job, *, lock=False):
        query = select(Source).where(Source.organization_id == job.organization_id,
            Source.marketplace_account_id == job.marketplace_account_id, Source.job_id == job.job_id).order_by(Source.source)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return list(s.scalars(query))

    def _view(self, s, job, account_id):
        if job is None:
            return {"marketplaceAccountId": account_id, "jobId": None, "state": "idle", "sources": [], "updatedAt": None}
        return {"marketplaceAccountId": account_id, "jobId": str(job.job_id), "state": job.state,
            "sources": [{"source": r.source, "state": r.state, "processed": r.processed,
                         "updatedAt": _utc(r.updated_at), "errorCode": r.error_code} for r in self._sources(s, job)],
            "updatedAt": _utc(job.updated_at)}

    def _new_job(self, s, principal, a, metadata, *, previous=None):
        now = s.scalar(select(func.clock_timestamp()))
        job = Job(organization_id=principal.organization_id, marketplace_account_id=a.marketplace_account_id,
            job_id=uuid4(), credential_id=metadata.credential_id, credential_generation=metadata.generation,
            account_incarnation=a.ingestion_binding_version, external_account_id=a.external_account_id,
            credential_ref=a.credential_ref, user_id=principal.user_id, membership_id=principal.membership_id,
            session_id=principal.session_id, state="queued", created_at=now, updated_at=now)
        old = {} if previous is None else {r.source: r for r in self._sources(s, previous)}
        s.add(job)
        s.flush()
        names = SOURCES + ((history.HISTORY_SOURCE,) if history.HISTORY_SOURCE in old
            and old[history.HISTORY_SOURCE].state == "completed" else ())
        for name in names:
            checkpoint = {}
            if name in {"content", history.HISTORY_SOURCE} and name in old and old[name].state == "completed" and old[name].checkpoint:
                checkpoint = dict(old[name].checkpoint)
            s.add(Source(organization_id=job.organization_id, marketplace_account_id=job.marketplace_account_id,
                job_id=job.job_id, source=name, run_id=uuid4(), state="queued", checkpoint=checkpoint,
                processed=0, revision=0, attempt=0, next_due_at=max(now, old[name].next_due_at) if name in old else now,
                updated_at=now))
        s.flush()
        return job

    @_safe
    def create_history_job(self, actor, account_id, idempotency_key, *, date_from):
        history.validate_date_from(date_from)
        if type(idempotency_key) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
            raise WbLiveError("WB_SYNC_CONFLICT")
        with self._session() as s:
            principal, a, guard = self._actor_guard(s, actor, account_id)
            request = s.execute(text("SELECT job_id,date_from FROM wb_live_history_requests WHERE "
                "organization_id=:o AND marketplace_account_id=:a AND idempotency_key=:k"),
                {"o": actor.organization_id, "a": account_id, "k": idempotency_key}).mappings().first()
            if request is not None:
                if request["date_from"] != date_from:
                    raise WbLiveError("WB_SYNC_CONFLICT")
                return self._view(s, self._job(s, JobLocator(actor.organization_id, account_id, str(request["job_id"]))), account_id)
            latest = s.scalar(select(Job).where(Job.organization_id == actor.organization_id, Job.marketplace_account_id == account_id)
                .order_by(Job.created_at.desc(), Job.job_id.desc()).limit(1).with_for_update().execution_options(populate_existing=True))
            metadata = _get_marketplace_credential_metadata_in_session(s,
                MarketplaceAccountCredentialOwner(actor.organization_id, account_id, "wb"), "wb_api")
            now = s.scalar(select(func.clock_timestamp()))
            if metadata is None or metadata.revoked_at is not None or (metadata.expires_at is not None and metadata.expires_at <= now):
                raise WbLiveError("WB_BINDING_CHANGED")
            sources = [] if latest is None else self._sources(s, latest, lock=True)
            prior = next((r for r in sources if r.source == history.HISTORY_SOURCE), None)
            active = latest is not None and latest.state in ACTIVE_STATES
            if active or prior is not None:
                if (latest.credential_id != metadata.credential_id or latest.credential_generation != metadata.generation
                        or latest.account_incarnation != a.ingestion_binding_version or latest.external_account_id != a.external_account_id
                        or latest.credential_ref != a.credential_ref):
                    raise WbLiveError("WB_BINDING_CHANGED")
                if (latest.user_id, latest.membership_id) != (principal.user_id, principal.membership_id):
                    raise WbLiveError("WB_ACCESS_DENIED")
            if prior is not None:
                if prior.error_code in {"WB_ACCESS_DENIED", "WB_BINDING_CHANGED"}:
                    raise WbLiveError(prior.error_code)
                if prior.checkpoint != {"dateFrom": date_from}:
                    raise WbLiveError("WB_SYNC_CONFLICT")
            # Account lock from the original live HTTP guard serializes all starts.
            # Preserve the greatest reserved provider floor, even across job IDs.
            floor = s.scalar(select(func.max(Source.next_due_at)).where(Source.organization_id == actor.organization_id,
                Source.marketplace_account_id == account_id, Source.source == history.HISTORY_SOURCE))
            job = latest if active else self._new_job(s, principal, a, metadata, previous=latest)
            current = prior if active else next((r for r in self._sources(s, job) if r.source == history.HISTORY_SOURCE), None)
            if current is None:
                current = Source(organization_id=actor.organization_id, marketplace_account_id=account_id, job_id=job.job_id,
                    source=history.HISTORY_SOURCE, run_id=uuid4(), state="queued", checkpoint={"dateFrom": date_from},
                    processed=0, revision=0, attempt=0, next_due_at=max(now, floor) if floor is not None else now, updated_at=now)
                s.add(current)
                job.state, job.updated_at = "queued" if job.state == "completed" else job.state, now
            elif current.state == "failed":
                # Only an explicit fresh request of its original authority retries.
                current.state, current.attempt, current.error_code = "queued", 0, None
                current.lease_token = current.lease_expires_at = None
                current.next_due_at, current.updated_at = max(now, current.next_due_at, floor or now), now
                job.updated_at = now
            s.flush()
            s.execute(text("INSERT INTO wb_live_history_requests(organization_id,marketplace_account_id,idempotency_key,job_id,date_from) "
                "VALUES(:o,:a,:k,:j,:date_from)"),
                {"o": actor.organization_id, "a": account_id, "k": idempotency_key, "j": job.job_id, "date_from": date_from})
            guard.revalidate_before_write()
            return self._view(s, job, account_id)

    @_safe
    def create_job(self, actor, account_id, idempotency_key):
        if type(idempotency_key) is not str or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
            raise WbLiveError("WB_SYNC_CONFLICT")
        with self._session() as s:
            principal, a, guard = self._actor_guard(s, actor, account_id)
            old_id = s.scalar(text("SELECT job_id FROM wb_live_sync_requests WHERE organization_id=:o AND marketplace_account_id=:a AND idempotency_key=:k"),
                {"o": actor.organization_id, "a": account_id, "k": idempotency_key})
            if old_id:
                return self._view(s, self._job(s, JobLocator(actor.organization_id, account_id, str(old_id))), account_id)
            latest = s.scalar(select(Job).where(Job.organization_id == actor.organization_id, Job.marketplace_account_id == account_id)
                .order_by(Job.created_at.desc(), Job.job_id.desc()).limit(1).with_for_update().execution_options(populate_existing=True))
            metadata = _get_marketplace_credential_metadata_in_session(s,
                MarketplaceAccountCredentialOwner(actor.organization_id, account_id, "wb"), "wb_api")
            now = s.scalar(select(func.clock_timestamp()))
            if metadata is None or metadata.revoked_at is not None or (metadata.expires_at is not None and metadata.expires_at <= now):
                raise WbLiveError("WB_BINDING_CHANGED")
            job = latest if latest is not None and latest.state in ACTIVE_STATES else None
            if job is not None and (job.credential_id != metadata.credential_id or job.credential_generation != metadata.generation
                    or job.account_incarnation != a.ingestion_binding_version or job.external_account_id != a.external_account_id
                    or job.credential_ref != a.credential_ref):
                raise WbLiveError("WB_BINDING_CHANGED")
            if job is not None and job.state == "partial":
                failed = [source for source in self._sources(s, job, lock=True) if source.state == "failed"]
                if failed:
                    for source in failed:
                        if source.error_code in {"WB_ACCESS_DENIED", "WB_BINDING_CHANGED"}:
                            raise WbLiveError(source.error_code)
                    # Preserve the installed live HTTP guard and the job's original
                    # authority. A new login of its same user/membership may retry;
                    # another principal cannot replace that durable authorization.
                    if (job.user_id, job.membership_id) != (principal.user_id, principal.membership_id):
                        raise WbLiveError("WB_ACCESS_DENIED")
                    for source in failed:
                        source.state, source.attempt, source.error_code = "queued", 0, None
                        source.lease_token = source.lease_expires_at = None
                        source.next_due_at, source.updated_at = max(source.next_due_at, now), now
                    job.updated_at = now
            if job is None:
                job = self._new_job(s, principal, a, metadata, previous=latest)
            s.execute(text("INSERT INTO wb_live_sync_requests(organization_id,marketplace_account_id,idempotency_key,job_id) VALUES(:o,:a,:k,:j)"),
                {"o": actor.organization_id, "a": account_id, "k": idempotency_key, "j": job.job_id})
            guard.revalidate_before_write()
            return self._view(s, job, account_id)

    @_safe
    def status(self, actor, account_id):
        with self._session() as s:
            self._actor_guard(s, actor, account_id, permission="sync:read")
            job = s.scalar(select(Job).where(Job.organization_id == actor.organization_id, Job.marketplace_account_id == account_id)
                .order_by(Job.created_at.desc(), Job.job_id.desc()).limit(1))
            return self._view(s, job, account_id)

    @_safe
    def due_jobs(self, limit=100):
        with self._session() as s:
            return [JobLocator(r.org_id, r.account_id, str(r.job_id)) for r in s.execute(
                text("SELECT * FROM public.wb_live_due_jobs(:limit)"), {"limit": max(0, min(int(limit), 100))})]

    @_safe
    def claim_batch(self, locator):
        try:
            with self._session(locator) as s:
                job = self._job(s, locator)
                self._guard(s, job)
                job = self._job(s, locator, lock=True)
                now = s.scalar(select(func.clock_timestamp()))
                if job.state == "completed":
                    latest = s.scalar(select(Job.job_id).where(Job.organization_id == locator.org_id,
                        Job.marketplace_account_id == locator.account_id).order_by(Job.created_at.desc(), Job.job_id.desc()).limit(1))
                    if latest != job.job_id or not any(r.next_due_at <= now for r in self._sources(s, job)):
                        return None
                    a = s.scalar(select(MarketplaceAccountRow).where(MarketplaceAccountRow.marketplace_account_id == locator.account_id))
                    meta = _get_marketplace_credential_metadata_in_session(s,
                        MarketplaceAccountCredentialOwner(locator.org_id, locator.account_id, "wb"), "wb_api")
                    job = self._new_job(s, UserSessionPrincipal(job.organization_id, job.user_id, job.membership_id, job.session_id), a, meta, previous=job)
                    locator = JobLocator(locator.org_id, locator.account_id, str(job.job_id))
                    now = s.scalar(select(func.clock_timestamp()))
                if job.state not in ACTIVE_STATES:
                    return None
                r = s.scalar(select(Source).where(Source.organization_id == locator.org_id,
                    Source.marketplace_account_id == locator.account_id, Source.job_id == UUID(locator.job_id),
                    Source.state.in_(("queued", "running", "completed")), Source.next_due_at <= now,
                    (Source.lease_expires_at.is_(None) | (Source.lease_expires_at <= now)))
                    .order_by(Source.next_due_at, Source.source).limit(1).with_for_update())
                if r is None:
                    return None
                if r.state == "completed":
                    checkpoint = {}
                    if r.source in {"content", history.HISTORY_SOURCE} and r.checkpoint:
                        checkpoint = dict(r.checkpoint)
                    r.run_id, r.checkpoint, r.processed, r.attempt = uuid4(), checkpoint, 0, 0
                    r.revision += 1
                if r.attempt >= MAX_ATTEMPTS:
                    r.state, r.error_code = "failed", "WB_RETRY_EXHAUSTED"
                    r.lease_token = r.lease_expires_at = None
                    self._rollup(s, job, now)
                    return None
                r.attempt += 1
                r.lease_token, r.lease_expires_at = uuid4(), now + timedelta(seconds=LEASE_SECONDS)
                # Reserve provider pacing before HTTP, including worker death.
                r.next_due_at = now + timedelta(seconds=history.HISTORY_INTERVAL if r.source == history.HISTORY_SOURCE else (900 if r.source == "prices" else .6))
                r.state, r.updated_at, r.error_code = "running", now, None
                job.state, job.updated_at = "running", now
                return BatchLease(locator, r.source, str(r.lease_token), job.credential_id, job.credential_generation,
                    job.account_incarnation, dict(r.checkpoint), r.attempt, str(r.run_id), _utc(r.lease_expires_at))
        except (PublicationGuardError, WbLiveError) as e:
            if isinstance(e, WbLiveError) and e.code not in {"WB_BINDING_CHANGED", "WB_ACCESS_DENIED"}:
                raise
            self._invalidate(locator, "WB_BINDING_CHANGED" if getattr(e, "code", "") != "publication_access_denied" else "WB_ACCESS_DENIED")
            return None

    def _invalidate(self, locator, code):
        # Metadata-only terminal bookkeeping. Never authorizes provider fetch or facts.
        with self._session(locator) as s:
            job = self._job(s, locator, lock=True)
            now = s.scalar(select(func.clock_timestamp()))
            for r in self._sources(s, job):
                if r.state != "completed":
                    r.state, r.error_code, r.updated_at = "failed", code, now
                    r.lease_token = r.lease_expires_at = None
            job.state, job.updated_at = "failed", now

    def _leased(self, s, lease):
        job = self._job(s, lease.locator)
        guard = self._guard(s, job)
        return self._lease_from_guard(s, lease, guard)

    def _lease_from_guard(self, s, lease, guard):
        # A transaction-bound witness, not an authorization-skipping boolean.
        # Revalidation rejects detached, replaced, poisoned or expired contexts.
        session_ref = getattr(guard, "_session_ref", None)
        if (not callable(session_ref) or session_ref() is not s
                or getattr(guard, "_job_id", None) != UUID(lease.locator.job_id)
                or getattr(guard, "_binding", ())[:2] != (lease.locator.org_id, lease.locator.account_id)):
            raise WbLiveError("WB_LEASE_LOST")
        guard.revalidate_before_write()
        job = self._job(s, lease.locator, lock=True)
        r = s.scalar(select(Source).where(Source.organization_id == lease.locator.org_id,
            Source.marketplace_account_id == lease.locator.account_id, Source.job_id == UUID(lease.locator.job_id),
            Source.source == lease.source).with_for_update().execution_options(populate_existing=True))
        now = s.scalar(select(func.clock_timestamp()))
        if (r is None or r.lease_token != UUID(lease.lease_token) or r.state != "running"
                or r.lease_expires_at <= now or job.credential_id != lease.credential_id
                or job.credential_generation != lease.generation or job.account_incarnation != lease.account_incarnation
                or r.checkpoint != lease.checkpoint or r.run_id != UUID(lease.run_id)):
            raise WbLiveError("WB_LEASE_LOST")
        return job, r, now

    @_safe
    def resolve_for_fetch(self, lease):
        with self._session(lease.locator) as s:
            self._leased(s, lease)
            return _resolve_fetch_in_session(s, MarketplaceAccountCredentialOwner(lease.locator.org_id,
                lease.locator.account_id, "wb"), "wb_api", self._keyring())

    def _rollup(self, s, job, now):
        s.flush()
        rows = self._sources(s, job)
        if all(r.state == "completed" for r in rows):
            job.state = "completed"
        elif all(r.state == "failed" for r in rows):
            job.state = "failed"
        elif any(r.processed or r.state == "failed" for r in rows):
            job.state = "partial"
        else:
            job.state = "queued"
        job.updated_at = now

    @_safe
    def commit_batch(self, lease, *, rows, next_checkpoint, complete, page_digest, next_due_at):
        prepared = _rows(lease.source, rows)
        checkpoint = _checkpoint(lease.source, next_checkpoint)
        if type(complete) is not bool or not re.fullmatch(r"[0-9a-f]{64}", page_digest):
            raise WbLiveError("WB_RESPONSE_INVALID")
        with self._session(lease.locator) as s:
            job, r, now = self._leased(s, lease)
            for product, sizes in prepared:
                owner = {"organization_id": lease.locator.org_id, "marketplace_account_id": lease.locator.account_id}
                values = {**owner, **product, f"{lease.source}_updated_at": now}
                stmt = insert(Product).values(**values)
                update = {k: v for k, v in values.items() if k not in {*owner, "nm_id"}}
                condition = None
                if lease.source == "content":
                    condition = Product.source_updated_at.is_(None) | (Product.source_updated_at <= product["source_updated_at"])
                saved = s.scalar(stmt.on_conflict_do_update(index_elements=[Product.organization_id, Product.marketplace_account_id, Product.nm_id],
                    set_=update, where=condition).returning(Product.nm_id))
                if saved is None:
                    continue
                for size in sizes:
                    values = {**owner, "nm_id": product["nm_id"], **size, f"{lease.source}_updated_at": now}
                    stmt = insert(Size).values(**values)
                    s.execute(stmt.on_conflict_do_update(index_elements=[Size.organization_id, Size.marketplace_account_id, Size.nm_id, Size.chrt_id],
                        set_={k: v for k, v in values.items() if k not in {*owner, "nm_id", "chrt_id"}}))
            s.execute(text("INSERT INTO wb_live_pages(organization_id,marketplace_account_id,job_id,source,run_id,lease_token,page_digest,checkpoint,row_count) "
                "VALUES(:o,:a,:j,:source,:run,:lease,:digest,CAST(:checkpoint AS jsonb),:count)"),
                {"o": lease.locator.org_id, "a": lease.locator.account_id, "j": UUID(lease.locator.job_id), "source": lease.source,
                 "run": UUID(lease.run_id), "lease": UUID(lease.lease_token), "digest": page_digest, "checkpoint": json.dumps(lease.checkpoint), "count": len(prepared)})
            r.checkpoint, r.processed, r.revision, r.attempt = checkpoint, r.processed + len(prepared), r.revision + 1, 0
            r.state, r.error_code, r.updated_at = ("completed" if complete else "queued"), None, now
            minimum = 900 if lease.source == "prices" else (60 if complete else 0.6)
            r.next_due_at = max(_utc(next_due_at), now + timedelta(seconds=minimum))
            r.lease_token = r.lease_expires_at = None
            self._rollup(s, job, now)
            return True

    @_safe
    def defer_batch(self, lease, *, error_code, next_due_at):
        return self._transition(lease, error_code, next_due_at, False)

    @_safe
    def fail_batch(self, lease, *, error_code):
        return self._transition(lease, error_code, datetime.now(timezone.utc), True)

    def _transition(self, lease, code, next_due, terminal):
        if code not in ERROR_CODES or code in {"WB_LEASE_LOST", "WB_SYNC_CONFLICT"}:
            code = "WB_PROVIDER_UNAVAILABLE"
        with self._session(lease.locator) as s:
            job, r, now = self._leased(s, lease)
            terminal = terminal or r.attempt >= MAX_ATTEMPTS
            r.state, r.error_code, r.updated_at = ("failed" if terminal else "queued"), code, now
            minimum = history.HISTORY_INTERVAL if lease.source == history.HISTORY_SOURCE else (900 if lease.source == "prices" else .6)
            r.next_due_at = max(_utc(next_due), now + timedelta(seconds=max(minimum, 2 ** min(r.attempt, 8))))
            r.lease_token = r.lease_expires_at = None
            self._rollup(s, job, now)
            return True

def _rows(source, rows):
    if source not in SOURCES or type(rows) not in (list, tuple) or len(rows) > (100 if source == "content" else 1000):
        raise WbLiveError("WB_RESPONSE_INVALID")
    mapping = {"vendorCode": "vendor_code", "title": "title", "brand": "brand", "subjectId": "subject_id",
        "subjectName": "subject_name", "photoUrl": "photo_url", "discountPct": "discount_pct", "clubDiscountPct": "club_discount_pct"}
    size_map = {"techSize": "tech_size", "skus": "skus", "priceKopecks": "price_kopecks",
        "discountedPriceKopecks": "discounted_price_kopecks", "clubPriceKopecks": "club_price_kopecks"}
    permitted = ({"nmId", "sizes", "vendorCode", "title", "brand", "subjectId", "subjectName", "photoUrl", "sourceUpdatedAt"}
        if source == "content" else {"nmId", "sizes", "vendorCode", "discountPct", "clubDiscountPct"})
    size_permitted = {"chrtId", "techSize", "skus"} if source == "content" else {"chrtId", "techSize", "priceKopecks", "discountedPriceKopecks", "clubPriceKopecks"}
    result, ids, total_sizes = [], set(), 0
    def integer(v):
        if type(v) is not int or not 0 < v <= 2**63 - 1:
            raise WbLiveError("WB_RESPONSE_INVALID")
        return v
    for row in rows:
        if type(row) is not dict or set(row) - permitted or "nmId" not in row or type(row.get("sizes")) is not list:
            raise WbLiveError("WB_RESPONSE_INVALID")
        nm_id = integer(row["nmId"])
        if nm_id in ids:
            raise WbLiveError("WB_RESPONSE_INVALID")
        ids.add(nm_id)
        product = {"nm_id": nm_id}
        for key, col in mapping.items():
            if key not in row:
                continue
            value = row[key]
            if value is not None:
                if key in {"subjectId", "discountPct", "clubDiscountPct"}:
                    if type(value) is not int or value < 0 or value > (2**63-1 if key == "subjectId" else 100):
                        raise WbLiveError("WB_RESPONSE_INVALID")
                elif type(value) is not str or len(value) > 4096 or "\x00" in value:
                    raise WbLiveError("WB_RESPONSE_INVALID")
            product[col] = value
        if source == "content":
            if "sourceUpdatedAt" not in row:
                raise WbLiveError("WB_RESPONSE_INVALID")
            product["source_updated_at"] = _parse_time(row["sourceUpdatedAt"])
        sizes, size_ids = [], set()
        for size in row["sizes"]:
            total_sizes += 1
            if total_sizes > 10000 or type(size) is not dict or set(size) - size_permitted or "chrtId" not in size:
                raise WbLiveError("WB_RESPONSE_INVALID")
            sid = integer(size["chrtId"])
            if sid in size_ids:
                raise WbLiveError("WB_RESPONSE_INVALID")
            size_ids.add(sid)
            shaped = {"chrt_id": sid}
            for key, col in size_map.items():
                if key not in size:
                    continue
                value = size[key]
                if value is not None:
                    if key.endswith("Kopecks"):
                        if type(value) is not int or not 0 <= value <= 2**63 - 1:
                            raise WbLiveError("WB_RESPONSE_INVALID")
                    elif key == "skus":
                        if type(value) is not list or len(value) > 500 or any(type(v) is not str or len(v) > 255 for v in value):
                            raise WbLiveError("WB_RESPONSE_INVALID")
                    elif type(value) is not str or len(value) > 255:
                        raise WbLiveError("WB_RESPONSE_INVALID")
                shaped[col] = value
            sizes.append(shaped)
        result.append((product, sizes))
    return result
