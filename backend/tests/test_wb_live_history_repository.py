"""Pure real repository boundaries; SQL doubles are not PostgreSQL/RLS proof."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.wb_live.contracts import BatchLease, JobLocator, WbLiveError
from app.wb_live.repository import WbLiveRepository
from app.wb_live.statistics_orders import iter_orders_page, orders_request

NOW = datetime(2026, 9, 10, tzinfo=UTC)
CURSOR = "2026-09-01T00:00:00.12345"
SOURCE = "wb-statistics-supplier-orders"


def events(count=1):
    raw = json.dumps([{"srid": f"unit-{n}", "nmId": 12, "isCancel": False,
        "lastChangeDate": "2026-09-02T00:00:00.12345", "barcode": "native"} for n in range(count)]).encode()
    return list(iter_orders_page([raw], organization_id=1, marketplace_account_id=2,
        date_from=CURSOR, observed_at=NOW))


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)
    def mappings(self):
        return self
    def all(self):
        return self.rows
    def first(self):
        return self.rows[0] if self.rows else None
    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]


@pytest.fixture
def history(monkeypatch):
    lease = BatchLease(JobLocator(1, 2, str(uuid4())), SOURCE, str(uuid4()), uuid4(), 1, 1,
        {"dateFrom": CURSOR}, 1, str(uuid4()), NOW + timedelta(seconds=120))
    source = SimpleNamespace(checkpoint=dict(lease.checkpoint), processed=0, revision=0, attempt=1,
        state="running", error_code=None, updated_at=NOW, next_due_at=NOW + timedelta(hours=3),
        lease_token=UUID(lease.lease_token), lease_expires_at=lease.expires_at)
    job = SimpleNamespace(credential_id=lease.credential_id, credential_generation=1, account_incarnation=1)
    d = SimpleNamespace(lease=lease, source=source, job=job, pages={}, rows={}, sql=[], writes=[], valid=True,
        authority=True, rollups=0)
    def execute(query, params):
        sql = str(query)
        d.sql.append(sql)
        if sql.startswith("SELECT") and "wb_live_history_pages" in sql:
            page = next((p for p in d.pages.values() if all(p[k] == params[k] for k in
                ("o", "a", "j", "source", "run", "lease")) and ("page" not in params or p["page"] == params["page"])), None)
            return Result([] if page is None else [page])
        if sql.startswith("INSERT INTO wb_live_history_pages"):
            d.writes.append(sql)
            page = {**params, "state": "staging", "receipt": None}
            d.pages[params["page"]] = page
            return Result()
        if sql.startswith("SELECT") and "wb_live_history_rows" in sql:
            rows = sorted(d.rows.values(), key=lambda r: r["ordinal"])
            if "count(*)" in sql:
                return Result([{"count": len(rows), "first": rows[0]["ordinal"] if rows else None,
                    "last": rows[-1]["ordinal"] if rows else None}])
            if "ORDER BY ordinal DESC" in sql:
                return Result(rows[-1:])
            return Result([r for r in rows if params.get("first", 0) <= r["ordinal"] <= params.get("last", 100000)])
        if sql.startswith("INSERT INTO wb_live_history_rows"):
            d.writes.append(sql)
            assert isinstance(params, list)
            for row in params:
                d.rows[row["ordinal"]] = row
            return Result()
        if sql.startswith("UPDATE wb_live_history_pages"):
            d.writes.append(sql)
            d.pages[params["page"]].update(state="published", receipt=json.loads(params["receipt"]))
            return Result()
        raise AssertionError(sql)
    session = SimpleNamespace(execute=execute, flush=lambda: None)
    repo = WbLiveRepository(lambda: None)
    monkeypatch.setattr(repo, "_session", lambda locator: nullcontext(session))
    def leased(s, candidate):
        if not d.valid:
            raise WbLiveError("WB_LEASE_LOST")
        if not d.authority:
            raise WbLiveError("WB_BINDING_CHANGED")
        return job, source, NOW
    def guard(*args):
        if not d.authority:
            raise WbLiveError("WB_BINDING_CHANGED")
    monkeypatch.setattr(repo, "_leased", leased)
    monkeypatch.setattr(repo, "_lease_from_guard", lambda s, lease, guard: leased(s, lease))
    monkeypatch.setattr(repo, "_guard", guard)
    monkeypatch.setattr(repo, "_job", lambda *args, **kwargs: job)
    monkeypatch.setattr(repo, "_rollup", lambda *args: setattr(d, "rollups", d.rollups + 1))
    d.repo = repo
    d.checksum = hashlib.sha256(orders_request(1, 2, CURSOR)[1]).hexdigest()
    return d


def begin(d):
    return d.repo.begin_history_page(d.lease, request_checksum=d.checksum)


def test_begin_is_scoped_exact_manifest_idempotent_and_uuid(history):
    d = history
    page = begin(d)
    assert str(UUID(page)) == page and begin(d) == page
    assert len(d.pages) == 1
    for sql in (s for s in d.sql if s.startswith("SELECT")):
        assert all(name in sql for name in ("organization_id", "marketplace_account_id", "job_id", "source", "run_id", "lease_token"))
    with pytest.raises(WbLiveError, match="WB_RESPONSE_INVALID"):
        d.repo.begin_history_page(d.lease, request_checksum="0" * 64)


def test_stage_and_eof_publish_once_exact_replay_after_lease_cleared(history):
    d = history
    row, end = events()
    page = begin(d)
    assert d.repo.stage_history_rows(d.lease, page_id=page, first_ordinal=0, rows=(row,))
    assert d.source.checkpoint == {"dateFrom": CURSOR} and d.source.processed == 0
    assert d.repo.stage_history_rows(d.lease, page_id=page, first_ordinal=0, rows=(row,))
    due = NOW + timedelta(hours=1)
    assert d.repo.commit_history_page(d.lease, page_id=page, end=end, next_due_at=due)
    assert d.source.checkpoint == {"dateFrom": end.next_date_from}
    assert (d.source.processed, d.source.revision, d.source.state) == (1, 1, "queued")
    assert d.source.next_due_at >= NOW + timedelta(hours=3)
    assert d.source.lease_token is None
    d.valid = False
    assert d.repo.commit_history_page(d.lease, page_id=page, end=end, next_due_at=due)
    assert d.rollups == 1 and d.source.processed == 1
    with pytest.raises(WbLiveError, match="WB_SYNC_CONFLICT"):
        d.repo.commit_history_page(d.lease, page_id=page, end=replace(end, raw_checksum="0" * 64), next_due_at=due)
    d.authority = False
    with pytest.raises(WbLiveError, match="WB_BINDING_CHANGED"):
        d.repo.commit_history_page(d.lease, page_id=page, end=end, next_due_at=due)


@pytest.mark.parametrize("mutation", ["raw_hash", "nm_id", "barcode", "owner", "adapter", "revision", "ordinal", "oversized", "list"])
def test_entire_chunk_validated_before_any_row_write(history, mutation):
    d = history
    good, bad, _ = events(2)
    first, rows = 0, None
    if mutation == "raw_hash": bad = replace(bad, source_row_checksum="not-a-hash")
    if mutation == "nm_id": bad = replace(bad, nm_id=True)
    if mutation == "barcode": bad = replace(bad, barcode="x" * 256)
    if mutation == "owner":
        ident = replace(bad.observation.identity, organization_id=9)
        item = replace(bad.observation.items[0], identity=replace(bad.observation.items[0].identity, order_identity=ident))
        bad = replace(bad, observation=replace(bad.observation, identity=ident, items=(item,)))
    if mutation == "adapter": bad = replace(bad, observation=replace(bad.observation, adapter_version="untrusted"))
    if mutation == "revision": bad = replace(bad, observation=replace(bad.observation, source_revision="invalid"))
    if mutation == "ordinal": first = 1
    if mutation == "oversized": rows = (good,) * 1001
    if mutation == "list": rows = [good]
    page = begin(d)
    with pytest.raises(WbLiveError):
        d.repo.stage_history_rows(d.lease, page_id=page, first_ordinal=first, rows=rows if rows is not None else (good, bad))
    assert not d.rows and len(d.writes) == 1


@pytest.mark.parametrize("change", [{"row_count": 2}, {"terminal": True}, {"next_date_from": CURSOR},
    {"raw_checksum": "A" * 64}, {"request_checksum": "0" * 64}, {"row_count": True}, {"next_date_from": "2026-09-03"}])
def test_invalid_eof_never_publishes_or_advances(history, change):
    d = history
    row, end = events()
    page = begin(d)
    d.repo.stage_history_rows(d.lease, page_id=page, first_ordinal=0, rows=(row,))
    with pytest.raises(WbLiveError):
        d.repo.commit_history_page(d.lease, page_id=page, end=replace(end, **change), next_due_at=NOW)
    assert d.pages[UUID(page)]["state"] == "staging"
    assert d.source.checkpoint == {"dateFrom": CURSOR}


def test_empty_eof_completes_source_and_expired_stage_cannot_publish(history):
    d = history
    (end,) = events(0)
    page = begin(d)
    d.valid = False
    assert not d.repo.commit_history_page(d.lease, page_id=page, end=end, next_due_at=NOW)
    assert d.pages[UUID(page)]["state"] == "staging"
    d.valid = True
    assert d.repo.commit_history_page(d.lease, page_id=page, end=end, next_due_at=NOW)
    assert (d.source.state, d.source.processed, d.source.checkpoint) == ("completed", 0, {"dateFrom": CURSOR})


def test_same_ordinal_conflict_and_page_uuid_not_enough(history):
    d = history
    row, _ = events()
    page = begin(d)
    d.repo.stage_history_rows(d.lease, page_id=page, first_ordinal=0, rows=(row,))
    with pytest.raises(WbLiveError, match="WB_SYNC_CONFLICT"):
        d.repo.stage_history_rows(d.lease, page_id=page, first_ordinal=0, rows=(replace(row, source_row_checksum="0" * 64),))
    with pytest.raises(WbLiveError, match="WB_SYNC_CONFLICT"):
        d.repo.stage_history_rows(replace(d.lease, run_id=str(uuid4())), page_id=page, first_ordinal=0, rows=(row,))


def test_fresh_eof_does_not_install_publication_authority_twice(history, monkeypatch):
    d = history
    page = begin(d)
    (end,) = events(0)
    installed = []
    validate_lease = d.repo._leased
    def install(*args):
        assert not installed, "Publication root already has a guard"
        witness = SimpleNamespace(revalidate_before_write=lambda: None)
        installed.append(witness)
        return witness
    def leased_again(s, lease):
        install(s, d.job)
        return validate_lease(s, lease)
    monkeypatch.setattr(d.repo, "_guard", install)
    monkeypatch.setattr(d.repo, "_leased", leased_again)
    monkeypatch.setattr(d.repo, "_lease_from_guard", lambda s, lease, guard: validate_lease(s, lease), raising=False)
    assert d.repo.commit_history_page(d.lease, page_id=page, end=end, next_due_at=NOW)
    assert len(installed) == 1


@pytest.mark.parametrize("borrowed", ["session", "job", "organization", "account"])
def test_lease_validation_rejects_borrowed_guard_witness_before_query(history, monkeypatch, borrowed):
    d = history
    session = object()
    witness = SimpleNamespace(_session_ref=lambda: session, _job_id=UUID(d.lease.locator.job_id),
        _binding=(1, 2), revalidate_before_write=lambda: None)
    if borrowed == "session": witness._session_ref = lambda: object()
    if borrowed == "job": witness._job_id = uuid4()
    if borrowed == "organization": witness._binding = (9, 2)
    if borrowed == "account": witness._binding = (1, 9)
    def queried(*args, **kwargs):
        pytest.fail("borrowed guard reached a protected row query")
    monkeypatch.setattr(d.repo, "_job", queried)
    with pytest.raises(WbLiveError, match="WB_LEASE_LOST"):
        WbLiveRepository._lease_from_guard(d.repo, session, d.lease, witness)
