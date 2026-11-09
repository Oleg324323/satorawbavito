from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from hashlib import sha1

import pytest
from fastapi.testclient import TestClient

from app import wb_reports_sprint_d
from app.repricer_bff import FINANCE_REVENUE_BASIS
from app.main import create_app
from app.repricer_cache.store import FINANCE_SCHEMA_VERSION
from app.wb_reports_sprint_d import build_abc_report, build_pnl_report
from tests.auth_helpers import auth_headers


def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def isolated_bff_presentation_cache(monkeypatch):
    """Presentation fixtures use in-memory persistence; SQL has separate tests."""
    from app.routers import wb_reports_bff as reports
    cache = {}
    monkeypatch.setattr(reports, 'get_source_cache', lambda org, key, **kw: cache.get((org, key)))
    monkeypatch.setattr(reports, 'save_source_cache', lambda org, key, value: cache.__setitem__((org, key), value))
    monkeypatch.setattr(reports, 'get_source_cache_fetched_at', lambda *a, **kw: None)
    monkeypatch.setattr(reports, 'list_source_cache_ranges_by_prefix', lambda *a, **kw: [])
    monkeypatch.setattr(reports, '_report_daily_sources_ready', lambda *a, **kw: (True, []))
    monkeypatch.setattr(reports, '_abc_economics_version', lambda org: 'synthetic-version')
    monkeypatch.setattr('app.platform.economics.legacy_catalog.catalog_facts', lambda org, **kw: {})


def _install_saved_ads_snapshot(monkeypatch):
    from app.routers import wb_reports_bff as reports
    cache = {}
    def build(**kw):
        kw.pop('organization_id', None)
        return reports.build_ads_attribution_snapshot(**kw)
    monkeypatch.setattr(reports, 'build_cached_ads_attribution_snapshot', build)
    monkeypatch.setattr(reports, 'get_ads_report_cache', lambda **kw: cache.get('report'))
    def save(**kw):
        payload = {**kw['payload'], 'cache': {'status': 'refreshed'}}
        cache['report'] = {**payload, 'cache': {'status': 'hit'}}
        return payload
    monkeypatch.setattr(reports, 'save_ads_report_cache', save)
    monkeypatch.setattr(reports, 'save_ads_history_snapshots', lambda **kw: None)


@pytest.fixture(autouse=True)
def confirmed_tax(monkeypatch):
    """These source/expense fixtures use explicit synthetic tax; dated policy has its own SQL tests."""
    amounts = {}
    monkeypatch.setattr(wb_reports_sprint_d, "legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    monkeypatch.setattr("app.routers.wb_reports_bff.legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    def synthetic_economics(org, cache, period, **kwargs):
        # Preserve explicit expense inputs of these provider-free presentation
        # fixtures. Real canonical cost/date resolution has SQL integration tests.
        goods = wb_reports_sprint_d._goods_index(org)
        runtime = wb_reports_sprint_d.load_runtime_state(org) or {}
        algorithm = {**wb_reports_sprint_d.DEFAULT_ALGORITHM_SETTINGS, **(wb_reports_sprint_d.load_algorithm_settings(org) or {})}
        result = {}
        for nm, fact in cache.get('aggregates', {}).items():
            article = goods.get(int(nm), {}).get('vendorCode') or fact.get('vendorCode') or f'NM_{nm}'
            cost, settings = wb_reports_sprint_d._abc_period_cogs(nm_id=int(nm), article_id=article,
                finance=fact, finance_cache=cache, runtime=runtime, algorithm=algorithm,
                date_from=period.date_from, date_to=period.date_to)
            result[str(nm)] = {'taxKopecks': amounts.get(str(nm), 0), 'factTaxState': 'configured',
                'factTaxReason': None, 'settlementCogsKopecks': cost,
                'costEffectiveFrom': settings.get('cogsEffectiveFrom')}
        return result
    monkeypatch.setattr(wb_reports_sprint_d, "get_legacy_finance_taxes", synthetic_economics)
    return amounts


def _isolate_pnl_source_cache(monkeypatch):
    """Synthetic cache inputs for legacy HTTP contracts; no live source or disk I/O."""
    monkeypatch.setattr('app.platform.economics.legacy_catalog.catalog_facts', lambda org, **kw: {})
    def cached_source(_organization_id, source_key, **_kwargs):
        if source_key == "finance_2026-05-01_2026-05-28":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "aggregates": {"101": {
                    "salesUnits": 1, "sellerRevenueKopecks": 100_000,
                    "commissionKopecks": 10_000, "reportedCommissionRows": 1,
                }},
            }
        if source_key == "ads_2026-05-01_2026-05-28":
            return {"aggregates": {"101": {"adSpendKopecks": 1_000}}}
        return None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", cached_source)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _org: [{"nmID": 101, "vendorCode": "TEST"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _org: {})
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _org: {})


