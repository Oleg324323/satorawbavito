from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from app import wb_report_budgets as budgets
from app.wb_api import ads_runtime
from app.wb_api.client import FakeWbApiClient
from tests.test_wb_report_reads import redis_process  # Disposable isolated Redis only.


@pytest.fixture
def runtime(monkeypatch, redis_process):
    redis_process.flushdb()
    cache, queue = {}, []
    monkeypatch.setattr(budgets, 'get_redis_client', lambda: redis_process)
    monkeypatch.setattr(budgets, 'get_source_cache', lambda org, key, **kw: deepcopy(cache.get((org, key))))
    monkeypatch.setattr(budgets, 'save_source_cache', lambda org, key, value, **kw: cache.update({(org, key): deepcopy(value)}))
    client = FakeWbApiClient(fixtures={'/adv/v1/budget': {'cash': 12, 'netting': 3, 'total': 15}})
    monkeypatch.setattr(ads_runtime, 'build_wb_ads_client', lambda **kw: client)
    return SimpleNamespace(cache=cache, queue=queue, client=client,
                           enqueue=lambda *args: queue.append(args), token='synthetic-budget-token', redis=redis_process)


def start(runtime, ids=(11, 12)):
    return budgets.start(7, 'user', budgets.connection(runtime.token), ids, runtime.enqueue)


def step(runtime, call=None, token=None):
    org, user, digest, owner, sequence, _ = call or runtime.queue.pop(0)
    return budgets.run_step(org, user, digest, owner, sequence,
                            lambda *args: token or runtime.token, runtime.enqueue)


def test_bounded_provider_batch_and_one_delayed_continuation(runtime):
    assert start(runtime, (11, 12, 13, 14, 15))['state'] == 'queued'
    assert runtime.client.requests == []
    assert step(runtime)['state'] == 'running'
    assert len(runtime.client.requests) == 4 and len(runtime.queue) == 1
    assert runtime.queue[0][-1] >= 1
    assert step(runtime)['state'] == 'completed'
    assert len(runtime.client.requests) == 5 and runtime.queue == []
    assert budgets.recent(7, budgets.connection(runtime.token))['11']['totalKopecks'] == 1500


def test_deduplicates_active_jobs_and_old_deliveries(runtime):
    start(runtime)
    first = runtime.queue[0]
    start(runtime)
    assert len(runtime.queue) == 1
    step(runtime)
    assert step(runtime, first)['state'] == 'superseded'
    assert len(runtime.client.requests) == 2


def test_429_retries_are_delayed_bounded_and_old_sequence_ignored(runtime):
    runtime.client.errors['/adv/v1/budget'] = 429
    start(runtime, (11,))
    first = runtime.queue[0]
    assert step(runtime)['state'] == 'running'
    assert runtime.queue[0][-1] >= 20
    assert step(runtime, first)['state'] == 'superseded'
    assert step(runtime)['state'] == 'running'
    assert step(runtime)['state'] == 'failed'
    assert len(runtime.client.requests) == 3 and runtime.queue == []


@pytest.mark.parametrize('status', [401, 403])
def test_auth_failure_stops_optional_collection(runtime, status):
    runtime.client.errors['/adv/v1/budget'] = status
    start(runtime)
    assert step(runtime)['state'] == 'failed'
    assert len(runtime.client.requests) == 1 and runtime.queue == []


def test_connection_change_stops_before_any_provider_read(runtime):
    start(runtime)
    assert step(runtime, token='different-synthetic-token')['state'] == 'failed'
    assert runtime.client.requests == []


def test_deadline_stops_collection_and_overlay_preserves_historical_fields(runtime):
    start(runtime)
    _, job_key, _ = budgets.keys(7, budgets.connection(runtime.token))
    runtime.cache[7, job_key]['deadline'] = 0
    assert step(runtime)['state'] == 'failed'
    report = {'rows': [{'campaignId': '11', 'adSpendKopecks': 16128796, 'impressions': 900}],
              'performanceComplete': True, 'cache': {'fresh': True},
              'kpis': [{'id': 'ad_spend', 'value': '16128796'}, {'id': 'campaign_budget', 'value': '999'}]}
    output = budgets.overlay(7, budgets.connection(runtime.token), report)
    assert output['rows'][0]['adSpendKopecks'] == 16128796
    assert output['rows'][0]['budgetTotalKopecks'] is None
    assert output['cache'] == report['cache'] and output['performanceComplete'] is True
    assert output['kpis'][0] == report['kpis'][0]
    assert report['kpis'][1]['value'] == '999'  # No mutation of saved report.


def test_budget_cache_is_org_connection_scoped_and_has_ttl(runtime):
    start(runtime, (11,)); step(runtime)
    digest = budgets.connection(runtime.token)
    assert budgets.recent(8, digest) == budgets.recent(7, 'other') == {}
    cache_key, _, _ = budgets.keys(7, digest)
    runtime.cache[7, cache_key]['budgets']['11']['fetchedAt'] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    assert budgets.recent(7, digest) == {}


