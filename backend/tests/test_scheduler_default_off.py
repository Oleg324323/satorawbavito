from __future__ import annotations

import pytest

from app.config import Settings, get_settings
from app.infra.celery_app import get_celery_app


@pytest.fixture(autouse=True)
def _isolated_celery_factory(monkeypatch):
    monkeypatch.setenv("VELLA_CELERY_BROKER_URL", "memory://")
    monkeypatch.setenv("VELLA_CELERY_RESULT_BACKEND", "cache+memory://")
    get_celery_app.cache_clear()
    try:
        yield
    finally:
        get_celery_app.cache_clear()


def test_settings_dataclass_defaults_scheduler_off():
    assert Settings().repricer_scheduler_enabled is False


def test_get_settings_defaults_scheduler_off_without_disabling_legacy_collectors(monkeypatch):
    monkeypatch.delenv("VELLA_REPRICER_SCHEDULER_ENABLED", raising=False)

    settings = get_settings()

    assert settings.repricer_scheduler_enabled is False
    assert settings.repricer_wb_sync_enabled is True
    assert settings.avito_returns_sync_enabled is True


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    (("true", True), ("false", False)),
)
def test_get_settings_preserves_explicit_scheduler_override(monkeypatch, raw_value, expected):
    monkeypatch.setenv("VELLA_REPRICER_SCHEDULER_ENABLED", raw_value)

    assert get_settings().repricer_scheduler_enabled is expected


def test_celery_omits_scheduler_driven_jobs_by_default(monkeypatch):
    monkeypatch.delenv("VELLA_REPRICER_SCHEDULER_ENABLED", raising=False)

    schedule = get_celery_app().conf.beat_schedule

    assert "repricer-execute-assigned" not in schedule
    assert "repricer-execute-avito" not in schedule
    assert "reviews-sync-feedbacks" not in schedule
    assert "repricer-sync-wb-nightly" in schedule
    assert "avito-sync-returns" in schedule


def test_celery_registers_scheduler_driven_jobs_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("VELLA_REPRICER_SCHEDULER_ENABLED", "true")

    schedule = get_celery_app().conf.beat_schedule

    assert schedule["repricer-execute-assigned"]["task"] == (
        "repricer.execute_assigned_all_orgs"
    )
    assert schedule["repricer-execute-avito"]["task"] == "repricer.execute_avito_all_orgs"
    assert schedule["reviews-sync-feedbacks"]["task"] == "reviews.sync_all_orgs"