def test_pnl_finance_viewer_gets_financial_fields_and_preliminary_state(monkeypatch):
    _isolate_pnl_source_cache(monkeypatch)
    api = client()
    response = api.get("/api/v1/wb-reports/pnl", headers=auth_headers(api, "finance_viewer"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["reportState"] == "preliminary"
    assert payload["totals"]["revenueKopecks"] is not None
    assert payload["totals"]["netProfitKopecks"] is not None
    assert "WB-11" not in payload["blockerIds"]


def _signed_pnl_source(monkeypatch, credit):
    _isolate_pnl_source_cache(monkeypatch)
    read = wb_reports_sprint_d.get_source_cache

    def cached(org, key, **kwargs):
        value = read(org, key, **kwargs)
        if value and key.startswith("finance_"):
            value["aggregates"] = {"101": {"salesUnits": 1, "sellerRevenueKopecks": 10000,
                                          "additionalPaymentKopecks": credit}}
        if value and key.startswith("ads_"):
            value["aggregates"] = {"101": {"adSpendKopecks": 0}}
        return value

    monkeypatch.setattr(wb_reports_sprint_d, "get_source_cache", cached)
    monkeypatch.setattr(wb_reports_sprint_d, "load_runtime_state", lambda org: {
        "skuSettingsOverrides": {"TEST": {"cogsKopecks": 0}}})
    monkeypatch.setattr(wb_reports_sprint_d, "load_algorithm_settings", lambda org: {
        "taxPct": 0, "otherExpensePricePct": 0, "otherExpensePerSaleRub": 0})


@pytest.mark.parametrize("credit,expected", [(-1000, 9000), (1000, 11000), (0, 10000)])
def test_pnl_preserves_signed_withdrawal_adjustment(monkeypatch, credit, expected):
    _signed_pnl_source(monkeypatch, credit)
    result = build_pnl_report(date(2026, 5, 1), date(2026, 5, 28), "sku", "preliminary", True, 77)
    assert result.rows[0].netProfitKopecks == expected
    assert result.totals.netProfitKopecks == expected


@pytest.mark.parametrize('group_by', ['sku', 'brand'])
def test_pnl_saved_catalog_identity_and_sales_order(monkeypatch, group_by):
    _signed_pnl_source(monkeypatch, 0)
    read = wb_reports_sprint_d.get_source_cache
    def cache(org, key, **kwargs):
        value = read(org, key, **kwargs)
        if value and key.startswith('finance_'):
            value['aggregates']['202'] = {'salesUnits': 2, 'sellerRevenueKopecks': 20000}
        return value
    monkeypatch.setattr(wb_reports_sprint_d, 'get_source_cache', cache)
    monkeypatch.setattr(wb_reports_sprint_d, 'list_cached_goods', lambda org: [
        {'nmID': 101, 'vendorCode': 'A-first-alphabetically', 'title': 'Smaller sales', 'brand': 'one'},
        {'nmID': 202, 'vendorCode': 'Z-higher-sales', 'title': 'Higher sales', 'brand': 'two',
         'subjectName': 'Clothing', 'photoUrl': 'https://basket-01.wbbasket.ru/saved.webp'},
    ])
    monkeypatch.setattr('app.platform.economics.legacy_catalog.catalog_facts', lambda org, **kw: {})
    report = build_pnl_report(date(2026, 5, 1), date(2026, 5, 28), group_by, 'preliminary', True, 77)
    assert [r.revenueKopecks for r in report.rows] == ([20000, 10000] if group_by == 'sku' else [10000, 20000])
    assert report.totals.revenueKopecks == 30000
    row = report.rows[0]
    if group_by == 'sku':
        assert (row.nmId, row.sku, row.productName, row.category) == (202, 'Z-higher-sales', 'Higher sales', 'Clothing')
        assert row.photoUrl == 'https://basket-01.wbbasket.ru/saved.webp'
    else:
        assert row.nmId is row.sku is row.productName is row.photoUrl is None


def test_pnl_orders_use_saved_funnel_demand_without_altering_revenue(monkeypatch):
    _signed_pnl_source(monkeypatch, 0)
    read = wb_reports_sprint_d.get_source_cache
    def cache(org, key, **kwargs):
        if key == 'baskets_2026-05-01_2026-05-28':
            return {'aggregates': {'101': {'orderCount': 30}, '202': {'orderCount': 10}}}
        value = read(org, key, **kwargs)
        if value and key.startswith('finance_'):
            value['aggregates']['202'] = {'salesUnits': 2, 'sellerRevenueKopecks': 20000}
        return value
    monkeypatch.setattr(wb_reports_sprint_d, 'get_source_cache', cache)
    report = build_pnl_report(date(2026, 5, 1), date(2026, 5, 28), 'sku', 'preliminary', True, 77)
    assert [r.nmId for r in report.rows] == [101, 202]
    assert report.totals.revenueKopecks == 30000


@pytest.mark.parametrize("credit,expected", [(-1000, 9000), (1000, 11000), (0, 10000)])
def test_pnl_http_preserves_signed_withdrawal_adjustment(monkeypatch, credit, expected):
    _signed_pnl_source(monkeypatch, credit)
    api = client()
    response = api.get("/api/v1/wb-reports/pnl", headers=auth_headers(api, "finance_viewer"))
    assert response.status_code == 200
    assert response.json()["rows"][0]["netProfitKopecks"] == expected
    assert response.json()["totals"]["netProfitKopecks"] == expected


def test_pnl_rejects_previous_internal_cache_and_reuses_corrected_report(monkeypatch):
    _signed_pnl_source(monkeypatch, -1000)
    args = (date(2026, 5, 1), date(2026, 5, 28), "sku", "preliminary", True, 77)
    stale = build_pnl_report(*args).model_dump(mode="json")
    stale["rows"][0]["netProfitKopecks"] = 11000
    stale["totals"]["netProfitKopecks"] = 11000
    old_key = "pnl_report_" + sha1(b"v3|2026-05-01|2026-05-28|sku|preliminary|finance").hexdigest()[:16]
    entries = {(77, old_key): {"report": stale, "fetchedAt": datetime.now(timezone.utc).isoformat()}}
    source = wb_reports_sprint_d.get_source_cache
    monkeypatch.setattr(wb_reports_sprint_d, "get_source_cache", lambda org, key, **kw: entries.get((org, key)) or source(org, key, **kw))
    monkeypatch.setattr(wb_reports_sprint_d, "save_source_cache", lambda org, key, value: entries.__setitem__((org, key), {**value, "fetchedAt": datetime.now(timezone.utc).isoformat()}))
    corrected = build_pnl_report(*args)
    assert corrected.rows[0].netProfitKopecks == corrected.totals.netProfitKopecks == 9000
    monkeypatch.setattr(wb_reports_sprint_d, "get_source_cache", lambda org, key, **kw: entries.get((org, key)))
    assert build_pnl_report(*args) == corrected
    assert build_pnl_report(*args[:-1], 78).rows == []
    assert build_pnl_report(*args[:4], False, 77).rows == []


@pytest.mark.parametrize("path", ["exact", "covering"])
def test_pnl_rejects_previous_finance_schema(monkeypatch, path):
    _signed_pnl_source(monkeypatch, 1000)
    source = wb_reports_sprint_d.get_source_cache
    old = source(77, "finance_2026-05-01_2026-05-28")
    old.update(financeSchemaVersion="v3", dateFrom="2026-05-01", dateTo="2026-05-28")
    old["aggregates"]["101"]["paymentScheduleKopecks"] = 1000
    old["dailyAggregates"] = {"2026-05-01": old["aggregates"]}
    key = "finance_2026-05-01_2026-05-28" if path == "exact" else "finance_2026-04-01_2026-05-31"
    if path == "covering":
        old.update(dateFrom="2026-04-01", dateTo="2026-05-31")
    monkeypatch.setattr(wb_reports_sprint_d, "get_source_cache", lambda org, requested, **kw: old if requested == key else None)
    monkeypatch.setattr(wb_reports_sprint_d, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [{**old, "sourceKey": key}])
    result = build_pnl_report(date(2026, 5, 1), date(2026, 5, 28), "sku", "preliminary", True, 77)
    assert result.rows == []
    assert result.sourceStatus == "blocked"


def test_pnl_supports_operative_and_final_states(monkeypatch):
    _isolate_pnl_source_cache(monkeypatch)
    api = client()

    operative = api.get(
        "/api/v1/wb-reports/pnl",
        params={"source": "operative"},
        headers=auth_headers(api, "finance_viewer"),
    )
    assert operative.status_code == 200
    assert operative.json()["reportState"] == "operative"

    final = api.get(
        "/api/v1/wb-reports/pnl",
        params={"source": "final"},
        headers=auth_headers(api, "finance_viewer"),
    )
    assert final.status_code == 200
    final_payload = final.json()
    assert final_payload["reportState"] == "final"
    assert final_payload["blockerIds"] == []


def test_ads_weak_attribution_stays_campaign_level_and_not_sku_level(monkeypatch):
    metrics = {"adSpendKopecks": 500, "impressions": 100, "clicks": 10,
               "cartAdds": 3, "ordersCount": 2, "ordersKopecks": 10_000}
    cache = {"aggregates": {
        "campaign-only": {"campaignId": 55, **metrics},
        "101": {"nmId": 101, "campaignId": 56, **metrics},
    }}
    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", lambda *_args, **_kwargs: cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    api = client()

    campaign = api.get(
        "/api/v1/wb-reports/ads/performance",
        params={"groupBy": "campaign"},
        headers=auth_headers(api, "finance_viewer"),
    )
    assert campaign.status_code == 200
    assert any(row["attributionLevel"] == "campaign_only" for row in campaign.json()["rows"])
    assert "WB-02" not in campaign.json()["blockerIds"]

    sku = api.get(
        "/api/v1/wb-reports/ads/performance",
        params={"groupBy": "sku"},
        headers=auth_headers(api, "finance_viewer"),
    )
    assert sku.status_code == 200
    assert sku.json()["rows"][0]["attributionLevel"] in {"exact_sku", "campaign_sku"}


def test_ads_bff_exposes_confirmed_budget_balance_upd_and_daily_sources(monkeypatch):
    _install_saved_ads_snapshot(monkeypatch)
    api = client()
    response = api.get("/api/wb/reports/ads", headers=auth_headers(api, "finance_viewer"))

    assert response.status_code == 200
    payload = response.json()
    kpis = {item["id"]: item for item in payload["kpis"]}
    assert int(kpis["campaign_budget"]["value"]) > 0
    assert int(kpis["cabinet_balance"]["value"]) > 0
    assert int(kpis["upd_rows"]["value"]) > 0
    assert int(kpis["daily_rows"]["value"]) > 0
    assert payload["spendDocuments"][0]["adSpendKopecks"] > 0
    assert payload["dailyRows"][0]["date"]
    assert any(row["unallocatedSpend"] for row in payload["rows"])
    assert any(source["sourceId"] == "wb-ads-budget" for source in payload["sourceEvidence"])
    assert any(source["sourceId"] == "wb-ads-balance" for source in payload["sourceEvidence"])
    assert any(source["sourceId"] == "wb-ads-upd" for source in payload["sourceEvidence"])


def test_ads_bff_refreshes_and_reuses_cached_report_payload(monkeypatch):
    _install_saved_ads_snapshot(monkeypatch)
    api = client()
    headers = auth_headers(api, "finance_viewer")
    params = {"preset": "custom", "from": "2026-07-01", "to": "2026-07-01"}

    refreshed = api.post("/api/wb/reports/ads/refresh", params=params, headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["cache"]["status"] == "refreshed"

    def fail_live_builder(*_args, **_kwargs):
        raise AssertionError("GET must use ads report cache after refresh")

    monkeypatch.setattr("app.routers.wb_reports_bff.build_ads_attribution_snapshot", fail_live_builder)
    cached = api.get("/api/wb/reports/ads", params=params, headers=headers)

    assert cached.status_code == 200
    payload = cached.json()
    assert payload["cache"]["status"] == "hit"
    assert payload["rows"]


def test_rnp_has_confirmed_drr_formula_and_finance_viewer_can_see_roi(monkeypatch):
    from tests.rnp_cache_fixture import install_rnp_cache

    install_rnp_cache(monkeypatch, date(2026, 5, 1), date(2026, 5, 28))
    api = client()
    response = api.get("/api/v1/wb-reports/rnp", headers=auth_headers(api, "finance_viewer"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["drrPct"] is not None
    assert payload["rows"][0]["roiPct"] is not None
    assert "WB-02" not in payload["blockerIds"]
    assert "WB-11" not in payload["blockerIds"]


def test_plan_fact_and_export_are_available_for_finance_viewer():
    api = client()

    plan_fact = api.get(
        "/api/v1/wb-reports/plan-fact",
        params={"dimension": "manager"},
        headers=auth_headers(api, "finance_viewer"),
    )
    assert plan_fact.status_code == 200
    row = plan_fact.json()["rows"][0]
    assert row["planKopecks"] is not None
    assert row["factKopecks"] is not None

    export = api.get(
        "/api/v1/wb-export/current-view",
        params={"filters": "group=locomotive"},
        headers=auth_headers(api, "finance_viewer"),
    )
    assert export.status_code == 200
    export_payload = export.json()
    assert export_payload["exportState"] == "ready"
    assert export_payload["fileName"] is not None


def test_viewer_sees_all_financial_sections_except_monthly_company_costs(monkeypatch):
    _isolate_pnl_source_cache(monkeypatch)
    api = client()
    viewer_headers = auth_headers(api, "viewer")

    pnl = api.get("/api/v1/wb-reports/pnl", headers=viewer_headers)
    assert pnl.status_code == 200
    pnl_payload = pnl.json()
    assert pnl_payload["rows"][0]["overheadKopecks"] is None
    assert pnl_payload["rows"][0]["adSpendKopecks"] is not None
    assert pnl_payload["manualCosts"][-1]["costId"] == "overhead"
    assert pnl_payload["manualCosts"][-1]["amountKopecks"] is None

    export = api.get("/api/v1/wb-export/current-view", headers=viewer_headers)
    assert export.status_code == 200
    assert export.json()["exportState"] == "ready"


def test_pnl_uses_repricer_finance_cache_before_legacy_runtime(monkeypatch, confirmed_tax):
    confirmed_tax["123456"] = 12_000
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda *_args, **_kwargs: None)
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key == "finance_2026-06-01_2026-06-30":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "dateFrom": "2026-06-01",
                "dateTo": "2026-06-30",
                "aggregates": {
                    "123456": {
                        "salesUnits": 2,
                        "sellerRevenueKopecks": 200_000,
                        "buyerRevenueKopecks": 190_000,
                        "commissionFormulaKopecks": 20_000,
                        "commissionKopecks": 18_000,
                        "reportedCommissionRows": 1,
                        "logisticsKopecks": 7_000,
                        "storageKopecks": 1_000,
                        "acceptanceKopecks": 500,
                        "penaltyKopecks": -300,
                        "deductionKopecks": 200,
                        "additionalPaymentKopecks": 100,
                        "acquiringKopecks": 3_000,
                    }
                },
            }
        if source_key == "ads_2026-06-01_2026-06-30":
            return {
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "dateFrom": "2026-06-01",
                "dateTo": "2026-06-30",
                "aggregates": {"123456": {"adSpendKopecks": 4_000}},
            }
        return None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.list_cached_goods",
        lambda _organization_id: [{"nmID": 123456, "vendorCode": "FBBT_42", "brand": "Satorna", "subjectName": "Футболки"}],
    )
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_runtime_state",
        lambda _organization_id: {
            "skuSettingsOverrides": {"FBBT_42": {"cogsKopecks": 45_000}},
            "skuMetaOverrides": {"FBBT_42": {"managerId": "maria", "status": "locomotive"}},
        },
    )
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_algorithm_settings",
        lambda _organization_id: {"taxPct": 6, "otherExpensePricePct": 5, "otherExpensePerSaleRub": 0},
    )

    payload = build_pnl_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        requested_state="final",
        finance_allowed=True,
        organization_id=77,
    )

    row = payload.rows[0]
    assert row.rowId == "nm-123456"
    assert row.cogsKopecks == 90_000
    # Existing v3 contract prefers reported commission over the formula estimate.
    assert row.commissionKopecks == 18_000
    assert row.logisticsKopecks == 7_000
    assert row.storageKopecks == 1_000
    assert row.adSpendKopecks == 4_000
    assert row.taxKopecks == 12_000
    assert row.overheadKopecks == 13_300  # Includes acquiring/acceptance/deductions minus credits.
    assert row.netProfitKopecks == 54_700
    assert payload.reportState == "final"
    assert payload.sourceEvidence[0].sourceId == "wb-finance-sales-reports-detailed-cache"
    assert all(mapping.sourceId != "wb-statistics-realization-details" for mapping in payload.fieldMapping)


