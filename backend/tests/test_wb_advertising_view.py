from copy import deepcopy
from datetime import date

from app.wb_advertising_view import advertising_view, enrich_advertising_products
from app.wb_api import ads_runtime
from app.wb_api.client import FakeWbApiClient
from app.routers.wb_reports_bff import _map_ads_to_report_response


def report():
    return {"filters": {"dateRange": {"from": "2026-10-01", "to": "2026-10-09"}},
        "rows": [{"campaignId": "42", "nmId": 0, "adSpendKopecks": 700,
                  "impressions": 17, "clicks": 2, "baskets": 3, "orders": {"units": 1, "kopecks": 20000}}],
        "campaignMembership": {"42": [101], "43": [101, 102]},
        "dailyRows": [{"campaignId": "42", "skuId": "101", "date": "2026-10-01", "appType": 1,
                       "adSpendKopecks": 600, "impressions": 15, "clicks": 1, "cartAdds": 2, "ordersCount": 1, "ordersKopecks": 20000}]}


def test_ids_are_external_and_membership_is_distinct_from_period_attribution():
    source = report()
    original = deepcopy(source)
    result = advertising_view(source)
    assert source == original
    product = result["rows"][0]
    assert product["nmId"] == 101
    assert product["campaignId"] == "42"
    assert product["campaignIds"] == ["42", "43"]
    assert product["directCampaignIds"] == ["42", "43"]
    assert product["attributedCampaignIds"] == ["42"]
    assert "историческое" in product["campaignMappingNote"]
    assert advertising_view(result) == result


def test_detail_and_residual_do_not_double_count_campaign_totals():
    rows = advertising_view(report())["rows"]
    assert len(rows) == 2
    assert [row["adSpendKopecks"] for row in rows] == [600, 100]
    assert sum(row["impressions"] for row in rows) == 17
    assert rows[1]["nmId"] == 0
    assert rows[1]["rowKind"] == "campaign-residual"
    assert rows[0]["sales"] == {"units": None, "kopecks": None}


def test_boundaries_duplicates_platforms_and_missing_metrics():
    source = report()
    first = source["dailyRows"][0]
    source["dailyRows"] += [deepcopy(first), {**first, "date": "2026-10-09"},
                           {**first, "date": "2026-09-30"}, {**first, "date": "2026-10-10"}]
    rows = advertising_view(source)["rows"]
    assert rows[0]["adSpendKopecks"] == 1200  # both inclusive boundaries, duplicate ignored
    assert rows[1]["adSpendKopecks"] == -500  # signed reconciliation, not clipped
    source["dailyRows"].append({**first, "clicks": None, "appType": 32})
    assert advertising_view(source)["rows"][0]["clicks"] is None


def test_conflicting_duplicate_is_not_silently_selected():
    source = report()
    source["dailyRows"].append({**source["dailyRows"][0], "adSpendKopecks": 999})
    assert advertising_view(source)["rows"][0]["adSpendKopecks"] is None
    source["rows"][0]["orders"] = None
    assert advertising_view(source)["rows"][-1]["orders"] == {"units": None, "kopecks": None}


def test_multiple_period_campaigns_and_unavailable_mapping_do_not_guess_by_name():
    source = report()
    source["rows"] += [{"campaignId": "44", "nmId": 101},
                       {"campaignId": "unknown", "nmId": 102, "sku": "42", "campaignName": "42"}]
    result = advertising_view(source)
    assert result["rows"][0]["campaignIds"] == ["42", "43", "44"]
    last = result["rows"][-1]
    assert last["campaignId"] is None
    assert last["campaignMappingState"] == "current-membership-only"
    assert last["attributedCampaignIds"] == []
    assert last["campaignIds"] == ["43"]  # independently known current membership only


def test_official_adverts_and_fullstats_flow_uses_existing_batched_reads(monkeypatch):
    fake = FakeWbApiClient(fixtures={
        "/adv/v1/promotion/count": {"adverts": [{"type": 9, "status": 9, "advert_list": [42, 43]}]},
        "/adv/v1/balance": {}, "/adv/v1/upd": [],
        "/api/advert/v2/adverts": {"adverts": [{"id": 42, "nm_settings": [{"nm_id": 101}], "settings": {"name": "Same name"}},
                                                {"id": 43, "nm_settings": [{"nm_id": 101}], "settings": {"name": "Same name"}}]},
        "/adv/v3/fullstats": [{"advertId": 42, "sum": 6, "views": 15, "clicks": 0, "atbs": 2, "orders": 1, "sum_price": 200,
            "days": [{"date": "2026-10-01", "apps": [{"appType": 1, "nms": [{"nmId": 101, "name": "Real WB product title", "sum": 6, "views": 15,
                "clicks": 0, "atbs": 2, "orders": 1, "sum_price": 200}]}]}]}],
    })
    monkeypatch.setattr(ads_runtime, "build_wb_ads_client", lambda **kw: fake)
    snapshot = ads_runtime.build_ads_attribution_snapshot(date_from=date(2026, 10, 1), date_to=date(2026, 10, 9), group_by="campaign", include_budgets=False)
    payload = _map_ads_to_report_response({"from": "2026-10-01", "to": "2026-10-09"}, snapshot)
    assert payload["rows"][0]["nmId"] == 101
    assert payload["rows"][0]["campaignIds"] == ["42", "43"]
    assert payload["rows"][0]["adSpendKopecks"] == 600
    assert payload["rows"][0]["productName"] == "Real WB product title"
    assert len([item for item in fake.requests if item.path == "/api/advert/v2/adverts"]) == 1
    assert len([item for item in fake.requests if item.path == "/adv/v3/fullstats"]) == 1
    assert all(item.method == "GET" for item in fake.requests)


