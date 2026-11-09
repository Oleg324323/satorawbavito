"""Detached legacy calculation service; no authorization or price writer.

Arithmetic is copied from repricer_execution at c94bc3f without correcting its
float/rounding behavior. This is a calculation-only migration seam, not a new
financial policy. Loading dated facts, validating source completeness and all
apply guards remain mandatory responsibilities of the future assembly service.
"""

# Preserve the extracted legacy arithmetic and timestamp spelling for parity.
# ruff: noqa: RUF046, FURB162

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal
from zoneinfo import ZoneInfo


def _freeze(value):
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("calculation mapping keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("unsupported calculation input value")


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CalculationReference:
    """An actual upstream revision reference, never an inferred source version."""

    role: str
    version: str
    checksum: str

    def __post_init__(self):
        for value in (self.role, self.version):
            if type(value) is not str or not value or value.strip() != value:
                raise ValueError("exact nonblank calculation reference required")
        if (
            type(self.checksum) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.checksum) is None
        ):
            raise ValueError("reference checksum must be lowercase SHA256")


@dataclass(frozen=True, slots=True)
class CalculationContext:
    organization_id: int
    marketplace_account_id: int
    catalog_sku_id: int
    as_of: datetime
    references: tuple[CalculationReference, ...]
    row: Mapping[str, Any]
    algorithm: Mapping[str, Any]
    liquidation: Mapping[str, Any]
    per_update_step_pct: float
    per_day_step_pct: float

    def __post_init__(self):
        for value in (
            self.organization_id,
            self.marketplace_account_id,
            self.catalog_sku_id,
        ):
            if type(value) is not int or not 0 < value <= 2**31 - 1:
                raise ValueError("positive internal INT4 scope required")
        if (
            type(self.as_of) is not datetime
            or self.as_of.tzinfo is None
            or self.as_of.utcoffset() is None
        ):
            raise ValueError("aware calculation instant required")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        if not isinstance(self.references, (list, tuple)) or not self.references:
            raise ValueError("pinned calculation references required")
        if any(type(ref) is not CalculationReference for ref in self.references):
            raise ValueError("typed calculation references required")
        roles = [ref.role for ref in self.references]
        if len(roles) != len(set(roles)):
            raise ValueError("duplicate calculation reference role")
        # Assemblers cannot omit the dated/source ownership dependencies and claim
        # that a detached copy of a legacy row alone is a canonical context.
        if not {
            "settings",
            "mapping",
            "cost",
            "economics",
            "price",
            "stock",
            "history",
        } <= set(roles):
            raise ValueError("required calculation reference missing")
        object.__setattr__(
            self, "references", tuple(sorted(self.references, key=lambda ref: ref.role))
        )
        for name in ("row", "algorithm", "liquidation"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError("calculation mapping required")  # noqa: TRY004
            object.__setattr__(self, name, _freeze(value))
        for value in (self.per_update_step_pct, self.per_day_step_pct):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("finite nonnegative guard limit required")

    @property
    def cache_key(self) -> str:
        payload = {
            "formula": "repricer-execution-legacy/c94bc3f",
            "org": self.organization_id,
            "account": self.marketplace_account_id,
            "sku": self.catalog_sku_id,
            "asOf": self.as_of.isoformat(),
            "references": [
                (ref.role, ref.version, ref.checksum) for ref in self.references
            ],
            "row": _plain(self.row),
            "algorithm": _plain(self.algorithm),
            "liquidation": _plain(self.liquidation),
            "updateLimit": self.per_update_step_pct,
            "dayLimit": self.per_day_step_pct,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()


PLAN_FACT_BANDS = (
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
)


def settings_minimum_price_kopecks(settings: Mapping[str, Any]) -> int:
    """Existing execution arithmetic, including its rounding and fallback rules."""
    def number(key: str) -> float:
        try:
            return float(settings.get(key) if settings.get(key) is not None else 0)
        except (TypeError, ValueError):
            return 0.0

    def amount(key: str) -> int:
        try:
            return int(round(number(key)))
        except (TypeError, ValueError):
            return 0

    try:
        override = int(settings.get("pMinKopecks") or settings.get("pminKopecks") or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return override
    base_cost = (
        amount("cogsKopecks")
        + amount("logisticsKopecks")
        + amount("otherExpensePerSaleKopecks")
        + amount("storageCostPerSaleKopecks")
    )
    pick_pack_pct = number("pickPackCostPercent")
    if pick_pack_pct > 0:
        base_cost += round(amount("cogsKopecks") * pick_pack_pct / 100)
    fixed_margin = amount("minMarginKopecks")
    variable_pct = (
        number("wbCommissionPct")
        + number("minMarginPct")
        + number("taxPct")
        + max(number("promoCostPercent"), number("otherExpensePricePct"))
        + number("advertCostPercent")
    )
    denominator = 1 - variable_pct / 100
    if denominator <= 0:
        return max(1, base_cost + fixed_margin)
    return max(1, int(round((base_cost + fixed_margin) / denominator)))


@dataclass(frozen=True, slots=True)
class LegacyCalculationService:
    context: CalculationContext

    def __post_init__(self):
        if type(self.context) is not CalculationContext:
            raise ValueError("immutable calculation context required")

    def minimum_price(self) -> int:
        return self._settings_pmin_kopecks(self.context.row.get("settings") or {})

    def maximum_price(self) -> int:
        return self._settings_pmax_kopecks(
            self.context.row.get("settings") or {},
            int(self.context.row["meta"]["currentPriceKopecks"]),
        )

    def basket_candidate(self) -> tuple[int, str]:
        return self._basket_threshold_recommended_price(self.context.row)

    def plan_fact_candidate(
        self,
        min_price: int,
        *,
        metric: Literal["orders", "revenue", "margin"] = "orders",
    ) -> tuple[int, str]:
        if metric not in ("orders", "revenue", "margin"):
            raise ValueError("unsupported plan fact metric")
        return self._plan_fact_recommended_price(
            self.context.row, min_price, metric=metric
        )

    def liquidation_candidate(self) -> tuple[int | None, str, tuple[str, ...]]:
        price, explanation, blockers = self._illiquid_recommended_price(
            self.context.row
        )
        return price, explanation, tuple(blockers)

    def interval_candidate(
        self, min_price: int
    ) -> tuple[int | None, str, tuple[str, ...]]:
        price, explanation, blockers = self._plan_fact_interval_recommended_price(
            self.context.row, min_price
        )
        return price, explanation, tuple(blockers)

    def clamp_candidate(
        self, recommended: int, min_price: int, p_max: int | None
    ) -> tuple[int, tuple[str, ...]]:
        price, notes = self._clamp_candidate_price(
            current=int(self.context.row["meta"]["currentPriceKopecks"]),
            recommended=recommended,
            min_price=min_price,
            p_max=p_max,
        )
        return price, tuple(notes)

    def round_candidate(self, candidate: int) -> tuple[int, str | None]:
        return self._apply_price_rounding(
            candidate,
            current=int(self.context.row["meta"]["currentPriceKopecks"]),
            settings=self.context.row.get("settings") or {},
        )

    def _hourly_orders_units(self, row: dict[str, Any], key: str) -> list[int]:
        buckets = (row.get("analytics") or {}).get(key)
        result = [0 for _ in range(24)]
        if not isinstance(buckets, (list, tuple)):
            return result
        for item in buckets:
            if not isinstance(item, Mapping):
                continue
            try:
                hour = int(item.get("hour"))
                orders = int(item.get("ordersUnits") or 0)
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23:
                result[hour] += max(0, orders)
        return result

    def _interval_bounds(self, hour: int, interval_hours: int = 4) -> tuple[int, int]:
        normalized_interval = max(1, min(24, int(interval_hours or 4)))
        start = (max(0, min(23, hour)) // normalized_interval) * normalized_interval
        return start, min(24, start + normalized_interval)

    def _sum_interval(self, values: list[int], start: int, end: int) -> int:
        return sum(values[max(0, start) : min(24, end)])

    def _plan_fact_interval_recommended_price(
        self, row: dict[str, Any], min_price: int
    ) -> tuple[int | None, str, list[str]]:
        current = int(row["meta"]["currentPriceKopecks"])
        config = (row.get("strategy") or {}).get("config") or {}
        if not isinstance(config, Mapping):
            config = {}
        interval_hours = int(
            config.get("intervalHours")
            or self.context.algorithm.get("planFactIntervalHours")
            or 4
        )
        history = self._hourly_orders_units(row, "hourlyOrders")
        history_total = sum(history)
        if history_total <= 0:
            return (
                None,
                "Интервальный план-факт: WB orders не дали почасовую историю за выбранный период",
                ["hourly_sales_required"],
            )

        now_msk = self.context.as_of.astimezone(ZoneInfo("Europe/Moscow"))
        start, end = self._interval_bounds(now_msk.hour, interval_hours)
        interval_history_orders = self._sum_interval(history, start, end)
        weight = interval_history_orders / history_total
        daily_plan = self._daily_plan_orders(row)
        interval_plan = max(1.0, daily_plan * weight)
        today = self._hourly_orders_units(row, "todayHourlyOrders")
        interval_fact = self._sum_interval(today, start, end)
        completion_pct = interval_fact / interval_plan * 100.0
        recommended, base_explanation = self._plan_fact_price_from_completion(
            current,
            min_price,
            completion_pct,
            label=f"Интервал {start:02d}:00-{end:02d}:00 МСК",
        )
        explanation = (
            f"{base_explanation}; вес {weight * 100:.1f}% от дня, "
            f"план {interval_plan:.1f} заказов, факт {interval_fact}"
        )
        return recommended, explanation, []

    def _parse_iso(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            return None

    def _settings_pmin_kopecks(self, settings: dict[str, Any]) -> int:
        return settings_minimum_price_kopecks(settings)

    def _settings_pmax_kopecks(self, settings: dict[str, Any], old_price: int) -> int:
        p_max = self._setting_int(settings, "pMaxKopecks")
        if p_max > 0:
            return p_max
        rrp = self._setting_int(settings, "rrpKopecks")
        target_discount = self._setting_float(settings, "targetDiscountPct")
        if rrp > 0 and target_discount > 0:
            return max(1, int(round(rrp * max(0.01, 1 - target_discount / 100))))
        max_margin_amount = self._setting_int(settings, "maxMarginKopecks")
        max_margin_pct = self._setting_float(settings, "maxMarginPct")
        if max_margin_amount > 0 or max_margin_pct > 0:
            base_cost = (
                self._setting_int(settings, "cogsKopecks")
                + self._setting_int(settings, "logisticsKopecks")
                + self._setting_int(settings, "otherExpensePerSaleKopecks")
                + self._setting_int(settings, "storageCostPerSaleKopecks")
                + max_margin_amount
            )
            denominator = (
                1
                - (
                    self._setting_float(settings, "wbCommissionPct")
                    + self._setting_float(settings, "taxPct")
                    + self._effective_price_expense_pct(settings)
                    + self._setting_float(settings, "advertCostPercent")
                    + max_margin_pct
                )
                / 100
            )
            if denominator > 0:
                return max(1, int(round(base_cost / denominator)))
        return max(1, old_price * 2)

    def _settings_price_step_pct(
        self, settings: dict[str, Any], default: float = 3.0
    ) -> float:
        try:
            value = float(settings.get("priceStepPct") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
        return self._algorithm_float_setting("priceStepPct", default)

    def _settings_step_interval_minutes(self, settings: dict[str, Any]) -> int:
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
        return max(5, int(self.context.algorithm.get("syncIntervalMinutes") or 60))

    def _analytics_number(
        self, row: dict[str, Any], key: str, default: float = 0.0
    ) -> float:
        value = (row.get("analytics") or {}).get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _setting_float(
        self, settings: dict[str, Any], key: str, default: float = 0.0
    ) -> float:
        try:
            return float(
                settings.get(key) if settings.get(key) is not None else default
            )
        except (TypeError, ValueError):
            return default

    def _setting_int(self, settings: dict[str, Any], key: str, default: int = 0) -> int:
        try:
            return int(
                round(
                    float(
                        settings.get(key) if settings.get(key) is not None else default
                    )
                )
            )
        except (TypeError, ValueError):
            return default

    def _setting_bool(
        self, settings: dict[str, Any], key: str, default: bool = False
    ) -> bool:
        raw = settings.get(key)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw != 0
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "да", "on"}

    def _effective_price_expense_pct(self, settings: dict[str, Any]) -> float:
        promo_pct = self._setting_float(settings, "promoCostPercent")
        other_pct = self._setting_float(settings, "otherExpensePricePct")
        return max(promo_pct, other_pct)

    def _orders_units(self, row: dict[str, Any]) -> int:
        return int(self._analytics_number(row, "ordersUnits", 0.0))

    def _apply_pct(self, price_kopecks: int, delta_pct: float) -> int:
        return max(1, int(round(price_kopecks * (1 + delta_pct / 100))))

    def _algorithm_bool_setting(self, key: str, default: bool = False) -> bool:
        raw = self.context.algorithm.get(key)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw != 0
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "да", "on"}

    def _algorithm_float_setting(self, key: str, default: float) -> float:
        try:
            return float(self.context.algorithm.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _pretty_price_kopecks(self, price_kopecks: int, template: Any = None) -> int:
        rub = max(1, int(round(price_kopecks / 100)))
        template_text = str(template or "").strip().lower()
        match = None
        if template_text:
            match = re.search(r"(\d{1,3})\s*$", template_text.replace("x", ""))
        if match:
            suffix = int(match.group(1))
            width = len(match.group(1))
            base = 10**width
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

    def _apply_price_rounding(
        self,
        price_kopecks: int,
        *,
        current: int,
        settings: dict[str, Any] | None = None,
    ) -> tuple[int, str | None]:
        sku_enabled = self._setting_bool(settings or {}, "beautyPriceEnabled", False)
        if not sku_enabled and not self._algorithm_bool_setting(
            "priceRoundingEnabled", False
        ):
            return price_kopecks, None
        rounded = self._pretty_price_kopecks(
            price_kopecks, (settings or {}).get("beautyPriceTemplate")
        )
        if rounded == price_kopecks:
            return price_kopecks, None
        if price_kopecks > current and rounded < current:
            rounded = price_kopecks
        max_change = self._setting_int(settings or {}, "beautyPriceMaxChangeKopecks")
        max_change_pct = self._setting_float(settings or {}, "beautyPriceMaxChangePct")
        if max_change > 0 and abs(rounded - price_kopecks) > max_change:
            return price_kopecks, None
        if (
            max_change_pct > 0
            and abs(rounded - price_kopecks) / max(1, price_kopecks) * 100
            > max_change_pct
        ):
            return price_kopecks, None
        return rounded, f"округлено до красивой цены {round(rounded / 100)} ₽"

    def _clamp_candidate_price(
        self,
        *,
        current: int,
        recommended: int,
        min_price: int,
        p_max: int | None,
    ) -> tuple[int, list[str]]:
        limits = self.context
        lower = max(1, int(min_price or 1))
        upper = int(p_max) if p_max and p_max > 0 else None
        notes: list[str] = []

        if recommended > current:
            step_cap = self._apply_pct(
                current, max(0.0, float(limits.per_update_step_pct))
            )
            if recommended > step_cap:
                recommended = step_cap
                notes.append(
                    f"шаг ограничен guard до +{limits.per_update_step_pct:.0f}%"
                )
            day_cap = self._apply_pct(current, max(0.0, float(limits.per_day_step_pct)))
            if recommended > day_cap:
                recommended = day_cap
                notes.append(f"дневной лимит guard до +{limits.per_day_step_pct:.0f}%")
        elif recommended < current:
            step_floor = self._apply_pct(
                current, -max(0.0, float(limits.per_update_step_pct))
            )
            if recommended < step_floor:
                recommended = step_floor
                notes.append(
                    f"шаг ограничен guard до -{limits.per_update_step_pct:.0f}%"
                )
            day_floor = self._apply_pct(
                current, -max(0.0, float(limits.per_day_step_pct))
            )
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

    def _algorithm_int_setting(self, key: str, default: int) -> int:
        try:
            return int(self.context.algorithm.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _basket_norm_period_days(self) -> int:
        return max(1, self._algorithm_int_setting("basketNormPeriodDays", 7))

    def _row_period_days(self, row: dict[str, Any]) -> int:
        analytics = row.get("analytics") or {}
        return max(
            1,
            int(
                analytics.get("periodDays")
                or self.context.algorithm.get("planFactFactPeriodDays")
                or self._basket_norm_period_days()
            ),
        )

    def _plan_orders_for_row_period(self, row: dict[str, Any]) -> float:
        settings = row.get("settings") or {}
        orders_plan = self._setting_float(settings, "ordersPlanQty")
        if orders_plan > 0:
            plan_days = max(
                1.0,
                self._setting_float(
                    settings, "ordersPlanDays", self._row_period_days(row)
                ),
            )
            return max(1.0, orders_plan * (self._row_period_days(row) / plan_days))
        meta = row["meta"]
        basket_norm = max(1, int(meta.get("basketNorm") or 1))
        return max(
            1.0,
            basket_norm
            * (self._row_period_days(row) / self._basket_norm_period_days()),
        )

    def _daily_plan_orders(self, row: dict[str, Any]) -> float:
        settings = row.get("settings") or {}
        orders_plan = self._setting_float(settings, "ordersPlanQty")
        if orders_plan > 0:
            return max(
                1.0,
                orders_plan
                / max(1.0, self._setting_float(settings, "ordersPlanDays", 1)),
            )
        meta = row["meta"]
        basket_norm = max(1, int(meta.get("basketNorm") or 1))
        return max(1.0, basket_norm / self._basket_norm_period_days())

    def _plan_fact_pct(
        self,
        row: dict[str, Any],
        *,
        metric: Literal["orders", "revenue", "margin"] = "orders",
    ) -> float:
        meta = row["meta"]
        analytics = row.get("analytics") or {}
        plan_orders = self._plan_orders_for_row_period(row)
        if metric == "revenue":
            actual = float(analytics.get("revenueKopecks") or 0)
            plan = max(1.0, float(meta.get("currentPriceKopecks") or 0) * plan_orders)
            return actual / plan * 100
        if metric == "margin":
            actual = float(analytics.get("marginKopecks") or 0) * max(
                1, self._orders_units(row)
            )
            plan = max(
                1.0, float(meta.get("currentPriceKopecks") or 0) * plan_orders * 0.2
            )
            return actual / plan * 100
        return self._orders_units(row) / plan_orders * 100

    def _plan_fact_price_from_completion(
        self,
        current: int,
        min_price: int,
        completion_pct: float,
        *,
        label: str,
    ) -> tuple[int, str]:
        for low, high, delta_pct in PLAN_FACT_BANDS:
            if low <= completion_pct < high:
                if delta_pct is None:
                    return (
                        min_price,
                        f"{label} {completion_pct:.1f}%: ниже 10%, ставим минимальную цену",
                    )
                if delta_pct == 0:
                    return (
                        current,
                        f"{label} {completion_pct:.1f}%: 100-110%, держим базовую цену",
                    )
                return self._apply_pct(
                    current, delta_pct
                ), f"{label} {completion_pct:.1f}% -> {delta_pct:+.0f}%"
        return current, f"{label} {completion_pct:.1f}%: правило не изменило цену"

    def _plan_fact_recommended_price(
        self,
        row: dict[str, Any],
        min_price: int,
        *,
        metric: Literal["orders", "revenue", "margin"] = "orders",
    ) -> tuple[int, str]:
        current = int(row["meta"]["currentPriceKopecks"])
        completion_pct = self._plan_fact_pct(row, metric=metric)
        return self._plan_fact_price_from_completion(
            current, min_price, completion_pct, label="План-факт"
        )

    def _basket_threshold_recommended_price(
        self, row: dict[str, Any]
    ) -> tuple[int, str]:
        current = int(row["meta"]["currentPriceKopecks"])
        meta = row.get("meta") or {}
        analytics = row.get("analytics") or {}
        baskets = int(
            analytics.get("baskets")
            if analytics.get("baskets") is not None
            else meta.get("basketsLast7d") or 0
        )
        high = max(0, self._algorithm_int_setting("cartHighBasketsThreshold", 30))
        low = max(0, self._algorithm_int_setting("cartLowBasketsThreshold", 5))
        step_pct = max(
            0.0, self._settings_price_step_pct(row.get("settings") or {}, 3.0)
        )
        if high > 0 and baskets >= high:
            return self._apply_pct(
                current, step_pct
            ), f"Корзины {baskets} >= {high} -> +{step_pct:.1f}%"
        if baskets <= low:
            return self._apply_pct(
                current, -step_pct
            ), f"Корзины {baskets} <= {low} -> -{step_pct:.1f}%"
        return (
            current,
            f"Корзины {baskets} в нейтральном диапазоне {low + 1}-{max(low + 1, high - 1)}",
        )

    def _illiquid_recommended_price(
        self, row: dict[str, Any]
    ) -> tuple[int | None, str, list[str]]:
        article_id = row["meta"]["articleId"]
        runtime = self.context.liquidation.get(article_id) or {}
        next_step_at = (
            self._parse_iso(str(runtime.get("nextStepAt") or "")) if runtime else None
        )
        if next_step_at is not None and self.context.as_of < next_step_at:
            return (
                None,
                "Неликвид: следующий дневной шаг ещё не наступил",
                ["liquidation_not_due"],
            )

        current = int(
            runtime.get("currentPriceKopecks") or row["meta"]["currentPriceKopecks"]
        )
        step_pct = float(runtime.get("stepPct") or 3.0)
        hold_to = int(runtime.get("holdOrdersTo") or 1)
        orders = self._orders_units(row)
        target = int(runtime.get("targetPriceKopecks") or 1)
        if orders <= 0:
            recommended = max(target, self._apply_pct(current, -step_pct))
            explanation = f"Неликвид: 0 заказов за период -> -{step_pct:.0f}%"
        elif orders <= hold_to:
            recommended = current
            explanation = f"Неликвид: {orders} заказ(ов) в hold-диапазоне 1-{hold_to}, держим цену"
        else:
            recommended = self._apply_pct(current, step_pct)
            explanation = (
                f"Неликвид: спрос восстановился ({orders} заказов) -> +{step_pct:.0f}%"
            )

        return recommended, explanation, []
