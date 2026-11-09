from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import repricer_bff as repricer_bff_module
from app import repricer_sync
from app.main import create_app
from app.repricer_cache.store import FINANCE_SCHEMA_VERSION, FINANCE_REVENUE_BASIS
from app.routers import wb_repricer_bff as wb_repricer_bff_router
from tests.auth_helpers import auth_headers


DATE_FROM = "2026-07-01"
DATE_TO = "2026-07-07"
SUFFIX = f"{DATE_FROM}_{DATE_TO}"
FETCHED_AT = "2026-07-08T01:00:00+00:00"


def _good(index: int) -> dict:
    return {
        "vendorCode": f"STATS_{index:02d}",
        "nmID": 10_000 + index,
        "brand": "Ogni",
        "subjectName": "Футболки",
        "sizes": [
            {
                "price": 1_000,
                "discountedPrice": 1_000,
                "buyerPriceNoWalletKopecks": 90_000,
            }
        ],
    }


def _source_payload(aggregates: dict, **metadata: object) -> dict:
    return {
        "fetchedAt": FETCHED_AT,
        "dateFrom": DATE_FROM,
        "dateTo": DATE_TO,
        "periodDays": 7,
        "aggregates": aggregates,
        **metadata,
    }


def _fail(message: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(message)

    return fail


@pytest.fixture
def stats_runtime(monkeypatch):
    monkeypatch.setattr("app.platform.economics.legacy_catalog.catalog_facts", lambda _org, **kw: {})
    goods = [_good(1)]
    storage: dict[tuple[int, str], dict] = {}
    save_calls: list[tuple[int, str]] = []
    settings = replace(repricer_bff_module.get_settings(), wb_api_mode="real")

    def get_cache(organization_id: int, key: str, **_kwargs):
        return deepcopy(storage.get((organization_id, key)))

    def save_cache(organization_id: int, key: str, payload: dict):
        save_calls.append((organization_id, key))
        storage[organization_id, key] = deepcopy(payload)
        return deepcopy(payload)

    def meta_fields(organization_id: int, key: str):
        cache = storage.get((organization_id, key)) or {}
        return {
            field: cache.get(field)
            for field in (
                "fetchedAt",
                "cachedGoodsNmIds",
                "matchedCachedGoodsNmIds",
                "requestedNmIds",
                "matchedNmIds",
            )
            if cache.get(field) is not None
        }

    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache", get_cache)
    monkeypatch.setattr(wb_repricer_bff_router, "save_source_cache", save_cache)
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache_meta_fields", meta_fields)
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache_range_revision", lambda org, prefix, **_kwargs:
                        max((cache.get("fetchedAt") for (scope, key), cache in storage.items()
                             if scope == org and key.startswith(prefix) and cache.get("fetchedAt")), default=None))
    monkeypatch.setattr(
        wb_repricer_bff_router,
        "get_source_cache_fetched_at",
        lambda organization_id, key: (storage.get((organization_id, key)) or {}).get("fetchedAt"),
    )
    monkeypatch.setattr(wb_repricer_bff_router, "list_cached_goods", lambda organization_id: deepcopy(goods) if organization_id == 1 else [])
    monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda organization_id: {"totalCached": len(goods) if organization_id == 1 else 0})
    monkeypatch.setattr(wb_repricer_bff_router, "get_wb_sync_status", lambda _organization_id: {})
    monkeypatch.setattr(wb_repricer_bff_router, "legacy_finance_tax_revision", lambda _org: "fixture-tax")
    monkeypatch.setattr(wb_repricer_bff_router, "get_legacy_finance_taxes", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(wb_repricer_bff_router, "_hydrate_org_repricer_state", lambda _request: 1)
    monkeypatch.setattr(repricer_bff_module, "get_settings", lambda: settings)
    monkeypatch.setattr(repricer_bff_module, "fetch_commission_tariffs", _fail("commission provider must not be called"))
    monkeypatch.setattr(repricer_bff_module, "build_wb_common_client", _fail("WB provider must not be called"))

    def seed_common() -> None:
        period = {str(good["nmID"]): {"ordersUnits": 0} for good in goods}
        finance = {
            str(good["nmID"]): {
                "salesUnits": 0,
                "returnsUnits": 0,
                "sellerRevenueKopecks": 0,
                "commissionPct": 10,
            }
            for good in goods
        }
        ads = {str(good["nmID"]): {"adImpressions": 0} for good in goods}
        storage[1, f"period_stats_{SUFFIX}"] = _source_payload(period)
        storage[1, f"finance_{SUFFIX}"] = _source_payload(
            finance,
            revenueBasis=FINANCE_REVENUE_BASIS,
            financeSchemaVersion=FINANCE_SCHEMA_VERSION,
        )
        storage[1, f"ads_{SUFFIX}"] = _source_payload(ads)
        storage[1, "stocks"] = {
            "fetchedAt": FETCHED_AT,
            "aggregates": {str(good["nmID"]): {"wbStockUnits": 10} for good in goods},
        }

    def request(*, baskets: dict | None, params: dict | None = None) -> dict:
        seed_common()
        if baskets is not None:
            storage[1, f"baskets_{SUFFIX}"] = deepcopy(baskets)
        api = TestClient(create_app())
        response = api.get(
            "/api/v1/wb-repricer/stats",
            params={"dateFrom": DATE_FROM, "dateTo": DATE_TO, "periodDays": 7, "topMode": "false", **(params or {})},
            headers=auth_headers(api, "viewer"),
        )
        assert response.status_code == 200, response.text
        return response.json()

    request.save_calls = save_calls
    return goods, storage, request


@pytest.mark.parametrize(
    "baskets,want,conversion",
    [(None, None, None), (0, 0, None), (7, 7, 42.9)],
)
def test_stats_mapper_does_not_substitute_synthetic_baskets(baskets, want, conversion):
    row = {
        "meta": {"basketsLast7d": 22},
        "analytics": {"baskets": baskets, "ordersUnits": 3},
    }
    metrics = wb_repricer_bff_router._repricer_stats_metrics(row)
    assert metrics["baskets"] == want
    assert metrics["cartToOrderCrPct"] == conversion


@pytest.mark.parametrize(
    "loaded,aggregates,mode,want_baskets,want_state,want_meta",
    [
        (False, {}, "real", None, "no_data", None),
        (True, {}, "real", None, "no_data", 0),
        (True, {"123456": {"cartCount": None}}, "real", None, "no_data", 0),
        (True, {"123456": {"cartCount": 0}}, "real", 0, "ok", 0),
        (False, {}, "fake", 20, "fallback", 20),
    ],
)
def test_cached_list_distinguishes_missing_zero_and_demo_fallback(
    monkeypatch, loaded, aggregates, mode, want_baskets, want_state, want_meta
):
    settings = replace(repricer_bff_module.get_settings(), wb_api_mode=mode)
    monkeypatch.setattr(repricer_bff_module, "get_settings", lambda: settings)
    monkeypatch.setattr(repricer_bff_module, "fetch_commission_tariffs", _fail("commission provider must not be called"))
    row = repricer_bff_module.list_repricer_skus(
        wb_token=None,
        include_promotions=False,
        include_content=False,
        cached_goods=[
            {
                "vendorCode": "DIRECT_UNKNOWN",
                "nmID": 123456,
                "sizes": [{"price": 1_000, "discountedPrice": 1_000}],
            }
        ],
        cached_content_cards=[],
        cached_promotions=[],
        cached_stock_aggregates={},
        cached_period_stats={},
        cached_finance_aggregates={},
        cached_ads_aggregates={},
        cached_baskets_aggregates=aggregates,
        baskets_cache_loaded=loaded,
        list_view=True,
        allow_commission_tariff_fetch=False,
    )[0]
    assert row["analytics"]["baskets"] == want_baskets
    assert row["analytics"]["basketsState"] == want_state
    if want_meta is not None:
        assert row["meta"]["basketsLast7d"] == want_meta


@pytest.mark.parametrize(
    "baskets_payload,want_baskets,want_conversion,want_state",
    [
        (None, None, None, "no_data"),
        (_source_payload({}, requestedNmIds=1, matchedNmIds=0), None, None, "no_data"),
        (_source_payload({"10001": {"cartCount": None, "orderCount": 3}}, requestedNmIds=1, matchedNmIds=1), None, None, "no_data"),
        (_source_payload({"10001": {"cartCount": 0, "orderCount": 0}}, requestedNmIds=1, matchedNmIds=1), 0, None, "ok"),
        (_source_payload({"10001": {"cartCount": 7, "orderCount": 3}}, requestedNmIds=1, matchedNmIds=1), 7, 42.9, "ok"),
    ],
)
def test_stats_endpoint_preserves_unknown_and_explicit_baskets(
    stats_runtime, baskets_payload, want_baskets, want_conversion, want_state
):
    _goods, _storage, request = stats_runtime
    payload = request(baskets=baskets_payload)
    item = payload["items"][0]
    assert item["metrics"]["baskets"] == want_baskets
    assert item["metrics"]["cartToOrderCrPct"] == want_conversion
    assert item["sources"]["states"]["baskets"] == want_state
    assert payload["summary"]["baskets"] == want_baskets
    assert payload["summary"]["cartToOrderCrPct"] == want_conversion
    if want_baskets is None:
        assert "baskets" in item["sources"]["missing"]
        assert "below_basket_norm" not in item["flags"]
        assert payload["cache"]["statsSourceStatus"] != "ready"


def test_fetched_empty_baskets_cache_retains_coverage_metadata(stats_runtime):
    _goods, storage, request = stats_runtime
    payload = request(
        baskets=_source_payload({}, requestedNmIds=1, matchedNmIds=0),
    )
    saved = storage[1, f"repricer_stats_baskets_{SUFFIX}"]
    assert saved["fetchedAt"] == FETCHED_AT
    assert saved["requestedNmIds"] == 1
    assert saved["matchedNmIds"] == 0
    assert saved["aggregates"] == {}
    assert payload["cache"]["basketsFetchedAt"] == FETCHED_AT
    assert payload["cache"]["basketsRequestedNmIds"] == 1
    assert payload["cache"]["basketsMatchedNmIds"] == 0


@pytest.mark.parametrize(
    "cart_count,order_count,want_conversion",
    [(7, 3, 42.9), (0, 0, None)],
)
def test_fetched_empty_baskets_cache_recovers_from_newer_observation_without_deletion(
    stats_runtime, cart_count, order_count, want_conversion
):
    goods, storage, request = stats_runtime
    source_key = f"baskets_{SUFFIX}"
    stats_key = f"repricer_stats_baskets_{SUFFIX}"
    empty = _source_payload(
        {},
        fetchedAt="2026-07-08T01:00:00+00:00",
        requestedNmIds=1,
        matchedNmIds=0,
    )

    first = request(baskets=empty)
    assert first["items"][0]["metrics"]["baskets"] is None
    assert first["cache"]["basketsFetchedAt"] == "2026-07-08T01:00:00+00:00"
    assert first["cache"]["basketsRequestedNmIds"] == 1
    assert first["cache"]["basketsMatchedNmIds"] == 0
    assert storage[1, stats_key]["aggregates"] == {}

    range_start, range_end, period_days, period_suffix = wb_repricer_bff_router._repricer_period_context(
        7,
        date.fromisoformat(DATE_FROM),
        date.fromisoformat(DATE_TO),
    )
    saves_before_repeat = len(request.save_calls)
    repeated = wb_repricer_bff_router._ensure_repricer_stats_period_caches(
        1,
        "complete",
        wb_token=None,
        resolved_period_days=period_days,
        period_suffix=period_suffix,
        range_start=range_start,
        range_end=range_end,
    )
    assert "baskets" in repeated["missingSources"]
    assert len(request.save_calls) == saves_before_repeat
    assert storage[1, stats_key]["fetchedAt"] == "2026-07-08T01:00:00+00:00"

    storage[1, source_key] = _source_payload(
        {"10001": {"cartCount": cart_count, "orderCount": order_count}},
        fetchedAt="2026-07-09T01:00:00+00:00",
        requestedNmIds=1,
        matchedNmIds=1,
    )
    recovered = request(baskets=None)
    recovered_item = recovered["items"][0]
    assert recovered_item["metrics"]["baskets"] == cart_count
    assert recovered_item["metrics"]["cartToOrderCrPct"] == want_conversion
    assert recovered_item["sources"]["states"]["baskets"] == "ok"
    assert recovered["summary"]["baskets"] == cart_count
    assert recovered["summary"]["cartToOrderCrPct"] == want_conversion
    assert recovered["cache"]["basketsFetchedAt"] == "2026-07-09T01:00:00+00:00"
    assert recovered["cache"]["basketsRequestedNmIds"] == 1
    assert recovered["cache"]["basketsMatchedNmIds"] == 1
    assert storage[1, stats_key]["dateFrom"] == DATE_FROM
    assert storage[1, stats_key]["dateTo"] == DATE_TO
    assert storage[1, stats_key]["aggregates"]["10001"]["cartCount"] == cart_count

    del storage[1, source_key]
    unavailable = request(baskets=None)
    assert unavailable["items"][0]["metrics"]["baskets"] == cart_count
    assert unavailable["cache"]["basketsFetchedAt"] == "2026-07-09T01:00:00+00:00"

    goods.append(_good(2))
    storage[1, source_key] = _source_payload(
        {"10001": {"cartCount": 99, "orderCount": 99}},
        fetchedAt="2026-07-08T02:00:00+00:00",
        requestedNmIds=2,
        matchedNmIds=1,
    )
    older = request(baskets=None)
    by_article = {item["articleId"]: item for item in older["items"]}
    assert by_article["STATS_01"]["metrics"]["baskets"] == cart_count
    assert by_article["STATS_02"]["metrics"]["baskets"] is None
    assert by_article["STATS_02"]["sources"]["states"]["baskets"] == "no_data"
    assert older["summary"]["baskets"] is None
    assert older["cache"]["basketsFetchedAt"] == "2026-07-09T01:00:00+00:00"


@pytest.mark.parametrize(
    "aggregates",
    [None, [], False, "broken", {"10001": []}, {"10001": ["broken"]}, {"10001": None}],
    ids=["null", "list", "boolean", "string", "empty-row-list", "row-list", "null-row"],
)
def test_newer_malformed_baskets_preserves_last_good_cache(stats_runtime, aggregates):
    _goods, storage, request = stats_runtime
    good = _source_payload(
        {"10001": {"cartCount": 7, "orderCount": 3}},
        requestedNmIds=1,
        matchedNmIds=1,
    )
    first = request(baskets=good)
    assert first["summary"]["baskets"] == 7
    cache_key = (1, f"repricer_stats_baskets_{SUFFIX}")
    saved = deepcopy(storage[cache_key])
    saves_before = list(request.save_calls)

    payload = request(baskets={
        **good,
        "fetchedAt": "2026-07-09T01:00:00+00:00",
        "aggregates": aggregates,
        "matchedNmIds": 0,
    })

    assert payload["items"][0]["metrics"]["baskets"] == 7
    assert payload["summary"]["baskets"] == 7
    assert payload["summary"]["cartToOrderCrPct"] == 42.9
    assert payload["cache"]["basketsFetchedAt"] == FETCHED_AT
    assert payload["cache"]["basketsMatchedNmIds"] == 1
    assert storage[cache_key] == saved
    assert request.save_calls == saves_before


def test_summary_is_unknown_when_missing_sku_is_outside_page(stats_runtime):
    goods, _storage, request = stats_runtime
    goods[:] = [_good(index) for index in range(1, 27)]
    baskets = {
        str(good["nmID"]): {"cartCount": 1, "orderCount": 0}
        for good in goods[:25]
    }
    payload = request(
        baskets=_source_payload(baskets, requestedNmIds=26, matchedNmIds=25),
        params={"pageSize": 25},
    )
    assert payload["itemsReturned"] == 25
    assert all(item["metrics"]["baskets"] == 1 for item in payload["items"])
    assert payload["summary"]["skuCount"] == 26
    assert payload["summary"]["baskets"] is None
    assert payload["summary"]["cartToOrderCrPct"] is None
    assert payload["cache"]["statsSourceStatus"] != "ready"


def test_filtered_summary_uses_only_known_selected_sku(stats_runtime):
    goods, _storage, request = stats_runtime
    goods[:] = [_good(1), _good(2)]
    payload = request(
        baskets=_source_payload(
            {"10001": {"cartCount": 7, "orderCount": 3}},
            requestedNmIds=2,
            matchedNmIds=1,
        ),
        params={"q": "STATS_01"},
    )
    assert payload["total"] == 1
    assert payload["summary"]["baskets"] == 7
    assert payload["summary"]["cartToOrderCrPct"] == 42.9


def test_stats_cache_rejects_wrong_tenant_and_malformed_range(stats_runtime):
    _goods, storage, request = stats_runtime
    storage[1, f"repricer_stats_baskets_{SUFFIX}"] = {
        **_source_payload({"10001": {"cartCount": 88, "orderCount": 3}}),
        "dateFrom": "not-a-date",
    }
    storage[2, f"repricer_stats_baskets_{SUFFIX}"] = _source_payload(
        {"10001": {"cartCount": 99, "orderCount": 3}}
    )
    payload = request(baskets=None)
    assert payload["items"][0]["metrics"]["baskets"] is None
    assert payload["summary"]["baskets"] is None
    assert payload["cache"]["basketsFetchedAt"] is None
    assert payload["cache"]["statsSourceStatus"] != "ready"


@pytest.mark.parametrize("params", [{"page": 2, "pageSize": 25}, {"q": "STATS_01"}, {"topMode": "true"}])
def test_stats_reuses_materialized_sources_for_metadata_and_pagination(stats_runtime, monkeypatch, params):
    goods, _storage, request = stats_runtime
    goods[:] = [_good(index) for index in range(1, 27)]
    request(baskets=_source_payload({str(good["nmID"]): {"cartCount": 1} for good in goods[:25]}))
    resolve = wb_repricer_bff_router._period_source_cache
    read = wb_repricer_bff_router.get_source_cache
    cache_reads = []
    def fresh_baskets_only(org, prefix, *args, **kwargs):
        assert prefix == "baskets", "prepared period sources must not be resolved again for metadata"
        return resolve(org, prefix, *args, **kwargs)
    def read_once(org, key, **kwargs):
        if key.startswith("repricer_stats_"):
            assert (org, key) not in cache_reads, "materialized source was deserialized twice"
            cache_reads.append((org, key))
        return read(org, key, **kwargs)
    monkeypatch.setattr(wb_repricer_bff_router, "_period_source_cache", fresh_baskets_only)
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache", read_once)
    monkeypatch.setattr(wb_repricer_bff_router, "get_covering_source_cache", _fail("prepared cache must not scan covering history"))
    payload = request(baskets=None, params=params)
    assert len(cache_reads) == 4
    assert (payload["dateFrom"], payload["dateTo"], payload["periodDays"]) == (DATE_FROM, DATE_TO, 7)
    assert payload["summary"]["skuCount"] == (1 if params.get("q") else 26)
    assert payload["summary"]["baskets"] == (1 if params.get("q") else None)
    assert payload["itemsReturned"] == (1 if params.get("q") or params.get("page") == 2 else 26)


def test_list_metadata_preserves_default_resolution_and_accepts_prepared_sources(stats_runtime, monkeypatch):
    _goods, storage, request = stats_runtime
    request(baskets=_source_payload({"10001": {"cartCount": 7}}, coverageState="partial", coveredDays=6,
                                    missingDates=[DATE_TO]))
    sources = {prefix: deepcopy(storage[1, f"{prefix}_{SUFFIX}"]) for prefix in ("period_stats", "finance", "ads", "baskets")}
    calls = []
    def resolve(org, prefix, *_args, **_kwargs):
        assert org == 1
        calls.append(prefix)
        return sources[prefix]
    monkeypatch.setattr(wb_repricer_bff_router, "_period_source_cache", resolve)
    kwargs = dict(include_content=True, date_from=date.fromisoformat(DATE_FROM), date_to=date.fromisoformat(DATE_TO),
                  require_full_sync_coverage=False)
    expected = wb_repricer_bff_router._repricer_list_cache_meta(1, 7, **kwargs)
    assert calls == ["period_stats", "finance", "ads", "baskets"]
    monkeypatch.setattr(wb_repricer_bff_router, "_period_source_cache", _fail("prepared metadata must not resolve original sources"))
    actual = wb_repricer_bff_router._repricer_list_cache_meta(1, 7, period_caches=sources, **kwargs)
    assert actual == expected
    assert (actual["basketsCoverageState"], actual["basketsCoveredDays"], actual["basketsMissingDates"]) == ("partial", 6, [DATE_TO])


def test_stats_next_page_reuses_rows_and_whole_catalog_summary(stats_runtime, monkeypatch):
    goods, _storage, request = stats_runtime
    goods[:] = [_good(index) for index in range(1, 177)]
    monkeypatch.setattr(wb_repricer_bff_router, "get_repricer_sources_revision", lambda _org: "source-1")
    first = request(baskets=_source_payload({str(good["nmID"]): {"cartCount": 0} for good in goods[:-1]}),
                    params={"topMode": "true", "pageSize": 150})
    monkeypatch.setattr(wb_repricer_bff_router, "_ensure_repricer_stats_period_caches", _fail("page 2 must not decode source payloads"))
    monkeypatch.setattr(wb_repricer_bff_router, "list_repricer_skus", _fail("page 2 must not rebuild the catalog"))
    second = request(baskets=None, params={"topMode": "true", "pageSize": 150, "page": 2})
    assert second["summary"] == first["summary"]
    assert second["summary"]["baskets"] is None
    assert second["summary"]["skuCount"] == second["total"] == 176
    assert second["itemsReturned"] == 26
    assert len({item["articleId"] for item in first["items"] + second["items"]}) == 176
    assert second["cache"]["statsPage"] == second["page"] == 2
    assert second["cache"]["statsItemsLimit"] == second["pageSize"] == 150


@pytest.mark.parametrize("changed_input", ["source", "goods", "algorithm", "settings", "assignments", "templates", "tax_policy", "missing_chunk", "mixed_generation"])
def test_stats_snapshot_rebuilds_when_inputs_change(stats_runtime, monkeypatch, changed_input):
    _goods, storage, request = stats_runtime
    revision = ["source-1"]
    tax_revision = ["policy-1"]
    monkeypatch.setattr(wb_repricer_bff_router, "get_repricer_sources_revision", lambda _org: revision[0])
    monkeypatch.setattr(wb_repricer_bff_router, "legacy_finance_tax_revision", lambda _org: tax_revision[0], raising=False)
    request(baskets=_source_payload({"10001": {"cartCount": 0}}), params={"topMode": "true"})
    build = wb_repricer_bff_router.list_repricer_skus
    builds = []
    def record_build(*args, **kwargs):
        builds.append(True)
        return build(*args, **kwargs)
    monkeypatch.setattr(wb_repricer_bff_router, "list_repricer_skus", record_build)
    if changed_input == "source":
        revision[0] = "source-2"
    elif changed_input == "goods":
        monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda _org: {"totalCached": 1, "latestFetchedAt": "new-goods"})
    elif changed_input == "algorithm":
        monkeypatch.setitem(repricer_bff_module.ALGORITHM_SETTINGS_STATE, "taxPct", 9)
    elif changed_input == "settings":
        monkeypatch.setitem(repricer_bff_module.SKU_SETTINGS_OVERRIDES, "STATS_01", {"pMinKopecks": 1})
    elif changed_input == "assignments":
        monkeypatch.setitem(repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS, "STATS_01", {"strategyId": "baskets_orders", "source": "manual"})
    elif changed_input == "templates":
        monkeypatch.setitem(repricer_bff_module.TEMPLATES_STATE, "globalCommissionPct", 11)
    elif changed_input == "tax_policy":
        tax_revision[0] = "policy-2"
    else:
        key = next(key for key in storage if key[1].startswith("stats_list_snapshot_") and "_chunk_" in key[1])
        if changed_input == "missing_chunk":
            del storage[key]
        else:
            storage[key]["generation"] = "other-request"
    payload = request(baskets=None, params={"topMode": "true"})
    assert len(builds) == 1
    assert payload["itemsReturned"] == payload["total"] == 1
    if changed_input == "settings":
        assert "p_min" not in payload["items"][0]["priceProtection"]["blockerIds"]
    if changed_input == "assignments":
        assert payload["items"][0]["strategyId"] == "baskets_orders"


