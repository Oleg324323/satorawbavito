from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app import repricer_bff as repricer_bff_module
from app import repricer_execution as repricer_execution_module
from app.routers import wb_repricer_bff as wb_repricer_bff_router
from app.routers.wb_repricer_bff import _finance_diagnostics_response, _margin_breakdown_from_rows, _repricer_list_summary, _request_wb_token, _row_matches_sku_filters
from app.repricer_bff import _fetch_catalog_goods
from app.wb_api.price_units import wb_goods_price_to_kopecks
from app.wb_api.client import FakeWbApiClient, RateLimitInfo, RateLimitedWbApiClient, WbApiRequest, WbApiResponseEnvelope, classify_wb_error
from tests.auth_helpers import auth_headers
from tests.test_promotion_excel import _xlsx_bytes


def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def isolated_shared_catalog(monkeypatch, tmp_path):
    """Legacy source fixtures have no canonical catalog; SQL costs are tested separately."""
    monkeypatch.setattr("app.platform.economics.legacy_catalog.catalog_facts", lambda org, **kw: {})
    from app.repricer_persistence import store
    monkeypatch.setattr(store, "_STATE_FILE", tmp_path / "repricer-state.json")
    monkeypatch.setattr(store, "_MEMORY_RUNTIME_STATE", {})
    monkeypatch.setattr(store, "_MEMORY_ALGORITHM_SETTINGS", {})


def test_catalog_goods_rate_limit_does_not_fallback_spam_same_endpoint():
    fake = FakeWbApiClient(
        errors={"/api/v2/list/goods/filter": 429},
        headers={
            "/api/v2/list/goods/filter": {
                "X-Ratelimit-Limit": "10",
                "X-Ratelimit-Remaining": "0",
                "X-Ratelimit-Retry": "1",
            }
        },
    )
    wb_client = RateLimitedWbApiClient(inner=fake)

    try:
        repricer_bff_module._request_catalog_goods_page(wb_client, limit=1000, offset=0)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 429
    else:
        raise AssertionError("Expected catalog goods request to surface WB rate limit")

    assert [request.method for request in fake.requests] == ["POST", "POST"]
    assert all(request.jsonBody == {"limit": 1000, "offset": 0} for request in fake.requests)


def test_content_cards_rate_limit_retries_same_request():
    class RetryContentClient:
        def __init__(self):
            self.requests: list[WbApiRequest] = []
            self.sleeps: list[float] = []

        def _sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            if len(self.requests) == 1:
                rate_limit = RateLimitInfo(retryAfterSeconds=1)
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=429,
                    ok=False,
                    error=classify_wb_error(429, "Limited by global limiter, per seller", rate_limit),
                    rateLimit=rate_limit,
                )
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"cards": []})

    progress: list[dict[str, object]] = []
    payload = repricer_bff_module._request_or_raise_content_cards(
        RetryContentClient(),  # type: ignore[arg-type]
        WbApiRequest(method="POST", path="/content/v2/get/cards/list", jsonBody={"settings": {"cursor": {"limit": 100}}}),
        progress_callback=progress.append,
    )

    assert payload == {"cards": []}
    assert progress[0]["phase"] == "rate_limit"


def test_repricer_ui_requires_current_user_wb_token_even_when_env_token_exists(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-without-token", organization_id=1),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_user_wb_token_secret",
        lambda *_args, **_kwargs: None,
    )

    try:
        _request_wb_token(object())
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert getattr(exc, "detail", None) == "WB_TOKEN_REQUIRED"
    else:
        raise AssertionError("Expected user-facing repricer requests to require a user WB token")


@pytest.mark.parametrize("user_token,organization_token", [
    ("user-token", "organization-token"),
    (None, "organization-token"),
    (None, None),
])
def test_repricer_bff_sku_list_reads_cached_goods(monkeypatch, user_token, organization_token):
    repricer_bff_module.COMMISSION_TARIFFS_CACHE.clear()
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
    monkeypatch.setattr(wb_repricer_bff_router, "get_user_wb_token_secret", lambda _user_id: user_token)
    def organization_secret(organization_id):
        assert organization_id == 1
        return organization_token
    monkeypatch.setattr(wb_repricer_bff_router, "get_organization_wb_token_secret", organization_secret)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", organization_id=1),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda _organization_id: [{"vendorCode": "FBBT_42", "nmID": 123456, "sizes": [{"price": 129000, "discountedPrice": 119900}]}],
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.cached_goods_meta",
        lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.compact_heavy_source_cache_rows", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_meta_fields", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_fetched_at", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        repricer_bff_module,
        "fetch_commission_tariffs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("list read must not fetch WB tariffs")),
    )
    api = client()
    cached_tokens = []
    read_cached = wb_repricer_bff_router._list_repricer_skus_from_cached_sources
    def capture_connection(*args, **kwargs):
        from app.repricer_page_finance import connection_key, page_connection
        expected = user_token or organization_token
        assert page_connection.get() == (connection_key(expected) if expected else None)
        cached_tokens.append(kwargs["wb_token"])
        return read_cached(*args, **kwargs)
    monkeypatch.setattr(wb_repricer_bff_router, "_list_repricer_skus_from_cached_sources", capture_connection)

    listing = api.get("/api/v1/wb-repricer/sku", headers=auth_headers(api, "viewer"))
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["total"] == 1
    assert payload["cache"]["nextOffset"] == 1000
    assert payload["summary"]["skuCount"] == 1
    assert "revenueKopecks" in payload["summary"]
    assert "totalBaskets" in payload["summary"]
    assert any(item["meta"]["articleId"] == "FBBT_42" for item in payload["items"])

    by_nm_id = api.get("/api/v1/wb-repricer/sku", params={"q": "123456"}, headers=auth_headers(api, "viewer"))
    assert by_nm_id.status_code == 200
    assert by_nm_id.json()["total"] == 1

    by_nm_id_list = api.get("/api/v1/wb-repricer/sku", params={"q": "999999 123456"}, headers=auth_headers(api, "viewer"))
    assert by_nm_id_list.status_code == 200
    assert by_nm_id_list.json()["total"] == 1
    assert cached_tokens[-2:] == [user_token or organization_token] * 2


def test_pending_price_approval_sent_upload_updates_local_price(monkeypatch):
    approval = {
        "approvalId": "apr_1",
        "status": "pending",
        "articleId": "SKU_SENT",
        "nmId": 123,
        "oldPriceKopecks": 119_100,
        "recommendedPriceKopecks": 120_000,
        "strategyName": "worker",
        "draftRequest": {
            "scenario": "complete",
            "articleId": "SKU_SENT",
            "nmId": 123,
            "candidateSellerPriceKopecks": 120_000,
            "economics": {
                "cogsKopecks": 10_000,
                "commissionPct": 10,
                "logisticsKopecks": 1_000,
            },
            "reason": "worker test",
        },
    }
    saved_goods: list[list[dict[str, object]]] = []
    price_changes: list[dict[str, object]] = []

    monkeypatch.setattr(wb_repricer_bff_router, "actor_from_request", lambda _request: SimpleNamespace(actor_id="sender", role="price_sender"))
    monkeypatch.setattr(wb_repricer_bff_router, "_ensure_price_send_permission", lambda _actor: None)
    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 2)
    monkeypatch.setattr(wb_repricer_bff_router, "_flush_org_repricer_state", lambda _organization_id: True)
    monkeypatch.setattr(wb_repricer_bff_router, "_request_wb_token", lambda _request: "wb-token")
    monkeypatch.setattr(wb_repricer_bff_router, "_build_repricing_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(wb_repricer_bff_router, "get_pending_price_approval", lambda **_kwargs: dict(approval))
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "update_pending_price_approval",
        lambda **kwargs: {**approval, **kwargs["patch"], "approvalId": kwargs["approval_id"]},
    )
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "_list_repricer_skus_for_request",
        lambda *_args, **_kwargs: [
            {
                "meta": {"articleId": "SKU_SENT", "nmId": 123, "name": "Sent SKU", "currentPriceKopecks": 119_100},
                "strategy": {"name": "Worker strategy"},
            }
        ],
    )
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "list_cached_goods",
        lambda _organization_id: [{"vendorCode": "SKU_SENT", "nmID": 123, "sizes": [{"price": 119_100, "discountedPrice": 119_100}]}],
    )
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "save_goods_page",
        lambda **kwargs: saved_goods.append(kwargs["goods"]) or {"totalCached": len(kwargs["goods"])},
    )
    monkeypatch.setattr(wb_repricer_bff_router, "record_repricer_price_change", lambda **kwargs: price_changes.append(kwargs))

    draft = SimpleNamespace(
        state="ready",
        draftId="drf_sent",
        normalized=SimpleNamespace(currentSellerPriceKopecks=119_100),
        model_dump=lambda **_kwargs: {"draftId": "drf_sent"},
    )
    approved = SimpleNamespace(model_dump=lambda **_kwargs: {"draftId": "drf_sent", "state": "approved"})
    apply_result = SimpleNamespace(applyMode="wb_api", wbMutationSent=True, statusLabel="processing")
    job = SimpleNamespace(
        jobId="job_sent",
        state="sent",
        blockerIds=[],
        rowErrors=[],
        lastApplyResult=apply_result,
        sourceStatus="partial",
        wbUploadId=321,
        wbStatus=None,
        notes=["WB accepted the price upload; status check is deferred"],
        model_dump=lambda **_kwargs: {"jobId": "job_sent", "state": "sent"},
    )
    monkeypatch.setattr(wb_repricer_bff_router, "create_price_draft", lambda **_kwargs: draft)
    monkeypatch.setattr(wb_repricer_bff_router, "approve_draft", lambda **_kwargs: approved)
    monkeypatch.setattr(wb_repricer_bff_router, "apply_approved_draft", lambda **_kwargs: job)

    response = wb_repricer_bff_router.approve_pending_price_approval(object(), "apr_1")

    assert response["job"]["state"] == "sent"
    assert saved_goods[0][0]["sizes"][0]["price"] == 120_000
    assert saved_goods[0][0]["sizes"][0]["discountedPrice"] == 120_000
    assert price_changes[0]["new_price_kopecks"] == 120_000
    assert price_changes[0]["source"] == "wb_api_pending"


def test_repricer_sku_filter_matches_frontend_statuses_and_exact_nm_id():
    row = {
        "meta": {
            "articleId": "JÔ×007",
            "nmId": 541547642,
            "status": "auto",
            "brand": None,
            "basketsLast7d": 18,
            "basketNorm": 20,
        },
        "analytics": {"marginPct": 28.2, "baskets": 18},
        "settings": {"pMinKopecks": 0},
    }

    assert _row_matches_sku_filters(row, q="", status="illiquid", brand="all", manager="all")
    assert not _row_matches_sku_filters(row, q="", status="loko", brand="all", manager="all")
    assert _row_matches_sku_filters(row, q="", status="all", brand="wb", manager="all")
    assert _row_matches_sku_filters(row, q="541547642", status="loko", brand="anomie", manager="manager-maria-dudina")


def test_repricer_sku_list_applies_costs_excel_cache_when_runtime_settings_missing(monkeypatch):
    previous_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    source_state = {
        "costs_excel": {
            "applied": [
                {
                    "articleId": "FBBT_42",
                    "nmId": 123456,
                    "vendorCode": "FBBT_42",
                    "cogsKopecks": 35_000,
                    "pMinKopecks": 175_000,
                    "pMaxKopecks": 176_300,
                }
            ]
        }
    }
    try:
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
        monkeypatch.setattr("app.routers.wb_repricer_bff.get_user_wb_token_secret", lambda _user_id: "user-token")
        monkeypatch.setattr(
            "app.routers.wb_repricer_bff.actor_from_request",
            lambda _request: SimpleNamespace(user_id="user-1", organization_id=1),
        )
        monkeypatch.setattr("app.routers.wb_repricer_bff.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("app.repricer_bff.fetch_commission_tariffs", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(
            "app.routers.wb_repricer_bff.list_cached_goods",
            lambda _organization_id: [{"vendorCode": "FBBT_42", "nmID": 123456, "sizes": [{"price": 129000, "discountedPrice": 119900}]}],
        )
        monkeypatch.setattr(
            "app.routers.wb_repricer_bff.cached_goods_meta",
            lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
        )
        monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda _organization_id, key, **_kwargs: source_state.get(key, {}))
        monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_meta_fields", lambda *_args, **_kwargs: {})
        monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_fetched_at", lambda *_args, **_kwargs: None)

        api = client()
        listing = api.get("/api/v1/wb-repricer/sku", headers=auth_headers(api, "viewer"))

        assert listing.status_code == 200
        row = listing.json()["items"][0]
        assert row["settings"]["cogsKopecks"] == 35_000
        assert row["settings"]["pMinKopecks"] == 175_000
        assert row["settings"]["pMaxKopecks"] == 176_300
    finally:
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_settings)


def test_repricer_list_summary_prefers_fact_margin_over_planned_period_margin():
    summary = _repricer_list_summary(
        [
            {
                "meta": {"articleId": "FBBT_42", "currentPriceKopecks": 100_000},
                "analytics": {
                    "ordersUnits": 1,
                    "salesUnits": 2,
                    "revenueKopecks": 100_000,
                    "netProfitKopecks": 20_000,
                    "plannedPeriodMarginKopecks": 40_000,
                    "cogsTotalKopecks": 30_000,
                    "expensesKopecks": 50_000,
                    "otherExpensesKopecks": 5_000,
                    "penaltyChargedKopecks": 1_000,
                    "penaltyReturnedKopecks": 200,
                    "deductionChargedKopecks": 3_000,
                    "deductionCompensationKopecks": 400,
                    "additionalPaymentKopecks": 500,
                    "adSpendKopecks": 7_000,
                    "adImpressions": 100,
                    "adClicks": 8,
                    "adCartAdds": 3,
                    "adOrders": 2,
                    "adRevenueKopecks": 25_000,
                },
            }
        ]
    )

    assert summary["marginKopecks"] == 20_000
    assert summary["avgMarginPct"] == 20.0
    assert summary["salesUnits"] == 2
    assert summary["ordersUnits"] == 1
    assert summary["returnsUnits"] == 0
    assert summary["cogsKopecks"] == 30_000
    assert summary["expensesKopecks"] == 50_000
    assert summary["otherExpensesKopecks"] == 5_000
    assert summary["penaltyChargedKopecks"] == 1_000
    assert summary["penaltyReturnedKopecks"] == 200
    assert summary["deductionChargedKopecks"] == 3_000
    assert summary["deductionCompensationKopecks"] == 400
    assert summary["additionalPaymentKopecks"] == 500
    assert summary["missingOtherExpensesKopecks"] == 0
    assert summary["adSpendKopecks"] == 7_000
    assert summary["adImpressions"] == 100
    assert summary["adClicks"] == 8
    assert summary["adCartAdds"] == 3
    assert summary["adOrders"] == 2
    assert summary["adRevenueKopecks"] == 25_000
    assert summary["adSkuCount"] == 1


def test_repricer_list_summary_does_not_invent_five_percent_other_expenses():
    summary = _repricer_list_summary(
        [
            {
                "meta": {"articleId": "FBBT_42", "currentPriceKopecks": 100_000},
                "analytics": {
                    "salesUnits": 1,
                    "revenueKopecks": 1_000_000,
                    "netProfitKopecks": 300_000,
                    "cogsTotalKopecks": 200_000,
                    "expensesKopecks": 500_000,
                },
            }
        ]
    )

    assert summary["missingOtherExpensesKopecks"] == 0
    assert summary["otherExpensesKopecks"] == 0
    assert summary["expensesKopecks"] == 500_000
    assert summary["marginKopecks"] == 300_000
    assert summary["avgMarginPct"] == 30.0


def test_repricer_list_summary_subtracts_unassigned_raw_storage_from_margin():
    summary = _repricer_list_summary(
        [
            {
                "meta": {"articleId": "FBBT_42", "currentPriceKopecks": 100_000},
                "analytics": {
                    "salesUnits": 1,
                    "revenueKopecks": 1_000_000,
                    "netProfitKopecks": 300_000,
                    "cogsTotalKopecks": 200_000,
                    "expensesKopecks": 500_000,
                    "storageKopecks": 0,
                    "otherExpensesKopecks": 50_000,
                },
            }
        ],
        finance_diagnostics={
            "storageAcceptance": {
                "rawRowsAvailable": True,
                "paidStorageSumKopecks": 293_908,
                "paidAcceptanceSumKopecks": 0,
            }
        },
    )

    assert summary["storageKopecks"] == 293_908
    assert summary["unassignedStorageKopecks"] == 293_908
    assert summary["expensesKopecks"] == 793_908
    assert summary["marginKopecks"] == 6_092
    assert summary["avgMarginPct"] == 0.6


def test_repricer_list_summary_keeps_source_orders_without_estimate():
    summary = _repricer_list_summary(
        [
            {
                "meta": {"articleId": "FBBT_42"},
                "analytics": {
                    "ordersUnits": 100,
                    "funnelOrderCount": 13,
                    "salesUnits": 10,
                    "returnsUnits": 1,
                    "revenueKopecks": 100_000,
                    "otherExpensesKopecks": 5_000,
                    "netProfitKopecks": 20_000,
                },
            }
        ]
    )

    assert summary["ordersUnits"] == 100
    assert summary["funnelOrderCount"] == 13


def test_repricer_bff_sku_settings_match_frontend_shape(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    api = client()

    settings = api.get("/api/v1/wb-repricer/sku/FBBT_42/settings", headers=auth_headers(api, "viewer"))
    assert settings.status_code == 200
    body = settings.json()
    assert body["meta"]["articleId"] == "FBBT_42"
    assert body["meta"]["nmId"] == 123456
    assert body["settings"]["automationEnabled"] is True
    # No sales-funnel price was provided in this fixture. Do not manufacture it
    # from the current seller price or the historical SPP percentage.
    assert body["analytics"]["avgPriceWithSppKopecks"] is None
    assert body["settings"]["promoBoostEnabled"] is False
    assert body["settings"]["promoBoostPct"] == 25
    assert body["settings"]["promoBoostHours"] == 48
    assert body["settings"]["pMinKopecks"] == 0
    assert body["settings"]["priceStepPct"] == 6
    assert body["settings"]["priceStepMinutes"] == 60
    assert body["settings"]["priceStepHours"] == 1
    assert body["settings"]["rrpKopecks"] == 0
    assert body["settings"]["nightMedianEnabled"] is True


def test_liquidation_start_records_global_changelog(monkeypatch):
    row = repricer_bff_module.list_repricer_skus("complete")[0]
    article_id = row["meta"]["articleId"]
    previous_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
    previous_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    previous_assignments = dict(repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS)
    previous_liquidation = dict(repricer_bff_module.LIQUIDATION_ACTIVE)
    previous_changelog = list(repricer_bff_module.CHANGELOG_ENTRIES)
    monkeypatch.setattr(
        repricer_bff_module,
        "list_repricer_skus",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sku_rows cache must be used")),
    )
    try:
        result = repricer_bff_module.start_repricer_liquidation([article_id], sku_rows=[row])

        assert result["started"] is True
        changelog = repricer_bff_module.list_repricer_changelog(article_id=article_id, triggers=["liquidation"])
        assert changelog["total"] >= 1
        assert changelog["items"][0]["trigger"] == "liquidation"
        assert "Запуск ликвидации" in changelog["items"][0]["reason"]
    finally:
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_META_OVERRIDES.update(previous_meta)
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_settings)
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.update(previous_assignments)
        repricer_bff_module.LIQUIDATION_ACTIVE.clear()
        repricer_bff_module.LIQUIDATION_ACTIVE.update(previous_liquidation)
        repricer_bff_module.CHANGELOG_ENTRIES.clear()
        repricer_bff_module.CHANGELOG_ENTRIES.extend(previous_changelog)


def test_liquidation_candidates_ignore_healthy_low_order_skus():
    row = deepcopy(repricer_bff_module.list_repricer_skus("complete")[0])
    row["meta"]["articleId"] = "HEALTHY_LOW_ORDER"
    row["meta"]["currentPriceKopecks"] = 120_000
    row["meta"]["basketsLast7d"] = 0
    row["meta"]["basketNorm"] = 20
    row["settings"]["pMinKopecks"] = 90_000
    row["analytics"]["ordersUnits"] = 0
    row["analytics"]["marginPct"] = 22
    row["analytics"]["wbStockUnits"] = 12

    payload = repricer_bff_module.get_repricer_liquidation(sku_rows=[row])

    assert payload["candidates"] == []


def test_work_status_endpoint_returns_pricing_status_rows(monkeypatch):
    row = repricer_bff_module.list_repricer_skus("complete")[0]
    previous_liquidation = dict(repricer_bff_module.LIQUIDATION_ACTIVE)
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._list_repricer_skus_for_request",
        lambda *_args, **_kwargs: [row],
    )
    repricer_bff_module.LIQUIDATION_ACTIVE.clear()
    try:
        api = client()

        response = api.get("/api/v1/wb-repricer/work-status", headers=auth_headers(api, "viewer"))

        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["total"] == 1
        assert payload["items"][0]["articleId"] == row["meta"]["articleId"]
        assert "lastDecision" in payload["items"][0]
    finally:
        repricer_bff_module.LIQUIDATION_ACTIVE.clear()
        repricer_bff_module.LIQUIDATION_ACTIVE.update(previous_liquidation)


def test_work_status_endpoint_keeps_liquidation_rows_over_limit(monkeypatch):
    rows = repricer_bff_module.list_repricer_skus("complete")[:2]
    regular_row = rows[0]
    liquidation_row = rows[1]
    liquidation_id = liquidation_row["meta"]["articleId"]
    previous_liquidation = dict(repricer_bff_module.LIQUIDATION_ACTIVE)
    try:
        repricer_bff_module.LIQUIDATION_ACTIVE.clear()
        repricer_bff_module.LIQUIDATION_ACTIVE[liquidation_id] = {
            "articleId": liquidation_id,
            "name": liquidation_row["meta"]["name"],
            "startPriceKopecks": liquidation_row["meta"]["currentPriceKopecks"],
            "currentPriceKopecks": liquidation_row["meta"]["currentPriceKopecks"],
            "targetPriceKopecks": 1,
            "startedAt": repricer_bff_module._iso_now(),
            "nextStepAt": repricer_bff_module._iso_now(),
            "stepPct": 3,
            "requiresNegativeMarginConfirm": False,
        }
        monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: None)
        monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
        monkeypatch.setattr(
            "app.routers.wb_repricer_bff._list_repricer_skus_for_request",
            lambda *_args, **_kwargs: [regular_row, liquidation_row],
        )
        api = client()

        response = api.get("/api/v1/wb-repricer/work-status?limit=1", headers=auth_headers(api, "viewer"))

        assert response.status_code == 200
        payload = response.json()
        assert payload["items"][0]["articleId"] == liquidation_id
        assert payload["items"][0]["stage"] == "active_liquidation"
    finally:
        repricer_bff_module.LIQUIDATION_ACTIVE.clear()
        repricer_bff_module.LIQUIDATION_ACTIVE.update(previous_liquidation)


def test_work_status_endpoint_keeps_runtime_liquidation_missing_from_rows(monkeypatch):
    article_id = "MISSING_LIQ_SKU"
    previous_liquidation = dict(repricer_bff_module.LIQUIDATION_ACTIVE)
    try:
        repricer_bff_module.LIQUIDATION_ACTIVE.clear()
        repricer_bff_module.LIQUIDATION_ACTIVE[article_id] = {
            "articleId": article_id,
            "name": article_id,
            "startPriceKopecks": 300_000,
            "currentPriceKopecks": 285_000,
            "targetPriceKopecks": 180_000,
            "startedAt": repricer_bff_module._iso_now(),
            "nextStepAt": repricer_bff_module._iso_now(),
            "stepPct": 5,
            "requiresNegativeMarginConfirm": False,
        }
        monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: None)
        monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
        monkeypatch.setattr("app.routers.wb_repricer_bff._list_repricer_skus_for_request", lambda *_args, **_kwargs: [])
        api = client()

        response = api.get("/api/v1/wb-repricer/work-status", headers=auth_headers(api, "viewer"))

        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["activeLiquidation"] == 1
        assert payload["items"][0]["articleId"] == article_id
        assert payload["items"][0]["strategy"]["assignmentSource"] == "runtime"
    finally:
        repricer_bff_module.LIQUIDATION_ACTIVE.clear()
        repricer_bff_module.LIQUIDATION_ACTIVE.update(previous_liquidation)


def test_repricer_bff_sku_settings_partial_update_preserves_existing_fields(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    previous = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    try:
        api = client()

        updated = api.put(
            "/api/v1/wb-repricer/sku/FBBT_42/settings",
            json={
                "promoBoostEnabled": True,
                "pMinKopecks": 111100,
                "pMaxKopecks": 222200,
                "priceStepPct": 5.5,
                "priceStepMinutes": 15,
                "priceStepHours": 3,
                "rrpKopecks": 250000,
                "nightMedianEnabled": True,
            },
            headers=auth_headers(api, "price_sender"),
        )

        assert updated.status_code == 200
        body = updated.json()
        assert body["settings"]["promoBoostEnabled"] is True
        assert body["settings"]["pMinKopecks"] == 111100
        assert body["settings"]["pMaxKopecks"] == 222200
        assert body["settings"]["priceStepPct"] == 5.5
        assert body["settings"]["priceStepMinutes"] == 15
        assert body["settings"]["priceStepHours"] == 3
        assert body["settings"]["rrpKopecks"] == 250000
        assert body["settings"]["nightMedianEnabled"] is True
        assert body["settings"]["cogsKopecks"] > 0
        assert body["settings"]["wbCommissionPct"] > 0
        assert body["settings"]["automationEnabled"] is True
    finally:
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous)


