"""SKU reads are admitted only by strict explicit configuration and exact pairs."""
from dataclasses import replace

import pytest

from app import config


def gate(**kwargs):
    fn = getattr(config, "is_wb_sku_overrides_enabled", None)
    assert callable(fn), "missing exact SKU override gate"
    return fn(**kwargs)


def test_unconfigured_sku_override_reads_are_disabled(monkeypatch):
    monkeypatch.delenv("VELLA_WB_SKU_OVERRIDES_ENABLED", raising=False)
    monkeypatch.delenv("VELLA_WB_SKU_OVERRIDE_ACCOUNT_PAIRS", raising=False)
    assert gate(organization_id=1, marketplace_account_id=2) is False


def test_only_exact_enabled_account_pair_passes(monkeypatch):
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDES_ENABLED", "true")
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDE_ACCOUNT_PAIRS", "1:2,2147483647:3")
    settings = config.get_settings()
    assert gate(organization_id=1, marketplace_account_id=2, settings=settings) is True
    assert gate(organization_id=1, marketplace_account_id=3, settings=settings) is False
    assert gate(organization_id=2, marketplace_account_id=2, settings=settings) is False
    assert gate(organization_id=2147483647, marketplace_account_id=3, settings=settings) is True


@pytest.mark.parametrize("value", ["1", "", "yes", "falsey"])
def test_malformed_rollout_flag_is_configuration_error(monkeypatch, value):
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDES_ENABLED", value)
    with pytest.raises(RuntimeError, match="^wb_sku_overrides_configuration_invalid$"):
        config.get_settings()


@pytest.mark.parametrize("value", ["1:2,", "1:2,1:2", "01:2", "0:2", "1:2147483648", "1: 2", "1:2:wb", "-1:2"])
def test_malformed_pair_prevents_partial_activation(monkeypatch, value):
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDE_ACCOUNT_PAIRS", value)
    with pytest.raises(RuntimeError, match="^wb_sku_overrides_configuration_invalid$"):
        config.get_settings()


@pytest.mark.parametrize("org,account", [(True, 2), (1, False), ("1", 2), (1, 0), (1, 2**31)])
def test_gate_does_not_coerce_invalid_identity(org, account, monkeypatch):
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDES_ENABLED", "true")
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDE_ACCOUNT_PAIRS", "1:2")
    assert gate(organization_id=org, marketplace_account_id=account) is False


def test_notification_flag_cannot_activate_sku_reads(monkeypatch):
    monkeypatch.setenv("VELLA_CANONICAL_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("VELLA_CANONICAL_NOTIFICATION_ACCOUNTS", "1:2:wb")
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDE_ACCOUNT_PAIRS", "1:2")
    assert gate(organization_id=1, marketplace_account_id=2) is False


@pytest.mark.parametrize("changes", [{"wb_sku_overrides_enabled": 1}, {"wb_sku_override_account_pairs": [(1, 2)]},
    {"wb_sku_override_account_pairs": ((1, True),)}, {"wb_sku_override_account_pairs": ((1, 2), (1, 2))}])
def test_typed_settings_cannot_bypass_strict_gate_validation(changes):
    assert hasattr(config.Settings(), "wb_sku_overrides_enabled"), "missing typed rollout settings"
    settings = replace(config.Settings(), **changes)
    with pytest.raises(RuntimeError, match="^wb_sku_overrides_configuration_invalid$"):
        gate(organization_id=1, marketplace_account_id=2, settings=settings)


def test_account_discovery_defaults_off_and_is_independent(monkeypatch):
    monkeypatch.delenv("VELLA_CANONICAL_ACCOUNT_DISCOVERY_ENABLED", raising=False)
    monkeypatch.setenv("VELLA_WB_SKU_OVERRIDES_ENABLED", "true")
    monkeypatch.setenv("VELLA_CANONICAL_NOTIFICATIONS_ENABLED", "true")
    assert config.get_settings().canonical_account_discovery_enabled is False


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), (" TRUE ", True)])
def test_account_discovery_strict_boolean_values(monkeypatch, raw, expected):
    monkeypatch.setenv("VELLA_CANONICAL_ACCOUNT_DISCOVERY_ENABLED", raw)
    assert config.get_settings().canonical_account_discovery_enabled is expected


@pytest.mark.parametrize("raw", ["1", "yes", "", "private-malformed-value"])
def test_account_discovery_invalid_flag_is_safe_configuration_error(monkeypatch, raw):
    monkeypatch.setenv("VELLA_CANONICAL_ACCOUNT_DISCOVERY_ENABLED", raw)
    with pytest.raises(RuntimeError, match="^canonical_account_discovery_configuration_invalid$"):
        config.get_settings()
