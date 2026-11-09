from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repricer_cache.store import FINANCE_SCHEMA_VERSION
from app.repricer_bff import FINANCE_REVENUE_BASIS
from app.routers.wb_reports_bff import (
    DIGEST_REPORT_PAYLOAD_VERSION,
    RNP_REPORT_PAYLOAD_VERSION,
    STOCK_REPORT_PAYLOAD_VERSION,
    WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
    _apply_digest_plan,
    _build_digest_payload,
    _build_digest_problem_rows,
    _build_stock_report_payload,
    _build_week_over_week_payload,
    _latest_report_payload_cache,
    _normalize_digest_plan,
    _previous_period,
    _report_daily_sources_ready,
    _rnp_daily_baskets_ready,
    _report_job_cache_key,
    _report_payload_cache_is_usable,
    _stock_products_request_body,
)
from app.wb_api import reports_sources_runtime as reports_runtime
from app.wb_api.client import FakeWbApiClient, WbApiRequest
from app.wb_api.reports_sources_runtime import WbStockRow
from tests.auth_helpers import auth_headers


@pytest.fixture(autouse=True)
def isolated_catalog_facts(monkeypatch):
    # These report unit tests supply their own cached catalog; canonical DB
    # integration is covered separately in test_wb_cost_import.
    monkeypatch.setattr('app.platform.economics.legacy_catalog.catalog_facts', lambda _organization_id, **kw: {})
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    # Direct handler tests supply an actor rather than real HTTP headers. The
    # quarterly-tax wrapper must resolve that same actor, not an unrelated DB.
    from app.routers import wb_reports_bff as reports
    from app import repricer_tasks
    monkeypatch.setattr(reports, '_report_start_guard', lambda *a: nullcontext())
    monkeypatch.setattr(repricer_tasks.build_report_for_org, 'apply_async',
        lambda args, **kw: repricer_tasks.build_report_for_org.delay(*args))
    monkeypatch.setattr('app.control_plane.auth.actor_from_request', lambda request: reports.actor_from_request(request))


def test_rnp_buyout_composite_keeps_zero_instead_of_copying_orders():
    from app.routers.wb_reports_bff import _map_rnp_to_report_response
    from app.wb_api.rnp_runtime import _rnp_row_from_sources

    row = _rnp_row_from_sources(
        {"nmId": 101, "sku": "SKU-101", "orderCount": 7, "orderSumKopecks": 100_000,
         "buyoutCount": 0, "buyoutSumKopecks": 0},
        None, "partial", "medium",
    )
    payload = SimpleNamespace(
        rows=[row], sourceStatus="partial", groupBy="sku", adSpendKopecks=0,
        formulaNotes=[], sourceEvidence=[], adsSourceStatus="partial", diagnostics={},
    )

    mapped = _map_rnp_to_report_response(payload, {"preset": "custom", "from": "2026-09-17", "to": "2026-09-17"})

    assert mapped["rows"][0]["ordersComposite"] == {"units": 7, "kopecks": 100_000, "deltaPct": None}
    assert mapped["rows"][0]["salesComposite"] == {"units": 0, "kopecks": 0, "deltaPct": None}


def test_cached_reports_sources_snapshot_builds_from_repricer_sync_caches(monkeypatch):
    from app.routers import wb_reports_bff

    def fake_get_source_cache(_organization_id, key, **_kwargs):
        if key == "stocks":
            return {
                "aggregates": {
                    "101": {
                        "wbStockUnits": 5,
                        "availableUnits": 7,
                        "inWayToClient": 1,
                        "inWayFromClient": 2,
                        "warehouseName": "WB Коледино",
                        "warehouseId": 77,
                    }
                }
            }
        return None

    def fake_period_cache(_organization_id, prefix, _date_from, _date_to):
        if prefix == "period_stats":
            return {
                "aggregates": {
                    "101": {
                        "ordersUnits": 2,
                        "ordersKopecks": 20000,
                        "salesUnits": 1,
                        "revenueKopecks": 12000,
                    }
                }
            }
        if prefix == "finance":
            return {
                "aggregates": {
                    "101": {
                        "sellerPayoutKopecks": 9000,
                        "realizationRevenueKopecks": 10000,
                        "commissionKopecks": 1000,
                        "logisticsKopecks": 500,
                        "penaltyKopecks": 100,
                        "acceptanceKopecks": 200,
                        "storageKopecks": 300,
                    }
                }
            }
        return {}

    monkeypatch.setattr(wb_reports_bff, "get_source_cache", fake_get_source_cache)
    monkeypatch.setattr(wb_reports_bff, "_period_cache", fake_period_cache)

    snapshot = wb_reports_bff.build_cached_wb_reports_sources_snapshot(
        organization_id=1,
        date_from=date(2026, 7, 10),
        date_to=date(2026, 7, 16),
    )

    assert snapshot.source_status == "cached"
    assert snapshot.confidence == "high"
    assert snapshot.stocks[0].nm_id == 101
    assert snapshot.stocks[0].available_units == 7
    assert wb_reports_bff._orders_sales_by_nm(snapshot)[101]["orders_qty"] == 2
    assert wb_reports_bff._orders_sales_by_nm(snapshot)[101]["sales_qty"] == 1
    assert snapshot.revenue_by_nm_kopecks == {101: 10000}  # Finance, not Statistics' 12000.
    assert snapshot.seller_payout_by_nm_kopecks == {101: 9000}
    assert snapshot.logistics_cost_by_nm_kopecks == {101: 500}


def test_cached_ads_snapshot_builds_from_sync_cache(monkeypatch):
    from app.routers import wb_reports_bff

    def fake_period_cache(_organization_id, prefix, _date_from, _date_to):
        if prefix == "ads":
            return {
                "aggregates": {
                    "101": {
                        "nmId": 101,
                        "campaignId": 55,
                        "campaignName": "Search",
                        "adSpendKopecks": 12300,
                        "impressions": 1000,
                        "clicks": 50,
                        "cartCount": 7,
                        "ordersCount": 3,
                        "ordersKopecks": 45000,
                    }
                }
            }
        return {}

    monkeypatch.setattr(wb_reports_bff, "_period_cache", fake_period_cache)

    snapshot = wb_reports_bff.build_cached_ads_attribution_snapshot(
        organization_id=1,
        date_from=date(2026, 7, 10),
        date_to=date(2026, 7, 16),
        group_by="sku",
    )

    assert snapshot.source_status == "cached"
    assert snapshot.rows[0].sku_id == "101"
    assert snapshot.rows[0].campaign_id == "55"
    assert snapshot.totals["ad_spend_kopecks"] == 12300
    assert snapshot.totals["orders_kopecks"] == 45000


def test_alerts_and_export_use_cached_sources_without_live_wb(monkeypatch):
    from app.routers import wb_reports_bff

    actor = SimpleNamespace(organization_id=1, user_id="viewer")
    cached_snapshot = SimpleNamespace(
        source_status="cached",
        orders=[{"nmId": 101, "_cachedUnits": 2, "finishedPrice": 100}],
        sales=[],
        stocks=[
            WbStockRow(
                nm_id=101,
                chrt_id=None,
                warehouse_id=7,
                warehouse_name="WB",
                region_name=None,
                quantity=0,
                in_way_to_client=0,
                in_way_from_client=0,
                available_units=0,
                was_out_of_stock=True,
            )
        ],
    )
    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: actor)
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wb_reports_bff, "build_cached_wb_reports_sources_snapshot", lambda **_kwargs: cached_snapshot)
    assert not hasattr(wb_reports_bff, "build_wb_reports_sources_snapshot")

    alerts = wb_reports_bff.get_reports_alerts(
        SimpleNamespace(query_params={}),
        preset="custom",
        from_="2026-07-10",
        to="2026-07-16",
    )
    assert alerts["items"][0]["id"] == "oos-101-7"

    assert (
        wb_reports_bff._estimated_export_rows(
            "stock",
            date(2026, 7, 10),
            date(2026, 7, 16),
            finance_allowed=False,
            wb_token="token",
            organization_id=1,
        )
        == 1
    )
    assert (
        wb_reports_bff._estimated_export_rows(
            "week-over-week",
            date(2026, 7, 10),
            date(2026, 7, 16),
            finance_allowed=False,
            wb_token="token",
            organization_id=1,
        )
        == 1
    )


def client() -> TestClient:
    return TestClient(create_app())