def test_old_cached_report_mapping_is_upgraded_without_provider_or_recollection(monkeypatch):
    from app.routers import wb_reports_bff as reports
    from types import SimpleNamespace
    monkeypatch.setattr(reports, "get_ads_report_cache", lambda **kw: report())
    monkeypatch.setattr(reports, "_catalog_meta_by_nm", lambda org, **kw: {101: {"productName": "Saved catalog title", "sku": "REAL-ARTICLE"}})
    monkeypatch.setattr(reports, "build_cached_ads_attribution_snapshot", lambda **kw: (_ for _ in ()).throw(AssertionError("must reuse cache")))
    result = reports._build_ads_report_payload(actor=SimpleNamespace(organization_id=1), date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 9), date_range={"from": "2026-10-01", "to": "2026-10-09"}, wb_token=None)
    assert result["rows"][0]["nmId"] == 101
    assert result["rows"][0]["campaignIds"] == ["42", "43"]
    assert result["rows"][0]["productName"] == "Saved catalog title"
    assert result["rows"][0]["sku"] == "REAL-ARTICLE"


def test_catalog_join_is_by_nm_id_not_seller_article_and_does_not_mutate_cached_report():
    source = report()
    original = deepcopy(source)
    result = enrich_advertising_products(source, {
        999: {"sku": "101", "productName": "Wrong product"},
        101: {"sku": "REAL-ARTICLE", "productName": "Correct complete product title", "photoUrl": "https://example.test/photo.jpg"},
    })
    assert result["rows"][0]["productName"] == "Correct complete product title"
    assert result["rows"][0]["sku"] == "REAL-ARTICLE"
    assert source == original
    assert enrich_advertising_products(source, {})["rows"][0]["productName"] is None
    assert result["rows"][1]["productName"] == "Нераспределённая часть РК"


def test_product_title_metadata_does_not_turn_duplicate_metrics_into_conflicts():
    source = report()
    source["dailyRows"].append({**source["dailyRows"][0], "productName": "Complete WB title"})
    row = advertising_view(source)["rows"][0]
    assert row["impressions"] == 15
    assert row["adSpendKopecks"] == 600
    assert row["productName"] == "Complete WB title"


def test_advertising_catalog_is_a_batched_cache_read_without_costs_or_provider_requests(monkeypatch):
    from app.routers import wb_reports_bff as reports
    from app.platform.economics import legacy_catalog
    calls = []
    monkeypatch.setattr(reports, "list_cached_goods", lambda org: [{"nmID": 101, "vendorCode": "ARTICLE"}])
    def cached(org, key, **kw):
        calls.append((org, key))
        return {"cards": [{"nmID": 101, "title": "Complete saved card title", "vendorCode": "ARTICLE"}]}
    monkeypatch.setattr(reports, "get_source_cache", cached)
    def metadata(org, *, include_costs=True):
        assert include_costs is False
        return {102: {"productName": "Saved technical catalog title", "sku": "ARTICLE-2"}}
    monkeypatch.setattr(legacy_catalog, "catalog_facts", metadata)
    result = reports._catalog_meta_by_nm(77, include_shared_facts=False)
    assert result[101]["productName"] == "Complete saved card title"
    assert result[102]["productName"] == "Saved technical catalog title"
    assert calls == [(77, "content_cards")]


def test_mapping_survives_database_cache_roundtrip_and_does_not_change_shared_metrics(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.wb_ads_cache import store
    from app.wb_ads_cache.orm import WbAdsReportCacheRow
    from app import wb_report_ads

    original = report()
    original["performanceComplete"] = True
    mapped = advertising_view(original)
    shared = []
    monkeypatch.setattr(wb_report_ads, "save_source_cache", lambda org, key, payload: shared.append(payload))
    for value in (original, mapped):
        wb_report_ads.save_campaign_performance(1, date(2026, 10, 1), date(2026, 10, 9), value)
    assert shared[0] == shared[1]  # Statistics/Repricer inputs remain untouched.

    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        WbAdsReportCacheRow.__table__.create(engine)
        monkeypatch.setattr(store, "get_engine", lambda: engine)
        monkeypatch.setattr(store, "get_session_factory", lambda: sessionmaker(bind=engine))
        monkeypatch.setattr(store, "_MEMORY_REPORT_CACHE", {})
        scope = dict(organization_id=1, date_from=date(2026, 10, 1), date_to=date(2026, 10, 9), group_by="campaign")
        store.save_ads_report_cache(**scope, payload=mapped)
        loaded = store.get_ads_report_cache(**scope)
        assert store._MEMORY_REPORT_CACHE == {}  # Must be a real SQL roundtrip, not memory fallback.
        assert loaded["campaignMembership"] == {"42": [101], "43": [101, 102]}
        assert loaded["rows"][0]["campaignIds"] == ["42", "43"]
        assert store.get_ads_report_cache(**{**scope, "date_to": date(2026, 10, 8)}) is None
    finally:
        engine.dispose()
