from fastapi.testclient import TestClient

from app.account_health import orm as _account_health_models  # noqa: F401
from app.cabinet import orm as _cabinet_models  # noqa: F401
from app.reviews import orm as _review_models  # noqa: F401
from app.wb_ads_cache import orm as _wb_ads_cache_models  # noqa: F401
from app.config import get_settings
from app.infra.celery_app import get_celery_app
from app.infra.models import Base
from app.main import create_app


def test_infra_settings_defaults_are_present():
    settings = get_settings()

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")
    assert settings.celery_broker_url.startswith("redis://")
    assert settings.celery_result_backend.startswith("redis://")


def test_advertising_shadow_settings_parse_canary_organizations(monkeypatch):
    monkeypatch.setenv("VELLA_ADVERTISING_SHADOW_INGEST_ENABLED", "true")
    monkeypatch.setenv("VELLA_ADVERTISING_SHADOW_INGEST_ORGANIZATION_IDS", "2, 7")

    settings = get_settings()

    assert settings.advertising_shadow_ingest_enabled is True
    assert settings.advertising_shadow_ingest_organization_ids == (2, 7)


def test_health_exposes_infrastructure_config_flags():
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    infra = response.json()["infrastructure"]
    assert infra["databaseConfigured"] is True
    assert infra["redisConfigured"] is True
    assert infra["celeryBrokerConfigured"] is True
    assert infra["celeryResultBackendConfigured"] is True


def test_celery_app_is_configured_from_settings(monkeypatch):
    monkeypatch.setenv("VELLA_REPRICER_SCHEDULER_ENABLED", "true")
    get_celery_app.cache_clear()
    try:
        settings = get_settings()
        celery_app = get_celery_app()

        assert celery_app.conf.broker_url == settings.celery_broker_url
        assert celery_app.conf.result_backend == settings.celery_result_backend
        assert "repricer-execute-assigned" in celery_app.conf.beat_schedule
        assert "repricer-execute-avito" in celery_app.conf.beat_schedule
        assert (
            celery_app.conf.beat_schedule["repricer-execute-avito"]["task"]
            == "repricer.execute_avito_all_orgs"
        )
        assert "repricer-sync-wb-nightly" in celery_app.conf.beat_schedule
        assert (
            celery_app.conf.beat_schedule["repricer-sync-wb-nightly"]["task"]
            == "repricer.sync_wb_nightly_all_orgs"
        )
    finally:
        get_celery_app.cache_clear()


def test_infra_metadata_contains_baseline_table():
    assert "infra_runtime_state" in Base.metadata.tables
    assert "wb_accounts" in Base.metadata.tables
    assert "wb_account_capabilities" in Base.metadata.tables
    assert "wb_account_missing_scopes" in Base.metadata.tables
    assert "wb_token_health_checks" in Base.metadata.tables
    assert "lk_organizations" in Base.metadata.tables
    assert "lk_users" in Base.metadata.tables
    assert "lk_user_permissions" in Base.metadata.tables
    assert "lk_sessions" in Base.metadata.tables
    assert "lk_integrations" in Base.metadata.tables
    assert "lk_audit_events" in Base.metadata.tables
    assert "lk_user_preferences" in Base.metadata.tables
    assert "wb_ads_report_cache" in Base.metadata.tables
    assert "wb_ads_campaign_status_snapshots" in Base.metadata.tables
    assert "wb_ads_campaign_budget_snapshots" in Base.metadata.tables
    assert "wb_ads_fullstats_daily_rows" in Base.metadata.tables
    assert "wb_ads_spend_documents" in Base.metadata.tables


def test_infra_metadata_contains_wb_reviews_tables():
    assert "rv_review_templates" in Base.metadata.tables
    assert "rv_review_stop_topics" in Base.metadata.tables
    assert "rv_review_moderation_rules" in Base.metadata.tables
    assert "rv_review_feedbacks" in Base.metadata.tables
    assert "rv_review_drafts" in Base.metadata.tables
    assert "rv_review_send_jobs" in Base.metadata.tables
    assert "rv_review_prompt_traces" in Base.metadata.tables
    assert "rv_review_sync_settings" in Base.metadata.tables
    assert "rv_review_sync_runs" in Base.metadata.tables
