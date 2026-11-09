from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import repricer_bff
from app.routers import wb_repricer_bff as router


@pytest.mark.parametrize("enabled,auto_all,eligible", [(True, False, 2), (True, True, 3), (False, True, 0)])
def test_worker_status_reads_assignments_without_rebuilding_finance(monkeypatch, enabled, auto_all, eligible):
    assignments = {
        "A": {"strategyId": "turnover_control", "source": "group"},
        "B": {"strategyId": "night_price_mode", "source": "manual"},
        "C": {"strategyId": "illiquid", "source": "manual"},
        "D": {"strategyId": "baskets_orders", "source": "derived"},
        "E": {"strategyId": "metric_dynamics", "source": "manual"},
        "removed": {"strategyId": "night_price_mode", "source": "manual"},
    }
    monkeypatch.setattr(repricer_bff, "FRONTEND_STRATEGY_ASSIGNMENTS", assignments)
    monkeypatch.setattr(repricer_bff, "SKU_SETTINGS_OVERRIDES", {
        "A": {"nightMedianEnabled": False}, "B": {"nightMedianEnabled": False},
        "C": {"nightMedianEnabled": True}, "E": {"nightMedianEnabled": True},
    })
    monkeypatch.setattr(repricer_bff, "ALGORITHM_SETTINGS_STATE", {
        "nightMedianEnabled": enabled, "nightMedianAutoEnableAllSkus": auto_all,
    })
    monkeypatch.setattr(router, "actor_from_request", lambda _: SimpleNamespace(organization_id=1, user_id=1))
    monkeypatch.setattr(router, "hydrate_repricer_bff_state", lambda *_: None)
    monkeypatch.setattr(router, "_apply_costs_excel_cache_to_sku_settings", lambda *_: None)
    monkeypatch.setattr(router, "get_user_wb_token_secret", lambda *_: None)
    monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(
        wb_api_mode="real", real_price_apply_enabled=False, repricer_scheduler_enabled=False,
    ))
    monkeypatch.setattr(router, "list_cached_goods", lambda _: [{"vendorCode": key} for key in "ABCDEF"])
    monkeypatch.setattr(router, "list_execution_runs", lambda **_: [])
    monkeypatch.setattr(router, "list_pending_price_approvals", lambda **_: [])
    monkeypatch.setattr(router, "get_wb_sync_status", lambda _: {})
    monkeypatch.setattr(router, "list_wb_sync_history", lambda *_, **__: [])

    reads = []
    def source_cache(_organization_id, key, **_):
        reads.append(key)
        assert key == "night_median_baskets"
        return {}
    monkeypatch.setattr(router, "get_source_cache", source_cache)

    expensive_calls = []
    def rebuild(*_, **__):
        expensive_calls.append(True)
        raise AssertionError("Worker status must not read financial reports or build priced SKU rows")
    monkeypatch.setattr(router, "_list_repricer_skus_for_request", rebuild)

    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})
    payload = router.get_repricer_worker_status(request, limit=8)
    night = payload["nightMedian"]
    assert expensive_calls == []
    assert reads == ["night_median_baskets"]
    assert night["activeStrategySkuCount"] == 3
    assert night["manualNightSkuCount"] == 1
    assert night["individualEnabledSkuCount"] == 2
    assert night["eligibleSkuCount"] == eligible
    assert {row["articleId"] for row in night["eligibleItems"]} == (
        ({"A", "B", "E"} if auto_all else {"B", "E"}) if enabled else set()
    )
