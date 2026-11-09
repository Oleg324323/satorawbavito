from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import repricer_bff as bff
from app.routers import wb_repricer_bff as router


def test_summary_paginates_and_uses_details_without_sku_requests(monkeypatch):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(bff, "_utc_now", lambda: now)
    monkeypatch.setattr(bff, "_PROMOTION_LIST_PAGE_SIZE", 1)
    calls = []
    promotions = [
        {"id": 1, "startDateTime": (now - timedelta(days=1)).isoformat(), "endDateTime": (now + timedelta(days=1)).isoformat(), "type": "auto"},
        {"id": 2, "startDateTime": (now + timedelta(days=1)).isoformat(), "endDateTime": (now + timedelta(days=2)).isoformat(), "type": "regular"},
    ]

    def request(_client, req):
        calls.append(req)
        if req.path.endswith('/details'):
            return {"promotions": [{"id": 1, "inPromoActionTotal": 7, "notInPromoActionTotal": 3, "participationPercentage": 70}, {"id": 2, "inPromoActionTotal": 0, "notInPromoActionTotal": 5, "participationPercentage": 0}]}
        assert req.path == '/api/v1/calendar/promotions'
        offset = req.query['offset']
        return {"promotions": promotions[offset:offset + 1]}

    monkeypatch.setattr(bff, '_request_or_raise_calendar', request)
    rows = bff.list_promotions(summary_only=True)
    by_id = {row['id']: row for row in rows}
    assert by_id['1']['participatingSkuCount'] == 7
    assert by_id['1']['eligibleSkuCount'] == 10
    assert by_id['1']['status'] == 'active'
    assert by_id['2']['participatingSkuCount'] == 0
    assert by_id['2']['status'] == 'upcoming'
    assert len(calls) == 4  # Three listing pages and one details batch.


@pytest.mark.parametrize('failure', ['missing', 'missing_counts', 'rate_limit'])
def test_summary_does_not_turn_missing_details_into_zero(monkeypatch, failure):
    now = datetime.now(timezone.utc)
    def request(_client, req):
        if req.path.endswith('/details'):
            if failure == 'rate_limit':
                raise HTTPException(429, 'Rate limited')
            if failure == 'missing_counts':
                return {"promotions": [{"id": 1}]}
            return {"promotions": []}
        return {"promotions": [{"id": 1, "startDateTime": (now - timedelta(days=1)).isoformat(), "endDateTime": (now + timedelta(days=1)).isoformat()}]}
    monkeypatch.setattr(bff, '_request_or_raise_calendar', request)
    with pytest.raises(HTTPException) as exc:
        bff.list_promotions(summary_only=True)
    assert exc.value.status_code == (429 if failure == 'rate_limit' else 502)


@pytest.mark.parametrize('refresh,age,fetches', [(False, 60, False), (True, 60, True), (False, 301, True)])
def test_promotions_page_cache_refresh_and_isolation(monkeypatch, refresh, age, fetches):
    monkeypatch.setattr(router, '_request_actor_and_wb_token', lambda _: (SimpleNamespace(organization_id=99), None))
    monkeypatch.setattr(router, '_promotion_thresholds_cache', lambda _: {})
    monkeypatch.setattr(router, '_apply_promotion_threshold_cache', lambda rows, _: rows)
    cached = [{'id': 'old'}]
    keys, saved, fetched = [], [], []
    def cache(_org, key):
        keys.append(key)
        return {'promotions': cached}
    monkeypatch.setattr(router, 'get_source_cache', cache)
    monkeypatch.setattr(router, 'get_source_cache_fetched_at', lambda _org, key: (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat())
    def load(*args, **kwargs):
        fetched.append(kwargs)
        return [{'id': 'new'}]
    monkeypatch.setattr(router, 'list_promotions', load)
    monkeypatch.setattr(router, 'save_source_cache', lambda org, key, payload: saved.append((key, payload)))
    result = router.get_promotions(Request({'type': 'http'}), scenario='complete', refresh=refresh)
    assert result[0]['id'] == ('new' if fetches else 'old')
    assert keys == ['promotions_listing']
    assert bool(fetched) == fetches
    if fetches:
        assert fetched[0]['summary_only'] is True
        assert saved[0][0] == 'promotions_listing'


def test_refresh_failure_is_visible_and_does_not_overwrite_cache(monkeypatch):
    monkeypatch.setattr(router, '_request_actor_and_wb_token', lambda _: (SimpleNamespace(organization_id=99), None))
    monkeypatch.setattr(router, '_promotion_thresholds_cache', lambda _: {})
    monkeypatch.setattr(router, 'get_source_cache', lambda *args: {'promotions': [{'id': 'stale'}]})
    monkeypatch.setattr(router, 'get_source_cache_fetched_at', lambda *args: None)
    def load(*args, **kwargs):
        raise HTTPException(429, 'Rate limited')
    monkeypatch.setattr(router, 'list_promotions', load)
    monkeypatch.setattr(router, 'save_source_cache', lambda *args: pytest.fail('Failed refresh must not replace saved data'))
    with pytest.raises(HTTPException):
        router.get_promotions(Request({'type': 'http'}), scenario='complete', refresh=True)
