from __future__ import annotations

from datetime import date

import pytest

from app.wb_api.client import WbApiRequest, WbApiResponseEnvelope
from app.wb_api.reports_sources_runtime import (
    _load_realization_rows,
    _load_stock_report_wb_warehouses,
    build_wb_reports_sources_snapshot,
)
from app.wb_reports_sprint_d import build_pnl_report


def test_reports_sources_snapshot_uses_finance_and_warehouse_remains_sources():
    snapshot = build_wb_reports_sources_snapshot(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )

    assert snapshot.financial_rows
    assert snapshot.finance_reconciliation is not None
    assert snapshot.commission_cost_by_nm_kopecks[123456] > 0
    assert snapshot.logistics_cost_by_nm_kopecks[123456] > 0
    assert snapshot.stocks
    first_stock = snapshot.stocks[0]
    assert first_stock.warehouse_name == "Коледино"
    assert first_stock.available_units == first_stock.quantity + first_stock.in_way_from_client


def test_stock_report_wb_warehouses_paginates_until_short_page():
    class FakeAnalyticsClient:
        def __init__(self):
            self.offsets: list[int] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            assert request.path == "/api/analytics/v1/stocks-report/wb-warehouses"
            limit = int(request.jsonBody.get("limit") or 0)
            offset = int(request.jsonBody.get("offset") or 0)
            self.offsets.append(offset)
            page_size = limit if offset < 2000 else 1
            rows = [
                {
                    "nmId": offset + index + 1,
                    "warehouseName": "Коледино",
                    "quantity": 10,
                    "inWayToClient": 0,
                    "inWayFromClient": 0,
                }
                for index in range(page_size)
            ]
            return WbApiResponseEnvelope(
                request=request,
                statusCode=200,
                ok=True,
                data={"data": rows},
            )

    fake = FakeAnalyticsClient()

    envelope, rows = _load_stock_report_wb_warehouses(fake, limit=1000, sleeper=lambda _seconds: None)

    assert envelope.ok
    assert len(rows) == 2001
    assert fake.offsets == [0, 1000, 2000]


def test_stock_report_wb_warehouses_uses_official_max_page_limit_by_default():
    class FakeAnalyticsClient:
        def __init__(self):
            self.limits: list[int] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.limits.append(int(request.jsonBody.get("limit") or 0))
            return WbApiResponseEnvelope(
                request=request,
                statusCode=200,
                ok=True,
                data={"data": [{"nmId": 1, "warehouseName": "Коледино", "quantity": 10}]},
            )

    fake = FakeAnalyticsClient()

    envelope, rows = _load_stock_report_wb_warehouses(fake, sleeper=lambda _seconds: None)

    assert envelope.ok
    assert len(rows) == 1
    assert fake.limits == [250_000]


def test_stock_report_wb_warehouses_keeps_rows_when_next_page_is_rate_limited():
    class PageThenRateLimitClient:
        def __init__(self):
            self.offsets: list[int] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            offset = int(request.jsonBody.get("offset") or 0)
            self.offsets.append(offset)
            if offset == 0:
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=200,
                    ok=True,
                    data={
                        "data": [
                            {"nmId": index + 1, "warehouseName": "Коледино", "quantity": 1}
                            for index in range(1000)
                        ]
                    },
                )
            return WbApiResponseEnvelope(request=request, statusCode=429, ok=False, data={"error": "rate limited"})

    fake = PageThenRateLimitClient()
    sleeps: list[float] = []

    envelope, rows = _load_stock_report_wb_warehouses(fake, limit=1000, sleeper=sleeps.append)

    assert envelope.statusCode == 429
    assert envelope.ok is False
    assert len(rows) == 1000
    assert fake.offsets == [0, 1000, 1000, 1000, 1000]
    assert sleeps == [20.0, 66.0, 66.0, 66.0]


