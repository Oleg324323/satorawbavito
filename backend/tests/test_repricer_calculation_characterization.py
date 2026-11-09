"""Legacy behavior fixtures for the future immutable calculation-context move.

These tests call calculation/guard functions only. They do not approve the
business formulas or invoke draft/apply, routes, workers, storage, or providers.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import socket
from types import SimpleNamespace

import pytest


@pytest.fixture
def legacy(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("characterization must not perform I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    from app import repricer_execution as execution
    from app import repricer_sprint_b as sprint
    from app.repricer_settings import RepricerGuardLimits

    monkeypatch.setattr(execution, "ALGORITHM_SETTINGS_STATE", {})
    monkeypatch.setattr(execution, "LIQUIDATION_ACTIVE", {})
    monkeypatch.setattr(execution, "get_repricer_guard_limits", RepricerGuardLimits)
    monkeypatch.setattr(sprint, "get_repricer_guard_limits", RepricerGuardLimits)
    monkeypatch.setattr(sprint, "_daily_reference_price", lambda article, fallback: fallback)
    monkeypatch.setattr(sprint, "build_repricer_discovery_readiness", lambda: SimpleNamespace(
        canUnblockPriceGuard=True, blockingIds=[],
    ))
    for name in ("apply_approved_draft", "approve_draft", "create_price_draft",
                 "get_source_cache", "save_source_cache", "append_execution_run",
                 "upsert_pending_price_approvals"):
        monkeypatch.setattr(execution, name, forbidden)
    monkeypatch.setattr(sprint, "run_price_apply", forbidden)
    return execution, sprint


@pytest.mark.parametrize("completion,want", [
    (0, 70000), (9.99, 70000), (10, 85000), (20, 87000), (30, 90000),
    (40, 92000), (50, 94000), (60, 96000), (70, 97000), (80, 98000),
    (90, 99000), (100, 100000), (109.99, 100000), (110, 101000),
    (120, 102000), (130, 105000), (140, 110000),
])
def test_plan_fact_band_boundaries_preserve_exact_kopecks(legacy, completion, want):
    execution, _ = legacy
    price, _ = execution._plan_fact_price_from_completion(
        100000, 70000, completion, label="synthetic",
    )
    assert price == want


@pytest.mark.parametrize("current,delta,want", [
    (100005, 10, 110006), (100005, -10, 90004), (1, -99, 1),
])
def test_legacy_percentage_rounding_preserves_half_even_and_positive_floor(
    legacy, current, delta, want,
):
    assert legacy[0]._apply_pct(current, delta) == want


@pytest.mark.parametrize("candidate,current,settings,want", [
    (201300, 190000, {}, 201300),
    (201300, 190000, {"beautyPriceEnabled": True}, 199000),
    (201300, 200000, {"beautyPriceEnabled": True}, 201300),
    (201300, 190000, {"beautyPriceEnabled": True, "beautyPriceMaxChangeKopecks": 2000}, 201300),
    (201300, 190000, {"beautyPriceEnabled": True, "beautyPriceMaxChangePct": 1}, 201300),
    (201300, 190000, {"beautyPriceEnabled": True, "beautyPriceTemplate": "x99"}, 199900),
])
def test_pretty_price_limits_do_not_reverse_an_increase(legacy, candidate, current, settings, want):
    original = deepcopy(settings)
    assert legacy[0]._apply_price_rounding(candidate, current=current, settings=settings)[0] == want
    assert settings == original


@pytest.mark.parametrize("candidate,minimum,maximum,want", [
    (120000, 70000, None, 106000), (80000, 70000, None, 94000),
    (95000, 97000, None, 97000), (105000, 70000, 103000, 103000),
    (100000, 70000, None, 100000),
])
def test_clamp_preserves_update_step_and_price_bounds(legacy, candidate, minimum, maximum, want):
    assert legacy[0]._clamp_candidate_price(
        current=100000, recommended=candidate, min_price=minimum, p_max=maximum,
    )[0] == want


def test_day_cap_is_applied_after_update_cap(legacy, monkeypatch):
    execution, _ = legacy
    limits = execution.get_repricer_guard_limits()
    monkeypatch.setattr(execution, "get_repricer_guard_limits", lambda: replace(
        limits, per_update_step_pct=10, per_day_step_pct=3,
    ))
    assert execution._clamp_candidate_price(
        current=100000, recommended=120000, min_price=70000, p_max=None,
    )[0] == 103000


@pytest.mark.parametrize("baskets,want", [(0, 97000), (5, 97000), (6, 100000), (29, 100000), (30, 103000)])
def test_basket_thresholds_preserve_explicit_zero_and_boundaries(legacy, baskets, want):
    execution, _ = legacy
    execution.ALGORITHM_SETTINGS_STATE.update({"cartHighBasketsThreshold": 30, "cartLowBasketsThreshold": 5})
    row = {"meta": {"currentPriceKopecks": 100000, "basketsLast7d": 100},
           "analytics": {"baskets": baskets}}
    original = deepcopy(row)
    assert execution._basket_threshold_recommended_price(row)[0] == want
    assert row == original


def test_period_scaling_uses_selected_fact_period_without_mutating_row(legacy):
    execution, _ = legacy
    execution.ALGORITHM_SETTINGS_STATE.update({"basketNormPeriodDays": 7})
    row = {"meta": {"basketNorm": 70, "currentPriceKopecks": 100000},
           "analytics": {"ordersUnits": 140, "periodDays": 14}}
    original = deepcopy(row)
    assert execution._plan_fact_pct(row) == 100
    assert execution._daily_plan_orders(row) == 10
    assert row == original


def normalized(sprint, **changes):
    values = dict(articleId="synthetic-sku", nmId=123, currentSellerPriceKopecks=100000,
                  currentSppPct=20, candidateSellerPriceKopecks=100000,
                  cogsKopecks=40000, commissionPct=20, logisticsKopecks=5000,
                  buyoutPct=80, stockUnits=10, minPriceKopecks=70000,
                  predictedMarginKopecks=30000)
    values.update(changes)
    return sprint.NormalizedEconomics(**values)


def guard(sprint, value, *, source="fresh", pmin_valid=True, margin="fresh"):
    return sprint._evaluate_guards(
        value.articleId, SimpleNamespace(), SimpleNamespace(sourceStatus=source), value,
        SimpleNamespace(isValid=pmin_valid, reasons=[]), SimpleNamespace(sourceStatus=margin),
    )


@pytest.mark.parametrize("field,value,code", [
    ("currentSppPct", None, "spp_missing"), ("cogsKopecks", None, "cogs_missing"),
    ("commissionPct", None, "commission_missing"), ("logisticsKopecks", None, "logistics_missing"),
    ("buyoutPct", None, "buyout_missing"), ("stockUnits", None, "stock_missing"),
    ("stockUnits", 0, "stock_oos"), ("predictedMarginKopecks", -1, "negative_margin"),
    ("candidateSellerPriceKopecks", 106001, "per_update_step_exceeded"),
])
def test_guard_missing_inputs_and_unsafe_outcomes_block(legacy, field, value, code):
    _, sprint = legacy
    report = guard(sprint, normalized(sprint, **{field: value}))
    assert report.canApply is False
    assert "WB-23" in report.blockers
    assert code in {trigger.code for trigger in report.triggers}


@pytest.mark.parametrize("source", ["stale", "blocked"])
def test_stale_or_blocked_sources_prevent_apply(legacy, source):
    sprint = legacy[1]
    report = guard(sprint, normalized(sprint), source=source)
    assert not report.canApply
    assert "source_stale_or_blocked" in {trigger.code for trigger in report.triggers}


def test_exact_update_limit_is_allowed_but_daily_history_still_blocks(legacy, monkeypatch):
    sprint = legacy[1]
    value = normalized(sprint, candidateSellerPriceKopecks=106000)
    assert guard(sprint, value).canApply
    monkeypatch.setattr(sprint, "_daily_reference_price", lambda article, fallback: 80000)
    report = guard(sprint, value)
    assert not report.canApply
    assert {trigger.code for trigger in report.triggers} == {"per_day_step_exceeded"}


def test_promo_requires_minimum_and_candidate_must_respect_it(legacy):
    sprint = legacy[1]
    report = guard(sprint, normalized(sprint, promoActive=True, minPriceKopecks=None))
    assert not report.canApply
    assert "promo_min_price_missing" in {trigger.code for trigger in report.triggers}
    report = guard(sprint, normalized(sprint, minPriceKopecks=101000))
    assert not report.canApply
    assert "candidate_below_min_price" in {trigger.code for trigger in report.triggers}


def test_discovery_pmin_and_margin_blockers_are_preserved(legacy, monkeypatch):
    sprint = legacy[1]
    monkeypatch.setattr(sprint, "build_repricer_discovery_readiness", lambda: SimpleNamespace(
        canUnblockPriceGuard=False, blockingIds=["WB-22", "WB-23"],
    ))
    report = guard(sprint, normalized(sprint), pmin_valid=False, margin="blocked")
    assert not report.canApply
    assert report.blockers == ["WB-22", "WB-23"]
    assert {trigger.code for trigger in report.triggers} == {
        "discovery_not_ready", "pmin_pmax_block", "margin_preview_blocked",
    }


def test_margin_includes_configured_tax_exactly_once(legacy):
    sprint = legacy[1]
    economics = sprint.DraftEconomicsInput(
        cogsKopecks=40000, commissionPct=20, logisticsKopecks=5000,
        storageKopecks=1000, taxKopecks=9000,
    )
    assert sprint._predict_margin(80000, economics) == (9000, 11.25)
    assert sprint._predict_margin(None, economics) == (None, None)


@pytest.mark.parametrize("hour,window,previous", [
    (0, "2026-09-07", "2026-09-07"), (5, "2026-09-07", "2026-09-07"),
    (6, "2026-09-08", "2026-09-07"), (22, "2026-09-08", "2026-09-07"),
    (23, "2026-09-08", "2026-09-08"),
])
def test_night_window_boundaries_preserve_cross_midnight_keys(legacy, hour, window, previous):
    execution, _ = legacy
    now = datetime(2026, 9, 8, hour, tzinfo=timezone(timedelta(hours=3)))
    assert execution._night_window_key(now) == window
    assert execution._night_previous_window_key(now) == previous


@pytest.mark.parametrize("mode,configured,want", [
    ("conservative", 4, 3), ("aggressive", 4, 4),
    ("conservative", 40, 20), ("aggressive", 40, 20),
])
def test_night_delta_mode_and_cap_are_characterized(legacy, mode, configured, want):
    execution, _ = legacy
    execution.ALGORITHM_SETTINGS_STATE.update({"nightMedianMode": mode, "nightMedianApplyDeltaPct": configured})
    assert execution._night_target_delta_pct() == want


@pytest.mark.parametrize("current,want", [(7, -3), (8, 3), (9, 3)])
def test_night_median_direction_and_input_immutability(legacy, monkeypatch, current, want):
    execution, _ = legacy
    from app.repricer_price_safety import checksum
    morning = datetime(2026, 9, 8, 3, 1, tzinfo=timezone.utc)
    values = []
    for index, baskets in enumerate((2, 8, 10)):
        stamp = datetime(2026, 9, 7, 20 + index, tzinfo=timezone.utc).isoformat()
        values.append({"baskets": baskets, "at": stamp,
            "observationId": checksum([7, "synthetic-scope", 101, "2026-09-07", stamp])})
    payload = {"windows": {"2026-09-07": {
        "samples": {"synthetic": {"values": values, "nmId": 101, "organizationId": 7, "accountScope": "synthetic-scope"}},
        "applied": {},
    }}}
    original = deepcopy(payload)
    monkeypatch.setattr(execution, "_night_median_cache", lambda org: payload)
    monkeypatch.setattr(execution, "_night_previous_window_key", lambda: "2026-09-07")
    monkeypatch.setattr(execution, "_night_median_collecting_now", lambda: False)
    monkeypatch.setattr(execution, "_utc_now", lambda: morning)
    result = execution._night_median_apply_payload({"meta": {"articleId": "synthetic", "nmId": 101, "accountScope": "synthetic-scope"},
        "analytics": {"baskets": current, "basketsObservedAt": morning.isoformat()}}, 7)
    assert result == {"windowKey": "2026-09-07", "medianBaskets": 8.0,
                      "currentBaskets": current, "samplesCount": 3, "deltaPct": want,
                      "accountScope": "synthetic-scope", "nmId": 101}
    assert payload == original


@pytest.mark.parametrize("case", ["already_applied", "collecting", "disabled", "missing_current", "empty_samples", "no_org"])
def test_night_median_skip_conditions_do_not_create_an_apply_payload(legacy, monkeypatch, case):
    execution, _ = legacy
    window = {"samples": {"synthetic": {"values": [{"baskets": 8}]}}, "applied": {}}
    row = {"meta": {"articleId": "synthetic"}, "analytics": {"baskets": 10}}
    if case == "already_applied":
        window["applied"]["synthetic"] = {"jobId": "existing"}
    if case == "empty_samples":
        window["samples"]["synthetic"]["values"] = []
    if case == "missing_current":
        row["analytics"] = {}
    if case == "disabled":
        execution.ALGORITHM_SETTINGS_STATE["nightMedianEnabled"] = False
    monkeypatch.setattr(execution, "_night_median_cache", lambda org: {"windows": {"2026-09-07": window}})
    monkeypatch.setattr(execution, "_night_previous_window_key", lambda: "2026-09-07")
    monkeypatch.setattr(execution, "_night_median_collecting_now", lambda: case == "collecting")
    assert execution._night_median_apply_payload(row, None if case == "no_org" else 7) is None


@pytest.mark.parametrize("orders,target,want", [(0, 80000, 97000), (0, 98000, 98000), (1, 80000, 100000), (2, 80000, 103000)])
def test_liquidation_demand_branches_preserve_target_floor(legacy, orders, target, want):
    execution, _ = legacy
    execution.LIQUIDATION_ACTIVE["synthetic"] = {"currentPriceKopecks": 100000,
        "stepPct": 3, "holdOrdersTo": 1, "targetPriceKopecks": target}
    row = {"meta": {"articleId": "synthetic", "currentPriceKopecks": 110000}, "analytics": {"ordersUnits": orders}}
    original = deepcopy(execution.LIQUIDATION_ACTIVE)
    result, _, blockers = execution._illiquid_recommended_price(row)
    assert result == want
    assert blockers == []
    assert execution.LIQUIDATION_ACTIVE == original


def test_liquidation_future_step_skips_without_changing_runtime(legacy, monkeypatch):
    execution, _ = legacy
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    execution.LIQUIDATION_ACTIVE["synthetic"] = {"nextStepAt": (now + timedelta(seconds=1)).isoformat()}
    monkeypatch.setattr(execution, "_utc_now", lambda: now)
    result, _, blockers = execution._illiquid_recommended_price({"meta": {"articleId": "synthetic"}})
    assert result is None
    assert blockers == ["liquidation_not_due"]


@pytest.mark.parametrize("strategy,want", [
    ("baskets_orders", ("baskets_orders", "baskets_orders_4599")),
    ("metric_dynamics", ("metric_dynamics", "revenue_dynamics_4600")),
    ("illiquid", ("illiquid", None)), ("unknown-strategy", (None, None)),
])
def test_explicit_strategy_resolution_does_not_read_global_default(legacy, monkeypatch, strategy, want):
    execution, _ = legacy
    def forbidden():
        raise AssertionError("explicit strategy must not read persisted default")
    monkeypatch.setattr(execution, "default_typed_strategy_for_mode", forbidden)
    assert execution._resolve_strategy_id({"meta": {}, "strategy": {"id": strategy}}) == want


@pytest.mark.parametrize("settings,want", [
    ({}, 1),
    ({"pMinKopecks": 100, "pminKopecks": 200, "cogsKopecks": 900}, 100),
    ({"pMinKopecks": 0, "pminKopecks": 200}, 200),
    ({"pMinKopecks": 0, "cogsKopecks": 100}, 100),
    ({"cogsKopecks": 10000, "logisticsKopecks": 1000,
      "otherExpensePerSaleKopecks": 200, "storageCostPerSaleKopecks": 300,
      "minMarginKopecks": 500, "wbCommissionPct": 10, "minMarginPct": 10}, 15000),
    ({"cogsKopecks": 10000, "pickPackCostPercent": 2.5}, 10250),
    ({"cogsKopecks": 10000, "minMarginKopecks": 500, "taxPct": 100}, 10500),
    ({"cogsKopecks": 9000, "promoCostPercent": 10, "otherExpensePricePct": 5}, 10000),
])
def test_legacy_pmin_override_inheritance_and_expense_precedence(legacy, settings, want):
    execution, _ = legacy
    before = deepcopy(settings)
    assert execution._settings_pmin_kopecks(settings) == want
    assert settings == before


@pytest.mark.parametrize("settings,want", [
    ({"pMaxKopecks": 200, "rrpKopecks": 10000, "targetDiscountPct": 20}, 200),
    ({"rrpKopecks": 10000, "targetDiscountPct": 20}, 8000),
    ({"rrpKopecks": 10000, "targetDiscountPct": 150}, 100),
    ({"rrpKopecks": 10000, "targetDiscountPct": 0}, 20000),
    ({"cogsKopecks": 9000, "maxMarginKopecks": 1000, "maxMarginPct": 20}, 12500),
    ({"cogsKopecks": 9000, "maxMarginKopecks": 1000, "maxMarginPct": 100}, 20000),
    ({"pMaxKopecks": 0}, 20000),
])
def test_legacy_pmax_priority_and_invalid_denominator_fallback(legacy, settings, want):
    execution, _ = legacy
    before = deepcopy(settings)
    assert execution._settings_pmax_kopecks(settings, 10000) == want
    assert settings == before


@pytest.mark.parametrize("settings,global_minutes,want", [
    ({"priceStepMinutes": 1, "priceStepHours": 2}, 60, 5),
    ({"priceStepMinutes": 15, "priceStepHours": 2}, 60, 15),
    ({"priceStepMinutes": 0, "priceStepHours": 2}, 60, 120),
    ({}, 0, 60), ({}, 1, 5),
    ({"priceStepMinutes": "invalid", "priceStepHours": "invalid"}, 7, 7),
])
def test_legacy_interval_override_and_global_fallback(legacy, monkeypatch, settings, global_minutes, want):
    execution, _ = legacy
    state = {"syncIntervalMinutes": global_minutes}
    before = deepcopy(settings)
    monkeypatch.setattr(execution, "ALGORITHM_SETTINGS_STATE", state)
    assert execution._settings_step_interval_minutes(settings) == want
    assert settings == before and state == {"syncIntervalMinutes": global_minutes}


def test_legacy_cost_float_precision_loss_is_evidence_not_new_money_policy(legacy):
    execution, _ = legacy
    amount = 2**53 + 1
    # Direct explicit minimum is exact; legacy cost normalization crosses float.
    # A future adapter must not claim end-to-end exactness from the storage codec.
    assert execution._settings_pmin_kopecks({"pMinKopecks": amount}) == amount
    assert execution._settings_pmin_kopecks({"cogsKopecks": amount}) == amount - 1