def test_pnl_finance_cache_does_not_require_live_ads_token(monkeypatch):
    from app import wb_reports_sprint_d

    assert not hasattr(wb_reports_sprint_d, "build_ads_attribution_snapshot")
    monkeypatch.setattr(wb_reports_sprint_d, "list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wb_reports_sprint_d, "save_source_cache", lambda *_args, **_kwargs: None)
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key == "finance_2026-06-01_2026-06-30":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "aggregates": {
                    "123456": {
                        "salesUnits": 1,
                        "sellerRevenueKopecks": 100_000,
                        "commissionKopecks": 10_000,
                        "logisticsKopecks": 5_000,
                    }
                },
            }
        return None

    live_calls = []

    def fail_live_ads_call(*_args, **_kwargs):
        live_calls.append(True)
        raise AssertionError("P&L must use cache only")

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_api.ads_runtime.build_ads_attribution_snapshot", fail_live_ads_call)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 123456, "vendorCode": "FBBT_42"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _organization_id: {})
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _organization_id: {"taxPct": 0})

    payload = build_pnl_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        requested_state="final",
        finance_allowed=True,
        organization_id=77,
    )

    assert payload.sourceStatus == "partial"
    assert payload.confidence == "medium"
    assert payload.blockerIds == ["WB-02"]
    assert payload.reportState == "preliminary"
    assert payload.rows[0].adSpendKopecks is None
    assert payload.rows[0].netProfitKopecks is None
    assert any(evidence.sourceId == "wb-ads-attribution-cache" for evidence in payload.sourceEvidence)
    assert live_calls == []


