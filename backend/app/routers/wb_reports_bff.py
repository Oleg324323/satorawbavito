from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from collections import defaultdict, OrderedDict
from threading import RLock
from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query, Request

from app.cabinet.store import get_user_wb_token_secret
from app.cabinet.store import list_team_users
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.control_plane.store import assert_permission_or_audit, record_audit_event
from app.routers.one_c_cash_flow import get_cash_flow_for_period
from app.reports_exports import ReportExportId, get_report_export_or_none, materialize_report_export, request_report_export
from app.reports_history import ensure_daily_stock_history
from app.report_rules.presets import PRESET_CONFIGS
from app.report_rules.schemas import RulesDraftRequest, RulesSaveRequest
from app.report_rules.service import (
    PreviewTokenError,
    ReportRulesValidationError,
    create_preview,
    evaluate_metrics,
    issue_preview_token,
    normalize_config,
    verify_preview_token,
)
from app.report_rules.store import ProfileVersionConflict, activate_profile, list_profile_history, load_active_profile
from app.repricer_cache.store import (
    finance_cache_uses_current_revenue_basis,
    get_covering_source_cache,
    get_source_cache_fetched_at,
    get_source_cache,
    list_cached_goods,
    list_source_cache_by_prefix,
    list_source_cache_ranges_by_prefix,
    save_source_cache as _save_source_cache,
)
from app.repricer_bff import _extract_wb_media_url
from app.repricer_persistence.store import load_algorithm_settings, load_runtime_state
from app.platform.economics.legacy_tax import legacy_finance_tax_revision
from app.platform.economics.wb_quarterly_tax import tax_consistent_report, assert_current_snapshot, current_revision as quarterly_tax_revision, SETTINGS_KEY
from app.wb_ads_cache.store import get_ads_report_cache, save_ads_history_snapshots, save_ads_report_cache
from app.wb_api.ads_runtime import AdsAttributionRow, AdsAttributionSnapshot, build_ads_attribution_snapshot
from app.wb_api.report_reads import touch_demand, release_demand
from app.wb_api.client import (
    RateLimitedWbApiClient,
    WbApiRequest,
    build_wb_analytics_client,
    build_wb_common_client,
    build_wb_marketplace_client,
    build_wb_supplies_client,
)
from app.wb_api.reports_sources_runtime import (
    WbFinanceReconciliation,
    WbReportsSourcesSnapshot,
    WbStockRow,
)
from app.wb_reports_sprint_d import (
    DEFAULT_FROM,
    DEFAULT_TO,
    _cache_aggregates,
    _period_cache,
    build_abc_report,
    build_pnl_report,
    build_plan_fact_report,
    build_rnp_report,
)
from app.wb_sync_plan import historical_sync_as_of
from app.routers.wb_week_export import export_week_table
from app.routers.wb_pnl_export import export_pnl_table
from app.wb_pnl_filters import pnl_filter_metadata
from app.wb_advertising_view import advertising_view, enrich_advertising_products
from app.routers.wb_ads_export import export_ads_table


router = APIRouter(tags=["wb-reports-bff"])

router.add_api_route("/api/wb/reports/week-over-week/table.xlsx", export_week_table, methods=["POST"])
router.add_api_route("/api/wb/reports/pnl/table.xlsx", export_pnl_table, methods=["POST"])
router.add_api_route("/api/wb/reports/pnl/filter-metadata", pnl_filter_metadata, methods=["POST"])
router.add_api_route("/api/wb/reports/ads/table.xlsx", export_ads_table, methods=["POST"])


