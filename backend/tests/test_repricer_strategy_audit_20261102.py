"""Audit only. Run with tests.repricer_offline_plugin; no real account access.

Strict xfails document unmet requirements, not successful checks or fixes.
All storage/provider effects are confined by the existing isolated fixture.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from tests.test_repricer_safety_regressions import (
    ARTICLE, Provider, bff, ex, healthy_row, isolated,  # noqa: F401
)


def row(price=100000, **analytics):
    return {"meta": {"articleId": ARTICLE, "currentPriceKopecks": price, "basketNorm": 7},
            "settings": {}, "strategy": {"config": {}},
            "analytics": {"ordersUnits": 7, "periodDays": 7, "wbStockUnits": 14, **analytics}}


@pytest.mark.parametrize("days,expected", [(2.99, 125000), (3, 115000), (4.99, 115000),
    (5, 110000), (6.99, 110000), (7, 100000), (8, 100000)])
def test_stockout_thresholds(days, expected):
    value = row(wbStockUnits=days)
    # Integer inventory with one hundred orders per day keeps exact boundaries.
    value["analytics"].update(wbStockUnits=round(days * 100), ordersUnits=700)
    assert ex._stockout_guard_recommended_price(value)[0] == expected


@pytest.mark.parametrize("days,expected", [(0, 125000), (3, 120000), (5, 115000),
    (7, 100000), (29, 100000), (30, 85000), (60, 80000), (90, 75000)])
def test_turnover_thresholds(days, expected):
    assert ex._turnover_recommended_price(row(wbStockUnits=days))[0] == expected


@pytest.mark.parametrize("target,stock,expected", [(10, 4, 125000), (10, 6, 112000),
    (10, 10, 100000), (10, 14, 90000), (10, 21, 80000)])
def test_turnover_target_mode(target, stock, expected):
    value = row(wbStockUnits=stock)
    value["settings"]["targetTurnover"] = target
    assert ex._turnover_recommended_price(value)[0] == expected


def test_stockout_uses_actual_analysis_duration():
    # 14 items / (14 orders / 14 days) = 14 days, not 7.
    assert ex._days_to_oos(row(wbStockUnits=14, ordersUnits=14, periodDays=14)) == 14


def test_unknown_stock_does_not_become_a_synthetic_days_to_oos():
    value = row(wbStockUnits=None)
    value["meta"]["basketsLast7d"] = 14
    assert ex._days_to_oos(value) is None


def test_optimal_equal_daily_demand_does_not_change_with_period_length():
    week = row(ordersUnits=7, periodDays=7)
    fortnight = row(ordersUnits=14, periodDays=14)
    for value in (week,fortnight):
        value["analytics"]["strategyPeriods"]={"main":{"ordersUnits":3,"periodDays":3,"state":"ok"},
            "comparison":{"ordersUnits":7,"periodDays":7,"state":"ok"}}
    assert ex._optimal_price_recommended_price(week)[0] == 100000
    assert ex._optimal_price_recommended_price(fortnight)[0] == 100000


def test_metric_dynamics_compares_last_closed_day_with_trailing_week_average():
    value = row()
    value["analytics"]["strategyPeriods"] = {"main": {"ordersUnits": 20, "periodDays": 1, "state": "ok"},
        "comparison": {"ordersUnits": 70, "periodDays": 7, "state": "ok"}}
    growth = ex._revenue_recommended_price(value, ex._economics_from_row(value))[0]
    value["analytics"]["strategyPeriods"]["comparison"]["ordersUnits"] = 280
    decline = ex._revenue_recommended_price(value, ex._economics_from_row(value))[0]
    assert decline < growth


@pytest.mark.parametrize("strategy", ["plan_fact_daily", "plan_fact_period", "baskets_orders", "metric_dynamics"])
def test_missing_demand_sources_cannot_create_a_price_draft(strategy):
    provider = Provider()
    value = healthy_row(provider)
    value["settings"]["nightMedianEnabled"] = False
    value["strategy"].update(id=strategy, typedStrategyId=bff.FRONTEND_STRATEGY_BY_ID[strategy]["typedStrategyId"])
    value["meta"]["basketsLast7d"] = None
    value["analytics"].update(ordersUnits=None, baskets=None, revenueKopecks=None,
        basketsState="no_data", periodStatsState="no_data", financeState="no_data")
    result = ex._execute_single_sku(value, client=provider, actor_id="audit", actor_role="admin",
        options=ex.StrategyExecuteOptions(autoApprove=False, applyPrices=False), organization_id=1,
        all_rows=[value])
    assert result.status in {"blocked", "skipped"}, result.model_dump()
    assert not result.draftId


def test_disabled_assignment_is_rejected_without_changing_state():
    provider = Provider()
    for strategy in ("cross_marketplace", "bundles"):
        with pytest.raises(HTTPException) as error:
            bff.apply_frontend_strategy_assignment([ARTICLE], strategy_id_or_name=strategy,
                sku_rows=[healthy_row(provider)])
        assert error.value.status_code == 409
    assert not bff.FRONTEND_STRATEGY_ASSIGNMENTS and not provider.calls


@pytest.mark.parametrize("strategy", [key for key, value in bff.FRONTEND_STRATEGY_BY_ID.items()
    if value["status"] != "disabled"])
def test_every_selectable_assignment_survives_real_persistence_and_clear(isolated, monkeypatch, strategy):
    from tests.test_repricer_safety_regressions import persistence
    from app.repricer_persistence.orm import WbRepricerRuntimeStateRow
    provider = Provider()
    value = healthy_row(provider)
    monkeypatch.setattr(bff, "SKU_AUDIT_EVENTS", {})
    monkeypatch.setattr(bff, "REPRICER_SKU_GROUPS", {})
    config = {"scheduleRules": [{"mode": "hold"}]} if strategy == "schedule" else (
        {"groupArticleIds": [ARTICLE], "planOrders": 5} if strategy == "plan_fact_group" else {})
    bff.ALGORITHM_SETTINGS_STATE["nightMedianDefaultAllSkusVersion"] = 1
    response = bff.apply_frontend_strategy_assignment([ARTICLE], strategy_id_or_name=strategy,
        sku_rows=[value], config=config)
    assigned = deepcopy(bff.FRONTEND_STRATEGY_ASSIGNMENTS[ARTICLE])
    assert response["strategy"]["id"] == strategy
    assert persistence.flush_repricer_bff_state(1, bff)
    bff.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
    persistence.hydrate_repricer_bff_state(1, bff)
    assert bff.FRONTEND_STRATEGY_ASSIGNMENTS[ARTICLE] == assigned
    bff.unassign_frontend_strategy_assignment([ARTICLE], sku_rows=[value])
    assert persistence.flush_repricer_bff_state(1, bff)
    persistence.hydrate_repricer_bff_state(1, bff)
    assert ARTICLE not in bff.FRONTEND_STRATEGY_ASSIGNMENTS
    assert bff.SKU_SETTINGS_OVERRIDES[ARTICLE]["automationEnabled"] is False
    with isolated() as session:
        assert session.get(WbRepricerRuntimeStateRow, 2).state_payload == {}
    assert not provider.calls


def test_preexisting_disabled_cross_assignment_cannot_execute():
    provider = Provider()
    value = healthy_row(provider)
    value["settings"].update(nightMedianEnabled=False, competeDiffType="pct", competeDiffValue=2, competePriceType="below")
    value["strategy"].update(id="cross_marketplace", typedStrategyId=None)
    value["analytics"]["competitorPriceKopecks"] = 180000
    result = ex._execute_single_sku(value, client=provider, actor_id="audit", actor_role="admin",
        options=ex.StrategyExecuteOptions(autoApprove=False, applyPrices=False), organization_id=1,
        all_rows=[value])
    assert result.blockedReasons == ["strategy_disabled"]
    assert not result.draftId


def test_explicit_night_strategy_cannot_be_replaced_by_metric_mode():
    value = row()
    value["settings"]["repricerMode"] = "revenue"
    value["strategy"].update(id="night_price_mode", typedStrategyId="night_price_mode")
    assert ex._resolve_strategy_id(value) == ("night_price_mode", "night_price_mode")


def test_group_requires_complete_member_population():
    value = row(ordersUnits=7)
    value["strategy"]["config"] = {"groupArticleIds": [ARTICLE, "synthetic-missing"], "planOrders": 14}
    price, _, blockers = ex._plan_fact_group_recommended_price(value, 70000, all_rows=[value])
    assert price is None and blockers


@pytest.mark.parametrize("orders,expected", [(1, 70000), (10, 94000), (20, 100000), (30, 110000)])
def test_group_calculation_uses_full_population_without_mutating_members(orders, expected):
    first, second = row(ordersUnits=orders), row(ordersUnits=orders)
    second["meta"]["articleId"] = "synthetic-second"
    first["strategy"]["config"] = {"groupArticleIds": [ARTICLE, "synthetic-second"], "planOrders": 40}
    original = deepcopy([first, second])
    price, _, blockers = ex._plan_fact_group_recommended_price(first, 70000, all_rows=[first, second])
    assert price == expected and blockers == []
    assert [first, second] == original


@pytest.mark.parametrize("fact,expected", [(0, 70000), (5, 100000), (8, 110000)])
def test_interval_plan_uses_the_history_weight(monkeypatch, fact, expected):
    monkeypatch.setattr(ex, "_utc_now", lambda: datetime(2026, 10, 14, 9, tzinfo=timezone.utc))
    value = row(hourlyOrders=[{"hour": 12, "ordersUnits": 5}, {"hour": 1, "ordersUnits": 15}],
        todayHourlyOrders=[{"hour": 12, "ordersUnits": fact}])
    value["settings"].update(ordersPlanQty=20, ordersPlanDays=1)
    price, explanation, blockers = ex._plan_fact_interval_recommended_price(value, 70000)
    assert price == expected and not blockers
    assert "25.0%" in explanation and "план 5.0" in explanation


@pytest.mark.parametrize("baskets,orders,expected", [(20, 12, 103000), (0, 12, 101000),
    (20, 0, 99000), (0, 0, 97000)])
def test_basket_order_matrix_independent_expected_percentages(baskets, orders, expected):
    from app import repricer_sprint_c as strategies
    provider = Provider()
    economics = ex._economics_from_row(healthy_row(provider))
    value = row(baskets=baskets, ordersUnits=orders)
    value["meta"]["basketNorm"] = 20
    basket_trend, order_trend = ex._baskets_orders_trends(value)
    item = strategies.StrategyDryRunItem(articleId=ARTICLE, nmId=101, candidateSellerPriceKopecks=100000,
        basketsTrend=basket_trend, ordersTrend=order_trend, economics=economics)
    # Use a real validated recommendation produced by the isolated helper, then
    # override only its numeric baseline for this pure matrix characterization.
    from tests.test_repricer_safety_regressions import sprint
    base = sprint.build_price_recommendation(client=provider,
        payload=sprint.PriceRecommendationRequest(articleId=ARTICLE, nmId=101, candidateSellerPriceKopecks=172000,
            economics=economics), sku_row=healthy_row(provider))
    base.normalized.currentSellerPriceKopecks = 100000
    version = strategies._latest("baskets_orders_4599")
    result = strategies._dry_run_4599(version, item, base)
    assert result.recommendedSellerPriceKopecks == expected


@pytest.mark.parametrize("index,expected", [(74, -3000), (75, -2000), (85, -1000),
    (95, 0), (105, 1000), (115, 2000), (125, 3000)])
def test_metric_bands_and_ruble_floors(index, expected):
    value = row(price=10000, ordersUnits=10, basePriceKopecks=10000, revenueKopecks=index * 1000)
    value["analytics"]["strategyPeriods"]={"main":{"ordersUnits":index,"periodDays":1,"state":"ok"},
        "comparison":{"ordersUnits":700,"periodDays":7,"state":"ok"}}
    assert ex._revenue_recommended_price(value, ex._economics_from_row(value))[0] == 10000 + expected


@pytest.mark.parametrize("mode,rule,expected", [
    ("set_price", {"priceKopecks": 104000}, 104000),
    ("delta_pct", {"deltaPct": -2}, 98000),
    ("delta_pct", {"deltaPct": 2}, 102000),
    ("hold", {}, 100000),
])
def test_schedule_uses_first_matching_rule_in_moscow(monkeypatch, mode, rule, expected):
    monkeypatch.setattr(ex, "_utc_now", lambda: datetime(2026, 10, 12, 21, 30, tzinfo=timezone.utc))
    value = row()
    value["strategy"]["config"]["scheduleRules"] = [
        {"dateFrom": "2026-10-13", "dateTo": "2026-10-13", "daysOfWeek": [2],
         "timeFrom": "23:00", "timeTo": "01:00", "mode": mode, **rule},
        {"mode": "set_price", "priceKopecks": 99999},
    ]
    price, _, blockers = ex._schedule_recommended_price(value)
    assert price == expected and blockers == []


@pytest.mark.parametrize("hour,active", [(22, False), (23, True), (0, True), (5, True), (6, False)])
def test_night_boundaries_are_half_open_moscow(monkeypatch, hour, active):
    bff.ALGORITHM_SETTINGS_STATE.update(nightMedianTimezone="Europe/Moscow", nightMedianEnabled=True,
        nightMedianCollectEnabled=True, nightMedianWindowStartHour=23, nightMedianWindowEndHour=6)
    local = datetime(2026, 10, 13, hour, tzinfo=timezone(timedelta(hours=3)))
    monkeypatch.setattr(ex, "_utc_now", lambda: local.astimezone(timezone.utc))
    assert ex._night_median_collecting_now() is active


def test_night_retains_only_latest_24_unique_samples_and_seven_windows(monkeypatch):
    provider = Provider()
    value = healthy_row(provider)
    start = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)
    for number in range(30):
        at = start + timedelta(minutes=number * 5)
        monkeypatch.setattr(ex, "_utc_now", lambda at=at: at)
        value["analytics"].update(baskets=number, basketsObservedAt=at.isoformat())
        assert ex.collect_night_median_baskets(1, [value])["skuCount"] == 1
    sample = ex._night_median_cache(1)["windows"]["2026-10-13"]["samples"][ARTICLE]
    assert [item["baskets"] for item in sample["values"]] == list(range(6, 30))
    assert sample["medianBaskets"] == 17.5
    cache = {"windows": {f"2026-10-{day:02}": {} for day in range(1, 11)}}
    assert list(ex._trim_night_median_cache(cache)["windows"]) == [f"2026-10-{day:02}" for day in range(4, 11)]


def test_typed_discovery_strategy_never_produces_a_production_candidate():
    from app import repricer_sprint_c as strategies
    from tests.test_repricer_safety_regressions import sprint
    provider = Provider()
    value = healthy_row(provider)
    economics = ex._economics_from_row(value)
    base = sprint.build_price_recommendation(client=provider,
        payload=sprint.PriceRecommendationRequest(articleId=ARTICLE, nmId=101, candidateSellerPriceKopecks=172000,
            economics=economics), sku_row=value)
    item = strategies.StrategyDryRunItem(articleId=ARTICLE, nmId=101,
        candidateSellerPriceKopecks=172000, economics=economics)
    result = strategies._dry_run_4445(strategies._latest("spp_turnover_4445"), item, base)
    assert result.recommendedSellerPriceKopecks is None
    assert not result.canCreateProductionDraft and not result.autoCommitAllowed
    assert any("discovery_required" in code for code in result.blockedReasons)
    assert not provider.uploads


@pytest.mark.parametrize("automatic", [False, True])
def test_worker_task_reaches_normal_engine_and_respects_automatic_approval(monkeypatch, automatic):
    from types import SimpleNamespace
    from app import repricer_tasks as tasks
    provider = Provider()
    value = healthy_row(provider)
    value["settings"]["nightMedianEnabled"] = False
    bff.FRONTEND_STRATEGY_ASSIGNMENTS[ARTICLE] = {"strategyId": "optimal_price"}
    bff.ALGORITHM_SETTINGS_STATE["workerAutoApplyPricesEnabled"] = automatic
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(
        repricer_scheduler_enabled=True, wb_api_mode="real", repricer_wb_sync_period_days=7,
        repricer_execute_interval_minutes=60))
    monkeypatch.setattr(ex, "get_settings", lambda: SimpleNamespace(
        real_price_apply_enabled=True, repricer_local_price_apply_enabled=False))
    monkeypatch.setattr(tasks, "is_wb_sync_running", lambda org: False)
    monkeypatch.setattr(tasks, "hydrate_repricer_bff_state", lambda *args: None)
    monkeypatch.setattr(tasks, "flush_repricer_bff_state", lambda *args: None)
    monkeypatch.setattr(tasks, "_scheduler_wb_token", lambda org: provider.token)
    monkeypatch.setattr(tasks, "_last_scheduler_execution_at", lambda org: None)
    monkeypatch.setattr(tasks, "list_cached_goods", lambda org: list(provider.goods.values()))
    monkeypatch.setattr(tasks, "get_wb_sync_status", lambda org: {"periodDays": 7})
    monkeypatch.setattr(tasks, "get_source_cache", lambda *args, **kwargs: {})
    monkeypatch.setattr(tasks, "list_repricer_skus", lambda *args, **kwargs: [value])
    monkeypatch.setattr(tasks, "build_wb_client", lambda *args, **kwargs: provider)
    monkeypatch.setattr(tasks, "RateLimitedWbApiClient", lambda inner: inner)
    result = tasks.execute_assigned_for_org.run(1)
    assert result["executedCount"] == 1 and not result["blockedCount"]
    assert len(provider.uploads) == int(automatic)
    if automatic:
        assert [item["nmID"] for item in provider.uploads[0]["data"]] == [101]


@pytest.mark.parametrize("reason", ["none", "disabled", "manual", "warmup"])
def test_clear_and_nonautomatic_rows_cannot_execute_or_create_draft(reason):
    provider = Provider()
    value = healthy_row(provider)
    if reason == "none":
        value["strategy"]["assignmentSource"] = "derived"
        value["meta"]["assignmentSource"] = "none"
    elif reason == "disabled":
        value["settings"]["automationEnabled"] = False
    else:
        value["meta"]["status"] = reason
    result = ex._execute_single_sku(value, client=provider, actor_id="audit", actor_role="admin",
        options=ex.StrategyExecuteOptions(), organization_id=1, all_rows=[value])
    assert result.status == "skipped" and not result.draftId
    assert not provider.calls


def test_confirmed_history_is_exactly_once_and_pending_has_no_success(isolated):
    from tests.test_repricer_safety_regressions import draft, apply, sprint
    from app.repricer_persistence.orm import WbRepricerChangelogRow
    from sqlalchemy import select
    provider = Provider(confirm=False)
    value = draft(provider)
    pending = apply(value, provider)
    with isolated() as session:
        assert not session.scalars(select(WbRepricerChangelogRow)).all()
    provider.confirm_last()
    sprint.retry_apply_job(pending.jobId, provider, True, False, sprint.RetryApplyJobRequest())
    sprint.retry_apply_job(pending.jobId, provider, True, False, sprint.RetryApplyJobRequest())
    with isolated() as session:
        entries = session.scalars(select(WbRepricerChangelogRow)).all()
        assert len(entries) == 1
        assert entries[0].entry_payload["priceConfirmed"] is True
    assert len(provider.uploads) == 1


def test_night_auto_apply_off_still_requires_confirmation(monkeypatch):
    from types import SimpleNamespace
    from tests.test_repricer_safety_regressions import _collect_night
    provider = Provider()
    value = _collect_night(monkeypatch, provider, [2, 8, 10])
    bff.ALGORITHM_SETTINGS_STATE["nightMedianAutoApplyEnabled"] = False
    monkeypatch.setattr(ex, "get_settings", lambda: SimpleNamespace(
        real_price_apply_enabled=True, repricer_local_price_apply_enabled=False))
    result = ex._execute_single_sku(value, client=provider, actor_id="audit-worker", actor_role="system",
        options=ex.StrategyExecuteOptions(autoApprove=True, applyPrices=True), organization_id=1,
        all_rows=[value], apply_context={})
    assert result.applyState == "approval_required", result.model_dump()
    assert not provider.uploads
