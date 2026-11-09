"""New opt-in flag/pairs only; isolated synthetic environment."""

import pytest

from app import config


@pytest.fixture(autouse=True)
def empty_environment(monkeypatch):
    monkeypatch.setattr(config.os, "environ", {})


def test_defaults_do_not_activate_any_account():
    for settings in (config.Settings(), config.get_settings()):
        assert settings.canonical_avito_order_status_enabled is False
        assert settings.canonical_avito_order_status_account_pairs == ()
        assert settings.real_price_apply_enabled is False


def test_explicit_scope_uses_exact_internal_int4_pairs(monkeypatch):
    monkeypatch.setenv("VELLA_CANONICAL_AVITO_ORDER_STATUS_ENABLED", "true")
    monkeypatch.setenv("VELLA_CANONICAL_AVITO_ORDER_STATUS_ACCOUNT_PAIRS", "1:2, 1:2147483647")
    settings = config.get_settings()
    assert settings.canonical_avito_order_status_enabled is True
    assert settings.canonical_avito_order_status_account_pairs == ((1, 2), (1, 2147483647))


@pytest.mark.parametrize("value", ["1", "yes", "", "typo"])
def test_malformed_enable_never_silently_enables_or_disables(monkeypatch, value):
    monkeypatch.setenv("VELLA_CANONICAL_AVITO_ORDER_STATUS_ENABLED", value)
    with pytest.raises(
        RuntimeError, match="^canonical_avito_order_status_configuration_invalid$"
    ):
        config.get_settings()


@pytest.mark.parametrize(
    "value",
    [
        "0:2",
        "1:0",
        "1:2147483648",
        "01:2",
        "1:true",
        "1:external",
        "1:2,1:2",
        "1:2,",
        "1: 2",
        "1:٢",
        "1:2:3",
    ],
)
def test_invalid_pairs_rejected_even_while_disabled(monkeypatch, value):
    monkeypatch.setenv("VELLA_CANONICAL_AVITO_ORDER_STATUS_ACCOUNT_PAIRS", value)
    with pytest.raises(
        RuntimeError, match="^canonical_avito_order_status_configuration_invalid$"
    ) as error:
        config.get_settings()
    assert error.value.__context__ is None


def test_enabled_empty_pairs_still_has_no_implicit_all_accounts(monkeypatch):
    monkeypatch.setenv("VELLA_CANONICAL_AVITO_ORDER_STATUS_ENABLED", "true")
    assert config.get_settings().canonical_avito_order_status_account_pairs == ()
