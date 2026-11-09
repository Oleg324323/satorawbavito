"""Repricer campaign expense projection; isolated data, no marketplace calls."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from app.routers import wb_repricer_bff as router
from app.wb_repricer_reporting import (
    apply_campaign_advertising, campaign_advertising, filtered_campaign_advertising,
    apply_summary_availability,
)

START, END = date(2026, 10, 12), date(2026, 10, 16)


def snapshot(spend=2500, **patch):
    return dict(dateFrom=str(START), dateTo=str(END), fetchedAt="2026-10-21T12:00:00Z",
                source="wb_ads_fullstats", aggregates={"101": dict(adSpendKopecks=spend)},
                totals=dict(adSpendKopecks=spend), **patch)


def rows(financial_ad=1000, **patch):
    return [dict(meta=dict(nmId=101), analytics=dict(
        adSpendKopecks=financial_ad, expensesKopecks=7000 + financial_ad,
        netProfitKopecks=12000 - financial_ad, factNetProfitKopecks=12000 - financial_ad,
        settlementProfitKopecks=12000 - financial_ad,
        settlementAttributedProfitKopecks=12000 - financial_ad,
        settlementAdvertisingKopecks=financial_ad,
        finalPayoutKopecks=20000 - financial_ad,
        settlementFormulaVersion="wb-final-payout-cogs-tax-v1",
        settlementCogsKopecks=5000, settlementBlockers=[], financeState="ok",
        logisticsKopecks=7000, revenueKopecks=27000, salesUnits=1, returnsUnits=0,
        **patch))]


@pytest.mark.parametrize("financial,spend", [(1000, 2500), (1000, 0), (0, 2500), (-1000, 2500)])
def test_replaces_financial_advertising_once_without_changing_any_other_expense_or_cash_payout(financial, spend):
    ledger = dict(aggregates={"101": dict(adSpendKopecks=financial)})
    original = deepcopy(ledger)
    items = rows(financial)
    items[0]["analytics"]["marginPct"] = 99
    payout = items[0]["analytics"]["finalPayoutKopecks"]
    ads = campaign_advertising(snapshot(spend), START, END)
    apply_campaign_advertising(items, ledger, ads)
    value = deepcopy(items)
    apply_campaign_advertising(items, ledger, ads)
    assert items == value  # A second presentation pass must not charge Ads twice.
    fact = items[0]["analytics"]
    assert fact["adSpendKopecks"] == spend
    assert fact["expensesKopecks"] == 7000 + spend
    assert fact["netProfitKopecks"] == fact["campaignProfitKopecks"] == 12000 - spend
    assert fact["campaignAttributedProfitKopecks"] == 12000 - spend
    assert fact["marginPct"] == round((12000 - spend) / 27000 * 100, 1)
    assert fact["adsState"] == "ok"
    assert fact["logisticsKopecks"] == 7000
    assert fact["financeAdSpendKopecks"] == financial
    assert fact["finalPayoutKopecks"] == payout
    assert ledger == original


def test_missing_and_complete_empty_advertising_are_not_interchangeable():
    absent = campaign_advertising({}, START, END)
    empty = campaign_advertising({**snapshot(), "aggregates": {}, "totals": {"adSpendKopecks": 0}}, START, END)
    assert absent["state"] == "no_data" and absent["adSpendKopecks"] is None
    assert empty["state"] == "ok" and empty["adSpendKopecks"] == 0
    for cache, expected in ((absent, None), (empty, 0)):
        items = rows()
        apply_campaign_advertising(items, {"aggregates": {"101": {"adSpendKopecks": 1000}}}, cache)
        assert items[0]["analytics"]["adSpendKopecks"] == expected
        assert (items[0]["analytics"]["campaignProfitKopecks"] is None) == (expected is None)


@pytest.mark.parametrize("cache,state,freshness", [
    ({}, "no_data", "stale"),
    (snapshot(sourceComplete=False), "partial", "fresh"),
    (snapshot(0, sourceComplete=True), "ok", "fresh"),
    ({**snapshot(), "oldestFetchedAt": "2026-10-20T12:00:00Z"}, "ok", "stale"),
])
def test_repricer_metadata_exposes_campaign_completeness_and_existing_freshness_policy(monkeypatch, cache, state, freshness):
    from app.wb_api.source_freshness import source_is_fresh

    monkeypatch.setattr(router, "cached_goods_meta", lambda _org: {})
    monkeypatch.setattr(router, "get_source_cache_fetched_at", lambda *_args: None)
    monkeypatch.setattr(router, "get_source_cache_meta_fields", lambda *_args: {})
    monkeypatch.setattr(router, "_latest_ads_sync_step", lambda _org: {})
    monkeypatch.setattr(router, "_page_finance_active", lambda: True)
    monkeypatch.setattr("app.wb_api.source_freshness.source_is_fresh",
        lambda payload: source_is_fresh(payload, now=datetime(2026, 10, 21, 12, 30, tzinfo=timezone.utc)))
    metadata = router._repricer_list_cache_meta(1, 5, include_content=False, date_from=START, date_to=END,
        period_caches={"ads": cache}, require_full_sync_coverage=False)
    assert metadata["adsCoverageState"] == state
    assert metadata["adsFreshnessState"] == freshness
    assert metadata["adsDateFrom"] == cache.get("dateFrom")
    assert metadata["adsDateTo"] == cache.get("dateTo")
    assert "ads" in metadata["requiredPeriodSources"]


def test_existing_ads_only_loader_saves_the_exact_cache_consumed_by_repricer(monkeypatch):
    from app import repricer_sync

    stored, reads = {}, []
    def fetch(_scenario, *, wb_token, date_from, date_to, progress_callback):
        assert wb_token == "synthetic"
        assert (date_from.date(), date_to.date()) == (START, END)
        reads.append("ads")
        return snapshot(2500)
    def save(org, key, payload, **_kwargs):
        stored[org, key] = deepcopy(payload)
        return payload
    def unexpected(*_args, **_kwargs):
        pytest.fail("An advertising-only refresh must not fetch another source")

    monkeypatch.setattr(repricer_sync, "fetch_ads_spend_aggregates", fetch)
    monkeypatch.setattr(repricer_sync, "save_source_cache", save)
    monkeypatch.setattr(repricer_sync, "shadow_ingest_legacy_advertising_payload", lambda *_a, **_kw: {})
    for name in ("fetch_finance_report_aggregates", "fetch_baskets_aggregates", "fetch_period_stats_aggregates",
                 "fetch_catalog_goods_page", "fetch_stock_aggregates", "_fetch_content_cards", "list_promotions"):
        monkeypatch.setattr(repricer_sync, name, unexpected)
    result = repricer_sync.refresh_wb_data_sources(organization_id=7, wb_token="synthetic",
        date_from=START, date_to=END, sources=["ads"], execute_lock=False)
    assert result["state"] == "completed"
    assert reads == ["ads"]
    assert list(stored) == [(7, f"ads_{START}_{END}")]
    observation = campaign_advertising(stored[7, f"ads_{START}_{END}"], START, END)
    assert observation["state"] == "ok" and observation["adSpendKopecks"] == 2500


@pytest.mark.parametrize("patch", [dict(coverageState="partial"), dict(sourceComplete=False), dict(coveredDays=3)])
def test_partial_campaign_data_does_not_certify_profit_or_missing_sku_as_zero(patch):
    ads = campaign_advertising(snapshot(**patch), START, END)
    items = rows() + [dict(meta=dict(nmId=102), analytics={})]
    apply_campaign_advertising(items, dict(aggregates={"101": dict(adSpendKopecks=1000)}), ads)
    assert items[0]["analytics"]["adSpendKopecks"] == 2500
    assert items[0]["analytics"]["campaignProfitKopecks"] is None
    assert items[1]["analytics"]["adSpendKopecks"] is None
    assert filtered_campaign_advertising(items)["state"] == "partial"


def test_another_period_is_never_used_even_if_its_campaign_amount_exists():
    assert campaign_advertising(snapshot(), date(2026, 10, 5), date(2026, 10, 11))["state"] == "no_data"


def test_summary_uses_campaign_total_and_preserves_financial_cash_and_other_expenses():
    items = rows()
    ads = campaign_advertising(snapshot(2500), START, END)
    ledger = dict(aggregates={"101": dict(adSpendKopecks=1000)})
    apply_campaign_advertising(items, ledger, ads)
    diagnostics = dict(rawExpenseTotals={"adSpendKopecks": 1000, "logisticsKopecks": 7000})
    summary = router._repricer_list_summary(items, finance_diagnostics=diagnostics, campaign_ads=ads)
    assert summary["adSpendKopecks"] == 2500
    assert summary["adSpendSource"] == "wb_ads_fullstats"
    assert summary["expensesKopecks"] == 9500
    assert summary["campaignProfitKopecks"] == 9500
    assert summary["settlementProfitKopecks"] == 11000
    assert summary["logisticsKopecks"] == 7000


def test_cabinet_campaign_total_includes_unattributed_spend_without_allocating_it_to_products():
    items = rows(0)
    ads = campaign_advertising({**snapshot(2500), "totals": {"adSpendKopecks": 3000}}, START, END)
    apply_campaign_advertising(items, dict(aggregates={"101": dict(adSpendKopecks=0)}), ads)
    summary = router._repricer_list_summary(items, finance_diagnostics=dict(rawExpenseTotals={"adSpendKopecks": 0}), campaign_ads=ads)
    assert items[0]["analytics"]["adSpendKopecks"] == 2500
    assert summary["adSpendKopecks"] == 3000
    assert summary["expensesKopecks"] == 10000
    assert summary["campaignProfitKopecks"] == 9000
    filtered = router._repricer_list_summary(items, campaign_ads=filtered_campaign_advertising(items))
    assert filtered["adSpendKopecks"] == 2500
    assert filtered["expensesKopecks"] == 9500


def test_ads_stays_available_when_finance_is_missing_but_profit_does_not():
    summary = dict(adSpendSource="wb_ads_fullstats", adSpendKopecks=2500, campaignProfitKopecks=9000)
    coverage = {name: dict(state="no_data") for name in ("finance", "baskets")}
    apply_summary_availability(summary, coverage, {})
    assert summary["adSpendKopecks"] == 2500
    assert summary["expensesKopecks"] is summary["campaignProfitKopecks"] is None


def test_cabinet_profit_accounts_for_observed_shared_fees_without_fabricating_product_allocation():
    items = rows()
    items[0]["analytics"].update(settlementProfitKopecks=None,
        settlementBlockers=["settlement_account_expenses_unreconciled"])
    ads = campaign_advertising(snapshot(2500), START, END)
    apply_campaign_advertising(items, dict(aggregates={"101": dict(adSpendKopecks=1000)}), ads)
    raw = dict.fromkeys(("commissionKopecks", "logisticsKopecks", "storageKopecks", "acceptanceKopecks",
        "penaltyKopecks", "deductionKopecks", "loyaltyCostKopecks", "acquiringKopecks", "additionalPaymentKopecks"), 0)
    raw.update(adSpendKopecks=1000, logisticsKopecks=7000, storageKopecks=500)
    summary = router._repricer_list_summary(items, finance_diagnostics=dict(rawExpenseTotals=raw), campaign_ads=ads)
    assert summary["campaignProfitKopecks"] == 9000
    assert summary["expensesKopecks"] == 10000
    assert items[0]["analytics"]["campaignProfitKopecks"] is None
    assert items[0]["analytics"]["campaignAttributedProfitKopecks"] == 9500
    items[0]["analytics"]["settlementAttributedProfitKopecks"] = None
    assert router._repricer_list_summary(items, finance_diagnostics=dict(rawExpenseTotals=raw), campaign_ads=ads)["campaignProfitKopecks"] is None