def test_bff_digest_endpoint_returns_frontend_shape():
    api = client()
    response = api.get("/api/wb/reports/digest", headers=auth_headers(api, "viewer"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["id"] == "digest"
    assert isinstance(payload["kpis"], list)
    assert isinstance(payload["planFactRows"], list)
    assert isinstance(payload["freshness"], list)
    assert isinstance(payload["alerts"], list)
    assert isinstance(payload["charts"], list)
    assert isinstance(payload["quickLinks"], list)


def test_digest_read_uses_cached_result_without_calling_wb(monkeypatch):
    cached = {
        'dateFrom': '2026-07-10', 'dateTo': '2026-07-16', 'sourceComplete': True, 'sourceRevision': 'test-revision',
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "digest": {"cacheVersion": DIGEST_REPORT_PAYLOAD_VERSION, "meta": {"id": "digest", "freshnessState": "fresh"}, "kpis": [{"id": "orders_qty", "value": "7"}], "planFactRows": [], "freshness": [], "alerts": [], "charts": [], "quickLinks": []},
    }
    cache_key = f"reports_digest_{DIGEST_REPORT_PAYLOAD_VERSION}_2026-07-10_2026-07-16"

    def read_cache(organization_id, key, **_kwargs):
        assert organization_id == 1
        return cached if key == cache_key else {}

    monkeypatch.setattr("app.routers.wb_reports_bff.get_source_cache", read_cache)
    monkeypatch.setattr("app.routers.wb_reports_bff._digest_source_revision", lambda *a: 'test-revision')
    monkeypatch.setattr("app.routers.wb_reports_bff.save_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.routers.wb_reports_bff.record_audit_event", lambda **_kwargs: None)
    monkeypatch.setattr(reports_runtime, "build_wb_reports_sources_snapshot", lambda **_kwargs: pytest.fail("digest GET must not call WB"))
    monkeypatch.setattr("app.routers.wb_reports_bff.build_cached_wb_reports_sources_snapshot", lambda **_kwargs: pytest.fail("digest GET must not rebuild the cached report"))
    api = client()
    response = api.get("/api/wb/reports/digest?preset=custom&from=2026-07-10&to=2026-07-16", headers=auth_headers(api, "viewer"))
    assert response.status_code == 200
    assert response.json()["cache"]["status"] == "exact"
    assert response.json()["cache"]["fresh"] is True
    assert response.json()["kpis"] == cached["digest"]["kpis"]


def test_digest_payload_uses_wb_funnel_totals_and_daily_points():
    source_snapshot = SimpleNamespace(
        source_status="fresh",
        orders=[{"nmId": 101, "finishedPrice": 1000}],
        sales=[{"nmId": 101, "finishedPrice": 700, "forPay": 500}],
        stocks=[],
        seller_payout_by_nm_kopecks={101: 500_00},
        commission_cost_by_nm_kopecks={},
        logistics_cost_by_nm_kopecks={},
        penalties_cost_by_nm_kopecks={},
        acceptance_cost_by_nm_kopecks={},
        storage_cost_by_nm_kopecks={},
    )
    ads_snapshot = SimpleNamespace(totals={}, rows=[], source_status="fresh", blocker_ids=[])
    plan_fact = SimpleNamespace(blockerIds=[], rows=[])
    funnel_snapshot = {
        "status": "fresh",
        "rows": [{"nmId": 101}],
        "totals": {
            "orderCount": 60_846,
            "openCount": 95_823,
            "cartCount": 5_511,
            "orderSumKopecks": 11_009_938_600,
            "buyoutCount": 24_359,
            "buyoutSumKopecks": 4_288_069_900,
            "cancelCount": 0,
        },
        "dailyRows": {
            "2026-07-19": [
                {
                    "openCount": 95_823,
                    "cartCount": 5_511,
                    "orderCount": 1_242,
                    "orderSumKopecks": 2_000_000,
                    "buyoutCount": 3,
                    "buyoutSumKopecks": 6_000,
                    "cancelCount": 4,
                }
            ]
        },
    }

    payload = _build_digest_payload(
        {"preset": "custom", "from": "2026-07-19", "to": "2026-07-19"},
        source_snapshot,
        ads_snapshot,
        plan_fact,
        funnel_snapshot,
    )

    kpis = {item["id"]: item["value"] for item in payload["kpis"]}
    kpi_labels = {item["id"]: item["label"] for item in payload["kpis"]}
    kpi_hints = {item["id"]: item["hint"] for item in payload["kpis"] if "hint" in item}
    assert kpis["orders_qty"] == "60846"
    assert kpis["sales_revenue"] == "4288069900"
    assert kpi_labels["sales_revenue"] == "Выкупили на сумму"
    assert "по дате заказа" in kpi_hints["sales_revenue"].lower()
    assert "seller payout - комиссии" in kpi_hints["margin_profit"]
    assert payload["cacheVersion"] == DIGEST_REPORT_PAYLOAD_VERSION
    assert payload["funnelSummary"]["buyoutCount"] == 24_359
    assert payload["periodCards"][0]["openCount"] == funnel_snapshot["totals"]["openCount"]
    assert payload["periodCards"][0]["cartCount"] == funnel_snapshot["totals"]["cartCount"]
    point = payload["charts"][0]["points"][0]
    assert point["value"] == 1_242
    assert point["compareValue"] == 3
    assert point["returnsUnits"] is None  # cancelCount is not returns.
    assert point["buyoutPct"] == 42.9


def test_digest_funnel_snapshot_uses_baskets_cache_without_live_wb(monkeypatch):
    from app.routers import wb_reports_bff

    def fake_period_cache(_organization_id, prefix, _date_from, _date_to):
        if prefix == "baskets":
            return {
                "aggregates": {
                    "101": {
                        "nmId": 101,
                        "openCount": 20,
                        "cartCount": 10,
                        "orderCount": 4,
                        "orderSumKopecks": 80000,
                        "buyoutCount": 3,
                        "buyoutSumKopecks": 60000,
                        "cancelCount": 1,
                    }
                },
                "dailyRows": {
                    "2026-07-16": [
                        {
                            "nmId": 101,
                            "orderCount": 2,
                            "orderSumKopecks": 40000,
                            "buyoutCount": 1,
                            "buyoutSumKopecks": 20000,
                        }
                    ]
                },
            }
        return {}

    monkeypatch.setattr(wb_reports_bff, "_period_cache", fake_period_cache)
    assert not hasattr(wb_reports_bff, "_load_or_refresh_funnel_rows")

    snapshot = wb_reports_bff._build_digest_funnel_snapshot(
        organization_id=1,
        date_from=date(2026, 7, 10),
        date_to=date(2026, 7, 16),
        wb_token="token",
    )

    assert snapshot["status"] == "fresh"
    assert snapshot["cacheStatus"] == "hit"
    assert snapshot["rows"][0]["nmId"] == 101
    assert snapshot["totals"]["orderCount"] == 4
    assert snapshot["dailyRows"]["2026-07-16"][0]["buyoutSumKopecks"] == 20000


def test_digest_refresh_reuses_existing_running_job(monkeypatch):
    monkeypatch.setattr("app.routers.wb_reports_bff.get_source_cache", lambda *_args, **_kwargs: {"state": "running", "taskId": "digest-task-1", "updatedAt": datetime.now(timezone.utc).isoformat()})
    api = client()
    response = api.post("/api/wb/reports/digest/refresh", headers=auth_headers(api, "viewer"))
    assert response.status_code == 200
    assert response.json()["taskId"] == "digest-task-1"
    assert response.json()["reused"] is True


def test_digest_refresh_does_not_pass_user_wb_token_to_report_task(monkeypatch):
    monkeypatch.setattr("app.routers.wb_reports_bff.save_source_cache", lambda _org, _key, payload: payload)
    from app import repricer_tasks

    captured_args = {}

    def fake_get_source_cache(*_args, **_kwargs):
        return {}

    def fake_delay(*args, **kwargs):
        captured_args["args"] = args
        captured_args["kwargs"] = kwargs
        return type("Task", (), {"id": "digest-task-cache-only"})()

    monkeypatch.setattr("app.routers.wb_reports_bff.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr(repricer_tasks.build_digest_for_org, "delay", fake_delay)
    monkeypatch.setattr(repricer_tasks.refresh_report_sources_for_org, "delay", fake_delay)
    monkeypatch.setattr("app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix", lambda *a, **kw: [])
    api = client()
    response = api.post("/api/wb/reports/digest/refresh", headers=auth_headers(api, "viewer"))

    assert response.status_code == 200
    assert captured_args["args"][-1] is None


def test_report_job_cache_key_scopes_report_range_and_grouping():
    assert _report_job_cache_key("stock", date(2026, 6, 1), date(2026, 6, 7), "warehouse") == "reports_job_stock_2026-06-01_2026-06-07_warehouse"


def test_week_over_week_uses_equal_previous_period():
    assert _previous_period(date(2026, 7, 7), date(2026, 7, 13)) == (date(2026, 6, 30), date(2026, 7, 6))


def test_week_over_week_returns_real_deltas_for_current_and_previous_snapshots():
    current = SimpleNamespace(
        source_status="fresh",
        orders=[
            {"nmId": 101, "finishedPrice": 1000},
            {"nmId": 101, "finishedPrice": 1000},
            {"nmId": 101, "finishedPrice": 1000},
        ],
        sales=[
            {"nmId": 101, "finishedPrice": 1000, "forPay": 700},
            {"nmId": 101, "finishedPrice": 1000, "forPay": 700},
        ],
        stocks=[],
    )
    previous = SimpleNamespace(
        source_status="fresh",
        orders=[{"nmId": 101, "finishedPrice": 1000}],
        sales=[{"nmId": 101, "finishedPrice": 1000, "forPay": 700}],
        stocks=[],
    )

    payload = _build_week_over_week_payload(
        {"preset": "custom", "from": "2026-07-07", "to": "2026-07-13"},
        current,
        previous,
    )

    row = payload["rows"][0]
    assert row["orders"]["units"] is None
    assert row["orders"]["deltaPct"] is None
    assert row["sales"]["units"] == 2
    assert row["sales"]["deltaPct"] == 100.0


def test_week_over_week_rows_preserve_sales_funnel_cart_order_buyout_metrics():
    from app.routers.wb_reports_bff import _report_payload_cache_is_usable, _week_over_week_shell, _week_rows_from_abc_rows, _week_rows_with_funnel_metrics

    current_rows = [
        {
            "sku": "Фчbt_0269",
            "nmId": 1096582536,
            "productName": "Футболка с принтом y2k Bape x Maison Margiela",
            "cartCount": 1164,
            "baskets": 1164,
            "ordersComposite": {"units": 221, "kopecks": 63_065_200},
            "salesComposite": {"units": 315, "kopecks": 21_942_800},
            "priceKopecks": 200_200,
        }
    ]
    previous_rows = [
        {
            "sku": "Фчbt_0269",
            "nmId": 1096582536,
            "cartCount": 1058,
            "baskets": 1058,
            "ordersComposite": {"units": 190, "kopecks": 48_141_374},
            "salesComposite": {"units": 229, "kopecks": 16_627_600},
            "priceKopecks": 190_600,
        }
    ]

    rows = _week_rows_from_abc_rows(current_rows, previous_rows)
    payload = _week_over_week_shell({"preset": "custom", "from": "2026-07-10", "to": "2026-07-16"}, rows, source_status="fresh")

    row = payload["rows"][0]
    assert row["baskets"]["units"] == 1164
    assert row["orders"]["units"] == 221
    assert row["sales"]["units"] == 315
    assert row["sales"]["kopecks"] == 21_942_800
    assert row["baskets"]["deltaPct"] == 10.02
    assert payload["cacheVersion"] == WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION
    assert _report_payload_cache_is_usable(
        "week-over-week",
        {"completedAt": datetime.now(timezone.utc).isoformat(), "report": payload},
    )
    assert not _report_payload_cache_is_usable(
        "week-over-week",
        {"completedAt": datetime.now(timezone.utc).isoformat(), "report": {**payload, "cacheVersion": "v1"}},
    )
    assert not _report_payload_cache_is_usable(
        "week-over-week",
        {"completedAt": datetime.now(timezone.utc).isoformat(), "report": {**payload, "rows": []}},
    )


def test_week_over_week_overlays_sales_funnel_metrics_over_fallback_rows():
    from app.routers.wb_reports_bff import _week_rows_from_abc_rows, _week_rows_with_funnel_metrics

    current_rows = [
        {
            "sku": "Фч_sc_091",
            "nmId": 376785768,
            "baskets": 0,
            "ordersComposite": {"units": 174, "kopecks": 358_560_00},
            "salesComposite": {"units": 0, "kopecks": 88_628_00},
            "priceKopecks": 155_200,
        }
    ]
    previous_rows = [
        {
            "sku": "Фч_sc_091",
            "nmId": 376785768,
            "baskets": 636,
            "ordersComposite": {"units": 182, "kopecks": 52_000_000},
            "salesComposite": {"units": 220, "kopecks": 25_000_000},
            "priceKopecks": 128_300,
        }
    ]

    current_rows = _week_rows_with_funnel_metrics(
        current_rows,
        {
            "376785768": {
                "cartCount": 833,
                "orderCount": 197,
                "orderSumKopecks": 55_405_400,
                "buyoutCount": 290,
                "buyoutSumKopecks": 30_605_400,
                "buyoutPct": 147.21,
            }
        },
    )
    previous_rows = _week_rows_with_funnel_metrics(
        previous_rows,
        {
            "376785768": {
                "cartCount": 636,
                "orderCount": 182,
                "orderSumKopecks": 51_780_748,
                "buyoutCount": 220,
                "buyoutSumKopecks": 23_400_000,
                "buyoutPct": 120.88,
            }
        },
    )

    row = _week_rows_from_abc_rows(current_rows, previous_rows)[0]
    assert row["baskets"]["units"] == 833
    assert row["orders"]["units"] == 197
    assert row["orders"]["kopecks"] == 55_405_400
    assert row["sales"]["units"] == 290
    assert row["sales"]["kopecks"] == 30_605_400
    assert row["historySource"] == "missing"


def test_report_job_requeues_stale_queued_jobs():
    from app.routers.wb_reports_bff import _report_job_is_reusable

    stale_queued = {
        "state": "queued",
        "reportId": "week-over-week",
        "queuedAt": (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(),
    }
    fresh_queued = {
        "state": "queued",
        "reportId": "week-over-week",
        "queuedAt": datetime.now(timezone.utc).isoformat(),
    }

    assert not _report_job_is_reusable(stale_queued)
    assert _report_job_is_reusable(fresh_queued)


def test_week_over_week_row_exposes_stock_history_coverage_and_null_unavailable_values():
    current = SimpleNamespace(source_status="partial", orders=[{"nmId": 101, "finishedPrice": 1000}], sales=[], stocks=[])
    payload = _build_week_over_week_payload({"preset": "7d", "from": "2026-07-07", "to": "2026-07-13"}, current)
    row = payload["rows"][0]
    assert row["stockAvailability7d"] == []
    assert row["stockSnapshotCoveragePct"] == 0
    assert row["historySource"] == "missing"
    assert row["baskets"]["units"] is None
    assert row["marginPct"]["percent"] is None
    assert row["profit"]["kopecks"] is None


def test_week_over_week_enriches_rows_from_cached_catalog(monkeypatch):
    from app.routers import wb_reports_bff

    current = SimpleNamespace(
        source_status="fresh",
        orders=[{"nmId": 101, "finishedPrice": 1000}],
        sales=[],
        stocks=[],
    )
    monkeypatch.setattr(
        wb_reports_bff,
        "list_cached_goods",
        lambda _organization_id: [{"nmID": 101, "vendorCode": "REAL_SKU_101", "brand": "Real Brand", "subjectName": "Real Category"}],
    )
    monkeypatch.setattr(
        wb_reports_bff,
        "get_source_cache",
        lambda _organization_id, key, **_kwargs: {
            "cards": [{"nmID": 101, "vendorCode": "REAL_SKU_101", "title": "Real Product"}]
        }
        if key == "content_cards"
        else {},
    )

    payload = _build_week_over_week_payload(
        {"preset": "custom", "from": "2026-07-07", "to": "2026-07-13"},
        current,
        organization_id=7,
    )

    row = payload["rows"][0]
    assert row["sku"] == "REAL_SKU_101"
    assert row["productName"] == "Real Product"
    assert row["brand"] == "Real Brand"
    assert row["category"] == "Real Category"


def test_week_over_week_job_endpoint_starts_and_reuses_task(monkeypatch):
    monkeypatch.setattr('app.routers.wb_reports_bff._report_daily_sources_ready', lambda *a, **kw: (True, []))
    from app import repricer_tasks
    from app.routers import wb_reports_bff

    saved: dict[str, dict] = {}
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.get_source_cache",
        lambda _organization_id, key, slim=False: saved.get(key, {}),
    )
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.save_source_cache",
        lambda _organization_id, key, payload: saved.__setitem__(key, payload),
    )
    monkeypatch.setattr(
        repricer_tasks.build_report_for_org,
        "delay",
        lambda *_args, **_kwargs: type("Task", (), {"id": "wow-task-1"})(),
    )
    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: SimpleNamespace(organization_id=1, user_id="viewer"))
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wb_reports_bff, "_actor_wb_token", lambda _actor: None)
    request = SimpleNamespace()
    params = {"preset": "custom", "from_": "2026-07-07", "to": "2026-07-13", "groupBy": "sku", "source": "operational"}

    first = wb_reports_bff.start_report_job(request, "week-over-week", **params)
    second = wb_reports_bff.start_report_job(request, "week-over-week", **params)

    assert first["taskId"]
    assert second["reused"] is True


