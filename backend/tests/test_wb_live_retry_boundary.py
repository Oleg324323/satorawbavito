"""Pure retry state/lock regressions; DB concurrency is not simulated here."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.wb_live import repository as module
from app.wb_live.contracts import WbLiveError
from app.wb_live.orm import WbLiveSyncJobRow as Job, WbLiveSyncSourceRow as Source

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


@pytest.fixture
def retry(monkeypatch):
    job = Job(organization_id=12, marketplace_account_id=34, job_id=uuid4(), credential_id=uuid4(),
        credential_generation=3, account_incarnation=2, external_account_id="synthetic-seller", credential_ref=None,
        user_id="synthetic", membership_id=56, session_id="original-login", state="partial", created_at=NOW, updated_at=NOW)
    healthy = Source(organization_id=12, marketplace_account_id=34, job_id=job.job_id, source="content", run_id=uuid4(),
        state="completed", checkpoint={"updatedAt": "2026-09-09T21:22:33+03:00", "nmID": 123}, processed=100,
        revision=2, attempt=0, lease_token=None, lease_expires_at=None, next_due_at=NOW, updated_at=NOW, error_code=None)
    failed = Source(organization_id=12, marketplace_account_id=34, job_id=job.job_id, source="prices", run_id=uuid4(),
        state="failed", checkpoint={"offset": 1000}, processed=1000, revision=1, attempt=8,
        lease_token=uuid4(), lease_expires_at=NOW, next_due_at=NOW + timedelta(minutes=15), updated_at=NOW,
        error_code="WB_RETRY_EXHAUSTED")
    actor = SimpleNamespace(organization_id=12, user_id="synthetic", session_id="fresh-login")
    principal = SimpleNamespace(organization_id=12, user_id="synthetic", membership_id=56, session_id="fresh-login")
    account = SimpleNamespace(marketplace_account_id=34, ingestion_binding_version=2, external_account_id="synthetic-seller", credential_ref=None)
    credential = SimpleNamespace(credential_id=job.credential_id, generation=3, revoked_at=None, expires_at=None)
    state = SimpleNamespace(job=job, healthy=healthy, failed=failed, actor=actor, principal=principal, account=account,
        credential=credential, requests={}, sql=[], guard_checks=0)
    def scalar(query, parameters=None):
        sql = str(query.compile(dialect=postgresql.dialect()))
        state.sql.append(sql)
        if "wb_live_sync_requests" in sql:
            return state.requests.get(parameters["k"])
        if "clock_timestamp" in sql:
            return NOW
        if "wb_live_sync_jobs" in sql:
            return job
        raise AssertionError("Unexpected query")
    def scalars(query):
        state.sql.append(str(query.compile(dialect=postgresql.dialect())))
        return [healthy, failed]
    def execute(query, parameters):
        state.sql.append(str(query))
        assert str(query).startswith("INSERT INTO wb_live_sync_requests")
        state.requests[parameters["k"]] = parameters["j"]
    def revalidate():
        state.guard_checks += 1
        return NOW
    session = SimpleNamespace(scalar=scalar, scalars=scalars, execute=execute, flush=lambda: None, add=lambda row: None)
    repo = module.WbLiveRepository(lambda: None)
    monkeypatch.setattr(repo, "_session", lambda: nullcontext(session))
    monkeypatch.setattr(repo, "_actor_guard", lambda session, actor, account_id: (principal, account, SimpleNamespace(revalidate_before_write=revalidate)))
    monkeypatch.setattr(module, "_get_marketplace_credential_metadata_in_session", lambda *args: credential)
    state.repo = repo
    return state


def snapshot(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def test_new_request_rearms_only_failed_source_and_preserves_durable_progress(retry):
    d = retry
    healthy_before = snapshot(d.healthy)
    old_run, old_checkpoint = d.failed.run_id, dict(d.failed.checkpoint)
    view = d.repo.create_job(d.actor, 34, "new-retry-key")
    assert view["jobId"] == str(d.job.job_id)
    assert d.failed.state == "queued"
    assert (d.failed.attempt, d.failed.error_code, d.failed.lease_token, d.failed.lease_expires_at) == (0, None, None, None)
    assert (d.failed.run_id, d.failed.checkpoint, d.failed.processed, d.failed.revision) == (old_run, old_checkpoint, 1000, 1)
    assert d.failed.next_due_at == NOW + timedelta(minutes=15)
    assert snapshot(d.healthy) == healthy_before
    assert (d.job.user_id, d.job.membership_id, d.job.session_id) == ("synthetic", 56, "original-login")
    assert d.guard_checks >= 1
    assert any("wb_live_sync_jobs" in sql and "FOR UPDATE" in sql for sql in d.sql)
    assert any("wb_live_sync_sources" in sql and "ORDER BY" in sql and "FOR UPDATE" in sql for sql in d.sql)


def test_exact_idempotency_readback_never_rearms_failed_source(retry):
    d = retry
    d.requests["original-request-key"] = d.job.job_id
    before = snapshot(d.failed)
    assert d.repo.create_job(d.actor, 34, "original-request-key")["jobId"] == str(d.job.job_id)
    assert snapshot(d.failed) == before


def test_later_new_request_does_not_reset_already_queued_or_running_work(retry):
    d = retry
    d.repo.create_job(d.actor, 34, "first-retry-key")
    d.failed.attempt = 1
    before = snapshot(d.failed)
    d.repo.create_job(d.actor, 34, "second-retry-key")
    assert snapshot(d.failed) == before
    d.failed.state = "running"
    d.failed.lease_token, d.failed.lease_expires_at = uuid4(), NOW + timedelta(seconds=120)
    before = snapshot(d.failed)
    d.repo.create_job(d.actor, 34, "third-retry-key")
    assert snapshot(d.failed) == before


@pytest.mark.parametrize("mutation,code", [("generation", "WB_BINDING_CHANGED"), ("expired", "WB_BINDING_CHANGED"),
    ("revoked", "WB_BINDING_CHANGED"), ("incarnation", "WB_BINDING_CHANGED"), ("identity", "WB_BINDING_CHANGED"),
    ("initiator", "WB_ACCESS_DENIED")])
def test_invalid_binding_or_original_authority_cannot_rearm(retry, mutation, code):
    d = retry
    if mutation == "generation":
        d.credential.generation += 1
    elif mutation == "expired":
        d.credential.expires_at = NOW - timedelta(seconds=1)
    elif mutation == "revoked":
        d.credential.revoked_at = NOW
    elif mutation == "incarnation":
        d.account.ingestion_binding_version += 1
    elif mutation == "identity":
        d.account.external_account_id = "different-seller"
    else:
        d.principal.membership_id += 1
    before = snapshot(d.failed)
    with pytest.raises(WbLiveError, match=code):
        d.repo.create_job(d.actor, 34, "denied-retry-key")
    assert snapshot(d.failed) == before


@pytest.mark.parametrize("code", ["WB_ACCESS_DENIED", "WB_BINDING_CHANGED"])
def test_persisted_access_or_binding_failure_is_not_rearmed_by_new_key(retry, code):
    d = retry
    d.failed.error_code = code
    before = snapshot(d.failed)
    with pytest.raises(WbLiveError, match=code):
        d.repo.create_job(d.actor, 34, "invalid-source-retry")
    assert snapshot(d.failed) == before