def test_repricer_nomenclature_import_falls_back_to_nm_id_when_article_changed(monkeypatch):
    economics_calls: list[dict[str, object]] = []
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr("app.routers.wb_repricer_bff._flush_org_repricer_state", lambda _organization_id: True)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.EconomicsService.reconcile_legacy_sku_override",
        lambda _self, _article_id, _settings, **kwargs: economics_calls.append(
            {"articleId": _article_id, "settings": _settings, **kwargs}
        ),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.save_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._list_repricer_skus_for_request",
        lambda *_args, **_kwargs: [
            {
                "meta": {"articleId": "004_ФБ_С1", "nmId": 507764443},
                "settings": {
                    "pMinKopecks": 99_000,
                    "pMaxKopecks": 110_000,
                    "taxPct": 6,
                    "otherExpensePricePct": 5,
                    "otherExpensePerSaleKopecks": 0,
                },
            }
        ],
    )
    previous_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    try:
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        api = client()

        response = api.post(
            "/api/v1/wb-repricer/sku/import-xlsx",
            files={
                "file": (
                    "nomenclature.xlsx",
                    _xlsx_bytes(
                        [
                            ["Артикул МП (NmId)", "Арт. поставщика (SupplierArticle)", "Мин. марж., ₽ (MinMarginAmount)", "Мин. цена, ₽ (MinPrice)", "Базовая цена (МаксРЦ), ₽ (MaxPrice)", "Затраты от цены с СПП, % (TaxRate)"],
                            ["507764443", "004_та_я1", "100", "600", "1000", "7.5"],
                        ]
                    ),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            headers=auth_headers(api, "price_sender"),
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["appliedCount"] == 1
        assert payload["unmatchedCount"] == 0
        assert payload["applied"][0]["articleId"] == "004_ФБ_С1"
        assert payload["applied"][0]["originalArticleId"] == "004_та_я1"
        assert repricer_bff_module.SKU_SETTINGS_OVERRIDES["004_ФБ_С1"]["minMarginKopecks"] == 10_000
        assert repricer_bff_module.SKU_SETTINGS_OVERRIDES["004_ФБ_С1"]["pMinKopecks"] == 60_000
        assert economics_calls[0]["articleId"] == "004_ФБ_С1"
        assert economics_calls[0]["settings"]["taxPct"] == 7.5
    finally:
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_settings)


def test_next_strategy_cycle_uses_sku_step_minutes_before_global():
    previous_interval = repricer_bff_module.ALGORITHM_SETTINGS_STATE.get("syncIntervalMinutes")
    try:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE["syncIntervalMinutes"] = 60
        row = {
            "meta": {"lastSavedAt": "2026-06-30T09:00:00+00:00"},
            "settings": {"priceStepMinutes": 15, "priceStepHours": 3},
        }

        next_at = repricer_bff_module._next_strategy_cycle_at(row, [])

        assert next_at == "2026-06-30T09:15:00+00:00"
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE["syncIntervalMinutes"] = previous_interval


def test_repricer_bff_sku_manager_assignment_persists_team_user(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    previous_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
    previous_events = {sku: list(events) for sku, events in repricer_bff_module.SKU_AUDIT_EVENTS.items()}
    try:
        api = client()
        headers = auth_headers(api, "admin")

        team = api.get("/api/v1/cabinet/team/users", headers=headers)
        assert team.status_code == 200
        assignee = next(user for user in team.json()["data"] if user["permissionProfile"] == "price_sender")

        updated = api.patch(
            "/api/v1/wb-repricer/sku/FBBT_42/manager",
            json={"managerUserId": assignee["userId"], "reason": "owner test assignment"},
            headers=headers,
        )

        assert updated.status_code == 200
        body = updated.json()
        assert body["meta"]["managerId"] == assignee["userId"]
        assert body["meta"]["managerName"] == assignee["fullName"]
        assert body["meta"]["assignmentSource"] == "manual"
        assert any(event["action"] == "Смена ответственного" for event in body["auditEvents"])

        reloaded = api.get("/api/v1/wb-repricer/sku/FBBT_42/settings", headers=headers)
        assert reloaded.status_code == 200
        assert reloaded.json()["meta"]["managerId"] == assignee["userId"]
    finally:
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_META_OVERRIDES.update(previous_meta)
        repricer_bff_module.SKU_AUDIT_EVENTS.clear()
        repricer_bff_module.SKU_AUDIT_EVENTS.update(previous_events)


def test_repricer_execution_uses_manual_pmin_override():
    settings = {
        "cogsKopecks": 45_000,
        "wbCommissionPct": 25,
        "logisticsKopecks": 5_000,
        "minMarginPct": 15,
        "pMinKopecks": 111_100,
    }

    assert repricer_execution_module._settings_pmin_kopecks(settings) == 111_100


def test_period_stats_aggregates_include_supplier_orders_spp(monkeypatch):
    fake = FakeWbApiClient(
        fixtures={
            "/api/v1/supplier/orders": [
                {
                    "nmId": 123456,
                    "srid": "order-123456-1",
                    "finishedPrice": 1145,
                    "spp": 26,
                    "lastChangeDate": "2026-05-30T08:30:00+03:00",
                    "date": "2026-05-28T08:25:00+03:00",
                },
                {
                    "nmId": 123456,
                    "srid": "order-123456-cancelled",
                    "finishedPrice": 1145,
                    "spp": 26,
                    "isCancel": True,
                    "cancelDate": "2026-05-28T09:25:00+03:00",
                    "lastChangeDate": "2026-05-28T09:30:00+03:00",
                    "date": "2026-05-28T08:25:00+03:00",
                },
                {
                    "nmId": 123456,
                    "srid": "order-123456-1",
                    "finishedPrice": 1145,
                    "spp": 26,
                    "lastChangeDate": "2026-05-30T08:31:00+03:00",
                    "date": "2026-05-28T08:25:00+03:00",
                },
                {
                    "nmId": 123457,
                    "srid": "order-123457-1",
                    "finishedPrice": 980,
                    "spp": 24,
                    "lastChangeDate": "2026-05-28T08:40:00+03:00",
                    "date": "2026-05-28T08:39:00+03:00",
                },
                {
                    "nmId": 123458,
                    "gNumber": "client-cart-1",
                    "barcode": "barcode-size-s",
                    "techSize": "S",
                    "finishedPrice": 990,
                    "date": "2026-05-28T11:00:00+03:00",
                },
                {
                    "nmId": 123458,
                    "gNumber": "client-cart-1",
                    "barcode": "barcode-size-m",
                    "techSize": "M",
                    "finishedPrice": 990,
                    "date": "2026-05-28T11:01:00+03:00",
                },
                {
                    "nmId": 999999,
                    "finishedPrice": 111,
                    "spp": 1,
                    "lastChangeDate": "2026-05-28T08:40:00+03:00",
                    "date": "2026-05-27T08:39:00+03:00",
                },
            ],
            "/api/v1/supplier/sales": [
                {
                    "nmId": 123456,
                    "finishedPrice": 1145,
                    "forPay": 980,
                    "lastChangeDate": "2026-05-30T10:00:00+03:00",
                    "date": "2026-05-28T09:55:00+03:00",
                    "isReturn": False,
                },
                {
                    "nmId": 123457,
                    "finishedPrice": 980,
                    "forPay": 820,
                    "lastChangeDate": "2026-05-28T10:10:00+03:00",
                    "date": "2026-05-28T10:00:00+03:00",
                    "isReturn": False,
                },
                {
                    "nmId": 123457,
                    "finishedPrice": 980,
                    "forPay": -820,
                    "lastChangeDate": "2026-05-29T10:10:00+03:00",
                    "date": "2026-05-29T10:00:00+03:00",
                    "isReturn": True,
                },
            ],
        }
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_statistics_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_period_stats_aggregates(
        "complete",
        date_from=datetime(2026, 5, 28, tzinfo=timezone.utc),
        date_to=datetime(2026, 5, 28, tzinfo=timezone.utc),
    )
    aggregates = payload["aggregates"]

    row = aggregates["123456"]
    assert row["ordersUnits"] == 1
    assert row["cancelledOrdersUnits"] == 1
    assert row["unitKeyedOrdersCount"] == 2
    assert row["cancelledOrderUnitCount"] == 1
    assert row["salesUnits"] == 1
    assert row["returnsUnits"] == 0
    assert row["sppPct"] == 26
    assert row["sppSource"] == "supplier.orders.spp"
    assert row["avgPriceWithSppKopecks"] is not None

    assert aggregates["123457"]["ordersUnits"] == 1
    assert aggregates["123457"]["salesUnits"] == 1
    assert aggregates["123457"]["returnsUnits"] == 0
    assert aggregates["123458"]["ordersUnits"] == 2
    assert aggregates["123458"]["unitKeyedOrdersCount"] == 2
    assert "999999" not in aggregates
    assert payload["dailyAggregates"]["2026-05-28"]["123456"]["ordersUnits"] == 1


def test_build_sku_row_keeps_historical_spp_separate_from_missing_live_price():
    row = repricer_bff_module._build_sku_row(
        "TEST_ORDERS_SPP",
        nm_id=123456,
        name="Test",
        subject="Товары",
        brand="Brand",
        chrt_ids=[],
        current_price_kopecks=139_000,
        discounted_price_kopecks=122_300,
        buyer_price_kopecks=None,
        promotions=[],
        use_demo_data=False,
        period_aggregate={"ordersUnits": 1, "sppPct": 26, "sppSource": "supplier.orders.spp"},
    )

    assert row["analytics"]["periodSppPct"] == 26
    assert row["analytics"]["sppPct"] is None
    assert row["analytics"]["sppSource"] is None
    assert row["analytics"]["buyerPriceNoWalletKopecks"] is None
    assert row["analytics"]["sppAccountingMode"] == "spp_only"
    assert row["analytics"]["accountedBuyerPriceKopecks"] is None
    assert row["analytics"]["marginBaseKopecks"] == 90_502


@pytest.mark.parametrize("overrides", [{}, {"currentPriceKopecks": 77_000, "localPriceOverrideSource": "wb_api"},
                                      {"currentPriceKopecks": 77_000, "localPriceOverrideSource": "wb_api_pending"}])
def test_sku_without_local_price_does_not_rebuild_application_settings(monkeypatch, overrides):
    monkeypatch.setitem(repricer_bff_module.SKU_META_OVERRIDES, "SERVER_PRICE", overrides)
    monkeypatch.setattr(repricer_bff_module, "get_settings", lambda: pytest.fail("Unneeded application settings read"))
    row = repricer_bff_module._build_sku_row(
        "SERVER_PRICE", nm_id=123456, name="Test", subject="Товары", brand=None, chrt_ids=[],
        current_price_kopecks=139_000, discounted_price_kopecks=122_300, buyer_price_kopecks=None,
        promotions=[], use_demo_data=False,
    )
    assert row["meta"]["currentPriceKopecks"] == 122_300


def test_build_sku_row_prefers_live_buyer_price_over_period_spp():
    row = repricer_bff_module._build_sku_row(
        "TEST_LIVE_SPP",
        nm_id=123456,
        name="Test",
        subject="Товары",
        brand="Brand",
        chrt_ids=[],
        current_price_kopecks=139_000,
        discounted_price_kopecks=122_300,
        buyer_price_kopecks=100_000,
        promotions=[],
        use_demo_data=False,
        period_aggregate={"ordersUnits": 1, "sppPct": 26, "sppSource": "supplier.orders.spp"},
    )

    assert row["analytics"]["sppPct"] == 18.23
    assert row["analytics"]["sppSource"] == "live_buyer_price"
    assert row["analytics"]["periodSppPct"] == 26
    assert row["analytics"]["buyerPriceNoWalletKopecks"] == 100_000


def test_build_sku_row_prefers_sales_funnel_orders_when_available():
    row = repricer_bff_module._build_sku_row(
        "TEST_ORDERS_SANITY",
        nm_id=123456,
        name="Test",
        subject="Товары",
        brand="Brand",
        chrt_ids=[],
        current_price_kopecks=139_000,
        discounted_price_kopecks=122_300,
        buyer_price_kopecks=100_000,
        promotions=[],
        use_demo_data=False,
        period_aggregate={"ordersUnits": 100, "buyoutPct": 10, "sppPct": 26, "sppSource": "supplier.orders.spp"},
        finance_aggregate={"salesUnits": 10, "returnsUnits": 1, "sellerRevenueKopecks": 1_223_000},
        baskets_aggregate={"orderCount": 1763, "buyoutPct": 84.6, "buyoutSource": "sales_funnel.conversions.buyoutPercent"},
        baskets_cache_loaded=True,
    )

    assert row["analytics"]["ordersUnits"] == 1763
    assert row["analytics"]["ordersSource"] == "sales_funnel.orderCount"
    assert row["analytics"]["funnelOrderCount"] == 1763
    assert row["analytics"]["buyoutPct"] == 84.6


def test_build_sku_row_can_account_spp_plus_configured_wallet():
    previous = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    repricer_bff_module.ALGORITHM_SETTINGS_STATE.update({"sppAccountingMode": "spp_plus_wallet", "wbWalletType": 4})
    try:
        row = repricer_bff_module._build_sku_row(
            "TEST_ORDERS_SPP_WALLET",
            nm_id=123456,
            name="Test",
            subject="Товары",
            brand="Brand",
            chrt_ids=[],
            current_price_kopecks=139_000,
            discounted_price_kopecks=122_300,
            buyer_price_kopecks=None,
            promotions=[],
            use_demo_data=False,
            period_aggregate={"ordersUnits": 1, "sppPct": 26, "sppSource": "supplier.orders.spp"},
        )
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous)

    assert row["analytics"]["sppAccountingMode"] == "spp_plus_wallet"
    assert row["analytics"]["accountedWbWalletPct"] == 4.0
    assert row["analytics"]["accountedPlatformDiscountPct"] is None
    assert row["analytics"]["buyerPriceNoWalletKopecks"] is None
    assert row["analytics"]["accountedBuyerPriceKopecks"] is None
    assert row["analytics"]["marginBaseKopecks"] == 86_800


def test_build_sku_row_preserves_tariff_but_does_not_plan_margin_without_buyout():
    previous_settings = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    previous_overrides = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    repricer_bff_module.ALGORITHM_SETTINGS_STATE.update({"sppAccountingMode": "spp_plus_wallet", "wbWalletType": 4})
    repricer_bff_module.SKU_SETTINGS_OVERRIDES["LBBT_3068"] = {
        "cogsKopecks": 44_000,
        "logisticsKopecks": 4_100,
    }
    try:
        row = repricer_bff_module._build_sku_row(
            "LBBT_3068",
            nm_id=486612890,
            name="Test longsleeve",
            subject="Лонгсливы",
            brand="Anomie Studio",
            chrt_ids=[],
            current_price_kopecks=484_200,
            discounted_price_kopecks=174_300,
            buyer_price_kopecks=None,
            promotions=[],
            use_demo_data=False,
            period_aggregate={"ordersUnits": 1, "sppPct": 16.69, "sppSource": "supplier.orders.spp"},
            finance_aggregate={"salesUnits": 1, "commissionPct": 7},
            commission_tariffs_index={
                "name:лонгсливы": {
                    "baseCommissionPct": 38.49,
                    "sourceField": "tariffs.commission.kgvpMarketplace",
                }
            },
        )
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_settings)
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_overrides)

    assert row["settings"]["wbCommissionPct"] == 41.8
    assert row["analytics"]["commissionDisplayPct"] == 41.8
    assert row["analytics"]["baseWbCommissionPct"] == 38.49
    assert row["analytics"]["reportCommissionPct"] == 7
    assert row["analytics"]["commissionSource"] == "tariffs.commission.kgvpMarketplace"
    assert row["analytics"]["commissionState"] == "ok"
    assert row["analytics"]["accountedBuyerPriceKopecks"] is None
    assert row["analytics"]["marginMode"] == "planned_indeepa"
    assert row["analytics"]["plannedMarginState"] == "missing_inputs"
    assert row["analytics"]["marginKopecks"] is None
    assert row["analytics"]["plannedMarginKopecks"] is None
    assert row["analytics"]["plannedPeriodMarginKopecks"] is None
    assert row["analytics"]["marginPct"] is None


def test_cyrillic_longsleeve_article_uses_longsleeve_defaults_and_official_spp():
    previous_settings = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    repricer_bff_module.ALGORITHM_SETTINGS_STATE.update({"sppAccountingMode": "spp_plus_wallet", "wbWalletType": 4})
    try:
        row = repricer_bff_module._build_sku_row(
            "Лбbt_3068",
            nm_id=486612890,
            name="Test longsleeve",
            subject="Товары",
            brand="Anomie Studio",
            chrt_ids=[],
            current_price_kopecks=484_200,
            discounted_price_kopecks=174_300,
            buyer_price_kopecks=None,
            promotions=[],
            use_demo_data=False,
            period_aggregate={"ordersUnits": 1, "sppPct": 16.69, "sppSource": "supplier.orders.spp"},
            finance_aggregate={"salesUnits": 1, "commissionPct": 7},
        )
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_settings)

    assert row["settings"]["cogsKopecks"] == 44_000
    assert row["settings"]["logisticsKopecks"] == 4_100
    assert row["settings"]["wbCommissionPct"] == 10.31
    assert row["analytics"]["sppPct"] is None
    assert row["analytics"]["periodSppPct"] == 16.69
    assert row["analytics"]["buyerPriceNoWalletKopecks"] is None
    assert row["analytics"]["accountedBuyerPriceKopecks"] is None
    assert row["analytics"]["baseWbCommissionPct"] == 7.0
    assert row["analytics"]["commissionSource"] == "finance.commissionPct"
    assert row["analytics"]["commissionDisplayPct"] == 10.3
    assert row["analytics"]["commissionState"] == "fallback"
    assert row["analytics"]["reportCommissionPct"] == 7.0
    assert row["analytics"]["plannedMarginState"] == "missing_inputs"
    assert row["analytics"]["marginKopecks"] is None
    assert row["analytics"]["plannedPeriodMarginKopecks"] is None
    assert row["analytics"]["marginPct"] is None


def test_missing_tariff_commission_uses_finance_report_percent_as_fallback():
    row = repricer_bff_module._build_sku_row(
        "FBBT_42",
        nm_id=123456,
        name="Test tee",
        subject="Футболки",
        brand="Anomie Studio",
        chrt_ids=[],
        current_price_kopecks=100_000,
        discounted_price_kopecks=100_000,
        buyer_price_kopecks=100_000,
        promotions=[],
        use_demo_data=False,
        finance_aggregate={"salesUnits": 1, "commissionPct": 12, "sellerRevenueKopecks": 100_000},
        commission_tariffs_index={},
    )

    assert row["analytics"]["baseWbCommissionPct"] == 12
    assert row["analytics"]["commissionDisplayPct"] == 15.3
    assert row["analytics"]["commissionSource"] == "finance.commissionPct"
    assert row["analytics"]["commissionState"] == "fallback"
    assert row["settings"]["wbCommissionPct"] == 15.31


def test_repricer_bff_frontend_strategy_catalog_and_sku_shape(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_user_wb_token_secret", lambda _user_id: "user-token")
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", organization_id=1),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda _organization_id: [{"vendorCode": "FBBT_42", "nmID": 123456, "sizes": [{"price": 129000, "discountedPrice": 119900}]}],
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.cached_goods_meta",
        lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
    )
    repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
    repricer_bff_module.SKU_META_OVERRIDES.clear()
    repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
    repricer_bff_module.SKU_AUDIT_EVENTS.clear()

    api = client()

    listing = api.get("/api/v1/wb-repricer/sku")
    assert listing.status_code == 200
    row = listing.json()["items"][0]
    assert row["strategy"]["id"] in {
        "stockout_guard",
        "turnover_control",
        "plan_fact_daily",
        "plan_fact_period",
        "plan_fact_group",
        "plan_fact_interval",
        "cross_marketplace",
        "illiquid",
        "optimal_price",
        "baskets_orders",
        "metric_dynamics",
        "schedule",
        "bundles",
    }
    assert row["meta"]["activeStrategyId"] == row["strategy"]["id"]

    catalog = api.get("/api/v1/wb-repricer/strategies/catalog")
    assert catalog.status_code == 200
    payload = catalog.json()
    assert payload["total"] == 14
    assert any(item["id"] == "night_price_mode" for item in payload["items"])
    assert any(item["name"] == "Неликвид" for item in payload["items"])
    assert any(item["name"] == "Контроль оборачиваемости" for item in payload["items"])


def test_repricer_sku_does_not_infer_category_from_article_when_category_missing(monkeypatch):
    monkeypatch.setattr(repricer_bff_module, "fetch_commission_tariffs", lambda *_args, **_kwargs: {})
    rows = repricer_bff_module.list_repricer_skus(
        wb_token="env-token",
        include_promotions=False,
        cached_goods=[
            {
                "vendorCode": "ФБbt_1133",
                "nmID": 453200669,
                "brand": "Anomie Studio",
                "sizes": [{"price": 2809, "discountedPrice": 1376.41}],
            }
        ],
        cached_content_cards=[
            {
                "vendorCode": "ФБbt_1133",
                "nmID": 453200669,
                "title": "Футболка с принтом",
                "object": None,
                "brand": "Anomie Studio",
                "sizes": [{"skus": []}],
            }
        ],
        cached_promotions=[],
        cached_stock_aggregates={},
        cached_period_stats={},
        cached_finance_aggregates={},
        cached_ads_aggregates={},
        cached_baskets_aggregates={},
        stocks_cache_loaded=True,
        baskets_cache_loaded=True,
        sort_by_demand=False,
    )

    row = rows[0]
    assert row["meta"]["subject"] == "Товары"
    assert row["analytics"]["baseWbCommissionPct"] == 0.0
    assert row["analytics"]["commissionDisplayPct"] is None
    assert row["analytics"]["commissionSource"] == "tariffs.commission.missing"
    assert row["analytics"]["commissionState"] == "no_data"


def test_repricer_bff_bulk_strategy_assignment_updates_sku_row(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_user_wb_token_secret", lambda _user_id: "user-token")
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", organization_id=1),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda _organization_id: [{"vendorCode": "FBBT_42", "nmID": 123456, "sizes": [{"price": 129000, "discountedPrice": 119900}]}],
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.cached_goods_meta",
        lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
    )
    repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
    repricer_bff_module.SKU_META_OVERRIDES.clear()
    repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
    repricer_bff_module.SKU_AUDIT_EVENTS.clear()

    api = client()

    applied = api.post(
        "/api/v1/wb-repricer/strategy-assignments/bulk",
        json={"articleIds": ["FBBT_42"], "strategyName": "Ночная медиана"},
    )
    assert applied.status_code == 200
    body = applied.json()
    assert body["strategy"]["id"] == "night_price_mode"
    assert body["assignedCount"] == 1
    assert body["items"][0]["strategyId"] == "night_price_mode"
    assert body["items"][0]["assignmentSource"] == "manual"

    listing = api.get("/api/v1/wb-repricer/sku")
    assert listing.status_code == 200
    row = listing.json()["items"][0]
    assert row["strategy"]["id"] == "night_price_mode"
    assert row["meta"]["activeStrategyName"] == "Ночная медиана"
    assert row["meta"]["status"] == "auto"


def test_repricer_bff_bulk_strategy_assignment_can_unassign(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_user_wb_token_secret", lambda _user_id: "user-token")
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", organization_id=1),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda _organization_id: [{"vendorCode": "FBBT_42", "nmID": 123456, "sizes": [{"price": 129000, "discountedPrice": 119900}]}],
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.cached_goods_meta",
        lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
    )
    repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
    repricer_bff_module.SKU_META_OVERRIDES.clear()
    repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
    repricer_bff_module.SKU_AUDIT_EVENTS.clear()

    api = client()

    assigned = api.post(
        "/api/v1/wb-repricer/strategy-assignments/bulk",
        json={"articleIds": ["FBBT_42"], "strategyId": "stockout_guard", "executeAfterAssign": False},
    )
    assert assigned.status_code == 200

    unassigned = api.post(
        "/api/v1/wb-repricer/strategy-assignments/bulk",
        json={"articleIds": ["FBBT_42"], "strategyId": "none", "executeAfterAssign": True},
    )
    assert unassigned.status_code == 200
    body = unassigned.json()
    assert body["strategy"]["id"] == "none"
    assert body["unassignedCount"] == 1
    assert body["items"][0]["status"] == "manual"

    listing = api.get("/api/v1/wb-repricer/sku")
    assert listing.status_code == 200
    row = listing.json()["items"][0]
    assert row["strategy"]["assignmentSource"] == "derived"
    assert row["meta"]["assignmentSource"] == "none"
    assert row["meta"]["status"] == "manual"
    assert row["settings"]["automationEnabled"] is False


def test_repricer_bff_bulk_strategy_assignment_uses_full_sku_index(monkeypatch):
    article_id = "SKU_151"
    row = {
        "meta": {
            "articleId": article_id,
            "currentPriceKopecks": 119900,
            "status": "manual",
        },
        "settings": {"automationEnabled": True},
        "strategy": {"name": "не задана"},
    }
    list_calls: list[Any] = []

    def fake_list_repricer_skus_for_request(_request, _scenario, **kwargs):
        list_calls.append(kwargs.get("max_items", "missing"))
        if "max_items" in kwargs and kwargs["max_items"] is None:
            return [row]
        return []

    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr("app.routers.wb_repricer_bff._flush_org_repricer_state", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: "user-token")
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._list_repricer_skus_for_request",
        fake_list_repricer_skus_for_request,
    )
    repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
    repricer_bff_module.SKU_META_OVERRIDES.clear()
    repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
    repricer_bff_module.SKU_AUDIT_EVENTS.clear()

    api = client()
    applied = api.post(
        "/api/v1/wb-repricer/strategy-assignments/bulk",
        json={"articleIds": [article_id], "strategyId": "stockout_guard", "executeAfterAssign": False},
    )

    assert applied.status_code == 200
    assert applied.json()["assignedCount"] == 1
    assert list_calls == [None]


def test_sku_list_page_limits_backend_row_build_for_unfiltered_first_page(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(wb_repricer_bff_router, "_load_repricer_sku_snapshot_page", lambda *a, **kw: None)
    monkeypatch.setattr(wb_repricer_bff_router, "_build_repricer_sku_snapshot", lambda *a, **kw: {})
    monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda org: {"totalCached": 1000, "pagesCached": 1})

    def fake_list_repricer_skus_for_request(_request, _scenario, **kwargs):
        captured["max_items"] = kwargs.get("max_items", "missing")
        limit = int(kwargs["max_items"] or 1000)
        return [
            {
                "meta": {"articleId": f"SKU_{index}", "brand": "wb", "status": "manual"},
                "analytics": {},
            }
            for index in range(limit)
        ]

    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(wb_repricer_bff_router, "_list_repricer_skus_for_request", fake_list_repricer_skus_for_request)
    monkeypatch.setattr(wb_repricer_bff_router, "_repricer_list_cache_meta", lambda *_args, **_kwargs: {"totalCached": 1000})

    def fake_period_source_cache(_organization_id, source, *_args, **_kwargs):
        if source == "finance":
            return {
                "aggregates": {
                    "101": {
                        "salesUnits": 2,
                        "revenueKopecks": 200_000,
                        "sellerRevenueKopecks": 200_000,
                        "commissionKopecks": 20_000,
                        "logisticsKopecks": 5_000,
                        "storageKopecks": 1_000,
                        "expensesKopecks": 26_000,
                        "netProfitKopecks": 74_000,
                        "cogsTotalKopecks": 100_000,
                    },
                    "102": {
                        "salesUnits": 1,
                        "revenueKopecks": 100_000,
                        "sellerRevenueKopecks": 100_000,
                        "commissionKopecks": 10_000,
                        "logisticsKopecks": 3_000,
                        "expensesKopecks": 13_000,
                        "netProfitKopecks": 37_000,
                        "cogsTotalKopecks": 50_000,
                    },
                }
            }
        if source == "period_stats":
            return {"aggregates": {"101": {"ordersUnits": 3}, "102": {"ordersUnits": 1}}}
        return {}

    monkeypatch.setattr(wb_repricer_bff_router, "_period_source_cache", fake_period_source_cache)
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "list_cached_goods",
        lambda _organization_id: [
            {"vendorCode": "SKU_101", "nmID": 101, "sizes": [{"price": 100_000, "discountedPrice": 100_000}]},
            {"vendorCode": "SKU_102", "nmID": 102, "sizes": [{"price": 100_000, "discountedPrice": 100_000}]},
        ],
    )
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache", lambda *_args, **_kwargs: {})

    payload = wb_repricer_bff_router.get_sku_list(
        SimpleNamespace(query_params={}),
        scenario="complete",
        include_promotions=False,
        include_content=False,
        period_days=30,
        date_from=None,
        date_to=None,
        page=1,
        page_size=25,
        q="",
        status="all",
        brand="all",
        manager="all",
        top_mode=True,
    )

    assert captured["max_items"] == 25
    assert payload["itemsReturned"] == 25
    assert payload["total"] == 1000
    assert payload["summary"]["skuCount"] == 2
    assert payload["summary"]["revenueKopecks"] == 300_000
    assert payload["summary"]["expensesKopecks"] == 39_000
    # An old aggregate profit is not authoritative without dated cost/tax.
    assert payload["summary"]["marginKopecks"] is None
    assert payload["summary"]["avgMarginPct"] is None
    assert payload["summary"]["ordersUnits"] == 4
    assert payload["summary"]["salesUnits"] == 3