def test_abc_refresh_action_builds_from_saved_sources_without_recollecting(monkeypatch):
    from app import repricer_tasks
    from app.routers import wb_reports_bff

    saved: dict[str, dict] = {}
    delay_calls: list[tuple] = []

    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: SimpleNamespace(organization_id=1, user_id="viewer"))
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(wb_reports_bff, "get_source_cache", lambda _organization_id, key, slim=False: saved.get(key, {}))
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(wb_reports_bff, "_report_daily_sources_ready", lambda *_args, **_kwargs: (True, []))

    def delay(*args, **_kwargs):
        delay_calls.append(args)
        return type("Task", (), {"id": "refresh-task-1"})()

    monkeypatch.setattr(repricer_tasks.refresh_report_sources_for_org, "delay", lambda *a, **kw: pytest.fail("ABC must reuse saved sources"))
    monkeypatch.setattr(repricer_tasks.build_report_for_org, "delay", delay)

    result = wb_reports_bff.start_report_source_refresh_job(
        SimpleNamespace(),
        "abc",
        preset="custom",
        from_="2026-07-10",
        to="2026-07-16",
        groupBy="sku",
        source="operational",
    )

    assert result["state"] == "queued"
    assert result["taskId"]
    assert delay_calls[0][2] == "abc"


def test_regular_report_job_does_not_overwrite_active_source_refresh(monkeypatch):
    from app import repricer_tasks
    from app.routers import wb_reports_bff

    active_job = {
        "state": "running",
        "stage": "refreshing_sources",
        "taskId": "refresh-task-1",
        "reportId": "abc",
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }

    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: SimpleNamespace(organization_id=1, user_id="viewer"))
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(wb_reports_bff, "_report_daily_sources_ready", lambda *_args, **_kwargs: (True, []))
    monkeypatch.setattr(
        wb_reports_bff,
        "get_source_cache",
        lambda _organization_id, key, slim=False: (
            active_job
            if key.startswith("reports_job_abc")
            else {"report": {"meta": {"id": "abc"}, "rows": [{"sku": "STALE"}]}}
            if key.startswith("reports_payload_abc")
            else {}
        ),
    )
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda *_args, **_kwargs: pytest.fail("active refresh job must not be overwritten"))
    monkeypatch.setattr(
        repricer_tasks.build_report_for_org,
        "delay",
        lambda *_args, **_kwargs: pytest.fail("active refresh job must not enqueue regular build"),
    )

    result = wb_reports_bff.start_report_job(
        SimpleNamespace(),
        "abc",
        preset="custom",
        from_="2026-07-10",
        to="2026-07-16",
        groupBy="sku",
        source="operational",
    )

    assert result["reused"] is True
    assert result["stage"] == "refreshing_sources"


def test_report_job_status_does_not_overwrite_active_source_refresh(monkeypatch):
    from app.routers import wb_reports_bff

    active_job = {
        "state": "running",
        "stage": "refreshing_sources",
        "taskId": "refresh-task-1",
        "reportId": "abc",
        "label": "Обновляем источники WB для отчета",
        "percent": 18,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }

    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: SimpleNamespace(organization_id=1, user_id="viewer"))
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        wb_reports_bff,
        "get_source_cache",
        lambda _organization_id, key, slim=False: (
            active_job
            if key.startswith("reports_job_abc")
            else {"report": {"meta": {"id": "abc"}, "rows": [{"sku": "STALE"}]}}
            if key.startswith("reports_payload_abc")
            else {}
        ),
    )
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda *_args, **_kwargs: pytest.fail("active refresh status must not be overwritten"))

    result = wb_reports_bff.get_report_job(
        SimpleNamespace(),
        "abc",
        preset="custom",
        from_="2026-07-10",
        to="2026-07-16",
        groupBy="sku",
        source="operational",
    )

    assert result["state"] == "running"
    assert result["stage"] == "refreshing_sources"
    assert result["percent"] == 18


def test_week_over_week_job_endpoint_reuses_completed_cached_report(monkeypatch):
    monkeypatch.setattr('app.routers.wb_reports_bff._report_daily_sources_ready', lambda *a, **kw: (True, []))
    from app import repricer_tasks
    from app.routers import wb_reports_bff

    monkeypatch.setattr(wb_reports_bff, "legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    delay_calls: list[tuple] = []
    saved: dict[str, dict] = {}
    cached = {
        "taxRevision": "synthetic-confirmation",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "report": {
            "meta": {"id": "week-over-week"},
            "cacheVersion": wb_reports_bff.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
            "rows": [{"sku": "CACHED", "orders": {"units": 1}}],
        },
    }

    def fake_cache(_organization_id, key, slim=False):
        if key.startswith("reports_payload_week-over-week"):
            return cached
        if key.startswith("reports_job_week-over-week"):
            return {"state": "completed", "taskId": "done-task", "reportId": "week-over-week"}
        return {}

    monkeypatch.setattr("app.routers.wb_reports_bff.get_source_cache", fake_cache)
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda _org, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(
        repricer_tasks.build_report_for_org,
        "delay",
        lambda *args, **_kwargs: delay_calls.append(args) or type("Task", (), {"id": "unexpected"})(),
    )
    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: SimpleNamespace(organization_id=1, user_id="viewer"))
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wb_reports_bff, "_actor_wb_token", lambda _actor: None)

    result = wb_reports_bff.start_report_job(
        SimpleNamespace(),
        "week-over-week",
        preset="custom",
        from_="2026-07-07",
        to="2026-07-13",
        groupBy="sku",
        source="operational",
    )

    assert result["state"] == "completed"
    assert result["reused"] is True
    assert result["taskId"] == "done-task"
    assert delay_calls == []
    assert saved == {"reports_job_week-over-week_2026-07-07_2026-07-13_sku_nofinance": result}


def test_week_over_week_latest_cache_builds_from_source_cache_for_inner_period(monkeypatch):
    from app.routers import wb_reports_bff

    saved: dict[str, dict] = {}
    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: SimpleNamespace(organization_id=1, user_id="viewer"))
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wb_reports_bff, "list_source_cache_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wb_reports_bff, "get_source_cache", lambda _organization_id, key, slim=False: saved.get(key, {}))
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(
        wb_reports_bff,
        "_build_week_over_week_fallback_report",
        lambda **kwargs: {
            "meta": {"id": "week-over-week"},
            "cacheVersion": wb_reports_bff.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
            "filters": {"dateRange": kwargs["date_range"]},
            "rows": [{"sku": "INNER", "orders": {"units": 2}}],
            "reportJob": {"state": "completed", "stage": "cache_only", "percent": 100},
        },
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        wb_reports_bff.get_reports_latest_cache(
            SimpleNamespace(), "week-over-week", groupBy="sku", source="operational",
            preset="custom", from_="2026-07-24", to="2026-07-25")
    assert error.value.status_code == 404
    assert saved == {}  # A cold read delegates assembly to the normal worker.


def test_week_over_week_job_endpoint_restarts_stale_running_job(monkeypatch):
    monkeypatch.setattr('app.routers.wb_reports_bff._report_daily_sources_ready', lambda *a, **kw: (True, []))
    from app import repricer_tasks
    from app.routers import wb_reports_bff

    stale_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    saved: dict[str, dict] = {}
    delay_calls: list[tuple] = []
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.get_source_cache",
        lambda _organization_id, key, slim=False: saved.get(key, {"state": "running", "taskId": "stale-task", "updatedAt": stale_updated_at}),
    )
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.save_source_cache",
        lambda _organization_id, key, payload: saved.__setitem__(key, payload),
    )

    def delay(*args, **_kwargs):
        delay_calls.append(args)
        return type("Task", (), {"id": "fresh-task"})()

    monkeypatch.setattr(repricer_tasks.build_report_for_org, "delay", delay)
    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: SimpleNamespace(organization_id=1, user_id="viewer"))
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wb_reports_bff, "_actor_wb_token", lambda _actor: None)

    result = wb_reports_bff.start_report_job(
        SimpleNamespace(),
        "week-over-week",
        preset="custom",
        from_="2026-07-07",
        to="2026-07-13",
        groupBy="sku",
        source="operational",
    )

    assert result["taskId"]
    assert result["reused"] is False
    assert len(delay_calls) == 1


