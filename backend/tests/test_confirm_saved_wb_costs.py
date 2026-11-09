"""Explicit actual-cost confirmation uses disposable data, never the WB API."""
from datetime import date, datetime, timezone

from sqlalchemy import func, select

from app.platform.economics.backfill import LegacyCostSnapshot, confirm_saved_costs
from app.platform.economics.costs import CostsService
from app.platform.economics.legacy_tax import get_legacy_finance_taxes, legacy_finance_tax_revision
from app.platform.economics.orm import CatalogCostVersionRow
from app.platform.period import Period
from tests.test_legacy_finance_tax import tax_db, _cache, _fact, _policy  # noqa: F401

START = Period(date(2026, 10, 12), date(2026, 10, 18)).start_at


def snapshot(*, cost=720, override=None):
    return LegacyCostSnapshot(captured_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
        goods=[{"nmID": 123, "vendorCode": "sku", "title": "Худи"}],
        content_cards=[], runtime_state={"skuSettingsOverrides": {"sku": override or {}}},
        algorithm_settings={} if cost is None else {"cogsByGarmentRub": {"hoodie": cost}},
        costs_excel={})


def confirm(db, **kwargs):
    return confirm_saved_costs(db, organization_id=1, marketplace_account_id=12,
        snapshot=snapshot(**kwargs), effective_from=START)


def test_saved_cost_confirmation_changes_real_finance_inputs_not_other_expenses(tax_db):
    _policy(tax_db, "2026-10-11T21:00:00", rate=2200)
    before = legacy_finance_tax_revision(1)
    result = confirm(tax_db)
    assert result["confirmed"] == 1
    cache = _cache(_fact(300000, sales=3, returns=1), {
        "2026-10-12": _fact(200000, sales=2),
        "2026-10-18": _fact(100000, sales=1, returns=1)})
    fact = get_legacy_finance_taxes(1, cache, Period(date(2026, 10, 12), date(2026, 10, 18)), include_costs=True)["123"]
    assert fact["settlementCogsKopecks"] == 144000
    assert fact["settlementCogsState"] == "configured"
    assert fact["taxKopecks"] == 66000
    assert legacy_finance_tax_revision(1) != before
    previous = get_legacy_finance_taxes(1, _cache(_fact(300000)),
        Period(date(2026, 10, 5), date(2026, 10, 11)), include_costs=True)["123"]
    assert previous["settlementCogsKopecks"] is None


def test_confirmation_is_idempotent_and_does_not_replace_later_edit(tax_db):
    assert confirm(tax_db)["confirmed"] == 1
    assert confirm(tax_db)["preserved"] == 1
    CostsService(tax_db, 1).set_cost(catalog_sku_id=11, amount_kopecks=90000,
        value_state="configured", effective_from=datetime(2026, 10, 20, tzinfo=timezone.utc),
        source="manual", source_reference="newer", evidence_status="dated")
    assert confirm(tax_db, cost=800)["preserved"] == 1
    assert tax_db.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 2
    assert CostsService(tax_db, 1).get_cost_at(11, datetime(2026, 10, 25, tzinfo=timezone.utc)).amount_kopecks == 90000


def test_hardcoded_type_default_is_not_a_user_entered_actual_cost(tax_db):
    assert confirm(tax_db, cost=None)["missing"] == 1
    assert tax_db.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 0


def test_saved_product_override_including_zero_wins_over_category(tax_db):
    assert confirm(tax_db, override={"cogsKopecks": 0})["confirmed"] == 1
    assert CostsService(tax_db, 1).get_cost_at(11, START).amount_kopecks == 0


def test_wrong_account_never_confirms(tax_db):
    import pytest
    from app.platform.economics.backfill import BackfillValidationError
    with pytest.raises(BackfillValidationError):
        confirm_saved_costs(tax_db, organization_id=2, marketplace_account_id=12,
            snapshot=snapshot(), effective_from=START)
    assert tax_db.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 0
