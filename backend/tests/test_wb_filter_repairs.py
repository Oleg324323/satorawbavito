"""Regression checks with isolated saved facts; no DB, account, or WB writes."""
import copy
import json
import unittest
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from starlette.requests import Request

from app.wb_repricer_filters import RepricerFilters, compile_filter, displayed_margin, attributes, garment
from app.routers import wb_repricer_bff as router
from app.wb_sales_brand_filter import project_sales_brands


def product(i, **facts):
    return {"meta": {"nmId": i, "articleId": f"SKU-{i}", "name": "Футболка female white", "subject": "Футболка",
                     "brand": "Alpha", "managerId": "me", "status": "manual", "currentPriceKopecks": 20001},
            "settings": {"pMinKopecks": 100},
            "analytics": {"financeState": "ok", "settlementFormulaVersion": "wb-final-payout-cogs-tax-v1",
                          "settlementProfitKopecks": 0, "revenueKopecks": 10000, "basketsState": "ok", "baskets": 0,
                          "stockState": "ok", "wbStockUnits": 5, "reportOrdersUnits": 0, **facts}}


class RepricerFilterRepairs(unittest.TestCase):
    def test_validation(self):
        for raw in ({"priceFrom": -1}, {"ordersFrom": 2, "ordersTo": 1}, {"marginFrom": "NaN"}, {"types": ["invented"]}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                RepricerFilters.model_validate(raw)

    def test_null_zero_decimal_and_kopecks(self):
        missing, zero, negative = product(1, settlementProfitKopecks=None), product(2), product(3, settlementProfitKopecks=-525)
        for query, expected in [({"marginTo": 0}, [2, 3]), ({"marginFrom": 0, "marginTo": 0}, [2]),
                                ({"marginFrom": "-5.25", "marginTo": "-5.25"}, [3])]:
            matcher = compile_filter(RepricerFilters(**query))
            self.assertEqual([r["meta"]["nmId"] for r in [missing, zero, negative] if matcher(r)], expected)
        self.assertTrue(compile_filter(RepricerFilters(priceFrom="200.01", priceTo="200.01"))(zero))
        self.assertFalse(compile_filter(RepricerFilters(priceTo=200))(zero))
        self.assertIsNone(displayed_margin(product(4, financeState="no_data", marginPct=0)))

    def test_unknown_facets_and_defaults(self):
        row = product(1)
        row["meta"].update(name="Unknown", subject="Unknown", articleId="OTHER")
        self.assertIsNone(garment(row))
        self.assertEqual(attributes(row), set())
        for query in ({}, {"types": ["shirt", "hoodie", "longsleeve"]}, {"types": []}, {"attrs": []}):
            self.assertTrue(compile_filter(RepricerFilters(**query))(row))
        self.assertFalse(compile_filter(RepricerFilters(types=["shirt"]))(row))
        self.assertFalse(compile_filter(RepricerFilters(attrs=["male"]))(row))
        self.assertNotIn("male", attributes(product(2)))
        card = {"characteristics": [{"name": "Пол", "value": ["male"]}, {"name": "Цвет", "value": ["black"]}]}
        self.assertEqual(attributes(product(2), card), {"male", "black"})
        self.assertTrue(compile_filter(RepricerFilters(attrs=["male"]), {"2": card})(product(2)))

    def test_promotion_tristate_and_combinations(self):
        rows = [product(i, promotionStatus=value) for i, value in enumerate(["yes", "no", None], 1)]
        self.assertEqual([r["meta"]["nmId"] for r in rows if compile_filter(RepricerFilters(promo="no"))(r)], [2])
        self.assertTrue(compile_filter(RepricerFilters(promo="yes", attrs=["female"], stock="ending"))(rows[0]))
        self.assertFalse(compile_filter(RepricerFilters(promo="no", attrs=["male"]))(rows[1]))

    def call_list(self, rows, *, snapshot=None, **query):
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        prefix = "app.routers.wb_repricer_bff."
        with ExitStack() as stack:
            for name, value in {"_hydrate_org_repricer_state": 9, "_cached_read_wb_token": None,
                    "cached_goods_meta": {"pagesCached": 1, "totalCached": len(rows)},
                    "_repricer_list_cache_meta": {"totalCached": len(rows), "periodCoverage": {}},
                    "_period_source_cache": {}, "_load_repricer_sku_snapshot_page": snapshot,
                    "filtered_campaign_advertising": {}, "_apply_period_reporting": None}.items():
                stack.enter_context(patch(prefix + name, return_value=value))
            stack.enter_context(patch(prefix + "actor_from_request", return_value=SimpleNamespace(user_id="me")))
            read = stack.enter_context(patch(prefix + "_list_repricer_skus_for_request", return_value=copy.deepcopy(rows)))
            stack.enter_context(patch("app.repricer_bff._frontend_strategy_payload", return_value={"id": None, "name": None, "typedStrategyId": None}))
            stack.enter_context(patch(prefix + "_repricer_list_summary", side_effect=lambda selected, **kw: {"skuCount": len(selected)}))
            result = router.get_sku_list(request, scenario="complete", include_promotions=False, include_content=False,
                period_days=7, date_from=date(2026, 10, 19), date_to=date(2026, 10, 25), page=1, page_size=150,
                top_mode=True, q="", status="all", brand="all", **query)
            return result, read.call_args.kwargs if read.called else None

    def test_route_filters_before_pagination_and_resolves_mine(self):
        rows = [product(i, baskets=100 if i == 151 else 0) for i in range(1, 152)]
        result, args = self.call_list(rows, manager="mine", filters=json.dumps({"basketsFrom": 100}))
        self.assertEqual([r["meta"]["nmId"] for r in result["items"]], [151])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["summary"]["skuCount"], 1)
        self.assertIsNone(args["max_items"])
        self.assertTrue(result["cache"]["serverFiltersApplied"])
        result, _ = self.call_list(rows, manager="unassigned", filters=None)
        self.assertEqual(result["total"], 0)

    def test_exact_identity_basic_override_does_not_override_advanced(self):
        row = product(1)
        self.assertTrue(router._row_matches_sku_filters(row, q="SKU-1", status="auto", brand="Other", manager="other"))
        self.assertFalse(compile_filter(RepricerFilters(basketsFrom=1))(row))

    def test_unrestricted_snapshot_fast_path_and_manager_scopes(self):
        rows = [product(i) for i in range(1, 4)]
        snapshot = ({"total": 3, "summary": {"skuCount": 3}, "cache": {"totalCached": 3}}, rows)
        result, built = self.call_list(rows, snapshot=snapshot, manager="all", filters=None)
        self.assertIsNone(built)
        self.assertEqual(result["total"], 3)
        rows[1]["meta"]["managerId"] = "other"
        rows[2]["meta"]["managerId"] = None
        for manager, expected in [("mine", [1]), ("other", [2]), ("unassigned", [3])]:
            result, _ = self.call_list(rows, manager=manager, filters=None)
            self.assertEqual([row["meta"]["nmId"] for row in result["items"]], expected)
        # An advanced selection cannot read an incompatible unfiltered snapshot.
        result, built = self.call_list(rows, snapshot=snapshot, manager="all", filters='{"basketsFrom":1}')
        self.assertIsNotNone(built)
        self.assertEqual(result["items"], [])

    def test_normalization_segments_and_unchanged_financial_facts(self):
        first = RepricerFilters(types=["shirt", "hoodie", "shirt"], attrs=["black", "male"])
        second = RepricerFilters(types=["hoodie", "shirt"], attrs=["male", "black"])
        self.assertEqual(first.model_dump(), second.model_dump())
        row = product(1, settlementProfitKopecks=525, wbStockUnits=100)
        row["settings"]["effectivePMinKopecks"] = 100
        before = copy.deepcopy(row)
        self.assertTrue(compile_filter(RepricerFilters(segment="problem"))(row))  # Existing threshold is <10%, not only losses.
        self.assertEqual(row, before)
        row["analytics"].update(revenueKopecks=0, settlementProfitKopecks=-100, salesUnits=2, returnsUnits=1)
        self.assertTrue(compile_filter(RepricerFilters(segment="loss"))(row))
        self.assertIsNone(displayed_margin(row))


