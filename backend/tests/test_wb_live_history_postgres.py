"""Owned disposable PostgreSQL: source evidence only, never real WB transport."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.infra.db import set_tenant_context, set_marketplace_account_context
from app.platform.integrations.credential_store import reencrypt_credential
from app.wb_live.contracts import JobLocator, WbLiveError
from app.wb_live.orm import WbLiveSyncSourceRow as Source
from app.wb_live.repository import WbLiveRepository
from app.wb_live.statistics_orders import iter_orders_page, orders_request
from tests import test_orders_schema_candidate as candidate
from tests import test_wb_live_platform as platform

cluster = candidate.cluster
pg_store = platform.pg_store
data = platform.data
live = platform.live
SOURCE = "wb-statistics-supplier-orders"
CURSOR = "2026-09-01T00:00:00.12345"


@pytest.fixture(scope="module")
def pg_database(cluster):
    role, worker, dispatcher, api = ("history_" + uuid4().hex for _ in range(4))
    with candidate.disposable_database(cluster, (role, worker, dispatcher, api)) as database:
        # Empty schema only: demonstrate both additive downgrades do not erase data.
        for verb, revision in (("upgrade", "20260910_0082"), ("downgrade", "20260910_0080"), ("upgrade", "20260910_0082")):
            result = candidate.migrate(database.url, verb, revision)
            assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as c:
                c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
                c.exec_driver_sql(f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {role}")
                c.exec_driver_sql(f"GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
                sql = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
                sql = "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))
                for old, new in (("satorna_wb_live", database.name), ("wb_live_owner", database.owner),
                    ("wb_live_worker", worker), ("wb_live_dispatch", dispatcher), ("wb_live_api", api)):
                    sql = sql.replace(old, new)
                c.execute(text(sql))
            runtime._history_worker_role, runtime._history_api_role = worker, api
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def prepare(d):
    # Fresh HTTP start under actual API column/table grants, not a superuser role.
    engine = d.factory.kw["bind"]
    api_engine = create_engine(engine.url.set(username=engine._history_api_role), hide_parameters=True)
    try:
        api_repo = WbLiveRepository(sessionmaker(api_engine, expire_on_commit=False), d.keys)
        view = api_repo.create_history_job(d.actor, d.org, "synthetic-history-first", date_from=CURSOR)
        assert api_repo.create_history_job(d.actor, d.org, "synthetic-history-first", date_from=CURSOR) == view
    finally:
        api_engine.dispose()
    locator = JobLocator(d.org, d.org, view["jobId"])
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source != SOURCE).values(
            next_due_at=datetime.now(UTC) + timedelta(days=1)))
    return locator


def page_events(d, count=2):
    raw = json.dumps([{"srid": f"unit-{n}", "nmId": 12, "isCancel": False,
        "lastChangeDate": "2026-09-02T00:00:00.12345", "barcode": "native"} for n in range(count)]).encode()
    return list(iter_orders_page([raw], organization_id=d.org, marketplace_account_id=d.org,
        date_from=CURSOR, observed_at=datetime.now(UTC)))


def begin(d, lease):
    digest = hashlib.sha256(orders_request(d.org, d.org, lease.checkpoint["dateFrom"])[1]).hexdigest()
    return d.repo.begin_history_page(lease, request_checksum=digest)


def published_count(d):
    with d.engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM wb_live_history_rows r JOIN wb_live_history_pages p "
            "USING(organization_id,marketplace_account_id,job_id,source,page_id) WHERE p.organization_id=:o AND p.state='published'"), {"o": d.org})


def test_real_worker_staging_eof_ack_and_immutability(live):
    d = live
    locator = prepare(d)
    runtime = d.factory.kw["bind"]
    worker = create_engine(runtime.url.set(username=runtime._history_worker_role), hide_parameters=True)
    try:
        d.repo = WbLiveRepository(sessionmaker(worker, expire_on_commit=False), d.keys)
        before = datetime.now(UTC)
        lease = d.repo.claim_batch(locator)
        assert lease.source == SOURCE
        assert d.repo.resolve_for_fetch(lease).secret.reveal()["token"] == "synthetic-guard"
        with d.engine.connect() as c:
            assert c.scalar(text("SELECT next_due_at FROM wb_live_sync_sources WHERE organization_id=:o AND source=:source"),
                {"o": d.org, "source": SOURCE}) >= before + timedelta(seconds=10800)
        page = begin(d, lease)
        assert begin(d, lease) == page
        first, second, end = page_events(d)
        assert d.repo.stage_history_rows(lease, page_id=page, first_ordinal=0, rows=(first,))
        assert published_count(d) == 0
        assert d.repo.stage_history_rows(lease, page_id=page, first_ordinal=0, rows=(first,))
        with pytest.raises(WbLiveError, match="WB_SYNC_CONFLICT"):
            d.repo.stage_history_rows(lease, page_id=page, first_ordinal=0, rows=(replace(first, source_row_checksum="0" * 64),))
        with pytest.raises(WbLiveError):
            d.repo.commit_history_page(lease, page_id=page, end=end, next_due_at=datetime.now(UTC))
        assert published_count(d) == 0
        assert d.repo.stage_history_rows(lease, page_id=page, first_ordinal=1, rows=(second,))
        assert d.repo.commit_history_page(lease, page_id=page, end=end, next_due_at=datetime.now(UTC))
        assert d.repo.commit_history_page(lease, page_id=page, end=end, next_due_at=datetime.now(UTC))
        assert published_count(d) == 2
        with d.engine.connect() as c:
            result = c.execute(text("SELECT processed,revision,state,checkpoint FROM wb_live_sync_sources WHERE organization_id=:o AND source=:source"),
                {"o": d.org, "source": SOURCE}).one()
            assert tuple(result) == (2, 1, "queued", {"dateFrom": end.next_date_from})
        with pytest.raises(WbLiveError, match="WB_SYNC_CONFLICT"):
            d.repo.commit_history_page(lease, page_id=page, end=replace(end, raw_checksum="0" * 64), next_due_at=datetime.now(UTC))
        # Actual column grants and trigger, each attempted mutation rolls back.
        for sql in ("UPDATE wb_live_history_rows SET nm_id=99 WHERE organization_id=:o",
                    "UPDATE wb_live_history_pages SET request_checksum=repeat('0',64) WHERE organization_id=:o",
                    "UPDATE wb_live_history_pages SET receipt=receipt WHERE organization_id=:o"):
            with pytest.raises(DBAPIError), worker.begin() as c:
                c.execute(text("SELECT set_config('app.organization_id',:o,true),set_config('app.marketplace_account_id',:o,true)"), {"o": str(d.org)})
                c.execute(text(sql), {"o": d.org})
    finally:
        worker.dispose()


def test_abandoned_lease_cannot_seal_and_run_refresh_keeps_page_provenance(live):
    d = live
    locator = prepare(d)
    old = d.repo.claim_batch(locator)
    abandoned = begin(d, old)
    first, second, end = page_events(d)
    d.repo.stage_history_rows(old, page_id=abandoned, first_ordinal=0, rows=(first, second))
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == SOURCE).values(
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1), next_due_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert not d.repo.commit_history_page(old, page_id=abandoned, end=end, next_due_at=datetime.now(UTC))
    lease = d.repo.claim_batch(locator)
    assert lease.lease_token != old.lease_token and lease.checkpoint == old.checkpoint
    page = begin(d, lease)
    assert page != abandoned
    (empty,) = page_events(d, 0)
    assert d.repo.commit_history_page(lease, page_id=page, end=empty, next_due_at=datetime.now(UTC))
    with d.engine.begin() as c:
        c.execute(update(Source).where(Source.organization_id == d.org, Source.source == SOURCE).values(next_due_at=datetime.now(UTC) - timedelta(seconds=1)))
    refreshed = d.repo.claim_batch(locator)
    assert refreshed.run_id != lease.run_id and refreshed.checkpoint == {"dateFrom": CURSOR}
    assert d.repo.commit_history_page(lease, page_id=page, end=empty, next_due_at=datetime.now(UTC))
    assert published_count(d) == 0
    with d.engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM wb_live_history_pages WHERE organization_id=:o"), {"o": d.org}) == 2


def test_history_rotation_denies_staging_and_forced_scope_rls(live):
    d = live
    locator = prepare(d)
    lease = d.repo.claim_batch(locator)
    page = begin(d, lease)
    first, second, end = page_events(d)
    d.repo.stage_history_rows(lease, page_id=page, first_ordinal=0, rows=(first, second))
    # Stable ownership FKs do not block ordinary credential generation changes.
    rotated = reencrypt_credential(d.credential.credential_id, d.credential.generation, 8, account_identity=d.owner)
    assert rotated.generation == d.credential.generation + 1
    with pytest.raises(WbLiveError, match="WB_BINDING_CHANGED"):
        d.repo.commit_history_page(lease, page_id=page, end=end, next_due_at=datetime.now(UTC))
    assert published_count(d) == 0
    runtime = d.factory.kw["bind"]
    for org, account in ((None, None), (d.org + 200000, d.org), (d.org, d.org + 100000)):
        with runtime.begin() as c:
            if org is not None:
                c.execute(text("SELECT set_config('app.organization_id',:o,true),set_config('app.marketplace_account_id',:a,true)"),
                    {"o": str(org), "a": str(account)})
            for table in ("wb_live_history_pages", "wb_live_history_rows", "wb_live_history_requests"):
                assert c.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    with d.engine.connect() as c:
        states = c.execute(text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN "
            "('wb_live_history_pages','wb_live_history_rows','wb_live_history_requests')")).all()
        assert states == [(True, True)] * 3


def test_data_bearing_downgrade_fails_closed_for_non_bypass_table_owner(live, monkeypatch):
    from pathlib import Path
    import runpy
    from alembic import op
    d = live
    prepare(d)
    runtime = d.factory.kw["bind"]
    role = runtime.url.username  # Exact disposable role allocated by this fixture.
    tables = ("wb_live_sync_sources", "wb_live_history_pages", "wb_live_history_rows", "wb_live_history_requests")
    with d.engine.begin() as c:
        c.exec_driver_sql(f"GRANT CREATE ON SCHEMA public TO {role}")
        for table in tables:
            c.exec_driver_sql(f"ALTER TABLE public.{table} OWNER TO {role}")
        c.exec_driver_sql(f"ALTER FUNCTION public.wb_live_history_immutable() OWNER TO {role}")
    # Demonstrate the dangerous premise: the owner really has no BYPASSRLS,
    # and absent scope produces zero visible rows despite occupied history.
    with runtime.connect() as c:
        assert c.scalar(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")) is False
        assert c.scalar(text("SELECT count(*) FROM wb_live_sync_sources WHERE source=:source"), {"source": SOURCE}) == 0
    migration = Path(__file__).resolve().parents[1] / "alembic/versions/20260910_0082_wb_history_staging.py"
    downgrade = runpy.run_path(str(migration))["downgrade"]
    with pytest.raises(DBAPIError, match="row.level security"), runtime.begin() as c:
        monkeypatch.setattr(op, "execute", c.exec_driver_sql)
        downgrade()
    with d.engine.connect() as c:
        assert all(c.scalar(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}) for table in tables)
        assert c.scalar(text("SELECT count(*) FROM wb_live_sync_sources WHERE organization_id=:o AND source=:source"),
            {"o": d.org, "source": SOURCE}) == 1
