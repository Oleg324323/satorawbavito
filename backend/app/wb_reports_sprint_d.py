from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from vella_wb_19_05.models import (
    AbcFilteredSummary,
    AbcReportResponse,
    AdsAttributionPolicy,
    AdsPerformanceResponse,
    AdsPerformanceRow,
    AdsTotals,
    DatePeriod,
    DayAllocationSummary,
    ManualCost,
    PnlFieldMapping,
    PnlReportResponse,
    PnlRow,
    PnlTotals,
    ReportGroupBy,
    RnpReportResponse,
    RnpRow,
    SourceEvidence,
    utc_now,
)
from app.repricer_bff import DEFAULT_ALGORITHM_SETTINGS, TYPE_DEFAULTS, FRONTEND_STRATEGY_BY_ID, _article_type, _extract_wb_media_url
from app.repricer_cache.store import (
    finance_cache_uses_current_revenue_basis,
    get_source_cache,
    list_cached_goods,
    list_source_cache_ranges_by_prefix,
    save_source_cache,
)
from app.repricer_persistence.store import load_algorithm_settings, load_runtime_state
from app.platform.economics.legacy_tax import get_legacy_finance_taxes, legacy_finance_tax_revision
from app.platform.economics.wb_quarterly_tax import tax_consistent_report
from app.platform.period import Period
from app.repricer_sync import (
    _good_buyer_price_no_wallet_kopecks,
    _good_seller_price_kopecks,
    _normalize_period_range,
    _period_cache_suffix,
)
from app.wb_api.ads_runtime import AdsAttributionRow, AdsAttributionSnapshot
from app.wb_api.rnp_runtime import build_rnp_snapshot


PnlRequestedState = Literal["operative", "preliminary", "final"]
PlanFactDimension = Literal["company", "manager", "brand"]

DEFAULT_FROM = date(2026, 5, 1)
DEFAULT_TO = date(2026, 5, 28)
PNL_REPORT_CACHE_VERSION = "v24-organization-quarterly-tax"
PNL_REPORT_CACHE_TTL = timedelta(hours=24)
PNL_REPORT_STALE_TTL = timedelta(hours=24)


class PlanFactRow(BaseModel):
    rowId: str = Field(min_length=1)
    dimension: PlanFactDimension
    ownerId: str = Field(min_length=1)
    ownerLabel: str = Field(min_length=1)
    metric: Literal["margin_profit_kopecks", "revenue_kopecks"]
    planKopecks: int | None = Field(default=None, ge=0)
    factKopecks: int | None = Field(default=None, ge=0)
    deviationKopecks: int | None = None
    completionPct: float | None = None
    forecastKopecks: int | None = Field(default=None, ge=0)
    minPerDayKopecks: int | None = None
    sourceStatus: Literal["fresh", "partial", "stale", "blocked", "unknown"]
    confidence: Literal["high", "medium", "low", "blocked"]
    blockerIds: list[str] = Field(default_factory=list)


class PlanFactResponse(BaseModel):
    sourceStatus: Literal["fresh", "partial", "stale", "blocked", "unknown"]
    confidence: Literal["high", "medium", "low", "blocked"]
    blockerIds: list[str]
    sourceEvidence: list[SourceEvidence]
    calculatedAt: datetime
    period: DatePeriod
    dimension: PlanFactDimension
    rows: list[PlanFactRow]


class RatingLink(BaseModel):
    kind: Literal["sku_drawer", "repricer", "liquidation"]
    href: str = Field(min_length=1)


class RatingRow(BaseModel):
    skuId: str = Field(min_length=1)
    nmId: int = Field(gt=0)
    group: Literal["locomotive", "profitable", "promising", "weak", "loss_maker", "new"]
    score: int = Field(ge=0, le=100)
    scoreReasons: list[str]
    links: list[RatingLink]
    sourceStatus: Literal["fresh", "partial", "stale", "blocked", "unknown"]
    confidence: Literal["high", "medium", "low", "blocked"]
    blockerIds: list[str] = Field(default_factory=list)


class SkuRatingResponse(BaseModel):
    sourceStatus: Literal["fresh", "partial", "stale", "blocked", "unknown"]
    confidence: Literal["high", "medium", "low", "blocked"]
    blockerIds: list[str]
    sourceEvidence: list[SourceEvidence]
    calculatedAt: datetime
    period: DatePeriod
    rows: list[RatingRow]


class ExportCurrentViewResponse(BaseModel):
    exportState: Literal["ready", "no_access"]
    reason: str | None = None
    fileName: str | None = None
    format: Literal["xlsx"]
    sourceStatus: Literal["fresh", "partial", "stale", "blocked", "unknown"]
    confidence: Literal["high", "medium", "low", "blocked"]
    blockerIds: list[str]
    sourceEvidence: list[SourceEvidence]
    generatedAt: datetime


def _period(date_from: date, date_to: date) -> DatePeriod:
    return DatePeriod(dateFrom=date_from, dateTo=date_to)


def _evidence(source_id: str, source_type: Literal["wb_api", "wb_excel", "manual", "derived", "mock"], source_name: str, fields: list[str]) -> list[SourceEvidence]:
    return [
        SourceEvidence(
            sourceId=source_id,
            sourceType=source_type,
            sourceName=source_name,
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=fields,
        )
    ]


def _calc_margin_pct(net_profit_kopecks: int | None, revenue_kopecks: int) -> float | None:
    if net_profit_kopecks is None or revenue_kopecks <= 0:
        return None
    return round(net_profit_kopecks / revenue_kopecks * 100, 2)


def _calc_drr_pct(ad_spend_kopecks: int, revenue_kopecks: int) -> float:
    if revenue_kopecks <= 0:
        return 0.0
    return round(ad_spend_kopecks / revenue_kopecks * 100, 2)


def _calc_roi_pct(ad_spend_kopecks: int, revenue_kopecks: int) -> float:
    if ad_spend_kopecks <= 0:
        return 0.0
    return round((revenue_kopecks - ad_spend_kopecks) / ad_spend_kopecks * 100, 2)


def _finance_commission_kopecks(finance: dict[str, Any]) -> int | None:
    """ABC/P&L use reported commission, not another view's payout bridge."""
    value = finance.get("reportedCommissionKopecks")
    if finance.get("reportedCommissionComplete") is True and type(value) is int:
        return value
    return None


def _abc_financial_components(
    *,
    finance: dict[str, Any],
    settings: dict[str, Any],
    ads: dict[str, Any],
    revenue_kopecks: int,
    sales_units: int,
    finance_tax: dict[str, Any],
    cogs_kopecks: int | None = None,
    canonical_costs: bool = False,
    campaign_ads: bool = False,
) -> dict[str, int | None]:
    cogs = cogs_kopecks if canonical_costs else (_nonnegative_int(settings.get("cogsKopecks")) * sales_units if cogs_kopecks is None else cogs_kopecks)
    commission = _finance_commission_kopecks(finance)
    logistics = _int_or_zero(finance.get("logisticsKopecks"))
    penalty_signed = _int_or_zero(finance.get("penaltyKopecks"))
    deduction_signed = _int_or_zero(finance.get("deductionKopecks"))
    storage = _int_or_zero(finance.get("storageKopecks"))
    acceptance = _int_or_zero(finance.get("acceptanceKopecks"))
    penalty = penalty_signed
    deduction = deduction_signed
    additional_payment_signed = _int_or_zero(finance.get("additionalPaymentKopecks"))
    finance_other_expenses = max(0, -additional_payment_signed)
    finance_income = max(0, additional_payment_signed)
    compensation = _int_or_zero(finance.get("compensationKopecks"))
    other_finance_credits = finance_income - compensation
    finance_credits = finance_income + max(0, -penalty_signed) + max(0, -deduction_signed) - finance_other_expenses
    acquiring = _int_or_zero(finance.get("acquiringKopecks"))
    loyalty_cost = _int_or_zero(finance.get("loyaltyCostKopecks"))
    ad_spend = (ads.get("adSpendKopecks") if campaign_ads else
                _int_or_zero(finance.get("adSpendKopecks")) if finance.get("financeAdSpendAuthoritative") else None)
    ad_spend = ad_spend if type(ad_spend) is int else None
    other_expenses = (
        _nonnegative_int(settings.get("otherExpensePerSaleKopecks")) * sales_units
        + int(round(revenue_kopecks * _nonnegative_float_or_zero(settings.get("otherExpensePricePct")) / 100))
    )
    tax = finance_tax.get("taxKopecks") if finance_tax.get("factTaxState") == "configured" else None
    expenses = (cogs + commission + logistics + storage + acceptance + penalty + deduction + finance_other_expenses + acquiring + loyalty_cost + ad_spend + other_expenses - finance_income) if cogs is not None and commission is not None and ad_spend is not None else None
    net_profit = revenue_kopecks - expenses - tax if tax is not None and expenses is not None else None
    return {
        "revenueKopecks": revenue_kopecks,
        "cogsKopecks": cogs,
        "commissionKopecks": commission,
        "logisticsKopecks": logistics,
        "storageKopecks": storage,
        "acceptanceKopecks": acceptance,
        "penaltyKopecks": penalty,
        "deductionKopecks": deduction,
        "financeCreditsKopecks": finance_credits,
        "financeOtherExpensesKopecks": finance_other_expenses,
        "compensationKopecks": compensation,
        "otherFinanceCreditsKopecks": other_finance_credits,
        "financeIncomeKopecks": finance_income,
        "penaltyRefundKopecks": max(0, -penalty_signed),
        "deductionRefundKopecks": max(0, -deduction_signed),
        "acquiringKopecks": acquiring,
        "loyaltyCostKopecks": loyalty_cost,
        "adSpendKopecks": ad_spend,
        "otherExpensesKopecks": other_expenses,
        "taxKopecks": tax,
        "netProfitKopecks": net_profit,
    }


def _financial_coverage(cache):
    return {"complete": cache.get("sourceComplete") is True,
            "dateFrom": cache.get("dateFrom"), "dateTo": cache.get("dateTo"),
            "missingDates": cache.get("missingDates", []), "fetchedAt": cache.get("fetchedAt")}


def _abc_unallocated_row(cache):
    account = cache.get("unallocatedFinancials", {})
    trades = cache.get("unallocatedRealization", {})
    revenue = _int_or_zero(trades.get("revenueKopecks"))
    reported = cache.get("reportedAccountCommission", {})
    if not any(type(value) is int and value for value in [*account.values(), *trades.values(), reported.get("reportedCommissionKopecks")]):
        return None
    no_taxable_activity = not revenue and not account.get("additionalPaymentKopecks")
    components = _abc_financial_components(finance={**account, **cache.get("reportedAccountCommission", {}), "financeAdSpendAuthoritative": cache.get("financialAdvertisingComplete") is not False},
        settings={}, ads={}, revenue_kopecks=revenue, sales_units=0, cogs_kopecks=0 if no_taxable_activity else None, canonical_costs=True,
        finance_tax={"taxKopecks": cache.get("unallocatedTaxKopecks"),
                     "factTaxState": "configured" if cache.get("unallocatedTaxKopecks") is not None else "missing"})
    profit = components["netProfitKopecks"] if cache.get("sourceComplete") and not cache.get("projectionBlockers") else None
    return {**components, "isUnallocated": True, "nmId": None, "sku": "Нераспределённые расходы WB",
        "productName": "Начисления кабинета без подтверждённой привязки к товару", "abcCode": "—",
        "ordersComposite": {"units": 0, "kopecks": 0},
        "salesComposite": {"units": trades.get("netSalesUnits", 0), "kopecks": revenue},
        "salesKopecks": revenue, "salesUnits": trades.get("netSalesUnits", 0), "returnsKopecks": 0, "netTotalKopecks": profit,
        "campaignAdSpendKopecks": None, "financeAdSpendKopecks": components["adSpendKopecks"],
        "adSpendSource": "wb_finance_report", "factTaxState": "configured" if cache.get("unallocatedTaxKopecks") is not None else "missing", "baskets": 0,
        "sourceStatus": "partial", "confidence": "medium", "blockerIds": ["WB_FINANCE_UNALLOCATED"]}


def _unallocated_expense_tax(cache, finance_taxes):
    # Zero is justified for an expense-only residual only when the existing
    # dated tax calculation is configured for all known merchandise. This is
    # not a fallback rate or a tax rule for unidentified income/merchandise.
    if (finance_taxes and all(row.get("factTaxState") == "configured" for row in finance_taxes.values())
            and not any(cache.get("unallocatedRealization", {}).values())
            and not cache.get("unallocatedFinancials", {}).get("additionalPaymentKopecks")):
        return 0
    return None


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(round(float(value.replace(",", ".").strip())))
        except ValueError:
            return 0
    return 0


def _nonnegative_int(value: Any) -> int:
    return max(0, _int_or_zero(value))


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None
    return None


def _nonnegative_float_or_zero(value: Any) -> float:
    parsed = _float_or_none(value)
    return max(0.0, float(parsed or 0))


