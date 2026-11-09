"""Pure initialization, native carry, and pre-HTTP pacing regressions."""
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.wb_live import repository as module
from app.wb_live.contracts import JobLocator, WbLiveError
from app.wb_live.orm import WbLiveSyncJobRow as Job, WbLiveSyncSourceRow as Source

NOW = datetime(2026, 9, 10, tzinfo=UTC)
CURSOR = "2026-09-01T00:00:00.12345"
HISTORY = "wb-statistics-supplier-orders"


@pytest.fixture
def intent(monkeypatch):
    job = Job(organization_id=1, marketplace_account_id=2, job_id=uuid4(), credential_id=uuid4(),
        credential_generation=1, account_incarnation=1, external_account_id="synthetic", credential_ref=None,
        user_id="owner", membership_id=3, session_id="origin", state="partial", created_at=NOW, updated_at=NOW)
    healthy = Source(organization_id=1, marketplace_account_id=2, job_id=job.job_id, source="content", run_id=uuid4(),
        state="completed", checkpoint={"updatedAt": CURSOR, "nmID": 7}, processed=1, revision=1, attempt=0,
        next_due_at=NOW, updated_at=NOW)
    d = SimpleNamespace(job=job, sources=[healthy], requests={}, statements=[], checks=0, quota=NOW + timedelta(hours=2))
    actor = SimpleNamespace(organization_id=1)
    principal = SimpleNamespace(organization_id=1, user_id="owner", membership_id=3, session_id="fresh")
    account = SimpleNamespace(marketplace_account_id=2, ingestion_binding_version=1, external_account_id="synthetic", credential_ref=None)
    metadata = SimpleNamespace(credential_id=job.credential_id, generation=1, expires_at=None, revoked_at=None)
    def scalar(query, params=None):
        sql = str(query.compile(dialect=postgresql.dialect()))
        d.statements.append(sql)
        if "clock_timestamp" in sql: return NOW
        if "max(" in sql: return d.quota
        if "wb_live_sync_jobs" in sql: return d.job
        raise AssertionError(sql)
    def execute(query, params):
        sql = str(query)
        d.statements.append(sql)
        if sql.startswith("SELECT") and "wb_live_history_requests" in sql:
            row = d.requests.get(params["k"])
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))
        if sql.startswith("INSERT INTO wb_live_history_requests"):
            d.requests[params["k"]] = {"job_id": params["j"], "date_from": params["date_from"]}
            return None
        raise AssertionError(sql)
    def scalars(query):
        d.statements.append(str(query.compile(dialect=postgresql.dialect())))
        return d.sources
    def add(row):
        if isinstance(row, Source): d.sources.append(row)
        else: d.job = row
    def revalidate(): d.checks += 1
    session = SimpleNamespace(scalar=scalar, scalars=scalars, execute=execute, add=add, flush=lambda: None)
    repo = module.WbLiveRepository(lambda: None)
    monkeypatch.setattr(repo, "_session", lambda: nullcontext(session))
    monkeypatch.setattr(repo, "_actor_guard", lambda *args: (principal, account, SimpleNamespace(revalidate_before_write=revalidate)))
    monkeypatch.setattr(module, "_get_marketplace_credential_metadata_in_session", lambda *args: metadata)
    d.repo, d.actor, d.principal, d.metadata, d.account = repo, actor, principal, metadata, account
    return d


def test_explicit_history_adds_one_source_without_touching_sibling_or_authority(intent):
    d = intent
    old = dict(d.sources[0].__dict__)
    view = d.repo.create_history_job(d.actor, 2, "history-first-key", date_from=CURSOR)
    added = d.sources[-1]
    assert added.source == HISTORY and added.checkpoint == {"dateFrom": CURSOR}
    assert added.next_due_at == d.quota
    assert dict(d.sources[0].__dict__) == old and d.job.session_id == "origin"
    assert view["jobId"] == str(d.job.job_id) and d.checks > 0
    assert any("wb_live_sync_jobs" in sql and "FOR UPDATE" in sql for sql in d.statements)
    assert any("wb_live_sync_sources" in sql and "FOR UPDATE" in sql for sql in d.statements)