def test_complete_current_cache_never_enqueues_provider_reads(runtime):
    start(runtime, (11,)); step(runtime)
    assert start(runtime, (11,))['state'] == 'completed'
    assert runtime.queue == [] and len(runtime.client.requests) == 1


def test_incomplete_200_is_not_successfully_cached(runtime):
    runtime.client.fixtures['/adv/v1/budget'] = {}
    start(runtime, (11,))
    assert step(runtime)['state'] == 'failed'
    assert budgets.recent(7, budgets.connection(runtime.token)) == {}


def test_second_period_campaigns_can_follow_completed_shared_job(runtime):
    start(runtime, (11,))
    start(runtime, (12,))
    assert len(runtime.queue) == 1
    step(runtime)
    incomplete = budgets.overlay(7, budgets.connection(runtime.token), {'rows': [{'campaignId': '12'}]})
    assert incomplete['budgetRefresh'] == {'state': 'completed', 'collected': 0, 'total': 1, 'updatedAt': incomplete['budgetRefresh']['updatedAt']}
    assert start(runtime, (12,))['state'] == 'queued'
    step(runtime)
    assert len(budgets.recent(7, budgets.connection(runtime.token))) == 2


def test_complete_requested_budgets_override_unrelated_failed_job(runtime):
    start(runtime, (11,)); step(runtime)
    runtime.client.errors['/adv/v1/budget'] = 403
    start(runtime, (12,)); step(runtime)
    report = budgets.overlay(7, budgets.connection(runtime.token), {'rows': [{'campaignId': '11'}]})
    assert report['budgetRefresh'] == {'state': 'completed', 'collected': 1, 'total': 1}


def test_optional_enqueue_failure_is_visible_without_secret_text(runtime):
    def fail(*args):
        raise RuntimeError('synthetic-secret-must-not-be-persisted')
    job = budgets.start(7, 'user', budgets.connection(runtime.token), [11], fail)
    assert job['state'] == 'failed'
    assert 'synthetic-secret' not in str(runtime.cache)


def test_imported_report_without_campaign_ids_is_unavailable_not_endless_idle(runtime):
    report = budgets.overlay(7, budgets.connection(runtime.token), {
        'rows': [{'campaignId': 'unknown'}, {'campaignId': 'unallocated'}],
        'kpis': [{'id': 'campaign_budget', 'value': '—'}]})
    assert report['budgetRefresh']['state'] == 'failed'
    assert report['budgetRefresh']['total'] == 0
    assert 'ID кампаний' in report['budgetRefresh']['error']
    assert report['kpis'][0]['value'] == '—'
    assert runtime.client.requests == []


def test_http_budget_reads_never_call_provider_and_enforce_permission(runtime, monkeypatch):
    from app.routers import wb_reports_bff as bff
    from app import repricer_tasks
    actor = SimpleNamespace(organization_id=7, user_id='user')
    checked = []
    monkeypatch.setattr(bff, 'actor_from_request', lambda req: actor)
    monkeypatch.setattr(bff, 'assert_permission_or_audit', lambda **kw: checked.append(kw['permission']))
    monkeypatch.setattr(bff, 'get_ads_report_cache', lambda **kw: {'rows': [{'campaignId': '11'}]})
    monkeypatch.setattr(repricer_tasks, '_report_refresh_wb_token', lambda *args: runtime.token)
    monkeypatch.setattr(repricer_tasks, '_enqueue_report_budget_step', runtime.enqueue)
    output = bff.get_optional_ads_budgets(SimpleNamespace(method='POST'), '2026-09-01', '2026-09-07')
    assert output['budgetRefresh']['state'] == 'queued' and checked == ['settings:read']
    assert runtime.client.requests == []
    bff.get_optional_ads_budgets(SimpleNamespace(method='GET'), '2026-09-01', '2026-09-07')
    assert len(runtime.queue) == 2  # Independent current balance and budget jobs.


def test_shared_campaign_budgets_are_counted_once_and_partial_zero_is_not_missing(runtime):
    start(runtime, (11, 11)); step(runtime)
    assert len(runtime.client.requests) == 1
    report = {'rows': [{'campaignId': '11', 'nmId': 101}, {'campaignId': '11', 'nmId': 102}, {'campaignId': '12'}],
              'kpis': [{'id': 'campaign_budget'}]}
    result = budgets.overlay(7, budgets.connection(runtime.token), report)
    assert result['kpis'][0]['value'] == '1500'
    assert 'Частичная сумма' in result['kpis'][0]['hint']
    assert result['budgetRefresh']['total'] == 2
    assert result['rows'][2]['budgetTotalKopecks'] is None
    runtime.client.fixtures['/adv/v1/budget'] = {'total': 0}
    start(runtime, (12,)); step(runtime)
    result = budgets.overlay(7, budgets.connection(runtime.token), report)
    assert result['rows'][2]['budgetTotalKopecks'] == 0
    assert result['kpis'][0]['value'] == '1500'
    assert result['budgetRefresh']['state'] == 'completed'