def test_week_over_week_get_returns_cached_payload_while_job_is_running(monkeypatch):
    from app.routers import wb_reports_bff

    monkeypatch.setattr(wb_reports_bff, "legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    actor = SimpleNamespace(organization_id=1, user_id="viewer")
    stale_cached_report = {
        "meta": {"id": "week-over-week"},
        "cacheVersion": wb_reports_bff.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
        "rows": [{"sku": "STALE", "orders": {"units": 1}}],
    }
    fresh_updated_at = datetime.now(timezone.utc).isoformat()
    active_job = {"state": "running", "reportId": "week-over-week", "updatedAt": fresh_updated_at}
    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: actor)
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        wb_reports_bff,
        "get_source_cache",
        lambda _organization_id, key, slim=False: (
            {"report": stale_cached_report, "completedAt": fresh_updated_at, "taxRevision": "synthetic-confirmation"}
            if key.startswith("reports_payload_week-over-week")
            else active_job
        ),
    )
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda *_args, **_kwargs: pytest.fail("cached GET must preserve active job"))
    monkeypatch.setattr(
        wb_reports_bff,
        "build_cached_wb_reports_sources_snapshot",
        lambda **_kwargs: pytest.fail("WoW GET must not build source snapshots directly"),
    )
    monkeypatch.setattr(wb_reports_bff, "build_abc_report", lambda **_kwargs: pytest.fail("WoW GET must not use ABC fallback"))
    monkeypatch.setattr(wb_reports_bff, "_repricer_rows_for_abc_report", lambda **_kwargs: pytest.fail("WoW GET must not use repricer fallback"))

    payload = wb_reports_bff.get_reports_by_id(
        SimpleNamespace(),
        "week-over-week",
        preset="custom",
        from_="2026-06-01",
        to="2026-07-14",
        groupBy="sku",
        source="operational",
    )

    assert payload["cache"]["status"] == "stale"
    assert payload["reportJob"] == active_job
    assert len(payload["rows"]) == 1
    original_row = stale_cached_report["rows"][0]
    assert {key: payload["rows"][0][key] for key in original_row} == original_row


def test_week_over_week_get_returns_completed_cached_job_payload(monkeypatch):
    from app.routers import wb_reports_bff

    monkeypatch.setattr(wb_reports_bff, "legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    actor = SimpleNamespace(organization_id=1, user_id="viewer")
    cached_report = {
        "meta": {"id": "week-over-week"},
        "cacheVersion": wb_reports_bff.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
        "rows": [{"sku": "JOB-1", "orders": {"units": 7}}],
    }
    completed_job = {
        "state": "completed",
        "reportId": "week-over-week",
        "dateFrom": "2026-07-01",
        "dateTo": "2026-07-14",
        "groupBy": "sku",
        "taskId": "done-task",
    }
    saved: dict[str, dict] = {}
    completed_at = datetime.now(timezone.utc).isoformat()

    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: actor)
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        wb_reports_bff,
        "get_source_cache",
        lambda _organization_id, key, **_kwargs: (
            {"report": cached_report, "completedAt": completed_at, "taxRevision": "synthetic-confirmation"}
            if key.startswith("reports_payload_week-over-week")
            else completed_job
            if key.startswith("reports_job_week-over-week")
            else {}
        ),
    )
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda _org, key, payload: saved.__setitem__(key, payload))
    monkeypatch.setattr(wb_reports_bff, "build_abc_report", lambda **_kwargs: pytest.fail("completed WoW job payload should be returned from cache"))
    monkeypatch.setattr(wb_reports_bff, "_repricer_rows_for_abc_report", lambda **_kwargs: pytest.fail("completed WoW job payload should be returned from cache"))

    payload = wb_reports_bff.get_reports_by_id(
        SimpleNamespace(query_params={}),
        "week-over-week",
        preset="custom",
        from_="2026-07-01",
        to="2026-07-14",
        groupBy="sku",
        source="operational",
    )

    assert len(payload["rows"]) == 1
    original_row = cached_report["rows"][0]
    assert {key: payload["rows"][0][key] for key in original_row} == original_row
    assert payload["cache"]["status"] == "exact"
    assert {key: payload["reportJob"][key] for key in completed_job} == completed_job
    assert payload["reportJob"]["stage"] == "cache"
    assert payload["reportJob"]["reused"] is True
    assert saved == {"reports_job_week-over-week_2026-07-01_2026-07-14_sku_nofinance": payload["reportJob"]}


def test_week_over_week_get_returns_missing_background_report_without_cache(monkeypatch):
    from app.routers import wb_reports_bff

    actor = SimpleNamespace(organization_id=1, user_id="viewer")
    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: actor)
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(wb_reports_bff, "get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        wb_reports_bff,
        "build_cached_wb_reports_sources_snapshot",
        lambda **_kwargs: pytest.fail("WoW GET must not build source snapshots directly"),
    )
    monkeypatch.setattr(wb_reports_bff, "build_abc_report", lambda **_kwargs: pytest.fail("WoW GET must not use ABC fallback"))
    monkeypatch.setattr(wb_reports_bff, "_repricer_rows_for_abc_report", lambda **_kwargs: pytest.fail("WoW GET must not use repricer fallback"))

    payload = wb_reports_bff.get_reports_by_id(
        SimpleNamespace(query_params={}),
        "week-over-week",
        preset="custom",
        from_="2026-07-01",
        to="2026-07-14",
        groupBy="sku",
        source="operational",
    )

    assert payload["cache"]["status"] == "missing"
    assert payload["reportJob"]["state"] == "idle"
    assert payload["rows"] == []


def test_week_over_week_get_marks_stale_running_job_without_cache(monkeypatch):
    from app.routers import wb_reports_bff

    actor = SimpleNamespace(organization_id=1, user_id="viewer")
    stale_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()

    monkeypatch.setattr(wb_reports_bff, "actor_from_request", lambda _request: actor)
    monkeypatch.setattr(wb_reports_bff, "assert_permission_or_audit", lambda **_kwargs: None)
    monkeypatch.setattr(wb_reports_bff, "has_permission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        wb_reports_bff,
        "get_source_cache",
        lambda _organization_id, key, **_kwargs: (
            {"state": "running", "taskId": "stuck-task", "updatedAt": stale_updated_at, "label": "Загружаем продажи WB: соблюдаем лимит WB", "percent": 22}
            if key.startswith("reports_job_week-over-week")
            else {}
        ),
    )

    payload = wb_reports_bff.get_reports_by_id(
        SimpleNamespace(query_params={}),
        "week-over-week",
        preset="custom",
        from_="2026-06-01",
        to="2026-06-20",
        groupBy="sku",
        source="operational",
    )

    assert payload["cache"]["status"] == "missing"
    assert payload["reportJob"]["state"] == "stale"
    assert payload["reportJob"]["previousState"] == "running"
    assert payload["reportJob"]["percent"] == 22


def test_week_over_week_enriches_rows_from_period_stats():
    from app.routers import wb_reports_bff

    rows = [
        {
            "sku": "REP-STATS",
            "nmId": 303,
            "ordersComposite": {"units": 0, "kopecks": 0},
            "salesComposite": {"units": 0, "kopecks": 0},
            "netTotalKopecks": 0,
            "marginPct": 25,
        }
    ]

    enriched = wb_reports_bff._week_rows_with_period_stats(
        rows,
        {"303": {"ordersUnits": 8, "salesUnits": 5, "revenueKopecks": 1_000_000}},
    )

    row = enriched[0]
    assert row["ordersComposite"]["units"] == 8
    assert row["salesComposite"]["units"] == 5
    assert row["salesComposite"]["kopecks"] == 1_000_000
    assert row["netTotalKopecks"] == 0
    assert "profitSource" not in row
    assert row["historySource"] == "repricer_period_stats"