@pytest.mark.parametrize("params", [{"q": "STATS_01"}, {"topMode": "false"}])
def test_stats_snapshot_does_not_replace_filtered_or_catalog_order_requests(stats_runtime, monkeypatch, params):
    goods, _storage, request = stats_runtime
    goods[:] = [_good(1), _good(2)]
    monkeypatch.setattr(wb_repricer_bff_router, "get_repricer_sources_revision", lambda _org: "source-1")
    request(baskets=_source_payload({"10001": {"cartCount": 1}, "10002": {"cartCount": 20}}), params={"topMode": "true"})
    actual = request(baskets=None, params={"topMode": "true", **params})
    assert actual["items"][0]["articleId"] == "STATS_01"
    assert actual["summary"]["skuCount"] == (1 if params.get("q") else 2)
    assert actual["summary"]["baskets"] == (1 if params.get("q") else 21)


def test_stats_reuses_products_snapshot_with_identical_metrics_and_source_dates(stats_runtime, monkeypatch):
    goods, _storage, request = stats_runtime
    goods[:] = [_good(1), _good(2)]
    monkeypatch.setattr(wb_repricer_bff_router, "get_repricer_sources_revision", lambda _org: "source-1")
    request(baskets=_source_payload({"10001": {"cartCount": 4}}))
    start, end, days, suffix = wb_repricer_bff_router._repricer_period_context(7, date.fromisoformat(DATE_FROM), date.fromisoformat(DATE_TO))
    product_snapshot = wb_repricer_bff_router._build_repricer_sku_snapshot(
        1, "complete", wb_token=None, resolved_period_days=days, period_suffix=suffix,
        range_start=start, range_end=end,
    )
    _, products = wb_repricer_bff_router._load_repricer_sku_snapshot_page(
        1, "complete", period_suffix=suffix, include_promotions=False, include_content=False,
        page=1, page_size=150,
    )
    from app.wb_statistics import project_row, apply_account_totals
    ads = _storage[1, f"ads_{SUFFIX}"]
    expected = [wb_repricer_bff_router._repricer_stats_item(project_row(row, ads)) for row in products]
    monkeypatch.setattr(wb_repricer_bff_router, "_ensure_repricer_stats_period_caches", _fail("fresh Products rows must share their source basis"))
    monkeypatch.setattr(wb_repricer_bff_router, "list_repricer_skus", _fail("existing Products rows must not be rebuilt"))
    actual = request(baskets=None, params={"topMode": "true"})
    assert actual["items"] == expected
    expected_summary = wb_repricer_bff_router._repricer_stats_summary(expected)
    apply_account_totals(expected_summary, ads)
    assert actual["summary"] == expected_summary
    for key in ("periodStatsFetchedAt", "financeFetchedAt", "adsFetchedAt", "basketsFetchedAt", "stocksFetchedAt"):
        assert actual["cache"][key] == product_snapshot["cache"][key]


