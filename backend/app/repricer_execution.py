from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from statistics import median
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.config import get_settings
from app.modules.wb_repricing_calculation import settings_minimum_price_kopecks
from app.repricer_cache.store import get_source_cache, save_source_cache
from app.repricer_persistence.store import append_execution_run, upsert_pending_price_approvals
from app.repricer_settings import (
    default_typed_strategy_for_mode,
    get_repricer_guard_limits,
    is_auto_strategy_execution_allowed,
    current_algorithm_settings,
    with_organization_algorithm,
)
from app.repricer_bff import (
    ALGORITHM_SETTINGS_STATE,
    FRONTEND_STRATEGY_BY_ID,
    LIQUIDATION_ACTIVE,
    apply_frontend_strategy_assignment,
    _effective_pmin,
    list_repricer_changelog,
    list_repricer_skus,
    record_repricer_price_change,
)
from app.repricer_sprint_b import (
    ApplyDraftRequest,
    ApproveDraftRequest,
    DraftEconomicsInput,
    PriceDraftCreateRequest,
    PriceRecommendationRequest,
    apply_approved_draft,
    approve_draft,
    build_price_recommendation,
    create_price_draft,
)
from app.repricer_sprint_c import (
    RevenueMode,
    StrategyDryRunItem,
    StrategyId,
    run_strategy_item_with_guards,
)
from app.wb_api.client import WbApiClient
from app.repricer_price_safety import binding_key, utc_timestamp, checksum
from app.repricer_dispatch_store import mutate_night_cache, DispatchPersistenceError
from app.repricer_strategy_sources import prepare_strategy_rows


ExecutionSkipReason = Literal[
    "manual_mode",
    "warmup",
    "automation_disabled",
    "no_nm_id",
    "no_strategy",
    "no_price_change",
    "liquidation_not_due",
    "night_median_collecting",
    "night_mode_disabled",
    "strategy_interval_not_due",
    "sku_not_found",
]

PRESET_CONFIG_OVERRIDES: dict[str, dict[str, Any]] = {
    "baskets_orders": {"capPct": 3.0},
    "metric_dynamics": {"mode": "percent_rub_floor", "maxStepPct": 5.0},
}

NIGHT_MEDIAN_SOURCE_KEY = "night_median_baskets"


def _algorithm_settings():
    return current_algorithm_settings(ALGORITHM_SETTINGS_STATE)

REVENUE_BANDS: list[tuple[float, float, float, int]] = [
    (-1000.0, 75.0, -5.0, 3000),
    (75.0, 85.0, -4.0, 2000),
    (85.0, 95.0, -3.0, 1000),
    (95.0, 105.0, 0.0, 0),
    (105.0, 115.0, 3.0, 1000),
    (115.0, 125.0, 4.0, 2000),
    (125.0, 10000.0, 5.0, 3000),
]


class StrategyExecuteOptions(BaseModel):
    scenario: str = "complete"
    createDrafts: bool = True
    autoApprove: bool = True
    approvalRef: str | None = None
    applyPrices: bool = True
    simulateLocalPrice: bool = False
    force: bool = False


class StrategyExecuteSkuResult(BaseModel):
    articleId: str
    status: Literal["executed", "skipped", "blocked", "failed"]
    skipReason: ExecutionSkipReason | None = None
    strategyId: str | None = None
    frontendStrategyId: str | None = None
    explanation: str | None = None
    oldPriceKopecks: int | None = None
    recommendedPriceKopecks: int | None = None
    deltaKopecks: int | None = None
    blockedReasons: list[str] = Field(default_factory=list)
    blockerDetails: list[dict[str, Any]] = Field(default_factory=list)
    draftId: str | None = None
    jobId: str | None = None
    applyState: str | None = None
    priceConfirmed: bool = False


class StrategyExecuteReport(BaseModel):
    runId: str
    executedCount: int
    skippedCount: int
    blockedCount: int
    items: list[StrategyExecuteSkuResult]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _settings_pmin_kopecks(settings: dict[str, Any]) -> int:
    return settings_minimum_price_kopecks(settings)


def _settings_pmax_kopecks(settings: dict[str, Any], old_price: int) -> int:
    p_max = _setting_int(settings, "pMaxKopecks")
    if p_max > 0:
        return p_max
    rrp = _setting_int(settings, "rrpKopecks")
    target_discount = _setting_float(settings, "targetDiscountPct")
    if rrp > 0 and target_discount > 0:
        return max(1, int(round(rrp * max(0.01, 1 - target_discount / 100))))
    max_margin_amount = _setting_int(settings, "maxMarginKopecks")
    max_margin_pct = _setting_float(settings, "maxMarginPct")
    if max_margin_amount > 0 or max_margin_pct > 0:
        base_cost = (
            _setting_int(settings, "cogsKopecks")
            + _setting_int(settings, "logisticsKopecks")
            + _setting_int(settings, "otherExpensePerSaleKopecks")
            + _setting_int(settings, "storageCostPerSaleKopecks")
            + max_margin_amount
        )
        denominator = 1 - (
            _setting_float(settings, "wbCommissionPct")
            + _setting_float(settings, "taxPct")
            + _effective_price_expense_pct(settings)
            + _setting_float(settings, "advertCostPercent")
            + max_margin_pct
        ) / 100
        if denominator > 0:
            return max(1, int(round(base_cost / denominator)))
    return max(1, old_price * 2)


