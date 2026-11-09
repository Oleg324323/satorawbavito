from __future__ import annotations

from datetime import date
from app.wb_api.ads_runtime import AdsAttributionRow, AdsAttributionSnapshot
from app.wb_api.client import RateLimitInfo, WbApiRequest, WbApiResponseEnvelope, classify_wb_error
from app.wb_api.rnp_runtime import _fetch_sales_funnel_rows, _rnp_row_from_sources, build_rnp_snapshot


def _install_cached_ads(monkeypatch, factory):
    def cached_ads(**kwargs):
        snapshot = factory(**kwargs)
        return (snapshot.rows, snapshot.totals, snapshot.source_status,
                snapshot.confidence, snapshot.blocker_ids, {})
    monkeypatch.setattr("app.wb_api.rnp_runtime._cached_ads_rows", cached_ads)


def test_build_rnp_snapshot_merges_cached_sales_funnel_with_ads(monkeypatch):
    saved_payloads: dict[str, dict] = {}

    def fake_get_source_cache(organization_id: int, source_key: str, slim: bool = True):
        assert organization_id == 7
        if source_key == "rnp_funnel_v2_2026-06-01_2026-06-07":
            return {
                "rows": [
                    {
                        "nmId": 123456,
                        "sku": "FBBT_01",
                        "productName": "Demo SKU 1",
                        "brandName": "Satorna",
                        "categoryName": "Футболки",
                        "openCount": 100,
                        "cartCount": 20,
                        "orderCount": 10,
                        "orderSumKopecks": 1000000,
                        "buyoutCount": 7,
                        "buyoutSumKopecks": 700000,
                        "buyoutPct": 70.0,
                        "cartCountDeltaPct": 5.0,
                        "orderCountDeltaPct": -2.0,
                        "orderSumDeltaPct": 4.0,
                        "atcrPct": 20.0,
                        "cartToOrderPct": 50.0,
                    }
                ]
            }
        return saved_payloads.get(source_key)

    def fake_save_source_cache(organization_id: int, source_key: str, payload: dict):
        saved_payloads[source_key] = payload
        return payload

    def fake_ads_snapshot(**kwargs):
        return AdsAttributionSnapshot(
            source_status="fresh",
            confidence="high",
            blocker_ids=[],
            source_evidence=[],
            totals={
                "ad_spend_kopecks": 10000,
                "impressions": 40,
                "clicks": 8,
                "cart_adds": 4,
                "orders_count": 2,
                "orders_kopecks": 200000,
            },
            rows=[
                AdsAttributionRow(
                    campaign_id="900",
                    sku_id="123456",
                    attribution_level="exact_sku",
                    confidence="high",
                    ad_spend_kopecks=10000,
                    impressions=40,
                    clicks=8,
                    cart_adds=4,
                    orders_count=2,
                    orders_kopecks=200000,
                )
            ],
        )

    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_api.rnp_runtime.save_source_cache", fake_save_source_cache)
    _install_cached_ads(monkeypatch, fake_ads_snapshot)
    monkeypatch.setattr("app.wb_api.rnp_runtime._cached_funnel_rows", lambda **kwargs: (
        fake_get_source_cache(7, "rnp_funnel_v2_2026-06-01_2026-06-07")["rows"], "hit", [],
    ))

    snapshot = build_rnp_snapshot(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 7),
        group_by="sku",
        organization_id=7,
        wb_token="token",
    )

    assert snapshot.rows[0].nmId == 123456
    assert snapshot.rows[0].openCount == 100
    assert snapshot.rows[0].impressions is None
    assert snapshot.rows[0].ctrPct is None
    assert snapshot.rows[0].wishlistCount is None
    assert snapshot.rows[0].adClicks == 8
    assert snapshot.rows[0].adSpendKopecks == 10000
    assert snapshot.rows[0].organicOrderCountEstimated == 8
    assert snapshot.rows[0].organicSalesKopecksEstimated == 800000
    assert snapshot.rows[0].acooPct == 5.0
    assert snapshot.rows[0].tacooPct == 1.0
    assert snapshot.rows[0].organicEstimate is True
    assert "estimated_organic" in snapshot.rows[0].reasons
    assert "rnp_report_v8_2026-06-01_2026-06-07_sku" in saved_payloads


