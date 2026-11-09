"""Integration-owned registration guards; no provider or infrastructure calls."""

from dataclasses import replace

import pytest

from app.config import Settings
from app.infra import celery_app as worker


@pytest.mark.parametrize("enabled", [False, True])
def test_live_routes_only_mounted_in_explicit_mode(monkeypatch, enabled):
    from fastapi.routing import iter_route_contexts

    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: replace(Settings(), wb_live_sync_enabled=enabled))
    application = main.create_app()
    paths = [route.path for route in iter_route_contexts(application.routes)]
    # Route placeholder spelling is not material; method/path registration is.
    products = [path for path in paths if path.startswith("/api/v2/wb/accounts/") and path.endswith("/products")]
    connects = [path for path in paths if path == "/api/v1/cabinet/marketplace-accounts/connect/wb"]
    syncs = [path for path in paths if path.startswith("/api/v2/wb/accounts/") and path.endswith("/sync")]
    assert len(products) == (1 if enabled else 0)
    assert len(connects) == (1 if enabled else 0)
    assert len(syncs) == (2 if enabled else 0)


@pytest.mark.parametrize("enabled", [False, True])
def test_live_dispatch_has_one_dedicated_read_only_schedule(monkeypatch, enabled):
    settings = replace(
        Settings(),
        wb_live_sync_enabled=enabled,
        canonical_shadow_collection_enabled=False,
        repricer_wb_sync_enabled=False,
        repricer_scheduler_enabled=False,
        avito_returns_sync_enabled=False,
    )
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "install_process_heartbeat", lambda *args: None)
    app = worker.get_celery_app.__wrapped__()
    try:
        schedule = app.conf.beat_schedule
        assert set(schedule) == ({"wb-live-dispatch-pending"} if enabled else set())
        if enabled:
            tick = schedule["wb-live-dispatch-pending"]
            assert tick["task"] == "wb_live.dispatch_pending"
            assert tick["options"] == {"queue": "vella.wb-live"}
            assert tick["schedule"].total_seconds() == 30
        for task in ("wb_live.dispatch_pending", "wb_live.run_batch"):
            assert app.conf.task_routes[task] == {"queue": "vella.wb-live"}
        assert app.conf.accept_content == ["json"]
    finally:
        app.close()