def test_sku_list_returns_materialized_snapshot_without_full_sync_status(monkeypatch):
    snapshot = {
        "items": [
            {
                "meta": {"articleId": "FBBT_42", "nmId": 123456, "currentPriceKopecks": 100_000},
                "settings": {},
                "analytics": {"ordersUnits": 3, "baskets": 5, "financeState": "ok"},
            }
        ],
        "summary": {"ordersUnits": 3, "totalBaskets": 5},
        "cache": {
            "totalCached": 1,
            "periodDays": 22,
            "dateFrom": "2026-07-02",
            "dateTo": "2026-07-23",
            "financeMatchedNmIds": 1,
            "basketsMatchedNmIds": 1,
        },
    }

    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda _organization_id: {"totalCached": 1})
    monkeypatch.setattr(wb_repricer_bff_router, "get_wb_sync_status", lambda _organization_id: {})
    monkeypatch.setattr(wb_repricer_bff_router, "list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wb_repricer_bff_router, "_load_repricer_sku_snapshot", lambda *_args, **_kwargs: snapshot)

    payload = wb_repricer_bff_router.get_sku_list(
        SimpleNamespace(query_params={}),
        scenario="complete",
        include_promotions=False,
        include_content=False,
        period_days=22,
        date_from=date(2026, 7, 2),
        date_to=date(2026, 7, 23),
        page=1,
        page_size=150,
        q="",
        status="all",
        brand="all",
        manager="all",
        top_mode=True,
    )

    assert payload["total"] == 1
    assert payload["items"][0]["meta"]["articleId"] == "FBBT_42"
    assert payload["summary"]["ordersUnits"] == 3
    assert payload["cache"]["financeMatchedNmIds"] == 1


def test_sku_list_reuses_snapshot_after_confirming_catalog_exists(monkeypatch):
    snapshot = {
        "items": [
            {
                "meta": {"articleId": "FAST_1", "nmId": 9001, "currentPriceKopecks": 100_000},
                "settings": {},
                "analytics": {"ordersUnits": 2, "baskets": 4, "financeState": "ok"},
            }
        ],
        "summary": {"ordersUnits": 2, "totalBaskets": 4},
        "cache": {
            "totalCached": 5000,
            "periodDays": 7,
            "dateFrom": "2026-07-22",
            "dateTo": "2026-07-28",
            "financeMatchedNmIds": 4000,
            "basketsMatchedNmIds": 3900,
        },
    }

    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "cached_goods_meta",
        lambda _organization_id: {"pagesCached": 5, "totalCached": 5000},
    )
    monkeypatch.setattr(wb_repricer_bff_router, "list_cached_goods", lambda org: pytest.fail("warm snapshot must not rebuild all goods"))
    monkeypatch.setattr(wb_repricer_bff_router, "_load_repricer_sku_snapshot", lambda *_args, **_kwargs: snapshot)

    payload = wb_repricer_bff_router.get_sku_list(
        SimpleNamespace(query_params={}),
        scenario="complete",
        include_promotions=False,
        include_content=False,
        period_days=7,
        date_from=date(2026, 7, 22),
        date_to=date(2026, 7, 28),
        page=1,
        page_size=150,
        q="",
        status="all",
        brand="all",
        manager="all",
        top_mode=True,
    )

    assert payload["itemsReturned"] == 1
    assert payload["total"] == 5000
    assert payload["items"][0]["meta"]["articleId"] == "FAST_1"


def test_repricer_stats_endpoint_returns_backend_diagnostics(monkeypatch):
    monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda org: {"pagesCached": 1, "totalCached": 2})
    rows = [
        {
            "meta": {
                "articleId": "FBBT_42",
                "nmId": 101,
                "name": "Oversize tee",
                "brand": "Anomie Studio",
                "status": "auto",
                "managerId": "manager-irina",
                "managerName": "Ирина",
                "currentPriceKopecks": 129_000,
                "basketsLast7d": 20,
                "basketNorm": 15,
            },
            "settings": {"automationEnabled": True, "pMinKopecks": 90_000},
            "strategy": {"id": "baskets_orders", "name": "Корзины и заказы", "assignmentSource": "manual"},
            "analytics": {
                "adImpressions": 1000,
                "adClicks": 100,
                "adCartAdds": 40,
                "adOrders": 12,
                "adSpendKopecks": 25_000,
                "adRevenueKopecks": 280_000,
                "baskets": 50,
                "ordersUnits": 20,
                "revenueKopecks": 500_000,
                "netProfitKopecks": 120_000,
                "marginPct": 24.0,
                "wbStockUnits": 80,
                "avgPriceWithSppKopecks": 119_000,
                "sppPct": 7.8,
                "buyoutPct": 70.0,
                "sppState": "ok",
                "commissionState": "ok",
                "financeState": "ok",
                "stockState": "ok",
                "basketsState": "ok",
                "periodStatsState": "ok",
                "promotionStatus": "yes",
                "promotionStatusText": "Летняя акция",
            },
        },
        {
            "meta": {
                "articleId": "NOFIN_1",
                "nmId": 102,
                "name": "No finance sku",
                "brand": "Anomie Studio",
                "status": "auto",
                "currentPriceKopecks": 99_000,
                "basketsLast7d": 0,
                "basketNorm": 10,
            },
            "settings": {"automationEnabled": True, "pMinKopecks": 80_000},
            "strategy": {"id": "turnover_control", "name": "Оборот", "assignmentSource": "manual"},
            "analytics": {
                "adImpressions": 0,
                "adClicks": 0,
                "baskets": None,
                "ordersUnits": 0,
                "revenueKopecks": 0,
                "wbStockUnits": None,
                "avgPriceWithSppKopecks": 99_000,
                "sppState": "no_buyer_price",
                "commissionState": "no_data",
                "financeState": "no_data",
                "stockState": "no_data",
                "basketsState": "no_data",
                "periodStatsState": "no_data",
            },
        },
    ]

    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(wb_repricer_bff_router, "get_wb_sync_status", lambda _organization_id: {})
    monkeypatch.setattr(
        wb_repricer_bff_router, "_load_repricer_sku_snapshot_page",
        lambda *_args, **kwargs: None if kwargs.get("view") == "stats" else ({"cache": {"totalCached": 2}}, rows),
    )
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "_repricer_list_cache_meta",
        lambda *_args, **_kwargs: {"totalCached": 2, "periodStatsFetchedAt": "2026-07-21T01:00:00Z"},
    )

    payload = wb_repricer_bff_router.get_repricer_stats(
        SimpleNamespace(query_params={}),
        scenario="complete",
        period_days=7,
        date_from=None,
        date_to=None,
        page=1,
        page_size=25,
        q="",
        status="all",
        brand="all",
        manager="all",
        top_mode=True,
    )
    assert payload["total"] == 2
    assert payload["summary"]["skuCount"] == 2
    assert payload["summary"]["canRecalculate"] == 1
    assert payload["summary"]["priceBlocked"] == 1
    assert payload["summary"]["sourceReady"] == 1
    assert payload["summary"]["sourceBlocked"] == 1
    assert payload["summary"]["adCtrPct"] == 10.0
    assert payload["summary"]["cartToOrderCrPct"] is None
    assert payload["summary"]["metricTotals"]["cartToOrderCrPct"] == {"value": 40.0, "coveredSku": 1, "partialSku": 0}

    first = payload["items"][0]
    assert first["articleId"] == "FBBT_42"
    assert first["decision"]["id"] == "can_recalculate"
    assert first["priceProtection"]["status"] == "can_recalculate"
    assert first["sources"]["status"] == "ready"
    assert first["metrics"]["impressions"] == 1000
    assert first["metrics"]["clicks"] == 100
    assert first["metrics"]["ctrPct"] == 10.0
    assert first["metrics"]["baskets"] == 50
    assert first["metrics"]["orders"] == 20
    assert first["metrics"]["cartToOrderCrPct"] == 40.0
    assert first["metrics"]["drrPct"] == 5.0
    assert "promo" in first["flags"]
    assert "ads" in first["flags"]

    second = payload["items"][1]
    assert second["decision"]["id"] == "price_blocked"
    assert second["sources"]["status"] == "blocked"
    assert "finance" in second["sources"]["missing"]
    assert "commission" in second["priceProtection"]["blockerIds"]


def test_repricer_stats_summary_uses_full_filtered_slice_not_first_page(monkeypatch):
    monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda org: {"pagesCached": 1, "totalCached": 600})
    rows = [
        {
            "meta": {"articleId": f"SKU_{index}", "nmId": 10_000 + index, "currentPriceKopecks": 100_000},
            "settings": {"pMinKopecks": 80_000},
            "analytics": {
                "baskets": 1,
                "ordersUnits": 1,
                "sppState": "ok",
                "commissionState": "ok",
                "financeState": "ok",
                "stockState": "ok",
                "basketsState": "ok",
                "periodStatsState": "ok",
            },
        }
        for index in range(600)
    ]
    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(wb_repricer_bff_router, "get_wb_sync_status", lambda _organization_id: {})
    monkeypatch.setattr(
        wb_repricer_bff_router, "_load_repricer_sku_snapshot_page",
        lambda *_args, **kwargs: None if kwargs.get("view") == "stats" else ({"cache": {"totalCached": 600}}, rows),
    )
    monkeypatch.setattr(wb_repricer_bff_router, "_repricer_list_cache_meta", lambda *_args, **_kwargs: {"totalCached": 600})

    payload = wb_repricer_bff_router.get_repricer_stats(
        SimpleNamespace(query_params={}),
        scenario="complete",
        period_days=30,
        date_from=None,
        date_to=None,
        page=1,
        page_size=500,
        q="",
        status="all",
        brand="all",
        manager="all",
        top_mode=True,
    )

    assert payload["total"] == 600
    assert payload["itemsReturned"] == 500
    assert payload["summary"]["skuCount"] == 600
    assert payload["summary"]["baskets"] == 600


def test_repricer_stats_keeps_requested_range_when_cache_is_missing(monkeypatch):
    # A confirmed empty catalog is distinct from missing period metrics.
    monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda org: {"pagesCached": 1, "totalCached": 0})
    captured: dict[str, Any] = {}

    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "get_wb_sync_status",
        lambda _organization_id: {
            "state": "completed",
            "running": False,
            "periodDays": 30,
            "periodCacheSuffix": "2026-06-01_2026-06-30",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-30",
        },
    )

    def fake_cache_meta(_organization_id, period_days, *, include_content, date_from=None, date_to=None, **_kwargs):
        captured["cache_period_days"] = period_days
        captured["cache_date_from"] = date_from
        captured["cache_date_to"] = date_to
        return {"totalCached": 0, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "periodDays": period_days}

    monkeypatch.setattr(wb_repricer_bff_router, "_load_repricer_sku_snapshot_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wb_repricer_bff_router, "_ensure_repricer_stats_period_caches", lambda *_args, **_kwargs: {
        "caches": {key: {} for key in ("period_stats", "finance", "ads", "baskets")},
        "missingSources": ["period-stats", "finance", "ads", "baskets"],
        "onDemandFetchedSources": [],
    })
    monkeypatch.setattr(wb_repricer_bff_router, "list_cached_goods", lambda _organization_id: [])
    monkeypatch.setattr(wb_repricer_bff_router, "list_repricer_skus", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wb_repricer_bff_router, "_repricer_finance_taxes", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(wb_repricer_bff_router, "_repricer_list_cache_meta", fake_cache_meta)

    payload = wb_repricer_bff_router.get_repricer_stats(
        SimpleNamespace(query_params={}),
        scenario="complete",
        period_days=7,
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 7),
        page=1,
        page_size=25,
        q="",
        status="all",
        brand="all",
        manager="all",
        top_mode=True,
    )

    assert captured["cache_period_days"] == 7
    assert captured["cache_date_from"] == date(2026, 7, 1)
    assert captured["cache_date_to"] == date(2026, 7, 7)
    assert payload["dateFrom"] == "2026-07-01"
    assert payload["dateTo"] == "2026-07-07"
    assert payload["periodDays"] == 7
    assert payload["cache"]["statsRangeAdjusted"] is False
    assert payload["cache"]["requestedRange"] == {"from": "2026-07-01", "to": "2026-07-07"}
    assert payload["cache"]["statsSourceRange"] == {"from": "2026-07-01", "to": "2026-07-07"}


def test_repricer_stats_metrics_accept_ads_alias_fields():
    metrics = wb_repricer_bff_router._repricer_stats_metrics(
        {
            "meta": {"currentPriceKopecks": 100_000},
            "analytics": {
                "impressions": 1200,
                "clicks": 60,
                "cartAdds": 15,
                "adOrderCount": 6,
                "adSpendKopecks": 30_000,
                "revenueKopecks": 300_000,
            },
        }
    )

    assert metrics["impressions"] == 1200
    assert metrics["clicks"] == 60
    assert metrics["ctrPct"] == 5.0
    assert metrics["adCartAdds"] == 15
    assert metrics["adOrders"] == 6


def test_repricer_stats_metrics_keeps_missing_ads_as_nulls():
    metrics = wb_repricer_bff_router._repricer_stats_metrics(
        {
            "meta": {"currentPriceKopecks": 100_000},
            "analytics": {
                "adDataAvailable": False,
                "adImpressions": 0,
                "adClicks": 0,
                "adSpendKopecks": 0,
                "revenueKopecks": 300_000,
            },
        }
    )

    assert metrics["impressions"] is None
    assert metrics["clicks"] is None
    assert metrics["ctrPct"] is None
    assert metrics["adSpendKopecks"] is None
    assert metrics["drrPct"] is None


def test_repricer_stats_summary_tracks_coverage_and_weights_rates():
    items = [
        {"metrics": {"totalImpressions": None, "impressions": 100, "clicks": 10, "baskets": 10,
                     "orders": 5, "adSpendKopecks": 500, "revenueKopecks": 10_000,
                     "averagePriceKopecks": 2_000, "averagePriceOrderCount": 5},
         "sources": {"states": {"baskets": "ok"}}},
        {"metrics": {"totalImpressions": None, "impressions": 900, "clicks": 180, "baskets": None,
                     "orders": 9, "adSpendKopecks": 900, "revenueKopecks": 30_000,
                     "averagePriceKopecks": 3_000, "averagePriceOrderCount": 10},
         "sources": {"states": {"baskets": "partial"}}},
    ]
    totals = wb_repricer_bff_router._repricer_stats_summary(items)["metricTotals"]
    assert totals["totalImpressions"] == {"value": None, "coveredSku": 0, "partialSku": 0}
    assert totals["impressions"]["value"] == 1_000
    assert totals["ctrPct"]["value"] == 19.0
    assert totals["cartToOrderCrPct"] == {"value": 50.0, "coveredSku": 1, "partialSku": 0}
    assert totals["drrPct"]["value"] == 3.5
    assert totals["averagePriceKopecks"] == {"value": 2_667, "coveredSku": 2, "partialSku": 1}

    metrics = wb_repricer_bff_router._repricer_stats_metrics({"analytics": {
        "adDataAvailable": True, "adMetricsAvailable": False, "adSpendKopecks": 100,
        "adImpressions": 0, "adClicks": 0, "financeState": "ok", "revenueKopecks": 1_000,
        "ordersSource": None, "stockState": "no_data", "wbStockUnits": 0,
    }})
    assert metrics["impressions"] is None
    assert metrics["adSpendKopecks"] == 100
    assert metrics["orders"] is None
    assert metrics["stockUnits"] is None


def test_repricer_stats_total_metrics_and_period_price_do_not_use_ad_or_current_fallbacks():
    row = {
        "meta": {"currentPriceKopecks": 100_000},
        "analytics": {
            "adDataAvailable": True, "adImpressions": 1200, "adClicks": 60,
            "adCartAdds": 15, "adSpendKopecks": 30_000, "revenueKopecks": 300_000,
            "totalClicks": 180, "avgPriceWithSppKopecks": 85_000,
        },
    }
    metrics = wb_repricer_bff_router._repricer_stats_metrics(row)
    assert metrics["totalImpressions"] is None
    assert metrics["totalClicks"] == 180
    assert metrics["impressions"] == 1200
    assert metrics["adCartAdds"] == 15
    assert metrics["averagePriceKopecks"] == 85_000
    assert metrics["ctrPct"] == 5.0
    assert metrics["drrPct"] == 10.0
    row["analytics"].pop("avgPriceWithSppKopecks")
    assert wb_repricer_bff_router._repricer_stats_metrics(row)["averagePriceKopecks"] is None


def test_build_sku_row_without_finance_sets_ads_availability_false():
    row = repricer_bff_module._build_sku_row(
        "NO_FIN_ADS",
        nm_id=123456,
        name="No finance",
        subject="Товары",
        brand="Brand",
        chrt_ids=[],
        current_price_kopecks=129_000,
        discounted_price_kopecks=119_900,
        buyer_price_kopecks=110_000,
        promotions=[],
        use_demo_data=False,
        finance_aggregate=None,
        ads_aggregate=None,
    )

    analytics = row["analytics"]
    assert analytics["adDataAvailable"] is False
    assert analytics["adSpendKopecks"] is None
    assert analytics["adImpressions"] is None
    assert analytics["adClicks"] is None


def test_repricer_list_helper_reuses_explicit_missing_wb_token(monkeypatch):
    captured: dict[str, object] = {}
    request = SimpleNamespace(query_params={})
    actor = SimpleNamespace(organization_id=77, user_id="u-1")

    monkeypatch.setattr("app.routers.wb_repricer_bff.actor_from_request", lambda _request: (_ for _ in ()).throw(AssertionError("unexpected auth")))
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: (_ for _ in ()).throw(AssertionError("unexpected token lookup")))
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_cached_goods", lambda _organization_id: [])
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.routers.wb_repricer_bff._period_source_cache", lambda *_args, **_kwargs: {})

    def fake_list_repricer_skus(_scenario, **kwargs):
        captured["wb_token"] = kwargs.get("wb_token")
        return []

    monkeypatch.setattr("app.routers.wb_repricer_bff.list_repricer_skus", fake_list_repricer_skus)

    rows = wb_repricer_bff_router._list_repricer_skus_for_request(
        request,
        "complete",
        actor=actor,
        wb_token=None,
    )

    assert rows == []
    assert captured["wb_token"] is None


def test_repricer_catalog_goods_uses_live_filter_method(monkeypatch):
    fake = FakeWbApiClient(fixtures={"/api/v2/list/goods/filter": {"data": {"listGoods": []}}})

    monkeypatch.setattr("app.repricer_bff.build_wb_client", lambda *args, **kwargs: fake)

    assert _fetch_catalog_goods("complete", wb_token="token-value") == []
    assert fake.requests
    assert fake.requests[0].method == "POST"
    assert fake.requests[0].path == "/api/v2/list/goods/filter"


def test_finance_report_revenue_uses_seller_discounted_unit_price_net_of_returns(monkeypatch):
    monkeypatch.setattr(repricer_bff_module, "_finance_report_last_request_at", 0.0)
    fake = FakeWbApiClient(
        fixtures={
            "/api/finance/v1/sales-reports/detailed": {
                "data": [
                    {
                        "rrdId": 10,
                        "currency": "RUB",
                        "docTypeName": "Продажа",
                        "quantity": 2,
                        "retailAmount": "1500.50",
                        "retailPriceWithDisc": "1000",
                        "forPay": "1200",
                        "commissionPercent": 25,
                        "ppvzSalesCommission": "100",
                        "acquiringFee": "10",
                        "deliveryService": "20",
                        "paidStorage": "30",
                        "paidAcceptance": "40",
                        "penalty": "50",
                        "deduction": "60",
                        "additionalPayment": "70",
                        "nmId": 123456,
                        "vendorCode": "FBBT_42",
                        "sku": "sku-1",
                        "saleDt": "2026-06-01T00:00:00Z",
                        "rrDate": "2026-06-01",
                        "srid": "sale-1",
                    },
                    {
                        "rrdId": 11,
                        "currency": "RUB",
                        "docTypeName": "Продажа",
                        "quantity": 2,
                        "retailAmount": "0",
                        "retailPriceWithDisc": "0",
                        "nmId": 123456,
                        "vendorCode": "FBBT_42",
                        "sku": "sku-1",
                        "saleDt": "2026-06-01T00:00:00Z",
                        "rrDate": "2026-06-01",
                        "srid": "sale-1",
                    },
                    {
                        "rrdId": 12,
                        "currency": "RUB",
                        "docTypeName": "Возврат",
                        "quantity": 1,
                        "retailAmount": "400.25",
                        "retailPriceWithDisc": "500",
                        "commissionPercent": 25,
                        "nmId": 123456,
                        "vendorCode": "FBBT_42",
                        "sku": "sku-1",
                        "saleDt": "2026-06-02T00:00:00Z",
                        "rrDate": "2026-06-02",
                        "srid": "return-1",
                    },
                    {
                        "rrdId": 13,
                        "currency": "RUB",
                        "docTypeName": "Продажа",
                        "quantity": 1,
                        "retailAmount": "900",
                        "retailPriceWithDisc": "1000",
                        "commissionPercent": 25,
                        "nmId": 123456,
                        "vendorCode": "FBBT_42",
                        "sku": "sku-2",
                        "saleDt": "2026-06-03T00:00:00Z",
                        "rrDate": "2026-06-03",
                        "srid": "sale-2",
                    },
                    {
                        "rrdId": 14,
                        "currency": "RUB",
                        "docTypeName": "Корректировка",
                        "sellerOperName": "Возврат штрафа",
                        "bonusTypeName": "Компенсация продавцу",
                        "penalty": "-20",
                        "deduction": "-30",
                        "additionalPayment": "40",
                        "nmId": 123456,
                        "vendorCode": "FBBT_42",
                        "sku": "sku-2",
                        "rrDate": "2026-06-03",
                    },
                ]
            }
        }
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_finance_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_finance_report_aggregates(
        "complete",
        wb_token="finance-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    row = payload["aggregates"]["123456"]
    assert row["buyerRevenueKopecks"] == 200_025
    assert row["sellerRevenueKopecks"] == 250_000
    assert row["revenueGrossKopecks"] == 250_000
    assert row["platformDiscountKopecks"] == 49_975
    assert row["commissionFormulaKopecks"] == 50_006
    assert row["salesUnits"] == 3
    assert row["returnsUnits"] == 1
    assert row["netSalesUnits"] == 2
    assert row["payableKopecks"] == 120_000
    assert row["commissionKopecks"] == 10_000
    assert row["acquiringKopecks"] == 1_000
    assert row["logisticsKopecks"] == 2_000
    assert row["storageKopecks"] == 3_000
    assert row["acceptanceKopecks"] == 4_000
    assert row["penaltyKopecks"] == 3_000
    assert row["penaltyChargedKopecks"] == 5_000
    assert row["penaltyReturnedKopecks"] == 2_000
    assert row["deductionKopecks"] == 3_000
    assert row["deductionChargedKopecks"] == 6_000
    assert row["deductionCompensationKopecks"] == 3_000
    # Raw additionalPayment is a signed reward expense; canonical credits
    # carry its inverse sign, matching platform.finance.service.
    assert row["additionalPaymentKopecks"] == -11_000
    assert row["unitKeyedSalesCount"] == 2
    assert row["unitKeyedReturnsCount"] == 1
    diagnostics = payload["diagnostics"]
    assert diagnostics["tax"]["taxPct"] is None
    assert diagnostics["tax"]["taxIncludedInExpenses"] is False
    assert diagnostics["totals"]["taxKopecks"] is None
    assert diagnostics["totals"]["storageKopecks"] == 3_000
    assert diagnostics["totals"]["acceptanceKopecks"] == 4_000
    assert diagnostics["totals"]["penaltyReturnedKopecks"] == 2_000
    assert diagnostics["totals"]["deductionCompensationKopecks"] == 3_000
    assert diagnostics["totals"]["additionalPaymentKopecks"] == -11_000
    assert diagnostics["storageAcceptance"]["rawRowsAvailable"] is True
    assert diagnostics["storageAcceptance"]["paidStorageFieldRequested"] is True
    assert diagnostics["storageAcceptance"]["paidAcceptanceFieldRequested"] is True
    assert diagnostics["storageAcceptance"]["paidStorageRowsWithField"] == 1
    assert diagnostics["storageAcceptance"]["paidStorageNonzeroRows"] == 1
    assert diagnostics["storageAcceptance"]["paidStorageSumKopecks"] == 3_000
    assert diagnostics["storageAcceptance"]["paidAcceptanceRowsWithField"] == 1
    assert diagnostics["storageAcceptance"]["paidAcceptanceNonzeroRows"] == 1
    assert diagnostics["storageAcceptance"]["paidAcceptanceSumKopecks"] == 4_000
    assert diagnostics["paidStorageNonzeroRows"] == 1
    assert diagnostics["paidStorageSum"] == 3_000
    assert diagnostics["paidAcceptanceNonzeroRows"] == 1
    assert diagnostics["paidAcceptanceSum"] == 4_000
    assert diagnostics["adjustmentRowsTotal"] == 2
    correction = next(item for item in diagnostics["adjustmentRows"] if item["rrdId"] == 14)
    assert correction["raw"]["sellerOperName"] == "Возврат штрафа"
    assert correction["raw"]["bonusTypeName"] == "Компенсация продавцу"
    assert correction["normalized"]["penaltyKopecks"] == -2_000
    assert correction["normalized"]["deductionKopecks"] == -3_000
    assert correction["normalized"]["additionalPaymentKopecks"] == 4_000
    assert correction["normalized"]["expenseFormulaContributionKopecks"] == -1_000
    assert diagnostics["skuSummaries"][0]["taxIncludedInExpenses"] is False
    assert diagnostics["skuSummaries"][0]["expensesWithoutTaxKopecks"] == 37_000
    assert diagnostics["skuSummaries"][0]["expensesIfTaxIncludedKopecks"] is None
    assert payload["rowsCount"] == 5
    assert fake.requests[0].jsonBody["limit"] == 10_000
    assert fake.requests[0].jsonBody["rrdId"] == 0
    assert fake.requests[0].jsonBody["dateFrom"] == "2026-06-01"
    assert fake.requests[0].jsonBody["dateTo"] == "2026-06-03"  # Inclusive finance endpoint bound
    assert payload["dateTo"] == "2026-06-03"
    assert fake.requests[0].jsonBody["period"] == "daily"
    assert {
        "docTypeName",
        "retailAmount",
        "retailPriceWithDisc",
        "nmId",
        "vendorCode",
        "paidStorage",
        "paidAcceptance",
        "deduction",
        "bonusTypeName",
        "forPay",
        "srid",
    }.issubset(set(fake.requests[0].jsonBody["fields"]))
    assert "paidStorage" in payload["requestedFields"]
    assert "paidAcceptance" in payload["requestedFields"]


def test_finance_report_logistics_uses_delivery_service_money_not_counts_or_rebill(monkeypatch):
    monkeypatch.setattr(repricer_bff_module, "_finance_report_last_request_at", 0.0)
    fake = FakeWbApiClient(
        fixtures={
            "/api/finance/v1/sales-reports/detailed": {
                "data": [
                    {
                        "rrdId": 20,
                        "currency": "RUB",
                        "docTypeName": "Продажа",
                        "quantity": 1,
                        "retailAmount": "1000",
                        "retailPriceWithDisc": "1000",
                        "nmId": 123456,
                        "deliveryAmount": 3,
                        "rebillLogisticCost": "554.92",
                        "deliveryService": "2360.66",
                        "rrDate": "2026-06-01",
                    }
                ]
            }
        }
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_finance_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_finance_report_aggregates(
        "complete",
        wb_token="finance-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    assert payload["aggregates"]["123456"]["logisticsKopecks"] == 236_066


def test_finance_diagnostics_response_returns_cached_clean_payload():
    cache = {
        "aggregates": {
            "123456": {
                "rowsCount": 2,
                "salesUnits": 1,
                "sellerRevenueKopecks": 100_000,
                "buyerRevenueKopecks": 90_000,
                "commissionFormulaKopecks": 25_000,
                "logisticsKopecks": 2_000,
                "storageKopecks": 1_000,
                "acceptanceKopecks": 500,
                "penaltyKopecks": -2_000,
                "penaltyChargedKopecks": 0,
                "penaltyReturnedKopecks": 2_000,
                "deductionKopecks": -3_000,
                "deductionChargedKopecks": 0,
                "deductionCompensationKopecks": 3_000,
                "additionalPaymentKopecks": 4_000,
                "acquiringKopecks": 800,
            }
        },
        "diagnostics": {
            "state": "ok",
            "tax": {"taxPct": 6, "taxKopecks": 6_000, "taxIncludedInExpenses": False},
            "formula": {"taxIncludedInExpenses": False},
            "totals": {"taxKopecks": 6_000, "expensesWithoutTaxKopecks": 20_300},
            "skuSummaries": [{"nmId": "123456", "taxIncludedInExpenses": False}],
            "adjustmentRows": [
                {
                    "nmId": 123456,
                    "rrdId": 77,
                    "raw": {"sellerOperName": "Компенсация штрафа", "penalty": "-20"},
                    "normalized": {"penaltyKopecks": -2_000},
                }
            ],
            "adjustmentRowsTotal": 1,
        },
        "count": 1,
        "rowsCount": 2,
        "pagesLoaded": 1,
        "periodDays": 3,
        "dateFrom": "2026-06-01",
        "dateTo": "2026-06-03",
        "fetchedAt": "2026-06-03T12:00:00+00:00",
        "cachedGoodsNmIds": 1,
        "matchedCachedGoodsNmIds": 1,
    }
    payload = _finance_diagnostics_response(
        organization_id=1,
        cache=cache,
        range_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        range_end=datetime(2026, 6, 3, tzinfo=timezone.utc),
        resolved_period_days=3,
        period_suffix="2026-06-01_2026-06-03",
        limit=1,
        refreshed=False,
    )

    assert payload["state"] == "ok"
    assert payload["refreshed"] is False
    assert payload["rawRowsStrippedFromCache"] is True
    assert payload["rowsCount"] == 2
    assert payload["diagnostics"]["tax"]["taxIncludedInExpenses"] is False
    assert payload["diagnostics"]["storageAcceptance"]["rawRowsAvailable"] is False
    assert payload["diagnostics"]["storageAcceptance"]["paidStorageNonzeroRows"] is None
    assert payload["diagnostics"]["storageAcceptance"]["paidStorageSumKopecks"] == 1_000
    assert payload["diagnostics"]["storageAcceptance"]["paidAcceptanceNonzeroRows"] is None
    assert payload["diagnostics"]["storageAcceptance"]["paidAcceptanceSumKopecks"] == 500
    assert payload["diagnostics"]["adjustmentRowsReturned"] == 1
    assert payload["diagnostics"]["adjustmentRows"][0]["rrdId"] == 77


def test_margin_breakdown_adds_raw_storage_not_assigned_to_sku_rows():
    rows = [
        {
            "meta": {"articleId": "FBBT_42", "nmId": 123456},
            "settings": {"otherExpensePricePct": 5, "otherExpensePerSaleKopecks": 0},
            "analytics": {
                "revenueKopecks": 1_000_000,
                "cogsTotalKopecks": 300_000,
                "commissionKopecks": 200_000,
                "logisticsKopecks": 50_000,
                "storageKopecks": 0,
                "acceptanceKopecks": 0,
                "penaltyKopecks": 0,
                "deductionKopecks": 0,
                "acquiringKopecks": 30_000,
                "adSpendKopecks": 20_000,
                "otherExpensesKopecks": 50_000,
                "additionalPaymentKopecks": 0,
                "taxKopecks": 60_000,
                "expensesKopecks": 350_000,
                "netProfitKopecks": 350_000,
            },
        }
    ]
    diagnostics = {
        "storageAcceptance": {
            "rawRowsAvailable": True,
            "paidStorageSumKopecks": 293_908,
            "paidStorageMissingNmRows": 30,
            "paidStorageMissingNmSumKopecks": 293_908,
            "paidAcceptanceSumKopecks": 0,
        }
    }

    breakdown = _margin_breakdown_from_rows(rows, 5000, finance_diagnostics=diagnostics)

    assert breakdown["totals"]["storage"] == 293_908
    assert breakdown["totals"]["unassignedStorage"] == 293_908
    assert breakdown["totals"]["expensesKopecks"] == 643_908
    assert breakdown["totals"]["actualNetProfitKopecks"] == 56_092
    assert breakdown["unassignedComponents"][0]["label"] == "Хранение WB без SKU"


def test_finance_report_retries_wb_429_retry_after(monkeypatch):
    class RetryThenOkFinanceClient:
        def __init__(self) -> None:
            self.requests: list[WbApiRequest] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            if len(self.requests) == 1:
                rate_limit = RateLimitInfo(retryAfterSeconds=18)
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=429,
                    ok=False,
                    error=classify_wb_error(429, "Limited by global limiter", rate_limit),
                    rateLimit=rate_limit,
                )
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": []})

    fake = RetryThenOkFinanceClient()
    sleeps: list[float] = []
    clock = [1000.0]
    monkeypatch.setattr(repricer_bff_module, "_finance_report_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(repricer_bff_module.time, "sleep", lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)))
    monkeypatch.setattr("app.repricer_bff.build_wb_finance_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_finance_report_aggregates(
        "complete",
        wb_token="finance-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    assert payload["rowsCount"] == 0
    assert len(fake.requests) == 2
    assert sleeps == [repricer_bff_module._FINANCE_REPORT_MIN_INTERVAL_S + 1.0]


def test_baskets_sales_funnel_retries_wb_429_retry_after(monkeypatch):
    class RetryThenOkAnalyticsClient:
        def __init__(self) -> None:
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
                                "product": {"nmId": 101},
                                "statistic": {
                                    "selected": {
                                        "viewCount": 20,
                                        "openCard": 5,
                                        "cartCount": 2,
                                        "orderCount": 1,
                                        "buyoutCount": 1,
                                        "conversions": {},
                                    }
                                },
                            }
                        ]
                    }
                },
            )

    fake = RetryThenOkAnalyticsClient()
    sleeps: list[float] = []
    clock = [1000.0]
    monkeypatch.setattr(repricer_bff_module, "_sales_funnel_products_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        repricer_bff_module.time,
        "sleep",
        lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_baskets_aggregates(
        "complete",
        wb_token="analytics-token",
        period_days=30,
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 30, tzinfo=timezone.utc),
        nm_ids=[101],
    )

    assert payload["count"] == 1
    assert payload["aggregates"]["101"]["impressions"] == 20
    assert payload["aggregates"]["101"]["openCount"] == 5
    assert payload["aggregates"]["101"]["cartCount"] == 2
    assert len(fake.requests) == 2
    assert fake.requests[0].path == "/api/analytics/v3/sales-funnel/products"
    assert sleeps == [repricer_bff_module._SALES_FUNNEL_PRODUCTS_MIN_INTERVAL_S + 1.0]