def test_build_rnp_snapshot_reads_ad_prefixed_metrics_from_period_cache(monkeypatch):
    saved_payloads: dict[str, dict] = {}

    def fake_get_source_cache(_organization_id: int, source_key: str, slim: bool = True):
        if source_key == "baskets_2026-06-01_2026-06-07":
            return {
                "dateFrom": "2026-06-01",
                "dateTo": "2026-06-07",
                "aggregates": {
                    "123456": {
                        "openCount": 100,
                        "impressions": 1_000,
                        "cartCount": 20,
                        "wishlistCount": 3,
                        "orderCount": 10,
                        "orderSumKopecks": 1_000_000,
                    }
                },
            }
        if source_key == "ads_2026-06-01_2026-06-07":
            return {
                "dateFrom": "2026-06-01",
                "dateTo": "2026-06-07",
                "aggregates": {
                    "123456": {
                        "adImpressions": 40,
                        "adClicks": 8,
                        "adCartAdds": 4,
                        "adOrders": 2,
                        "adSalesKopecks": 200_000,
                        "adSpendKopecks": 10_000,
                    }
                },
            }
        return saved_payloads.get(source_key)

    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_api.rnp_runtime.save_source_cache", lambda _org_id, key, payload: saved_payloads.setdefault(key, payload))
    monkeypatch.setattr("app.wb_api.rnp_runtime.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])

    snapshot = build_rnp_snapshot(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 7),
        group_by="sku",
        organization_id=7,
        wb_token=None,
    )

    row = snapshot.rows[0]
    assert row.impressions == 1_000
    assert row.ctrPct == 10.0
    assert row.wishlistCount == 3
    assert row.adImpressions == 40
    assert row.adClicks == 8
    assert row.adCartAdds == 4
    assert row.adOrders == 2
    assert row.adSalesKopecks == 200_000
    assert row.adSpendKopecks == 10_000


def test_build_rnp_snapshot_reads_report_cache_without_refetching_wb(monkeypatch):
    cached_row = _rnp_row_from_sources(
        {"nmId": 1, "sku": "SKU-1", "productName": "Товар"}, None, "partial", "medium"
    ).model_dump(mode="json")

    def fake_get_source_cache(_organization_id: int, source_key: str, slim: bool = True):
        if source_key == "rnp_report_v8_2026-06-01_2026-06-07_sku":
            return {
                "rows": [cached_row],
                "sourceRevision": {"baskets_": None, "ads_": None},
                "sourceStatus": "partial",
                "confidence": "medium",
                "blockerIds": [],
                "adsSourceStatus": "partial",
            }
        return None

    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", fake_get_source_cache)
    snapshot = build_rnp_snapshot(
        date_from=date(2026, 6, 1), date_to=date(2026, 6, 7), group_by="sku", organization_id=7, wb_token="token"
    )

    assert snapshot.cache_status == "hit"
    assert snapshot.rows[0].sku == "SKU-1"


def test_fetch_sales_funnel_rows_paginates_until_wb_returns_short_page(monkeypatch):
    class FakeAnalyticsClient:
        def __init__(self):
            self.requests: list[WbApiRequest] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            offset = int(request.jsonBody.get("offset") or 0)
            limit = int(request.jsonBody.get("limit") or 1000)
            page_size = 1000 if offset < 2000 else 150
            products = [
                {
                    "product": {"nmId": offset + index + 1, "vendorCode": f"SKU-{offset + index + 1}"},
                    "statistic": {"selected": {"openCount": 1}},
                }
                for index in range(page_size)
            ]
            assert limit == 1000
            return WbApiResponseEnvelope(
                request=request,
                statusCode=200,
                ok=True,
                data={"data": {"products": products}},
            )

    fake = FakeAnalyticsClient()
    monkeypatch.setattr("app.wb_api.rnp_runtime.build_wb_analytics_client", lambda *args, **kwargs: fake)

    rows = _fetch_sales_funnel_rows(date(2026, 6, 1), date(2026, 6, 7), wb_token="token")

    assert len(rows) == 2150
    assert [request.jsonBody["offset"] for request in fake.requests] == [0, 1000, 2000]


def test_sales_funnel_row_keeps_cancel_count_for_digest_cancel_series():
    from app.wb_api.rnp_runtime import _normalize_sales_funnel_product

    row = _normalize_sales_funnel_product(
        {
            "product": {"nmId": 101, "vendorCode": "SKU_101"},
            "statistic": {
                "selected": {
                    "openCount": 95_823,
                    "cartCount": 5_511,
                    "orderCount": 1_242,
                    "orderSum": 2_000,
                    "buyoutCount": 3,
                    "buyoutSum": 60,
                    "cancelCount": 4,
                    "cancelSum": 80,
                    "conversions": {"buyoutPercent": 0.2},
                }
            },
        }
    )

    assert row is not None
    assert row["cancelCount"] == 4
    assert row["cancelSumKopecks"] == 8_000


