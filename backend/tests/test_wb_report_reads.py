from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import shutil
import socket
import subprocess
import time
from types import SimpleNamespace
import pytest
from redis import Redis
from starlette.requests import Request
from app.wb_api import report_reads as reads
from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope, RateLimitInfo
from app.wb_api.source_freshness import source_is_fresh


@pytest.fixture(scope="module")
def redis_process():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([shutil.which("redis-server"), "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = Redis(host="127.0.0.1", port=port)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except Exception:
                time.sleep(.02)
        assert client.ping()
        yield client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture
def runtime(redis_process, monkeypatch):
    from app.wb_api import report_rate_limit
    redis_process.flushdb()  # Only the disposable process created above.
    cache = {}
    monkeypatch.setattr(reads, "get_redis_client", lambda: redis_process)
    monkeypatch.setattr(report_rate_limit, "get_redis_client", lambda: redis_process)
    monkeypatch.setattr(reads, "get_source_cache", lambda org, key, **kw: cache.get((org, key)))
    monkeypatch.setattr(reads, "save_source_cache", lambda org, key, value, **kw: cache.setdefault((org, key), value))
    return cache


def test_distributed_budget_spaces_different_requests_across_clients(runtime):
    from app.wb_api.report_rate_limit import ReportReadBudget
    from app.wb_api.client import WbRateLimitPolicy
    policy = WbRateLimitPolicy(category='test', periodSeconds=1, limit=100, intervalMs=80, burst=10)
    def acquire(_):
        budget = ReportReadBudget(17, 'synthetic', 'https://synthetic.invalid', policy)
        assert budget.acquire(lambda: None) is None
        return time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        sent = sorted(pool.map(acquire, range(3)))
    assert all(b - a >= .065 for a, b in zip(sent, sent[1:]))


def test_distributed_cooldown_is_scoped_and_never_shortened(runtime):
    from app.wb_api.report_rate_limit import ReportReadBudget
    from app.wb_api.client import WbRateLimitPolicy
    policy = WbRateLimitPolicy(category='test', periodSeconds=1, limit=100, intervalMs=0, burst=10)
    budget = ReportReadBudget(17, 'synthetic', 'https://synthetic.invalid', policy)
    req = WbApiRequest(method='GET', path='/test')
    budget.observe(WbApiResponseEnvelope(request=req, ok=False, statusCode=429,
        rateLimit=RateLimitInfo(retryAfterSeconds=600)))
    budget.observe(WbApiResponseEnvelope(request=req, ok=True, statusCode=200,
        rateLimit=RateLimitInfo(retryAfterSeconds=1)))
    assert budget.acquire(lambda: None).retryAfterSeconds >= 599
    for org, credential, host in [(18, 'synthetic', 'https://synthetic.invalid'),
                                  (17, 'different', 'https://synthetic.invalid'),
                                  (17, 'synthetic', 'https://other.invalid')]:
        assert ReportReadBudget(org, credential, host, policy).acquire(lambda: None) is None


def test_distributed_wait_can_be_cancelled_without_reserving_future_slots(runtime):
    from app.wb_api.report_rate_limit import ReportReadBudget
    from app.wb_api.client import WbRateLimitPolicy
    policy = WbRateLimitPolicy(category='test', periodSeconds=1, limit=100, intervalMs=80, burst=10)
    budget = ReportReadBudget(17, 'synthetic', 'https://synthetic.invalid', policy)
    budget.acquire(lambda: None)
    calls = []
    def heartbeat():
        calls.append(1)
        if len(calls) == 2:
            raise reads.ReportDemandEnded()
    with pytest.raises(reads.ReportDemandEnded):
        budget.acquire(heartbeat)
    assert budget.acquire(lambda: None) is None


def test_positive_server_remaining_caps_but_never_refills_local_budget(runtime, redis_process):
    from app.wb_api.report_rate_limit import ReportReadBudget
    from app.wb_api.client import WbRateLimitPolicy
    policy = WbRateLimitPolicy(category='test', periodSeconds=1, limit=100, intervalMs=0, burst=10)
    budget = ReportReadBudget(17, 'synthetic', 'https://synthetic.invalid', policy)
    budget.acquire(lambda: None)
    request = WbApiRequest(method='GET', path='/test')
    for remaining in (2, 8):
        budget.observe(WbApiResponseEnvelope(request=request, ok=True, statusCode=200,
            rateLimit=RateLimitInfo(remaining=remaining)))
        assert float(redis_process.hget(budget.key, 'tokens')) == 2


def test_real_client_report_path_shares_429_between_different_campaigns(runtime):
    import httpx
    from app.wb_api.client import RealWbApiClient, RateLimitedWbApiClient
    calls = []
    def send(req):
        calls.append(req.url)
        return httpx.Response(429, headers={'X-Ratelimit-Retry': '60'}, json={'message': 'limited'})
    inner = RealWbApiClient(INNER.base_url, INNER.token, transport=httpx.MockTransport(send))
    client = RateLimitedWbApiClient(inner)
    context = reads.active_report_read.set(session())
    try:
        for campaign in (1, 2):
            req = WbApiRequest(method='GET', path='/adv/v1/budget', query={'id': campaign})
            result = client.request(req)
            assert result.statusCode == 429
            assert result.request == req  # Never reuse another campaign's response payload.
        assert len(calls) == 1
    finally:
        reads.active_report_read.reset(context)


def request(consumer="c" * 32, view="v" * 32, sequence=1):
    return Request({"type": "http", "headers": [(key.encode(), str(value).encode()) for key, value in {
        "x-wb-consumer": consumer, "x-wb-view": view, "x-wb-sequence": sequence,
    }.items()]})


ACTOR = SimpleNamespace(organization_id=17, user_id="synthetic")
INNER = SimpleNamespace(token="synthetic-test-credential", base_url="https://synthetic.invalid", timeout_seconds=10)


def session(org=17, job="one", token=INNER.token):
    return reads.ReportReadSession(org, sha256(token.encode()).hexdigest(), job)


def response(req, value=1):
    return WbApiResponseEnvelope(request=req, ok=True, statusCode=200, data={"value": value})


def test_switch_cancels_old_view_and_old_poll_cannot_revive_it(runtime):
    reads.touch_demand(request(), ACTOR, "one", start=True)
    session().check()
    reads.touch_demand(request(view="n" * 32, sequence=2), ACTOR, "two", start=True)
    reads.touch_demand(request(), ACTOR, "one")
    reads.touch_demand(request(sequence=1), ACTOR, "one", start=True)
    reads.release_demand(request(), ACTOR)
    with pytest.raises(reads.ReportDemandEnded):
        session().check()
    session(job="two").check()


def test_another_consumer_keeps_shared_job_alive_and_tenants_are_isolated(runtime):
    reads.touch_demand(request(), ACTOR, "one", start=True)
    reads.touch_demand(request(consumer="b" * 32), ACTOR, "one", start=True)
    reads.release_demand(request(), ACTOR)
    session().check()
    reads.release_demand(request(consumer="b" * 32), SimpleNamespace(organization_id=99, user_id="synthetic"))
    session().check()
    reads.release_demand(request(consumer="b" * 32), ACTOR)
    with pytest.raises(reads.ReportDemandEnded):
        session().check()


def test_closed_browser_lease_expires(runtime, redis_process):
    reads.touch_demand(request(), ACTOR, "one", start=True)
    key = reads._demand_key(17)
    member = reads._member(ACTOR.user_id, "c" * 32)
    value = json.loads(redis_process.hget(key, member))
    value["jobs"]["one"] = 0
    redis_process.hset(key, member, json.dumps(value))
    with pytest.raises(reads.ReportDemandEnded):
        session().check()


def test_same_screen_can_need_multiple_reports_and_release_blocks_late_start(runtime):
    reads.touch_demand(request(), ACTOR, "one", start=True)
    reads.touch_demand(request(sequence=2), ACTOR, "two", start=True)
    session().check()
    session(job="two").check()
    reads.release_demand(request(), ACTOR)
    reads.touch_demand(request(sequence=3), ACTOR, "one", start=True)
    for job in ("one", "two"):
        with pytest.raises(reads.ReportDemandEnded):
            session(job=job).check()
    reads.touch_demand(request(view="n" * 32, sequence=4), ACTOR, "two", start=True)
    session(job="two").check()


def test_release_before_first_start_cannot_be_revived(runtime):
    reads.release_demand(request(), ACTOR)
    reads.touch_demand(request(), ACTOR, "one", start=True)
    with pytest.raises(reads.ReportDemandEnded):
        session().check()


@pytest.mark.parametrize("wrapper", ["_request_or_raise_calendar", "_request_or_raise_finance_report",
    "_request_or_raise_stock_report", "_request_or_raise_statistics_report",
    "_request_or_raise_sales_funnel_products"])
def test_report_wrappers_do_not_retry_429_or_wait_before_cached_client(runtime, monkeypatch, wrapper):
    from app import repricer_bff
    from fastapi import HTTPException
    req = WbApiRequest(method="GET", path="/adv/v3/fullstats")
    calls = []
    client = SimpleNamespace(request=lambda req: (calls.append(req) or WbApiResponseEnvelope(
        request=req, ok=False, statusCode=429, rateLimit=RateLimitInfo(retryAfterSeconds=60))))
    monkeypatch.setattr(repricer_bff.time, "sleep", lambda _: pytest.fail("must not sleep/retry"))
    context = reads.active_report_read.set(session())
    try:
        with pytest.raises(HTTPException):
            getattr(repricer_bff, wrapper)(client, req)
        assert len(calls) == 1
    finally:
        reads.active_report_read.reset(context)


@pytest.mark.parametrize("recovers", [True, False])
def test_report_ads_fullstats_retries_429_cooperatively_with_bounded_budget(runtime, monkeypatch, recovers):
    from app import repricer_bff
    from app.wb_api import ads_runtime
    from fastapi import HTTPException
    req = WbApiRequest(method="GET", path="/adv/v3/fullstats")
    calls, delays = [], []
    def invoke(request):
        calls.append(request)
        if recovers and len(calls) == 2:
            return WbApiResponseEnvelope(request=req, ok=True, statusCode=200, data=[{"advertId": 1}])
        return WbApiResponseEnvelope(request=req, ok=False, statusCode=429,
                                    rateLimit=RateLimitInfo(retryAfterSeconds=2))
    monkeypatch.setattr(ads_runtime.time, "sleep", delays.append)
    context = reads.active_report_read.set(session())
    try:
        if recovers:
            assert repricer_bff._request_or_raise_ads_fullstats(SimpleNamespace(request=invoke), req) == [{"advertId": 1}]
            assert len(calls) == 2 and sum(delays) == repricer_bff._ADS_FULLSTATS_MIN_INTERVAL_S
        else:
            with pytest.raises(HTTPException):
                repricer_bff._request_or_raise_ads_fullstats(SimpleNamespace(request=invoke), req)
            assert len(calls) == ads_runtime.ADS_RETRY_MAX_ATTEMPTS
            assert sum(delays) == repricer_bff._ADS_FULLSTATS_MIN_INTERVAL_S * (len(calls) - 1)
    finally:
        reads.active_report_read.reset(context)


def test_shared_request_is_fetched_once_and_checkpoint_survives_view_switch(runtime):
    calls = []
    req = WbApiRequest(method="GET", path="/adv/v3/fullstats", query={"ids": "1"})
    def invoke(heartbeat):
        heartbeat()
        calls.append(1)
        time.sleep(.04)
        return response(req)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(session(job=job).request, INNER, req, invoke) for job in ("one", "two")]
        assert [f.result().data for f in futures] == [{"value": 1}, {"value": 1}]
    assert len(calls) == 1
    assert len(runtime) == 1
    reads.touch_demand(request(), ACTOR, "one", start=True)
    reads.touch_demand(request(view="n" * 32, sequence=2), ACTOR, "two", start=True)
    with pytest.raises(reads.ReportDemandEnded):
        session().request(INNER, req, invoke)
    assert session(job="two").request(INNER, req, invoke).ok
    assert len(calls) == 1


def test_exact_request_org_and_credential_isolation(runtime):
    calls = []
    req = WbApiRequest(method="GET", path="/adv/v3/fullstats", query={"ids": "1"})
    invoke = lambda heartbeat: (calls.append(1) or response(req))
    session().request(INNER, req, invoke)
    session(org=18).request(INNER, req, invoke)
    other = SimpleNamespace(**{**vars(INNER), "token": "another-synthetic-credential"})
    session(token=other.token).request(other, req, invoke)
    changed = req.model_copy(update={"query": {"ids": "2"}})
    session().request(INNER, changed, lambda beat: (calls.append(1) or response(changed)))
    assert len(calls) == 4
    assert INNER.token not in json.dumps(list(runtime))


def test_429_is_not_retried_and_success_requires_database_commit(runtime, monkeypatch):
    req = WbApiRequest(method="GET", path="/adv/v1/budget", query={"id": 1})
    calls = []
    def limited(beat):
        calls.append(1)
        return WbApiResponseEnvelope(request=req, ok=False, statusCode=429, rateLimit=RateLimitInfo(retryAfterSeconds=60))
    assert session().request(INNER, req, limited).statusCode == 429
    assert session(job="two").request(INNER, req, limited).statusCode == 429
    assert calls == [1]
    assert not runtime
    monkeypatch.setattr(reads, "save_source_cache", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB unavailable")))
    other = req.model_copy(update={"query": {"id": 2}})
    with pytest.raises(RuntimeError, match="DB unavailable"):
        session().request(INNER, other, lambda beat: response(other))


def test_task_status_is_never_frozen_in_hourly_checkpoint(runtime):
    req = WbApiRequest(method="GET", path="/api/v1/paid_storage/tasks/synthetic/status")
    calls = []
    for _ in range(2):
        session().request(INNER, req, lambda beat: (calls.append(1) or response(req)))
    assert calls == [1, 1]
    assert not runtime


def test_rejects_business_writes_and_changed_credentials(runtime):
    req = WbApiRequest(method="POST", path="/adv/v1/budget")
    with pytest.raises(RuntimeError, match="Unsupported"):
        session().request(INNER, req, lambda _: pytest.fail("must not send"))
    with pytest.raises(RuntimeError, match="connection changed"):
        session(token="different").request(INNER, req, lambda _: pytest.fail("must not send"))


def test_source_freshness_revalidates_recent_and_historical_corrections():
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    fresh = {"dateTo": "2026-10-05", "fetchedAt": (now - timedelta(minutes=59)).isoformat()}
    assert source_is_fresh(fresh, now=now)
    stale = {**fresh, "fetchedAt": (now - timedelta(hours=2)).isoformat()}
    assert not source_is_fresh(stale, now=now)
    assert source_is_fresh({**stale, "dateTo": "2026-08-01"}, now=now)
    assert not source_is_fresh({**fresh, "dateTo": "2026-08-01", "fetchedAt": (now - timedelta(days=2)).isoformat()}, now=now)
    assert not source_is_fresh({}, now=now)


def test_stock_readiness_requires_fresh_actual_stock_snapshot(monkeypatch):
    from app.routers import wb_reports_bff as reports
    from datetime import date
    stock = {}
    monkeypatch.setattr(reports, 'get_source_cache', lambda *a, **kw: stock)
    start, end = date(2026, 8, 1), date(2026, 8, 7)
    missing = lambda: reports._report_missing_source_ranges(17, 'stocks', date_from=start, date_to=end, fresh_only=True)
    assert missing() == [(start, end)]
    stock.update(count=0, fetchedAt=datetime.now(timezone.utc).isoformat())
    assert missing() == []  # A confirmed empty snapshot is legitimate.
    stock['fetchedAt'] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert missing() == [(start, end)]


def test_changed_source_invalidates_built_report_and_wow_loads_both_periods(monkeypatch):
    from app.routers import wb_reports_bff as reports
    from datetime import date
    start, end = date(2026, 8, 17), date(2026, 8, 23)
    assert reports._report_source_bounds('week-over-week', start, end) == (date(2026, 8, 10), end)
    built = datetime.now(timezone.utc)
    cache = {'completedAt': built.isoformat(), 'dateFrom': str(start), 'dateTo': str(end),
             'report': {'cacheVersion': reports.STOCK_REPORT_PAYLOAD_VERSION}}
    metadata = []
    monkeypatch.setattr(reports, 'get_source_cache_fetched_at', lambda *a, **kw: None)
    monkeypatch.setattr(reports, 'list_source_cache_ranges_by_prefix', lambda *a, **kw: metadata)
    assert reports._report_payload_cache_is_usable('stock', cache, organization_id=17)
    metadata.append({'dateFrom': str(start), 'dateTo': str(end), 'fetchedAt': (built + timedelta(seconds=1)).isoformat()})
    assert not reports._report_payload_cache_is_usable('stock', cache, organization_id=17)
