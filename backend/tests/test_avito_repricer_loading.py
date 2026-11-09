from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from app.avito.auth import scoped_avito_cache_key
from app.avito.listings import AvitoListingRow, AvitoListingsError, AvitoListingsFetchRequest, AvitoListingsFetchResult, LiveAvitoListingsClient
from app.avito.stats import AvitoStatsAccount
from app.routers import avito_repricer as route


def test_successful_listing_fetch_and_retry_after_failure_reset_analytics_error(monkeypatch):
    client = LiveAvitoListingsClient('synthetic')
    monkeypatch.setattr(client, '_http_client', lambda: nullcontext(None))
    monkeypatch.setattr(client, '_accounts', lambda *_: [AvitoStatsAccount(accountId='account', accountName='Synthetic')])
    monkeypatch.setattr(client, '_items', lambda *_: [{'itemId': 'item', 'accountId': 'account', 'title': 'Synthetic', 'status': 'active', 'price': 100}])
    def failed(*_):
        raise RuntimeError('Synthetic analytics unavailable')
    request = AvitoListingsFetchRequest(dateFrom=date(2026, 10, 1), dateTo=date(2026, 10, 8))
    monkeypatch.setattr(client, '_v2_item_analytics_for_account', failed)
    assert client.fetch_listings(request).status == 'partial'
    monkeypatch.setattr(client, '_v2_item_analytics_for_account', lambda *_: {
        'item': {'_source': 'v2_item_analytics', 'views': 100, 'contactsMessenger': 0, 'allSpending': 500}})
    for _ in range(2):
        result = client.fetch_listings(request)
        assert result.status == 'synced' and result.error is None
        assert result.rows[0].views == 100
        assert result.rows[0].contactsMessenger == 0
        assert result.rows[0].spendKopecks == 500


def test_partial_cache_does_not_hide_missing_statistics_error():
    saved = {'status': 'partial', 'rows': [], 'source': {'error': {
        'code': 'rate_limited', 'retryable': True, 'blockerIds': ['AVITO_RATE_LIMIT']}}}
    result = route._cache_hit_payload(saved)
    assert result['status'] == 'partial'
    assert result['source']['error']['code'] == 'rate_limited'
    assert saved['source']['error']['code'] == 'rate_limited'


@pytest.fixture
def repricer(monkeypatch):
    cache, writes, fetches, oauth = {}, [], [], []
    credentials = SimpleNamespace(client_id='synthetic-client', client_secret='synthetic-secret')
    monkeypatch.setattr(route, 'actor_from_request', lambda _: SimpleNamespace(user_id='synthetic-user', organization_id=17))
    monkeypatch.setattr(route, 'has_permission', lambda *_: True)
    monkeypatch.setattr(route, 'get_user_avito_credentials_secret', lambda _: credentials)
    monkeypatch.setattr(route, 'get_source_cache', lambda org, key, **_: cache.get((org, key)))
    def save(org, key, value):
        cache[org, key] = value
        writes.append((key, value))
    monkeypatch.setattr(route, 'save_source_cache', save)
    token = ['synthetic-token']
    def resolve(**_):
        oauth.append(1)
        return token[0]
    monkeypatch.setattr(route, 'resolve_user_avito_access_token', resolve)
    row = AvitoListingRow(itemId='item', accountId='account', accountName='Synthetic', title='Synthetic',
                          status='active', priceKopecks=10000, views=100, contacts=5, contactsMessenger=0,
                          spendKopecks=500, orders=2, sourceStatus='fresh')
    client = SimpleNamespace(result=AvitoListingsFetchResult(status='synced', rows=[row],
                                                            accounts=[AvitoStatsAccount(accountId='account', accountName='Synthetic')]))
    def fetch(request):
        fetches.append(request)
        return client.result
    client.fetch_listings = fetch
    monkeypatch.setattr(route, 'build_avito_listings_client', lambda **_: client)
    def get(force=False):
        return route.get_avito_repricer(Request({'type': 'http', 'headers': []}), date(2026, 10, 1), date(2026, 10, 8),
                                        period_days=8, account_id=[], force_refresh=force, include_analytics=False)
    scope = credentials.client_id + '\0' + credentials.client_secret
    return SimpleNamespace(cache=cache, writes=writes, fetches=fetches, oauth=oauth, client=client, get=get, token=token, credentials=credentials,
                           scope=scope, key=scoped_avito_cache_key(route._cache_key(date(2026, 10, 1), date(2026, 10, 8), []), scope))


