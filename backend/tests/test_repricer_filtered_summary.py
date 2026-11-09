from datetime import date
from types import SimpleNamespace

import pytest

from app.routers import wb_repricer_bff as router


@pytest.mark.parametrize("revenue,profit,expected", [
    (-10_000, -2_500, 25.0),
    (-10_000, 2_500, -25.0),
    (0, 2_500, None),
    (10_000, 2_500, 25.0),
])
def test_margin_percentage_keeps_ratio_of_totals_for_signed_revenue(revenue, profit, expected):
    summary = router._repricer_list_summary([
        {"analytics": {"revenueKopecks": revenue, "netProfitKopecks": profit, "marginPct": 99}},
    ])
    assert summary["avgMarginPct"] == expected
    assert summary["revenueKopecks"] == revenue
    assert summary["marginKopecks"] == profit


def test_empty_summary_has_no_margin_percentage():
    assert router._repricer_list_summary([])["avgMarginPct"] is None


@pytest.mark.parametrize("filters", [
    {"q": "selected"},
    {"brand": "selected"},
    {"manager": "selected"},
    {"status": "manual"},
    {"q": "absent"},
])
def test_filtered_summary_covers_every_selected_page_without_global_costs(monkeypatch, filters):
    # This isolated filter arithmetic fixture predates the page-finance contract
    # and supplies neither nmId nor period coverage. Test that contract separately.
    monkeypatch.setattr(router, "_page_finance_active", lambda: False)
    rows = [
        {
            "meta": {"articleId": f"SKU_{index}", "name": "selected", "brand": "selected",
                     "managerId": "selected", "status": "manual"},
            "analytics": {"revenueKopecks": 100_000, "netProfitKopecks": 50_000,
                          "cogsTotalKopecks": 40_000, "expensesKopecks": 10_000,
                          "storageKopecks": 500, "adSpendKopecks": 1_000,
                          "salesUnits": 2, "returnsUnits": 1, "ordersUnits": 3},
        }
        for index in range(26)
    ]
    rows.append({"meta": {"articleId": "EXCLUDED", "brand": "other", "status": "liquidation"},
                 "analytics": {"revenueKopecks": 9_000_000, "netProfitKopecks": 8_000_000}})
    monkeypatch.setattr(router, "_hydrate_org_repricer_state", lambda _: 1)
    monkeypatch.setattr(router, "cached_goods_meta", lambda _: {"totalCached": len(rows)})
    monkeypatch.setattr(router, "_list_repricer_skus_for_request", lambda *a, **kw: rows)
    monkeypatch.setattr(router, "_repricer_list_cache_meta", lambda *a, **kw: {"totalCached": len(rows)})
    monkeypatch.setattr(router, "_period_source_cache", lambda *a, **kw: {
        "diagnostics": {"rawExpenseTotals": {"storageKopecks": 90_000, "adSpendKopecks": 40_000}},
    })

    for page in (1, 2):
        payload = router.get_sku_list(
            SimpleNamespace(query_params={}), scenario="complete", include_promotions=False,
            include_content=False, period_days=7, date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 7), page=page, page_size=25, top_mode=True,
            **({"q": "", "brand": "all", "manager": "all", "status": "all"} | filters),
        )
        count = 0 if filters.get("q") == "absent" else 26
        assert payload["total"] == count
        assert payload["itemsReturned"] == (0 if count == 0 else 25 if page == 1 else 1)
        summary = payload["summary"]
        assert summary["skuCount"] == count
        assert summary["revenueKopecks"] == count * 100_000
        assert summary["marginKopecks"] == count * 50_000
        assert summary["avgMarginPct"] == (50.0 if count else None)
        assert summary["cogsKopecks"] == count * 40_000
        assert summary["expensesKopecks"] == count * 10_000
        assert summary["storageKopecks"] == count * 500
        assert summary["adSpendKopecks"] == count * 1_000
        assert summary["salesUnits"] == summary["returnsUnits"] == count
        assert payload["cache"]["summaryScope"] == "filtered_skus"