def test_baskets_sales_funnel_accepts_count_aliases_from_wb(monkeypatch):
    class AnalyticsClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            return WbApiResponseEnvelope(
                request=request,
                statusCode=200,
                ok=True,
                data={
                    "data": {
                        "products": [
                            {
                                "product": {"nmId": 101},
                                "statistic": {
                                    "selected": {
                                        "openCardCount": 380,
                                        "viewCount": 1_900,
                                        "addToCartCount": 64,
                                        "addToWishlist": 12,
                                        "ordersCount": 27,
                                        "ordersSumRub": 48120,
                                        "buyoutsCount": 19,
                                        "buyoutsSumRub": 33820,
                                        "conversions": {"buyoutPercent": 70},
                                    }
                                },
                            }
                        ]
                    }
                },
            )

    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: AnalyticsClient())
    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", lambda client, request, **_kwargs: client.request(request).data)

    payload = repricer_bff_module.fetch_baskets_aggregates(
        "complete",
        wb_token="analytics-token",
        period_days=7,
        date_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 7, 7, tzinfo=timezone.utc),
        nm_ids=[101],
    )

    aggregate = payload["aggregates"]["101"]
    assert aggregate["openCount"] == 380
    assert aggregate["impressions"] == 1_900
    assert aggregate["wishlistCount"] == 12
    assert aggregate["cartCount"] == 64
    assert aggregate["orderCount"] == 27
    assert aggregate["orderSumKopecks"] == 4_812_000
    assert aggregate["buyoutCount"] == 19
    assert aggregate["buyoutSumKopecks"] == 3_382_000
    assert aggregate["buyoutPct"] == 70


def test_stock_aggregates_merge_product_stock_count_without_mp_stock_type(monkeypatch):
    class AnalyticsClient:
        def __init__(self):
            self.stock_types: list[str] = []
            self.paths: list[str] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.paths.append(request.path)
            if request.path == "/api/v2/stocks-report/products/products":
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=200,
                    ok=True,
                    data={"data": {"items": [{"nmId": 101, "stockCount": 1330}]}},
                )
            stock_type = str(request.jsonBody.get("stockType"))
            self.stock_types.append(stock_type)
            return WbApiResponseEnvelope(
                request=request,
                statusCode=200,
                ok=True,
                data={
                    "data": {
                        "items": [
                            {
                                "nmId": 101,
                                "warehouseName": "Коледино",
                                "quantity": 36,
                                "inWayToClient": 2,
                                "inWayFromClient": 3,
                            }
                        ]
                    }
                },
            )

    fake = AnalyticsClient()
    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: fake)

    aggregates = repricer_bff_module.fetch_stock_aggregates("complete", wb_token="analytics-token")

    assert fake.stock_types == ["wb"]
    assert fake.paths == ["/api/analytics/v1/stocks-report/wb-warehouses", "/api/v2/stocks-report/products/products"]
    assert aggregates["101"]["wbStockUnits"] == 36
    assert aggregates["101"]["marketplaceStockUnits"] == 1294
    assert aggregates["101"]["totalStockUnits"] == 1330
    assert aggregates["101"]["inWayToClient"] == 2
    assert aggregates["101"]["inWayFromClient"] == 3


def test_baskets_aggregate_progress_advances_for_each_sku_batch(monkeypatch):
    class AnalyticsClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            products = [
                {"product": {"nmId": nm_id}, "statistic": {"selected": {"cartCount": 1}}}
                for nm_id in request.jsonBody["nmIds"]
            ]
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": {"products": products}})

    progress: list[dict] = []
    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: AnalyticsClient())
    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", lambda client, request, **_kwargs: client.request(request).data)

    payload = repricer_bff_module.fetch_baskets_aggregates(
        "complete",
        wb_token="token",
        period_days=2,
        nm_ids=list(range(1, 2002)),
        progress_callback=progress.append,
    )

    completed = [item for item in progress if item["phase"] == "completed"]
    assert payload["requestedNmIds"] == 2001
    assert [item["requestsCompleted"] for item in completed] == [1, 2, 3]
    assert completed[-1]["processedNmIds"] == 2001


def test_baskets_daily_detail_request_total_is_days_times_sku_batches(monkeypatch):
    class AnalyticsClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": {"products": []}})

    progress: list[dict] = []
    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: AnalyticsClient())
    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", lambda client, request, **_kwargs: client.request(request).data)

    payload = repricer_bff_module.fetch_baskets_daily_detail(
        "complete",
        wb_token="token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
        nm_ids=list(range(1, 1002)),
        progress_callback=progress.append,
    )

    assert payload["requestsTotal"] == 6
    assert payload["requestsCompleted"] == 6
    assert len([item for item in progress if item["phase"] == "requesting"]) == 6
    assert len([item for item in progress if item["phase"] == "completed"]) == 6


def test_baskets_daily_detail_reports_sales_funnel_waits(monkeypatch):
    class AnalyticsClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": {"products": []}})

    def request_with_wait(client, request, *, progress_callback=None):
        if progress_callback:
            progress_callback({"phase": "rate_limit", "message": "WB Sales Funnel: 429, ждём 18 сек"})
        return client.request(request).data

    progress: list[dict] = []
    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: AnalyticsClient())
    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", request_with_wait)

    repricer_bff_module.fetch_baskets_daily_detail(
        "complete",
        wb_token="token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        nm_ids=[101],
        progress_callback=progress.append,
    )

    assert any(item["phase"] == "rate_limit" and item["day"] == "2026-06-01" for item in progress)


def test_baskets_daily_detail_skips_existing_days(monkeypatch):
    requested_days: list[str] = []

    class AnalyticsClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            requested_days.append(request.jsonBody["selectedPeriod"]["start"])
            return WbApiResponseEnvelope(
                request=request,
                statusCode=200,
                ok=True,
                data={"data": {"products": [{"nmID": request.jsonBody["nmIds"][0], "openCardCount": 2}]}},
            )

    progress: list[dict] = []
    completed_days: list[str] = []
    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: AnalyticsClient())
    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", lambda client, request, **_kwargs: client.request(request).data)

    payload = repricer_bff_module.fetch_baskets_daily_detail(
        "complete",
        wb_token="token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
        nm_ids=[101],
        progress_callback=progress.append,
        existing_daily_aggregates={"2026-06-01": {"101": {"openCardCount": 7}}},
        day_completed_callback=lambda day, _rows, _progress: completed_days.append(day),
    )

    assert requested_days == ["2026-06-02", "2026-06-03"]
    assert payload["requestsTotal"] == 2
    assert payload["requestsCompleted"] == 2
    assert payload["dailyAggregates"]["2026-06-01"]["101"]["openCardCount"] == 7
    assert completed_days == ["2026-06-02", "2026-06-03"]
    assert any(item["phase"] == "skipped" and item["day"] == "2026-06-01" for item in progress)


def test_baskets_daily_detail_counts_only_missing_chunk_requests(monkeypatch):
    requested: list[tuple[str, int]] = []
    progress: list[dict] = []

    def request(_client, request, **_kwargs):
        day = request.jsonBody["selectedPeriod"]["start"]
        requested.append((day, len(request.jsonBody["nmIds"])))
        return {"data": {"products": []}}

    nm_ids = list(range(1, 3201))
    cached_days = [date(2026, 7, 29) + timedelta(days=offset) for offset in range(7)]
    cached_chunks = [
        {"type": "daily", "date": day.isoformat(), "chunkIndex": chunk_index, "status": "done", "attempts": 1}
        for day in cached_days
        for chunk_index in range(4)
    ]

    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", request)

    payload = repricer_bff_module.fetch_baskets_daily_detail(
        "complete",
        date_from=datetime(2026, 7, 6, tzinfo=timezone.utc),
        date_to=datetime(2026, 8, 4, tzinfo=timezone.utc),
        nm_ids=nm_ids,
        progress_callback=progress.append,
        existing_daily_aggregates={day.isoformat(): {} for day in cached_days},
        existing_chunks=cached_chunks,
    )

    assert payload["requestsTotal"] == 92
    assert payload["requestsCompleted"] == 92
    assert len(requested) == 92
    assert progress[0]["phase"] == "requesting"
    assert progress[0]["requestsTotal"] == 92
    assert not any(day.isoformat() in {request_day for request_day, _count in requested} for day in cached_days)


def test_baskets_daily_detail_skips_existing_days_when_chunks_empty(monkeypatch):
    requested_days: list[str] = []

    class AnalyticsClient:
        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            requested_days.append(request.jsonBody["selectedPeriod"]["start"])
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data={"data": {"products": []}})

    monkeypatch.setattr("app.repricer_bff.build_wb_analytics_client", lambda *args, **kwargs: AnalyticsClient())
    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", lambda client, request, **_kwargs: client.request(request).data)

    payload = repricer_bff_module.fetch_baskets_daily_detail(
        "complete",
        wb_token="token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 2, tzinfo=timezone.utc),
        nm_ids=[101],
        existing_daily_aggregates={"2026-06-01": {"101": {"openCardCount": 7}}},
        existing_chunks=[],
    )

    assert requested_days == ["2026-06-02"]
    assert payload["requestsTotal"] == 1
    assert payload["requestsCompleted"] == 1
    assert payload["dailyAggregates"]["2026-06-01"]["101"]["openCardCount"] == 7


def test_baskets_detail_job_merges_exact_cache_and_cache_hit_avoids_duplicate(monkeypatch):
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "get_wb_sync_status",
        lambda _organization_id: {
            "state": "completed",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-03",
        },
    )
    source_state = {
        "baskets_2026-06-01_2026-06-03": {
            "aggregates": {"101": {"cartCount": 9}},
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-03",
        }
    }
    fetch_calls: list[dict] = []

    monkeypatch.setattr("app.routers.wb_repricer_bff._request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=7), "token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.actor_from_request", lambda _request: SimpleNamespace(organization_id=7))
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_cached_goods", lambda organization_id: [{"nmID": 101}] if organization_id == 7 else [])
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda organization_id, key, **_kwargs: deepcopy(source_state.get(f"{organization_id}:{key}") or source_state.get(key) or {}))

    def save(organization_id, key, payload):
        source_state[f"{organization_id}:{key}"] = deepcopy(payload)
        return payload

    def fetch(*_args, **kwargs):
        fetch_calls.append(kwargs)
        kwargs["progress_callback"]({"phase": "requesting", "requestsCompleted": 0, "requestsTotal": 3, "day": "2026-06-01", "batch": 1, "batchesTotal": 1})
        return {"dailyAggregates": {"2026-06-01": {"101": {"cartCount": 2}}}, "requestsCompleted": 3, "requestsTotal": 3, "dateFrom": "2026-06-01", "dateTo": "2026-06-03", "periodDays": 3}

    monkeypatch.setattr("app.routers.wb_repricer_bff.save_source_cache", save)
    monkeypatch.setattr("app.routers.wb_repricer_bff.fetch_baskets_daily_detail", fetch)
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    api = client()

    started = api.post("/api/v1/wb-repricer/baskets/detail/start", json={"dateFrom": "2026-06-01", "dateTo": "2026-06-03"})
    assert started.status_code == 200
    run_id = started.json()["runId"]
    status = api.get("/api/v1/wb-repricer/baskets/detail/status", params={"runId": run_id})
    assert status.status_code == 200
    assert status.json()["state"] == "completed"
    exact = source_state["7:baskets_2026-06-01_2026-06-03"]
    assert exact["aggregates"]["101"]["cartCount"] == 9
    assert exact["dailyAggregates"]["2026-06-01"]["101"]["cartCount"] == 2

    cached = api.post("/api/v1/wb-repricer/baskets/detail/start", json={"dateFrom": "2026-06-01", "dateTo": "2026-06-03"})
    assert cached.status_code == 200
    assert cached.json()["cached"] is True
    assert len(fetch_calls) == 1


def test_baskets_detail_start_combines_split_nightly_caches(monkeypatch):
    first_days = [date(2026, 7, 11) + timedelta(days=offset) for offset in range(15)]
    second_days = [date(2026, 7, 26) + timedelta(days=offset) for offset in range(15)]
    first_key = "baskets_2026-07-11_2026-07-25"
    second_key = "baskets_2026-07-26_2026-08-09"
    source_state = {
        first_key: {
            "dateFrom": "2026-07-11",
            "dateTo": "2026-07-25",
            "dailyDetailStatus": "fetched",
            "dailyDetailFetchedAt": "2026-08-10T01:00:00+00:00",
            "dailyAggregates": {day.isoformat(): {"101": {"cartCount": 1}} for day in first_days},
        },
        second_key: {
            "dateFrom": "2026-07-26",
            "dateTo": "2026-08-09",
            "dailyDetailStatus": "fetched",
            "dailyDetailFetchedAt": "2026-08-10T01:05:00+00:00",
            "dailyAggregates": {day.isoformat(): {"101": {"cartCount": 2}} for day in second_days},
        },
    }

    monkeypatch.setattr("app.routers.wb_repricer_bff._request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=7), "token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_wb_sync_status", lambda _organization_id: {})
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_source_cache",
        lambda organization_id, key, **_kwargs: deepcopy(source_state.get(key) or {}) if organization_id == 7 else {},
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda organization_id, prefix, **_kwargs: (
            [
                {
                    "sourceKey": key,
                    "dateFrom": payload["dateFrom"],
                    "dateTo": payload["dateTo"],
                    "dailyAggregateDates": sorted(payload["dailyAggregates"]),
                }
                for key, payload in source_state.items()
                if organization_id == 7
            ]
            if prefix == "baskets_"
            else [{"sourceKey": f"{prefix}nightly", "dateFrom": "2026-07-11", "dateTo": "2026-08-09"}]
        ),
    )

    def save(organization_id, key, payload):
        assert organization_id == 7
        source_state[key] = deepcopy(payload)
        return payload

    monkeypatch.setattr("app.routers.wb_repricer_bff.save_source_cache", save)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("WB refresh must not start")),
    )

    response = client().post(
        "/api/v1/wb-repricer/baskets/detail/start",
        json={"dateFrom": "2026-07-13", "dateTo": "2026-07-28"},
    )

    assert response.status_code == 200
    assert response.json()["cached"] is True
    combined = source_state["baskets_2026-07-13_2026-07-28"]
    assert sorted(combined["dailyAggregates"]) == [
        (date(2026, 7, 13) + timedelta(days=offset)).isoformat() for offset in range(16)
    ]
    assert combined["dailyAggregates"]["2026-07-25"]["101"]["cartCount"] == 1
    assert combined["dailyAggregates"]["2026-07-26"]["101"]["cartCount"] == 2


def test_baskets_detail_start_rejects_second_active_run_for_same_org(monkeypatch):
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "get_wb_sync_status",
        lambda _organization_id: {
            "state": "completed",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-03",
        },
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=7), "token"))
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_source_cache",
        lambda organization_id, key, **_kwargs: {"runId": "active-run", "state": "running", "dateFrom": "2026-05-01", "dateTo": "2026-05-03"} if organization_id == 7 and key == "baskets_detail_active" else {},
    )
    api = client()

    response = api.post("/api/v1/wb-repricer/baskets/detail/start", json={"dateFrom": "2026-06-01", "dateTo": "2026-06-03"})

    assert response.status_code == 409
    assert response.json()["error"]["details"]["activeRunId"] == "active-run"


def test_baskets_detail_start_reuses_same_range_active_run(monkeypatch):
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "get_wb_sync_status",
        lambda _organization_id: {
            "state": "completed",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-03",
        },
    )
    active = {"runId": "active-run", "state": "running", "dateFrom": "2026-06-01", "dateTo": "2026-06-03", "progressPercent": 40}
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=7), "token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda organization_id, key, **_kwargs: active if organization_id == 7 and key == "baskets_detail_active" else {})
    api = client()

    response = api.post("/api/v1/wb-repricer/baskets/detail/start", json={"dateFrom": "2026-06-01", "dateTo": "2026-06-03"})

    assert response.status_code == 200
    assert response.json()["runId"] == "active-run"
    assert response.json()["progressPercent"] == 40


def test_baskets_detail_start_marks_stale_active_run_failed_and_starts_new(monkeypatch):
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "get_wb_sync_status",
        lambda _organization_id: {
            "state": "completed",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-03",
        },
    )
    active = {
        "runId": "stale-run",
        "state": "running",
        "dateFrom": "2026-05-01",
        "dateTo": "2026-05-31",
        "periodDays": 31,
        "requestsCompleted": 57,
        "requestsTotal": 124,
        "progressPercent": 46,
        "updatedAt": (
            datetime.now(timezone.utc)
            - wb_repricer_bff_router.BASKETS_DETAIL_HEARTBEAT_TIMEOUT
            - timedelta(seconds=1)
        ).isoformat(),
    }
    source_state = {"7:baskets_detail_active": active}
    saved: list[tuple[str, dict]] = []

    monkeypatch.setattr("app.routers.wb_repricer_bff._request_actor_and_wb_token", lambda _request: (SimpleNamespace(organization_id=7), "token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_cached_goods", lambda organization_id: [{"nmID": 101}] if organization_id == 7 else [])
    def get_cache(organization_id, key, **_kwargs):
        return deepcopy(source_state.get(f"{organization_id}:{key}") or {})

    def save_cache(organization_id, key, payload):
        source_state[f"{organization_id}:{key}"] = deepcopy(payload)
        saved.append((key, deepcopy(payload)))
        return payload

    class Background:
        def add_task(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", get_cache)
    monkeypatch.setattr("app.routers.wb_repricer_bff.save_source_cache", save_cache)
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])

    response = wb_repricer_bff_router.start_baskets_detail(
        SimpleNamespace(),
        wb_repricer_bff_router.BasketsDetailStartRequest(dateFrom=date(2026, 6, 1), dateTo=date(2026, 6, 3)),
        Background(),
    )

    failed_active = next(payload for key, payload in saved if key == "baskets_detail_active" and payload.get("runId") == "stale-run")
    assert failed_active["state"] == "failed"
    assert failed_active["error"] == "Baskets detail run heartbeat expired"
    assert response["state"] == "running"
    assert response["runId"] != "stale-run"


def test_period_stats_retries_statistics_429_retry_after(monkeypatch):
    class RetryThenOkStatisticsClient:
        def __init__(self) -> None:
            self.requests: list[WbApiRequest] = []
            self.sales_requests = 0

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            if request.path == "/api/v1/supplier/orders":
                return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data=[])
            self.sales_requests += 1
            if self.sales_requests == 1:
                rate_limit = RateLimitInfo(retryAfterSeconds=34)
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=429,
                    ok=False,
                    error=classify_wb_error(429, "Limited by global limiter, per seller", rate_limit),
                    rateLimit=rate_limit,
                )
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data=[])

    fake = RetryThenOkStatisticsClient()
    sleeps: list[float] = []
    clock = [1000.0]
    monkeypatch.setattr(repricer_bff_module, "_statistics_report_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        repricer_bff_module.time,
        "sleep",
        lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_statistics_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_period_stats_aggregates(
        "complete",
        wb_token="statistics-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    assert payload["aggregates"] == {}
    assert [request.path for request in fake.requests] == [
        "/api/v1/supplier/orders",
        "/api/v1/supplier/sales",
        "/api/v1/supplier/sales",
    ]
    assert sleeps == [repricer_bff_module._STATISTICS_REPORT_MIN_INTERVAL_S, repricer_bff_module._STATISTICS_REPORT_MIN_INTERVAL_S + 1.0]


def test_finance_uses_actual_expenses_and_keeps_profit_unknown_without_dated_tax():
    row = repricer_bff_module._build_sku_row(
        "TEST_01",
        nm_id=123456,
        name="Test",
        subject="Товары",
        brand="Brand",
        chrt_ids=[],
        current_price_kopecks=129_000,
        discounted_price_kopecks=119_900,
        buyer_price_kopecks=110_000,
        promotions=[],
        use_demo_data=False,
        finance_aggregate={
            "salesUnits": 2,
            "buyerRevenueKopecks": 200_025,
            "sellerRevenueKopecks": 260_000,
            "platformDiscountKopecks": 59_975,
            "revenueGrossKopecks": 260_000,
            "commissionKopecks": 10_000,
            "commissionFormulaKopecks": 60_000,
            "acquiringKopecks": 1_000,
            "logisticsKopecks": 2_000,
            "storageKopecks": 3_000,
            "acceptanceKopecks": 4_000,
            "penaltyKopecks": 5_000,
            "deductionKopecks": 6_000,
            "additionalPaymentKopecks": 8_000,
            "penaltyChargedKopecks": 5_000,
            "penaltyReturnedKopecks": 0,
            "deductionChargedKopecks": 6_000,
            "deductionCompensationKopecks": 0,
        },
        ads_aggregate={
            "adSpendKopecks": 7_000,
            "adImpressions": 1000,
            "adClicks": 40,
            "adCartAdds": 5,
            "adOrders": 3,
            "adRevenueKopecks": 450_000,
        },
    )

    analytics = row["analytics"]
    assert row["settings"]["cogsKopecks"] == 45_000
    assert analytics["cogsTotalKopecks"] == 90_000
    assert analytics["sellerRevenueKopecks"] == 260_000
    assert analytics["buyerRevenueKopecks"] == 200_025
    assert analytics["platformDiscountKopecks"] == 59_975
    assert analytics["commissionKopecks"] == 10_000
    assert analytics["financeCommissionKopecks"] == 10_000
    assert analytics["commissionFormulaKopecks"] == 60_000
    assert analytics["commissionCalcMode"] == "finance_actual"
    assert analytics["otherExpensesKopecks"] == 0
    assert analytics["taxKopecks"] is None
    assert analytics["taxPct"] == 6
    assert analytics["acquiringKopecks"] == 1_000
    assert analytics["expensesKopecks"] == 30_000
    assert analytics["netProfitKopecks"] is None
    assert analytics["factNetProfitKopecks"] is None
    assert analytics["plannedPeriodMarginKopecks"] is None
    assert analytics["plannedCommissionKopecks"] is None
    assert analytics["plannedAcquiringKopecks"] == 8_606
    assert analytics["plannedForwardLogisticsKopecks"] is None
    assert analytics["plannedReturnLogisticsKopecks"] is None
    assert analytics["plannedOtherExpensesKopecks"] == 13_000
    assert analytics["plannedTaxKopecks"] == 15_600
    assert analytics["adSpendKopecks"] == 7_000
    assert analytics["adImpressions"] == 1000
    assert analytics["adClicks"] == 40
    assert analytics["adCartAdds"] == 5
    assert analytics["adOrders"] == 3
    assert analytics["adRevenueKopecks"] == 450_000
    assert analytics["penaltyChargedKopecks"] == 5_000
    assert analytics["penaltyReturnedKopecks"] == 0
    assert analytics["deductionChargedKopecks"] == 6_000
    assert analytics["deductionCompensationKopecks"] == 0
    assert analytics["additionalPaymentKopecks"] == 8_000


def test_ads_spend_aggregates_use_exact_nm_fullstats(monkeypatch):
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [{"advert_list": [{"advertId": 22161678}]}],
            },
            "/adv/v3/fullstats": [
                {
                    "advertId": 22161678,
                    "apps": [
                        {
                            "nm": [
                                {
                                    "nmId": 123456,
                                    "sum": 321.45,
                                    "views": 1000,
                                    "clicks": 40,
                                    "atbs": 5,
                                    "orders": 3,
                                    "sum_price": 4500,
                                }
                            ]
                        }
                    ],
                }
            ],
        }
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    row = payload["aggregates"]["123456"]
    assert row["adSpendKopecks"] == 32_145
    assert row["adImpressions"] == 1000
    assert row["adClicks"] == 40
    assert row["adOrders"] == 3
    assert row["adRevenueKopecks"] == 450_000
    assert payload["campaignCount"] == 1
    assert payload["totals"]["adSpendKopecks"] == 32_145
    assert payload["totals"]["adOrders"] == 3
    fullstats_request = next(request for request in fake.requests if request.path == "/adv/v3/fullstats")
    assert fullstats_request.method == "GET"
    assert fullstats_request.query == {
        "ids": "22161678",
        "beginDate": "2026-06-01",
        "endDate": "2026-06-03",
    }