def test_cached_statistics_survive_token_renewal_without_oauth_or_recollection(repricer):
    first = repricer.get(True)
    assert first['rows'][0]['views'] == 100
    repricer.token[0] = 'synthetic-renewed-token'
    second = repricer.get()
    assert second['rows'][0]['views'] == 100
    assert len(repricer.oauth) == len(repricer.fetches) == 1
    assert repricer.key in [key for key, _ in repricer.writes]


def test_reconnected_credentials_do_not_reuse_previous_account_metrics(repricer):
    repricer.get(True)
    repricer.credentials.client_secret = 'synthetic-other-credential-generation'
    repricer.client.result = AvitoListingsFetchResult(status='blocked', error=AvitoListingsError(
        code='forbidden_scope', message='Synthetic forbidden account', retryable=False))
    result = repricer.get()
    assert result['status'] == 'blocked'
    assert result['rows'] == []
    assert len(repricer.fetches) == 2


def test_manual_refresh_recovers_inventory_only_cache_and_reuses_it(repricer):
    repricer.cache[17, repricer.key] = {'status': 'partial', 'rows': [
        {'itemId': 'item', 'title': 'Synthetic', 'accountId': 'account', 'accountName': 'Synthetic', 'views': None}],
        'source': {'error': {'code': 'rate_limited', 'retryable': True}}}
    saved = repricer.get()
    assert saved['rows'][0]['views'] is None
    assert saved['source']['error']['code'] == 'rate_limited'
    assert not repricer.fetches
    result = repricer.get(True)
    assert result['status'] == 'synced'
    assert result['rows'][0]['contactsMessenger'] == 0
    assert result['rows'][0]['orderConversionPct'] == 2
    assert result['rows'][0]['averageContactCostKopecks'] == 100
    assert result['source']['error'] is None
    assert repricer.get()['rows'][0]['spendKopecks'] == 500
    assert len(repricer.fetches) == 1
    assert str(repricer.fetches[0].dateFrom) == '2026-10-01'
    assert str(repricer.fetches[0].dateTo) == '2026-10-08'


@pytest.mark.parametrize('code', ['rate_limited', 'avito_server_error'])
def test_failed_refresh_preserves_existing_metrics_and_exposes_error(repricer, code):
    repricer.get(True)
    repricer.writes.clear()
    repricer.client.result = AvitoListingsFetchResult(status='partial', rows=[AvitoListingRow(
        itemId='item', title='Synthetic', accountId='account', accountName='Synthetic', status='active')],
        error=AvitoListingsError(code=code, message='Synthetic failure', retryable=True, retryAfterSeconds=240))
    failed = repricer.get(True)
    assert failed['status'] == 'partial'
    assert failed['rows'][0]['views'] == 100
    assert failed['rows'][0]['spendKopecks'] == 500
    assert failed['source']['error']['code'] == code
    assert not any(key == repricer.key for key, _ in repricer.writes)
    if code == 'rate_limited':
        calls = len(repricer.fetches)
        assert repricer.get(True)['rows'][0]['views'] == 100
        assert len(repricer.fetches) == calls
        until = datetime.fromisoformat(failed['source']['error']['retryAfterUntil'])
        assert until > datetime.now(timezone.utc) + timedelta(seconds=230)


def test_old_token_cache_is_migrated_only_for_matching_period(repricer):
    payload = {'status': 'partial', 'period': {'dateFrom': '2026-10-01', 'dateTo': '2026-10-08'},
               'rows': [{'itemId': 'item', 'accountId': 'account', 'accountName': 'Synthetic', 'title': 'Synthetic'}],
               'source': {'error': {'code': 'rate_limited', 'retryable': True}}}
    legacy = scoped_avito_cache_key(route._cache_key(date(2026, 10, 1), date(2026, 10, 8), []), repricer.token[0])
    repricer.cache[17, legacy] = payload
    result = repricer.get()
    assert result['source']['error']['code'] == 'rate_limited'
    assert repricer.cache[17, repricer.key] is payload
    assert not repricer.fetches


def test_stats_retry_after_header_is_preserved():
    response = httpx.Response(429, headers={'Retry-After': '240'}, request=httpx.Request('POST', 'https://example.test/stats'))
    error = LiveAvitoListingsClient._fetch_error(httpx.HTTPStatusError('Synthetic limit', request=response.request, response=response),
                                                fallback_code='avito_analytics_failed', fallback_blocker='AVITO_STATS')
    assert error.code == 'rate_limited' and error.retryAfterSeconds == 240
