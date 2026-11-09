"""Scoped strategy regressions: disposable DB, no sockets or real account."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import repricer_bff as bff, repricer_execution as ex, repricer_sprint_b as sprint
from app import repricer_strategy_sources as sources
from app.repricer_price_safety import seller_to_base, seller_price_interval
from app.repricer_cache.store import save_source_cache
from tests.test_repricer_safety_regressions import ARTICLE, NOW, Provider, healthy_row, isolated, _collect_night


def comparison(row, main, week, days=1):
    row["analytics"]["strategyPeriods"] = {
        "main": {"ordersUnits": main, "periodDays": days, "state": "ok", "periodTo": "2026-11-01"},
        "comparison": {"ordersUnits": week, "periodDays": 7, "state": "ok", "periodTo": "2026-11-01"}}
    return row


@pytest.mark.parametrize("edge", [75, 85, 95, 105, 115, 125])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_metric_classification_immediately_around_every_exact_edge(edge, offset):
    value = comparison(healthy_row(), edge * 10000 + offset, 7000000)
    index = ex._revenue_index(value)
    assert index == Decimal(edge) + Decimal(offset) / 10000
    # These independent expectations capture the half-open bands.
    expected = (-5, 3000) if index < 75 else (-4, 2000) if index < 85 else (-3, 1000) if index < 95 else (
        (0, 0) if index < 105 else (3, 1000) if index < 115 else (4, 2000) if index < 125 else (5, 3000))
    assert ex._revenue_band_delta(index) == expected


@pytest.mark.parametrize("main,week,expected", [(30, 70, 172000), (36, 70, 177160), (24, 70, 166840)])
def test_optimal_uses_orders_per_day_in_trailing_three_and_seven_days(main, week, expected):
    value = comparison(healthy_row(), main, week, days=3)
    assert ex._optimal_price_recommended_price(value)[0] == expected


@pytest.mark.parametrize("state", ["partial", "failed", "stale", "no_data"])
def test_bad_demand_coverage_blocks_before_a_draft(state):
    provider = Provider()
    value = healthy_row(provider)
    value["strategy"].update(id="turnover_control")
    value["settings"]["nightMedianEnabled"] = False
    value["analytics"]["periodStatsState"] = state
    result = ex._execute_single_sku(value, client=provider, actor_id="synthetic", actor_role="admin",
        options=ex.StrategyExecuteOptions(autoApprove=False, applyPrices=False), organization_id=1)
    assert result.status == "blocked" and not result.draftId and not provider.calls


@pytest.mark.parametrize("stock,orders,days,expected", [(14, 14, 14, 14), (0, 14, 14, 0), (14, 0, 14, None),
    (None, 14, 14, None), (14, None, 14, None), (14, 14, None, None), (14, 14, 0, None)])
def test_inventory_duration_and_zero_are_distinct(stock, orders, days, expected):
    value = healthy_row()
    value["analytics"].update(wbStockUnits=stock, ordersUnits=orders, periodDays=days)
    assert ex._days_to_oos(value) == expected


@pytest.mark.parametrize("baskets,revenue", [(None, None), (999999, 1), (0, 999999999)])
def test_order_metric_does_not_change_with_revenue_or_basket_values(baskets, revenue):
    value = comparison(healthy_row(), 12, 70)
    value["analytics"].update(baskets=baskets, revenueKopecks=revenue, basePriceKopecks=999999999)
    assert ex._revenue_index(value) == 120


@pytest.mark.parametrize("strategy,days", [("optimal_price", 3), ("metric_dynamics", 1)])
def test_missing_or_zero_comparison_cannot_become_an_actionable_price(strategy, days):
    value = comparison(healthy_row(), 0, 0, days)
    assert "comparison_zero_baseline" in ex._strategy_data_blockers(value, strategy)
    value["analytics"]["strategyPeriods"]["comparison"]["ordersUnits"] = None
    assert "comparison_orders_period_required" in ex._strategy_data_blockers(value, strategy)


@pytest.mark.parametrize("minimum,maximum,expected", [(172001, 180000, 172043), (160000, 171999, 171957),
    (172000, 172000, 172000)])
def test_provider_rounding_stays_inside_both_bounds(minimum, maximum, expected):
    _, actual = seller_to_base(172000, 57, minimum=minimum, maximum=maximum)
    assert actual == expected and minimum <= actual <= maximum


def test_no_provider_representation_or_economic_step_intersection_is_blocked():
    with pytest.raises(ValueError, match="NO_REPRESENTABLE"):
        seller_to_base(172010, 57, minimum=172001, maximum=172020)
    low, high = seller_price_interval(168000, 189511, 300000, 6, 168000, 20)
    assert low == 189511 and high == 178080 and low > high
    with pytest.raises(ValueError, match="WB_PRICE_ROUNDS_TO_ZERO"):
        seller_to_base(1, 0)


def test_normal_draft_selects_a_safe_alternative_to_nearest_rounding():
    provider = Provider()
    value = healthy_row(provider)
    created = sprint.create_price_draft("synthetic", "admin", provider, sprint.PriceDraftCreateRequest(
        articleId=ARTICLE, nmId=101, candidateSellerPriceKopecks=172001,
        minPriceKopecks=172001, pMaxKopecks=300000, economics=ex._economics_from_row(value)),
        sku_row=value, organization_id=1)
    assert created.state == "draft" and created.normalized.candidateSellerPriceKopecks == 172043
    assert not provider.uploads


@pytest.mark.parametrize("night,approve,apply", [(False, True, True), (True, False, True), (True, True, False), (True, False, False)])
def test_night_permission_matrix_never_escalates_an_invocation(monkeypatch, night, approve, apply):
    provider = Provider()
    value = _collect_night(monkeypatch, provider, [2, 8, 10])
    bff.ALGORITHM_SETTINGS_STATE["nightMedianAutoApplyEnabled"] = night
    monkeypatch.setattr(ex, "get_settings", lambda: SimpleNamespace(real_price_apply_enabled=True, repricer_local_price_apply_enabled=False))
    result = ex._execute_single_sku(value, client=provider, actor_id="synthetic", actor_role="system",
        options=ex.StrategyExecuteOptions(autoApprove=approve, applyPrices=apply), organization_id=1, apply_context={})
    assert result.status == "executed" and not provider.uploads
    if not night or not approve:
        assert result.applyState == "approval_required"


def test_selected_group_member_reads_other_members_without_writing_them():
    provider = Provider()
    first, second = healthy_row(provider), healthy_row(provider, 102)
    for value in (first, second):
        value["settings"]["nightMedianEnabled"] = False
        value["analytics"]["ordersUnits"] = 10
    first["strategy"].update(id="plan_fact_group", config={"groupArticleIds": [ARTICLE, "synthetic-102"], "planOrders": 40})
    original = deepcopy(second)
    result = ex.execute_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
        options=ex.StrategyExecuteOptions(autoApprove=False, applyPrices=False), sku_rows=[first, second],
        organization_id=1, persist_run=False)
    assert len(result.items) == 1 and result.items[0].draftId and second == original
    assert "факт 20" in result.items[0].explanation and not provider.uploads


def test_account_bound_cached_windows_are_reused_independent_of_open_report(monkeypatch):
    from app.repricer_sync import wb_token_fingerprint, _period_cache_suffix
    provider = Provider()
    row = healthy_row(provider)
    row["settings"]["nightMedianEnabled"] = False
    end = NOW.date() - timedelta(days=1)
    fingerprint = wb_token_fingerprint(provider.token)
    save_source_cache(1, "stocks", {"strategyTokenFingerprint": fingerprint,
        "dateTo": NOW.date().isoformat(), "fetchedAt": NOW.isoformat(), "aggregates": {"101": {"wbStockUnits": 14}}})
    for days, count in ((3, 6), (7, 14)):
        start = end - timedelta(days=days - 1)
        suffix = _period_cache_suffix(days, date_from=start, date_to=end)
        save_source_cache(1, f"baskets_{suffix}", {"strategyTokenFingerprint": fingerprint,
            "dateFrom": start.isoformat(), "dateTo": end.isoformat(), "fetchedAt": NOW.isoformat(),
            "aggregateComplete": True, "aggregates": {"101": {"orderCount": count, "cartCount": 20}}})
    queued = []
    monkeypatch.setattr(sources, "queue_strategy_sources", lambda *args: queued.append(args))
    row["analytics"].update(periodDays=99, ordersUnits=99999)
    prepared = sources.prepare_strategy_rows([row], org=1, token=provider.token,
        algorithm={"nightMedianEnabled": False}, default_days=30, now=NOW, queue_missing=True)
    assert ex._order_comparison(prepared[0], 3) == (Decimal(100), [])
    assert prepared[0]["analytics"]["wbStockUnits"] == 14 and queued == []
    assert row["analytics"]["ordersUnits"] == 99999


def test_fractional_plan_duration_keeps_an_exact_band_boundary():
    value = healthy_row()
    value["meta"]["currentPriceKopecks"] = 100000
    value["settings"].update(ordersPlanQty=10, ordersPlanDays=7)
    value["analytics"].update(periodDays=3, ordersUnits=3)
    assert ex._plan_fact_pct(value) == 70
    assert ex._plan_fact_recommended_price(value, 70000)[0] == 97000


def test_old_worker_proposal_without_a_revision_requires_recalculation():
    from app.repricer_persistence import store
    provider = Provider()
    row = healthy_row(provider)
    payload = sprint.PriceDraftCreateRequest(articleId=ARTICLE, nmId=101, candidateSellerPriceKopecks=182320,
        minPriceKopecks=100000, pMaxKopecks=300000, economics=ex._economics_from_row(row))
    store.upsert_pending_price_approvals(organization_id=1, approvals=[{
        "approvalId": "legacy-worker", "articleId": ARTICLE, "source": "worker", "draftRequest": payload.model_dump(mode="json")}])
    created = sprint.create_price_draft("synthetic", "admin", provider, payload, sku_row=row,
        organization_id=1, pending_approval_id="legacy-worker")
    assert created.state == "blocked"
    assert "strategy_recalculation_required" in [trigger.code for trigger in created.guardReport.triggers]
    assert not provider.calls


@pytest.mark.parametrize("automatic", [False, True])
def test_cached_sources_reach_normal_engine_dispatch_readback_and_history(monkeypatch, isolated, automatic):
    from app import repricer_dispatch_store as ledger
    from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow, WbRepricerRuntimeStateRow, WbRepricerChangelogRow
    from app.repricer_sync import wb_token_fingerprint, _period_cache_suffix
    from sqlalchemy import select
    provider = Provider()
    unrelated = deepcopy(provider.goods[102])
    value = healthy_row(provider)
    value["settings"]["nightMedianEnabled"] = False
    with isolated() as session:
        session.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload = {
            "taxPct": 22, "nightMedianEnabled": False, "nightMedianAutoEnableAllSkus": False}
        session.get(WbRepricerRuntimeStateRow, 1).state_payload = {
            "assignments": {ARTICLE: {"strategyId": "optimal_price", "source": "manual"}},
            "skuSettingsOverrides": {ARTICLE: {"automationEnabled": True}}}
        session.commit()
    value["meta"]["pricingSettingsRevision"] = ledger.pricing_settings_revision(1, ARTICLE)
    end = NOW.date() - timedelta(days=1)
    fingerprint = wb_token_fingerprint(provider.token)
    save_source_cache(1, "stocks", {"strategyTokenFingerprint": fingerprint, "fetchedAt": NOW.isoformat(),
        "dateTo": NOW.date().isoformat(), "aggregates": {"101": {"wbStockUnits": 14}}})
    for days, count in ((3, 9), (7, 14)):
        first = end - timedelta(days=days - 1)
        save_source_cache(1, f"baskets_{_period_cache_suffix(days, date_from=first, date_to=end)}", {
            "strategyTokenFingerprint": fingerprint, "fetchedAt": NOW.isoformat(), "dateFrom": first.isoformat(),
            "dateTo": end.isoformat(), "aggregateComplete": True, "aggregates": {"101": {"orderCount": count, "cartCount": 20}}})
    monkeypatch.setattr(ex, "_utc_now", lambda: NOW)
    monkeypatch.setattr(ex, "prepare_strategy_rows", sources.prepare_strategy_rows)
    monkeypatch.setattr(ex, "get_settings", lambda: SimpleNamespace(real_price_apply_enabled=True,
        repricer_local_price_apply_enabled=False, repricer_wb_sync_period_days=30))
    queued = []
    monkeypatch.setattr(sources, "queue_strategy_sources", lambda *args: queued.append(args))
    result = ex.execute_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
        wb_token=provider.token, sku_rows=[value], organization_id=1,
        options=ex.StrategyExecuteOptions(autoApprove=automatic, applyPrices=automatic))
    assert result.executedCount == 1 and not result.blockedCount, result.model_dump()
    assert result.items[0].recommendedPriceKopecks == 177160
    assert result.items[0].priceConfirmed is automatic and len(provider.uploads) == int(automatic)
    assert not queued and provider.goods[102] == unrelated
    if automatic:
        assert all(size["price"] == 4120 and size["discountedPrice"] == 1771.6 for size in provider.goods[101]["sizes"])
        assert provider.goods[101]["discount"] == 57
    else:
        assert result.items[0].applyState == "approval_required"
    with isolated() as session:
        assert len(session.scalars(select(WbRepricerChangelogRow)).all()) == int(automatic)


@pytest.mark.parametrize("bad", ["wrong_account", "partial", "stale", "oldest_stale", "missing_observation"])
def test_source_loader_never_promotes_unverified_or_incomplete_cache_to_a_signal(monkeypatch, bad):
    from app.routers import wb_repricer_bff as router
    from app.repricer_sync import wb_token_fingerprint
    provider = Provider()
    fingerprint = wb_token_fingerprint(provider.token)
    save_source_cache(1, "stocks", {"strategyTokenFingerprint": fingerprint, "fetchedAt": NOW.isoformat(),
        "dateTo": NOW.date().isoformat(), "aggregates": {"101": {"wbStockUnits": 14}}})
    def cached(org, prefix, suffix, days, first, last, **kwargs):
        payload = {"strategyTokenFingerprint": fingerprint, "fetchedAt": NOW.isoformat(),
            "dateFrom": first.date().isoformat(), "dateTo": last.date().isoformat(),
            "aggregates": {"101": {"orderCount": 9, "cartCount": 20}}}
        if bad == "wrong_account": payload["strategyTokenFingerprint"] = "other-synthetic-account"
        if bad == "partial": payload["coverageState"] = "partial"
        if bad == "stale": payload["fetchedAt"] = (NOW - timedelta(hours=2)).isoformat()
        if bad == "oldest_stale": payload["oldestSourceFetchedAt"] = (NOW - timedelta(hours=2)).isoformat()
        if bad == "missing_observation": payload.pop("fetchedAt")
        return payload
    monkeypatch.setattr(router, "_period_source_cache", cached)
    queued = []
    monkeypatch.setattr(sources, "queue_strategy_sources", lambda *args: queued.append(args))
    prepared = sources.prepare_strategy_rows([healthy_row(provider)], org=1, token=provider.token,
        algorithm={"nightMedianEnabled": False}, default_days=30, now=NOW, queue_missing=True)
    assert ex._strategy_data_blockers(prepared[0], "optimal_price") and len(queued) == 1
    assert not provider.calls


def test_identical_source_preparation_requests_are_coalesced(monkeypatch):
    from app.infra.celery_app import celery_app
    calls = []
    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(celery_app.amqp.router, "route", lambda *args, **kwargs: {
        "queue": SimpleNamespace(name="synthetic-readonly-reports")})
    requests = [("baskets", "2026-10-26", "2026-11-01")]
    sources.queue_strategy_sources(1, "synthetic-binding", requests)
    sources.queue_strategy_sources(1, "synthetic-binding", requests)
    assert len(calls) == 1
    assert calls[0][0] == ("repricer.prepare_strategy_sources",)
    assert calls[0][1]["queue"] == "synthetic-readonly-reports"


@pytest.mark.parametrize("change", ["assignment", "credential"])
def test_strategy_or_binding_changed_during_rate_limit_wait_never_reaches_upload_transport(change):
    from app import repricer_dispatch_store as ledger
    from app.wb_api.client import RateLimitedWbApiClient
    from tests.test_repricer_safety_regressions import draft, apply
    provider = Provider()
    class Delayed(RateLimitedWbApiClient):
        def _request(self, request, *args, **kwargs):
            if request.path == "/api/v2/upload/task":
                if change == "assignment":
                    ledger.mutate_assignments(1, [ARTICLE], lambda state: (
                        state.setdefault("skuSettingsOverrides", {}).update({ARTICLE: {"automationEnabled": False}}) or {}))
                else:
                    provider.token += "-rotated-synthetic-binding"
            return super()._request(request, *args, **kwargs)
    client = Delayed(provider)
    created = draft(provider)
    job = apply(created, client)
    assert job.state == "blocked" and not job.lastApplyResult.wbMutationSent
    assert not provider.uploads
    receipt = ledger.load_receipt(1, sprint.get_draft_or_404(created.draftId).dispatchKey)
    assert receipt["state"] == "rejected"
