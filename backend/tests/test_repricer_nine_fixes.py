"""Regressions for A01/A02/A03/A07; in-memory DB and synthetic provider only.

Run with -p tests.repricer_offline_plugin. UI regressions live in the frontend.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app import repricer_bff as bff, repricer_execution as ex, repricer_strategy_sources as sources
from app import repricer_dispatch_store as ledger
from app.repricer_cache.store import save_source_cache
from app.repricer_persistence import store
from app.repricer_persistence.orm import WbRepricerChangelogRow, WbRepricerExecutionRunRow, WbRepricerRuntimeStateRow, WbRepricerAlgorithmSettingsRow
from app.repricer_sync import _period_cache_suffix, wb_token_fingerprint
from app.routers import wb_repricer_bff as router
from tests import test_repricer_safety_regressions as base
from tests.test_repricer_safety_regressions import isolated, Provider, ARTICLE, healthy_row

NOW = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixed_clock(isolated, monkeypatch):
    from app import wb23_runtime, repricer_price_safety
    from app.repricer_cache import store as cache
    from app.wb_api import source_freshness
    monkeypatch.setattr(base, "NOW", NOW)
    for module in (wb23_runtime, repricer_price_safety, ledger, cache, source_freshness):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(ex, "_utc_now", lambda: NOW)
    monkeypatch.setattr(bff, "REPRICER_SKU_GROUPS", {})
    bff.ALGORITHM_SETTINGS_STATE.update(nightMedianEnabled=False, nightMedianAutoEnableAllSkus=False)
    monkeypatch.setattr(ex, "get_settings", lambda: SimpleNamespace(
        real_price_apply_enabled=True, repricer_local_price_apply_enabled=False, repricer_wb_sync_period_days=7))


def save_period(provider, now, days, orders, **patch):
    last = now.astimezone(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
    first = last - timedelta(days=days - 1)
    value = dict(strategyTokenFingerprint=wb_token_fingerprint(provider.token), fetchedAt=now.isoformat(),
        dateFrom=first.isoformat(), dateTo=last.isoformat(), aggregateComplete=True,
        aggregates={"101": {"orderCount": orders, "cartCount": 20}})
    value.update(patch)
    save_source_cache(1, f"baskets_{_period_cache_suffix(days, date_from=first, date_to=last)}", value)


def save_stock(provider, now):
    save_source_cache(1, "stocks", {"strategyTokenFingerprint": wb_token_fingerprint(provider.token),
        "fetchedAt": now.isoformat(), "dateTo": now.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat(),
        "aggregates": {"101": {"wbStockUnits": 90}}})


@pytest.mark.parametrize("many", [False, True])
def test_selected_preview_uses_normal_assignment_before_source_preparation_without_writes(isolated, monkeypatch, many):
    provider = Provider()
    rows = [healthy_row(provider, nm) for nm in ([101, 102] if many else [101])]
    for row in rows:
        row["strategy"].update(id="stockout_guard", typedStrategyId=None)
        row["analytics"].update(wbStockUnits=90, ordersUnits=14, periodDays=7)
        row["settings"]["normalStockQty"] = 100
    originals = deepcopy(rows)
    seen = []
    def prepare(values, **kwargs):
        seen.extend(deepcopy(values))
        assert kwargs["queue_missing"] is False
        return values
    monkeypatch.setattr(ex, "prepare_strategy_rows", prepare)
    snapshot = deepcopy((bff.FRONTEND_STRATEGY_ASSIGNMENTS, bff.SKU_SETTINGS_OVERRIDES, bff.SKU_META_OVERRIDES, bff.REPRICER_SKU_GROUPS))
    with isolated() as session:
        before = deepcopy(session.get(WbRepricerRuntimeStateRow, 1).state_payload)
    old = ex.preview_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin", sku_rows=rows, organization_id=1)
    report = ex.preview_repricer_strategies([row["meta"]["articleId"] for row in rows], client=provider,
        actor_id="synthetic", actor_role="admin", sku_rows=rows, wb_token=provider.token,
        organization_id=1, strategy_id_or_name="Контроль оборачиваемости")
    assert old.items[0].frontendStrategyId == "stockout_guard"
    assert old.items[0].recommendedPriceKopecks == 182320
    assert report.executedCount == len(rows)
    assert all(item.frontendStrategyId == "turnover_control" and item.recommendedPriceKopecks == 161680
               and item.draftId is None for item in report.items)
    assert all(row["strategy"]["id"] == "turnover_control" for row in seen)
    assert rows == originals and not provider.calls
    assert snapshot == (bff.FRONTEND_STRATEGY_ASSIGNMENTS, bff.SKU_SETTINGS_OVERRIDES, bff.SKU_META_OVERRIDES, bff.REPRICER_SKU_GROUPS)
    with isolated() as session:
        assert session.get(WbRepricerRuntimeStateRow, 1).state_payload == before
        assert not session.scalars(select(WbRepricerChangelogRow)).all()
        assert not session.scalars(select(WbRepricerExecutionRunRow)).all()


def test_preview_normalizes_config_and_rejects_disabled_or_invalid_strategy():
    provider = Provider()
    row = healthy_row(provider)
    result = ex.preview_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
        sku_rows=[row], organization_id=1, strategy_id_or_name="schedule",
        config={"scheduleRules": [{"mode": "delta_pct", "deltaPct": 2}]}).items[0]
    assert result.frontendStrategyId == "schedule" and result.recommendedPriceKopecks == 175440
    for strategy in ("cross_marketplace", "bundles", "not-a-strategy"):
        with pytest.raises(HTTPException):
            ex.preview_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
                sku_rows=[row], strategy_id_or_name=strategy)
    with pytest.raises(HTTPException):
        ex.preview_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
            sku_rows=[row], strategy_id_or_name="schedule", config={"scheduleRules": [{"mode": "set_price"}]})
    assert not provider.calls


@pytest.mark.parametrize("strategy,price", [("plan_fact_daily", 172000), ("optimal_price", 177160)])
def test_selected_preview_reads_the_selected_strategy_cached_windows(monkeypatch, strategy, price):
    provider = Provider()
    row = healthy_row(provider)
    row["strategy"].update(id="stockout_guard", typedStrategyId=None)
    row["settings"].update(ordersPlanQty=10, ordersPlanDays=1)
    save_stock(provider, NOW)
    save_period(provider, NOW, 3, 30 if strategy == "plan_fact_daily" else 9)
    if strategy == "optimal_price": save_period(provider, NOW, 7, 14)
    prepared = []
    def load(values, **kwargs):
        values = sources.prepare_strategy_rows(values, **kwargs)
        prepared.extend(values)
        return values
    monkeypatch.setattr(ex, "prepare_strategy_rows", load)
    result = ex.preview_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
        sku_rows=[row], organization_id=1, wb_token=provider.token, strategy_id_or_name=strategy).items[0]
    assert result.frontendStrategyId == strategy and result.recommendedPriceKopecks == price
    analytics = prepared[0]["analytics"]
    assert (analytics["periodDays"] if strategy == "plan_fact_daily" else analytics["strategyPeriods"]["main"]["periodDays"]) == 3
    assert not provider.calls


@pytest.mark.parametrize("field,value", [("strategyId", []), ("strategyName", 123), ("config", [])])
def test_preview_request_rejects_invalid_override_shapes(field, value):
    with pytest.raises(HTTPException) as error:
        router._preview_request_from_payload({"articleIds": [ARTICLE], field: value})
    assert error.value.status_code == 422


@pytest.mark.parametrize("strategy", ["schedule", "plan_fact_group"])
def test_configured_assignment_roundtrips_through_normal_api_and_database(isolated, monkeypatch, strategy):
    group = {"groupId": "saved-group", "groupName": "Saved synthetic group", "articleIds": [ARTICLE, "synthetic-102"], "planOrders": 123}
    groups = {group["groupId"]: group}
    monkeypatch.setattr(bff, "REPRICER_SKU_GROUPS", deepcopy(groups))
    with isolated() as session:
        session.get(WbRepricerRuntimeStateRow, 1).state_payload = {"skuGroups": deepcopy(groups)}
        session.commit()
    config = ({"scheduleRules": [
        {"id": "one", "dateFrom": "2026-11-10", "dateTo": "2026-11-12", "timeFrom": "08:30", "timeTo": "09:45", "daysOfWeek": [1, 3], "mode": "delta_pct", "deltaPct": 5.5},
        {"id": "two", "dateFrom": None, "dateTo": None, "timeFrom": "12:00", "timeTo": "13:00", "daysOfWeek": [5], "mode": "hold"},
    ]} if strategy == "schedule" else {"groupId": group["groupId"], "groupName": group["groupName"], "groupArticleIds": group["articleIds"], "planOrders": 123})
    provider = Provider()
    monkeypatch.setattr(router, "_hydrate_org_repricer_state", lambda request: 1)
    monkeypatch.setattr(router, "_request_wb_token", lambda request: None)
    monkeypatch.setattr(router, "_list_repricer_skus_for_request", lambda *args, **kwargs: [healthy_row(provider), healthy_row(provider, 102)])
    request = Request({"type": "http", "headers": []})
    payload = router.StrategyAssignmentBulkRequest(articleIds=[ARTICLE], strategyId=strategy, config=config, executeAfterAssign=False)
    for _ in range(2):
        result = router.post_frontend_strategy_assignments_bulk(request, payload, scenario="complete")
        assert result["items"][0]["config"] == config
        bff.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
        store.hydrate_repricer_bff_state(1, bff)
        assert bff.FRONTEND_STRATEGY_ASSIGNMENTS[ARTICLE]["config"] == config
        assert "synthetic-102" not in bff.FRONTEND_STRATEGY_ASSIGNMENTS
        assert bff.REPRICER_SKU_GROUPS == groups
    assert not provider.calls


@pytest.mark.parametrize("hour", [20, 21, 23])
@pytest.mark.parametrize("bad", [None, "partial", "stale", "wrong_account", "zero", "missing"])
def test_daily_plan_three_completed_moscow_days_and_coverage(monkeypatch, hour, bad):
    provider = Provider()
    now = NOW.replace(hour=hour)
    row = healthy_row(provider)
    row["strategy"].update(id="plan_fact_daily", typedStrategyId=None)
    row["settings"].update(ordersPlanQty=10, ordersPlanDays=1)
    save_stock(provider, now)
    save_period(provider, now, 1, 0)
    patch = {}
    if bad == "partial": patch["coverageState"] = "partial"
    if bad == "stale": patch["fetchedAt"] = (now - timedelta(hours=2)).isoformat()
    if bad == "wrong_account": patch["strategyTokenFingerprint"] = "different-synthetic-account"
    if bad != "missing": save_period(provider, now, 3, 0 if bad == "zero" else 30, **patch)
    queued = []
    monkeypatch.setattr(sources, "queue_strategy_sources", lambda *args: queued.append(args))
    prepared = sources.prepare_strategy_rows([row], org=1, token=provider.token,
        algorithm={"nightMedianEnabled": False}, default_days=30, now=now, queue_missing=True)[0]
    analytics = prepared["analytics"]
    assert analytics["periodDays"] == 3
    end = now.astimezone(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
    assert analytics["periodFrom"] == (end - timedelta(days=2)).isoformat()
    assert analytics["periodTo"] == end.isoformat()
    if bad not in {None, "zero"}:
        assert analytics["ordersUnits"] is None and analytics["basketsState"] == "no_data"
        assert any(request[0] == "baskets" and request[1] == analytics["periodFrom"] and request[2] == analytics["periodTo"]
                   for request in queued[0][2])
    else:
        assert analytics["ordersUnits"] == (0 if bad == "zero" else 30)
        assert not queued
        assert ex._plan_fact_pct(prepared) == (0 if bad == "zero" else 100)
        assert ex._plan_fact_recommended_price(prepared, 100000)[0] == (100000 if bad == "zero" else 172000)


@pytest.mark.parametrize("state", ["manual", "warmup", "automation_off", "derived", "none_source", "missing", "cross_marketplace", "bundles", "illiquid", "invalid", "night_off", "global_off", "eligible", "interval_not_due"])
def test_night_collection_eligibility_and_interval_independence(isolated, monkeypatch, state):
    provider = Provider()
    row = healthy_row(provider)
    at = NOW.replace(hour=20, minute=30)
    monkeypatch.setattr(ex, "_utc_now", lambda: at)
    bff.ALGORITHM_SETTINGS_STATE.update(nightMedianEnabled=state != "global_off", nightMedianAutoEnableAllSkus=False)
    with isolated() as session:
        session.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload = deepcopy(bff.ALGORITHM_SETTINGS_STATE)
        session.commit()
    row["settings"]["nightMedianEnabled"] = state != "night_off"
    row["analytics"]["basketsObservedAt"] = at.isoformat()
    if state in {"manual", "warmup"}: row["meta"]["status"] = state
    if state == "automation_off": row["settings"]["automationEnabled"] = False
    if state == "derived": row["strategy"]["assignmentSource"] = "derived"
    if state == "none_source": row["strategy"]["assignmentSource"] = "none"
    if state == "missing": row["strategy"] = {}
    if state in {"cross_marketplace", "bundles", "illiquid", "invalid"}: row["strategy"]["id"] = state
    if state == "interval_not_due":
        monkeypatch.setattr(ex, "_strategy_interval_not_due", lambda *args: (True, at + timedelta(hours=1), 60))
    expected = int(state in {"eligible", "interval_not_due"})
    # Test the direct collector and the normal entrypoint independently.
    if state != "interval_not_due":
        assert ex.collect_night_median_baskets(1, [row]).get("skuCount", 0) == expected
    report = ex.execute_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
        sku_rows=[row], organization_id=1,
        options=ex.StrategyExecuteOptions(autoApprove=False, applyPrices=False))
    windows = ex._night_median_cache(1).get("windows", {})
    values = [value for window in windows.values() for value in window.get("samples", {}).get(ARTICLE, {}).get("values", [])]
    assert len(values) == expected and not provider.uploads
    if state == "interval_not_due": assert report.items[0].skipReason == "strategy_interval_not_due"


def append(ident, at, *, org=1, article=ARTICLE, trigger="algorithm"):
    store.append_changelog_entry(organization_id=org, entry={"id": ident, "articleId": article,
        "timestamp": at, "trigger": trigger, "oldPriceKopecks": 172000, "newPriceKopecks": 177160,
        "priceConfirmed": True})


def test_sqlite_history_filters_exact_instants_offsets_microseconds_and_paginates():
    append("before", "2026-11-02T08:59:59.999999+00:00")
    append("equal-z", "2026-11-02T09:00:00Z")
    append("equal-offset", "2026-11-02T12:00:00.000000+03:00")
    append("one-us", "2026-11-02T04:00:00.000001-05:00")
    append("fraction", "2026-11-02T09:00:00.1Z")
    append("last-us", "2026-11-02T09:00:00.999999Z")
    append("next-second", "2026-11-02T09:00:01+00:00")
    append("other-tenant", "2026-11-02T09:30:00Z", org=2)
    append("other-sku", "2026-11-02T09:30:00Z", article="synthetic-other")
    append("other-trigger", "2026-11-02T09:30:00Z", trigger="manual")
    params = dict(organization_id=1, article_id=ARTICLE, triggers=["algorithm"], from_date=NOW)
    page = store.list_changelog_page(**params, limit=2)
    assert page["total"] == 6
    assert [item["id"] for item in page["items"]] == ["next-second", "last-us"]
    assert [item["id"] for item in store.list_changelog_page(**params, page=2, limit=2)["items"]] == ["fraction", "one-us"]
    assert [item["id"] for item in store.list_changelog_page(**params, page=3, limit=2)["items"]] == ["equal-offset", "equal-z"]
    params["from_date"] = NOW + timedelta(microseconds=1)
    assert store.list_changelog_page(**params)["total"] == 4
    params["from_date"] = NOW + timedelta(microseconds=999999)
    assert store.list_changelog_page(**params)["total"] == 2
    params["from_date"] = NOW.astimezone(ZoneInfo("Europe/Moscow"))
    assert store.list_changelog_page(**params)["total"] == 6


def test_normal_preview_assignment_confirmed_readback_and_filtered_history(isolated, monkeypatch):
    provider = Provider()
    unrelated = deepcopy(provider.goods[102])
    with isolated() as session:
        session.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload = {**bff.ALGORITHM_SETTINGS_STATE}
        session.commit()
    def rows(*args, **kwargs):
        row = healthy_row(provider)
        row["settings"]["nightMedianEnabled"] = False
        row["analytics"]["strategyPeriods"] = {"main": {"ordersUnits": 9, "periodDays": 3, "state": "ok"},
            "comparison": {"ordersUnits": 14, "periodDays": 7, "state": "ok"}}
        assignment = bff.FRONTEND_STRATEGY_ASSIGNMENTS.get(ARTICLE)
        row["strategy"] = {**row["strategy"], "id": assignment["strategyId"] if assignment else "stockout_guard"}
        row["meta"]["pricingSettingsRevision"] = ledger.pricing_settings_revision(1, ARTICLE)
        return [row]
    monkeypatch.setattr(router, "_hydrate_org_repricer_state", lambda request: 1)
    monkeypatch.setattr(router, "_request_wb_token", lambda request: provider.token)
    monkeypatch.setattr(router, "_build_repricing_client", lambda *args: provider)
    monkeypatch.setattr(router, "_list_repricer_skus_for_request", rows)
    monkeypatch.setattr(router, "actor_from_request", lambda request: SimpleNamespace(actor_id="synthetic", role="admin"))
    async def receive():
        return {"type": "http.request", "body": b'{"articleIds":["synthetic-101"],"strategyId":"optimal_price"}', "more_body": False}
    request = Request({"type": "http", "headers": []}, receive)
    preview = asyncio.run(router.post_preview_repricer_strategies(request))
    assert preview["items"][0]["frontendStrategyId"] == "optimal_price"
    assert preview["items"][0]["recommendedPriceKopecks"] == 177160
    assert not provider.uploads and not bff.FRONTEND_STRATEGY_ASSIGNMENTS
    result = router.post_frontend_strategy_assignments_bulk(request, router.StrategyAssignmentBulkRequest(
        articleIds=[ARTICLE], strategyId="optimal_price", executeAfterAssign=True), scenario="complete")
    assert result["execution"]["items"][0]["priceConfirmed"] is True
    assert len(provider.uploads) == 1 and provider.goods[102] == unrelated
    assert all(round(size["discountedPrice"] * 100) == 177160 for size in provider.goods[101]["sizes"])
    history = router.get_changelog(request, articleId=ARTICLE, trigger=[], from_=(NOW - timedelta(days=30)).isoformat(), page=1, limit=50)
    assert history["total"] == 1 and history["items"][0]["newPriceKopecks"] == 177160
    assert history["items"][0]["priceConfirmed"] is True
    # Optional handoff for the browser integration test: never touches live state.
    import os, json
    artifact = os.environ.get("REPRICER_TEST_HISTORY_ARTIFACT")
    if artifact:
        from pathlib import Path
        Path(artifact).write_text(json.dumps(history))