def test_pnl_uses_repricing_period_cache_when_exact_range_cache_is_missing(monkeypatch):
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda *_args, **_kwargs: None)
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key == "finance_30":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "aggregates": {
                    "123456": {
                        "salesUnits": 2,
                        "sellerRevenueKopecks": 200_000,
                        "commissionFormulaKopecks": 20_000,
                        "logisticsKopecks": 7_000,
                        "penaltyKopecks": 1_000,
                    }
                },
            }
        return None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 123456, "vendorCode": "FBBT_42"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _organization_id: {})
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _organization_id: {"taxPct": 0})

    payload = build_pnl_report(
        date_from=date(2026, 6, 2),
        date_to=date(2026, 7, 1),
        group_by="sku",
        requested_state="preliminary",
        finance_allowed=True,
        organization_id=77,
    )

    assert payload.rows
    assert payload.rows[0].cogsKopecks == 76_000
    assert payload.rows[0].commissionKopecks == 20_000
    assert payload.rows[0].storageKopecks == 0  # Acceptance is not storage.


def test_pnl_finance_cache_normalizes_negative_revenue_rows(monkeypatch):
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda *_args, **_kwargs: None)
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key == "ads_2026-06-01_2026-06-30":
            return {"sourceComplete": True, "aggregates": {"123456": {"adSpendKopecks": 0}}}
        if source_key == "finance_2026-06-01_2026-06-30":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "aggregates": {
                    "123456": {
                        "salesUnits": -1,
                        "sellerRevenueKopecks": -139_000,
                        "commissionKopecks": -10_000,
                        "logisticsKopecks": -5_000,
                        "storageKopecks": -1_000,
                    }
                },
            }
        return None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 123456, "vendorCode": "FBBT_42"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _organization_id: {})
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_algorithm_settings",
        lambda _organization_id: {"taxPct": 6, "otherExpensePricePct": 5, "otherExpensePerSaleRub": 0},
    )

    payload = build_pnl_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        requested_state="preliminary",
        finance_allowed=True,
        organization_id=77,
    )

    row = payload.rows[0]
    assert row.revenueKopecks == -139_000
    assert row.marginPct is None
    assert row.taxKopecks == 0
    assert row.overheadKopecks == -6_950
    assert row.commissionKopecks == -10_000
    assert row.logisticsKopecks == -5_000
    assert row.storageKopecks == -1_000
    assert row.cogsKopecks == -38_000
    assert row.netProfitKopecks == -78_050