@pytest.mark.parametrize("prefix,source_field,metric,cache_field,value", [
    ("period_stats", "ordersUnits", "orders", "periodStatsFetchedAt", 17),
    ("ads", "adClicks", "clicks", "adsFetchedAt", 23),
    ("finance", "sellerRevenueKopecks", "revenueKopecks", "financeFetchedAt", 12000),
])
def test_stats_replaces_older_materialization_when_source_changes(
    stats_runtime, monkeypatch, prefix, source_field, metric, cache_field, value,
):
    _goods, storage, request = stats_runtime
    request(baskets=_source_payload({"10001": {"cartCount": 1}}))
    newer = deepcopy(storage[1, f"{prefix}_{SUFFIX}"])
    newer["fetchedAt"] = "2026-07-09T01:00:00+00:00"
    newer["aggregates"]["10001"][source_field] = value
    read = wb_repricer_bff_router.get_source_cache
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache", lambda org, key, **kwargs:
                        deepcopy(newer) if (org, key) == (1, f"{prefix}_{SUFFIX}") else read(org, key, **kwargs))
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache_range_revision",
                        lambda _org, source_key_prefix, **_kwargs: newer["fetchedAt"] if source_key_prefix == f"{prefix}_" else FETCHED_AT,
                        raising=False)
    actual = request(baskets=None)
    assert actual["items"][0]["metrics"][metric] == value
    assert actual["cache"][cache_field] == newer["fetchedAt"]


