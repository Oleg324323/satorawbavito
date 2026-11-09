from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.cabinet.orm import LkOrganizationRow
from app.infra.models import Base
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceOfferRow, MarketplaceProductRow
from app.platform.economics.policies import EconomicsService
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import Period


@pytest.fixture
def tax_db(monkeypatch):
    from app.platform.economics import legacy_tax

    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            LkOrganizationRow(organization_id=1, slug="one", name="One"),
            LkOrganizationRow(organization_id=2, slug="two", name="Two"),
            CatalogSkuRow(catalog_sku_id=11, organization_id=1, code="sku"),
            MarketplaceAccountRow(marketplace_account_id=12, organization_id=1, marketplace="wb", external_account_id="synthetic", status="connected"),
            MarketplaceProductRow(marketplace_product_id=13, organization_id=1, marketplace_account_id=12, external_product_id="123"),
            MarketplaceOfferRow(marketplace_offer_id=14, organization_id=1, marketplace_account_id=12, marketplace_product_id=13, external_offer_key="offer", catalog_sku_id=11),
        ])
        db.commit()
        monkeypatch.setattr(legacy_tax, "get_session_factory", lambda: lambda: db)
        # Both legacy financial adapters must use the same disposable catalog;
        # never let a report regression consult the developer's configured DB.
        monkeypatch.setattr("app.platform.economics.legacy_catalog.get_session_factory", lambda: lambda: db)
        yield db


def _policy(db, day, rate=750, *, confirmed=True, reference="tax"):
    return EconomicsService(db, 1).set_organization_policy(
        tax_basis_points=rate, other_expense_price_basis_points=500, other_expense_per_sale_kopecks=1000,
        value_state="assumed", evidence_status="undated", source="fixture", source_reference=reference,
        effective_from=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
        tax_value_state="configured" if confirmed else None, tax_evidence_status="dated" if confirmed else None,
    )


def _fact(revenue, sales=1, returns=0):
    return {"sellerRevenueKopecks": revenue, "buyerRevenueKopecks": revenue, "salesUnits": sales, "returnsUnits": returns}