def _settings_price_step_pct(settings: dict[str, Any], default: float = 3.0) -> float:
    try:
        value = float(settings.get("priceStepPct") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return value
    return _algorithm_float_setting("priceStepPct", default)


def _settings_step_interval_minutes(settings: dict[str, Any]) -> int:
    try:
        minutes = int(settings.get("priceStepMinutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0:
        return max(5, minutes)
    try:
        hours = int(settings.get("priceStepHours") or 0)
    except (TypeError, ValueError):
        hours = 0
    if hours > 0:
        return max(5, hours * 60)
    return max(5, int(_algorithm_settings().get("syncIntervalMinutes") or 60))


def _last_price_change_at(article_id: str, organization_id: int | None) -> datetime | None:
    try:
        changelog = list_repricer_changelog(article_id=article_id, limit=1, organization_id=organization_id)
    except Exception:
        return None
    items = changelog.get("items") if isinstance(changelog, dict) else None
    if not isinstance(items, list) or not items:
        return None
    timestamp = items[0].get("timestamp") if isinstance(items[0], dict) else None
    return _parse_iso(str(timestamp or ""))


def _strategy_interval_not_due(
    article_id: str,
    settings: dict[str, Any],
    organization_id: int | None,
) -> tuple[bool, datetime | None, int]:
    interval_minutes = _settings_step_interval_minutes(settings)
    last_at = _last_price_change_at(article_id, organization_id)
    if last_at is None:
        return False, None, interval_minutes
    next_at = last_at + timedelta(minutes=interval_minutes)
    return _utc_now() < next_at, next_at, interval_minutes


def _should_enforce_strategy_interval(options: StrategyExecuteOptions) -> bool:
    return bool(options.createDrafts or options.applyPrices or options.simulateLocalPrice)


def _guard_reason_codes(guard_report: Any) -> list[str]:
    reasons = list(getattr(guard_report, "blockers", []) or [])
    for trigger in getattr(guard_report, "triggers", []) or []:
        code = getattr(trigger, "code", None)
        if code:
            reasons.append(str(code))
        observed = getattr(trigger, "observedValue", None)
        if code == "pmin_pmax_block" and isinstance(observed, str):
            reasons.extend([item.strip() for item in observed.split(",") if item.strip()])
    return sorted(set(reasons))


def _guard_detail_payload(guard_report: Any) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for trigger in getattr(guard_report, "triggers", []) or []:
        code = getattr(trigger, "code", None)
        if not code:
            continue
        item: dict[str, Any] = {"code": str(code)}
        message = getattr(trigger, "message", None)
        observed = getattr(trigger, "observedValue", None)
        threshold = getattr(trigger, "threshold", None)
        if message is not None:
            item["message"] = message
        if observed is not None:
            item["observedValue"] = observed
        if threshold is not None:
            item["threshold"] = threshold
        details.append(item)
    return details


def _economics_from_row(row: dict[str, Any]) -> DraftEconomicsInput:
    settings = row["settings"]
    analytics = row.get("analytics") or {}
    stock_units = analytics.get("wbStockUnits")
    current_price = int(row.get("meta", {}).get("currentPriceKopecks") or analytics.get("basePriceKopecks") or 0)
    storage = _setting_int(settings, "storageCostPerSaleKopecks")
    tax_kopecks = round(current_price * _setting_float(settings, "taxPct") / 100) if current_price > 0 else 0
    commission_pct = analytics.get("wbCommissionPct")
    if analytics.get("commissionState") in {"fallback", "no_data"}:
        commission_pct = None
    elif commission_pct is None:
        commission_pct = settings.get("wbCommissionPct")
    return DraftEconomicsInput(
        cogsKopecks=_setting_int(settings, "cogsKopecks"),
        commissionPct=commission_pct,
        logisticsKopecks=_setting_int(settings, "logisticsKopecks") + _setting_int(settings, "otherExpensePerSaleKopecks"),
        storageKopecks=storage,
        taxKopecks=tax_kopecks,
        taxPct=_setting_float(settings, "taxPct") if settings.get("taxPct") is not None else None,
        taxConfigured=settings.get("taxPct") is not None,
        buyoutPct=analytics.get("buyoutPct"),
        stockUnits=stock_units if type(stock_units) is int and stock_units >= 0 else None,
        promoActive=analytics.get("promotionStatus") == "yes",
    )


def _baskets_orders_trends(row: dict[str, Any]) -> tuple[Literal["up", "down"], Literal["up", "down"]]:
    meta = row["meta"]
    analytics = row.get("analytics") or {}
    baskets = int(analytics["baskets"])
    basket_norm = max(1, int(meta.get("basketNorm") or 1))
    orders = int(analytics["ordersUnits"])
    baskets_trend: Literal["up", "down"] = "up" if baskets >= basket_norm else "down"
    orders_trend: Literal["up", "down"] = "up" if orders >= max(1, round(basket_norm * 0.6)) else "down"
    return baskets_trend, orders_trend


def _order_comparison(row: dict[str, Any], main_days: int) -> tuple[Decimal | None, list[str]]:
    periods = (row.get("analytics") or {}).get("strategyPeriods")
    periods = periods if isinstance(periods, dict) else {}
    values = []
    blockers = []
    for name, days in (("main", main_days), ("comparison", 7)):
        period = periods.get(name)
        period = period if isinstance(period, dict) else {}
        count = _observed_number({"analytics": period}, "ordersUnits")
        if count is None or count < 0 or count != count.to_integral_value() or period.get("periodDays") != days:
            blockers.append(f"{name}_orders_period_required")
        if period.get("state") not in {"ok", "fresh", "complete"}:
            blockers.append(f"{name}_orders_period_incomplete")
        values.append(count)
    if blockers:
        return None, blockers
    if periods["main"].get("periodTo") != periods["comparison"].get("periodTo"):
        return None, ["comparison_period_end_mismatch"]
    if values[1] == 0:
        return None, ["comparison_zero_baseline"]
    return values[0] * 7 * 100 / (values[1] * main_days), []


def _revenue_index(row: dict[str, Any]) -> Decimal:
    # Keep the established typed ID and bands; the approved metric is orders.
    index, blockers = _order_comparison(row, 1)
    if blockers or index is None:
        raise ValueError(",".join(blockers))
    return index


def _revenue_trend_pct(row: dict[str, Any]) -> float:
    return float(_revenue_index(row) - 100)


def _revenue_band_delta(revenue_index_pct: float) -> tuple[float, int]:
    for low, high, delta_pct, rub_floor in REVENUE_BANDS:
        if low <= revenue_index_pct < high:
            return delta_pct, rub_floor
    return 0.0, 0


PLAN_FACT_BANDS: list[tuple[float, float, float | None]] = [
    (0.0, 10.0, None),
    (10.0, 20.0, -15.0),
    (20.0, 30.0, -13.0),
    (30.0, 40.0, -10.0),
    (40.0, 50.0, -8.0),
    (50.0, 60.0, -6.0),
    (60.0, 70.0, -4.0),
    (70.0, 80.0, -3.0),
    (80.0, 90.0, -2.0),
    (90.0, 100.0, -1.0),
    (100.0, 110.0, 0.0),
    (110.0, 120.0, 1.0),
    (120.0, 130.0, 2.0),
    (130.0, 140.0, 5.0),
    (140.0, 10000.0, 10.0),
]

TURNOVER_BANDS: list[tuple[float, float, float]] = [
    (0.0, 3.0, 25.0),
    (3.0, 5.0, 20.0),
    (5.0, 7.0, 15.0),
    (30.0, 60.0, -15.0),
    (60.0, 90.0, -20.0),
    (90.0, 10000.0, -25.0),
]

EXTERNAL_STRATEGY_BLOCKERS: dict[str, tuple[str, str]] = {
    "bundles": ("strategy_disabled", "Комплекты временно выключены"),
}


def _analytics_number(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = (row.get("analytics") or {}).get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _setting_float(settings: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(settings.get(key) if settings.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _setting_int(settings: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(round(float(settings.get(key) if settings.get(key) is not None else default)))
    except (TypeError, ValueError):
        return default


def _setting_bool(settings: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = settings.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "да", "on"}


def _effective_price_expense_pct(settings: dict[str, Any]) -> float:
    promo_pct = _setting_float(settings, "promoCostPercent")
    other_pct = _setting_float(settings, "otherExpensePricePct")
    return max(promo_pct, other_pct)


def _orders_units(row: dict[str, Any]) -> int:
    return int(_analytics_number(row, "ordersUnits", 0.0))


def _observed_number(row: dict[str, Any], key: str) -> Decimal | None:
    value = (row.get("analytics") or {}).get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _stock_units(row: dict[str, Any]) -> int | None:
    stock = _observed_number(row, "wbStockUnits")
    if stock is None or stock < 0 or stock != stock.to_integral_value():
        return None
    return int(stock)


def _days_to_oos(row: dict[str, Any]) -> float | None:
    orders = _observed_number(row, "ordersUnits")
    days = _observed_number(row, "periodDays")
    stock = _stock_units(row)
    if orders is None or orders <= 0 or days is None or days <= 0 or stock is None:
        return None
    return float(Decimal(stock) * days / orders)


def _strategy_data_blockers(row: dict[str, Any], strategy: str) -> list[str]:
    """Validate only the signals consumed by this branch; zero is an observation."""
    analytics = row.get("analytics") or {}
    required: dict[str, str | None] = {}
    if strategy in {"stockout_guard", "turnover_control", "plan_fact_daily", "plan_fact_period",
                    "plan_fact_group", "plan_fact_interval", "illiquid", "baskets_orders"}:
        orders_state = "basketsState" if analytics.get("ordersSource") == "sales_funnel.orderCount" else "periodStatsState"
        required.update(ordersUnits=orders_state, periodDays=orders_state)
        if (analytics.get("ordersSource") in {"finance_sales_fallback", "fallback"}
            or "ordersSource" in analytics and analytics["ordersSource"] is None):
            return ["orders_source_required"]
    if strategy in {"stockout_guard", "turnover_control"}:
        required["wbStockUnits"] = "stockState"
    if strategy == "baskets_orders":
        required["baskets"] = "basketsState"
        if _setting_float(row.get("meta") or {}, "basketNorm") <= 0:
            return ["basket_norm_required"]
    if strategy in {"plan_fact_daily", "plan_fact_period", "plan_fact_interval"}:
        settings = row.get("settings") or {}
        if _setting_float(settings, "ordersPlanQty") <= 0 and _setting_float(row.get("meta") or {}, "basketNorm") <= 0:
            return ["orders_plan_required"]
        if _setting_float(settings, "ordersPlanQty") > 0 and settings.get("ordersPlanDays") is not None and _setting_float(settings, "ordersPlanDays") <= 0:
            return ["orders_plan_period_required"]
    if strategy == "plan_fact_period":
        metric = str(_algorithm_settings().get("planFactMetric") or row.get("settings", {}).get("repricerMode") or "orders")
        if metric == "revenue":
            required["revenueKopecks"] = "financeState"
        elif metric == "margin":
            required["marginKopecks"] = "plannedMarginState"
    blockers = []
    for key, state_key in required.items():
        value = _observed_number(row, key)
        if (value is None or (value < 0 and key != "marginKopecks") or (key == "periodDays" and value <= 0)
            or key in {"wbStockUnits", "ordersUnits", "baskets", "periodDays"} and value != value.to_integral_value()):
            blockers.append("stock_missing" if key == "wbStockUnits" else f"{key}_required")
        state = analytics.get(state_key) if state_key else None
        if state is not None and state not in {"ok", "fresh", "ready", "complete"}:
            blockers.append(f"{state_key}_{state}")
        coverage = (analytics.get("periodCoverageStates") or {}).get(state_key)
        if coverage is not None and coverage not in {"ok", "complete"}:
            blockers.append(f"{state_key}_coverage_{coverage}")
    if strategy in {"optimal_price", "metric_dynamics"}:
        _index, comparison_blockers = _order_comparison(row, 3 if strategy == "optimal_price" else 1)
        blockers.extend(comparison_blockers)
    if strategy == "plan_fact_interval":
        for key in ("hourlyOrders", "todayHourlyOrders"):
            buckets = analytics.get(key)
            if not isinstance(buckets, list) or not buckets or any(
                not isinstance(item, dict) or type(item.get("hour")) is not int or not 0 <= item["hour"] < 24
                or type(item.get("ordersUnits")) is not int or item["ordersUnits"] < 0 for item in buckets
            ):
                blockers.append("hourly_sales_required" if key == "hourlyOrders" else f"{key}_required")
        if "todayHourlyObservedAt" in analytics:
            observed = _parse_iso(str(analytics.get("todayHourlyObservedAt") or ""))
            local_now = _utc_now().astimezone(ZoneInfo("Europe/Moscow"))
            start, _end = _interval_bounds(local_now.hour, int(_strategy_config(row).get("intervalHours") or 4))
            if observed is None or observed < local_now.replace(hour=start, minute=0, second=0, microsecond=0):
                blockers.append("today_hourly_coverage_required")
    return list(dict.fromkeys(blockers))


def _apply_pct(price_kopecks: int, delta_pct: float) -> int:
    return max(1, int(round(price_kopecks * (1 + delta_pct / 100))))


def _algorithm_bool_setting(key: str, default: bool = False) -> bool:
    raw = _algorithm_settings().get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "да", "on"}


def _algorithm_float_setting(key: str, default: float) -> float:
    try:
        return float(_algorithm_settings().get(key) or default)
    except (TypeError, ValueError):
        return default


def _pretty_price_kopecks(price_kopecks: int, template: Any = None) -> int:
    rub = max(1, int(round(price_kopecks / 100)))
    template_text = str(template or "").strip().lower()
    match = None
    if template_text:
        match = re.search(r"(\d{1,3})\s*$", template_text.replace("x", ""))
    if match:
        suffix = int(match.group(1))
        width = len(match.group(1))
        base = 10 ** width
        rounded = (rub // base) * base + suffix
        if rounded > rub:
            rounded -= base
        if rounded > 0:
            return rounded * 100
    if rub >= 1000:
        rounded = (rub // 1000) * 1000 + 990
        if rounded > rub:
            rounded -= 1000
        if rounded <= 0:
            rounded = 990
    elif rub >= 100:
        rounded = (rub // 100) * 100 + 90
        if rounded > rub:
            rounded -= 100
        if rounded <= 0:
            rounded = 90
    else:
        rounded = max(1, rub)
    return rounded * 100


def _apply_price_rounding(price_kopecks: int, *, current: int, settings: dict[str, Any] | None = None) -> tuple[int, str | None]:
    sku_enabled = _setting_bool(settings or {}, "beautyPriceEnabled", False)
    if not sku_enabled and not _algorithm_bool_setting("priceRoundingEnabled", False):
        return price_kopecks, None
    rounded = _pretty_price_kopecks(price_kopecks, (settings or {}).get("beautyPriceTemplate"))
    if rounded == price_kopecks:
        return price_kopecks, None
    if price_kopecks > current and rounded < current:
        rounded = price_kopecks
    max_change = _setting_int(settings or {}, "beautyPriceMaxChangeKopecks")
    max_change_pct = _setting_float(settings or {}, "beautyPriceMaxChangePct")
    if max_change > 0 and abs(rounded - price_kopecks) > max_change:
        return price_kopecks, None
    if max_change_pct > 0 and abs(rounded - price_kopecks) / max(1, price_kopecks) * 100 > max_change_pct:
        return price_kopecks, None
    return rounded, f"округлено до красивой цены {round(rounded / 100)} ₽"


def _clamp_candidate_price(
    *,
    current: int,
    recommended: int,
    min_price: int,
    p_max: int | None,
) -> tuple[int, list[str]]:
    limits = get_repricer_guard_limits()
    lower = max(1, int(min_price or 1))
    upper = int(p_max) if p_max and p_max > 0 else None
    notes: list[str] = []

    if recommended > current:
        step_cap = _apply_pct(current, max(0.0, float(limits.per_update_step_pct)))
        if recommended > step_cap:
            recommended = step_cap
            notes.append(f"шаг ограничен guard до +{limits.per_update_step_pct:.0f}%")
        day_cap = _apply_pct(current, max(0.0, float(limits.per_day_step_pct)))
        if recommended > day_cap:
            recommended = day_cap
            notes.append(f"дневной лимит guard до +{limits.per_day_step_pct:.0f}%")
    elif recommended < current:
        step_floor = _apply_pct(current, -max(0.0, float(limits.per_update_step_pct)))
        if recommended < step_floor:
            recommended = step_floor
            notes.append(f"шаг ограничен guard до -{limits.per_update_step_pct:.0f}%")
        day_floor = _apply_pct(current, -max(0.0, float(limits.per_day_step_pct)))
        if recommended < day_floor:
            recommended = day_floor
            notes.append(f"дневной лимит guard до -{limits.per_day_step_pct:.0f}%")

    if recommended < lower:
        recommended = lower
        notes.append("цена поднята до P_MIN")
    if upper is not None and recommended > upper:
        recommended = upper
        notes.append("цена ограничена P_MAX")
    return max(1, recommended), notes


def _strategy_config(row: dict[str, Any]) -> dict[str, Any]:
    strategy = row.get("strategy") or {}
    config = strategy.get("config")
    return config if isinstance(config, dict) else {}


def _algorithm_int_setting(key: str, default: int) -> int:
    try:
        return int(_algorithm_settings().get(key) or default)
    except (TypeError, ValueError):
        return default


def _basket_norm_period_days() -> int:
    return max(1, _algorithm_int_setting("basketNormPeriodDays", 7))


def _row_period_days(row: dict[str, Any]) -> int:
    analytics = row.get("analytics") or {}
    return max(1, int(analytics.get("periodDays") or _algorithm_settings().get("planFactFactPeriodDays") or _basket_norm_period_days()))


def _plan_orders_for_row_period(row: dict[str, Any]) -> Fraction:
    settings = row.get("settings") or {}
    orders_plan = _setting_float(settings, "ordersPlanQty")
    if orders_plan > 0:
        plan_days = max(1.0, _setting_float(settings, "ordersPlanDays", _row_period_days(row)))
        return max(Fraction(1), Fraction(str(orders_plan)) * _row_period_days(row) / Fraction(str(plan_days)))
    meta = row["meta"]
    basket_norm = max(1, int(meta.get("basketNorm") or 1))
    return max(Fraction(1), Fraction(basket_norm * _row_period_days(row), _basket_norm_period_days()))


def _daily_plan_orders(row: dict[str, Any]) -> Fraction:
    settings = row.get("settings") or {}
    orders_plan = _setting_float(settings, "ordersPlanQty")
    if orders_plan > 0:
        return max(Fraction(1), Fraction(str(orders_plan)) / Fraction(str(max(1.0, _setting_float(settings, "ordersPlanDays", 1)))))
    meta = row["meta"]
    basket_norm = max(1, int(meta.get("basketNorm") or 1))
    return max(Fraction(1), Fraction(basket_norm, _basket_norm_period_days()))


def _plan_fact_pct(row: dict[str, Any], *, metric: Literal["orders", "revenue", "margin"] = "orders") -> Fraction:
    meta = row["meta"]
    analytics = row.get("analytics") or {}
    plan_orders = _plan_orders_for_row_period(row)
    if metric == "revenue":
        actual = Fraction(str(analytics["revenueKopecks"]))
        plan = max(Fraction(1), meta["currentPriceKopecks"] * plan_orders)
        return actual / plan * 100
    if metric == "margin":
        actual = Fraction(str(analytics["marginKopecks"])) * max(1, _orders_units(row))
        plan = max(Fraction(1), meta["currentPriceKopecks"] * plan_orders * Fraction(1, 5))
        return actual / plan * 100
    return _orders_units(row) * 100 / plan_orders


def _plan_fact_price_from_completion(
    current: int,
    min_price: int,
    completion_pct: float | Fraction,
    *,
    label: str,
) -> tuple[int, str]:
    displayed = float(completion_pct)
    for low, high, delta_pct in PLAN_FACT_BANDS:
        if low <= completion_pct < high:
            if delta_pct is None:
                return min_price, f"{label} {displayed:.1f}%: ниже 10%, ставим минимальную цену"
            if delta_pct == 0:
                return current, f"{label} {displayed:.1f}%: 100-110%, держим базовую цену"
            return _apply_pct(current, delta_pct), f"{label} {displayed:.1f}% -> {delta_pct:+.0f}%"
    return current, f"{label} {displayed:.1f}%: правило не изменило цену"


def _plan_fact_recommended_price(row: dict[str, Any], min_price: int, *, metric: Literal["orders", "revenue", "margin"] = "orders") -> tuple[int, str]:
    current = int(row["meta"]["currentPriceKopecks"])
    completion_pct = _plan_fact_pct(row, metric=metric)
    return _plan_fact_price_from_completion(current, min_price, completion_pct, label="План-факт")


def _hourly_orders_units(row: dict[str, Any], key: str) -> list[int]:
    buckets = (row.get("analytics") or {}).get(key)
    result = [0 for _ in range(24)]
    if not isinstance(buckets, list):
        return result
    for item in buckets:
        if not isinstance(item, dict):
            continue
        try:
            hour = int(item.get("hour"))
            orders = int(item.get("ordersUnits") or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            result[hour] += max(0, orders)
    return result


def _interval_bounds(hour: int, interval_hours: int = 4) -> tuple[int, int]:
    normalized_interval = max(1, min(24, int(interval_hours or 4)))
    start = (max(0, min(23, hour)) // normalized_interval) * normalized_interval
    return start, min(24, start + normalized_interval)


def _sum_interval(values: list[int], start: int, end: int) -> int:
    return sum(values[max(0, start): min(24, end)])


def _plan_fact_interval_recommended_price(row: dict[str, Any], min_price: int) -> tuple[int | None, str, list[str]]:
    current = int(row["meta"]["currentPriceKopecks"])
    config = _strategy_config(row)
    interval_hours = int(config.get("intervalHours") or _algorithm_settings().get("planFactIntervalHours") or 4)
    history = _hourly_orders_units(row, "hourlyOrders")
    history_total = sum(history)
    if history_total <= 0:
        return None, "Интервальный план-факт: WB orders не дали почасовую историю за выбранный период", ["hourly_sales_required"]

    now_msk = _utc_now().astimezone(ZoneInfo("Europe/Moscow"))
    start, end = _interval_bounds(now_msk.hour, interval_hours)
    interval_history_orders = _sum_interval(history, start, end)
    weight = Fraction(interval_history_orders, history_total)
    daily_plan = _daily_plan_orders(row)
    interval_plan = max(Fraction(1), daily_plan * weight)
    today = _hourly_orders_units(row, "todayHourlyOrders")
    interval_fact = _sum_interval(today, start, end)
    completion_pct = interval_fact * 100 / interval_plan
    recommended, base_explanation = _plan_fact_price_from_completion(
        current,
        min_price,
        completion_pct,
        label=f"Интервал {start:02d}:00-{end:02d}:00 МСК",
    )
    explanation = (
        f"{base_explanation}; вес {float(weight * 100):.1f}% от дня, "
        f"план {float(interval_plan):.1f} заказов, факт {interval_fact}"
    )
    return recommended, explanation, []


def _config_date(raw: Any) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def _config_time_minutes(raw: Any, *, fallback: str) -> int:
    text = str(raw or fallback).strip()
    parts = text.split(":", 1)
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        hour, minute = [int(part) for part in fallback.split(":", 1)]
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    return hour * 60 + minute


def _time_window_contains(start_minutes: int, end_minutes: int, now_minutes: int) -> bool:
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes <= end_minutes
    return now_minutes >= start_minutes or now_minutes <= end_minutes


def _schedule_rule_active(rule: dict[str, Any], now_msk: datetime) -> bool:
    start_date = _config_date(rule.get("dateFrom"))
    end_date = _config_date(rule.get("dateTo"))
    today = now_msk.date()
    if start_date is not None and today < start_date:
        return False
    if end_date is not None and today > end_date:
        return False
    days = rule.get("daysOfWeek")
    if isinstance(days, list) and days:
        try:
            allowed_days = {int(day) for day in days}
        except (TypeError, ValueError):
            allowed_days = set()
        if allowed_days and now_msk.isoweekday() not in allowed_days:
            return False
    start_minutes = _config_time_minutes(rule.get("timeFrom"), fallback="00:00")
    end_minutes = _config_time_minutes(rule.get("timeTo"), fallback="23:59")
    now_minutes = now_msk.hour * 60 + now_msk.minute
    return _time_window_contains(start_minutes, end_minutes, now_minutes)


def _schedule_recommended_price(row: dict[str, Any]) -> tuple[int | None, str, list[str]]:
    current = int(row["meta"]["currentPriceKopecks"])
    config = _strategy_config(row)
    rules = config.get("scheduleRules")
    if not isinstance(rules, list) or not rules:
        return None, "Расписание: нет сохраненных правил дат/часов", ["schedule_rules_required"]

    now_msk = _utc_now().astimezone(ZoneInfo("Europe/Moscow"))
    for rule in rules:
        if not isinstance(rule, dict) or not _schedule_rule_active(rule, now_msk):
            continue
        mode = str(rule.get("mode") or "set_price")
        if mode == "hold":
            return current, "Расписание: активное окно держит текущую цену", []
        if mode == "delta_pct":
            delta_pct = float(rule.get("deltaPct") or 0)
            return _apply_pct(current, delta_pct), f"Расписание: активное окно -> {delta_pct:+.1f}%", []
        price_kopecks = int(rule.get("priceKopecks") or 0)
        if price_kopecks <= 0:
            return None, "Расписание: в активном окне нет целевой цены", ["schedule_price_required"]
        return price_kopecks, f"Расписание: активное окно -> цена {round(price_kopecks / 100)} ₽", []

    return current, "Расписание: сейчас нет активного окна, цену не меняем", []


def _plan_fact_group_recommended_price(
    row: dict[str, Any],
    min_price: int,
    *,
    all_rows: list[dict[str, Any]] | None,
    context: dict[str, Any] | None = None,
) -> tuple[int | None, str, list[str]]:
    current = int(row["meta"]["currentPriceKopecks"])
    config = _strategy_config(row)
    group_ids = config.get("groupArticleIds")
    if not isinstance(group_ids, list) or not group_ids:
        return None, "План-факт группы: нет состава группы SKU", ["group_scope_required"]
    group_set = {str(article_id) for article_id in group_ids}
    execution_context = context if context is not None else {}
    if "group_row_index" not in execution_context:
        execution_context["group_row_index"] = {
            str(item.get("meta", {}).get("articleId") or ""): item for item in (all_rows or [])}
    row_index = execution_context["group_row_index"]
    group_rows = [row_index[article_id] for article_id in group_set if article_id in row_index]
    group_rows = [{**item, "analytics": {**item.get("analytics", {}), **item.get("analytics", {}).get("groupFact", {}),
        "periodCoverageStates": {**item.get("analytics", {}).get("periodCoverageStates", {}), "basketsState": item["analytics"]["groupFact"]["state"]},
        "ordersSource": "sales_funnel.orderCount", "basketsState": item["analytics"]["groupFact"]["state"]}}
        if item.get("analytics", {}).get("groupFact") else item for item in group_rows]
    period = (row.get("analytics") or {}).get("periodDays")
    scope = row.get("meta", {}).get("accountScope")
    if len(group_rows) != len(group_set) or any(
        _strategy_data_blockers(item, "plan_fact_group")
        or item.get("meta", {}).get("accountScope") != scope
        or item.get("analytics", {}).get("periodDays") != period
        or item.get("analytics", {}).get("periodFrom") != row.get("analytics", {}).get("periodFrom")
        or item.get("analytics", {}).get("periodTo") != row.get("analytics", {}).get("periodTo")
        for item in group_rows
    ):
        return None, "План-факт группы: неполный состав, период или данные участников", ["group_data_incomplete"]

    try:
        group_plan = int(config.get("planOrders") or 0)
    except (TypeError, ValueError):
        group_plan = 0
    if group_plan <= 0:
        if any(_setting_float(item.get("settings") or {}, "ordersPlanQty") <= 0
               and _setting_float(item.get("meta") or {}, "basketNorm") <= 0 for item in group_rows):
            return None, "План-факт группы: нужен общий план или подтверждённые планы всех участников", ["group_plan_required"]
        group_plan = round(sum(_plan_orders_for_row_period(item) for item in group_rows))
    facts = execution_context.setdefault("group_order_facts", {})
    group_key = (frozenset(group_set), scope, period)
    if group_key not in facts:
        facts[group_key] = sum(_orders_units(item) for item in group_rows)
    group_fact = facts[group_key]
    completion_pct = Fraction(group_fact * 100, max(1, group_plan))
    recommended, base_explanation = _plan_fact_price_from_completion(
        current,
        min_price,
        completion_pct,
        label=f"План-факт группы «{config.get('groupName') or 'SKU'}»",
    )
    explanation = f"{base_explanation}; факт {group_fact}, план {group_plan}, SKU в группе {len(group_rows)}"
    return recommended, explanation, []


def _turnover_recommended_price(row: dict[str, Any]) -> tuple[int, str]:
    current = int(row["meta"]["currentPriceKopecks"])
    settings = row.get("settings") or {}
    days = _days_to_oos(row)
    if days is None:
        return current, "Оборачиваемость не рассчитана: нет заказов за период"
    target = _setting_float(settings, "targetTurnover")
    if target > 0:
        ratio = days / max(1.0, target)
        if ratio < 0.5:
            return _apply_pct(current, 25.0), f"Оборачиваемость {days:.1f} дн ниже цели {target:.1f} -> +25%"
        if ratio < 0.8:
            return _apply_pct(current, 12.0), f"Оборачиваемость {days:.1f} дн ниже цели {target:.1f} -> +12%"
        if ratio > 2.0:
            return _apply_pct(current, -20.0), f"Оборачиваемость {days:.1f} дн выше цели {target:.1f} -> -20%"
        if ratio > 1.35:
            return _apply_pct(current, -10.0), f"Оборачиваемость {days:.1f} дн выше цели {target:.1f} -> -10%"
        return current, f"Оборачиваемость {days:.1f} дн около цели {target:.1f}"
    for low, high, delta_pct in TURNOVER_BANDS:
        if low <= days < high:
            return _apply_pct(current, delta_pct), f"Оборачиваемость {days:.1f} дн -> {delta_pct:+.0f}%"
    return current, f"Оборачиваемость {days:.1f} дн в нейтральном диапазоне"


def _stockout_guard_recommended_price(row: dict[str, Any]) -> tuple[int, str]:
    current = int(row["meta"]["currentPriceKopecks"])
    settings = row.get("settings") or {}
    stock = _stock_units(row)
    normal_stock = _setting_int(settings, "normalStockQty")
    if normal_stock > 0 and stock is not None and stock <= normal_stock:
        delta = 25.0 if stock <= max(1, normal_stock // 2) else 12.0
        return _apply_pct(current, delta), f"OOS-защита: остаток {stock} <= минимум {normal_stock} -> +{delta:.0f}%"
    days = _days_to_oos(row)
    if days is None:
        return current, "OOS-защита не активна: нет заказов за период"
    if days >= 7:
        return current, f"OOS-защита не активна: запаса примерно на {days:.1f} дн"
    if days < 3:
        delta = 25.0
    elif days < 5:
        delta = 15.0
    else:
        delta = 10.0
    return _apply_pct(current, delta), f"OOS-защита: запаса {days:.1f} дн (<7) -> {delta:+.0f}%"


def _competitor_reference_price(row: dict[str, Any]) -> int | None:
    analytics = row.get("analytics") or {}
    settings = row.get("settings") or {}
    for key in (
        "competitorPriceKopecks",
        "competePriceKopecks",
        "marketplacePriceKopecks",
        "externalPriceKopecks",
    ):
        value = analytics.get(key)
        if value is None:
            value = settings.get(key)
        try:
            price = int(value or 0)
        except (TypeError, ValueError):
            price = 0
        if price > 0:
            return price
    return None


def _competitor_recommended_price(row: dict[str, Any]) -> tuple[int | None, str, list[str]]:
    current = int(row["meta"]["currentPriceKopecks"])
    settings = row.get("settings") or {}
    reference = _competitor_reference_price(row)
    if reference is None:
        return None, "Конкурентная стратегия: нет цены конкурента в источниках данных", ["competitor_price_required"]
    diff_value = _setting_float(settings, "competeDiffValue")
    diff_type = str(settings.get("competeDiffType") or "pct").strip().lower()
    if diff_type in {"amount", "rub", "руб", "ruble"}:
        delta = int(round(diff_value * 100))
    else:
        delta = int(round(reference * diff_value / 100))
    price_type = str(settings.get("competePriceType") or "below").strip().lower()
    if price_type in {"above", "higher", "выше", "plus", "+"}:
        recommended = reference + abs(delta)
    elif price_type in {"equal", "same", "равно", "match"}:
        recommended = reference
    else:
        recommended = reference - abs(delta)
    return max(1, recommended), f"Конкурентная цена {round(reference / 100)} ₽, отличие {diff_value:g} ({diff_type}) -> {round(max(1, recommended) / 100)} ₽", []


def _basket_threshold_recommended_price(row: dict[str, Any]) -> tuple[int, str]:
    current = int(row["meta"]["currentPriceKopecks"])
    meta = row.get("meta") or {}
    analytics = row.get("analytics") or {}
    baskets = int(analytics.get("baskets") if analytics.get("baskets") is not None else meta.get("basketsLast7d") or 0)
    high = max(0, _algorithm_int_setting("cartHighBasketsThreshold", 30))
    low = max(0, _algorithm_int_setting("cartLowBasketsThreshold", 5))
    step_pct = max(0.0, _settings_price_step_pct(row.get("settings") or {}, 3.0))
    if high > 0 and baskets >= high:
        return _apply_pct(current, step_pct), f"Корзины {baskets} >= {high} -> +{step_pct:.1f}%"
    if baskets <= low:
        return _apply_pct(current, -step_pct), f"Корзины {baskets} <= {low} -> -{step_pct:.1f}%"
    return current, f"Корзины {baskets} в нейтральном диапазоне {low + 1}-{max(low + 1, high - 1)}"


def _illiquid_recommended_price(row: dict[str, Any]) -> tuple[int | None, str, list[str]]:
    article_id = row["meta"]["articleId"]
    runtime = row.get("_previewLiquidationRuntime", LIQUIDATION_ACTIVE.get(article_id)) or {}
    next_step_at = _parse_iso(str(runtime.get("nextStepAt") or "")) if runtime else None
    if next_step_at is not None and _utc_now() < next_step_at:
        return None, "Неликвид: следующий дневной шаг ещё не наступил", ["liquidation_not_due"]

    current = int(runtime.get("currentPriceKopecks") or row["meta"]["currentPriceKopecks"])
    step_pct = float(runtime.get("stepPct") or 3.0)
    hold_to = int(runtime.get("holdOrdersTo") or 1)
    orders = _orders_units(row)
    target = int(runtime.get("targetPriceKopecks") or 1)
    if orders <= 0:
        recommended = max(target, _apply_pct(current, -step_pct))
        explanation = f"Неликвид: 0 заказов за период -> -{step_pct:.0f}%"
    elif orders <= hold_to:
        recommended = current
        explanation = f"Неликвид: {orders} заказ(ов) в hold-диапазоне 1-{hold_to}, держим цену"
    else:
        recommended = _apply_pct(current, step_pct)
        explanation = f"Неликвид: спрос восстановился ({orders} заказов) -> +{step_pct:.0f}%"

    return recommended, explanation, []


def _commit_illiquid_runtime_step(article_id: str, recommended: int, explanation: str) -> None:
    runtime = LIQUIDATION_ACTIVE.get(article_id)
    if not runtime:
        return
    runtime["currentPriceKopecks"] = recommended
    runtime["nextStepAt"] = (_utc_now() + timedelta(hours=24)).isoformat()
    runtime["lastDecision"] = explanation


def _optimal_price_recommended_price(row: dict[str, Any]) -> tuple[int, str]:
    current = int(row["meta"]["currentPriceKopecks"])
    speed_index, blockers = _order_comparison(row, 3)
    if blockers or speed_index is None:
        return current, "Оптимальная цена: нужны полные сопоставимые периоды 3 и 7 дней"
    if speed_index >= 115:
        return _apply_pct(current, 3.0), f"Оптимальная цена: скорость заказов {speed_index:.1f}% к базе -> +3%"
    if speed_index <= 85:
        return _apply_pct(current, -3.0), f"Оптимальная цена: скорость заказов {speed_index:.1f}% к базе -> -3%"
    return current, f"Оптимальная цена: скорость заказов {speed_index:.1f}% стабильна"


def _resolve_strategy_id(row: dict[str, Any]) -> tuple[str | None, StrategyId | None]:
    strategy = row.get("strategy") or {}
    frontend_id = str(strategy.get("id") or row["meta"].get("activeStrategyId") or "")
    if frontend_id not in FRONTEND_STRATEGY_BY_ID:
        return None, None

    settings = row.get("settings") or {}
    repricer_mode = settings.get("repricerMode", "baskets")
    typed = strategy.get("typedStrategyId") or row["meta"].get("activeTypedStrategyId")

    if frontend_id in {
        "stockout_guard",
        "turnover_control",
        "plan_fact_daily",
        "plan_fact_period",
        "plan_fact_group",
        "plan_fact_interval",
        "cross_marketplace",
        "illiquid",
        "optimal_price",
        "schedule",
        "bundles",
    }:
        return frontend_id, None
    if frontend_id == "baskets_orders":
        return frontend_id, "baskets_orders_4599"
    if frontend_id == "metric_dynamics":
        return frontend_id, "revenue_dynamics_4600"
    if frontend_id == "night_price_mode":
        return frontend_id, "night_price_mode"
    global_default = default_typed_strategy_for_mode()
    if repricer_mode == "revenue" or global_default == "revenue_dynamics_4600":
        return frontend_id, "revenue_dynamics_4600"
    if typed in {"baskets_orders_4599", "revenue_dynamics_4600", "night_price_mode"}:
        return frontend_id, typed  # type: ignore[return-value]
    return frontend_id, "baskets_orders_4599"


def _is_night_window_active() -> bool:
    limits = get_repricer_guard_limits()
    if not limits.night_mode_enabled:
        return False
    timezone_name = _night_timezone_name()
    now_local = _utc_now().astimezone(ZoneInfo(timezone_name))
    hour = now_local.hour
    start = _algorithm_int_setting("nightMedianWindowStartHour", limits.night_window_start_hour)
    end = _algorithm_int_setting("nightMedianWindowEndHour", limits.night_window_end_hour)
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _night_local_now() -> datetime:
    return _utc_now().astimezone(ZoneInfo(_night_timezone_name()))


def _night_timezone_name() -> str:
    raw = str(_algorithm_settings().get("nightMedianTimezone") or "").strip()
    mapping = {
        "мск (utc+3)": "Europe/Moscow",
        "мск": "Europe/Moscow",
        "utc+3": "Europe/Moscow",
        "utc+03:00": "Europe/Moscow",
        "europe/moscow": "Europe/Moscow",
        "ekaterinburg": "Asia/Yekaterinburg",
        "asia/yekaterinburg": "Asia/Yekaterinburg",
    }
    normalized = raw.lower()
    if normalized in mapping:
        return mapping[normalized]
    try:
        ZoneInfo(raw)
        return raw
    except Exception:
        return "Europe/Moscow"


def _has_explicit_strategy(row: dict[str, Any]) -> bool:
    strategy = row.get("strategy") or {}
    source = str(
        strategy.get("assignmentSource")
        or row.get("meta", {}).get("assignmentSource")
        or ""
    ).lower().strip()
    return bool(strategy.get("id")) and source not in {"", "none", "derived"}


def _is_liquidation_scheduled(row: dict[str, Any]) -> bool:
    meta = row.get("meta") or {}
    strategy = row.get("strategy") or {}
    article_id = str(meta.get("articleId") or "")
    strategy_id = str(strategy.get("id") or meta.get("activeStrategyId") or "")
    return meta.get("status") == "liquidation" and strategy_id == "illiquid" and article_id in LIQUIDATION_ACTIVE


def _revenue_recommended_price(row: dict[str, Any], economics: DraftEconomicsInput) -> tuple[int, float, int, str]:
    meta = row["meta"]
    current = int(meta["currentPriceKopecks"])
    revenue_index = _revenue_index(row)
    delta_pct, rub_floor = _revenue_band_delta(revenue_index)
    if delta_pct == 0.0:
        return current, 0.0, 0, f"Индекс заказов {revenue_index:.1f}%: удержание цены"

    raw_delta_kopecks = round(current * delta_pct / 100)
    if rub_floor > 0 and raw_delta_kopecks != 0 and abs(raw_delta_kopecks) < rub_floor:
        sign = 1 if raw_delta_kopecks > 0 else -1
        raw_delta_kopecks = sign * rub_floor
    recommended = max(1, current + raw_delta_kopecks)
    return (
        recommended,
        (raw_delta_kopecks / current * 100) if current > 0 else 0.0,
        raw_delta_kopecks,
        f"Индекс заказов за завершённый день к среднему за 7 дней {revenue_index:.1f}% -> {delta_pct:+.1f}% (минимум {rub_floor} коп.)",
    )


def _build_dry_run_item(
    row: dict[str, Any],
    economics: DraftEconomicsInput,
    *,
    night_delta_pct: float | None = None,
) -> StrategyDryRunItem | None:
    meta = row["meta"]
    nm_id = meta.get("nmId")
    if nm_id is None:
        return None
    # Night uses its window median, not the daytime basket/order matrix.
    baskets_trend, orders_trend = _baskets_orders_trends(row) if night_delta_pct is None else ("down", "down")
    return StrategyDryRunItem(
        articleId=str(meta["articleId"]),
        nmId=int(nm_id),
        candidateSellerPriceKopecks=int(meta["currentPriceKopecks"]),
        economics=economics,
        basketsTrend=baskets_trend,
        ordersTrend=orders_trend,
        revenueTrendPct=0,
        nightTargetDeltaPct=night_delta_pct if night_delta_pct is not None else _night_target_delta_pct(),
    )


def _night_target_delta_pct() -> float:
    base = abs(_algorithm_float_setting("nightMedianApplyDeltaPct", 4.0))
    mode = str(_algorithm_settings().get("nightMedianMode") or "conservative").strip().lower()
    if mode == "aggressive":
        return min(20.0, base)
    return min(20.0, base * 0.75)


def _night_window_hours() -> tuple[int, int]:
    limits = get_repricer_guard_limits()
    return (
        _algorithm_int_setting("nightMedianWindowStartHour", limits.night_window_start_hour),
        _algorithm_int_setting("nightMedianWindowEndHour", limits.night_window_end_hour),
    )


def _night_window_label() -> str:
    start, end = _night_window_hours()
    return f"{start:02d}:00-{end:02d}:00"


def _night_window_key(now_local: datetime | None = None) -> str:
    current = now_local or _night_local_now()
    start, end = _night_window_hours()
    window_date = current.date()
    if start > end and current.hour < end:
        window_date = window_date - timedelta(days=1)
    return window_date.isoformat()


def _night_previous_window_key(now_local: datetime | None = None) -> str:
    current = now_local or _night_local_now()
    start, end = _night_window_hours()
    if start < end:
        window_date = current.date() if current.hour >= end else current.date() - timedelta(days=1)
    else:
        window_date = current.date() if current.hour >= start else current.date() - timedelta(days=1)
    return window_date.isoformat()


def _night_strategy_name(strategy_id: str | None, frontend_id: str | None, row: dict[str, Any]) -> str:
    if strategy_id == "night_price_mode" or frontend_id == "night_price_mode":
        return "Ночная медиана"
    return str((row.get("strategy") or {}).get("name") or frontend_id or strategy_id or "")


def _night_median_enabled_for_row(row: dict[str, Any]) -> bool:
    if not _algorithm_bool_setting("nightMedianEnabled", True):
        return False
    if _algorithm_bool_setting(
        "nightMedianAutoEnableAllSkus",
        _algorithm_bool_setting("nightMedianGlobal", True),
    ):
        return True
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    return bool(settings.get("nightMedianEnabled", False))


def _night_median_collecting_now() -> bool:
    return (
        _algorithm_bool_setting("nightMedianEnabled", True)
        and _algorithm_bool_setting("nightMedianCollectEnabled", True)
        and _is_night_window_active()
    )


def _night_strategy_config_override() -> dict[str, Any]:
    start, end = _night_window_hours()
    return {
        "enabled": True,
        "windowStartHour": start,
        "windowEndHour": end,
        "timezone": _night_timezone_name(),
    }


def _night_execution_explanation() -> str:
    mode = str(_algorithm_settings().get("nightMedianMode") or "conservative").strip().lower()
    mode_label = "агрессивный" if mode == "aggressive" else "консервативный"
    return (
        f"Ночная медиана: окно {_night_window_label()} МСК, режим {mode_label}, "
        f"коррекция {_night_target_delta_pct():+.2f}%."
    )


def _night_median_apply_explanation(payload: dict[str, Any] | None) -> str:
    if not payload:
        return _night_execution_explanation()
    direction = "повышение" if float(payload.get("deltaPct") or 0) >= 0 else "снижение"
    median_baskets = float(payload.get("medianBaskets") or 0)
    current_baskets = int(payload.get("currentBaskets") or 0)
    samples_count = int(payload.get("samplesCount") or 0)
    return (
        f"Ночная медиана корзин: окно {payload.get('windowKey')}, медиана {median_baskets:.1f}, "
        f"текущее значение {current_baskets}, samples {samples_count} -> {direction} "
        f"{float(payload.get('deltaPct') or 0):+.2f}%."
    )


def _night_basket_observation(row: dict[str, Any]) -> tuple[int, datetime] | None:
    """Use count, coverage and timestamp from one complete observation."""
    analytics = row.get("analytics") if isinstance(row.get("analytics"), dict) else {}
    dedicated = any(key in analytics for key in ("nightBaskets", "nightBasketsObservedAt", "nightBasketsState"))
    field = "nightBaskets" if dedicated else "baskets"
    state_key = f"{field}State"
    states = [analytics[state_key]] if state_key in analytics else []
    coverage = analytics.get("periodCoverageStates")
    if isinstance(coverage, dict) and state_key in coverage:
        states.append(coverage[state_key])
    elif not dedicated and coverage is not None and not isinstance(coverage, dict):
        return None
    if not states or any(state not in ("ok", "complete") for state in states):
        return None
    count = analytics.get(field)
    observed = utc_timestamp(analytics.get(f"{field}ObservedAt"))
    if type(count) is not int or count < 0 or observed is None:
        return None
    return count, observed


def _row_baskets_count(row: dict[str, Any]) -> int | None:
    observation = _night_basket_observation(row)
    return observation[0] if observation is not None else None


def _night_observed_at(row: dict[str, Any]) -> datetime | None:
    observation = _night_basket_observation(row)
    return observation[1] if observation is not None else None


def _night_observation_fresh(row: dict[str, Any]) -> bool:
    observed = _night_observed_at(row)
    now = _utc_now()
    return observed is not None and now - timedelta(hours=1) <= observed <= now + timedelta(seconds=30)


def _night_contains(key: str, at: datetime) -> bool:
    try:
        day = date.fromisoformat(key)
    except ValueError:
        return False
    start, end = _night_window_hours()
    zone = ZoneInfo(_night_timezone_name())
    start_at = datetime(day.year, day.month, day.day, start, tzinfo=zone)
    end_at = datetime(day.year, day.month, day.day, end, tzinfo=zone)
    if end <= start:
        end_at += timedelta(days=1)
    return start_at <= at.astimezone(zone) < end_at


def _night_median_cache(organization_id: int) -> dict[str, Any]:
    payload = get_source_cache(organization_id, NIGHT_MEDIAN_SOURCE_KEY, slim=False) or {}
    windows = payload.get("windows")
    if not isinstance(windows, dict):
        payload["windows"] = {}
    return payload


def _trim_night_median_cache(payload: dict[str, Any]) -> dict[str, Any]:
    windows = payload.get("windows") if isinstance(payload.get("windows"), dict) else {}
    keep_keys = sorted(str(key) for key in windows.keys())[-7:]
    payload["windows"] = {key: windows[key] for key in keep_keys if key in windows}
    return payload


def collect_night_median_baskets(organization_id: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not _night_median_collecting_now():
        return {"collected": False, "reason": "night_median_not_collecting"}
    now = _utc_now()
    window_key = _night_window_key(now.astimezone(ZoneInfo(_night_timezone_name())))
    observations = []
    for row in rows:
        if not _night_median_enabled_for_row(row) or not _has_explicit_strategy(row):
            continue
        # Collection precedes execution, so it needs the same product/strategy
        # eligibility checks, but must not depend on a price interval being due.
        if not row.get("settings", {}).get("automationEnabled", True) or row.get("meta", {}).get("status") in {"manual", "warmup"}:
            continue
        frontend_id, strategy_id = _resolve_strategy_id(row)
        if frontend_id == "illiquid" or FRONTEND_STRATEGY_BY_ID.get(frontend_id, {}).get("status") not in {"active", "special"}:
            continue
        article_id = str((row.get("meta") or {}).get("articleId") or "")
        nm_id = (row.get("meta") or {}).get("nmId")
        baskets = _row_baskets_count(row)
        scope = (row.get("meta") or {}).get("accountScope")
        baseline = (row.get("meta") or {}).get("wbPriceBaseline")
        if isinstance(baseline, dict) and (baseline.get("nmId"), baseline.get("articleId")) != (nm_id, article_id):
            continue
        observed = _night_observed_at(row)
        if not article_id or type(nm_id) is not int or not scope or baskets is None or not _night_observation_fresh(row) or not _night_contains(window_key, observed):
            continue
        observation_id = checksum([organization_id, scope, nm_id, window_key, observed.isoformat()])
        observations.append((article_id, nm_id, scope, {"at": observed.isoformat(), "baskets": baskets,
            "observationId": observation_id, "collectedAt": now.isoformat()}))
    if not observations:
        return {"collected": False, "reason": "no_valid_fresh_night_observations", "skuCount": 0}

    def merge(payload):
        window = payload.setdefault("windows", {}).setdefault(window_key, {"windowKey": window_key,
            "windowLabel": _night_window_label(), "timezone": _night_timezone_name(), "samples": {}, "applied": {}})
        collected = 0
        for article_id, nm_id, scope, observation in observations:
            samples = window.setdefault("samples", {})
            item = samples.get(article_id)
            if not isinstance(item, dict) or (item.get("nmId"), item.get("organizationId"), item.get("accountScope")) != (nm_id, organization_id, scope):
                item = {"articleId": article_id, "nmId": nm_id, "organizationId": organization_id, "accountScope": scope, "values": []}
            values = [value for value in (item.get("values") or []) if isinstance(value, dict)
                and type(value.get("baskets")) is int and value["baskets"] >= 0
                and (at := utc_timestamp(value.get("at"))) is not None and _night_contains(window_key, at)
                and value.get("observationId") == checksum([organization_id, scope, nm_id, window_key, at.isoformat()])]
            values = list({value["observationId"]: value for value in values}.values())
            if any(value.get("observationId") == observation["observationId"] for value in values):
                continue
            values.append(observation)
            item["values"] = sorted(values, key=lambda value: value["at"])[-24:]
            item["lastAt"] = observation["at"]
            item["medianBaskets"] = float(median(value["baskets"] for value in item["values"]))
            samples[article_id] = item
            collected += 1
        payload.update(schemaVersion=2, lastCollectedAt=now.isoformat(), activeWindowKey=window_key)
        return {"collected": True, "windowKey": window_key, "skuCount": collected}
    try:
        return mutate_night_cache(organization_id, merge)
    except DispatchPersistenceError:
        return {"collected": False, "reason": "night_observation_storage_unavailable", "skuCount": 0}


def _night_median_apply_payload(row: dict[str, Any], organization_id: int | None) -> dict[str, Any] | None:
    if organization_id is None or _night_median_collecting_now() or not _night_median_enabled_for_row(row):
        return None
    article_id = str((row.get("meta") or {}).get("articleId") or "")
    if not article_id:
        return None
    payload = _night_median_cache(organization_id)
    window_key = _night_previous_window_key()
    window = (payload.get("windows") or {}).get(window_key)
    if not isinstance(window, dict):
        return None
    applied = (window.get("applied") or {}).get(article_id)
    if isinstance(applied, dict) and applied.get("priceConfirmed") is True and applied.get("accountScope") == (row.get("meta") or {}).get("accountScope"):
        return None
    sample = (window.get("samples") or {}).get(article_id)
    meta = row.get("meta") or {}
    if not isinstance(sample, dict) or not meta.get("accountScope") or (sample.get("nmId"), sample.get("organizationId"), sample.get("accountScope")) != (meta.get("nmId"), organization_id, meta["accountScope"]):
        return None
    values = sample.get("values") if isinstance(sample, dict) else None
    if not isinstance(values, list) or not values:
        return None
    baskets_values = []
    seen = set()
    for item in values:
        if not isinstance(item, dict) or type(item.get("baskets")) is not int or item["baskets"] < 0:
            continue
        at = utc_timestamp(item.get("at"))
        expected_id = checksum([organization_id, meta["accountScope"], meta.get("nmId"), window_key, at.isoformat()]) if at else None
        if at is None or not _night_contains(window_key, at) or item.get("observationId") != expected_id or expected_id in seen:
            continue
        seen.add(expected_id)
        baskets_values.append(item["baskets"])
    if not baskets_values:
        return None
    median_baskets = float(median(baskets_values))
    current_baskets = _row_baskets_count(row)
    if current_baskets is None or not _night_observation_fresh(row):
        return None
    base_delta = _night_target_delta_pct()
    delta_pct = base_delta if current_baskets >= median_baskets else -base_delta
    return {
        "windowKey": window_key,
        "medianBaskets": median_baskets,
        "currentBaskets": current_baskets,
        "samplesCount": len(baskets_values),
        "deltaPct": delta_pct,
        "accountScope": meta["accountScope"],
        "nmId": meta.get("nmId"),
    }


def _execute_single_sku(
    row: dict[str, Any],
    *,
    client: WbApiClient,
    actor_id: str,
    actor_role: str,
    options: StrategyExecuteOptions,
    organization_id: int | None = None,
    all_rows: list[dict[str, Any]] | None = None,
    apply_context: dict[str, Any] | None = None,
) -> StrategyExecuteSkuResult:
    meta = row["meta"]
    settings = row["settings"]
    article_id = str(meta["articleId"])
    old_price = int(meta["currentPriceKopecks"])
    nm_id = meta.get("nmId")

    if not options.force:
        if not bool(settings.get("automationEnabled", True)):
            return StrategyExecuteSkuResult(articleId=article_id, status="skipped", skipReason="automation_disabled")
        if meta.get("status") == "warmup":
            return StrategyExecuteSkuResult(articleId=article_id, status="skipped", skipReason="warmup")
        if meta.get("status") == "manual":
            return StrategyExecuteSkuResult(articleId=article_id, status="skipped", skipReason="manual_mode")

    if nm_id is None:
        return StrategyExecuteSkuResult(articleId=article_id, status="skipped", skipReason="no_nm_id")

    frontend_id, strategy_id = _resolve_strategy_id(row)
    if not options.force and frontend_id != "illiquid" and not _has_explicit_strategy(row):
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="skipped",
            skipReason="no_strategy",
        )

    if frontend_id is None:
        return StrategyExecuteSkuResult(articleId=article_id, status="skipped", skipReason="no_strategy")

    if FRONTEND_STRATEGY_BY_ID[frontend_id].get("status") == "disabled":
        return StrategyExecuteSkuResult(articleId=article_id, status="blocked", frontendStrategyId=frontend_id,
            blockedReasons=["strategy_disabled"], explanation="Стратегия отключена в каталоге")

    if not options.force and frontend_id != "illiquid" and _should_enforce_strategy_interval(options):
        not_due, next_at, interval_minutes = _strategy_interval_not_due(article_id, settings, organization_id)
        if not_due:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="skipped",
                skipReason="strategy_interval_not_due",
                frontendStrategyId=frontend_id,
                strategyId=strategy_id,
                explanation=f"Индивидуальный интервал SKU {interval_minutes} мин: следующий шаг после {next_at.isoformat() if next_at else 'позже'}.",
                oldPriceKopecks=old_price,
            )

    if frontend_id != "illiquid" and _night_median_collecting_now() and _night_median_enabled_for_row(row):
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="skipped",
            skipReason="night_median_collecting",
            frontendStrategyId="night_price_mode",
            strategyId="night_price_mode",
            explanation=f"Ночная медиана собирает корзины в окне {_night_window_label()} МСК; цены ночью не меняются.",
            oldPriceKopecks=old_price,
        )

    night_apply_payload = _night_median_apply_payload(row, organization_id)
    if frontend_id != "illiquid" and strategy_id != "night_price_mode" and night_apply_payload:
        frontend_id = "night_price_mode"
        strategy_id = "night_price_mode"
    elif strategy_id == "night_price_mode":
        night_apply_payload = _night_median_apply_payload(row, organization_id)

    required_blockers = _strategy_data_blockers(row, frontend_id)
    if required_blockers:
        days = (row.get("analytics") or {}).get("periodDays")
        messages = {
            "stock_missing": "Нет подтверждённого доступного остатка WB",
            "ordersUnits_required": "Нет подтверждённых заказов за период анализа",
            "periodDays_required": "Неизвестна длительность периода заказов",
            "baskets_required": "Нет подтверждённых корзин WB",
            "orders_plan_required": "Не задан допустимый план заказов",
            "basket_norm_required": "Не задана допустимая норма корзин",
            "comparison_zero_baseline": "Среднее заказов за 7 дней равно нулю; индекс не определён",
            "main_orders_period_required": f"Нет полных заказов за {'3 завершённых дня' if frontend_id == 'optimal_price' else 'последний завершённый день'}",
            "comparison_orders_period_required": "Нет полных заказов за 7 завершённых дней",
            "hourly_sales_required": "Нет пригодной почасовой истории заказов",
            "todayHourlyOrders_required": "Нет подтверждённых заказов текущего интервала",
            "today_hourly_coverage_required": "Почасовая статистика не покрывает текущий интервал",
        }
        details = [{"code": code, "message": messages.get(code,
            f"Обязательный источник данных неполный, устарел или недоступен: {code}"), "periodDays": days}
            for code in required_blockers]
        return StrategyExecuteSkuResult(articleId=article_id, status="blocked", frontendStrategyId=frontend_id,
            strategyId=strategy_id, oldPriceKopecks=old_price, blockedReasons=required_blockers,
            explanation="; ".join(detail["message"] for detail in details), blockerDetails=details)

    economics = _economics_from_row(row)
    min_price, _ = _effective_pmin(row)
    p_max = _settings_pmax_kopecks(settings, old_price)
    if p_max < min_price:
        return StrategyExecuteSkuResult(articleId=article_id, status="blocked", frontendStrategyId=frontend_id,
            strategyId=strategy_id, oldPriceKopecks=old_price, blockedReasons=["price_bounds_conflict"],
            explanation="P_MIN превышает P_MAX; проверьте экономические входы и ценовые ограничения")
    if strategy_id == "night_price_mode" and night_apply_payload is None:
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="skipped",
            skipReason="night_mode_disabled",
            frontendStrategyId=frontend_id,
            strategyId=strategy_id,
            explanation=f"Ночная медиана ждёт завершённое ночное окно {_night_window_label()} МСК и подтверждённые snapshots корзин.",
            oldPriceKopecks=old_price,
        )

    explanation = ""
    recommended: int | None = None
    blocked_reasons: list[str] = []
    guard_checked = False
    strategy_min = strategy_max = None

    if frontend_id == "illiquid":
        recommended, explanation, blocked_reasons = _illiquid_recommended_price(row)
        if recommended is None:
            skip = "liquidation_not_due" if "liquidation_not_due" in blocked_reasons else "no_price_change"
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="skipped",
                skipReason=skip,  # type: ignore[assignment]
                frontendStrategyId=frontend_id,
                explanation=explanation,
                blockedReasons=blocked_reasons,
                oldPriceKopecks=old_price,
            )
    elif frontend_id == "stockout_guard":
        recommended, explanation = _stockout_guard_recommended_price(row)
    elif frontend_id == "turnover_control":
        recommended, explanation = _turnover_recommended_price(row)
    elif frontend_id == "plan_fact_daily":
        recommended, explanation = _plan_fact_recommended_price(row, min_price, metric="orders")
    elif frontend_id == "plan_fact_period":
        configured_metric = str(_algorithm_settings().get("planFactMetric") or settings.get("repricerMode") or "orders")
        metric: Literal["orders", "revenue", "margin"] = "revenue" if configured_metric == "revenue" else ("margin" if configured_metric == "margin" else "orders")
        recommended, explanation = _plan_fact_recommended_price(row, min_price, metric=metric)
    elif frontend_id == "plan_fact_group":
        recommended, explanation, blocked_reasons = _plan_fact_group_recommended_price(row, min_price, all_rows=all_rows, context=apply_context)
        if blocked_reasons:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="blocked",
                frontendStrategyId=frontend_id,
                explanation=explanation,
                oldPriceKopecks=old_price,
                blockedReasons=blocked_reasons,
                blockerDetails=[{"code": code, "message": explanation} for code in blocked_reasons],
            )
    elif frontend_id == "plan_fact_interval":
        recommended, explanation, blocked_reasons = _plan_fact_interval_recommended_price(row, min_price)
        if blocked_reasons:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="blocked",
                frontendStrategyId=frontend_id,
                explanation=explanation,
                oldPriceKopecks=old_price,
                blockedReasons=blocked_reasons,
                blockerDetails=[{"code": code, "message": explanation} for code in blocked_reasons],
            )
    elif frontend_id == "optimal_price":
        recommended, explanation = _optimal_price_recommended_price(row)
    elif frontend_id == "schedule":
        recommended, explanation, blocked_reasons = _schedule_recommended_price(row)
        if blocked_reasons:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="blocked",
                frontendStrategyId=frontend_id,
                explanation=explanation,
                oldPriceKopecks=old_price,
                blockedReasons=blocked_reasons,
                blockerDetails=[{"code": code, "message": explanation} for code in blocked_reasons],
            )
    elif frontend_id == "cross_marketplace":
        recommended, explanation, blocked_reasons = _competitor_recommended_price(row)
        if blocked_reasons:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="blocked",
                frontendStrategyId=frontend_id,
                explanation=explanation,
                oldPriceKopecks=old_price,
                blockedReasons=blocked_reasons,
                blockerDetails=[{"code": code, "message": explanation} for code in blocked_reasons],
            )
    elif frontend_id == "baskets_orders" and str(_algorithm_settings().get("basketSignalMode") or "matrix") == "thresholds":
        recommended, explanation = _basket_threshold_recommended_price(row)
    elif frontend_id in EXTERNAL_STRATEGY_BLOCKERS:
        code, message = EXTERNAL_STRATEGY_BLOCKERS[frontend_id]
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="blocked",
            frontendStrategyId=frontend_id,
            explanation=message,
            oldPriceKopecks=old_price,
            blockedReasons=[code],
            blockerDetails=[{"code": code, "message": message}],
        )
    elif strategy_id == "revenue_dynamics_4600":
        recommended, _delta_pct, _delta_kopecks, explanation = _revenue_recommended_price(row, economics)
        _band_delta, rub_floor = _revenue_band_delta(_revenue_index(row))
        if rub_floor:
            if recommended > old_price:
                strategy_min = old_price + rub_floor
            elif recommended < old_price:
                strategy_max = old_price - rub_floor
        guard_payload = build_price_recommendation(
            client=client,
            payload=PriceRecommendationRequest(
                scenario=options.scenario,
                articleId=article_id,
                nmId=int(nm_id),
                candidateSellerPriceKopecks=recommended,
                strategyMinPriceKopecks=strategy_min,
                strategyMaxPriceKopecks=strategy_max,
                minPriceKopecks=min_price,
                pMaxKopecks=p_max,
                economics=economics,
            ),
            sku_row=row,
        )
        blocked_reasons = _guard_reason_codes(guard_payload.guardReport)
        guard_checked = True
        if not guard_payload.guardReport.canApply:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="blocked",
                frontendStrategyId=frontend_id,
                strategyId=strategy_id,
                explanation=explanation,
                oldPriceKopecks=old_price,
                recommendedPriceKopecks=recommended,
                deltaKopecks=recommended - old_price,
                blockedReasons=blocked_reasons,
                blockerDetails=_guard_detail_payload(guard_payload.guardReport),
            )
        recommended = guard_payload.normalized.candidateSellerPriceKopecks
    else:
        dry_item = _build_dry_run_item(
            row,
            economics,
            night_delta_pct=float(night_apply_payload["deltaPct"]) if night_apply_payload else None,
        )
        if dry_item is None:
            return StrategyExecuteSkuResult(articleId=article_id, status="skipped", skipReason="no_nm_id")

        config_override = PRESET_CONFIG_OVERRIDES.get(frontend_id or "", {})
        if strategy_id == "revenue_dynamics_4600":
            config_override = {
                **config_override,
                "mode": "percent_rub_floor",
                "maxStepPct": 5.0,
            }
        if strategy_id == "night_price_mode":
            config_override = {
                **config_override,
                **_night_strategy_config_override(),
            }

        dry_result = run_strategy_item_with_guards(
            strategy_id=strategy_id,
            client=client,
            scenario=options.scenario,
            item=dry_item,
            config_override=config_override,
            min_price_kopecks=min_price,
            p_max_kopecks=p_max,
            execution_mode=True,
            sku_row=row,
        )
        explanation = _night_median_apply_explanation(night_apply_payload) if strategy_id == "night_price_mode" else dry_result.explanation
        recommended = dry_result.recommendedSellerPriceKopecks
        blocked_reasons = list(dry_result.blockedReasons)
        guard_checked = True
        if not dry_result.canCreateProductionDraft:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="blocked",
                frontendStrategyId=frontend_id,
                strategyId=strategy_id,
                explanation=explanation,
                oldPriceKopecks=old_price,
                recommendedPriceKopecks=recommended,
                deltaKopecks=dry_result.deltaKopecks,
                blockedReasons=blocked_reasons,
                blockerDetails=list(dry_result.guardDetails),
            )

    if recommended is not None and recommended != old_price and not guard_checked:
        rounded_recommended, rounding_note = _apply_price_rounding(recommended, current=old_price, settings=settings)
        if rounded_recommended != recommended:
            recommended = rounded_recommended
            if rounding_note:
                explanation = f"{explanation}; {rounding_note}"

    if recommended is not None and recommended != old_price and not guard_checked:
        clamped_recommended, clamp_notes = _clamp_candidate_price(
            current=old_price,
            recommended=recommended,
            min_price=min_price,
            p_max=p_max,
        )
        if clamped_recommended != recommended:
            explanation = f"{explanation}; {'; '.join(clamp_notes)}"
            recommended = clamped_recommended

    if recommended is not None and recommended != old_price and not guard_checked:
        guard_payload = build_price_recommendation(
            client=client,
            payload=PriceRecommendationRequest(
                scenario=options.scenario,
                articleId=article_id,
                nmId=int(nm_id),
                candidateSellerPriceKopecks=recommended,
                minPriceKopecks=min_price,
                pMaxKopecks=p_max,
                economics=economics,
            ),
            sku_row=row,
        )
        blocked_reasons = _guard_reason_codes(guard_payload.guardReport)
        if not guard_payload.guardReport.canApply:
            return StrategyExecuteSkuResult(
                articleId=article_id,
                status="blocked",
                frontendStrategyId=frontend_id,
                strategyId=strategy_id,
                explanation=explanation,
                oldPriceKopecks=old_price,
                recommendedPriceKopecks=recommended,
                deltaKopecks=recommended - old_price,
                blockedReasons=blocked_reasons,
                blockerDetails=_guard_detail_payload(guard_payload.guardReport),
            )

        recommended = guard_payload.normalized.candidateSellerPriceKopecks

    if recommended is None or recommended == old_price:
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="skipped",
            skipReason="no_price_change",
            frontendStrategyId=frontend_id,
            strategyId=strategy_id,
            explanation=explanation or "No price change required",
            oldPriceKopecks=old_price,
            recommendedPriceKopecks=recommended,
        )

    if not options.createDrafts:
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="executed",
            frontendStrategyId=frontend_id,
            strategyId=strategy_id,
            explanation=explanation,
            oldPriceKopecks=old_price,
            recommendedPriceKopecks=recommended,
            deltaKopecks=recommended - old_price,
        )

    if options.applyPrices and apply_context is not None and apply_context.get("wb_rate_limited"):
        message = "WB price upload rate limit is active for this run; retry on the next scheduler cycle"
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="blocked",
            frontendStrategyId=frontend_id,
            strategyId=strategy_id,
            explanation=message,
            oldPriceKopecks=old_price,
            recommendedPriceKopecks=recommended,
            deltaKopecks=recommended - old_price,
            blockedReasons=["wb_rate_limited"],
            blockerDetails=[{"code": "wb_rate_limited", "message": message}],
        )

    draft_request = PriceDraftCreateRequest(
        scenario=options.scenario,
        articleId=article_id,
        nmId=int(nm_id),
        candidateSellerPriceKopecks=recommended,
        minPriceKopecks=min_price,
        pMaxKopecks=p_max,
        economics=economics,
        reason=explanation,
        strategyMinPriceKopecks=strategy_min,
        strategyMaxPriceKopecks=strategy_max,
        expectedSettingsRevision=meta.get("pricingSettingsRevision"),
        strategySourceEvidence=(row.get("analytics") or {}).get(
            "nightStrategySourceEvidence" if strategy_id == "night_price_mode" else "strategySourceEvidence") or [],
        nightWindowKey=night_apply_payload.get("windowKey") if night_apply_payload else None,
    )
    draft = create_price_draft(
        actor_id=actor_id,
        actor_role=actor_role,
        client=client,
        payload=draft_request,
        sku_row=row,
        organization_id=organization_id,
    )
    if draft.state == "blocked":
        return StrategyExecuteSkuResult(
            articleId=article_id,
            status="blocked",
            frontendStrategyId=frontend_id,
            strategyId=strategy_id,
            explanation=explanation,
            oldPriceKopecks=old_price,
            recommendedPriceKopecks=recommended,
            deltaKopecks=recommended - old_price,
            blockedReasons=_guard_reason_codes(draft.guardReport),
            blockerDetails=_guard_detail_payload(draft.guardReport),
            draftId=draft.draftId,
        )

    draft_id = draft.draftId
    recommended = draft.normalized.candidateSellerPriceKopecks
    draft_request = draft_request.model_copy(update={"candidateSellerPriceKopecks": recommended,
        "expectedPriceBaseline": draft.sourceSnapshot.priceBaseline})
    job_id: str | None = None
    apply_state: str | None = None
    effective_auto_approve = bool(options.autoApprove)
    effective_apply_prices = bool(options.applyPrices)
    if strategy_id == "night_price_mode":
        night_auto = _algorithm_bool_setting("nightMedianAutoApplyEnabled", False)
        effective_auto_approve = effective_auto_approve and night_auto
        effective_apply_prices = effective_apply_prices and night_auto

    if effective_auto_approve:
        approval_ref = options.approvalRef or f"strategy-exec-{uuid4().hex[:12]}"
        approve_draft(draft_id=draft_id, actor_id=actor_id, request=ApproveDraftRequest(approvalRef=approval_ref))

        if effective_apply_prices:
            job = apply_approved_draft(
                draft_id=draft_id,
                client=client,
                real_apply_enabled=get_settings().real_price_apply_enabled,
                local_apply_enabled=get_settings().repricer_local_price_apply_enabled,
                request=ApplyDraftRequest(scenario=options.scenario),
            )
            job_id = job.jobId
            apply_state = job.state
            if job.state in {"accepted", "local_applied"}:
                apply_mode = job.lastApplyResult.applyMode
                if frontend_id == "illiquid":
                    _commit_illiquid_runtime_step(article_id, recommended, explanation)
                record_repricer_price_change(
                    article_id=article_id,
                    sku_name=str(meta.get("name") or article_id),
                    old_price_kopecks=old_price,
                    new_price_kopecks=recommended,
                    trigger="night_median_up" if strategy_id == "night_price_mode" else "algorithm",
                    strategy_name=_night_strategy_name(strategy_id, frontend_id, row),
                    reason=explanation,
                    source=apply_mode,
                    organization_id=organization_id,
                )
                return StrategyExecuteSkuResult(
                    articleId=article_id,
                    status="executed",
                    frontendStrategyId=frontend_id,
                    strategyId=strategy_id,
                    explanation=explanation,
                    oldPriceKopecks=old_price,
                    recommendedPriceKopecks=recommended,
                    deltaKopecks=recommended - old_price,
                    draftId=draft_id,
                    jobId=job_id,
                    applyState=apply_state or apply_mode,
                    priceConfirmed=bool(job.lastApplyResult.priceConfirmed),
                )
            if job.state in {"blocked", "failed", "needs_attention"}:
                if "wb_rate_limited" in set(job.blockerIds):
                    if apply_context is not None:
                        apply_context["wb_rate_limited"] = True
                    apply_context_message = (
                        job.notes[0]
                        if getattr(job, "notes", None)
                        else "WB price upload rate limit reached"
                    )
                else:
                    apply_context_message = None
                return StrategyExecuteSkuResult(
                    articleId=article_id,
                    status="blocked" if job.state == "blocked" else "failed",
                    frontendStrategyId=frontend_id,
                    strategyId=strategy_id,
                    explanation=explanation,
                    oldPriceKopecks=old_price,
                    recommendedPriceKopecks=recommended,
                    deltaKopecks=recommended - old_price,
                    draftId=draft_id,
                    jobId=job_id,
                    applyState=apply_state,
                    blockedReasons=list(job.blockerIds),
                    blockerDetails=[
                        {
                            "code": str(code),
                            **({"message": apply_context_message} if code == "wb_rate_limited" and apply_context_message else {}),
                        }
                        for code in job.blockerIds
                    ],
                )
    elif apply_context is not None:
        pending = apply_context.setdefault("pending_price_approvals", [])
        pending.append(
            {
                "approvalId": draft_id,
                "draftId": draft_id,
                "articleId": article_id,
                "nmId": int(nm_id),
                "status": "pending",
                "source": "worker",
                "scenario": options.scenario,
                "frontendStrategyId": frontend_id,
                "strategyId": strategy_id,
                "strategyName": _night_strategy_name(strategy_id, frontend_id, row),
                "oldPriceKopecks": old_price,
                "recommendedPriceKopecks": recommended,
                "deltaKopecks": recommended - old_price,
                "explanation": explanation,
                "draftRequest": draft_request.model_dump(mode="json"),
                "nightWindowKey": draft_request.nightWindowKey,
            }
        )

    return StrategyExecuteSkuResult(
        articleId=article_id,
        status="executed",
        frontendStrategyId=frontend_id,
        strategyId=strategy_id,
        explanation=explanation,
        oldPriceKopecks=old_price,
        recommendedPriceKopecks=recommended,
        deltaKopecks=recommended - old_price,
        draftId=draft_id,
        jobId=job_id,
        applyState=apply_state or (None if effective_auto_approve else "approval_required"),
    )