def test_week_over_week_live_builder_uses_own_sources_not_abc_or_repricer(monkeypatch):
    """Historical node ID; current fallback composes scoped caches, never live APIs."""
    from app.routers import wb_reports_bff

    actor = SimpleNamespace(organization_id=1, user_id="viewer")
    snapshot_calls: list[tuple[date, date]] = []
    ads_calls: list[tuple[date, date]] = []
    monkeypatch.setattr(wb_reports_bff, "list_cached_goods", lambda _organization_id: [])
    monkeypatch.setattr(wb_reports_bff, "get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(wb_reports_bff, "ensure_daily_stock_history", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(wb_reports_bff, "build_abc_report", lambda **_kwargs: pytest.fail("WoW metrics must not use ABC report"))
    monkeypatch.setattr(wb_reports_bff, "_repricer_rows_for_abc_report", lambda **_kwargs: pytest.fail("WoW metrics must not use repricer rows"))

    def fake_sources_snapshot(*, organization_id, date_from, date_to):
        assert organization_id == 1
        snapshot_calls.append((date_from, date_to))
        if date_from == date(2026, 6, 13):
            return SimpleNamespace(
                source_status="fresh",
                orders=[{"nmId": 101, "finishedPrice": 1000}, {"nmId": 101, "finishedPrice": 1000}],
                sales=[{"nmId": 101, "finishedPrice": 1000, "forPay": 700}],
                stocks=[],
            )
        return SimpleNamespace(
            source_status="fresh",
            orders=[{"nmId": 101, "finishedPrice": 1000}],
            sales=[],
            stocks=[],
        )

    assert not hasattr(wb_reports_bff, "build_wb_reports_sources_snapshot")
    monkeypatch.setattr("app.wb_api.reports_sources_runtime.build_wb_reports_sources_snapshot", lambda **_kwargs: pytest.fail("WoW must not fetch live sources"))
    monkeypatch.setattr("app.wb_api.ads_runtime.build_ads_attribution_snapshot", lambda **_kwargs: pytest.fail("WoW must not fetch live ads"))
    monkeypatch.setattr(wb_reports_bff, "build_cached_wb_reports_sources_snapshot", fake_sources_snapshot)
    def fake_ads_snapshot(*, organization_id, date_from, date_to, group_by):
        assert organization_id == 1
        assert group_by == "sku"
        ads_calls.append((date_from, date_to))
        return SimpleNamespace(rows=[])

    monkeypatch.setattr(wb_reports_bff, "build_cached_ads_attribution_snapshot", fake_ads_snapshot)

    payload = wb_reports_bff._build_week_over_week_fallback_report(
        request=SimpleNamespace(query_params={}),
        actor=actor,
        date_from=date(2026, 6, 13),
        date_to=date(2026, 7, 14),
        date_range={"from": "2026-06-13", "to": "2026-07-14"},
        finance_allowed=False,
        wb_token="wb-token",
    )

    assert snapshot_calls == [(date(2026, 6, 13), date(2026, 7, 14)), (date(2026, 5, 12), date(2026, 6, 12))]
    assert ads_calls == snapshot_calls
    assert payload["cache"]["status"] == "cache-only"
    assert [step["stage"] for step in payload["diagnostics"]["requests"]] == ["current_sources_cache", "previous_sources_cache", "current_ads_cache", "previous_ads_cache"]
    assert payload["diagnostics"]["requests"][0]["rows"]["orders"] == 2
    assert payload["rows"][0]["orders"]["units"] is None
    assert payload["rows"][0]["orders"]["deltaPct"] is None
    # Buyouts require the financial ledger and permission, not an unrelated
    # statistics-sales fallback. This fixture has neither.
    assert payload["rows"][0]["sales"]["kopecks"] is None


def test_week_over_week_task_builds_current_and_previous_source_ranges(monkeypatch):
    from app import repricer_tasks
    from app.routers import wb_reports_bff

    calls: list[tuple[date, date]] = []
    saved: dict[str, dict] = {}
    funnel_calls = []
    period_calls = []

    def cached_abc(*, organization_id, date_from, date_to, group_by, filters, finance_allowed):
        assert (organization_id, group_by, filters, finance_allowed) == (1, "sku", "", False)
        calls.append((date_from, date_to))
        return SimpleNamespace(sourceStatus="fresh", confidence="high", rows=[{"nmId": 101, "sku": "TEST"}])

    def period_stats(organization_id, date_from, date_to):
        assert organization_id == 1
        period_calls.append((date_from, date_to))
        return {}

    def cached_funnel(*, organization_id, date_from, date_to, wb_token, progress_callback):
        assert organization_id == 1
        assert wb_token is None
        funnel_calls.append((date_from, date_to))
        return {}

    def save_cache(organization_id, key, payload):
        assert organization_id == 1
        saved[key] = payload

    def read_cache(organization_id, key, **_kwargs):
        assert organization_id == 1
        return saved.get(key)

    monkeypatch.setattr(wb_reports_bff, "build_abc_report", cached_abc)
    monkeypatch.setattr(wb_reports_bff, "_week_period_stats_aggregates", period_stats)
    monkeypatch.setattr(wb_reports_bff, "_week_funnel_metrics_by_nm", cached_funnel)
    monkeypatch.setattr(wb_reports_bff, "_repricer_rows_for_abc_report", lambda **_kwargs: pytest.fail("populated ABC must not use repricer fallback"))
    monkeypatch.setattr(wb_reports_bff, "_apply_report_rules_to_payload", lambda report, _organization_id: report)
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", save_cache)
    monkeypatch.setattr(wb_reports_bff, "get_source_cache", read_cache)
    monkeypatch.setattr("app.wb_api.reports_sources_runtime.build_wb_reports_sources_snapshot", lambda **_kwargs: pytest.fail("no live source fetch"))
    monkeypatch.setattr("app.wb_api.ads_runtime.build_ads_attribution_snapshot", lambda **_kwargs: pytest.fail("no live ads fetch"))

    result = repricer_tasks.build_report_for_org.run(
        1,
        "viewer",
        "week-over-week",
        "2026-07-07",
        "2026-07-13",
        "sku",
        "operational",
        False,
        None,
    )

    assert calls == [(date(2026, 7, 7), date(2026, 7, 13)), (date(2026, 6, 30), date(2026, 7, 6))]
    assert funnel_calls == period_calls == calls
    assert result["state"] == "completed"
    assert any(key.startswith("reports_payload_week-over-week") for key in saved)
    stored = next(value for key, value in saved.items() if key.startswith("reports_payload_week-over-week"))
    assert stored["report"]["cache"]["status"] == "background-abc"
    assert stored["report"]["cache"]["previousRange"] == {"from": "2026-06-30", "to": "2026-07-06"}


def test_digest_payload_builds_weekly_balance_periods_and_real_problem_rows():
    source_snapshot = SimpleNamespace(
        source_status="fresh",
        orders=[
            {"nmId": 101, "finishedPrice": 1000, "lastChangeDate": "2026-05-02T10:00:00+03:00"},
            {"nmId": 101, "finishedPrice": 1200, "lastChangeDate": "2026-05-02T11:00:00+03:00"},
            {"nmId": 202, "finishedPrice": 900, "lastChangeDate": "2026-05-03T12:00:00+03:00"},
        ],
        sales=[
            {"nmId": 101, "finishedPrice": 1000, "forPay": 720, "lastChangeDate": "2026-05-02T15:00:00+03:00"},
            {"nmId": 101, "finishedPrice": 1000, "forPay": 0, "isReturn": True, "lastChangeDate": "2026-05-02T16:00:00+03:00"},
            {"nmId": 202, "finishedPrice": 900, "forPay": 640, "lastChangeDate": "2026-05-03T15:00:00+03:00"},
        ],
        stocks=[
            WbStockRow(
                nm_id=101,
                chrt_id=None,
                warehouse_id=1,
                warehouse_name="Коледино",
                region_name=None,
                quantity=0,
                in_way_to_client=0,
                in_way_from_client=0,
                available_units=0,
                was_out_of_stock=True,
            )
        ],
        seller_payout_by_nm_kopecks={101: 72_000, 202: 64_000},
        commission_cost_by_nm_kopecks={101: 10_000},
        logistics_cost_by_nm_kopecks={101: 5_000},
        penalties_cost_by_nm_kopecks={},
        acceptance_cost_by_nm_kopecks={},
        storage_cost_by_nm_kopecks={},
    )
    ads_snapshot = SimpleNamespace(
        source_status="partial",
        totals={"ad_spend_kopecks": 15_000},
        cabinet_balance={"balanceKopecks": 50_000},
        rows=[
            SimpleNamespace(
                campaignId="adv-1",
                campaignName="Weak campaign",
                attributionLevel="campaign_only",
                adSpendKopecks=15_000,
            )
        ],
    )
    plan_fact_payload = SimpleNamespace(sourceStatus="partial", blockerIds=["WB-14A"], rows=[])

    payload = _build_digest_payload(
        {"preset": "7d", "from": "2026-05-02", "to": "2026-05-08"},
        source_snapshot,
        ads_snapshot,
        plan_fact_payload,
    )

    assert payload["planFactRows"] == []
    assert payload["weeklyBalance"]["points"][0] == {
        "label": "02.05",
        "date": "2026-05-02",
        "ordersUnits": 2,
        "ordersKopecks": 220_000,
        "salesUnits": 1,
        "salesKopecks": 100_000,
        "returnsUnits": 1,
        "buyoutPct": 50.0,
        "coverageComplete": True,
    }
    assert payload["periodCards"][0]["id"] == "selected"
    assert payload["periodCards"][0]["ordersUnits"] == 3
    assert payload["periodCards"][0]["returnsUnits"] == 1
    assert any(row["reason"] == "oos_risk" for row in payload["problemRows"])
    oos = next(row for row in payload["problemRows"] if row["reason"] == "oos_risk")
    assert oos["affectedItems"][0]["sku"] == "NM_101"
    assert oos["affectedItems"][0]["warehouseName"] == "Коледино"
    assert oos["affectedItems"][0]["availableUnits"] == 0
    assert "нулевой доступный остаток" in oos["details"]
    assert any(row["reason"] == "weak_ads_attribution" for row in payload["problemRows"])
    assert any(kpi["id"] == "margin_profit" for kpi in payload["kpis"])


def test_digest_problem_rows_count_unique_nm_ids_not_warehouse_rows():
    rows = _build_digest_problem_rows(
        [
            {"sku": "NM_101", "nmId": 101, "availableUnits": 0},
            {"sku": "NM_101", "nmId": 101, "availableUnits": -1},
            {"sku": "NM_202", "nmId": 202, "warehouseName": "Казань", "availableUnits": 0, "wbStockUnits": 0, "fromClientUnits": 0},
        ],
        SimpleNamespace(rows=[]),
    )

    oos = next(row for row in rows if row["reason"] == "oos_risk")
    assert oos["skuCount"] == 2
    assert oos["title"] == "2 SKU с риском OOS"
    assert oos["details"] == "Товары, у которых нулевой доступный остаток: NM_101, NM_202"
    assert oos["affectedItems"] == [
        {
            "sku": "NM_101",
            "nmId": 101,
            "warehouseName": "unknown",
            "availableUnits": 0,
            "wbStockUnits": None,
            "fromClientUnits": None,
            "toClientUnits": None,
            "reason": "Доступный остаток 0 шт",
        },
        {
            "sku": "NM_202",
            "nmId": 202,
            "warehouseName": "Казань",
            "availableUnits": 0,
            "wbStockUnits": 0,
            "fromClientUnits": 0,
            "toClientUnits": None,
            "reason": "Доступный остаток 0 шт",
        },
    ]


def test_apply_digest_plan_keeps_company_and_manager_revenue_and_margin_plans():
    payload = {
        "kpis": [
            {"id": "margin_profit", "value": "90000"},
            {"id": "sales_revenue", "value": "300000"},
        ]
    }
    _apply_digest_plan(
        payload,
        {
            "company": {"marginPlanKopecks": 120000, "revenuePlanKopecks": 400000},
            "managers": [
                {"id": "maria", "name": "Мария Дудина", "marginPlanKopecks": 50000, "revenuePlanKopecks": 150000},
                {"id": "anna", "name": "Анна Петрова", "marginPlanKopecks": 70000, "revenuePlanKopecks": 250000},
            ],
        },
    )

    assert [row["name"] for row in payload["planFactRows"]] == ["Компания", "Мария Дудина", "Анна Петрова"]
    assert payload["planFactRows"][0]["revenuePlanKopecks"] == 400000
    assert payload["planFactRows"][0]["revenueFactKopecks"] == 300000
    assert payload["planFactRows"][1]["revenuePlanKopecks"] == 150000
    assert payload["planFactRows"][1]["revenueFactKopecks"] == 150000


def test_apply_digest_plan_uses_revenue_when_margin_plan_is_not_set():
    payload = {
        "dateRange": {"to": "2026-07-10"},
        "kpis": [
            {"id": "margin_profit", "value": "90000"},
            {"id": "sales_revenue", "value": "300000"},
        ],
    }
    _apply_digest_plan(payload, {"month": "2026-07", "company": {"revenuePlanKopecks": 930000}, "managers": []})

    company = payload["planFactRows"][0]
    assert company["metricLabel"] == "Выручка"
    assert company["planKopecks"] == 930000
    assert company["factKopecks"] == 300000
    assert company["completionPct"] == 32.3
    assert company["forecastKopecks"] == 930000


def test_digest_plan_keeps_only_active_project_managers():
    normalized = _normalize_digest_plan(
        {
            "month": "2026-07",
            "company": {},
            "managers": [
                {"id": "real-manager", "name": "Реальный менеджер"},
                {"id": "mock-manager", "name": "Моковый менеджер"},
            ],
        },
        allowed_managers={"real-manager": "Реальный менеджер"},
    )

    assert normalized["managers"] == [
        {"id": "real-manager", "name": "Реальный менеджер", "revenuePlanKopecks": 0, "marginPlanKopecks": 0}
    ]


def test_statistics_report_requests_wait_for_wb_shared_limit(monkeypatch):
    monkeypatch.setattr(reports_runtime, "_statistics_report_last_request_at", 0.0)
    monkeypatch.setattr(reports_runtime, "get_settings", lambda: SimpleNamespace(wb_api_mode="real"))
    client = FakeWbApiClient(fixtures={"/api/v1/supplier/orders": []})
    request = WbApiRequest(method="GET", path="/api/v1/supplier/orders")
    waits: list[float] = []

    reports_runtime._request_statistics_report(client, request, clock=lambda: 100.0, sleeper=waits.append)
    reports_runtime._request_statistics_report(client, request, clock=lambda: 100.0, sleeper=waits.append)

    assert waits == [reports_runtime.STATISTICS_REPORT_MIN_INTERVAL_SECONDS]


def test_digest_endpoint_reports_missing_cache_without_calling_wb(monkeypatch):
    monkeypatch.setattr("app.routers.wb_reports_bff.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(reports_runtime, "build_wb_reports_sources_snapshot", lambda **_kwargs: pytest.fail("must not call WB"))
    monkeypatch.setattr("app.routers.wb_reports_bff.build_cached_wb_reports_sources_snapshot", lambda **_kwargs: pytest.fail("missing digest GET must not start a build"))
    api = client()
    response = api.get("/api/wb/reports/digest?preset=custom&from=2026-06-01&to=2026-07-09", headers=auth_headers(api, "viewer"))
    assert response.status_code == 200
    assert response.json()["cache"]["status"] == "missing"
    assert response.json()["kpis"] == []
    assert response.json()["digestJob"]["state"] == "idle"


def test_bff_digest_degrades_when_ads_token_is_missing(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("VELLA_WB_ADS_API_TOKEN", raising=False)
    assert get_settings().wb_ads_api_token is None
    monkeypatch.setattr("app.routers.wb_reports_bff.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "app.wb_api.ads_runtime.build_ads_attribution_snapshot",
        lambda **_kwargs: pytest.fail("digest GET must not require a live ads token"),
    )
    monkeypatch.setattr("app.routers.wb_reports_bff._build_digest_ads_snapshot", lambda **_kwargs: pytest.fail("digest GET must not rebuild ads"))

    api = client()
    response = api.get("/api/wb/reports/digest", headers=auth_headers(api, "viewer"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["id"] == "digest"
    assert payload["cache"]["status"] == "missing"
    assert payload["kpis"] == []


def test_bff_stock_and_week_over_week_endpoints_return_rows(monkeypatch):
    from app.routers import wb_reports_bff

    monkeypatch.setattr(wb_reports_bff, "legacy_finance_tax_revision", lambda org: "synthetic-confirmation")
    date_from = date(2026, 7, 14)
    date_to = date(2026, 7, 20)
    params = {"preset": "custom", "from": str(date_from), "to": str(date_to)}
    wow_cache_key = "reports_payload_week-over-week_2026-07-14_2026-07-20_sku_operational_nofinance"
    wow_cache = {
        "taxRevision": "synthetic-confirmation",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "dateFrom": str(date_from),
        "dateTo": str(date_to),
        "report": {
            "meta": {"id": "week-over-week"},
            "cacheVersion": WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
            "filters": {"dateRange": params, "groupBy": "sku"},
            "rows": [
                {
                    "sku": "WOW-CACHE-505",
                    "nmId": 505,
                    "orders": {"units": 5, "kopecks": 750_000, "deltaPct": 25},
                    "sales": {"units": 4, "kopecks": 600_000, "deltaPct": 33.3},
                    "stockAvailability7d": [True, False, True, True, False, True, True],
                    "historySource": "captured",
                }
            ],
        },
    }
    cached_snapshot = SimpleNamespace(
        source_status="cached",
        orders=[{"nmId": 404, "_cachedUnits": 14, "finishedPrice": 1500}],
        sales=[{"nmId": 404, "_cachedUnits": 7, "finishedPrice": 1600}],
        stocks=[
            WbStockRow(
                nm_id=404,
                chrt_id=4,
                warehouse_id=77,
                warehouse_name="WB Test Warehouse",
                region_name="Test Region",
                quantity=7,
                in_way_to_client=1,
                in_way_from_client=2,
                available_units=9,
                was_out_of_stock=False,
            )
        ],
        financial_rows=[],
        logistics_cost_by_nm_kopecks={404: 21_000},
    )
    cache_reads: list[str] = []
    snapshot_calls: list[dict] = []

    def fake_source_cache(organization_id, key, **_kwargs):
        assert organization_id == 1
        cache_reads.append(key)
        return wow_cache if key == wow_cache_key else {}

    def save_source_cache(organization_id, key, payload):
        assert organization_id == 1
        return payload

    def daily_sources_ready(organization_id, sources, *, date_from: date, date_to: date):
        assert (organization_id, sources, date_from, date_to) == (
            1,
            ("stocks", "period-stats", "finance"),
            date(2026, 7, 14),
            date(2026, 7, 20),
        )
        return True, []

    def cached_sources_snapshot(**kwargs):
        snapshot_calls.append(kwargs)
        return cached_snapshot

    monkeypatch.setattr(wb_reports_bff, "get_source_cache", fake_source_cache)
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", save_source_cache)
    monkeypatch.setattr(wb_reports_bff, "_report_daily_sources_ready", daily_sources_ready)
    monkeypatch.setattr(wb_reports_bff, "build_cached_wb_reports_sources_snapshot", cached_sources_snapshot)
    monkeypatch.setattr(wb_reports_bff, "get_settings", lambda: SimpleNamespace(wb_api_mode="real"))
    monkeypatch.setattr(wb_reports_bff, "list_cached_goods", lambda organization_id: [{"nmID": 404, "vendorCode": "STOCK-SOURCE-404"}] if organization_id == 1 else pytest.fail("wrong stock organization"))
    monkeypatch.setattr(wb_reports_bff, "_request_source_with_cache", lambda **_kwargs: pytest.fail("stock GET must not call a live WB provider"))
    monkeypatch.setattr(wb_reports_bff, "build_abc_report", lambda **_kwargs: pytest.fail("stock/WoW GET must not use the obsolete ABC builder"))
    api = client()
    headers = auth_headers(api, "viewer")

    stock = api.get("/api/wb/reports/stock", params=params, headers=headers)
    assert stock.status_code == 200
    stock_payload = stock.json()
    assert stock_payload["meta"]["id"] == "stock"
    assert isinstance(stock_payload["rows"], list)
    assert stock_payload["rows"]
    assert stock_payload["rows"][0]["sku"] == "STOCK-SOURCE-404"
    assert stock_payload["rows"][0]["availableUnits"] == 9
    assert stock_payload["rows"][0]["historyCoverageDays"] == 0
    assert stock_payload["rows"][0]["historySource"] == "missing"
    assert stock_payload["cacheVersion"] == STOCK_REPORT_PAYLOAD_VERSION
    assert snapshot_calls == [{"organization_id": 1, "date_from": date_from, "date_to": date_to}]

    wow = api.get("/api/wb/reports/week-over-week", params=params, headers=headers)
    assert wow.status_code == 200
    wow_payload = wow.json()
    assert wow_payload["meta"]["id"] == "week-over-week"
    assert isinstance(wow_payload["rows"], list)
    assert wow_payload["rows"]
    assert wow_payload["rows"][0]["sku"] == "WOW-CACHE-505"
    assert len(wow_payload["rows"][0]["stockAvailability7d"]) == 7
    assert wow_payload["rows"][0]["historySource"] == "captured"
    assert wow_payload["cacheVersion"] == WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION
    assert wow_cache_key in cache_reads


def test_stock_get_does_not_build_cached_snapshot_without_daily_sources(monkeypatch):
    from app.routers import wb_reports_bff

    saved: dict[str, dict] = {}

    def readiness(organization_id, sources, *, date_from, date_to):
        assert (organization_id, sources, date_from, date_to) == (
            1,
            ("stocks", "period-stats", "finance"),
            date(2026, 7, 14),
            date(2026, 7, 20),
        )
        return False, ["finance"]

    monkeypatch.setattr(wb_reports_bff, "_report_daily_sources_ready", readiness)
    monkeypatch.setattr(wb_reports_bff, "get_source_cache", lambda organization_id, _key, **_kwargs: {} if organization_id == 1 else pytest.fail("wrong stock organization"))
    monkeypatch.setattr(wb_reports_bff, "save_source_cache", lambda organization_id, key, payload: saved.setdefault(key, payload) if organization_id == 1 else pytest.fail("wrong stock organization"))
    monkeypatch.setattr(wb_reports_bff, "build_cached_wb_reports_sources_snapshot", lambda **_kwargs: pytest.fail("missing readiness must not build cached stock sources"))

    api = client()
    response = api.get(
        "/api/wb/reports/stock",
        params={"preset": "custom", "from": "2026-07-14", "to": "2026-07-20"},
        headers=auth_headers(api, "viewer"),
    )

    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["reportJob"]["state"] == "waiting_daily_detail"
    assert response.json()["reportJob"]["missingSources"] == ["finance"]
    assert list(saved) == ["reports_job_stock_2026-07-14_2026-07-20_sku"]


def test_stock_report_payload_cache_requires_current_version():
    fresh_cache = {
        "report": {"meta": {"id": "stock"}, "rows": [{"sku": "OLD", "availableUnits": 1}]},
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }

    assert _report_payload_cache_is_usable("stock", fresh_cache) is False

    fresh_cache["report"]["cacheVersion"] = STOCK_REPORT_PAYLOAD_VERSION

    assert _report_payload_cache_is_usable("stock", fresh_cache) is True


def test_stock_report_payload_groups_warehouses_by_product_with_catalog_meta(monkeypatch):
    history_calls = []

    def history_stub(snapshot_date, rows, *, organization_id):
        history_calls.append((snapshot_date, rows, organization_id))
        return {}

    # Grouping is independent of legacy history capture/backfill side effects.
    monkeypatch.setattr("app.routers.wb_reports_bff.ensure_daily_stock_history", history_stub)
    snapshot = SimpleNamespace(
        source_status="fresh",
        stocks=[
            WbStockRow(
                nm_id=101,
                chrt_id=1,
                warehouse_id=507,
                warehouse_name="Коледино",
                region_name="Центральный",
                quantity=12,
                in_way_to_client=3,
                in_way_from_client=1,
                available_units=13,
                was_out_of_stock=False,
            ),
            WbStockRow(
                nm_id=101,
                chrt_id=1,
                warehouse_id=1201,
                warehouse_name="Подольск",
                region_name="Центральный",
                quantity=8,
                in_way_to_client=2,
                in_way_from_client=4,
                available_units=12,
                was_out_of_stock=False,
            ),
        ],
        orders=[],
        sales=[],
        financial_rows=[],
        logistics_cost_by_nm_kopecks={},
    )

    monkeypatch.setattr(
        "app.routers.wb_reports_bff._build_stock_source_bundle",
        lambda **_kwargs: {
            "stock_product_metrics": {
                "status": "fresh",
                "data": {
                    "data": {
                        "items": [
                            {
                                "nmID": 101,
                                "metrics": {"ordersCount": 70, "avgOrders": 10, "stockCount": 100},
                            }
                        ]
                    }
                },
            },
            "buyer_region_sales": {"status": "fresh", "data": {"data": []}},
            "local_orders_exact": {"status": "missing", "data": None},
            "ktr_table": {"status": "missing", "data": None},
            "stock_decision_rules": {"status": "missing", "data": None},
        },
    )
    monkeypatch.setattr(
        "app.routers.wb_reports_bff._catalog_meta_by_nm",
        lambda _organization_id: {
            101: {
                "sku": "Фч_sc_091",
                "productName": "Футболка с принтом y2k Psycho archive 2077",
                "brand": "Anomie Studio",
                "category": "Футболки",
            }
        },
    )

    payload = _build_stock_report_payload(
        {"preset": "custom", "from": "2026-07-14", "to": "2026-07-20"},
        snapshot,
        organization_id=7,
        wb_token="token",
    )

    assert payload["filters"]["groupBy"] == "sku"
    assert history_calls == [(date(2026, 7, 20), snapshot.stocks, 7)]
    assert payload["kpis"][0]["label"] == "Товаров"
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["sku"] == "Фч_sc_091"
    assert row["productName"] == "Футболка с принтом y2k Psycho archive 2077"
    assert row["brand"] == "Anomie Studio"
    assert row["category"] == "Футболки"
    assert row["wbStockUnits"] == 20
    assert row["marketplaceStockUnits"] == 80
    assert row["totalStockUnits"] == 100
    assert row["fromClientUnits"] == 5
    assert row["toClientUnits"] == 5
    assert row["availableUnits"] == 25
    assert row["ordersCount"] == 70
    assert row["ordersPerDay"] == 10
    assert row["daysToOos"] == 2.5
    assert row["comment"] == "WB не передал долю локальных заказов; логистика/шт не подтверждена по продажам"
    assert {"ktrIndex", "decision", "decisionStatus"}.isdisjoint(row)
    assert row["warehouseCount"] == 2
    assert [item["warehouseName"] for item in row["warehouses"]] == ["Коледино", "Подольск"]
    assert any(kpi["id"] == "marketplace_stock_units" and kpi["value"] == "80" for kpi in payload["kpis"])
    assert any(kpi["id"] == "total_stock_units" and kpi["value"] == "100" for kpi in payload["kpis"])
    assert "wbStockUnits" in {column["key"] for column in payload["columns"]}
    assert "totalStockUnits" in {column["key"] for column in payload["columns"]}
    assert next(card for card in payload["managementCards"] if card["title"] == "Local orders")["value"] == "derived"


def test_stock_products_request_body_uses_wb_contract_for_all_stock():
    assert _stock_products_request_body({"from": "2026-07-14", "to": "2026-07-20"}, stock_type="") == {
        "nmIDs": [],
        "currentPeriod": {"start": "2026-07-14", "end": "2026-07-20"},
        "stockType": "",
        "skipDeletedNm": True,
        "orderBy": {"field": "avgOrders", "mode": "desc"},
        "availabilityFilters": ["deficient", "actual", "balanced", "nonActual", "nonLiquid", "invalidData"],
        "limit": 1000,
        "offset": 0,
    }


def test_bff_rnp_endpoint_returns_full_backend_funnel_shape(monkeypatch):
    from tests.rnp_cache_fixture import install_rnp_cache

    install_rnp_cache(monkeypatch, date(2026, 6, 1), date(2026, 6, 7), bff=True)
    api = client()
    response = api.get(
        "/api/wb/reports/rnp?preset=custom&from=2026-06-01&to=2026-06-07&groupBy=sku",
        headers=auth_headers(api, "viewer"),
    )

    assert response.status_code == 200
    payload = response.json()
    column_keys = {column["key"] for column in payload["columns"]}
    assert {
        "openCount",
        "cartCount",
        "ordersComposite",
        "adSpendKopecks",
        "acooPct",
        "tacooPct",
        "organicOrderCountEstimated",
    } <= column_keys
    assert payload["rows"]
    row = payload["rows"][0]
    assert "openCount" in row
    assert "organicEstimate" in row
    assert "reasons" in row
    assert payload["diagnostics"]["summary"]["funnelRows"] >= 0
    assert payload["diagnostics"]["sources"][0]["endpoint"] == "POST /api/analytics/v3/sales-funnel/products"
    assert any(source["endpoint"] == "repricer ads period cache" and source["sourceId"] == "wb-ads-cache" for source in payload["diagnostics"]["sources"])


def test_latest_report_payload_cache_respects_requested_range(monkeypatch):
    monkeypatch.setattr("app.routers.wb_reports_bff.legacy_finance_tax_revision", lambda org: "synthetic-current")
    caches = [
        {
            "sourceKey": "reports_payload_rnp_2026-06-08_2026-06-14_sku_operational",
            "catalogRevision": "synthetic-current",
            "report": {"cacheVersion": RNP_REPORT_PAYLOAD_VERSION, "rows": [{"sku": "WRONG", "orderCount": 1}]},
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "dateFrom": "2026-06-08",
            "dateTo": "2026-06-14",
        },
        {
            "sourceKey": "reports_payload_rnp_2026-06-01_2026-06-07_sku_operational",
            "catalogRevision": "synthetic-current",
            "report": {"cacheVersion": RNP_REPORT_PAYLOAD_VERSION, "rows": [{"sku": "RIGHT", "orderCount": 1}]},
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-07",
        },
    ]

    calls = []
    by_key = {cache["sourceKey"]: cache for cache in caches}

    def read_cache(organization_id, key, **_kwargs):
        calls.append((organization_id, key))
        return by_key.get(key)

    monkeypatch.setattr("app.routers.wb_reports_bff.get_source_cache", read_cache)
    monkeypatch.setattr("app.routers.wb_reports_bff.list_source_cache_by_prefix", lambda *_args, **_kwargs: pytest.fail("exact range must not scan unrelated caches"))

    latest = _latest_report_payload_cache(
        organization_id=1,
        report_id="rnp",
        group_by="sku",
        source="operational",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 7),
    )

    assert latest is not None
    cache, matched_from, matched_to = latest
    assert cache["report"]["rows"][0]["sku"] == "RIGHT"
    assert matched_from == date(2026, 6, 1)
    assert matched_to == date(2026, 6, 7)
    assert calls == [(1, "reports_payload_rnp_2026-06-01_2026-06-07_sku_operational")]


def test_rnp_ads_only_cache_is_not_usable_without_wb_funnel_rows():
    cache = {
        "sourceKey": "reports_payload_rnp_2026-06-01_2026-07-01_sku_operational",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "report": {
            "diagnostics": {"summary": {"funnelRows": 0, "adsRows": 1}},
            "rows": [
                {
                    "sku": "376785768",
                    "openCount": 0,
                    "cartCount": 0,
                    "orderCount": 0,
                    "adImpressions": 100_328,
                    "adClicks": 7_736,
                    "adOrders": 370,
                    "adSpendKopecks": 4_813_700,
                }
            ],
        },
    }

    assert _report_payload_cache_is_usable("rnp", cache) is False


def test_rnp_daily_baskets_ready_requires_daily_detail(monkeypatch):
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.list_source_cache_by_prefix",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "baskets_2026-06-25_2026-07-24",
                "dateFrom": "2026-06-25",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            }
        ]
        if prefix == "baskets_"
        else [],
    )

    assert _rnp_daily_baskets_ready(
        2,
        date_from=date(2026, 7, 8),
        date_to=date(2026, 7, 21),
    ) is False


def test_rnp_daily_baskets_ready_accepts_daily_detail(monkeypatch):
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.list_source_cache_by_prefix",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "baskets_2026-07-18_2026-07-24",
                "dateFrom": "2026-07-18",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 7,
            }
        ]
        if prefix == "baskets_"
        else [],
    )

    assert _rnp_daily_baskets_ready(
        2,
        date_from=date(2026, 7, 18),
        date_to=date(2026, 7, 24),
    ) is True