def test_reports_sources_snapshot_uses_partial_stock_page_when_next_page_is_rate_limited(monkeypatch):
    class FakeAnalyticsClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            if request.path == "/api/analytics/v1/stocks-report/wb-warehouses":
                offset = int(request.jsonBody.get("offset") or 0)
                if offset == 0:
                    return WbApiResponseEnvelope(
                        request=request,
                        statusCode=200,
                        ok=True,
                        data={
                            "data": [
                                {
                                    "nmId": 123456,
                                    "chrtId": 7654321,
                                    "warehouseId": 507,
                                    "warehouseName": "Коледино",
                                    "regionName": "Центральный",
                                    "quantity": 43,
                                    "inWayToClient": 14,
                                    "inWayFromClient": 11,
                                }
                                for _ in range(1000)
                            ]
                        },
                    )
                return WbApiResponseEnvelope(request=request, statusCode=429, ok=False, data={"error": "rate limited"})
            if request.path == "/api/v1/warehouse_remains":
                return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": {"taskId": "stock-task"}})
            if request.path == "/api/v1/warehouse_remains/tasks/stock-task/status":
                return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": {"status": "running"}})
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": []})

    class EmptyClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": []})

    monkeypatch.setattr("app.wb_api.reports_sources_runtime.build_wb_analytics_client", lambda *args, **kwargs: FakeAnalyticsClient())
    monkeypatch.setattr("app.wb_api.reports_sources_runtime.build_wb_statistics_client", lambda *args, **kwargs: EmptyClient())
    monkeypatch.setattr("app.wb_api.reports_sources_runtime.build_wb_finance_client", lambda *args, **kwargs: EmptyClient())
    monkeypatch.setattr("app.wb_api.reports_sources_runtime.STOCK_REPORT_PAGE_LIMIT", 1000)
    monkeypatch.setattr("app.wb_api.reports_sources_runtime.time.sleep", lambda _seconds: None)

    snapshot = build_wb_reports_sources_snapshot(
        date_from=date(2026, 6, 15),
        date_to=date(2026, 7, 15),
        wb_token="token",
    )

    assert snapshot.source_status == "partial"
    assert snapshot.stocks
    assert snapshot.stocks[0].nm_id == 123456
    assert snapshot.stocks[0].available_units == 54


def test_pnl_report_uses_realization_details_and_finance_reconciliation(monkeypatch):
    """Unscoped realization data cannot bypass the existing cache-only P&L guard.

    Keep the historical node ID, but reject its obsolete expectation of an
    automatically final report without organization-owned finance evidence.
    Source parsing/reconciliation itself is exercised by the snapshot test above.
    """
    import app.wb_reports_sprint_d as reports

    def unexpected_source_read(*args, **kwargs):
        pytest.fail("unscoped P&L must neither fetch nor read another owner's cache")

    monkeypatch.setattr(reports, "get_source_cache", unexpected_source_read)
    payload = build_pnl_report(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        group_by="sku",
        requested_state="final",
        finance_allowed=True,
    )

    assert payload.reportState == "blocked"
    assert payload.sourceStatus == "blocked"
    assert payload.blockerIds == ["WB_PNL_FINANCE_CACHE_MISSING"]
    assert payload.rows == []


def test_realization_rows_use_finance_delivery_money_not_dlv_prc_tariff(monkeypatch):
    monkeypatch.setattr("app.repricer_bff._FINANCE_REPORT_MIN_INTERVAL_S", 0)
    def fake_request(request: WbApiRequest) -> WbApiResponseEnvelope:
        assert request.method == "POST"
        assert request.path == "/api/finance/v1/sales-reports/detailed"
        if request.jsonBody["rrdId"]:
            return WbApiResponseEnvelope(request=request, statusCode=204, ok=True, data=None)
        return WbApiResponseEnvelope(
            request=request,
            statusCode=200,
            ok=True,
            data={
                "data": [
                    {
                        "rrdId": 1, "nmId": 123456, "currency": "RUB",
                        "docTypeName": "Продажа", "sellerOperName": "Продажа", "quantity": 1,
                        "retailAmount": 1000, "retailPriceWithDisc": 1000,
                        "forPay": 900, "ppvzSalesCommission": 100,
                        "commissionPercent": 10, "dlvPrc": 55.49,
                        "deliveryService": 2360.66,
                    }
                ]
            },
        )

    _, rows = _load_realization_rows(
        finance_client=None,  # type: ignore[arg-type]
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 1),
        request_finance=fake_request,
    )

    assert rows[0].logistics_kopecks == 236_066
