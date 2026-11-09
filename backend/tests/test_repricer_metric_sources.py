from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app import repricer_bff as bff
from app.routers import wb_repricer_bff as router


def _row(**kwargs):
    return bff._build_sku_row(
        "METRIC_TEST", nm_id=123, name="Test", subject="Тест", brand=None,
        chrt_ids=[], current_price_kopecks=100_000, discounted_price_kopecks=100_000,
        buyer_price_kopecks=90_000, promotions=[], use_demo_data=False, **kwargs,
    )


@pytest.mark.parametrize("field", ["orderCount", "ordersCount", "orders"])
@pytest.mark.parametrize("period_orders", [100, 0])
def test_measured_zero_funnel_orders_keeps_priority_and_provenance(field, period_orders):
    funnel = bff._sales_funnel_metrics({field: 0, "cartCount": 5})
    row = _row(
        baskets_aggregate=funnel, baskets_cache_loaded=True,
        period_aggregate={"ordersUnits": period_orders},
        finance_aggregate={"salesUnits": 10, "returnsUnits": 1},
    )
    assert row["analytics"]["ordersUnits"] == 0
    assert row["analytics"]["ordersSource"] == "sales_funnel.orderCount"
    assert row["analytics"]["funnelOrderCount"] == 0
    assert router._repricer_stats_metrics(row)["orders"] == 0
    assert router._repricer_list_summary([row])["ordersUnits"] == 0


@pytest.mark.parametrize("selected", [{"cartCount": 5}, {"orderCount": None, "cartCount": 5}])
@pytest.mark.parametrize("period_orders,expected,source", [
    (100, 100, "supplier.orders"), (0, 11, "finance_sales_fallback"),
])
def test_missing_funnel_orders_retains_existing_fallback(selected, period_orders, expected, source):
    funnel = bff._sales_funnel_metrics(selected)
    assert funnel["orderCount"] is None
    row = _row(
        baskets_aggregate=funnel, baskets_cache_loaded=True,
        period_aggregate={"ordersUnits": period_orders},
        finance_aggregate={"salesUnits": 10, "returnsUnits": 1},
    )
    assert row["analytics"]["ordersUnits"] == expected
    assert row["analytics"]["ordersSource"] == source
    assert row["analytics"]["funnelOrderCount"] is None
    assert router._repricer_stats_metrics(row)["orders"] == (expected if source == "supplier.orders" else None)


def test_invalid_negative_funnel_orders_does_not_gain_priority():
    row = _row(baskets_aggregate={"orderCount": -1}, period_aggregate={"ordersUnits": 7})
    assert row["analytics"]["ordersUnits"] == 7
    assert row["analytics"]["ordersSource"] == "supplier.orders"
    assert row["analytics"]["funnelOrderCount"] is None