class SalesBrandRepairs(unittest.TestCase):
    def project(self, brands, complete=True, missing_buyout=False, finance_rows=None):
        from app.routers import wb_reports_bff as reports
        start, end = date(2026, 10, 19), date(2026, 10, 25)
        # Alpha sits beyond the unfiltered top-20. Filter before truncation.
        rows = {str(i): {"vendorCode": f"SKU-{i}", "productName": f"Product {i}", "openCount": 10, "cartCount": 2,
            "orderCount": i, "orderSumKopecks": i * 100, "buyoutCount": i, "buyoutSumKopecks": i * 100,
            "cancelCount": 0, "buyoutCountProvided": not missing_buyout, "previous": {"orderCount": 0, "buyoutCount": 0, "buyoutCountProvided": True}} for i in range(1, 23)}
        cache = {"dateFrom": str(start), "dateTo": str(end), "comparisonDateFrom": "2026-10-12", "comparisonDateTo": "2026-10-18",
                 "aggregates": rows, "aggregateComplete": complete, "fetchedAt": "2026-11-01T09:00:00Z"}
        catalog = {str(i): {"brand": "Alpha" if i == 1 else "Beta"} for i in range(1, 23)}
        finance = {"aggregates": {str(i): {"returnsUnits": 0} for i in range(1, 23)}} if finance_rows is not None else {}
        with patch("app.wb_sales_brand_filter.saved_catalog", return_value=({"marketplaceAccountId": 12}, catalog)), \
             patch("app.wb_sales_brand_filter._saved_cache", side_effect=lambda org, key, ctx: cache if key.startswith("baskets_") else finance), \
             patch("app.wb_reports_sprint_d._complete_finance_ledger", return_value=bool(finance)), \
             patch("app.wb_reports_sprint_d._get_cached_pnl_response", return_value=SimpleNamespace(sourceStatus="cached", rows=finance_rows)):
            before = copy.deepcopy(cache)
            result = project_sales_brands(SimpleNamespace(organization_id=9), start, end, brands, finance_allowed=finance_rows is not None)
            self.assertEqual(cache, before)
            return result

    def test_brand_before_top20_and_totals(self):
        result = self.project(["Alpha"])
        self.assertEqual([r["nmId"] for r in result["topMovers"]["rows"]], [1])
        self.assertEqual(result["funnelSummary"]["orderCount"], 1)
        self.assertEqual(result["availableBrands"], ["Alpha", "Beta"])
        self.assertEqual(result["brandScope"], ["alpha"])
        self.assertIsNone(result["weeklyBalance"]["points"][0]["ordersUnits"])

    def test_multi_empty_and_incomplete(self):
        self.assertEqual(self.project(["Alpha", "Beta"])["funnelSummary"]["orderCount"], sum(range(1, 23)))
        self.assertEqual(self.project(["Absent"])["funnelSummary"]["orderCount"], 0)
        partial = self.project(["Alpha"], complete=False)
        self.assertIsNone(partial["funnelSummary"]["orderCount"])
        self.assertFalse(partial["cache"]["fresh"])
        self.assertEqual(next(k["value"] for k in partial["kpis"] if k["id"] == "orders_qty"), "—")
        self.assertIsNone(partial["periodCards"][0]["ordersUnits"])
        buyouts = self.project(["Alpha"], missing_buyout=True)
        self.assertEqual(next(k["value"] for k in buyouts["kpis"] if k["id"] == "sales_revenue"), "—")
        self.assertIsNone(buyouts["periodCards"][0]["revenueKopecks"])

    def test_normal_and_latest_paths_dispatch_same_scope_without_source_jobs(self):
        from app.routers import wb_reports_bff as reports
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        actor = SimpleNamespace(organization_id=9, user_id="me")
        with patch.object(reports, "actor_from_request", return_value=actor), \
             patch.object(reports, "assert_permission_or_audit"), \
             patch.object(reports, "has_permission", return_value=False), \
             patch("app.wb_sales_brand_filter.project_sales_brands", return_value={"brandScope": ["alpha"]}) as project:
            normal = reports.get_reports_digest(request, preset="custom", from_="2026-10-19", to="2026-10-25", brand=["Alpha"])
            latest = reports.get_reports_latest_cache.__wrapped__(request, "digest", preset="custom", from_="2026-10-19", to="2026-10-25", brand=["Alpha"])
            self.assertEqual(normal, latest)
            self.assertEqual(project.call_count, 2)
            self.assertEqual(project.call_args.args, (actor, date(2026, 10, 19), date(2026, 10, 25), ["Alpha"]))

    def test_brand_selects_saved_product_profit_without_reallocating_cabinet_amounts(self):
        rows = [SimpleNamespace(nmId=i, netProfitKopecks=i * 25) for i in range(1, 23)]
        rows.append(SimpleNamespace(nmId=None, netProfitKopecks=999999))
        result = self.project(["Alpha"], finance_rows=rows)
        self.assertEqual(next(k["value"] for k in result["kpis"] if k["id"] == "margin_profit"), "25")
        self.assertEqual(rows[-1].netProfitKopecks, 999999)
        result = self.project(["Alpha"], finance_rows=rows[1:])
        self.assertEqual(next(k["value"] for k in result["kpis"] if k["id"] == "margin_profit"), "—")


if __name__ == "__main__":
    unittest.main()