def test_fetch_sales_funnel_rows_retries_wb_429_retry_after(monkeypatch):
    sleeps: list[float] = []

    class RetryThenOkAnalyticsClient:
        def __init__(self):
            self.requests: list[WbApiRequest] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            if len(self.requests) == 1:
                rate_limit = RateLimitInfo(retryAfterSeconds=16)
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=429,
                    ok=False,
                    error=classify_wb_error(429, "Limited by global limiter, per seller", rate_limit),
                    rateLimit=rate_limit,
                )
            return WbApiResponseEnvelope(
                request=request,
                statusCode=200,
                ok=True,
                data={
                    "data": {
                        "products": [
                            {
                                "product": {"nmId": 376785768, "vendorCode": "Фч_sc_091"},
                                "statistic": {"selected": {"openCount": 877847, "cartCount": 5332, "orderCount": 1007}},
                            }
                        ]
                    }
                },
            )

    fake = RetryThenOkAnalyticsClient()
    monkeypatch.setattr("app.wb_api.rnp_runtime.build_wb_analytics_client", lambda *args, **kwargs: fake)
    monkeypatch.setattr("app.wb_api.rnp_runtime.time.sleep", sleeps.append)

    rows = _fetch_sales_funnel_rows(date(2026, 6, 1), date(2026, 7, 1), wb_token="token")

    assert len(fake.requests) == 2
    assert sleeps == [16.0]
    assert rows[0]["nmId"] == 376785768
    assert rows[0]["openCount"] == 877847
    assert rows[0]["cartCount"] == 5332
    assert rows[0]["orderCount"] == 1007


def test_build_rnp_snapshot_does_not_create_ads_only_rows_when_funnel_is_blocked(monkeypatch):
    def fake_load_or_refresh_funnel_rows(**kwargs):
        return [], "blocked", ["WB_SALES_FUNNEL_FAILED:429:Limited by global limiter, per seller"]

    def fake_ads_snapshot(**kwargs):
        return AdsAttributionSnapshot(
            source_status="fresh",
            confidence="high",
            blocker_ids=[],
            source_evidence=[],
            totals={
                "ad_spend_kopecks": 4813700,
                "impressions": 100328,
                "clicks": 7736,
                "cart_adds": 1040,
                "orders_count": 370,
                "orders_kopecks": 0,
            },
            rows=[
                AdsAttributionRow(
                    campaign_id="42",
                    sku_id="376785768",
                    attribution_level="exact_sku",
                    confidence="high",
                    ad_spend_kopecks=4813700,
                    impressions=100328,
                    clicks=7736,
                    cart_adds=1040,
                    orders_count=370,
                    orders_kopecks=0,
                )
            ],
        )

    saved_payloads: dict[str, dict] = {}

    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.wb_api.rnp_runtime._cached_funnel_rows", fake_load_or_refresh_funnel_rows)
    _install_cached_ads(monkeypatch, fake_ads_snapshot)
    monkeypatch.setattr(
        "app.wb_api.rnp_runtime.save_source_cache",
        lambda _organization_id, source_key, payload: saved_payloads.setdefault(source_key, payload),
    )

    snapshot = build_rnp_snapshot(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 7, 1),
        group_by="sku",
        organization_id=7,
        wb_token="token",
    )

    assert snapshot.source_status == "blocked"
    assert snapshot.confidence == "blocked"
    assert snapshot.rows == []
    assert snapshot.diagnostics["summary"]["funnelRows"] == 0
    assert snapshot.diagnostics["summary"]["reportRows"] == 0
    assert snapshot.diagnostics["summary"]["adsRows"] == 1
    assert snapshot.diagnostics["summary"]["adsOnlySkuCount"] == 0
    assert snapshot.diagnostics["sources"][0]["status"] == "blocked"
    assert "WB_SALES_FUNNEL_FAILED:429" in snapshot.diagnostics["sources"][0]["errors"][0]
    payload = saved_payloads["rnp_report_v8_2026-06-01_2026-07-01_sku"]
    assert payload["rows"] == []