def test_pnl_report_response_is_cached_for_two_hours(monkeypatch):
    """Retain the historical node ID; current cache contract is 24h, no stale window."""
    source_cache: dict[str, dict] = {}
    finance_reads = {"count": 0}
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])

    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key.startswith("pnl_report_"):
            return source_cache.get(source_key)
        if source_key == "finance_2026-06-01_2026-06-30":
            finance_reads["count"] += 1
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "aggregates": {
                    "123456": {
                        "salesUnits": 1,
                        "sellerRevenueKopecks": 100_000,
                        "commissionKopecks": 10_000,
                    }
                },
            }
        return None

    def fake_save_source_cache(_organization_id: int, source_key: str, payload: dict):
        stored = dict(payload)
        stored["fetchedAt"] = datetime.now(timezone.utc).isoformat()
        source_cache[source_key] = stored
        return stored

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", fake_save_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 123456, "vendorCode": "FBBT_42"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _organization_id: {})
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _organization_id: {"taxPct": 0})

    first = build_pnl_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        requested_state="preliminary",
        finance_allowed=True,
        organization_id=77,
    )
    second = build_pnl_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        requested_state="preliminary",
        finance_allowed=True,
        organization_id=77,
    )

    assert finance_reads["count"] == 1
    assert first.rows[0].revenueKopecks == second.rows[0].revenueKopecks == 100_000
    assert any(key.startswith("pnl_report_") for key in source_cache)
    cached_payload = next(iter(source_cache.values()))
    assert cached_payload["ttlSeconds"] == 86400
    assert cached_payload["staleTtlSeconds"] == 86400

    cached_payload["fetchedAt"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    finance_reads["count"] = 0

    still_cached = build_pnl_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        requested_state="preliminary",
        finance_allowed=True,
        organization_id=77,
    )

    assert finance_reads["count"] == 0
    assert still_cached == first

    cached_payload["fetchedAt"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    rebuilt = build_pnl_report(
        date_from=date(2026, 6, 1), date_to=date(2026, 6, 30),
        group_by="sku", requested_state="preliminary", finance_allowed=True,
        organization_id=77,
    )
    assert finance_reads["count"] == 1
    assert rebuilt.rows[0].revenueKopecks == 100_000
    assert next(iter(source_cache.values()))["fetchedAt"] != cached_payload["fetchedAt"]


def test_abc_report_uses_repricer_period_cache_without_own_report_cache(monkeypatch):
    finance_reads = {"count": 0}

    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key.startswith("abc_report_"):
            raise AssertionError("ABC report must not read its own response cache")
        if source_key == "finance_2026-06-01_2026-06-30":
            finance_reads["count"] += 1
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "dateFrom": "2026-06-01",
                "dateTo": "2026-06-30",
                "aggregates": {
                    "111": {
                        "salesUnits": 10,
                        "sellerRevenueKopecks": 1_000_000,
                        "buyerRevenueKopecks": 1_100_000,
                        "commissionFormulaKopecks": 100_000,
                        "logisticsKopecks": 50_000,
                        "storageKopecks": 10_000,
                        "netProfitKopecks": 0,
                    },
                    "222": {
                        "salesUnits": 1,
                        "sellerRevenueKopecks": 100_000,
                        "buyerRevenueKopecks": 110_000,
                        "commissionFormulaKopecks": 10_000,
                        "logisticsKopecks": 5_000,
                        "storageKopecks": 1_000,
                        "netProfitKopecks": 0,
                    },
                },
            }
        if source_key == "ads_2026-06-01_2026-06-30":
            return {
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "dateFrom": "2026-06-01",
                "dateTo": "2026-06-30",
                "aggregates": {
                    "111": {"adSpendKopecks": 20_000},
                    "222": {"adSpendKopecks": 60_000},
                },
            }
        if source_key == "stocks_2026-06-01_2026-06-30":
            return {
                "aggregates": {
                    "111": {"stockUnits": 15, "wbStockUnits": 15},
                    "222": {"stockUnits": 4, "wbStockUnits": 4},
                }
            }
        if source_key == "baskets_2026-06-01_2026-06-30":
            return {
                "aggregates": {
                    "111": {"orderCount": 12, "orderSumKopecks": 1_200_000},
                    "222": {"orderCount": 2, "orderSumKopecks": 200_000},
                }
            }
        if source_key == "period_stats_2026-06-01_2026-06-30":
            return {
                "aggregates": {
                    "111": {"ordersUnits": 12, "ordersKopecks": 1_200_000, "baskets": 30, "buyoutPct": 80},
                    "222": {"ordersUnits": 2, "ordersKopecks": 200_000, "baskets": 3, "buyoutPct": 40},
                }
            }
        return None

    def fake_save_source_cache(_organization_id: int, source_key: str, payload: dict):
        raise AssertionError("ABC report must not write its own response cache")

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", fake_save_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.list_cached_goods",
        lambda _organization_id: [
            {"nmID": 111, "vendorCode": "FBBT_11", "brand": "Satorna", "subjectName": "Футболки"},
            {"nmID": 222, "vendorCode": "HBBT_22", "brand": "Satorna", "subjectName": "Худи"},
        ],
    )
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_runtime_state",
        lambda _organization_id: {
            "skuSettingsOverrides": {
                "FBBT_11": {"cogsKopecks": 30_000},
                "HBBT_22": {"cogsKopecks": 80_000},
            },
            "skuMetaOverrides": {
                "FBBT_11": {"managerId": "maria", "status": "locomotive"},
                "HBBT_22": {"managerId": "irina", "status": "liquidation"},
            },
        },
    )
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_algorithm_settings",
        lambda _organization_id: {"taxPct": 0, "otherExpensePricePct": 0, "otherExpensePerSaleRub": 0},
    )

    first = build_abc_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )
    second = build_abc_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )

    assert finance_reads["count"] == 2
    assert len(first.rows) == 2
    assert first.rows[0]["abcCode"] == "BB"
    assert first.rows[0]["sku"] == "FBBT_11"
    assert first.rows[0]["productStatus"] == "locomotive"
    assert first.rows[0]["ordersComposite"]["units"] == 12
    assert first.rows[0]["salesComposite"]["units"] == 10
    assert first.rows[0]["adSpendKopecks"] == 20_000
    assert first.rows[0]["wbStockUnits"] == 15
    assert first.filteredSummary.skuCount == 2
    assert first.filteredSummary.locomotiveCount == 1
    assert first.filteredSummary.ordersCount == 14
    assert first.filteredSummary.adSpendKopecks == 80_000
    assert second.rows[0]["abcCode"] == first.rows[0]["abcCode"]


