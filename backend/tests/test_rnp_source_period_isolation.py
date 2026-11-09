from datetime import date

import pytest

from app.wb_api import rnp_runtime as runtime


@pytest.mark.parametrize("prefix", ["ads", "baskets"])
@pytest.mark.parametrize("period", [
    ("2026-05-01", "2026-05-07"),
    ("2026-06-02", "2026-06-08"),
    (None, None),
])
def test_same_length_fallback_from_another_period_is_not_a_source(prefix, period, monkeypatch):
    fallback = {"dateFrom": period[0], "dateTo": period[1],
                "aggregates": {"123": {"ordersCount": 100, "adSpendKopecks": 50000}}}
    monkeypatch.setattr(runtime, "get_source_cache", lambda org, key, slim=False: fallback if key == f"{prefix}_7" else None)
    monkeypatch.setattr(runtime, "list_source_cache_ranges_by_prefix", lambda *args, **kwargs: [])
    result = runtime._period_cache(7, prefix, date(2026, 6, 1), date(2026, 6, 7))
    assert result == {}


@pytest.mark.parametrize("prefix", ["ads", "baskets"])
def test_covering_aggregate_without_daily_evidence_cannot_be_sliced(prefix, monkeypatch):
    fallback = {"dateFrom": "2026-05-01", "dateTo": "2026-06-30",
                "aggregates": {"123": {"ordersCount": 100}}}
    monkeypatch.setattr(runtime, "get_source_cache", lambda org, key, slim=False: fallback if key == f"{prefix}_7" else None)
    monkeypatch.setattr(runtime, "list_source_cache_ranges_by_prefix", lambda *args, **kwargs: [])
    assert runtime._period_cache(7, prefix, date(2026, 6, 1), date(2026, 6, 7)) == {}


def test_exact_key_keeps_existing_cache_contract(monkeypatch):
    exact = {"dateFrom": "2026-06-01", "dateTo": "2026-06-07",
             "aggregates": {"123": {"ordersCount": 0}}}
    monkeypatch.setattr(runtime, "get_source_cache", lambda org, key, slim=False: exact if key == "baskets_2026-06-01_2026-06-07" else None)
    assert runtime._period_cache(7, "baskets", date(2026, 6, 1), date(2026, 6, 7)) == exact


def test_same_period_legacy_fallback_does_not_require_daily_rollup(monkeypatch):
    fallback = {"dateFrom": "2026-06-01", "dateTo": "2026-06-07",
                "aggregates": {"123": {"ordersCount": 7}}}
    monkeypatch.setattr(runtime, "get_source_cache", lambda org, key, slim=False: fallback if key == "baskets_7" else None)
    monkeypatch.setattr(runtime, "list_source_cache_ranges_by_prefix", lambda *args, **kwargs: [])
    assert runtime._period_cache(7, "baskets", date(2026, 6, 1), date(2026, 6, 7)) == fallback


def test_covering_daily_rollup_uses_only_requested_days(monkeypatch):
    fallback = {"dateFrom": "2026-05-31", "dateTo": "2026-06-08",
                "aggregates": {"123": {"ordersCount": 900}},
                "dailyAggregates": {f"2026-06-{day:02}": {"123": {"ordersCount": 1}}
                                    for day in range(1, 9)}}
    monkeypatch.setattr(runtime, "get_source_cache", lambda org, key, slim=False: fallback if key == "baskets_7" else None)
    result = runtime._period_cache(7, "baskets", date(2026, 6, 1), date(2026, 6, 7))
    assert result["aggregates"] == {"123": {"ordersCount": 7}}
    assert fallback["aggregates"] == {"123": {"ordersCount": 900}}


def test_missing_daily_observation_cannot_be_silently_treated_as_zero(monkeypatch):
    fallback = {"dateFrom": "2026-05-31", "dateTo": "2026-06-08",
                "dailyAggregates": {"2026-06-01": {"123": {"ordersCount": 1}}}}
    monkeypatch.setattr(runtime, "get_source_cache", lambda org, key, slim=False: fallback if key == "baskets_7" else None)
    monkeypatch.setattr(runtime, "list_source_cache_ranges_by_prefix", lambda *args, **kwargs: [])
    assert runtime._period_cache(7, "baskets", date(2026, 6, 1), date(2026, 6, 7)) == {}
