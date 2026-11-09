"""Default-off metadata preview; authorization precedes all live dependencies."""

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
    settings.canonical_avito_reviews_preview_enabled = enabled
    settings.canonical_avito_reviews_preview_account_pairs = ()
    settings.wb_live_sync_enabled = False
    settings.canonical_account_discovery_enabled = False
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return main.create_app()


@pytest.mark.parametrize("enabled", [False, True])
def test_own_flag_controls_reviews_preview_route(monkeypatch, enabled):
    paths = [route.path for route in iter_route_contexts(configured(monkeypatch, enabled).routes)]
    assert paths.count("/api/v2/avito/accounts/{account_id}/reviews/preview") == (1 if enabled else 0)


def test_authentication_precedes_reviews_preview_dependencies(monkeypatch):
    with TestClient(configured(monkeypatch, True)) as client:
        response = client.get("/api/v2/avito/accounts/42/reviews/preview?offset=0")
    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "AVITO_REVIEWS_PREVIEW_ACCESS_DENIED"}}
    assert response.headers["cache-control"] == "no-store"


def test_empty_allowlist_denies_before_keys_database_provider(monkeypatch):
    from app import avito_reviews_preview_bootstrap as bootstrap
    from app.avito.account_reviews import AccountReviewsError

    settings = SimpleNamespace(canonical_avito_reviews_preview_enabled=True,
        canonical_avito_reviews_preview_account_pairs=())
    engine = create_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "get_engine", lambda: engine)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Disabled account reached keys or provider")

    monkeypatch.setattr(bootstrap, "_load_keyring", forbidden)
    monkeypatch.setattr(bootstrap, "BoundedAvitoReviewsClient", forbidden)
    actor = ActorContext("user", "user", 7, "synthetic", frozenset(), session_id="session")
    try:
        with pytest.raises(AccountReviewsError, match="AVITO_REVIEWS_PREVIEW_DISABLED"):
            bootstrap.avito_reviews_preview_service().preview(actor, marketplace_account_id=42, offset=0)
    finally:
        engine.dispose()


def test_exact_pair_and_runtime_disable(monkeypatch):
    from app import avito_reviews_preview_bootstrap as bootstrap
    from app.avito.account_reviews import AccountReviewsError

    settings = SimpleNamespace(canonical_avito_reviews_preview_enabled=True,
        canonical_avito_reviews_preview_account_pairs=((7, 42),))
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    assert bootstrap.enabled_for(7, 42) is True
    assert bootstrap.enabled_for(8, 42) is False
    assert bootstrap.enabled_for(7, 43) is False
    assert bootstrap.enabled_for(True, 42) is False
    settings.canonical_avito_reviews_preview_enabled = False
    assert bootstrap.enabled_for(7, 42) is False
    with pytest.raises(AccountReviewsError, match="AVITO_REVIEWS_PREVIEW_DISABLED"):
        bootstrap.avito_reviews_preview_service()