def test_ads_fullstats_uses_only_supported_campaign_statuses(monkeypatch):
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [
                    {"status": 4, "advert_list": [{"advertId": 444}]},
                    {"status": 7, "advert_list": [{"advertId": 777}]},
                    {"status": 9, "advert_list": [999]},
                    {"status": 11, "advert_list": [{"advertId": 1111}]},
                ],
            },
            "/adv/v3/fullstats": [],
        }
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    fullstats_request = next(request for request in fake.requests if request.path == "/adv/v3/fullstats")
    assert fullstats_request.query["ids"] == "777,999,1111"
    assert payload["campaignCount"] == 3


def test_ads_spend_aggregates_split_fullstats_windows_to_31_days(monkeypatch):
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [{"status": 7, "advert_list": [{"advertId": 22161678}]}],
            },
            "/adv/v3/fullstats": [],
        }
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)
    monkeypatch.setattr("app.repricer_bff._ads_fullstats_last_request_at", 0.0)
    monkeypatch.setattr("app.repricer_bff.time.sleep", lambda _seconds: None)

    repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )

    fullstats_requests = [request for request in fake.requests if request.path == "/adv/v3/fullstats"]
    assert [(request.query["beginDate"], request.query["endDate"]) for request in fullstats_requests] == [
        ("2026-06-10", "2026-07-10"),
        ("2026-07-11", "2026-07-15"),
    ]


def test_ads_fullstats_splits_invalid_payload_batch_and_skips_bad_campaign(monkeypatch):
    class InvalidBatchAdsClient:
        def __init__(self) -> None:
            self.requests: list[WbApiRequest] = []

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            if request.path == "/adv/v1/promotion/count":
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=200,
                    ok=True,
                    data={"adverts": [{"advert_list": [{"advertId": 111}, {"advertId": 222}]}]},
                )
            if request.query and request.query.get("ids") == "111":
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=200,
                    ok=True,
                    data=[
                        {
                            "advertId": 111,
                            "apps": [{"nm": [{"nmId": 123456, "sum": 10, "views": 5}]}],
                        }
                    ],
                )
            return WbApiResponseEnvelope(
                request=request,
                statusCode=400,
                ok=False,
                error=classify_wb_error(400, "invalid payload"),
            )

    fake = InvalidBatchAdsClient()
    clock = [1000.0]
    sleeps: list[float] = []
    monkeypatch.setattr(repricer_bff_module, "_ads_fullstats_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        repricer_bff_module.time,
        "sleep",
        lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)),
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    fullstats_requests = [request for request in fake.requests if request.path == "/adv/v3/fullstats"]
    assert [request.method for request in fullstats_requests] == ["GET", "GET", "GET"]
    assert [request.query["ids"] for request in fullstats_requests] == ["111,222", "111", "222"]
    assert payload["aggregates"]["123456"]["adSpendKopecks"] == 1_000
    assert payload["campaignCount"] == 2
    assert sleeps == [
        repricer_bff_module._ADS_FULLSTATS_MIN_INTERVAL_S,
        repricer_bff_module._ADS_FULLSTATS_MIN_INTERVAL_S,
    ]


def test_ads_spend_aggregates_filter_fullstats_rows_by_date(monkeypatch):
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {
                "adverts": [{"advert_list": [{"advertId": 22161678}]}],
            },
            "/adv/v3/fullstats": [
                {
                    "advertId": 22161678,
                    "days": [
                        {"date": "2026-06-02", "apps": [{"nm": [{"nmId": 123456, "sum": 100}]}]},
                        {"date": "2026-05-20", "apps": [{"nm": [{"nmId": 123456, "sum": 900}]}]},
                    ],
                }
            ],
        }
    )
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    assert payload["aggregates"]["123456"]["adSpendKopecks"] == 10_000
    assert payload["totals"]["adSpendKopecks"] == 10_000


def test_ads_fullstats_batches_wait_between_requests(monkeypatch):
    campaign_ids = [{"advertId": item} for item in range(1, 52)]
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {"adverts": [{"advert_list": campaign_ids}]},
            "/adv/v3/fullstats": [],
        }
    )
    sleeps: list[float] = []
    clock = [1000.0]
    monkeypatch.setattr(repricer_bff_module, "_ads_fullstats_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(repricer_bff_module.time, "sleep", lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)))
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    fullstats_requests = [request for request in fake.requests if request.path == "/adv/v3/fullstats"]
    assert payload["campaignCount"] == 51
    assert len(fullstats_requests) == 2
    assert sleeps == [repricer_bff_module._ADS_FULLSTATS_MIN_INTERVAL_S]


def test_ads_spend_aggregates_reports_progress_by_campaign_batches(monkeypatch):
    campaign_ids = [{"advertId": item} for item in range(1, 121)]
    fake = FakeWbApiClient(
        fixtures={
            "/adv/v1/promotion/count": {"adverts": [{"advert_list": campaign_ids}]},
            "/adv/v3/fullstats": [],
        }
    )
    progress: list[dict[str, object]] = []
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)
    monkeypatch.setattr(repricer_bff_module, "_ads_fullstats_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "sleep", lambda _seconds: None)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
        progress_callback=progress.append,
    )

    assert payload["campaignCount"] == 120
    assert [
        (item["phase"], item["campaignsProcessed"], item["campaignsTotal"], item["requestsCompleted"], item["requestsTotal"])
        for item in progress
    ] == [
        ("campaigns", 0, 120, 0, 3),
        ("fullstats", 50, 120, 1, 3),
        ("fullstats-wait", 50, 120, 1, 3),
        ("fullstats", 100, 120, 2, 3),
        ("fullstats-wait", 100, 120, 2, 3),
        ("fullstats", 120, 120, 3, 3),
    ]


def test_ads_fullstats_retries_wb_429_retry_after(monkeypatch):
    class RetryThenOkAdsClient:
        def __init__(self) -> None:
            self.requests: list[WbApiRequest] = []
            self.fullstats_requests = 0

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            if request.path == "/adv/v1/promotion/count":
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=200,
                    ok=True,
                    data={"adverts": [{"advert_list": [{"advertId": 22161678}]}]},
                )
            self.fullstats_requests += 1
            if self.fullstats_requests == 1:
                rate_limit = RateLimitInfo(retryAfterSeconds=1)
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=429,
                    ok=False,
                    error=classify_wb_error(429, "Limited by global limiter", rate_limit),
                    rateLimit=rate_limit,
                )
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data=[])

    fake = RetryThenOkAdsClient()
    sleeps: list[float] = []
    clock = [1000.0]
    monkeypatch.setattr(repricer_bff_module, "_ads_fullstats_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(repricer_bff_module.time, "sleep", lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)))
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    assert payload["count"] == 0
    assert fake.fullstats_requests == 2
    assert sleeps == [repricer_bff_module._ADS_FULLSTATS_MIN_INTERVAL_S + 2.0]


def test_ads_fullstats_retries_wb_internal_recovery_conflict(monkeypatch):
    class RetryThenOkAdsClient:
        def __init__(self) -> None:
            self.requests: list[WbApiRequest] = []
            self.fullstats_requests = 0

        def request(self, request: WbApiRequest) -> WbApiResponseEnvelope:
            self.requests.append(request)
            if request.path == "/adv/v1/promotion/count":
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=200,
                    ok=True,
                    data={"adverts": [{"advert_list": [{"advertId": 22161678}]}]},
                )
            self.fullstats_requests += 1
            if self.fullstats_requests == 1:
                message = (
                    "error getting stats: service.GetAvgPositions failed: "
                    "ERROR: canceling statement due to conflict with recovery (SQLSTATE 40001)"
                )
                return WbApiResponseEnvelope(
                    request=request,
                    statusCode=500,
                    ok=False,
                    error=classify_wb_error(500, message),
                )
            return WbApiResponseEnvelope(request=request, statusCode=200, ok=True, data=[])

    fake = RetryThenOkAdsClient()
    sleeps: list[float] = []
    clock = [1000.0]
    monkeypatch.setattr(repricer_bff_module, "_ads_fullstats_last_request_at", 0.0)
    monkeypatch.setattr(repricer_bff_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(repricer_bff_module.time, "sleep", lambda seconds: (sleeps.append(seconds), clock.__setitem__(0, clock[0] + seconds)))
    monkeypatch.setattr("app.repricer_bff.build_wb_ads_client", lambda *args, **kwargs: fake)

    payload = repricer_bff_module.fetch_ads_spend_aggregates(
        "complete",
        wb_token="ads-token",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    assert payload["count"] == 0
    assert fake.fullstats_requests == 2
    assert sleeps == [repricer_bff_module._ADS_FULLSTATS_MIN_INTERVAL_S]


def test_refresh_ads_endpoint_starts_background_sync_without_waiting_for_fullstats(monkeypatch):
    monkeypatch.setattr(wb_repricer_bff_router, "_full_sync_covers_range", lambda *a: (True, {}))
    started: list[dict[str, object]] = []
    background_calls: list[dict[str, object]] = []

    def unexpected_fullstats(*_args, **_kwargs):
        raise AssertionError("refresh-ads endpoint must not fetch fullstats synchronously")

    def fake_begin_wb_sync(**kwargs):
        started.append(kwargs)
        return {
            "runId": "wb_sync_ads_test",
            "state": "running",
            "running": True,
            "sources": kwargs.get("sources") or [],
            "steps": [],
        }

    def fake_refresh_task(**kwargs):
        background_calls.append(kwargs)
        return {"state": "completed", "steps": [{"source": "ads", "status": "ok"}]}

    monkeypatch.setattr("app.routers.wb_repricer_bff.actor_from_request", lambda _request: SimpleNamespace(user_id="user-1", organization_id=1))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_user_wb_token_secret", lambda _user_id: "wb-token")
    monkeypatch.setattr("app.routers.wb_repricer_bff.begin_wb_sync", lambda *args, **kwargs: fake_begin_wb_sync(**kwargs))
    monkeypatch.setattr("app.routers.wb_repricer_bff._refresh_wb_data_sources_and_flush", fake_refresh_task)
    monkeypatch.setattr("app.routers.wb_repricer_bff.fetch_ads_spend_aggregates", unexpected_fullstats)

    api = client()
    response = api.post(
        "/api/v1/wb-repricer/sku/refresh-ads?period_days=7&dateFrom=2026-06-01&dateTo=2026-06-07",
        headers=auth_headers(api, "admin"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "ads"
    assert payload["sync"]["runId"] == "wb_sync_ads_test"
    assert started[0]["sources"] == ["ads"]
    assert started[0]["period_days"] == 7
    assert background_calls[0]["sources"] == ["ads"]
    assert background_calls[0]["period_days"] == 7
    assert background_calls[0]["wb_token"] == "wb-token"


def test_refresh_ads_endpoint_does_not_force_start_over_running_sync(monkeypatch):
    monkeypatch.setattr(wb_repricer_bff_router, "_full_sync_covers_range", lambda *a: (True, {}))
    started: list[dict[str, object]] = []

    def fake_begin_wb_sync(**kwargs):
        started.append(kwargs)
        raise wb_repricer_bff_router.WbSyncAlreadyRunning("wb_sync_busy")

    monkeypatch.setattr("app.routers.wb_repricer_bff.actor_from_request", lambda _request: SimpleNamespace(user_id="user-1", organization_id=1))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_user_wb_token_secret", lambda _user_id: "wb-token")
    monkeypatch.setattr("app.routers.wb_repricer_bff.begin_wb_sync", lambda *args, **kwargs: fake_begin_wb_sync(**kwargs))

    api = client()
    response = api.post(
        "/api/v1/wb-repricer/sku/refresh-ads?period_days=7",
        headers=auth_headers(api, "admin"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "WB_SYNC_RUNNING"
    assert started[0]["force"] is False


def test_manual_sync_run_returns_window_profile_metadata(monkeypatch):
    captured: dict[str, dict[str, object]] = {}
    background_calls: list[tuple[object, dict[str, object]]] = []

    monkeypatch.setattr("app.routers.wb_repricer_bff._request_actor_and_wb_token", lambda _request: (SimpleNamespace(user_id="user-1", organization_id=7), "wb-token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.hydrate_repricer_bff_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff.wb_token_fingerprint", lambda _token: "token-fp")

    def begin_stub(*_args, **kwargs):
        captured["begin"] = kwargs
        return {
            "runId": "wb_sync_manual",
            "state": "running",
            "syncProfile": kwargs.get("sync_profile"),
            "windowKind": kwargs.get("window_kind"),
            "basketsDailyDetail": kwargs.get("baskets_daily_detail"),
        }

    def background_stub(**kwargs):
        captured["background"] = kwargs
        return {"state": "completed"}

    monkeypatch.setattr("app.routers.wb_repricer_bff.begin_wb_sync", begin_stub)
    monkeypatch.setattr("app.routers.wb_repricer_bff._refresh_wb_data_sources_and_flush", background_stub)

    background_tasks = SimpleNamespace(add_task=lambda fn, **kwargs: background_calls.append((fn, kwargs)))
    payload = wb_repricer_bff_router.post_repricer_sync_run(
        object(),
        wb_repricer_bff_router.RepricerSyncRunRequest(periodDays=7, sources=["baskets"]),
        background_tasks,
    )

    assert payload["syncProfile"] == "manual-window"
    assert payload["windowKind"] == "manual"
    assert payload["basketsDailyDetail"] is False
    assert captured["begin"]["sync_profile"] == "manual-window"
    assert background_calls[0][0] is background_stub
    assert background_calls[0][1]["sync_profile"] == "manual-window"
    assert background_calls[0][1]["baskets_include_daily_detail"] is False


def test_manual_cold_full_sync_enqueues_onboarding_task(monkeypatch):
    queued: list[tuple[int, str]] = []
    saved_statuses: list[dict[str, Any]] = []
    threads: list[dict[str, object]] = []

    def status_stub(actor):
        assert actor.organization_id == 7
        saved = saved_statuses[-1]
        return {"state": "queued", "running": True, "syncProfile": saved["sync_profile"], "taskId": saved["task_id"]}

    # Test route orchestration only: do not enter status diagnostics/secret reads.
    monkeypatch.setattr(wb_repricer_bff_router, "_repricer_sync_status_payload", status_stub)
    monkeypatch.setattr(wb_repricer_bff_router, "uuid4", lambda: "synthetic-request-id")

    monkeypatch.setattr("app.routers.wb_repricer_bff._request_actor_and_wb_token", lambda _request: (SimpleNamespace(user_id="user-1", organization_id=7), "wb-token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.is_wb_sync_running", lambda _organization_id: False)
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_wb_sync_status", lambda _organization_id: {})
    monkeypatch.setattr("app.routers.wb_repricer_bff.queue_wb_sync", lambda organization_id, **payload: saved_statuses.append({"organizationId": organization_id, **payload}) or {"state": "queued", **payload})

    class TaskStub:
        @staticmethod
        def delay(organization_id: int, scenario: str):
            raise AssertionError("manual onboarding currently uses the API background runner, not Celery")

    def run_onboarding_stub(
        organization_id: int, scenario: str, wb_token_override: str
    ):
        queued.append((organization_id, scenario))

    class InlineThread:
        def __init__(self, *, target, kwargs, **_thread_options):
            self._target = target
            self._kwargs = kwargs
            threads.append({"target": target, "kwargs": kwargs, **_thread_options})

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr("app.repricer_tasks.sync_wb_onboarding_for_org", TaskStub)
    monkeypatch.setattr("app.repricer_tasks.run_wb_onboarding_for_org", run_onboarding_stub)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.threading",
        SimpleNamespace(Thread=InlineThread),
    )

    payload = wb_repricer_bff_router.post_repricer_sync_run(
        object(),
        wb_repricer_bff_router.RepricerSyncRunRequest(mode="onboarding"),
        SimpleNamespace(add_task=lambda *_args, **_kwargs: None),
    )

    assert queued == [(7, "complete")]
    assert payload["state"] == "queued"
    assert payload["running"] is True
    assert payload["syncProfile"] == "onboarding-full"
    assert payload["taskId"] == "api-bg-onboarding-synthetic-request-id"
    assert saved_statuses[0]["organizationId"] == 7
    assert saved_statuses[0]["trigger"] == "manual-onboarding"
    assert saved_statuses[0]["sync_profile"] == "onboarding-full"
    assert saved_statuses[0]["task_id"] == payload["taskId"]
    assert threads == [{
        "target": run_onboarding_stub,
        "kwargs": {"organization_id": 7, "scenario": "complete", "wb_token_override": "wb-token"},
        "name": "wb-onboarding-7",
        "daemon": True,
    }]


def test_sync_status_includes_schedule_plan(monkeypatch):
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="viewer", organization_id=7),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_wb_sync_status",
        lambda _organization_id: {
            "state": "running",
            "running": True,
            "syncProfile": "sales-funnel-incremental",
            "startedAt": "2026-07-22T10:15:00+00:00",
            "sources": ["baskets"],
            "steps": [{"source": "baskets", "status": "running", "progressPercent": 42}],
        },
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_wb_sync_history",
        lambda *_args, **_kwargs: [
            {
                "type": "sync_finished",
                "syncProfile": "hourly-operational",
                "state": "completed",
                "finishedAt": "2026-07-22T09:00:00+00:00",
            },
            {
                "type": "sync_finished",
                "syncProfile": "sales-funnel-incremental",
                "state": "completed",
                "finishedAt": "2026-07-22T08:30:00+00:00",
            },
        ],
    )

    payload = wb_repricer_bff_router.get_repricer_sync_status(object())

    plan = payload["syncPlan"]
    assert [item["syncProfile"] for item in plan[:2]] == ["onboarding-7d", "onboarding-30d"]
    assert all(item["windowKind"] != "backfill" for item in plan)
    funnel = next(item for item in plan if item["syncProfile"] == "sales-funnel-incremental")
    assert funnel["running"] is True
    assert funnel["state"] == "running"
    assert funnel["lastRunAt"] == "2026-07-22T08:30:00+00:00"
    assert funnel["nextRunAt"] == "2026-07-22T10:30:00+00:00"
    hourly = next(item for item in plan if item["syncProfile"] == "hourly-operational")
    assert hourly["cadenceMinutes"] == 60
    assert hourly["nextRunAt"] == "2026-07-22T10:00:00+00:00"


def test_sync_status_marks_onboarding_plan_queued(monkeypatch):
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="viewer", organization_id=7),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_wb_sync_status",
        lambda _organization_id: {
            "state": "queued",
            "running": False,
            "trigger": "manual-onboarding",
            "syncProfile": "onboarding-full",
            "taskId": "cold-sync-task-1",
            "startedAt": "2026-07-22T20:00:00+00:00",
        },
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_wb_sync_history", lambda *_args, **_kwargs: [])

    payload = wb_repricer_bff_router.get_repricer_sync_status(object())

    onboarding = [item for item in payload["syncPlan"] if item["group"] == "onboarding"]
    assert [(item["syncProfile"], item["state"]) for item in onboarding] == [
        ("onboarding-7d", "queued"),
        ("onboarding-30d", "queued"),
    ]
    assert all(item["running"] is False for item in onboarding)


def test_sync_status_marks_onboarding_partial_without_daily_baskets_detail(monkeypatch):
    monkeypatch.setattr(wb_repricer_bff_router, "datetime", SimpleNamespace(
        now=lambda _tz=None: datetime(2026, 7, 29, 9, tzinfo=timezone.utc),
        fromisoformat=datetime.fromisoformat,
    ))
    caches_by_prefix = {
        "period_stats_": [{"dateFrom": "2026-07-22", "dateTo": "2026-07-28"}],
        "finance_": [{"dateFrom": "2026-07-22", "dateTo": "2026-07-28"}],
        "ads_": [{"dateFrom": "2026-07-22", "dateTo": "2026-07-28"}],
        "baskets_": [
            {
                "dateFrom": "2026-07-22",
                "dateTo": "2026-07-28",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            }
        ],
    }

    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="viewer", organization_id=7),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_wb_sync_status", lambda _organization_id: {"state": "idle", "running": False})
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_wb_sync_history",
        lambda *_args, **_kwargs: [
            {
                "type": "sync_finished",
                "syncProfile": "onboarding-7d",
                "state": "completed",
                "finishedAt": "2026-07-28T08:30:00+00:00",
            }
        ],
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.cached_goods_meta", lambda _organization_id: {"totalCached": 3200})
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_source_cache",
        lambda _organization_id, key, **_kwargs: {"count": 1} if key in {"content_cards", "stocks"} else {},
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_user_wb_token_secret", lambda _user_id: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_organization_wb_token_secret", lambda _organization_id: None)

    payload = wb_repricer_bff_router.get_repricer_sync_status(object())

    onboarding_7d = next(item for item in payload["syncPlan"] if item["syncProfile"] == "onboarding-7d")
    baskets = next(item for item in onboarding_7d["sourceReadiness"] if item["source"] == "baskets")
    assert onboarding_7d["state"] == "partial"
    assert onboarding_7d["cacheReady"] is False
    assert baskets["ready"] is False
    assert baskets["reason"] == "baskets_daily_detail_missing"
    assert baskets["dailyAggregatesDays"] == 0
    assert baskets["requiredDays"] == 7


def test_cache_coverage_marks_complete_partial_and_missing_days(monkeypatch):
    caches_by_prefix = {
        "period_stats_": [
            {"sourceKey": "period_stats_2026-06-10_2026-06-11", "dateFrom": "2026-06-10", "dateTo": "2026-06-11", "fetchedAt": "2026-06-11T08:00:00+00:00"},
        ],
        "finance_": [
            {"sourceKey": "finance_2026-06-10_2026-06-11", "dateFrom": "2026-06-10", "dateTo": "2026-06-11", "fetchedAt": "2026-06-11T08:00:00+00:00"},
        ],
        "ads_": [
            {"sourceKey": "ads_2026-06-10_2026-06-11", "dateFrom": "2026-06-10", "dateTo": "2026-06-11", "fetchedAt": "2026-06-11T08:00:00+00:00"},
        ],
        "baskets_": [
            {
                "sourceKey": "baskets_2026-06-10_2026-06-10",
                "dateFrom": "2026-06-10",
                "dateTo": "2026-06-10",
                "fetchedAt": "2026-06-10T08:00:00+00:00",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 1,
            },
        ],
    }

    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="viewer", organization_id=7),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    payload = wb_repricer_bff_router.get_repricer_cache_coverage(
        object(),
        date_from=date.fromisoformat("2026-06-10"),
        date_to=date.fromisoformat("2026-06-12"),
    )

    days = {item["date"]: item for item in payload["days"]}
    assert days["2026-06-10"]["state"] == "complete"
    assert set(days["2026-06-10"]["availableSources"]) == {"period-stats", "finance", "ads", "baskets"}
    assert days["2026-06-11"]["state"] == "partial"
    assert set(days["2026-06-11"]["missingSources"]) == {"baskets"}
    assert days["2026-06-12"]["state"] == "missing"


def test_cache_coverage_does_not_mark_baskets_ready_without_daily_detail(monkeypatch):
    caches_by_prefix = {
        "period_stats_": [
            {"sourceKey": "period_stats_2026-06-25_2026-07-24", "dateFrom": "2026-06-25", "dateTo": "2026-07-24", "fetchedAt": "2026-07-24T23:40:00+00:00"},
        ],
        "finance_": [
            {"sourceKey": "finance_2026-06-25_2026-07-24", "dateFrom": "2026-06-25", "dateTo": "2026-07-24", "fetchedAt": "2026-07-24T23:40:00+00:00"},
        ],
        "ads_": [
            {"sourceKey": "ads_2026-06-25_2026-07-24", "dateFrom": "2026-06-25", "dateTo": "2026-07-24", "fetchedAt": "2026-07-24T23:40:00+00:00"},
        ],
        "baskets_": [
            {
                "sourceKey": "baskets_2026-06-25_2026-07-24",
                "dateFrom": "2026-06-25",
                "dateTo": "2026-07-24",
                "fetchedAt": "2026-07-24T23:40:00+00:00",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            },
        ],
    }

    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="viewer", organization_id=7),
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    payload = wb_repricer_bff_router.get_repricer_cache_coverage(
        object(),
        date_from=date.fromisoformat("2026-07-18"),
        date_to=date.fromisoformat("2026-07-18"),
    )

    day = payload["days"][0]
    assert day["state"] == "partial"
    assert "baskets" not in day["availableSources"]
    assert "baskets" in day["missingSources"]


def test_full_sync_coverage_falls_back_to_period_caches_when_status_has_no_range(monkeypatch):
    caches_by_prefix = {
        "period_stats_": [{"dateFrom": "2026-06-15", "dateTo": "2026-07-15"}],
        "finance_": [{"dateFrom": "2026-06-15", "dateTo": "2026-07-15"}],
        "ads_": [{"dateFrom": "2026-06-15", "dateTo": "2026-07-15"}],
        "baskets_": [{"dateFrom": "2026-06-15", "dateTo": "2026-07-15", "dailyAggregatesDays": 31}],
    }

    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_wb_sync_status",
        lambda _organization_id: {"state": "queued", "syncProfile": "onboarding-full", "dateFrom": None, "dateTo": None},
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    covered, source_range = wb_repricer_bff_router._full_sync_covers_range(
        7,
        date.fromisoformat("2026-06-15"),
        date.fromisoformat("2026-07-15"),
    )

    assert covered is True
    assert source_range == {"from": "2026-06-15", "to": "2026-07-15"}