def save_source_cache(organization_id: int, source_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    # A report/job acknowledgement is a durability promise, not an in-memory
    # fallback. Never show successful collection after a failed DB commit.
    return _save_source_cache(organization_id, source_key, payload, strict=True)

DIGEST_REPORT_PAYLOAD_VERSION = "v24"

ReportId = Literal["digest", "abc", "rnp", "pnl", "expenses", "ads", "stock", "week-over-week"]
ReportGroupBy = Literal["sku", "manager", "brand", "category", "status", "warehouse", "campaign"]


def _actor_wb_token(actor: Any) -> str | None:
    token = get_user_wb_token_secret(actor.user_id)
    return token.strip() if token and token.strip() else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_rules_preview_rows(organization_id: int, *, finance_allowed: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    reports: set[str] = set()
    for cached in list_source_cache_by_prefix(organization_id, "reports_payload_", limit=50, slim=False):
        source_key = str(cached.get("sourceKey") or "")
        source_report_id = source_key.removeprefix("reports_payload_").split("_", 1)[0]
        if source_report_id in {"abc", "pnl"}:
            _, _, _, cached_source = _parse_report_payload_cache_key(source_key, source_report_id)
            if not cached_source or not cached_source.endswith(f"_{'finance' if finance_allowed else 'nofinance'}"):
                continue
            if not _report_payload_cache_is_usable(source_report_id, cached, organization_id=organization_id):
                continue
        report = cached.get("report") if isinstance(cached.get("report"), dict) else {}
        report_rows = report.get("rows") if isinstance(report.get("rows"), list) else []
        report_id = str((report.get("meta") or {}).get("id") or cached.get("sourceKey") or "report")
        for raw in report_rows:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["reportId"] = report_id
            if isinstance(row.get("marginPct"), dict):
                row["marginPct"] = row["marginPct"].get("percent")
            if isinstance(row.get("orders"), dict) and row.get("baskets"):
                baskets = row.get("baskets")
                baskets_value = baskets.get("units") if isinstance(baskets, dict) else baskets
                orders_value = row["orders"].get("units")
                if baskets_value and orders_value is not None:
                    row["cartToOrderPct"] = float(orders_value) / float(baskets_value) * 100
            rows.append(row)
            reports.add(report_id)
    return rows, sorted(reports)


def _rules_profile_payload(profile: Any) -> dict[str, Any]:
    return profile.model_dump(mode="json")


def _range_from_preset(
    preset: str,
    from_raw: str | None,
    to_raw: str | None,
    *,
    as_of: date | None = None,
) -> tuple[date, date, dict[str, str]]:
    anchor = as_of or historical_sync_as_of()
    if preset == "custom":
        if not from_raw or not to_raw:
            raise HTTPException(status_code=422, detail="REPORT_PERIOD_DATES_REQUIRED")
        try:
            date_from = date.fromisoformat(from_raw)
            date_to = date.fromisoformat(to_raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="REPORT_PERIOD_INVALID") from exc
        if date_from > date_to or date_to > anchor or (date_to - date_from).days >= 90:
            raise HTTPException(status_code=422, detail="REPORT_PERIOD_INVALID")
    else:
        days_by_preset = {"1d": 1, "7d": 7, "14d": 14, "30d": 30}
        if preset not in days_by_preset:
            preset = "7d"
        date_to = anchor
        date_from = date_to - timedelta(days=days_by_preset[preset] - 1)
    return date_from, date_to, {"preset": preset, "from": date_from.isoformat(), "to": date_to.isoformat()}


def _freshness_state(source_status: str) -> Literal["fresh", "partial", "pending_financial", "stale"]:
    if source_status == "fresh":
        return "fresh"
    if source_status in {"blocked", "unknown", "stale"}:
        return "stale"
    return "partial"


def _int_value(value: Any) -> int:
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


def _parse_cache_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _rnp_daily_baskets_ready(
    organization_id: int,
    *,
    date_from: date,
    date_to: date,
) -> bool:
    return _report_daily_source_ready(organization_id, "baskets", date_from=date_from, date_to=date_to)


REPORT_DAILY_SOURCE_PREFIXES: dict[str, str] = {
    "period-stats": "period_stats_",
    "finance": "finance_",
    "ads": "ads_",
    "baskets": "baskets_",
    "baskets-summary": "baskets_",
}


REPORT_DAILY_SOURCES_BY_ID: dict[str, tuple[str, ...]] = {
    "stats": ("period-stats", "finance", "ads", "baskets-summary"),
    "digest": ("period-stats", "finance", "ads", "baskets"),
    "abc": ("period-stats", "finance", "ads", "baskets-summary"),
    "rnp": ("baskets-summary", "finance", "ads"),
    "ads": ("ads",),
    "stock": ("stocks", "period-stats", "finance"),
    "week-over-week": ("stocks", "period-stats", "finance", "ads", "baskets-summary"),
    "pnl": ("finance", "ads"),
}


def _report_daily_source_ready(
    organization_id: int,
    source: str,
    *,
    date_from: date,
    date_to: date,
) -> bool:
    return not _report_missing_source_ranges(organization_id, source, date_from=date_from, date_to=date_to)


def _report_missing_source_ranges(organization_id: int, source: str, *, date_from: date, date_to: date, fresh_only: bool = False) -> list[tuple[date, date]]:
    if source == "stocks":
        from app.wb_api.source_freshness import source_is_fresh
        snapshot = get_source_cache(organization_id, "stocks", slim=True) or {}
        today = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
        valid = isinstance(snapshot.get("aggregates"), dict) or "count" in snapshot
        fresh = source_is_fresh({**snapshot, "dateTo": today})
        return [] if valid and (not fresh_only or fresh) else [(date_from, date_to)]
    prefix = REPORT_DAILY_SOURCE_PREFIXES.get(source)
    if not prefix:
        return [(date_from, date_to)]
    available: set[str] = set()
    if source == "finance":
        # Use the reader's field/currency/cursor contract, not a weaker
        # sourceComplete-only interpretation or an old monetary aggregate.
        from app.cabinet.store import get_organization_wb_token_secret
        from app.repricer_page_finance import missing_page_ranges
        token = get_organization_wb_token_secret(organization_id)
        return (missing_page_ranges(organization_id, token, date_from, date_to, fresh_only=fresh_only)
                if token else [(date_from, date_to)])
    for cache in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100):
        if not isinstance(cache, dict):
            continue
        if fresh_only:
            from app.wb_api.source_freshness import source_is_fresh
            if not source_is_fresh(cache):
                continue
        cache_from = _parse_cache_date(cache.get("dateFrom"))
        cache_to = _parse_cache_date(cache.get("dateTo"))
        if not cache_from or not cache_to or cache_from > date_to or cache_to < date_from:
            continue
        if source == "finance" and not finance_cache_uses_current_revenue_basis(cache):
            continue
        if source == "baskets-summary":
            payload = get_source_cache(organization_id, cache["sourceKey"], slim=False) if cache.get("sourceKey") else {}
            if payload and payload.get("aggregateComplete") is True and cache_from == date_from and cache_to == date_to:
                # A period-wide response proves its own exact interval, even
                # when optional per-day requests are deferred or incomplete.
                available.update((cache_from + timedelta(days=offset)).isoformat()
                    for offset in range((cache_to - cache_from).days + 1))
            continue
        incomplete_dates: set[str] = set()
        if source == "baskets" and cache.get("dailyDetailStatus") in {"partial", "pending", "failed", "paused", "deferred"}:
            # One day can be split into many nmID batches. Its presence alone
            # does not prove that every product batch was persisted.
            payload = get_source_cache(organization_id, cache["sourceKey"], slim=False) if cache.get("sourceKey") else None
            if not payload or not isinstance(payload.get("chunks"), list):
                continue
            from app.routers.wb_repricer_bff import _baskets_range_has_daily_detail
            incomplete_dates = {day for day in cache.get("dailyAggregateDates", [])
                if (parsed := _parse_cache_date(day)) is None or not _baskets_range_has_daily_detail(payload, parsed, parsed)}
        dates = cache.get("dailyAggregateDates")
        if isinstance(dates, list):
            available.update(day for day in dates if isinstance(day, str) and cache_from.isoformat() <= day <= cache_to.isoformat() and day not in incomplete_dates)
        elif _int_value(cache.get("dailyAggregatesDays")) == (cache_to - cache_from).days + 1:
            available.update((cache_from + timedelta(days=offset)).isoformat() for offset in range((cache_to - cache_from).days + 1))
    missing = [date_from + timedelta(days=offset) for offset in range((date_to - date_from).days + 1)
               if (date_from + timedelta(days=offset)).isoformat() not in available]
    ranges: list[tuple[date, date]] = []
    for day in missing:
        if ranges and ranges[-1][1] + timedelta(days=1) == day:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def _digest_source_bounds(date_from: date, date_to: date) -> tuple[date, date]:
    completed_end = min(date_to, datetime.now(timezone(timedelta(hours=3))).date() - timedelta(days=1))
    return min(date_from, completed_end - timedelta(days=5)), date_to


def _report_source_bounds(report_id: str, date_from: date, date_to: date) -> tuple[date, date]:
    if report_id == "digest":
        return _digest_source_bounds(date_from, date_to)
    if report_id == "week-over-week":
        return date_from - timedelta(days=(date_to - date_from).days + 1), date_to
    # ABC's comparison uses saved observations, but must never delay or
    # recollect the selected financial interval to fill the previous one.
    return date_from, date_to


def _report_daily_sources_ready(
    organization_id: int,
    sources: tuple[str, ...],
    *,
    date_from: date,
    date_to: date,
    fresh_only: bool = False,
    report_id: str | None = None,
    selected_date_from: date | None = None,
    finance_allowed: bool = True,
) -> tuple[bool, list[str]]:
    if not finance_allowed:
        sources = tuple(item for item in sources if item != "finance")
    if finance_allowed and report_id in {"abc", "week-over-week"} and "ads" in sources:
        # Campaign performance is an independent operational enrichment. A
        # completed Finance ledger already proves the settlement ad expense.
        if not _report_missing_source_ranges(organization_id, "finance", date_from=date_from, date_to=date_to, fresh_only=fresh_only):
            sources = tuple(item for item in sources if item != "ads")
    if report_id in {"abc", "pnl", "week-over-week"}:
        pnl_ads_missing = False
        if report_id == "pnl" and "ads" in sources:
            from app.wb_pnl_advertising import missing_campaign_ranges
            pnl_ads_missing = bool(missing_campaign_ranges(organization_id, date_from, date_to, fresh_only=fresh_only))
            sources = tuple(item for item in sources if item != "ads")
        from app.wb_abc_loading import missing_source_ranges
        missing = [source for source in sources if (source != "baskets-summary" or report_id != "week-over-week") and missing_source_ranges(
            organization_id, source, date_from, date_to, fresh_only=fresh_only,
            **({"raw_required": True} if report_id in {"pnl", "week-over-week"} else {}))]
        if pnl_ads_missing:
            missing.append("ads")
        if report_id == "week-over-week" and "baskets-summary" in sources:
            from app.wb_week_sources import load_funnel
            periods = [(date_from, date_to)] if selected_date_from is None else [
                (date_from, selected_date_from - timedelta(days=1)), (selected_date_from, date_to)]
            if any(load_funnel(organization_id, left, right)["needsCollection"] for left, right in periods):
                missing.append("baskets-summary")
        return not missing, missing
    missing = [
        source
        for source in sources
        if _report_missing_source_ranges(organization_id, source, date_from=date_from, date_to=date_to, fresh_only=fresh_only)
    ]
    return not missing, missing


def _report_waiting_daily_detail_job(
    report_id: str,
    date_from: date,
    date_to: date,
    group_by: str,
    missing_sources: list[str],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label = "Ждем дневную детализацию WB"
    if missing_sources:
        label = f"{label}: {', '.join(missing_sources)}"
    return {
        **(existing or {}),
        "state": "waiting_daily_detail",
        "reportId": report_id,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "groupBy": group_by,
        "stage": "waiting_daily_detail",
        "label": label,
        "missingSources": missing_sources,
        "percent": 20,
        "updatedAt": _utc_now_iso(),
    }


def _rnp_waiting_baskets_detail_job(
    report_id: str,
    date_from: date,
    date_to: date,
    group_by: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(existing or {}),
        "state": "waiting_baskets_detail",
        "reportId": report_id,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "groupBy": group_by,
        "stage": "waiting_baskets_detail",
        "label": "Ждем дневную детализацию корзин WB для RNP",
        "percent": 20,
        "updatedAt": _utc_now_iso(),
    }


def _float_value(value: Any) -> float | None:
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


def _meta(report_id: ReportId, title: str, description: str, source_type: Literal["operational", "financial"] = "operational", source_status: str = "partial") -> dict[str, Any]:
    freshness = _freshness_state(source_status)
    if report_id in {"pnl", "expenses"} and source_type == "financial" and freshness != "fresh":
        freshness = "pending_financial"
    return {
        "id": report_id,
        "title": title,
        "description": description,
        "sourceType": source_type,
        "freshnessState": freshness,
        "lastUpdatedAt": _utc_now_iso(),
    }


def _kpi(id_: str, label: str, value: str, hint: str | None = None) -> dict[str, Any]:
    payload = {"id": id_, "label": label, "value": value}
    if hint is not None:
        payload["hint"] = hint
    return payload


def _cached_row_units(row: dict[str, Any], default: int = 1) -> int:
    return max(0, _int_value(row.get("_cachedUnits") or row.get("quantity") or default))


def _orders_sales_by_nm(snapshot: Any) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = defaultdict(lambda: {"orders_qty": 0, "orders_revenue": 0, "sales_qty": 0, "sales_revenue": 0, "seller_revenue": 0, "returns_qty": 0})
    for row in snapshot.orders:
        nm_id = int(row.get("nmId") or 0)
        if nm_id <= 0:
            continue
        units = _cached_row_units(row)
        result[nm_id]["orders_qty"] += units
        result[nm_id]["orders_revenue"] += int(round(float(row.get("finishedPrice") or 0) * 100)) * units
    for row in snapshot.sales:
        nm_id = int(row.get("nmId") or 0)
        if nm_id <= 0:
            continue
        units = _cached_row_units(row)
        is_return = bool(row.get("isReturn"))
        result[nm_id]["sales_qty"] += units
        result[nm_id]["sales_revenue"] += int(round(float(row.get("finishedPrice") or 0) * 100)) * units
        result[nm_id]["seller_revenue"] += int(round(float(row.get("forPay") or 0) * 100)) * units
        if is_return:
            result[nm_id]["returns_qty"] += units
    return result


def _average_rubles(total_kopecks: int, units: int) -> float:
    return round(total_kopecks / max(units, 1) / 100, 2)


def _int_map_from_aggregates(aggregates: dict[str, dict[str, Any]], *keys: str) -> dict[int, int]:
    result: dict[int, int] = {}
    for raw_nm_id, row in aggregates.items():
        if not isinstance(row, dict):
            continue
        nm_id = _int_value(row.get("nmId") or raw_nm_id)
        if nm_id <= 0:
            continue
        value = 0
        for key in keys:
            value = _int_value(row.get(key))
            if value or key == "realizationRevenueKopecks" and key in row:
                break
        if value:
            result[nm_id] = value
    return result


def _cached_stock_rows(stock_cache: dict[str, Any]) -> list[WbStockRow]:
    aggregates = _cache_aggregates(stock_cache)
    rows: list[WbStockRow] = []
    for raw_nm_id, aggregate in aggregates.items():
        if not isinstance(aggregate, dict):
            continue
        nm_id = _int_value(aggregate.get("nmId") or raw_nm_id)
        if nm_id <= 0:
            continue
        quantity = _int_value(aggregate.get("wbStockUnits") or aggregate.get("quantity") or aggregate.get("stockUnits"))
        in_way_to_client = _int_value(aggregate.get("inWayToClient") or aggregate.get("inWayToClientUnits"))
        in_way_from_client = _int_value(aggregate.get("inWayFromClient") or aggregate.get("inWayFromClientUnits"))
        available_units = _int_value(aggregate.get("availableUnits"))
        if available_units == 0 and "availableUnits" not in aggregate:
            available_units = quantity + in_way_from_client
        total_stock_units = _int_value(aggregate.get("stockCount") or aggregate.get("totalStockUnits"))

        rows.append(
            WbStockRow(
                nm_id=nm_id,
                chrt_id=_int_value(aggregate.get("chrtId")) or None,
                warehouse_id=_int_value(aggregate.get("warehouseId")) or None,
                warehouse_name=aggregate.get("warehouseName") or aggregate.get("warehouse") or "WB",
                region_name=aggregate.get("regionName") or aggregate.get("clusterName"),
                quantity=quantity,
                in_way_to_client=in_way_to_client,
                in_way_from_client=in_way_from_client,
                available_units=available_units,
                was_out_of_stock=available_units <= 0,
                stock_type="wb",
                total_stock_units=total_stock_units or None,
            )
        )
    return rows


def _cached_activity_rows(
    aggregates: dict[str, dict[str, Any]],
    *,
    date_to: date,
    units_key: str,
    kopecks_key: str,
    seller_payout_by_nm_kopecks: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_nm_id, aggregate in aggregates.items():
        if not isinstance(aggregate, dict):
            continue
        nm_id = _int_value(aggregate.get("nmId") or raw_nm_id)
        units = _int_value(aggregate.get(units_key))
        if nm_id <= 0 or units <= 0:
            continue
        total_kopecks = _int_value(aggregate.get(kopecks_key))
        row = {
            "nmId": nm_id,
            "warehouseName": aggregate.get("warehouseName") or "WB",
            "regionName": aggregate.get("regionName"),
            "lastChangeDate": f"{date_to.isoformat()}T00:00:00",
            "finishedPrice": _average_rubles(total_kopecks, units),
            "_cachedUnits": units,
        }
        if seller_payout_by_nm_kopecks is not None:
            row["forPay"] = _average_rubles(seller_payout_by_nm_kopecks.get(nm_id, 0), units)
        rows.append(row)
    return rows


def build_cached_wb_reports_sources_snapshot(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
) -> WbReportsSourcesSnapshot:
    stock_cache = get_source_cache(organization_id, "stocks", slim=False) or {}
    period_stats = _cache_aggregates(_period_cache(organization_id, "period_stats", date_from, date_to))
    finance_cache = _period_cache(organization_id, "finance", date_from, date_to)
    finance = _cache_aggregates(finance_cache)

    seller_payout_by_nm_kopecks = _int_map_from_aggregates(finance, "sellerPayoutKopecks", "payableKopecks", "forPayKopecks")
    revenue_by_nm_kopecks = _int_map_from_aggregates(finance, "realizationRevenueKopecks", "buyerRevenueKopecks")
    if not revenue_by_nm_kopecks:
        revenue_by_nm_kopecks = _int_map_from_aggregates(finance, "realizationRevenueKopecks", "buyerRevenueKopecks", "sellerRevenueKopecks", "revenueGrossKopecks", "retailAmountKopecks")

    orders = _cached_activity_rows(period_stats, date_to=date_to, units_key="ordersUnits", kopecks_key="ordersKopecks")
    sales = _cached_activity_rows(
        period_stats,
        date_to=date_to,
        units_key="salesUnits",
        kopecks_key="revenueKopecks",
        seller_payout_by_nm_kopecks=seller_payout_by_nm_kopecks,
    )
    stocks = _cached_stock_rows(stock_cache)

    blockers: list[str] = []
    if not stocks:
        blockers.append("WB_CACHE_STOCKS_EMPTY")
    if not period_stats:
        blockers.append("WB_CACHE_PERIOD_STATS_EMPTY")
    if not finance:
        blockers.append("WB_CACHE_FINANCE_EMPTY")
    has_any_data = bool(stocks or period_stats or finance)

    return WbReportsSourcesSnapshot(
        source_status="cached" if has_any_data else "blocked",
        confidence="high" if has_any_data and not blockers else ("medium" if has_any_data else "blocked"),
        blocker_ids=blockers,
        source_evidence=[],
        orders=orders,
        sales=sales,
        stocks=stocks,
        financial_rows=[],
        finance_reconciliation=WbFinanceReconciliation(
            report_id=None,
            retail_amount_kopecks=sum(revenue_by_nm_kopecks.values()) + _int_value(finance_cache.get("unallocatedRealization", {}).get("revenueKopecks")),
            for_pay_kopecks=sum(seller_payout_by_nm_kopecks.values()),
            delivery_service_kopecks=sum(_int_map_from_aggregates(finance, "logisticsKopecks").values()),
            paid_storage_kopecks=sum(_int_map_from_aggregates(finance, "storageKopecks").values()),
            paid_acceptance_kopecks=sum(_int_map_from_aggregates(finance, "acceptanceKopecks").values()),
            penalty_kopecks=sum(_int_map_from_aggregates(finance, "penaltyKopecks").values()),
        )
        if finance
        else None,
        revenue_by_nm_kopecks=revenue_by_nm_kopecks,
        seller_payout_by_nm_kopecks=seller_payout_by_nm_kopecks,
        commission_cost_by_nm_kopecks=_int_map_from_aggregates(finance, "commissionKopecks", "commissionFormulaKopecks"),
        logistics_cost_by_nm_kopecks=_int_map_from_aggregates(finance, "logisticsKopecks"),
        penalties_cost_by_nm_kopecks=_int_map_from_aggregates(finance, "penaltyKopecks"),
        acceptance_cost_by_nm_kopecks=_int_map_from_aggregates(finance, "acceptanceKopecks"),
        storage_cost_by_nm_kopecks=_int_map_from_aggregates(finance, "storageKopecks"),
    )


def _cached_ads_metric(aggregate, *keys):
    value = next((aggregate[key] for key in keys if aggregate.get(key) is not None), None)
    return _int_value(value) if value is not None else None


def build_cached_ads_attribution_snapshot(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
    group_by: str = "sku",
) -> AdsAttributionSnapshot:
    ads_cache = _period_cache(organization_id, "ads", date_from, date_to)
    aggregates = _cache_aggregates(ads_cache)
    daily_aggregates = ads_cache.get("dailyAggregates") if isinstance(ads_cache.get("dailyAggregates"), dict) else {}
    rows: list[AdsAttributionRow] = []
    daily_rows: list[dict[str, Any]] = []
    totals: dict[str, int] = {
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
        nm_id = _int_value(aggregate.get("nmId") or aggregate.get("nmID") or raw_key)
        campaign_id_raw = aggregate.get("campaignId") or aggregate.get("advertId") or aggregate.get("advert_id")
        campaign_id = str(campaign_id_raw) if campaign_id_raw not in (None, "") else None
        ad_spend = _cached_ads_metric(aggregate, "adSpendKopecks", "spendKopecks", "sumKopecks")
        impressions = _cached_ads_metric(aggregate, "impressions", "adImpressions", "views")
        clicks = _cached_ads_metric(aggregate, "clicks", "adClicks")
        cart_adds = _cached_ads_metric(aggregate, "cartAdds", "adCartAdds", "cartCount", "baskets")
        orders_count = _cached_ads_metric(aggregate, "ordersCount", "adOrders", "orderCount", "orders")
        orders_kopecks = _cached_ads_metric(aggregate, "ordersKopecks", "adRevenueKopecks", "orderSumKopecks", "salesKopecks")

        if not any((nm_id > 0, campaign_id, ad_spend, impressions, clicks, cart_adds, orders_count, orders_kopecks)):
            continue

        for name, value in (("ad_spend_kopecks", ad_spend), ("impressions", impressions),
                            ("clicks", clicks), ("cart_adds", cart_adds),
                            ("orders_count", orders_count), ("orders_kopecks", orders_kopecks)):
            if value is None:
                totals.pop(name, None)
            elif name in totals:
                totals[name] += value

        sku_id = str(nm_id) if nm_id > 0 else None
        attribution_level = "campaign_sku" if sku_id and campaign_id else ("exact_sku" if sku_id else "campaign_only")
        rows.append(
            AdsAttributionRow(
                campaign_id=campaign_id,
                sku_id=sku_id,
                attribution_level=attribution_level,
                confidence="high" if ad_spend or orders_count else "medium",
                ad_spend_kopecks=ad_spend,
                impressions=impressions,
                clicks=clicks,
                cart_adds=cart_adds,
                orders_count=orders_count,
                orders_kopecks=orders_kopecks,
                campaign_name=str(aggregate.get("campaignName") or aggregate.get("advertName") or "") or (f"WB карточка {nm_id}" if nm_id > 0 and campaign_id is None else None),
                campaign_type=aggregate.get("campaignType") or aggregate.get("advertType"),
                campaign_status=aggregate.get("campaignStatus") or aggregate.get("status"),
                payment_type=aggregate.get("paymentType"),
            )
        )

    if isinstance(daily_aggregates, dict):
        for raw_day, raw_rows in sorted(daily_aggregates.items()):
            if not isinstance(raw_rows, dict):
                continue
            for raw_key, aggregate in raw_rows.items():
                if not isinstance(aggregate, dict):
                    continue
                nm_id = _int_value(aggregate.get("nmId") or aggregate.get("nmID") or raw_key)
                campaign_id_raw = aggregate.get("campaignId") or aggregate.get("advertId") or aggregate.get("advert_id")
                daily_rows.append(
                    {
                        "date": str(raw_day),
                        "campaignId": str(campaign_id_raw) if campaign_id_raw not in (None, "") else None,
                        "skuId": str(nm_id) if nm_id > 0 else None,
                        "attributionLevel": "campaign_sku" if campaign_id_raw and nm_id > 0 else "exact_sku" if nm_id > 0 else "campaign_only",
                        "adSpendKopecks": _cached_ads_metric(aggregate, "adSpendKopecks", "spendKopecks", "sumKopecks"),
                        "impressions": _cached_ads_metric(aggregate, "impressions", "adImpressions", "views"),
                        "clicks": _cached_ads_metric(aggregate, "clicks", "adClicks"),
                        "cartAdds": _cached_ads_metric(aggregate, "cartAdds", "adCartAdds", "cartCount", "baskets"),
                        "ordersCount": _cached_ads_metric(aggregate, "ordersCount", "adOrders", "orderCount", "orders"),
                        "ordersKopecks": _cached_ads_metric(aggregate, "ordersKopecks", "adRevenueKopecks", "orderSumKopecks", "salesKopecks"),
                    }
                )

    has_any_data = bool(rows or any(totals.values()) or ads_cache.get("sourceComplete") is True)
    if isinstance(ads_cache.get("campaignTotals"), dict):
        totals = dict(ads_cache["campaignTotals"])
    return AdsAttributionSnapshot(
        source_status="partial" if ads_cache.get("sourceComplete") is False else "cached" if has_any_data else "blocked",
        confidence="high" if has_any_data else "blocked",
        blocker_ids=[] if has_any_data else ["WB_ADS_CACHE_EMPTY"],
        source_evidence=[],
        totals=totals,
        rows=rows,
        daily_rows=daily_rows,
        daily_performance_totals=ads_cache.get("dailyCampaignTotals") or {},
        diagnostics={
            "summary": {
                "source": "repricer_ads_cache",
                "sourceComplete": ads_cache.get("sourceComplete") is True,
                "rows": len(rows),
                "blockedAt": None if has_any_data else "wb-ads-cache",
                "blockedSources": [] if has_any_data else ["wb-ads-cache"],
            }
        },
    )


def _previous_period(date_from: date, date_to: date) -> tuple[date, date]:
    period_days = (date_to - date_from).days + 1
    previous_to = date_from - timedelta(days=1)
    return previous_to - timedelta(days=period_days - 1), previous_to


def _delta_pct(current: int | float | None, previous: int | float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / abs(previous) * 100, 2)


def _ads_by_nm(ads_snapshot: Any | None) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = defaultdict(lambda: {"baskets": 0, "ad_spend": 0})
    for row in getattr(ads_snapshot, "rows", []) or []:
        raw_nm_id = getattr(row, "sku_id", None)
        try:
            nm_id = int(raw_nm_id)
        except (TypeError, ValueError):
            continue
        if nm_id <= 0:
            continue
        result[nm_id]["baskets"] += int(getattr(row, "cart_adds", 0) or 0)
        result[nm_id]["ad_spend"] += int(getattr(row, "ad_spend_kopecks", 0) or 0)
    return result


def _profit_by_nm(snapshot: Any, ads_by_nm: dict[int, dict[str, int]]) -> dict[int, dict[str, int | float | None]]:
    # Raw WB streams do not carry dated tax or SKU cost evidence; ABC supplies factual profit.
    return {nm_id: {"profit": None, "margin": None} for nm_id in getattr(snapshot, "revenue_by_nm_kopecks", {})}


def _catalog_meta_by_nm(organization_id: int | None, *, include_shared_facts: bool = True) -> dict[int, dict[str, Any]]:
    if organization_id is None:
        return {}
    result: dict[int, dict[str, Any]] = {}
    for good in list_cached_goods(organization_id):
        nm_id = _int_value(good.get("nmID") or good.get("nmId"))
        if nm_id <= 0:
            continue
        result[nm_id] = {
            "sku": str(good.get("vendorCode") or f"NM_{nm_id}").strip() or f"NM_{nm_id}",
            "productName": good.get("title") or good.get("name"),
            "brand": good.get("brand"),
            "category": good.get("subjectName") or good.get("subject"),
            "photoUrl": _extract_wb_media_url(good, nm_id),
        }
    content_cards = get_source_cache(organization_id, "content_cards", slim=True) or {}
    cards = content_cards.get("cards") if isinstance(content_cards.get("cards"), list) else []
    for card in cards:
        if not isinstance(card, dict):
            continue
        nm_id = _int_value(card.get("nmID") or card.get("nmId"))
        if nm_id <= 0:
            continue
        meta = result.setdefault(nm_id, {"sku": f"NM_{nm_id}", "productName": None, "brand": None, "category": None, "photoUrl": None})
        if card.get("vendorCode"):
            meta["sku"] = str(card.get("vendorCode"))
        if card.get("title"):
            meta["productName"] = card.get("title")
        if card.get("brand"):
            meta["brand"] = card.get("brand")
        if card.get("subjectName") or card.get("object"):
            meta["category"] = card.get("subjectName") or card.get("object")
        meta["photoUrl"] = _extract_wb_media_url(card, nm_id) or meta.get("photoUrl")
    # Advertising needs cached titles, not the shared catalog's cost calculation.
    from app.platform.economics.legacy_catalog import catalog_facts
    for nm_id, fact in catalog_facts(organization_id, include_costs=False).items():
        meta = result.setdefault(nm_id, {})
        for key in ("photoUrl", "productName", "sku"):
            if fact.get(key) and (include_shared_facts or not meta.get(key) or (key == "sku" and meta[key] == f"NM_{nm_id}")):
                meta[key] = fact[key]
    return result


def _dict_or_attr(node: Any, key: str, default: Any = None) -> Any:
    if isinstance(node, dict):
        return node.get(key, default)
    return getattr(node, key, default)


def _date_from_source_row(row: dict[str, Any]) -> date | None:
    raw = row.get("lastChangeDate") or row.get("date") or row.get("saleDt") or row.get("orderDt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None


def _digest_period_bounds(date_range: dict[str, str]) -> tuple[date, date]:
    return date.fromisoformat(date_range["from"]), date.fromisoformat(date_range["to"])


def _empty_daily_bucket(day: date) -> dict[str, Any]:
    return {
        "label": day.strftime("%d.%m"),
        "date": day.isoformat(),
        "ordersUnits": 0,
        "ordersKopecks": 0,
        "salesUnits": 0,
        "salesKopecks": 0,
        "returnsUnits": 0,
        "buyoutPct": None,
    }


def _funnel_rows_totals(rows: list[dict[str, Any]]) -> dict[str, int | float | None]:
    order_count = sum(_int_value(row.get("orderCount")) for row in rows)
    order_sum = sum(_int_value(row.get("orderSumKopecks")) for row in rows)
    buyout_count = sum(_int_value(row.get("buyoutCount")) for row in rows)
    buyout_sum = sum(_int_value(row.get("buyoutSumKopecks")) for row in rows)
    cancel_count = sum(_int_value(row.get("cancelCount")) for row in rows)
    open_count = sum(_int_value(row.get("openCount")) for row in rows)
    cart_count = sum(_int_value(row.get("cartCount")) for row in rows)
    return {
        "openCount": open_count,
        "cartCount": cart_count,
        "orderCount": order_count,
        "orderSumKopecks": order_sum,
        "buyoutCount": buyout_count,
        "buyoutSumKopecks": buyout_sum,
        "cancelCount": cancel_count,
        "buyoutPct": round(buyout_count / (buyout_count + cancel_count) * 100, 1) if buyout_count + cancel_count > 0 else None,
    }


def _cached_funnel_rows_from_baskets_cache(cache: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_nm_id, aggregate in _cache_aggregates(cache).items():
        if not isinstance(aggregate, dict):
            continue
        nm_id = _int_value(aggregate.get("nmId") or aggregate.get("nmID") or raw_nm_id)
        if nm_id <= 0:
            continue
        rows.append(
            {
                "nmId": nm_id,
                "openCount": _int_value(
                    aggregate.get("openCount")
                    or aggregate.get("openCard")
                    or aggregate.get("openCardCount")
                    or aggregate.get("views")
                    or aggregate.get("viewCount")
                ),
                "cartCount": _int_value(
                    aggregate.get("cartCount")
                    or aggregate.get("cartAdds")
                    or aggregate.get("addToCart")
                    or aggregate.get("addToCartCount")
                    or aggregate.get("basketCount")
                    or aggregate.get("baskets")
                ),
                "orderCount": _int_value(aggregate.get("orderCount") or aggregate.get("ordersCount") or aggregate.get("orders")),
                "orderSumKopecks": _int_value(aggregate.get("orderSumKopecks") or aggregate.get("ordersKopecks")),
                "buyoutCount": _int_value(aggregate.get("buyoutCount") or aggregate.get("salesUnits")),
                "buyoutSumKopecks": _int_value(aggregate.get("buyoutSumKopecks") or aggregate.get("salesKopecks") or aggregate.get("revenueKopecks")),
                "cancelCount": _int_value(aggregate.get("cancelCount") or aggregate.get("returnsUnits")),
            }
        )
    return rows


def _cached_funnel_daily_rows(cache: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw_daily = cache.get("dailyAggregates") or cache.get("dailyRows") or cache.get("daily")
    if not isinstance(raw_daily, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for raw_day, raw_rows in raw_daily.items():
        if isinstance(raw_rows, dict):
            rows = _cached_funnel_rows_from_baskets_cache({"aggregates": raw_rows})
        elif isinstance(raw_rows, list):
            rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
        else:
            rows = []
        if rows:
            result[str(raw_day)] = rows
    return result


def _funnel_rows_from_merged_daily(merged: dict[str, Any]) -> list[dict[str, Any]]:
    """Sum per-SKU daily funnel rows into one row set for the whole period.

    Lets a range that crosses cache boundaries still report totals instead of
    falling back to "blocked" and rendering every KPI as zero.
    """
    countable = (
        "openCount", "cartCount", "orderCount", "orderSumKopecks",
        "buyoutCount", "buyoutSumKopecks", "cancelCount", "cancelSumKopecks",
        "impressions", "clicks",
    )
    totals: dict[str, dict[str, Any]] = {}
    for rows in merged.values():
        if not isinstance(rows, dict):
            continue
        for raw_nm_id, row in rows.items():
            if not isinstance(row, dict):
                continue
            accumulated = totals.setdefault(str(raw_nm_id), {})
            for field in countable:
                value = row.get(field)
                if value is None:
                    continue
                accumulated[field] = _int_value(accumulated.get(field)) + _int_value(value)
    return _cached_funnel_rows_from_baskets_cache({"aggregates": totals})


def _merged_daily_aggregates(
    organization_id: int,
    prefix: str,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """Merge raw dailyAggregates for a window across the caches that hold them.

    Same reason as the funnel variant: the days are spread over several cache
    windows, and the covering-cache query is far too expensive to reach for.
    """
    wanted = {
        (date_from + timedelta(days=offset)).isoformat()
        for offset in range((date_to - date_from).days + 1)
    }
    candidates: list[tuple[int, str]] = []
    for meta in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100):
        if not isinstance(meta, dict):
            continue
        source_key = str(meta.get("sourceKey") or "")
        if not source_key or "detail_status" in source_key or source_key.endswith("detail_active"):
            continue
        meta_from = _safe_cache_date(meta.get("dateFrom"))
        meta_to = _safe_cache_date(meta.get("dateTo"))
        if meta_from and meta_to:
            if meta_to < date_from or meta_from > date_to:
                continue
            overlap = (min(meta_to, date_to) - max(meta_from, date_from)).days + 1
        else:
            overlap = 0
        candidates.append((overlap, source_key))

    merged: dict[str, Any] = {}
    for _overlap, source_key in sorted(candidates, key=lambda item: item[0], reverse=True):
        if not wanted - merged.keys():
            break
        cache = get_source_cache(organization_id, source_key, slim=False) or {}
        if prefix == "finance_" and not finance_cache_uses_current_revenue_basis(cache):
            continue
        raw_daily = cache.get("dailyAggregates")
        if not isinstance(raw_daily, dict):
            continue
        for raw_day, rows in raw_daily.items():
            day = str(raw_day)
            if day in wanted and day not in merged and rows:
                merged[day] = rows
    return merged


def _merged_funnel_daily_rows(
    organization_id: int,
    date_from: date,
    date_to: date,
) -> dict[str, list[dict[str, Any]]]:
    """Stitch basket daily detail for a window out of every cache that holds it.

    Daily detail is filled in chunks, so a 14-day chart routinely needs days
    spread over several cache windows.  Reading only the newest covering cache
    left the older half of the chart at zero orders while those days sat in a
    neighbouring cache.

    Each cache carries thousands of SKU rows, so we only open the ones whose
    range actually overlaps the window, widest overlap first, and stop as soon
    as every day is accounted for.
    """
    wanted = {
        (date_from + timedelta(days=offset)).isoformat()
        for offset in range((date_to - date_from).days + 1)
    }
    candidates: list[tuple[int, str]] = []
    for meta in list_source_cache_ranges_by_prefix(organization_id, "baskets_", limit=100):
        if not isinstance(meta, dict):
            continue
        source_key = str(meta.get("sourceKey") or "")
        if not source_key or "detail_status" in source_key or source_key.endswith("detail_active"):
            continue
        meta_from = _safe_cache_date(meta.get("dateFrom"))
        meta_to = _safe_cache_date(meta.get("dateTo"))
        if meta_from and meta_to:
            if meta_to < date_from or meta_from > date_to:
                continue
            overlap = (min(meta_to, date_to) - max(meta_from, date_from)).days + 1
        else:
            overlap = 0
        candidates.append((overlap, source_key))

    merged: dict[str, list[dict[str, Any]]] = {}
    for _overlap, source_key in sorted(candidates, key=lambda item: item[0], reverse=True):
        if not wanted - merged.keys():
            break
        cache = get_source_cache(organization_id, source_key, slim=False) or {}
        raw_daily = cache.get("dailyAggregates") or cache.get("dailyRows") or cache.get("daily")
        if not isinstance(raw_daily, dict):
            continue
        for raw_day, raw_rows in raw_daily.items():
            day = str(raw_day)
            if day not in wanted or day in merged:
                continue
            if isinstance(raw_rows, dict):
                rows = _cached_funnel_rows_from_baskets_cache({"aggregates": raw_rows})
            elif isinstance(raw_rows, list):
                rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
            else:
                rows = []
            if rows:
                merged[day] = rows
    return merged


def _safe_cache_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _build_digest_funnel_snapshot(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
    wb_token: str | None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    cache = _period_cache(organization_id, "baskets", date_from, date_to)
    comparison_end = min(date_to, datetime.now(timezone(timedelta(hours=3))).date() - timedelta(days=1))
    graph_from = min(date_from, comparison_end - timedelta(days=5))
    rows = _cached_funnel_rows_from_baskets_cache(cache)
    if not rows:
        rows = _funnel_rows_from_merged_daily(
            _merged_daily_aggregates(organization_id, "baskets_", date_from, date_to)
        )
    daily = _merged_funnel_daily_rows(organization_id, graph_from, date_to)
    if not daily:
        graph_cache = get_covering_source_cache(
            organization_id,
            "baskets_",
            date_from=graph_from,
            date_to=date_to,
            slim=False,
        ) or cache
        daily = _cached_funnel_daily_rows(graph_cache)
    finance_daily = _merged_daily_aggregates(organization_id, "finance_", graph_from, date_to)
    if not finance_daily:
        finance_cache = get_covering_source_cache(
            organization_id,
            "finance_",
            date_from=graph_from,
            date_to=date_to,
            slim=False,
        ) or {}
        if not finance_cache_uses_current_revenue_basis(finance_cache):
            finance_cache = _period_cache(organization_id, "finance", date_from, date_to)
        raw_finance = finance_cache.get("dailyAggregates")
        finance_daily = raw_finance if isinstance(raw_finance, dict) else {}
    errors = [] if rows else ["WB_FUNNEL_CACHE_EMPTY"]
    status = "hit" if cache else "missing"
    return {
        "topMovers": _top_movers_from_cache(cache, date_from, date_to),
        "rows": rows,
        "dailyRows": daily,
        "financeDailyRows": finance_daily,
        "totals": _funnel_rows_totals(rows),
        "status": "fresh" if rows else ("blocked" if errors else "empty"),
        "cacheStatus": status,
        "errors": errors,
    }


def _top_movers_from_cache(cache: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """WB's selected/past pair compares equal inclusive ranges in one read.

    Never subtract a wider cache or treat an omitted product as zero. Legacy
    buyout normalization defaulted omissions to zero; require explicit proof
    before presenting a sales delta from those observations.
    """
    previous_start, previous_end = _previous_period(start, end)
    result = {"rows": [], "status": "unavailable", "dateFrom": str(start), "dateTo": str(end),
        "comparisonFrom": str(previous_start), "comparisonTo": str(previous_end)}
    if str(cache.get("dateFrom", ""))[:10] != str(start) or str(cache.get("dateTo", ""))[:10] != str(end):
        return result
    if cache.get("comparisonDateFrom") != str(previous_start) or cache.get("comparisonDateTo") != str(previous_end):
        return result
    eligible = 0
    complete_metrics = True
    for nm, current in (cache.get("aggregates") or {}).items():
        previous = current.get("previous") or {}
        if not isinstance(previous, dict) or current.get("orderCount") is None or previous.get("orderCount") is None:
            continue
        eligible += 1
        order_delta = current["orderCount"] - previous["orderCount"]
        sales_delta = (current["buyoutCount"] - previous["buyoutCount"]
            if current.get("buyoutCountProvided") is True and previous.get("buyoutCountProvided") is True else None)
        complete_metrics = complete_metrics and sales_delta is not None
        if not order_delta and not sales_delta:
            continue
        result["rows"].append({"nmId": int(nm), "sku": current.get("vendorCode") or str(nm),
            "title": current.get("productName") or f"Товар WB {nm}",
            "salesDelta": sales_delta, "ordersDelta": order_delta})
    result["rows"] = sorted(result["rows"], key=lambda row: (-abs(row["salesDelta"] if row["salesDelta"] is not None else row["ordersDelta"]), row["nmId"]))[:20]
    result["status"] = ("ready" if cache.get("aggregateComplete") is True and complete_metrics
        and eligible == len(cache.get("aggregates") or {}) else "partial" if eligible else "unavailable")
    return result


def _build_weekly_balance(date_range: dict[str, str], source_snapshot: Any) -> dict[str, Any]:
    date_from, date_to = _digest_period_bounds(date_range)
    days = [date_from + timedelta(days=offset) for offset in range((date_to - date_from).days + 1)]
    buckets = {day: _empty_daily_bucket(day) for day in days}

    for row in source_snapshot.orders:
        row_date = _date_from_source_row(row)
        if row_date not in buckets:
            continue
        units = _cached_row_units(row)
        buckets[row_date]["ordersUnits"] += units
        buckets[row_date]["ordersKopecks"] += int(round(float(row.get("finishedPrice") or 0) * 100)) * units

    for row in source_snapshot.sales:
        row_date = _date_from_source_row(row)
        if row_date not in buckets:
            continue
        units = _cached_row_units(row)
        if bool(row.get("isReturn")):
            buckets[row_date]["returnsUnits"] += units
            continue
        buckets[row_date]["salesUnits"] += units
        buckets[row_date]["salesKopecks"] += int(round(float(row.get("finishedPrice") or 0) * 100)) * units

    points = []
    for bucket in buckets.values():
        orders_units = int(bucket["ordersUnits"])
        sales_units = int(bucket["salesUnits"])
        bucket["buyoutPct"] = round(sales_units / orders_units * 100, 1) if orders_units > 0 else None
        bucket["coverageComplete"] = source_snapshot.source_status in {"fresh", "cached"}
        points.append(bucket)

    return {
        "title": "Баланс за неделю",
        "valueLabel": "Заказы",
        "compareLabel": "Продажи",
        "points": points,
    }


def _build_funnel_weekly_balance(date_range: dict[str, str], funnel_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    daily_rows = funnel_snapshot.get("dailyRows") if isinstance(funnel_snapshot.get("dailyRows"), dict) else {}
    if not daily_rows:
        return None
    finance_daily_rows = funnel_snapshot.get("financeDailyRows") if isinstance(funnel_snapshot.get("financeDailyRows"), dict) else {}
    date_from, date_to = _digest_period_bounds(date_range)
    comparison_end = min(date_to, datetime.now(timezone(timedelta(hours=3))).date() - timedelta(days=1))
    graph_from = min(date_from, comparison_end - timedelta(days=5))
    days = [graph_from + timedelta(days=offset) for offset in range((date_to - graph_from).days + 1)]
    points = []
    for day in days:
        rows = daily_rows.get(day.isoformat())
        totals = _funnel_rows_totals([row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else [])
        finance_rows = finance_daily_rows.get(day.isoformat())
        finance_rows = finance_rows.values() if isinstance(finance_rows, dict) else []
        finance_rows = [row for row in finance_rows if isinstance(row, dict)]
        gross_sales_units = sum(_int_value(row.get("salesUnits")) for row in finance_rows)
        returns_units = sum(_int_value(row.get("returnsUnits")) for row in finance_rows)
        sales_units = _int_value(totals["buyoutCount"]) if isinstance(rows, list) else None
        sales_kopecks = _int_value(totals["buyoutSumKopecks"]) if isinstance(rows, list) else None
        returns_units = returns_units if day.isoformat() in finance_daily_rows else None
        bucket = _empty_daily_bucket(day)
        bucket.update(
            {
                "ordersUnits": totals["orderCount"] if isinstance(rows, list) else None,
                "ordersKopecks": totals["orderSumKopecks"] if isinstance(rows, list) else None,
                "salesUnits": sales_units,
                "salesKopecks": sales_kopecks,
                "returnsUnits": returns_units,
                "buyoutPct": totals["buyoutPct"],
                "cancelCount": totals["cancelCount"] if isinstance(rows, list) else None,
                "openCount": totals["openCount"] if isinstance(rows, list) else None,
                "cartCount": totals["cartCount"] if isinstance(rows, list) else None,
                "source": "wb_sales_funnel",
                "coverageComplete": isinstance(rows, list),
            }
        )
        points.append(bucket)
    return {
        "title": "Воронка продаж по дням",
        "valueLabel": "Заказали",
        "compareLabel": "Выкупили",
        "points": [point for point in points if date_from.isoformat() <= point["date"] <= date_to.isoformat()],
        "comparisonPoints": [point for point in points if (comparison_end - timedelta(days=5)).isoformat() <= point["date"] <= comparison_end.isoformat()],
        "source": "wb_sales_funnel",
    }


def _period_card(id_: str, label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    orders_units = sum(_int_value(row.get("ordersUnits")) for row in rows)
    orders_kopecks = sum(_int_value(row.get("ordersKopecks")) for row in rows)
    sales_units = sum(_int_value(row.get("salesUnits")) for row in rows)
    sales_kopecks = sum(_int_value(row.get("salesKopecks")) for row in rows)
    returns_units = sum(_int_value(row.get("returnsUnits")) for row in rows)
    buyout_pct = round(sales_units / orders_units * 100, 1) if orders_units > 0 and all(row.get("ordersUnits") is not None and row.get("salesUnits") is not None for row in rows) else None
    return {
        "id": id_,
        "label": label,
        "ordersUnits": orders_units if rows and all(row.get("ordersUnits") is not None for row in rows) else None,
        "ordersKopecks": orders_kopecks if rows and all(row.get("ordersKopecks") is not None for row in rows) else None,
        "salesUnits": sales_units if rows and all(row.get("salesUnits") is not None for row in rows) else None,
        "returnsUnits": returns_units if rows and all(row.get("returnsUnits") is not None for row in rows) else None,
        "buyoutPct": buyout_pct,
        "revenueKopecks": sales_kopecks if rows and all(row.get("salesKopecks") is not None for row in rows) else None,
        "openCount": sum(row["openCount"] for row in rows) if rows and all(row.get("openCount") is not None for row in rows) else None,
        "cartCount": sum(row["cartCount"] for row in rows) if rows and all(row.get("cartCount") is not None for row in rows) else None,
    }


def _build_period_cards(date_range: dict[str, str], weekly_balance: dict[str, Any]) -> list[dict[str, Any]]:
    _date_from, date_to = _digest_period_bounds(date_range)
    points = list(weekly_balance["points"])
    today_rows = [row for row in points if row["date"] == date_to.isoformat()]
    yesterday_rows = [row for row in points if row["date"] == (date_to - timedelta(days=1)).isoformat()]
    return [
        _period_card("selected", "Выбранный период", points),
        _period_card("today", "Сегодня", today_rows),
        _period_card("yesterday", "Вчера", yesterday_rows),
    ]


def _preliminary_margin_kopecks(source_snapshot: Any) -> int:
    seller_payout = sum(int(value or 0) for value in getattr(source_snapshot, "seller_payout_by_nm_kopecks", {}).values())
    costs = 0
    for attr in (
        "commission_cost_by_nm_kopecks",
        "logistics_cost_by_nm_kopecks",
        "penalties_cost_by_nm_kopecks",
        "acceptance_cost_by_nm_kopecks",
        "storage_cost_by_nm_kopecks",
    ):
        costs += sum(abs(int(value or 0)) for value in getattr(source_snapshot, attr, {}).values())
    return seller_payout - costs


def _digest_oos_affected_item(stock_row: dict[str, Any]) -> dict[str, Any]:
    available_units = _as_int(stock_row.get("availableUnits"))
    return {
        "sku": str(stock_row.get("sku") or f"NM_{stock_row.get('nmId') or ''}"),
        "nmId": _as_int(stock_row.get("nmId")),
        "warehouseName": str(stock_row.get("warehouseName") or "unknown"),
        "availableUnits": available_units,
        "wbStockUnits": _as_int(stock_row.get("wbStockUnits")),
        "fromClientUnits": _as_int(stock_row.get("fromClientUnits")),
        "toClientUnits": _as_int(stock_row.get("toClientUnits")),
        "reason": f"Доступный остаток {available_units or 0} шт",
    }


def _build_digest_problem_rows(stock_rows: list[dict[str, Any]], ads_snapshot: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Stocks arrive per warehouse, while the digest and its link promise a SKU count.
    # Keep one row per NM ID so a SKU stored in two empty warehouses is not counted twice.
    oos_by_nm: dict[str, dict[str, Any]] = {}
    for stock_row in stock_rows:
        if stock_row["availableUnits"] <= 0:
            oos_by_nm.setdefault(str(stock_row.get("nmId") or stock_row["sku"]), stock_row)
    oos_rows = list(oos_by_nm.values())
    if oos_rows:
        sample_skus = [str(row["sku"]) for row in oos_rows[:5]]
        affected_items = [_digest_oos_affected_item(row) for row in oos_rows]
        rows.append(
            {
                "id": "oos-risk",
                "kind": "oos_risk",
                "title": f"{len(oos_rows)} SKU с риском OOS",
                "details": f"Товары, у которых нулевой доступный остаток: {', '.join(sample_skus)}"
                + ("..." if len(oos_rows) > len(sample_skus) else ""),
                "metric": "Остатки",
                "reason": "oos_risk",
                "recommendation": "Проверить поставку и остатки WB",
                "skuCount": len(oos_rows),
                "affectedItems": affected_items,
            }
        )
    weak_ads_rows: list[Any] = []
    for row in getattr(ads_snapshot, "rows", []):
        attribution_level = _dict_or_attr(row, "attributionLevel")
        if attribution_level in {"campaign_only", "unknown"}:
            weak_ads_rows.append(row)
    if weak_ads_rows:
        sample_campaigns = [str(_dict_or_attr(row, "campaignName", _dict_or_attr(row, "campaignId", "РК"))) for row in weak_ads_rows[:3]]
        rows.append(
            {
                "id": "weak-ads-attribution",
                "kind": "weak_ads_attribution",
                "title": f"{len(weak_ads_rows)} кампании требуют review",
                "details": f"Ads attribution частичная: {', '.join(sample_campaigns)}. Stop/action только после approval.",
                "metric": "Реклама",
                "reason": "weak_ads_attribution",
                "recommendation": "Review перед stop/action",
                "campaignCount": len(weak_ads_rows),
            }
        )
    return rows


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip().replace(",", ".")))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _extract_payload_items(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        for key in ("items", "data", "rows", "report", "warehouseList", "stocks"):
            candidate = node.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
            if isinstance(candidate, dict):
                nested = _extract_payload_items(candidate)
                if nested:
                    return nested
        response = node.get("response")
        if isinstance(response, dict):
            nested = _extract_payload_items(response)
            if nested:
                return nested
        return [node]
    return []


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _stock_rows_as_dicts(rows: list[WbStockRow]) -> list[dict[str, Any]]:
    return [
        {
            "nmId": row.nm_id,
            "chrtId": row.chrt_id,
            "warehouseId": row.warehouse_id,
            "warehouseName": row.warehouse_name,
            "regionName": row.region_name,
            "quantity": row.quantity,
            "inWayToClient": row.in_way_to_client,
            "inWayFromClient": row.in_way_from_client,
            "availableUnits": row.available_units,
            "wasOutOfStock": row.was_out_of_stock,
            "stockType": row.stock_type,
            "totalStockUnits": row.total_stock_units,
        }
        for row in rows
    ]


def _financial_rows_as_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "rrdId": row.rrd_id,
            "nmId": row.nm_id,
            "srid": row.srid,
            "retailAmountKopecks": row.retail_amount_kopecks,
            "sellerPayoutKopecks": row.seller_payout_kopecks,
            "commissionPercent": row.commission_percent,
            "commissionKopecks": row.commission_kopecks,
            "logisticsKopecks": row.logistics_kopecks,
        }
        for row in rows
    ]


def _cache_source_payload(
    *,
    organization_id: int,
    source_key: str,
    status: str,
    payload: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    rows = _extract_payload_items(payload.get("data", payload))
    stored = {
        "sourceKey": source_key,
        "status": status,
        "rowsCount": len(rows),
        **payload,
    }
    if error:
        stored["error"] = error
    if status in {"fresh", "partial"}:
        return save_source_cache(organization_id, source_key, stored)
    cached = get_source_cache(organization_id, source_key)
    if cached:
        cached_payload = dict(cached)
        cached_payload["status"] = "cached"
        cached_payload.setdefault("sourceKey", source_key)
        cached_payload.setdefault("rowsCount", len(_extract_payload_items(cached_payload.get("data", cached_payload))))
        if error:
            cached_payload["error"] = error
        return cached_payload
    return stored


def _request_source_with_cache(
    *,
    organization_id: int,
    source_key: str,
    client: RateLimitedWbApiClient,
    request: WbApiRequest,
) -> dict[str, Any]:
    try:
        envelope = client.request(request)
    except Exception as exc:  # pragma: no cover - real network/client guard
        return _cache_source_payload(
            organization_id=organization_id,
            source_key=source_key,
            status="error",
            payload={"data": None, "request": request.model_dump(mode="json")},
            error=str(exc),
        )
    status = "fresh" if envelope.ok else "error"
    return _cache_source_payload(
        organization_id=organization_id,
        source_key=source_key,
        status=status,
        payload={
            "data": envelope.data,
            "statusCode": envelope.statusCode,
            "request": envelope.request.model_dump(mode="json"),
            "wbRequestId": envelope.wbRequestId,
        },
        error=envelope.error.message if envelope.error else None,
    )


STOCK_AVAILABILITY_FILTERS = ["deficient", "actual", "balanced", "nonActual", "nonLiquid", "invalidData"]


def _stock_products_request_body(date_range: dict[str, str], *, stock_type: str = "") -> dict[str, Any]:
    return {
        "nmIDs": [],
        "currentPeriod": {"start": date_range["from"], "end": date_range["to"]},
        "stockType": stock_type,
        "skipDeletedNm": True,
        "orderBy": {"field": "avgOrders", "mode": "desc"},
        "availabilityFilters": STOCK_AVAILABILITY_FILTERS,
        "limit": 1000,
        "offset": 0,
    }


def _build_stock_source_bundle(
    *,
    organization_id: int,
    snapshot: Any,
    date_range: dict[str, str],
    wb_token: str | None,
    read_only: bool = False,
) -> dict[str, dict[str, Any]]:
    if read_only:
        result = {key: get_source_cache(organization_id, key) or {"status": "missing"} for key in (
            "seller_stock_current", "buyer_region_sales", "stock_product_metrics", "stock_size_office_metrics",
            "wb_offices_dim", "fbw_warehouses_dim", "box_tariffs", "pallet_tariffs", "local_orders_exact")}
        for key, data in (("wb_stock_current", _stock_rows_as_dicts(snapshot.stocks)),
                          ("wb_stock_remains_async", _stock_rows_as_dicts(snapshot.stocks)),
                          ("raw_orders", snapshot.orders), ("raw_sales", snapshot.sales)):
            result[key] = {"sourceKey": key, "status": "cached" if data else "missing", "data": data, "rowsCount": len(data)}
        result["finance_logistics_fact"] = {"status": "cached" if snapshot.financial_rows else "missing",
            "financialRows": _financial_rows_as_dicts(snapshot.financial_rows)}
        return result
    sources: dict[str, dict[str, Any]] = {
        "wb_stock_current": _cache_source_payload(
            organization_id=organization_id,
            source_key="wb_stock_current",
            status="fresh" if snapshot.stocks else snapshot.source_status,
            payload={"data": _stock_rows_as_dicts(snapshot.stocks), "grain": "nmId x chrtId x warehouseId"},
        ),
        "wb_stock_remains_async": _cache_source_payload(
            organization_id=organization_id,
            source_key="wb_stock_remains_async",
            status="fresh" if snapshot.stocks else snapshot.source_status,
            payload={"data": _stock_rows_as_dicts(snapshot.stocks), "grain": "nmId x barcode x warehouseName"},
        ),
        "raw_orders": _cache_source_payload(
            organization_id=organization_id,
            source_key="raw_orders",
            status="fresh" if snapshot.orders else snapshot.source_status,
            payload={"data": snapshot.orders, "grain": "srid x nmId x warehouseName"},
        ),
        "raw_sales": _cache_source_payload(
            organization_id=organization_id,
            source_key="raw_sales",
            status="fresh" if snapshot.sales else snapshot.source_status,
            payload={"data": snapshot.sales, "grain": "sale/return x nmId x warehouseName"},
        ),
        "finance_logistics_fact": _cache_source_payload(
            organization_id=organization_id,
            source_key="finance_logistics_fact",
            status="fresh" if snapshot.financial_rows else snapshot.source_status,
            payload={"financialRows": _financial_rows_as_dicts(snapshot.financial_rows), "grain": "rrdId / srid / nmId"},
        ),
    }

    if get_settings().wb_api_mode == "real" and not wb_token:
        for key in (
            "seller_stock_current",
            "buyer_region_sales",
            "stock_product_metrics",
            "stock_size_office_metrics",
            "wb_offices_dim",
            "fbw_warehouses_dim",
            "box_tariffs",
            "pallet_tariffs",
        ):
            sources[key] = _cache_source_payload(
                organization_id=organization_id,
                source_key=key,
                status="blocked",
                payload={"data": None},
                error="WB_TOKEN_REQUIRED",
            )
    else:
        analytics_client = RateLimitedWbApiClient(inner=build_wb_analytics_client(token_override=wb_token))
        marketplace_client = RateLimitedWbApiClient(inner=build_wb_marketplace_client(token_override=wb_token))
        supplies_client = RateLimitedWbApiClient(inner=build_wb_supplies_client(token_override=wb_token))
        common_client = RateLimitedWbApiClient(inner=build_wb_common_client(token_override=wb_token))
        sources["buyer_region_sales"] = _request_source_with_cache(
            organization_id=organization_id,
            source_key="buyer_region_sales",
            client=analytics_client,
            request=WbApiRequest(method="GET", path="/api/v1/analytics/region-sale", query={"dateFrom": date_range["from"], "dateTo": date_range["to"]}),
        )
        sources["stock_product_metrics"] = _request_source_with_cache(
            organization_id=organization_id,
            source_key="stock_product_metrics",
            client=analytics_client,
            request=WbApiRequest(
                method="POST",
                path="/api/v2/stocks-report/products/products",
                jsonBody=_stock_products_request_body(date_range, stock_type=""),
            ),
        )
        sources["stock_size_office_metrics"] = _cache_source_payload(
            organization_id=organization_id,
            source_key="stock_size_office_metrics",
            status="missing",
            payload={
                "data": None,
                "reason": "WB /api/v2/stocks-report/products/sizes requires a concrete nmID; product drilldown is not requested for the summary table",
            },
        )
        sources["wb_offices_dim"] = _request_source_with_cache(
            organization_id=organization_id,
            source_key="wb_offices_dim",
            client=marketplace_client,
            request=WbApiRequest(method="GET", path="/api/v3/offices"),
        )
        sources["fbw_warehouses_dim"] = _request_source_with_cache(
            organization_id=organization_id,
            source_key="fbw_warehouses_dim",
            client=supplies_client,
            request=WbApiRequest(method="GET", path="/api/v1/warehouses"),
        )
        sources["box_tariffs"] = _request_source_with_cache(
            organization_id=organization_id,
            source_key="box_tariffs",
            client=common_client,
            request=WbApiRequest(method="GET", path="/api/v1/tariffs/box", query={"date": date_range["to"]}),
        )
        sources["pallet_tariffs"] = _request_source_with_cache(
            organization_id=organization_id,
            source_key="pallet_tariffs",
            client=common_client,
            request=WbApiRequest(method="GET", path="/api/v1/tariffs/pallet", query={"date": date_range["to"]}),
        )

        configured_ids = set(get_settings().wb_seller_stock_warehouse_ids)
        stock_ids = {row.warehouse_id for row in snapshot.stocks if row.warehouse_id}
        warehouse_ids = sorted(configured_ids | stock_ids)[:10]
        seller_stock_rows: list[dict[str, Any]] = []
        seller_stock_errors: list[str] = []
        for warehouse_id in warehouse_ids:
            result = _request_source_with_cache(
                organization_id=organization_id,
                source_key=f"seller_stock_current_{warehouse_id}",
                client=marketplace_client,
                request=WbApiRequest(method="POST", path=f"/api/v3/stocks/{warehouse_id}", jsonBody={"skus": []}),
            )
            if result.get("status") in {"fresh", "cached"}:
                seller_stock_rows.extend(_extract_payload_items(result.get("data")))
            if result.get("error"):
                seller_stock_errors.append(str(result["error"]))
        sources["seller_stock_current"] = _cache_source_payload(
            organization_id=organization_id,
            source_key="seller_stock_current",
            status="fresh" if seller_stock_rows else "partial",
            payload={"data": seller_stock_rows, "warehouseIds": warehouse_ids, "grain": "warehouseId x chrtId"},
            error="; ".join(seller_stock_errors[:3]) if seller_stock_errors else None,
        )

    for manual_key in ("local_orders_exact",):
        cached = get_source_cache(organization_id, manual_key)
        sources[manual_key] = cached if cached else {"sourceKey": manual_key, "status": "missing", "rowsCount": 0, "data": None}
    sources["krp_table"] = _cache_source_payload(
        organization_id=organization_id,
        source_key="krp_table",
        status="fresh",
        payload={
            "data": [
                {"fromPct": 0, "toPct": 29.99, "krp": 1.0},
                {"fromPct": 30, "toPct": 59.99, "krp": 0.8},
                {"fromPct": 60, "toPct": 100, "krp": 0.6},
            ],
            "source": "backend_public_doc_seed",
        },
    )
    return sources


def _source_coverage(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "entity": key,
            "status": value.get("status", "unknown"),
            "rowsCount": value.get("rowsCount", len(_extract_payload_items(value.get("data", value)))),
            "fetchedAt": value.get("fetchedAt"),
            "error": value.get("error"),
        }
        for key, value in sorted(sources.items())
    ]


def _orders_index(orders: list[dict[str, Any]], days: int) -> tuple[dict[tuple[int, str], int], dict[int, int], dict[tuple[int, str], int]]:
    by_nm_warehouse: dict[tuple[int, str], int] = defaultdict(int)
    by_nm: dict[int, int] = defaultdict(int)
    local_by_nm_cluster: dict[tuple[int, str], int] = defaultdict(int)
    seen: set[str] = set()
    for row in orders:
        nm_id = _as_int(row.get("nmId"))
        if not nm_id:
            continue
        if bool(row.get("isCancel")):
            continue
        srid = str(row.get("srid") or row.get("gNumber") or "")
        dedupe = srid or f"{nm_id}:{row.get('lastChangeDate')}:{row.get('warehouseName')}"
        units = _cached_row_units(row)
        if row.get("_cachedUnits") is None and dedupe in seen:
            continue
        seen.add(dedupe)
        warehouse = _normalize_key(row.get("warehouseName"))
        buyer_region = _normalize_key(row.get("regionName") or row.get("oblastOkrugName") or row.get("oblast"))
        by_nm[nm_id] += units
        by_nm_warehouse[(nm_id, warehouse)] += units
        if buyer_region:
            local_by_nm_cluster[(nm_id, buyer_region)] += units
    return dict(by_nm_warehouse), dict(by_nm), dict(local_by_nm_cluster)


def _sales_units_by_nm(sales: list[dict[str, Any]]) -> dict[int, int]:
    result: dict[int, int] = defaultdict(int)
    for row in sales:
        nm_id = _as_int(row.get("nmId"))
        if not nm_id:
            continue
        units = _cached_row_units(row)
        result[nm_id] += -units if bool(row.get("isReturn")) else units
    return dict(result)


def _metric_order_rate_by_nm(sources: dict[str, dict[str, Any]]) -> dict[int, float]:
    result: dict[int, float] = {}
    for row in _extract_payload_items(sources.get("stock_product_metrics", {}).get("data")):
        nm_id = _as_int(row.get("nmID") or row.get("nmId"))
        rate = _as_float(
            _stock_metric_value(row, "avgOrders")
            or _stock_metric_value(row, "avgOrdersPerDay")
            or _stock_metric_value(row, "ordersPerDay")
        )
        if nm_id and rate is not None:
            result[nm_id] = rate
    return result


def _stock_product_metrics_by_nm(sources: dict[str, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in _extract_payload_items(sources.get("stock_product_metrics", {}).get("data")):
        nm_id = _as_int(row.get("nmID") or row.get("nmId"))
        if not nm_id:
            continue
        result[nm_id] = row
    return result


def _stock_metric_value(row: dict[str, Any], key: str) -> Any:
    metrics = row.get("metrics")
    if isinstance(metrics, dict) and key in metrics:
        return metrics.get(key)
    return row.get(key)


def _buyer_region_orders_by_nm_region(sources: dict[str, dict[str, Any]]) -> dict[tuple[int, str], int]:
    result: dict[tuple[int, str], int] = defaultdict(int)
    for row in _extract_payload_items(sources.get("buyer_region_sales", {}).get("data")):
        nm_id = _as_int(row.get("nmID") or row.get("nmId"))
        region = _normalize_key(row.get("regionName") or row.get("buyerRegion") or row.get("region"))
        count = _as_int(row.get("ordersCount") or row.get("orders") or row.get("quantity")) or 0
        if nm_id and region:
            result[(nm_id, region)] += count
    return dict(result)


def _is_source_missing(sources: dict[str, dict[str, Any]], source_key: str) -> bool:
    return sources.get(source_key, {}).get("status") == "missing"


def _stock_manual_status(
    sources: dict[str, dict[str, Any]],
    *,
    has_derived_local_orders: bool,
) -> tuple[bool, list[str]]:
    local_orders_exact_missing = _is_source_missing(sources, "local_orders_exact")
    warnings: list[str] = []
    if local_orders_exact_missing and not has_derived_local_orders:
        warnings.append("WB не передал долю локальных заказов")
    return (local_orders_exact_missing and not has_derived_local_orders), warnings


def _stock_row_to_payload(
    row: WbStockRow,
    *,
    orders_by_nm_warehouse: dict[tuple[int, str], int],
    orders_by_nm: dict[int, int],
    local_orders_by_nm_cluster: dict[tuple[int, str], int],
    buyer_region_orders: dict[tuple[int, str], int],
    metric_rates: dict[int, float],
    sales_units: dict[int, int],
    logistics_cost_by_nm_kopecks: dict[int, int],
    days: int,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    warehouse_key = _normalize_key(row.warehouse_name)
    cluster_key = _normalize_key(row.region_name or row.warehouse_name)
    exact_order_count = orders_by_nm_warehouse.get((row.nm_id, warehouse_key), 0)
    nm_order_count = orders_by_nm.get(row.nm_id, 0)
    metric_rate = metric_rates.get(row.nm_id)
    orders_per_day = metric_rate if metric_rate is not None else (exact_order_count / max(days, 1))
    local_orders = local_orders_by_nm_cluster.get((row.nm_id, cluster_key), 0)
    if local_orders == 0:
        local_orders = buyer_region_orders.get((row.nm_id, cluster_key), 0)
    localization_pct = round(min(local_orders / nm_order_count * 100, 100), 1) if nm_order_count > 0 else None
    days_to_oos = round(row.available_units / orders_per_day, 1) if orders_per_day > 0 else None
    sold_units = max(sales_units.get(row.nm_id, 0), 0)
    logistics_total = logistics_cost_by_nm_kopecks.get(row.nm_id, 0)
    logistics_per_unit = round(logistics_total / sold_units) if sold_units > 0 and logistics_total > 0 else None

    _, manual_warnings = _stock_manual_status(
        sources,
        has_derived_local_orders=nm_order_count > 0,
    )
    comment_parts = []
    comment_parts.extend(manual_warnings)
    if logistics_per_unit is None:
        comment_parts.append("логистика/шт не подтверждена по продажам")
    return {
        "sku": f"NM_{row.nm_id}",
        "nmId": row.nm_id,
        "warehouseName": row.warehouse_name or "unknown",
        "clusterName": row.region_name or "unknown",
        "wbStockUnits": row.quantity,
        "fromClientUnits": row.in_way_from_client,
        "toClientUnits": row.in_way_to_client,
        "availableUnits": row.available_units,
        "ordersPerDay": round(orders_per_day, 2),
        "ordersCount": exact_order_count,
        "allOrdersCount": nm_order_count,
        "localOrdersCount": local_orders,
        "daysToOos": days_to_oos,
        "localizationPct": localization_pct,
        "logisticsPerUnitKopecks": logistics_per_unit,
        "comment": "; ".join(comment_parts) if comment_parts else "источники подтверждены",
    }


def _stock_row_to_digest_payload(row: WbStockRow) -> dict[str, Any]:
    return {
        "sku": f"NM_{row.nm_id}",
        "nmId": row.nm_id,
        "warehouseName": row.warehouse_name or "unknown",
        "clusterName": row.region_name or "unknown",
        "wbStockUnits": row.quantity,
        "fromClientUnits": row.in_way_from_client,
        "toClientUnits": row.in_way_to_client,
        "availableUnits": row.available_units,
    }


def _stock_product_rows_from_snapshot(
    *,
    stock_rows: list[WbStockRow],
    orders_by_nm: dict[int, int],
    metric_rates: dict[int, float],
    product_metrics: dict[int, dict[str, Any]],
    sales_units: dict[int, int],
    logistics_cost_by_nm_kopecks: dict[int, int],
    days: int,
    sources: dict[str, dict[str, Any]],
    catalog_meta: dict[int, dict[str, Any]],
    history: dict[int, list[Any]],
    funnel_by_nm: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in stock_rows:
        bucket = grouped.setdefault(
            row.nm_id,
            {
                "quantity": 0,
                "totalStockUnits": None,
                "inWayToClient": 0,
                "inWayFromClient": 0,
                "availableUnits": 0,
                "warehouses": [],
                "regions": set(),
            },
        )
        stock_type = str(getattr(row, "stock_type", "wb") or "wb")
        bucket["quantity"] += row.quantity
        bucket["inWayToClient"] += row.in_way_to_client
        bucket["inWayFromClient"] += row.in_way_from_client
        row_total_stock = _as_int(getattr(row, "total_stock_units", None))
        if row_total_stock is not None:
            bucket["totalStockUnits"] = max(int(bucket["totalStockUnits"] or 0), row_total_stock)
        bucket["availableUnits"] += row.available_units
        if row.region_name:
            bucket["regions"].add(row.region_name)
        bucket["warehouses"].append(
            {
                "warehouseId": row.warehouse_id,
                "warehouseName": row.warehouse_name or "unknown",
                "clusterName": row.region_name or "unknown",
                "stockType": stock_type,
                "wbStockUnits": row.quantity,
                "marketplaceStockUnits": 0,
                "fromClientUnits": row.in_way_from_client,
                "toClientUnits": row.in_way_to_client,
                "availableUnits": row.available_units,
            }
        )

    rows: list[dict[str, Any]] = []
    for nm_id, bucket in grouped.items():
        metric = product_metrics.get(nm_id, {})
        meta = catalog_meta.get(nm_id, {})
        orders_count = _as_int(_stock_metric_value(metric, "ordersCount") or _stock_metric_value(metric, "orders")) or orders_by_nm.get(nm_id, 0)
        orders_per_day = metric_rates.get(nm_id)
        if orders_per_day is None:
            orders_per_day = orders_count / max(days, 1)
        wb_stock_units = int(bucket["quantity"])
        total_stock_units = _as_int(_stock_metric_value(metric, "stockCount"))
        if total_stock_units is None:
            total_stock_units = _as_int(bucket.get("totalStockUnits"))
        if total_stock_units is None:
            total_stock_units = wb_stock_units
        total_stock_units = max(total_stock_units, wb_stock_units)
        marketplace_stock_units = max(total_stock_units - wb_stock_units, 0)
        available_units = int(bucket["availableUnits"])
        days_to_oos = round(available_units / orders_per_day, 1) if orders_per_day > 0 else None
        sold_units = max(sales_units.get(nm_id, 0), 0)
        logistics_total = logistics_cost_by_nm_kopecks.get(nm_id, 0)
        logistics_per_unit = round(logistics_total / sold_units) if sold_units > 0 and logistics_total > 0 else None
        localization = _as_float(funnel_by_nm.get(str(nm_id), {}).get("localizationPct"))
        has_derived_local_orders = localization is not None
        _, manual_warnings = _stock_manual_status(
            sources,
            has_derived_local_orders=has_derived_local_orders,
        )
        comment_parts = []
        comment_parts.extend(manual_warnings)
        if logistics_per_unit is None:
            comment_parts.append("логистика/шт не подтверждена по продажам")
        stock_history = history.get(nm_id, [])
        history_days = {item.snapshotDate for item in stock_history if getattr(item, "snapshotDate", None) is not None}
        warehouses = sorted(bucket["warehouses"], key=lambda item: (-(item["availableUnits"] or 0), str(item["warehouseName"])))
        top_warehouse = warehouses[0] if warehouses else {}
        regions = sorted(str(region) for region in bucket["regions"] if region)
        rows.append(
            {
                "sku": str(meta.get("sku") or f"NM_{nm_id}"),
                "nmId": nm_id,
                "productName": meta.get("productName") or f"WB {nm_id}",
                "photoUrl": meta.get("photoUrl") or _extract_wb_media_url(meta, nm_id),
                "brand": meta.get("brand"),
                "category": meta.get("category"),
                "warehouseName": f"{len(warehouses)} складов" if len(warehouses) != 1 else top_warehouse.get("warehouseName"),
                "clusterName": ", ".join(regions) if regions else None,
                "wbStockUnits": wb_stock_units,
                "marketplaceStockUnits": marketplace_stock_units,
                "totalStockUnits": total_stock_units,
                "fromClientUnits": int(bucket["inWayFromClient"]),
                "toClientUnits": int(bucket["inWayToClient"]),
                "availableUnits": available_units,
                "ordersPerDay": round(float(orders_per_day), 2),
                "ordersCount": orders_count,
                "allOrdersCount": orders_count,
                "localOrdersCount": None,
                "daysToOos": days_to_oos,
                "localizationPct": localization,
                "logisticsPerUnitKopecks": logistics_per_unit,
                "comment": "; ".join(comment_parts) if comment_parts else "источники подтверждены",
                "historyCoverageDays": min(7, len(history_days) or len(stock_history)),
                "historySource": "captured" if stock_history else "missing",
                "warehouseCount": len(warehouses),
                "warehouses": warehouses,
            }
        )
    return sorted(rows, key=lambda row: (-_int_value(row.get("availableUnits")), str(row.get("sku") or "")))


def _build_stock_report_payload(date_range: dict[str, str], snapshot: Any, *, organization_id: int, wb_token: str | None, read_only: bool = False) -> dict[str, Any]:
    snapshot_date = date.fromisoformat(date_range["to"])
    history = ensure_daily_stock_history(snapshot_date, snapshot.stocks, organization_id=organization_id)
    date_from = date.fromisoformat(date_range["from"])
    days = max((snapshot_date - date_from).days + 1, 1)
    sources = _build_stock_source_bundle(
        organization_id=organization_id,
        snapshot=snapshot,
        date_range=date_range,
        wb_token=wb_token,
        read_only=read_only,
    )
    _orders_by_nm_warehouse, orders_by_nm, _local_orders_by_nm_cluster = _orders_index(snapshot.orders, days)
    metric_rates = _metric_order_rate_by_nm(sources)
    product_metrics = _stock_product_metrics_by_nm(sources)
    sales_units = _sales_units_by_nm(snapshot.sales)
    rows = _stock_product_rows_from_snapshot(
        stock_rows=list(snapshot.stocks),
        orders_by_nm=orders_by_nm,
        metric_rates=metric_rates,
        product_metrics=product_metrics,
        sales_units=sales_units,
        logistics_cost_by_nm_kopecks=snapshot.logistics_cost_by_nm_kopecks,
        days=days,
        sources=sources,
        catalog_meta=_catalog_meta_by_nm(organization_id),
        history=history,
        funnel_by_nm=_cache_aggregates(_period_cache(organization_id, "baskets", date_from, snapshot_date)),
    )
    total_available = sum(row["availableUnits"] for row in rows)
    total_wb_stock = sum(row["wbStockUnits"] for row in rows)
    total_marketplace_stock = sum(row.get("marketplaceStockUnits", 0) for row in rows)
    total_stock = sum(row.get("totalStockUnits", row["wbStockUnits"]) for row in rows)
    total_from_client = sum(row["fromClientUnits"] for row in rows)
    total_to_client = sum(row["toClientUnits"] for row in rows)
    oos_risk = sum(1 for row in rows if row["daysToOos"] is not None and row["daysToOos"] < 7 or row["availableUnits"] <= 0)
    partial_sources = [item for item in _source_coverage(sources) if item["status"] not in {"fresh", "cached"}]
    local_orders_total = sum(orders_by_nm.values())
    has_derived_local_orders = local_orders_total > 0 or any(_int_value(row.get("ordersCount")) > 0 for row in rows)
    local_orders_card = {
        "title": "Local orders",
        "value": "derived" if has_derived_local_orders else ("нет exact" if _is_source_missing(sources, "local_orders_exact") else "loaded"),
        "meta": "WB orders/region-sale за выбранный период" if has_derived_local_orders else "seller portal geography orders",
        "className": "" if has_derived_local_orders or not _is_source_missing(sources, "local_orders_exact") else "warn",
        "chip": "Все",
    }
    return {
        "meta": _meta("stock", "Остатки WB", "Текущие остатки, inWay и доступность по складам WB.", "operational", snapshot.source_status),
        "cacheVersion": STOCK_REPORT_PAYLOAD_VERSION,
        "headline": f"Текущие остатки WB: наблюдение {get_source_cache_fetched_at(organization_id, 'stocks') or 'не датировано'}. Выбранные даты относятся к спросу. Остатки за прошлые даты доступны только при наличии сохранённых наблюдений.",
        "filters": {"dateRange": date_range, "groupBy": "sku"},
        "kpis": [
            _kpi("stock_rows", "Товаров", str(len(rows))),
            _kpi("available_units", "Доступно, шт", str(total_available)),
            _kpi("total_stock_units", "Итого остатков, шт", str(total_stock)),
            _kpi("wb_stock_units", "Остаток WB, шт", str(total_wb_stock)),
            _kpi("marketplace_stock_units", "Маркетплейс, шт", str(total_marketplace_stock)),
            _kpi("from_client_units", "От клиента, шт", str(total_from_client)),
            _kpi("to_client_units", "К клиенту, шт", str(total_to_client)),
            _kpi("oos_risk", "Риск OOS", str(oos_risk)),
            _kpi("history_days", "История, дней", str(max((row.get("historyCoverageDays", 0) for row in rows), default=0))),
        ],
        "chart": {
            "title": "Доступность по складам",
            "valueLabel": "Доступно, шт",
            "points": [{"label": row["warehouseName"], "value": row["availableUnits"]} for row in rows[:20]],
        },
        "columns": [
            {"key": "sku", "label": "Артикул"},
            {"key": "nmId", "label": "WB"},
            {"key": "productName", "label": "Товар"},
            {"key": "brand", "label": "Бренд"},
            {"key": "category", "label": "Категория"},
            {"key": "warehouseName", "label": "Склад WB"},
            {"key": "clusterName", "label": "Кластер"},
            {"key": "totalStockUnits", "label": "Итого остатков"},
            {"key": "wbStockUnits", "label": "Остаток WB"},
            {"key": "fromClientUnits", "label": "От клиента"},
            {"key": "toClientUnits", "label": "К клиенту"},
            {"key": "availableUnits", "label": "Доступно"},
            {"key": "ordersPerDay", "label": "Заказы/день"},
            {"key": "daysToOos", "label": "Дней до OOS"},
            {"key": "localizationPct", "label": "Локализация"},
            {"key": "logisticsPerUnitKopecks", "label": "Логистика/шт"},
            {"key": "comment", "label": "Комментарий"},
            {"key": "historySource", "label": "Источник истории"},
        ],
        "rows": rows,
        "sourceCoverage": _source_coverage(sources),
        "managementCards": [
            {"title": "OOS risk", "value": f"{oos_risk} SKU", "meta": "меньше 7 дней покрытия или нулевой доступный остаток", "className": "danger" if oos_risk else "", "chip": "OOS риск"},
            {"title": "Partial sources", "value": str(len(partial_sources)), "meta": "источники без fresh/cache покрытия", "className": "warn" if partial_sources else "", "chip": "Все"},
            local_orders_card,
        ],
    }


def _week_pnl_values(organization_id: int, start: date, end: date) -> dict[int, dict[str, Any]]:
    # Use the existing dated P&L calculation, without new WB requests.
    try:
        report = build_pnl_report(date_from=start, date_to=end, group_by="sku",
            requested_state="preliminary", finance_allowed=True,
            organization_id=organization_id, wb_token=None)
    except ValueError as exc:
        if str(exc) != "WB_FINANCE_UNALLOCATED_PAYABLE":
            raise
        # Week can still display the direct ledger and separate unallocated
        # compensation. An unavailable profit/tax calculation is not zero.
        return {}
    return {_int_value(row.nmId): {"profit": row.netProfitKopecks, "margin": row.marginPct,
            "taxKopecks": row.taxKopecks}
            for row in report.rows if getattr(row, "nmId", None)}


def _week_financial_columns(report, organization_id, start, end, finance_allowed, *, current_pnl=None, previous_pnl=None, include_previous=True):
    from app.wb_week_finance import attach
    from app.wb_week_sources import attach as attach_operational
    previous_start, previous_end = _previous_period(start, end)
    if finance_allowed:
        if current_pnl is None:
            current_pnl = _week_pnl_values(organization_id, start, end)
        if previous_pnl is None and include_previous:
            previous_pnl = _week_pnl_values(organization_id, previous_start, previous_end)
    catalog = _catalog_meta_by_nm(organization_id)
    report = attach(report, organization_id, start, end, previous_start, previous_end,
        finance_allowed=finance_allowed, current_pnl=current_pnl, previous_pnl=previous_pnl,
        catalog=catalog, include_previous=include_previous)
    report = attach_operational(report, organization_id, start, end, previous_start, previous_end,
        finance_allowed=finance_allowed, catalog=catalog, include_previous=include_previous)
    # Both attachments can introduce products (including fee-only products).
    # Reconcile the final union with the SAME dated P&L used above, not a new
    # formula or values preserved from an earlier financial revision.
    for row in report.get("rows", []):
        nm = _int_value(row.get("nmId"))
        if not nm or row.get("isUnallocated"):
            continue
        now = (current_pnl or {}).get(nm, {}) if finance_allowed else {}
        before = (previous_pnl or {}).get(nm, {}) if finance_allowed and include_previous else {}
        row["profit"] = {"kopecks": now.get("profit"), "previousKopecks": before.get("profit"),
            "deltaPct": _delta_pct(now.get("profit"), before.get("profit"))}
        row["marginPct"] = {"percent": now.get("margin"), "previousPercent": before.get("margin"),
            "deltaPct": _delta_pct(now.get("margin"), before.get("margin"))}
    for point, row in zip(report.get("chart", {}).get("points", []), report.get("rows", [])):
        for field in ("profit", "marginPct"):
            point[field] = deepcopy(row.get(field))
    return report


def _build_week_over_week_payload(
    date_range: dict[str, str],
    snapshot: Any,
    previous_snapshot: Any | None = None,
    ads_snapshot: Any | None = None,
    previous_ads_snapshot: Any | None = None,
    organization_id: int | None = None,
    finance_allowed: bool = False,
    include_previous: bool = True,
) -> dict[str, Any]:
    base = _orders_sales_by_nm(snapshot)
    previous = _orders_sales_by_nm(previous_snapshot) if previous_snapshot is not None else {}
    ads_by_nm = _ads_by_nm(ads_snapshot)
    previous_ads_by_nm = _ads_by_nm(previous_ads_snapshot)
    profit_by_nm = _profit_by_nm(snapshot, ads_by_nm)
    previous_profit_by_nm = _profit_by_nm(previous_snapshot, previous_ads_by_nm) if previous_snapshot is not None else {}
    catalog_meta = _catalog_meta_by_nm(organization_id)
    current_from, snapshot_date = date.fromisoformat(date_range["from"]), date.fromisoformat(date_range["to"])
    previous_to = current_from - timedelta(days=1)
    previous_from = previous_to - (snapshot_date - current_from)
    if organization_id is not None and finance_allowed:
        # Reuse the same dated, source-backed P&L calculation; never derive
        # profit from raw sales or substitute missing costs/tax with zero.
        profit_by_nm = _week_pnl_values(organization_id, current_from, snapshot_date)
        previous_profit_by_nm = _week_pnl_values(organization_id, previous_from, previous_to) if include_previous else {}
    current_funnel = _week_funnel_metrics_by_nm(organization_id=organization_id, date_from=current_from, date_to=snapshot_date, wb_token=None) if organization_id else {}
    previous_funnel = _week_funnel_metrics_by_nm(organization_id=organization_id, date_from=previous_from, date_to=previous_to, wb_token=None) if organization_id and include_previous else {}
    rows: list[dict[str, Any]] = []
    history = ensure_daily_stock_history(snapshot_date, snapshot.stocks, organization_id=organization_id)
    for nm_id, agg in base.items():
        previous_agg = previous.get(nm_id, {})
        profit_agg = profit_by_nm.get(nm_id, {})
        previous_profit_agg = previous_profit_by_nm.get(nm_id, {})
        funnel = current_funnel.get(str(nm_id), {})
        prior_funnel = previous_funnel.get(str(nm_id), {})
        orders, orders_amount = funnel.get("orderCount"), funnel.get("orderSumKopecks")
        prior_orders, prior_amount = prior_funnel.get("orderCount"), prior_funnel.get("orderSumKopecks")
        current_price = round(orders_amount / orders) if orders and orders_amount is not None else None
        previous_price = round(prior_amount / prior_orders) if prior_orders and prior_amount is not None else None
        stock_history = history.get(nm_id, [])
        availability_7d = [not item.wasOutOfStock for item in stock_history]
        was_oos = any(not value for value in availability_7d) if availability_7d else None
        history_source = "captured" if len(stock_history) == 7 else "partial" if stock_history else "missing"
        meta = catalog_meta.get(nm_id, {})
        sku = str(meta.get("sku") or f"NM_{nm_id}")
        rows.append(
            {
                "sku": sku,
                "nmId": nm_id,
                "productName": meta.get("productName"),
                "photoUrl": meta.get("photoUrl") or _extract_wb_media_url(meta, nm_id),
                "brand": meta.get("brand"),
                "category": meta.get("category"),
                "productStatus": meta.get("productStatus"),
                "abcCode": None,
                "orders": {"units": orders, "kopecks": orders_amount, "deltaPct": _delta_pct(orders, prior_orders)},
                "sales": {"units": agg["sales_qty"], "kopecks": agg["sales_revenue"], "deltaPct": _delta_pct(agg["sales_revenue"], previous_agg.get("sales_revenue"))},
                "baskets": {"units": funnel.get("cartCount"), "kopecks": None, "deltaPct": _delta_pct(funnel.get("cartCount"), prior_funnel.get("cartCount"))},
                "marginPct": {"percent": profit_agg.get("margin"), "deltaPct": _delta_pct(profit_agg.get("margin"), previous_profit_agg.get("margin"))},
                "profit": {"kopecks": profit_agg.get("profit"), "deltaPct": _delta_pct(profit_agg.get("profit"), previous_profit_agg.get("profit"))},
                "price": {"kopecks": current_price, "deltaPct": _delta_pct(current_price, previous_price)},
                "wasOutOfStock": was_oos,
                "stockAvailability7d": availability_7d,
                "stockOutDays": sum(1 for available in availability_7d if not available) if availability_7d else None,
                "stockSnapshotCoveragePct": round(len(stock_history) / 7 * 100),
                "historySource": history_source,
                "conclusion": f"Сохранены остатки за {len(stock_history)} из 7 дней. Прошлые остатки WB не предоставлены." if history_source != "captured" else "Остатки подтверждены ежедневными снимками WB.",
            }
        )
    payload = {
        "meta": _meta("week-over-week", "Неделя к неделе", "Сравнение заказов, продаж и OOS-контекста.", "operational", snapshot.source_status),
        "cacheVersion": WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
        "headline": "Сравниваем текущую неделю с прошлой по продажам, заказам, марже и рискам по остаткам.",
        "filters": {"dateRange": date_range, "groupBy": "sku"},
        "kpis": [
            _kpi("sku_rows", "SKU в отчете", str(len(rows))),
            _kpi("oos_count", "SKU с OOS", str(sum(1 for row in rows if row["wasOutOfStock"]))),
            _kpi("history_days", "История, дней", str(max((len(row.get("stockAvailability7d") or []) for row in rows), default=0))),
        ],
        "chart": {
            "title": "WoW: цены, маржа, прибыль, продажи, заказы, корзины",
            "valueLabel": "Текущее значение",
            "compareLabel": "Динамика, %",
            "series": ["price", "marginPct", "profit", "sales", "orders", "baskets"],
            "points": [
                {
                    "label": row["sku"],
                    "value": row["orders"]["units"],
                    "compareValue": row["orders"]["deltaPct"],
                    "sales": row["sales"],
                    "baskets": row["baskets"],
                    "marginPct": row["marginPct"],
                    "profit": row["profit"],
                    "price": row["price"],
                }
                for row in rows[:20]
            ],
        },
        "columns": [
            {"key": "sku", "label": "Артикул"},
            {"key": "abcCode", "label": "ABC"},
            {"key": "productStatus", "label": "Статус"},
            {"key": "price", "label": "Цена"},
            {"key": "orders", "label": "Заказы"},
            {"key": "sales", "label": "Продажи"},
            {"key": "wasOutOfStock", "label": "Был ОС"},
            {"key": "stockAvailability7d", "label": "Наличие 7 дней"},
            {"key": "historySource", "label": "Источник истории"},
            {"key": "conclusion", "label": "Вывод"},
        ],
        "rows": rows,
    }
    return _week_financial_columns(payload, organization_id, current_from, snapshot_date, finance_allowed,
        current_pnl=profit_by_nm, previous_pnl=previous_profit_by_nm, include_previous=include_previous) if organization_id else payload


def _composite_units(row: dict[str, Any], composite_key: str, fallback_key: str) -> int:
    composite = row.get(composite_key) if isinstance(row.get(composite_key), dict) else {}
    return _int_value(composite.get("units") or row.get(fallback_key))


def _composite_kopecks(row: dict[str, Any], composite_key: str, fallback_key: str) -> int:
    composite = row.get(composite_key) if isinstance(row.get(composite_key), dict) else {}
    return _int_value(composite.get("kopecks") or row.get(fallback_key))


def _abc_rows_have_period_activity(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _composite_units(row, "ordersComposite", "ordersUnits") > 0:
            return True
        if _composite_kopecks(row, "ordersComposite", "ordersKopecks") > 0:
            return True
        if _composite_units(row, "salesComposite", "salesUnits") > 0:
            return True
        if _composite_kopecks(row, "salesComposite", "salesKopecks") > 0:
            return True
        if _int_value(row.get("netTotalKopecks") or row.get("profitKopecks")) != 0:
            return True
        if _int_value(row.get("baskets")) > 0:
            return True
    return False


def _abc_rows_have_financial_activity(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _composite_units(row, "ordersComposite", "ordersUnits") > 0:
            return True
        if _composite_kopecks(row, "ordersComposite", "ordersKopecks") > 0:
            return True
        if _composite_units(row, "salesComposite", "salesUnits") > 0:
            return True
        if _composite_kopecks(row, "salesComposite", "salesKopecks") > 0:
            return True
        if _int_value(row.get("netTotalKopecks") or row.get("profitKopecks")) != 0:
            return True
        if _int_value(row.get("adSpendKopecks")) > 0:
            return True
    return False


def _repricer_rows_to_abc_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        mapped
        for mapped in (_repricer_row_to_abc_row(row) for row in rows if isinstance(row, dict))
        if mapped.get("sku")
    ]


def _week_report_has_period_activity(report: dict[str, Any]) -> bool:
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        orders = row.get("orders") if isinstance(row.get("orders"), dict) else {}
        sales = row.get("sales") if isinstance(row.get("sales"), dict) else {}
        baskets = row.get("baskets") if isinstance(row.get("baskets"), dict) else {}
        profit = row.get("profit") if isinstance(row.get("profit"), dict) else {}
        if _int_value(orders.get("units") or orders.get("kopecks")) > 0:
            return True
        if _int_value(sales.get("units") or sales.get("kopecks")) > 0:
            return True
        if _int_value(baskets.get("units") or baskets.get("kopecks")) > 0:
            return True
        if _int_value(profit.get("kopecks")) != 0:
            return True
        if any(_int_value((row.get(field) or {}).get("kopecks")) != 0 for field in (
                "commissionKopecks", "logisticsKopecks", "storageKopecks", "adSpendKopecks",
                "acceptanceKopecks", "penaltyKopecks", "compensationKopecks", "taxKopecks")):
            return True
    return False


def _week_row_key(row: dict[str, Any]) -> str:
    return str(row.get("nmId") or row.get("sku") or row.get("label") or "").strip()


def _abc_row_profit(row: dict[str, Any]) -> int | None:
    if row.get("factTaxState") == "missing":
        return None
    for key in ("netTotalKopecks", "profitKopecks"):
        if key in row:
            return _int_value(row[key]) if row[key] is not None else None
    return None


def _week_rows_from_abc_rows(
    rows: list[dict[str, Any]], previous_rows: list[dict[str, Any]] | None = None,
    *, organization_id: int | None = None, snapshot_date: date | None = None,
) -> list[dict[str, Any]]:
    previous_by_key = {_week_row_key(row): row for row in previous_rows or [] if _week_row_key(row)}
    history = ensure_daily_stock_history(snapshot_date, [_int_value(row.get("nmId")) for row in rows], organization_id=organization_id) if organization_id and snapshot_date else {}
    result: list[dict[str, Any]] = []
    for row in rows:
        key = _week_row_key(row)
        previous = previous_by_key.get(key, {})
        orders_units = _composite_units(row, "ordersComposite", "ordersUnits")
        orders_kopecks = _composite_kopecks(row, "ordersComposite", "ordersKopecks")
        sales_units = _composite_units(row, "salesComposite", "salesUnits")
        sales_kopecks = _composite_kopecks(row, "salesComposite", "salesKopecks")
        previous_orders_units = _composite_units(previous, "ordersComposite", "ordersUnits") if previous else None
        previous_orders_kopecks = _composite_kopecks(previous, "ordersComposite", "ordersKopecks") if previous else None
        previous_sales_kopecks = _composite_kopecks(previous, "salesComposite", "salesKopecks") if previous else None
        baskets = _int_value(row.get("baskets"))
        previous_baskets = _int_value(previous.get("baskets")) if previous else None
        margin = _float_value(row.get("marginPct"))
        previous_margin = _float_value(previous.get("marginPct")) if previous else None
        profit = _abc_row_profit(row)
        previous_profit = _abc_row_profit(previous) if previous else None
        price = round(orders_kopecks / orders_units) if orders_units > 0 and orders_kopecks > 0 else None
        previous_price = round(previous_orders_kopecks / previous_orders_units) if previous_orders_units and previous_orders_kopecks else None
        availability = [not item.wasOutOfStock for item in history.get(_int_value(row.get("nmId")), [])]
        result.append(
            {
                "sku": str(row.get("sku") or row.get("label") or f"NM_{row.get('nmId') or 'unknown'}"),
                "nmId": row.get("nmId"),
                "productName": row.get("productName") or row.get("name"),
                "photoUrl": row.get("photoUrl"),
                "brand": row.get("brand"),
                "category": row.get("category"),
                "productStatus": row.get("productStatus") or "средний",
                "abcCode": row.get("abcCode") or "CC",
                "orders": {"units": orders_units, "kopecks": orders_kopecks, "deltaPct": _delta_pct(orders_units, previous_orders_units)},
                "sales": {"units": sales_units, "kopecks": sales_kopecks, "deltaPct": _delta_pct(sales_kopecks, previous_sales_kopecks)},
                "baskets": {"units": baskets, "kopecks": None, "deltaPct": _delta_pct(baskets, previous_baskets)},
                "marginPct": {"percent": margin, "deltaPct": _delta_pct(margin, previous_margin)},
                "profit": {"kopecks": profit, "deltaPct": _delta_pct(profit, previous_profit)},
                "factTaxState": row.get("factTaxState"),
                "factTaxReason": row.get("factTaxReason"),
                "previousFactTaxReason": previous.get("factTaxReason"),
                "price": {"kopecks": price, "deltaPct": _delta_pct(price, previous_price)},
                "wasOutOfStock": any(value is False for value in availability) if availability else None,
                "stockAvailability7d": availability,
                "stockOutDays": sum(1 for available in availability if available is False) if availability else None,
                "stockSnapshotCoveragePct": round(len(availability) / 7 * 100),
                "historySource": "captured" if len(availability) == 7 else "partial" if availability else "missing",
                "conclusion": f"Сохранены остатки за {len(availability)} из 7 дней. Прошлые остатки WB не предоставлены." if len(availability) < 7 else "Остатки подтверждены ежедневными снимками WB.",
            }
        )
    return result


def _week_over_week_shell(date_range: dict[str, str], rows: list[dict[str, Any]], *, source_status: str = "partial") -> dict[str, Any]:
    return {
        "meta": _meta("week-over-week", "Неделя к неделе", "Сравнение заказов, продаж и OOS-контекста.", "operational", source_status),
        "cacheVersion": WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
        "headline": "Сравниваем текущую неделю с прошлой по продажам, заказам, марже и рискам по остаткам.",
        "filters": {"dateRange": date_range, "groupBy": "sku"},
        "kpis": [
            _kpi("sku_rows", "SKU в отчете", str(len(rows))),
            _kpi("oos_count", "SKU с OOS", str(sum(1 for row in rows if row.get("wasOutOfStock")))),
            _kpi("history_days", "История, дней", str(max((len(row.get("stockAvailability7d") or []) for row in rows), default=0))),
        ],
        "chart": {
            "title": "WoW: цены, маржа, прибыль, продажи, заказы, корзины",
            "valueLabel": "Текущее значение",
            "compareLabel": "Динамика, %",
            "series": ["price", "marginPct", "profit", "sales", "orders", "baskets"],
            "points": [
                {
                    "label": row["sku"],
                    "value": row["orders"]["units"],
                    "compareValue": row["orders"]["deltaPct"],
                    "sales": row["sales"],
                    "baskets": row["baskets"],
                    "marginPct": row["marginPct"],
                    "profit": row["profit"],
                    "price": row["price"],
                }
                for row in rows[:20]
            ],
        },
        "columns": [
            {"key": "sku", "label": "Артикул"},
            {"key": "abcCode", "label": "ABC"},
            {"key": "productStatus", "label": "Статус"},
            {"key": "price", "label": "Цена"},
            {"key": "orders", "label": "Заказы"},
            {"key": "sales", "label": "Продажи"},
            {"key": "wasOutOfStock", "label": "Был ОС"},
            {"key": "stockAvailability7d", "label": "Наличие 7 дней"},
            {"key": "historySource", "label": "Источник истории"},
            {"key": "conclusion", "label": "Вывод"},
        ],
        "rows": rows,
    }


def _abc_payload_rows(payload: Any) -> list[dict[str, Any]]:
    return [row for row in getattr(payload, "rows", []) or [] if isinstance(row, dict)]


def _week_period_stats_aggregates(organization_id: int, date_from: date, date_to: date) -> dict[str, dict[str, Any]]:
    try:
        return _cache_aggregates(_period_cache(organization_id, "period_stats", date_from, date_to))
    except Exception:
        return {}


def _week_rows_with_period_stats(rows: list[dict[str, Any]], period_stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows or not period_stats:
        return rows
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        nm_key = str(row.get("nmId") or "").strip()
        stats = period_stats.get(nm_key) if nm_key else None
        if not isinstance(stats, dict):
            enriched_rows.append(row)
            continue

        enriched = dict(row)
        changed = False
        period_orders = _int_value(stats.get("ordersUnits"))
        period_sales = _int_value(stats.get("salesUnits"))
        period_revenue = _int_value(stats.get("revenueKopecks"))

        orders_units = _composite_units(enriched, "ordersComposite", "ordersUnits")
        orders_kopecks = _composite_kopecks(enriched, "ordersComposite", "ordersKopecks")
        if period_orders > 0 and orders_units <= 0:
            orders = dict(enriched.get("ordersComposite") or {})
            orders["units"] = period_orders
            orders["kopecks"] = orders_kopecks or period_revenue
            enriched["ordersUnits"] = period_orders
            enriched["ordersKopecks"] = orders["kopecks"]
            enriched["ordersComposite"] = orders
            changed = True

        sales_units = _composite_units(enriched, "salesComposite", "salesUnits")
        sales_kopecks = _composite_kopecks(enriched, "salesComposite", "salesKopecks")
        if period_sales > 0 and sales_units <= 0:
            sales = dict(enriched.get("salesComposite") or {})
            sales["units"] = period_sales
            sales["kopecks"] = sales_kopecks or period_revenue
            enriched["salesUnits"] = period_sales
            enriched["salesKopecks"] = sales["kopecks"]
            enriched["salesComposite"] = sales
            sales_kopecks = _int_value(sales["kopecks"])
            changed = True
        elif period_revenue > 0 and sales_kopecks <= 0:
            sales = dict(enriched.get("salesComposite") or {})
            sales["units"] = sales_units
            sales["kopecks"] = period_revenue
            enriched["salesKopecks"] = period_revenue
            enriched["salesComposite"] = sales
            sales_kopecks = period_revenue
            changed = True

        profit = _int_value(enriched.get("netTotalKopecks") or enriched.get("profitKopecks"))
        margin = _float_value(enriched.get("marginPct"))
        if not enriched.get("factTaxState") and "netTotalKopecks" not in enriched and "profitKopecks" not in enriched and profit == 0 and margin is not None and margin != 0 and sales_kopecks > 0:
            enriched["netTotalKopecks"] = int(round(sales_kopecks * margin / 100))
            enriched["profitSource"] = "margin_estimate"
            changed = True

        if changed:
            enriched["historySource"] = "repricer_period_stats"
            enriched["conclusion"] = "WoW row uses repricer identity plus cached period stats for orders, sales, and margin-derived profit."
        enriched_rows.append(enriched)
    return enriched_rows


def _week_funnel_metrics_by_nm(
    *,
    organization_id: int,
    date_from: date,
    date_to: date,
    wb_token: str | None,
    progress_callback: Any | None = None,
) -> dict[str, dict[str, int | float | None]]:
    rows = _cached_funnel_rows_from_baskets_cache(_period_cache(organization_id, "baskets", date_from, date_to))
    metrics: dict[str, dict[str, int | float | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        nm_id = row.get("nmId")
        if nm_id is None:
            continue
        key = str(nm_id)
        bucket = metrics.setdefault(
            key,
            {
                "cartCount": 0,
                "orderCount": 0,
                "orderSumKopecks": 0,
                "buyoutCount": 0,
                "buyoutSumKopecks": 0,
                "cancelCount": 0,
                "buyoutPct": None,
            },
        )
        for field in ("cartCount", "orderCount", "orderSumKopecks", "buyoutCount", "buyoutSumKopecks", "cancelCount"):
            bucket[field] = _int_value(bucket.get(field)) + _int_value(row.get(field))
    for bucket in metrics.values():
        order_count = _int_value(bucket.get("orderCount"))
        buyout_count = _int_value(bucket.get("buyoutCount"))
        settled_count = buyout_count + _int_value(bucket.get("cancelCount"))
        bucket["buyoutPct"] = round(buyout_count / settled_count * 100, 2) if settled_count > 0 else None
    return metrics


def _week_rows_with_funnel_metrics(rows: list[dict[str, Any]], funnel_metrics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows or not funnel_metrics:
        return rows
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        nm_key = str(row.get("nmId") or "").strip()
        metrics = funnel_metrics.get(nm_key) if nm_key else None
        if not isinstance(metrics, dict):
            enriched_rows.append(row)
            continue
        enriched = dict(row)
        cart_count = _int_value(metrics.get("cartCount"))
        order_count = _int_value(metrics.get("orderCount"))
        order_sum = _int_value(metrics.get("orderSumKopecks"))
        buyout_count = _int_value(metrics.get("buyoutCount"))
        buyout_sum = _int_value(metrics.get("buyoutSumKopecks"))
        if metrics.get("cartCount") is not None:
            enriched["baskets"] = cart_count
            enriched["cartCount"] = cart_count
        if metrics.get("orderCount") is not None and metrics.get("orderSumKopecks") is not None:
            enriched["ordersUnits"] = order_count
            enriched["ordersKopecks"] = order_sum
            enriched["ordersComposite"] = {"units": order_count, "kopecks": order_sum}
        if metrics.get("buyoutCount") is not None and metrics.get("buyoutSumKopecks") is not None:
            enriched["salesUnits"] = buyout_count
            enriched["salesKopecks"] = buyout_sum
            enriched["salesComposite"] = {"units": buyout_count, "kopecks": buyout_sum}
        if metrics.get("buyoutPct") is not None:
            enriched["buyoutPct"] = metrics.get("buyoutPct")
        enriched["historySource"] = "wb_sales_funnel"
        enriched["conclusion"] = "Заказы, выкупы и корзины взяты из WB Sales Funnel за текущий и предыдущий периоды."
        enriched_rows.append(enriched)
    return enriched_rows


def _build_week_over_week_abc_report(
    *,
    request: Request,
    actor: Any,
    date_from: date,
    date_to: date,
    date_range: dict[str, str],
    group_by: ReportGroupBy,
    finance_allowed: bool,
) -> dict[str, Any]:
    previous_from, previous_to = _previous_period(date_from, date_to)
    diagnostics: dict[str, Any] = {
        "mode": "abc_direct",
        "reportId": "week-over-week",
        "requestedRange": date_range,
        "previousRange": {"from": previous_from.isoformat(), "to": previous_to.isoformat()},
        "requests": [],
    }

    def add_step(stage: str, endpoint: str, start: date, end: date, rows: int, source_status: str | None = None) -> None:
        step: dict[str, Any] = {
            "stage": stage,
            "endpoint": endpoint,
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
            "status": "ok",
            "rows": rows,
        }
        if source_status:
            step["sourceStatus"] = source_status
        diagnostics["requests"].append(step)

    current_payload = build_abc_report(
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        filters="",
        finance_allowed=finance_allowed,
        organization_id=actor.organization_id,
    )
    previous_payload = build_abc_report(
        date_from=previous_from,
        date_to=previous_to,
        group_by=group_by,
        filters="",
        finance_allowed=finance_allowed,
        organization_id=actor.organization_id,
    )
    current_rows = _abc_payload_rows(current_payload)
    previous_rows = _abc_payload_rows(previous_payload)
    add_step("current_abc", "build_abc_report", date_from, date_to, len(current_rows), getattr(current_payload, "sourceStatus", None))
    add_step("previous_abc", "build_abc_report", previous_from, previous_to, len(previous_rows), getattr(previous_payload, "sourceStatus", None))

    used_repricer_fallback = False
    if not current_rows or not previous_rows:
        wb_token = _actor_wb_token(actor)
        if not current_rows:
            repricer_rows = _repricer_rows_for_abc_report(
                request=request,
                actor=actor,
                wb_token=wb_token,
                date_from=date_from,
                date_to=date_to,
            )
            current_rows = _repricer_rows_to_abc_rows(repricer_rows)
            used_repricer_fallback = bool(current_rows)
            add_step("current_repricer", "_repricer_rows_for_abc_report", date_from, date_to, len(current_rows), "fresh" if current_rows else "empty")
        if not previous_rows:
            repricer_rows = _repricer_rows_for_abc_report(
                request=request,
                actor=actor,
                wb_token=wb_token,
                date_from=previous_from,
                date_to=previous_to,
            )
            previous_rows = _repricer_rows_to_abc_rows(repricer_rows)
            used_repricer_fallback = used_repricer_fallback or bool(previous_rows)
            add_step("previous_repricer", "_repricer_rows_for_abc_report", previous_from, previous_to, len(previous_rows), "fresh" if previous_rows else "empty")

    current_period_stats = _week_period_stats_aggregates(actor.organization_id, date_from, date_to)
    previous_period_stats = _week_period_stats_aggregates(actor.organization_id, previous_from, previous_to)
    current_rows = _week_rows_with_period_stats(current_rows, current_period_stats)
    previous_rows = _week_rows_with_period_stats(previous_rows, previous_period_stats)
    add_step("current_period_stats", "period_stats_cache", date_from, date_to, len(current_period_stats), "cached" if current_period_stats else "empty")
    add_step("previous_period_stats", "period_stats_cache", previous_from, previous_to, len(previous_period_stats), "cached" if previous_period_stats else "empty")

    source_status = "fresh" if used_repricer_fallback and current_rows else str(getattr(current_payload, "sourceStatus", None) or "partial")
    rows = _week_rows_from_abc_rows(current_rows, previous_rows, organization_id=actor.organization_id, snapshot_date=date_to)
    payload = _week_over_week_shell(date_range, rows, source_status=source_status)
    payload = _week_financial_columns(payload, actor.organization_id, date_from, date_to, finance_allowed)
    payload["headline"] = "WoW-отчёт построен прямым запросом из того же источника, что ABC, без ожидания фоновой сборки."
    payload["cache"] = {
        "status": "live-abc-fallback" if used_repricer_fallback else "live-abc",
        "requestedRange": date_range,
        "previousRange": {"from": previous_from.isoformat(), "to": previous_to.isoformat()},
    }
    payload["diagnostics"] = diagnostics
    payload["sourceStatus"] = source_status
    payload["confidence"] = "medium" if used_repricer_fallback else getattr(current_payload, "confidence", None)
    payload["reportJob"] = {
        "state": "completed",
        "reportId": "week-over-week",
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "groupBy": group_by,
        "stage": "abc_direct",
        "label": "WoW построен прямым GET по ABC-источнику",
        "percent": 100,
        "finishedAt": _utc_now_iso(),
    }
    return payload


@tax_consistent_report
def _build_week_over_week_fallback_report(
    *,
    request: Request,
    actor: Any,
    date_from: date,
    date_to: date,
    date_range: dict[str, str],
    finance_allowed: bool,
    wb_token: str | None,
    include_previous: bool = True,
) -> dict[str, Any]:
    from types import SimpleNamespace
    previous_from, previous_to = _previous_period(date_from, date_to)
    diagnostics: dict[str, Any] = {
        "mode": "live",
        "reportId": "week-over-week",
        "requestedRange": date_range,
        "requests": [],
    }

    def add_step(stage: str, endpoint: str, start: date, end: date) -> dict[str, Any]:
        step = {
            "stage": stage,
            "endpoint": endpoint,
            "dateFrom": start.isoformat(),
            "dateTo": end.isoformat(),
            "status": "started",
        }
        diagnostics["requests"].append(step)
        return step

    def finish_snapshot_step(step: dict[str, Any], snapshot: Any) -> None:
        step["status"] = "ok"
        step["sourceStatus"] = getattr(snapshot, "source_status", None)
        step["rows"] = {
            "orders": len(getattr(snapshot, "orders", []) or []),
            "sales": len(getattr(snapshot, "sales", []) or []),
            "stocks": len(getattr(snapshot, "stocks", []) or []),
        }

    def finish_ads_step(step: dict[str, Any], snapshot: Any) -> None:
        step["status"] = "ok"
        step["rows"] = {"ads": len(getattr(snapshot, "rows", []) or [])}

    current_step = add_step("current_sources_cache", "repricer_source_cache", date_from, date_to)
    current_snapshot = build_cached_wb_reports_sources_snapshot(organization_id=actor.organization_id, date_from=date_from, date_to=date_to)
    finish_snapshot_step(current_step, current_snapshot)
    previous_step = add_step("previous_sources_cache", "repricer_source_cache", previous_from, previous_to)
    previous_snapshot = (
        build_cached_wb_reports_sources_snapshot(organization_id=actor.organization_id, date_from=previous_from, date_to=previous_to)
        if include_previous else SimpleNamespace(orders=[], sales=[], stocks=[], source_status="pending")
    )
    finish_snapshot_step(previous_step, previous_snapshot)
    current_ads_step = add_step("current_ads_cache", "repricer_ads_cache", date_from, date_to)
    current_ads = build_cached_ads_attribution_snapshot(organization_id=actor.organization_id, date_from=date_from, date_to=date_to, group_by="sku")
    finish_ads_step(current_ads_step, current_ads)
    previous_ads_step = add_step("previous_ads_cache", "repricer_ads_cache", previous_from, previous_to)
    previous_ads = build_cached_ads_attribution_snapshot(organization_id=actor.organization_id, date_from=previous_from, date_to=previous_to, group_by="sku") if include_previous else None
    finish_ads_step(previous_ads_step, previous_ads)
    payload = _build_week_over_week_payload(
        date_range,
        current_snapshot,
        previous_snapshot,
        current_ads,
        previous_ads,
        organization_id=actor.organization_id,
        finance_allowed=finance_allowed,
        include_previous=include_previous,
    )
    missing = {label: (["comparison-pending"] if label == "previous" and not include_previous else _report_daily_sources_ready(actor.organization_id, ("period-stats", "finance", "ads", "baskets-summary"),
        date_from=start, date_to=end, report_id="week-over-week")[1]
        )
        for label, start, end in (("selected", date_from, date_to), ("previous", previous_from, previous_to))}
    payload["periodCoverage"] = missing
    payload["previousRange"] = {"from": previous_from.isoformat(), "to": previous_to.isoformat()}
    if any(missing.values()):
        payload["meta"]["freshnessState"] = "partial"
        payload["warning"] = " ".join(filter(None, [payload.get("warning"), "Сравнение неполное: " + "; ".join(f"{label}: {', '.join(sources)}" for label, sources in missing.items() if sources)]))
    payload["headline"] = f"Период {date_from} — {date_to}; сравнение {previous_from} — {previous_to}." + (
        " Неполные источники не подтверждают итоговые значения и динамику." if any(missing.values()) else "")
    payload["cache"] = {"status": "cache-only", "requestedRange": date_range}
    payload["diagnostics"] = diagnostics
    payload["reportJob"] = {
        "state": "completed",
        "reportId": "week-over-week",
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "groupBy": "sku",
        "stage": "cache_only",
        "label": "WoW построен из repricer source cache для выбранного диапазона",
        "percent": 100,
        "finishedAt": _utc_now_iso(),
    }
    return payload


def _estimated_export_rows(
    report_id: ReportId,
    date_from: date,
    date_to: date,
    finance_allowed: bool,
    wb_token: str | None = None,
    organization_id: int | None = None,
) -> int:
    if report_id == "digest":
        return 1
    if report_id == "stock":
        if organization_id is None:
            return 0
        source_snapshot = build_cached_wb_reports_sources_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to)
        return len(source_snapshot.stocks)
    if report_id == "week-over-week":
        if organization_id is None:
            return 0
        source_snapshot = build_cached_wb_reports_sources_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to)
        return len(_orders_sales_by_nm(source_snapshot))
    if report_id == "ads":
        if organization_id is None:
            return 0
        ads_snapshot = build_cached_ads_attribution_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to, group_by="campaign")
        return len(ads_snapshot.rows)
    if report_id == "rnp":
        return len(
            build_rnp_report(
                date_from=date_from,
                date_to=date_to,
                group_by="sku",
                finance_allowed=finance_allowed,
                organization_id=organization_id or 1,
                wb_token=wb_token,
            ).rows
        )
    if report_id == "abc":
        return len(
            build_abc_report(
                date_from=date_from,
                date_to=date_to,
                group_by="sku",
                filters="",
                finance_allowed=finance_allowed,
                organization_id=organization_id,
            ).rows
        )
    if report_id == "pnl":
        return len(
            build_pnl_report(
                date_from=date_from,
                date_to=date_to,
                group_by="sku",
                requested_state="preliminary",
                finance_allowed=finance_allowed,
                organization_id=organization_id,
                wb_token=None,
            ).rows
        )
    return 0


def _repricer_rows_for_abc_report(
    *,
    request: Request,
    actor: Any,
    wb_token: str | None,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    from app.routers.wb_repricer_bff import _hydrate_org_repricer_state, _list_repricer_skus_for_request

    _hydrate_org_repricer_state(request)
    scenario = str(request.query_params.get("scenario") or "complete")
    period_days = max(1, min(90, (date_to - date_from).days + 1))
    return _list_repricer_skus_for_request(
        request,
        scenario,
        actor=actor,
        wb_token=wb_token,
        include_promotions=False,
        include_content=False,
        period_days=period_days,
        date_from=date_from,
        date_to=date_to,
        max_items=None,
        sort_by_demand=True,
    )


def _repricer_row_to_abc_row(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    analytics = row.get("analytics") if isinstance(row.get("analytics"), dict) else {}
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    orders_units = _int_value(analytics.get("ordersUnits"))
    orders_kopecks = _int_value(analytics.get("revenueKopecks"))
    sales_units = _int_value(analytics.get("salesUnits")) - _int_value(analytics.get("returnsUnits"))
    sales_kopecks = _int_value(analytics.get("revenueKopecks") if analytics.get("realizationVersion")
                              else analytics.get("sellerRevenueKopecks", analytics.get("revenueKopecks")))
    fact_tax_state = analytics.get("factTaxState")
    if fact_tax_state:
        raw_profit = analytics.get("factNetProfitKopecks") if "factNetProfitKopecks" in analytics else analytics.get("netProfitKopecks")
        net_profit_kopecks = _int_value(raw_profit) if fact_tax_state == "configured" and raw_profit is not None else None
        margin_pct = round(net_profit_kopecks / sales_kopecks * 100, 2) if net_profit_kopecks is not None and sales_kopecks > 0 else None
    else:
        net_profit_kopecks = _int_value(analytics.get("netProfitKopecks") or analytics.get("factNetProfitKopecks"))
        margin_pct = _float_value(analytics.get("marginPct"))
    return {
        "sku": str(meta.get("articleId") or meta.get("vendorCode") or row.get("articleId") or ""),
        "nmId": meta.get("nmId"),
        "name": meta.get("name") or meta.get("title"),
        "photoUrl": meta.get("photoUrl") or meta.get("imageUrl") or _extract_wb_media_url(meta, _int_value(meta.get("nmId"))),
        "productStatus": analytics.get("productStatus") or meta.get("status") or "unknown",
        "manager": meta.get("managerId") or settings.get("managerId"),
        "brand": meta.get("brand"),
        "category": meta.get("subject") or meta.get("category"),
        "abcCode": analytics.get("abcCode") or "CC",
        "priceWithSppKopecks": (
            _int_value(analytics.get("buyerPriceNoWalletKopecks"))
            if analytics.get("buyerPriceNoWalletKopecks") is not None
            else None
        ),
        "ordersComposite": {"units": orders_units, "kopecks": orders_kopecks, "deltaPct": None},
        "salesComposite": {"units": sales_units, "kopecks": sales_kopecks, "deltaPct": None},
        "netTotalKopecks": net_profit_kopecks,
        "marginPct": margin_pct,
        "taxKopecks": analytics.get("taxKopecks"),
        "factTaxState": fact_tax_state,
        "factTaxReason": analytics.get("factTaxReason"),
        "adSpendKopecks": _int_value(analytics.get("adSpendKopecks")),
        "wbStockUnits": _int_value(analytics.get("wbStockUnits")),
        "buyoutPct": _float_value(analytics.get("buyoutPct")),
        "baskets": _int_value(analytics.get("baskets") if analytics.get("baskets") is not None else meta.get("basketsLast7d")),
        "logisticsCostPct": _float_value(analytics.get("logisticsCostPct")),
        "commissionCostPct": _float_value(analytics.get("commissionDisplayPct") or analytics.get("wbCommissionPct")),
        "storageCostPct": _float_value(analytics.get("storageCostPct")),
        "financeState": analytics.get("financeState"),
        "periodStatsState": analytics.get("periodStatsState"),
        "basketsState": analytics.get("basketsState"),
    }


def _abc_filtered_summary_from_rows(rows: list[dict[str, Any]], *, source_status: str, confidence: str) -> dict[str, Any]:
    def complete_sum(values: list[Any]) -> int | None:
        return sum(_int_value(value) for value in values) if values and all(value is not None for value in values) else None

    def composites(field: str, part: str) -> list[Any]:
        return [
            row[field].get(part) if isinstance(row.get(field), dict) else None
            for row in rows
            if isinstance(row, dict)
        ]
    sales_kopecks = complete_sum(composites("salesComposite", "kopecks"))
    profit_kopecks = complete_sum([
        _abc_row_profit(row)
        for row in rows
        if isinstance(row, dict)
    ])
    margin_pct = round(profit_kopecks / sales_kopecks * 100, 2) if profit_kopecks is not None and sales_kopecks and sales_kopecks > 0 else None
    return {
        "filterHash": "repricer-list",
        "skuCount": sum(not row.get("isUnallocated") for row in rows),
        "locomotiveCount": sum(1 for row in rows if str(row.get("productStatus")) == "locomotive"),
        "basketsCount": complete_sum([row.get("baskets") for row in rows if isinstance(row, dict)]),
        "ordersCount": complete_sum(composites("ordersComposite", "units")),
        "ordersKopecks": complete_sum(composites("ordersComposite", "kopecks")),
        "salesKopecks": sales_kopecks,
        "returnsKopecks": complete_sum([row.get("returnsKopecks") for row in rows if isinstance(row, dict)]),
        "profitKopecks": profit_kopecks,
        "marginPct": margin_pct,
        "adSpendKopecks": complete_sum([row.get("adSpendKopecks") for row in rows if isinstance(row, dict)]),
        "sourceStatus": source_status,
        "confidence": confidence,
    }


def _abc_row_cart_count(row: dict[str, Any]) -> int | None:
    for key in ("baskets", "cartCount", "cartAdds", "addToCart", "addToCartCount", "basketCount"):
        if row.get(key) is not None:
            return max(0, _int_value(row[key]))
    return None


def _normalize_abc_report_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        baskets = _abc_row_cart_count(item)
        item["baskets"] = baskets
        item["cartCount"] = baskets
        if item.get("basketsDeltaPct") is None and item.get("cartCountDeltaPct") is not None:
            item["basketsDeltaPct"] = item.get("cartCountDeltaPct")
        if item.get("cartCountDeltaPct") is None and item.get("basketsDeltaPct") is not None:
            item["cartCountDeltaPct"] = item.get("basketsDeltaPct")
        normalized.append(item)
    return normalized


def _abc_report_columns() -> list[dict[str, Any]]:
    right = {"format": "currency", "align": "right"}
    number = {"format": "number", "align": "right"}
    percent = {"format": "percent", "align": "right"}
    return [
        {"key": "sku", "label": "Товар", "sticky": True},
        {"key": "brand", "label": "Бренд"},
        {"key": "nmId", "label": "Артикул WB"},
        {"key": "productStatus", "label": "Статус"},
        {"key": "abcCode", "label": "ABC", "format": "abc", "align": "center"},
        {"key": "ruleStatus", "label": "Статус правила"},
        {"key": "ruleReasons", "label": "Причина правила"},
        {"key": "promotionStatusText", "label": "Акция"},
        {"key": "manager", "label": "Менеджер"},
        {"key": "priceBeforeSppKopecks", "label": "Цена до СПП", **right},
        {"key": "priceWithSppKopecks", "label": "Цена после СПП", **right},
        {"key": "sppPct", "label": "СПП", **percent},
        {"key": "averageSalePriceKopecks", "label": "Средняя цена", **right},
        {"key": "cogsPerUnitKopecks", "label": "Себестоимость", **right},
        {"key": "grossMarginKopecks", "label": "Валовая маржа", **right},
        {"key": "profitabilityPct", "label": "Рентабельность", **percent},
        {"key": "marginKopecks", "label": "Маржа на единицу", **right},
        {"key": "impressions", "label": "Показы", **number},
        {"key": "clicks", "label": "Переходы", **number},
        {"key": "baskets", "label": "Корзины", **number},
        {"key": "cartCrPct", "label": "Конверсия", **percent},
        {"key": "ordersComposite", "label": "Заказы"},
        {"key": "cancelledOrdersUnits", "label": "Отмены", **number},
        {"key": "salesComposite", "label": "Продажи"},
        {"key": "returnsKopecks", "label": "Возвраты", **right},
        {"key": "adSpendKopecks", "label": "Реклама / ДРР", **right},
        {"key": "commissionKopecks", "label": "Комиссия WB", **right},
        {"key": "logisticsKopecks", "label": "Логистика", **right},
        {"key": "storageKopecks", "label": "Хранение", **right},
        {"key": "acceptanceKopecks", "label": "Приёмка", **right},
        {"key": "acquiringKopecks", "label": "Эквайринг", **right},
        {"key": "penaltyKopecks", "label": "Штрафы", **right},
        {"key": "deductionKopecks", "label": "Удержания", **right},
        {"key": "loyaltyCostKopecks", "label": "Лояльность WB", **right},
        {"key": "taxKopecks", "label": "Налоги", **right},
        {"key": "totalOtherExpensesKopecks", "label": "Прочие расходы", **right},
        {"key": "compensationKopecks", "label": "Компенсации", **right},
        {"key": "netTotalKopecks", "label": "Чистая прибыль", **right},
        {"key": "ktrIndex", "label": "КТР / локализация", **number},
        {"key": "wbStockUnits", "label": "Остаток WB", **number},
        {"key": "turnoverDays", "label": "Оборачиваемость", **number},
        {"key": "comment", "label": "Комментарий"},
    ]


def _map_abc_to_report_response(payload: Any, date_range: dict[str, str]) -> dict[str, Any]:
    abc_rows = _normalize_abc_report_rows([row for row in getattr(payload, "rows", []) or [] if isinstance(row, dict)])
    source_status = getattr(payload, "sourceStatus", "partial")
    confidence = getattr(payload, "confidence", "unknown")
    blocker_ids = list(getattr(payload, "blockerIds", []) or [])
    summary_model = getattr(payload, "filteredSummary", None)
    filtered_summary = (
        summary_model.model_dump(mode="json")
        if hasattr(summary_model, "model_dump")
        else dict(summary_model or {})
    )
    if abc_rows and not _abc_rows_have_financial_activity(abc_rows):
        source_status = "partial"
        confidence = "low"
        if "WB_ABC_PERIOD_ACTIVITY_EMPTY" not in blocker_ids:
            blocker_ids.append("WB_ABC_PERIOD_ACTIVITY_EMPTY")
        filtered_summary["sourceStatus"] = source_status
        filtered_summary["confidence"] = confidence
    group_by = getattr(payload, "groupBy", "sku")
    def metric_text(value: Any) -> str:
        return "—" if value is None else str(value)
    return {
        "cacheVersion": ABC_REPORT_PAYLOAD_VERSION,
        "meta": _meta("abc", "ABC-анализ", "ABC and SKU profitability view.", "operational", source_status),
        "headline": "ABC summary for selected period.",
        "filters": {"dateRange": date_range, "groupBy": group_by},
        "kpis": [
            _kpi("sku_count", "SKU", metric_text(filtered_summary.get("skuCount"))),
            _kpi("orders", "Заказы", metric_text(filtered_summary.get("ordersCount"))),
            _kpi("orders_revenue", "Сумма заказов", metric_text(filtered_summary.get("ordersKopecks"))),
            _kpi("profit", "Прибыль", metric_text(filtered_summary.get("profitKopecks"))),
        ],
        "chart": {
            "title": "Чистая прибыль по SKU",
            "valueLabel": "Чистая прибыль, коп",
            "points": [
                {"label": str(row.get("sku") or row.get("label") or index + 1), "value": _abc_row_profit(row)}
                for index, row in enumerate(abc_rows[:20])
                if isinstance(row, dict) and _abc_row_profit(row) is not None
            ],
        },
        "columns": _abc_report_columns(),
        "rows": abc_rows,
        "sourceStatus": source_status,
        "confidence": confidence,
        "blockerIds": blocker_ids,
        "sourceEvidence": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in (getattr(payload, "sourceEvidence", []) or [])
        ],
        "filteredSummary": filtered_summary,
        **_financial_report_metadata(payload),
    }


def _storage_allocation_notice(storage: dict[str, Any]) -> str:
    basis = storage.get("basis")
    if basis == "current_wb_stock_units":
        notice = "Часть хранения распределена расчётно по текущему снимку остатков WB."
        if storage.get("stockObservedAt"):
            notice += f" Снимок остатков: {storage['stockObservedAt']}."
    elif basis in {"historical_stock_allocation", "historical_wb_stock_units", "wb_stock_history_units"}:
        notice = "Часть хранения распределена расчётно по истории остатков WB."
        if storage.get("observedDates"):
            notice += " Подтверждённые даты: " + ", ".join(storage["observedDates"]) + "."
    else:
        notice = "Источник распределения хранения не подтверждён."
    if not storage.get("complete"):
        notice += " Остальная сумма не распределена."
    return notice


def _with_storage_presentation(payload: dict[str, Any]) -> dict[str, Any]:
    """Correct saved presentation without invalidating or rebuilding its amounts."""
    storage = payload.get("storageAllocation")
    if not isinstance(storage, dict) or not storage:
        return payload
    legacy = ("Часть хранения распределена расчётно по историческим остаткам WB. История сохранена за даты: "
              + (", ".join(storage.get("observedDates") or []) or "нет") + "."
              + (" Остальная сумма не распределена." if not storage.get("complete") else ""))
    notice = _storage_allocation_notice(storage)
    warning = str(payload.get("warning") or "").replace(legacy, "").strip()
    return {**payload, "warning": warning if notice in warning else " ".join(filter(None, (warning, notice)))}


def _financial_report_metadata(payload: Any) -> dict[str, Any]:
    coverage = getattr(payload, "financialCoverage", {}) or {}
    storage = getattr(payload, "storageAllocation", {}) or {}
    notices = []
    if coverage and not coverage.get("complete"):
        missing = ", ".join(coverage.get("missingDates") or [])
        notices.append("Финансовые данные неполные; показана известная часть." + (f" Не загружены даты: {missing}." if missing else ""))
    if storage:
        notices.append(_storage_allocation_notice(storage))
    return {"financialCoverage": coverage, "storageAllocation": storage,
            "unallocatedFinancials": getattr(payload, "unallocatedFinancials", {}) or {},
            "warning": " ".join(notices) or None}


def _map_pnl_to_report_response(payload: Any, date_range: dict[str, str], cash_flow: dict[str, Any] | None = None, *, finance_allowed: bool = False, financial_only: bool = False) -> dict[str, Any]:
    cash_flow_disabled = (cash_flow or {}).get("status") == "disabled"
    cash_flow_pending = (cash_flow or {}).get("status") in {"not_loaded", "pending", "processing", "failed", "unavailable"}
    example_math = 'WB_USER_EXAMPLE_COSTS' in payload.blockerIds
    # Financial WB results already include the configured product economics.
    # Missing optional 1C expenses qualify the company total; they must not
    # erase known WB product results. Operational P&L retains its original gate.
    hide_profit = not finance_allowed or ((cash_flow_disabled or cash_flow_pending) and not example_math and not financial_only)
    financial_confirmation_status = "confirmed" if finance_allowed and not payload.blockerIds and not cash_flow_disabled and not cash_flow_pending else "pending"
    source_status = "partial" if (cash_flow_disabled or cash_flow_pending) and payload.sourceStatus == "fresh" else payload.sourceStatus
    rows = [row.model_dump(mode="json") for row in payload.rows]
    if hide_profit:
        for row in rows:
            row.update(overheadKopecks=None, netProfitKopecks=None, marginPct=None, sourceStatus=source_status)
    metadata = _financial_report_metadata(payload)
    if cash_flow_disabled or cash_flow_pending:
        metadata["warning"] = " ".join(filter(None, [metadata.get("warning"),
            "Показана прибыль по финансовому отчёту WB и сохранённым настройкам; расходы из 1С не включены. Итоговая прибыль компании не подтверждена." if financial_only else
            "Примерный расчёт по данным WB и настройкам расходов; расходы из 1С не включены. Это не фактическая итоговая прибыль." if example_math else "Расходы из 1С и итоговая прибыль не подтверждены."]))
    return {
        "cacheVersion": PNL_REPORT_PAYLOAD_VERSION,
        **({"profitBasis": "wb_financial_report"} if financial_only else {}),
        "meta": _meta("pnl", "P&L", "Unit P&L report.", "financial", source_status),
        "headline": "Финансовые поля показываются с учетом finance_viewer policy.",
        "warning": ("Примерный расчёт по данным WB и настройкам расходов; расходы из 1С не включены. Это не фактическая итоговая прибыль." if example_math else "Расходы из 1С и итоговая прибыль не подтверждены.") if cash_flow_disabled or cash_flow_pending else None,
        "blockerIds": [*payload.blockerIds, "ONE_C_DISABLED"] if cash_flow_disabled else list(payload.blockerIds),
        "financialConfirmationStatus": financial_confirmation_status,
        "sourceEvidence": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in (getattr(payload, "sourceEvidence", []) or [])
        ],
        "filters": {"dateRange": date_range, "groupBy": payload.groupBy},
        "kpis": [
            _kpi("revenue", "Выручка", str(payload.totals.revenueKopecks or 0)),
            _kpi("net_profit", "Прибыль по WB" if financial_only else "Примерная прибыль" if example_math else "Чистая прибыль", "—" if hide_profit or payload.totals.netProfitKopecks is None else str(payload.totals.netProfitKopecks)),
            _kpi("margin_pct", "Маржа", "—" if hide_profit or payload.totals.marginPct is None else f"{payload.totals.marginPct}%"),
        ],
        "chart": {
            "title": "Маржа по строкам",
            "valueLabel": "Маржа, %",
            "points": [] if hide_profit else [{"label": row.label, "value": row.marginPct} for row in payload.rows if row.marginPct is not None],
        },
        "columns": [
            {"key": "label", "label": "SKU"},
            {"key": "revenueKopecks", "label": "Выручка"},
            {"key": "cogsKopecks", "label": "Себестоимость"},
            {"key": "commissionKopecks", "label": "Комиссия"},
            {"key": "logisticsKopecks", "label": "Логистика"},
            {"key": "storageKopecks", "label": "Хранение"},
            {"key": "penaltyKopecks", "label": "Штрафы, ₽"},
            {"key": "acceptanceKopecks", "label": "Платная приёмка, ₽"},
            {"key": "taxKopecks", "label": "Налоги"},
            {"key": "adSpendKopecks", "label": "Реклама"},
            {"key": "overheadKopecks", "label": "Прочие расходы и корректировки"},
            {"key": "netProfitKopecks", "label": "Чистая прибыль"},
            {"key": "marginPct", "label": "Маржа"},
        ],
        "rows": rows,
        "cashFlow": cash_flow if finance_allowed else None,
        **metadata,
    }


def _map_cash_flow_to_expenses_response(
    cash_flow: dict[str, Any],
    date_range: dict[str, str],
    group_by: str,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    disabled = cash_flow.get("status") == "disabled"
    ready = cash_flow.get("status") == "ready"
    data = cash_flow.get("data") if isinstance(cash_flow.get("data"), dict) else {}
    source_rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    period_label = f"{date_range['from']} — {date_range['to']}"
    rows = [
        {
            "id": f"cf_{index}",
            "category": str(row.get("article") or "Статья 1С"),
            "article": str(row.get("article") or "Статья 1С"),
            "amountKopecks": _int_value(row.get("amountKopecks")),
            "sourceLabel": "1С cash-flow",
            "flowType": str(row.get("type") or "expense"),
            "periodLabel": period_label,
            "approvalStatusLabel": "готово к P&L",
            "allocationBaseLabel": "статья ДДС",
            "allocationCoverageLabel": "100% статьи",
            "owner": "1С",
            "pending": "нет",
            "comment": "Операционная статья ДДС из 1С cash-flow.",
        }
        for index, row in enumerate(source_rows)
        if isinstance(row, dict) and row.get("type") == "expense" and row.get("operationalExpense") is True
    ]
    total_kopecks = data.get("totals", {}).get("operationalExpenseKopecks") if isinstance(data.get("totals"), dict) else None
    if total_kopecks is None:
        total_kopecks = sum(_int_value(row.get("amountKopecks")) for row in rows)
    source_status = "partial" if disabled else "fresh" if cash_flow.get("status") == "ready" else "pending_financial"
    response = {
        "cacheVersion": EXPENSES_REPORT_PAYLOAD_VERSION,
        "meta": _meta("expenses", "Расходы", "Статьи ДДС из 1С cash-flow для операционных расходов.", "financial", source_status),
        "headline": "Показываем операционные расходные статьи ДДС из 1С за выбранный период.",
        "warning": "Интеграция с 1С отключена. Данные расходов за выбранный период не получены." if disabled else None if cash_flow.get("status") == "ready" else "Ждём 1С cash-flow для выбранного периода.",
        "financialConfirmationStatus": "final_financial" if cash_flow.get("status") == "ready" else "pending_financial",
        "filters": {"dateRange": date_range, "groupBy": group_by},
        "kpis": [
            _kpi("expense_rows", "Статей ДДС", str(len(rows)) if ready else "—"),
            _kpi("operational_expenses", "Опер. расходы", str(total_kopecks or 0) if ready else "—"),
            _kpi("cash_flow_status", "1С cash-flow", str(cash_flow.get("status") or "pending")),
        ],
        "chart": {
            "title": "Операционные статьи ДДС",
            "valueLabel": "Сумма, коп",
            "points": [{"label": row["article"], "value": row["amountKopecks"]} for row in rows[:20]],
        },
        "columns": [
            {"key": "category", "label": "Статья", "sticky": True},
            {"key": "periodLabel", "label": "Период"},
            {"key": "amountKopecks", "label": "Сумма", "format": "currency", "align": "right"},
            {"key": "sourceLabel", "label": "Источник"},
            {"key": "allocationBaseLabel", "label": "База распределения"},
            {"key": "allocationCoverageLabel", "label": "Покрытие SKU"},
            {"key": "approvalStatusLabel", "label": "Статус"},
            {"key": "owner", "label": "Ответственный"},
            {"key": "pending", "label": "Действие"},
        ],
        "rows": rows,
        "cashFlow": cash_flow,
    }
    if job is not None:
        response["reportJob"] = _report_job_for_response(job)
    return response


def _map_rnp_to_report_response(payload: Any, date_range: dict[str, str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for row in payload.rows:
        item = row.model_dump(mode="json")
        item["ordersComposite"] = {
            "units": item.get("orderCount"),
            "kopecks": item.get("orderSumKopecks"),
            "deltaPct": item.get("orderCountDeltaPct"),
        }
        item["salesComposite"] = {
            "units": item.get("buyoutCount"),
            "kopecks": item.get("buyoutSumKopecks"),
            "deltaPct": item.get("buyoutSumDeltaPct"),
        }
        item["sourceStatus"] = item.get("sourceStatus") or payload.sourceStatus
        rows.append(item)

    total_open = sum(_int_value(row.get("openCount")) for row in rows)
    total_carts = sum(_row_basket_units(row) for row in rows if isinstance(row, dict))
    total_orders = sum(_int_value(row.get("orderCount")) for row in rows)
    total_order_sum = sum(_int_value(row.get("orderSumKopecks")) for row in rows)
    ads_known = bool(rows) and all(row.get("adSpendKopecks") is not None for row in rows)
    total_ad_spend = (payload.adSpendKopecks if payload.adSpendKopecks is not None else sum(_int_value(row.get("adSpendKopecks")) for row in rows)) if ads_known else None
    active_warehouse_keys = {
        str(warehouse.get("warehouseId") or warehouse.get("warehouseName"))
        for row in rows
        for warehouse in (row.get("warehouses") or [])
        if isinstance(warehouse, dict) and (warehouse.get("warehouseId") is not None or warehouse.get("warehouseName"))
    }
    tacoo_pct = round(total_ad_spend / total_order_sum * 100, 2) if total_ad_spend is not None and total_order_sum > 0 else None
    financial_coverage = (payload.diagnostics or {}).get("financialCoverage", {})
    selected_finance = financial_coverage.get("selected", {})
    return {
        "meta": _meta("rnp", "РНП", "Funnel and ad economics by SKU.", "operational", payload.sourceStatus),
        "cacheVersion": RNP_REPORT_PAYLOAD_VERSION,
        "warning": ("Финансовые выкупы за период неполные; показана известная часть. Не загружены даты: "
            + ", ".join(selected_finance.get("missingDates", [])) + ".")
            if selected_finance and not selected_finance.get("complete") and any(row.get("financialBuyoutState") != "forbidden" for row in rows) else None,
        "headline": "Выкупили: финансовая реализация WB (rrDate, за вычетом возвратов). Заказы, трафик и процент выкупа: воронка WB; реклама: кампании WB Ads; органика рассчитана оценочно." + (
            " Общие показы и CTR недоступны: ответ WB sales-funnel/products не содержит показов. Показы рекламы не подставляются вместо общих."
            if rows and all(row.get("impressions") is None for row in rows) else ""),
        "comments": [comment for row in rows for comment in (row.get("comments") or [])],
        "auditEvents": [event for row in rows for event in (row.get("auditEvents") or [])],
        "filters": {"dateRange": date_range, "groupBy": payload.groupBy},
        "kpis": [
            _kpi("opens", "Открытия", str(total_open)),
            _kpi("carts", "Корзины", str(total_carts)),
            _kpi("orders", "Заказы", str(total_orders)),
            _kpi("ad_spend", "Реклама", str(total_ad_spend) if total_ad_spend is not None else "—", "Нет полной рекламной статистики за период" if not ads_known else "Расходы рекламных кампаний WB"),
            _kpi("tacoo", "TACoO", f"{tacoo_pct}%" if tacoo_pct is not None else "—"),
            _kpi("active_warehouses", "Активные склады", str(len(active_warehouse_keys))),
        ],
        "chart": {
            "title": "TACoO по SKU",
            "valueLabel": "TACoO, %",
            "points": [{"label": row.get("label") or row.get("sku") or str(row.get("nmId") or ""), "value": row["tacooPct"]} for row in rows if row.get("tacooPct") is not None],
        },
        "columns": [
            {"key": "sku", "label": "SKU"},
            {"key": "nmId", "label": "NM"},
            {"key": "productName", "label": "Товар"},
            {"key": "warehouseName", "label": "Основной склад WB"},
            {"key": "warehouses", "label": "Активные склады WB"},
            {"key": "impressions", "label": "Показы"},
            {"key": "openCount", "label": "Переходы"},
            {"key": "ctrPct", "label": "CTR из показов в клики, %"},
            {"key": "openCountDeltaPct", "label": "Дельта переходов"},
            {"key": "cartCount", "label": "Корзины"},
            {"key": "atcrPct", "label": "CR из карточки в корзину, %"},
            {"key": "wishlistCount", "label": "Добавили в отложенные"},
            {"key": "cartCountDeltaPct", "label": "Дельта корзин"},
            {"key": "ordersComposite", "label": "Заказы"},
            {"key": "salesComposite", "label": "Выкупили"},
            {"key": "buyoutPct", "label": "Выкуп"},
            {"key": "adImpressions", "label": "Показы РК"},
            {"key": "adClicks", "label": "Клики РК"},
            {"key": "adCtrPct", "label": "CTR РК"},
            {"key": "adCartAdds", "label": "Корзины РК"},
            {"key": "adOrders", "label": "Заказы РК"},
            {"key": "adSalesKopecks", "label": "Выручка РК"},
            {"key": "adSpendKopecks", "label": "Реклама"},
            {"key": "acooPct", "label": "ACoO"},
            {"key": "tacooPct", "label": "TACoO"},
            {"key": "organicOrderCountEstimated", "label": "Орг. заказы"},
            {"key": "organicSalesKopecksEstimated", "label": "Орг. выручка"},
            {"key": "organicEstimate", "label": "Органика оценка"},
            {"key": "reasons", "label": "Причины"},
            {"key": "drrPct", "label": "ДРР legacy"},
            {"key": "roiPct", "label": "ROI"},
            {"key": "marginPct", "label": "Маржа"},
        ],
        "rows": rows,
        "formulaNotes": payload.formulaNotes,
        "sourceEvidence": [item.model_dump(mode="json") for item in payload.sourceEvidence],
        "adsSourceStatus": payload.adsSourceStatus,
        "diagnostics": payload.diagnostics,
        "financialCoverage": financial_coverage,
    }


def _map_ads_to_report_response(date_range: dict[str, str], ads_snapshot: Any) -> dict[str, Any]:
    performance_totals = getattr(ads_snapshot, "performance_totals", None) or ads_snapshot.totals
    fullstats = [item for item in (ads_snapshot.diagnostics or {}).get("sources", []) if item.get("sourceId") == "wb-ads-fullstats"]
    performance_complete = (bool(fullstats) and all(item.get("status") == "fresh" for item in fullstats)) or bool((ads_snapshot.diagnostics or {}).get("summary", {}).get("sourceComplete"))
    rows = []
    for row in ads_snapshot.rows:
        is_unallocated = row.attribution_level == "campaign_only"
        ad_spend_kopecks = row.ad_spend_kopecks if getattr(row, "spend_source", "fullstats") == "fullstats" else None
        orders_kopecks = row.orders_kopecks or 0
        drr_pct = round((ad_spend_kopecks / orders_kopecks) * 100, 2) if orders_kopecks > 0 and ad_spend_kopecks is not None else None
        rows.append(
            {
                "campaignId": row.campaign_id or "unknown",
                "campaignName": row.campaign_name or f"Campaign {row.campaign_id or 'unknown'}",
                "campaignType": row.campaign_type or "unknown",
                "campaignStatus": row.campaign_status,
                "paymentType": row.payment_type,
                "campaignChangeTime": row.change_time,
                "manager": "maria",
                "sku": row.sku_id or "",
                "nmId": int(row.sku_id) if row.sku_id and row.sku_id.isdigit() else 0,
                "impressions": row.impressions,
                "clicks": row.clicks,
                "adClicks": row.clicks,
                "ctrPct": round(row.clicks / row.impressions * 100, 2) if row.clicks is not None and row.impressions else None,
                "baskets": row.cart_adds,
                "orders": {"units": row.orders_count or 0, "kopecks": orders_kopecks, "deltaPct": 0},
                "sales": {"units": row.orders_count or 0, "kopecks": orders_kopecks, "deltaPct": 0},
                "adSpendKopecks": ad_spend_kopecks,
                "adSpendSource": getattr(row, "spend_source", "fullstats"),
                "drrPct": drr_pct,
                "budgetCashKopecks": row.budget_cash_kopecks,
                "budgetNettingKopecks": row.budget_netting_kopecks,
                "budgetTotalKopecks": row.budget_total_kopecks,
                "unallocatedSpend": is_unallocated,
                "recommendation": "review" if is_unallocated else "keep",
                "recommendationStatus": "draft",
                "recommendationReason": "Расход не распределен по SKU: WB не вернул nms в fullstats." if is_unallocated else "SKU-атрибуция доступна для расчетов уровня SKU.",
                "attributionLevel": row.attribution_level,
                "confidence": row.confidence,
            }
        )
    daily_points_by_date: dict[str, int] = defaultdict(int)
    for daily_row in ads_snapshot.daily_rows:
        day = str(daily_row.get("date") or "")
        if not day:
            continue
        daily_points_by_date[day] += int(daily_row.get("adSpendKopecks") or 0)
    budget_by_campaign: dict[str, int] = {}
    for row in rows:
        campaign_id = str(row["campaignId"])
        budget = row.get("budgetTotalKopecks")
        if budget is not None:
            budget_by_campaign[campaign_id] = int(budget)
    cabinet_balance_kopecks = ads_snapshot.cabinet_balance.get("balanceKopecks") if isinstance(ads_snapshot.cabinet_balance, dict) else None
    budgets_complete = bool(rows) and all(row.get("budgetTotalKopecks") is not None for row in rows)
    return advertising_view({
        "meta": _meta("ads", "Реклама", "Campaign-first ads report.", "operational", "fresh" if performance_complete else ads_snapshot.source_status),
        "headline": "Клики рекламы и переходы в карточку разделены; campaign_only не распределяется в SKU P&L.",
        "filters": {"dateRange": date_range, "groupBy": "campaign"},
        "kpis": [
            _kpi("ad_spend", "Расход кампаний" if performance_complete else "Полученная часть расходов", str(performance_totals["ad_spend_kopecks"]) if performance_totals.get("ad_spend_kopecks") is not None and (performance_complete or any(row["adSpendKopecks"] is not None for row in rows)) else "—", "fullstats за выбранные даты; акты списаний не подставляются вместо статистики"),
            _kpi("orders_revenue", "Сумма рекламных заказов", str(performance_totals["orders_kopecks"]) if performance_totals.get("orders_kopecks") is not None else "—"),
            _kpi("campaign_budget", "Бюджет РК", str(sum(budget_by_campaign.values())) if budgets_complete else "—", f"Получено бюджетов: {len(budget_by_campaign)} из {len({row['campaignId'] for row in rows})} кампаний. Текущий budget.total, не расход и не исторический остаток."),
            _kpi("cabinet_balance", "Баланс кабинета", str(cabinet_balance_kopecks) if cabinet_balance_kopecks is not None else "—", "Текущий баланс рекламного кабинета, не бюджет конкретной РК."),
            _kpi("upd_rows", "Акты расходов", str(len(ads_snapshot.spend_documents))),
            _kpi("daily_rows", "Дневные строки", str(len(ads_snapshot.daily_rows))),
        ],
        "chart": {
            "title": "Расходы по дням",
            "valueLabel": "Расход, коп",
            "points": (
                [{"label": label, "value": value} for label, value in sorted(daily_points_by_date.items())]
                if daily_points_by_date
                else [{"label": row["campaignName"], "value": row["adSpendKopecks"]} for row in rows[:20] if row["adSpendKopecks"] is not None]
            ),
        },
        "columns": [
            {"key": "campaignName", "label": "РК"},
            {"key": "campaignType", "label": "Тип"},
            {"key": "campaignStatus", "label": "Статус"},
            {"key": "paymentType", "label": "Оплата"},
            {"key": "sku", "label": "SKU"},
            {"key": "nmId", "label": "WB"},
            {"key": "impressions", "label": "Показы"},
            {"key": "adClicks", "label": "Клики рекламы"},
            {"key": "ctrPct", "label": "CTR"},
            {"key": "baskets", "label": "Корзины"},
            {"key": "adSpendKopecks", "label": "Расход"},
            {"key": "budgetTotalKopecks", "label": "Бюджет РК"},
            {"key": "drrPct", "label": "ДРР"},
            {"key": "attributionLevel", "label": "Атрибуция"},
            {"key": "confidence", "label": "Уверенность"},
            {"key": "unallocatedSpend", "label": "Не распределено"},
            {"key": "recommendation", "label": "Рекомендация"},
            {"key": "recommendationReason", "label": "Основание"},
        ],
        "rows": rows,
        "sourceEvidence": [item.model_dump(mode="json") for item in ads_snapshot.source_evidence],
        "cabinetBalance": ads_snapshot.cabinet_balance,
        "spendDocuments": ads_snapshot.spend_documents,
        "dailyRows": ads_snapshot.daily_rows,
        "diagnostics": ads_snapshot.diagnostics,
        "performanceTotals": performance_totals,
        "dailyPerformanceTotals": getattr(ads_snapshot, "daily_performance_totals", {}),
        "performanceComplete": performance_complete,
        "campaignDetailsCollected": performance_complete,
        "campaignMembership": getattr(ads_snapshot, "campaign_membership", {}),
    })


def _ads_report_cache_is_usable(payload: dict[str, Any]) -> bool:
    rows = payload.get("rows")
    spend_documents = payload.get("spendDocuments")
    if not isinstance(rows, list) or not isinstance(spend_documents, list) or not spend_documents:
        return True
    spend_total = sum(int(item.get("adSpendKopecks") or 0) for item in spend_documents if isinstance(item, dict))
    if spend_total <= 0:
        return True
    row_spend_total = sum(int(item.get("adSpendKopecks") or 0) for item in rows if isinstance(item, dict))
    has_real_campaign_id = any(
        isinstance(item, dict) and str(item.get("campaignId") or "").isdigit() and int(item.get("campaignId") or 0) > 0
        for item in rows
    )
    return row_spend_total > 0 and has_real_campaign_id


def _build_ads_report_payload(
    *,
    actor: Any,
    date_from: date,
    date_to: date,
    date_range: dict[str, str],
    wb_token: str | None,
    refresh: bool = False,
) -> dict[str, Any]:
    group_by = "campaign"
    if not refresh:
        cached = get_ads_report_cache(
            organization_id=actor.organization_id,
            date_from=date_from,
            date_to=date_to,
            group_by=group_by,
        )
        if cached is not None:
            if _ads_report_cache_is_usable(cached):
                return enrich_advertising_products(cached, _catalog_meta_by_nm(actor.organization_id, include_shared_facts=False))

    ads_snapshot = build_cached_ads_attribution_snapshot(
        organization_id=actor.organization_id,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
    )
    if refresh and ads_snapshot.source_status == "blocked":
        raise HTTPException(status_code=409, detail="WB_ADS_CACHE_EMPTY")
    payload = _map_ads_to_report_response(date_range, ads_snapshot)
    stored = save_ads_report_cache(
        organization_id=actor.organization_id,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        payload=payload,
    )
    save_ads_history_snapshots(
        organization_id=actor.organization_id,
        rows=stored.get("rows") if isinstance(stored.get("rows"), list) else [],
        daily_rows=stored.get("dailyRows") if isinstance(stored.get("dailyRows"), list) else [],
        spend_documents=stored.get("spendDocuments") if isinstance(stored.get("spendDocuments"), list) else [],
    )
    return enrich_advertising_products(stored, _catalog_meta_by_nm(actor.organization_id, include_shared_facts=False))


def _build_digest_payload(
    date_range: dict[str, str],
    source_snapshot: Any,
    ads_snapshot: Any,
    plan_fact_payload: Any,
    funnel_snapshot: dict[str, Any] | None = None,
    report_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    aggregates = _orders_sales_by_nm(source_snapshot)
    funnel_totals = funnel_snapshot.get("totals") if isinstance(funnel_snapshot, dict) and isinstance(funnel_snapshot.get("totals"), dict) else {}
    scoped_selection = isinstance(funnel_snapshot, dict) and "selectionComplete" in funnel_snapshot
    has_funnel = bool(funnel_snapshot and (funnel_snapshot.get("rows") or scoped_selection))
    summary = report_summary if isinstance(report_summary, dict) else {}
    total_orders = _int_value(funnel_totals.get("orderCount")) if has_funnel and (not scoped_selection or funnel_totals.get("orderCount") is not None) else None
    total_sales_revenue = _int_value(funnel_totals.get("buyoutSumKopecks")) if has_funnel and (not scoped_selection or funnel_totals.get("buyoutSumKopecks") is not None) else None
    total_returns = (_int_value(summary["returnsUnits"]) if summary["returnsUnits"] is not None else None) if "returnsUnits" in summary else (sum(item["returns_qty"] for item in aggregates.values()) if source_snapshot.sales else None)
    performance_totals = getattr(ads_snapshot, "performance_totals", None) or ads_snapshot.totals
    total_ad_spend = performance_totals.get("ad_spend_kopecks") if ads_snapshot.source_status in {"fresh", "cached"} else None
    margin_profit = (_int_value(summary["marginKopecks"]) if summary["marginKopecks"] is not None else None) if "marginKopecks" in summary else _preliminary_margin_kopecks(source_snapshot)
    margin_hint = (
        "Прибыль после себестоимости, расходов WB, рекламы и подтверждённого налога за период."
        if summary
        else "Предварительная прибыль после удержаний WB. Формула: seller payout - комиссии WB - логистика - штрафы - приемка - хранение."
    )
    if margin_profit is None:
        margin_hint = "Прибыль неизвестна: не подтверждены все исходные данные, себестоимость или налог за выбранный период."
    elif summary.get("exampleCosts"):
        margin_hint = "Расчёт по финансовому P&L с примерной себестоимостью 800 ₽. Это не фактическая прибыль."
    stock_rows = [_stock_row_to_digest_payload(row) for row in source_snapshot.stocks]
    alerts = []
    for row in stock_rows:
        if row["availableUnits"] <= 0:
            alerts.append(
                {
                    "id": f"stock-oos-{row['nmId']}",
                    "alertType": "stock_oos",
                    "severity": "critical",
                    "entityType": "warehouse",
                    "entityId": str(row["nmId"]),
                    "title": f"{row['sku']} OOS",
                    "details": "availableUnits <= 0",
                    "createdAt": _utc_now_iso(),
                    "resolvedAt": None,
                    "route": "/wb/reports/stock",
                }
            )
    plan_fact_rows = []
    plan_fact_blockers = set(getattr(plan_fact_payload, "blockerIds", []) or [])
    if "WB-14A" not in plan_fact_blockers:
        for row in plan_fact_payload.rows:
            fact = row.factKopecks or 0
            plan = row.planKopecks or 0
            plan_fact_rows.append(
                {
                    "owner": "company" if row.ownerId == "company" else "manager",
                    "ownerId": row.ownerId,
                    "name": row.ownerLabel,
                    "planKopecks": plan,
                    "factKopecks": fact,
                    "completionPct": row.completionPct or 0,
                    "forecastKopecks": row.forecastKopecks or 0,
                    "deltaPct": round(((fact - plan) / plan * 100), 2) if plan else 0,
                    "needPerDayKopecks": row.minPerDayKopecks,
                    "status": "ok" if plan else "no_plan",
                    "factFreshness": "preliminary",
                }
            )
    funnel_weekly_balance = _build_funnel_weekly_balance(date_range, funnel_snapshot or {}) if has_funnel else None
    weekly_balance = funnel_weekly_balance or _build_weekly_balance(date_range, source_snapshot)
    period_cards = _build_period_cards(date_range, weekly_balance)
    if has_funnel:
        period_cards[0].update(ordersUnits=total_orders, ordersKopecks=funnel_totals.get("orderSumKopecks"),
            openCount=funnel_totals.get("openCount"), cartCount=funnel_totals.get("cartCount"),
            salesUnits=funnel_totals.get("buyoutCount"), revenueKopecks=total_sales_revenue,
            buyoutPct=funnel_totals.get("buyoutPct"), returnsUnits=total_returns)
    problem_rows = _build_digest_problem_rows(stock_rows, ads_snapshot)
    oos_count = sum(int(row.get("skuCount") or 0) for row in problem_rows if row.get("reason") == "oos_risk")
    ads_freshness_message = (
        "WB_TOKEN_REQUIRED"
        if "WB_TOKEN_REQUIRED" in (getattr(ads_snapshot, "blocker_ids", []) or [])
        else "promotion/adverts/fullstats chain"
    )
    return {
        "meta": _meta("digest", "Воронка продаж", "Сводка продаж и проблемных SKU.", "operational", source_snapshot.source_status),
        "cacheVersion": DIGEST_REPORT_PAYLOAD_VERSION,
        "headline": "Воронка продаж собрана из WB sales funnel, finance/stocks/ads и plan-fact.",
        "dateRange": date_range,
        "kpis": [
            _kpi("orders_qty", "Заказы, шт", str(total_orders) if total_orders is not None else "—", "Заказы по дате заказа из воронки WB за выбранный период."),
            _kpi("sales_revenue", "Выкупили на сумму", str(total_sales_revenue) if total_sales_revenue is not None else "—", "Выкупы по дате заказа из воронки WB. Не финансовая выручка по дате реализации."),
            _kpi(
                "margin_profit",
                "Марж. прибыль",
                str(margin_profit) if margin_profit is not None else "—",
                margin_hint,
            ),
            _kpi("oos_risk", "OOS риск", f"{oos_count} SKU", "Товары с нулевым доступным остатком: остаток WB + возвраты от клиента = 0. Такие позиции могут перестать продаваться, пока не появится доступный остаток."),
            _kpi("ad_spend", "Реклама, коп", str(total_ad_spend) if total_ad_spend is not None else "—", "Расходы рекламных кампаний WB за выбранный период; финансовые удержания не подставляются."),
            _kpi("returns_qty", "Возвраты, шт", str(total_returns) if total_returns is not None else "—", "Количество возвратов по продажам WB за выбранный период. Отмены заказов не считаются возвратами."),
        ],
        "planFactRows": plan_fact_rows,
        "freshness": [
            {"id": "orders", "label": "Orders", "sourceType": "operational", "state": _freshness_state(source_snapshot.source_status), "updatedAt": _utc_now_iso(), "message": "statistics orders stream"},
            {"id": "sales", "label": "Sales", "sourceType": "operational", "state": _freshness_state(source_snapshot.source_status), "updatedAt": _utc_now_iso(), "message": "statistics sales stream"},
            {"id": "stocks", "label": "Stocks", "sourceType": "operational", "state": _freshness_state(source_snapshot.source_status), "updatedAt": _utc_now_iso(), "message": "analytics warehouse_remains current snapshot"},
            {"id": "finance", "label": "Finance", "sourceType": "financial", "state": _freshness_state(source_snapshot.source_status), "updatedAt": _utc_now_iso(), "message": "realization details + finance sales reports reconciliation"},
            {"id": "ads", "label": "Ads", "sourceType": "operational", "state": _freshness_state(ads_snapshot.source_status), "updatedAt": _utc_now_iso(), "message": ads_freshness_message},
        ],
        "alerts": alerts,
        "charts": [
            {
                "title": weekly_balance["title"],
                "valueLabel": weekly_balance["valueLabel"],
                "compareLabel": weekly_balance["compareLabel"],
                "points": [
                    {
                        "label": row["label"],
                        "value": row["ordersUnits"],
                        "compareValue": row["salesUnits"],
                        "date": row["date"],
                        "ordersKopecks": row["ordersKopecks"],
                        "salesKopecks": row["salesKopecks"],
                        "returnsUnits": row["returnsUnits"],
                        "buyoutPct": row["buyoutPct"],
                    }
                    for row in weekly_balance["points"]
                ],
            }
        ],
        "weeklyBalance": weekly_balance,
        "funnelSummary": funnel_totals if has_funnel else None,
        "topMovers": (funnel_snapshot or {}).get("topMovers"),
        "funnelSourceStatus": (funnel_snapshot or {}).get("status") if isinstance(funnel_snapshot, dict) else None,
        "periodCards": period_cards,
        "problemRows": problem_rows,
        "quickLinks": [
            {"title": "ABC", "description": "SKU таблица", "href": "/wb/reports/abc"},
            {"title": "РНП", "description": "Funnel + ads", "href": "/wb/reports/rnp"},
            {"title": "Реклама", "description": "Campaign attribution", "href": "/wb/reports/ads"},
            {"title": "Остатки", "description": "Склады WB", "href": "/wb/reports/stock"},
        ],
    }


def _digest_cache_key(date_from: date, date_to: date) -> str:
    return f"reports_digest_{DIGEST_REPORT_PAYLOAD_VERSION}_{date_from.isoformat()}_{date_to.isoformat()}"


def _digest_job_cache_key(date_from: date, date_to: date) -> str:
    return f"reports_digest_job_{date_from.isoformat()}_{date_to.isoformat()}"


BACKGROUND_REPORT_IDS = {"abc", "rnp", "ads", "pnl", "expenses", "stock", "week-over-week"}
ACTIVE_REPORT_JOB_STATES = {"queued", "running", "waiting_1c", "waiting_baskets_detail", "waiting_daily_detail"}
ACTIVE_REPORT_JOB_STAGES = {"queued", "refreshing_sources", "building_report"}
BACKGROUND_REPORT_JOB_STALE_AFTER = timedelta(minutes=15)
BACKGROUND_REPORT_QUEUED_STALE_AFTER = timedelta(seconds=30)
DIGEST_CACHE_TTL = timedelta(hours=24)
REPORT_PAYLOAD_CACHE_TTL = timedelta(hours=24)
ABC_REPORT_PAYLOAD_VERSION = "v35"
PNL_REPORT_PAYLOAD_VERSION = "v26"
EXPENSES_REPORT_PAYLOAD_VERSION = "v4"
RNP_REPORT_PAYLOAD_VERSION = "v13"
STOCK_REPORT_PAYLOAD_VERSION = "v8"
WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION = "v18-current-storage"


def _abc_economics_version(organization_id: int) -> str:
    runtime = load_runtime_state(organization_id) or {}
    rules = load_active_profile(organization_id)
    algorithm = load_algorithm_settings(organization_id) or {}
    payload = {
        "algorithm": {
            key: algorithm.get(key)
            for key in (
                "cogsByGarmentRub",
                "cogsByGarmentHistory",
                "taxPct",
                SETTINGS_KEY,
                "otherExpensePricePct",
                "otherExpensePerSaleRub",
            )
        },
        "skuSettingsOverrides": {
            sku: {
                key: settings.get(key)
                for key in ("cogsKopecks", "taxPct", "otherExpensePricePct", "otherExpensePerSaleKopecks")
            }
            for sku, settings in (runtime.get("skuSettingsOverrides") or {}).items()
            if isinstance(settings, dict)
        },
        "skuMetaOverrides": {
            sku: {key: meta.get(key) for key in ("status", "managerId", "managerName", "brand")}
            for sku, meta in (runtime.get("skuMetaOverrides") or {}).items()
            if isinstance(meta, dict)
        },
        "cogsHistory": runtime.get("cogsHistory") or {},
        "rulesProfile": {"id": rules.profileId, "version": rules.version, "config": rules.config.model_dump(mode="json")},
    }
    return sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:12]


def _report_cache_key(
    report_id: str,
    date_from: date,
    date_to: date,
    group_by: str,
    source: str,
    *,
    organization_id: int | None = None,
    finance_allowed: bool = False,
) -> str:
    if report_id == "abc" and organization_id is not None:
        return f"reports_payload_abc_{ABC_REPORT_PAYLOAD_VERSION}_{_abc_economics_version(organization_id)}_org{organization_id}_{date_from.isoformat()}_{date_to.isoformat()}_{group_by}_{source}_{'finance' if finance_allowed else 'nofinance'}"
    if report_id == "pnl":
        return f"reports_payload_pnl_{PNL_REPORT_PAYLOAD_VERSION}_{date_from.isoformat()}_{date_to.isoformat()}_{group_by}_{source}_{'finance' if finance_allowed else 'nofinance'}"
    if report_id == "week-over-week":
        return f"reports_payload_week-over-week_{date_from.isoformat()}_{date_to.isoformat()}_{group_by}_{source}_{'finance' if finance_allowed else 'nofinance'}"
    return f"reports_payload_{report_id}_{date_from.isoformat()}_{date_to.isoformat()}_{group_by}_{source}"


def _report_job_cache_key(
    report_id: str,
    date_from: date,
    date_to: date,
    group_by: str,
    source: str | None = None,
    *,
    finance_allowed: bool | None = None,
) -> str:
    if report_id == "stats" and source and source.startswith("catalog:"):
        return f"reports_job_catalog_{source.split(':', 1)[1]}"
    source_suffix = f"_{source}" if source and (report_id == "pnl" or (report_id == "stats" and (source.startswith("repricer-finance:") or source == "statistics-comparison"))) else ""
    finance_suffix = (
        f"_{'finance' if finance_allowed else 'nofinance'}"
        if report_id in {"abc", "pnl", "week-over-week"} and finance_allowed is not None
        else ""
    )
    return f"reports_job_{report_id}_{date_from.isoformat()}_{date_to.isoformat()}_{group_by}{source_suffix}{finance_suffix}"


def _parse_report_job_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _digest_cache_age_seconds(cache: dict[str, Any]) -> int | None:
    timestamp = (
        _parse_report_job_timestamp(cache.get("completedAt"))
        or _parse_report_job_timestamp(cache.get("fetchedAt"))
    )
    if timestamp is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds()))


def _digest_source_revision(organization_id: int, date_from: date, date_to: date) -> str:
    start, end = _digest_source_bounds(date_from, date_to)
    parts: list[Any] = [legacy_finance_tax_revision(organization_id)]
    for prefix in REPORT_DAILY_SOURCE_PREFIXES.values():
        parts.append([prefix, [row for row in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100)
            if (_parse_cache_date(row.get("dateFrom")) or date.max) <= end
            and (_parse_cache_date(row.get("dateTo")) or date.min) >= start]])
    return sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _digest_cache_is_fresh(cache: dict[str, Any], organization_id: int | None = None) -> bool:
    age_seconds = _digest_cache_age_seconds(cache)
    digest = cache.get("digest") if isinstance(cache.get("digest"), dict) else None
    if organization_id is not None:
        start, end = _parse_cache_date(cache.get("dateFrom")), _parse_cache_date(cache.get("dateTo"))
        if not start or not end or cache.get("sourceComplete") is not True:
            return False
        if cache.get("sourceRevision") != _digest_source_revision(organization_id, start, end):
            return False
    return (
        isinstance(digest, dict)
        and digest.get("cacheVersion") == DIGEST_REPORT_PAYLOAD_VERSION
        and age_seconds is not None
        and age_seconds <= int(DIGEST_CACHE_TTL.total_seconds())
    )


def _report_payload_cache_age_seconds(cache: dict[str, Any]) -> int | None:
    timestamp = (
        _parse_report_job_timestamp(cache.get("completedAt"))
        or _parse_report_job_timestamp(cache.get("fetchedAt"))
    )
    if timestamp is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds()))


def _report_payload_cache_is_fresh(cache: dict[str, Any]) -> bool:
    age_seconds = _report_payload_cache_age_seconds(cache)
    return (
        isinstance(cache.get("report"), dict)
        and age_seconds is not None
        and age_seconds <= int(REPORT_PAYLOAD_CACHE_TTL.total_seconds())
    )


def _rnp_report_cache_has_funnel_signal(cache: dict[str, Any]) -> bool:
    report = cache.get("report") if isinstance(cache.get("report"), dict) else {}
    diagnostics = report.get("diagnostics") if isinstance(report.get("diagnostics"), dict) else {}
    summary = diagnostics.get("summary") if isinstance(diagnostics.get("summary"), dict) else {}
    funnel_rows = _int_value(summary.get("funnelRows"))
    if funnel_rows > 0:
        return True
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    has_funnel_metric = any(
        isinstance(row, dict)
        and (
            _int_value(row.get("openCount")) > 0
            or _int_value(row.get("cartCount")) > 0
            or _int_value(row.get("orderCount")) > 0
            or _int_value(row.get("orderSumKopecks")) > 0
            or _int_value((row.get("ordersComposite") or {}).get("units") if isinstance(row.get("ordersComposite"), dict) else None) > 0
        )
        for row in rows
    )
    if has_funnel_metric:
        return True
    ads_rows = _int_value(summary.get("adsRows"))
    has_ads_only_rows = any(
        isinstance(row, dict)
        and (
            _int_value(row.get("adImpressions")) > 0
            or _int_value(row.get("adClicks")) > 0
            or _int_value(row.get("adOrders")) > 0
            or _int_value(row.get("adSpendKopecks")) > 0
        )
        for row in rows
    )
    return not (funnel_rows == 0 and (ads_rows > 0 or has_ads_only_rows))


def _report_tax_unavailable(report: dict[str, Any]) -> bool:
    return "tax_policy_unavailable" in (report.get("blockerIds") or []) or any(
        row.get(key) == "tax_policy_unavailable"
        for row in report.get("rows") or [] if isinstance(row, dict)
        for key in ("factTaxReason", "previousFactTaxReason")
    )


def _report_payload_cache_is_usable(report_id: str, cache: dict[str, Any], *, organization_id: int | None = None, allow_stale: bool = False) -> bool:
    if not isinstance(cache.get("report"), dict) or (not allow_stale and not _report_payload_cache_is_fresh(cache)):
        return False
    if cache["report"].get("loadingSnapshot") and not allow_stale:
        return False  # A worker preview is never a completed source receipt.
    if report_id == "pnl" and not allow_stale and (cache["report"].get("meta") or {}).get("freshnessState") == "stale":
        return False  # A source changed while calculating; never certify that snapshot.
    if report_id == "pnl" and organization_id is not None:
        from app.wb_pnl_advertising import _source_context
        from app.wb_pnl_costs import current_cost_revision
        if cache.get("pnlAdvertisingContext") != _source_context(organization_id):
            return False  # Not even a preview may leak a previous WB connection.
        if cache.get("pnlCurrentCostRevision") != current_cost_revision(organization_id):
            return False  # Stale previews must not retain superseded/missing costs.
    if report_id == "ads":
        report = cache.get("report") or {}
        if not report.get("campaignDetailsCollected") or (report.get("meta") or {}).get("freshnessState") not in {"fresh", "cached"}:
            return False
    if organization_id is not None and not (allow_stale and report_id in {"abc", "pnl", "week-over-week"}):
        start, end, _ = _date_range_from_report_cache(cache)
        if start and end:
            if report_id == "pnl":
                from app.wb_reports_sprint_d import _pnl_source_revision
                if cache.get("pnlSourceRevision") != _pnl_source_revision(organization_id, start, end):
                    return False
            source_start, source_end = _report_source_bounds(report_id, start, end)
            built_at = _parse_report_job_timestamp(cache.get("completedAt") or cache.get("fetchedAt"))
            if report_id in {"abc", "pnl", "rnp", "week-over-week"}:
                # Compare the source's observation used by the calculation,
                # not a revision sampled after calculation has completed.
                evidence = (cache.get("report") or {}).get("sourceEvidence") or []
                used = [_parse_report_job_timestamp(item.get("lastSyncedAt")) for item in evidence
                        if isinstance(item, dict) and "finance" in str(item.get("sourceId", ""))]
                used_at = max((stamp for stamp in used if stamp), default=built_at)
                for meta in list_source_cache_ranges_by_prefix(organization_id, "repricer_finance_", limit=100):
                    if str(meta.get("sourceKey") or "").startswith("repricer_finance_progress_"):
                        continue  # Cursor progress is not a new financial source.
                    if (_parse_cache_date(meta.get("dateFrom")) or date.min) > source_end:
                        continue
                    observed = _parse_report_job_timestamp(meta.get("fetchedAt"))
                    if (_parse_cache_date(meta.get("dateTo")) or date.min) >= source_start and observed and (not used_at or observed > used_at):
                        return False
            if report_id in {"abc", "pnl", "week-over-week"}:
                # New historical stock can change only the approved storage
                # allocation, even when no financial operation was reloaded.
                for meta in list_source_cache_ranges_by_prefix(organization_id, "stock_history_", limit=500):
                    day = _parse_cache_date(meta.get("dateFrom"))
                    observed = _parse_report_job_timestamp(meta.get("fetchedAt"))
                    if day and source_start <= day <= source_end and observed and (not built_at or observed > built_at):
                        return False
            for source_name in REPORT_DAILY_SOURCES_BY_ID.get(report_id, ()):
                if source_name == "stocks":
                    observed = _parse_report_job_timestamp(get_source_cache_fetched_at(organization_id, "stocks"))
                    if observed and (not built_at or observed > built_at):
                        return False
                    continue
                prefix = REPORT_DAILY_SOURCE_PREFIXES[source_name]
                for row in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100):
                    if (_parse_cache_date(row.get("dateFrom")) or date.max) > source_end or (_parse_cache_date(row.get("dateTo")) or date.min) < source_start:
                        continue
                    observed = _parse_report_job_timestamp(row.get("fetchedAt"))
                    if observed and (not built_at or observed > built_at):
                        return False
            imported = _parse_report_job_timestamp(get_source_cache_fetched_at(organization_id, f"funnel_seller_{start}_{end}"))
            cached_at = _parse_report_job_timestamp(cache.get("fetchedAt") or cache.get("completedAt"))
            if imported and (not cached_at or imported > cached_at):
                return False
    if report_id in {"abc", "pnl", "week-over-week"} and organization_id is not None:
        tax_revision = legacy_finance_tax_revision(organization_id)
        preview = allow_stale and cache["report"].get("loadingSnapshot")
        if cache.get("taxRevision") != tax_revision or (not preview and tax_revision == "unavailable"):
            return False
        if not allow_stale and _report_tax_unavailable(cache["report"]):
            return False  # A transient resolver failure needs a background retry.
        if cache.get("quarterlyTaxRevision") is not None and cache["quarterlyTaxRevision"] != quarterly_tax_revision(organization_id):
            return False
    if report_id == "rnp" and not _rnp_report_cache_has_funnel_signal(cache):
        return False
    if report_id == "rnp":
        if organization_id is not None:
            revision = legacy_finance_tax_revision(organization_id)
            if revision == 'unavailable' or cache.get('catalogRevision') != revision:
                return False
        report = cache.get("report") if isinstance(cache.get("report"), dict) else {}
        if report.get("cacheVersion") != RNP_REPORT_PAYLOAD_VERSION:
            return False
    if report_id == "stock":
        report = cache.get("report") if isinstance(cache.get("report"), dict) else {}
        if report.get("cacheVersion") != STOCK_REPORT_PAYLOAD_VERSION:
            return False
    if report_id == "abc":
        report = cache.get("report") if isinstance(cache.get("report"), dict) else {}
        if report.get("cacheVersion") != ABC_REPORT_PAYLOAD_VERSION:
            return False
        if organization_id is not None and report.get("economicsVersion") != _abc_economics_version(organization_id):
            return False
    if report_id in {"pnl", "expenses"}:
        report = cache.get("report") if isinstance(cache.get("report"), dict) else {}
        version = PNL_REPORT_PAYLOAD_VERSION if report_id == "pnl" else EXPENSES_REPORT_PAYLOAD_VERSION
        if report.get("cacheVersion") != version:
            return False
        cash_flow = report.get("cashFlow")
        cached_1c_disabled = "ONE_C_DISABLED" in (report.get("blockerIds") or []) or (isinstance(cash_flow, dict) and cash_flow.get("status") == "disabled")
        # Earlier previews used disabled for a temporarily unavailable optional
        # source. That is not a settings change and must not rebuild forever.
        if isinstance(cash_flow, dict) and cash_flow.get("reason") == "optional_operating_costs_unavailable":
            cached_1c_disabled = False
        if cached_1c_disabled != (not get_settings().one_c_enabled):
            return False
    if report_id == "week-over-week":
        report = cache.get("report") if isinstance(cache.get("report"), dict) else {}
        if report.get("cacheVersion") != WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION:
            return False
        start, end, _ = _date_range_from_report_cache(cache)
        revision_args = (organization_id, _previous_period(start, end)[0], end) if start and end else (organization_id,)
        if organization_id is not None and report.get("operationalSourceRevision"):
            from app.wb_week_sources import source_revision as operational_revision
            if report["operationalSourceRevision"] != operational_revision(*revision_args):
                return False
        if organization_id is not None and report.get("financialVersion") and report.get("financialAccess"):
            from app.wb_week_finance import source_revision
            if report.get("financialSourceRevision") != source_revision(*revision_args):
                return False
        if not _week_report_has_period_activity(report):
            return False
    return True


def _report_payload_cache_meta(cache: dict[str, Any], date_range: dict[str, str], status: str | None = None) -> dict[str, Any]:
    age_seconds = _report_payload_cache_age_seconds(cache)
    fresh = age_seconds is not None and age_seconds <= int(REPORT_PAYLOAD_CACHE_TTL.total_seconds())
    report = cache.get("report") or {}
    fresh = fresh and not report.get("loadingSnapshot", False)
    if report.get("financialVersion") and report.get("financialAccess") is not False:
        from app.wb_week_finance import coverage_fresh
        coverage = report.get("financialCoverage") or {}
        fresh = fresh and all(coverage_fresh(coverage.get(label, {})) for label in ("selected", "previous"))
        fresh = fresh and not any((report.get("periodCoverage") or {}).values())
        fresh = fresh and all(not item.get("funnelNeedsCollection", not item.get("funnelComplete")) and item.get("advertisingComplete")
            for item in (report.get("metricCoverage") or {}).values())
    return {
        "status": status or ("exact" if fresh else "stale"),
        "requestedRange": date_range,
        "fresh": fresh,
        "ageSeconds": age_seconds,
        "ttlSeconds": int(REPORT_PAYLOAD_CACHE_TTL.total_seconds()),
        "completedAt": cache.get("completedAt"),
        "fetchedAt": cache.get("fetchedAt"),
        "calculationRevision": cache.get("taxRevision"),
    }


def _save_exact_report_payload_cache(
    *,
    organization_id: int,
    report_id: str,
    date_from: date,
    date_to: date,
    group_by: str,
    source: str,
    report: dict[str, Any],
    finance_allowed: bool = False,
) -> dict[str, Any]:
    quarter_revision = None
    if report_id in {"abc", "pnl", "week-over-week", "rnp"}:
        quarter_revision = assert_current_snapshot(organization_id)
    report = _normalize_report_basket_fields(report)
    if report_id == "abc":
        report["economicsVersion"] = _abc_economics_version(organization_id)
    cache = {
        "report": report,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "completedAt": _utc_now_iso(),
    }
    if quarter_revision is not None:
        cache["quarterlyTaxRevision"] = quarter_revision
    if report_id in {"abc", "pnl", "week-over-week"}:
        # Unknown financial inputs are report blockers, not missing settings
        # revisions. Keep those values unknown, but do not rebuild the same
        # DB report forever when only a user cost/tax configuration can fix it.
        cache["taxRevision"] = legacy_finance_tax_revision(organization_id)
    if report_id == "pnl":
        from app.wb_pnl_advertising import _source_context
        from app.wb_pnl_costs import current_cost_revision
        from app.wb_reports_sprint_d import _pnl_source_revision
        cache["pnlAdvertisingContext"] = _source_context(organization_id)
        cache["pnlCurrentCostRevision"] = current_cost_revision(organization_id)
        cache["pnlSourceRevision"] = _pnl_source_revision(organization_id, date_from, date_to)
    if report_id == 'rnp':
        cache['catalogRevision'] = legacy_finance_tax_revision(organization_id)
    save_source_cache(
        organization_id,
        _report_cache_key(report_id, date_from, date_to, group_by, source, organization_id=organization_id, finance_allowed=finance_allowed),
        cache,
    )
    return cache


def _basket_units_from_value(value: Any) -> int:
    if isinstance(value, dict):
        return max(
            0,
            _int_value(
                value.get("units")
                or value.get("value")
                or value.get("cartCount")
                or value.get("cartAdds")
                or value.get("baskets")
            ),
        )
    return max(0, _int_value(value))


def _row_basket_units(row: dict[str, Any]) -> int:
    for key in ("baskets", "cartCount", "cartAdds", "adCartAdds", "addToCart", "addToCartCount", "basketCount"):
        units = _basket_units_from_value(row.get(key))
        if units > 0:
            return units
    return 0


def _normalize_report_basket_row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    baskets = _row_basket_units(item)
    if baskets <= 0:
        return item
    existing_baskets = item.get("baskets")
    if isinstance(existing_baskets, dict):
        basket_payload = dict(existing_baskets)
        basket_payload.setdefault("units", baskets)
        item["baskets"] = basket_payload
    else:
        item["baskets"] = baskets
    item["cartCount"] = baskets
    item.setdefault("cartAdds", baskets)
    if item.get("basketsDeltaPct") is None and item.get("cartCountDeltaPct") is not None:
        item["basketsDeltaPct"] = item.get("cartCountDeltaPct")
    if item.get("cartCountDeltaPct") is None and item.get("basketsDeltaPct") is not None:
        item["cartCountDeltaPct"] = item.get("basketsDeltaPct")
    return item


def _normalize_report_basket_fields(report: dict[str, Any]) -> dict[str, Any]:
    result = dict(report)
    rows = result.get("rows")
    if isinstance(rows, list):
        result["rows"] = [_normalize_report_basket_row(row) if isinstance(row, dict) else row for row in rows]
    return result


def _date_range_from_report_cache(cache: dict[str, Any], fallback_from: date | None = None, fallback_to: date | None = None) -> tuple[date | None, date | None, dict[str, str] | None]:
    report = cache.get("report") if isinstance(cache.get("report"), dict) else cache.get("digest")
    filters = report.get("filters") if isinstance(report, dict) and isinstance(report.get("filters"), dict) else {}
    raw_range = filters.get("dateRange") if isinstance(filters.get("dateRange"), dict) else {}
    parsed_from = _parse_report_job_timestamp(raw_range.get("from")) if raw_range.get("from") else None
    parsed_to = _parse_report_job_timestamp(raw_range.get("to")) if raw_range.get("to") else None
    date_from = parsed_from.date() if parsed_from else None
    date_to = parsed_to.date() if parsed_to else None
    if date_from is None and cache.get("dateFrom"):
        parsed = _parse_report_job_timestamp(cache.get("dateFrom"))
        date_from = parsed.date() if parsed else None
    if date_to is None and cache.get("dateTo"):
        parsed = _parse_report_job_timestamp(cache.get("dateTo"))
        date_to = parsed.date() if parsed else None
    date_from = date_from or fallback_from
    date_to = date_to or fallback_to
    if date_from is None or date_to is None:
        return date_from, date_to, None
    return date_from, date_to, {"preset": "custom", "from": date_from.isoformat(), "to": date_to.isoformat()}


def _parse_report_payload_cache_key(source_key: str, report_id: str) -> tuple[date | None, date | None, str | None, str | None]:
    prefix = f"reports_payload_{report_id}_"
    if not source_key.startswith(prefix):
        return None, None, None, None
    tail = source_key[len(prefix):]
    if report_id == "abc":
        tail = re.sub(rf"^{re.escape(ABC_REPORT_PAYLOAD_VERSION)}_[0-9a-f]+_org\d+_", "", tail)
    if report_id == "pnl":
        version = f"{PNL_REPORT_PAYLOAD_VERSION}_"
        if not tail.startswith(version):
            return None, None, None, None
        tail = tail[len(version):]
    match = re.match(r"(?P<date_from>\d{4}-\d{2}-\d{2})_(?P<date_to>\d{4}-\d{2}-\d{2})_(?P<group_by>[^_]+)_(?P<source>.+)$", tail)
    if not match:
        return None, None, None, None
    try:
        return (
            date.fromisoformat(match.group("date_from")),
            date.fromisoformat(match.group("date_to")),
            match.group("group_by"),
            match.group("source"),
        )
    except ValueError:
        return None, None, None, None


def _latest_report_payload_cache(
    *,
    organization_id: int,
    report_id: str,
    group_by: str,
    source: str,
    date_from: date | None = None,
    date_to: date | None = None,
    finance_allowed: bool = False,
) -> tuple[dict[str, Any], date, date] | None:
    requested_from = date_from
    requested_to = date_to
    if requested_from is not None and requested_to is not None:
        exact = get_source_cache(
            organization_id,
            _report_cache_key(
                report_id,
                requested_from,
                requested_to,
                group_by,
                source,
                organization_id=organization_id,
                finance_allowed=finance_allowed,
            ),
            slim=False,
        ) or {}
        if _report_payload_cache_is_usable(report_id, exact, organization_id=organization_id, allow_stale=True):
            return exact, requested_from, requested_to
        return None
    scoped_source = f"{source}_{'finance' if finance_allowed else 'nofinance'}" if report_id in {"abc", "pnl", "week-over-week"} else source
    for cache in list_source_cache_by_prefix(organization_id, f"reports_payload_{report_id}_", limit=50, slim=False):
        source_key = str(cache.get("sourceKey") or "")
        fallback_from, fallback_to, cached_group_by, cached_source = _parse_report_payload_cache_key(source_key, report_id)
        if cached_group_by != group_by or cached_source != scoped_source:
            continue
        if not _report_payload_cache_is_usable(report_id, cache, organization_id=organization_id, allow_stale=True):
            continue
        cached_from, cached_to, _date_range = _date_range_from_report_cache(cache, fallback_from, fallback_to)
        if cached_from is None or cached_to is None:
            continue
        if requested_from is not None and cached_from != requested_from:
            continue
        if requested_to is not None and cached_to != requested_to:
            continue
        return cache, cached_from, cached_to
    return None


def _rule_metric_value(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("percent", value.get("value", value.get("units")))
        parsed = _float_value(value)
        if parsed is not None:
            return parsed
    return None


def _rule_metrics_from_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "abcCode": row.get("abcCode"),
        "ctrPct": _rule_metric_value(row, "ctrPct"),
        "crPct": _rule_metric_value(row, "crPct"),
        "cartToOrderPct": _rule_metric_value(row, "cartToOrderPct", "cartCrPct"),
        "buyoutPct": _rule_metric_value(row, "buyoutPct"),
        "marginPct": _rule_metric_value(row, "marginPct"),
        "drrPct": _rule_metric_value(row, "drrPct", "drrSalesPct", "drrOrdersPct"),
        "roiPct": _rule_metric_value(row, "roiPct", "romiPct"),
        "daysToOos": _rule_metric_value(row, "daysToOos"),
        "stockUnits": _rule_metric_value(row, "availableUnits", "wbStockUnits", "stockUnits"),
        "localizationPct": _rule_metric_value(row, "localizationPct"),
        "impressions": _rule_metric_value(row, "impressions", "views"),
    }
    if metrics["cartToOrderPct"] is None:
        orders_units = _rule_metric_value(row, "ordersUnits", "orders", "ordersComposite")
        baskets_units = _rule_metric_value(row, "baskets", "cartCount", "cartAdds", "adCartAdds")
        if orders_units is not None and baskets_units and baskets_units > 0:
            metrics["cartToOrderPct"] = orders_units / baskets_units * 100
    return metrics


def _apply_report_rules_to_payload(
    payload: dict[str, Any],
    organization_id: int,
    *,
    compact_abc: bool | None = None,
) -> dict[str, Any]:
    profile = load_active_profile(organization_id)
    result = _normalize_report_basket_fields(payload)
    if compact_abc is None:
        compact_abc = isinstance(result.get("meta"), dict) and result["meta"].get("id") == "abc"
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    # Current price columns are not period finance. Overlay them at the read
    # boundary so a 24-hour financial report cannot freeze an expired browser
    # quote. Keep historical order SPP/history and every monetary total intact.
    current_goods = None
    if (result.get("meta") or {}).get("id") == "abc" and (result.get("filters") or {}).get("groupBy", "sku") == "sku":
        from app.wb_browser_prices import CONNECTION_KEY
        if (get_source_cache(organization_id, CONNECTION_KEY) or {}).get("marketplaceAccountId"):
            current_goods = {str(good.get("nmID")): good for good in list_cached_goods(organization_id)}
    decorated: list[Any] = []
    oos_count = 0
    attention_count = 0
    for raw in rows:
        if not isinstance(raw, dict):
            decorated.append(raw)
            continue
        row = dict(raw)
        if current_goods is not None and row.get("nmId") and not row.get("isUnallocated"):
            from app.repricer_sync import _good_seller_price_kopecks, _good_buyer_price_no_wallet_kopecks
            good = current_goods.get(str(row["nmId"]), {})
            row["priceBeforeSppKopecks"] = _good_seller_price_kopecks(good)
            row["priceWithSppKopecks"] = _good_buyer_price_no_wallet_kopecks(good)
        evaluation = evaluate_metrics(_rule_metrics_from_row(row), profile).model_dump(mode="json")
        if evaluation["status"] == "risk":
            attention_count += 1
        if compact_abc:
            row["ruleStatusCode"] = evaluation["status"]
        else:
            row["ruleEvaluation"] = evaluation
        row["ruleStatus"] = {"unknown": "Недостаточно данных", "risk": "Риск", "opportunity": "Возможность", "normal": "Норма"}.get(evaluation["status"], evaluation["status"])
        row["ruleReasons"] = " · ".join(evaluation.get("statusReasons") or []) or "Пороговых рекомендаций нет"
        row["ruleRecommendation"] = ", ".join(
            f"{item.get('type')} · черновик" for item in evaluation.get("recommendedActions", []) if isinstance(item, dict)
        ) or "—"
        reasons = {item.get("reason") for item in evaluation.get("recommendedActions", []) if isinstance(item, dict)}
        if "oos" in reasons:
            oos_count += 1
        decorated.append({key: value for key, value in row.items() if value is not None} if compact_abc else row)
    result["rows"] = decorated
    result["rulesProfileVersion"] = profile.version
    result["rulesProfile"] = {"name": profile.name, "preset": profile.preset}
    filtered_summary = result.get("filteredSummary")
    if isinstance(filtered_summary, dict):
        result["filteredSummary"] = {**filtered_summary, "attentionCount": attention_count}
    meta = result.get("meta")
    if isinstance(meta, dict):
        result["meta"] = {**meta, "rulesProfileVersion": profile.version}
    kpis = result.get("kpis")
    if isinstance(kpis, list):
        if not any(isinstance(item, dict) and item.get("id") == "rules_profile" for item in kpis):
            kpis.append({"id": "rules_profile", "label": "Правила", "value": f"v{profile.version}", "hint": f"{profile.name} · {profile.preset}"})
        for item in kpis:
            if isinstance(item, dict) and item.get("id") == "oos_risk":
                item["value"] = str(oos_count)
    columns = result.get("columns")
    if isinstance(columns, list) and rows:
        existing = {item.get("key") for item in columns if isinstance(item, dict)}
        if "ruleStatus" not in existing:
            columns.extend([
                {"key": "ruleStatus", "label": "Статус правил"},
                {"key": "ruleReasons", "label": "Причина"},
                {"key": "ruleRecommendation", "label": "Рекомендация"},
            ])
    cards = result.get("managementCards")
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict) and card.get("chip") == "OOS риск":
                card["value"] = f"{oos_count} SKU"
                card["meta"] = (
                    f"до {profile.config.qualityBands.daysToOos.warnBelow:g} дней покрытия "
                    f"или меньше {profile.config.qualityBands.stockUnits.criticalBelow:g} доступных единиц"
                )
                card["className"] = "danger" if oos_count else ""
    return result


def _completed_report_job_from_cache(
    report_id: str,
    date_from: date,
    date_to: date,
    group_by: str,
    cache: dict[str, Any],
    current_job: dict[str, Any] | None = None,
    *,
    preserve_finished_refresh: bool = True,
) -> dict[str, Any]:
    if current_job and (
        _report_job_is_reusable(current_job)
        or (preserve_finished_refresh and _report_job_is_finished_refresh(current_job))
    ):
        return dict(current_job)
    return {
        **(current_job or {}),
        "state": "completed",
        "reportId": report_id,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "groupBy": group_by,
        "stage": "cache",
        "label": f"Отчёт взят из готового кэша выбранного периода {date_from.isoformat()} — {date_to.isoformat()}",
        "percent": 100,
        "updatedAt": _utc_now_iso(),
        "reused": True,
        "cacheFresh": True,
        "cacheAgeSeconds": _report_payload_cache_age_seconds(cache),
        "cacheTtlSeconds": int(REPORT_PAYLOAD_CACHE_TTL.total_seconds()),
    }


def _report_job_is_reusable(job: dict[str, Any]) -> bool:
    state = job.get("state")
    if state not in {"queued", "running", "waiting_1c"}:
        return False
    heartbeat = (
        _parse_report_job_timestamp(job.get("updatedAt"))
        or _parse_report_job_timestamp(job.get("queuedAt"))
        or _parse_report_job_timestamp(job.get("startedAt"))
    )
    if heartbeat is None:
        return False
    if state == "queued":
        stale_after = BACKGROUND_REPORT_QUEUED_STALE_AFTER
        # A Statistics job can wait behind minute-limited WB collection in the
        # shared read-only worker. Keep a bound, but not the 30s launch timeout.
        if job.get("reportId") in {"abc", "pnl", "rnp", "week-over-week"} or job.get("reportId") == "stats" and (
            job.get("source") in {"operational", "statistics-comparison", "catalog"}
            or str(job.get("source") or "").startswith("catalog:")
        ):
            stale_after = BACKGROUND_REPORT_JOB_STALE_AFTER
        if job.get("stage") == "rate_limit":
            try:
                delay = max(0, int(job.get("retryAfterSeconds") or 0))
                stale_after += timedelta(seconds=delay)
            except (ValueError, TypeError, OverflowError):
                pass
        return datetime.now(timezone.utc) - heartbeat <= stale_after
    stale_after = BACKGROUND_REPORT_JOB_STALE_AFTER
    if job.get("reportId") == "week-over-week" and str(job.get("stage") or "").startswith("week-over-week-ads"):
        stale_after = timedelta(minutes=5)
    return datetime.now(timezone.utc) - heartbeat <= stale_after


def _report_job_is_active_refresh(job: dict[str, Any]) -> bool:
    state = str(job.get("state") or "")
    stage = str(job.get("stage") or "")
    return state in ACTIVE_REPORT_JOB_STATES and (stage in ACTIVE_REPORT_JOB_STAGES or job.get("kind") == "report_source_refresh") and _report_job_is_reusable(job)


def _report_job_is_finished_refresh(job: dict[str, Any]) -> bool:
    state = str(job.get("state") or "")
    stage = str(job.get("stage") or "")
    kind = str(job.get("kind") or "")
    return state in {"completed", "failed", "paused"} and stage in {"completed", "failed", "paused", "partial"} and kind == "report_source_refresh"


def _report_job_for_response(job: dict[str, Any], *, cache_missing: bool = False) -> dict[str, Any]:
    if not isinstance(job, dict) or not job:
        return job
    state = job.get("state")
    if cache_missing and state == "completed":
        return {**job, "state": "idle", "stage": "stale", "cacheFresh": False,
                "percent": 0, "label": "Данные отчёта изменились. Требуется обновить расчёт."}
    if state == "waiting_1c" and not job.get("taskId"):
        return job  # The expenses view can wait for 1C without a background worker.
    if state not in {"queued", "running", "waiting_1c"} or _report_job_is_reusable(job):
        return job
    return {
        **job,
        "state": "stale",
        "previousState": state,
        "staleAfterSeconds": int(BACKGROUND_REPORT_JOB_STALE_AFTER.total_seconds()),
        "label": f"Задача зависла: {job.get('label') or 'нет свежего heartbeat'}",
    }


def _empty_background_report(report_id: ReportId, date_range: dict[str, str], group_by: str, job: dict[str, Any]) -> dict[str, Any]:
    job = _report_job_for_response(job, cache_missing=True)
    return {
        "meta": _meta(report_id, report_id, "Отчёт ожидает фоновое обновление.", "operational", "stale"),
        "headline": "Отчёт ещё не собран для выбранного периода.",
        "filters": {"dateRange": date_range, "groupBy": group_by},
        "kpis": [], "chart": {"title": "Нет данных", "valueLabel": "-", "points": []}, "columns": [], "rows": [],
        "cache": {"status": "missing", "requestedRange": date_range}, "reportJob": job,
    }


def _digest_plan_cache_key(month: str) -> str:
    return f"reports_digest_plan_{month}"


def _apply_digest_plan(payload: dict[str, Any], plan: dict[str, Any]) -> None:
    company = plan.get("company") if isinstance(plan.get("company"), dict) else {}
    managers = plan.get("managers") if isinstance(plan.get("managers"), list) else []
    def kpi_value(kpi_id: str) -> int | None:
        item = next((candidate for candidate in payload.get("kpis", []) if candidate.get("id") == kpi_id), None)
        if not isinstance(item, dict):
            return None
        try:
            return int(item.get("value"))
        except (TypeError, ValueError):
            return None

    margin_fact = kpi_value("margin_profit")
    revenue_fact = kpi_value("sales_revenue")
    rows: list[dict[str, Any]] = []
    manager_count = max(len([item for item in managers if isinstance(item, dict)]), 1)
    month = str(plan.get("month") or "")
    try:
        plan_month = datetime.strptime(f"{month}-01", "%Y-%m-%d").date()
        days_in_month = ((plan_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        as_of = date.fromisoformat(str((payload.get("dateRange") or {}).get("to") or ""))
        elapsed_days = min(days_in_month, max(1, as_of.day if as_of.year == plan_month.year and as_of.month == plan_month.month else days_in_month))
    except ValueError:
        days_in_month = 30
        elapsed_days = 1

    def row(owner_id: str, name: str, values: dict[str, Any], margin_fact_value: int | None, revenue_fact_value: int | None) -> dict[str, Any]:
        margin_plan = int(values.get("marginPlanKopecks") or 0)
        revenue_plan = int(values.get("revenuePlanKopecks") or 0)
        metric_label = "Маржа" if margin_plan else "Выручка"
        plan_value = margin_plan or revenue_plan
        fact = margin_fact_value if margin_plan else revenue_fact_value
        forecast = round(fact / elapsed_days * days_in_month) if fact is not None else None
        remaining_days = max(1, days_in_month - elapsed_days)
        return {"owner": "company" if owner_id == "company" else "manager", "ownerId": owner_id, "name": name, "metricLabel": metric_label, "planKopecks": plan_value or None, "factKopecks": fact, "completionPct": round(fact / plan_value * 100, 1) if plan_value and fact is not None else None, "forecastKopecks": forecast, "needPerDayKopecks": max(0, plan_value - fact) // remaining_days if plan_value and fact is not None else None, "status": "no_plan" if not plan_value else "no_fact" if fact is None else "ok" if forecast is not None and forecast >= plan_value else "risk", "factFreshness": "preliminary", "revenuePlanKopecks": revenue_plan or None, "revenueFactKopecks": revenue_fact_value}
    rows.append(row("company", "Компания", company, margin_fact, revenue_fact))
    active = [item for item in managers if isinstance(item, dict)]
    for index, manager in enumerate(active):
        manager_margin_fact = margin_fact // manager_count if margin_fact is not None else None
        manager_revenue_fact = revenue_fact // manager_count if revenue_fact is not None else None
        rows.append(row(str(manager.get("id") or f"manager-{index}"), str(manager.get("name") or "Менеджер"), manager, manager_margin_fact, manager_revenue_fact))
    payload["planFactRows"] = rows


def _empty_digest_payload(date_range: dict[str, str]) -> dict[str, Any]:
    return {
        "meta": _meta("digest", "WB-витрина", "Дайджест ожидает фоновое обновление.", "operational", "stale"),
        "headline": "Дайджест ещё не собран для выбранного периода.", "dateRange": date_range,
        "kpis": [], "planFactRows": [], "freshness": [], "alerts": [], "charts": [], "periodCards": [], "problemRows": [], "quickLinks": [],
    }


def _build_digest_ads_snapshot(*, organization_id: int, date_from: date, date_to: date, wb_token: str | None) -> AdsAttributionSnapshot:
    return build_cached_ads_attribution_snapshot(
        organization_id=organization_id,
        date_from=date_from,
        date_to=date_to,
        group_by="sku",
    )


@router.get("/api/wb/reports/digest")
def get_reports_digest(
    request: Request,
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    brand_options: bool = Query(default=False, alias="brandOptions"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.bff.digest.get",
        object_type="wb_report",
        object_id="digest",
        reason="actor cannot read digest report",
    )
    date_from, date_to, date_range = _range_from_preset(preset, from_, to)
    if isinstance(brand, list) and brand:
        from app.wb_sales_brand_filter import project_sales_brands
        return project_sales_brands(actor, date_from, date_to, brand, finance_allowed=has_permission(actor, "finance:read"))
    cached = get_source_cache(actor.organization_id, _digest_cache_key(date_from, date_to), slim=False) or {}
    digest = cached.get("digest") if isinstance(cached.get("digest"), dict) else None
    if isinstance(digest, dict) and digest.get("cacheVersion") != DIGEST_REPORT_PAYLOAD_VERSION:
        digest = None
    cache_status = "exact" if digest else "missing"
    selected_cache = cached if digest else {}
    if digest is None:
        latest = get_source_cache(actor.organization_id, "reports_digest_latest", slim=False) or {}
        digest = latest.get("digest") if isinstance(latest.get("digest"), dict) else None
        if isinstance(digest, dict) and digest.get("cacheVersion") != DIGEST_REPORT_PAYLOAD_VERSION:
            digest = None
        cache_status = "fallback" if digest else "missing"
        selected_cache = latest if digest else {}
    payload = dict(digest or _empty_digest_payload(date_range))
    plan = get_source_cache(actor.organization_id, _digest_plan_cache_key(date_to.strftime("%Y-%m")), slim=False) or {}
    if isinstance(plan, dict) and plan:
        _apply_digest_plan(payload, plan)
    job = get_source_cache(actor.organization_id, _digest_job_cache_key(date_from, date_to), slim=False) or {"state": "idle"}
    cache_age_seconds = _digest_cache_age_seconds(selected_cache)
    cache_fresh = cache_status == "exact" and _digest_cache_is_fresh(selected_cache, actor.organization_id)
    if cache_status == "exact" and cache_fresh and not (_report_job_is_reusable(job) or _report_job_is_finished_refresh(job)):
        job = {
            **job,
            "state": "completed",
            "stage": "cache",
            "label": "Воронка продаж взята из кэша за последние 24 часа",
            "percent": 100,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "updatedAt": _utc_now_iso(),
            "reused": True,
            "cacheFresh": True,
            "cacheAgeSeconds": cache_age_seconds,
            "cacheTtlSeconds": int(DIGEST_CACHE_TTL.total_seconds()),
        }
        save_source_cache(actor.organization_id, _digest_job_cache_key(date_from, date_to), job)
    payload["cache"] = {
        "status": cache_status,
        "requestedRange": date_range,
        "fresh": cache_fresh,
        "ageSeconds": cache_age_seconds,
        "ttlSeconds": int(DIGEST_CACHE_TTL.total_seconds()),
        "completedAt": selected_cache.get("completedAt"),
        "fetchedAt": selected_cache.get("fetchedAt"),
    }
    payload["digestJob"] = _report_job_for_response(job)
    record_audit_event(
        actor=actor,
        action="reports.bff.digest.get",
        object_type="wb_report",
        object_id="digest",
        before_state=None,
        after_state={"sourceStatus": payload["meta"]["freshnessState"], "cache": cache_status},
        reason="read bff digest report",
        approval_ref=None,
        evidence_refs=["WB-03", "WB-11_CLOSED_2026-05-29", "WB-02_CLOSED_2026-06-01"],
    )
    if brand_options is True:
        from app.wb_sales_brand_filter import available_brands
        payload["availableBrands"] = available_brands(actor.organization_id)
    return payload


def _digest_plan_managers(organization_id: int) -> dict[str, str]:
    """Managers are organization users, never presentation-only frontend names."""
    return {
        user.userId: user.fullName
        for user in list_team_users(organization_id)
        if user.isActive and user.permissionProfile not in {"viewer", "admin"}
    }


def _normalize_digest_plan(raw: dict[str, Any], *, allowed_managers: dict[str, str] | None = None) -> dict[str, Any]:
    month = str(raw.get("month") or "")
    try:
        datetime.strptime(f"{month}-01", "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must have YYYY-MM format") from exc

    def values(item: Any) -> dict[str, int]:
        item = item if isinstance(item, dict) else {}
        result: dict[str, int] = {}
        for key in ("revenuePlanKopecks", "marginPlanKopecks"):
            try:
                amount = int(item.get(key) or 0)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"{key} must be a non-negative integer") from exc
            if amount < 0:
                raise HTTPException(status_code=422, detail=f"{key} must be a non-negative integer")
            result[key] = amount
        return result

    managers: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("managers") if isinstance(raw.get("managers"), list) else []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        manager_id = str(item.get("id") or f"manager-{index}")
        if allowed_managers is not None:
            if manager_id not in allowed_managers:
                continue
            name = allowed_managers[manager_id]
        managers.append({"id": manager_id, "name": name, **values(item)})
    return {"month": month, "company": values(raw.get("company")), "managers": managers, "updatedAt": _utc_now_iso()}


@router.get("/api/wb/reports/rules")
def get_report_rules(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.rules.get",
        object_type="wb_report_rule_profile",
        object_id=str(actor.organization_id),
        reason="actor cannot read report rules",
    )
    profile = load_active_profile(actor.organization_id)
    return {
        "profile": _rules_profile_payload(profile),
        "presets": {name: config.model_dump(mode="json") for name, config in PRESET_CONFIGS.items()},
        "automationActions": ["raise_price", "lower_price", "rnp", "liquidation", "stop_ads", "alert", "audit"],
        "canWrite": has_permission(actor, "settings:write"),
    }


@router.get("/api/wb/reports/rules/history")
def get_report_rules_history(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.rules.history",
        object_type="wb_report_rule_profile",
        object_id=str(actor.organization_id),
        reason="actor cannot read report rules history",
    )
    return {"items": [_rules_profile_payload(item) for item in list_profile_history(actor.organization_id, limit=limit)]}


@router.post("/api/wb/reports/rules/preview")
def preview_report_rules(request: Request, draft: RulesDraftRequest = Body(...)) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:write",
        action="reports.rules.preview",
        object_type="wb_report_rule_profile",
        object_id=str(actor.organization_id),
        reason="actor cannot preview report rules",
    )
    active = load_active_profile(actor.organization_id)
    if active.version != draft.expectedVersion:
        raise HTTPException(status_code=409, detail={"code": "RULES_VERSION_CONFLICT", "currentVersion": active.version})
    try:
        normalized = normalize_config(draft.config)
    except ReportRulesValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc
    rows, available_reports = _report_rules_preview_rows(actor.organization_id, finance_allowed=has_permission(actor, "finance:read"))
    preview = create_preview(rows, active, normalized)
    return {
        **preview,
        "availableReports": available_reports,
        "previewToken": issue_preview_token(actor.organization_id, active.version, normalized),
    }


@router.put("/api/wb/reports/rules")
def save_report_rules(request: Request, draft: RulesSaveRequest = Body(...)) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:write",
        action="reports.rules.save",
        object_type="wb_report_rule_profile",
        object_id=str(actor.organization_id),
        reason="actor cannot save report rules",
    )
    active = load_active_profile(actor.organization_id)
    try:
        normalized = normalize_config(draft.config)
        token_payload = verify_preview_token(draft.previewToken, actor.organization_id, draft.expectedVersion, normalized)
        saved = activate_profile(
            actor.organization_id,
            actor.user_id,
            draft.expectedVersion,
            draft.preset,
            draft.name.strip(),
            normalized,
        )
    except ReportRulesValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from exc
    except (PreviewTokenError, ProfileVersionConflict) as exc:
        current_version = load_active_profile(actor.organization_id).version
        raise HTTPException(status_code=409, detail={"code": "RULES_VERSION_CONFLICT", "message": str(exc), "currentVersion": current_version}) from exc
    record_audit_event(
        actor=actor,
        action="reports.rules.activate",
        object_type="wb_report_rule_profile",
        object_id=f"{actor.organization_id}:{saved.version}",
        before_state={"version": active.version, "preset": active.preset},
        after_state={"version": saved.version, "preset": saved.preset},
        reason="report rules activated after preview",
        approval_ref=str(token_payload.get("configHash")),
        evidence_refs=[f"rules-preview:{token_payload.get('configHash')}"],
    )
    return {"profile": _rules_profile_payload(saved)}


@router.get("/api/wb/reports/digest/plan")
def get_digest_plan(request: Request, month: str = Query(...)) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.bff.digest.plan.get", object_type="wb_report", object_id="digest-plan", reason="actor cannot read digest plan")
    managers = _digest_plan_managers(actor.organization_id)
    normalized = _normalize_digest_plan({"month": month}, allowed_managers=managers)
    stored = get_source_cache(actor.organization_id, _digest_plan_cache_key(normalized["month"]), slim=False) or normalized
    if not isinstance(stored, dict):
        stored = normalized
    # Reconcile saved values with the current team: removed users disappear, new users appear with zero values.
    saved_by_id = {str(item.get("id")): item for item in stored.get("managers", []) if isinstance(item, dict)}
    def saved_values(item: Any) -> dict[str, int]:
        item = item if isinstance(item, dict) else {}
        return {
            key: max(0, int(item.get(key) or 0))
            for key in ("revenuePlanKopecks", "marginPlanKopecks")
        }

    stored["managers"] = [
        {"id": manager_id, "name": name, **saved_values(saved_by_id.get(manager_id))}
        for manager_id, name in managers.items()
    ]
    return stored


@router.post("/api/wb/reports/digest/plan")
def save_digest_plan(request: Request, raw_plan: dict[str, Any] = Body(...)) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:write", action="reports.bff.digest.plan.save", object_type="wb_report", object_id="digest-plan", reason="actor cannot save digest plan")
    plan = _normalize_digest_plan(raw_plan, allowed_managers=_digest_plan_managers(actor.organization_id))
    save_source_cache(actor.organization_id, _digest_plan_cache_key(plan["month"]), plan)
    return plan


@router.post("/api/wb/reports/work-demand/release")
def release_report_screen(request: Request) -> dict[str, bool]:
    actor = actor_from_request(request)
    release_demand(request, actor)
    return {"released": True}


@router.post("/api/wb/reports/digest/refresh")
def refresh_reports_digest(request: Request, preset: str = Query(default="7d"), from_: str | None = Query(default=None, alias="from"), to: str | None = Query(default=None)) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.bff.digest.refresh", object_type="wb_report", object_id="digest", reason="actor cannot refresh digest report")
    date_from, date_to, _ = _range_from_preset(preset, from_, to)
    key = _digest_job_cache_key(date_from, date_to)
    touch_demand(request, actor, key, start=True)
    current = get_source_cache(actor.organization_id, key, slim=False) or {}
    if _report_job_is_reusable(current):
        return {**current, "reused": True}
    exact = get_source_cache(actor.organization_id, _digest_cache_key(date_from, date_to), slim=False) or {}
    exact_age_seconds = _digest_cache_age_seconds(exact)
    source_from, source_to = _digest_source_bounds(date_from, date_to)
    sources_fresh, _ = _report_daily_sources_ready(actor.organization_id, REPORT_DAILY_SOURCES_BY_ID["digest"], date_from=source_from, date_to=source_to, fresh_only=True)
    if _digest_cache_is_fresh(exact, actor.organization_id) and sources_fresh:
        payload = {
            **current,
            "state": "completed",
            "stage": "cache",
            "label": "Воронка продаж взята из кэша за последние 24 часа",
            "percent": 100,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "updatedAt": _utc_now_iso(),
            "reused": True,
            "cacheFresh": True,
            "cacheAgeSeconds": exact_age_seconds,
            "cacheTtlSeconds": int(DIGEST_CACHE_TTL.total_seconds()),
        }
        save_source_cache(actor.organization_id, key, payload)
        return payload
    from app.repricer_tasks import build_digest_for_org, refresh_report_sources_for_org
    source_from, source_to = _digest_source_bounds(date_from, date_to)
    ready, _ = _report_daily_sources_ready(actor.organization_id, REPORT_DAILY_SOURCES_BY_ID["digest"], date_from=source_from, date_to=source_to, fresh_only=True)
    if ready:
        task = build_digest_for_org.delay(actor.organization_id, date_from.isoformat(), date_to.isoformat(), has_permission(actor, "finance:read"), None)
    else:
        task = refresh_report_sources_for_org.delay(actor.organization_id, actor.user_id, "digest",
            date_from.isoformat(), date_to.isoformat(), "sku", "operational", has_permission(actor, "finance:read"), None,
            refresh_missing_only=True)
    payload = {"state": "queued", "taskId": task.id, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "queuedAt": _utc_now_iso()}
    save_source_cache(actor.organization_id, key, payload)
    return {**payload, "reused": False}


@router.get("/api/wb/reports/digest/status")
def get_reports_digest_status(request: Request, preset: str = Query(default="7d"), from_: str | None = Query(default=None, alias="from"), to: str | None = Query(default=None)) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.bff.digest.status", object_type="wb_report", object_id="digest", reason="actor cannot read digest job")
    date_from, date_to, _ = _range_from_preset(preset, from_, to)
    touch_demand(request, actor, _digest_job_cache_key(date_from, date_to))
    job = get_source_cache(actor.organization_id, _digest_job_cache_key(date_from, date_to), slim=False) or {"state": "idle", "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat()}
    return _report_job_for_response(job)


def _saved_preview_revision(organization_id: int, date_from: date, date_to: date) -> str:
    from app.repricer_page_finance import VERSION as finance_version
    observations = []
    for prefix in ("finance_", "repricer_finance_", "ads_", "baskets_", "period_stats_", "funnel_seller_"):
        for meta in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100):
            if (_parse_cache_date(meta.get("dateTo")) or date.min) < date_from:
                continue
            observations.append((meta.get("sourceKey"), meta.get("fetchedAt")))
    for key in ("stocks", "content_cards"):
        observations.append((key, get_source_cache_fetched_at(organization_id, key)))
    return sha1(repr((finance_version, sorted(observations), legacy_finance_tax_revision(organization_id),
        _abc_economics_version(organization_id), get_settings().one_c_enabled)).encode()).hexdigest()


_saved_previews: OrderedDict = OrderedDict()
_saved_previews_lock = RLock()


def _saved_report_preview(*, actor, report_id, date_from, date_to, group_by, finance_allowed, source="operational"):
    if report_id not in {"abc", "pnl", "rnp", "ads", "stock", "expenses"}:
        return None
    key = _report_cache_key(report_id, date_from, date_to, group_by, source,
        organization_id=actor.organization_id, finance_allowed=finance_allowed) + "_saved_preview"
    revision = _saved_preview_revision(actor.organization_id, date_from, date_to)
    if report_id == "pnl":
        from app.wb_pnl_costs import current_cost_revision
        revision += current_cost_revision(actor.organization_id)
    cache_id = (actor.organization_id, key, bool(finance_allowed))
    with _saved_previews_lock:
        cached = _saved_previews.get(cache_id)
        if cached and cached[0] == revision and time.monotonic() - cached[1] < 60:
            _saved_previews.move_to_end(cache_id)
            return _with_storage_presentation(deepcopy(cached[2]))
    calculation_revision = legacy_finance_tax_revision(actor.organization_id)
    payload = _saved_report_preview_uncached(actor=actor, report_id=report_id, date_from=date_from,
        date_to=date_to, group_by=group_by, finance_allowed=finance_allowed, source=source)
    current_revision = _saved_preview_revision(actor.organization_id, date_from, date_to)
    if report_id == "pnl":
        current_revision += current_cost_revision(actor.organization_id)
    if payload and revision != current_revision:
        raise HTTPException(status_code=409, detail="WB_CALCULATION_INPUTS_CHANGED")
    if payload and revision == current_revision:
        payload.setdefault("cache", {})["calculationRevision"] = calculation_revision
        # A cache-only GET never rewrites source observations or job state.
        with _saved_previews_lock:
            _saved_previews[cache_id] = (revision, time.monotonic(), deepcopy(payload))
            _saved_previews.move_to_end(cache_id)
            while len(_saved_previews) > 16:
                _saved_previews.popitem(last=False)
    return payload


@tax_consistent_report
def _saved_report_preview_uncached(*, actor, report_id, date_from, date_to, group_by, finance_allowed, source="operational"):
    """Render saved observations without waiting for the provider queue.

    This is deliberately not a completed-job/cache acknowledgement. Missing
    source days and financial blockers remain visible and collection continues.
    All builders below are cache-only (no marketplace credentials).
    """
    date_range = {"preset": "custom", "from": date_from.isoformat(), "to": date_to.isoformat()}
    common = dict(date_from=date_from, date_to=date_to, group_by=group_by,
                  finance_allowed=finance_allowed, organization_id=actor.organization_id)
    if report_id == "expenses":
        payload = _map_cash_flow_to_expenses_response(
            {"status": "not_loaded" if get_settings().one_c_enabled else "disabled"}, date_range, group_by)
    elif report_id == "rnp":
        payload = _map_rnp_to_report_response(build_rnp_report(**common, wb_token=None), date_range)
    elif report_id == "abc":
        payload = _map_abc_to_report_response(build_abc_report(**common, filters=""), date_range)
    elif report_id == "pnl":
        # Do not enqueue 1C jobs from a cache read, including viewers without finance permission.
        cash_flow = {"status": "not_loaded" if get_settings().one_c_enabled else "disabled"}
        payload = _map_pnl_to_report_response(build_pnl_report(**common, requested_state="preliminary", wb_token=None),
            date_range, cash_flow, finance_allowed=finance_allowed, financial_only=source == "financial")
    elif report_id == "ads":
        snapshot = build_cached_ads_attribution_snapshot(organization_id=actor.organization_id,
            date_from=date_from, date_to=date_to, group_by="campaign")
        payload = _map_ads_to_report_response(date_range, snapshot)
    elif report_id == "stock":
        snapshot = build_cached_wb_reports_sources_snapshot(organization_id=actor.organization_id, date_from=date_from, date_to=date_to)
        payload = _build_stock_report_payload(date_range, snapshot, organization_id=actor.organization_id, wb_token=None, read_only=True)
    else:
        return None
    if not payload.get("rows") and report_id != "expenses":
        return None
    payload = _apply_report_rules_to_payload(payload, actor.organization_id)
    payload["meta"] = {**payload.get("meta", {}), "freshnessState": "partial"}
    payload["cache"] = {"status": "saved-preview", "fresh": False, "requestedRange": date_range}
    payload["warning"] = "Показана сохранённая часть выбранного периода. Дозагрузка WB выполняется отдельно. " + (payload.get("warning") or "")
    return payload


def _saved_digest_preview(*, actor: Any, date_from: date, date_to: date, finance_allowed: bool) -> dict[str, Any] | None:
    """Read only the requested days; never borrow another range or start a job."""
    from types import SimpleNamespace
    from app.repricer_tasks import _digest_report_summary
    snapshot = build_cached_wb_reports_sources_snapshot(organization_id=actor.organization_id,
        date_from=date_from, date_to=date_to)
    funnel = _build_digest_funnel_snapshot(organization_id=actor.organization_id,
        date_from=date_from, date_to=date_to, wb_token=None)
    if not (snapshot.orders or snapshot.sales or funnel.get("rows") or funnel.get("dailyRows")):
        return None
    ads = build_cached_ads_attribution_snapshot(organization_id=actor.organization_id,
        date_from=date_from, date_to=date_to, group_by="sku")
    summary = _digest_report_summary(actor.organization_id, date_from, date_to)
    if not finance_allowed:
        summary = {**summary, "marginKopecks": None}
    date_range = {"preset": "custom", "from": date_from.isoformat(), "to": date_to.isoformat()}
    payload = _build_digest_payload(date_range, snapshot, ads,
        SimpleNamespace(rows=[], blockerIds=["WB-14A"]), funnel, summary)
    plan = get_source_cache(actor.organization_id, _digest_plan_cache_key(date_to.strftime("%Y-%m")), slim=False) or {}
    if plan:
        _apply_digest_plan(payload, plan)
    payload["meta"] = {**payload.get("meta", {}), "freshnessState": "partial"}
    payload["cache"] = {"status": "saved-preview", "fresh": False, "requestedRange": date_range}
    payload["warning"] = "Показана сохранённая часть выбранного периода. Дозагрузка WB выполняется отдельно."
    for item in payload.get("freshness", []):
        item.update(state="partial", updatedAt=None)
    return payload


@router.get("/api/wb/reports/{report_id}/latest-cache")
@tax_consistent_report
def get_reports_latest_cache(
    request: Request,
    report_id: Literal["digest", "abc", "rnp", "ads", "pnl", "expenses", "stock", "week-over-week"],
    groupBy: ReportGroupBy = Query(default="sku"),
    source: str = Query(default="operational"),
    preset: str = Query(default="latest"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    brand: list[str] | None = Query(default=None),
    brand_options: bool = Query(default=False, alias="brandOptions"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="finance:read" if report_id == "expenses" else "settings:read",
        action="reports.bff.latest_cache.get",
        object_type="wb_report",
        object_id=report_id,
        reason="actor cannot read latest report cache",
    )
    if report_id == "digest":
        if isinstance(brand, list) and brand:
            from app.wb_sales_brand_filter import project_sales_brands
            start, end, _ = _range_from_preset(preset if preset != "latest" else "7d", from_, to)
            return project_sales_brands(actor, start, end, brand, finance_allowed=has_permission(actor, "finance:read"))
        requested_from: date | None = None
        requested_to: date | None = None
        if preset != "latest" or from_ or to:
            requested_from, requested_to, _ = _range_from_preset(preset, from_, to)
        if requested_from is not None and requested_to is not None:
            candidates = [get_source_cache(actor.organization_id, _digest_cache_key(requested_from, requested_to), slim=False) or {}]
        else:
            candidates = [get_source_cache(actor.organization_id, "reports_digest_latest", slim=False) or {}]
            candidates.extend(
                cache
                for cache in list_source_cache_by_prefix(actor.organization_id, "reports_digest_", limit=50, slim=False)
                if re.match(rf"^reports_digest_{re.escape(DIGEST_REPORT_PAYLOAD_VERSION)}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{4}}-\d{{2}}-\d{{2}}$", str(cache.get("sourceKey") or ""))
            )
        for cache in candidates:
            if not isinstance(cache.get("digest"), dict) or cache["digest"].get("cacheVersion") != DIGEST_REPORT_PAYLOAD_VERSION:
                continue
            digest = cache.get("digest") if isinstance(cache.get("digest"), dict) else None
            if digest is None:
                continue
            date_from, date_to, date_range = _date_range_from_report_cache(cache)
            if date_from is None or date_to is None or date_range is None:
                continue
            if requested_from is not None and requested_to is not None and (date_from != requested_from or date_to != requested_to):
                continue
            payload = dict(digest)
            source_from, source_to = _digest_source_bounds(date_from, date_to)
            sources_fresh, _ = _report_daily_sources_ready(actor.organization_id, REPORT_DAILY_SOURCES_BY_ID["digest"], date_from=source_from, date_to=source_to, fresh_only=True)
            payload["cache"] = {
                "status": "latest",
                "requestedRange": date_range,
                "fresh": sources_fresh and _digest_cache_is_fresh(cache, actor.organization_id),
                "ageSeconds": _digest_cache_age_seconds(cache),
                "ttlSeconds": int(DIGEST_CACHE_TTL.total_seconds()),
                "completedAt": cache.get("completedAt"),
                "fetchedAt": cache.get("fetchedAt"),
            }
            job = get_source_cache(actor.organization_id, _digest_job_cache_key(date_from, date_to), slim=False) or {}
            payload["digestJob"] = _report_job_for_response(job) if job else {
                "state": "completed",
                "stage": "cache",
                "label": "Воронка продаж взята из последнего свежего кэша",
                "percent": 100,
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "updatedAt": _utc_now_iso(),
                "reused": True,
                "cacheFresh": True,
                "cacheAgeSeconds": _digest_cache_age_seconds(cache),
                "cacheTtlSeconds": int(DIGEST_CACHE_TTL.total_seconds()),
            }
            if brand_options is True:
                from app.wb_sales_brand_filter import available_brands
                payload["availableBrands"] = available_brands(actor.organization_id)
            return payload
        if requested_from is not None and requested_to is not None:
            preview = _saved_digest_preview(actor=actor, date_from=requested_from, date_to=requested_to,
                finance_allowed=has_permission(actor, "finance:read"))
            if preview is not None:
                if brand_options is True:
                    from app.wb_sales_brand_filter import available_brands
                    preview["availableBrands"] = available_brands(actor.organization_id)
                return preview
        raise HTTPException(status_code=404, detail="REPORT_LATEST_CACHE_MISSING")

    finance_allowed = has_permission(actor, "finance:read")
    requested_from = None
    requested_to = None
    if preset != "latest" or from_ or to:
        requested_from, requested_to, _ = _range_from_preset(preset, from_, to)

    latest = _latest_report_payload_cache(
        organization_id=actor.organization_id,
        report_id=report_id,
        group_by=groupBy,
        source=source,
        date_from=requested_from,
        date_to=requested_to,
        finance_allowed=finance_allowed,
    )
    if latest is None:
        if report_id in {"abc", "pnl"} and requested_from is not None and requested_to is not None:
            # Local setting changes invalidate derived results, not WB receipts.
            # Rebuild a requested preview from saved sources without a job or
            # provider call; unknown coverage stays explicit in that preview.
            preview = _saved_report_preview(actor=actor, report_id=report_id,
                date_from=requested_from, date_to=requested_to, group_by=groupBy,
                finance_allowed=finance_allowed, source=source)
            if preview is not None:
                return preview
        if report_id in {"abc", "pnl", "rnp", "week-over-week"}:
            # A cache read must not assemble raw ledgers / both weeks inside
            # the gateway request. The idempotent jobs endpoint builds and
            # publishes DB-only previews before collecting missing WB data.
            raise HTTPException(status_code=404, detail="REPORT_LATEST_CACHE_MISSING")
        if requested_from is not None and requested_to is not None:
            preview = _saved_report_preview(actor=actor, report_id=report_id,
                date_from=requested_from, date_to=requested_to, group_by=groupBy, finance_allowed=finance_allowed)
            if preview is not None:
                return preview
        raise HTTPException(status_code=404, detail="REPORT_LATEST_CACHE_MISSING")
    cache, date_from, date_to = latest
    report = cache.get("report") if isinstance(cache.get("report"), dict) else None
    if report is None:
        raise HTTPException(status_code=404, detail="REPORT_LATEST_CACHE_MISSING")
    date_range = {"preset": "custom", "from": date_from.isoformat(), "to": date_to.isoformat()}
    job = get_source_cache(
        actor.organization_id,
        _report_job_cache_key(
            report_id,
            date_from,
            date_to,
            groupBy,
            source,
            finance_allowed=finance_allowed,
        ),
        slim=False,
    ) or {}
    payload = _apply_report_rules_to_payload(report, actor.organization_id, compact_abc=False) if report_id == "abc" else dict(report)
    if report_id == "ads":
        payload = enrich_advertising_products(payload, _catalog_meta_by_nm(actor.organization_id, include_shared_facts=False))
    payload["cache"] = _report_payload_cache_meta(cache, date_range, "latest")
    source_from, source_to = _report_source_bounds(report_id, date_from, date_to)
    sources_fresh, _ = _report_daily_sources_ready(actor.organization_id, REPORT_DAILY_SOURCES_BY_ID.get(report_id, ()), date_from=source_from, date_to=source_to, fresh_only=True, report_id=report_id, selected_date_from=date_from if report_id == "week-over-week" else None, finance_allowed=finance_allowed)
    payload["cache"]["fresh"] = payload["cache"]["fresh"] and sources_fresh and _report_payload_cache_is_usable(report_id, cache, organization_id=actor.organization_id)
    if report_id in {"abc", "pnl", "rnp", "week-over-week"} and not payload["cache"]["fresh"]:
        payload["meta"] = {**payload.get("meta", {}), "freshnessState": "partial"}
        payload["reportJob"] = _report_job_for_response(job) if job else {"state": "idle"}
    else:
        payload["reportJob"] = _completed_report_job_from_cache(report_id, date_from, date_to, groupBy, cache, job)
    return _with_storage_presentation(payload) if report_id in {"abc", "pnl"} else payload


@router.get("/api/wb/reports/alerts")
def get_reports_alerts(
    request: Request,
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.bff.alerts.get",
        object_type="wb_report",
        object_id="alerts",
        reason="actor cannot read report alerts",
    )
    date_from, date_to, _date_range = _range_from_preset(preset, from_, to)
    source_snapshot = build_cached_wb_reports_sources_snapshot(organization_id=actor.organization_id, date_from=date_from, date_to=date_to)
    items = []
    for row in source_snapshot.stocks:
        if row.available_units <= 0:
            items.append(
                {
                    "id": f"oos-{row.nm_id}-{row.warehouse_id or 0}",
                    "alertType": "stock_oos",
                    "severity": "critical",
                    "entityType": "warehouse",
                    "entityId": str(row.nm_id),
                    "title": f"OOS risk for NM {row.nm_id}",
                    "details": f"{row.warehouse_name or 'warehouse'} availableUnits <= 0",
                    "createdAt": _utc_now_iso(),
                    "resolvedAt": None,
                    "route": "/wb/reports/stock",
                }
            )
    return {"items": items}


@router.get("/api/wb/reports/export/{report_id}")
def get_reports_export(
    request: Request,
    report_id: ReportId,
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="finance:read" if report_id == "expenses" else "settings:read",
        action="reports.bff.export.get",
        object_type="wb_export",
        object_id=f"export:{report_id}",
        reason="actor cannot request report export",
    )
    date_from, date_to, _date_range = _range_from_preset(preset, from_, to)
    row_count = _estimated_export_rows(
        report_id,
        date_from,
        date_to,
        has_permission(actor, "finance:read"),
        wb_token=None,
        organization_id=actor.organization_id,
    )
    export = request_report_export(
        actor=actor,
        report_id=report_id,
        preset=preset,
        from_raw=from_,
        to_raw=to,
        row_count=row_count,
    )
    completed = materialize_report_export(
        actor=actor,
        export_id=export.exportId,
        row_count=row_count,
    )
    return {
        "exportId": completed.exportId,
        "jobId": completed.jobId,
        "status": completed.status,
        "fileName": completed.fileName,
        "rows": completed.rows,
        "emptySourceNote": completed.emptySourceNote,
        "downloadUrl": completed.downloadUrl,
    }


@router.post("/api/wb/reports/export/{report_id}")
def request_reports_export_job(
    request: Request,
    report_id: ReportExportId,
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.bff.export.create",
        object_type="wb_export",
        object_id=f"export:{report_id}",
        reason="actor cannot create report export job",
    )
    date_from, date_to, _date_range = _range_from_preset(preset, from_, to)
    export = request_report_export(
        actor=actor,
        report_id=report_id,
        preset=preset,
        from_raw=from_,
        to_raw=to,
        row_count=_estimated_export_rows(
            report_id,
            date_from,
            date_to,
            has_permission(actor, "finance:read"),
            wb_token=None,
            organization_id=actor.organization_id,
        ),
    )
    record_audit_event(
        actor=actor,
        action="reports.bff.export.create",
        object_type="wb_export",
        object_id=export.exportId,
        before_state=None,
        after_state=export.model_dump(mode="json"),
        reason="queue async report export",
        approval_ref=None,
        evidence_refs=["REPORT_EXPORT_ASYNC_FLOW"],
    )
    return export.model_dump(mode="json")


@router.get("/api/wb/reports/export/jobs/{export_id}")
def get_reports_export_job(
    request: Request,
    export_id: str,
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.bff.export.get",
        object_type="wb_export",
        object_id=export_id,
        reason="actor cannot read report export job",
    )
    export = get_report_export_or_none(export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="EXPORT_JOB_NOT_FOUND")
    completed = materialize_report_export(actor=actor, export_id=export_id, row_count=export.rows)
    return completed.model_dump(mode="json")


@router.get("/reports/cash-flow")
@router.get("/api/wb/reports/cash-flow")
def get_cash_flow_report(
    request: Request,
    from_: str = Query(alias="from"),
    to: str = Query(),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="finance:read",
        action="reports.cash_flow.get",
        object_type="cash_flow",
        object_id=f"{from_}:{to}",
        reason="actor cannot read cash-flow report",
    )
    period_from = date.fromisoformat(from_)
    period_to = date.fromisoformat(to)
    return get_cash_flow_for_period(
        organization_id=actor.organization_id,
        period_from=period_from,
        period_to=period_to,
        requested_by=actor.user_id,
    )


@router.get("/api/wb/reports/{report_id}")
@tax_consistent_report
def get_reports_by_id(
    request: Request,
    report_id: ReportId,
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    groupBy: ReportGroupBy = Query(default="sku"),
    source: str = Query(default="operational"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="finance:read" if report_id == "expenses" else "settings:read",
        action="reports.bff.get",
        object_type="wb_report",
        object_id=report_id,
        reason="actor cannot read bff report",
    )
    finance_allowed = has_permission(actor, "finance:read")
    wb_token = None if report_id in BACKGROUND_REPORT_IDS else _actor_wb_token(actor)
    date_from, date_to, date_range = _range_from_preset(preset, from_, to)
    job_key = _report_job_cache_key(
        report_id,
        date_from,
        date_to,
        groupBy,
        source,
        finance_allowed=finance_allowed,
    )

    if report_id == "expenses":
        cash_flow = get_cash_flow_for_period(
            organization_id=actor.organization_id,
            period_from=date_from,
            period_to=date_to,
            requested_by=actor.user_id,
        )
        job = get_source_cache(actor.organization_id, job_key, slim=False) or {
            "state": "waiting_1c" if cash_flow.get("status") in {"pending", "processing"} else "completed",
            "reportId": report_id,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "groupBy": groupBy,
            "stage": "waiting_1c" if cash_flow.get("status") in {"pending", "processing"} else "completed",
            "label": "Операционные расходы недоступны" if cash_flow.get("status") == "disabled" else "Ждём операционные расходы от 1С" if cash_flow.get("status") in {"pending", "processing"} else "Расходы из 1С готовы",
            "percent": 20 if cash_flow.get("status") in {"pending", "processing"} else 100,
        }
        return _map_cash_flow_to_expenses_response(cash_flow, date_range, groupBy, job)

    if report_id == "abc":
        cache_key = _report_cache_key(report_id, date_from, date_to, groupBy, source, organization_id=actor.organization_id, finance_allowed=finance_allowed)
        cached = get_source_cache(actor.organization_id, cache_key, slim=False) or {}
        report = cached.get("report") if isinstance(cached.get("report"), dict) else None
        if report is not None and _report_payload_cache_is_usable(report_id, cached, organization_id=actor.organization_id):
            job = get_source_cache(
                actor.organization_id,
                job_key,
                slim=False,
            ) or {}
            payload = dict(report)
            payload["cache"] = _report_payload_cache_meta(cached, date_range)
            payload["reportJob"] = _completed_report_job_from_cache(report_id, date_from, date_to, groupBy, cached, job)
            return _with_storage_presentation(payload)

    if report_id == "week-over-week":
        cache_key = _report_cache_key(report_id, date_from, date_to, groupBy, source, organization_id=actor.organization_id, finance_allowed=finance_allowed)
        cached = get_source_cache(actor.organization_id, cache_key, slim=False) or {}
        report = cached.get("report") if isinstance(cached.get("report"), dict) else None
        job = get_source_cache(actor.organization_id, job_key, slim=False) or {
            "state": "idle", "reportId": report_id, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "groupBy": groupBy,
        }
        if report is not None:
            if not _report_payload_cache_is_usable(report_id, cached, organization_id=actor.organization_id):
                return _empty_background_report(report_id, date_range, groupBy, _report_job_for_response(job))
            job = _report_job_for_response(job)
            response_job = _completed_report_job_from_cache(report_id, date_from, date_to, groupBy, cached, job)
            if response_job != job:
                save_source_cache(actor.organization_id, job_key, response_job)
            job = response_job
            payload = _apply_report_rules_to_payload(report, actor.organization_id)
            payload["cache"] = _report_payload_cache_meta(
                cached,
                date_range,
                "stale" if job.get("state") in {"queued", "running", "waiting_1c"} else None,
            )
            payload["reportJob"] = job
            return payload
        return _empty_background_report(report_id, date_range, groupBy, job)

    if report_id == "pnl":
        cache_key = _report_cache_key(report_id, date_from, date_to, groupBy, source, organization_id=actor.organization_id, finance_allowed=finance_allowed)
        cached = get_source_cache(actor.organization_id, cache_key, slim=False) or {}
        report = cached.get("report") if isinstance(cached.get("report"), dict) else None
        job = get_source_cache(actor.organization_id, job_key, slim=False) or {
            "state": "idle", "reportId": report_id, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "groupBy": groupBy,
        }
        if report is not None:
            if not _report_payload_cache_is_usable(report_id, cached, organization_id=actor.organization_id):
                return _empty_background_report(report_id, date_range, groupBy, _report_job_for_response(job))
            job = _report_job_for_response(job)
            response_job = _completed_report_job_from_cache(report_id, date_from, date_to, groupBy, cached, job)
            if response_job != job:
                save_source_cache(actor.organization_id, job_key, response_job)
            job = response_job
            payload = _apply_report_rules_to_payload(report, actor.organization_id)
            payload["cache"] = _report_payload_cache_meta(
                cached,
                date_range,
                "stale" if job.get("state") in {"queued", "running", "waiting_1c", "waiting_daily_detail"} else None,
            )
            payload["reportJob"] = job
            return _with_storage_presentation(payload)
        return _empty_background_report(report_id, date_range, groupBy, job)

    daily_sources = REPORT_DAILY_SOURCES_BY_ID.get(report_id, ())
    if daily_sources:
        ready, missing_sources = _report_daily_sources_ready(actor.organization_id, daily_sources, date_from=date_from, date_to=date_to,
            **({"report_id": report_id, "finance_allowed": finance_allowed} if report_id == "rnp" else {}))
        if not ready:
            current_job = get_source_cache(actor.organization_id, job_key, slim=False) or {}
            if _report_job_is_reusable(current_job):
                return _empty_background_report(report_id, date_range, groupBy, current_job)
            job = _report_waiting_daily_detail_job(report_id, date_from, date_to, groupBy, missing_sources, current_job)
            save_source_cache(actor.organization_id, job_key, job)
            return _empty_background_report(report_id, date_range, groupBy, job)

    if report_id == "stock":
        source_snapshot = build_cached_wb_reports_sources_snapshot(organization_id=actor.organization_id, date_from=date_from, date_to=date_to)
        payload = _apply_report_rules_to_payload(
            _build_stock_report_payload(date_range, source_snapshot, organization_id=actor.organization_id, wb_token=wb_token),
            actor.organization_id,
        )
        _save_exact_report_payload_cache(organization_id=actor.organization_id, report_id=report_id, date_from=date_from, date_to=date_to, group_by=groupBy, source=source, report=payload)
        return payload
    if report_id == "ads":
        payload = _apply_report_rules_to_payload(
            _build_ads_report_payload(
                actor=actor,
                date_from=date_from,
                date_to=date_to,
                date_range=date_range,
                wb_token=wb_token,
            ),
            actor.organization_id,
        )
        _save_exact_report_payload_cache(organization_id=actor.organization_id, report_id=report_id, date_from=date_from, date_to=date_to, group_by=groupBy, source=source, report=payload)
        return payload
    if report_id == "rnp":
        rnp_payload = build_rnp_report(
            date_from=date_from,
            date_to=date_to,
            group_by=groupBy,
            finance_allowed=finance_allowed,
            organization_id=actor.organization_id,
            wb_token=None,
        )
        payload = _apply_report_rules_to_payload(_map_rnp_to_report_response(rnp_payload, date_range), actor.organization_id)
        _save_exact_report_payload_cache(organization_id=actor.organization_id, report_id=report_id, date_from=date_from, date_to=date_to, group_by=groupBy, source=source, report=payload)
        return payload
    if report_id == "abc":
        abc_payload = build_abc_report(
            date_from=date_from,
            date_to=date_to,
            group_by=groupBy,
            filters="",
            finance_allowed=finance_allowed,
            organization_id=actor.organization_id,
        )
        abc_rows = _normalize_abc_report_rows([row for row in abc_payload.rows if isinstance(row, dict)])
        source_status = abc_payload.sourceStatus
        confidence = abc_payload.confidence
        blocker_ids = list(getattr(abc_payload, "blockerIds", []) or [])
        filtered_summary = abc_payload.filteredSummary.model_dump(mode="json")
        if not abc_rows:
            repricer_rows = _repricer_rows_for_abc_report(
                request=request,
                actor=actor,
                wb_token=wb_token,
                date_from=date_from,
                date_to=date_to,
            )
            abc_rows = [
                mapped
                for mapped in (_repricer_row_to_abc_row(row) for row in repricer_rows if isinstance(row, dict))
                if mapped.get("sku")
            ]
            if abc_rows:
                source_status = "partial"
                confidence = "medium" if _abc_rows_have_financial_activity(abc_rows) else "low"
                blocker_ids.append("WB_ABC_RUNTIME_FALLBACK_REPRICER_LIST")
                filtered_summary = _abc_filtered_summary_from_rows(
                    abc_rows,
                    source_status=source_status,
                    confidence=confidence,
                )
        abc_rows = _normalize_abc_report_rows(abc_rows)
        if abc_rows and not _abc_rows_have_financial_activity(abc_rows):
            source_status = "partial"
            confidence = "low"
            if "WB_ABC_PERIOD_ACTIVITY_EMPTY" not in blocker_ids:
                blocker_ids.append("WB_ABC_PERIOD_ACTIVITY_EMPTY")
            filtered_summary["sourceStatus"] = source_status
            filtered_summary["confidence"] = confidence
        payload = _apply_report_rules_to_payload({
            "cacheVersion": ABC_REPORT_PAYLOAD_VERSION,
            "meta": _meta("abc", "ABC-анализ", "ABC and SKU profitability view.", "operational", source_status),
            "headline": "ABC summary for selected period.",
            "filters": {"dateRange": date_range, "groupBy": groupBy},
            "kpis": [
                _kpi("sku_count", "SKU", str(filtered_summary.get("skuCount") or 0)),
                _kpi("orders", "Заказы", str(filtered_summary.get("ordersCount") or 0)),
                _kpi("orders_revenue", "Сумма заказов", str(filtered_summary.get("ordersKopecks") or 0)),
                _kpi("profit", "Прибыль", "—" if filtered_summary.get("profitKopecks") is None else str(filtered_summary["profitKopecks"])),
            ],
            "chart": {
                "title": "Чистая прибыль по SKU",
                "valueLabel": "Чистая прибыль, коп",
                "points": [
                    {"label": str(row.get("sku") or row.get("label") or index + 1), "value": _abc_row_profit(row)}
                    for index, row in enumerate(abc_rows[:20])
                    if isinstance(row, dict) and _abc_row_profit(row) is not None
                ],
            },
            "columns": _abc_report_columns(),
            "rows": abc_rows,
            "sourceStatus": source_status,
            "confidence": confidence,
            "blockerIds": blocker_ids,
            "sourceEvidence": [item.model_dump(mode="json") for item in abc_payload.sourceEvidence],
            "filteredSummary": filtered_summary,
        }, actor.organization_id, compact_abc=True)
        _save_exact_report_payload_cache(organization_id=actor.organization_id, report_id=report_id, date_from=date_from, date_to=date_to, group_by=groupBy, source=source, report=payload, finance_allowed=finance_allowed)
        return payload
    if report_id == "pnl":
        source_mode = "final" if source == "financial" else "preliminary"
        cash_flow = get_cash_flow_for_period(
            organization_id=actor.organization_id,
            period_from=date_from,
            period_to=date_to,
            requested_by=actor.user_id,
        )
        pnl_payload = build_pnl_report(
            date_from=date_from,
            date_to=date_to,
            group_by=groupBy,
            requested_state=source_mode,
            finance_allowed=finance_allowed,
            organization_id=actor.organization_id,
            wb_token=None,
        )
        return _apply_report_rules_to_payload(_map_pnl_to_report_response(pnl_payload, date_range, cash_flow, finance_allowed=finance_allowed, financial_only=source == "financial"), actor.organization_id)

    return {
        "meta": _meta(report_id, report_id, "unknown report"),
        "headline": "No data",
        "filters": {"dateRange": date_range, "groupBy": groupBy},
        "kpis": [],
        "chart": {"title": "No data", "valueLabel": "-", "points": []},
        "columns": [],
        "rows": [],
    }


def _repricer_finance_job_source(actor, source: str, report_id: str) -> str:
    if source.startswith("catalog"):
        if source != "catalog" or report_id != "stats":
            raise HTTPException(400, "INVALID_CATALOG_JOB")
        token = get_user_wb_token_secret(actor.user_id)
        if not token:
            raise HTTPException(409, "WB_TOKEN_REQUIRED")
        from app.repricer_page_finance import connection_key
        return f"catalog:{connection_key(token)}"
    if not source.startswith("repricer-finance"):
        return source
    if source != "repricer-finance" or report_id != "stats":
        raise HTTPException(400, "INVALID_REPRICER_FINANCE_JOB")
    if not has_permission(actor, "finance:read"):
        raise HTTPException(403, "FINANCE_READ_REQUIRED")
    token = get_user_wb_token_secret(actor.user_id)
    if not token:
        raise HTTPException(409, "WB_TOKEN_REQUIRED")
    from app.repricer_page_finance import connection_key
    return f"repricer-finance:{connection_key(token)}"


@contextmanager
def _report_start_guard(organization_id, key):
    from redis.exceptions import RedisError
    from app.infra.redis_client import get_redis_client
    lock = get_redis_client().lock(f"wb:report-start:{organization_id}:{sha1(key.encode()).hexdigest()}", timeout=120)
    try:
        acquired = lock.acquire(blocking=True, blocking_timeout=1)
    except RedisError:
        raise HTTPException(503, "Очередь загрузки недоступна; сохранённые данные не удалены.") from None
    if not acquired:
        raise HTTPException(503, "Запуск этого отчёта уже обрабатывается. Повторите проверку через несколько секунд.")
    try:
        yield
    finally:
        try:
            lock.release()
        except RedisError:
            pass  # Expired ownership must not mask a successfully queued job.


def _serialize_report_starts(function):
    parameters = signature(function, eval_str=True)
    @wraps(function)
    def guarded(*args, **kwargs):
        bound = parameters.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        if values["report_id"] not in {"abc", "pnl", "rnp", "week-over-week"}:
            return function(*args, **kwargs)
        actor = actor_from_request(values["request"])
        source = _repricer_finance_job_source(actor, values["source"], values["report_id"])
        start, end, _ = _range_from_preset(values["preset"], values["from_"], values["to"])
        key = _report_job_cache_key(values["report_id"], start, end, values["groupBy"], source,
            finance_allowed=has_permission(actor, "finance:read"))
        # Serialize check-and-dispatch across API workers, not merely within
        # one browser. Provider HTTP checkpoints retain their own shared locks.
        with _report_start_guard(actor.organization_id, key):
            return function(*args, **kwargs)
    guarded.__signature__ = parameters
    return guarded


@router.post("/api/wb/reports/{report_id}/jobs")
@_serialize_report_starts
def start_report_job(request: Request, report_id: Literal["stats", "abc", "rnp", "ads", "pnl", "expenses", "stock", "week-over-week"], preset: str = Query(default="7d"), from_: str | None = Query(default=None, alias="from"), to: str | None = Query(default=None), groupBy: ReportGroupBy = Query(default="sku"), source: str = Query(default="operational"), background_tasks: BackgroundTasks = None) -> dict[str, Any]:
    actor = actor_from_request(request)
    source = _repricer_finance_job_source(actor, source, report_id)
    assert_permission_or_audit(actor=actor, permission="finance:read" if report_id == "expenses" else "settings:read", action="reports.bff.job.start", object_type="wb_report", object_id=report_id, reason="actor cannot refresh report")
    date_from, date_to, _ = _range_from_preset(preset, from_, to)
    finance_allowed = has_permission(actor, "finance:read")
    key = _report_job_cache_key(
        report_id,
        date_from,
        date_to,
        groupBy,
        source,
        finance_allowed=finance_allowed,
    )
    cache_key = _report_cache_key(report_id, date_from, date_to, groupBy, source, organization_id=actor.organization_id, finance_allowed=finance_allowed)
    touch_demand(request, actor, key, start=True)
    cached = get_source_cache(actor.organization_id, cache_key, slim=False) or {}
    current = get_source_cache(actor.organization_id, key, slim=False) or {}
    if report_id == "week-over-week":
        from app.wb_week_preview import enqueue
        enqueue(background_tasks, organization_id=actor.organization_id, user_id=actor.user_id,
            start=date_from, end=date_to, group_by=groupBy, source=source,
            finance_allowed=finance_allowed, cached=cached)
    if _report_job_is_reusable(current):
        return {**current, "reused": True}
    if report_id == "stats" and source.startswith("catalog:"):
        from app.wb_catalog_loading import queue_catalog_recovery
        from app.repricer_tasks import refresh_report_sources_for_org
        return queue_catalog_recovery(actor.organization_id, key,
            (actor.organization_id, actor.user_id, report_id, str(date_from), str(date_to),
                groupBy, source, finance_allowed, None), refresh_report_sources_for_org.apply_async)
    if report_id == "stats" and source.startswith("repricer-finance:"):
        if not finance_allowed:
            raise HTTPException(403, "FINANCE_READ_REQUIRED")
        from app.repricer_page_finance import missing_page_ranges
        token = get_user_wb_token_secret(actor.user_id)
        if not token:
            raise HTTPException(409, "WB_TOKEN_REQUIRED")
        missing = missing_page_ranges(actor.organization_id, token, date_from, date_to, fresh_only=True)
        if not missing:
            payload = {"state": "completed", "reportId": report_id, "percent": 100,
                       "dateFrom": str(date_from), "dateTo": str(date_to),
                       "kind": "report_source_refresh", "stage": "completed",
                       "finishedAt": _utc_now_iso(),
                       "label": "Финансы репрайсера загружены из БД", "updatedAt": _utc_now_iso()}
        else:
            from app.repricer_tasks import refresh_report_sources_for_org
            task = refresh_report_sources_for_org.delay(actor.organization_id, actor.user_id, report_id,
                str(date_from), str(date_to), groupBy, source, finance_allowed, None, refresh_missing_only=True)
            payload = {"state": "queued", "taskId": task.id, "reportId": report_id, "percent": 0,
                       "dateFrom": str(date_from), "dateTo": str(date_to),
                       "label": "Дозагружаем финансы репрайсера", "updatedAt": _utc_now_iso()}
        save_source_cache(actor.organization_id, key, payload)
        return payload
    source_from, source_to = _report_source_bounds(report_id, date_from, date_to)
    daily_sources = (("ads", "baskets-summary") if report_id == "stats" and source == "statistics-comparison"
                     else REPORT_DAILY_SOURCES_BY_ID.get(report_id, ()))
    sources_fresh, _ = _report_daily_sources_ready(actor.organization_id, daily_sources, date_from=source_from, date_to=source_to, fresh_only=True, report_id=report_id, selected_date_from=date_from if report_id == "week-over-week" else None, finance_allowed=finance_allowed)
    from app.wb_catalog_loading import missing_catalog_sources
    catalog_missing = missing_catalog_sources(actor.organization_id)
    sources_fresh = sources_fresh and not catalog_missing
    if _report_payload_cache_is_usable(report_id, cached, organization_id=actor.organization_id) and sources_fresh:
        payload = _completed_report_job_from_cache(
            report_id, date_from, date_to, groupBy, cached, current,
            preserve_finished_refresh=False,
        )
        save_source_cache(actor.organization_id, key, payload)
        return payload
    cash_flow = None
    if report_id in {"pnl", "expenses"}:
        cash_flow = get_cash_flow_for_period(
            organization_id=actor.organization_id,
            period_from=date_from,
            period_to=date_to,
            requested_by=actor.user_id,
        )
    cash_flow_response = {"cashFlow": cash_flow if finance_allowed else None} if cash_flow is not None else {}
    if daily_sources:
        ready, missing_sources = _report_daily_sources_ready(actor.organization_id, daily_sources, date_from=source_from, date_to=source_to, fresh_only=True, report_id=report_id, selected_date_from=date_from if report_id == "week-over-week" else None, finance_allowed=finance_allowed)
        if catalog_missing:
            ready, missing_sources = False, [*missing_sources, *catalog_missing]
        # Every campaign report rejected by the cache fast-path must use the
        # campaign collector, not the SKU-only aggregate builder (no budgets).
        if report_id == "ads":
            ready, missing_sources = False, ["campaigns/budgets"]
        if not ready:
            from app.repricer_tasks import refresh_report_sources_for_org

            args = (
                actor.organization_id,
                actor.user_id,
                report_id,
                date_from.isoformat(),
                date_to.isoformat(),
                groupBy,
                source,
                finance_allowed,
                None,
            )
            task_id = str(uuid4())
            payload = {
                **_report_waiting_daily_detail_job(report_id, date_from, date_to, groupBy, missing_sources, current),
                "state": "queued",
                "taskId": task_id,
                "kind": "report_source_refresh",
                "stage": "queued",
                "label": "Ожидаем свободный обработчик загрузки WB" if report_id == "stats" else "Готовим недостающие данные отчета",
                **({"source": source} if report_id == "stats" else {}),
                "error": None,
                "percent": 0,
                "queuedAt": _utc_now_iso(),
                "updatedAt": _utc_now_iso(),
            }
            save_source_cache(actor.organization_id, key, payload)
            # Persist before dispatch: a fast worker must not have its
            # running/completed heartbeat overwritten by the HTTP handler.
            try:
                refresh_report_sources_for_org.apply_async(args=args,
                    kwargs={"refresh_missing_only": True}, task_id=task_id)
            except Exception:
                save_source_cache(actor.organization_id, key, {**payload, "state": "failed", "stage": "failed",
                    "label": "Не удалось поставить загрузку WB в очередь", "updatedAt": _utc_now_iso()})
                raise HTTPException(503, "Очередь загрузки недоступна. Сохранённые данные доступны.") from None
            return {**payload, "reused": False, **cash_flow_response}
    if report_id == "stats":
        payload = {"state": "completed", "stage": "cache", "reportId": report_id, "percent": 100,
            "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "reused": True,
            "label": "Статистика загружена из БД", "updatedAt": _utc_now_iso()}
        save_source_cache(actor.organization_id, key, payload)
        return payload
    from app.repricer_tasks import build_report_for_org
    if report_id in {"abc", "pnl", "rnp", "week-over-week"}:
        task_id = str(uuid4())
        payload = {"state": "queued", "stage": "queued", "taskId": task_id,
            "reportId": report_id, "dateFrom": str(date_from), "dateTo": str(date_to),
            "groupBy": groupBy, "source": source, "queuedAt": _utc_now_iso(), "updatedAt": _utc_now_iso()}
        save_source_cache(actor.organization_id, key, payload)
        try:
            build_report_for_org.apply_async(args=(actor.organization_id, actor.user_id, report_id,
                str(date_from), str(date_to), groupBy, source, finance_allowed, None), task_id=task_id)
        except Exception:
            save_source_cache(actor.organization_id, key, {**payload, "state": "failed", "stage": "failed",
                "label": "Не удалось поставить сборку отчёта в очередь", "updatedAt": _utc_now_iso()})
            raise HTTPException(503, "Очередь сборки недоступна. Сохранённые данные доступны.") from None
        return {**payload, "reused": False, **cash_flow_response}
    task = build_report_for_org.delay(actor.organization_id, actor.user_id, report_id, date_from.isoformat(), date_to.isoformat(), groupBy, source, finance_allowed, None)
    payload = {"state": "queued", "taskId": task.id, "reportId": report_id, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "groupBy": groupBy, "source": source, "queuedAt": _utc_now_iso()}
    save_source_cache(actor.organization_id, key, payload)
    return {**payload, "reused": False, **cash_flow_response}


@router.post("/api/wb/reports/{report_id}/refresh-sources-job")
def start_report_source_refresh_job(
    request: Request,
    report_id: Literal["digest", "abc", "rnp", "ads", "pnl", "stock", "week-over-week"],
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    groupBy: ReportGroupBy = Query(default="sku"),
    source: str = Query(default="operational"),
) -> dict[str, Any]:
    if report_id in {"abc", "pnl"}:
        # Older clients may still send this action. These reports resume only
        # missing/stale sources; never force a full financial recollection.
        return start_report_job(request, report_id, preset, from_, to, groupBy, source)
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.bff.sources_refresh.start", object_type="wb_report", object_id=report_id, reason="actor cannot refresh report sources")
    date_from, date_to, _ = _range_from_preset(preset, from_, to)
    finance_allowed = has_permission(actor, "finance:read")
    key = _digest_job_cache_key(date_from, date_to) if report_id == "digest" else _report_job_cache_key(
        report_id,
        date_from,
        date_to,
        groupBy,
        source,
        finance_allowed=finance_allowed,
    )
    touch_demand(request, actor, key, start=True)
    current = get_source_cache(actor.organization_id, key, slim=False) or {}
    if _report_job_is_active_refresh(current):
        return {**current, "reused": True}
    if report_id == "pnl":
        get_cash_flow_for_period(
            organization_id=actor.organization_id,
            period_from=date_from,
            period_to=date_to,
            requested_by=actor.user_id,
        )
    from app.repricer_tasks import refresh_report_sources_for_org
    task = refresh_report_sources_for_org.delay(actor.organization_id, actor.user_id, report_id, date_from.isoformat(), date_to.isoformat(), groupBy, source, finance_allowed, None)
    payload = {
        "state": "queued",
        "taskId": task.id,
        "kind": "report_source_refresh",
        "reportId": report_id,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "groupBy": groupBy,
        "stage": "queued",
        "label": "Обновление данных отчета поставлено в очередь",
        "percent": 0,
        "queuedAt": _utc_now_iso(),
        "updatedAt": _utc_now_iso(),
    }
    save_source_cache(actor.organization_id, key, payload)
    return {**payload, "reused": False}


@router.get("/api/wb/reports/{report_id}/jobs")
def get_report_job(request: Request, report_id: Literal["stats", "abc", "rnp", "ads", "pnl", "expenses", "stock", "week-over-week"], preset: str = Query(default="7d"), from_: str | None = Query(default=None, alias="from"), to: str | None = Query(default=None, alias="to"), groupBy: ReportGroupBy = Query(default="sku"), source: str = Query(default="operational"), background_tasks: BackgroundTasks = None) -> dict[str, Any]:
    actor = actor_from_request(request)
    source = _repricer_finance_job_source(actor, source, report_id)
    assert_permission_or_audit(actor=actor, permission="finance:read" if report_id == "expenses" else "settings:read", action="reports.bff.job.status", object_type="wb_report", object_id=report_id, reason="actor cannot read report job")
    date_from, date_to, _ = _range_from_preset(preset, from_, to)
    finance_allowed = has_permission(actor, "finance:read")
    key = _report_job_cache_key(
        report_id,
        date_from,
        date_to,
        groupBy,
        source,
        finance_allowed=finance_allowed,
    )
    touch_demand(request, actor, key)
    job = get_source_cache(actor.organization_id, key, slim=False) or {"state": "idle", "reportId": report_id, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "groupBy": groupBy}
    if report_id == "week-over-week":
        from app.wb_week_preview import enqueue, status_for
        cache_key = _report_cache_key(report_id, date_from, date_to, groupBy, source,
            organization_id=actor.organization_id, finance_allowed=finance_allowed)
        enqueue(background_tasks, organization_id=actor.organization_id, user_id=actor.user_id,
            start=date_from, end=date_to, group_by=groupBy, source=source,
            finance_allowed=finance_allowed, cached=get_source_cache(actor.organization_id, cache_key, slim=False) or {})
        assembly = status_for(actor.organization_id, cache_key)
        if assembly is not None:
            job = {**job, "databaseAssembly": assembly}
    if _report_job_is_reusable(job):
        return _report_job_for_response({**job, "reused": True})
    if _report_job_is_finished_refresh(job):
        return _report_job_for_response({**job, "reused": True})
    cached = get_source_cache(
        actor.organization_id,
        _report_cache_key(report_id, date_from, date_to, groupBy, source, organization_id=actor.organization_id, finance_allowed=finance_allowed),
        slim=False,
    ) or {}
    if _report_payload_cache_is_usable(report_id, cached, organization_id=actor.organization_id):
        payload = _completed_report_job_from_cache(report_id, date_from, date_to, groupBy, cached, job)
        save_source_cache(actor.organization_id, key, payload)
        return payload
    return _report_job_for_response(job, cache_missing=True)


@router.api_route("/api/wb/reports/ads/budgets", methods=["GET", "POST"])
def get_optional_ads_budgets(request: Request, from_: str = Query(alias="from"), to: str = Query()):
    """Read saved budgets or enqueue optional reads, never call WB in HTTP."""
    actor = actor_from_request(request)
    assert_permission_or_audit(actor=actor, permission="settings:read", action="reports.bff.ads.budgets",
        object_type="wb_report", object_id="ads", reason="actor cannot read campaign budgets")
    start_date, end_date, _ = _range_from_preset("custom", from_, to)
    from app.repricer_tasks import _report_refresh_wb_token, _enqueue_report_budget_step
    token = _report_refresh_wb_token(actor.organization_id, actor.user_id, None)
    if not token:
        raise HTTPException(409, "WB_TOKEN_REQUIRED")
    report = get_ads_report_cache(organization_id=actor.organization_id, date_from=start_date,
                                 date_to=end_date, group_by="campaign") or {}
    from app.wb_report_budgets import connection, overlay, start
    digest = connection(token)
    if request.method == "POST":
        start(actor.organization_id, actor.user_id, digest,
              [row.get("campaignId") for row in report.get("rows", [])], _enqueue_report_budget_step, include_balance=True)
    result = overlay(actor.organization_id, digest, report)
    return {"dateFrom": start_date.isoformat(), "dateTo": end_date.isoformat(),
            "rows": [{key: row.get(key) for key in ("campaignId", "budgetCashKopecks", "budgetNettingKopecks", "budgetTotalKopecks", "budgetFetchedAt", "budgetFresh")} for row in result.get("rows", [])],
            "cabinetBalance": result["cabinetBalance"], "balanceRefresh": result["balanceRefresh"],
            "balanceKpi": next((item for item in result.get("kpis", []) if item.get("id") == "cabinet_balance"), None),
            "budgetRefresh": result["budgetRefresh"],
            "kpi": next((item for item in result.get("kpis", []) if item.get("id") == "campaign_budget"), None)}


@router.post("/api/wb/reports/ads/refresh")
def refresh_ads_report_cache(
    request: Request,
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.bff.ads.refresh",
        object_type="wb_report",
        object_id="ads",
        reason="actor cannot refresh ads report cache",
    )
    date_from, date_to, date_range = _range_from_preset(preset, from_, to)
    payload = _build_ads_report_payload(
        actor=actor,
        date_from=date_from,
        date_to=date_to,
        date_range=date_range,
        wb_token=None,
        refresh=True,
    )
    _save_exact_report_payload_cache(
        organization_id=actor.organization_id,
        report_id="ads",
        date_from=date_from,
        date_to=date_to,
        group_by="campaign",
        source="operational",
        report=_apply_report_rules_to_payload(payload, actor.organization_id),
    )
    record_audit_event(
        actor=actor,
        action="reports.bff.ads.refresh",
        object_type="wb_report",
        object_id="ads",
        before_state=None,
        after_state={
            "rows": len(payload.get("rows") or []),
            "cache": payload.get("cache"),
        },
        reason="manual ads report cache refresh",
        approval_ref=None,
        evidence_refs=["wb_ads_report_cache", "wb_ads_campaign_status_snapshots", "wb_ads_campaign_budget_snapshots"],
    )
    return payload


@router.post("/api/wb/reports/rnp/refresh")
def refresh_rnp_report_cache(
    request: Request,
    preset: str = Query(default="7d"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    groupBy: ReportGroupBy = Query(default="sku"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    assert_permission_or_audit(
        actor=actor,
        permission="settings:read",
        action="reports.bff.rnp.refresh",
        object_type="wb_report",
        object_id="rnp",
        reason="actor cannot refresh rnp report cache",
    )
    finance_allowed = has_permission(actor, "finance:read")
    date_from, date_to, date_range = _range_from_preset(preset, from_, to)
    rnp_payload = build_rnp_report(
        date_from=date_from,
        date_to=date_to,
        group_by=groupBy,
        finance_allowed=finance_allowed,
        organization_id=actor.organization_id,
        wb_token=None,
        force_refresh=False,
    )
    payload = _map_rnp_to_report_response(rnp_payload, date_range)
    payload["cache"] = {"status": "refreshed"}
    record_audit_event(
        actor=actor,
        action="reports.bff.rnp.refresh",
        object_type="wb_report",
        object_id="rnp",
        before_state=None,
        after_state={
            "rows": len(payload.get("rows") or []),
            "sourceStatus": payload.get("meta", {}).get("freshnessState"),
        },
        reason="manual rnp report cache refresh",
        approval_ref=None,
        evidence_refs=["rnp_funnel_cache", "rnp_report_cache", "wb_ads_attribution"],
    )
    return payload
