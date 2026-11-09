"""Night observation completeness: synthetic provider and disposable SQLite only.

Run with -p tests.repricer_offline_plugin.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app import repricer_bff as bff, repricer_execution as ex, repricer_strategy_sources as sources
from app.repricer_cache.store import save_source_cache
from app.repricer_sync import _period_cache_suffix, wb_token_fingerprint
from tests.test_repricer_safety_regressions import ARTICLE, Provider, healthy_row, isolated

NIGHT = datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)


@pytest.fixture
def night(isolated, monkeypatch):
    monkeypatch.setattr(ex, "_utc_now", lambda: NIGHT)
    monkeypatch.setattr(sources, "queue_strategy_sources", lambda *args: None)
    bff.ALGORITHM_SETTINGS_STATE.update(nightMedianEnabled=True, nightMedianCollectEnabled=True,
        nightMedianAutoEnableAllSkus=False)
    provider = Provider()
    row = healthy_row(provider)
    row["analytics"].update(baskets=14, basketsState="ok", basketsObservedAt=NIGHT.isoformat(),
        periodCoverageStates={"basketsState": "ok"})
    return provider, row


def seed(provider, count=14, coverage="ok"):
    first, last = NIGHT.date() - timedelta(days=7), NIGHT.date() - timedelta(days=1)
    save_source_cache(1, "stocks", {"strategyTokenFingerprint": wb_token_fingerprint(provider.token),
        "fetchedAt": NIGHT.isoformat(), "dateTo": NIGHT.date().isoformat(),
        "aggregates": {"101": {"wbStockUnits": 55}}})
    save_source_cache(1, f"baskets_{_period_cache_suffix(7, date_from=first, date_to=last)}", {
        "strategyTokenFingerprint": wb_token_fingerprint(provider.token), "fetchedAt": NIGHT.isoformat(),
        "dateFrom": first.isoformat(), "dateTo": last.isoformat(), "aggregateComplete": True,
        "coverageState": coverage, "aggregates": {"101": {"orderCount": 14, "cartCount": count}}})


def samples():
    return [sample for window in ex._night_median_cache(1).get("windows", {}).values()
        for sample in window.get("samples", {}).get(ARTICLE, {}).get("values", [])]


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("coverage", ["partial", "failed", "no_data", "stale", "unknown"])
def test_normal_execution_never_collects_incomplete_night_inputs(night, monkeypatch, prepared, coverage):
    provider, row = night
    row["analytics"].update(basketsState=coverage, periodCoverageStates={"basketsState": coverage})
    if prepared:
        seed(provider, coverage=coverage)
        monkeypatch.setattr(ex, "prepare_strategy_rows", sources.prepare_strategy_rows)
    ex.execute_repricer_strategies([ARTICLE], client=provider, actor_id="synthetic", actor_role="admin",
        organization_id=1, sku_rows=[row], wb_token=provider.token if prepared else None,
        options=ex.StrategyExecuteOptions(autoApprove=False, applyPrices=False))
    assert samples() == []
    assert not provider.uploads


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("count", [0, 14])
def test_complete_positive_and_zero_observations_survive_collection(night, monkeypatch, prepared, count):
    provider, row = night
    row["analytics"]["baskets"] = count
    if prepared:
        seed(provider, count)
        row = sources.prepare_strategy_rows([row], org=1, token=provider.token,
            algorithm=bff.ALGORITHM_SETTINGS_STATE, default_days=7, now=NIGHT)[0]
        assert row["analytics"]["nightBasketsState"] == "ok"
        # Dedicated Night data must not depend on the ordinary comparison windows.
        row["analytics"].update(basketsState="partial", periodCoverageStates={"basketsState": "partial"})
    assert ex.collect_night_median_baskets(1, [row])["skuCount"] == 1
    assert [sample["baskets"] for sample in samples()] == [count]
    assert ex.collect_night_median_baskets(1, [row])["skuCount"] == 0
    row["analytics"]["nightBasketsObservedAt" if prepared else "basketsObservedAt"] = (NIGHT + timedelta(seconds=1)).isoformat()
    assert ex.collect_night_median_baskets(1, [row])["skuCount"] == 1
    assert [sample["baskets"] for sample in samples()] == [count, count]
    assert not provider.uploads


@pytest.mark.parametrize("patch", [
    {"basketsState": None, "periodCoverageStates": {}},
    {"basketsState": "ok", "periodCoverageStates": {"basketsState": "partial"}},
    {"basketsState": "failed", "periodCoverageStates": {"basketsState": "ok"}},
    {"nightBaskets": 8},
    {"nightBasketsObservedAt": NIGHT.isoformat()},
    {"nightBaskets": 8, "nightBasketsObservedAt": NIGHT.isoformat()},
    {"nightBaskets": 8, "nightBasketsObservedAt": NIGHT.isoformat(), "nightBasketsState": "partial"},
    {"nightBaskets": None, "nightBasketsObservedAt": NIGHT.isoformat(), "nightBasketsState": "ok"},
    {"nightBaskets": 8, "nightBasketsState": "ok"},
    {"nightBaskets": True, "nightBasketsObservedAt": NIGHT.isoformat(), "nightBasketsState": "ok"},
    {"nightBaskets": "8", "nightBasketsObservedAt": NIGHT.isoformat(), "nightBasketsState": "ok"},
    {"nightBaskets": 1.5, "nightBasketsObservedAt": NIGHT.isoformat(), "nightBasketsState": "ok"},
    {"nightBaskets": -1, "nightBasketsObservedAt": NIGHT.isoformat(), "nightBasketsState": "ok"},
    {"nightBaskets": 8, "nightBasketsObservedAt": "invalid", "nightBasketsState": "ok"},
])
def test_invalid_or_mixed_observations_never_change_existing_samples(night, patch):
    _, row = night
    assert ex.collect_night_median_baskets(1, [row])["skuCount"] == 1
    before = deepcopy(ex._night_median_cache(1))
    row["analytics"].update(patch)
    assert ex.collect_night_median_baskets(1, [row]).get("skuCount", 0) == 0
    assert ex._night_median_cache(1) == before


@pytest.mark.parametrize("generic_state", [None, "partial", "failed"])
def test_complete_preloaded_dedicated_source_ignores_unrelated_generic_coverage(night, generic_state):
    _, row = night
    row["analytics"].update(nightBaskets=8, nightBasketsState="complete", nightBasketsObservedAt=NIGHT.isoformat(),
        basketsState=generic_state, periodCoverageStates={"basketsState": generic_state})
    assert ex.collect_night_median_baskets(1, [row])["skuCount"] == 1
    assert [sample["baskets"] for sample in samples()] == [8]


@pytest.mark.parametrize("dedicated", [False, True])
@pytest.mark.parametrize("offset,accepted", [(-3601, False), (-3600, True), (30, True), (31, False)])
def test_observation_freshness_limits_are_preserved(night, dedicated, offset, accepted):
    _, row = night
    at = (NIGHT + timedelta(seconds=offset)).isoformat()
    if dedicated:
        row["analytics"].update(nightBaskets=0, nightBasketsState="complete", nightBasketsObservedAt=at)
    else:
        row["analytics"]["basketsObservedAt"] = at
    assert ex._night_observation_fresh(row) is accepted


@pytest.mark.parametrize("dedicated", [False, True])
def test_current_night_comparison_uses_the_same_completeness_guard(night, monkeypatch, dedicated):
    _, row = night
    assert ex.collect_night_median_baskets(1, [row])["skuCount"] == 1
    morning = datetime(2026, 11, 3, 3, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(ex, "_utc_now", lambda: morning)
    if dedicated:
        row["analytics"].update(nightBaskets=0, nightBasketsObservedAt=morning.isoformat(), nightBasketsState="ok")
    else:
        row["analytics"].update(baskets=0, basketsObservedAt=morning.isoformat())
    assert ex._night_median_apply_payload(row, 1)["currentBaskets"] == 0
    row["analytics"]["nightBasketsState" if dedicated else "basketsState"] = "partial"
    assert ex._night_median_apply_payload(row, 1) is None