def preview_repricer_strategies(
    article_ids: list[str],
    *,
    client: WbApiClient,
    actor_id: str,
    actor_role: str,
    wb_token: str | None = None,
    scenario: str = "complete",
    sku_rows: list[dict[str, Any]] | None = None,
    force: bool = False,
    organization_id: int | None = None,
    strategy_id_or_name: str | None = None,
    config: dict[str, Any] | None = None,
) -> StrategyExecuteReport:
    if strategy_id_or_name is not None:
        # Exercise normal assignment validation/normalization against disposable
        # state, then calculate prospective rows without saving any assignment.
        rows = deepcopy(sku_rows if sku_rows is not None else list_repricer_skus(scenario, wb_token=wb_token))
        transient_state: dict[str, Any] = {}
        assignment = apply_frontend_strategy_assignment(
            article_ids, strategy_id_or_name=strategy_id_or_name, scenario=scenario,
            wb_token=wb_token, sku_rows=rows, config=config, state=transient_state,
        )
        assigned = {item["articleId"]: item for item in assignment["items"]}
        for row in rows:
            item = assigned.get(row["meta"]["articleId"])
            if item is None:
                continue
            row["strategy"] = {**deepcopy(FRONTEND_STRATEGY_BY_ID[item["strategyId"]]),
                "config": item["config"], "assignmentSource": item["assignmentSource"], "assignedAt": item["assignedAt"]}
            row["meta"].update(transient_state["skuMetaOverrides"][item["articleId"]])
            row["settings"].update(transient_state["skuSettingsOverrides"][item["articleId"]])
            row["_previewLiquidationRuntime"] = transient_state.get("liquidationActive", {}).get(item["articleId"])
        sku_rows = rows
    return execute_repricer_strategies(
        article_ids,
        client=client,
        actor_id=actor_id,
        actor_role=actor_role,
        wb_token=wb_token,
        options=StrategyExecuteOptions(
            scenario=scenario,
            createDrafts=False,
            autoApprove=False,
            applyPrices=False,
            simulateLocalPrice=False,
            force=force,
        ),
        sku_rows=sku_rows,
        organization_id=organization_id,
        trigger="preview",
        persist_run=False,
    )