def test_sync_profile_window_ready_requires_daily_baskets_detail(monkeypatch):
    caches_by_prefix = {
        "period_stats_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "finance_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "ads_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "baskets_": [
            {
                "dateFrom": "2026-06-29",
                "dateTo": "2026-07-28",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            }
        ],
    }
    profile = next(
        item
        for item in wb_repricer_bff_router.onboarding_sync_profiles(as_of=date(2026, 7, 28))
        if item.profile_id == "onboarding-30d"
    )

    monkeypatch.setattr("app.routers.wb_repricer_bff.cached_goods_meta", lambda _organization_id: {"totalCached": 3201})
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_source_cache",
        lambda _organization_id, key, **_kwargs: {"count": 1} if key in {"content_cards", "stocks"} else {},
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    assert wb_repricer_bff_router._sync_profile_cache_window_ready(7, profile) is False

    caches_by_prefix["baskets_"][0]["dailyDetailStatus"] = "fetched"
    caches_by_prefix["baskets_"][0]["dailyAggregatesDays"] = 30

    assert wb_repricer_bff_router._sync_profile_cache_window_ready(7, profile) is True


def test_sync_profile_window_ready_rejects_shifted_daily_baskets_dates(monkeypatch):
    shifted_dates = [f"2026-06-{day:02d}" for day in range(1, 31)]
    caches_by_prefix = {
        "period_stats_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "finance_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "ads_": [{"dateFrom": "2026-06-29", "dateTo": "2026-07-28"}],
        "baskets_": [
            {
                "dateFrom": "2026-06-29",
                "dateTo": "2026-07-28",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 30,
                "dailyAggregateDates": shifted_dates,
            }
        ],
    }
    profile = next(
        item
        for item in wb_repricer_bff_router.onboarding_sync_profiles(as_of=date(2026, 7, 28))
        if item.profile_id == "onboarding-30d"
    )

    monkeypatch.setattr("app.routers.wb_repricer_bff.cached_goods_meta", lambda _organization_id: {"totalCached": 3201})
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_source_cache",
        lambda _organization_id, key, **_kwargs: {"count": 1} if key in {"content_cards", "stocks"} else {},
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    assert wb_repricer_bff_router._sync_profile_cache_window_ready(7, profile) is False


def test_full_sync_coverage_ignores_running_aggregate_only_baskets_when_week_cache_exists(monkeypatch):
    caches_by_prefix = {
        "period_stats_": [
            {"sourceKey": "period_stats_2026-07-21_2026-07-27", "dateFrom": "2026-07-21", "dateTo": "2026-07-27"},
            {"sourceKey": "period_stats_2026-06-28_2026-07-27", "dateFrom": "2026-06-28", "dateTo": "2026-07-27"},
        ],
        "finance_": [
            {"sourceKey": "finance_2026-07-21_2026-07-27", "dateFrom": "2026-07-21", "dateTo": "2026-07-27"},
            {"sourceKey": "finance_2026-06-28_2026-07-27", "dateFrom": "2026-06-28", "dateTo": "2026-07-27"},
        ],
        "ads_": [
            {"sourceKey": "ads_2026-07-21_2026-07-27", "dateFrom": "2026-07-21", "dateTo": "2026-07-27"},
            {"sourceKey": "ads_2026-06-28_2026-07-27", "dateFrom": "2026-06-28", "dateTo": "2026-07-27"},
        ],
        "baskets_": [
            {
                "sourceKey": "baskets_2026-06-28_2026-07-27",
                "dateFrom": "2026-06-28",
                "dateTo": "2026-07-27",
                "dailyDetailStatus": "deferred",
                "dailyAggregatesDays": 0,
            },
            {
                "sourceKey": "baskets_2026-07-21_2026-07-27",
                "dateFrom": "2026-07-21",
                "dateTo": "2026-07-27",
                "dailyDetailStatus": "fetched",
                "dailyAggregatesDays": 7,
            },
        ],
    }

    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.get_wb_sync_status",
        lambda _organization_id: {
            "state": "running",
            "running": True,
            "periodDays": 30,
            "dateFrom": "2026-06-28",
            "dateTo": "2026-07-27",
            "periodCacheSuffix": "2026-06-28_2026-07-27",
        },
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_source_cache_ranges_by_prefix",
        lambda _organization_id, prefix, **_kwargs: caches_by_prefix.get(prefix, []),
    )

    covered, source_range = wb_repricer_bff_router._full_sync_covers_range(
        7,
        date.fromisoformat("2026-07-21"),
        date.fromisoformat("2026-07-27"),
    )

    assert covered is True
    assert source_range == {"from": "2026-07-21", "to": "2026-07-27"}


def test_wb_sync_sources_filter_runs_only_requested_sources(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    calls: list[str] = []

    def unexpected_source(*_args, **_kwargs):
        raise AssertionError("unrequested source must not run")

    monkeypatch.setattr("app.repricer_sync.fetch_catalog_goods_page", unexpected_source)
    monkeypatch.setattr("app.repricer_sync._fetch_content_cards", unexpected_source)
    monkeypatch.setattr("app.repricer_sync.list_promotions", unexpected_source)
    monkeypatch.setattr("app.repricer_sync.fetch_stock_aggregates", unexpected_source)
    monkeypatch.setattr("app.repricer_sync.fetch_period_stats_aggregates", unexpected_source)
    monkeypatch.setattr("app.repricer_sync.fetch_baskets_aggregates", unexpected_source)
    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 123456}])
    monkeypatch.setattr(
        "app.repricer_sync.fetch_finance_report_aggregates",
        lambda *_args, **_kwargs: calls.append("finance") or {"aggregates": {"123456": {}}, "count": 1},
    )
    monkeypatch.setattr(
        "app.repricer_sync.fetch_ads_spend_aggregates",
        lambda *_args, **_kwargs: calls.append("ads") or {"aggregates": {"123456": {}}, "count": 1, "campaignCount": 1},
    )
    monkeypatch.setattr(
        "app.repricer_sync.save_source_cache",
        lambda _organization_id, key, payload, **_kwargs: calls.append(f"save:{key}") or payload,
    )

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=7,
        execute_lock=False,
        sources=["finance"],
    )

    assert [step["source"] for step in result["steps"]] == ["finance"]
    assert result["state"] == "completed"
    assert calls == ["finance", "save:finance_7"]


def test_wb_sync_ads_step_reports_smooth_campaign_progress(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    observed: list[dict] = []

    def fake_ads(*_args, **kwargs):
        progress_callback = kwargs["progress_callback"]
        progress_callback({"phase": "campaigns", "campaignsProcessed": 0, "campaignsTotal": 120, "requestsCompleted": 0, "requestsTotal": 3})
        progress_callback({"phase": "fullstats", "campaignsProcessed": 50, "campaignsTotal": 120, "requestsCompleted": 1, "requestsTotal": 3})
        progress_callback({"phase": "fullstats", "campaignsProcessed": 100, "campaignsTotal": 120, "requestsCompleted": 2, "requestsTotal": 3})
        progress_callback({"phase": "fullstats", "campaignsProcessed": 120, "campaignsTotal": 120, "requestsCompleted": 3, "requestsTotal": 3})
        return {"aggregates": {}, "count": 0, "campaignCount": 120}

    monkeypatch.setattr("app.repricer_sync.fetch_ads_spend_aggregates", fake_ads)
    monkeypatch.setattr("app.repricer_sync.save_source_cache", lambda _organization_id, _key, payload, **_kwargs: payload)

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=7,
        execute_lock=False,
        sources=["ads"],
        _progress_callback=observed.append,
        _parallelize=False,
    )

    running_ads = [item for item in observed if item.get("source") == "ads" and item.get("status") == "running"]
    assert [item["progressPercent"] for item in running_ads] == [5, 5, 35, 65, 95]
    assert running_ads[-1]["progressCurrent"] == 120
    assert running_ads[-1]["progressTotal"] == 120
    assert "120 из 120 кампаний" in running_ads[-1]["message"]
    assert result["steps"][0]["progressPercent"] == 100


def test_wb_sync_writes_ads_cache_before_advertising_shadow(monkeypatch):
    import app.repricer_sync as sync

    calls: list[tuple[str, str]] = []
    ads_payload = {
        "aggregates": {},
        "dailyAggregates": {},
        "totals": {},
        "count": 0,
        "campaignCount": 0,
        "dateFrom": "2026-08-17",
        "dateTo": "2026-08-23",
    }
    monkeypatch.setattr(
        sync, "fetch_ads_spend_aggregates", lambda *_args, **_kwargs: ads_payload
    )
    monkeypatch.setattr(
        sync,
        "save_source_cache",
        lambda _organization_id, key, value, **_kwargs: calls.append(
            ("legacy", key)
        )
        or value,
    )
    monkeypatch.setattr(
        sync,
        "shadow_ingest_legacy_advertising_payload",
        lambda *_args, **_kwargs: calls.append(("canonical", "ads_fullstats"))
        or {"state": "ready"},
    )

    result = sync.refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        date_from=date(2026, 8, 17),
        date_to=date(2026, 8, 23),
        execute_lock=False,
        sources=["ads"],
        _parallelize=False,
    )

    assert calls == [
        ("legacy", "ads_2026-08-17_2026-08-23"),
        ("canonical", "ads_fullstats"),
    ]
    assert result["steps"][0]["canonicalAdvertisingSnapshot"] == {
        "state": "ready"
    }


def test_wb_sync_runs_four_independent_sources_in_parallel_and_waits_for_goods(monkeypatch):
    monkeypatch.setattr("app.platform.catalog.photos.save_missing_catalog_photos", lambda *_args: {"saved": 0})
    monkeypatch.setattr("app.repricer_sync.browser_prices_selected", lambda _: False)
    monkeypatch.setattr("app.repricer_sync.fetch_commission_tariffs", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(repricer_bff_module, "fetch_commission_tariffs", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(wb_repricer_bff_router, "_build_repricer_sku_snapshot", lambda *_args, **_kwargs: {"total": 0})
    import threading
    import time

    from app.repricer_sync import refresh_wb_data_sources

    state_lock = threading.Lock()
    four_running = threading.Event()
    goods_done = threading.Event()
    active = 0
    peak_active = 0
    dependent_started: list[str] = []

    def overlap(name, result):
        def run(*_args, **_kwargs):
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
                if active >= 4:
                    four_running.set()
            four_running.wait(timeout=0.5)
            time.sleep(0.01)
            with state_lock:
                active -= 1
            return result

        return run

    monkeypatch.setattr(
        "app.repricer_sync.fetch_catalog_goods_page",
        overlap("goods", {"goods": [], "wbRequestId": "goods-request"}),
    )
    monkeypatch.setattr("app.repricer_sync._fetch_content_cards", overlap("content", []))
    monkeypatch.setattr("app.repricer_sync.list_promotions", overlap("promotions", []))
    monkeypatch.setattr("app.repricer_sync.fetch_stock_aggregates", overlap("stocks", {}))
    monkeypatch.setattr("app.repricer_sync.fetch_period_stats_aggregates", overlap("period-stats", {"aggregates": {}}))
    monkeypatch.setattr("app.repricer_sync.fetch_ads_spend_aggregates", overlap("ads", {"aggregates": {}, "count": 0}))
    monkeypatch.setattr("app.repricer_sync.fetch_external_spp_prices", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 123}])
    monkeypatch.setattr(
        "app.repricer_sync._mark_new_goods_as_warmup",
        lambda *_args, **_kwargs: goods_done.set() or 0,
    )

    def finance(*_args, **_kwargs):
        assert goods_done.is_set()
        dependent_started.append("finance")
        return {"aggregates": {}, "count": 0}

    def baskets(*_args, **_kwargs):
        assert goods_done.is_set()
        dependent_started.append("baskets")
        return {"aggregates": {}, "count": 0}

    monkeypatch.setattr("app.repricer_sync.fetch_finance_report_aggregates", finance)
    monkeypatch.setattr("app.repricer_sync.fetch_baskets_aggregates", baskets)
    monkeypatch.setattr("app.repricer_sync.replace_goods_pages", lambda **_kwargs: {"totalCached": 0})
    monkeypatch.setattr("app.repricer_sync.save_source_cache", lambda _organization_id, _key, payload, **_kwargs: payload)

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=7,
        execute_lock=False,
    )

    assert peak_active == 4
    assert sorted(dependent_started) == ["baskets", "finance"]
    assert [step["source"] for step in result["steps"]] == [
        "goods",
        "content",
        "promotions",
        "stocks",
        "period-stats",
        "finance",
        "ads",
        "baskets",
        "sku-snapshot",
    ]
    assert result["state"] == "completed", [
        (step["source"], step.get("error"))
        for step in result["steps"] if step["status"] not in {"ok", "skipped"}
    ]


def test_regular_wb_sync_defers_daily_baskets_detail(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    calls: list[dict] = []
    observed: list[dict] = []
    saved: dict[str, dict] = {}
    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": value} for value in range(1, 1002)])
    monkeypatch.setattr("app.repricer_sync.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.save_source_cache", lambda _organization_id, key, payload: saved.setdefault(key, payload))
    monkeypatch.setattr("app.repricer_sync._release_warmup_goods_by_baskets", lambda *_args: 0)

    def fetch(*_args, **kwargs):
        calls.append(kwargs)
        kwargs["progress_callback"]({"phase": "completed", "requestsCompleted": 1, "requestsTotal": 2, "processedNmIds": 1000, "totalNmIds": 1001})
        return {"aggregates": {}, "count": 0, "requestedNmIds": 1001, "matchedNmIds": 0}

    def fetch_detail(*_args, **kwargs):
        kwargs["progress_callback"]({"phase": "completed", "dayIndex": 1, "daysTotal": 7, "batch": 1, "batchesTotal": 2, "requestsCompleted": 1, "requestsTotal": 14})
        return {"dailyAggregates": {}, "requestsCompleted": 14, "requestsTotal": 14}

    monkeypatch.setattr("app.repricer_sync.fetch_baskets_aggregates", fetch)
    monkeypatch.setattr("app.repricer_sync.fetch_baskets_daily_detail", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("regular sync must not fetch daily detail")))
    result = refresh_wb_data_sources(organization_id=1, wb_token="token", period_days=7, sources=["baskets"], execute_lock=False, _progress_callback=observed.append)

    assert calls[0]["include_daily"] is False
    assert saved["baskets_7"]["dailyDetailStatus"] == "deferred"
    assert not any(item.get("source") == "baskets" and item.get("phase") == "daily-detail" for item in observed)
    assert any(item.get("source") == "baskets" and item.get("progressCurrent") == 1000 for item in observed)
    assert result["state"] == "completed"


def test_aggregate_baskets_refresh_preserves_existing_daily_detail(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    cache_key = "baskets_2026-07-07_2026-08-05"
    daily = {
        (date(2026, 7, 7) + timedelta(days=offset)).isoformat(): {"101": {"addToCartCount": offset + 1}}
        for offset in range(30)
    }
    source_state: dict[str, dict] = {
        cache_key: {
            "dateFrom": "2026-07-07",
            "dateTo": "2026-08-05",
            "dailyAggregates": daily,
            "dailyAggregatesDays": 30,
            "dailyDetailStatus": "fetched",
            "dailyDetailFetchedAt": "2026-08-06T08:00:00+00:00",
            "chunks": [{"type": "daily", "status": "done", "date": day, "chunkIndex": 0} for day in daily],
        }
    }

    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 101}])
    monkeypatch.setattr("app.repricer_sync.get_source_cache", lambda _organization_id, key, **_kwargs: deepcopy(source_state.get(key) or {}))

    def save_cache(_organization_id, key, payload):
        source_state[key] = deepcopy(payload)
        return payload

    monkeypatch.setattr("app.repricer_sync.save_source_cache", save_cache)
    monkeypatch.setattr("app.repricer_sync._release_warmup_goods_by_baskets", lambda *_args: 0)
    monkeypatch.setattr(
        "app.repricer_sync.fetch_baskets_aggregates",
        lambda *_args, **_kwargs: {
            "aggregates": {"101": {"addToCartCount": 99}},
            "count": 1,
            "requestedNmIds": 1,
            "matchedNmIds": 1,
            "dateFrom": "2026-07-07",
            "dateTo": "2026-08-05",
        },
    )
    monkeypatch.setattr("app.repricer_sync.fetch_baskets_daily_detail", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("aggregate refresh must not refetch daily detail")))

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=30,
        date_from=date(2026, 7, 7),
        date_to=date(2026, 8, 5),
        sources=["baskets"],
        execute_lock=False,
        baskets_include_daily_detail=False,
    )

    assert result["state"] == "completed"
    assert source_state[cache_key]["aggregates"]["101"]["addToCartCount"] == 99
    assert source_state[cache_key]["dailyDetailStatus"] == "fetched"
    assert source_state[cache_key]["dailyAggregates"] == daily
    assert source_state[cache_key]["dailyAggregatesDays"] == 30
    assert source_state[cache_key]["dailyDetailPreservedBy"] == "aggregate_refresh"


def test_wb_sync_can_opt_into_daily_baskets_detail(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    detail_calls: list[dict] = []
    observed: list[dict] = []
    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": value} for value in range(1, 1002)])
    monkeypatch.setattr("app.repricer_sync.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.save_source_cache", lambda _organization_id, _key, payload: payload)
    monkeypatch.setattr("app.repricer_sync._release_warmup_goods_by_baskets", lambda *_args: 0)
    monkeypatch.setattr(
        "app.repricer_sync.fetch_baskets_aggregates",
        lambda *_args, **kwargs: {"aggregates": {}, "count": 0, "requestedNmIds": 1001, "matchedNmIds": 0},
    )

    def fetch_detail(*args, **kwargs):
        detail_calls.append(kwargs)
        return repricer_bff_module.fetch_baskets_daily_detail(*args, **kwargs)

    monkeypatch.setattr("app.repricer_sync.fetch_baskets_daily_detail", fetch_detail)
    monkeypatch.setattr(
        repricer_bff_module,
        "_request_or_raise_sales_funnel_products",
        lambda *_args, **_kwargs: {"data": {"products": []}},
    )

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=7,
        date_from=date(2026, 7, 22),
        date_to=date(2026, 7, 28),
        sources=["baskets"],
        execute_lock=False,
        baskets_include_daily_detail=True,
        _progress_callback=observed.append,
    )

    assert len(detail_calls) == 1
    assert any(
        item.get("source") == "baskets" and item.get("status") == "running"
        and item.get("phase") == "completed"
        and item.get("progressCurrent") == item.get("progressTotal") == 7
        for item in observed
    )
    assert result["state"] == "completed"


def test_wb_sync_persists_partial_daily_baskets_detail_on_failure(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    cache_key = "baskets_2026-06-01_2026-06-03"
    source_state: dict[str, dict] = {
        cache_key: {
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-03",
            "dailyAggregates": {"2026-06-01": {"101": {"openCardCount": 7}}},
        }
    }
    saved_snapshots: list[dict] = []
    observed: list[dict] = []
    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 101}])
    monkeypatch.setattr("app.repricer_sync.get_source_cache", lambda _organization_id, key, **_kwargs: deepcopy(source_state.get(key) or {}))

    def save_cache(_organization_id, key, payload):
        source_state[key] = deepcopy(payload)
        if key == cache_key:
            saved_snapshots.append(deepcopy(payload))
        return payload

    monkeypatch.setattr("app.repricer_sync.save_source_cache", save_cache)
    monkeypatch.setattr("app.repricer_sync._release_warmup_goods_by_baskets", lambda *_args: 0)
    monkeypatch.setattr(
        "app.repricer_sync.fetch_baskets_aggregates",
        lambda *_args, **_kwargs: {
            "aggregates": {"101": {"openCardCount": 10}},
            "count": 1,
            "requestedNmIds": 1,
            "matchedNmIds": 1,
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-03",
        },
    )

    def fetch_detail(*_args, **kwargs):
        assert kwargs["existing_daily_aggregates"] == {"2026-06-01": {"101": {"openCardCount": 7}}}
        kwargs["day_completed_callback"](
            "2026-06-02",
            {"101": {"openCardCount": 3}},
            {"requestsCompleted": 2, "requestsTotal": 3},
        )
        raise RuntimeError("WB daily baskets timeout")

    monkeypatch.setattr("app.repricer_sync.fetch_baskets_daily_detail", fetch_detail)

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=3,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 3),
        sources=["baskets"],
        execute_lock=False,
        baskets_include_daily_detail=True,
        _progress_callback=observed.append,
    )

    latest = saved_snapshots[-1]
    assert result["state"] == "partial"
    assert result["steps"][0]["status"] == "partial"
    assert latest["dailyDetailStatus"] == "partial"
    assert latest["dailyAggregatesDays"] == 2
    assert latest["dailyAggregates"]["2026-06-01"]["101"]["openCardCount"] == 7
    assert latest["dailyAggregates"]["2026-06-02"]["101"]["openCardCount"] == 3


def test_wb_sync_seeds_30d_baskets_detail_from_recent_7d_cache(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    cache_30 = "baskets_2026-07-06_2026-08-04"
    cache_7 = "baskets_2026-07-29_2026-08-04"
    recent_days = [date(2026, 7, 29) + timedelta(days=offset) for offset in range(7)]
    source_state: dict[str, dict] = {
        cache_7: {
            "dateFrom": "2026-07-29",
            "dateTo": "2026-08-04",
            "dailyAggregates": {day.isoformat(): {"101": {"addToCartCount": 1}} for day in recent_days},
            "chunks": [{"type": "daily", "status": "done", "date": day.isoformat(), "chunkIndex": 0} for day in recent_days],
        }
    }
    detail_calls: list[dict] = []

    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 101}])
    monkeypatch.setattr("app.repricer_sync.get_source_cache", lambda _organization_id, key, **_kwargs: deepcopy(source_state.get(key) or {}))

    def save_cache(_organization_id, key, payload):
        source_state[key] = deepcopy(payload)
        return payload

    monkeypatch.setattr("app.repricer_sync.save_source_cache", save_cache)
    monkeypatch.setattr("app.repricer_sync._release_warmup_goods_by_baskets", lambda *_args: 0)
    monkeypatch.setattr(
        "app.repricer_sync.fetch_baskets_aggregates",
        lambda *_args, **_kwargs: {
            "aggregates": {"101": {"addToCartCount": 30}},
            "count": 1,
            "requestedNmIds": 1,
            "matchedNmIds": 1,
            "dateFrom": "2026-07-06",
            "dateTo": "2026-08-04",
        },
    )

    def fetch_detail(*_args, **kwargs):
        detail_calls.append(kwargs)
        daily = deepcopy(kwargs["existing_daily_aggregates"])
        for offset in range(23):
            day = date(2026, 7, 6) + timedelta(days=offset)
            daily[day.isoformat()] = {"101": {"addToCartCount": 2}}
        return {
            "dailyAggregates": daily,
            "requestsCompleted": 30,
            "requestsTotal": 30,
            "dateFrom": "2026-07-06",
            "dateTo": "2026-08-04",
            "periodDays": 30,
            "chunks": kwargs["existing_chunks"],
        }

    monkeypatch.setattr("app.repricer_sync.fetch_baskets_daily_detail", fetch_detail)

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=30,
        date_from=date(2026, 7, 6),
        date_to=date(2026, 8, 4),
        sources=["baskets"],
        execute_lock=False,
        baskets_include_daily_detail=True,
    )

    assert result["state"] == "completed"
    assert len(detail_calls) == 1
    assert set(detail_calls[0]["existing_daily_aggregates"]) == {day.isoformat() for day in recent_days}
    assert source_state[cache_30]["dailyAggregatesDays"] == 30
    assert source_state[cache_30]["dailyAggregates"]["2026-08-04"]["101"]["addToCartCount"] == 1


def test_wb_sync_seeds_baskets_detail_from_overlapping_recent_7d_cache(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    cache_30 = "baskets_2026-07-07_2026-08-05"
    cache_7 = "baskets_2026-07-29_2026-08-04"
    recent_days = [date(2026, 7, 29) + timedelta(days=offset) for offset in range(7)]
    source_state: dict[str, dict] = {
        cache_7: {
            "dateFrom": "2026-07-29",
            "dateTo": "2026-08-04",
            "dailyAggregates": {day.isoformat(): {"101": {"addToCartCount": 1}} for day in recent_days},
            "chunks": [{"type": "daily", "status": "done", "date": day.isoformat(), "chunkIndex": 0} for day in recent_days],
        }
    }
    detail_calls: list[dict] = []

    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 101}])
    monkeypatch.setattr("app.repricer_sync.get_source_cache", lambda _organization_id, key, **_kwargs: deepcopy(source_state.get(key) or {}))
    monkeypatch.setattr("app.repricer_sync.list_source_cache_ranges_by_prefix", lambda _organization_id, prefix, **_kwargs: [{"sourceKey": cache_7, **source_state[cache_7]}] if prefix == "baskets_" else [], raising=False)
    def save_cache(_organization_id, key, payload):
        source_state[key] = deepcopy(payload)
        return payload

    monkeypatch.setattr("app.repricer_sync.save_source_cache", save_cache)
    monkeypatch.setattr("app.repricer_sync._release_warmup_goods_by_baskets", lambda *_args: 0)
    monkeypatch.setattr(
        "app.repricer_sync.fetch_baskets_aggregates",
        lambda *_args, **_kwargs: {
            "aggregates": {"101": {"addToCartCount": 30}},
            "count": 1,
            "requestedNmIds": 1,
            "matchedNmIds": 1,
            "dateFrom": "2026-07-07",
            "dateTo": "2026-08-05",
        },
    )

    def fetch_detail(*_args, **kwargs):
        detail_calls.append(kwargs)
        daily = deepcopy(kwargs["existing_daily_aggregates"])
        for offset in range(23):
            day = date(2026, 7, 7) + timedelta(days=offset)
            daily.setdefault(day.isoformat(), {"101": {"addToCartCount": 2}})
        daily["2026-08-05"] = {"101": {"addToCartCount": 3}}
        return {
            "dailyAggregates": daily,
            "requestsCompleted": 24,
            "requestsTotal": 31,
            "dateFrom": "2026-07-07",
            "dateTo": "2026-08-05",
            "periodDays": 30,
            "chunks": kwargs["existing_chunks"],
        }

    monkeypatch.setattr("app.repricer_sync.fetch_baskets_daily_detail", fetch_detail)

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=30,
        date_from=date(2026, 7, 7),
        date_to=date(2026, 8, 5),
        sources=["baskets"],
        execute_lock=False,
        baskets_include_daily_detail=True,
    )

    assert result["state"] == "completed"
    assert set(detail_calls[0]["existing_daily_aggregates"]) == {day.isoformat() for day in recent_days}
    assert source_state[cache_30]["dailyAggregates"]["2026-08-04"]["101"]["addToCartCount"] == 1


def test_baskets_daily_detail_checkpoints_each_chunk(monkeypatch):
    from app import repricer_bff as repricer_bff_module

    requests: list[dict] = []
    chunk_events: list[tuple[str, int, dict, dict]] = []

    def request(_client, request, **_kwargs):
        body = request.jsonBody
        requests.append(body)
        nm_ids = body["nmIds"]
        day = body["selectedPeriod"]["start"]
        return {
            "data": {
                "products": [
                    {
                        "product": {"nmId": nm_id},
                        "statistic": {"selected": {"cartCount": nm_id, "orderCount": 1}},
                    }
                    for nm_id in nm_ids
                ]
            }
        }

    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", request)

    payload = repricer_bff_module.fetch_baskets_daily_detail(
        "complete",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        nm_ids=list(range(1, 1003)),
        chunk_completed_callback=lambda day, chunk_index, rows, progress: chunk_events.append((day, chunk_index, deepcopy(rows), deepcopy(progress))),
    )

    assert [event[1] for event in chunk_events] == [0, 1]
    assert chunk_events[0][0] == "2026-06-01"
    assert set(chunk_events[0][2].keys()) == {str(value) for value in range(1, 1001)}
    assert set(chunk_events[1][2].keys()) == {str(value) for value in range(1, 1003)}
    assert payload["chunks"] == [
        {"type": "daily", "date": "2026-06-01", "chunkIndex": 0, "status": "done", "attempts": 1},
        {"type": "daily", "date": "2026-06-01", "chunkIndex": 1, "status": "done", "attempts": 1},
    ]
    assert len(requests) == 2