@pytest.mark.parametrize("prefix,cache_field", [("period_stats", "periodStatsFetchedAt"),
                                               ("finance", "financeFetchedAt"), ("ads", "adsFetchedAt")])
@pytest.mark.parametrize("aggregates", [{}, {"10001": "malformed"}])
def test_stats_accepts_empty_observation_and_retains_last_valid_source(stats_runtime, monkeypatch, prefix, cache_field, aggregates):
    _goods, storage, request = stats_runtime
    request(baskets=_source_payload({"10001": {"cartCount": 1}}))
    previous = deepcopy(storage[1, f"repricer_stats_{prefix}_{SUFFIX}"])
    newer = {**storage[1, f"{prefix}_{SUFFIX}"], "fetchedAt": "2026-07-09T01:00:00+00:00", "aggregates": aggregates}
    resolve = wb_repricer_bff_router._period_source_cache
    monkeypatch.setattr(wb_repricer_bff_router, "_period_source_cache", lambda org, source, *args, **kwargs:
                        deepcopy(newer) if source == prefix else resolve(org, source, *args, **kwargs))
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache_range_revision", lambda *_args, **_kwargs: newer["fetchedAt"])
    actual = request(baskets=None)
    materialized = storage[1, f"repricer_stats_{prefix}_{SUFFIX}"]
    expected = newer if aggregates == {} else previous
    assert materialized["aggregates"] == expected["aggregates"]
    assert actual["cache"][cache_field] == expected["fetchedAt"]