@with_organization_algorithm
def execute_repricer_strategies(
    article_ids: list[str],
    *,
    client: WbApiClient,
    actor_id: str,
    actor_role: str,
    wb_token: str | None = None,
    options: StrategyExecuteOptions | None = None,
    sku_rows: list[dict[str, Any]] | None = None,
    organization_id: int | None = None,
    trigger: str = "manual",
    persist_run: bool = True,
) -> StrategyExecuteReport:
    opts = options or StrategyExecuteOptions()
    if (opts.createDrafts or opts.applyPrices) and not is_auto_strategy_execution_allowed(force=opts.force):
        return StrategyExecuteReport(
            runId=f"exec_{uuid4().hex}",
            executedCount=0,
            skippedCount=len(article_ids),
            blockedCount=0,
            items=[
                StrategyExecuteSkuResult(
                    articleId=article_id,
                    status="skipped",
                    skipReason="manual_mode",
                    explanation="Control plane strategyMode is manual",
                )
                for article_id in article_ids
            ],
        )
    rows = sku_rows if sku_rows is not None else list_repricer_skus(opts.scenario, wb_token=wb_token)
    unique_ids = list(dict.fromkeys(article_ids))
    if organization_id is not None and wb_token:
        rows = prepare_strategy_rows(rows, org=organization_id, token=wb_token,
            algorithm=ALGORITHM_SETTINGS_STATE,
            default_days=max(1, int(getattr(get_settings(), "repricer_wb_sync_period_days", 7) or 7)),
            now=_utc_now(), queue_missing=opts.createDrafts, article_ids=unique_ids)
    scope = binding_key(client, organization_id) or f"offline:{organization_id}"
    verified_rows = [{**row, "meta": {**row["meta"], "accountScope": scope}} for row in rows
                     if row.get("meta", {}).get("accountScope") in {None, scope}]
    row_index = {row["meta"]["articleId"]: row for row in verified_rows}
    selected_rows = [row_index[article_id] for article_id in unique_ids if article_id in row_index]
    if organization_id is not None and not opts.force and opts.createDrafts:
        collect_night_median_baskets(organization_id, selected_rows)

    results: list[StrategyExecuteSkuResult] = []
    apply_context: dict[str, Any] = {"group_row_index": row_index}
    for article_id in unique_ids:
        row = row_index.get(article_id)
        if row is None:
            results.append(
                StrategyExecuteSkuResult(
                    articleId=article_id,
                    status="skipped",
                    skipReason="sku_not_found",
                )
            )
            continue
        results.append(
            _execute_single_sku(
                row,
                client=client,
                actor_id=actor_id,
                actor_role=actor_role,
                options=opts,
                organization_id=organization_id,
                all_rows=verified_rows,
                apply_context=apply_context,
            )
        )

    executed = sum(1 for item in results if item.status == "executed")
    skipped = sum(1 for item in results if item.status == "skipped")
    blocked = sum(1 for item in results if item.status in {"blocked", "failed"})
    report = StrategyExecuteReport(
        runId=f"exec_{uuid4().hex}",
        executedCount=executed,
        skippedCount=skipped,
        blockedCount=blocked,
        items=results,
    )
    if persist_run and organization_id is not None:
        pending_approvals = list(apply_context.get("pending_price_approvals") or [])
        existing_pending_keys = {
            (str(item.get("articleId") or ""), item.get("recommendedPriceKopecks"))
            for item in pending_approvals
            if isinstance(item, dict)
        }
        for item in results:
            if item.applyState != "approval_required" or item.recommendedPriceKopecks is None:
                continue
            pending_key = (str(item.articleId or ""), item.recommendedPriceKopecks)
            if pending_key in existing_pending_keys:
                continue
            row = next(
                (
                    row
                    for row in rows
                    if str((row.get("meta") or {}).get("articleId") or "") == str(item.articleId)
                ),
                {},
            )
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            pending_approvals.append(
                {
                    "approvalId": item.draftId or f"price-apr-{uuid4().hex}",
                    "draftId": item.draftId,
                    "articleId": item.articleId,
                    "nmId": meta.get("nmId"),
                    "name": meta.get("name"),
                    "skuName": meta.get("name"),
                    "productName": meta.get("name"),
                    "status": "pending",
                    "source": "worker",
                    "scenario": opts.scenario,
                    "frontendStrategyId": item.frontendStrategyId,
                    "strategyId": item.strategyId,
                    "strategyName": _night_strategy_name(item.strategyId, item.frontendStrategyId, row),
                    "oldPriceKopecks": item.oldPriceKopecks,
                    "recommendedPriceKopecks": item.recommendedPriceKopecks,
                    "deltaKopecks": item.deltaKopecks,
                    "explanation": item.explanation,
                }
            )
            existing_pending_keys.add(pending_key)
        if pending_approvals:
            upsert_pending_price_approvals(
                organization_id=organization_id,
                approvals=[
                    {
                        **approval,
                        "runId": report.runId,
                        "trigger": trigger,
                    }
                    for approval in pending_approvals
                ],
            )
        append_execution_run(
            organization_id=organization_id,
            run_id=report.runId,
            trigger=trigger,
            report_payload=report.model_dump(mode="json"),
        )
    return report


def execute_all_assigned_skus(
    *,
    client: WbApiClient,
    actor_id: str,
    actor_role: str,
    wb_token: str | None = None,
    options: StrategyExecuteOptions | None = None,
    sku_rows: list[dict[str, Any]] | None = None,
    organization_id: int | None = None,
    trigger: str = "scheduler",
    persist_run: bool = True,
) -> StrategyExecuteReport:
    opts = options or StrategyExecuteOptions()
    rows = sku_rows if sku_rows is not None else list_repricer_skus(opts.scenario, wb_token=wb_token)
    article_ids = [
        row["meta"]["articleId"]
        for row in rows
        if bool(row["settings"].get("automationEnabled", True))
        and (
            (row["meta"].get("status") == "auto" and _has_explicit_strategy(row))
            or _is_liquidation_scheduled(row)
        )
    ]
    return execute_repricer_strategies(
        article_ids,
        client=client,
        actor_id=actor_id,
        actor_role=actor_role,
        wb_token=wb_token,
        options=opts,
        sku_rows=rows,
        organization_id=organization_id,
        trigger=trigger,
        persist_run=persist_run,
    )
