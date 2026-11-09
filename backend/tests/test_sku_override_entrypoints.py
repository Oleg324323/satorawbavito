"""Registration is not permission to turn off the legacy writer fence."""

from dataclasses import replace

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


@pytest.mark.parametrize("enabled", [False, True])
def test_override_routes_are_default_off(monkeypatch, enabled):
    settings = replace(Settings(), wb_sku_overrides_enabled=enabled)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    paths = [route.path for route in iter_route_contexts(main.create_app().routes)]
    root = "/api/v2/wb/repricing/accounts/{account_id}/skus/{catalog_sku_id}/overrides"
    assert paths.count(root) == (2 if enabled else 0)
    assert paths.count(root + "/history") == (1 if enabled else 0)


def test_enabled_override_read_requires_login_before_storage(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: replace(Settings(), wb_sku_overrides_enabled=True))
    with TestClient(main.create_app()) as client:
        result = client.get("/api/v2/wb/repricing/accounts/1/skus/2/overrides")
    assert result.status_code == 401


def test_rollout_is_exact_and_legacy_writer_fence_cannot_be_enabled(monkeypatch):
    from app import sku_override_bootstrap as bootstrap

    settings = replace(Settings(), wb_sku_overrides_enabled=True, wb_sku_override_account_pairs=((1, 2),))
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    assert bootstrap.enabled_for(1, 2) is True
    assert bootstrap.enabled_for(1, 3) is False
    assert bootstrap.enabled_for(2, 2) is False
    assert bootstrap.writer_fenced_for(1, 2) is False
    monkeypatch.setattr(bootstrap, "get_settings", lambda: replace(settings, wb_sku_overrides_enabled=False))
    assert bootstrap.enabled_for(1, 2) is False