def test_build_rnp_snapshot_keeps_ads_metrics_unknown_when_ads_source_is_partial(monkeypatch):
    def fake_get_source_cache(organization_id: int, source_key: str, slim: bool = True):
        if source_key == "rnp_funnel_v2_2026-06-01_2026-06-07":
            return {
                "rows": [
                    {
                        "nmId": 1025681400,
                        "sku": "Лбbt_0236",
                        "productName": "Лонгслив оверсайз",
                        "openCount": 69155,
                        "cartCount": 7819,
                        "orderCount": 1282,
                        "orderSumKopecks": 230028400,
                    }
                ]
            }
        return None

    def fake_save_source_cache(organization_id: int, source_key: str, payload: dict):
        return payload

    def fake_ads_snapshot(**kwargs):
        return [], {}, "partial", "low", ["WB-02"], {}

    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_api.rnp_runtime.save_source_cache", fake_save_source_cache)
    monkeypatch.setattr("app.wb_api.rnp_runtime._cached_ads_rows", fake_ads_snapshot)
    monkeypatch.setattr(
        "app.wb_api.rnp_runtime._cached_funnel_rows",
        lambda **kwargs: (fake_get_source_cache(7, "rnp_funnel_v2_2026-06-01_2026-06-07")["rows"], "hit", []),
    )

    snapshot = build_rnp_snapshot(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 7),
        group_by="sku",
        organization_id=7,
        wb_token="token",
    )

    row = snapshot.rows[0]
    assert snapshot.source_status == "partial"
    assert row.openCount == 69155
    assert row.adImpressions is None
    assert row.adClicks is None
    assert row.adSpendKopecks is None
    assert row.organicOrderCountEstimated is None
    assert row.organicSalesKopecksEstimated is None
    assert row.organicEstimate is False
    assert "ads_source_partial" in row.reasons
    assert "partial_source" in row.reasons
    assert snapshot.diagnostics["summary"]["funnelRows"] == 1
    assert snapshot.diagnostics["summary"]["adsRows"] == 0
    assert snapshot.diagnostics["summary"]["adsMatchedSkuCount"] == 0
    assert snapshot.diagnostics["summary"]["missingAdsSkuCount"] == 1
    assert snapshot.diagnostics["summary"]["adsOnlySkuCount"] == 0
    assert snapshot.diagnostics["funnelRequest"]["selectedPeriod"] == {"start": "2026-06-01", "end": "2026-06-07"}
    assert snapshot.diagnostics["rowExamples"][0]["nmId"] == 1025681400
    assert snapshot.diagnostics["summary"]["topReasons"]["ads_source_partial"] == 1
    assert snapshot.diagnostics["sources"][0]["endpoint"] == "POST /api/analytics/v3/sales-funnel/products"
    assert snapshot.diagnostics["sources"][1]["endpoint"] == "GET /adv/v3/fullstats"
    assert snapshot.diagnostics["sources"][1]["status"] == "partial"
    assert snapshot.diagnostics["missingAdsExamples"][0]["nmId"] == 1025681400


def test_build_rnp_snapshot_does_not_block_funnel_on_optional_warehouse_snapshot(monkeypatch):
    def fake_get_source_cache(organization_id: int, source_key: str, slim: bool = True):
        if source_key == "rnp_funnel_v2_2026-06-01_2026-06-07":
            return {
                "rows": [
                    {
                        "nmId": 123456,
                        "sku": "FBBT_01",
                        "productName": "Demo SKU",
                        "openCount": 100,
                        "cartCount": 20,
                        "orderCount": 10,
                        "orderSumKopecks": 1000000,
                    }
                ]
            }
        return None

    def fake_ads_snapshot(**kwargs):
        return AdsAttributionSnapshot(
            source_status="fresh",
            confidence="high",
            blocker_ids=[],
            source_evidence=[],
            totals={},
            rows=[],
        )

    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", fake_get_source_cache)
    monkeypatch.setattr("app.wb_api.rnp_runtime.save_source_cache", lambda *_args, **_kwargs: {})
    _install_cached_ads(monkeypatch, fake_ads_snapshot)
    monkeypatch.setattr("app.wb_api.rnp_runtime._cached_funnel_rows", lambda **kwargs: (
        fake_get_source_cache(7, "rnp_funnel_v2_2026-06-01_2026-06-07")["rows"], "hit", [],
    ))
    snapshot = build_rnp_snapshot(
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 7),
        group_by="sku",
        organization_id=7,
        wb_token="token",
    )

    row = snapshot.rows[0]
    assert row.warehouseId is None
    assert row.warehouseName is None
    assert row.warehouses == []
    assert snapshot.diagnostics["summary"]["activeWarehouseCount"] == 0