def test_abc_report_uses_funnel_orders_but_settled_finance_sales(monkeypatch):
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key.startswith("abc_report_"):
            return None
        if source_key == "finance_2026-06-01_2026-06-30":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-06-30T08:00:00+00:00",
                "aggregates": {
                    "111": {
                        "salesUnits": 10,
                        "sellerRevenueKopecks": 1_000_000,
                        "buyerRevenueKopecks": 1_100_000,
                        "commissionFormulaKopecks": 100_000,
                    }
                },
            }
        if source_key == "period_stats_2026-06-01_2026-06-30":
            return {"aggregates": {"111": {"ordersUnits": 12, "ordersKopecks": 1_200_000, "salesUnits": 10, "revenueKopecks": 1_000_000}}}
        if source_key == "ads_2026-06-01_2026-06-30":
            return {"aggregates": {"111": {"adImpressions": 10, "adClicks": 2, "adSpendKopecks": 50_000}}}
        if source_key == "baskets_2026-06-01_2026-06-30":
            return {
                "aggregates": {
                    "111": {
                        "impressions": 1_000,
                        "openCount": 200,
                        "cartCount": 90,
                        "orderCount": 40,
                        "orderSumKopecks": 4_000_000,
                        "buyoutCount": 25,
                        "buyoutSumKopecks": 2_500_000,
                        "buyoutPct": 62.5,
                    }
                }
            }
        return None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda _organization_id, _source_key, payload: payload)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 111, "vendorCode": "FBBT_11"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _organization_id: {"skuSettingsOverrides": {}, "skuMetaOverrides": {}})
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _organization_id: {"taxPct": 0, "otherExpensePricePct": 0, "otherExpensePerSaleRub": 0})

    report = build_abc_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )

    row = report.rows[0]
    assert row["impressions"] == 1_000
    assert row["clicks"] == 200
    assert row["ctrPct"] == 20
    assert row["baskets"] == 90
    assert row["cartCrPct"] == pytest.approx(44.4444444444)
    assert row["ordersComposite"]["units"] == 40
    assert row["ordersComposite"]["kopecks"] == 4_000_000
    assert row["salesComposite"]["units"] == 10
    assert row["salesComposite"]["kopecks"] == 1_000_000
    assert row["buyoutPct"] == 62.5
    assert report.filteredSummary.ordersCount == 40
    assert report.filteredSummary.ordersKopecks == 4_000_000


