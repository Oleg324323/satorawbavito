"""Persisted Repricer costs through actual P&L builders; disposable DB only."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, event, func, select

from app import wb_pnl_costs as costs, wb_pnl_advertising as advertising
from app import wb_pnl_expenses as fees, wb_reports_sprint_d as reports
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceOfferRow, MarketplaceProductRow
from app.platform.economics.costs import CostsService
from app.platform.economics.legacy_tax import get_legacy_finance_taxes
from app.platform.economics.orm import CatalogCostVersionRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow, WbRepricerRuntimeStateRow
from app.routers import wb_reports_bff as router
from tests.test_legacy_finance_tax import tax_db, _cache, _fact, _policy  # noqa: F401
from tests.test_abc_pnl_reported_economics import ads, ledger, trade, START, END

ARTICLE = "Л_test"
CONTEXT = dict(marketplaceAccountId=12, bindingVersion=1, connection="synthetic",
               connectedAt="2026-01-01T00:00:00+00:00")


@pytest.fixture
def saved(tax_db, monkeypatch):
    monkeypatch.setattr(costs, "get_session_factory", lambda: lambda: tax_db)
    monkeypatch.setattr(advertising, "_source_context", lambda org: CONTEXT if org == 1 else None)
    tax_db.add_all([
        WbRepricerAlgorithmSettingsRow(organization_id=1,
            settings_payload={"cogsByGarmentRub": {"longsleeve": "12.34"}}),
        WbRepricerRuntimeStateRow(organization_id=1, state_payload={}),
        WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=100,
            fetched_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
            goods_payload=[{"nmID": 123, "vendorCode": ARTICLE}]),
    ])
    tax_db.commit()
    return tax_db


def configure(db, *, garments=None, overrides=None):
    db.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload = {**db.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload, "cogsByGarmentRub": garments or {}}
    db.get(WbRepricerRuntimeStateRow, 1).state_payload = {"skuSettingsOverrides": overrides or {}}
    db.commit()


def cache(*, sales=3, returns=1, daily=None):
    return _cache(_fact(10000, sales, returns) | {"vendorCode": ARTICLE}, daily)


def resolve(source):
    return get_legacy_finance_taxes(1, source, Period(START, END), include_costs=True,
        current_costs=costs.saved_current_costs(1, source))["123"]


def test_empty_catalog_uses_saved_cost_without_creating_cost_history(saved):
    for model in (MarketplaceOfferRow, MarketplaceProductRow, CatalogSkuRow):
        saved.execute(delete(model).where(model.organization_id == 1))
    saved.commit()
    result = resolve(cache())
    assert result["settlementCogsKopecks"] == 1234 * (3 - 1)
    assert result["settlementCogsState"] == "current"
    assert result["settlementCogsBasis"] == "saved_repricer_current_cost"
    assert result["taxKopecks"] is None
    assert saved.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 0
    assert saved.scalar(select(func.count()).select_from(CatalogSkuRow)) == 0


@pytest.mark.parametrize("unit", [0, 4321])
def test_product_override_including_zero_wins_over_saved_category(saved, unit):
    configure(saved, garments={"longsleeve": 99}, overrides={ARTICLE: {"cogsKopecks": unit}})
    assert resolve(cache())["settlementCogsKopecks"] == unit * 2


def test_finance_case_difference_uses_catalog_spelling_for_product_override(saved):
    configure(saved, garments={"longsleeve": 99}, overrides={ARTICLE: {"cogsKopecks": 4321}})
    source = cache()
    source["aggregates"]["123"]["vendorCode"] = ARTICLE.lower()
    assert resolve(source)["settlementCogsKopecks"] == 8642


@pytest.mark.parametrize("garments,overrides", [({}, {}), ({"tshirt": 99}, {}),
    ({"longsleeve": 99}, {ARTICLE: {"cogsKopecks": None}}),
    ({"longsleeve": -1}, {}), ({"longsleeve": "NaN"}, {}),
    ({"longsleeve": 1.001}, {}), ({}, {ARTICLE: {"cogsKopecks": True}})])
def test_absent_invalid_or_other_category_never_uses_demo_defaults(saved, garments, overrides):
    configure(saved, garments=garments, overrides=overrides)
    assert resolve(cache())["settlementCogsKopecks"] is None


@pytest.mark.parametrize("sales,returns,expected", [(0, 2, -2468), (2, 2, 0), (0, 0, 0)])
def test_returns_zero_net_and_service_only_keep_settlement_semantics(saved, sales, returns, expected):
    assert resolve(cache(sales=sales, returns=returns))["settlementCogsKopecks"] == expected


def test_daily_historical_costs_win_and_only_missing_day_uses_current(saved):
    CostsService(saved, 1).set_cost(catalog_sku_id=11, amount_kopecks=2000, value_state="configured",
        effective_from=Period(date(2026, 10, 13), date(2026, 10, 13)).start_at,
        source="manual", source_reference="dated", evidence_status="dated")
    CostsService(saved, 1).set_cost(catalog_sku_id=11, amount_kopecks=3000, value_state="configured",
        effective_from=Period(date(2026, 10, 20), date(2026, 10, 20)).start_at,
        source="manual", source_reference="current", evidence_status="dated")
    source = cache(daily={"2026-10-12": _fact(4000, 1),
        "2026-10-13": _fact(4000, 1), "2026-10-18": _fact(2000, 1, 1)})
    result = resolve(source)
    assert result["settlementCogsKopecks"] == 3000 + 2000
    assert result["settlementCogsBasis"] == "mixed_historical_and_current"
    later = get_legacy_finance_taxes(1, _cache(_fact(8000, 3, 1), {
        "2026-10-13": _fact(4000, 2), "2026-10-18": _fact(4000, 1, 1)}),
        Period(date(2026, 10, 13), END), include_costs=True, current_costs={123: 9999})["123"]
    assert later["settlementCogsKopecks"] == 4000
    assert later["settlementCogsState"] == "configured"


def test_changing_history_without_daily_units_is_not_replaced_wholesale(saved):
    CostsService(saved, 1).set_cost(catalog_sku_id=11, amount_kopecks=2000, value_state="configured",
        effective_from=Period(date(2026, 10, 13), END).start_at,
        source="manual", source_reference="later", evidence_status="dated")
    assert resolve(cache())["settlementCogsKopecks"] is None


def test_daily_quantity_mismatch_remains_unknown(saved):
    result = resolve(cache(daily={str(START): _fact(10000, 1)}))
    assert result["settlementCogsKopecks"] is None
    assert result["settlementCogsReason"] == "cost_daily_coverage_mismatch"


def test_account_isolation_and_ambiguous_or_missing_identity(saved):
    assert costs.saved_current_costs(2, cache()) == {}
    assert costs.saved_current_costs(1, cache() | {"marketplaceAccountId": 999}) == {}
    assert costs.saved_current_costs(1, _cache(_fact(10000)) | {"aggregates": {"999": _fact(10000)}}) == {}
    assert costs.saved_current_costs(1, _cache(_fact(10000) | {"vendorCode": "Л_other"})) == {}
    duplicate = cache()
    duplicate["aggregates"]["124"] = _fact(5000) | {"vendorCode": ARTICLE}
    assert costs.saved_current_costs(1, duplicate) == {}
    saved.add(MarketplaceAccountRow(marketplace_account_id=15, organization_id=1,
        marketplace="wb", external_account_id="other", status="connected"))
    saved.commit()
    assert costs.saved_current_costs(1, cache()) == {}


def test_old_binding_catalog_and_ambiguous_canonical_mapping_are_not_fallback_evidence(saved):
    page = saved.scalar(select(WbRepricerGoodsCacheRow))
    page.fetched_at = datetime(2025, 12, 1, tzinfo=timezone.utc)
    saved.commit()
    assert costs.saved_current_costs(1, _cache(_fact(10000))) == {}
    saved.add(CatalogSkuRow(catalog_sku_id=15, organization_id=1, code="other"))
    saved.add(MarketplaceOfferRow(organization_id=1, marketplace_account_id=12,
        marketplace_product_id=13, external_offer_key="other", catalog_sku_id=15))
    saved.commit()
    assert costs.saved_current_costs(1, cache()) == {}


def test_cost_lookup_query_count_does_not_grow_per_product(saved):
    counts = []
    engine = saved.get_bind()
    def count(*args):
        counts.append(1)
    event.listen(engine, "before_cursor_execute", count)
    try:
        costs.saved_current_costs(1, cache())
        single = len(counts)
        counts.clear()
        many = {str(nm): _fact(10000) | {"vendorCode": f"Л_{nm}"} for nm in range(200, 400)}
        assert len(costs.saved_current_costs(1, {"aggregates": many})) == 200
        assert len(counts) <= single + 1
    finally:
        event.remove(engine, "before_cursor_execute", count)


@pytest.fixture
def report(saved, monkeypatch):
    inputs = {"finance": ledger([trade(nmId=123, vendorCode=ARTICLE, quantity=3),
        trade(2, nmId=123, vendorCode=ARTICLE, quantity=1, docTypeName="Возврат", sellerOperName="Возврат")]),
        "ads": ads()}
    monkeypatch.setattr(fees, "load_pnl_finance", lambda *a: inputs["finance"])
    monkeypatch.setattr(advertising, "_candidate_caches", lambda *a: [inputs["ads"]])
    monkeypatch.setattr(reports, "_period_cache", lambda *a, **kw: {})
    monkeypatch.setattr(reports, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(reports, "_goods_index", lambda *a, **kw: {})
    monkeypatch.setattr(reports, "load_runtime_state", lambda *a: {})
    monkeypatch.setattr(reports, "load_algorithm_settings", lambda *a: {})
    return inputs


def test_actual_pnl_builder_and_bff_cells_keep_cogs_while_tax_and_ads_are_missing(report):
    report["ads"] = {}
    built = reports._build_cached_pnl_report(organization_id=1, date_from=START, date_to=END,
        group_by="sku", requested_state="preliminary", finance_allowed=True)
    row = next(row for row in built.rows if row.nmId == 123)
    assert row.cogsKopecks == 2468 and row.costReasons == []
    assert row.netProfitKopecks is None and row.taxKopecks is None and row.adSpendKopecks is None
    assert "WB_PNL_DATED_COST_MISSING" not in built.blockerIds
    assert any(item.sourceId == "saved-repricer-current-cost" for item in built.sourceEvidence)
    rendered = router._map_pnl_to_report_response(built, {"from": str(START), "to": str(END)},
        {"status": "disabled"}, finance_allowed=True, financial_only=True)
    assert next(row for row in rendered["rows"] if row["nmId"] == 123)["cogsKopecks"] == 2468


def test_saved_report_reused_then_recalculated_from_changed_persisted_unit_cost(saved, report, monkeypatch):
    # A historical organization policy is no longer the report's tax input.
    settings_row = saved.get(WbRepricerAlgorithmSettingsRow, 1)
    settings_row.settings_payload = {**settings_row.settings_payload, 'taxPct': 0}
    saved.commit()
    _policy(saved, "2026-10-01T00:00:00", rate=0)
    caches, builds = {}, []
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **kw: deepcopy(caches.get((org, key))))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: caches.__setitem__((org, key),
        deepcopy(value) | {"fetchedAt": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setattr(reports, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [])
    monkeypatch.setattr("app.repricer_page_finance.page_finance_revision", lambda *a: "synthetic-finance")
    original = reports._build_cached_pnl_report
    def build(**kwargs):
        builds.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(reports, "_build_cached_pnl_report", build)
    args = dict(organization_id=1, date_from=START, date_to=END, group_by="sku",
        requested_state="preliminary", finance_allowed=True)
    first = reports.build_pnl_report.__wrapped__(**args)
    again = reports.build_pnl_report.__wrapped__(**args)
    assert first == again and len(builds) == 1
    assert first.rows[0].netProfitKopecks is not None
    old_revision = costs.current_cost_revision(1)
    configure(saved, overrides={ARTICLE: {"cogsKopecks": 2000}})
    assert costs.current_cost_revision(1) != old_revision
    updated = reports.build_pnl_report.__wrapped__(**args)
    assert len(builds) == 2
    assert updated.rows[0].cogsKopecks == 4000
    assert updated.rows[0].netProfitKopecks == first.rows[0].netProfitKopecks - (4000 - 2468)
    assert costs.saved_current_costs(2, report["finance"]) == {}


def test_pnl_payload_preview_rejects_cost_revision_change_even_when_allowing_stale(saved, monkeypatch):
    monkeypatch.setattr(router, "_report_payload_cache_is_fresh", lambda *a: True)
    entry = {"report": {"cacheVersion": router.PNL_REPORT_PAYLOAD_VERSION},
        "pnlAdvertisingContext": CONTEXT, "pnlCurrentCostRevision": costs.current_cost_revision(1)}
    monkeypatch.setattr(router, "legacy_finance_tax_revision", lambda *a: "synthetic-tax")
    entry["taxRevision"] = "synthetic-tax"
    assert router._report_payload_cache_is_usable("pnl", entry, organization_id=1, allow_stale=True)
    configure(saved, overrides={ARTICLE: {"cogsKopecks": 2000}})
    assert not router._report_payload_cache_is_usable("pnl", entry, organization_id=1, allow_stale=True)
