"""Rollout configuration must fail closed on malformed entries."""
import pytest

from app.config import get_settings


def test_default_configuration_is_disabled_and_empty(monkeypatch):
    monkeypatch.delenv("VELLA_CANONICAL_NOTIFICATIONS_ENABLED", raising=False)
    monkeypatch.delenv("VELLA_CANONICAL_NOTIFICATION_ACCOUNTS", raising=False)
    settings = get_settings()
    assert settings.canonical_notifications_enabled is False
    assert settings.canonical_notification_accounts == ()


def test_configuration_retains_exact_provider_scopes(monkeypatch):
    monkeypatch.setenv("VELLA_CANONICAL_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("VELLA_CANONICAL_NOTIFICATION_ACCOUNTS", "1:2:wb,1:2:avito,2147483647:3:wb")
    settings = get_settings()
    assert settings.canonical_notifications_enabled is True
    assert settings.canonical_notification_accounts == ((1, 2, "wb"), (1, 2, "avito"), (2147483647, 3, "wb"))


@pytest.mark.parametrize("value", ["1", "", "yes", "truthy"])
def test_invalid_enable_value_is_not_silently_disabled(monkeypatch, value):
    monkeypatch.setenv("VELLA_CANONICAL_NOTIFICATIONS_ENABLED", value)
    with pytest.raises(RuntimeError, match="^canonical_notifications_configuration_invalid$"):
        get_settings()


@pytest.mark.parametrize("value", ["1:2:wb,", "1:2:wb,1:2:wb", "0:2:wb", "01:2:wb", "1:2147483648:wb",
                                       "1:2:WB", "1:2", "1:2:wb:3", "-1:2:wb", "1:2:other", "1: 2:wb"])
def test_invalid_account_entry_rejects_whole_configuration(monkeypatch, value):
    monkeypatch.setenv("VELLA_CANONICAL_NOTIFICATION_ACCOUNTS", value)
    with pytest.raises(RuntimeError, match="^canonical_notifications_configuration_invalid$"):
        get_settings()