def test_baskets_daily_detail_resumes_missing_chunks_within_partial_day(monkeypatch):
    from app import repricer_bff as repricer_bff_module

    requested_batches: list[list[int]] = []

    def request(_client, request, **_kwargs):
        nm_ids = request.jsonBody["nmIds"]
        requested_batches.append(list(nm_ids))
        return {
            "data": {
                "products": [
                    {
                        "product": {"nmId": nm_id},
                        "statistic": {"selected": {"cartCount": nm_id}},
                    }
                    for nm_id in nm_ids
                ]
            }
        }

    monkeypatch.setattr(repricer_bff_module, "_request_or_raise_sales_funnel_products", request)

    payload = repricer_bff_module.fetch_baskets_daily_detail(
        "complete",
        date_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
        nm_ids=list(range(1, 1003)),
        existing_daily_aggregates={"2026-06-01": {str(value): {"cartCount": value} for value in range(1, 1001)}},
        existing_chunks=[{"type": "daily", "date": "2026-06-01", "chunkIndex": 0, "status": "done", "attempts": 1}],
    )

    assert requested_batches == [list(range(1001, 1003))]
    assert payload["dailyAggregates"]["2026-06-01"]["1"]["cartCount"] == 1
    assert payload["dailyAggregates"]["2026-06-01"]["1002"]["cartCount"] == 1002
    assert {item["chunkIndex"] for item in payload["chunks"] if item["status"] == "done"} == {0, 1}


def test_wb_sync_marks_long_sales_funnel_retry_after_as_paused(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources
    from app.repricer_bff import WbSalesFunnelDeferred

    saved: dict[str, dict] = {}

    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 101}])
    monkeypatch.setattr("app.repricer_sync.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.save_source_cache", lambda _organization_id, key, payload: saved.__setitem__(key, deepcopy(payload)) or payload)
    monkeypatch.setattr("app.repricer_sync._release_warmup_goods_by_baskets", lambda *_args: 0)
    monkeypatch.setattr(
        "app.repricer_sync.fetch_baskets_aggregates",
        lambda *_args, **_kwargs: {
            "aggregates": {"101": {"cartCount": 10}},
            "count": 1,
            "requestedNmIds": 1,
            "matchedNmIds": 1,
        },
    )
    monkeypatch.setattr(
        "app.repricer_sync.fetch_baskets_daily_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            WbSalesFunnelDeferred(retry_after_seconds=240, message="WB asked for a long retry")
        ),
    )

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=7,
        sources=["baskets"],
        execute_lock=False,
        baskets_include_daily_detail=True,
    )

    assert result["state"] == "partial"
    assert result["steps"][0]["status"] == "paused"
    assert result["steps"][0]["reason"] == "wb_rate_limited_long_retry"
    assert result["steps"][0]["retryAfterSeconds"] == 240
    assert saved["baskets_7"]["dailyDetailStatus"] == "partial"
    assert saved["baskets_7"]["aggregates"]["101"]["cartCount"] == 10


def test_wb_sync_marks_only_new_goods_as_warmup():
    from app import repricer_bff as repricer_bff_module
    from app import repricer_sync

    previous_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
    previous_settings = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    repricer_bff_module.SKU_META_OVERRIDES.clear()
    repricer_bff_module.ALGORITHM_SETTINGS_STATE.update({"warmupDays": 12})
    try:
        first_count = repricer_sync._mark_new_goods_as_warmup(
            [],
            [{"vendorCode": "OLD-1", "nmID": 1001}],
        )
        marked_count = repricer_sync._mark_new_goods_as_warmup(
            [{"vendorCode": "OLD-1", "nmID": 1001}],
            [
                {"vendorCode": "OLD-1", "nmID": 1001},
                {"vendorCode": "RENAMED-OLD", "nmID": 1001},
                {"vendorCode": "NEW-1", "nmID": 2002},
            ],
        )
    finally:
        next_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_META_OVERRIDES.update(previous_meta)
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_settings)

    assert first_count == 0
    assert marked_count == 1
    assert next_meta["NEW-1"]["status"] == "warmup"
    assert next_meta["NEW-1"]["assignmentSource"] == "sync_new_goods"
    assert next_meta["NEW-1"]["warmupDaysLeft"] == 12
    assert "RENAMED-OLD" not in next_meta


def test_wb_sync_external_spp_fetches_batches_and_waits_between_requests(monkeypatch):
    from app import repricer_sync

    requested: list[tuple[str, str]] = []
    sleeps: list[float] = []
    nm_ids = list(range(1, 202))
    progress_events: list[dict[str, object]] = []

    class ResponseStub:
        def __init__(self, prices: list[dict[str, object]]) -> None:
            self.status_code = 200
            self._prices = prices

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"prices": self._prices}

    class ClientStub:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, params, headers):
            requested.append((url, params["nm"]))
            prices = [
                {"nm": int(value), "product": 1000 + int(value), "status": "ok"}
                for value in str(params["nm"]).split(";")
            ]
            return ResponseStub(prices)

    monkeypatch.setattr("app.repricer_sync.httpx.Client", lambda **_kwargs: ClientStub())

    result = repricer_sync.fetch_external_spp_prices(
        nm_ids,
        api_token="token",
        api_base_url="https://41-spp.wbcon.su",
        sleep_fn=sleeps.append,
        progress_callback=progress_events.append,
    )

    assert requested == [
        ("https://41-spp.wbcon.su/prices", ";".join(str(item) for item in range(1, 101))),
        ("https://41-spp.wbcon.su/prices", ";".join(str(item) for item in range(101, 201))),
        ("https://41-spp.wbcon.su/prices", "201"),
    ]
    assert sleeps == [10.0, 10.0]
    assert result[1] == 100_100
    assert result[201] == 120_100
    assert progress_events == [
        {"batchCurrent": 1, "batchTotal": 3, "itemsCompleted": 100, "itemsTotal": 201},
        {"batchCurrent": 2, "batchTotal": 3, "itemsCompleted": 200, "itemsTotal": 201},
        {"batchCurrent": 3, "batchTotal": 3, "itemsCompleted": 201, "itemsTotal": 201},
    ]


@pytest.mark.parametrize("browser_selected", [False, True])
def test_wb_sync_goods_step_saves_external_spp_price_as_buyer_price(monkeypatch, browser_selected):
    from app.repricer_sync import refresh_wb_data_sources
    monkeypatch.setattr("app.repricer_sync.browser_prices_selected", lambda _: browser_selected)

    saved_goods: list[list[dict[str, object]]] = []

    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [])
    monkeypatch.setattr(
        "app.repricer_sync.fetch_catalog_goods_page",
        lambda *_args, **_kwargs: {
            "goods": [
                {
                    "vendorCode": "SKU-1",
                    "nmID": 123,
                    "sizes": [{"price": 220_000, "discountedPrice": 220_000}],
                }
            ],
            "wbRequestId": "wb-goods-1",
        },
    )
    external_calls = []
    def external_prices(*args, **kwargs):
        external_calls.append(args)
        return {123: 149_600}
    monkeypatch.setattr("app.repricer_sync.fetch_external_spp_prices", external_prices)
    monkeypatch.setattr(
        "app.repricer_sync.replace_goods_pages",
        lambda **kwargs: saved_goods.extend(page["goods"] for page in kwargs["pages"]) or {"totalCached": sum(len(page["goods"]) for page in kwargs["pages"])},
    )

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=7,
        execute_lock=False,
        sources=["goods"],
    )

    saved_size = saved_goods[0][0]["sizes"][0]
    assert bool(external_calls) is not browser_selected
    if browser_selected:
        assert result["steps"][0]["externalSppMatchedCount"] == 0
        assert "buyerPriceNoWalletKopecks" not in saved_size
        return
    assert result["steps"][0]["externalSppMatchedCount"] == 1
    assert saved_size["buyerPriceNoWalletKopecks"] == 149_600
    assert saved_size["buyerPriceNoWallet"] == 1496
    assert saved_size["clientPrice"] == 1496


def test_wb_sync_goods_step_persists_spp_heartbeat_progress(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources
    monkeypatch.setattr("app.repricer_sync.browser_prices_selected", lambda _: False)

    saved_statuses: list[dict[str, object]] = []
    monkeypatch.setattr("app.repricer_sync.get_wb_sync_status", lambda _organization_id: {"state": "idle"})
    monkeypatch.setattr(
        "app.repricer_sync._save_status",
        lambda _organization_id, payload: saved_statuses.append(deepcopy(payload)) or payload,
    )
    monkeypatch.setattr("app.repricer_sync.record_wb_sync_history_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.record_wb_sync_notification", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.repricer_sync.fetch_commission_tariffs", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [])
    monkeypatch.setattr(
        "app.repricer_sync.fetch_catalog_goods_page",
        lambda *_args, **_kwargs: {
            "goods": [{"vendorCode": "SKU-1", "nmID": 1, "sizes": [{"price": 100_000}]}],
            "wbRequestId": "request-1",
        },
    )

    def fake_spp(_nm_ids, **kwargs):
        kwargs["progress_callback"](
            {"batchCurrent": 1, "batchTotal": 1, "itemsCompleted": 1, "itemsTotal": 1}
        )
        return {1: 90_000}

    monkeypatch.setattr("app.repricer_sync.fetch_external_spp_prices", fake_spp)
    monkeypatch.setattr("app.repricer_sync.replace_goods_pages", lambda **_kwargs: {"totalCached": 1})

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=7,
        sources=["goods"],
    )

    running_goods = [
        payload["steps"][0]
        for payload in saved_statuses
        if payload.get("currentSource") == "goods" and payload.get("steps")
    ]
    assert any(step.get("phase") == "spp" and step.get("progressPercent") == 95 for step in running_goods)
    assert any(step.get("message") == "Получаем цены после СПП: пачка 1 из 1" for step in running_goods)
    assert any(step.get("request") == "Цены WB · до 100 артикулов в запросе" for step in running_goods)
    assert result["steps"][0]["progressPercent"] == 100


def test_wb_sync_price_audit_records_seller_and_spp_changes(monkeypatch):
    from app import repricer_sync

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.repricer_sync.record_repricer_changelog_event",
        lambda **kwargs: events.append(kwargs),
    )

    count = repricer_sync.record_wb_sync_price_change_events(
        [
            {
                "vendorCode": "FBBT_42",
                "nmID": 123,
                "sizes": [
                    {
                        "price": 220_000,
                        "discountedPrice": 210_000,
                        "buyerPriceNoWalletKopecks": 150_000,
                    }
                ],
            },
            {
                "vendorCode": "UNCHANGED",
                "nmID": 124,
                "sizes": [{"price": 100_000, "discountedPrice": 90_000, "buyerPriceNoWalletKopecks": 80_000}],
            },
        ],
        [
            {
                "vendorCode": "FBBT_42",
                "nmID": 123,
                "sizes": [
                    {
                        "price": 220_000,
                        "discountedPrice": 205_000,
                        "buyerPriceNoWalletKopecks": 148_900,
                    }
                ],
            },
            {
                "vendorCode": "UNCHANGED",
                "nmID": 124,
                "sizes": [{"price": 100_000, "discountedPrice": 90_000, "buyerPriceNoWalletKopecks": 80_000}],
            },
        ],
        organization_id=1,
        run_id="run-1",
    )

    assert count == 2
    assert [event["trigger"] for event in events] == ["wb_sync", "wb_sync"]
    assert [event["source"] for event in events] == ["wb_sync", "wb_sync"]
    assert events[0]["article_id"] == "FBBT_42"
    assert events[0]["old_price_kopecks"] == 210_000
    assert events[0]["new_price_kopecks"] == 205_000
    assert "до СПП" in str(events[0]["reason"])
    assert events[1]["old_price_kopecks"] == 150_000
    assert events[1]["new_price_kopecks"] == 148_900
    assert "с СПП" in str(events[1]["reason"])


@pytest.mark.parametrize("browser_selected", [False, True])
def test_manual_goods_refresh_saves_external_spp_price_as_buyer_price(monkeypatch, browser_selected):
    saved_goods: list[list[dict[str, object]]] = []
    monkeypatch.setattr("app.routers.wb_repricer_bff.browser_prices_selected", lambda _: browser_selected)

    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._request_actor_and_wb_token",
        lambda _request: (SimpleNamespace(user_id="user-1", organization_id=1), "wb-token"),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.is_wb_sync_running", lambda _organization_id: False)
    monkeypatch.setattr("app.routers.wb_repricer_bff.repricer_bff_module.fetch_commission_tariffs", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.fetch_catalog_goods_page",
        lambda *_args, **_kwargs: {
            "goods": [
                {
                    "vendorCode": "FBBT_42",
                    "nmID": 1238632126,
                    "sizes": [{"price": 220_000, "discountedPrice": 220_000}],
                }
            ],
            "wbRequestId": "wb-req-1",
        },
    )
    external_calls = []
    def external_prices(*args, **kwargs):
        external_calls.append(args)
        return {1238632126: 148_900}
    monkeypatch.setattr("app.routers.wb_repricer_bff.fetch_external_spp_prices", external_prices)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.replace_goods_pages",
        lambda **kwargs: saved_goods.extend(page["goods"] for page in kwargs["pages"]) or {"totalCached": sum(len(page["goods"]) for page in kwargs["pages"]), "pagesCached": len(kwargs["pages"]), "nextOffset": 1000, "pageLimit": 1000},
    )

    response = client().post("/api/v1/wb-repricer/sku/refresh?limit=1000&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert bool(external_calls) is not browser_selected
    if browser_selected:
        assert payload["externalSppMatchedCount"] == 0
        assert "buyerPriceNoWalletKopecks" not in saved_goods[0][0]["sizes"][0]
        return
    assert payload["externalSppMatchedCount"] == 1
    saved_size = saved_goods[0][0]["sizes"][0]
    assert saved_size["buyerPriceNoWalletKopecks"] == 148_900
    assert saved_size["buyerPriceNoWallet"] == 1489
    assert saved_size["clientPrice"] == 1489


def test_empty_running_wb_sync_status_becomes_stale(monkeypatch):
    from app import repricer_sync

    started_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.repricer_sync.get_source_cache",
        lambda *_args, **_kwargs: {
            "state": "running",
            "running": True,
            "runId": "lost-run",
            "startedAt": started_at.isoformat(),
            "updatedAt": started_at.isoformat(),
            "sources": ["finance", "ads"],
            "steps": [],
        },
    )
    monkeypatch.setattr(
        "app.repricer_sync._utc_now",
        lambda: started_at + timedelta(seconds=repricer_sync.SYNC_EMPTY_RUNNING_STALE_AFTER_SECONDS + 1),
    )

    status = repricer_sync.get_wb_sync_status(1)

    assert status["state"] == "stale"
    assert status["running"] is False
    assert status["stale"] is True


def test_repricer_sync_period_start_is_today_inclusive():
    from app import repricer_sync

    now = datetime(2026, 6, 17, 18, 30, tzinfo=timezone.utc)

    assert repricer_sync._period_start_for_days(1, now=now) == datetime(2026, 6, 17, tzinfo=timezone.utc)
    assert repricer_sync._period_start_for_days(7, now=now) == datetime(2026, 6, 11, tzinfo=timezone.utc)


def test_repricer_sync_exact_range_uses_date_cache_key(monkeypatch):
    from app.repricer_sync import refresh_wb_data_sources

    calls: list[str] = []
    monkeypatch.setattr("app.repricer_sync.fetch_catalog_goods_page", lambda *_args, **_kwargs: {"goods": []})
    monkeypatch.setattr("app.repricer_sync._fetch_content_cards", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.repricer_sync.list_promotions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.repricer_sync.fetch_stock_aggregates", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.fetch_period_stats_aggregates", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.fetch_baskets_aggregates", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_sync.list_cached_goods", lambda _organization_id: [{"nmID": 123456}])
    monkeypatch.setattr(
        "app.repricer_sync.fetch_finance_report_aggregates",
        lambda *_args, **_kwargs: calls.append(f"range:{_kwargs['date_from'].date()}:{_kwargs['date_to'].date()}") or {"aggregates": {"123456": {}}, "count": 1},
    )
    monkeypatch.setattr(
        "app.repricer_sync.save_source_cache",
        lambda _organization_id, key, payload, **_kwargs: calls.append(f"save:{key}") or payload,
    )

    result = refresh_wb_data_sources(
        organization_id=1,
        wb_token="token",
        period_days=30,
        date_from=date(2026, 6, 17),
        date_to=date(2026, 6, 17),
        execute_lock=False,
        sources=["finance"],
    )

    assert result["periodDays"] == 1
    assert result["dateFrom"] == "2026-06-17"
    assert result["dateTo"] == "2026-06-17"
    assert calls == ["range:2026-06-17:2026-06-17", "save:finance_2026-06-17_2026-06-17"]


def test_repricer_bff_algorithm_exposes_extended_fields_used_by_frontend():
    payload = repricer_bff_module.get_repricer_algorithm()
    assert payload["storageCostPer60Days"] == 21
    assert payload["acquiringPct"] == 3.31
    assert payload["taxPct"] == 6
    assert payload["otherExpensePricePct"] == 5
    assert payload["otherExpensePerSaleRub"] == 0
    assert payload["sppAccountingMode"] == "spp_only"
    assert payload["wbWalletType"] == 4
    assert payload["csvMaxCostDropPct"] == 20
    assert payload["discountStepEnabled"] is False
    assert payload["basketNormMode"] == "fallback_by_type"
    assert payload["basketNormPeriodDays"] == 7
    assert payload["basketNormFallbackByGarment"]["tshirt"] == 20
    assert payload["planFactMetric"] == "orders"
    assert payload["planFactIntervalHours"] == 4


def test_sku_cost_settings_keeps_default_other_expense_pct_when_override_is_zero():
    previous_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    try:
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES["TEST_01"] = {"otherExpensePricePct": 0}

        settings = repricer_bff_module._sku_cost_settings("TEST_01", use_demo_data=False)

        assert settings["otherExpensePricePct"] == 5
    finally:
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_settings)


def test_algorithm_basket_norm_fallback_changes_sku_plan(monkeypatch):
    monkeypatch.setattr(
        "app.repricer_bff.get_settings",
        lambda: SimpleNamespace(repricer_preserve_local_price_overrides=False),
    )
    previous_settings = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    previous_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
    previous_sku_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    try:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE["basketNormMode"] = "fallback_by_type"
        repricer_bff_module.ALGORITHM_SETTINGS_STATE["basketNormFallbackByGarment"] = {
            "tshirt": 33,
            "hoodie": 17,
            "longsleeve": 9,
        }
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()

        row = repricer_bff_module._build_sku_row(
            "FBBT_42",
            nm_id=123456,
            name="Футболка",
            subject="Футболки",
            brand=None,
            chrt_ids=[],
            current_price_kopecks=129000,
            discounted_price_kopecks=119000,
            buyer_price_kopecks=None,
            promotions=[],
            use_demo_data=False,
            period_aggregate={"ordersUnits": 12},
            period_days=7,
        )

        assert row["meta"]["basketNorm"] == 33
        assert row["meta"]["basketNormSource"] == "fallback"
        assert row["analytics"]["periodDays"] == 7
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_settings)
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_META_OVERRIDES.update(previous_meta)
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_sku_settings)