def _cache(total, daily=None):
    from app.repricer_bff import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION
    result = {"revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION, "aggregates": {"123": total}}
    if daily is not None:
        result["dailyAggregates"] = {day: {"123": values} for day, values in daily.items()}
    return result


def _resolve(cache, start, end=None, org=1):
    from app.platform.economics.legacy_tax import get_legacy_finance_taxes
    return get_legacy_finance_taxes(org, cache, Period(date.fromisoformat(start), date.fromisoformat(end or start)))["123"]


@pytest.mark.parametrize("amount", [0, 12345])
def test_settlement_cost_uses_dated_cost_and_net_sold_units(tax_db, amount):
    from app.platform.economics.costs import CostsService
    from app.platform.economics.legacy_tax import get_legacy_finance_taxes
    _policy(tax_db, "2026-08-31T21:00:00")
    CostsService(tax_db, 1).set_cost(catalog_sku_id=11, amount_kopecks=amount,
        value_state="configured", effective_from=datetime(2026, 8, 31, 21, tzinfo=timezone.utc),
        source="fixture", source_reference="dated-cost", evidence_status="dated")
    result = get_legacy_finance_taxes(1, _cache(_fact(100000, 3, 1)),
        Period(date(2026, 9, 1), date(2026, 9, 1)), include_costs=True)["123"]
    assert result["settlementCogsKopecks"] == amount * 2
    assert result["taxKopecks"] == 7500


def test_missing_dated_cost_does_not_become_zero(tax_db):
    from app.platform.economics.legacy_tax import get_legacy_finance_taxes
    _policy(tax_db, "2026-08-31T21:00:00")
    result = get_legacy_finance_taxes(1, _cache(_fact(100000)),
        Period(date(2026, 9, 1), date(2026, 9, 1)), include_costs=True)["123"]
    assert result["settlementCogsKopecks"] is None


def test_confirmed_cost_is_visible_without_tax_and_zero_service_days_need_no_cost(tax_db):
    from app.platform.economics.costs import CostsService
    from app.platform.economics.legacy_tax import get_legacy_finance_taxes
    CostsService(tax_db, 1).set_cost(catalog_sku_id=11, amount_kopecks=80000,
        value_state="configured", effective_from=datetime(2026, 9, 1, 21, tzinfo=timezone.utc),
        source="fixture", source_reference="confirmed", evidence_status="dated")
    result = get_legacy_finance_taxes(1, _cache(_fact(100000, 3, 1), {
        "2026-09-01": _fact(0, 0, 0), "2026-09-02": _fact(100000, 3, 1)}),
        Period(date(2026, 9, 1), date(2026, 9, 2)), include_costs=True)["123"]
    assert result["settlementCogsKopecks"] == 160000
    assert result["settlementCogsState"] == "configured"
    assert result["taxKopecks"] is None and result["factTaxReason"] == "tax_policy_unconfirmed"


def test_service_only_cost_is_confirmed_zero_without_account_but_tax_stays_missing(tax_db):
    from app.platform.economics.legacy_tax import get_legacy_finance_taxes
    result = get_legacy_finance_taxes(2, _cache(_fact(0, 0, 0)),
        Period(date(2026, 9, 1), date(2026, 9, 1)), include_costs=True)["123"]
    assert result["settlementCogsKopecks"] == 0
    assert result["factTaxReason"] == "tax_account_missing"


def test_cost_before_confirmation_is_not_backdated_and_diagnostics_identify_operation_dates(tax_db):
    from app.platform.economics.costs import CostsService
    from app.platform.economics.legacy_tax import get_legacy_finance_taxes
    CostsService(tax_db, 1).set_cost(catalog_sku_id=11, amount_kopecks=80000,
        value_state="configured", effective_from=datetime(2026, 10, 20, tzinfo=timezone.utc),
        source="fixture", source_reference="later-cost", evidence_status="dated")
    result = get_legacy_finance_taxes(1, _cache(_fact(100000), {"2026-10-12": _fact(100000)}),
        Period(date(2026, 10, 12), date(2026, 10, 18)), include_costs=True)["123"]
    assert result["settlementCogsKopecks"] is None
    assert result["settlementCostMissingDates"] == ["2026-10-12"]


def test_september_confirmation_does_not_reprice_august_tax(tax_db):
    _policy(tax_db, "2026-08-01T00:00:00", 600, confirmed=False, reference="old-assumption")
    _policy(tax_db, "2026-08-31T21:00:00")
    august = _fact(100_000)
    september = _fact(85_000, 1, 1)
    cache = _cache(_fact(185_000, 2, 1), {"2026-08-31": august, "2026-09-01": september})
    assert _resolve(cache, "2026-08-31", "2026-09-01") == {
        "taxKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_policy_unconfirmed",
    }
    assert _resolve(_cache(september, {"2026-09-01": september}), "2026-09-01")["taxKopecks"] == 6_375


def test_two_confirmed_rates_use_daily_net_revenue_without_rewriting_expenses(tax_db):
    _policy(tax_db, "2026-08-01T00:00:00", 600, reference="synthetic-confirmed-august")
    _policy(tax_db, "2026-08-31T21:00:00")
    cache = _cache(_fact(200_000, 2), {"2026-08-31": _fact(100_000), "2026-09-01": _fact(100_000)})
    original = deepcopy(cache)
    assert _resolve(cache, "2026-08-31", "2026-09-01")["taxKopecks"] == 13_500
    assert cache == original
    policy = EconomicsService(tax_db, 1).get_policies_for_points([(11, datetime(2026, 9, 1, tzinfo=timezone.utc))])
    assert next(iter(policy.values())).other_expense_price_basis_points == 500


def test_posted_realization_uses_original_sale_quarter_not_posting_quarter(tax_db):
    from app.repricer_page_finance import project_realization_operations
    from tests.test_repricer_page_finance import operation
    _policy(tax_db, "2026-08-31T21:00:00", 600, reference="q3")
    _policy(tax_db, "2026-09-30T21:00:00", 1200, reference="q4")
    result = project_realization_operations([operation(nmId=123, saleDt="2026-09-30", rrDate="2026-10-05")],
        date(2026, 10, 5), date(2026, 10, 11))
    cache = _cache(result["aggregates"]["123"])
    cache.update(dailyAggregates=result["dailyAggregates"], dateFrom="2026-10-05", dateTo="2026-10-11")
    assert _resolve(cache, "2026-10-05", "2026-10-11")["taxKopecks"] == 300


def test_uniform_confirmed_policy_can_tax_period_total_without_daily_split(tax_db):
    _policy(tax_db, "2026-08-31T21:00:00")
    assert _resolve(_cache(_fact(100_000)), "2026-09-01", "2026-09-03") == {
        "taxKopecks": 7_500, "factTaxState": "configured", "factTaxReason": None,
    }


@pytest.mark.parametrize("case,reason", [
    ("changing_rate", "tax_daily_basis_missing"),
    ("daily_mismatch", "tax_daily_coverage_mismatch"),
    ("missing_mapping", "tax_mapping_missing"),
    ("wrong_basis", "tax_revenue_basis_unconfirmed"),
    ("other_org", "tax_account_missing"),
])
def test_incomplete_tax_inputs_are_unknown_not_zero_or_current_default(tax_db, case, reason):
    _policy(tax_db, "2026-08-31T21:00:00")
    cache = _cache(_fact(100_000))
    if case == "changing_rate":
        _policy(tax_db, "2026-09-01T21:00:00", 900, reference="future-explicit-rate")
    elif case == "daily_mismatch":
        cache["dailyAggregates"] = {"2026-09-01": {"123": _fact(90_000)}}
    elif case == "missing_mapping":
        tax_db.get(MarketplaceOfferRow, 14).catalog_sku_id = None
        tax_db.commit()
    elif case == "wrong_basis":
        cache["revenueBasis"] = "forPay"
    result = _resolve(cache, "2026-09-01", "2026-09-02", org=2 if case == "other_org" else 1)
    assert result == {"taxKopecks": None, "factTaxState": "missing", "factTaxReason": reason}


@pytest.mark.parametrize("revenue,rate,expected", [(0, 750, 0), (100, 0, 0), (-60, 750, -4), (60, 750, 4)])
def test_tax_preserves_zero_returns_and_canonical_half_even_rounding(tax_db, revenue, rate, expected):
    _policy(tax_db, "2026-08-31T21:00:00", rate)
    assert _resolve(_cache(_fact(revenue)), "2026-09-01")["taxKopecks"] == expected


def test_same_rate_daily_rounding_matches_canonical_period_tax(tax_db):
    _policy(tax_db, "2026-08-31T21:00:00")
    cache = _cache(_fact(14, 2), {"2026-09-01": _fact(7), "2026-09-02": _fact(7)})
    assert _resolve(cache, "2026-09-01", "2026-09-02")["taxKopecks"] == 1


@pytest.mark.parametrize("value", ["1", 1.5, True, float("inf"), None])
@pytest.mark.parametrize("scope", ["daily", "total"])
def test_malformed_normalized_units_make_tax_unknown(tax_db, value, scope):
    _policy(tax_db, "2026-08-31T21:00:00")
    cache = _cache(_fact(100), {"2026-09-01": _fact(100)})
    row = cache["dailyAggregates"]["2026-09-01"]["123"] if scope == "daily" else cache["aggregates"]["123"]
    row["salesUnits"] = value
    assert _resolve(cache, "2026-09-01") == {
        "taxKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_daily_coverage_mismatch",
    }


def test_empty_daily_rows_do_not_claim_confirmed_policy_for_zero(tax_db):
    assert _resolve(_cache(_fact(0, 0, 0), {}), "2026-09-01") == {
        "taxKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_daily_coverage_mismatch",
    }


def test_tax_revision_changes_for_new_policy_and_mapping(tax_db):
    from app.platform.economics.legacy_tax import legacy_finance_tax_revision
    initial = legacy_finance_tax_revision(1)
    _policy(tax_db, "2026-08-31T21:00:00")
    after_policy = legacy_finance_tax_revision(1)
    assert after_policy != initial
    offer = tax_db.get(MarketplaceOfferRow, 14)
    offer.catalog_sku_id = None
    offer.updated_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    tax_db.commit()
    assert legacy_finance_tax_revision(1) != after_policy


def test_missing_tax_context_never_substitutes_current_settings_into_facts(monkeypatch):
    from app import repricer_bff as bff
    from app.routers import wb_repricer_bff as router
    monkeypatch.setitem(bff.ALGORITHM_SETTINGS_STATE, "taxPct", 7.5)
    row = bff._build_sku_row(
        "synthetic", nm_id=123, name="Test", subject="Test", brand=None, chrt_ids=[],
        current_price_kopecks=150_000, discounted_price_kopecks=150_000, buyer_price_kopecks=135_000,
        promotions=[], use_demo_data=False, list_view=True, finance_aggregate=_fact(100_000),
    )
    assert row["settings"]["taxPct"] == 7.5  # Current-price planning stays unchanged.
    assert row["analytics"]["factTaxState"] == "missing"
    assert row["analytics"]["taxKopecks"] is None
    assert row["analytics"]["netProfitKopecks"] is row["analytics"]["factNetProfitKopecks"] is None
    row["analytics"]["plannedPeriodMarginKopecks"] = 99_999
    summary = router._repricer_list_summary([row])
    assert summary["taxKopecks"] is summary["marginKopecks"] is summary["avgMarginPct"] is None
    assert summary["factTaxState"] == "missing"


def test_source_diagnostics_never_embed_current_tax_as_period_fact(monkeypatch):
    from app import repricer_bff as bff
    monkeypatch.setitem(bff.ALGORITHM_SETTINGS_STATE, "taxPct", 7.5)
    result = bff.build_finance_diagnostics_from_aggregates({"123": _fact(100_000)})
    assert result["tax"]["taxPct"] is None
    assert result["tax"]["factTaxState"] == "missing"
    assert result["totals"]["taxKopecks"] is result["totals"]["expensesIfTaxIncludedKopecks"] is None
    assert result["skuSummaries"][0]["taxKopecks"] is None


@pytest.mark.parametrize("missing", [False, True])
def test_diagnostic_response_reprojects_cached_tax_before_limiting(monkeypatch, missing):
    from app.routers import wb_repricer_bff as router
    cache = {"aggregates": {"123": _fact(100_000), "124": _fact(0)}, "diagnostics": {
        "tax": {"taxPct": 9, "taxKopecks": 999_999},
        "totals": {"taxKopecks": 999_999, "expensesWithoutTaxKopecks": 1000},
        "skuSummaries": [
            {"nmId": "123", "taxPct": 9, "taxKopecks": 999_999, "expensesWithoutTaxKopecks": 1000},
            {"nmId": "124", "taxPct": 9, "taxKopecks": 999_999, "expensesWithoutTaxKopecks": 0},
        ],
    }}
    original = deepcopy(cache)
    taxes = {
        "123": {"taxKopecks": None if missing else 7500, "factTaxState": "missing" if missing else "configured",
                "factTaxReason": "tax_policy_unconfirmed" if missing else None},
        "124": {"taxKopecks": 0, "factTaxState": "configured", "factTaxReason": None},
    }
    calls = []
    monkeypatch.setattr(router, "_repricer_finance_taxes", lambda *args, **kwargs: calls.append(args) or taxes)
    result = router._finance_diagnostics_response(
        organization_id=1, cache=cache, range_start=datetime(2026, 9, 1), range_end=datetime(2026, 9, 2),
        resolved_period_days=2, period_suffix="2026-09-01_2026-09-02", limit=1, refreshed=False,
    )["diagnostics"]
    assert len(calls) == 1
    assert cache == original
    assert result["tax"]["taxKopecks"] == (None if missing else 7500)
    assert result["totals"]["taxKopecks"] == (None if missing else 7500)
    assert result["totals"]["expensesIfTaxIncludedKopecks"] == (None if missing else 8500)
    assert result["tax"]["taxPct"] is None  # A period can contain several dated rates.
    assert result["skuSummaries"][0]["taxKopecks"] == (None if missing else 7500)
    assert result["skuSummariesTotal"] == 2 and result["skuSummariesReturned"] == 1


def test_diagnostic_breakdown_preserves_unknown_tax_and_profit_with_unassigned_costs():
    from app.routers import wb_repricer_bff as router
    result = router._margin_breakdown_from_rows([{"analytics": {
        "revenueKopecks": 100_000, "taxKopecks": None, "netProfitKopecks": None,
        "factTaxState": "missing", "factTaxReason": "tax_policy_unconfirmed",
    }}], 10, finance_diagnostics={"rawExpenseTotals": {"storageKopecks": 1000}})
    item = result["items"][0]
    tax = next(component for component in item["components"] if component["key"] == "tax")
    assert tax["amountKopecks"] is tax["effectKopecks"] is None
    assert item["expectedNetProfitKopecks"] is item["actualNetProfitKopecks"] is item["deltaKopecks"] is None
    assert result["totals"]["tax"] is result["totals"]["taxEffect"] is None
    assert result["totals"]["expectedNetProfitKopecks"] is result["totals"]["actualNetProfitKopecks"] is None
    assert result["totals"]["storage"] == 1000


@pytest.mark.parametrize("confirmed", [True, False])
def test_rows_and_source_summary_share_dated_tax_and_unassigned_ads(tax_db, monkeypatch, confirmed):
    from app import repricer_bff as bff
    from app.routers import wb_repricer_bff as router
    _policy(tax_db, "2026-08-31T21:00:00", confirmed=confirmed)
    finance = _fact(100_000)
    finance.update(commissionKopecks=10_000, logisticsKopecks=2_000, storageKopecks=1000,
                   acquiringKopecks=500, financeAdSpendAuthoritative=True, adSpendKopecks=3000)
    cache = _cache(finance)
    tax = _resolve(cache, "2026-09-01")
    row = bff._build_sku_row(
        "synthetic", nm_id=123, name="Test", subject="Test", brand=None, chrt_ids=[],
        current_price_kopecks=150_000, discounted_price_kopecks=150_000, buyer_price_kopecks=135_000,
        promotions=[], use_demo_data=False, list_view=True, finance_aggregate=finance,
        finance_tax=tax, ads_aggregate={"adSpendKopecks": 9999},
    )
    monkeypatch.setattr(router, "list_cached_goods", lambda _org: [{"nmID": 123, "vendorCode": "synthetic"}])
    monkeypatch.setattr(router, "_period_source_cache", lambda _org, prefix, *args, **kwargs: cache if prefix == "finance" else {})
    monkeypatch.setattr(router, "get_source_cache", lambda *_args, **_kwargs: {})
    diagnostics = {"rawExpenseTotals": {"adSpendKopecks": 4000}}
    summary = router._repricer_list_summary_from_source_caches(
        1, resolved_period_days=1, period_suffix="2026-09-01_2026-09-01",
        range_start=date(2026, 9, 1), range_end=date(2026, 9, 1), finance_diagnostics=diagnostics,
    )
    assert summary["taxKopecks"] == row["analytics"]["taxKopecks"] == (7500 if confirmed else None)
    assert summary["marginKopecks"] == (row["analytics"]["netProfitKopecks"] - 1000 if confirmed else None)
    assert summary["factTaxState"] == row["analytics"]["factTaxState"]
    assert summary["adSpendKopecks"] == 4000
    assert summary["unassignedAdSpendKopecks"] == 1000
