"""Pure regressions for configuration and native cursor integration boundaries."""
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import runpy
from types import SimpleNamespace
from uuid import uuid4

from alembic import context
from alembic.config import Config
import pytest
from sqlalchemy.engine import make_url

from app.wb_live.contracts import JobLocator
from app.wb_live.orm import WbLiveSyncJobRow as Job, WbLiveSyncSourceRow as Source
from app.wb_live.repository import WbLiveRepository


@pytest.mark.parametrize("url, host", [
    ("postgresql+psycopg://synthetic@/synthetic?host=%2Ftmp%2FWB%20live%25%20socket&port=55432", "/tmp/WB live% socket"),
    ("postgresql+psycopg://synthetic@/synthetic?host=/tmp/socket&port=55432", "/tmp/socket"),
])
def test_alembic_env_preserves_encoded_socket_url_for_sqlalchemy(monkeypatch, url, host):
    """The real env.py must cross ConfigParser interpolation without changing URI encoding."""
    from app import config as settings_module
    config = Config()
    received = {}
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", lambda **kwargs: received.update(kwargs))
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)
    monkeypatch.setattr(settings_module, "get_settings", lambda: SimpleNamespace(database_url=url))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "alembic/env.py"))
    assert config.get_main_option("sqlalchemy.url") == url
    assert config.get_section(config.config_ini_section)["sqlalchemy.url"] == url
    assert received["url"] == url
    assert make_url(received["url"]).query["host"] == host


NATIVE_CURSOR = {"updatedAt": "2026-09-09T21:22:33.123456+03:00", "nmID": 9223372036854000}
NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_successor_job_carries_exact_final_native_content_cursor(monkeypatch):
    saved = Source(organization_id=12, marketplace_account_id=34, job_id=uuid4(), source="content", run_id=uuid4(),
        state="completed", checkpoint=dict(NATIVE_CURSOR), processed=100, revision=4, attempt=0,
        next_due_at=NOW, updated_at=NOW)
    added = []
    session = SimpleNamespace(scalar=lambda query: NOW, add=added.append, flush=lambda: None)
    repo = WbLiveRepository(lambda: None)
    monkeypatch.setattr(repo, "_sources", lambda session, previous: [saved])
    principal = SimpleNamespace(organization_id=12, user_id="synthetic", membership_id=56, session_id="synthetic-login")
    account = SimpleNamespace(marketplace_account_id=34, ingestion_binding_version=1,
        external_account_id="synthetic-seller", credential_ref=None)
    credential = SimpleNamespace(credential_id=uuid4(), generation=1)
    repo._new_job(session, principal, account, credential, previous=object())
    carried = next(row.checkpoint for row in added if isinstance(row, Source) and row.source == "content")
    assert carried == {"updatedAt": "2026-09-09T21:22:33.123456+03:00", "nmID": 9223372036854000}
    assert carried is not saved.checkpoint
    assert next(row.checkpoint for row in added if isinstance(row, Source) and row.source == "prices") == {}


def test_independent_completed_source_claim_carries_exact_final_native_cursor(monkeypatch):
    job = Job(organization_id=12, marketplace_account_id=34, job_id=uuid4(), credential_id=uuid4(),
        credential_generation=1, account_incarnation=1, external_account_id="synthetic-seller", credential_ref=None,
        user_id="synthetic", membership_id=56, session_id="synthetic-login", state="partial", created_at=NOW, updated_at=NOW)
    source = Source(organization_id=12, marketplace_account_id=34, job_id=job.job_id, source="content", run_id=uuid4(),
        state="completed", checkpoint=dict(NATIVE_CURSOR), processed=100, revision=4, attempt=0,
        lease_token=None, lease_expires_at=None, next_due_at=NOW, updated_at=NOW, error_code=None)
    values = iter((NOW, source))  # clock query, then due completed source query
    session = SimpleNamespace(scalar=lambda query: next(values))
    repo = WbLiveRepository(lambda: None)
    monkeypatch.setattr(repo, "_session", lambda locator: nullcontext(session))
    monkeypatch.setattr(repo, "_job", lambda session, locator, lock=False: job)
    monkeypatch.setattr(repo, "_guard", lambda session, job: None)
    lease = repo.claim_batch(JobLocator(12, 34, str(job.job_id)))
    assert lease.checkpoint == {"updatedAt": "2026-09-09T21:22:33.123456+03:00", "nmID": 9223372036854000}
    assert source.checkpoint == lease.checkpoint