def test_stale_budget_remains_visible_with_timestamp_while_refresh_is_queued(runtime):
    start(runtime, (11,)); step(runtime)
    digest = budgets.connection(runtime.token)
    cache, _, _ = budgets.keys(7, digest)
    observed = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    runtime.cache[7, cache]['budgets']['11']['fetchedAt'] = observed
    start(runtime, (11,))
    report = budgets.overlay(7, digest, {'rows': [{'campaignId': '11'}], 'kpis': [{'id': 'campaign_budget'}]})
    assert report['rows'][0]['budgetTotalKopecks'] == 1500
    assert report['rows'][0]['budgetFetchedAt'] == observed
    assert report['rows'][0]['budgetFresh'] is False
    assert report['budgetRefresh']['state'] == 'queued'
    assert report['budgetRefresh']['collected'] == 0
    assert 'устаревшие' in report['kpis'][0]['hint']


def test_balance_is_current_connection_scoped_and_independent_of_report_period(runtime):
    runtime.client.fixtures['/adv/v1/balance'] = {'balance': 0, 'net': 100, 'bonus': 20,
        'cashbacks': [{'sum': 10, 'percent': 30, 'expiration_date': '2099-01-01'}]}
    digest = budgets.connection(runtime.token)
    budgets.start(7, 'user', digest, [], runtime.enqueue, include_balance=True)
    budgets.start(7, 'user', digest, [], runtime.enqueue, include_balance=True)
    assert len(runtime.queue) == 1 and runtime.client.requests == []
    assert step(runtime)['state'] == 'completed'
    for dates in [('2026-10-01', '2026-10-08'), ('2026-09-01', '2026-09-07')]:
        output = budgets.overlay(7, digest, {'filters': {'dateRange': {'from': dates[0], 'to': dates[1]}}})
        assert output['balanceRefresh']['fresh'] is True
        assert output['cabinetBalance']['balanceKopecks'] == 0
        kpi = next(item for item in output['kpis'] if item['id'] == 'cabinet_balance')
        assert kpi['value'] == '0'  # Do not add net, bonuses or cashbacks.
        assert 'счёт: 100.00' in kpi['hint'] and 'бонусы: 20.00' in kpi['hint']
        assert output['balanceRefresh']['fetchedAt'] in kpi['hint']
    budgets.start(7, 'user', digest, [], runtime.enqueue, include_balance=True)
    assert runtime.queue == [] and len(runtime.client.requests) == 1
    assert budgets.balance_snapshot(8, digest).get('value') is None
    assert budgets.balance_snapshot(7, 'different-connection').get('value') is None


def test_balance_failure_does_not_stop_budgets_or_erase_stale_balance(runtime):
    digest = budgets.connection(runtime.token)
    cache, _ = budgets.balance_keys(7, digest)
    runtime.cache[7, cache] = {'value': {'balanceKopecks': 1200},
        'fetchedAt': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    runtime.client.errors['/adv/v1/balance'] = 403
    budgets.start(7, 'user', digest, [11], runtime.enqueue, include_balance=True)
    assert step(runtime)['state'] == 'failed'
    assert step(runtime)['state'] == 'completed'
    report = budgets.overlay(7, digest, {'rows': [{'campaignId': '11', 'adSpendKopecks': 123}]})
    assert report['cabinetBalance']['balanceKopecks'] == 1200
    assert report['balanceRefresh']['fresh'] is False
    assert '403' in report['balanceRefresh']['error']
    assert report['rows'][0]['budgetTotalKopecks'] == 1500
    assert report['rows'][0]['adSpendKopecks'] == 123


def test_balance_retries_preserve_delay_and_ignore_old_delivery_without_releasing_lease(runtime, monkeypatch):
    from app.wb_api.client import WbApiResponseEnvelope, RateLimitInfo
    digest = budgets.connection(runtime.token)
    runtime.client.request = lambda request: WbApiResponseEnvelope(request=request, ok=False,
        statusCode=429, rateLimit=RateLimitInfo(retryAfterSeconds=170))
    budgets.start(7, 'user', digest, [], runtime.enqueue, include_balance=True)
    first = runtime.queue[0]
    assert step(runtime)['state'] == 'queued'
    assert runtime.queue[0][-1] >= 170
    assert step(runtime, first)['state'] == 'superseded'
    _, lock = budgets.balance_keys(7, digest)
    assert runtime.redis.exists(lock)
    assert step(runtime)['state'] == 'queued'
    assert step(runtime)['state'] == 'failed'
    assert not runtime.redis.exists(lock)