def test_history_key_binds_original_input_and_new_keys_cannot_reset_cursor(intent):
    d = intent
    d.repo.create_history_job(d.actor, 2, "history-first-key", date_from=CURSOR)
    source = d.sources[-1]
    source.state, source.attempt = "running", 3
    before = dict(source.__dict__)
    d.repo.create_history_job(d.actor, 2, "history-first-key", date_from=CURSOR)
    d.repo.create_history_job(d.actor, 2, "history-second-key", date_from=CURSOR)
    assert dict(source.__dict__) == before
    for key in ("history-first-key", "history-third-key"):
        with pytest.raises(WbLiveError, match="WB_SYNC_CONFLICT"):
            d.repo.create_history_job(d.actor, 2, key, date_from="2026-09-02")
    assert dict(source.__dict__) == before


@pytest.mark.parametrize("change,code", [("generation", "WB_BINDING_CHANGED"), ("revoked", "WB_BINDING_CHANGED"),
    ("expired", "WB_BINDING_CHANGED"), ("incarnation", "WB_BINDING_CHANGED"), ("initiator", "WB_ACCESS_DENIED")])
def test_history_does_not_substitute_authority_or_revive_invalid_binding(intent, change, code):
    d = intent
    if change == "generation": d.metadata.generation += 1
    if change == "revoked": d.metadata.revoked_at = NOW
    if change == "expired": d.metadata.expires_at = NOW
    if change == "incarnation": d.account.ingestion_binding_version += 1
    if change == "initiator": d.principal.membership_id += 1
    with pytest.raises(WbLiveError, match=code):
        d.repo.create_history_job(d.actor, 2, "history-first-key", date_from=CURSOR)
    assert len(d.sources) == 1 and not d.requests


def test_history_completed_claim_keeps_cursor_and_reserves_three_hours(monkeypatch):
    source = Source(organization_id=1, marketplace_account_id=2, job_id=uuid4(), source=HISTORY, run_id=uuid4(),
        state="completed", checkpoint={"dateFrom": CURSOR}, processed=100, revision=3, attempt=0,
        lease_token=None, lease_expires_at=None, next_due_at=NOW, updated_at=NOW)
    job = SimpleNamespace(state="partial", credential_id=uuid4(), credential_generation=1, account_incarnation=1,
        job_id=source.job_id)
    values = iter((NOW, source))
    session = SimpleNamespace(scalar=lambda query: next(values))
    repo = module.WbLiveRepository(lambda: None)
    monkeypatch.setattr(repo, "_session", lambda locator: nullcontext(session))
    monkeypatch.setattr(repo, "_job", lambda *args, **kwargs: job)
    monkeypatch.setattr(repo, "_guard", lambda *args: None)
    lease = repo.claim_batch(JobLocator(1, 2, str(source.job_id)))
    assert lease.checkpoint == {"dateFrom": CURSOR}
    assert source.next_due_at == NOW + timedelta(hours=3)


def test_successor_carries_history_but_fresh_default_job_does_not(intent):
    d = intent
    d.repo.create_history_job(d.actor, 2, "history-first-key", date_from=CURSOR)
    d.sources[-1].state = "completed"
    prior = d.job
    with d.repo._session() as s:
        d.repo._new_job(s, d.principal, d.account, d.metadata, previous=prior)
    added = [row for row in d.sources if row.job_id == d.job.job_id]
    assert {row.source for row in added} == {"content", "prices", HISTORY}
    assert next(row for row in added if row.source == HISTORY).checkpoint == {"dateFrom": CURSOR}


def test_downgrade_disables_rls_before_checking_occupancy(monkeypatch):
    """Execute real migration emission; actual non-BYPASS behavior is a PG case."""
    from pathlib import Path
    import runpy
    from alembic import op
    statements = []
    monkeypatch.setattr(op, "execute", statements.append)
    migration = Path(__file__).resolve().parents[1] / "alembic/versions/20260910_0082_wb_history_staging.py"
    runpy.run_path(str(migration))["downgrade"]()
    emitted = "\n".join(statements)
    assert "SET LOCAL row_security=off" in emitted
    assert emitted.index("SET LOCAL row_security=off") < emitted.index("IF EXISTS")
