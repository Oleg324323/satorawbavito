from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Literal

from app.control_plane.schemas import RepricerTypedSettings
from app.control_plane.store import list_settings_versions


StrategyMode = Literal["manual", "strategy_4599", "strategy_4600"]
_ALGORITHM_CONTEXT = ContextVar("repricer_execution_algorithm", default=None)


def current_algorithm_settings(fallback=None):
    current = _ALGORITHM_CONTEXT.get()
    if current is not None:
        return current[1]
    if fallback is not None:
        return fallback
    from app import repricer_bff
    return repricer_bff.ALGORITHM_SETTINGS_STATE


def current_algorithm_organization_id():
    current = _ALGORITHM_CONTEXT.get()
    return current[0] if current is not None else None


@contextmanager
def organization_algorithm_context(organization_id, settings_payload=None):
    existing = _ALGORITHM_CONTEXT.get()
    if existing is not None and organization_id is not None and existing[0] != organization_id:
        raise RuntimeError("REPRICER_ORGANIZATION_CONTEXT_MISMATCH")
    if organization_id is None or existing is not None:
        yield
        return
    from app import repricer_dispatch_store as ledger
    from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow
    from app.repricer_bff import DEFAULT_ALGORITHM_SETTINGS
    if settings_payload is None:
        with ledger.transaction(org=organization_id, write=False) as session:
            row = session.get(WbRepricerAlgorithmSettingsRow, organization_id)
            settings_payload = row.settings_payload if row else {}
    settings = {**DEFAULT_ALGORITHM_SETTINGS, **settings_payload}
    token = _ALGORITHM_CONTEXT.set((organization_id, settings))
    try:
        yield
    finally:
        _ALGORITHM_CONTEXT.reset(token)


def with_organization_algorithm(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        with organization_algorithm_context(kwargs.get("organization_id")):
            return function(*args, **kwargs)
    return scoped


@dataclass(frozen=True)
class RepricerGuardLimits:
    per_update_step_pct: float = 6.0
    per_day_step_pct: float = 20.0
    pmin_guard_enabled: bool = True
    pmax_guard_enabled: bool = True
    spp_fallback_mode: str = "block"
    strategy_mode: StrategyMode = "manual"
    night_mode_enabled: bool = False
    night_window_start_hour: int = 23
    night_window_end_hour: int = 6


@dataclass(frozen=True)
class RepricerSppAccountingSettings:
    mode: str = "spp_only"
    wb_wallet_pct: float | None = None


def get_active_repricer_settings() -> RepricerTypedSettings | None:
    versions = list_settings_versions(status="active")
    if not versions:
        return None
    return versions[0].settings


def get_repricer_guard_limits() -> RepricerGuardLimits:
    settings = get_active_repricer_settings()
    if settings is None:
        limits = RepricerGuardLimits()
    else:
        limits = RepricerGuardLimits(
            per_update_step_pct=float(settings.maxPriceStepPctPerHour),
            per_day_step_pct=min(20.0, float(settings.maxPriceStepPctPerHour) * 4),
            pmin_guard_enabled=bool(settings.pminGuardEnabled),
            pmax_guard_enabled=bool(settings.pmaxGuardEnabled),
            spp_fallback_mode=str(settings.sppFallbackMode),
            strategy_mode=settings.strategyMode,
            night_mode_enabled=bool(settings.nightModeEnabled),
            night_window_start_hour=int(settings.nightWindowStartHour),
            night_window_end_hour=int(settings.nightWindowEndHour),
        )

    try:
        from app import repricer_bff

        algorithm_settings = current_algorithm_settings()
        limits = replace(
            limits,
            per_update_step_pct=float(algorithm_settings.get("priceStepPct", limits.per_update_step_pct)),
            per_day_step_pct=float(algorithm_settings.get("maxPriceChangeDailyPct", limits.per_day_step_pct)),
            pmin_guard_enabled=bool(algorithm_settings.get("minPriceSyncEnabled", limits.pmin_guard_enabled)),
            night_mode_enabled=bool(algorithm_settings.get("nightMedianEnabled", limits.night_mode_enabled)),
            night_window_start_hour=int(algorithm_settings.get("nightMedianWindowStartHour", limits.night_window_start_hour)),
            night_window_end_hour=int(algorithm_settings.get("nightMedianWindowEndHour", limits.night_window_end_hour)),
        )
    except Exception:
        pass
    return limits


def get_repricer_spp_accounting_settings() -> RepricerSppAccountingSettings:
    try:
        from app import repricer_bff

        algorithm_settings = current_algorithm_settings()
        mode = str(algorithm_settings.get("sppAccountingMode") or "spp_only")
        if mode not in {"spp_only", "spp_plus_wallet"}:
            mode = "spp_only"
        raw_wallet = algorithm_settings.get("wbWalletType")
        try:
            parsed_wallet = float(raw_wallet) if raw_wallet is not None else 0.0
        except (TypeError, ValueError):
            parsed_wallet = 0.0
        wallet_pct = parsed_wallet if 0 < parsed_wallet <= 100 else None
        return RepricerSppAccountingSettings(mode=mode, wb_wallet_pct=wallet_pct)
    except Exception:
        return RepricerSppAccountingSettings()


def is_auto_strategy_execution_allowed(*, force: bool = False) -> bool:
    if force:
        return True
    settings = get_active_repricer_settings()
    if settings is None:
        return True
    return settings.strategyMode != "manual"


def default_typed_strategy_for_mode() -> str | None:
    limits = get_repricer_guard_limits()
    if limits.strategy_mode == "strategy_4599":
        return "baskets_orders_4599"
    if limits.strategy_mode == "strategy_4600":
        return "revenue_dynamics_4600"
    return None
