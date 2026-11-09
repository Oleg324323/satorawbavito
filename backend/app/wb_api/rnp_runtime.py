from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import time
from typing import Any, Callable

from vella_wb_19_05.models import Confidence, RnpRow, SourceEvidence, SourceStatus, utc_now

from app.config import get_settings
from app.repricer_sync import _normalize_period_range, _period_cache_suffix
from app.wb_api.ads_runtime import AdsAttributionRow
from app.wb_api.client import RateLimitedWbApiClient, WbApiRequest, build_wb_analytics_client


RNP_REPORT_CACHE_VERSION = "v8"
RNP_FUNNEL_CACHE_VERSION = "v2"
RNP_FUNNEL_PAGE_LIMIT = 1000
RNP_FUNNEL_MAX_ATTEMPTS = 3
RNP_FUNNEL_RETRY_FALLBACK_SECONDS = 60.0
RNP_FUNNEL_MAX_RETRY_AFTER_SECONDS = 180.0


@dataclass(frozen=True)
class RnpSnapshot:
    source_status: SourceStatus
    confidence: Confidence
    blocker_ids: list[str]
    source_evidence: list[SourceEvidence]
    calculated_at: datetime
    rows: list[RnpRow]
    ad_spend_kopecks: int
    drr_pct: float | None
    formula_notes: list[str]
    ads_source_status: SourceStatus
    cache_status: str
    diagnostics: dict[str, Any]


def get_source_cache(organization_id: int, source_key: str, slim: bool = True) -> dict[str, Any] | None:
    from app.repricer_cache.store import get_source_cache as _get_source_cache

    return _get_source_cache(organization_id, source_key, slim=slim)