def test_abc_report_net_profit_uses_full_finance_formula(monkeypatch, confirmed_tax):
    confirmed_tax["111"] = 60_000
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key == "finance_2026-06-01_2026-06-30":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "aggregates": {
                    "111": {
                        "salesUnits": 2,
                        "sellerRevenueKopecks": 1_000_000,
                        "commissionFormulaKopecks": 100_000,
                        "logisticsKopecks": 50_000,
                        "storageKopecks": 10_000,
                        "acceptanceKopecks": 5_000,
                        "penaltyKopecks": 3_000,
                        "deductionKopecks": 2_000,
                        "additionalPaymentKopecks": 4_000,
                        "acquiringKopecks": 20_000,
                    }
                }
            }
        if source_key == "ads_2026-06-01_2026-06-30":
            return {"aggregates": {"111": {"adSpendKopecks": 30_000}}}
        if source_key == "period_stats_2026-06-01_2026-06-30":
            return {"aggregates": {"111": {"ordersUnits": 2, "ordersKopecks": 1_100_000, "salesUnits": 2, "revenueKopecks": 1_000_000}}}
        if source_key == "baskets_2026-06-01_2026-06-30":
            return {"aggregates": {"111": {"cartCount": 4, "orderCount": 2, "buyoutCount": 2, "buyoutSumKopecks": 1_000_000}}}
        return {"aggregates": {}} if source_key == "stocks_2026-06-01_2026-06-30" else None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 111, "vendorCode": "FBBT_11"}])
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_runtime_state",
        lambda _organization_id: {"skuSettingsOverrides": {"FBBT_11": {"cogsKopecks": 200_000}}, "skuMetaOverrides": {}},
    )
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _organization_id: {"taxPct": 6, "otherExpensePricePct": 1, "otherExpensePerSaleKopecks": 1_000})

    report = build_abc_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )

    row = report.rows[0]
    assert row["netTotalKopecks"] == 314_000
    assert row["acquiringKopecks"] == 20_000
    assert row["acceptanceKopecks"] == 5_000
    assert row["additionalPaymentKopecks"] == 4_000
    assert report.filteredSummary.profitKopecks == 314_000


def test_abc_report_totals_include_baskets_only_snapshot_rows(monkeypatch):
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key == "finance_2026-06-01_2026-06-30":
            return {"aggregates": {"111": {"salesUnits": 1, "sellerRevenueKopecks": 100_000}}}
        if source_key == "baskets_2026-06-01_2026-06-30":
            return {
                "aggregates": {
                    "111": {"cartCount": 10, "orderCount": 2, "buyoutCount": 1},
                    "222": {"cartCount": 1_000, "orderCount": 0, "buyoutCount": 0},
                }
            }
        return {"aggregates": {}} if source_key in {"period_stats_2026-06-01_2026-06-30", "ads_2026-06-01_2026-06-30"} else None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 111, "vendorCode": "SKU_111"}, {"nmID": 222, "vendorCode": "SKU_222"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _organization_id: {"skuSettingsOverrides": {}, "skuMetaOverrides": {}})
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _organization_id: {"taxPct": 0, "otherExpensePricePct": 0, "otherExpensePerSaleRub": 0})

    report = build_abc_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )

    assert sum(row["baskets"] for row in report.rows) == 1_010
    assert {row["nmId"] for row in report.rows} == {111, 222}


def test_abc_report_keeps_funnel_opens_separate_when_impressions_missing(monkeypatch):
    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key.startswith("abc_report_"):
            return None
        if source_key == "finance_2026-06-01_2026-06-30":
            return {
                "aggregates": {
                    "111": {
                        "salesUnits": 10,
                        "sellerRevenueKopecks": 1_000_000,
                    }
                }
            }
        if source_key == "ads_2026-06-01_2026-06-30":
            return {"aggregates": {"111": {"adImpressions": 75_835, "adClicks": 6_154, "adSpendKopecks": 50_000}}}
        if source_key == "baskets_2026-06-01_2026-06-30":
            return {
                "aggregates": {
                    "111": {
                        "openCount": 38_417,
                        "cartCount": 4_267,
                        "orderCount": 1_495,
                        "orderSumKopecks": 2_442_024_00,
                    }
                }
            }
        return None

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda _organization_id, _source_key, payload: payload)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [{"nmID": 111, "vendorCode": "FBBT_11"}])
    monkeypatch.setattr("app.wb_reports_sprint_d.load_runtime_state", lambda _organization_id: {"skuSettingsOverrides": {}, "skuMetaOverrides": {}})
    monkeypatch.setattr("app.wb_reports_sprint_d.load_algorithm_settings", lambda _organization_id: {"taxPct": 0, "otherExpensePricePct": 0, "otherExpensePerSaleRub": 0})

    report = build_abc_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )

    row = report.rows[0]
    assert row["impressions"] is None
    assert row["clicks"] == 38_417
    assert row["ctrPct"] is None
    assert row["baskets"] == 4_267
    assert row["ordersComposite"]["units"] == 1_495


