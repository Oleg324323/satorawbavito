from contextlib import nullcontext
from types import SimpleNamespace

from app.avito import repricer_metrics as metrics
from app.routers.avito_repricer import _summary


def test_item_metrics_and_zero_denominators():
    assert metrics.row_metrics({"views": 200, "orders": 5, "contacts": 4, "spendKopecks": 8084}) == {
        "orderConversionPct": 2.5, "averageContactCostKopecks": 2021}
    assert metrics.row_metrics({"views": 0, "orders": 5, "contacts": 0, "spendKopecks": 8084}) == {
        "orderConversionPct": None, "averageContactCostKopecks": None}
    assert metrics.row_metrics({"contacts": 5, "views": 100})["averageContactCostKopecks"] is None
    assert metrics.trend(20, 10)["percent"] == 100
    assert metrics.trend(20, 10)["delta"] == 10
    assert metrics.trend(5, 10)["direction"] == "down"
    assert metrics.trend(0, 0)["direction"] == "flat"
    assert metrics.trend(5, 0)["percent"] is None
    assert metrics.trend(5, 0)["delta"] == 5
    assert metrics.trend(None, 4)["direction"] == "missing"


def test_blocked_ads_are_not_price_guard_failures_and_only_active_prices_count():
    summary = _summary([
        {"status": "active", "priceKopecks": 10000, "strategySignal": "blocked"},
        {"status": "blocked", "priceKopecks": 50000, "strategySignal": "blocked"},
        {"status": "old", "priceKopecks": 80000},
    ])
    assert summary["active"] == 1
    assert summary["activePriceKopecks"] == 10000
    assert summary["blockedListings"] == 1
    assert summary["blocked"] == 2


def test_comparison_windows_are_batched_cached_and_item_specific(monkeypatch):
    monkeypatch.setattr(metrics, 'photo_index', lambda _: {})
    cache, calls = {}, []
    monkeypatch.setattr(metrics, "get_source_cache", lambda org, key, **_: cache.get((org, key)))
    monkeypatch.setattr(metrics, "save_source_cache", lambda org, key, value: cache.update({(org, key): value}))
    monkeypatch.setattr(metrics, "blocked_count", lambda *_: 0)
    def fetch(http, request, account):
        calls.append((request, account))
        return {
            "a": {
                "views": 20 if len(calls) % 2 else 10,
                "contacts": 99,
                "contactsMessenger": 4 if len(calls) % 2 else 2,
            },
            "b": {"views": 7, "contacts": 8, "contactsMessenger": 0},
        }
    client = SimpleNamespace(_http_client=lambda: nullcontext(None), _v2_item_analytics_for_account=fetch)
    payload = {"period": {"dateFrom": "2026-07-01", "dateTo": "2026-07-28"}, "summary": {},
               "source": {"error": {"code": "rate_limited", "retryAfterUntil": "2020-01-01T00:00:00Z"}},
               "rows": [{"itemId": "a", "accountId": "account"}, {"itemId": "b", "accountId": "account"}]}
    enriched = metrics.enrich_comparisons(payload, 1, "scope", client)
    assert len(calls) == 2  # not two requests per product
    assert str(calls[0][0].dateFrom) == "2026-07-26"
    assert str(calls[1][0].dateFrom) == "2026-07-23"
    assert str(calls[1][0].dateTo) == "2026-07-25"
    assert enriched["rows"][0]["viewsTrend"]["percent"] == 100
    assert enriched["rows"][0]["contactsTrend"]["percent"] == 100
    assert enriched["rows"][1]["viewsTrend"]["direction"] == "flat"
    assert enriched["rows"][1]["contactsTrend"]["direction"] == "flat"
    assert "viewsTrend" not in payload["rows"][0]
    metrics.enrich_comparisons(payload, 1, "scope", client)
    assert len(calls) == 2
    metrics.enrich_comparisons(payload, 2, "scope", client)
    assert len(calls) == 4
    metrics.enrich_comparisons(payload, 1, "different-scope", client)
    assert len(calls) == 6


def test_comparison_failure_preserves_totals_and_backoffs(monkeypatch):
    monkeypatch.setattr(metrics, 'photo_index', lambda _: {})
    cache, calls = {}, []
    monkeypatch.setattr(metrics, "get_source_cache", lambda org, key, **_: cache.get(key))
    monkeypatch.setattr(metrics, "save_source_cache", lambda org, key, value: cache.update({key: value}))
    monkeypatch.setattr(metrics, "blocked_count", lambda *_: None)
    def fetch(*_):
        calls.append(1)
        raise RuntimeError("Synthetic provider unavailable")
    client = SimpleNamespace(_http_client=lambda: nullcontext(None), _v2_item_analytics_for_account=fetch)
    payload = {"period": {"dateTo": "2026-07-28"}, "rows": [{"itemId": "a", "accountId": "account", "views": 123}]}
    for _ in range(2):
        result = metrics.enrich_comparisons(payload, 1, "scope", client)
        assert result["rows"][0]["views"] == 123
        assert result["rows"][0]["viewsTrend"]["direction"] == "missing"
    assert len(calls) == 1


def test_saved_report_enrichment_does_not_request_provider_when_fetch_is_disabled(monkeypatch):
    monkeypatch.setattr(metrics, 'photo_index', lambda _: {})
    monkeypatch.setattr(metrics, 'get_source_cache', lambda *_, **__: None)
    def unexpected(*_, **__):
        raise AssertionError('A cached/cooldown read must not contact Avito')
    monkeypatch.setattr(metrics, 'save_source_cache', unexpected)
    monkeypatch.setattr(metrics, 'blocked_count', unexpected)
    payload = {'period': {'dateTo': '2026-10-08'}, 'summary': {'blockedListings': 2},
               'rows': [{'itemId': 'item', 'accountId': 'account', 'views': 100, 'contactsMessenger': 0}]}
    result = metrics.enrich_comparisons(payload, 1, 'synthetic-scope', None, allow_fetch=False)
    assert result['rows'][0]['views'] == 100
    assert result['rows'][0]['contactsTrend']['direction'] == 'missing'
    assert result['summary']['blockedListings'] == 2
