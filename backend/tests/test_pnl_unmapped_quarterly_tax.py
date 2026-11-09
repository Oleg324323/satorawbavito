"""P&L-only organization quarterly tax, without creating catalog identities."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, event, func, select

from app import wb_reports_sprint_d as reports
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceOfferRow, MarketplaceProductRow
from app.platform.economics.legacy_tax import get_legacy_finance_taxes, legacy_finance_tax_revision
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow
from app.routers import wb_reports_bff as router
from tests.test_legacy_finance_tax import tax_db, _cache, _fact, _policy  # noqa: F401
from tests.test_pnl_saved_repricer_costs import saved, report, ARTICLE  # noqa: F401
from tests.test_abc_pnl_reported_economics import ledger, trade, START, END


def quarterly(db, *, year=2026, rates=None, flags=None):
    row = db.get(WbRepricerAlgorithmSettingsRow, 1)
    payload = deepcopy(row.settings_payload) if row else {"taxPct": 6}
    payload.setdefault("wbQuarterlyTax", {})[str(year)] = {
        "basisPoints": rates if rates is not None else [None, None, None, 2200],
        "configured": flags if flags is not None else [False, False, False, True],
    }
    if row:
        row.settings_payload = payload
    else:
        db.add(WbRepricerAlgorithmSettingsRow(organization_id=1, settings_payload=payload))
    db.commit()


def clear_catalog(db):
    for model in (MarketplaceOfferRow, MarketplaceProductRow, CatalogSkuRow):
        db.execute(delete(model).where(model.organization_id == 1))
    db.commit()


@pytest.fixture
def empty(tax_db):
    clear_catalog(tax_db)
    quarterly(tax_db)
    return tax_db


def resolve(source, start="2026-10-05", end="2026-10-11", org=1, **options):
    return get_legacy_finance_taxes(org, source,
        Period(date.fromisoformat(start), date.fromisoformat(end)),
        allow_unmapped_quarterly_tax=True, **options)["123"]


def test_explicit_quarter_rate_does_not_require_catalog_but_other_consumers_stay_strict(empty):
    source = _cache(_fact(100_000, 3, 1), {"2026-10-05": _fact(120_000, 3),
        "2026-10-11": _fact(-20_000, 0, 1)})
    before = deepcopy(source)
    result = resolve(source, include_costs=True, current_costs={123: 1000})
    assert result["taxKopecks"] == 22000 and result["factTaxState"] == "configured"
    assert result["settlementCogsKopecks"] == 2000
    assert source == before
    strict = get_legacy_finance_taxes(1, source, Period(START, END))["123"]
    assert strict["taxKopecks"] is None and strict["factTaxReason"] == "tax_mapping_missing"
    assert empty.scalar(select(func.count()).select_from(CatalogSkuRow)) == 0


@pytest.mark.parametrize("rate,revenue,expected", [(0, 100000, 0), (2200, -10000, -2200),
    (2200, 0, 0), (750, 60, 4), (750, -60, -4)])
def test_zero_negative_revenue_and_existing_rounding(empty, rate, revenue, expected):
    quarterly(empty, rates=[None, None, None, rate])
    assert resolve(_cache(_fact(revenue)))["taxKopecks"] == expected


@pytest.mark.parametrize("rate", [None, True, -1, 10001, "2200", 2200.5])
def test_invalid_or_missing_rate_never_uses_planning_six_percent(empty, rate):
    quarterly(empty, rates=[None, None, None, rate])
    result = resolve(_cache(_fact(100000)))
    assert result["taxKopecks"] is None and result["factTaxReason"] == "tax_policy_unconfirmed"


@pytest.mark.parametrize("flags", [[False] * 4, [False, False, False, "true"], [True]])
def test_disabled_or_malformed_confirmation_is_not_a_confirmed_rate(empty, flags):
    quarterly(empty, flags=flags)
    assert resolve(_cache(_fact(100000)))["taxKopecks"] is None


def test_absent_quarter_schedule_does_not_fall_back_to_planning_tax(empty):
    empty.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload = {"taxPct": 6}
    empty.commit()
    assert resolve(_cache(_fact(100000)))["taxKopecks"] is None


def test_cross_quarter_groups_net_revenue_and_does_not_round_each_day(empty):
    quarterly(empty, rates=[None, None, 600, 2200], flags=[False, False, True, True])
    source = _cache(_fact(200000, 2), {
        "2026-09-30": _fact(100000), "2026-10-01": _fact(100000)})
    assert resolve(source, "2026-09-30", "2026-10-01")["taxKopecks"] == 28000
    quarterly(empty, rates=[None, None, None, 750])
    source = _cache(_fact(14, 2), {"2026-10-05": _fact(7), "2026-10-06": _fact(7)})
    assert resolve(source)["taxKopecks"] == 1


def test_cross_year_uses_each_year_and_missing_quarter_stays_unknown(empty):
    quarterly(empty, year=2027, rates=[1000, None, None, None], flags=[True, False, False, False])
    source = _cache(_fact(200000, 2), {
        "2026-12-31": _fact(100000), "2027-01-01": _fact(100000)})
    assert resolve(source, "2026-12-31", "2027-01-01")["taxKopecks"] == 32000
    quarterly(empty, year=2027, flags=[False] * 4)
    assert resolve(source, "2026-12-31", "2027-01-01")["taxKopecks"] is None


def test_delayed_operation_uses_sale_quarter_not_selected_posting_period(empty):
    quarterly(empty, rates=[None, None, 600, 2200], flags=[False, False, True, True])
    source = _cache(_fact(100000), {"2026-09-30": _fact(100000)})
    source.update(dateFrom="2026-10-05", dateTo="2026-10-11")
    assert resolve(source)["taxKopecks"] == 6000
    quarterly(empty)  # Q3 now unconfigured, even though Q4 is configured.
    assert resolve(source)["taxKopecks"] is None


def test_without_daily_split_only_uniform_fully_covered_rates_are_usable(empty):
    quarterly(empty, rates=[None, None, 600, 2200], flags=[False, False, True, True])
    result = resolve(_cache(_fact(200000, 2)), "2026-09-30", "2026-10-01")
    assert result["taxKopecks"] is None and result["factTaxReason"] == "tax_daily_basis_missing"
    assert resolve(_cache(_fact(100000)))["taxKopecks"] == 22000


@pytest.mark.parametrize("daily", [{}, {"2026-10-05": _fact(90000)},
    {"2026-10-05": _fact(100000, 2)}, {"2026-10-05": _fact(None)},
    {"2026-10-05": _fact(100000, True)}])
def test_incomplete_or_malformed_daily_basis_cannot_produce_tax(empty, daily):
    result = resolve(_cache(_fact(100000), daily))
    assert result["taxKopecks"] is None and result["factTaxReason"] == "tax_daily_coverage_mismatch"


@pytest.mark.parametrize("metadata,reason", [
    ({"marketplaceAccountId": 999}, "tax_account_mismatch"),
    ({"organizationId": 2}, "tax_organization_mismatch"),
    ({"aggregates": {"123": _fact(100000) | {"nmId": 124}}}, "tax_product_identity_unconfirmed"),
    ({"aggregates": {"not-a-wb-id": _fact(100000)}}, "tax_mapping_missing"),
])
def test_explicitly_foreign_context_or_invalid_product_is_rejected(empty, metadata, reason):
    source = _cache(_fact(100000)) | metadata
    result = get_legacy_finance_taxes(1, source, Period(START, END), allow_unmapped_quarterly_tax=True)
    row = next(iter(result.values()))
    assert row["taxKopecks"] is None and row["factTaxReason"] == reason


def test_other_organization_and_ambiguous_account_stay_unknown(empty):
    assert resolve(_cache(_fact(100000)), org=2)["factTaxReason"] == "tax_account_missing"
    empty.add(MarketplaceAccountRow(marketplace_account_id=15, organization_id=1,
        marketplace="wb", external_account_id="other", status="connected"))
    empty.commit()
    assert resolve(_cache(_fact(100000)))["factTaxReason"] == "tax_account_ambiguous"


def test_same_wb_product_in_another_organization_uses_only_its_own_rate(empty):
    empty.add_all([
        MarketplaceAccountRow(marketplace_account_id=22, organization_id=2,
            marketplace="wb", external_account_id="another-org", status="connected"),
        WbRepricerAlgorithmSettingsRow(organization_id=2, settings_payload={
            "wbQuarterlyTax": {"2026": {"basisPoints": [None, None, None, 1000],
                "configured": [False, False, False, True]}}}),
    ])
    empty.commit()
    source = _cache(_fact(100000)) | {"organizationId": 2, "marketplaceAccountId": 22}
    assert resolve(source, org=2)["taxKopecks"] == 10000
    assert resolve(_cache(_fact(100000)))["taxKopecks"] == 22000


def test_mapped_tax_priority_is_unchanged_and_ambiguous_mapping_is_not_repaired(tax_db):
    _policy(tax_db, "2026-10-01T00:00:00", rate=600)
    quarterly(tax_db)
    source = _cache(_fact(100000))
    strict = get_legacy_finance_taxes(1, source, Period(START, END))["123"]
    assert strict == resolve(source)
    assert strict["taxKopecks"] == 22000  # existing explicit-quarter priority
    tax_db.add(CatalogSkuRow(catalog_sku_id=15, organization_id=1, code="other"))
    tax_db.add(MarketplaceOfferRow(organization_id=1, marketplace_account_id=12,
        marketplace_product_id=13, external_offer_key="other", catalog_sku_id=15))
    tax_db.commit()
    assert resolve(source)["factTaxReason"] == "tax_mapping_ambiguous"


def test_bulk_tax_query_count_does_not_grow_per_product(empty):
    calls = []
    engine = empty.get_bind()
    def count(*args):
        calls.append(1)
    event.listen(engine, "before_cursor_execute", count)
    try:
        resolve(_cache(_fact(100000)))
        single = len(calls)
        calls.clear()
        many = {"aggregates": {str(nm): _fact(100000) for nm in range(200, 400)}}
        source = _cache(_fact(100000)) | many
        result = get_legacy_finance_taxes(1, source, Period(START, END), allow_unmapped_quarterly_tax=True)
        assert all(row["taxKopecks"] == 22000 for row in result.values())
        assert len(calls) <= single + 1
    finally:
        event.remove(engine, "before_cursor_execute", count)


def report_finance():
    return ledger([trade(nmId=123, vendorCode=ARTICLE, quantity=3, retailAmount="300",
        retailPriceWithDisc="100", forPay="270", ppvzSalesCommission="20"),
        trade(2, nmId=123, vendorCode=ARTICLE, quantity=1, retailAmount="100",
            retailPriceWithDisc="100", forPay="90", ppvzSalesCommission="8",
            docTypeName="Возврат", sellerOperName="Возврат")])


def build():
    return reports._build_cached_pnl_report(organization_id=1, date_from=START, date_to=END,
        group_by="sku", requested_state="preliminary", finance_allowed=True)


def test_actual_pnl_and_bff_calculate_profit_margin_but_keep_missing_cogs_row_local(saved, report):
    clear_catalog(saved)
    quarterly(saved)
    report["finance"] = report_finance()
    complete = build()
    row = next(row for row in complete.rows if row.nmId == 123)
    assert row.taxKopecks == 4400 and row.cogsKopecks == 2468
    assert row.netProfitKopecks == row.revenueKopecks - sum([
        row.cogsKopecks, row.commissionKopecks, row.logisticsKopecks,
        row.storageKopecks, row.adSpendKopecks, row.taxKopecks, row.overheadKopecks])
    assert row.marginPct == round(row.netProfitKopecks / row.revenueKopecks * 100, 2)
    report["finance"] = ledger([trade(nmId=123, vendorCode=ARTICLE, retailAmount="300"),
        trade(2, nmId=124, vendorCode="Unconfigured", retailAmount="100")])
    partial = build()
    by_nm = {row.nmId: row for row in partial.rows}
    assert by_nm[123].netProfitKopecks is not None
    assert by_nm[124].taxKopecks == 2200
    assert by_nm[124].cogsKopecks is None and by_nm[124].netProfitKopecks is None
    assert partial.totals.netProfitKopecks is None
    rendered = router._map_pnl_to_report_response(partial, {}, {"status": "disabled"},
        finance_allowed=True, financial_only=True)
    assert next(row for row in rendered["rows"] if row["nmId"] == 123)["netProfitKopecks"] is not None


@pytest.mark.parametrize("amount,returned", [("1", False), ("0", False), ("100", True)])
def test_actual_pnl_preserves_losses_and_undefined_margin_for_nonpositive_revenue(saved, report, amount, returned):
    clear_catalog(saved)
    quarterly(saved)
    report["finance"] = ledger([trade(nmId=123, vendorCode=ARTICLE, retailAmount=amount,
        docTypeName="Возврат" if returned else "Продажа",
        sellerOperName="Возврат" if returned else "Продажа")])
    row = next(row for row in build().rows if row.nmId == 123)
    assert row.netProfitKopecks is not None
    if row.revenueKopecks > 0:
        assert row.netProfitKopecks < 0 and row.marginPct < 0
    else:
        assert row.marginPct is None


def test_tax_change_invalidates_saved_null_report_then_reuses_compatible_result(saved, report, monkeypatch):
    clear_catalog(saved)
    report["finance"] = report_finance()
    caches, builds = {}, []
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **kw: deepcopy(caches.get((org, key))))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: caches.__setitem__((org, key),
        deepcopy(value) | {"fetchedAt": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setattr(reports, "_pnl_source_revision", lambda *a: "same-saved-finance-and-ads")
    original = reports._build_cached_pnl_report
    def calculate(**kwargs):
        builds.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(reports, "_build_cached_pnl_report", calculate)
    args = dict(organization_id=1, date_from=START, date_to=END, group_by="sku",
        requested_state="preliminary", finance_allowed=True)
    missing = reports.build_pnl_report.__wrapped__(**args)
    assert missing.rows[0].netProfitKopecks is None
    revision = legacy_finance_tax_revision(1)
    quarterly(saved)
    assert legacy_finance_tax_revision(1) != revision
    confirmed = reports.build_pnl_report.__wrapped__(**args)
    again = reports.build_pnl_report.__wrapped__(**args)
    assert confirmed == again and len(builds) == 2
    assert confirmed.rows[0].taxKopecks == 4400 and confirmed.rows[0].netProfitKopecks is not None
    quarterly(saved, rates=[None, None, None, 0])
    zero = reports.build_pnl_report.__wrapped__(**args)
    assert len(builds) == 3 and zero.rows[0].taxKopecks == 0
    assert zero.rows[0].netProfitKopecks == confirmed.rows[0].netProfitKopecks + 4400


def test_old_pnl_payload_with_null_tax_is_not_accepted():
    entry = {"fetchedAt": datetime.now(timezone.utc).isoformat(),
        "report": {"cacheVersion": "v25", "rows": [{"taxKopecks": None, "netProfitKopecks": None}]}}
    assert not router._report_payload_cache_is_usable("pnl", entry)
