"""Read-only diagnostics must describe the execution contract without applying prices."""
from copy import deepcopy

import pytest

from app import repricer_bff as bff
from app import repricer_execution as execution
from app.modules import wb_repricing_calculation as calculation
from app.routers import wb_repricer_bff as router


def _settings():
    return {
        "automationEnabled": True, "pMinKopecks": 0,
        "cogsKopecks": 40_000, "logisticsKopecks": 5_000,
        "otherExpensePerSaleKopecks": 2_000, "storageCostPerSaleKopecks": 1_000,
        "pickPackCostPercent": 10, "minMarginKopecks": 3_000,
        "wbCommissionPct": 20, "minMarginPct": 10, "taxPct": 7.5,
        "promoCostPercent": 3, "otherExpensePricePct": 5, "advertCostPercent": 2.5,
    }


def _row():
    return {
        "meta": {"articleId": "synthetic", "nmId": 123, "currentPriceKopecks": 150_000, "status": "auto"},
        "settings": _settings(),
        "strategy": {"id": "baskets_orders", "assignmentSource": "manual"},
        "analytics": {
            "sppState": "ok", "sppPct": 10, "buyerPriceNoWalletKopecks": 135_000,
            "commissionState": "ok", "wbCommissionPct": 20, "buyoutPct": 80,
            "stockState": "ok", "wbStockUnits": 20,
            "financeState": "ok", "basketsState": "ok", "periodStatsState": "ok",
        },
    }


@pytest.mark.parametrize("change,expected", [
    ({}, 100_000),
    ({"pMinKopecks": 75_001}, 75_001),
    ({"pminKopecks": "75002"}, 75_002),
    ({"promoCostPercent": 5, "otherExpensePricePct": 3}, 100_000),
    ({"minMarginPct": 65}, 55_000),  # Preserve the legacy invalid-denominator branch.
])
def test_shared_floor_preserves_existing_execution_amount_and_expense_alias(change, expected):
    settings = {**_settings(), **change}
    original = deepcopy(settings)
    assert execution._settings_pmin_kopecks(settings) == expected
    assert calculation.settings_minimum_price_kopecks(settings) == expected
    assert settings == original


@pytest.mark.parametrize("explicit,expected,source", [(0, 100_000, "calculated"), (75_001, 75_001, "explicit")])
def test_bff_exposes_actual_floor_after_tariff_resolution_without_changing_manual_setting(monkeypatch, explicit, expected, source):
    settings = bff._sku_cost_settings("synthetic", use_demo_data=False)
    settings.update(_settings(), pMinKopecks=explicit, wbCommissionPct=1)
    original = deepcopy(settings)
    monkeypatch.setattr(bff, "_sku_cost_settings", lambda *_a, **_kw: deepcopy(settings))
    monkeypatch.setitem(bff.ALGORITHM_SETTINGS_STATE, "acquiringPct", 2)
    row = bff._build_sku_row(
        "synthetic", nm_id=123, name="Test", subject="Test", brand=None,
        chrt_ids=[], current_price_kopecks=150_000, discounted_price_kopecks=150_000,
        buyer_price_kopecks=135_000, promotions=[], use_demo_data=False, list_view=True,
        commission_tariffs_index=bff._commission_tariffs_index_from_rows([{"subjectName": "Test", "kgvpMarketplace": 18}]),
    )
    assert row["settings"]["wbCommissionPct"] == 20
    assert row["settings"]["effectivePMinKopecks"] == expected
    assert row["settings"]["effectivePMinSource"] == source
    assert row["settings"]["pMinKopecks"] == explicit
    assert settings == original


