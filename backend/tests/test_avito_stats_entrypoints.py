"""Default-off encrypted-account route; no real provider or database access."""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import main
from app.config import Settings
from app.control_plane.auth import ActorContext


def configured(monkeypatch, enabled):
    settings = SimpleNamespace(**vars(Settings()))
    settings.canonical_avito_stats_enabled = enabled
    settings.canonical_avito_stats_account_pairs = ()
    settings.wb_live_sync_enabled = False
    settings.canonical_account_discovery_enabled = False
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return main.create_app()


@pytest.mark.parametrize("enabled", [False, True])
def test_avito_statistics_own_flag_controls_registration(monkeypatch, enabled):
    paths = [route.path for route in iter_route_contexts(configured(monkeypatch, enabled).routes)]
    assert paths.count("/api/v2/avito/accounts/{account_id}/statistics") == (1 if enabled else 0)


def test_avito_statistics_authentication_precedes_dependencies(monkeypatch):
    with TestClient(configured(monkeypatch, True)) as client:
        response = client.get("/api/v2/avito/accounts/42/statistics?dateFrom=2026-09-01&dateTo=2026-09-02")
    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "AVITO_ACCOUNT_STATS_ACCESS_DENIED"}}
    assert response.headers["cache-control"] == "no-store"


def test_empty_allowlist_denies_before_keys_database_and_provider(monkeypatch):
    from app import avito_stats_bootstrap as bootstrap
    from app.avito.account_stats import AccountStatsError

    settings = SimpleNamespace(canonical_avito_stats_enabled=True, canonical_avito_stats_account_pairs=())
    engine = create_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "get_engine", lambda: engine)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Disabled account reached a credential or provider dependency")

    monkeypatch.setattr(bootstrap, "_load_keyring", forbidden)
    monkeypatch.setattr(bootstrap, "BoundedAvitoTotalsClient", forbidden)
    actor = ActorContext("user", "user", 7, "synthetic", frozenset(), session_id="session")
    try:
        with pytest.raises(AccountStatsError, match="AVITO_ACCOUNT_STATS_DISABLED"):
            bootstrap.avito_stats_service().fetch(actor, marketplace_account_id=42,
                date_from=date(2026, 9, 1), date_to=date(2026, 9, 2))
    finally:
        engine.dispose()


def test_rollout_exact_pair_and_runtime_disable(monkeypatch):
    from app import avito_stats_bootstrap as bootstrap
    from app.avito.account_stats import AccountStatsError

    settings = SimpleNamespace(canonical_avito_stats_enabled=True, canonical_avito_stats_account_pairs=((7, 42),))
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    assert bootstrap.enabled_for(7, 42) is True
    assert bootstrap.enabled_for(8, 42) is False
    assert bootstrap.enabled_for(7, 43) is False
    assert bootstrap.enabled_for(True, 42) is False
    settings.canonical_avito_stats_enabled = False
    assert bootstrap.enabled_for(7, 42) is False
    with pytest.raises(AccountStatsError, match="AVITO_ACCOUNT_STATS_DISABLED"):
        bootstrap.avito_stats_service()