def test_abc_report_uses_covering_repricer_daily_cache(monkeypatch):
    monkeypatch.setattr("app.wb_reports_sprint_d.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    source_cache: dict[str, dict] = {}

    def fake_get_source_cache(_organization_id: int, source_key: str, *, slim: bool = False):
        if source_key.startswith("abc_report_"):
            return None
        if source_key == "wb_sync_status":
            return {
                "dateFrom": "2026-06-01",
                "dateTo": "2026-07-08",
                "periodDays": 38,
                "periodCacheSuffix": "2026-06-01_2026-07-08",
            }
        if source_key == "finance_2026-06-01_2026-07-08":
            return {
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "fetchedAt": "2026-07-08T08:00:00+00:00",
                "dateFrom": "2026-06-01",
                "dateTo": "2026-07-08",
                "periodDays": 38,
                "periodCacheSuffix": "2026-06-01_2026-07-08",
                "dailyAggregates": {
                    "2026-06-01": {
                        "111": {"salesUnits": 99, "sellerRevenueKopecks": 9_900_000},
                    },
                    "2026-06-02": {
                        "111": {
                            "salesUnits": 3,
                            "sellerRevenueKopecks": 300_000,
                            "buyerRevenueKopecks": 330_000,
                            "commissionFormulaKopecks": 30_000,
                            "logisticsKopecks": 15_000,
                            "storageKopecks": 3_000,
                        },
                    },
                    "2026-07-08": {
                        "111": {
                            "salesUnits": 2,
                            "sellerRevenueKopecks": 200_000,
                            "buyerRevenueKopecks": 220_000,
                            "commissionFormulaKopecks": 20_000,
                            "logisticsKopecks": 10_000,
                            "storageKopecks": 2_000,
                        },
                    },
                },
            }
        if source_key == "baskets_2026-06-01_2026-07-08":
            return {
                "dateFrom": "2026-06-01", "dateTo": "2026-07-08",
                "dailyAggregates": {
                    "2026-06-02": {"111": {"orderCount": 4, "orderSumKopecks": 400_000}},
                    "2026-07-08": {"111": {"orderCount": 3, "orderSumKopecks": 300_000}},
                },
            }
        if source_key == "period_stats_2026-06-01_2026-07-08":
            return {
                "dateFrom": "2026-06-01",
                "dateTo": "2026-07-08",
                "dailyAggregates": {
                    "2026-06-02": {"111": {"ordersUnits": 4, "ordersKopecks": 400_000, "baskets": 7}},
                    "2026-07-08": {"111": {"ordersUnits": 3, "ordersKopecks": 300_000, "baskets": 5}},
                },
            }
        return source_cache.get(source_key)

    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda _organization_id, key, payload: source_cache.setdefault(key, payload))
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.list_cached_goods",
        lambda _organization_id: [{"nmID": 111, "vendorCode": "FBBT_11", "brand": "Satorna", "subjectName": "Футболки"}],
    )
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_runtime_state",
        lambda _organization_id: {
            "skuSettingsOverrides": {"FBBT_11": {"cogsKopecks": 10_000}},
            "skuMetaOverrides": {"FBBT_11": {"managerId": "maria", "status": "locomotive"}},
        },
    )
    monkeypatch.setattr(
        "app.wb_reports_sprint_d.load_algorithm_settings",
        lambda _organization_id: {"taxPct": 0, "otherExpensePricePct": 0, "otherExpensePerSaleRub": 0},
    )

    report = build_abc_report(
        date_from=date(2026, 6, 2),
        date_to=date(2026, 7, 8),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )

    assert len(report.rows) == 1
    assert report.rows[0]["sku"] == "FBBT_11"
    assert report.rows[0]["salesComposite"]["units"] == 5
    assert report.rows[0]["salesComposite"]["kopecks"] == 500_000
    assert report.rows[0]["ordersComposite"]["units"] == 7
    assert report.filteredSummary.skuCount == 1


def test_abc_report_without_cache_returns_empty_backend_state_not_static_summary(monkeypatch):
    monkeypatch.setattr("app.wb_reports_sprint_d.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.wb_reports_sprint_d.save_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.wb_reports_sprint_d.list_cached_goods", lambda _organization_id: [])

    report = build_abc_report(
        date_from=date(2026, 6, 2),
        date_to=date(2026, 7, 8),
        group_by="sku",
        filters="",
        finance_allowed=True,
        organization_id=77,
    )

    assert report.rows == []
    assert report.filteredSummary.skuCount == 0
    assert report.filteredSummary.ordersKopecks is None
    assert report.filteredSummary.profitKopecks is None
    assert report.filteredSummary.adSpendKopecks is None
    assert report.sourceStatus == "partial"


def test_bff_abc_returns_sku_rows_not_summary_only(monkeypatch):
    def fake_build_abc_report(**_kwargs):
        report = build_abc_report(
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 30),
            group_by="sku",
            filters="",
            finance_allowed=True,
            organization_id=None,
        )
        payload = report.model_dump(mode="python")
        payload["rows"] = [
            {
                "sku": "FBBT_11",
                "nmId": 111,
                "productStatus": "locomotive",
                "abcCode": "AA",
                "ordersComposite": {"units": 12, "kopecks": 1_200_000, "deltaPct": 0},
                "salesComposite": {"units": 10, "kopecks": 1_000_000, "deltaPct": 0},
                "netTotalKopecks": 790_000,
                "marginPct": 79,
                "adSpendKopecks": 20_000,
                "wbStockUnits": 15,
            }
        ]
        return type(report).model_validate(payload)

    monkeypatch.setattr("app.routers.wb_reports_bff.build_abc_report", fake_build_abc_report)

    api = client()
    response = api.get("/api/wb/reports/abc", headers=auth_headers(api, "viewer"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["id"] == "abc"
    assert payload["rows"][0]["sku"] == "FBBT_11"
    assert payload["rows"][0]["abcCode"] == "AA"
    assert payload["columns"][0]["key"] == "sku"


def test_bff_abc_uses_repricer_rows_when_report_cache_is_empty(monkeypatch):
    def fake_build_abc_report(**_kwargs):
        return build_abc_report(
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 30),
            group_by="sku",
            filters="",
            finance_allowed=True,
            organization_id=77,
        )

    monkeypatch.setattr("app.routers.wb_reports_bff.build_abc_report", fake_build_abc_report)
    monkeypatch.setattr(
        "app.routers.wb_reports_bff._repricer_rows_for_abc_report",
        lambda **_kwargs: [
            {
                "meta": {
                    "articleId": "FBBT_11",
                    "nmId": 111,
                    "name": "Футболка",
                    "brand": "Vella",
                    "subject": "Футболки",
                    "managerId": "maria",
                },
                "analytics": {
                    "abcCode": "BA",
                    "productStatus": "locomotive",
                    "ordersUnits": 7,
                    "revenueKopecks": 700_000,
                    "salesUnits": 5,
                    "sellerRevenueKopecks": 500_000,
                    "netProfitKopecks": 180_000,
                    "marginPct": 36,
                    "adSpendKopecks": 20_000,
                    "wbStockUnits": 12,
                    "baskets": 15,
                    "financeState": "ok",
                },
            }
        ],
        raising=False,
    )

    api = client()
    response = api.get(
        "/api/wb/reports/abc?preset=custom&from=2026-06-02&to=2026-07-08&groupBy=sku",
        headers=auth_headers(api, "viewer"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"][0]["sku"] == "FBBT_11"
    assert payload["rows"][0]["abcCode"] == "BA"
    assert payload["rows"][0]["ordersComposite"]["units"] == 7
    assert payload["rows"][0]["salesComposite"]["kopecks"] == 500_000
    assert payload["filteredSummary"]["skuCount"] == 1
    assert payload["filteredSummary"]["ordersCount"] == 7
