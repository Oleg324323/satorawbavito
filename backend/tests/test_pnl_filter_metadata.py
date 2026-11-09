"""Read-only P&L attributes: scope, exact periods and unknown-versus-zero."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app import wb_pnl_filters as filters
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.repricer_persistence.orm import WbRepricerRuntimeStateRow, WbRepricerChangelogRow
from tests.test_legacy_finance_tax import tax_db  # noqa: F401

START, END = date(2026, 10, 5), date(2026, 10, 11)
CONTEXT = dict(marketplaceAccountId=12, connectedAt="2026-01-01T00:00:00+00:00")


@pytest.fixture
def saved(tax_db, monkeypatch):
    monkeypatch.setattr(filters, "get_session_factory", lambda: lambda: tax_db)
    monkeypatch.setattr(filters, "_source_context", lambda org: CONTEXT if org == 1 else None)
    tax_db.add_all([
        WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=100,
            fetched_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
            goods_payload=[{"nmID": 123, "vendorCode": "TW55", "discountedPrice": 123.45, "brand": "Brand"}]),
        WbRepricerRuntimeStateRow(organization_id=1, state_payload={
            "skuMetaOverrides": {"TW55": {"managerId": "mine", "managerName": "Manager"}},
            "skuSettingsOverrides": {"TW55": {"automationEnabled": False, "pMinKopecks": 0}},
            "assignments": {"TW55": {"strategyId": "test", "strategyName": "Saved strategy"}},
        }),
        WbRepricerChangelogRow(organization_id=1, entry_id="test", article_id="TW55",
            created_at=datetime.now(timezone.utc), entry_payload={"timestamp": datetime.now(timezone.utc).isoformat(), "oldPriceKopecks": 0, "newPriceKopecks": 10}),
    ])
    tax_db.commit()
    caches = {
        "content_cards": {"cards": [{"nmID": 123, "subjectName": "Футболки", "characteristics": [
            {"name": "Пол", "value": ["Женский"]}, {"name": "Цвет", "value": ["черный"]}]}]},
        f"baskets_{START}_{END}": {"dateFrom": str(START), "dateTo": str(END), "aggregateComplete": True,
            "aggregates": {"123": {"cartCount": 0, "orderCount": 2, "wbStockUnits": 0}}},
        "promotions": {"promotions": [{"status": "active", "nmIds": [123]}]},
    }
    calls = []
    def get(org, key, **kwargs):
        calls.append((org, key))
        value = caches.get(key)
        return {**value, "fetchedAt": "2026-10-29T00:00:00Z"} if value is not None else None
    monkeypatch.setattr(filters, "get_source_cache", get)
    monkeypatch.setattr(filters, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [])
    return tax_db, caches, calls


def load():
    return filters.load_filter_metadata(1, filters.PnlFilterRequest(dateFrom=START, dateTo=END, nmIds=[123]))


def test_saved_sources_and_real_category_not_article_prefix(saved):
    item = load()["products"]["123"]
    assert item == dict(managerId="mine", managerName="Manager", managerKnown=True, brand="Brand", status="manual",
        promotion=True, garment="shirt", attrs=["black", "female"], strategyId="test", strategyName="Saved strategy",
        stockUnits=0, currentPriceKopecks=12345, pminKopecks=None, changed24h=True, baskets=0, orders=2)
    assert all(org == 1 for org, _ in saved[2])
    assert len(saved[2]) <= 6


@pytest.mark.parametrize("change", ["partial", "wrong_period", "old_binding", "wrong_account"])
def test_ineligible_period_stats_are_unknown(saved, change):
    cache = saved[1][f"baskets_{START}_{END}"]
    if change == "partial": cache["aggregateComplete"] = False
    if change == "wrong_period": cache["dateFrom"] = "2026-10-04"
    if change == "old_binding": cache["fetchedAt"] = "2025-01-01T00:00:00Z"
    if change == "wrong_account": cache["marketplaceAccountId"] = 999
    # The helper's injected timestamp must not replace an explicit source timestamp.
    if change == "old_binding":
        original = filters.get_source_cache
        filters.get_source_cache = lambda org, key, **kw: {**(original(org, key, **kw) or {}), **({"fetchedAt": cache["fetchedAt"]} if key.startswith("baskets_") else {})}
    item = load()["products"]["123"]
    assert item["orders"] is None and item["baskets"] is None


def test_missing_sources_do_not_fabricate_zero_or_type(saved):
    saved[1].clear()
    item = load()["products"]["123"]
    assert item["promotion"] is None and item["stockUnits"] is None
    assert item["orders"] is None and item["baskets"] is None and item["garment"] is None


def test_real_goods_size_prices_keep_original_kopecks(saved):
    page = saved[0].query(WbRepricerGoodsCacheRow).first()
    page.goods_payload = [{"nmID": 123, "vendorCode": "TW55", "sizes": [{"price": 999, "discountedPrice": 123.45}]}]
    saved[0].commit()
    assert load()["products"]["123"]["currentPriceKopecks"] == 12345


@pytest.mark.parametrize("source,expected", [("local", 45000), ("wb_api_pending", 12345)])
def test_price_filter_follows_saved_local_override_precedence(saved, source, expected):
    row = saved[0].get(WbRepricerRuntimeStateRow, 1)
    row.state_payload = {"skuMetaOverrides": {"TW55": {"currentPriceKopecks": 45000, "localPriceOverrideSource": source}}}
    saved[0].commit()
    assert load()["products"]["123"]["currentPriceKopecks"] == expected


def test_missing_explicit_floor_does_not_claim_missing_calculated_minimum(saved, monkeypatch):
    assert load()["products"]["123"]["pminKopecks"] is None
    monkeypatch.setattr(filters, "_saved_floors", lambda *args: {123: 12345})
    assert load()["products"]["123"]["pminKopecks"] == 12345


def test_explicit_positive_minimum_is_reused_without_calculations(saved):
    row = saved[0].get(WbRepricerRuntimeStateRow, 1)
    row.state_payload = {"skuSettingsOverrides": {"TW55": {"pMinKopecks": 7654}}}
    saved[0].commit()
    assert load()["products"]["123"]["pminKopecks"] == 7654


@pytest.mark.parametrize("missing_day", [False, True])
def test_custom_range_reuses_daily_values_not_weekly_totals_or_summed_stock(saved, monkeypatch, missing_day):
    cache = saved[1][f"baskets_{START}_{END}"]
    cache["dailyAggregates"] = {f"2026-10-0{day}": {"123": {"cartCount": day, "orderCount": 1}} for day in (7, 8, 9)}
    if missing_day:
        del cache["dailyAggregates"]["2026-10-08"]
    cache["aggregates"]["123"]["wbStockUnits"] = 42
    monkeypatch.setattr(filters, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw: [
        {"sourceKey": f"baskets_{START}_{END}", "dateFrom": str(START), "dateTo": str(END)}] if prefix == "baskets_" else [])
    p = filters.load_filter_metadata(1, filters.PnlFilterRequest(dateFrom=date(2026, 10, 7), dateTo=date(2026, 10, 9), nmIds=[123]))["products"]["123"]
    assert p["baskets"] == (None if missing_day else 24)
    assert p["orders"] == (None if missing_day else 3)
    assert p["stockUnits"] == (None if missing_day else 42)


def test_account_and_period_revalidation_on_each_read(saved, monkeypatch):
    with pytest.raises(HTTPException) as error:
        filters.load_filter_metadata(2, filters.PnlFilterRequest(dateFrom=START, dateTo=END, nmIds=[123]))
    assert error.value.status_code == 409
    result = filters.load_filter_metadata(1, filters.PnlFilterRequest(dateFrom=date(2026, 10, 7), dateTo=date(2026, 10, 9), nmIds=[123]))
    assert result["products"]["123"]["baskets"] is None
    assert result["dateFrom"] == "2026-10-07"


def test_response_only_contains_requested_ids_and_no_financial_fields(saved):
    payload = filters.PnlFilterRequest(dateFrom=START, dateTo=END, nmIds=[456, 456])
    result = filters.load_filter_metadata(1, payload)
    assert set(result["products"]) == {"456"}
    assert result["products"]["456"]["currentPriceKopecks"] is None
    assert not {"taxKopecks", "netProfitKopecks", "marginPct", "cogsKopecks"} & result["products"]["456"].keys()


def test_endpoint_checks_permission_before_read(monkeypatch):
    monkeypatch.setattr(filters, "actor_from_request", lambda request: SimpleNamespace(organization_id=17))
    checked = []
    monkeypatch.setattr(filters, "assert_permission_or_audit", lambda **kw: checked.append(kw["permission"]))
    monkeypatch.setattr(filters, "load_filter_metadata", lambda org, payload: {"org": org})
    assert filters.pnl_filter_metadata(None, filters.PnlFilterRequest(dateFrom=START, dateTo=END, nmIds=[])) == {"org": 17}
    assert checked == ["settings:read"]


@pytest.mark.parametrize("subject,expected", [("Футболки", "shirt"), ("Худи", "hoodie"), ("Лонгсливы", "longsleeve"),
    ("Шорты", "shorts"), ("Балаклавы", "balaclava"), (None, None), ("Неизвестно", None)])
def test_supported_categories(subject, expected):
    assert filters._garment(subject) == expected