@pytest.mark.parametrize("selected,expected", [
    ({"orderCount": 0}, 0), ({"orderCount": 7}, 7),
    ({"orderCount": None}, 100), ({}, 100),
])
def test_source_summary_respects_measured_funnel_orders(monkeypatch, selected, expected):
    payloads = {
        "baskets": {"aggregates": {"123": bff._sales_funnel_metrics(selected)}},
        "period_stats": {"aggregates": {"123": {"ordersUnits": 100}}},
        "finance": {"aggregates": {"123": {"salesUnits": 10, "returnsUnits": 1}}},
    }
    monkeypatch.setattr(router, "list_cached_goods", lambda _org: [{"nmID": 123, "vendorCode": "METRIC_TEST"}])
    monkeypatch.setattr(router, "_period_source_cache", lambda _org, prefix, *_args, **_kwargs: payloads.get(prefix, {}))
    monkeypatch.setattr(router, "get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(router, "_repricer_finance_taxes", lambda *_args, **_kwargs: {})
    result = router._repricer_list_summary_from_source_caches(
        2, resolved_period_days=30, period_suffix="2026-08-16_2026-09-14",
        range_start=date(2026, 8, 16), range_end=date(2026, 9, 14),
    )
    assert result["ordersUnits"] == expected


@pytest.mark.parametrize("value", [0, 100, 84.6])
def test_buyout_uses_validated_wb_conversion_without_rescaling(value):
    metrics = bff._sales_funnel_metrics({
        "orderCount": 1, "buyoutCount": 20, "cartCount": 0,
        "conversions": {"buyoutPercent": value},
    })
    row = _row(baskets_aggregate=metrics, baskets_cache_loaded=True)
    assert row["analytics"]["buyoutPct"] == value
    assert row["analytics"]["baskets"] == 0
    assert row["analytics"]["buyoutSource"] == "sales_funnel.conversions.buyoutPercent"


@pytest.mark.parametrize("value", [None, -1, 889, float("nan"), float("inf")])
def test_buyout_does_not_invent_conversion_from_sales_or_untrusted_cache(value):
    metrics = bff._sales_funnel_metrics({"orderCount": 10, "buyoutCount": 7, "conversions": {"buyoutPercent": value}})
    assert metrics["buyoutPct"] is None
    row = _row(period_aggregate={"ordersUnits": 1, "salesUnits": 20, "buyoutPct": 889}, baskets_aggregate=metrics)
    assert row["analytics"]["buyoutPct"] is None
    assert _row(baskets_aggregate={"buyoutPct": 90})["analytics"]["buyoutPct"] is None


def test_stitched_days_retain_timestamp_counts_and_partial_coverage(monkeypatch):
    observed = "2026-09-14T01:00:00+00:00"
    daily = {
        "2026-09-12": {"123": {"cartCount": 3, "orderCount": 2, "buyoutPct": 75}},
        "2026-09-13": {"123": {"cartCount": 0, "orderCount": 0, "buyoutPct": 90}},
    }
    monkeypatch.setattr(router, "list_source_cache_ranges_by_prefix", lambda *_a, **_kw: [{
        "sourceKey": "baskets_window", "dateFrom": "2026-09-12", "dateTo": "2026-09-13",
        "dailyAggregatesDays": 2, "dailyAggregateDates": list(daily), "fetchedAt": observed,
    }])
    monkeypatch.setattr(router, "get_source_cache", lambda *_a, **_kw: {"dailyAggregates": daily, "fetchedAt": observed})
    cache = router._stitched_period_cache_from_days(2, "baskets", date(2026, 9, 12), date(2026, 9, 14))
    assert cache["fetchedAt"] == observed
    assert cache["missingDates"] == ["2026-09-14"]
    row = _row(baskets_aggregate=cache["aggregates"]["123"], baskets_cache_loaded=bool(cache["fetchedAt"]))
    assert row["analytics"]["baskets"] == 3
    assert row["analytics"]["basketsState"] == "partial"
    assert row["analytics"]["buyoutPct"] is None
    assert router._repricer_stats_item(row)["metrics"]["baskets"] == 3


def test_missing_tariff_blocks_plan_but_keeps_actual_commission():
    row = _row(
        finance_aggregate={"salesUnits": 1, "commissionKopecks": 12_000, "commissionPct": 12},
        baskets_aggregate=bff._sales_funnel_metrics({"conversions": {"buyoutPercent": 80}}),
    )
    analytics = row["analytics"]
    assert analytics["financeCommissionKopecks"] == 12_000
    assert analytics["reportCommissionPct"] == 12
    assert analytics["marginPct"] is None
    assert analytics["plannedMarginKopecks"] is None
    assert analytics["plannedPeriodMarginKopecks"] is None


def test_tariffs_survive_worker_restart_and_stay_in_organization(monkeypatch):
    storage = {}
    monkeypatch.setattr(bff, "COMMISSION_TARIFFS_CACHE", {})
    monkeypatch.setattr(bff, "get_source_cache", lambda org, key, **_kw: deepcopy(storage.get((org, key))))
    monkeypatch.setattr(bff, "save_source_cache", lambda org, key, payload: storage.update({(org, key): deepcopy(payload)}))
    monkeypatch.setattr(bff, "RateLimitedWbApiClient", lambda inner: inner)
    monkeypatch.setattr(bff, "build_wb_common_client", lambda *_a, **_kw: SimpleNamespace(request=lambda _r: SimpleNamespace(ok=True, data={
        "report": [{"subjectID": 1, "subjectName": "Тест", "kgvpMarketplace": 12, "kgvpSupplier": 5}],
    })))
    index = bff.fetch_commission_tariffs(wb_token="synthetic", organization_id=2)
    bff.COMMISSION_TARIFFS_CACHE.clear()
    assert bff.cached_commission_tariffs(organization_id=2) == index
    assert bff.cached_commission_tariffs(organization_id=3) == {}
    analytics = _row(
        commission_tariffs_index=bff.cached_commission_tariffs(organization_id=2),
        baskets_aggregate=bff._sales_funnel_metrics({"conversions": {"buyoutPercent": 80}}),
    )["analytics"]
    assert analytics["commissionState"] == "ok"
    assert analytics["commissionTariffFetchedAt"] == index["id:1"]["fetchedAt"]
    assert analytics["plannedMarginKopecks"] is not None
    assert _row(commission_tariffs_index=index)["analytics"]["plannedMarginKopecks"] is None
    assert bff._tariff_base_commission_pct({"kgvpSupplier": 5}) is None
    assert bff._tariff_base_commission_pct({"kgvpMarketplace": 0}) == 0


def test_club_discount_is_not_wallet_discount_and_price_pair_must_match():
    assert bff._resolve_spp_analytics({"clubDiscount": 5}, {"buyerPriceNoWalletKopecks": 90_000}, 100_000) == (90_000, None, 10.0, None)
    assert bff._resolve_spp_analytics({}, {"buyerPriceNoWalletKopecks": 90_000, "buyerPriceSellerKopecks": 110_000}, 100_000)[:3] == (None, None, None)


def test_strategy_assignment_read_hydrates_scope_and_uses_all_cached_rows(monkeypatch):
    calls = []
    rows = [{"meta": {"articleId": "METRIC_TEST", "status": "auto"}, "strategy": {"id": "baskets_orders", "name": "Тест", "assignmentSource": "manual"}}]
    monkeypatch.setattr(router, "_hydrate_org_repricer_state", lambda request: calls.append("hydrate") or 2)
    monkeypatch.setattr(router, "_request_wb_token", lambda request: "synthetic")
    monkeypatch.setattr(router, "_list_repricer_skus_for_request", lambda request, scenario, **kw: calls.append(kw["max_items"]) or rows)
    result = router.get_frontend_strategy_assignments(object(), scenario="complete")
    assert calls == ["hydrate", None]
    assert result["items"][0]["strategyId"] == "baskets_orders"


def test_snapshot_rebuilds_after_basket_source_refresh(monkeypatch):
    snapshot = {"version": router.SKU_LIST_SNAPSHOT_VERSION, "storage": "chunked", "sourceRevision": "old", "goodsRevision": "catalog",
                "stateRevision": router._repricer_view_state_revision(), "taxRevision": "tax"}
    monkeypatch.setattr(router, "legacy_finance_tax_revision", lambda _org: "tax")
    monkeypatch.setattr(router, "get_source_cache", lambda *_a, **_kw: deepcopy(snapshot))
    monkeypatch.setattr(router, "get_repricer_sources_revision", lambda org: "new")
    monkeypatch.setattr(router, "cached_goods_meta", lambda org: {"latestFetchedAt": "catalog"})
    assert router._load_repricer_sku_snapshot(2, "complete", period_suffix="2026-09-12_2026-09-14", include_promotions=False, include_content=False) is None


def test_covering_basket_cache_does_not_claim_missing_days():
    cache = {"dateFrom": "2026-09-01", "dateTo": "2026-09-03", "dailyAggregates": {"2026-09-01": {"123": {"cartCount": 7}}}}
    assert router._covered_cache_from_daily(cache, prefix="baskets", period_suffix="3", resolved_period_days=3, range_start=datetime(2026, 9, 1, tzinfo=timezone.utc), range_end=datetime(2026, 9, 3, tzinfo=timezone.utc)) == {}


def test_content_pagination_keeps_categories_beyond_2000_cards(monkeypatch):
    calls = []
    monkeypatch.setattr(bff, "RateLimitedWbApiClient", lambda inner: inner)
    monkeypatch.setattr(bff, "build_wb_content_client", lambda *_a, **_kw: object())

    def page(_client, request, **_kwargs):
        cursor = request.jsonBody["settings"]["cursor"]
        start = cursor.get("nmID", 0)
        calls.append(start)
        cards = [{"nmID": i, "subjectID": 1} for i in range(start + 1, min(start + 101, 2102))]
        return {"cards": cards, "cursor": {"nmID": start + len(cards), "updatedAt": "2026-09-15"}}

    monkeypatch.setattr(bff, "_request_or_raise_content_cards", page)
    pages = []
    assert len(bff._fetch_content_cards("complete", wb_token="synthetic", page_callback=lambda page: pages.append(page))) == 2101
    assert len(calls) == 22
    assert [len(page) for page in pages] == [100] * 21 + [1]


def test_stitch_uses_newest_complete_day_once(monkeypatch):
    caches = {
        "baskets_old": {
            "fetchedAt": "2026-09-14T01:00:00+00:00",
            "dailyAggregates": {"2026-09-12": {"123": {"cartCount": 3}}, "2026-09-13": {"123": {"cartCount": 4}}},
        },
        "baskets_new": {
            "fetchedAt": "2026-09-14T02:00:00+00:00",
            "dailyAggregates": {"2026-09-13": {"123": {"cartCount": 11}}},
        },
        "baskets_failed": {
            "fetchedAt": "2026-09-14T03:00:00+00:00",
            "dailyAggregates": {"2026-09-13": {"123": {"cartCount": 99}}},
            "chunks": [{"type": "daily", "date": "2026-09-13", "chunkIndex": 0, "status": "failed"}],
        },
    }
    monkeypatch.setattr(router, "list_source_cache_ranges_by_prefix", lambda *_a, **_kw: [
        {"sourceKey": key, "fetchedAt": cache["fetchedAt"], "dailyAggregateDates": list(cache["dailyAggregates"])}
        for key, cache in caches.items()
    ])
    monkeypatch.setattr(router, "get_source_cache", lambda org, key, **_kw: deepcopy(caches[key]))
    cache = router._stitched_period_cache_from_days(2, "baskets", date(2026, 9, 12), date(2026, 9, 13))
    assert cache["aggregates"]["123"]["cartCount"] == 14
    assert cache["fetchedAt"] == caches["baskets_new"]["fetchedAt"]
    assert cache["coverageState"] == "ok"
    assert not router._baskets_range_has_daily_detail(caches["baskets_failed"], date(2026, 9, 13), date(2026, 9, 13))


@pytest.mark.parametrize("previous_status", ["fetched", "partial"])
def test_short_sync_refreshes_days_and_resumes_partial_checkpoints(monkeypatch, previous_status):
    from app import repricer_sync as sync

    key = "baskets_2026-09-13_2026-09-14"
    daily = {"2026-09-13": {"123": {"cartCount": 3}}}
    chunks = [{"type": "daily", "date": "2026-09-13", "chunkIndex": 0, "status": "done"}]
    caches = {key: {"dateFrom": "2026-09-13", "dateTo": "2026-09-14", "dailyAggregates": daily, "chunks": chunks, "dailyDetailStatus": previous_status}}
    calls = []
    monkeypatch.setattr(sync, "list_cached_goods", lambda org: [{"nmID": 123}])
    monkeypatch.setattr(sync, "get_source_cache", lambda org, source_key, **_kw: deepcopy(caches.get(source_key) or {}))
    monkeypatch.setattr(sync, "save_source_cache", lambda org, source_key, payload: caches.update({source_key: deepcopy(payload)}) or payload)
    monkeypatch.setattr(sync, "_baskets_daily_seed_from_recent_cache", lambda *_a, **_kw: (deepcopy(daily), deepcopy(chunks)))
    monkeypatch.setattr(sync, "_release_warmup_goods_by_baskets", lambda *_a: 0)
    monkeypatch.setattr(sync, "fetch_baskets_aggregates", lambda *_a, **_kw: {"aggregates": {"123": {"cartCount": 8}}, "count": 1})

    def fetch_detail(*_args, **kwargs):
        calls.append(kwargs)
        return {"dailyAggregates": {"2026-09-13": {"123": {"cartCount": 4}}, "2026-09-14": {"123": {"cartCount": 4}}}}

    monkeypatch.setattr(sync, "fetch_baskets_daily_detail", fetch_detail)
    result = sync.refresh_wb_data_sources(
        organization_id=2, wb_token="synthetic", date_from=date(2026, 9, 13), date_to=date(2026, 9, 14),
        sources=["baskets"], execute_lock=False, _parallelize=False,
    )
    assert result["state"] == "completed"
    assert calls[0]["existing_daily_aggregates"] == (daily if previous_status == "partial" else {})
    assert calls[0]["existing_chunks"] == (chunks if previous_status == "partial" else [])
    assert caches[key]["dailyDetailStatus"] == "fetched"
    assert caches[key]["dailyAggregatesDays"] == 2


def test_execution_preserves_missing_economic_inputs_and_valid_zero_buyout():
    from app.repricer_execution import _economics_from_row

    row = _row(finance_aggregate={"commissionPct": 12})
    economics = _economics_from_row(row)
    assert economics.buyoutPct is None
    assert economics.commissionPct is None
    row["analytics"].update(buyoutPct=0, commissionState="ok", wbCommissionPct=12)
    economics = _economics_from_row(row)
    assert economics.buyoutPct == 0
    assert economics.commissionPct == 12