def test_stats_materialization_keeps_source_time_after_database_overrides_cache_time(stats_runtime, monkeypatch):
    _goods, storage, request = stats_runtime
    first = request(baskets=_source_payload({"10001": {"cartCount": 1}}))
    read = wb_repricer_bff_router.get_source_cache
    def database_read(org, key, **kwargs):
        cached = read(org, key, **kwargs)
        if cached and key.startswith("repricer_stats_"):
            cached["fetchedAt"] = "2026-07-09T01:00:00+00:00"
        return cached
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache", database_read)
    second = request(baskets=None)
    for field in ("periodStatsFetchedAt", "financeFetchedAt", "adsFetchedAt", "basketsFetchedAt"):
        assert second["cache"][field] == first["cache"][field] == FETCHED_AT


@pytest.mark.parametrize("buyout", [None, 0, 93.5])
def test_stats_exposes_trusted_buyout_without_recalculating_from_orders(buyout):
    metrics = wb_repricer_bff_router._repricer_stats_metrics({"analytics": {
        "buyoutPct": buyout, "ordersUnits": 1, "salesUnits": 12,
    }})
    assert metrics["buyoutPct"] == buyout


@pytest.mark.parametrize("view", ["sku", "stats"])
def test_rolling_snapshot_expires_at_moscow_midnight(monkeypatch, view):
    today = [datetime(2026, 9, 15, 20, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(repricer_sync, "_utc_now", lambda: today[0])
    monkeypatch.setattr(wb_repricer_bff_router, "get_repricer_sources_revision", lambda _org: "source")
    monkeypatch.setattr(wb_repricer_bff_router, "cached_goods_meta", lambda _org: {"latestFetchedAt": "goods"})
    monkeypatch.setattr(wb_repricer_bff_router, "legacy_finance_tax_revision", lambda _org: "tax", raising=False)
    period = {"dateFrom": "2026-08-17", "dateTo": "2026-09-15"}
    snapshot = {"version": wb_repricer_bff_router.SKU_LIST_SNAPSHOT_VERSION, "items": [],
                "stateRevision": wb_repricer_bff_router._repricer_view_state_revision(),
                "sourceRevision": "source", "goodsRevision": "goods", "taxRevision": "tax",
                **({"response": period} if view == "stats" else period)}
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache", lambda *_args, **_kwargs: deepcopy(snapshot))
    options = dict(period_suffix="30", include_promotions=False, include_content=False, view=view)
    assert wb_repricer_bff_router._load_repricer_sku_snapshot(1, "complete", **options) is not None
    today[0] += timedelta(minutes=1)
    assert wb_repricer_bff_router._load_repricer_sku_snapshot(1, "complete", **options) is None


@pytest.mark.parametrize("view", ["stats", "products"])
def test_newer_covering_daily_source_replaces_older_exact_period(stats_runtime, monkeypatch, view):
    _goods, storage, request = stats_runtime
    request(baskets=_source_payload({"10001": {"cartCount": 1}}))
    newer = _source_payload({}, dateTo="2026-07-08", fetchedAt="2026-07-09T01:00:00+00:00", dailyAggregates={
        f"2026-07-{day:02d}": {"10001": {"adClicks": 2}} for day in range(1, 9)
    })
    monkeypatch.setattr(wb_repricer_bff_router, "get_source_cache_range_revision", lambda _org, prefix, **_kwargs:
                        newer["fetchedAt"] if prefix == "ads_" else FETCHED_AT)
    cover_reads = []
    def covering(_org, prefix, **_kwargs):
        cover_reads.append(prefix)
        return deepcopy(newer) if prefix == "ads_" else None
    monkeypatch.setattr(wb_repricer_bff_router, "get_covering_source_cache", covering)
    if view == "products":
        start, end, days, suffix = wb_repricer_bff_router._repricer_period_context(7, date.fromisoformat(DATE_FROM), date.fromisoformat(DATE_TO))
        wb_repricer_bff_router._build_repricer_sku_snapshot(1, "complete", wb_token=None, resolved_period_days=days,
                                                         period_suffix=suffix, range_start=start, range_end=end)
    actual = request(baskets=None, params={"topMode": str(view == "products").lower()})
    assert actual["items"][0]["metrics"]["clicks"] == 14
    assert actual["cache"]["adsFetchedAt"] == newer["fetchedAt"]
    assert cover_reads == ["ads_"] * (2 if view == "products" else 1)


def test_products_rows_summary_and_stats_share_one_dated_tax_resolution(stats_runtime, monkeypatch):
    goods, _storage, request = stats_runtime
    goods[:] = [_good(index) for index in range(1, 177)]
    request(baskets=_source_payload({str(good["nmID"]): {"cartCount": 1} for good in goods}))
    calls = []
    def taxes(org, cache, period, **_kwargs):
        calls.append((org, cache["fetchedAt"], period.cache_key))
        return {key: {"taxKopecks": 0, "factTaxState": "configured", "factTaxReason": None}
                for key in cache["aggregates"]}
    monkeypatch.setattr(wb_repricer_bff_router, "get_legacy_finance_taxes", taxes)
    start, end, days, suffix = wb_repricer_bff_router._repricer_period_context(7, date.fromisoformat(DATE_FROM), date.fromisoformat(DATE_TO))
    wb_repricer_bff_router._build_repricer_sku_snapshot(1, "complete", wb_token=None, resolved_period_days=days,
                                                     period_suffix=suffix, range_start=start, range_end=end)
    payload = request(baskets=None, params={"topMode": "true", "pageSize": 500})
    assert calls == [(1, FETCHED_AT, SUFFIX)]
    assert len(payload["items"]) == 176
    for item in payload["items"]:
        assert item["metrics"]["taxKopecks"] == 0
        assert item["metrics"]["factTaxState"] == "configured"
        assert item["metrics"]["factTaxReason"] is None


@pytest.mark.parametrize("tax,profit,state,reason", [(None, None, "missing", "tax_policy_unconfirmed"),
                                                   (0, 0, "configured", None)])
def test_stats_keeps_actual_tax_and_profit_unknown_without_planned_substitution(tax, profit, state, reason):
    actual = wb_repricer_bff_router._repricer_stats_metrics({"analytics": {
        "taxKopecks": tax, "factNetProfitKopecks": profit, "factTaxState": state, "factTaxReason": reason,
        "plannedTaxKopecks": 1_000, "plannedMarginKopecks": 5_000,
    }})
    assert (actual["taxKopecks"], actual["factNetProfitKopecks"], actual["factTaxState"], actual["factTaxReason"]) == (tax, profit, state, reason)


@pytest.mark.parametrize("products_ready", [False, True])
def test_stats_retries_transient_tax_read_without_waiting_for_source_revision(stats_runtime, monkeypatch, products_ready):
    _goods, _storage, request = stats_runtime
    request(baskets=_source_payload({"10001": {"cartCount": 1}}))
    monkeypatch.setattr(wb_repricer_bff_router, "get_legacy_finance_taxes", lambda *_args, **_kwargs: {
        "10001": {"taxKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_policy_unavailable"},
    })
    if products_ready:
        start, end, days, suffix = wb_repricer_bff_router._repricer_period_context(7, date.fromisoformat(DATE_FROM), date.fromisoformat(DATE_TO))
        wb_repricer_bff_router._build_repricer_sku_snapshot(1, "complete", wb_token=None, resolved_period_days=days,
                                                         period_suffix=suffix, range_start=start, range_end=end)
    unavailable = request(baskets=None, params={"topMode": "true"})
    assert unavailable["items"][0]["metrics"]["taxKopecks"] is None
    assert unavailable["items"][0]["metrics"]["factTaxReason"] == "tax_policy_unavailable"
    monkeypatch.setattr(wb_repricer_bff_router, "get_legacy_finance_taxes", lambda *_args, **_kwargs: {
        "10001": {"taxKopecks": 0, "factTaxState": "configured", "factTaxReason": None},
    })
    recovered = request(baskets=None, params={"topMode": "true"})
    assert recovered["items"][0]["metrics"]["taxKopecks"] == 0
    assert recovered["items"][0]["metrics"]["factTaxState"] == "configured"
    monkeypatch.setattr(wb_repricer_bff_router, "get_legacy_finance_taxes", _fail("recovered snapshot must reuse resolved tax"))
    assert request(baskets=None, params={"topMode": "true"})["items"] == recovered["items"]