def _first_float_or_none(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _first_nonnegative_int_or_none(row: dict[str, Any], *keys: str) -> int | None:
    value = _first_float_or_none(row, *keys)
    return max(0, int(round(value))) if value is not None else None


def _abc_funnel_impressions(row: dict[str, Any]) -> int | None:
    return _first_nonnegative_int_or_none(
        row,
        "impressions",
        "viewCountTotal",
        "viewCount",
        "views",
        "viewsCount",
        "showCount",
        "showCountTotal",
        "impressionCount",
    )


def abc_spp_by_nm(
    organization_id: int,
    date_from: date,
    date_to: date,
    *,
    fallback_cache: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Current buyer-price SPP, with dated WB order observations as fallback."""
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    show_current = date_to >= today - timedelta(days=1)
    history_to = today if show_current else date_to
    history_from = history_to - timedelta(days=13)
    cache = _period_cache(organization_id, "period_stats", history_from, history_to)
    daily = cache.get("dailyAggregates") if isinstance(cache, dict) else None
    if not isinstance(daily, dict) or not daily:
        fallback = fallback_cache or _period_cache(organization_id, "period_stats", date_from, date_to)
        daily = fallback.get("dailyAggregates") if isinstance(fallback, dict) else None
    if not isinstance(daily, dict):
        daily = {}
    # day -> (pct, buyer price, source); the source tells the hover which WB feed observed it.
    observations: dict[str, dict[str, tuple[float, int | None, str]]] = {}
    for day_key, day_rows in daily.items():
        day = _parse_cache_date(day_key)
        if day is None or not history_from <= day <= history_to or not isinstance(day_rows, dict):
            continue
        for nm_id, source in day_rows.items():
            if not isinstance(source, dict):
                continue
            pct = _first_float_or_none(source, "sppPct")
            if pct is None or not 0 <= pct <= 100:
                continue
            price = _first_nonnegative_int_or_none(source, "avgPriceWithSppKopecks")
            observations.setdefault(str(nm_id), {})[day.isoformat()] = (round(pct, 2), price, "orders")
    if show_current:
        now = datetime.now(timezone.utc)
        for good in list_cached_goods(organization_id):
            nm_id = _int_or_zero(good.get("nmID") or good.get("nmId"))
            sizes = good.get("sizes") if isinstance(good.get("sizes"), list) else []
            size = sizes[0] if sizes and isinstance(sizes[0], dict) else {}
            observed = _parse_cache_datetime(size.get("buyerPriceObservedAt") or good.get("buyerPriceObservedAt"))
            seller = _good_seller_price_kopecks(good)
            buyer = _good_buyer_price_no_wallet_kopecks(good)
            if (nm_id <= 0 or observed is None or now - observed > timedelta(hours=36)
                or observed > now or not seller or buyer is None
                or size.get("buyerPriceSellerKopecks") not in (None, seller)):
                continue
            pct = round((seller - buyer) / seller * 100, 2)
            if not 0 <= pct <= 100:
                continue
            day_key = observed.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
            observations.setdefault(str(nm_id), {})[day_key] = (pct, buyer, "extension")
    result: dict[str, dict[str, Any]] = {}
    for nm_id, values in observations.items():
        latest_day = max(values)
        pct, price, source = values[latest_day]
        history = []
        for day in (history_from + timedelta(days=offset) for offset in range(14)):
            day_pct, day_price, day_source = values.get(day.isoformat(), (None, None, None))
            history.append({"date": day.isoformat(), "pct": day_pct,
                            "buyerPriceKopecks": day_price, "source": day_source})
        result[nm_id] = {
            "sppPct": pct,
            "sppBuyerPriceKopecks": price,
            "sppObservedOn": latest_day,
            "sppHistory": history,
            "sppSource": "current_buyer_price" if source == "extension" else "supplier.orders.spp",
        }
    return result


def _abc_funnel_open_count(row: dict[str, Any]) -> int | None:
    return _first_nonnegative_int_or_none(row, "openCount", "openCardCount", "openCard")


def _abc_funnel_cart_count(row: dict[str, Any]) -> int | None:
    return _first_nonnegative_int_or_none(
        row,
        "cartCount",
        "cartAdds",
        "addToCart",
        "addToCartCount",
        "basketCount",
        "baskets",
    )


def _parse_cache_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _cache_matches_range(cache: dict[str, Any], date_from: date, date_to: date) -> bool:
    return str(cache.get("dateFrom") or "") == date_from.isoformat() and str(cache.get("dateTo") or "") == date_to.isoformat()


def _complete_finance_ledger(cache: dict[str, Any], date_from: date, date_to: date) -> bool:
    from app.repricer_page_finance import realization_cache_compatible
    if not realization_cache_compatible(cache):
        return False
    if cache.get("projectionVersion"):
        from app.repricer_page_finance import VERSION
        return (cache.get("projectionVersion") == VERSION
            and finance_cache_uses_current_revenue_basis(cache)
            and _cache_matches_range(cache, date_from, date_to)
            and cache.get("sourceComplete") is True
            and not cache.get("missingDates"))
    return (finance_cache_uses_current_revenue_basis(cache)
        and _cache_matches_range(cache, date_from, date_to)
        and cache.get('sourceComplete') is not False
        and type(cache.get('pagesLoaded')) is int and cache['pagesLoaded'] >= 0
        and type(cache.get('rowsCount')) is int and cache['rowsCount'] >= 0)


def _cache_covers_range(cache: dict[str, Any], date_from: date, date_to: date) -> bool:
    cache_from = _parse_cache_date(cache.get("dateFrom"))
    cache_to = _parse_cache_date(cache.get("dateTo"))
    return bool(cache_from and cache_to and cache_from <= date_from and cache_to >= date_to)


def _merge_aggregate_row(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key.startswith("_") or value is None:
            continue
        if isinstance(value, bool):
            target[key] = bool(target.get(key)) or value
            continue
        if isinstance(value, (int, float)):
            if key.endswith("Pct") or key in {"sppPct", "commissionPct", "discountPct", "buyoutPct"}:
                target[key] = value
            else:
                target[key] = target.get(key, 0) + value
            continue
        if key not in target:
            target[key] = value


def _rollup_daily_aggregates(daily_aggregates: dict[str, Any], date_from: date, date_to: date) -> dict[str, dict[str, Any]]:
    rolled: dict[str, dict[str, Any]] = {}
    for day_key, day_rows in daily_aggregates.items():
        day = _parse_cache_date(day_key)
        if day is None or day < date_from or day > date_to or not isinstance(day_rows, dict):
            continue
        for nm_id, source_row in day_rows.items():
            if not isinstance(source_row, dict):
                continue
            row = rolled.setdefault(str(nm_id), {})
            _merge_aggregate_row(row, source_row)
    return rolled


def _slice_daily_aggregates(daily_aggregates: dict[str, Any], date_from: date, date_to: date) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for day_key, day_rows in daily_aggregates.items():
        day = _parse_cache_date(day_key)
        if day is not None and date_from <= day <= date_to:
            result[str(day_key)] = day_rows
    return result


def _covered_cache_from_daily(
    cache: dict[str, Any],
    *,
    prefix: str,
    suffix: str,
    resolved_days: int,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    if not _cache_covers_range(cache, date_from, date_to):
        return {}
    daily_aggregates = cache.get("dailyAggregates")
    if not isinstance(daily_aggregates, dict):
        return {}
    aggregates = _rollup_daily_aggregates(daily_aggregates, date_from, date_to)
    if not aggregates and cache.get("aggregates"):
        return {}
    payload = dict(cache)
    payload["aggregates"] = aggregates
    payload["count"] = len(aggregates)
    payload["dateFrom"] = date_from.isoformat()
    payload["dateTo"] = date_to.isoformat()
    payload["periodDays"] = resolved_days
    payload["periodCacheSuffix"] = suffix
    payload["dailyAggregates"] = _slice_daily_aggregates(daily_aggregates, date_from, date_to)
    if prefix == "ads":
        for day_rows in payload["dailyAggregates"].values():
            for nm, row in day_rows.items():
                if not isinstance(row, dict):
                    continue
                for key in ("adSpendKopecks", "impressions", "clicks", "cartAdds", "ordersCount", "ordersKopecks"):
                    if key in row and row[key] is None and str(nm) in aggregates:
                        aggregates[str(nm)][key] = None
    if prefix == "ads" and cache.get("campaignTotals") and (
        cache.get("dateFrom") != date_from.isoformat() or cache.get("dateTo") != date_to.isoformat()
    ):
        selected = _slice_daily_aggregates(cache.get("dailyCampaignTotals") or {}, date_from, date_to)
        # Never carry an entire campaign-period total into a narrower selection.
        payload.pop("campaignTotals", None)
        payload.pop("unallocatedPerformance", None)
        payload["dailyCampaignTotals"] = selected
        days = [(date_from + timedelta(days=i)).isoformat() for i in range(resolved_days)]
        fields = list(cache["campaignTotals"])
        if all(day in selected for day in days):
            payload["campaignTotals"] = {key: sum(selected[day][key] for day in days)
                if all(selected[day].get(key) is not None for day in days) else None for key in fields}
            payload["unallocatedPerformance"] = aggregates.get("campaign-unallocated", {})
        else:
            payload["sourceComplete"] = False
            payload["coverageState"] = "partial"
            payload["coverageReason"] = "Daily campaign totals unavailable for the selected subperiod"
    payload["coveredByCache"] = {
        "source": prefix,
        "dateFrom": cache.get("dateFrom"),
        "dateTo": cache.get("dateTo"),
        "periodDays": cache.get("periodDays"),
        "periodCacheSuffix": cache.get("periodCacheSuffix"),
    }
    return payload


def _sync_status_covering_keys(organization_id: int, prefix: str, date_from: date, date_to: date) -> list[str]:
    status = get_source_cache(organization_id, "wb_sync_status", slim=False) or {}
    if not isinstance(status, dict) or not _cache_covers_range(status, date_from, date_to):
        return []
    keys: list[str] = []
    suffix = str(status.get("periodCacheSuffix") or "").strip()
    if suffix:
        keys.append(f"{prefix}_{suffix}")
    period_days = status.get("periodDays")
    if period_days:
        keys.append(f"{prefix}_{period_days}")
    return list(dict.fromkeys(keys))


def _source_cache_covering_keys(organization_id: int, prefix: str, date_from: date, date_to: date) -> list[str]:
    keys: list[str] = []
    for cache in list_source_cache_ranges_by_prefix(organization_id, f"{prefix}_", limit=200):
        if not isinstance(cache, dict):
            continue
        cache_from = _parse_cache_date(cache.get("dateFrom"))
        cache_to = _parse_cache_date(cache.get("dateTo"))
        source_key = str(cache.get("sourceKey") or "")
        if cache_from and cache_to and cache_from <= date_from and cache_to >= date_to and source_key:
            keys.append(source_key)
    return list(dict.fromkeys(keys))


def _enrich_cache_with_covering_daily_aggregates(
    organization_id: int,
    prefix: str,
    cache: dict[str, Any],
    *,
    suffix: str,
    resolved_days: int,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    if prefix != "baskets":
        return cache
    if isinstance(cache.get("dailyAggregates"), dict) and cache["dailyAggregates"]:
        return cache
    current_keys = {f"{prefix}_{suffix}", f"{prefix}_{resolved_days}"}
    for covering_key in [
        *_sync_status_covering_keys(organization_id, prefix, date_from, date_to),
        *_source_cache_covering_keys(organization_id, prefix, date_from, date_to),
    ]:
        if covering_key in current_keys:
            continue
        covering = get_source_cache(organization_id, covering_key, slim=False) or {}
        rolled = _covered_cache_from_daily(
            covering,
            prefix=prefix,
            suffix=suffix,
            resolved_days=resolved_days,
            date_from=date_from,
            date_to=date_to,
        )
        daily_aggregates = rolled.get("dailyAggregates") if isinstance(rolled, dict) else None
        if isinstance(daily_aggregates, dict) and daily_aggregates:
            enriched = dict(cache)
            enriched["dailyAggregates"] = daily_aggregates
            enriched["dailyCoveredByCache"] = rolled.get("coveredByCache")
            return enriched
    return cache


def _compatible_period_cache(cache: dict[str, Any], *, require_current_finance_basis: bool) -> dict[str, Any]:
    if not require_current_finance_basis:
        return cache
    from app.repricer_page_finance import realization_cache_compatible
    return cache if finance_cache_uses_current_revenue_basis(cache) and realization_cache_compatible(cache) else {}


def _period_cache(
    organization_id: int,
    prefix: str,
    date_from: date,
    date_to: date,
    *,
    require_current_finance_basis: bool = False,
) -> dict[str, Any]:
    if prefix == "finance":
        # Reuse the current connection's completed raw cursor chains. This
        # projection separates trades from compensation and dates each fee
        # family correctly; do not change the legacy repricer's scope.
        from app.wb_report_finance import load_report_finance
        projected = load_report_finance(organization_id, date_from, date_to)
        return projected if projected.get("source") == "finance_sales_reports_detailed" else {}
    require_current_finance_basis = require_current_finance_basis or prefix == "finance"
    try:
        _range_start, _range_end, resolved_days = _normalize_period_range(30, date_from=date_from, date_to=date_to)
    except ValueError:
        return {}
    suffix = _period_cache_suffix(resolved_days, date_from=date_from, date_to=date_to)
    exact = _compatible_period_cache(
        get_source_cache(organization_id, f"{prefix}_{suffix}", slim=False) or {},
        require_current_finance_basis=require_current_finance_basis,
    )
    if exact:
        has_range = bool(exact.get("dateFrom") or exact.get("dateTo"))
        if not has_range or _cache_matches_range(exact, date_from, date_to):
            return _enrich_cache_with_covering_daily_aggregates(
                organization_id,
                prefix,
                exact,
                suffix=suffix,
                resolved_days=resolved_days,
                date_from=date_from,
                date_to=date_to,
            )
        rolled_exact = _covered_cache_from_daily(
            exact,
            prefix=prefix,
            suffix=suffix,
            resolved_days=resolved_days,
            date_from=date_from,
            date_to=date_to,
        )
        if rolled_exact:
            return rolled_exact
    fallback = _compatible_period_cache(
        get_source_cache(organization_id, f"{prefix}_{resolved_days}", slim=False) or {},
        require_current_finance_basis=require_current_finance_basis,
    )
    if fallback:
        has_range = bool(fallback.get("dateFrom") or fallback.get("dateTo"))
        if not has_range or _cache_matches_range(fallback, date_from, date_to):
            return _enrich_cache_with_covering_daily_aggregates(
                organization_id,
                prefix,
                fallback,
                suffix=suffix,
                resolved_days=resolved_days,
                date_from=date_from,
                date_to=date_to,
            )
    for covering_key in _sync_status_covering_keys(organization_id, prefix, date_from, date_to):
        if covering_key in {f"{prefix}_{suffix}", f"{prefix}_{resolved_days}"}:
            continue
        covering = _compatible_period_cache(
            get_source_cache(organization_id, covering_key, slim=False) or {},
            require_current_finance_basis=require_current_finance_basis,
        )
        rolled = _covered_cache_from_daily(
            covering,
            prefix=prefix,
            suffix=suffix,
            resolved_days=resolved_days,
            date_from=date_from,
            date_to=date_to,
        )
        if rolled:
            return rolled
    for covering_key in _source_cache_covering_keys(organization_id, prefix, date_from, date_to):
        if covering_key in {f"{prefix}_{suffix}", f"{prefix}_{resolved_days}"}:
            continue
        covering = _compatible_period_cache(
            get_source_cache(organization_id, covering_key, slim=False) or {},
            require_current_finance_basis=require_current_finance_basis,
        )
        rolled = _covered_cache_from_daily(
            covering,
            prefix=prefix,
            suffix=suffix,
            resolved_days=resolved_days,
            date_from=date_from,
            date_to=date_to,
        )
        if rolled:
            return rolled
    # Reuse observed days while preserving partial coverage. The report carries
    # WB_FINANCE_SOURCE_INCOMPLETE instead of hiding every saved row until WB responds.
    from app.routers.wb_repricer_bff import _stitched_period_cache_from_days
    stitched = _stitched_period_cache_from_days(organization_id, prefix, date_from, date_to)
    if stitched:
        return {**stitched, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()}
    return {}


def _pnl_report_cache_key(
    *,
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    requested_state: PnlRequestedState,
    finance_allowed: bool,
) -> str:
    raw = "|".join(
        [
            PNL_REPORT_CACHE_VERSION,
            date_from.isoformat(),
            date_to.isoformat(),
            str(group_by),
            str(requested_state),
            "finance" if finance_allowed else "nofinance",
        ]
    )
    return f"pnl_report_{sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _parse_cache_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _get_cached_pnl_response(
    *,
    organization_id: int,
    cache_key: str,
    tax_revision: str | None = None,
) -> PnlReportResponse | None:
    cache = get_source_cache(organization_id, cache_key, slim=False) or {}
    tax_revision = tax_revision if tax_revision is not None else legacy_finance_tax_revision(organization_id)
    if tax_revision == "unavailable" or cache.get("taxRevision") != tax_revision:
        return None
    fetched_at = _parse_cache_datetime(cache.get("fetchedAt"))
    if fetched_at is None:
        return None
    report = cache.get("report")
    if not isinstance(report, dict):
        return None
    period = report.get("period") or {}
    try:
        start = date.fromisoformat(str(period["dateFrom"]))
        end = date.fromisoformat(str(period["dateTo"]))
    except (KeyError, ValueError, TypeError):
        return None
    if cache.get("sourceRevision") != _pnl_source_revision(organization_id, start, end):
        return None
    age = datetime.now(timezone.utc) - fetched_at
    if age > PNL_REPORT_STALE_TTL:
        return None
    try:
        cached_report = PnlReportResponse.model_validate(report)
    except ValueError:
        return None
    if age <= PNL_REPORT_CACHE_TTL:
        return cached_report
    return _mark_pnl_report_stale(cached_report)


def _mark_pnl_report_stale(report: PnlReportResponse) -> PnlReportResponse:
    payload = report.model_dump(mode="json")
    payload["sourceStatus"] = "stale"
    if payload.get("reportState") == "final":
        payload["reportState"] = "preliminary"
    if payload.get("confidence") == "high":
        payload["confidence"] = "medium"
    totals = payload.get("totals")
    if isinstance(totals, dict):
        totals["sourceStatus"] = "stale"
        if totals.get("confidence") == "high":
            totals["confidence"] = "medium"
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        row["sourceStatus"] = "stale"
        if row.get("confidence") == "high":
            row["confidence"] = "medium"
    day_allocation = payload.get("dayAllocation")
    if isinstance(day_allocation, dict):
        day_allocation["sourceStatus"] = "stale"
    return PnlReportResponse.model_validate(payload)


def _save_pnl_response_cache(
    *,
    organization_id: int,
    cache_key: str,
    report: PnlReportResponse,
    tax_revision: str,
    source_revision: str,
) -> None:
    save_source_cache(
        organization_id,
        cache_key,
        {
            "taxRevision": "unavailable" if "tax_policy_unavailable" in report.blockerIds else tax_revision,
            "sourceRevision": source_revision,
            "ttlSeconds": int(PNL_REPORT_CACHE_TTL.total_seconds()),
            "staleTtlSeconds": int(PNL_REPORT_STALE_TTL.total_seconds()),
            "report": report.model_dump(mode="json"),
        },
    )


def _pnl_source_revision(organization_id: int, start: date, end: date) -> str:
    from app.repricer_page_finance import page_finance_revision
    from app.wb_pnl_costs import current_cost_revision
    observations = [("connection-finance", page_finance_revision(organization_id, start, end)),
                    ("saved-repricer-costs", current_cost_revision(organization_id))]
    # Finance/stock above are already scoped to the current connection and
    # completed overlapping receipts, excluding other accounts and cursors.
    for prefix in ("ads_", "pnl_ads_", "baskets_"):
        for meta in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100):
            left, right = str(meta.get("dateFrom") or "")[:10], str(meta.get("dateTo") or "")[:10]
            if left and right and (left > str(end) or right < str(start)):
                continue
            observations.append((str(meta.get("sourceKey")), str(meta.get("fetchedAt"))))
    return sha1(repr(sorted(observations)).encode()).hexdigest()


def _goods_index(organization_id: int, content_cards_cache: dict[str, Any] | None = None,
                 *, include_catalog_photos: bool = False) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for good in list_cached_goods(organization_id):
        nm_id = _int_or_zero(good.get("nmID") or good.get("nmId"))
        if nm_id <= 0:
            continue
        result[nm_id] = good
    cards = (content_cards_cache if content_cards_cache is not None else get_source_cache(organization_id, "content_cards", slim=False) or {}).get("cards") or []
    for card in cards:
        if not isinstance(card, dict):
            continue
        nm_id = _int_or_zero(card.get("nmID") or card.get("nmId"))
        if nm_id <= 0:
            continue
        good = result.get(nm_id, {})
        merged = {**good, **{key: value for key, value in card.items() if value is not None}}
        for key in (
            "sizes",
            "discountedPrice",
            "price",
            "buyerPriceNoWalletKopecks",
            "buyerPriceNoWallet",
            "buyerPriceKopecks",
            "buyerPrice",
            "clientPrice",
        ):
            if good.get(key) is not None:
                merged[key] = good[key]
        result[nm_id] = merged
    # A targeted photo refresh updates the shared catalog without replacing
    # the complete WB content snapshot. Reports must see those saved photos.
    if include_catalog_photos and result:
        from app.platform.economics.legacy_catalog import catalog_facts
        facts = catalog_facts(organization_id)
        for nm_id, good in result.items():
            photo = facts.get(nm_id, {}).get('photoUrl')
            if photo:
                result[nm_id] = {**good, 'photoUrl': photo}
    return result


def _photo_url_for_good(good: dict[str, Any], nm_id: int) -> str | None:
    return _extract_wb_media_url(good, nm_id)


def _sku_settings(
    organization_id: int,
    article_id: str,
) -> dict[str, Any]:
    runtime = load_runtime_state(organization_id) or {}
    algorithm = dict(DEFAULT_ALGORITHM_SETTINGS)
    algorithm.update(load_algorithm_settings(organization_id) or {})
    return _sku_settings_from_state(article_id, runtime=runtime, algorithm=algorithm)


def _sku_settings_from_state(
    article_id: str,
    *,
    runtime: dict[str, Any],
    algorithm: dict[str, Any],
    effective_on: date | None = None,
) -> dict[str, Any]:
    from app.platform.economics.wb_quarterly_tax import planning_tax_pct
    defaults = dict(TYPE_DEFAULTS.get(_article_type(article_id), TYPE_DEFAULTS["F"]))
    cogs_source = "type_default"
    cogs_effective_from: str | None = None
    cogs_key = {"F": "tshirt", "H": "hoodie", "L": "longsleeve"}.get(_article_type(article_id))
    cogs_by_garment = algorithm.get("cogsByGarmentRub")
    garment_history = (algorithm.get("cogsByGarmentHistory") or {}).get(cogs_key) if cogs_key else None
    if cogs_key and isinstance(cogs_by_garment, dict) and (effective_on is None or not garment_history):
        cogs_rub = _float_or_none(cogs_by_garment.get(cogs_key))
        if cogs_rub and cogs_rub > 0:
            defaults["cogsKopecks"] = int(round(cogs_rub * 100))
            cogs_source = "algorithm_garment"

    if effective_on is not None and isinstance(garment_history, list):
        effective_entries = []
        for index, entry in enumerate(garment_history):
            if not isinstance(entry, dict) or "cogsRub" not in entry:
                continue
            effective_from = _parse_cache_date(entry.get("effectiveFrom")) or date.min
            if effective_from <= effective_on:
                effective_entries.append((effective_from, index, entry))
        baseline = next((entry for entry in garment_history if isinstance(entry, dict) and entry.get("source") == "observed_snapshot"), None)
        if effective_entries or baseline:
            entry = max(effective_entries, key=lambda item: item[:2])[2] if effective_entries else baseline
            cogs_rub = _float_or_none(entry.get("cogsRub"))
            if cogs_rub is not None and cogs_rub > 0:
                defaults["cogsKopecks"] = int(round(cogs_rub * 100))
                cogs_source = "algorithm_garment_history"
            else:
                cogs_source = "type_default_after_algorithm_override"
            cogs_effective_from = entry.get("effectiveFrom") if effective_entries else None

    settings = {
        "cogsKopecks": int(defaults.get("cogsKopecks") or 0),
        "taxPct": float(_float_or_none(algorithm.get("taxPct")) or 0),
        "otherExpensePricePct": float(_float_or_none(algorithm.get("otherExpensePricePct")) or 0),
        "otherExpensePerSaleKopecks": int(round(float(_float_or_none(algorithm.get("otherExpensePerSaleRub")) or 0) * 100)),
    }
    base_cogs_kopecks = settings["cogsKopecks"]
    base_cogs_source = cogs_source
    base_cogs_effective_from = cogs_effective_from
    overrides = (runtime.get("skuSettingsOverrides") or {}).get(article_id)
    history = (runtime.get("cogsHistory") or {}).get(article_id)
    if isinstance(overrides, dict):
        for key in ("cogsKopecks", "taxPct", "otherExpensePricePct", "otherExpensePerSaleKopecks"):
            if overrides.get(key) is not None and (key != "cogsKopecks" or effective_on is None or not history):
                settings[key] = overrides[key]
        if overrides.get("cogsKopecks") is not None and (effective_on is None or not history):
            cogs_source = "sku_override"
            cogs_effective_from = None

    # These are price-planning inputs only. Factual period tax is resolved
    # separately from dated finance operations, never from this snapshot.
    from app.platform.economics.wb_quarterly_tax import selected_basis_points
    planning_rate = selected_basis_points(algorithm, effective_on or datetime.now(ZoneInfo("Europe/Moscow")).date())
    if planning_rate is not None:
        settings["taxPct"] = planning_tax_pct(algorithm, effective_on)

    if effective_on is not None and isinstance(history, list):
        effective_entries = []
        for index, entry in enumerate(history):
            if not isinstance(entry, dict) or "cogsKopecks" not in entry:
                continue
            effective_from = _parse_cache_date(entry.get("effectiveFrom")) or date.min
            if effective_from <= effective_on:
                effective_entries.append((effective_from, index, entry))
        baseline = next((entry for entry in history if isinstance(entry, dict) and entry.get("source") == "observed_snapshot"), None)
        if effective_entries or baseline:
            entry = max(effective_entries, key=lambda item: item[:2])[2] if effective_entries else baseline
            if entry.get("cogsKopecks") is not None:
                settings["cogsKopecks"] = entry["cogsKopecks"]
                cogs_source = "sku_history"
            else:
                settings["cogsKopecks"] = base_cogs_kopecks
                cogs_source = f"{base_cogs_source}_after_sku_override"
                cogs_effective_from = (
                    max((base_cogs_effective_from, entry.get("effectiveFrom")), key=lambda raw: _parse_cache_date(raw) or date.min)
                    if base_cogs_effective_from and entry.get("effectiveFrom")
                    else None
                )
            if entry.get("cogsKopecks") is not None:
                cogs_effective_from = entry.get("effectiveFrom") if effective_entries else None
    settings["cogsSource"] = cogs_source
    settings["cogsEffectiveFrom"] = cogs_effective_from
    return settings


def _abc_period_cogs(
    *,
    nm_id: int,
    article_id: str,
    finance: dict[str, Any],
    finance_cache: dict[str, Any],
    runtime: dict[str, Any],
    algorithm: dict[str, Any],
    date_from: date,
    date_to: date,
) -> tuple[int, dict[str, Any]]:
    net_units = _int_or_zero(finance.get("netSalesUnits")) if "netSalesUnits" in finance else _int_or_zero(finance.get("salesUnits")) - _int_or_zero(finance.get("returnsUnits"))
    end_settings = _sku_settings_from_state(
        article_id,
        runtime=runtime,
        algorithm=algorithm,
        effective_on=date_to,
    )
    end_settings["cogsEvidenceStatus"] = "dated" if end_settings.get("cogsEffectiveFrom") else "undated"
    daily = finance_cache.get("dailyAggregates")
    if not isinstance(daily, dict):
        end_settings["cogsEvidenceStatus"] = "period_end_fallback"
        end_settings["cogsSource"] = f"{end_settings['cogsSource']}_period_end_fallback"
        return _nonnegative_int(end_settings.get("cogsKopecks")) * net_units, end_settings

    daily_units = 0
    cogs = 0
    has_undated_units = False
    for day_key, rows in daily.items():
        day = _parse_cache_date(day_key)
        row = rows.get(str(nm_id)) if isinstance(rows, dict) else None
        if day is None or day < date_from or day > date_to or not isinstance(row, dict):
            continue
        units = _int_or_zero(row.get("netSalesUnits")) if "netSalesUnits" in row else _int_or_zero(row.get("salesUnits")) - _int_or_zero(row.get("returnsUnits"))
        daily_units += units
        settings = _sku_settings_from_state(
            article_id,
            runtime=runtime,
            algorithm=algorithm,
            effective_on=day,
        )
        has_undated_units = has_undated_units or (units != 0 and not settings.get("cogsEffectiveFrom"))
        cogs += _nonnegative_int(settings.get("cogsKopecks")) * units
    if daily_units == net_units:
        if has_undated_units or not end_settings.get("cogsEffectiveFrom"):
            end_settings["cogsEvidenceStatus"] = "undated"
            end_settings["cogsSource"] = f"{end_settings['cogsSource']}_undated"
        return cogs, end_settings
    end_settings["cogsEvidenceStatus"] = "period_end_fallback"
    end_settings["cogsSource"] = f"{end_settings['cogsSource']}_period_end_fallback"
    return _nonnegative_int(end_settings.get("cogsKopecks")) * net_units, end_settings


def _sku_meta(
    organization_id: int,
    article_id: str,
) -> dict[str, Any]:
    runtime = load_runtime_state(organization_id) or {}
    return _sku_meta_from_state(article_id, runtime=runtime)


def _sku_meta_from_state(article_id: str, *, runtime: dict[str, Any]) -> dict[str, Any]:
    overrides = (runtime.get("skuMetaOverrides") or {}).get(article_id)
    return overrides if isinstance(overrides, dict) else {}


def _label_for_group(
    group_by: ReportGroupBy,
    *,
    nm_id: int,
    article_id: str,
    good: dict[str, Any],
    meta: dict[str, Any],
) -> tuple[str, str, str | None, str | None, str | None]:
    brand = str(good.get("brand") or meta.get("brand") or "unknown-brand")
    manager = str(meta.get("managerId") or "unassigned")
    category = str(good.get("subjectName") or good.get("subject") or "unknown-category")
    status = str(meta.get("status") or "unknown-status")
    sku = article_id or f"NM_{nm_id}"
    if group_by == "brand":
        return f"brand-{brand}", brand, f"brand-{brand}", None, None
    if group_by == "manager":
        return f"manager-{manager}", manager, None, manager, None
    if group_by == "category":
        return f"category-{category}", category, brand, manager, None
    if group_by == "status":
        return f"status-{status}", status, brand, manager, None
    return f"nm-{nm_id}", sku, brand, manager, sku


def _add_amount(target: dict[str, Any], key: str, amount: int | None) -> None:
    target[key] = None if amount is None or target.get(key) is None else _int_or_zero(target[key]) + amount


_PNL_NONNEGATIVE_AMOUNT_KEYS = (
    "cogsKopecks",
    "commissionKopecks",
    "logisticsKopecks",
    "storageKopecks",
    "adSpendKopecks",
    "overheadKopecks",
)


def _normalize_pnl_row_amounts(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    for key in _PNL_NONNEGATIVE_AMOUNT_KEYS:
        normalized[key] = _int_or_zero(normalized[key]) if normalized.get(key) is not None else None
    for key in ("revenueKopecks", "taxKopecks", "netProfitKopecks"):
        normalized[key] = _int_or_zero(normalized[key]) if normalized.get(key) is not None else None
    return normalized


def _build_cached_pnl_report(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    requested_state: PnlRequestedState,
    finance_allowed: bool,
) -> PnlReportResponse | None:
    from app.wb_pnl_expenses import load_pnl_finance, product_expenses, financial_expense_subtotal
    from app.wb_pnl_advertising import load_campaign_spend, product_spend
    finance_cache = load_pnl_finance(organization_id, date_from, date_to) or _period_cache(organization_id, "finance", date_from, date_to)
    aggregates = finance_cache.get("aggregates") if isinstance(finance_cache.get("aggregates"), dict) else {}
    if not aggregates and not _complete_finance_ledger(finance_cache, date_from, date_to):
        return None

    from app.wb_pnl_costs import saved_current_costs
    finance_taxes = get_legacy_finance_taxes(organization_id, finance_cache, Period(date_from, date_to),
        include_costs=True, current_costs=saved_current_costs(organization_id, finance_cache),
        allow_unmapped_quarterly_tax=True, use_current_settings=True)
    projection_blockers = list(finance_cache.get("projectionBlockers") or [])

    ads_cache, campaign_spend = load_campaign_spend(organization_id, date_from, date_to)
    ads_aggregates = campaign_spend["aggregates"]
    # Ads can incur costs before the first sale. Do not omit those products
    # from P&L, or totals would disagree with ABC for the same report window.
    aggregates = dict(aggregates)
    for nm_id, ads_row in ads_aggregates.items():
        if str(nm_id).isdigit() and int(nm_id) > 0 and nm_id not in aggregates and isinstance(ads_row, dict) and ads_row.get('adSpendKopecks'):
            aggregates[nm_id] = {"reportedCommissionKopecks": 0, "reportedCommissionComplete": _complete_finance_ledger(finance_cache, date_from, date_to)}
            if _complete_finance_ledger(finance_cache, date_from, date_to):
                finance_taxes[nm_id] = {'taxKopecks': 0, 'factTaxState': 'configured',
                    'settlementCogsKopecks': 0, 'settlementCogsState': 'configured'}
    content_cards_cache = get_source_cache(organization_id, "content_cards", slim=False) or {}
    goods_by_nm = _goods_index(organization_id, content_cards_cache, include_catalog_photos=True)
    runtime = load_runtime_state(organization_id) or {}
    algorithm = dict(DEFAULT_ALGORITHM_SETTINGS)
    algorithm.update(load_algorithm_settings(organization_id) or {})
    rows_by_key: dict[str, dict[str, Any]] = {}

    for raw_nm_id, finance in aggregates.items():
        if not isinstance(finance, dict):
            continue
        nm_id = _int_or_zero(raw_nm_id)
        if nm_id <= 0:
            nm_id = _int_or_zero(finance.get("nmId") or finance.get("nmID"))
        if nm_id <= 0:
            continue

        good = goods_by_nm.get(nm_id, {})
        article_id = str(good.get("vendorCode") or finance.get("vendorCode") or finance.get("sku") or f"NM_{nm_id}").strip()
        meta = _sku_meta_from_state(article_id, runtime=runtime)
        settings = _sku_settings_from_state(article_id, runtime=runtime, algorithm=algorithm)
        group_key, label, brand_id, manager_id, sku_id = _label_for_group(
            group_by,
            nm_id=nm_id,
            article_id=article_id,
            good=good,
            meta=meta,
        )

        sales_units = _int_or_zero(finance.get("salesUnits"))
        returns_units = _int_or_zero(finance.get("returnsUnits"))
        from app.repricer_page_finance import report_revenue_kopecks
        revenue = _int_or_zero(report_revenue_kopecks(finance))
        cogs = finance_taxes.get(str(raw_nm_id), {}).get("settlementCogsKopecks")
        ads_row = {"adSpendKopecks": product_spend(campaign_spend, nm_id)}
        finance_tax = finance_taxes.get(str(raw_nm_id), {})
        tax = finance_tax.get("taxKopecks") if finance_tax.get("factTaxState") == "configured" else None
        # Use the same signed settlement math as ABC. Refunds/corrections must
        # not disappear, and every expense must reconcile to a visible column.
        financial = _abc_financial_components(finance=finance, settings=settings, ads=ads_row,
            revenue_kopecks=revenue, sales_units=sales_units - returns_units,
            finance_tax=finance_tax, cogs_kopecks=cogs, canonical_costs=True, campaign_ads=True)
        commission = financial["commissionKopecks"]
        logistics = financial["logisticsKopecks"]
        storage = financial["storageKopecks"]
        ads = financial["adSpendKopecks"]
        expenses = product_expenses(finance_cache, nm_id)
        subtotal = financial_expense_subtotal(finance_cache, nm_id)
        overhead = sum(financial[key] for key in ("acceptanceKopecks", "penaltyKopecks",
            "deductionKopecks", "financeOtherExpensesKopecks", "acquiringKopecks",
            "loyaltyCostKopecks", "otherExpensesKopecks")) - financial["financeIncomeKopecks"]
        net_profit = financial["netProfitKopecks"]
        if (projection_blockers or finance.get("settlementComponentsConfirmed") is False
                or any(expenses[field] is None for field in ("acceptanceKopecks", "penaltyKopecks"))
                or subtotal is None
                or not campaign_spend.get("productCoverageComplete", campaign_spend["state"] == "ok")
                or not _complete_finance_ledger(finance_cache, date_from, date_to)):
            net_profit = None

        row = rows_by_key.setdefault(
            group_key,
            {
                "rowId": group_key,
                "label": label,
                "brandId": brand_id,
                "managerId": manager_id,
                "skuId": sku_id,
                "nmId": nm_id if group_by == "sku" else None,
                "sku": article_id if group_by == "sku" else None,
                "productName": good.get("title") if group_by == "sku" else None,
                "category": good.get("subjectName") if group_by == "sku" else None,
                "photoUrl": _photo_url_for_good(good, nm_id) if group_by == "sku" else None,
                "revenueKopecks": 0,
                "cogsKopecks": 0,
                "commissionKopecks": 0,
                "logisticsKopecks": 0,
                "storageKopecks": 0,
                "adSpendKopecks": 0,
                "taxKopecks": 0,
                "overheadKopecks": 0,
                "netProfitKopecks": 0,
                "penaltyKopecks": 0,
                "acceptanceKopecks": 0,
                "financialExpenseSubtotalKopecks": 0,
                "expenseState": "confirmed",
                "costReasons": [],
                "costOperationDates": [],
            },
        )
        if finance_tax.get("settlementCogsReason"):
            row["costReasons"] = sorted(set(row["costReasons"] + [finance_tax["settlementCogsReason"]]))
            row["costOperationDates"] = sorted(set(row["costOperationDates"] + finance_tax.get("settlementCostMissingDates", [])))
        for key, amount in (
            ("revenueKopecks", revenue),
            ("cogsKopecks", cogs),
            ("commissionKopecks", commission),
            ("logisticsKopecks", logistics),
            ("storageKopecks", storage),
            ("adSpendKopecks", ads),
            ("penaltyKopecks", expenses["penaltyKopecks"]),
            ("acceptanceKopecks", expenses["acceptanceKopecks"]),
            ("financialExpenseSubtotalKopecks", subtotal),
            ("taxKopecks", tax),
            ("overheadKopecks", overhead),
            ("netProfitKopecks", net_profit),
        ):
            _add_amount(row, key, amount)

        if expenses["state"] != "confirmed" or subtotal is None:
            row["expenseState"] = "partial" if subtotal is None else expenses["state"]

    raw_expenses = (finance_cache.get("diagnostics") or {}).get("rawExpenseTotals") or {}
    expense_fields = ("commissionKopecks", "logisticsKopecks", "storageKopecks", "adSpendKopecks",
                      "acceptanceKopecks", "penaltyKopecks", "deductionKopecks", "acquiringKopecks",
                      "loyaltyCostKopecks", "additionalPaymentKopecks")
    residual = {key: _int_or_zero(raw_expenses[key]) - sum(_int_or_zero(row.get(key))
                for row in finance_cache.get("aggregates", {}).values() if isinstance(row, dict))
                for key in expense_fields if key in raw_expenses}
    unallocated_trades = finance_cache.get("unallocatedRealization", {})
    account_commission = finance_cache.get("reportedAccountCommission", {})
    account_ads = product_spend(campaign_spend, None)
    account_expenses = product_expenses(finance_cache, None)
    account_fees_known = all(account_expenses[field] is not None for field in ("acceptanceKopecks", "penaltyKopecks")) and financial_expense_subtotal(finance_cache, None) is not None
    if (any(residual.values()) or any(unallocated_trades.values()) or account_ads
            or account_commission.get("reportedCommissionKopecks")
            or account_commission.get("reportedCommissionComplete") is False or not account_fees_known):
        account_tax = _unallocated_expense_tax(finance_cache, finance_taxes)
        amounts = _abc_financial_components(finance={**residual, **account_commission}, settings={}, ads={"adSpendKopecks": account_ads},
            revenue_kopecks=0, sales_units=0, cogs_kopecks=0, canonical_costs=True,
            finance_tax={"taxKopecks": 0, "factTaxState": "configured"}, campaign_ads=True)
        overhead = sum(amounts[key] for key in ("acceptanceKopecks", "penaltyKopecks",
            "deductionKopecks", "financeOtherExpensesKopecks", "acquiringKopecks",
            "loyaltyCostKopecks", "otherExpensesKopecks")) - amounts["financeIncomeKopecks"]
        rows_by_key["account-unallocated"] = dict(rowId="account-unallocated",
            label="Нераспределённые расходы WB", brandId=None, managerId=None, skuId=None,
            revenueKopecks=unallocated_trades.get("revenueKopecks", 0),
            cogsKopecks=None if any(unallocated_trades.values()) else 0, commissionKopecks=amounts["commissionKopecks"],
            logisticsKopecks=amounts["logisticsKopecks"], storageKopecks=amounts["storageKopecks"],
            penaltyKopecks=account_expenses["penaltyKopecks"],
            acceptanceKopecks=account_expenses["acceptanceKopecks"],
            financialExpenseSubtotalKopecks=financial_expense_subtotal(finance_cache, None),
            expenseState=account_expenses["state"] if account_fees_known else "partial",
            adSpendKopecks=account_ads,
            taxKopecks=account_tax, overheadKopecks=overhead,
            netProfitKopecks=None if account_tax is None or any(unallocated_trades.values()) or residual.get("additionalPaymentKopecks") or projection_blockers or campaign_spend["state"] != "ok" or amounts["netProfitKopecks"] is None or not account_fees_known or not _complete_finance_ledger(finance_cache, date_from, date_to) else amounts["netProfitKopecks"])

    if not rows_by_key:
        return None

    ads_blockers = [] if campaign_spend["state"] == "ok" else ["WB-02"]
    if any(row["adSpendKopecks"] is None for row in rows_by_key.values()):
        ads_blockers.append("WB-02")
    tax_blockers = projection_blockers + (["WB_PNL_TAX_UNCONFIRMED"] if any(row["taxKopecks"] is None for row in rows_by_key.values()) else [])
    if any(row.get("expenseState") != "confirmed" for row in rows_by_key.values()):
        tax_blockers.append("WB_PNL_EXPENSES_UNCONFIRMED")
    if finance_cache.get('sourceComplete') is False or finance_cache.get('coverageState') in {'partial', 'missing', 'blocked'}:
        tax_blockers.append('WB_FINANCE_SOURCE_INCOMPLETE')
    tax_blockers += sorted({item["factTaxReason"] for item in finance_taxes.values() if item.get("factTaxReason")})
    if any(row["cogsKopecks"] is None for row in rows_by_key.values()):
        tax_blockers.append("WB_PNL_DATED_COST_MISSING")
    if any(row["commissionKopecks"] is None for row in rows_by_key.values()):
        tax_blockers.append("WB_FINANCE_COMMISSION_UNCONFIRMED")
    if any(item.get('settlementCogsState') == 'assumed' for item in finance_taxes.values()):
        tax_blockers.append('WB_USER_EXAMPLE_COSTS')
    source_status: Literal["fresh", "partial", "stale", "blocked", "unknown"] = "fresh" if not ads_blockers and not tax_blockers else "partial"
    confidence: Literal["high", "medium", "low", "blocked"] = "high" if source_status == "fresh" else "medium"
    if not finance_allowed:
        source_status = "partial"
        confidence = "medium"

    rows: list[PnlRow] = []
    # Match the product demand order: funnel orders, carts, then revenue.
    # Sorting uses saved observations only and never modifies P&L amounts.
    demand = _cache_aggregates(_period_cache(organization_id, "baskets", date_from, date_to)) if group_by == "sku" else {}
    def row_order(item):
        if group_by != "sku":
            return (str(item["label"]),)
        funnel = demand.get(str(item.get("nmId"))) or {}
        return (-_int_or_zero(funnel.get("orderCount")), -_int_or_zero(funnel.get("cartCount")),
                -_int_or_zero(item["revenueKopecks"]), str(item["label"]))
    for raw_row in sorted(rows_by_key.values(), key=row_order):
        row = _normalize_pnl_row_amounts(raw_row)
        missing_inputs = [label for field, label in (
            ("adSpendKopecks", "Не подтверждён расход рекламных кампаний за выбранные даты"),
            ("acceptanceKopecks", "Нет подтверждённых данных приёмки WB"),
            ("penaltyKopecks", "Нет подтверждённых данных штрафов WB"),
        ) if row.get(field) is None]
        if row.get("financialExpenseSubtotalKopecks") is None and not missing_inputs:
            missing_inputs.append("Не все удержания финансового отчёта подтверждены")
        row["comment"] = " · ".join(missing_inputs) or None
        rows.append(
            PnlRow(
                **{
                    **row,
                    "overheadKopecks": row["overheadKopecks"] if finance_allowed else None,
                    "financialExpenseSubtotalKopecks": row["financialExpenseSubtotalKopecks"] if finance_allowed else None,
                    "marginPct": _calc_margin_pct(row["netProfitKopecks"], _int_or_zero(row["revenueKopecks"])),
                    "sourceStatus": source_status,
                    "confidence": confidence,
                }
            )
        )

    total_revenue = sum(_int_or_zero(row.revenueKopecks) for row in rows)
    total_net_profit = sum(row.netProfitKopecks for row in rows) if not ads_blockers and all(row.netProfitKopecks is not None for row in rows) else None
    total_storage = sum(_int_or_zero(row.storageKopecks) for row in rows)
    total_tax = sum(row.taxKopecks for row in rows) if all(row.taxKopecks is not None for row in rows) else None
    total_overhead = sum(_int_or_zero(row.overheadKopecks) for row in rows)
    blockers = sorted(set(ads_blockers + tax_blockers))
    report_state: Literal["operative", "preliminary", "final", "blocked"] = requested_state
    if requested_state == "final" and blockers:
        report_state = "preliminary"

    fetched_at = finance_cache.get("fetchedAt")
    evidence_synced_at = None
    if isinstance(fetched_at, str):
        try:
            evidence_synced_at = datetime.fromisoformat(fetched_at)
        except ValueError:
            evidence_synced_at = None

    source_evidence = [
        SourceEvidence(
            sourceId="wb-finance-sales-reports-detailed-cache",
            sourceType="wb_api",
            sourceName="WB Finance /api/finance/v1/sales-reports/detailed cached by repricer sync",
            lastSyncedAt=evidence_synced_at or utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=[
                "sellerRevenueKopecks",
                "buyerRevenueKopecks",
                "commissionKopecks",
                "commissionFormulaKopecks",
                "logisticsKopecks",
                "storageKopecks",
                "acceptanceKopecks",
                "penaltyKopecks",
                "deductionKopecks",
                "additionalPaymentKopecks",
                "rewardAdjustmentKopecks",
                "paymentScheduleKopecks",
                "cashbackAmountKopecks",
                "cashbackCommissionChangeKopecks",
                "loyaltyCostKopecks",
                "adSpendKopecks",
                "acquiringKopecks",
                "salesUnits",
            ],
        ),
    ]
    if ads_cache:
        source_evidence.extend(
            _evidence(
                "wb-ads-attribution-cache",
                "wb_api",
                "WB Ads spend cached by repricer sync",
                ["adSpendKopecks"],
            )
        )
    else:
        source_evidence.extend(
            _evidence(
                "wb-ads-attribution-cache",
                "wb_api",
                "Campaign statistics incomplete or absent; unknown spend is not zero",
                ["adSpendKopecks"],
            )
        )
    source_evidence.extend(
        _evidence(
            "wb-pnl-runtime",
            "derived",
            "Runtime P&L from repricer source cache and SKU settings",
            ["finance_aggregates", "sku_settings", "ads_aggregates", "netProfit"],
        )
    )
    if any(item.get("settlementCogsState") == "current" for item in finance_taxes.values()):
        source_evidence.extend(_evidence("saved-repricer-current-cost", "derived",
            "Persisted Repricer unit cost fallback; not confirmed historical cost evidence",
            ["cogsByGarmentRub", "skuSettingsOverrides.cogsKopecks", "salesUnits", "returnsUnits"]))

    return PnlReportResponse(
        sourceStatus=source_status,
        confidence=confidence,
        blockerIds=blockers,
        sourceEvidence=source_evidence,
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        reportState=report_state,
        groupBy=group_by,
        totals=PnlTotals(
            revenueKopecks=total_revenue,
            netProfitKopecks=total_net_profit,
            marginPct=_calc_margin_pct(total_net_profit, total_revenue),
            sourceStatus=source_status,
            confidence=confidence,
        ),
        rows=rows,
        financialCoverage=_financial_coverage(finance_cache),
        unallocatedFinancials=finance_cache.get("unallocatedFinancials", {}) if finance_allowed else {},
        storageAllocation=finance_cache.get("storageAllocation", {}) if finance_allowed else {},
        fieldMapping=_pnl_field_mapping(),
        manualCosts=[
            ManualCost(costId="storage", label="Storage", amountKopecks=total_storage, allocationBase="sku", sourceStatus="fresh", blockerIds=[]),
            ManualCost(costId="tax", label="Tax", amountKopecks=total_tax, allocationBase="sku", sourceStatus="partial" if tax_blockers else "fresh", blockerIds=tax_blockers),
            ManualCost(
                costId="overhead",
                label="Operating expenses",
                amountKopecks=total_overhead if finance_allowed else None,
                allocationBase="sku",
                sourceStatus="fresh" if finance_allowed else "partial",
                blockerIds=[],
            ),
        ],
        dayAllocation=DayAllocationSummary(
            allocationDateField="saleDt/rrDate",
            expenseDateField="rrDate",
            rule="finance detailed rows are aggregated by nmId inside the selected report period; SKU manual costs use the same period and sales units",
            sourceStatus=source_status,
            blockerIds=blockers,
        ),
    )


def _pnl_field_mapping() -> list[PnlFieldMapping]:
    return [
        PnlFieldMapping(metricId="penalties", metricLabel="Штрафы, ₽",
            sourceId="wb-finance-sales-reports-detailed-cache", sourceField="penalty/nmId/rrdId/rrDate",
            formula="signed sum(penalty) by nmId for inclusive rrDate; deduplicate rrdId; missing nmId stays unallocated",
            fallback="нет данных; never allocate by revenue", blockerIds=[]),
        PnlFieldMapping(metricId="paid_acceptance", metricLabel="Платная приёмка, ₽",
            sourceId="wb-finance-sales-reports-detailed-cache", sourceField="paidAcceptance/nmId/rrdId/rrDate",
            formula="signed sum(paidAcceptance) by nmId for inclusive rrDate; already included in overhead/profit",
            fallback="нет данных; confirmed zero requires a completed cursor and present fields", blockerIds=[]),
        PnlFieldMapping(
            metricId="revenue",
            metricLabel="Revenue",
            sourceId="wb-finance-sales-reports-detailed-cache",
            sourceField="retailAmount",
            formula="revenueKopecks = sum(finance.realizationRevenueKopecks); retailAmount, rrDate, net of returns",
            fallback="unavailable; no Statistics or Sales Funnel substitution",
            blockerIds=[],
        ),
        PnlFieldMapping(
            metricId="cogs",
            metricLabel="COGS",
            sourceId="catalog-cost-history-or-saved-repricer-cost",
            sourceField="catalog_cost_versions.amount_kopecks/effective_from; saved Repricer unit cost fallback",
            formula="sum(applicable saved unit cost * signed merchandise units at each operation date)",
            fallback="saved current Repricer unit cost on net merchandise units; unknown if neither source resolves safely",
            blockerIds=[],
        ),
        PnlFieldMapping(
            metricId="commission",
            metricLabel="Commission expense",
            sourceId="wb-finance-sales-reports-detailed-cache",
            sourceField="ppvzSalesCommission/nmId/rrdId/rrDate",
            formula="signed net ppvzSalesCommission, including returns; no payout-bridge or percentage fallback",
            fallback="нет данных",
            blockerIds=[],
        ),
        PnlFieldMapping(
            metricId="logistics",
            metricLabel="Logistics expense",
            sourceId="wb-finance-sales-reports-detailed-cache",
            sourceField="rebillLogisticCost/deliveryAmount/deliveryService",
            formula="logisticsKopecks uses monetary logistics fields; dlv_prc is not used as ruble logistics",
            fallback="нет данных",
            blockerIds=[],
        ),
        PnlFieldMapping(
            metricId="storage",
            metricLabel="Storage",
            sourceId="wb-finance-sales-reports-detailed-cache",
            sourceField="paidStorage",
            formula="storage line = signed storage fees; other WB fees and credits are included in overhead",
            fallback="manual_input",
            blockerIds=[],
        ),
        PnlFieldMapping(
            metricId="ads",
            metricLabel="Ads spend",
            sourceId="wb-ads-attribution-cache",
            sourceField="adSpendKopecks",
            formula="saved campaign fullstats spend by nmId for the exact period; financial promotion is replaced, not added",
            fallback="unknown until campaign coverage is complete; confirmed no spend is zero",
            blockerIds=[],
        ),
        PnlFieldMapping(
            metricId="tax",
            metricLabel="Tax expense",
            sourceId="organization-economics-dated-tax",
            sourceField="tax_basis_points/effective_from; wbQuarterlyTax.basisPoints/configured",
            formula="taxKopecks = sum(selected-period net revenue by confirmed dated tax rate)",
            fallback="unknown until the selected-period tax policy and revenue basis are confirmed",
            blockerIds=[],
        ),
        PnlFieldMapping(
            metricId="overhead",
            metricLabel="Operating expenses",
            sourceId="repricer-sku-settings",
            sourceField="otherExpensePerSaleKopecks/otherExpensePricePct",
            formula="overheadKopecks = configured expenses on net sales + acquiring + acceptance + penalties + deductions + loyalty + other finance expenses - compensation; hidden without finance:read",
            fallback="0",
            blockerIds=[],
        ),
    ]


def _empty_pnl_report_from_cache_miss(
    *,
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    requested_state: PnlRequestedState,
    finance_allowed: bool,
) -> PnlReportResponse:
    blockers = ["WB_PNL_FINANCE_CACHE_MISSING"]
    return PnlReportResponse(
        sourceStatus="blocked",
        confidence="blocked",
        blockerIds=blockers,
        sourceEvidence=_evidence(
            "wb-pnl-cache-miss",
            "derived",
            "P&L report is cache-only; repricer finance cache is missing for the selected period",
            ["finance_aggregates", "ads_aggregates", "sku_settings"],
        ),
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        reportState="blocked",
        groupBy=group_by,
        totals=PnlTotals(
            revenueKopecks=0,
            netProfitKopecks=0,
            marginPct=None,
            sourceStatus="blocked",
            confidence="blocked",
        ),
        rows=[],
        fieldMapping=_pnl_field_mapping(),
        manualCosts=[
            ManualCost(costId="storage", label="Storage/acceptance/penalties/deductions", amountKopecks=0, allocationBase="sku", sourceStatus="blocked", blockerIds=blockers),
            ManualCost(costId="tax", label="Tax", amountKopecks=0, allocationBase="sku", sourceStatus="blocked", blockerIds=blockers),
            ManualCost(
                costId="overhead",
                label="Operating expenses",
                amountKopecks=0 if finance_allowed else None,
                allocationBase="sku",
                sourceStatus="blocked",
                blockerIds=blockers,
            ),
        ],
        dayAllocation=DayAllocationSummary(
            allocationDateField="saleDt/rrDate",
            expenseDateField="rrDate",
            rule="P&L waits for cached finance aggregates produced by WB sync; report runtime does not fetch WB directly",
            sourceStatus="blocked",
            blockerIds=blockers,
        ),
    )


@tax_consistent_report
def build_pnl_report(
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    requested_state: PnlRequestedState,
    finance_allowed: bool,
    organization_id: int | None = None,
    wb_token: str | None = None,
    progress_callback: Any | None = None,
) -> PnlReportResponse:
    pnl_cache_key = None
    if organization_id is not None:
        tax_revision = legacy_finance_tax_revision(organization_id)
        pnl_cache_key = _pnl_report_cache_key(
            date_from=date_from,
            date_to=date_to,
            group_by=group_by,
            requested_state=requested_state,
            finance_allowed=finance_allowed,
        )
        cached_pnl = _get_cached_pnl_response(organization_id=organization_id, cache_key=pnl_cache_key, tax_revision=tax_revision)
        if cached_pnl is not None:
            return cached_pnl

        source_revision = _pnl_source_revision(organization_id, date_from, date_to)
        cached_report = _build_cached_pnl_report(
            organization_id=organization_id,
            date_from=date_from,
            date_to=date_to,
            group_by=group_by,
            requested_state=requested_state,
            finance_allowed=finance_allowed,
        )
        if cached_report is not None:
            if source_revision != _pnl_source_revision(organization_id, date_from, date_to) or tax_revision != legacy_finance_tax_revision(organization_id):
                return _mark_pnl_report_stale(cached_report)
            _save_pnl_response_cache(organization_id=organization_id, cache_key=pnl_cache_key, report=cached_report, tax_revision=tax_revision, source_revision=source_revision)
            return cached_report
        return _empty_pnl_report_from_cache_miss(
            date_from=date_from,
            date_to=date_to,
            group_by=group_by,
            requested_state=requested_state,
            finance_allowed=finance_allowed,
        )

    return _empty_pnl_report_from_cache_miss(
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        requested_state=requested_state,
        finance_allowed=finance_allowed,
    )

def _build_cached_ads_snapshot(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
) -> AdsAttributionSnapshot:
    ads_cache = _period_cache(organization_id, "ads", date_from, date_to)
    aggregates = _cache_aggregates(ads_cache)
    rows: list[AdsAttributionRow] = []
    unallocated = False
    totals = {
        "ad_spend_kopecks": 0,
        "impressions": 0,
        "clicks": 0,
        "cart_adds": 0,
        "orders_count": 0,
        "orders_kopecks": 0,
    }
    for raw_key, aggregate in aggregates.items():
        if not isinstance(aggregate, dict):
            continue
        campaign_id_raw = aggregate.get("campaignId") or aggregate.get("advertId") or aggregate.get("advert_id")
        campaign_id = str(campaign_id_raw) if campaign_id_raw not in (None, "") else None
        nm_id = _int_or_zero(aggregate.get("nmId") or aggregate.get("nmID") or (raw_key if not campaign_id else None))
        ad_spend = _first_nonnegative_int_or_none(aggregate, "adSpendKopecks", "spendKopecks", "sumKopecks")
        impressions = _first_nonnegative_int_or_none(aggregate, "adImpressions", "impressions", "views")
        clicks = _first_nonnegative_int_or_none(aggregate, "adClicks", "clicks")
        cart_adds = _first_nonnegative_int_or_none(aggregate, "adCartAdds", "cartAdds", "cartCount", "baskets")
        orders_count = _first_nonnegative_int_or_none(aggregate, "adOrders", "ordersCount", "orderCount", "orders")
        orders_kopecks = _first_nonnegative_int_or_none(aggregate, "adRevenueKopecks", "adSalesKopecks", "ordersKopecks", "orderSumKopecks", "salesKopecks")
        if not any((nm_id > 0, campaign_id, ad_spend, impressions, clicks, cart_adds, orders_count, orders_kopecks)):
            continue
        if group_by == "sku" and nm_id <= 0:
            unallocated = True
            continue
        for key, value in (
            ("ad_spend_kopecks", ad_spend), ("impressions", impressions),
            ("clicks", clicks), ("cart_adds", cart_adds),
            ("orders_count", orders_count), ("orders_kopecks", orders_kopecks),
        ):
            if value is None:
                totals.pop(key, None)
            elif key in totals:
                totals[key] += value
        sku_id = str(nm_id) if nm_id > 0 else None
        rows.append(
            AdsAttributionRow(
                campaign_id=campaign_id,
                sku_id=sku_id,
                attribution_level="campaign_sku" if sku_id and campaign_id else ("exact_sku" if sku_id else "campaign_only"),
                confidence="high" if sku_id and (ad_spend or orders_count) else "medium",
                ad_spend_kopecks=ad_spend,
                impressions=impressions,
                clicks=clicks,
                cart_adds=cart_adds,
                orders_count=orders_count,
                orders_kopecks=orders_kopecks,
                campaign_name=str(aggregate.get("campaignName") or aggregate.get("advertName") or "") or None,
                campaign_type=aggregate.get("campaignType") or aggregate.get("advertType"),
                campaign_status=aggregate.get("campaignStatus") or aggregate.get("status"),
                payment_type=aggregate.get("paymentType"),
            )
        )
    has_any_data = bool(rows)
    blockers = []
    if unallocated:
        blockers.append("WB_ADS_SKU_ATTRIBUTION_INCOMPLETE")
    if has_any_data and len(totals) < 6:
        blockers.append("WB_ADS_METRICS_INCOMPLETE")
    if not has_any_data and not unallocated:
        blockers.append("WB_ADS_CACHE_EMPTY")
    if blockers:
        blockers.insert(0, "WB-02")
    return AdsAttributionSnapshot(
        source_status=("partial" if blockers else "fresh") if has_any_data else "blocked",
        confidence=("high" if not blockers and any(row.confidence == "high" for row in rows) else "medium") if has_any_data else "blocked",
        blocker_ids=blockers,
        source_evidence=_evidence(
            "wb-ads-attribution-cache",
            "wb_api",
            "WB Ads cached by repricer sync",
            ["adSpendKopecks", "adImpressions", "adClicks", "adCartAdds", "adOrders", "adRevenueKopecks"],
        ),
        totals=totals,
        rows=rows,
        diagnostics={"summary": {"source": "repricer_ads_cache", "rows": len(rows)}},
    )


def build_ads_performance_report(
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    finance_allowed: bool,
    organization_id: int | None = None,
    wb_token: str | None = None,
) -> AdsPerformanceResponse:
    snapshot = _build_cached_ads_snapshot(
        organization_id=organization_id or 1,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
    )

    totals = snapshot.totals if snapshot.rows else {}
    ad_spend = totals.get("ad_spend_kopecks")
    orders_kopecks = totals.get("orders_kopecks")
    drr_pct = _calc_drr_pct(ad_spend, orders_kopecks) if ad_spend is not None and orders_kopecks else None
    roi_pct = _calc_roi_pct(ad_spend, orders_kopecks) if ad_spend and orders_kopecks is not None else None
    romi_pct = roi_pct

    blocker_ids = snapshot.blocker_ids
    source_status = snapshot.source_status
    confidence_top = snapshot.confidence

    def to_row(index: int, row: AdsAttributionRow) -> AdsPerformanceRow:
        ad_spend_row = row.ad_spend_kopecks
        orders_row = row.orders_kopecks
        return AdsPerformanceRow(
            rowId=f"{group_by}-{row.campaign_id or 'unknown'}-{row.sku_id or index}",
            campaignId=row.campaign_id,
            skuId=row.sku_id,
            brandId=None,
            managerId=None,
            adSpendKopecks=ad_spend_row,
            impressions=row.impressions,
            clicks=row.clicks,
            cartAdds=row.cart_adds,
            ordersCount=row.orders_count,
            ordersKopecks=orders_row,
            drrPct=_calc_drr_pct(ad_spend_row, orders_row) if ad_spend_row is not None and orders_row else None,
            romiPct=_calc_roi_pct(ad_spend_row, orders_row) if ad_spend_row and orders_row is not None else None,
            roiPct=_calc_roi_pct(ad_spend_row, orders_row) if ad_spend_row and orders_row is not None else None,
            attributionLevel=row.attribution_level,
            confidence=row.confidence,
        )

    rows = [to_row(index, row) for index, row in enumerate(snapshot.rows, start=1)]

    return AdsPerformanceResponse(
        sourceStatus=source_status,
        confidence=confidence_top,
        blockerIds=blocker_ids,
        sourceEvidence=snapshot.source_evidence,
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        groupBy=group_by,
        totals=AdsTotals(
            adSpendKopecks=ad_spend,
            impressions=totals.get("impressions"),
            clicks=totals.get("clicks"),
            cartAdds=totals.get("cart_adds"),
            ordersCount=totals.get("orders_count"),
            ordersKopecks=orders_kopecks,
            drrPct=drr_pct,
            romiPct=romi_pct,
            roiPct=roi_pct,
        ),
        rows=rows,
        attributionPolicy=AdsAttributionPolicy(
            allowedSkuLevels=["exact_sku", "campaign_sku"],
            campaignOnlyCanAllocateToSkuPnl=False,
            notes=[
                "campaign_only attribution is kept campaign-level and is not allocated to SKU P&L",
                "weak attribution is returned with low/medium confidence only",
            ],
        ),
    )


@tax_consistent_report
def build_rnp_report(
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    finance_allowed: bool,
    organization_id: int = 1,
    wb_token: str | None = None,
    force_refresh: bool = False,
    progress_callback: Any | None = None,
) -> RnpReportResponse:
    snapshot = build_rnp_snapshot(
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        organization_id=organization_id,
        wb_token=wb_token,
        force_refresh=force_refresh,
        progress_callback=progress_callback,
    )
    from app.wb_rnp_finance import apply_financial_buyouts
    financial = apply_financial_buyouts(snapshot.rows, organization_id, date_from, date_to,
                                        finance_allowed=finance_allowed)
    rows = financial["rows"]
    goods = _goods_index(organization_id, include_catalog_photos=True) if rows else {}
    runtime = load_runtime_state(organization_id) or {}
    for row in rows:
        good = goods.get(row.nmId, {})
        article = str(good.get("vendorCode") or row.sku or "")
        meta = _sku_meta_from_state(article, runtime=runtime)
        row.categoryName = good.get("subjectName") or row.categoryName
        row.photoUrl = _photo_url_for_good(good, row.nmId or 0) or row.photoUrl
        row.managerId = meta.get("managerId")
        row.managerName = meta.get("managerName") or row.managerId
        assignment = (runtime.get("assignments") or {}).get(article) or {}
        row.strategyName = assignment.get("strategyName") or assignment.get("name") or FRONTEND_STRATEGY_BY_ID.get(assignment.get("strategyId"), {}).get("name")
        for characteristic in good.get("characteristics") or []:
            if isinstance(characteristic, dict) and str(characteristic.get("name", "")).lower() == "цвет":
                value = characteristic.get("value")
                row.colorName = ", ".join(map(str, value)) if isinstance(value, list) else str(value or "") or None
                break
    return RnpReportResponse(
        sourceStatus="partial" if rows and (snapshot.source_status == "blocked" or not financial["complete"]) else snapshot.source_status,
        confidence="medium" if rows and (snapshot.source_status == "blocked" or not financial["complete"]) else snapshot.confidence,
        blockerIds=snapshot.blocker_ids,
        sourceEvidence=snapshot.source_evidence,
        calculatedAt=snapshot.calculated_at,
        period=_period(date_from, date_to),
        groupBy=group_by,
        rows=rows,
        adSpendKopecks=snapshot.ad_spend_kopecks,
        drrPct=snapshot.drr_pct,
        formulaNotes=[*snapshot.formula_notes,
            "Выкупили: реализация retailAmount и количество за rrDate из финансового отчёта; возвраты вычтены.",
            "Процент выкупа и конверсии: исходная когорта заказов WB Sales Funnel, не отношение финансовых выкупов к заказам."],
        adsSourceStatus=snapshot.ads_source_status,
        diagnostics={**snapshot.diagnostics, "financialCoverage": financial["coverage"]},
    )


def _abc_letter(rank_index: int, total: int) -> str:
    if total <= 0:
        return "C"
    ratio = (rank_index + 1) / total
    if ratio <= 0.2:
        return "A"
    if ratio <= 0.5:
        return "B"
    return "C"


def _composite_metric(units: int, kopecks: int, delta_pct: float | None = None) -> dict[str, Any]:
    return {
        "units": units,
        "unitsLabel": "шт",
        "kopecks": kopecks,
        "deltaPct": delta_pct,
        "deltaLabel": f"{delta_pct:+.1f}%" if delta_pct is not None else "—",
    }


def _cache_aggregates(cache: dict[str, Any]) -> dict[str, dict[str, Any]]:
    aggregates = cache.get("aggregates")
    return aggregates if isinstance(aggregates, dict) else {}


def _stock_cache(organization_id: int, date_from: date, date_to: date) -> dict[str, Any]:
    period_stock = _period_cache(organization_id, "stocks", date_from, date_to)
    if period_stock:
        return period_stock
    return get_source_cache(organization_id, "stocks", slim=False) or {}


def _abc_filter_status(filters: str) -> str | None:
    for part in str(filters or "").split("&"):
        key, sep, value = part.partition("=")
        if sep and key.strip() == "status" and value.strip():
            return value.strip()
    return None


def _aggregate_sum(aggregates: dict[str, Any], *keys: str) -> int:
    total = 0
    for row in aggregates.values():
        if not isinstance(row, dict):
            continue
        total += sum(_int_or_zero(row.get(key)) for key in keys)
    return total


def _abc_group_rows(rows: list[dict[str, Any]], group_by: ReportGroupBy) -> list[dict[str, Any]]:
    if group_by == "sku":
        return rows
    key_by_group = {
        "brand": "brand",
        "manager": "manager",
        "category": "category",
        "status": "productStatus",
    }.get(group_by)
    if key_by_group is None:
        return rows

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row.get(key_by_group) or "Не задано")
        bucket = grouped.setdefault(
            label,
            {
                "sku": label,
                "label": label,
                "productStatus": row.get("productStatus"),
                "brand": row.get("brand"),
                "category": row.get("category"),
                "manager": row.get("manager"),
                "ordersUnits": 0,
                "ordersKopecks": 0,
                "salesUnits": 0,
                "salesKopecks": 0,
                "adSpendKopecks": 0,
                "commissionKopecks": 0,
                "logisticsKopecks": 0,
                "storageKopecks": 0,
                "acquiringKopecks": 0,
                "acceptanceKopecks": 0,
                "penaltyKopecks": 0,
                "deductionKopecks": 0,
                "additionalPaymentKopecks": 0,
                "financeCreditsKopecks": 0,
                "rewardAdjustmentKopecks": 0,
                "paymentScheduleKopecks": 0,
                "loyaltyCostKopecks": 0,
                "taxKopecks": 0,
                "otherExpensesKopecks": 0,
                "netTotalKopecks": 0,
                "wbStockUnits": 0,
                "baskets": 0,
                "impressions": 0,
                "clicks": 0,
                "sourceStatus": row.get("sourceStatus", "partial"),
                "confidence": row.get("confidence", "medium"),
            },
        )
        for key in (
            "ordersUnits",
            "ordersKopecks",
            "salesUnits",
            "salesKopecks",
            "adSpendKopecks",
            "commissionKopecks",
            "logisticsKopecks",
            "storageKopecks",
            "acquiringKopecks",
            "acceptanceKopecks",
            "penaltyKopecks",
            "deductionKopecks",
            "additionalPaymentKopecks",
            "financeCreditsKopecks",
            "rewardAdjustmentKopecks",
            "paymentScheduleKopecks",
            "loyaltyCostKopecks",
            "taxKopecks",
            "otherExpensesKopecks",
            "netTotalKopecks",
            "wbStockUnits",
            "baskets",
            "impressions",
            "clicks",
        ):
            _add_amount(bucket, key, row.get(key))
        for key in ("campaignAdSpendKopecks", "financeAdSpendKopecks"):
            if key not in bucket:
                bucket[key] = 0
            _add_amount(bucket, key, row.get(key))
        if bucket.get("factTaxReason") != "tax_policy_unavailable" and (row.get("factTaxState") == "missing" or bucket.get("factTaxState") != "missing"):
            bucket["factTaxState"] = row.get("factTaxState")
            bucket["factTaxReason"] = row.get("factTaxReason")

    result = []
    for bucket in grouped.values():
        sales = bucket["salesKopecks"]
        profit = bucket["netTotalKopecks"]
        bucket["marginPct"] = profit / sales * 100 if profit is not None and sales > 0 else None
        bucket["ordersComposite"] = _composite_metric(bucket["ordersUnits"], bucket["ordersKopecks"])
        bucket["salesComposite"] = _composite_metric(bucket["salesUnits"], bucket["salesKopecks"])
        bucket["ctrPct"] = bucket["clicks"] / bucket["impressions"] * 100 if bucket["impressions"] and bucket["clicks"] is not None else None
        bucket["cartCrPct"] = bucket["ordersUnits"] / bucket["baskets"] * 100 if bucket["baskets"] and bucket["ordersUnits"] is not None else None
        for amount, percent in (("commissionKopecks", "commissionCostPct"), ("logisticsKopecks", "logisticsCostPct"),
                                ("storageKopecks", "storageCostPct"), ("adSpendKopecks", "drrSalesPct")):
            bucket[percent] = bucket[amount] / sales * 100 if sales and bucket[amount] is not None else None
        bucket["abcCode"] = "BB"
        result.append(bucket)
    return sorted(result, key=lambda item: _int_or_zero(item.get("netTotalKopecks")), reverse=True)


def _promotion_label(promotion: dict[str, Any]) -> str:
    raw = promotion.get("name") or promotion.get("title") or promotion.get("promotionName")
    if raw:
        return str(raw)
    promotion_id = promotion.get("id") or promotion.get("promotionID") or promotion.get("promotionId")
    return f"Акция {promotion_id}" if promotion_id else "Акция WB"


def _promotion_int_values(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, dict):
        value = value.keys()
    if isinstance(value, (str, int, float)):
        value = [value]
    result: set[int] = set()
    if not isinstance(value, (list, tuple, set)):
        return result
    for item in value:
        try:
            numeric = int(item)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            result.add(numeric)
    return result


def _promotion_text_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (str, int, float)):
        value = [value]
    result: set[str] = set()
    if not isinstance(value, (list, tuple, set)):
        return result
    for item in value:
        text = str(item).strip()
        if text:
            result.add(text)
    return result


def _cached_promotions_for_abc(organization_id: int) -> list[dict[str, Any]]:
    promotions_cache = get_source_cache(organization_id, "promotions", slim=True) or {}
    raw_promotions = promotions_cache.get("promotions") if isinstance(promotions_cache, dict) else None
    if not isinstance(raw_promotions, list):
        return []

    thresholds_cache = get_source_cache(organization_id, "promotion_thresholds", slim=True) or {}
    by_promotion = thresholds_cache.get("byPromotionId") if isinstance(thresholds_cache, dict) else None
    by_promotion = by_promotion if isinstance(by_promotion, dict) else {}

    merged: list[dict[str, Any]] = []
    for promotion in raw_promotions:
        if not isinstance(promotion, dict):
            continue
        row = dict(promotion)
        promotion_id = row.get("id") or row.get("promotionID") or row.get("promotionId")
        imported = by_promotion.get(str(promotion_id)) if promotion_id is not None else None
        if isinstance(imported, dict):
            imported_nm_ids = _promotion_int_values(imported.get("thresholdNmIds"))
            if imported_nm_ids:
                row["thresholdNmIds"] = sorted(_promotion_int_values(row.get("thresholdNmIds")) | imported_nm_ids)
            if isinstance(imported.get("thresholdStatusesByNmId"), dict):
                statuses = dict(row.get("thresholdStatusesByNmId") or {})
                statuses.update(imported["thresholdStatusesByNmId"])
                row["thresholdStatusesByNmId"] = statuses
        merged.append(row)
    return merged


def _active_promotion_for_sku(article_id: str, nm_id: int, promotions: list[dict[str, Any]]) -> dict[str, Any] | None:
    article = str(article_id or "").strip()
    for status in ("active", "upcoming"):
        for promotion in promotions:
            if promotion.get("status") != status:
                continue
            if article and article in _promotion_text_values(promotion.get("articleIds")):
                return promotion
            nm_ids = _promotion_int_values(promotion.get("thresholdNmIds")) | _promotion_int_values(promotion.get("nmIds"))
            if nm_id > 0 and nm_id in nm_ids:
                return promotion
    return None


def _abc_promotion_lookup(promotions: list[dict[str, Any]]) -> dict[str, dict[Any, dict[str, Any]]]:
    lookup: dict[str, dict[Any, dict[str, Any]]] = {
        "activeArticle": {},
        "activeNm": {},
        "upcomingArticle": {},
        "upcomingNm": {},
    }
    for promotion in promotions:
        status = str(promotion.get("status") or "")
        if status not in {"active", "upcoming"}:
            continue
        article_key = f"{status}Article"
        nm_key = f"{status}Nm"
        for article in _promotion_text_values(promotion.get("articleIds")):
            lookup[article_key].setdefault(article, promotion)
        nm_ids = _promotion_int_values(promotion.get("thresholdNmIds")) | _promotion_int_values(promotion.get("nmIds"))
        for nm_id in nm_ids:
            lookup[nm_key].setdefault(nm_id, promotion)
    return lookup


def _abc_ktr_rows(organization_id: int) -> list[dict[str, Any]]:
    cache = get_source_cache(organization_id, "ktr_table", slim=False) or {}
    data = cache.get("data")
    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or data.get("rows")
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _abc_ktr_value(rows: list[dict[str, Any]], localization_pct: float | None) -> float | None:
    if localization_pct is None:
        return None
    for row in rows:
        lower = _first_float_or_none(row, "fromPct", "from", "min")
        upper = _first_float_or_none(row, "toPct", "to", "max")
        value = _first_float_or_none(row, "ktr", "ktrIndex", "value")
        if lower is not None and upper is not None and value is not None and lower <= localization_pct <= upper:
            return value
    return None


def _active_promotion_from_lookup(
    article_id: str,
    nm_id: int,
    lookup: dict[str, dict[Any, dict[str, Any]]],
) -> dict[str, Any] | None:
    article = str(article_id or "").strip()
    if article:
        promotion = lookup["activeArticle"].get(article) or lookup["upcomingArticle"].get(article)
        if promotion:
            return promotion
    if nm_id > 0:
        return lookup["activeNm"].get(nm_id) or lookup["upcomingNm"].get(nm_id)
    return None


def _build_abc_report_from_snapshots(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    filters: str,
    finance_allowed: bool,
    progress_callback: Any | None = None,
) -> AbcReportResponse | None:
    finance_cache = _period_cache(
        organization_id,
        "finance",
        date_from,
        date_to,
        require_current_finance_basis=True,
    )
    from app.wb_report_commission import with_commission_products
    finance_cache = with_commission_products(finance_cache, date_from, date_to)
    finance_basis_compatible = not finance_cache or finance_cache_uses_current_revenue_basis(finance_cache)
    finance_source_available = finance_allowed and bool(finance_cache) and finance_basis_compatible
    finance_aggregates = _cache_aggregates(finance_cache) if finance_source_available else {}
    finance_taxes = get_legacy_finance_taxes(organization_id, finance_cache, Period(date_from, date_to), include_costs=True, use_current_settings=True) if finance_source_available else {}

    ads_cache = _period_cache(organization_id, "ads", date_from, date_to)
    ads_source_available = bool(ads_cache)
    ads_aggregates = _cache_aggregates(ads_cache)
    period_stats_cache = _period_cache(organization_id, "period_stats", date_from, date_to)
    period_stats_aggregates = _cache_aggregates(period_stats_cache)
    spp_by_nm = abc_spp_by_nm(organization_id, date_from, date_to, fallback_cache=period_stats_cache)
    baskets_cache = _period_cache(organization_id, "baskets", date_from, date_to)
    baskets_aggregates = _cache_aggregates(baskets_cache)
    stock_cache = _stock_cache(organization_id, date_from, date_to)
    stock_aggregates = _cache_aggregates(stock_cache)
    content_cards_cache = get_source_cache(organization_id, "content_cards", slim=False) or {}
    goods_by_nm = _goods_index(organization_id, content_cards_cache)
    status_filter = _abc_filter_status(filters)
    blockers: list[str] = list(finance_cache.get("projectionBlockers") or [])
    if not finance_allowed:
        blockers.append("WB_ABC_FINANCE_FORBIDDEN")
    elif finance_cache and not finance_basis_compatible:
        blockers.append("WB_ABC_FINANCE_REVENUE_BASIS_INCOMPATIBLE")
    elif not finance_cache:
        blockers.append("WB_ABC_FINANCE_CACHE_MISSING")
    if not baskets_cache:
        blockers.append("WB_ABC_SALES_FUNNEL_CACHE_MISSING")
    if not ads_cache:
        blockers.append("WB_ABC_ADS_CACHE_MISSING")
    if not period_stats_cache:
        blockers.append("WB_ABC_PERIOD_STATS_CACHE_MISSING")
    if not stock_cache:
        blockers.append("WB_ABC_STOCK_CACHE_MISSING")
    if not content_cards_cache:
        blockers.append("WB_ABC_CONTENT_CARDS_CACHE_MISSING")
    if any(
        _int_or_zero(row.get("sellerRevenueMissingRows")) > 0
        for row in finance_aggregates.values()
        if isinstance(row, dict)
    ):
        blockers.append("WB_ABC_RETAIL_AMOUNT_MISSING")
    ktr_rows = _abc_ktr_rows(organization_id)
    if not ktr_rows:
        blockers.append("WB_ABC_KTR_TABLE_MISSING")
    source_status: Literal["fresh", "partial", "stale", "blocked", "unknown"] = "fresh" if finance_allowed and not blockers else "partial"
    confidence: Literal["high", "medium", "low", "blocked"] = "high" if source_status == "fresh" else "medium"
    rows: list[dict[str, Any]] = []
    cogs_evidence_statuses: set[str] = set()
    promotions = _cached_promotions_for_abc(organization_id)
    promotion_lookup = _abc_promotion_lookup(promotions)
    runtime = load_runtime_state(organization_id) or {}
    algorithm = dict(DEFAULT_ALGORITHM_SETTINGS)
    algorithm.update(load_algorithm_settings(organization_id) or {})

    row_sources = (finance_aggregates, baskets_aggregates, period_stats_aggregates, ads_aggregates, stock_aggregates)
    all_nm_ids = sorted(
        {
            str(raw_nm_id)
            for source in row_sources
            for raw_nm_id, aggregate in source.items()
            if isinstance(aggregate, dict) and _int_or_zero(raw_nm_id) > 0
        }
        | {str(nm_id) for nm_id in goods_by_nm},
        key=lambda value: _int_or_zero(value),
    )
    total_nm_ids = len(all_nm_ids)
    if progress_callback:
        progress_callback({"phase": "abc-build", "processed": 0, "total": total_nm_ids})
    for index, raw_nm_id in enumerate(all_nm_ids, start=1):
        nm_id = _int_or_zero(raw_nm_id)
        if nm_id <= 0:
            continue
        finance = finance_aggregates.get(str(nm_id)) or {}
        good = goods_by_nm.get(nm_id, {})
        baskets = baskets_aggregates.get(str(nm_id)) or {}
        article_id = str(good.get("vendorCode") or good.get("vendor_code") or finance.get("vendorCode") or baskets.get("vendorCode") or f"NM_{nm_id}")
        settings = _sku_settings_from_state(article_id, runtime=runtime, algorithm=algorithm, effective_on=date_to)
        meta = _sku_meta_from_state(article_id, runtime=runtime)
        product_status = str(meta.get("status") or "unknown")
        if status_filter and product_status != status_filter:
            continue

        stock = stock_aggregates.get(str(nm_id)) or {}
        period_stats = period_stats_aggregates.get(str(nm_id)) or {}
        ads = ads_aggregates.get(str(nm_id)) or {}

        funnel_orders_units = _first_nonnegative_int_or_none(baskets, "orderCount", "ordersCount", "orders")
        funnel_orders_kopecks = _first_nonnegative_int_or_none(baskets, "orderSumKopecks", "ordersKopecks")
        finance_sales_units = (
            _int_or_zero(finance.get("netSalesUnits"))
            if "netSalesUnits" in finance
            else _int_or_zero(finance.get("salesUnits")) - _int_or_zero(finance.get("returnsUnits"))
        )
        sales_units_for_costs = finance_sales_units if finance else 0
        sales_units = sales_units_for_costs
        orders_units = funnel_orders_units if funnel_orders_units is not None else 0
        orders_kopecks = funnel_orders_kopecks if funnel_orders_kopecks is not None else 0
        from app.repricer_page_finance import report_revenue_kopecks
        sales_kopecks = _int_or_zero(report_revenue_kopecks(finance))
        complete_ledger = finance_source_available and _complete_finance_ledger(finance_cache, date_from, date_to)
        finance_row_complete = finance_source_available and (bool(finance) or complete_ledger) and _int_or_zero(finance.get("sellerRevenueMissingRows")) == 0
        finance_tax = finance_taxes.get(str(nm_id), {"taxKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_revenue_basis_unconfirmed"})
        # Absence from an exhaustively collected ledger means no settled sales,
        # not a missing unit cost. Never infer this from partial/undated caches.
        if complete_ledger and finance_cache.get("reportMetricVersion") and finance_cache.get("financialAdvertisingComplete") is not False:
            finance = {**finance, "financeAdSpendAuthoritative": True}
        if str(nm_id) not in finance_aggregates and complete_ledger:
            finance = {**finance, "reportedCommissionKopecks": 0, "reportedCommissionComplete": True}
            finance_tax = {'taxKopecks': 0, 'factTaxState': 'configured', 'factTaxReason': None,
                           'settlementCogsKopecks': 0, 'settlementCogsState': 'configured'}
        ad_spend_available = finance_row_complete and bool(finance.get('financeAdSpendAuthoritative'))
        profit_row_complete = complete_ledger and finance_row_complete and ad_spend_available and finance_tax.get("taxKopecks") is not None and finance_tax.get("factTaxState") == "configured"
        cogs_total = finance_tax.get("settlementCogsKopecks")
        cogs_settings = {"cogsSource": "canonical_cost_history", "cogsEvidenceStatus": "dated" if cogs_total is not None else "missing", "cogsEffectiveFrom": finance_tax.get("costEffectiveFrom")}
        profit_row_complete = (profit_row_complete and cogs_total is not None
            and not finance_cache.get("projectionBlockers")
            and finance.get("settlementComponentsConfirmed") is not False)
        cogs_evidence_statuses.add(str(cogs_settings.get("cogsEvidenceStatus") or "undated"))
        financial = _abc_financial_components(
            finance=finance,
            settings=settings,
            ads=ads,
            revenue_kopecks=sales_kopecks,
            sales_units=sales_units_for_costs,
            finance_tax=finance_tax,
            cogs_kopecks=cogs_total,
            canonical_costs=True,
        )
        profit_row_complete = profit_row_complete and financial["commissionKopecks"] is not None
        cogs = financial["cogsKopecks"]
        commission = financial["commissionKopecks"]
        logistics = financial["logisticsKopecks"]
        storage = financial["storageKopecks"]
        acceptance = financial["acceptanceKopecks"]
        penalty = financial["penaltyKopecks"]
        deduction = financial["deductionKopecks"]
        finance_credits = financial["financeCreditsKopecks"]
        finance_other_expenses = financial["financeOtherExpensesKopecks"]
        compensation = financial["compensationKopecks"]
        acquiring = financial["acquiringKopecks"]
        loyalty_cost = financial["loyaltyCostKopecks"]
        ad_spend = financial["adSpendKopecks"]
        tax = financial["taxKopecks"]
        overhead = financial["otherExpensesKopecks"]
        net_profit = financial["netProfitKopecks"]
        gross_margin = sales_kopecks - cogs if cogs is not None else None
        funnel_baskets = _abc_funnel_cart_count(baskets)
        baskets_count = funnel_baskets if funnel_baskets is not None else 0
        funnel_impressions = _abc_funnel_impressions(baskets)
        funnel_opens = _abc_funnel_open_count(baskets)
        traffic_impressions = funnel_impressions
        traffic_clicks = funnel_opens
        funnel_stock_units = _first_nonnegative_int_or_none(baskets, "wbStockUnits")
        stock_data_available = funnel_stock_units is not None or bool(stock)
        stock_units = funnel_stock_units if funnel_stock_units is not None else max(0, _int_or_zero(stock.get("wbStockUnits") or stock.get("stockUnits") or stock.get("quantity")))
        price_before_spp = _good_seller_price_kopecks(good)
        price_with_spp = _good_buyer_price_no_wallet_kopecks(good)
        cogs_per_unit = round(cogs_total / sales_units_for_costs) if cogs_total is not None and sales_units_for_costs > 0 else None
        cart_to_order_pct = orders_units / baskets_count * 100 if baskets_count > 0 else None
        clicks_delta_pct = _first_float_or_none(baskets, "openCountDeltaPct", "clicksDeltaPct")
        baskets_delta_pct = _first_float_or_none(baskets, "cartCountDeltaPct", "basketsDeltaPct")
        orders_delta_pct = _first_float_or_none(baskets, "orderCountDeltaPct", "ordersDeltaPct")
        sales_delta_pct = _first_float_or_none(finance, "salesDeltaPct")
        buyout_count = _first_nonnegative_int_or_none(baskets, "buyoutCount", "buyoutsCount", "buyouts")
        buyout_pct = buyout_count / orders_units * 100 if buyout_count is not None and orders_units > 0 else None
        gross_sales_units = _nonnegative_int(finance.get("salesUnits"))
        returns_units = _nonnegative_int(finance.get("returnsUnits"))
        gross_sales_kopecks = _nonnegative_int(finance.get("realizationGrossSalesKopecks", finance.get("grossSalesKopecks")))
        returns_kopecks = _nonnegative_int(finance.get("realizationReturnsKopecks", finance.get("returnsKopecks")))
        average_sale_price = gross_sales_kopecks / gross_sales_units if gross_sales_units > 0 else None
        cancelled_orders_units = _nonnegative_int(period_stats.get("cancelledOrdersUnits")) if period_stats else None
        turnover_days = stock_units * max(1, (date_to - date_from).days + 1) / sales_units if stock_units >= 0 and sales_units > 0 else None
        localization_pct = _first_float_or_none(baskets, "localizationPct", "localizationPercent")
        promotion = _active_promotion_from_lookup(article_id, nm_id, promotion_lookup)
        promotion_name = _promotion_label(promotion) if promotion else None
        promotion_id = (
            promotion.get("id") or promotion.get("promotionID") or promotion.get("promotionId")
            if promotion
            else None
        )

        row = {
            "sku": article_id,
            "nmId": nm_id,
            "photoUrl": _photo_url_for_good(good, nm_id),
            "productName": str(good.get("title") or good.get("name") or baskets.get("productName") or article_id),
            "productStatus": product_status,
            "managerId": meta.get("managerId"),
            "manager": meta.get("managerName") or meta.get("managerId") or "Не задано",
            "brand": str(good.get("brand") or good.get("brandName") or baskets.get("brand") or meta.get("brand") or "Не задано"),
            "category": str(good.get("subjectName") or good.get("subject") or baskets.get("category") or "Не задано"),
            "priceBeforeSppKopecks": price_before_spp,
            "priceWithSppKopecks": price_with_spp,
            **spp_by_nm.get(str(nm_id), {}),
            "averageSalePriceKopecks": average_sale_price,
            "cogsPerUnitKopecks": cogs_per_unit,
            "cogsKopecks": cogs if finance_source_available else None,
            "cogsSource": cogs_settings.get("cogsSource"),
            "cogsEffectiveFrom": cogs_settings.get("cogsEffectiveFrom"),
            "cogsEvidenceStatus": cogs_settings.get("cogsEvidenceStatus"),
            "grossMarginKopecks": gross_margin if finance_row_complete else None,
            "grossMarginPct": gross_margin / sales_kopecks * 100 if finance_row_complete and gross_margin is not None and sales_kopecks > 0 else None,
            "profitabilityPct": net_profit / sales_kopecks * 100 if profit_row_complete and sales_kopecks > 0 else None,
            "marginPct": net_profit / sales_kopecks * 100 if profit_row_complete and sales_kopecks > 0 else None,
            "marginKopecks": (int(round(net_profit / max(sales_units_for_costs, 1))) if sales_units_for_costs else net_profit) if profit_row_complete else None,
            "marginDeltaPct": None,
            "impressions": traffic_impressions,
            "clicks": traffic_clicks,
            "clicksDeltaPct": clicks_delta_pct,
            "ctrPct": traffic_clicks / traffic_impressions * 100 if traffic_impressions and traffic_clicks is not None else None,
            "baskets": baskets_count if baskets else None,
            "cartCrPct": cart_to_order_pct,
            "basketsDeltaPct": baskets_delta_pct,
            "ordersUnits": orders_units,
            "ordersDeltaPct": orders_delta_pct,
            "ordersKopecks": orders_kopecks,
            "cancelledOrdersUnits": cancelled_orders_units,
            "ordersComposite": _composite_metric(orders_units, orders_kopecks, orders_delta_pct) if baskets else None,
            "salesUnits": sales_units if finance_row_complete else None,
            "salesDeltaPct": sales_delta_pct,
            "salesKopecks": sales_kopecks if finance_row_complete else None,
            "grossSalesUnits": gross_sales_units if finance_row_complete else None,
            "returnsUnits": returns_units if finance_row_complete else None,
            "grossSalesKopecks": gross_sales_kopecks if finance_row_complete else None,
            "returnsKopecks": returns_kopecks if finance_row_complete else None,
            "salesComposite": _composite_metric(sales_units, sales_kopecks, sales_delta_pct) if finance_row_complete else None,
            "adSpendKopecks": ad_spend if ad_spend_available else None,
            "campaignAdSpendKopecks": ads.get("adSpendKopecks", 0 if ads_cache.get("sourceComplete") is True else None),
            "financeAdSpendKopecks": ad_spend if finance.get("financeAdSpendAuthoritative") else None,
            "adSpendSource": "wb_finance_report" if ad_spend_available else None,
            "commissionKopecks": commission if finance_row_complete else None,
            "commissionSource": finance.get("reportedCommissionSource"),
            "logisticsKopecks": logistics if finance_row_complete else None,
            "storageKopecks": storage if finance_row_complete else None,
            "storageAttribution": finance.get("storageAttribution", "wb_finance_report"),
            "acquiringKopecks": acquiring if finance_row_complete else None,
            "acceptanceKopecks": acceptance if finance_row_complete else None,
            "penaltyKopecks": penalty if finance_row_complete else None,
            "deductionKopecks": deduction if finance_row_complete else None,
            "additionalPaymentKopecks": _int_or_zero(finance.get("additionalPaymentKopecks")) if finance_row_complete else None,
            "financeCreditsKopecks": finance_credits if finance_row_complete else None,
            "financeOtherExpensesKopecks": finance_other_expenses if finance_row_complete else None,
            "totalOtherExpensesKopecks": overhead + finance_other_expenses if finance_row_complete else None,
            "compensationKopecks": compensation if finance_row_complete else None,
            "rewardAdjustmentKopecks": _int_or_zero(finance.get("rewardAdjustmentKopecks")) if finance_row_complete else None,
            "paymentScheduleKopecks": _int_or_zero(finance.get("paymentScheduleKopecks")) if finance_row_complete else None,
            "loyaltyCostKopecks": loyalty_cost if finance_row_complete else None,
            "taxKopecks": tax if finance_row_complete else None,
            "factTaxState": finance_tax["factTaxState"],
            "factTaxReason": finance_tax.get("factTaxReason"),
            "otherExpensesKopecks": overhead if finance_row_complete else None,
            "drrOrdersPct": ad_spend / orders_kopecks * 100 if complete_ledger and ad_spend_available and orders_kopecks > 0 else None,
            "drrSalesPct": ad_spend / sales_kopecks * 100 if complete_ledger and ad_spend_available and sales_kopecks > 0 else None,
            "netPerUnitKopecks": (int(round(net_profit / max(sales_units_for_costs, 1))) if sales_units_for_costs else net_profit) if profit_row_complete else None,
            "netTotalKopecks": net_profit if profit_row_complete else None,
            "logisticsCostPct": logistics / sales_kopecks * 100 if sales_kopecks > 0 else None,
            "logisticsDeltaPct": None,
            "commissionCostPct": commission / sales_kopecks * 100 if commission is not None and sales_kopecks > 0 else None,
            "commissionDeltaPct": None,
            "storageCostPct": storage / sales_kopecks * 100 if sales_kopecks > 0 else None,
            "storageDeltaPct": None,
            "ktrIndex": _abc_ktr_value(ktr_rows, localization_pct),
            "localizationPct": localization_pct,
            "wbStockUnits": stock_units if stock_data_available else None,
            "wbStockKopecks": stock_units * price_with_spp if stock_data_available and price_with_spp is not None else None,
            "turnoverDays": turnover_days if stock_data_available else None,
            "daysToOos": turnover_days if stock_data_available else None,
            "promotionStatus": "yes" if promotion else "no",
            "promotionStatusText": promotion_name,
            "promotionName": promotion_name,
            "promotionId": promotion_id,
            "promotionType": promotion.get("type") if promotion else None,
            "abcCode": "CC",
            "buyoutPct": buyout_pct,
            "sourceStatus": source_status,
            "confidence": confidence,
        }
        rows.append(row)
        if progress_callback and (index % 100 == 0 or index == total_nm_ids):
            progress_callback({"phase": "abc-build", "processed": index, "total": total_nm_ids})

    if "missing" in cogs_evidence_statuses or "undated" in cogs_evidence_statuses:
        blockers.append("WB_ABC_COGS_EFFECTIVE_DATE_MISSING")
    if finance_cache.get('sourceComplete') is False or finance_cache.get('coverageState') in {'partial', 'missing', 'blocked'}:
        blockers.append('WB_FINANCE_SOURCE_INCOMPLETE')
    if any(item.get('settlementCogsState') == 'assumed' for item in finance_taxes.values()):
        blockers.append('WB_USER_EXAMPLE_COSTS')
    if "period_end_fallback" in cogs_evidence_statuses:
        blockers.append("WB_ABC_COGS_DAILY_COVERAGE_MISMATCH")
    if any(row.get("factTaxState") == "missing" for row in rows):
        blockers.append("WB_ABC_TAX_UNCONFIRMED")
    source_status = "fresh" if finance_allowed and not blockers else "partial"
    confidence = "high" if source_status == "fresh" else "medium"
    for row in rows:
        row["sourceStatus"] = source_status
        row["confidence"] = confidence
        row["blockerIds"] = list(blockers)

    sales_rank = sorted(rows, key=lambda row: (_int_or_zero(row.get("salesKopecks")), _int_or_zero(row.get("salesUnits")), _int_or_zero(row.get("ordersUnits"))), reverse=True)
    profit_rank = sorted(rows, key=lambda row: _int_or_zero(row.get("netTotalKopecks")), reverse=True)
    sales_letters = {row["nmId"]: _abc_letter(index, len(sales_rank)) for index, row in enumerate(sales_rank)}
    profit_letters = {row["nmId"]: _abc_letter(index, len(profit_rank)) for index, row in enumerate(profit_rank)}
    all_profit_known = all(item.get('netTotalKopecks') is not None for item in rows)
    for row in rows:
        row["abcCode"] = f"{sales_letters.get(row['nmId'], 'C')}{profit_letters.get(row['nmId'], 'C') if all_profit_known else '—'}"

    rows = sorted(rows, key=lambda row: (_int_or_zero(row.get("salesKopecks")), _int_or_zero(row.get("netTotalKopecks"))), reverse=True)
    output_rows = _abc_group_rows(rows, group_by)
    unallocated_row = _abc_unallocated_row({**finance_cache,
        "unallocatedTaxKopecks": _unallocated_expense_tax(finance_cache, finance_taxes)}) if finance_allowed else None
    if unallocated_row is not None:
        rows.append(unallocated_row)
        # SKU grouping returns the same list; never append the residual twice.
        if output_rows is not rows:
            output_rows.append(unallocated_row)
    finance_values_complete = finance_source_available and "WB_ABC_RETAIL_AMOUNT_MISSING" not in blockers
    profit_values_complete = finance_values_complete and all(row.get("netTotalKopecks") is not None for row in rows)
    total_orders = sum(_int_or_zero(row.get("ordersUnits")) for row in rows) if baskets_cache else None
    total_orders_kopecks = sum(_int_or_zero(row.get("ordersKopecks")) for row in rows) if baskets_cache else None
    total_profit = sum(_int_or_zero(row.get("netTotalKopecks")) for row in rows) if profit_values_complete else None
    total_sales = sum(_int_or_zero(row.get("salesKopecks")) for row in rows) if finance_values_complete else None
    total_returns = sum(_int_or_zero(row.get("returnsKopecks")) for row in rows) if finance_values_complete else None
    total_baskets = sum(_int_or_zero(row.get("baskets")) for row in rows) if baskets_cache else None
    total_ad_spend = sum(_int_or_zero(row.get("adSpendKopecks")) for row in rows) if (ads_source_available or rows) and all(row.get('adSpendKopecks') is not None for row in rows) else None

    source_evidence = [
        SourceEvidence(
            sourceId="wb-finance-sales-reports-detailed-cache",
            sourceType="wb_api",
            sourceName="WB Finance /api/finance/v1/sales-reports/detailed snapshot from repricer sync",
            lastSyncedAt=_parse_cache_datetime(finance_cache.get("fetchedAt")) or utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=["salesUnits", "returnsUnits", "netSalesUnits", "grossSalesKopecks", "returnsKopecks", "sellerRevenueKopecks", "commissionKopecks", "logisticsKopecks", "storageKopecks", "acceptanceKopecks", "penaltyKopecks", "deductionKopecks", "additionalPaymentKopecks", "loyaltyCostKopecks", "acquiringKopecks"],
        ),
        SourceEvidence(
            sourceId="wb-sales-funnel-products-cache",
            sourceType="wb_api",
            sourceName="WB Analytics /api/analytics/v3/sales-funnel/products",
            lastSyncedAt=_parse_cache_datetime(baskets_cache.get("fetchedAt")) or utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=["openCount", "cartCount", "orderCount", "orderSumKopecks", "buyoutCount", "localizationPct"],
        ),
        SourceEvidence(
            sourceId="wb-period-stats-cache",
            sourceType="wb_api",
            sourceName="WB Statistics orders/sales period snapshot",
            lastSyncedAt=_parse_cache_datetime(period_stats_cache.get("fetchedAt")) or utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=["cancelledOrdersUnits"],
        ),
        SourceEvidence(
            sourceId="wb-ads-cache",
            sourceType="wb_api",
            sourceName="WB Promotion campaign statistics",
            lastSyncedAt=_parse_cache_datetime(ads_cache.get("fetchedAt")) or utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=["adSpendKopecks"],
        ),
        SourceEvidence(
            sourceId="wb-current-goods-cache",
            sourceType="wb_api",
            sourceName="WB goods/prices and content cards current snapshots",
            lastSyncedAt=_parse_cache_datetime(content_cards_cache.get("fetchedAt")) or utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=["vendorCode", "nmID", "title", "photoUrl", "brand", "subjectName", "priceBeforeSppKopecks", "priceWithSppKopecks"],
        ),
        SourceEvidence(
            sourceId="platform-ktr-table",
            sourceType="manual",
            sourceName="Organization KTR lookup table",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=["ktrIndex"],
        ),
        SourceEvidence(
            sourceId="platform-abc-settings",
            sourceType="manual",
            sourceName="Organization SKU settings, COGS history and ABC algorithm rules",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=1440,
            fieldsUsed=["productStatus", "managerId", "cogsKopecks", "cogsEffectiveFrom", "taxPct", "otherExpensePricePct"],
        ),
        *_evidence("wb-abc-derived", "derived", "ABC calculations from canonical snapshots", ["abcCode", "averageSalePriceKopecks", "cartCrPct", "grossMarginKopecks", "profitabilityPct", "turnoverDays"]),
    ]

    return AbcReportResponse(
        financialCoverage=_financial_coverage(finance_cache),
        unallocatedFinancials=finance_cache.get("unallocatedFinancials", {}) if finance_allowed else {},
        storageAllocation=finance_cache.get("storageAllocation", {}) if finance_allowed else {},
        sourceStatus=source_status,
        confidence=confidence,
        blockerIds=blockers,
        sourceEvidence=source_evidence,
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        filteredSummary=AbcFilteredSummary(
            filterHash=sha1(f"{date_from}:{date_to}:{group_by}:{filters}".encode("utf-8")).hexdigest()[:12],
            skuCount=sum(not row.get("isUnallocated") for row in rows),
            locomotiveCount=sum(1 for row in rows if str(row.get("productStatus")) == "locomotive"),
            basketsCount=total_baskets,
            ordersCount=total_orders,
            ordersKopecks=total_orders_kopecks,
            salesKopecks=total_sales,
            returnsKopecks=total_returns,
            profitKopecks=total_profit,
            marginPct=total_profit / total_sales * 100 if total_profit is not None and total_sales is not None and total_sales > 0 else None,
            adSpendKopecks=total_ad_spend,
            sourceStatus=source_status,
            confidence=confidence,
        ),
        rows=output_rows,
    )


def _empty_abc_report_from_snapshot_miss(date_from: date, date_to: date, group_by: ReportGroupBy, filters: str) -> AbcReportResponse:
    return AbcReportResponse(
        sourceStatus="partial",
        confidence="low",
        blockerIds=["WB_ABC_FINANCE_CACHE_MISSING"],
        sourceEvidence=_evidence(
            "wb-abc-runtime-cache-miss",
            "derived",
            "ABC report did not find repricer finance snapshot for selected period",
            ["finance", "period_stats", "ads", "baskets", "stocks"],
        ),
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        filteredSummary=AbcFilteredSummary(
            filterHash=sha1(f"{date_from}:{date_to}:{group_by}:{filters}".encode("utf-8")).hexdigest()[:12],
            skuCount=0,
            locomotiveCount=None,
            ordersCount=None,
            ordersKopecks=None,
            profitKopecks=None,
            marginPct=None,
            adSpendKopecks=None,
            sourceStatus="partial",
            confidence="low",
        ),
        rows=[],
    )


@tax_consistent_report
def build_abc_report(
    date_from: date,
    date_to: date,
    group_by: ReportGroupBy,
    filters: str,
    finance_allowed: bool,
    organization_id: int | None = None,
    progress_callback: Any | None = None,
) -> AbcReportResponse:
    if organization_id is not None:
        snapshot_report = _build_abc_report_from_snapshots(
            organization_id=organization_id,
            date_from=date_from,
            date_to=date_to,
            group_by=group_by,
            filters=filters,
            finance_allowed=finance_allowed,
            progress_callback=progress_callback,
        )
        if snapshot_report is not None:
            return snapshot_report
        return _empty_abc_report_from_snapshot_miss(date_from, date_to, group_by, filters)

    filter_hash = sha1(f"{date_from}:{date_to}:{group_by}:{filters}".encode("utf-8")).hexdigest()[:12]
    ad_spend = 420_000
    blockers: list[str] = []

    return AbcReportResponse(
        sourceStatus="partial",
        confidence="medium",
        blockerIds=blockers,
        sourceEvidence=_evidence(
            "wb-abc-runtime",
            "derived",
            "ABC filtered summary runtime layer",
            ["filterHash", "skuCount", "ordersKopecks", "profitKopecks", "adSpendKopecks"],
        ),
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        filteredSummary=AbcFilteredSummary(
            filterHash=filter_hash,
            skuCount=11 if filters else 32,
            locomotiveCount=4 if filters else 9,
            ordersCount=92 if filters else 261,
            ordersKopecks=1_140_000 if filters else 3_320_000,
            profitKopecks=273_000,
            marginPct=23.95,
            adSpendKopecks=ad_spend,
            sourceStatus="partial",
            confidence="medium",
        ),
        rows=[],
    )


def build_plan_fact_report(date_from: date, date_to: date, dimension: PlanFactDimension, finance_allowed: bool) -> PlanFactResponse:
    days_left = max(1, (date_to - date_from).days // 2)
    plan = 5_000_000
    fact = 3_250_000
    deviation = fact - plan
    completion = round(fact / plan * 100, 2)
    forecast = 4_960_000
    min_per_day = int((plan - fact) / days_left)

    blockers: list[str] = ["WB-14A"]
    source_status: Literal["fresh", "partial", "stale", "blocked", "unknown"] = "partial"
    conf: Literal["high", "medium", "low", "blocked"] = "medium"

    row = PlanFactRow(
        rowId=f"{dimension}-maria",
        dimension=dimension,
        ownerId="maria" if dimension != "company" else "company",
        ownerLabel="Мария" if dimension != "company" else "Компания",
        metric="margin_profit_kopecks",
        planKopecks=plan,
        factKopecks=fact,
        deviationKopecks=deviation,
        completionPct=completion,
        forecastKopecks=forecast,
        minPerDayKopecks=min_per_day,
        sourceStatus=source_status,
        confidence=conf,
        blockerIds=[],
    )

    return PlanFactResponse(
        sourceStatus=source_status,
        confidence=conf,
        blockerIds=blockers,
        sourceEvidence=_evidence(
            "wb-plan-fact-runtime",
            "derived",
            "Plan-fact runtime layer",
            ["plan", "fact", "deviation", "completion", "forecast", "minPerDay"],
        ),
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        dimension=dimension,
        rows=[row],
    )


def build_sku_rating_report(date_from: date, date_to: date, finance_allowed: bool) -> SkuRatingResponse:
    blockers: list[str] = []
    source_status: Literal["fresh", "partial", "stale", "blocked", "unknown"] = "partial"
    confidence: Literal["high", "medium", "low", "blocked"] = "medium"
    reasons = [
        "ABC sales class A",
        "healthy conversion and basket trend",
    ]
    row = RatingRow(
        skuId="FBBT_42",
        nmId=123456,
        group="locomotive",
        score=84,
        scoreReasons=reasons,
        links=[
            RatingLink(kind="sku_drawer", href="/wb/repricer/sku/FBBT_42"),
            RatingLink(kind="repricer", href="/wb/repricer?sku=FBBT_42"),
            RatingLink(kind="liquidation", href="/wb/repricer/liquidation?sku=FBBT_42"),
        ],
        sourceStatus=source_status,
        confidence=confidence,
        blockerIds=blockers,
    )

    return SkuRatingResponse(
        sourceStatus=source_status,
        confidence=confidence,
        blockerIds=blockers,
        sourceEvidence=_evidence(
            "wb-sku-rating-runtime",
            "derived",
            "SKU rating runtime layer",
            ["group", "score", "scoreReasons", "links"],
        ),
        calculatedAt=utc_now(),
        period=_period(date_from, date_to),
        rows=[row],
    )


def build_export_response(finance_allowed: bool, filters: str, period_from: date, period_to: date) -> ExportCurrentViewResponse:
    file_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_name = f"wb_report_export_{file_stamp}.xlsx" if finance_allowed else f"wb_report_export_limited_{file_stamp}.xlsx"
    return ExportCurrentViewResponse(
        exportState="ready",
        reason=None if finance_allowed else "monthly company costs are masked for this permission set",
        fileName=file_name,
        format="xlsx",
        sourceStatus="fresh" if finance_allowed else "partial",
        confidence="high" if finance_allowed else "medium",
        blockerIds=[],
        sourceEvidence=_evidence(
            "wb-export-runtime" if finance_allowed else "wb-export-runtime-limited",
            "derived",
            "Export current view metadata and visibility policy",
            ["filters", "period", "columns", "sourceFreshness", "sourceConfidence", "visibilityPolicy"],
        ),
        generatedAt=utc_now(),
    )