def test_report_daily_sources_ready_rejects_aggregate_only_covering_cache(monkeypatch):
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "finance_2026-06-25_2026-07-24",
                "revenueBasis": "retailAmount",
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "dateFrom": "2026-06-25",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            }
        ]
        if prefix == "finance_"
        else [],
    )

    ready, missing = _report_daily_sources_ready(
        2,
        ("finance",),
        date_from=date(2026, 7, 8),
        date_to=date(2026, 7, 21),
    )

    assert ready is False
    assert missing == ["finance"]


def test_report_daily_sources_ready_rejects_legacy_finance_daily_detail_without_raw_contract(monkeypatch):
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: [
            {
                "sourceKey": "finance_2026-06-25_2026-07-24",
                "revenueBasis": FINANCE_REVENUE_BASIS,
                "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
                "dateFrom": "2026-06-25",
                "dateTo": "2026-07-24",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 30,
            }
        ]
        if prefix == "finance_"
        else [],
    )

    assert _report_daily_sources_ready(
        2,
        ("finance",),
        date_from=date(2026, 7, 8),
        date_to=date(2026, 7, 21),
    ) == (False, ["finance"])


def test_daily_source_union_reuses_overlap_and_only_reports_real_holes(monkeypatch):
    from app.routers.wb_reports_bff import _report_missing_source_ranges
    caches = [
        {'dateFrom': '2026-09-01', 'dateTo': '2026-09-03', 'dailyAggregateDates': ['2026-09-01', '2026-09-02', '2026-09-03']},
        {'dateFrom': '2026-09-03', 'dateTo': '2026-09-07', 'dailyAggregateDates': ['2026-09-03', '2026-09-06', '2026-09-07']},
    ]
    monkeypatch.setattr('app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix', lambda *args, **kwargs: caches)
    assert _report_missing_source_ranges(2, 'ads', date_from=date(2026, 9, 1), date_to=date(2026, 9, 7)) == [(date(2026, 9, 4), date(2026, 9, 5))]
    caches[1]['dailyAggregateDates'].extend(['2026-09-04', '2026-09-05'])
    assert _report_daily_sources_ready(2, ('ads',), date_from=date(2026, 9, 1), date_to=date(2026, 9, 7)) == (True, [])