def test_stats_uses_calculated_floor_and_keeps_missing_live_buyer_blocker():
    row = _row()
    row["analytics"].update(sppState="no_buyer_price", sppPct=None, buyerPriceNoWalletKopecks=None)
    original = deepcopy(row)
    item = router._repricer_stats_item(row)
    protection = item["priceProtection"]
    assert protection["effectivePMinKopecks"] == 100_000
    assert protection["effectivePMinSource"] == "calculated"
    assert protection["blockerIds"] == ["spp"]
    assert protection["status"] == "blocked"
    assert item["decision"]["id"] == "price_blocked"
    assert row == original


def test_missing_cost_settings_do_not_create_a_one_kopeck_diagnostic_floor():
    row = _row()
    row["settings"] = {"automationEnabled": True}
    protection = router._repricer_stats_item(row)["priceProtection"]
    assert protection["effectivePMinKopecks"] is None
    assert "p_min" in protection["blockerIds"]


@pytest.mark.parametrize("missing", ["finance", "periodStats"])
def test_missing_period_report_needs_review_but_is_not_a_basket_strategy_send_blocker(missing):
    row = _row()
    row["analytics"][f"{missing}State"] = "no_data"
    item = router._repricer_stats_item(row)
    assert item["priceProtection"]["status"] == "needs_review"
    assert missing not in item["priceProtection"]["blockerIds"]
    assert missing in item["sources"]["missing"]


@pytest.mark.parametrize("change,blocker", [
    ({"commissionState": "fallback"}, "commission"),
    ({"buyoutPct": None}, "buyout"),
    ({"wbStockUnits": 0}, "stocks"),
])
def test_stats_preserves_actual_execution_economic_blockers(change, blocker):
    row = _row()
    row["analytics"].update(change)
    assert blocker in router._repricer_stats_item(row)["priceProtection"]["blockerIds"]


def test_zero_buyout_is_known_and_does_not_block():
    row = _row()
    row["analytics"]["buyoutPct"] = 0
    assert router._repricer_stats_item(row)["priceProtection"]["status"] == "can_recalculate"


@pytest.mark.parametrize("change,reason,status", [
    ({"strategy": {}}, "no_strategy", "not_configured"),
    ({"strategy": {"id": "baskets_orders", "assignmentSource": "derived"}}, "no_strategy", "not_configured"),
    ({"meta": {"status": "manual"}}, "manual_mode", "paused"),
    ({"meta": {"status": "warmup"}}, "warmup", "paused"),
    ({"meta": {"nmId": None}}, "no_nm_id", "not_configured"),
    ({"settings": {"automationEnabled": False}}, "automation_disabled", "paused"),
])
def test_execution_skipped_rows_are_distinct_from_send_blocked_and_keep_source_details(change, reason, status):
    row = _row()
    for section, values in change.items():
        row[section] = {} if not values else {**row[section], **values}
    row["analytics"].update(sppState="no_buyer_price", sppPct=None)
    execution_result = execution._execute_single_sku(
        row, client=object(), actor_id="synthetic", actor_role="admin",
        options=execution.StrategyExecuteOptions(createDrafts=False, applyPrices=False),
    )
    assert execution_result.status == "skipped"
    assert execution_result.skipReason == reason
    item = router._repricer_stats_item(row)
    assert item["priceProtection"]["status"] == status
    assert item["priceProtection"]["skipReason"] == reason
    assert "spp" in item["priceProtection"]["blockerIds"]
    assert reason in item["decision"]["reasons"]
    assert "price_blocked" not in item["flags"]
    assert router._repricer_stats_summary([item])["priceBlocked"] == 0


def test_liquidation_diagnostic_retains_existing_no_margin_execution_floor():
    row = _row()
    row["strategy"] = {"id": "illiquid", "assignmentSource": "derived"}
    row["settings"]["pMinKopecks"] = 75_000
    item = router._repricer_stats_item(row)
    assert item["priceProtection"]["effectivePMinKopecks"] == 56_250
    assert item["priceProtection"]["effectivePMinSource"] == "calculated"
    assert item["priceProtection"]["status"] == "can_recalculate"
    assert row["settings"]["pMinKopecks"] == 75_000
