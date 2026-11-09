from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repricer_bff import _merge_ads_spend_aggregate_from_node
from tests.auth_helpers import auth_headers


def read_report(monkeypatch, aggregates, group_by):
    cached = {"aggregates": deepcopy(aggregates)} if aggregates is not None else {}
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.get_source_cache", lambda *args, **kwargs: cached
    )
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix",
        lambda *args, **kwargs: [],
    )
    api = TestClient(create_app())
    response = api.get(
        "/api/v1/wb-reports/ads/performance",
        params={"groupBy": group_by},
        headers=auth_headers(api, "viewer"),
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("group_by", ["campaign", "sku"])
@pytest.mark.parametrize("aggregates", [None, {}])
def test_missing_ads_cache_returns_valid_blocked_response_without_demo_data(
    monkeypatch, group_by, aggregates
):
    report = read_report(monkeypatch, aggregates, group_by)
    assert report["sourceStatus"] == "blocked"
    assert report["confidence"] == "blocked"
    assert {"WB-02", "WB_ADS_CACHE_EMPTY"} <= set(report["blockerIds"])
    assert report["rows"] == []
    assert all(value is None for value in report["totals"].values())


def test_campaign_only_cost_remains_campaign_level(monkeypatch):
    report = read_report(
        monkeypatch,
        {
            "campaign-only": {
                "campaignId": 55,
                "adSpendKopecks": 2_000,
                "ordersKopecks": 10_000,
            },
        },
        "campaign",
    )
    assert report["totals"]["adSpendKopecks"] == 2_000
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert row["attributionLevel"] == "campaign_only"
    assert row["confidence"] in {"low", "medium"}
    assert row["campaignId"] == "55"
    assert row["skuId"] is row["brandId"] is row["managerId"] is None


@pytest.mark.parametrize("has_sku", [False, True])
@pytest.mark.parametrize("campaign_key", ["campaign-only", "55"])
def test_sku_report_never_allocates_campaign_only_cost(
    monkeypatch, has_sku, campaign_key
):
    aggregates = {campaign_key: {"campaignId": 55, "adSpendKopecks": 2_000}}
    if has_sku:
        aggregates["101"] = {"nmId": 101, "adSpendKopecks": 1_000}
    report = read_report(monkeypatch, aggregates, "sku")
    assert {"WB-02", "WB_ADS_SKU_ATTRIBUTION_INCOMPLETE"} <= set(report["blockerIds"])
    assert report["sourceStatus"] == ("partial" if has_sku else "blocked")
    assert report["totals"]["adSpendKopecks"] == (1_000 if has_sku else None)
    assert [row["skuId"] for row in report["rows"]] == (["101"] if has_sku else [])


@pytest.mark.parametrize("group_by", ["campaign", "sku"])
def test_real_zero_sku_metrics_stay_zero_without_invented_identity(
    monkeypatch, group_by
):
    report = read_report(
        monkeypatch,
        {
            "101": {
                "nmId": 101,
                "adSpendKopecks": 0,
                "impressions": 0,
                "clicks": 0,
                "cartAdds": 0,
                "ordersCount": 0,
                "ordersKopecks": 0,
            }
        },
        group_by,
    )
    assert report["sourceStatus"] == "fresh"
    assert report["blockerIds"] == []
    assert report["totals"]["adSpendKopecks"] == 0
    row = report["rows"][0]
    assert row["skuId"] == "101"
    assert row["adSpendKopecks"] == 0
    assert row["brandId"] is row["managerId"] is None


def test_missing_metrics_do_not_turn_into_zero_or_partial_totals(monkeypatch):
    report = read_report(
        monkeypatch,
        {
            "101": {"nmId": 101, "adSpendKopecks": 100},
            "102": {"nmId": 102, "ordersKopecks": 200},
        },
        "sku",
    )
    assert report["sourceStatus"] == "partial"
    assert {"WB-02", "WB_ADS_METRICS_INCOMPLETE"} <= set(report["blockerIds"])
    assert report["rows"][0]["adSpendKopecks"] == 100
    assert report["rows"][1]["adSpendKopecks"] is None
    assert report["totals"]["adSpendKopecks"] is None
    assert report["totals"]["ordersKopecks"] is None
    assert report["totals"]["drrPct"] is report["totals"]["roiPct"] is None


def test_explicit_zero_metric_is_not_replaced_by_an_alias(monkeypatch):
    report = read_report(
        monkeypatch,
        {
            "101": {"nmId": 101, "adSpendKopecks": 0, "spendKopecks": 999},
        },
        "sku",
    )
    assert (
        report["rows"][0]["adSpendKopecks"] == report["totals"]["adSpendKopecks"] == 0
    )


def test_report_reads_actual_sync_producer_metrics_and_sku_key(monkeypatch):
    aggregates = {}
    _merge_ads_spend_aggregate_from_node(
        {
            "nm": 101,
            "sum": 10,
            "views": 100,
            "clicks": 10,
            "atbs": 3,
            "orders": 2,
            "sum_price": 100,
        },
        aggregates,
    )
    report = read_report(monkeypatch, aggregates, "sku")
    assert report["sourceStatus"] == "fresh"
    assert report["confidence"] == "high"
    assert report["blockerIds"] == []
    row = report["rows"][0]
    assert row["skuId"] == "101"
    assert row["attributionLevel"] == "exact_sku"
    for metrics in (row, report["totals"]):
        assert metrics["adSpendKopecks"] == 1_000
        assert metrics["ordersKopecks"] == 10_000
        assert metrics["drrPct"] == 10
        assert metrics["roiPct"] == 900


@pytest.mark.parametrize("campaign_key", ["campaign-only", "55"])
def test_complete_campaign_only_report_never_claims_high_confidence(
    monkeypatch, campaign_key
):
    report = read_report(
        monkeypatch,
        {
            campaign_key: {
                "campaignId": 55,
                "adSpendKopecks": 100,
                "impressions": 10,
                "clicks": 2,
                "cartAdds": 0,
                "ordersCount": 0,
                "ordersKopecks": 0,
            }
        },
        "campaign",
    )
    assert report["sourceStatus"] == "fresh"
    assert report["rows"][0]["attributionLevel"] == "campaign_only"
    assert report["rows"][0]["skuId"] is None
    assert report["confidence"] == "medium"


def test_producer_revenue_zero_is_not_replaced_by_legacy_alias(monkeypatch):
    report = read_report(
        monkeypatch,
        {"101": {"adSpendKopecks": 100, "adRevenueKopecks": 0, "adSalesKopecks": 999}},
        "sku",
    )
    assert report["rows"][0]["ordersKopecks"] == 0
    assert report["totals"]["ordersKopecks"] == 0
    assert report["totals"]["drrPct"] is None
    assert report["totals"]["roiPct"] == -100