def test_partial_product_batches_do_not_make_a_complete_funnel_day(monkeypatch):
    from app.routers.wb_reports_bff import _report_missing_source_ranges
    monkeypatch.setattr('app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix', lambda *a, **k: [{
        'sourceKey': 'baskets_2026-09-01_2026-09-02', 'dateFrom': '2026-09-01', 'dateTo': '2026-09-02',
        'dailyDetailStatus': 'partial', 'dailyAggregateDates': ['2026-09-01', '2026-09-02'],
    }])
    monkeypatch.setattr('app.routers.wb_reports_bff.get_source_cache', lambda *a, **k: {'dailyBatchCount': 2,
        'dailyDetailStatus': 'partial', 'dailyAggregateDates': ['2026-09-01', '2026-09-02'], 'chunks': [
        {'type': 'daily', 'date': '2026-09-01', 'chunkIndex': 0, 'status': 'done'},
        {'type': 'daily', 'date': '2026-09-01', 'chunkIndex': 1, 'status': 'done'},
        {'type': 'daily', 'date': '2026-09-02', 'chunkIndex': 0, 'status': 'done'},
        # The second batch has not started yet; its absence must not mean done.
    ]})
    assert _report_missing_source_ranges(2, 'baskets', date_from=date(2026, 9, 1), date_to=date(2026, 9, 2)) == [(date(2026, 9, 2), date(2026, 9, 2))]