def test_list_promotions_marks_only_in_action_skus(monkeypatch):
    monkeypatch.setattr(
        repricer_bff_module,
        "_utc_now",
        lambda: datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    promotions = repricer_bff_module.list_promotions("complete")
    promo_1001 = next(item for item in promotions if item["id"] == "1001")
    assert "FBBT_42" in promo_1001["articleIds"]
    assert "FCBT_18" not in promo_1001["articleIds"]


def test_build_sku_row_normalizes_goods_prices_from_rubles():
    row = repricer_bff_module._build_sku_row(
        "TEST_01",
        nm_id=1,
        name="Test",
        subject="Товары",
        brand="Brand",
        chrt_ids=[],
        current_price_kopecks=wb_goods_price_to_kopecks(8747),
        discounted_price_kopecks=wb_goods_price_to_kopecks(3761),
        buyer_price_kopecks=wb_goods_price_to_kopecks(3253),
        promotions=[],
        use_demo_data=False,
    )
    assert row["meta"]["currentPriceKopecks"] == 376_100
    assert row["analytics"]["basePriceKopecks"] == 874_700


def test_repricer_bff_promotions_and_excel_upload_are_available(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._request_actor_and_wb_token",
        lambda _request: (SimpleNamespace(user_id="user-1", actor_id="user-1", organization_id=1, role="price_sender"), None),
    )
    demo_promotions = [
        {
            "id": "1001",
            "name": "Весенняя распродажа",
            "type": "auto",
            "status": "active",
            "startDate": "2026-06-01",
            "endDate": "2026-06-30",
            "daysUntilEnd": 15,
            "daysUntilStart": 0,
            "eligibleSkuCount": 1,
            "participatingSkuCount": 1,
            "participationPct": 100,
            "excelLoaded": False,
            "excelStatus": "missing",
            "excelFileName": None,
            "excelLoadedAt": None,
            "thresholdRowsParsed": 0,
            "articleIds": ["FBBT_42"],
        }
    ]
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_promotions", lambda *args, **kwargs: [dict(item) for item in demo_promotions])
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_promotion_skus", lambda *args, **kwargs: [{"articleId": "FBBT_42"}])
    api = client()
    headers = auth_headers(api, "price_sender")

    promotions = api.get("/api/v1/wb-repricer/promotions", headers=headers)
    assert promotions.status_code == 200
    rows = promotions.json()
    assert rows
    promo_id = rows[0]["id"]
    assert "articleIds" not in rows[0]

    skus = api.get(f"/api/v1/wb-repricer/promotions/{promo_id}/skus", headers=headers)
    assert skus.status_code == 200

    upload = api.post(
        f"/api/v1/wb-repricer/promotions/{promo_id}/upload-excel",
        files={
            "file": (
                "promo.xlsx",
                _xlsx_bytes(
                    [
                        ["NmId", "Артикул продавца", "Текущая цена", "Цена для участия", "Скидка для участия"],
                        ["123456", "FBBT_42", "1290", "1148", "18"],
                    ]
                ),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers=headers,
    )
    assert upload.status_code == 200
    updated = upload.json()
    assert updated["excelLoaded"] is True
    assert updated["excelFileName"] == "promo.xlsx"


def test_repricer_bff_liquidation_and_changelog_routes_work(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    api = client()
    headers = auth_headers(api, "price_sender")

    liquidation = api.get("/api/v1/wb-repricer/liquidation", headers=headers)
    assert liquidation.status_code == 200
    before = liquidation.json()
    candidate_ids = [item["articleId"] for item in before["candidates"]]
    if candidate_ids:
        started = api.post("/api/v1/wb-repricer/liquidation/start", json={"articleIds": [candidate_ids[0]]}, headers=headers)
        assert started.status_code == 200
        assert started.json()["started"] is True

    changelog = api.get("/api/v1/wb-repricer/changelog", params={"trigger": "manual", "limit": 10}, headers=headers)
    assert changelog.status_code == 200
    payload = changelog.json()
    assert isinstance(payload["items"], list)
    assert all(item["trigger"] == "manual" for item in payload["items"])


def test_repricer_pricing_status_exposes_liquidation_progress_and_history(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", organization_id=1, actor_id="user-1", role="manager"),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: None)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda _organization_id: [
            {
                "vendorCode": "FBBT_42",
                "nmID": 123456,
                "title": "Test SKU",
                "sizes": [{"price": 129000, "discountedPrice": 119900}],
            }
        ],
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.cached_goods_meta",
        lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda *_args, **_kwargs: {})
    repricer_bff_module.LIQUIDATION_ACTIVE["FBBT_42"] = {
        "articleId": "FBBT_42",
        "startPriceKopecks": 129000,
        "currentPriceKopecks": 118000,
        "targetPriceKopecks": 90000,
        "startedAt": "2026-06-08T00:00:00+00:00",
        "nextStepAt": "2026-06-12T00:00:00+00:00",
        "stepPct": 5,
        "requiresNegativeMarginConfirm": False,
    }
    repricer_bff_module.SKU_META_OVERRIDES.setdefault("FBBT_42", {})["status"] = "liquidation"
    repricer_bff_module.CHANGELOG_ENTRIES.insert(
        0,
        {
            "id": "chg-test-fbbt-42",
            "articleId": "FBBT_42",
            "skuName": "Test SKU",
            "timestamp": "2026-06-10T00:00:00+00:00",
            "oldPriceKopecks": 124000,
            "newPriceKopecks": 118000,
            "changePct": -4.84,
            "trigger": "liquidation",
            "marginAfterPct": 8,
            "actor": {"id": "system", "name": "Система", "role": "system"},
            "source": "system",
            "scope": "sku",
            "reason": "Плановый шаг ликвидации",
        },
    )

    api = client()
    response = api.get("/api/v1/wb-repricer/sku/FBBT_42/pricing-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stage"] == "active_liquidation"
    assert payload["progress"]["kind"] == "liquidation"
    assert payload["progress"]["currentDay"] >= 1
    assert payload["progress"]["totalDays"] >= payload["progress"]["currentDay"]
    assert payload["lastDecision"]["trigger"] == "liquidation"
    assert payload["recentChanges"][0]["reason"] == "Плановый шаг ликвидации"


def test_repricer_sku_timeseries_groups_raw_rows_by_day_hour_and_changelog(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(wb_api_token="env-token"))
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", organization_id=1, actor_id="user-1", role="manager"),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: None)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda _organization_id: [
            {
                "vendorCode": "FBBT_42",
                "nmID": 123456,
                "title": "Test SKU",
                "sizes": [{"price": 129000, "discountedPrice": 119900}],
            }
        ],
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.cached_goods_meta",
        lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_bff._utc_now", lambda: datetime(2026, 6, 12, tzinfo=timezone.utc))
    previous_changelog = list(repricer_bff_module.CHANGELOG_ENTRIES)
    previous_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
    try:
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.CHANGELOG_ENTRIES[:] = [
            {
                "id": "chg-timeseries-fbbt-42",
                "articleId": "FBBT_42",
                "skuName": "Test SKU",
                "timestamp": "2026-05-29T09:00:00+00:00",
                "oldPriceKopecks": 129000,
                "newPriceKopecks": 119900,
                "changePct": -7.05,
                "trigger": "algorithm",
                "marginAfterPct": 12,
                "actor": {"id": "system", "name": "Система", "role": "system"},
                "source": "system",
                "scope": "sku",
                "reason": "Тестовое изменение цены",
            }
        ]

        response = client().get(
            "/api/v1/wb-repricer/sku/FBBT_42/timeseries",
            params={"days": 20, "includeRaw": "true"},
        )
    finally:
        repricer_bff_module.CHANGELOG_ENTRIES[:] = previous_changelog
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_META_OVERRIDES.update(previous_meta)

    assert response.status_code == 200
    payload = response.json()
    assert payload["articleId"] == "FBBT_42"
    assert payload["nmId"] == 123456
    assert payload["period"]["dateFrom"] == "2026-05-24"
    may_28 = next(row for row in payload["daily"] if row["date"] == "2026-05-28")
    assert may_28["ordersUnits"] == 1
    assert may_28["salesUnits"] == 1
    assert may_28["revenueKopecks"] == 114500
    assert may_28["avgPriceWithSppKopecks"] == 114500
    hour_8_wb = next(row for row in payload["hourlyOrders"] if row["hour"] == 8)
    assert hour_8_wb["ordersUnits"] == 1
    assert payload["rawRows"]["orders"][0]["srid"] == "srid-order-1"
    assert payload["rawRows"]["sales"][0]["saleID"] == "S123456"
    assert payload["priceEvents"][0]["newPriceKopecks"] == 119900
    may_29 = next(row for row in payload["daily"] if row["date"] == "2026-05-29")
    assert may_29["priceKopecks"] == 119900
    assert any(source["source"] == "wb-statistics-orders" and source["status"] == "fresh" for source in payload["sources"])


def test_repricer_sku_list_reuses_operational_month_but_rejects_legacy_finance_for_week(monkeypatch):
    from app.repricer_cache.store import FINANCE_SCHEMA_VERSION
    source_state = {
        "wb_sync_status": {
            "state": "completed",
            "periodDays": 30,
            "periodCacheSuffix": "2026-06-01_2026-06-30",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-30",
            "steps": [],
        },
        "finance_2026-06-01_2026-06-30": {
            "revenueBasis": "retailAmount", "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
            "fetchedAt": "2026-06-30T08:00:00+00:00",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-30",
            "periodDays": 30,
            "dailyAggregates": {
                "2026-06-09": {"123456": {"salesUnits": 1, "sellerRevenueKopecks": 90_000, "revenueGrossKopecks": 90_000}},
                "2026-06-10": {"123456": {"salesUnits": 2, "sellerRevenueKopecks": 200_000, "revenueGrossKopecks": 200_000, "commissionKopecks": 20_000}},
                "2026-06-16": {"123456": {"salesUnits": 1, "sellerRevenueKopecks": 100_000, "revenueGrossKopecks": 100_000, "commissionKopecks": 10_000}},
                "2026-06-17": {"123456": {"salesUnits": 5, "sellerRevenueKopecks": 500_000, "revenueGrossKopecks": 500_000}},
            },
        },
        "period_stats_2026-06-01_2026-06-30": {
            "fetchedAt": "2026-06-30T08:00:00+00:00",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-30",
            "periodDays": 30,
            "dailyAggregates": {
                "2026-06-10": {"123456": {"ordersUnits": 3, "revenueKopecks": 210_000}},
                "2026-06-16": {"123456": {"ordersUnits": 4, "revenueKopecks": 110_000}},
            },
        },
        "baskets_2026-06-01_2026-06-30": {
            "fetchedAt": "2026-06-30T08:00:00+00:00",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-30",
            "periodDays": 30,
            "dailyAggregates": {
                "2026-06-10": {"123456": {"cartCount": 11, "orderCount": 3, "source": "sales_funnel_products"}},
                "2026-06-16": {"123456": {"cartCount": 13, "orderCount": 7, "source": "sales_funnel_products"}},
            },
        },
        "baskets_2026-06-10_2026-06-16": {
            "fetchedAt": "2026-06-16T08:00:00+00:00",
            "aggregates": {"123456": {"cartCount": 999, "orderCount": 99, "source": "legacy_no_range"}},
        },
        "ads_2026-06-01_2026-06-30": {
            "fetchedAt": "2026-06-30T08:00:00+00:00",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-30",
            "periodDays": 30,
            "dailyAggregates": {
                "2026-06-10": {"123456": {"adSpendKopecks": 1_000}},
                "2026-06-16": {"123456": {"adSpendKopecks": 2_000}},
            },
        },
    }

    # These fixtures represent fetched days with no events, not absent days.
    for offset in range(30):
        source_state["baskets_2026-06-01_2026-06-30"]["dailyAggregates"].setdefault((date(2026, 6, 1) + timedelta(days=offset)).isoformat(), {})
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: None)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", organization_id=1, actor_id="user-1", role="manager"),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.list_cached_goods",
        lambda _organization_id: [
            {
                "vendorCode": "FBBT_42",
                "nmID": 123456,
                "brand": "Ogni",
                "subjectName": "Футболки",
                "sizes": [{"price": 139000, "discountedPrice": 139000, "buyerPriceNoWalletKopecks": 107000}],
            }
        ],
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.cached_goods_meta",
        lambda _organization_id: {"pagesCached": 1, "totalCached": 1, "nextOffset": 1000, "pageLimit": 1000, "latestFetchedAt": None},
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda _organization_id, key, **_kwargs: source_state.get(key))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_meta_fields", lambda _organization_id, key: {"fetchedAt": source_state.get(key, {}).get("fetchedAt")} if key in source_state else {})
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_fetched_at", lambda _organization_id, key: source_state.get(key, {}).get("fetchedAt"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_wb_sync_status", lambda _organization_id: source_state["wb_sync_status"])

    response = client().get(
        "/api/v1/wb-repricer/sku",
        params={"periodDays": 7, "dateFrom": "2026-06-10", "dateTo": "2026-06-16"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    analytics = item["analytics"]
    # Operational daily observations are reusable. This old financial blob has
    # neither posting-date realization fields nor a connection-scoped receipt;
    # a finance-schema stamp alone must not certify its seller-price total.
    assert analytics["periodCoverageStates"]["financeState"] == "partial"
    assert analytics["revenueKopecks"] != 300_000
    assert analytics["ordersUnits"] == 10
    assert analytics["ordersSource"] == "sales_funnel.orderCount"
    assert analytics["funnelOrderCount"] == 10
    assert analytics["baskets"] == 24
    assert response.json()["cache"].get("financeFetchedAt") is None


def test_period_source_cache_uses_covering_detail_cache_without_sync_status(monkeypatch):
    source_state = {
        "baskets_2026-06-01_2026-07-10": {
            "fetchedAt": "2026-07-10T08:00:00+00:00",
            "dateFrom": "2026-06-01",
            "dateTo": "2026-07-10",
            "periodDays": 40,
            "dailyDetailFetchedAt": "2026-07-10T08:00:00+00:00",
            "dailyAggregates": {
                "2026-06-19": {"123456": {"cartCount": 99, "orderCount": 99}},
                "2026-06-20": {"123456": {"cartCount": 11, "orderCount": 3}},
                "2026-07-10": {"123456": {"cartCount": 13, "orderCount": 7}},
            },
        },
    }

    for offset in range(40):
        source_state["baskets_2026-06-01_2026-07-10"]["dailyAggregates"].setdefault((date(2026, 6, 1) + timedelta(days=offset)).isoformat(), {})
    monkeypatch.setattr(wb_repricer_bff_router, "get_wb_sync_status", lambda _organization_id: {})
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache", lambda _organization_id, key, **_kwargs: source_state.get(key))
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "list_source_cache_by_prefix",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not scan large source cache payloads by prefix")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "get_covering_source_cache",
        lambda _organization_id, prefix, **_kwargs: {
            **source_state["baskets_2026-06-01_2026-07-10"],
            "sourceKey": "baskets_2026-06-01_2026-07-10",
        }
        if prefix == "baskets_"
        else None,
        raising=False,
    )

    payload = wb_repricer_bff_router._period_source_cache(
        1,
        "baskets",
        "2026-06-20_2026-07-10",
        21,
        datetime(2026, 6, 20, tzinfo=timezone.utc),
        datetime(2026, 7, 10, tzinfo=timezone.utc),
        require_full_sync_coverage=False,
    )

    assert payload["dateFrom"] == "2026-06-20"
    assert payload["dateTo"] == "2026-07-10"
    assert payload["periodDays"] == 21
    assert payload["count"] == 1
    assert payload["aggregates"]["123456"]["cartCount"] == 24
    assert payload["aggregates"]["123456"]["orderCount"] == 10
    assert payload["coveredByCache"]["periodDays"] == 40


def test_repricer_simulator_updates_wb_input_caches_and_runs_engine(monkeypatch):
    monkeypatch.setattr(
        repricer_execution_module,
        "_utc_now",
        lambda: datetime(2026, 6, 24, 9, tzinfo=timezone.utc),
    )
    goods_state = [
        {
            "vendorCode": "FBBT_42",
            "nmID": 123456,
            "brand": "Ogni",
            "subjectName": "Футболки",
            "sizes": [{"price": 139000, "discountedPrice": 139000, "buyerPriceNoWalletKopecks": 107000}],
        }
    ]
    source_state: dict[str, dict] = {}

    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(
        wb_api_token="env-token",
        wb_api_mode="fake",
        real_price_apply_enabled=False,
        repricer_local_price_apply_enabled=True,
        repricer_preserve_local_price_overrides=True,
        repricer_scheduler_enabled=True,
        repricer_execute_interval_minutes=60,
    ))
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", actor_id="user-1", role="manager", organization_id=1),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: "env-token")
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_cached_goods", lambda _organization_id: goods_state)
    monkeypatch.setattr("app.routers.wb_repricer_bff.cached_goods_meta", lambda _organization_id: {
        "pagesCached": 1,
        "totalCached": len(goods_state),
        "nextOffset": 1000,
        "pageLimit": 1000,
        "latestFetchedAt": None,
    })
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda _organization_id, key, **_kwargs: source_state.get(key))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_meta_fields", lambda _organization_id, key: {"fetchedAt": source_state.get(key, {}).get("fetchedAt")} if key in source_state else {})
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_fetched_at", lambda _organization_id, key: source_state.get(key, {}).get("fetchedAt"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.compact_heavy_source_cache_rows", lambda _organization_id: None)
    monkeypatch.setattr("app.repricer_bff.fetch_commission_tariffs", lambda *_args, **_kwargs: {})

    def save_goods_page_stub(**kwargs):
        goods_state[:] = kwargs["goods"]
        return {"pagesCached": 1, "totalCached": len(goods_state), "nextOffset": 1000, "pageLimit": kwargs["page_limit"], "latestFetchedAt": None}

    def save_source_cache_stub(_organization_id, source_key, payload):
        stored = dict(payload)
        stored.setdefault("fetchedAt", datetime.now(timezone.utc).isoformat())
        source_state[source_key] = stored
        return stored

    monkeypatch.setattr("app.routers.wb_repricer_bff.save_goods_page", save_goods_page_stub)
    monkeypatch.setattr("app.routers.wb_repricer_bff.save_source_cache", save_source_cache_stub)
    monkeypatch.setattr("app.routers.wb_repricer_bff._flush_org_repricer_state", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: 1)
    previous_assignments = dict(repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS)
    previous_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
    previous_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    try:
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        source_state["wb_sync_status"] = {
            "periodDays": 30,
            "periodCacheSuffix": "2026-05-26_2026-06-24",
            "dateFrom": "2026-05-26",
            "dateTo": "2026-06-24",
        }
        api = client()

        patch = api.put(
            "/api/v1/wb-repricer/simulator/sku/FBBT_42",
            json={
                "strategyId": "baskets_orders",
                "sellerPriceKopecks": 139000,
                "buyerPriceKopecks": 107000,
                "baskets": 120,
                "basketNorm": 20,
                "ordersUnits": 60,
                "salesUnits": 40,
                "stockUnits": 25,
                "revenueKopecks": 4_280_000,
            },
        )
        assert patch.status_code == 200
        body = patch.json()
        assert body["item"]["strategy"]["id"] == "baskets_orders"
        assert body["item"]["current"]["baskets"] == 120
        assert source_state["baskets_30"]["aggregates"]["123456"]["cartCount"] == 120
        assert source_state["baskets_2026-05-26_2026-06-24"]["aggregates"]["123456"]["cartCount"] == 120
        assert goods_state[0]["sizes"][0]["buyerPriceNoWalletKopecks"] == 107000

        run = api.post("/api/v1/wb-repricer/simulator/run", json={"articleIds": ["FBBT_42"], "applyPrices": False})
        assert run.status_code == 200
        report = run.json()["report"]
        assert report["runId"].startswith("exec_")
        assert report["items"][0]["articleId"] == "FBBT_42"

        run_with_inputs = api.post(
            "/api/v1/wb-repricer/simulator/run",
            json={
                "articleIds": ["FBBT_42"],
                "applyPrices": False,
                "inputs": {
                    "strategyId": "turnover_control",
                    "sellerPriceKopecks": 139000,
                    "buyerPriceKopecks": 0,
                    "sppPct": None,
                    "ordersUnits": 14,
                    "stockUnits": 2,
                    "baskets": 28,
                    "basketNorm": 20,
                    "salesUnits": 0,
                    "returnsUnits": 0,
                    "revenueKopecks": 0,
                    "buyoutPct": 90,
                    "makeLiquidationDue": False,
                },
            },
        )
        assert run_with_inputs.status_code == 200
        assert source_state["period_stats_30"]["aggregates"]["123456"]["ordersUnits"] == 14
        assert source_state["period_stats_2026-05-26_2026-06-24"]["aggregates"]["123456"]["ordersUnits"] == 14
        assert source_state["stocks"]["aggregates"]["123456"]["wbStockUnits"] == 2
        input_report = run_with_inputs.json()["report"]
        input_item = input_report["items"][0]
        assert input_item["frontendStrategyId"] == "turnover_control"
        assert "Оборачиваемость 1.0 дн" in input_item["explanation"]
        assert "шаг ограничен guard до +6%" in input_item["explanation"]
        assert input_item["recommendedPriceKopecks"] == 147340
        input_dashboard_item = next(item for item in run_with_inputs.json()["dashboard"]["items"] if item["articleId"] == "FBBT_42")
        assert input_dashboard_item["current"]["ordersUnits"] == 14
    finally:
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.update(previous_assignments)
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_META_OVERRIDES.update(previous_meta)
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_settings)


@pytest.mark.parametrize("tariff_available", [False, True])
def test_repricer_simulator_allows_input_overrides_in_real_apply_mode_without_applying(monkeypatch, tariff_available):
    monkeypatch.setattr(
        repricer_execution_module,
        "_utc_now",
        lambda: datetime(2026, 6, 24, 9, tzinfo=timezone.utc),
    )
    goods_state = [
        {
            "vendorCode": "FBBT_42",
            "nmID": 123456,
            "brand": "Ogni",
            "subjectName": "Футболки",
            "sizes": [{"price": 139000, "discountedPrice": 139000, "buyerPriceNoWalletKopecks": 107000}],
        }
    ]
    source_state: dict[str, dict] = {}

    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(
        wb_api_token="env-token",
        wb_api_mode="real",
        real_price_apply_enabled=True,
        repricer_local_price_apply_enabled=True,
        repricer_preserve_local_price_overrides=True,
        repricer_scheduler_enabled=True,
        repricer_execute_interval_minutes=60,
    ))
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.actor_from_request",
        lambda _request: SimpleNamespace(user_id="user-1", actor_id="user-1", role="manager", organization_id=1),
    )
    monkeypatch.setattr("app.routers.wb_repricer_bff._request_wb_token", lambda _request: "env-token")
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_cached_goods", lambda _organization_id: goods_state)
    monkeypatch.setattr("app.routers.wb_repricer_bff.cached_goods_meta", lambda _organization_id: {
        "pagesCached": 1,
        "totalCached": len(goods_state),
        "nextOffset": 1000,
        "pageLimit": 1000,
        "latestFetchedAt": None,
    })
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache", lambda _organization_id, key, **_kwargs: source_state.get(key))
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_meta_fields", lambda _organization_id, key: {"fetchedAt": source_state.get(key, {}).get("fetchedAt")} if key in source_state else {})
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_source_cache_fetched_at", lambda _organization_id, key: source_state.get(key, {}).get("fetchedAt"))
    monkeypatch.setattr("app.routers.wb_repricer_bff.compact_heavy_source_cache_rows", lambda _organization_id: None)
    monkeypatch.setattr("app.repricer_bff.fetch_commission_tariffs", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.repricer_bff.cached_commission_tariffs", lambda *_args, **_kwargs: {
        "name:футболки": {"baseCommissionPct": 15, "sourceField": "tariffs.commission.kgvpMarketplace"},
    } if tariff_available else {})

    def save_goods_page_stub(**kwargs):
        goods_state[:] = kwargs["goods"]
        return {"pagesCached": 1, "totalCached": len(goods_state), "nextOffset": 1000, "pageLimit": kwargs["page_limit"], "latestFetchedAt": None}

    def save_source_cache_stub(_organization_id, source_key, payload):
        stored = dict(payload)
        stored.setdefault("fetchedAt", datetime.now(timezone.utc).isoformat())
        source_state[source_key] = stored
        return stored

    monkeypatch.setattr("app.routers.wb_repricer_bff.save_goods_page", save_goods_page_stub)
    monkeypatch.setattr("app.routers.wb_repricer_bff.save_source_cache", save_source_cache_stub)
    monkeypatch.setattr("app.routers.wb_repricer_bff._flush_org_repricer_state", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: 1)
    previous_assignments = dict(repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS)
    previous_meta = dict(repricer_bff_module.SKU_META_OVERRIDES)
    previous_settings = dict(repricer_bff_module.SKU_SETTINGS_OVERRIDES)
    try:
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        api = client()

        patch = api.put(
            "/api/v1/wb-repricer/simulator/sku/FBBT_42",
            json={
                "strategyId": "turnover_control",
                "sellerPriceKopecks": 139000,
                "buyerPriceKopecks": 107000,
                "baskets": 28,
                "basketNorm": 20,
                "ordersUnits": 14,
                "salesUnits": 0,
                "stockUnits": 2,
                "revenueKopecks": 0,
                "buyoutPct": 90,
            },
        )
        assert patch.status_code == 200
        patch_body = patch.json()
        assert patch_body["dashboard"]["mode"]["realPriceApplyEnabled"] is True
        assert patch_body["dashboard"]["mode"]["inputOverridesAllowed"] is True
        assert patch_body["dashboard"]["mode"]["simulatorRunApplyAllowed"] is False
        assert source_state["period_stats_30"]["aggregates"]["123456"]["ordersUnits"] == 14
        assert source_state["stocks"]["aggregates"]["123456"]["wbStockUnits"] == 2

        run = api.post(
            "/api/v1/wb-repricer/simulator/run",
            json={
                "articleIds": ["FBBT_42"],
                "applyPrices": True,
                "inputs": {
                    "strategyId": "turnover_control",
                    "sellerPriceKopecks": 139000,
                    "buyerPriceKopecks": 107000,
                    "ordersUnits": 14,
                    "stockUnits": 2,
                    "baskets": 28,
                    "basketNorm": 20,
                    "salesUnits": 0,
                    "returnsUnits": 0,
                    "revenueKopecks": 0,
                    "buyoutPct": 90,
                    "makeLiquidationDue": False,
                },
            },
        )
        assert run.status_code == 200
        run_body = run.json()
        assert run_body["runMode"] == "preview_only"
        assert run_body["mode"]["simulatorRunApplyAllowed"] is False
        item = run_body["report"]["items"][0]
        assert item["status"] == ("executed" if tariff_available else "blocked")
        if not tariff_available:
            assert "commission_missing" in {detail["code"] for detail in item["blockerDetails"]}
        assert item["draftId"] is None
        assert item["jobId"] is None
        assert item["applyState"] is None
        assert item["frontendStrategyId"] == "turnover_control"
    finally:
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
        repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS.update(previous_assignments)
        repricer_bff_module.SKU_META_OVERRIDES.clear()
        repricer_bff_module.SKU_META_OVERRIDES.update(previous_meta)
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.clear()
        repricer_bff_module.SKU_SETTINGS_OVERRIDES.update(previous_settings)


def test_worker_status_returns_next_run_and_recent_scheduler_logs(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.hydrate_repricer_bff_state", lambda *_args: None)
    previous_algorithm = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    repricer_bff_module.ALGORITHM_SETTINGS_STATE["syncIntervalMinutes"] = 5
    repricer_bff_module.ALGORITHM_SETTINGS_STATE["fullSyncIntervalMinutes"] = 30
    repricer_bff_module.ALGORITHM_SETTINGS_STATE["workerAutoApplyPricesEnabled"] = True
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(
        wb_api_mode="real",
        real_price_apply_enabled=True,
        repricer_scheduler_enabled=True,
        repricer_wb_sync_enabled=True,
        repricer_execute_interval_minutes=5,
        repricer_wb_sync_interval_minutes=40,
    ))
    monkeypatch.setattr("app.routers.wb_repricer_bff.datetime", SimpleNamespace(
        now=lambda _tz=None: datetime(2026, 6, 24, 9, 31, 0, tzinfo=timezone.utc),
        fromisoformat=datetime.fromisoformat,
        fromtimestamp=datetime.fromtimestamp,
    ))
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_execution_runs", lambda **_kwargs: [
        {
            "runId": "exec_worker",
            "trigger": "scheduler",
            "createdAt": "2026-06-24T09:29:06+00:00",
            "report": {
                "executedCount": 1,
                "skippedCount": 0,
                "blockedCount": 0,
                "items": [
                    {
                        "articleId": "JФЧ1009",
                        "status": "executed",
                        "frontendStrategyId": "turnover_control",
                        "explanation": "Оборачиваемость 100.0 дн -> -25%",
                        "applyState": "accepted",
                    }
                ],
            },
        }
    ])
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_wb_sync_status", lambda _organization_id: {
        "state": "completed",
        "running": False,
        "runId": "sync_worker",
        "trigger": "scheduler",
        "dateFrom": "2026-06-18",
        "dateTo": "2026-06-24",
        "steps": [{"source": "goods", "status": "completed", "count": 2988}],
    })

    try:
        api = client()
        assert api.get("/api/v1/wb-repricer/worker/status").status_code == 401
        response = api.get("/api/v1/wb-repricer/worker/status", headers=auth_headers(api, "viewer"))

        assert response.status_code == 200
        payload = response.json()
        assert payload["organizationId"] == 1
        assert payload["mode"]["schedulerEnabled"] is True
        assert payload["mode"]["schedulerPollIntervalMinutes"] == 5
        assert payload["mode"]["executeIntervalMinutes"] == 5
        assert payload["mode"]["fullSyncIntervalMinutes"] == 30
        assert payload["mode"]["workerAutoApplyPricesEnabled"] is True
        assert payload["timing"]["lastSchedulerRunAt"] == "2026-06-24T09:29:06+00:00"
        assert payload["timing"]["nextSchedulerPollAt"] == "2026-06-24T09:35:00+00:00"
        assert payload["timing"]["secondsUntilNextSchedulerPoll"] == 240
        assert payload["timing"]["secondsUntilNextRun"] == 186
        assert payload["sync"]["runId"] == "sync_worker"
        assert payload["sync"]["steps"][0]["source"] == "goods"
        assert payload["runs"][0]["runId"] == "exec_worker"
        assert payload["runs"][0]["executedCount"] == 1
        assert payload["runs"][0]["items"][0]["articleId"] == "JФЧ1009"
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_algorithm)


def test_worker_status_without_runs_uses_stable_interval_boundary(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff.hydrate_repricer_bff_state", lambda *_args: None)
    previous_algorithm = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    repricer_bff_module.ALGORITHM_SETTINGS_STATE["syncIntervalMinutes"] = 5
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_settings", lambda: SimpleNamespace(
        wb_api_mode="real",
        real_price_apply_enabled=True,
        repricer_scheduler_enabled=True,
        repricer_wb_sync_enabled=False,
        repricer_execute_interval_minutes=60,
        repricer_wb_sync_interval_minutes=60,
    ))
    monkeypatch.setattr("app.routers.wb_repricer_bff.datetime", SimpleNamespace(
        now=lambda _tz=None: datetime(2026, 6, 24, 9, 31, 10, tzinfo=timezone.utc),
        fromisoformat=datetime.fromisoformat,
        fromtimestamp=datetime.fromtimestamp,
    ))
    monkeypatch.setattr("app.routers.wb_repricer_bff.list_execution_runs", lambda **_kwargs: [])
    monkeypatch.setattr("app.routers.wb_repricer_bff.get_wb_sync_status", lambda _organization_id: {"state": "idle"})

    try:
        api = client()
        response = api.get("/api/v1/wb-repricer/worker/status", headers=auth_headers(api, "viewer"))

        assert response.status_code == 200
        payload = response.json()
        assert payload["timing"]["lastSchedulerRunAt"] is None
        assert payload["timing"]["nextSchedulerPollAt"] == "2026-06-24T09:35:00+00:00"
        assert payload["timing"]["secondsUntilNextSchedulerPoll"] == 230
        assert payload["timing"]["nextRunAt"] == "2026-06-24T09:35:00+00:00"
        assert payload["timing"]["secondsUntilNextRun"] == 230
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_algorithm)


def test_algorithm_put_persists_minute_intervals(monkeypatch):
    monkeypatch.setattr("app.routers.wb_repricer_bff._hydrate_org_repricer_state", lambda _request: 10)
    monkeypatch.setattr("app.routers.wb_repricer_bff.flush_repricer_bff_state", lambda *_args, **_kwargs: True)
    previous_algorithm = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)

    try:
        response = client().put(
            "/api/v1/wb-repricer/algorithm",
            json={"syncIntervalMinutes": 5, "fullSyncIntervalMinutes": 15},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["syncIntervalMinutes"] == 5
        assert payload["syncIntervalHours"] == 1
        assert payload["fullSyncIntervalMinutes"] == 15
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_algorithm)


@pytest.mark.parametrize(
    "previous_tax,payload,tax_changed",
    [
        (6, {"taxPct": 7.5}, True),
        ("7.50", {"taxPct": 9}, True),
        (7.5, {"taxPct": 0}, True),
        (7.5, {"taxPct": "7.50"}, False),
        (7.5, {"taxPct": 7.501}, False),
        (7.5, {"taxPct": 7.5, "otherExpensePricePct": 6}, False),
        (7.5, {"otherExpensePricePct": 6}, False),
    ],
)
def test_algorithm_economics_dual_write_is_scoped(
    monkeypatch, previous_tax, payload, tax_changed,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._hydrate_org_repricer_state",
        lambda _request: 10,
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._flush_org_repricer_state",
        lambda _organization_id: True,
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.EconomicsService.reconcile_legacy_organization",
        lambda _self, settings, **kwargs: calls.append(
            {"organization_id": _self.organization_id, "settings": settings, **kwargs}
        ),
    )
    previous_algorithm = dict(repricer_bff_module.ALGORITHM_SETTINGS_STATE)
    try:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE["taxPct"] = previous_tax
        api = client()

        economics = api.put(
            "/api/v1/wb-repricer/algorithm",
            json=payload,
        )
        unrelated = api.put(
            "/api/v1/wb-repricer/algorithm",
            json={"syncIntervalMinutes": 5},
        )

        assert economics.status_code == unrelated.status_code == 200
        assert len(calls) == 1
        assert calls[0]["organization_id"] == 10
        assert calls[0]["settings"]["taxPct"] == payload.get("taxPct", previous_tax)
        assert calls[0].get("tax_value_state") == ("configured" if tax_changed else None)
        assert calls[0].get("tax_evidence_status") == ("dated" if tax_changed else None)
        for field in ("workerAutoApplyPricesEnabled", "nightMedianAutoApplyEnabled"):
            assert economics.json().get(field) == previous_algorithm.get(field)
    finally:
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.clear()
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.update(previous_algorithm)


def test_sku_economics_dual_write_is_scoped(monkeypatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._request_wb_token", lambda _request: None
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._hydrate_org_repricer_state",
        lambda _request: 10,
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff._flush_org_repricer_state",
        lambda _organization_id: True,
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.put_repricer_sku_settings",
        lambda article_id, payload, *_args, **_kwargs: {
            "meta": {
                "articleId": article_id,
                "lastSavedAt": "2026-09-02T18:00:00+00:00",
            },
            "settings": {
                "taxPct": 6,
                "otherExpensePricePct": payload.get("otherExpensePricePct", 5),
                "otherExpensePerSaleKopecks": 0,
            },
        },
    )
    monkeypatch.setattr(
        "app.routers.wb_repricer_bff.EconomicsService.reconcile_legacy_sku_override",
        lambda _self, article_id, settings, **kwargs: calls.append(
            {"articleId": article_id, "settings": settings, **kwargs}
        ),
    )
    api = client()

    economics = api.put(
        "/api/v1/wb-repricer/sku/SKU-11/settings",
        json={"otherExpensePricePct": 4},
    )
    unrelated = api.put(
        "/api/v1/wb-repricer/sku/SKU-11/settings",
        json={"automationEnabled": False},
    )

    assert economics.status_code == unrelated.status_code == 200
    assert len(calls) == 1
    assert calls[0]["articleId"] == "SKU-11"
    assert calls[0]["settings"]["otherExpensePricePct"] == 4