def save_source_cache(organization_id: int, source_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    from app.repricer_cache.store import save_source_cache as _save_source_cache

    return _save_source_cache(organization_id, source_key, payload)


def list_source_cache_ranges_by_prefix(organization_id: int, source_key_prefix: str, limit: int = 200) -> list[dict[str, Any]]:
    from app.repricer_cache.store import list_source_cache_ranges_by_prefix as _list_ranges

    return _list_ranges(organization_id, source_key_prefix, limit=limit)


def rnp_funnel_cache_key(date_from: date, date_to: date) -> str:
    return f"rnp_funnel_{RNP_FUNNEL_CACHE_VERSION}_{date_from.isoformat()}_{date_to.isoformat()}"


def rnp_report_cache_key(date_from: date, date_to: date, group_by: str) -> str:
    return f"rnp_report_{RNP_REPORT_CACHE_VERSION}_{date_from.isoformat()}_{date_to.isoformat()}_{group_by}"


def _parse_cache_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _cache_covers_range(cache: dict[str, Any], date_from: date, date_to: date) -> bool:
    cache_from = _parse_cache_date(cache.get("dateFrom"))
    cache_to = _parse_cache_date(cache.get("dateTo"))
    return bool(cache_from and cache_to and cache_from <= date_from and cache_to >= date_to)


def _merge_aggregate_row(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key.startswith("_") or isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            if key.endswith("Pct") or key in {"sppPct", "commissionPct", "discountPct", "buyoutPct"}:
                target[key] = value
            else:
                target[key] = target.get(key, 0) + value
        elif key not in target:
            target[key] = value


def _rollup_daily_aggregates(daily_aggregates: dict[str, Any], date_from: date, date_to: date) -> dict[str, dict[str, Any]]:
    rolled: dict[str, dict[str, Any]] = {}
    for day_key, day_rows in daily_aggregates.items():
        day = _parse_cache_date(day_key)
        if day is None or day < date_from or day > date_to or not isinstance(day_rows, dict):
            continue
        for nm_id, source_row in day_rows.items():
            if isinstance(source_row, dict):
                _merge_aggregate_row(rolled.setdefault(str(nm_id), {}), source_row)
    return rolled


def _covered_cache_from_daily(cache: dict[str, Any], *, prefix: str, suffix: str, resolved_days: int, date_from: date, date_to: date) -> dict[str, Any]:
    if not _cache_covers_range(cache, date_from, date_to):
        return {}
    daily_aggregates = cache.get("dailyAggregates")
    if not isinstance(daily_aggregates, dict):
        return {}
    observed_days = {
        day for key, values in daily_aggregates.items()
        if isinstance(values, dict)
        and (day := _parse_cache_date(key)) is not None
        and date_from <= day <= date_to
    }
    if len(observed_days) != (date_to - date_from).days + 1:
        return {}
    if prefix == "ads":
        from app.wb_reports_sprint_d import _covered_cache_from_daily as slice_campaign_cache
        return slice_campaign_cache(cache, prefix=prefix, suffix=suffix,
            resolved_days=resolved_days, date_from=date_from, date_to=date_to)
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
    payload["coveredByCache"] = {"source": prefix, "dateFrom": cache.get("dateFrom"), "dateTo": cache.get("dateTo")}
    return payload


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


def _period_cache(organization_id: int, prefix: str, date_from: date, date_to: date) -> dict[str, Any]:
    try:
        _range_start, _range_end, resolved_days = _normalize_period_range(30, date_from=date_from, date_to=date_to)
    except ValueError:
        return {}
    suffix = _period_cache_suffix(resolved_days, date_from=date_from, date_to=date_to)
    exact = get_source_cache(organization_id, f"{prefix}_{suffix}", slim=False) or {}
    if exact:
        return exact
    fallback = get_source_cache(organization_id, f"{prefix}_{resolved_days}", slim=False) or {}
    if (
        fallback
        and _parse_cache_date(fallback.get("dateFrom")) == date_from
        and _parse_cache_date(fallback.get("dateTo")) == date_to
    ):
        return fallback
    if fallback and _cache_covers_range(fallback, date_from, date_to):
        rolled = _covered_cache_from_daily(fallback, prefix=prefix, suffix=suffix, resolved_days=resolved_days, date_from=date_from, date_to=date_to)
        if rolled:
            return rolled
    for covering_key in _source_cache_covering_keys(organization_id, prefix, date_from, date_to):
        covering = get_source_cache(organization_id, covering_key, slim=False) or {}
        rolled = _covered_cache_from_daily(covering, prefix=prefix, suffix=suffix, resolved_days=resolved_days, date_from=date_from, date_to=date_to)
        if rolled:
            return rolled
    from app.routers.wb_repricer_bff import _stitched_period_cache_from_days
    stitched = _stitched_period_cache_from_days(organization_id, prefix, date_from, date_to)
    return {**stitched, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()} if stitched else {}


def _cache_aggregates(cache: dict[str, Any]) -> dict[str, dict[str, Any]]:
    aggregates = cache.get("aggregates")
    return aggregates if isinstance(aggregates, dict) else {}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(round(float(value.replace(",", ".").strip())))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
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


def _first_value(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None:
            return value
    return None


def _nonnegative(value: Any) -> int:
    return max(0, _as_int(value) or 0)


def _as_optional_nonnegative(value: Any) -> int | None:
    parsed = _as_int(value)
    return max(0, parsed) if parsed is not None else None


def _rub_to_kopecks(value: Any) -> int:
    parsed = _as_float(value)
    if parsed is None:
        return 0
    return max(0, int(round(parsed * 100)))


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 2)


def _extract_products(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    products = data.get("products") if isinstance(data, dict) else None
    return [item for item in products if isinstance(item, dict)] if isinstance(products, list) else []


def _normalize_sales_funnel_product(item: dict[str, Any]) -> dict[str, Any] | None:
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    statistic = item.get("statistic") if isinstance(item.get("statistic"), dict) else {}
    selected = statistic.get("selected") if isinstance(statistic.get("selected"), dict) else {}
    past = statistic.get("past") if isinstance(statistic.get("past"), dict) else {}
    comparison = statistic.get("comparison") if isinstance(statistic.get("comparison"), dict) else {}
    conversions = selected.get("conversions") if isinstance(selected.get("conversions"), dict) else {}
    past_conversions = past.get("conversions") if isinstance(past.get("conversions"), dict) else {}
    comparison_conversions = comparison.get("conversions") if isinstance(comparison.get("conversions"), dict) else {}

    nm_id = _as_int(product.get("nmId") or product.get("nmID"))
    if nm_id is None or nm_id <= 0:
        return None

    order_sum = _first_value(selected, "orderSum", "ordersSumRub", "ordersSum")
    buyout_sum = _first_value(selected, "buyoutSum", "buyoutsSumRub", "buyoutsSum")
    return {
        "nmId": nm_id,
        "sku": str(product.get("vendorCode") or nm_id),
        "productName": product.get("title") or product.get("name") or str(product.get("vendorCode") or nm_id),
        "brandName": product.get("brandName") or product.get("brand"),
        "categoryName": product.get("subjectName"),
        "openCount": _nonnegative(_first_value(selected, "openCount", "openCardCount", "openCard")),
        "impressions": _as_optional_nonnegative(_first_value(selected, "viewCount", "viewCountTotal", "views", "impressions", "showCount")),
        "cartCount": _nonnegative(_first_value(selected, "cartCount", "addToCartCount", "addToCart")),
        "wishlistCount": _as_optional_nonnegative(_first_value(selected, "addToWishlist", "addToWishlistCount")),
        "orderCount": _nonnegative(_first_value(selected, "orderCount", "ordersCount", "orders")),
        "orderSumKopecks": _rub_to_kopecks(order_sum),
        "buyoutCount": _nonnegative(_first_value(selected, "buyoutCount", "buyoutsCount", "buyouts")),
        "buyoutSumKopecks": _rub_to_kopecks(buyout_sum),
        "cancelCount": _nonnegative(selected.get("cancelCount")),
        "cancelSumKopecks": _rub_to_kopecks(selected.get("cancelSum")),
        "buyoutPct": _as_float(conversions.get("buyoutPercent")),
        "openCountDeltaPct": _as_float(_first_value(comparison, "openCountDynamic", "openCardCountDynamic", "openCardDynamic")),
        "cartCountDeltaPct": _as_float(_first_value(comparison, "cartCountDynamic", "addToCartCountDynamic", "addToCartDynamic")),
        "orderCountDeltaPct": _as_float(_first_value(comparison, "orderCountDynamic", "ordersCountDynamic", "ordersDynamic")),
        "orderSumDeltaPct": _as_float(comparison.get("orderSumDynamic")),
        "atcrPct": _as_float(conversions.get("addToCartPercent"))
        or _pct(_nonnegative(_first_value(selected, "cartCount", "addToCartCount", "addToCart")), _nonnegative(_first_value(selected, "openCount", "openCardCount", "openCard"))),
        "cartToOrderPct": _as_float(conversions.get("cartToOrderPercent"))
        or _pct(_nonnegative(_first_value(selected, "orderCount", "ordersCount", "orders")), _nonnegative(_first_value(selected, "cartCount", "addToCartCount", "addToCart"))),
        "pastAtcrPct": _as_float(past_conversions.get("addToCartPercent")),
        "pastCartToOrderPct": _as_float(past_conversions.get("cartToOrderPercent")),
        "comparisonAtcrPct": _as_float(comparison_conversions.get("addToCartPercent")),
        "comparisonCartToOrderPct": _as_float(comparison_conversions.get("cartToOrderPercent")),
    }


ProgressCallback = Callable[[str, str, int], None]


def _fetch_sales_funnel_rows(
    date_from: date,
    date_to: date,
    wb_token: str | None,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    if get_settings().wb_api_mode == "real" and not wb_token:
        raise RuntimeError("WB_TOKEN_REQUIRED")
    client = RateLimitedWbApiClient(inner=build_wb_analytics_client(token_override=wb_token))
    period_days = max(1, (date_to - date_from).days + 1)
    past_to = date.fromordinal(date_from.toordinal() - 1)
    past_from = date.fromordinal(past_to.toordinal() - period_days + 1)
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        request = WbApiRequest(
            method="POST",
            path="/api/analytics/v3/sales-funnel/products",
            jsonBody={
                "selectedPeriod": {"start": date_from.isoformat(), "end": date_to.isoformat()},
                "pastPeriod": {"start": past_from.isoformat(), "end": past_to.isoformat()},
                "skipDeletedNm": True,
                "orderBy": {"field": "openCard", "mode": "desc"},
                "limit": RNP_FUNNEL_PAGE_LIMIT,
                "offset": offset,
            },
        )
        response = None
        from app.wb_api.report_reads import active_report_read
        max_attempts = 1 if active_report_read.get() is not None else RNP_FUNNEL_MAX_ATTEMPTS
        for attempt in range(max_attempts):
            if progress_callback:
                progress_callback(
                    "rnp-funnel",
                    f"Загружаем воронку WB: offset {offset}, попытка {attempt + 1}",
                    min(55, 25 + offset // RNP_FUNNEL_PAGE_LIMIT * 5),
                )
            response = client.request(request)
            if response.ok or response.statusCode != 429 or attempt >= max_attempts - 1:
                break
            retry_after = (
                float(response.rateLimit.retryAfterSeconds)
                if response.rateLimit and response.rateLimit.retryAfterSeconds is not None
                else RNP_FUNNEL_RETRY_FALLBACK_SECONDS
            )
            if retry_after > RNP_FUNNEL_MAX_RETRY_AFTER_SECONDS:
                break
            if progress_callback:
                progress_callback("rnp-funnel-rate-limit", f"WB ограничил воронку, повтор через {retry_after:.0f} сек", 30)
            time.sleep(retry_after)
        if response is None:
            raise RuntimeError("WB_SALES_FUNNEL_FAILED:0:no response")
        if not response.ok:
            message = response.error.message if response.error else f"HTTP {response.statusCode}"
            raise RuntimeError(f"WB_SALES_FUNNEL_FAILED:{response.statusCode}:{message}")
        products = _extract_products(response.data)
        rows.extend(row for item in products if (row := _normalize_sales_funnel_product(item)))
        if progress_callback:
            progress_callback(
                "rnp-funnel",
                f"Воронка WB: получено {len(rows)} строк",
                min(60, 30 + len(rows) // RNP_FUNNEL_PAGE_LIMIT * 5),
            )
        if len(products) < RNP_FUNNEL_PAGE_LIMIT:
            break
        offset += RNP_FUNNEL_PAGE_LIMIT
    return rows


def _load_or_refresh_funnel_rows(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
    wb_token: str | None,
    force_refresh: bool,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    source_key = rnp_funnel_cache_key(date_from, date_to)
    if not force_refresh:
        cached = get_source_cache(organization_id, source_key, slim=False) or {}
        rows = cached.get("rows")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)], "hit", []

    try:
        rows = _fetch_sales_funnel_rows(date_from, date_to, wb_token, progress_callback=progress_callback)
    except RuntimeError as exc:
        return [], "blocked", [str(exc)]

    save_source_cache(
        organization_id,
        source_key,
        {
            "rows": rows,
            "count": len(rows),
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        },
    )
    return rows, "refreshed", []


def _cached_funnel_rows(organization_id: int, date_from: date, date_to: date) -> tuple[list[dict[str, Any]], str, list[str]]:
    cache = _period_cache(organization_id, "baskets", date_from, date_to)
    rows: list[dict[str, Any]] = []
    for raw_nm_id, aggregate in _cache_aggregates(cache).items():
        if not isinstance(aggregate, dict):
            continue
        nm_id = _nonnegative(aggregate.get("nmId") or aggregate.get("nmID") or raw_nm_id)
        if nm_id <= 0:
            continue
        order_count = _nonnegative(_first_value(aggregate, "orderCount", "ordersCount", "orders"))
        cart_count = _nonnegative(_first_value(aggregate, "cartCount", "cartAdds", "addToCartCount", "addToCart", "baskets"))
        open_count = _nonnegative(_first_value(aggregate, "openCount", "openCardCount", "openCard"))
        impressions = _as_optional_nonnegative(_first_value(aggregate, "impressions", "viewCount", "views"))
        buyout_count = _nonnegative(_first_value(aggregate, "buyoutCount", "buyoutsCount", "buyouts", "salesUnits"))
        rows.append(
            {
                "nmId": nm_id,
                "sku": str(aggregate.get("sku") or aggregate.get("vendorCode") or nm_id),
                "productName": str(aggregate.get("productName") or aggregate.get("name") or aggregate.get("title") or nm_id),
                "brandName": aggregate.get("brandName") or aggregate.get("brand"),
                "categoryName": aggregate.get("categoryName") or aggregate.get("subjectName") or aggregate.get("subject") or aggregate.get("category"),
                **{key: aggregate.get(key) for key in (
                    "openCountDeltaPct", "cartCountDeltaPct", "orderCountDeltaPct",
                    "orderSumDeltaPct", "buyoutCountDeltaPct", "buyoutSumDeltaPct",
                )},
                "openCount": open_count,
                "impressions": impressions,
                "cartCount": cart_count,
                "wishlistCount": _as_optional_nonnegative(_first_value(aggregate, "wishlistCount", "addToWishlist", "addToWishlistCount")),
                "orderCount": order_count,
                "orderSumKopecks": _nonnegative(_first_value(aggregate, "orderSumKopecks", "ordersKopecks")),
                "buyoutCount": buyout_count,
                "buyoutSumKopecks": _nonnegative(_first_value(aggregate, "buyoutSumKopecks", "salesKopecks", "revenueKopecks")),
                "cancelCount": _nonnegative(aggregate.get("cancelCount") or aggregate.get("returnsUnits")),
                "buyoutPct": _as_float(aggregate.get("buyoutPct")) if aggregate.get("buyoutPct") is not None else _pct(buyout_count, buyout_count + _nonnegative(aggregate.get("cancelCount"))),
                "atcrPct": _as_float(aggregate.get("atcrPct")) or _pct(cart_count, open_count),
                "cartToOrderPct": _as_float(aggregate.get("cartToOrderPct")) or _pct(order_count, cart_count),
            }
        )
    errors = ["WB_RNP_FUNNEL_CACHE_EMPTY"] if not rows else ["WB_RNP_FUNNEL_PERIOD_INCOMPLETE"] if cache.get("coverageState") == "partial" else []
    return rows, "hit" if cache else "missing", errors


def _cached_ads_rows(organization_id: int, date_from: date, date_to: date) -> tuple[list[AdsAttributionRow], dict[str, int], SourceStatus, Confidence, list[str], dict[str, Any]]:
    cache = _period_cache(organization_id, "ads", date_from, date_to)
    rows: list[AdsAttributionRow] = []
    totals = {"ad_spend_kopecks": 0, "impressions": 0, "clicks": 0, "cart_adds": 0, "orders_count": 0, "orders_kopecks": 0}
    for raw_nm_id, aggregate in _cache_aggregates(cache).items():
        if not isinstance(aggregate, dict):
            continue
        nm_id = _nonnegative(aggregate.get("nmId") or aggregate.get("nmID") or raw_nm_id)
        if nm_id <= 0:
            continue
        ad_spend = _as_optional_nonnegative(_first_value(aggregate, "adSpendKopecks", "spendKopecks", "sumKopecks"))
        impressions = _as_optional_nonnegative(_first_value(aggregate, "adImpressions", "impressions", "views"))
        clicks = _as_optional_nonnegative(_first_value(aggregate, "adClicks", "clicks"))
        cart_adds = _as_optional_nonnegative(_first_value(aggregate, "adCartAdds", "cartAdds", "cartCount", "baskets"))
        orders_count = _as_optional_nonnegative(_first_value(aggregate, "adOrders", "ordersCount", "orderCount", "orders"))
        orders_kopecks = _as_optional_nonnegative(_first_value(aggregate, "adSalesKopecks", "ordersKopecks", "orderSumKopecks", "salesKopecks"))
        totals["ad_spend_kopecks"] += ad_spend or 0
        totals["impressions"] += impressions or 0
        totals["clicks"] += clicks or 0
        totals["cart_adds"] += cart_adds or 0
        totals["orders_count"] += orders_count or 0
        totals["orders_kopecks"] += orders_kopecks or 0
        rows.append(
            AdsAttributionRow(
                campaign_id=str(aggregate.get("campaignId") or aggregate.get("advertId") or "") or None,
                sku_id=str(nm_id),
                attribution_level="campaign_sku" if aggregate.get("campaignId") or aggregate.get("advertId") else "exact_sku",
                confidence="high" if ad_spend or orders_count else "medium",
                ad_spend_kopecks=ad_spend,
                impressions=impressions,
                clicks=clicks,
                cart_adds=cart_adds,
                orders_count=orders_count,
                orders_kopecks=orders_kopecks,
                campaign_name=str(aggregate.get("campaignName") or aggregate.get("advertName") or "") or None,
            )
        )
    has_any_data = bool(rows) or cache.get("sourceComplete") is True
    if isinstance(cache.get("campaignTotals"), dict):
        totals = dict(cache["campaignTotals"])
    diagnostics = {
        "sources": [
            {
                "sourceId": "wb-ads-cache",
                "endpoint": "repricer ads period cache",
                "status": "cached" if has_any_data else "blocked",
                "rows": len(rows),
            }
        ]
    }
    diagnostics["summary"] = {"sourceComplete": cache.get("sourceComplete") is True,
        "unallocatedPerformance": cache.get("unallocatedPerformance", {})}
    partial = cache.get("coverageState") == "partial" or cache.get("sourceComplete") is False
    return rows, totals, ("partial" if partial else "fresh") if has_any_data else "blocked", ("medium" if partial else "high") if has_any_data else "blocked", (["WB_ADS_PERIOD_INCOMPLETE"] if partial else []) if has_any_data else ["WB-02", "WB_ADS_CACHE_EMPTY"], diagnostics


def _ads_by_nm(rows: list[AdsAttributionRow]) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = {}
    for row in rows:
        nm_id = _as_int(row.sku_id)
        if nm_id is None:
            continue
        bucket = result.setdefault(
            nm_id,
            {
                "adImpressions": 0,
                "adClicks": 0,
                "adCartAdds": 0,
                "adOrders": 0,
                "adSalesKopecks": 0,
                "adSpendKopecks": 0,
            },
        )
        for name, value in (("adImpressions", row.impressions), ("adClicks", row.clicks),
                            ("adCartAdds", row.cart_adds), ("adOrders", row.orders_count),
                            ("adSpendKopecks", row.ad_spend_kopecks)):
            bucket[name] = bucket[name] + value if bucket[name] is not None and value is not None else None
        bucket["adSalesKopecks"] = bucket["adSalesKopecks"] + row.orders_kopecks if bucket["adSalesKopecks"] is not None and row.orders_kopecks is not None else None
    return result


def _top_reason_counts(rows: list[RnpRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8])


def _warehouse_key(warehouse_id: Any, warehouse_name: Any) -> str | None:
    parsed_id = _as_int(warehouse_id)
    if parsed_id is not None:
        return f"id:{parsed_id}"
    name = str(warehouse_name or "").strip()
    return f"name:{name.casefold()}" if name else None


def _merge_active_warehouse(
    bucket: dict[str, dict[str, Any]],
    *,
    warehouse_id: Any = None,
    warehouse_name: Any = None,
    available_units: Any = 0,
    order_count: int = 0,
    sale_count: int = 0,
) -> None:
    key = _warehouse_key(warehouse_id, warehouse_name)
    if key is None:
        return
    current = bucket.setdefault(
        key,
        {
            "warehouseId": _as_optional_nonnegative(warehouse_id),
            "warehouseName": str(warehouse_name).strip() if warehouse_name is not None and str(warehouse_name).strip() else None,
            "availableUnits": 0,
            "orderCount": 0,
            "saleCount": 0,
        },
    )
    if current.get("warehouseId") is None:
        current["warehouseId"] = _as_optional_nonnegative(warehouse_id)
    if not current.get("warehouseName") and warehouse_name is not None and str(warehouse_name).strip():
        current["warehouseName"] = str(warehouse_name).strip()
    current["availableUnits"] = max(_nonnegative(current.get("availableUnits")), _nonnegative(available_units))
    current["orderCount"] = _nonnegative(current.get("orderCount")) + max(0, order_count)
    current["saleCount"] = _nonnegative(current.get("saleCount")) + max(0, sale_count)


def _active_warehouses_by_nm(source_snapshot: Any) -> dict[int, list[dict[str, Any]]]:
    by_nm: dict[int, dict[str, dict[str, Any]]] = {}
    for stock in getattr(source_snapshot, "stocks", []) or []:
        nm_id = _as_int(getattr(stock, "nm_id", None))
        if nm_id is None:
            continue
        available_units = _nonnegative(getattr(stock, "available_units", 0))
        quantity = _nonnegative(getattr(stock, "quantity", 0))
        if available_units <= 0 and quantity <= 0:
            continue
        _merge_active_warehouse(
            by_nm.setdefault(nm_id, {}),
            warehouse_id=getattr(stock, "warehouse_id", None),
            warehouse_name=getattr(stock, "warehouse_name", None),
            available_units=available_units or quantity,
        )

    for order in getattr(source_snapshot, "orders", []) or []:
        if not isinstance(order, dict) or bool(order.get("isCancel")):
            continue
        nm_id = _as_int(order.get("nmId") or order.get("nmID"))
        if nm_id is None:
            continue
        _merge_active_warehouse(
            by_nm.setdefault(nm_id, {}),
            warehouse_id=order.get("warehouseId"),
            warehouse_name=order.get("warehouseName"),
            order_count=1,
        )

    for sale in getattr(source_snapshot, "sales", []) or []:
        if not isinstance(sale, dict):
            continue
        nm_id = _as_int(sale.get("nmId") or sale.get("nmID"))
        if nm_id is None:
            continue
        _merge_active_warehouse(
            by_nm.setdefault(nm_id, {}),
            warehouse_id=sale.get("warehouseId"),
            warehouse_name=sale.get("warehouseName"),
            sale_count=1,
        )

    return {
        nm_id: sorted(
            warehouses.values(),
            key=lambda item: (
                -_nonnegative(item.get("availableUnits")),
                -_nonnegative(item.get("orderCount")),
                -_nonnegative(item.get("saleCount")),
                str(item.get("warehouseName") or ""),
            ),
        )
        for nm_id, warehouses in by_nm.items()
    }


def _active_warehouse_count(rows: list[RnpRow]) -> int:
    keys: set[str] = set()
    for row in rows:
        for warehouse in row.warehouses:
            key = _warehouse_key(warehouse.get("warehouseId"), warehouse.get("warehouseName"))
            if key:
                keys.add(key)
        if not row.warehouses:
            key = _warehouse_key(row.warehouseId, row.warehouseName)
            if key:
                keys.add(key)
    return len(keys)


def _row_revenue_for_drr(row: RnpRow) -> int:
    return row.orderSumKopecks or row.adSalesKopecks or row.buyoutSumKopecks or 0


def _enrich_rows_with_warehouses(rows: list[RnpRow], warehouse_index: dict[int, list[dict[str, Any]]]) -> list[RnpRow]:
    enriched: list[RnpRow] = []
    for row in rows:
        warehouses = row.warehouses or warehouse_index.get(row.nmId or 0, [])
        if not warehouses:
            enriched.append(row)
            continue
        primary = warehouses[0]
        enriched.append(
            row.model_copy(
                update={
                    "warehouseId": row.warehouseId if row.warehouseId is not None else primary.get("warehouseId"),
                    "warehouseName": row.warehouseName or primary.get("warehouseName"),
                    "warehouses": warehouses,
                }
            )
        )
    return enriched


def _diagnostics_from_rows(
    *,
    rows: list[RnpRow],
    funnel_rows_count: int,
    ads_rows_count: int,
    source_status: SourceStatus,
    ads_source_status: SourceStatus,
    cache_status: str,
    funnel_errors: list[str],
    ads_blocker_ids: list[str],
    ads_totals: dict[str, Any],
    ads_diagnostics: dict[str, Any] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    matched_ads = [row for row in rows if row.adImpressions is not None or row.adClicks is not None or row.adSpendKopecks is not None]
    missing_ads = [row for row in rows if "ads_source_partial" in row.reasons or (row.adImpressions is None and row.adClicks is None and row.adSpendKopecks is None)]
    ads_only_rows = [
        row
        for row in rows
        if (row.openCount or 0) == 0
        and (row.cartCount or 0) == 0
        and (row.orderCount or 0) == 0
        and (row.orderSumKopecks or 0) == 0
        and ((row.adImpressions or 0) > 0 or (row.adClicks or 0) > 0 or (row.adOrders or 0) > 0 or (row.adSpendKopecks or 0) > 0)
    ]
    ads_sources = ads_diagnostics.get("sources") if isinstance(ads_diagnostics, dict) else None
    if not isinstance(ads_sources, list) or not ads_sources:
        ads_sources = [
            {
                "sourceId": "wb-ads-fullstats",
                "endpoint": "GET /adv/v3/fullstats",
                "status": ads_source_status,
                "rows": ads_rows_count,
                "blockerIds": ads_blocker_ids,
                "totals": {
                    "adSpendKopecks": _nonnegative(ads_totals.get("ad_spend_kopecks")),
                    "impressions": _nonnegative(ads_totals.get("impressions")),
                    "clicks": _nonnegative(ads_totals.get("clicks")),
                    "ordersCount": _nonnegative(ads_totals.get("orders_count")),
                    "ordersKopecks": _nonnegative(ads_totals.get("orders_kopecks")),
                },
            },
            {
                "sourceId": "wb-ads-upd",
                "endpoint": "GET /adv/v1/upd",
                "status": ads_source_status,
                "note": "Used by ads attribution snapshot for spend documents; campaign-only spend is not allocated to SKU RNP.",
            },
            {
                "sourceId": "wb-ads-adverts",
                "endpoint": "GET /api/advert/v2/adverts",
                "status": ads_source_status,
                "note": "Used to map campaign membership when fullstats does not return per-nm rows.",
            },
        ]
    funnel_request: dict[str, Any] = {
        "endpoint": "POST /api/analytics/v3/sales-funnel/products",
        "limit": RNP_FUNNEL_PAGE_LIMIT,
        "orderBy": {"field": "openCard", "mode": "desc"},
        "skipDeletedNm": True,
    }
    if date_from is not None and date_to is not None:
        period_days = max(1, (date_to - date_from).days + 1)
        past_to = date.fromordinal(date_from.toordinal() - 1)
        past_from = date.fromordinal(past_to.toordinal() - period_days + 1)
        funnel_request.update(
            {
                "selectedPeriod": {"start": date_from.isoformat(), "end": date_to.isoformat()},
                "pastPeriod": {"start": past_from.isoformat(), "end": past_to.isoformat()},
            }
        )

    def row_debug(row: RnpRow) -> dict[str, Any]:
        return {
            "nmId": row.nmId,
            "sku": row.sku or row.skuId or row.label,
            "productName": row.productName,
            "openCount": row.openCount,
            "cartCount": row.cartCount,
            "orderCount": row.orderCount,
            "orderSumKopecks": row.orderSumKopecks,
            "buyoutCount": row.buyoutCount,
            "buyoutSumKopecks": row.buyoutSumKopecks,
            "adImpressions": row.adImpressions,
            "adClicks": row.adClicks,
            "adCartAdds": row.adCartAdds,
            "adOrders": row.adOrders,
            "adSpendKopecks": row.adSpendKopecks,
            "reasons": row.reasons,
        }

    return {
        "summary": {
            "sourceStatus": source_status,
            "adsSourceStatus": ads_source_status,
            "cacheStatus": cache_status,
            "funnelRows": funnel_rows_count,
            "reportRows": len(rows),
            "adsRows": ads_rows_count,
            "adsMatchedSkuCount": len(matched_ads),
            "missingAdsSkuCount": len(missing_ads),
            "adsOnlySkuCount": len(ads_only_rows),
            "activeWarehouseCount": _active_warehouse_count(rows),
            "topReasons": _top_reason_counts(rows),
        },
        "funnelRequest": funnel_request,
        "sources": [
            {
                "sourceId": "wb-analytics-sales-funnel-products",
                "endpoint": "POST /api/analytics/v3/sales-funnel/products",
                "status": "blocked" if funnel_errors else cache_status,
                "rows": funnel_rows_count,
                "errors": funnel_errors,
            },
            *ads_sources,
        ],
        "missingAdsExamples": [
            {
                "nmId": row.nmId,
                "sku": row.sku or row.skuId or row.label,
                "productName": row.productName,
                "reasons": row.reasons,
            }
            for row in missing_ads[:12]
        ],
        "adsOnlyExamples": [row_debug(row) for row in ads_only_rows[:12]],
        "rowExamples": [row_debug(row) for row in rows[:12]],
    }


def _rnp_row_from_sources(
    funnel: dict[str, Any],
    ads: dict[str, int] | None,
    source_status: SourceStatus,
    confidence: Confidence,
    warehouses: list[dict[str, Any]] | None = None,
) -> RnpRow:
    nm_id = _nonnegative(funnel.get("nmId"))
    sku = str(funnel.get("sku") or nm_id)
    open_count = _nonnegative(funnel.get("openCount"))
    impressions = _as_optional_nonnegative(funnel.get("impressions"))
    cart_count = _nonnegative(funnel.get("cartCount"))
    order_count = _nonnegative(funnel.get("orderCount"))
    order_sum = _nonnegative(funnel.get("orderSumKopecks"))
    ads_available = ads is not None
    ads_payload = ads or {}
    ad_impressions = _as_optional_nonnegative(ads_payload.get("adImpressions")) if ads_available else None
    ad_clicks = _as_optional_nonnegative(ads_payload.get("adClicks")) if ads_available else None
    ad_cart_adds = _as_optional_nonnegative(ads_payload.get("adCartAdds")) if ads_available else None
    ad_orders = _as_optional_nonnegative(ads_payload.get("adOrders")) if ads_available else None
    ad_sales = _as_optional_nonnegative(ads_payload.get("adSalesKopecks")) if ads_available else None
    ad_spend = _as_optional_nonnegative(ads_payload.get("adSpendKopecks")) if ads_available else None
    reasons = ["estimated_organic"] if ads_available else ["ads_source_partial"]
    if source_status != "fresh":
        reasons.append("partial_source")
    if (ad_spend or 0) > 0 and (ad_orders or 0) == 0:
        reasons.append("spend_no_orders")
    if (_as_float(funnel.get("cartToOrderPct")) or 0) < 10 and cart_count > 0:
        reasons.append("weak_order")
    active_warehouses = warehouses or []
    primary_warehouse = active_warehouses[0] if active_warehouses else {}

    return RnpRow(
        rowId=f"sku-{sku}",
        label=sku,
        sku=sku,
        nmId=nm_id,
        productName=str(funnel.get("productName") or sku),
        brandName=funnel.get("brandName"),
        categoryName=funnel.get("categoryName"),
        warehouseId=primary_warehouse.get("warehouseId"),
        warehouseName=primary_warehouse.get("warehouseName"),
        warehouses=active_warehouses,
        skuId=sku,
        brandId=str(funnel.get("brandName") or "") or None,
        managerId=None,
        activeRule=None,
        openCount=open_count,
        impressions=impressions,
        openCountDeltaPct=_as_float(funnel.get("openCountDeltaPct")),
        cartCount=cart_count,
        wishlistCount=_as_optional_nonnegative(funnel.get("wishlistCount")),
        cartCountDeltaPct=_as_float(funnel.get("cartCountDeltaPct")),
        orderCount=order_count,
        orderCountDeltaPct=_as_float(funnel.get("orderCountDeltaPct")),
        orderSumKopecks=order_sum,
        orderSumDeltaPct=_as_float(funnel.get("orderSumDeltaPct")),
        buyoutCount=_nonnegative(funnel.get("buyoutCount")),
        buyoutCountDeltaPct=_as_float(funnel.get("buyoutCountDeltaPct")),
        buyoutSumKopecks=_nonnegative(funnel.get("buyoutSumKopecks")),
        buyoutSumDeltaPct=_as_float(funnel.get("buyoutSumDeltaPct")),
        buyoutPct=_as_float(funnel.get("buyoutPct")),
        ctrPct=_pct(open_count, impressions or 0),
        atcrPct=_as_float(funnel.get("atcrPct")),
        cartToOrderPct=_as_float(funnel.get("cartToOrderPct")),
        adImpressions=ad_impressions,
        adClicks=ad_clicks,
        adCtrPct=_pct(ad_clicks, ad_impressions) if ad_clicks is not None and ad_impressions is not None else None,
        adCartAdds=ad_cart_adds,
        adAtcrPct=_pct(ad_cart_adds or 0, ad_clicks or 0) if ads_available else None,
        adOrders=ad_orders,
        adSalesKopecks=ad_sales,
        adSpendKopecks=ad_spend,
        adSpendDeltaPct=None,
        drrPct=_pct(ad_spend or 0, order_sum) if ads_available else None,
        roiPct=round(((ad_sales or 0) - (ad_spend or 0)) / (ad_spend or 1) * 100, 2) if ad_sales is not None and (ad_spend or 0) > 0 else None,
        acooPct=_pct(ad_spend or 0, ad_sales or 0) if ads_available else None,
        tacooPct=_pct(ad_spend or 0, order_sum) if ads_available else None,
        marginPct=None,
        avgPosition=None,
        organicOpenCountEstimated=max(0, open_count - (ad_clicks or 0)) if ads_available else None,
        organicCartCountEstimated=max(0, cart_count - (ad_cart_adds or 0)) if ads_available else None,
        organicOrderCountEstimated=max(0, order_count - (ad_orders or 0)) if ads_available else None,
        organicSalesKopecksEstimated=max(0, order_sum - ad_sales) if ad_sales is not None else None,
        organicEstimate=ads_available,
        reasons=reasons,
        comments=[],
        auditEvents=[],
        sourceStatus=source_status,
        confidence=confidence,
    )


def build_rnp_snapshot(
    *,
    date_from: date,
    date_to: date,
    group_by: str,
    organization_id: int,
    wb_token: str | None = None,
    force_refresh: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> RnpSnapshot:
    from app.repricer_cache.store import get_source_cache_fetched_at, get_source_cache_range_revision
    source_revision = {prefix: get_source_cache_range_revision(organization_id, prefix, date_from=date_from, date_to=date_to)
                       for prefix in ("baskets_", "ads_")}
    report_key = rnp_report_cache_key(date_from, date_to, group_by)
    imported_at = get_source_cache_fetched_at(organization_id, f"funnel_seller_{date_from}_{date_to}")
    report_at = get_source_cache_fetched_at(organization_id, report_key)
    if imported_at and (not report_at or imported_at > report_at):
        force_refresh = True
    if not force_refresh:
        cached_report = get_source_cache(organization_id, report_key, slim=False) or {}
        cached_rows = cached_report.get("rows")
        if isinstance(cached_rows, list) and cached_report.get("sourceRevision") == source_revision:
            rows = [RnpRow.model_validate(row) for row in cached_rows if isinstance(row, dict)]
            ad_spend = cached_report.get("campaignAdSpendKopecks")
            if ad_spend is None:
                ad_spend = sum(row.adSpendKopecks or 0 for row in rows)
            order_sum = sum(_row_revenue_for_drr(row) for row in rows)
            drr_pct = _pct(ad_spend, order_sum)
            blocker_ids = list(cached_report.get("blockerIds") or [])
            if cached_report.get("adsSourceStatus") in {"blocked", "unknown"}:
                blocker_ids = sorted(set(blocker_ids + ["WB-02"]))
            if drr_pct is None:
                blocker_ids = sorted(set(blocker_ids + ["WB-11"]))
            else:
                blocker_ids = [blocker_id for blocker_id in blocker_ids if blocker_id != "WB-11"]
            diagnostics = cached_report.get("diagnostics") if isinstance(cached_report.get("diagnostics"), dict) else _diagnostics_from_rows(
                rows=rows,
                funnel_rows_count=len(rows),
                ads_rows_count=0,
                source_status=cached_report.get("sourceStatus") or "partial",
                ads_source_status=cached_report.get("adsSourceStatus") or "partial",
                cache_status="hit",
                funnel_errors=[],
                ads_blocker_ids=list(cached_report.get("blockerIds") or []),
                ads_totals={},
                ads_diagnostics=None,
                date_from=date_from,
                date_to=date_to,
            )
            summary = diagnostics.setdefault("summary", {}) if isinstance(diagnostics, dict) else {}
            if isinstance(summary, dict):
                summary["activeWarehouseCount"] = _active_warehouse_count(rows)
            return RnpSnapshot(
                source_status=cached_report.get("sourceStatus") or "partial",
                confidence=cached_report.get("confidence") or "medium",
                blocker_ids=blocker_ids,
                source_evidence=[],
                calculated_at=utc_now(),
                rows=rows,
                ad_spend_kopecks=ad_spend,
                drr_pct=drr_pct,
                formula_notes=list(cached_report.get("formulaNotes") or []),
                ads_source_status=cached_report.get("adsSourceStatus") or "partial",
                cache_status="hit",
                diagnostics=diagnostics,
            )

    funnel_rows, cache_status, funnel_errors = _cached_funnel_rows(
        organization_id=organization_id,
        date_from=date_from,
        date_to=date_to,
    )
    if progress_callback:
        progress_callback("rnp-ads-cache", "Берём рекламную атрибуцию из WB sync cache", 65)
    ads_rows, ads_totals, ads_source_status, ads_confidence, ads_blocker_ids, ads_diagnostics = _cached_ads_rows(
        organization_id=organization_id,
        date_from=date_from,
        date_to=date_to,
    )
    if progress_callback:
        progress_callback("rnp-join", f"Сводим воронку и рекламу из cache: {len(funnel_rows)} строк воронки, {len(ads_rows)} строк рекламы", 80)
    ads_index = _ads_by_nm(ads_rows)
    # Warehouse enrichment is auxiliary for RNP. Do not turn a cached/fast sales-funnel
    # report into a multi-minute orders/sales/finance fetch just to fill this optional column.
    warehouse_index: dict[int, list[dict[str, Any]]] = {}

    blockers = sorted(set(ads_blocker_ids + (["WB_RNP_FUNNEL_SOURCE"] if funnel_errors else [])))
    source_status: SourceStatus = "fresh"
    if funnel_errors and not funnel_rows:
        source_status = "blocked"
    elif funnel_errors or ads_source_status != "fresh":
        source_status = "partial"
    confidence: Confidence = "blocked" if source_status == "blocked" else ("high" if ads_confidence == "high" and not funnel_errors else "medium")

    rows = []
    for row in funnel_rows:
        nm_id = _nonnegative(row.get("nmId"))
        ads = ads_index.get(nm_id)
        if ads is None and ads_source_status == "fresh" and (ads_diagnostics.get("summary") or {}).get("sourceComplete"):
            ads = {key: 0 for key in ("adImpressions", "adClicks", "adCartAdds", "adOrders", "adSalesKopecks", "adSpendKopecks")}
        rows.append(_rnp_row_from_sources(row, ads, source_status, confidence, warehouse_index.get(nm_id, [])))
    if not rows and source_status != "blocked":
        for nm_id, ads in ads_index.items():
            rows.append(
                _rnp_row_from_sources(
                    {"nmId": nm_id, "sku": str(nm_id), "productName": f"NM {nm_id}"},
                    ads,
                    source_status,
                    confidence,
                    warehouse_index.get(nm_id, []),
                )
            )

    if progress_callback:
        progress_callback("rnp-rows", f"Рассчитываем метрики РНП: {len(rows)} строк", 90)

    ad_spend = sum(row.adSpendKopecks or 0 for row in rows)
    if (ads_diagnostics.get("summary") or {}).get("sourceComplete"):
        ad_spend = ads_totals.get("ad_spend_kopecks", ad_spend)
    order_sum = sum(_row_revenue_for_drr(row) for row in rows)
    drr_pct = _pct(ad_spend, order_sum)
    if drr_pct is None:
        blockers = sorted(set(blockers + ["WB-11"]))
    diagnostics = _diagnostics_from_rows(
        rows=rows,
        funnel_rows_count=len(funnel_rows),
        ads_rows_count=len(ads_rows),
        source_status=source_status,
        ads_source_status=ads_source_status,
        cache_status=cache_status,
        funnel_errors=funnel_errors,
        ads_blocker_ids=ads_blocker_ids,
        ads_totals=ads_totals,
        ads_diagnostics=ads_diagnostics,
        date_from=date_from,
        date_to=date_to,
    )
    payload = {
        "campaignAdSpendKopecks": ad_spend,
        "sourceRevision": source_revision,
        "sourceStatus": source_status,
        "confidence": confidence,
        "blockerIds": blockers,
        "rows": [row.model_dump(mode="json") for row in rows],
        "adsSourceStatus": ads_source_status,
        "diagnostics": diagnostics,
        "formulaNotes": [
            "total funnel: repricer baskets period cache produced by WB sync",
            "ads funnel: repricer ads period cache produced by WB sync",
            "organicEstimated = total funnel - adAttributed; estimate only",
            "Детализация по складам не загружается в RNP: используйте отчёт «Остатки» для актуальных остатков.",
        ],
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "groupBy": group_by,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }
    save_source_cache(organization_id, report_key, payload)

    evidence = [
        SourceEvidence(
            sourceId="wb-sales-funnel-cache",
            sourceType="wb_api",
            sourceName="WB Sales Funnel cached by repricer sync",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=60,
            fieldsUsed=["openCount", "cartCount", "orderCount", "orderSum", "comparison.*Dynamic"],
        ),
        SourceEvidence(
            sourceId="wb-ads-cache",
            sourceType="wb_api",
            sourceName="WB Ads cached by repricer sync",
            lastSyncedAt=utc_now(),
            freshnessTtlMinutes=180,
            fieldsUsed=["adSpendKopecks", "impressions", "clicks", "ordersKopecks"],
        ),
    ]
    return RnpSnapshot(
        source_status=source_status,
        confidence=confidence,
        blocker_ids=blockers,
        source_evidence=evidence,
        calculated_at=utc_now(),
        rows=rows,
        ad_spend_kopecks=ad_spend,
        drr_pct=drr_pct,
        formula_notes=payload["formulaNotes"],
        ads_source_status=ads_source_status,
        cache_status=cache_status,
        diagnostics=diagnostics,
    )