def test_short_digest_includes_six_days_for_comparison_without_changing_selected_period():
    from app.routers.wb_reports_bff import _digest_source_bounds
    assert _digest_source_bounds(date(2026, 9, 7), date(2026, 9, 7)) == (date(2026, 9, 2), date(2026, 9, 7))


@pytest.mark.parametrize("metadata", [
    {},
    {"revenueBasis": "retailAmount", "financeSchemaVersion": "v2"},
    {"revenueBasis": "sellerPayout", "financeSchemaVersion": "v3"},
])
def test_report_daily_sources_ready_rejects_legacy_finance_basis(monkeypatch, metadata):
    monkeypatch.setattr(
        "app.routers.wb_reports_bff.list_source_cache_ranges_by_prefix",
        lambda *_args, **_kwargs: [{
            "dateFrom": "2026-06-25", "dateTo": "2026-07-24",
            "dailyDetailStatus": "fetched", "dailyAggregatesDays": 30,
            **metadata,
        }],
    )
    assert _report_daily_sources_ready(
        2, ("finance",), date_from=date(2026, 7, 8), date_to=date(2026, 7, 21),
    ) == (False, ["finance"])


def test_latest_rnp_cache_skips_ads_only_payload(monkeypatch):
    caches = [
        {
            "sourceKey": "reports_payload_rnp_2026-06-01_2026-07-01_sku_operational",
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "dateFrom": "2026-06-01",
            "dateTo": "2026-07-01",
            "report": {
                "diagnostics": {"summary": {"funnelRows": 0, "adsRows": 1}},
                "rows": [{"sku": "ADS-ONLY", "adImpressions": 100_328, "adOrders": 370}],
            },
        }
    ]

    monkeypatch.setattr("app.routers.wb_reports_bff.list_source_cache_by_prefix", lambda *_args, **_kwargs: caches)

    latest = _latest_report_payload_cache(
        organization_id=1,
        report_id="rnp",
        group_by="sku",
        source="operational",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 7, 1),
    )

    assert latest is None


def test_pnl_export_estimate_does_not_pass_user_wb_token_to_report_runtime(monkeypatch):
    from app.routers import wb_reports_bff as bff_router

    captured: dict[str, str | None] = {}

    def spy_build_pnl_report(**kwargs):
        captured["wb_token"] = kwargs.get("wb_token")
        return SimpleNamespace(rows=[])

    monkeypatch.setattr("app.routers.wb_reports_bff.build_pnl_report", spy_build_pnl_report)

    rows = bff_router._estimated_export_rows(
        "pnl",
        date(2026, 7, 1),
        date(2026, 7, 31),
        finance_allowed=False,
        wb_token="user-token",
        organization_id=1,
    )

    assert rows == 0
    assert captured["wb_token"] is None


def test_pnl_report_cache_miss_does_not_fall_back_to_live_wb(monkeypatch):
    from app import wb_reports_sprint_d

    monkeypatch.setattr(wb_reports_sprint_d, "get_source_cache", lambda *_args, **_kwargs: {})
    assert not hasattr(wb_reports_sprint_d, "build_wb_reports_sources_snapshot")
    assert not hasattr(wb_reports_sprint_d, "build_ads_attribution_snapshot")

    payload = wb_reports_sprint_d.build_pnl_report(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 31),
        group_by="sku",
        requested_state="preliminary",
        finance_allowed=True,
        organization_id=1,
        wb_token="token",
    )

    assert payload.sourceStatus == "blocked"
    assert payload.rows == []
    assert "WB_PNL_FINANCE_CACHE_MISSING" in payload.blockerIds


def test_rnp_report_uses_period_caches_without_live_wb(monkeypatch):
    from app.wb_api import rnp_runtime
    from app.wb_reports_sprint_d import build_rnp_report

    def fake_get_source_cache(_organization_id, key, **_kwargs):
        if key.startswith("rnp_report_"):
            return {}
        if key.startswith("baskets_"):
            return {
                "aggregates": {
                    "101": {
                        "nmId": 101,
                        "cartCount": 8,
                        "orderCount": 4,
                        "orderSumKopecks": 100000,
                    }
                }
            }
        if key.startswith("ads_"):
            return {
                "aggregates": {
                    "101": {
                        "nmId": 101,
                        "adSpendKopecks": 10000,
                        "impressions": 200,
                        "clicks": 20,
                        "ordersCount": 2,
                        "ordersKopecks": 50000,
                    }
                }
            }
        return {}

    monkeypatch.setattr(rnp_runtime, "get_source_cache", fake_get_source_cache)
    monkeypatch.setattr(rnp_runtime, "save_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(rnp_runtime, "_load_or_refresh_funnel_rows", lambda **_kwargs: pytest.fail("RNP must not fetch WB sales funnel directly"))
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda *a: dict(sourceComplete=True,
        aggregates={"101": dict(netSalesUnits=5, realizationRevenueKopecks=50000)}))
    assert not hasattr(rnp_runtime, "build_ads_attribution_snapshot")

    payload = build_rnp_report(
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 7),
        group_by="sku",
        finance_allowed=True,
        organization_id=1,
        wb_token="token",
        force_refresh=True,
    )

    assert payload.sourceStatus == "fresh"
    assert payload.diagnostics["summary"]["cacheStatus"] == "hit"
    assert payload.rows[0].nmId == 101
    assert payload.rows[0].adSpendKopecks == 10000
    assert payload.drrPct == 10.0


def test_bff_export_supports_async_job_flow_and_compat_get():
    api = client()
    headers = auth_headers(api, "viewer")

    queued = api.post("/api/wb/reports/export/pnl", headers=headers)
    assert queued.status_code == 200
    queued_payload = queued.json()
    assert queued_payload["status"] == "queued"
    assert queued_payload["jobId"] >= 1
    assert queued_payload["rows"] >= 0

    status = api.get(f"/api/wb/reports/export/jobs/{queued_payload['exportId']}", headers=headers)
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["status"] == "success"
    assert status_payload["fileName"] is not None
    assert status_payload["downloadUrl"] is not None

    compat = api.get("/api/wb/reports/export/pnl", headers=headers)
    assert compat.status_code == 200
    compat_payload = compat.json()
    assert compat_payload["status"] == "success"
    assert compat_payload["fileName"] is not None


def test_starting_pnl_job_immediately_exposes_cash_flow_job_to_1c(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.wb_reports_bff.save_source_cache", lambda _org, _key, payload: payload)
    from app import repricer_tasks
    from app.routers import one_c_cash_flow

    captured_args = {}
    monkeypatch.setattr("app.routers.one_c_cash_flow.JOBS_PATH", tmp_path / "1c_jobs.json")
    monkeypatch.setattr(
        "app.routers.wb_reports_bff._report_daily_sources_ready",
        lambda *_args, **_kwargs: (False, ["finance"]),
    )

    def fake_delay(*args, **kwargs):
        jobs = one_c_cash_flow._read_jobs_state()["jobs"]
        assert len(jobs) == 1, "1C request must exist before WB refresh dispatch"
        assert jobs[0]["organizationId"] == 1
        assert jobs[0]["periodFrom"] == "2026-05-01"
        assert jobs[0]["periodTo"] == "2026-05-31"
        assert jobs[0]["status"] == "pending"
        captured_args["args"] = args
        captured_args["kwargs"] = kwargs
        return type("Task", (), {"id": "pnl-task-1"})()
    monkeypatch.setattr(repricer_tasks.refresh_report_sources_for_org, "delay", fake_delay)
    monkeypatch.setattr(repricer_tasks.refresh_report_sources_for_org, "apply_async",
        lambda *, args, kwargs, **options: fake_delay(*args, **kwargs))
    monkeypatch.setattr(
        repricer_tasks.build_report_for_org, "delay",
        lambda *_args, **_kwargs: pytest.fail("missing WB sources must refresh before building"),
    )

    api = client()
    response = api.post(
        "/api/wb/reports/pnl/jobs",
        params={"preset": "custom", "from": "2026-05-01", "to": "2026-05-31", "source": "financial", "groupBy": "sku"},
        headers=auth_headers(api, "finance_viewer"),
    )

    assert response.status_code == 200
    assert captured_args["args"][-1] is None
    assert response.json()["kind"] == "report_source_refresh"
    assert response.json()["cashFlow"]["status"] == "pending"
    next_job = api.get("/api/1c/jobs/next", headers={"Authorization": "Bearer change-me"})
    assert next_job.status_code == 200
    assert next_job.json()["has_job"] is True
    assert next_job.json()["period_from"] == "2026-05-01"
    assert next_job.json()["period_to"] == "2026-05-31"


def test_starting_expenses_job_immediately_exposes_cash_flow_job_to_1c(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.wb_reports_bff.save_source_cache", lambda _org, _key, payload: payload)
    from app import repricer_tasks

    captured_args = {}
    monkeypatch.setattr("app.routers.one_c_cash_flow.JOBS_PATH", tmp_path / "1c_jobs.json")
    def fake_delay(*args, **kwargs):
        captured_args["args"] = args
        captured_args["kwargs"] = kwargs
        return type("Task", (), {"id": "expenses-task-1"})()
    monkeypatch.setattr(repricer_tasks.build_report_for_org, "delay", fake_delay)

    api = client()
    response = api.post(
        "/api/wb/reports/expenses/jobs",
        params={"preset": "custom", "from": "2026-06-01", "to": "2026-06-30", "groupBy": "sku"},
        headers=auth_headers(api, "finance_viewer"),
    )

    assert response.status_code == 200
    assert captured_args["args"][-1] is None
    assert response.json()["cashFlow"]["status"] == "pending"
    next_job = api.get("/api/1c/jobs/next", headers={"Authorization": "Bearer change-me"})
    assert next_job.status_code == 200
    assert next_job.json()["has_job"] is True
    assert next_job.json()["period_from"] == "2026-06-01"
    assert next_job.json()["period_to"] == "2026-06-30"
