from __future__ import annotations

import logging
import hashlib
import json
import re
import threading
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.cabinet.store import get_organization_wb_token_secret, get_user_wb_token_secret, list_team_users, record_audit_event
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.infra.db import get_db_session
from app.platform.period import Period
from app.repricer_page_finance import repricer_finance_scope
from app.repricer_source_provenance import (
    daily_source_evidence, oldest_source_observation, source_binding_matches, source_observations_fresh,
)
from app.platform.economics.legacy_tax import get_legacy_finance_taxes, legacy_finance_tax_revision
from app.platform.economics.wb_quarterly_tax import tax_consistent_report, assert_current_snapshot
from app.platform.economics.policies import (
    EconomicsNotFoundError,
    EconomicsService,
    EconomicsValidationError,
    _legacy_values,
)
from app.platform.finance.service import shadow_ingest_legacy_finance_payload
from app.promotion_excel import ParsedPromotionExcel, normalized_filename, parse_promotion_excel
from app.repricer_nomenclature_excel import (
    build_repricer_nomenclature_xlsx,
    parse_repricer_nomenclature_excel,
)
from app.repricer_cache.store import (
    browser_prices_selected,
    cached_goods_meta,
    compact_heavy_source_cache_rows,
    finance_cache_uses_current_revenue_basis,
    get_covering_source_cache,
    get_goods_price_revision,
    get_source_cache,
    get_source_cache_fetched_at,
    get_source_cache_meta_fields,
    get_source_cache_range_revision,
    get_repricer_sources_revision,
    list_source_cache_ranges_by_prefix,
    list_cached_goods,
    replace_goods_pages,
    save_goods_page,
    save_source_cache,
)
from app.wb_browser_prices import PRICES_KEY as BROWSER_PRICES_KEY, rows_expire_at, timestamp as browser_price_timestamp
from app.wb_statistics import apply_account_totals, compact_cache, observed_number, project_row, ratio
from app.wb_repricer_reporting import filtered_campaign_advertising
from app.wb_repricer_filters import RepricerFilters, compile_filter, saved_attribute_cards
from app.repricer_promotion_status import promotion_membership, promotion_refresh_metadata, timestamp as promotion_timestamp

SKU_LIST_MAX_ITEMS = 150
SKU_LIST_PAGE_SIZE_DEFAULT = 150
from app import repricer_bff as repricer_bff_module
from app.repricer_execution import (
    StrategyExecuteOptions,
    _commit_illiquid_runtime_step,
    _has_explicit_strategy,
    _resolve_strategy_id,
    execute_all_assigned_skus,
    execute_repricer_strategies,
    preview_repricer_strategies,
)
from app.repricer_persistence.store import (
    flush_repricer_bff_state,
    get_pending_price_approval,
    hydrate_repricer_bff_state,
    list_execution_runs,
    list_pending_price_approvals,
    update_pending_price_approval,
    update_pending_price_approvals,
)
from app.repricer_sprint_b import (
    ApplyDraftRequest,
    ApproveDraftRequest,
    PriceDraftCreateRequest,
    apply_approved_draft,
    apply_approved_drafts,
    approve_draft,
    create_price_draft,
)
from app.repricer_sync import (
    ExternalSppRateLimited,
    WbSyncAlreadyRunning,
    _apply_external_spp_prices_to_goods,
    _normalize_period_range,
    _period_cache_suffix,
    _unique_nm_ids_from_goods,
    abandon_stale_wb_sync,
    begin_wb_sync,
    fetch_external_spp_prices,
    get_wb_sync_status,
    is_wb_sync_running,
    list_wb_sync_history,
    queue_wb_sync,
    record_wb_sync_price_change_events,
    refresh_wb_data_sources,
    wb_token_fingerprint,
)
from app.wb_sync_plan import WbSyncProfile, historical_sync_as_of, next_nightly_sync_at, nightly_reconciliation_profile, onboarding_sync_profiles, periodic_sync_profiles
from app.repricer_bff import (
    _http_exception_message,
    _promotions_for_cache,
    apply_frontend_strategy_assignment,
    apply_repricer_template,
    confirm_repricer_negative_margin,
    _fetch_content_cards,
    _nm_ids_from_goods,
    fetch_baskets_aggregates,
    fetch_baskets_daily_detail,
    fetch_ads_spend_aggregates,
    fetch_catalog_goods_page,
    fetch_finance_report_aggregates,
    fetch_period_stats_aggregates,
    fetch_stock_aggregates,
    get_promotion_skus,
    get_repricer_algorithm,
    get_repricer_dashboard,
    get_repricer_liquidation,
    get_repricer_pricing_status,
    get_repricer_sku_settings,
    get_repricer_sku_timeseries,
    get_repricer_templates,
    list_frontend_strategy_assignments,
    list_frontend_strategy_catalog,
    list_repricer_changelog,
    list_promotions,
    list_repricer_sku_groups,
    list_repricer_skus,
    patch_repricer_automation,
    put_repricer_algorithm,
    put_repricer_sku_settings,
    put_repricer_templates,
    record_repricer_price_change,
    start_repricer_liquidation,
    stop_repricer_liquidation,
    unassign_frontend_strategy_assignment,
    upsert_repricer_sku_group,
    upload_promotion_excel,
)
from app.wb_api.client import RateLimitedWbApiClient, build_wb_client
from app.wb_import_excel import parse_cost_excel, parse_stock_excel


router = APIRouter(tags=["wb-repricer-bff"])
logger = logging.getLogger(__name__)
_WB_TOKEN_NOT_PROVIDED = object()
_ALGORITHM_ECONOMICS_FIELDS = frozenset(
    {"taxPct", "otherExpensePricePct", "otherExpensePerSaleRub"}
)
_SKU_ECONOMICS_FIELDS = frozenset(
    {"taxPct", "otherExpensePricePct", "otherExpensePerSaleKopecks"}
)

LEGACY_REPRICER_MANAGERS: dict[str, str] = {
    "manager-maria-dudina": "Мария Дудина",
    "manager-anna-petrova": "Анна Петрова",
    "manager-irina-kosheleva": "Ирина Кошелева",
    "manager-svetlana-volkova": "Светлана Волкова",
    "manager-irina": "Ирина",
}


class AutomationPatchRequest(BaseModel):
    automationEnabled: bool
    expectedRevision: str | None = None


class SkuManagerAssignmentRequest(BaseModel):
    managerUserId: str | None = None
    reason: str = Field(min_length=1)


class LiquidationStartRequest(BaseModel):
    articleIds: list[str] = Field(default_factory=list)


class StrategyAssignmentBulkRequest(BaseModel):
    articleIds: list[str] = Field(default_factory=list)
    strategyId: str | None = None
    strategyName: str | None = None
    executeAfterAssign: bool = True
    config: dict[str, Any] | None = None
    expectedRevisions: dict[str, str] | None = None


class SkuGroupUpsertRequest(BaseModel):
    groupId: str | None = None
    groupName: str | None = None
    articleIds: list[str] = Field(default_factory=list)
    planOrders: int | None = None
    mode: Literal["add", "replace"] = "add"


class StrategyExecuteRequest(BaseModel):
    articleIds: list[str] = Field(default_factory=list)
    scenario: str = "complete"
    createDrafts: bool = True
    autoApprove: bool = True
    approvalRef: str | None = None
    applyPrices: bool = True
    simulateLocalPrice: bool = True
    force: bool = False


class SkuConfigurationCheckpointRequest(BaseModel):
    nmId: int = Field(gt=0)
    expectedRevision: str = Field(pattern=r"^[a-f0-9]{64}$")


class StrategyPreviewRequest(BaseModel):
    articleIds: list[str] = Field(default_factory=list)
    scenario: str = "complete"
    force: bool = False
    periodDays: int = Field(default=30, ge=1, le=90)
    inputs: dict[str, Any] | None = None
    strategyId: str | None = None
    strategyName: str | None = None
    config: dict[str, Any] | None = None


class PendingPriceApprovalDecisionRequest(BaseModel):
    reason: str | None = None
    approvalRef: str | None = None


class PendingPriceApprovalBulkDecisionRequest(BaseModel):
    approvalIds: list[str] = Field(min_length=1, max_length=100)
    decision: Literal["approve", "reject"]
    reason: str | None = None


class RepricerSimulatorPatchRequest(BaseModel):
    sellerPriceKopecks: int | None = Field(default=None, ge=1)
    buyerPriceKopecks: int | None = Field(default=None, ge=1)
    sppPct: float | None = Field(default=None, ge=0, le=100)
    baskets: int | None = Field(default=None, ge=0)
    basketNorm: int | None = Field(default=None, ge=0)
    ordersUnits: int | None = Field(default=None, ge=0)
    salesUnits: int | None = Field(default=None, ge=0)
    returnsUnits: int | None = Field(default=None, ge=0)
    revenueKopecks: int | None = Field(default=None, ge=0)
    buyoutPct: float | None = Field(default=None, ge=0, le=100)
    stockUnits: int | None = Field(default=None, ge=0)
    strategyId: str | None = None
    makeLiquidationDue: bool = False


class RepricerSimulatorRunRequest(BaseModel):
    articleIds: list[str] = Field(default_factory=list)
    scenario: str = "complete"
    applyPrices: bool = True
    force: bool = True
    mode: str = "execute_assigned"
    periodDays: int = Field(default=30, ge=1, le=90)
    inputs: dict[str, Any] | None = None


class RepricerSyncRunRequest(BaseModel):
    scenario: str = "complete"
    mode: Literal["manual", "onboarding"] = "manual"
    periodDays: int = Field(default=30, ge=1, le=90)
    dateFrom: date | None = None
    dateTo: date | None = None
    force: bool = False
    sources: list[str] | None = None


class RepricerSyncRetryStepRequest(BaseModel):
    source: str
    scenario: str = "complete"


class BasketsDetailStartRequest(BaseModel):
    dateFrom: date
    dateTo: date
    scenario: str = "complete"
    force: bool = False


ImportMode = Literal["replace", "add"]


def _payload_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _payload_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _payload_article_ids(value: Any) -> list[str]:
    raw_items = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
    return list(dict.fromkeys(str(item).strip() for item in raw_items if str(item or "").strip()))


def _payload_inputs(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _preview_request_from_payload(raw: dict[str, Any]) -> StrategyPreviewRequest:
    for name in ("strategyId", "strategyName", "config"):
        expected = dict if name == "config" else str
        if raw.get(name) is not None and not isinstance(raw[name], expected):
            raise HTTPException(status_code=422, detail=f"INVALID_{name.upper()}")
    return StrategyPreviewRequest(
        articleIds=_payload_article_ids(raw.get("articleIds")),
        scenario=str(raw.get("scenario") or "complete"),
        force=_payload_bool(raw.get("force"), False),
        periodDays=_payload_int(raw.get("periodDays"), 30, min_value=1, max_value=90),
        inputs=_payload_inputs(raw.get("inputs")),
        strategyId=raw.get("strategyId"),
        strategyName=raw.get("strategyName"),
        config=raw.get("config"),
    )


def _simulator_run_request_from_payload(raw: dict[str, Any]) -> RepricerSimulatorRunRequest:
    return RepricerSimulatorRunRequest(
        articleIds=_payload_article_ids(raw.get("articleIds")),
        scenario=str(raw.get("scenario") or "complete"),
        applyPrices=_payload_bool(raw.get("applyPrices"), True),
        force=_payload_bool(raw.get("force"), True),
        mode=str(raw.get("mode") or "execute_assigned"),
        periodDays=_payload_int(raw.get("periodDays"), 30, min_value=1, max_value=90),
        inputs=_payload_inputs(raw.get("inputs")),
    )


async def _request_json_object(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_JSON",
                "message": "Request body must be a valid JSON object",
            },
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_JSON_OBJECT",
                "message": "Request body must be a JSON object",
            },
        )
    return payload


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _economics_source_reference(prefix: str, *parts: object) -> str:
    reference = ":".join((prefix, *(str(part) for part in parts)))
    return (
        reference
        if len(reference) <= 255
        else f"{prefix}:{hashlib.sha256(reference.encode('utf-8')).hexdigest()}"
    )


def _raise_invalid_economics(exc: EconomicsValidationError) -> None:
    raise HTTPException(
        status_code=422,
        detail={"code": "INVALID_ECONOMICS_SETTINGS", "message": str(exc)},
    ) from exc


def _repricer_period_context(
    period_days: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[datetime, datetime, int, str]:
    try:
        start, end, days = _normalize_period_range(period_days, date_from=date_from, date_to=date_to)
        suffix = _period_cache_suffix(period_days, date_from=date_from, date_to=date_to)
        return start, end, days, suffix
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _apply_costs_excel_cache_to_sku_settings(organization_id: int) -> None:
    cache = get_source_cache(organization_id, "costs_excel", slim=False) or {}
    rows = cache.get("applied") if isinstance(cache.get("applied"), list) else None
    if rows is None:
        rows = cache.get("items") if isinstance(cache.get("items"), list) else []
    if not rows:
        return

    by_nm_id, by_vendor_code = _goods_lookup(organization_id)
    for item in rows:
        if not isinstance(item, dict):
            continue
        cogs_kopecks = _positive_int_or_none(item.get("cogsKopecks"))
        p_min_kopecks = _positive_int_or_none(item.get("pMinKopecks"))
        p_max_kopecks = _positive_int_or_none(item.get("pMaxKopecks"))
        if cogs_kopecks is None and p_min_kopecks is None and p_max_kopecks is None:
            continue

        article_id = str(item.get("articleId") or "").strip()
        nm_id = _positive_int_or_none(item.get("nmId") or item.get("nmID"))
        if not article_id and nm_id is not None:
            article_id = by_nm_id.get(nm_id, "")
        if not article_id:
            vendor_code = str(item.get("vendorCode") or "").strip()
            mapped_nm_id = by_vendor_code.get(vendor_code.lower()) if vendor_code else None
            article_id = by_nm_id.get(mapped_nm_id, vendor_code) if mapped_nm_id is not None else vendor_code
        if not article_id:
            continue

        existing = repricer_bff_module.SKU_SETTINGS_OVERRIDES.get(article_id)
        has_existing_settings = isinstance(existing, dict)
        next_settings = dict(existing) if has_existing_settings else repricer_bff_module._sku_cost_settings(article_id, use_demo_data=False)
        if cogs_kopecks is not None and (not has_existing_settings or not _positive_int_or_none(next_settings.get("cogsKopecks"))):
            next_settings["cogsKopecks"] = cogs_kopecks
        if p_min_kopecks is not None and (not has_existing_settings or not _positive_int_or_none(next_settings.get("pMinKopecks") or next_settings.get("pminKopecks"))):
            next_settings["pMinKopecks"] = p_min_kopecks
        if p_max_kopecks is not None and (not has_existing_settings or not _positive_int_or_none(next_settings.get("pMaxKopecks"))):
            next_settings["pMaxKopecks"] = p_max_kopecks
        repricer_bff_module.SKU_SETTINGS_OVERRIDES[article_id] = next_settings


def _hydrate_org_repricer_state(request: Request) -> int:
    actor = actor_from_request(request)
    hydrate_repricer_bff_state(actor.organization_id, repricer_bff_module)
    _apply_costs_excel_cache_to_sku_settings(actor.organization_id)
    return actor.organization_id


def _flush_org_repricer_state(organization_id: int) -> bool:
    return flush_repricer_bff_state(organization_id, repricer_bff_module)


def _refresh_wb_data_sources_and_flush(**kwargs: Any) -> dict[str, Any]:
    organization_id = int(kwargs["organization_id"])
    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    try:
        repricer_bff_module.fetch_commission_tariffs(
            kwargs.get("scenario") or "complete",
            wb_token=kwargs.get("wb_token"),
            force=True,
            organization_id=organization_id,
        )
        result = refresh_wb_data_sources(**kwargs)
        range_start, range_end, resolved_period_days, period_suffix = _repricer_period_context(
            int(kwargs.get("period_days") or 30),
            kwargs.get("date_from"),
            kwargs.get("date_to"),
        )
        _build_repricer_sku_snapshot(
            organization_id,
            kwargs.get("scenario") or "complete",
            wb_token=None,
            resolved_period_days=resolved_period_days,
            period_suffix=period_suffix,
            range_start=range_start,
            range_end=range_end,
        )
        return result
    finally:
        _flush_org_repricer_state(organization_id)


def _request_actor_and_wb_token(request: Request) -> tuple[Any, str]:
    actor = actor_from_request(request)
    token = get_user_wb_token_secret(actor.user_id)
    if not token:
        raise HTTPException(status_code=409, detail="WB_TOKEN_REQUIRED")
    return actor, token


def _request_wb_token(request: Request) -> str | None:
    actor = actor_from_request(request)
    token = get_user_wb_token_secret(actor.user_id)
    if not token:
        raise HTTPException(status_code=409, detail="WB_TOKEN_REQUIRED")
    return token


def _cached_read_wb_token(request: Request) -> str | None:
    """Select the cached cabinet connection without requiring a personal credential."""
    actor = actor_from_request(request)
    return get_user_wb_token_secret(actor.user_id) or get_organization_wb_token_secret(actor.organization_id)


def _ensure_wb_sync_not_running(organization_id: int) -> None:
    if is_wb_sync_running(organization_id):
        raise HTTPException(status_code=409, detail="WB_SYNC_RUNNING")


def _build_repricing_client(scenario: str, wb_token: str | None) -> RateLimitedWbApiClient:
    token_override = wb_token if getattr(get_settings(), "wb_api_mode", "fake") == "real" else None
    return RateLimitedWbApiClient(inner=build_wb_client(scenario, token_override=token_override))


def _team_user_by_id(organization_id: int, user_id: str | None) -> Any | None:
    if not user_id:
        return None
    return next((user for user in list_team_users(organization_id) if user.userId == user_id), None)


def _fallback_manager_name(manager_user_id: str | None) -> str | None:
    if not manager_user_id:
        return None
    if manager_user_id in LEGACY_REPRICER_MANAGERS:
        return LEGACY_REPRICER_MANAGERS[manager_user_id]
    if manager_user_id.startswith("manager-"):
        parts = [part for part in manager_user_id.removeprefix("manager-").replace("_", "-").split("-") if part]
        if parts:
            return " ".join(part.capitalize() for part in parts)
    return None


def _strip_internal_promotion_fields(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    payload.pop("articleIds", None)
    return payload


def _ensure_import_permission(actor: Any) -> None:
    if not (has_permission(actor, "settings:write") or has_permission(actor, "price:send")):
        raise HTTPException(status_code=403, detail="NO_ACCESS:settings:write")


def _ensure_price_send_permission(actor: Any) -> None:
    if not has_permission(actor, "price:send"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:price:send")


def _goods_lookup(organization_id: int) -> tuple[dict[int, str], dict[str, int]]:
    by_nm_id: dict[int, str] = {}
    by_vendor_code: dict[str, int] = {}
    for good in list_cached_goods(organization_id):
        vendor_code = str(good.get("vendorCode") or "").strip()
        try:
            nm_id = int(good.get("nmID") or good.get("nmId") or 0)
        except (TypeError, ValueError):
            nm_id = 0
        if vendor_code and nm_id > 0:
            by_nm_id[nm_id] = vendor_code
            by_vendor_code[vendor_code.lower()] = nm_id
    return by_nm_id, by_vendor_code


def _excel_import_meta(filename: str, file_hash: str, mode: ImportMode, rows_total: int, rows_parsed: int) -> dict[str, Any]:
    return {
        "filename": filename,
        "fileHash": file_hash,
        "mode": mode,
        "rowsTotal": rows_total,
        "rowsParsed": rows_parsed,
        "importedAt": datetime.now(timezone.utc).isoformat(),
    }


def _append_import_history(organization_id: int, source_key: str, entry: dict[str, Any]) -> None:
    cache = get_source_cache(organization_id, "excel_imports", slim=False) or {}
    history = cache.get("history") if isinstance(cache.get("history"), list) else []
    history.insert(0, {"source": source_key, **entry})
    save_source_cache(organization_id, "excel_imports", {"history": history[:50]})


async def _extract_uploaded_filename(request: Request) -> str | None:
    body = await request.body()
    match = re.search(rb'filename="([^"]+)"', body)
    if match is None:
        return None
    return match.group(1).decode("utf-8", errors="ignore") or None


async def _extract_uploaded_files(request: Request) -> list[tuple[str, bytes]]:
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    boundary_match = re.search(r"boundary=([^;]+)", content_type)
    if boundary_match is None:
        filename = await _extract_uploaded_filename(request)
        return [(filename or "upload.xlsx", body)] if body else []
    boundary = boundary_match.group(1).strip().strip('"').encode()
    files: list[tuple[str, bytes]] = []
    for part in body.split(b"--" + boundary):
        if b"filename=" not in part or b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        filename_match = re.search(rb'filename="([^"]+)"', header_blob)
        if filename_match is None:
            continue
        filename = filename_match.group(1).decode("utf-8", errors="ignore") or "upload.xlsx"
        content = content.rstrip(b"\r\n")
        if content.endswith(b"--"):
            content = content[:-2].rstrip(b"\r\n")
        files.append((filename, content))
    return files


def _promotion_thresholds_cache(organization_id: int) -> dict[str, Any]:
    cache = get_source_cache(organization_id, "promotion_thresholds") or {}
    cache.setdefault("byPromotionId", {})
    cache.setdefault("files", [])
    return cache


def _threshold_nm_ids(import_payload: dict[str, Any]) -> list[int]:
    nm_ids: list[int] = []
    for row in import_payload.get("thresholds") or []:
        try:
            nm_ids.append(int(row.get("nmId")))
        except (TypeError, ValueError):
            continue
    return sorted(set(nm_ids))


def _apply_promotion_threshold_cache(promotions: list[dict[str, Any]], thresholds_cache: dict[str, Any]) -> list[dict[str, Any]]:
    by_promotion = thresholds_cache.get("byPromotionId") if isinstance(thresholds_cache.get("byPromotionId"), dict) else {}
    result: list[dict[str, Any]] = []
    for promotion in promotions:
        row = dict(promotion)
        imported = by_promotion.get(str(row.get("id"))) if isinstance(by_promotion, dict) else None
        if isinstance(imported, dict):
            nm_ids = _threshold_nm_ids(imported)
            statuses_by_nm_id: dict[str, str] = {}
            for threshold in imported.get("thresholds") or []:
                if not isinstance(threshold, dict):
                    continue
                nm_id = threshold.get("nmId")
                wb_status = threshold.get("wbStatus")
                if nm_id is None or not wb_status:
                    continue
                statuses_by_nm_id[str(nm_id)] = str(wb_status)
            status = "loaded" if nm_ids else (imported.get("status") or "error")
            row.update(
                {
                    "excelLoaded": bool(nm_ids),
                    "excelStatus": status,
                    "excelFileName": imported.get("originalFilename"),
                    "excelLoadedAt": imported.get("importedAt"),
                    "thresholdRowsParsed": imported.get("rowsParsed") or len(nm_ids),
                    "thresholdRowsTotal": imported.get("rowsTotal"),
                    "thresholdNmIds": nm_ids,
                    "thresholdStatusesByNmId": statuses_by_nm_id,
                    "excelErrorText": imported.get("errorText"),
                }
            )
        else:
            row.setdefault("excelLoaded", False)
            row.setdefault("excelStatus", "missing")
            row.setdefault("thresholdNmIds", [])
        result.append(row)
    return result


def _cache_import_payload(parsed: ParsedPromotionExcel, *, promotion_id: str | None, imported_at: str) -> dict[str, Any]:
    return {
        "promotionId": promotion_id,
        "originalFilename": parsed.original_filename,
        "normalizedFilename": parsed.normalized_filename,
        "fileHash": parsed.file_hash,
        "status": parsed.status,
        "rowsTotal": parsed.rows_total,
        "rowsParsed": parsed.rows_parsed,
        "thresholds": parsed.thresholds,
        "errorText": parsed.error_text,
        "importedAt": imported_at,
    }


def _find_promotion_for_file(promotions: list[dict[str, Any]], filename: str) -> str | None:
    normalized = normalized_filename(filename)
    for promotion in promotions:
        if normalized_filename(str(promotion.get("name") or "")) == normalized:
            return str(promotion.get("id"))
    for promotion in promotions:
        promotion_name = normalized_filename(str(promotion.get("name") or ""))
        if len(normalized) >= 6 and len(promotion_name) >= 6 and (
            normalized in promotion_name or promotion_name in normalized
        ):
            return str(promotion.get("id"))
    return None


def _promotion_name_from_normalized(normalized: str) -> str:
    value = normalized.strip() or "Импортированная акция WB"
    return value[:1].upper() + value[1:]


def _imported_promotion_from_excel(parsed: ParsedPromotionExcel) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    promotion_hash = hashlib.sha256(parsed.normalized_filename.encode("utf-8")).hexdigest()[:16]
    status_counts = {"participating": 0}
    for threshold in parsed.thresholds:
        wb_status = str(threshold.get("wbStatus") or "").lower()
        if wb_status and wb_status.startswith("не участвует"):
            continue
        status_counts["participating"] += 1
    eligible = parsed.rows_parsed or parsed.rows_total
    participating = min(status_counts["participating"], eligible)
    return {
        "id": f"excel-{promotion_hash}",
        "name": _promotion_name_from_normalized(parsed.normalized_filename),
        "type": "auto",
        "status": "active",
        "startDate": today.isoformat(),
        "endDate": (today + timedelta(days=30)).isoformat(),
        "daysUntilEnd": 30,
        "daysUntilStart": 0,
        "eligibleSkuCount": eligible,
        "participatingSkuCount": participating,
        "participationPct": round((participating / eligible) * 100) if eligible else 0,
        "excelImportOnly": True,
    }


def _save_promotion_import(
    organization_id: int,
    *,
    promotion_id: str,
    parsed: ParsedPromotionExcel,
    promotions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    imported_at = datetime.now(timezone.utc).isoformat()
    thresholds_cache = _promotion_thresholds_cache(organization_id)
    import_payload = _cache_import_payload(parsed, promotion_id=promotion_id, imported_at=imported_at)
    thresholds_cache["byPromotionId"][str(promotion_id)] = import_payload
    thresholds_cache["files"] = [
        file
        for file in thresholds_cache.get("files", [])
        if not (
            isinstance(file, dict)
            and file.get("promotionId") == str(promotion_id)
            and file.get("fileHash") == parsed.file_hash
        )
    ]
    thresholds_cache["files"].append({k: v for k, v in import_payload.items() if k != "thresholds"})
    saved_thresholds = save_source_cache(organization_id, "promotion_thresholds", thresholds_cache)
    merged_promotions = _apply_promotion_threshold_cache(promotions, thresholds_cache)
    save_source_cache(
        organization_id,
        "promotions",
        {"promotions": _promotions_for_cache(merged_promotions), "count": len(merged_promotions)},
    )
    promotion = next((item for item in merged_promotions if str(item.get("id")) == str(promotion_id)), None)
    if promotion is None:
        raise HTTPException(status_code=404, detail="PROMOTION_NOT_FOUND")
    return promotion, saved_thresholds


def _ads_cache_totals(ads_cache: dict[str, Any]) -> dict[str, int]:
    keys = (
        "adSpendKopecks",
        "adImpressions",
        "adClicks",
        "adCartAdds",
        "adOrders",
        "adRevenueKopecks",
    )
    totals = {key: 0 for key in keys}
    raw_totals = ads_cache.get("totals")
    if isinstance(raw_totals, dict):
        for key in keys:
            totals[key] = _int_or_zero(raw_totals.get(key))
        if any(totals.values()):
            return totals
    aggregates = ads_cache.get("aggregates")
    if isinstance(aggregates, dict):
        for row in aggregates.values():
            if not isinstance(row, dict):
                continue
            for key in keys:
                totals[key] += _int_or_zero(row.get(key))
    return totals


def _latest_ads_sync_step(organization_id: int) -> dict[str, Any] | None:
    status = get_wb_sync_status(organization_id)
    steps = status.get("steps") if isinstance(status, dict) else None
    if not isinstance(steps, list):
        return None
    for step in reversed(steps):
        if isinstance(step, dict) and step.get("source") == "ads":
            return step
    return None


def _cache_matches_range(cache: dict[str, Any], range_start: datetime, range_end: datetime) -> bool:
    return (
        str(cache.get("dateFrom") or "") == range_start.date().isoformat()
        and str(cache.get("dateTo") or "") == range_end.date().isoformat()
    )


def _period_suffix_requires_range(period_suffix: str) -> bool:
    return "_" in str(period_suffix or "")


def _parse_cache_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _cache_covers_range(cache: dict[str, Any], range_start: datetime, range_end: datetime) -> bool:
    cache_from = _parse_cache_date(cache.get("dateFrom"))
    cache_to = _parse_cache_date(cache.get("dateTo"))
    return bool(cache_from and cache_to and cache_from <= range_start.date() and cache_to >= range_end.date())


def _merge_aggregate_row(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key.startswith("_"):
            continue
        if value is None:
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
        if isinstance(value, dict):
            continue
        if key not in target:
            target[key] = value


def _rollup_daily_aggregates(daily_aggregates: dict[str, Any], range_start: datetime, range_end: datetime) -> dict[str, dict[str, Any]]:
    rolled: dict[str, dict[str, Any]] = {}
    payable_evidence: dict[str, list[bool]] = {}
    components_evidence: dict[str, list[bool]] = {}
    missing_order_amount: set[str] = set()
    for day_key, day_rows in daily_aggregates.items():
        day = _parse_cache_date(day_key)
        if day is None or day < range_start.date() or day > range_end.date() or not isinstance(day_rows, dict):
            continue
        for nm_id, source_row in day_rows.items():
            if not isinstance(source_row, dict):
                continue
            row = rolled.setdefault(str(nm_id), {})
            if "orderCount" in source_row and source_row.get("orderSumKopecks") is None:
                missing_order_amount.add(str(nm_id))
            _merge_aggregate_row(row, source_row)
            payable_evidence.setdefault(str(nm_id), []).append(repricer_bff_module.settlement_payable_confirmed(source_row))
            components_evidence.setdefault(str(nm_id), []).append(all(type(source_row.get(key)) is int for key in (
                "payableKopecks", "logisticsKopecks", "storageKopecks", "acceptanceKopecks", "penaltyKopecks",
                "deductionKopecks", "loyaltyCostKopecks", "adSpendKopecks", "additionalPaymentKopecks")))
    for nm_id in missing_order_amount:
        rolled[nm_id]["orderSumKopecks"] = None
    for nm_id, evidence in payable_evidence.items():
        if "payableKopecks" in rolled[nm_id]:
            rolled[nm_id]["settlementPayableConfirmed"] = all(evidence)
            rolled[nm_id]["settlementComponentsConfirmed"] = all(components_evidence[nm_id])
    if range_start.date() != range_end.date():
        for row in rolled.values():
            # A period conversion cannot be reconstructed from daily percentages.
            if "buyoutPct" in row:
                row["buyoutPct"] = None
                row["buyoutSource"] = None
    return rolled


def _preserve_strategy_unknowns(aggregates, daily, prefix, date_from, date_to):
    fields = ("orderCount", "cartCount") if prefix == "baskets" else ("ordersUnits",)
    days = _iter_date_range(date_from, date_to)
    for nm, total in aggregates.items():
        for field in fields:
            values = []
            for day in days:
                rows = daily.get(day.isoformat())
                row = rows.get(nm) if isinstance(rows, dict) else None
                complete = (isinstance(row, dict) and row.get("coverageState", "ok") in {"ok", "complete"}
                    and row.get("aggregateComplete", True) is True)
                values.append(row.get(field) if complete else None)
            if any(type(value) is not int or value < 0 for value in values):
                total[field] = None


def _covered_cache_from_daily(
    cache: dict[str, Any],
    *,
    prefix: str,
    period_suffix: str,
    resolved_period_days: int,
    range_start: datetime,
    range_end: datetime,
    strategy_fingerprint: str | None = None,
) -> dict[str, Any]:
    if not _cache_covers_range(cache, range_start, range_end):
        return {}
    daily_aggregates = cache.get("dailyAggregates")
    if not isinstance(daily_aggregates, dict):
        return {}
    if prefix == "baskets" and not _baskets_range_has_daily_detail(cache, range_start.date(), range_end.date()):
        return {}
    aggregates = _rollup_daily_aggregates(daily_aggregates, range_start, range_end)
    if strategy_fingerprint is not None:
        _preserve_strategy_unknowns(aggregates, daily_aggregates, prefix, range_start.date(), range_end.date())
    if not aggregates and cache.get("aggregates"):
        return {}
    previous_end = range_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=resolved_period_days - 1)
    previous_aggregates: dict[str, dict[str, Any]] = {}
    if _cache_covers_range(cache, previous_start, previous_end):
        previous_aggregates = _rollup_daily_aggregates(daily_aggregates, previous_start, previous_end)
        for nm_id, row in aggregates.items():
            previous = previous_aggregates.get(nm_id)
            if previous:
                row["previous"] = previous
    payload = dict(cache)
    payload["aggregates"] = aggregates
    payload["count"] = len(aggregates)
    payload["dateFrom"] = range_start.date().isoformat()
    payload["dateTo"] = range_end.date().isoformat()
    payload["periodDays"] = resolved_period_days
    payload["periodCacheSuffix"] = period_suffix
    payload["coveredByCache"] = {
        "source": prefix,
        "dateFrom": cache.get("dateFrom"),
        "dateTo": cache.get("dateTo"),
        "periodDays": cache.get("periodDays"),
        "periodCacheSuffix": cache.get("periodCacheSuffix"),
    }
    if prefix == "baskets":
        # WB's order-cohort conversion cannot be reconstructed by summing days.
        payload["periodConversionState"] = "no_data"
    if prefix == "ads":
        payload["totals"] = _ads_cache_totals(payload)
    elif prefix == "finance":
        # Period-wide unassigned charges cannot be copied into a shorter range.
        payload["diagnostics"] = repricer_bff_module.build_finance_diagnostics_from_aggregates(aggregates)
        payload["diagnostics"].update(dateFrom=payload["dateFrom"], dateTo=payload["dateTo"])
    return payload


def _sync_status_covering_keys(organization_id: int, prefix: str, range_start: datetime, range_end: datetime) -> list[str]:
    status = get_wb_sync_status(organization_id)
    if not isinstance(status, dict):
        return []
    status_from = _parse_cache_date(status.get("dateFrom"))
    status_to = _parse_cache_date(status.get("dateTo"))
    if not status_from or not status_to or status_from > range_start.date() or status_to < range_end.date():
        return []
    keys: list[str] = []
    suffix = str(status.get("periodCacheSuffix") or "").strip()
    if suffix:
        keys.append(f"{prefix}_{suffix}")
    period_days = status.get("periodDays")
    if period_days:
        keys.append(f"{prefix}_{period_days}")
    return list(dict.fromkeys(keys))


def _compatible_period_source_cache(prefix: str, cache: dict[str, Any]) -> dict[str, Any]:
    return cache if prefix != "finance" or (finance_cache_uses_current_revenue_basis(cache) and not cache.get("revenueOnly")) else {}


def _load_period_source_cache(
    organization_id: int, prefix: str, period_suffix: str, resolved_period_days: int,
    range_start: datetime, range_end: datetime, *, slim: bool = True,
    require_full_sync_coverage: bool = True, prefer_freshest_covering: bool = False,
    strategy_fingerprint: str | None = None, strategy_now: datetime | None = None,
) -> dict[str, Any]:
    from app.repricer_page_finance import page_projection, load_page_finance
    if prefix == "finance" and page_projection.get():
        return load_page_finance(
            organization_id,
            range_start.date() if isinstance(range_start, datetime) else range_start,
            range_end.date() if isinstance(range_end, datetime) else range_end,
        )
    result = _load_period_source_cache_base(
        organization_id, prefix, period_suffix, resolved_period_days, range_start, range_end,
        slim=slim, require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=prefer_freshest_covering,
        strategy_fingerprint=strategy_fingerprint, strategy_now=strategy_now,
    )
    if prefix == "finance":
        from app.wb_statistics_finance import load_statistics_finance
        persisted = load_statistics_finance(organization_id, range_start.date(), range_end.date())
        if persisted.get("sourceComplete") is True:
            return persisted
        if not result and persisted.get("coveredDays"):
            return persisted
    if prefix != "finance" or not result:
        return result
    ledger = _finance_period_from_operations(organization_id, range_start, range_end)
    if not ledger:
        return result
    result["revenueCoverage"] = {key: ledger[key] for key in ("coverageState", "coveredDays", "requestedDays", "missingDates")}
    result["revenueFetchedAt"] = ledger["fetchedAt"]
    if ledger["coverageState"] != "ok":
        return result
    # Correct revenue without substituting the trade ledger for expense/payout data.
    keys = ("sellerRevenueKopecks", "revenueGrossKopecks", "grossSalesKopecks", "returnsKopecks",
            "sellerRevenueRows", "sellerRevenueMissingRows")
    daily = result.setdefault("dailyAggregates", {})
    for rows, correct in [(result.setdefault("aggregates", {}), ledger["aggregates"]),
                          *[(daily.setdefault(day, {}), ledger["dailyAggregates"].get(day, {}))
                            for day in daily.keys() | ledger["dailyAggregates"].keys()]]:
        for nm in rows.keys() | correct.keys():
            row = rows.setdefault(nm, {})
            for key in keys:
                row[key] = correct.get(nm, {}).get(key, 0)
    return result


def _load_period_source_cache_base(
    organization_id: int,
    prefix: str,
    period_suffix: str,
    resolved_period_days: int,
    range_start: datetime,
    range_end: datetime,
    *,
    slim: bool = True,
    require_full_sync_coverage: bool = True,
    prefer_freshest_covering: bool = False,
    strategy_fingerprint: str | None = None,
    strategy_now: datetime | None = None,
) -> dict[str, Any]:
    if require_full_sync_coverage and not _full_sync_covers_range(organization_id, range_start, range_end)[0]:
        return {}

    def compatible(cache):
        cache = _compatible_period_source_cache(prefix, cache)
        if strategy_fingerprint is not None and not (
            source_binding_matches(cache, organization_id=organization_id, fingerprint=strategy_fingerprint)
            and source_observations_fresh(cache, now=strategy_now or datetime.now(timezone.utc), date_to=range_end.date().isoformat())
            and cache.get("coverageState", "ok") in {"ok", "complete"}
            and cache.get("aggregateComplete", True) is True
        ):
            return {}
        return cache

    exact_key = f"{prefix}_{period_suffix}"
    exact = compatible(get_source_cache(organization_id, exact_key, slim=slim) or {})
    if exact and (prefix in {"finance", "baskets"} or exact.get("source") == "seller_export") and _cache_matches_range(exact, range_start, range_end):
        # This report includes unassigned charges for precisely the requested dates.
        return exact
    if exact and prefer_freshest_covering:
        latest = _parse_utc_datetime(get_source_cache_range_revision(
            organization_id, f"{prefix}_", date_from=range_start.date(), date_to=range_end.date(),
        ))
        observed = _parse_utc_datetime(exact.get("fetchedAt"))
        prefer_freshest_covering = latest is None or observed is None or latest > observed
    if exact and prefer_freshest_covering:
        covering = compatible(
            get_covering_source_cache(
                organization_id,
                f"{prefix}_",
                date_from=range_start.date(),
                date_to=range_end.date(),
                slim=slim,
            ) or {},
        )
        if str(covering.get("fetchedAt") or "") > str(exact.get("fetchedAt") or ""):
            rolled = _covered_cache_from_daily(
                covering,
                prefix=prefix,
                period_suffix=period_suffix,
                resolved_period_days=resolved_period_days,
                range_start=range_start,
                range_end=range_end,
                strategy_fingerprint=strategy_fingerprint,
            )
            if rolled:
                return rolled
    if exact:
        has_range = bool(exact.get("dateFrom") or exact.get("dateTo"))
        if has_range:
            if _cache_matches_range(exact, range_start, range_end):
                return exact
        elif not _period_suffix_requires_range(period_suffix):
            return exact
    day_key = f"{prefix}_{resolved_period_days}"
    fallback = (
        compatible(
            get_source_cache(organization_id, day_key, slim=slim) or {},
        )
        if day_key != exact_key
        else {}
    )
    if fallback and _cache_matches_range(fallback, range_start, range_end):
        return fallback
    if prefix == "baskets" and strategy_fingerprint is not None:
        # A refreshed covering container may retain older days. Select each
        # requested day by its own evidence before projecting a shorter window.
        stitched = _stitched_period_cache_from_days(
            organization_id, prefix, range_start.date(), range_end.date(),
            strategy_fingerprint=strategy_fingerprint, strategy_now=strategy_now,
        )
        if stitched:
            stitched.update(periodDays=resolved_period_days, dateFrom=range_start.date().isoformat(),
                dateTo=range_end.date().isoformat(), source="stitched_daily_aggregates")
        return stitched
    covering = compatible(
        get_covering_source_cache(
            organization_id,
            f"{prefix}_",
            date_from=range_start.date(),
            date_to=range_end.date(),
            slim=slim,
        ) or {},
    )
    if covering:
        rolled = _covered_cache_from_daily(
            covering,
            prefix=prefix,
            period_suffix=period_suffix,
            resolved_period_days=resolved_period_days,
            range_start=range_start,
            range_end=range_end,
            strategy_fingerprint=strategy_fingerprint,
        )
        if rolled:
            return rolled
    for covering_key in _sync_status_covering_keys(organization_id, prefix, range_start, range_end):
        if covering_key in {exact_key, day_key}:
            continue
        covering = compatible(
            get_source_cache(organization_id, covering_key, slim=slim) or {},
        )
        rolled = _covered_cache_from_daily(
            covering,
            prefix=prefix,
            period_suffix=period_suffix,
            resolved_period_days=resolved_period_days,
            range_start=range_start,
            range_end=range_end,
            strategy_fingerprint=strategy_fingerprint,
        )
        if rolled:
            return rolled

    # Nothing stored spans the whole range.  Each profile writes its own
    # window, so a long custom range routinely straddles two of them; summing
    # the cached days keeps it from resolving to nothing and reporting zeros.
    stitched = _stitched_period_cache_from_days(
        organization_id, prefix, range_start.date(), range_end.date(),
        strategy_fingerprint=strategy_fingerprint, strategy_now=strategy_now,
    )
    if stitched:
        stitched.update(
            {
                "periodDays": resolved_period_days,
                "dateFrom": range_start.date().isoformat(),
                "dateTo": range_end.date().isoformat(),
                "source": "stitched_daily_aggregates",
            }
        )
        return stitched
    return {}


def _finance_period_from_operations(organization_id: int, start: datetime, end: datetime) -> dict[str, Any]:
    """Merge report observations by posting day, then filter by operation date.

    A later report may contain a sale from an earlier day. Replacing an entire
    sale-day aggregate loses that correction; adding overlapping reports doubles
    it. The latest complete observation owns each posting day; rrdId owns a row.
    """
    from app.repricer_bff import aggregate_finance_report_rows

    metas = list_source_cache_ranges_by_prefix(organization_id, "finance_", limit=100)
    owned: set[str] = set()
    operations: dict[str, dict[str, Any]] = {}
    fetched_at = None
    for meta in sorted(metas, key=lambda m: str(m.get("fetchedAt") or ""), reverse=True):
        left, right = _parse_cache_date(meta.get("dateFrom")), _parse_cache_date(meta.get("dateTo"))
        if not left or not right or right < start.date():
            continue
        report_days = {day.isoformat() for day in _iter_date_range(left, right)}
        available = report_days - owned
        if not available:
            continue
        cache = get_source_cache(organization_id, str(meta.get("sourceKey")), slim=False) or {}
        rows = cache.get("revenueOperations", cache.get("rows"))
        if cache.get("sourceComplete") is not True or not isinstance(rows, list):
            continue
        # Missing original seller prices cannot silently become zero revenue.
        if any(str(row.get("docTypeName") or "").lower() in {"продажа", "возврат"}
               and row.get("retailPriceWithDisc") is None for row in rows):
            continue
        for row in rows:
            posting_day = str(row.get("rrDate") or row.get("rr_dt") or "")[:10]
            identity = str(row.get("rrdId") or row.get("rrd_id") or "")
            if posting_day in available and identity and identity not in operations:
                operations[identity] = row
        owned.update(report_days)
        fetched_at = max(fetched_at or "", str(cache.get("fetchedAt") or meta.get("fetchedAt") or ""))
    wanted = {day.isoformat() for day in _iter_date_range(start.date(), end.date())}
    covered = wanted & owned
    if not covered:
        return {}
    result = aggregate_finance_report_rows(list(operations.values()), date_from=start, date_to=end)
    result.pop("rows", None)
    result.update(fetchedAt=fetched_at, source="finance_operations", sourceComplete=covered == wanted,
                  coverageState="ok" if covered == wanted else "partial", coveredDays=len(covered),
                  requestedDays=len(wanted), missingDates=sorted(wanted - covered),
                  periodDays=len(wanted))
    return result


def _stitched_period_cache_from_days(
    organization_id: int,
    prefix: str,
    date_from: date,
    date_to: date,
    *,
    strategy_fingerprint: str | None = None,
    strategy_now: datetime | None = None,
) -> dict[str, Any]:
    """Roll up observed days once, retaining freshness and missing-day coverage."""
    wanted = {
        (date_from + timedelta(days=offset)).isoformat()
        for offset in range((date_to - date_from).days + 1)
    }
    seen: set[str] = set()
    daily: dict[str, Any] = {}
    source_metadata: dict[str, Any] = {}
    contributors = {}
    strategy_daily = prefix == "baskets" and strategy_fingerprint is not None
    candidates: list[tuple[str, int, str, set[str]]] = []
    for meta in list_source_cache_ranges_by_prefix(organization_id, f"{prefix}_", limit=100):
        if not isinstance(meta, dict):
            continue
        source_key = str(meta.get("sourceKey") or "")
        if not source_key or "detail_status" in source_key or source_key.endswith("detail_active"):
            continue
        if meta.get("dailyAggregatesDays") == 0:
            continue
        meta_from = _parse_cache_date(meta.get("dateFrom"))
        meta_to = _parse_cache_date(meta.get("dateTo"))
        if meta_from and meta_to:
            if meta_to < date_from or meta_from > date_to:
                continue
            overlap = (min(meta_to, date_to) - max(meta_from, date_from)).days + 1
        else:
            overlap = 0
        known_days = set(meta.get("dailyAggregateDates") or ()) & wanted
        candidates.append((str(meta.get("fetchedAt") or ""), overlap, source_key, known_days))

    for _fetched_at, _overlap, source_key, known_days in sorted(candidates, key=lambda item: item[:2], reverse=True):
        if not strategy_daily and not wanted - seen:
            break
        if not strategy_daily and known_days and known_days <= seen:
            continue
        cache = _compatible_period_source_cache(
            prefix, get_source_cache(organization_id, source_key, slim=False) or {}
        )
        if strategy_fingerprint is not None and not (
            source_binding_matches(cache, organization_id=organization_id, fingerprint=strategy_fingerprint)
            and source_observations_fresh(cache, now=strategy_now or datetime.now(timezone.utc), date_to=date_to.isoformat())
            and cache.get("aggregateComplete", True) is True
            and cache.get("coverageState", "ok") in ({"ok", "complete", "partial"} if prefix == "baskets" else {"ok", "complete"})
        ):
            continue
        raw_daily = cache.get("dailyAggregates")
        if not isinstance(raw_daily, dict):
            continue
        if prefix == "finance":
            source_metadata = {key: cache[key] for key in ("revenueBasis", "financeSchemaVersion")}
        for raw_day, rows in raw_daily.items():
            day = str(raw_day)
            if day not in wanted or (day in seen and not strategy_daily) or not isinstance(rows, dict):
                continue
            if prefix == "baskets" and not _baskets_range_has_daily_detail(cache, date.fromisoformat(day), date.fromisoformat(day)):
                continue
            evidence = daily_source_evidence(cache, day) if prefix == "baskets" else cache
            if strategy_fingerprint is not None and not (
                source_binding_matches(evidence, organization_id=organization_id, fingerprint=strategy_fingerprint)
                and source_observations_fresh(evidence, now=strategy_now or datetime.now(timezone.utc), date_to=date_to.isoformat())
            ):
                continue
            observation = oldest_source_observation(evidence)
            if strategy_daily and day in contributors and observation <= contributors[day][1]:
                continue
            seen.add(day)
            daily[day] = rows
            binding = evidence.get("strategyTokenFingerprint")
            contributors[day] = (binding if isinstance(binding, str) and binding else None,
                                 observation, str(cache.get("fetchedAt") or _fetched_at))
    if not daily:
        return {}
    totals = _rollup_daily_aggregates(
        daily, datetime.combine(date_from, datetime.min.time()), datetime.combine(date_to, datetime.min.time())
    )
    if strategy_fingerprint is not None:
        _preserve_strategy_unknowns(totals, daily, prefix, date_from, date_to)
    coverage = {"coverageState": "ok" if wanted == seen else "partial", "coveredDays": len(seen), "requestedDays": len(wanted), "missingDates": sorted(wanted - seen)}
    if prefix == "baskets":
        source_metadata["periodConversionState"] = "no_data"
        for row in totals.values():
            row.update(coverage)
    bindings = {item[0] for item in contributors.values()}
    observations = [item[1] for item in contributors.values()]
    fetched_at = max(item[2] for item in contributors.values()) or None
    oldest_fetched_at = min(item[2] for item in contributors.values()) or None
    if len(bindings) == 1 and None not in bindings:
        source_metadata["strategyTokenFingerprint"] = next(iter(bindings))
    source_metadata["oldestSourceFetchedAt"] = (min(observations).isoformat()
        if observations and all(at is not None for at in observations) else None)
    return {"aggregates": totals, "dailyAggregates": daily, "count": len(totals), "fetchedAt": fetched_at,
            "oldestFetchedAt": oldest_fetched_at, **coverage, **source_metadata}


def _period_source_cache(
    organization_id: int,
    prefix: str,
    period_suffix: str,
    resolved_period_days: int,
    range_start: datetime,
    range_end: datetime,
    *,
    slim: bool = True,
    require_full_sync_coverage: bool = True,
    prefer_freshest_covering: bool = False,
    memo: dict[tuple[Any, ...], dict[str, Any]] | None = None,
    strategy_fingerprint: str | None = None,
    strategy_now: datetime | None = None,
) -> dict[str, Any]:
    cache_key = (
        organization_id,
        prefix,
        period_suffix,
        resolved_period_days,
        str(range_start),
        str(range_end),
        slim,
        require_full_sync_coverage,
        prefer_freshest_covering,
        strategy_fingerprint,
        strategy_now,
    )
    if memo is not None and cache_key in memo:
        return memo[cache_key]
    cache = _load_period_source_cache(
        organization_id,
        prefix,
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        slim=slim,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=prefer_freshest_covering,
        strategy_fingerprint=strategy_fingerprint, strategy_now=strategy_now,
    )
    if memo is not None:
        memo[cache_key] = cache
    return cache


def _range_payload(range_start: datetime | date, range_end: datetime | date) -> dict[str, str]:
    start = range_start.date() if isinstance(range_start, datetime) else range_start
    end = range_end.date() if isinstance(range_end, datetime) else range_end
    return {"from": start.isoformat(), "to": end.isoformat()}


REPRICER_CACHE_COVERAGE_SOURCES: tuple[tuple[str, str], ...] = (
    ("period-stats", "period_stats_"),
    ("finance", "finance_"),
    ("ads", "ads_"),
    ("baskets", "baskets_"),
)


def _iter_date_range(start: date, end: date) -> list[date]:
    days = max(1, min(370, (end - start).days + 1))
    return [start + timedelta(days=offset) for offset in range(days)]


def _cache_payload_covers_day(cache: dict[str, Any], day: date) -> bool:
    cache_from = _parse_cache_date(cache.get("dateFrom"))
    cache_to = _parse_cache_date(cache.get("dateTo"))
    if cache_from and cache_to:
        return cache_from <= day <= cache_to
    daily = cache.get("dailyAggregates")
    return isinstance(daily, dict) and day.isoformat() in daily


def _cache_range_from_meta(cache: dict[str, Any]) -> tuple[date, date] | None:
    cache_from = _parse_cache_date(cache.get("dateFrom"))
    cache_to = _parse_cache_date(cache.get("dateTo"))
    if cache_from and cache_to:
        return cache_from, cache_to
    return None


def _daily_aggregate_dates(cache: dict[str, Any]) -> set[str]:
    dates = cache.get("dailyAggregateDates")
    if isinstance(dates, list):
        return {str(item)[:10] for item in dates if item}
    daily = cache.get("dailyAggregates")
    if isinstance(daily, dict):
        return {str(key)[:10] for key in daily}
    return set()


def _baskets_range_has_daily_detail(cache: dict[str, Any], range_from: date, range_to: date) -> bool:
    required_days = (range_to - range_from).days + 1
    dates = _daily_aggregate_dates(cache)
    if cache.get("dailyDetailStatus") in {"partial", "pending", "failed", "paused", "deferred"}:
        batch_count = _int_or_zero(cache.get("dailyBatchCount"))
        if batch_count <= 0:
            return False
        completed = {(str(chunk.get("date") or ""), chunk.get("chunkIndex"))
            for chunk in (cache.get("chunks") or []) if isinstance(chunk, dict)
            and chunk.get("type") == "daily" and chunk.get("status") == "done"}
        if not all((day.isoformat(), index) in completed
            for day in _iter_date_range(range_from, range_to) for index in range(batch_count)):
            return False
    incomplete_dates = {
        str(chunk.get("date") or "")
        for chunk in (cache.get("chunks") or [])
        if isinstance(chunk, dict) and chunk.get("type") == "daily" and chunk.get("status") != "done"
    }
    if dates:
        required_dates = {day.isoformat() for day in _iter_date_range(range_from, range_to)}
        return required_dates.issubset(dates - incomplete_dates)
    daily_days = _int_or_zero(cache.get("dailyAggregatesDays"))
    return daily_days >= required_days


def _source_cache_can_cover_range(source: str, cache: dict[str, Any], range_from: date, range_to: date) -> bool:
    if source != "baskets":
        return True
    return _baskets_range_has_daily_detail(cache, range_from, range_to)


def _cache_coverage_days(organization_id: int, start: date, end: date) -> dict[str, dict[str, Any]]:
    requested_days = _iter_date_range(start, end)
    required_sources = [source for source, _prefix in REPRICER_CACHE_COVERAGE_SOURCES]
    coverage: dict[str, dict[str, Any]] = {
        day.isoformat(): {
            "date": day.isoformat(),
            "state": "missing",
            "availableSources": [],
            "missingSources": list(required_sources),
            "sourceFetchedAt": {},
        }
        for day in requested_days
    }
    for source, prefix in REPRICER_CACHE_COVERAGE_SOURCES:
        caches = list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100)
        ranges = [
            (cache_range[0], cache_range[1], str(cache.get("fetchedAt") or ""))
            for cache in caches
            if isinstance(cache, dict)
            for cache_range in [_cache_range_from_meta(cache)]
            if cache_range is not None
            if _source_cache_can_cover_range(source, cache, cache_range[0], cache_range[1])
        ]
        for range_from, range_to, fetched_at in ranges:
            for day in requested_days:
                if day < range_from or day > range_to:
                    continue
                item = coverage[day.isoformat()]
                available = item["availableSources"]
                if source not in available:
                    available.append(source)
                if fetched_at:
                    item["sourceFetchedAt"][source] = fetched_at
    for item in coverage.values():
        available = set(item["availableSources"])
        missing = [source for source in required_sources if source not in available]
        item["missingSources"] = missing
        item["state"] = "complete" if not missing else "partial" if available else "missing"
    return coverage


PROFILE_RANGE_SOURCE_PREFIXES: dict[str, str] = {
    "period-stats": "period_stats_",
    "finance": "finance_",
    "ads": "ads_",
    "baskets": "baskets_",
}


def _profile_source_readiness(organization_id: int, profile: WbSyncProfile) -> list[dict[str, Any]]:
    readiness: list[dict[str, Any]] = []
    goods_meta_cache: dict[str, Any] | None = None

    def goods_meta_once() -> dict[str, Any]:
        nonlocal goods_meta_cache
        if goods_meta_cache is None:
            goods_meta_cache = cached_goods_meta(organization_id)
        return goods_meta_cache

    for source in profile.sources:
        item: dict[str, Any] = {"source": source, "ready": False, "reason": "cache_missing"}
        if source == "goods":
            meta = goods_meta_once()
            total = _int_or_zero(meta.get("totalCached"))
            item.update(
                {
                    "ready": total > 0,
                    "reason": "ready" if total > 0 else "goods_cache_empty",
                    "count": total,
                    "fetchedAt": meta.get("latestFetchedAt"),
                }
            )
        elif source == "content":
            cache = get_source_cache(organization_id, "content_cards", slim=True) or {}
            count = _int_or_zero(cache.get("count"))
            item.update({"ready": count > 0, "reason": "ready" if count > 0 else "content_cache_empty", "count": count})
        elif source == "promotions":
            cache = get_source_cache(organization_id, "promotions", slim=True) or {}
            count = _int_or_zero(cache.get("count"))
            item.update({"ready": count > 0, "reason": "ready" if count > 0 else "promotions_cache_empty", "count": count})
        elif source == "stocks":
            cache = get_source_cache(organization_id, "stocks", slim=True) or {}
            count = _int_or_zero(cache.get("count"))
            item.update({"ready": count > 0, "reason": "ready" if count > 0 else "stocks_cache_empty", "count": count})
        elif source in PROFILE_RANGE_SOURCE_PREFIXES:
            prefix = PROFILE_RANGE_SOURCE_PREFIXES[source]
            required_days = max(1, (profile.date_to - profile.date_from).days + 1)
            saw_cache = False
            saw_covering_range = False
            best_range: tuple[date, date] | None = None
            for cache in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100):
                if not isinstance(cache, dict):
                    continue
                cache_range = _cache_range_from_meta(cache)
                if cache_range is None:
                    continue
                saw_cache = True
                cache_from, cache_to = cache_range
                if best_range is None or cache_to > best_range[1]:
                    best_range = cache_range
                if cache_from > profile.date_from or cache_to < profile.date_to:
                    continue
                saw_covering_range = True
                item.update(
                    {
                        "dateFrom": cache_from.isoformat(),
                        "dateTo": cache_to.isoformat(),
                        "fetchedAt": cache.get("fetchedAt"),
                        "requiredDays": required_days,
                    }
                )
                if source == "baskets" and profile.baskets_daily_detail:
                    daily_days = _int_or_zero(cache.get("dailyAggregatesDays"))
                    item.update(
                        {
                            "dailyAggregatesDays": daily_days,
                            "dailyDetailStatus": cache.get("dailyDetailStatus"),
                            "dailyDetailError": cache.get("dailyDetailError"),
                            "dailyDetailDeferredAt": cache.get("dailyDetailDeferredAt"),
                            "dailyDetailPausedAt": cache.get("dailyDetailPausedAt"),
                            "dailyDetailFailedAt": cache.get("dailyDetailFailedAt"),
                            "dailyDetailFetchedAt": cache.get("dailyDetailFetchedAt"),
                            "dailyDetailPartialAt": cache.get("dailyDetailPartialAt"),
                            "dailyDetailPreservedAt": cache.get("dailyDetailPreservedAt"),
                            "dailyDetailPreservedBy": cache.get("dailyDetailPreservedBy"),
                            "dailyDetailRequestsCompleted": cache.get("dailyDetailRequestsCompleted"),
                            "dailyDetailRequestsTotal": cache.get("dailyDetailRequestsTotal"),
                        }
                    )
                    if not _baskets_range_has_daily_detail(cache, profile.date_from, profile.date_to):
                        item.update({"reason": "baskets_daily_detail_missing"})
                        continue
                item.update({"ready": True, "reason": "ready"})
                break
            if not item["ready"]:
                if saw_covering_range and item.get("reason") == "baskets_daily_detail_missing":
                    pass
                elif best_range:
                    item.update(
                        {
                            "reason": "range_not_covered" if saw_cache else "cache_missing",
                            "dateFrom": best_range[0].isoformat(),
                            "dateTo": best_range[1].isoformat(),
                            "requiredDays": required_days,
                        }
                    )
                else:
                    item.update({"reason": "cache_missing", "requiredDays": required_days})
        readiness.append(item)
    return readiness


def _sync_profile_cache_window_ready(organization_id: int, profile: WbSyncProfile) -> bool:
    return all(item.get("ready") for item in _profile_source_readiness(organization_id, profile))


@router.get("/api/v1/wb-repricer/cache/coverage")
def get_repricer_cache_coverage(
    request: Request,
    date_from: date = Query(..., alias="dateFrom"),
    date_to: date = Query(..., alias="dateTo"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="dateTo must be on or after dateFrom")
    safe_days = _iter_date_range(date_from, date_to)
    safe_to = safe_days[-1]
    coverage = _cache_coverage_days(actor.organization_id, date_from, safe_to)
    days = [coverage[day.isoformat()] for day in safe_days]
    complete_count = sum(1 for day in days if day.get("state") == "complete")
    partial_count = sum(1 for day in days if day.get("state") == "partial")
    missing_count = sum(1 for day in days if day.get("state") == "missing")
    return {
        "dateFrom": date_from.isoformat(),
        "dateTo": safe_to.isoformat(),
        "sources": [source for source, _prefix in REPRICER_CACHE_COVERAGE_SOURCES],
        "days": days,
        "summary": {
            "totalDays": len(days),
            "completeDays": complete_count,
            "partialDays": partial_count,
            "missingDays": missing_count,
        },
    }


def _full_sync_covers_range(
    organization_id: int,
    requested_start: datetime | date,
    requested_end: datetime | date,
) -> tuple[bool, dict[str, str] | None]:
    status = get_wb_sync_status(organization_id)
    source_from = _parse_cache_date(status.get("dateFrom"))
    source_to = _parse_cache_date(status.get("dateTo"))
    start = requested_start.date() if isinstance(requested_start, datetime) else requested_start
    end = requested_end.date() if isinstance(requested_end, datetime) else requested_end
    status_range = _range_payload(source_from, source_to) if source_from and source_to else None
    if str(status.get("state") or "") == "completed" and source_from and source_to and source_from <= start and source_to >= end:
        return True, _range_payload(source_from, source_to)

    covering_ranges: list[tuple[date, date]] = []
    for source, prefix in REPRICER_CACHE_COVERAGE_SOURCES:
        source_ranges: list[tuple[date, date]] = []
        source_caches = list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100)
        for cache in source_caches:
            if not isinstance(cache, dict):
                continue
            cache_from = _parse_cache_date(cache.get("dateFrom"))
            cache_to = _parse_cache_date(cache.get("dateTo"))
            if (
                cache_from
                and cache_to
                and cache_from <= start
                and cache_to >= end
                and _source_cache_can_cover_range(source, cache, start, end)
            ):
                source_ranges.append((cache_from, cache_to))
        if not source_ranges and source == "baskets":
            required_dates = {day.isoformat() for day in _iter_date_range(start, end)}
            available_dates = {
                day
                for cache in source_caches
                if isinstance(cache, dict)
                for day in _daily_aggregate_dates(cache)
                if start.isoformat() <= day <= end.isoformat()
            }
            if required_dates.issubset(available_dates):
                source_ranges.append((start, end))
        if not source_ranges:
            return False, status_range
        covering_ranges.append(max(source_ranges, key=lambda item: (item[1] - item[0]).days))
    if not covering_ranges:
        return False, status_range
    fallback_from = max(item[0] for item in covering_ranges)
    fallback_to = min(item[1] for item in covering_ranges)
    return fallback_from <= start and fallback_to >= end, _range_payload(fallback_from, fallback_to)


def _repricer_stats_cache_context(
    organization_id: int,
    requested_start: datetime,
    requested_end: datetime,
) -> dict[str, Any]:
    status = get_wb_sync_status(organization_id)
    source_from = _parse_cache_date(status.get("dateFrom"))
    source_to = _parse_cache_date(status.get("dateTo"))
    requested_range = _range_payload(requested_start, requested_end)
    context: dict[str, Any] = {
        "requestedRange": requested_range,
        "statsSourceRange": None,
        "statsSourceStatus": "missing",
        "statsRangeAdjusted": False,
        "dateFrom": requested_start.date(),
        "dateTo": requested_end.date(),
        "periodDays": (requested_end.date() - requested_start.date()).days + 1,
    }
    if not source_from or not source_to:
        return context

    context["statsSourceRange"] = _range_payload(source_from, source_to)
    if source_from <= requested_start.date() and source_to >= requested_end.date():
        context["statsSourceStatus"] = "ready"
        return context

    context["statsSourceStatus"] = "ready"
    context["statsRangeAdjusted"] = True
    context["dateFrom"] = source_from
    context["dateTo"] = source_to
    context["periodDays"] = (source_to - source_from).days + 1
    return context


def _save_finance_source_cache(
    organization_id: int,
    *,
    scenario: str,
    wb_token: str | None,
    range_start: datetime,
    range_end: datetime,
    resolved_period_days: int,
    period_suffix: str,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    payload = fetch_finance_report_aggregates(scenario, wb_token=wb_token, date_from=range_start, date_to=range_end)
    canonical_snapshot = shadow_ingest_legacy_finance_payload(
        organization_id,
        payload,
        observed_at=datetime.now(timezone.utc),
    )
    finance_aggregates = payload.get("aggregates") if isinstance(payload.get("aggregates"), dict) else {}
    cached_goods_nm_ids = {str(nm_id) for nm_id in _nm_ids_from_goods(list_cached_goods(organization_id))}
    matched_cached_goods_nm_ids = len(set(finance_aggregates.keys()) & cached_goods_nm_ids)
    cached = save_source_cache(
        organization_id,
        f"finance_{period_suffix}",
        {
            **payload,
            "canonicalSnapshot": canonical_snapshot,
            "periodDays": resolved_period_days,
            "cachedGoodsNmIds": len(cached_goods_nm_ids),
            "matchedCachedGoodsNmIds": matched_cached_goods_nm_ids,
        },
    )
    return payload, cached, len(cached_goods_nm_ids), matched_cached_goods_nm_ids


def _limited_finance_diagnostics(
    cache: dict[str, Any], limit: int, *, finance_taxes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    aggregates = cache.get("aggregates") if isinstance(cache.get("aggregates"), dict) else {}
    requested_fields = cache.get("requestedFields") if isinstance(cache.get("requestedFields"), list) else None
    diagnostics = (
        deepcopy(cache.get("diagnostics"))
        if isinstance(cache.get("diagnostics"), dict)
        else repricer_bff_module.build_finance_diagnostics_from_aggregates(
            aggregates,
            requested_fields=requested_fields,
        )
    )
    diagnostics.setdefault("state", "aggregate_only" if aggregates else "missing_cache")
    if not isinstance(diagnostics.get("storageAcceptance"), dict):
        fallback_diagnostics = repricer_bff_module.build_finance_diagnostics_from_aggregates(
            aggregates,
            requested_fields=requested_fields,
        )
        diagnostics["storageAcceptance"] = fallback_diagnostics.get("storageAcceptance", {})
        diagnostics["paidStorageNonzeroRows"] = fallback_diagnostics.get("paidStorageNonzeroRows")
        diagnostics["paidStorageSum"] = fallback_diagnostics.get("paidStorageSum")
        diagnostics["paidAcceptanceNonzeroRows"] = fallback_diagnostics.get("paidAcceptanceNonzeroRows")
        diagnostics["paidAcceptanceSum"] = fallback_diagnostics.get("paidAcceptanceSum")
    diagnostics.setdefault("requestedFields", requested_fields or [])
    diagnostics = repricer_bff_module.project_finance_tax_diagnostics(diagnostics, finance_taxes)
    adjustment_rows = diagnostics.get("adjustmentRows") if isinstance(diagnostics.get("adjustmentRows"), list) else []
    diagnostics["adjustmentRowsTotal"] = int(diagnostics.get("adjustmentRowsTotal") or len(adjustment_rows))
    diagnostics["adjustmentRows"] = adjustment_rows[:limit]
    diagnostics["adjustmentRowsReturned"] = len(diagnostics["adjustmentRows"])
    diagnostics["adjustmentRowsTruncatedByResponse"] = diagnostics["adjustmentRowsTotal"] > len(diagnostics["adjustmentRows"])
    sku_summaries = diagnostics.get("skuSummaries") if isinstance(diagnostics.get("skuSummaries"), list) else []
    diagnostics["skuSummariesTotal"] = len(sku_summaries)
    diagnostics["skuSummaries"] = sku_summaries[:limit]
    diagnostics["skuSummariesReturned"] = len(diagnostics["skuSummaries"])
    diagnostics["skuSummariesTruncatedByResponse"] = diagnostics["skuSummariesTotal"] > len(diagnostics["skuSummaries"])
    return diagnostics


def _diagnostic_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _signed_component_effect(operation: str, amount_kopecks: int) -> int:
    if operation == "+":
        return amount_kopecks
    if operation == "-":
        return -amount_kopecks
    if operation == "- signed":
        return -amount_kopecks
    if operation == "+ credit":
        return amount_kopecks
    return 0


def _margin_component(
    *,
    key: str,
    label: str,
    operation: str,
    amount_kopecks: Any,
    source: str,
    note: str | None = None,
) -> dict[str, Any]:
    amount = _diagnostic_int(amount_kopecks)
    return {
        "key": key,
        "label": label,
        "operation": operation,
        "amountKopecks": amount,
        "effectKopecks": _signed_component_effect(operation, amount),
        "source": source,
        "note": note,
    }


def _finance_raw_cost_total(diagnostics: dict[str, Any] | None, cost_key: str) -> int | None:
    if not isinstance(diagnostics, dict):
        return None
    metric_key = f"{cost_key}Kopecks"
    raw_totals = diagnostics.get("rawExpenseTotals")
    if isinstance(raw_totals, dict) and raw_totals.get(metric_key) is not None:
        return _diagnostic_int(raw_totals.get(metric_key))
    storage_acceptance = diagnostics.get("storageAcceptance")
    if cost_key == "storage" and isinstance(storage_acceptance, dict):
        raw_value = storage_acceptance.get("paidStorageSumKopecks")
        aggregate_value = storage_acceptance.get("paidStorageAggregateSumKopecks")
    elif cost_key == "acceptance" and isinstance(storage_acceptance, dict):
        raw_value = storage_acceptance.get("paidAcceptanceSumKopecks")
        aggregate_value = storage_acceptance.get("paidAcceptanceAggregateSumKopecks")
    else:
        raw_value = aggregate_value = None
    if raw_value is not None or aggregate_value is not None:
        return _diagnostic_int(raw_value if raw_value is not None else aggregate_value)
    totals = diagnostics.get("totals")
    if isinstance(totals, dict) and totals.get(metric_key) is not None:
        return _diagnostic_int(totals.get(metric_key))
    return None


def _apply_unassigned_finance_cost(
    totals: dict[str, int],
    unassigned_components: list[dict[str, Any]],
    *,
    key: str,
    label: str,
    source: str,
    raw_total_kopecks: int | None,
) -> None:
    if raw_total_kopecks is None:
        return
    assigned_kopecks = _diagnostic_int(totals.get(key))
    unassigned_kopecks = raw_total_kopecks - assigned_kopecks
    if unassigned_kopecks == 0:
        return
    component = _margin_component(
        key=f"unassigned{key[:1].upper()}{key[1:]}",
        label=label,
        operation="-",
        amount_kopecks=unassigned_kopecks,
        source=source,
        note="Есть в raw Finance detailed, но не попало в SKU rows. Обычно причина: WB прислал строку без nmId или SKU нет в cached goods.",
    )
    unassigned_components.append(component)
    totals[key] = assigned_kopecks + unassigned_kopecks
    totals[f"{key}Effect"] = _diagnostic_int(totals.get(f"{key}Effect")) + int(component["effectKopecks"])
    totals["expensesKopecks"] = _diagnostic_int(totals.get("expensesKopecks")) + unassigned_kopecks
    totals["actualNetProfitKopecks"] = _diagnostic_int(totals.get("actualNetProfitKopecks")) + int(component["effectKopecks"])
    totals["expectedNetProfitKopecks"] = _diagnostic_int(totals.get("expectedNetProfitKopecks")) + int(component["effectKopecks"])
    totals[component["key"]] = unassigned_kopecks
    totals[f"{component['key']}Effect"] = int(component["effectKopecks"])
    totals["unassignedExpensesKopecks"] = _diagnostic_int(totals.get("unassignedExpensesKopecks")) + unassigned_kopecks


def _margin_breakdown_from_rows(
    rows: list[dict[str, Any]],
    limit: int,
    *,
    finance_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    totals: dict[str, Any] = {}
    missing_tax_reasons: set[str] = set()
    unassigned_components: list[dict[str, Any]] = []
    for row in rows[:limit]:
        meta = row.get("meta") or {}
        analytics = row.get("analytics") or {}
        settings = row.get("settings") or {}
        components = [
            _margin_component(
                key="revenue",
                label="Выручка после СПП",
                operation="+",
                amount_kopecks=(
                    analytics.get("buyerRevenueKopecks")
                    or analytics.get("revenueKopecks")
                    or analytics.get("sellerRevenueKopecks")
                ),
                source="analytics.buyerRevenueKopecks / finance retailAmount",
            ),
            _margin_component(
                key="workReturn",
                label="Возврат работы",
                operation="+",
                amount_kopecks=analytics.get("workReturnKopecks"),
                source="settings.workReturnPerSaleKopecks * (salesUnits - returnsUnits)",
            ),
            _margin_component(
                key="cogs",
                label="Себестоимость продаж",
                operation="-",
                amount_kopecks=analytics.get("cogsTotalKopecks"),
                source="settings.cogsKopecks * (analytics.salesUnits - analytics.returnsUnits)",
            ),
            _margin_component(
                key="commission",
                label="Комиссия WB",
                operation="-",
                amount_kopecks=analytics.get("commissionKopecks"),
                source=f"analytics.commissionKopecks / {analytics.get('commissionCalcMode') or 'unknown'}",
            ),
            _margin_component(
                key="logistics",
                label="Логистика WB",
                operation="-",
                amount_kopecks=analytics.get("logisticsKopecks"),
                source="finance rebillLogisticCost/deliveryAmount/deliveryService",
            ),
            _margin_component(
                key="storage",
                label="Хранение WB",
                operation="-",
                amount_kopecks=analytics.get("storageKopecks"),
                source="finance paidStorage",
            ),
            _margin_component(
                key="acceptance",
                label="Приемка WB",
                operation="-",
                amount_kopecks=analytics.get("acceptanceKopecks"),
                source="finance paidAcceptance",
            ),
            _margin_component(
                key="penalty",
                label="Штрафы WB net",
                operation="- signed",
                amount_kopecks=analytics.get("penaltyKopecks"),
                source="finance penalty, знак WB сохранен",
                note="Если amount отрицательный, это возврат штрафа и effect становится плюсом к марже.",
            ),
            _margin_component(
                key="deduction",
                label="Удержания WB net",
                operation="- signed",
                amount_kopecks=analytics.get("deductionKopecks"),
                source="finance deduction, знак WB сохранен",
                note="Если amount отрицательный, это компенсация удержания и effect становится плюсом к марже.",
            ),
            _margin_component(
                key="loyaltyCost",
                label="Программа лояльности WB",
                operation="- signed",
                amount_kopecks=analytics.get("loyaltyCostKopecks"),
                source="finance cashbackAmount + cashbackCommissionChange",
            ),
            _margin_component(
                key="acquiring",
                label="Эквайринг",
                operation="-",
                amount_kopecks=analytics.get("acquiringKopecks"),
                source="finance acquiringFee",
            ),
            _margin_component(
                key="ads",
                label="Реклама WB",
                operation="-",
                amount_kopecks=analytics.get("adSpendKopecks"),
                source="finance WB Promotion deduction; ads fullstats fallback",
            ),
            _margin_component(
                key="otherExpenses",
                label="Прочие расходы",
                operation="-",
                amount_kopecks=analytics.get("otherExpensesKopecks"),
                source=(
                    "settings.otherExpensePerSaleKopecks="
                    f"{settings.get('otherExpensePerSaleKopecks')}; "
                    f"settings.otherExpensePricePct={settings.get('otherExpensePricePct')}"
                ),
            ),
            _margin_component(
                key="additionalPayment",
                label="Компенсации и корректировки WB net",
                operation="+ credit",
                amount_kopecks=analytics.get("additionalPaymentKopecks"),
                source="finance -paymentSchedule - raw additionalPayment",
            ),
            _margin_component(
                key="tax",
                label="Налог",
                operation="-",
                amount_kopecks=analytics.get("taxKopecks"),
                source="analytics.taxKopecks",
                note="Уменьшает netProfit, но показывается отдельно от расходов WB.",
            ),
        ]
        missing_tax = analytics.get("factTaxState") == "missing"
        if missing_tax:
            missing_tax_reasons.add(str(analytics.get("factTaxReason") or "tax_context_missing"))
        expected_net_profit_kopecks = None if missing_tax else sum(int(component["effectKopecks"]) for component in components)
        actual_net_profit_kopecks = analytics.get("netProfitKopecks")
        if expected_net_profit_kopecks is not None:
            totals["expectedNetProfitKopecks"] = totals.get("expectedNetProfitKopecks", 0) + expected_net_profit_kopecks
        if actual_net_profit_kopecks is not None:
            totals["actualNetProfitKopecks"] = totals.get("actualNetProfitKopecks", 0) + int(actual_net_profit_kopecks)
        if analytics.get("expensesKopecks") is not None:
            totals["expensesKopecks"] = totals.get("expensesKopecks", 0) + int(analytics.get("expensesKopecks") or 0)
        for component in components:
            if missing_tax and component["key"] == "tax":
                component["amountKopecks"] = component["effectKopecks"] = None
                continue
            totals[component["key"]] = totals.get(component["key"], 0) + int(component["amountKopecks"])
            totals[f"{component['key']}Effect"] = totals.get(f"{component['key']}Effect", 0) + int(component["effectKopecks"])
        items.append(
            {
                "articleId": meta.get("articleId"),
                "nmId": meta.get("nmId"),
                "name": meta.get("name"),
                "financeState": analytics.get("financeState"),
                "salesUnits": analytics.get("salesUnits"),
                "returnsUnits": analytics.get("returnsUnits"),
                "expensesKopecks": analytics.get("expensesKopecks"),
                "otherExpensePricePct": settings.get("otherExpensePricePct"),
                "otherExpensePerSaleKopecks": settings.get("otherExpensePerSaleKopecks"),
                "taxIncludedInExpenses": False,
                "expectedNetProfitKopecks": expected_net_profit_kopecks,
                "actualNetProfitKopecks": actual_net_profit_kopecks,
                "deltaKopecks": (
                    int(actual_net_profit_kopecks) - expected_net_profit_kopecks
                    if actual_net_profit_kopecks is not None and expected_net_profit_kopecks is not None
                    else None
                ),
                "components": components,
            }
        )
    _apply_unassigned_finance_cost(
        totals,
        unassigned_components,
        key="storage",
        label="Хранение WB без SKU",
        source="diagnostics.storageAcceptance.paidStorageSumKopecks - sum(SKU storageKopecks)",
        raw_total_kopecks=_finance_raw_cost_total(finance_diagnostics, "storage"),
    )
    _apply_unassigned_finance_cost(
        totals,
        unassigned_components,
        key="acceptance",
        label="Приемка WB без SKU",
        source="diagnostics.storageAcceptance.paidAcceptanceSumKopecks - sum(SKU acceptanceKopecks)",
        raw_total_kopecks=_finance_raw_cost_total(finance_diagnostics, "acceptance"),
    )
    if missing_tax_reasons:
        for key in ("tax", "taxEffect", "expectedNetProfitKopecks", "actualNetProfitKopecks"):
            totals[key] = None
        totals["factTaxState"] = "missing"
        totals["factTaxReason"] = sorted(missing_tax_reasons)[0]
    return {
        "formula": (
            "netProfit = revenue + workReturn - cogs - commission - logistics - storage - acceptance "
            "- signed(penalty) - signed(deduction) - loyaltyCost - acquiring - ads - otherExpenses "
            "+ additionalPayment - tax"
        ),
        "taxIncludedInExpenses": False,
        "items": items,
        "unassignedComponents": unassigned_components,
        "unassignedComponentsReturned": len(unassigned_components),
        "itemsReturned": len(items),
        "itemsTotalFromBuiltRows": len(rows),
        "itemsTruncated": len(rows) > len(items),
        "totals": totals,
    }


def _finance_diagnostics_response(
    *,
    organization_id: int,
    cache: dict[str, Any],
    range_start: datetime,
    range_end: datetime,
    resolved_period_days: int,
    period_suffix: str,
    limit: int,
    refreshed: bool,
) -> dict[str, Any]:
    diagnostics = _limited_finance_diagnostics(
        cache, limit, finance_taxes=_repricer_finance_taxes(organization_id, cache, range_start, range_end),
    )
    return {
        "source": "finance",
        "state": "ok" if cache else "missing_cache",
        "refreshed": refreshed,
        "organizationId": organization_id,
        "periodDays": resolved_period_days,
        "periodCacheSuffix": period_suffix,
        "dateFrom": range_start.date().isoformat(),
        "dateTo": range_end.date().isoformat(),
        "fetchedAt": cache.get("fetchedAt"),
        "count": int(cache.get("count") or 0),
        "rowsCount": int(cache.get("rowsCount") or 0),
        "pagesLoaded": int(cache.get("pagesLoaded") or 0),
        "cachedGoodsNmIds": int(cache.get("cachedGoodsNmIds") or 0),
        "matchedCachedGoodsNmIds": int(cache.get("matchedCachedGoodsNmIds") or 0),
        "rawRowsStrippedFromCache": "rows" not in cache,
        "diagnostics": diagnostics,
    }


def _repricer_list_cache_meta(
    organization_id: int,
    period_days: int,
    *,
    include_content: bool,
    date_from: date | None = None,
    date_to: date | None = None,
    require_full_sync_coverage: bool = True,
    period_cache_memo: dict[tuple[Any, ...], dict[str, Any]] | None = None,
    period_caches: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cache = cached_goods_meta(organization_id)
    range_start, range_end, resolved_period_days, period_suffix = _repricer_period_context(period_days, date_from, date_to)
    period_key = f"period_stats_{period_suffix}"
    finance_key = f"finance_{period_suffix}"
    ads_key = f"ads_{period_suffix}"
    baskets_key = f"baskets_{period_suffix}"
    cache["periodDays"] = resolved_period_days
    cache["dateFrom"] = range_start.date().isoformat()
    cache["dateTo"] = range_end.date().isoformat()
    cache["periodCacheSuffix"] = period_suffix
    cache["contentFetchedAt"] = (
        (get_source_cache(organization_id, "content_cards", slim=True) or {}).get("fetchedAt")
        if include_content
        else get_source_cache_fetched_at(organization_id, "content_cards")
    )
    cache["promotionsFetchedAt"] = get_source_cache_meta_fields(organization_id, "promotions").get("fetchedAt")
    promotions_source = get_source_cache(organization_id, "promotions", slim=True) or {}
    cache.update(promotion_refresh_metadata({**promotions_source, "fetchedAt": cache["promotionsFetchedAt"]}))
    cache["stocksFetchedAt"] = get_source_cache_meta_fields(organization_id, "stocks").get("fetchedAt")
    period_cache = period_caches.get("period_stats", {}) if period_caches is not None else _period_source_cache(
        organization_id,
        "period_stats",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    cache["periodStatsFetchedAt"] = period_cache.get("fetchedAt") or (
        get_source_cache_meta_fields(organization_id, period_key).get("fetchedAt") if period_caches is None else None
    )
    finance_meta = get_source_cache_meta_fields(organization_id, finance_key) if period_caches is None else {}
    finance_cache = period_caches.get("finance", {}) if period_caches is not None else _period_source_cache(
        organization_id,
        "finance",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    if not finance_cache:
        finance_meta = {}
    cache["financeFetchedAt"] = finance_cache.get("fetchedAt") if _page_finance_active() else finance_cache.get("fetchedAt") or finance_meta.get("fetchedAt")
    cache["financeCachedGoodsNmIds"] = finance_cache.get("cachedGoodsNmIds") or finance_meta.get("cachedGoodsNmIds")
    cache["financeMatchedNmIds"] = finance_cache.get("matchedCachedGoodsNmIds") or finance_meta.get("matchedCachedGoodsNmIds")
    storage_allocation = finance_cache.get("storageAllocation") or {}
    cache["storageStocksRequired"] = storage_allocation.get("reason") == "stock_snapshot_incomplete"
    ads_meta = get_source_cache_meta_fields(organization_id, ads_key) if period_caches is None else {}
    ads_cache = period_caches.get("ads", {}) if period_caches is not None else _period_source_cache(
        organization_id,
        "ads",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    ads_totals = _ads_cache_totals(ads_cache)
    ads_step = _latest_ads_sync_step(organization_id)
    cache["adsFetchedAt"] = ads_cache.get("fetchedAt") or ads_meta.get("fetchedAt")
    cache["adsCount"] = ads_cache.get("count")
    cache["adsCampaignCount"] = ads_cache.get("campaignCount")
    cache["adsDateFrom"] = ads_cache.get("dateFrom")
    cache["adsDateTo"] = ads_cache.get("dateTo")
    cache["adsSource"] = ads_cache.get("source")
    from app.wb_repricer_reporting import campaign_advertising
    cache["adsCoverageState"] = campaign_advertising(ads_cache, range_start, range_end)["state"]
    cache["adsSpendKopecks"] = ads_totals["adSpendKopecks"]
    cache["adsImpressions"] = ads_totals["adImpressions"]
    cache["adsClicks"] = ads_totals["adClicks"]
    cache["adsCartAdds"] = ads_totals["adCartAdds"]
    cache["adsOrders"] = ads_totals["adOrders"]
    cache["adsRevenueKopecks"] = ads_totals["adRevenueKopecks"]
    cache["adsLastStatus"] = ads_step.get("status") if ads_step else None
    cache["adsLastError"] = ads_step.get("error") if ads_step else None
    cache["adsLastFinishedAt"] = ads_step.get("finishedAt") if ads_step else None
    baskets_meta = get_source_cache_meta_fields(organization_id, baskets_key) if period_caches is None else {}
    baskets_cache = period_caches.get("baskets", {}) if period_caches is not None else _period_source_cache(
        organization_id,
        "baskets",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    cache["basketsFetchedAt"] = baskets_cache.get("fetchedAt") or baskets_meta.get("fetchedAt")
    cache["basketsCoverageState"] = baskets_cache.get("coverageState") or ("ok" if baskets_cache else "no_data")
    cache["basketsPeriodConversionState"] = baskets_cache.get("periodConversionState") or ("ok" if baskets_cache and not baskets_cache.get("coveredByCache") else "no_data")
    cache["basketsCoveredDays"] = baskets_cache.get("coveredDays")
    from app.wb_repricer_reporting import period_coverage
    cache["basketsMissingDates"] = period_coverage(baskets_cache, range_start, range_end)["missingDates"]
    from app.wb_api.source_freshness import source_is_fresh
    for name, payload in (("periodStats", period_cache), ("finance", finance_cache), ("baskets", baskets_cache), ("ads", ads_cache)):
        cache[f"{name}OldestFetchedAt"] = payload.get("oldestFetchedAt") or payload.get("fetchedAt")
        cache[f"{name}FreshnessState"] = "fresh" if source_is_fresh({**payload, "fetchedAt": payload.get("oldestFetchedAt") or payload.get("fetchedAt")}) else "stale"
    cache["periodCoverage"] = {
        name: period_coverage(payload, range_start, range_end)
        for name, payload in (("finance", finance_cache), ("orders", period_cache), ("ads", ads_cache), ("baskets", baskets_cache))
    }
    if _page_finance_active():
        cache["requiredPeriodSources"] = ["finance", "orders", "baskets", "ads"]
    cache["basketsRequestedNmIds"] = baskets_cache.get("requestedNmIds") or baskets_meta.get("requestedNmIds")
    cache["basketsMatchedNmIds"] = baskets_cache.get("matchedNmIds") or baskets_meta.get("matchedNmIds")
    cache["listItemsLimit"] = SKU_LIST_MAX_ITEMS
    return cache


def _list_repricer_skus_for_request(
    request: Request,
    scenario: str,
    *,
    actor: Any | None = None,
    wb_token: str | None | object = _WB_TOKEN_NOT_PROVIDED,
    include_promotions: bool = False,
    include_content: bool = False,
    period_days: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    max_items: int | None = SKU_LIST_MAX_ITEMS,
    sort_by_demand: bool = True,
    require_full_sync_coverage: bool = True,
    period_cache_memo: dict[tuple[Any, ...], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    actor = actor or actor_from_request(request)
    organization_id = actor.organization_id
    resolved_period_days = period_days if period_days is not None else int(request.query_params.get("periodDays") or 30)
    query_date_from = date_from
    query_date_to = date_to
    if query_date_from is None and request.query_params.get("dateFrom"):
        query_date_from = date.fromisoformat(str(request.query_params["dateFrom"]))
    if query_date_to is None and request.query_params.get("dateTo"):
        query_date_to = date.fromisoformat(str(request.query_params["dateTo"]))
    range_start, range_end, _resolved_days, period_suffix = _repricer_period_context(resolved_period_days, query_date_from, query_date_to)
    resolved_wb_token = _request_wb_token(request) if wb_token is _WB_TOKEN_NOT_PROVIDED else wb_token
    return _list_repricer_skus_from_cached_sources(
        organization_id,
        scenario,
        wb_token=resolved_wb_token if isinstance(resolved_wb_token, str) else None,
        include_promotions=include_promotions,
        include_content=include_content,
        period_days=_resolved_days,
        period_suffix=period_suffix,
        range_start=range_start,
        range_end=range_end,
        max_items=max_items,
        sort_by_demand=sort_by_demand,
        require_full_sync_coverage=require_full_sync_coverage,
        period_cache_memo=period_cache_memo,
    )


def _repricer_finance_taxes(
    organization_id: int, finance_cache: dict[str, Any], range_start: date, range_end: date,
    *, memo: dict[tuple[Any, ...], dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    period = Period(range_start.date() if isinstance(range_start, datetime) else range_start,
                    range_end.date() if isinstance(range_end, datetime) else range_end)
    key = (organization_id, "finance_taxes", period.cache_key)
    if memo is not None and key in memo:
        return memo[key]
    taxes = get_legacy_finance_taxes(organization_id, finance_cache, period, include_costs=True, use_current_settings=True)
    # Do not rank SKU profit while some account expenses remain outside SKUs.
    # Missing reconciliation is not evidence of a zero residual.
    diagnostics = _limited_finance_diagnostics(finance_cache, limit=1)
    aggregates = finance_cache.get("aggregates") or {}
    components = ("logistics", "storage", "acceptance", "penalty", "deduction",
                  "loyaltyCost", "adSpend", "additionalPayment")
    reconciled = all(
        (raw := _finance_raw_cost_total(diagnostics, component)) is not None
        and raw == sum(_int_or_zero(row.get(f"{component}Kopecks")) for row in aggregates.values())
        for component in components
    )
    campaign_reconciled = all(
        (raw := _finance_raw_cost_total(diagnostics, component)) is not None
        and raw == sum(_int_or_zero(row.get(f"{component}Kopecks")) for row in aggregates.values())
        for component in components if component not in {"deduction", "adSpend"}
    ) and not any((diagnostics or {}).get("unallocatedRealization", {}).values())
    for context in taxes.values():
        context["settlementAllocationConfirmed"] = reconciled
        context["campaignAllocationConfirmed"] = campaign_reconciled
    if memo is not None:
        memo[key] = taxes
    return taxes


def _list_repricer_skus_from_cached_sources(
    organization_id: int,
    scenario: str,
    *,
    wb_token: str | None,
    include_promotions: bool = False,
    include_content: bool = False,
    period_days: int,
    period_suffix: str,
    range_start: Any,
    range_end: Any,
    max_items: int | None = SKU_LIST_MAX_ITEMS,
    sort_by_demand: bool = True,
    require_full_sync_coverage: bool = True,
    period_cache_memo: dict[tuple[Any, ...], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    cached_goods = list_cached_goods(organization_id)
    # Category IDs from content are needed for tariffs even without rich content.
    content_cache = get_source_cache(organization_id, "content_cards", slim=True) or {}
    promotions_cache = get_source_cache(organization_id, "promotions", slim=True) or {}
    thresholds_cache = get_source_cache(organization_id, "promotion_thresholds", slim=True) or {}
    stocks_cache = get_source_cache(organization_id, "stocks", slim=True) or {}
    source_cache_kwargs = {
        "require_full_sync_coverage": require_full_sync_coverage,
        "prefer_freshest_covering": True,
        "memo": period_cache_memo,
    }
    period_cache = _period_source_cache(
        organization_id, "period_stats", period_suffix, period_days, range_start, range_end, **source_cache_kwargs
    )
    finance_cache = _period_source_cache(
        organization_id, "finance", period_suffix, period_days, range_start, range_end, **source_cache_kwargs
    )
    ads_cache = _period_source_cache(
        organization_id, "ads", period_suffix, period_days, range_start, range_end, **source_cache_kwargs
    )
    baskets_cache = _period_source_cache(
        organization_id, "baskets", period_suffix, period_days, range_start, range_end, **source_cache_kwargs
    )
    cached_content_cards = content_cache.get("cards") if isinstance(content_cache.get("cards"), list) else []
    cached_promotions = promotions_cache.get("promotions") if isinstance(promotions_cache.get("promotions"), list) else []
    cached_promotions = _apply_promotion_threshold_cache(cached_promotions, thresholds_cache)
    cached_stock_aggregates = stocks_cache.get("aggregates") if isinstance(stocks_cache.get("aggregates"), dict) else {}
    cached_period_stats = period_cache.get("aggregates") if isinstance(period_cache.get("aggregates"), dict) else {}
    cached_finance_aggregates = finance_cache.get("aggregates") if isinstance(finance_cache.get("aggregates"), dict) else {}
    cached_ads_aggregates = ads_cache.get("aggregates") if isinstance(ads_cache.get("aggregates"), dict) else {}
    cached_baskets_aggregates = baskets_cache.get("aggregates") if isinstance(baskets_cache.get("aggregates"), dict) else {}
    stocks_cache_loaded = bool(stocks_cache.get("fetchedAt"))
    baskets_cache_loaded = bool(baskets_cache.get("fetchedAt"))
    rows = list_repricer_skus(
        scenario,
        wb_token=wb_token,
        include_promotions=include_promotions,
        include_content=include_content,
        tolerate_content_errors=True,
        list_view=True,
        max_items=max_items,
        cached_goods=cached_goods,
        cached_content_cards=cached_content_cards,
        cached_promotions=cached_promotions,
        cached_stock_aggregates=cached_stock_aggregates,
        stocks_cache_loaded=stocks_cache_loaded,
        cached_period_stats=cached_period_stats,
        cached_finance_aggregates=cached_finance_aggregates,
        cached_finance_taxes=_repricer_finance_taxes(organization_id, finance_cache, range_start, range_end,
                                                   memo=period_cache_memo),
        cached_ads_aggregates=cached_ads_aggregates,
        cached_baskets_aggregates=cached_baskets_aggregates,
        baskets_cache_loaded=baskets_cache_loaded,
        cached_baskets_observed_at=baskets_cache.get("fetchedAt"),
        period_days=period_days,
        sort_by_demand=sort_by_demand,
        allow_commission_tariff_fetch=False,
        organization_id=organization_id,
    )
    for row in rows:
        analytics = row.setdefault("analytics", {})
        for field, payload in (("financeState", finance_cache), ("periodStatsState", period_cache),
                               ("adsState", ads_cache), ("basketsState", baskets_cache)):
            if field in {"financeState", "periodStatsState"}:
                analytics.setdefault("periodCoverageStates", {})[field] = payload.get("coverageState") or ("ok" if payload else "no_data")
            elif payload.get("coverageState") == "partial":
                analytics[field] = "partial"
            elif field == "adsState":
                analytics[field] = "ok" if analytics.get("adDataAvailable") and payload else "no_data"
    if _page_finance_active():
        from app.wb_repricer_reporting import apply_financial_row_state, apply_campaign_advertising, campaign_advertising
        apply_financial_row_state(rows, finance_cache)
        campaign_ads = campaign_advertising(ads_cache, range_start, range_end)
        apply_campaign_advertising(rows, finance_cache, campaign_ads)
        from app.wb_repricer_reporting import apply_repricer_economics
        apply_repricer_economics(rows, finance_cache, campaign_ads,
            _repricer_finance_taxes(organization_id, finance_cache, range_start, range_end, memo=period_cache_memo))
    return rows


SKU_LIST_SNAPSHOT_VERSION = 30
SKU_LIST_SNAPSHOT_CHUNK_SIZE = 150


def _snapshot_source_revision(organization_id, start=None, end=None):
    start, end = _parse_cache_date(start), _parse_cache_date(end)
    if not start or not end:
        return get_repricer_sources_revision(organization_id)
    # The financial comparison and operational trends depend on the preceding
    # equal-length period; updates outside both windows are not dependencies.
    return get_repricer_sources_revision(organization_id,
        date_from=start - timedelta(days=(end - start).days + 1), date_to=end)


def _repricer_sku_snapshot_key(
    scenario: str,
    *,
    period_suffix: str,
    include_promotions: bool,
    include_content: bool,
    version: int = SKU_LIST_SNAPSHOT_VERSION,
    view: Literal["sku", "stats"] = "sku",
) -> str:
    safe_scenario = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(scenario or "complete")).strip("_") or "complete"
    flags = f"promo{1 if include_promotions else 0}_content{1 if include_content else 0}"
    from app.repricer_page_finance import page_projection
    projection = "_page_finance2_campaign_ads" if page_projection.get() and view == "sku" else ""
    return f"{view}_list_snapshot_v{version}_{safe_scenario}_{period_suffix}_{flags}{'_metrics3' if view == 'stats' else ''}{projection}"


def _repricer_sku_snapshot_chunk_key(snapshot_key: str, chunk_index: int) -> str:
    return f"{snapshot_key}_chunk_{max(0, int(chunk_index)):05d}"


def _load_repricer_sku_snapshot(
    organization_id: int,
    scenario: str,
    *,
    period_suffix: str,
    include_promotions: bool,
    include_content: bool,
    view: Literal["sku", "stats"] = "sku",
    state_revision: str | None = None,
    tax_revision: str | None = None,
) -> dict[str, Any] | None:
    snapshot_key = _repricer_sku_snapshot_key(
        scenario,
        period_suffix=period_suffix,
        include_promotions=include_promotions,
        include_content=include_content,
        view=view,
    )
    cache = get_source_cache(organization_id, snapshot_key, slim=False)
    if not cache or int(cache.get("version") or 0) != SKU_LIST_SNAPSHOT_VERSION:
        return None
    now = datetime.now(timezone.utc)
    if cache.get("promotionStatusExpiresAt"):
        expiry = promotion_timestamp(cache["promotionStatusExpiresAt"])
        if expiry is None or expiry <= now:
            return None
    if cache.get("browserPricesExpireAt"):
        expiry = browser_price_timestamp(cache["browserPricesExpireAt"])
        if expiry is None or expiry <= now:
            return None
    if str(period_suffix).isdigit():
        start, end, _days, _suffix = _repricer_period_context(int(period_suffix))
        if not _cache_matches_range(cache.get("response", {}) if view == "stats" else cache, start, end):
            return None
    if cache.get("stateRevision") != (state_revision or _repricer_view_state_revision()):
        return None
    if cache.get("taxRevision") == "unavailable" or cache.get("taxRevision") != (
        tax_revision if tax_revision is not None else legacy_finance_tax_revision(organization_id)
    ):
        return None
    scope = cache.get("response", {}) if view == "stats" else cache
    if cache.get("sourceRevision") != _snapshot_source_revision(organization_id, scope.get("dateFrom"), scope.get("dateTo")):
        return None
    if _page_finance_active() and view == "sku":
        from app.repricer_page_finance import page_finance_revision
        first, last = _parse_cache_date(scope.get("dateFrom")), _parse_cache_date(scope.get("dateTo"))
        if first and last:
            first -= timedelta(days=(last - first).days + 1)
        if cache.get("pageFinanceRevision") != page_finance_revision(organization_id, first, last):
            return None
    if cache.get("goodsRevision") != cached_goods_meta(organization_id).get("latestFetchedAt"):
        return None
    if cache.get("goodsPriceRevision") != get_goods_price_revision(organization_id):
        return None
    from app.wb_api.source_freshness import source_is_fresh
    source_meta = scope.get("cache") or {}
    for source in ("finance", "periodStats", "baskets"):
        stored_state = source_meta.get(f"{source}FreshnessState")
        if stored_state and stored_state != ("fresh" if source_is_fresh({
                "dateTo": scope.get("dateTo"),
                "fetchedAt": source_meta.get(f"{source}OldestFetchedAt") or source_meta.get(f"{source}FetchedAt"),
        }) else "stale"):
            return None
    if cache.get("browserPricesRevision") != get_source_cache_fetched_at(organization_id, BROWSER_PRICES_KEY):
        # Coalesce upload batches from the original read, never extend a price's TTL.
        read_at = browser_price_timestamp(cache.get("browserPricesReadAt"))
        if read_at is None or not read_at <= now < read_at + timedelta(seconds=60):
            return None
    if cache.get("storage") == "chunked":
        cache.setdefault("snapshotKey", snapshot_key)
        return cache
    items = cache.get("items")
    if isinstance(items, list):
        cache.setdefault("snapshotKey", snapshot_key)
        return cache
    return None


def _load_repricer_sku_snapshot_page(
    organization_id: int,
    scenario: str,
    *,
    period_suffix: str,
    include_promotions: bool,
    include_content: bool,
    page: int,
    page_size: int,
    view: Literal["sku", "stats"] = "sku",
    state_revision: str | None = None,
    tax_revision: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    snapshot = _load_repricer_sku_snapshot(
        organization_id,
        scenario,
        period_suffix=period_suffix,
        include_promotions=include_promotions,
        include_content=include_content,
        view=view,
        state_revision=state_revision,
        tax_revision=tax_revision,
    )
    if snapshot is None:
        return None
    if snapshot.get("storage") != "chunked":
        items = snapshot.get("items") if isinstance(snapshot.get("items"), list) else []
        start = (page - 1) * page_size
        return snapshot, items[start : start + page_size]

    snapshot_key = str(snapshot.get("snapshotKey") or _repricer_sku_snapshot_key(
        scenario,
        period_suffix=period_suffix,
        include_promotions=include_promotions,
        include_content=include_content,
        view=view,
    ))
    chunk_size = max(1, int(snapshot.get("chunkSize") or SKU_LIST_SNAPSHOT_CHUNK_SIZE))
    start = (page - 1) * page_size
    end = min(start + page_size, int(snapshot.get("total") or 0))
    if end <= start:
        return snapshot, []
    first_chunk = start // chunk_size
    last_chunk = (max(start, end - 1)) // chunk_size
    rows: list[dict[str, Any]] = []
    for chunk_index in range(first_chunk, last_chunk + 1):
        chunk = get_source_cache(organization_id, _repricer_sku_snapshot_chunk_key(snapshot_key, chunk_index), slim=False) or {}
        if snapshot.get("generation") is not None and chunk.get("generation") != snapshot["generation"]:
            return None
        chunk_items = chunk.get("items") if isinstance(chunk.get("items"), list) else []
        if chunk_items:
            rows.extend(chunk_items)
    offset = start - first_chunk * chunk_size
    items = rows[offset : offset + page_size]
    return (snapshot, items) if len(items) == end - start else None


def _save_repricer_list_snapshot(
    organization_id: int, snapshot_key: str, rows: list[dict[str, Any]], payload: dict[str, Any],
) -> dict[str, Any]:
    promotion_expiries = [
        value for row in rows
        if (value := promotion_timestamp((row.get("analytics") or row).get("promotionStatusExpiresAt"))) is not None
    ]
    if promotion_expiries:
        payload = {**payload, "promotionStatusExpiresAt": min(promotion_expiries).isoformat()}
    if _page_finance_active():
        from app.repricer_page_finance import page_finance_revision
        first, last = _parse_cache_date(payload.get("dateFrom")), _parse_cache_date(payload.get("dateTo"))
        if first and last:
            first -= timedelta(days=(last - first).days + 1)
        payload = {**payload, "pageFinanceRevision": page_finance_revision(organization_id, first, last)}
    chunk_size = SKU_LIST_SNAPSHOT_CHUNK_SIZE
    if any((row.get("analytics") or row.get("metrics") or {}).get("factTaxReason") == "tax_policy_unavailable" for row in rows):
        payload = {**payload, "taxRevision": "unavailable"}
    generation = uuid4().hex
    for chunk_start in range(0, len(rows), chunk_size):
        chunk_index = chunk_start // chunk_size
        items = rows[chunk_start : chunk_start + chunk_size]
        save_source_cache(organization_id, _repricer_sku_snapshot_chunk_key(snapshot_key, chunk_index), {
            "version": SKU_LIST_SNAPSHOT_VERSION, "snapshotKey": snapshot_key,
            "generation": generation, "chunkIndex": chunk_index, "chunkSize": chunk_size,
            "items": items, "itemsReturned": len(items),
        })
    return save_source_cache(organization_id, snapshot_key, {
        **payload, "version": SKU_LIST_SNAPSHOT_VERSION, "storage": "chunked",
        "browserPricesExpireAt": payload.get("browserPricesExpireAt") or rows_expire_at(rows),
        "snapshotKey": snapshot_key, "generation": generation, "chunkSize": chunk_size,
        "chunksCount": (len(rows) + chunk_size - 1) // chunk_size, "total": len(rows),
        "builtAt": datetime.now(timezone.utc).isoformat(),
    })


def _repricer_view_state_revision() -> str:
    # Hydration has already loaded these small inputs; no source JSON is read.
    state = {name: getattr(repricer_bff_module, name) for name in (
        "FRONTEND_STRATEGY_ASSIGNMENTS", "SKU_META_OVERRIDES", "SKU_SETTINGS_OVERRIDES",
        "ALGORITHM_SETTINGS_STATE", "LIQUIDATION_ACTIVE", "NEGATIVE_MARGIN_CONFIRMATIONS",
        "REPRICER_SKU_GROUPS", "TEMPLATES_STATE",
    )}
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@tax_consistent_report
def _build_repricer_sku_snapshot(
    organization_id: int,
    scenario: str,
    *,
    wb_token: str | None,
    resolved_period_days: int,
    period_suffix: str,
    range_start: Any,
    range_end: Any,
    include_promotions: bool = False,
    include_content: bool = False,
) -> dict[str, Any]:
    period_cache_memo: dict[tuple[Any, ...], dict[str, Any]] = {}
    from app.wb_catalog_loading import require_goods_cache
    require_goods_cache(organization_id, cached_goods_meta(organization_id))
    state_revision = _repricer_view_state_revision()
    tax_revision = legacy_finance_tax_revision(organization_id)
    source_revision = _snapshot_source_revision(organization_id, range_start, range_end)
    goods_revision = cached_goods_meta(organization_id).get("latestFetchedAt")
    goods_price_revision = get_goods_price_revision(organization_id)
    browser_prices_read_at = datetime.now(timezone.utc).isoformat()
    browser_prices_revision = get_source_cache_fetched_at(organization_id, BROWSER_PRICES_KEY)
    rows = _list_repricer_skus_from_cached_sources(
        organization_id,
        scenario,
        wb_token=wb_token,
        include_promotions=include_promotions,
        include_content=include_content,
        period_days=resolved_period_days,
        period_suffix=period_suffix,
        range_start=range_start,
        range_end=range_end,
        max_items=None,
        sort_by_demand=True,
        require_full_sync_coverage=False,
        period_cache_memo=period_cache_memo,
    )
    finance_cache = _period_source_cache(
        organization_id,
        "finance",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        slim=True,
        require_full_sync_coverage=False,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    finance_diagnostics = _limited_finance_diagnostics(finance_cache, limit=1) if finance_cache else None
    cache_meta = _repricer_list_cache_meta(
        organization_id,
        resolved_period_days,
        include_content=include_content,
        date_from=range_start.date(),
        date_to=range_end.date(),
        require_full_sync_coverage=False,
        period_cache_memo=period_cache_memo,
    )
    summary = _repricer_list_summary_from_source_caches(
        organization_id,
        resolved_period_days=resolved_period_days,
        period_suffix=period_suffix,
        range_start=range_start,
        range_end=range_end,
        finance_diagnostics=finance_diagnostics,
        require_full_sync_coverage=False,
        prefer_freshest_finance=True,
        period_cache_memo=period_cache_memo,
    )
    snapshot_key = _repricer_sku_snapshot_key(
        scenario,
        period_suffix=period_suffix,
        include_promotions=include_promotions,
        include_content=include_content,
    )
    assert_current_snapshot(organization_id)
    return _save_repricer_list_snapshot(organization_id, snapshot_key, rows, {
        "browserPricesRevision": browser_prices_revision,
        "browserPricesReadAt": browser_prices_read_at,
        "sourceRevision": source_revision,
        "goodsRevision": goods_revision,
        "goodsPriceRevision": goods_price_revision,
        "stateRevision": state_revision,
        "taxRevision": tax_revision,
        "scenario": scenario,
        "periodDays": resolved_period_days,
        "periodSuffix": period_suffix,
        "dateFrom": range_start.date().isoformat(),
        "dateTo": range_end.date().isoformat(),
        "includePromotions": include_promotions,
        "includeContent": include_content,
        "summary": summary,
        "cache": cache_meta,
    })


def _repricer_row_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    analytics = row.get("analytics") or {}
    meta = row.get("meta") or {}
    orders = int(analytics.get("ordersUnits") or 0)
    baskets_raw = analytics.get("baskets")
    baskets = int(baskets_raw) if baskets_raw is not None else int(meta.get("basketsLast7d") or 0)
    revenue = int(analytics.get("revenueKopecks") or 0)
    return (-orders, -baskets, -revenue, str(meta.get("articleId") or ""))


def _repricer_load_trace_start(kind: str) -> dict[str, Any]:
    now = perf_counter()
    return {
        "kind": kind,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "_started": now,
        "_last": now,
        "steps": [],
    }


def _repricer_load_trace_step(trace: dict[str, Any], name: str, **meta: Any) -> None:
    now = perf_counter()
    step: dict[str, Any] = {
        "name": name,
        "durationMs": round((now - float(trace.get("_last") or now)) * 1000, 1),
        "elapsedMs": round((now - float(trace.get("_started") or now)) * 1000, 1),
    }
    public_meta = {key: value for key, value in meta.items() if value is not None}
    if public_meta:
        step["meta"] = public_meta
    trace.setdefault("steps", []).append(step)
    trace["_last"] = now


def _repricer_load_trace_payload(trace: dict[str, Any]) -> dict[str, Any]:
    total_ms = round((perf_counter() - float(trace.get("_started") or perf_counter())) * 1000, 1)
    payload = {
        "kind": trace.get("kind") or "repricer",
        "startedAt": trace.get("startedAt"),
        "totalMs": total_ms,
        "steps": trace.get("steps") or [],
    }
    if total_ms >= 2000:
        logger.warning("Slow WB repricer load trace: %s", json.dumps(payload, ensure_ascii=False))
    return payload


def _normalize_search_text(value: Any) -> str:
    return str(value or "").lower().replace("ё", "е").strip()


def _search_terms(value: str) -> list[str]:
    normalized = _normalize_search_text(value)
    if not normalized:
        return []
    return [item for item in re.split(r"[\s,;]+", normalized) if item]


def _row_identity_matches_search(row: dict[str, Any], q: str) -> bool:
    terms = set(_search_terms(q))
    if not terms:
        return False
    meta = row.get("meta") or {}
    identities = {
        _normalize_search_text(value)
        for value in (
            meta.get("articleId"),
            meta.get("nmId"),
        )
        if value is not None and _normalize_search_text(value)
    }
    return bool(terms & identities)


def _row_ui_status(row: dict[str, Any]) -> str:
    meta = row.get("meta") or {}
    analytics = row.get("analytics") or {}
    raw_status = str(meta.get("status") or "")
    if raw_status == "liquidation":
        return "liq"
    if raw_status == "manual":
        return "manual"
    if raw_status == "warmup":
        return "new"
    margin_pct = _float_or_none(analytics.get("marginPct")) or 0
    baskets_raw = analytics.get("baskets")
    baskets = _int_or_zero(baskets_raw if baskets_raw is not None else meta.get("basketsLast7d"))
    basket_norm = _int_or_zero(meta.get("basketNorm"))
    if margin_pct < 10 or (basket_norm > 0 and baskets < basket_norm):
        return "illiquid"
    return "loko"


def _row_matches_status_filter(row: dict[str, Any], status: str) -> bool:
    if not status or status == "all":
        return True
    if status == "nopmin":
        settings = row.get("settings") or {}
        return int(settings.get("pMinKopecks") or settings.get("pminKopecks") or 0) <= 0
    meta = row.get("meta") or {}
    return str(meta.get("status") or "") == status or _row_ui_status(row) == status


def _row_matches_query(row: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    meta = row.get("meta") or {}
    analytics = row.get("analytics") or {}
    needle = _normalize_search_text(q)
    haystack = " ".join(
        _normalize_search_text(value)
        for value in (
            meta.get("articleId"),
            meta.get("nmId"),
            meta.get("name"),
            meta.get("subject"),
            meta.get("brand"),
            meta.get("managerName"),
            analytics.get("abcCode"),
        )
    )
    if needle in haystack:
        return True
    terms = _search_terms(needle)
    return len(terms) > 1 and any(term in haystack for term in terms)


def _row_matches_sku_filters(
    row: dict[str, Any],
    *,
    q: str,
    status: str,
    brand: str,
    manager: str,
) -> bool:
    meta = row.get("meta") or {}
    if q and _row_identity_matches_search(row, q):
        return True
    if not _row_matches_status_filter(row, status):
        return False
    if brand and brand != "all":
        row_brand = _normalize_search_text(meta.get("brand") or "wb")
        if row_brand != _normalize_search_text(brand):
            return False
    if manager and manager != "all":
        manager_id = str(meta.get("managerId") or "unassigned")
        if manager_id != manager:
            return False
    return _row_matches_query(row, q)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _page_finance_active() -> bool:
    from app.repricer_page_finance import page_projection
    return page_projection.get()


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _buyout_summary(rows: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    facts = [row.get("analytics") or {} for row in rows]
    confirmed = [fact for fact in facts if fact.get("financeState") == "ok"]
    if not confirmed:
        return (None, None) if rows else (0, 0)
    if any(type(fact.get(key)) is not int for fact in confirmed for key in ("buyoutUnits", "buyoutAmountKopecks")):
        return None, None
    return sum(fact["buyoutUnits"] for fact in confirmed), sum(fact["buyoutAmountKopecks"] for fact in confirmed)


def _apply_period_reporting(summary, organization_id, start, end, *, ledger, coverage, nm_ids=None, memo=None):
    if not _page_finance_active():
        return summary
    from app.wb_repricer_reporting import apply_summary_availability, financial_totals, period_coverage, load_buyout_comparison
    apply_summary_availability(summary, coverage, ledger, nm_ids)
    left, right = _parse_cache_date(start), _parse_cache_date(end)
    days = (right - left).days + 1
    previous_left, previous_right = left - timedelta(days=days), left - timedelta(days=1)
    previous = load_buyout_comparison(organization_id, previous_left, previous_right, memo=memo)
    previous_coverage = period_coverage(previous, previous_left, previous_right)
    from app.wb_api.source_freshness import source_is_fresh
    previous_units, previous_amount = financial_totals(previous, nm_ids)
    summary["buyoutComparison"] = {
        **previous_coverage, "dateFrom": str(previous_left), "dateTo": str(previous_right),
        "units": previous_units, "amountKopecks": previous_amount,
        "currentState": coverage["finance"]["state"],
        "source": "finance.rrDate.retailAmount.netSalesUnits",
        "freshnessState": "fresh" if source_is_fresh({**previous, "fetchedAt": previous.get("oldestFetchedAt") or previous.get("fetchedAt")}) else "stale",
    }
    return summary


def _repricer_list_summary(
    rows: list[dict[str, Any]],
    *,
    finance_diagnostics: dict[str, Any] | None = None,
    ads_totals: dict[str, Any] | None = None,
    campaign_ads: dict[str, Any] | None = None,
) -> dict[str, Any]:
    revenue_kopecks = 0
    margin_kopecks = 0
    cogs_kopecks = 0
    expenses_kopecks = 0
    orders_units_total = 0
    cancelled_orders_units = 0
    funnel_orders_units = 0
    sales_units = 0
    returns_units = 0
    total_baskets = 0
    in_sale = 0
    promo_count = 0
    ad_spend_kopecks = 0
    ad_impressions = 0
    ad_clicks = 0
    ad_cart_adds = 0
    ad_orders = 0
    ad_revenue_kopecks = 0
    ad_sku_count = 0
    other_expenses_kopecks = 0
    tax_kopecks = 0
    work_return_kopecks = 0
    storage_kopecks = 0
    acceptance_kopecks = 0
    penalty_charged_kopecks = 0
    penalty_returned_kopecks = 0
    deduction_charged_kopecks = 0
    deduction_compensation_kopecks = 0
    additional_payment_kopecks = 0
    finance_components = {
        key: 0
        for key in ("commission", "logistics", "storage", "acceptance", "penalty", "deduction", "loyaltyCost", "acquiring", "additionalPayment")
    }
    buyer_revenue_kopecks = 0
    fact_tax_reasons: set[str] = set()
    has_fact_tax_contract = False
    has_finance_rows = False

    for row in rows:
        meta = row.get("meta") or {}
        analytics = row.get("analytics") or {}
        if analytics.get("factTaxState") is not None:
            has_fact_tax_contract = True
            if analytics.get("financeState") != "no_data":
                has_finance_rows = True
                if analytics.get("factTaxState") != "configured":
                    fact_tax_reasons.add(str(analytics.get("factTaxReason") or "tax_context_missing"))
        orders_units = _int_or_zero(analytics.get("ordersUnits"))
        cancelled_orders_units += _int_or_zero(analytics.get("cancelledOrdersUnits"))
        buyer_price_kopecks = _int_or_zero(
            analytics.get("accountedBuyerPriceKopecks")
            or analytics.get("buyerPriceNoWalletKopecks")
            or analytics.get("avgPriceWithSppKopecks")
            or meta.get("currentPriceKopecks")
        )
        row_revenue = _int_or_zero(analytics.get("revenueKopecks"))
        revenue_kopecks += row_revenue
        row_buyer_revenue = analytics.get("buyerRevenueKopecks")
        buyer_revenue_kopecks += _int_or_zero(row_revenue if row_buyer_revenue is None else row_buyer_revenue)
        cogs_kopecks += _int_or_zero(analytics.get("cogsTotalKopecks"))
        expenses_kopecks += _int_or_zero(analytics.get("expensesKopecks"))
        orders_units_total += orders_units
        funnel_orders_units += _int_or_zero(analytics.get("funnelOrderCount"))
        sales_units += _int_or_zero(analytics.get("salesUnits"))
        returns_units += _int_or_zero(analytics.get("returnsUnits"))
        row_ad_spend = _int_or_zero(analytics.get("adSpendKopecks"))
        row_ad_impressions = _int_or_zero(analytics.get("adImpressions"))
        row_ad_clicks = _int_or_zero(analytics.get("adClicks"))
        row_ad_cart_adds = _int_or_zero(analytics.get("adCartAdds"))
        row_ad_orders = _int_or_zero(analytics.get("adOrders"))
        row_ad_revenue = _int_or_zero(analytics.get("adRevenueKopecks"))
        other_expenses_kopecks += _int_or_zero(analytics.get("otherExpensesKopecks"))
        tax_kopecks += _int_or_zero(analytics.get("taxKopecks"))
        work_return_kopecks += _int_or_zero(analytics.get("workReturnKopecks"))
        storage_kopecks += _int_or_zero(analytics.get("storageKopecks"))
        acceptance_kopecks += _int_or_zero(analytics.get("acceptanceKopecks"))
        penalty_charged_kopecks += _int_or_zero(analytics.get("penaltyChargedKopecks"))
        penalty_returned_kopecks += _int_or_zero(analytics.get("penaltyReturnedKopecks"))
        deduction_charged_kopecks += _int_or_zero(analytics.get("deductionChargedKopecks"))
        deduction_compensation_kopecks += _int_or_zero(analytics.get("deductionCompensationKopecks"))
        additional_payment_kopecks += _int_or_zero(analytics.get("additionalPaymentKopecks"))
        for key in finance_components:
            finance_components[key] += _int_or_zero(analytics.get(f"{key}Kopecks"))
        ad_spend_kopecks += row_ad_spend
        ad_impressions += row_ad_impressions
        ad_clicks += row_ad_clicks
        ad_cart_adds += row_ad_cart_adds
        ad_orders += row_ad_orders
        ad_revenue_kopecks += row_ad_revenue
        if any((row_ad_spend, row_ad_impressions, row_ad_clicks, row_ad_cart_adds, row_ad_orders, row_ad_revenue)):
            ad_sku_count += 1

        if analytics.get("netProfitKopecks") is not None:
            row_margin = _int_or_zero(analytics.get("netProfitKopecks"))
        elif analytics.get("factTaxState") == "missing":
            row_margin = 0  # An unknown fact must not be replaced with a current-price plan.
        elif analytics.get("plannedPeriodMarginKopecks") is not None:
            row_margin = _int_or_zero(analytics.get("plannedPeriodMarginKopecks"))
        else:
            row_margin = _int_or_zero(analytics.get("marginKopecks")) * orders_units
        margin_kopecks += row_margin

        baskets_raw = analytics.get("baskets")
        total_baskets += _int_or_zero(baskets_raw if baskets_raw is not None else meta.get("basketsLast7d"))

        stock_units = _int_or_zero(analytics.get("wbStockUnits"))
        if stock_units > 0 and str(meta.get("status") or "") != "liquidation":
            in_sale += 1
        if analytics.get("promotionStatus") == "yes":
            promo_count += 1

    missing_other_expenses_kopecks = 0
    buyout_units, buyout_amount = _buyout_summary(rows)
    unallocated_trades = (finance_diagnostics or {}).get("unallocatedRealization", {})
    if any(unallocated_trades.values()):
        revenue_kopecks += _int_or_zero(unallocated_trades.get("revenueKopecks"))
        buyer_revenue_kopecks += _int_or_zero(unallocated_trades.get("revenueKopecks"))
        sales_units += _int_or_zero(unallocated_trades.get("salesUnits"))
        returns_units += _int_or_zero(unallocated_trades.get("returnsUnits"))
        if buyout_units is not None:
            buyout_units += _int_or_zero(unallocated_trades.get("netSalesUnits"))
        if buyout_amount is not None:
            buyout_amount += _int_or_zero(unallocated_trades.get("revenueKopecks"))
        fact_tax_reasons.add("unallocated_financial_realization")

    finance_ad_spend_kopecks = _finance_raw_cost_total(finance_diagnostics, "adSpend")
    if campaign_ads is not None:
        ads_totals = {**(ads_totals or {}), "adSpendKopecks": campaign_ads.get("adSpendKopecks")}
    elif finance_ad_spend_kopecks is not None:
        ads_totals = {**(ads_totals or {}), "adSpendKopecks": finance_ad_spend_kopecks}
    unassigned_ad_spend_kopecks = 0
    if ads_totals is not None:
        total_ad_spend_kopecks = _int_or_zero(ads_totals.get("adSpendKopecks"))
        unassigned_ad_spend_kopecks = total_ad_spend_kopecks - ad_spend_kopecks
        expenses_kopecks += unassigned_ad_spend_kopecks
        margin_kopecks -= unassigned_ad_spend_kopecks
        ad_spend_kopecks = total_ad_spend_kopecks
        ad_impressions = _int_or_zero(ads_totals.get("adImpressions"))
        ad_clicks = _int_or_zero(ads_totals.get("adClicks"))
        ad_cart_adds = _int_or_zero(ads_totals.get("adCartAdds"))
        ad_orders = _int_or_zero(ads_totals.get("adOrders"))
        ad_revenue_kopecks = _int_or_zero(ads_totals.get("adRevenueKopecks"))

    unassigned_finance_components: dict[str, int] = {}
    for key, assigned_kopecks in finance_components.items():
        authoritative_kopecks = _finance_raw_cost_total(finance_diagnostics, key)
        if authoritative_kopecks is None:
            continue
        delta_kopecks = authoritative_kopecks - assigned_kopecks
        unassigned_finance_components[key] = delta_kopecks
        finance_components[key] = authoritative_kopecks
        expense_delta = -delta_kopecks if key == "additionalPayment" else delta_kopecks
        expenses_kopecks += expense_delta
        margin_kopecks -= expense_delta

    storage_kopecks = finance_components["storage"]
    acceptance_kopecks = finance_components["acceptance"]
    additional_payment_kopecks = finance_components["additionalPayment"]
    unassigned_storage_kopecks = unassigned_finance_components.get("storage", 0)
    unassigned_acceptance_kopecks = unassigned_finance_components.get("acceptance", 0)
    unassigned_finance_expenses_kopecks = sum(
        (-amount if key == "additionalPayment" else amount)
        for key, amount in unassigned_finance_components.items()
    )

    # Keep the same ratio for return-dominated periods; zero has no ratio.
    # Per-SKU percentages are not a substitute for the ratio of totals.
    if has_fact_tax_contract and not has_finance_rows:
        fact_tax_reasons.add("finance_missing")
    avg_margin_pct = (margin_kopecks / revenue_kopecks * 100) if revenue_kopecks != 0 and not fact_tax_reasons else None
    promo_share_pct = round(promo_count / len(rows) * 100) if rows else 0
    settlement_rows = [row.get("analytics") or {} for row in rows if (row.get("analytics") or {}).get("financeState") != "no_data"]
    settlement_blockers = sorted({reason for row in settlement_rows for reason in row.get("settlementBlockers", [])})
    if not settlement_rows or any(row.get("settlementFormulaVersion") != repricer_bff_module.SETTLEMENT_PROFIT_VERSION
           or type(row.get("settlementProfitKopecks")) is not int for row in settlement_rows):
        settlement_blockers.append("settlement_rows_incomplete")
    if any(unassigned_finance_components.get(key, 0) for key in ("commission", "acquiring")):
        settlement_blockers.append("settlement_unassigned_payable_unconfirmed")
    settlement_residual = sum((-amount if key == "additionalPayment" else amount)
        for key, amount in unassigned_finance_components.items() if key not in {"commission", "acquiring"})
    if finance_ad_spend_kopecks is not None:
        # Compare Finance promotions only, never the parallel Ads representation.
        settlement_residual += finance_ad_spend_kopecks - sum(
            _int_or_zero(row.get("settlementAdvertisingKopecks")) for row in settlement_rows)
    settlement_profit = None if settlement_blockers else sum(row["settlementProfitKopecks"] for row in settlement_rows) - settlement_residual
    campaign_profit = (settlement_profit + finance_ad_spend_kopecks - campaign_ads["adSpendKopecks"]
        if campaign_ads is not None and campaign_ads.get("state") == "ok"
        and type(campaign_ads.get("adSpendKopecks")) is int and type(finance_ad_spend_kopecks) is int
        and type(settlement_profit) is int else None)
    if (campaign_ads is not None and campaign_ads.get("state") == "ok"
            and type(campaign_ads.get("adSpendKopecks")) is int):
        # Cabinet totals can include observed residual fees without inventing
        # per-SKU attribution. Unknown tax/cost/payout still blocks the result.
        attributed = [row.get("settlementAttributedProfitKopecks") for row in settlement_rows]
        only_allocation_blocked = all(set(row.get("settlementBlockers") or []) <= {
            "settlement_account_expenses_unreconciled"} for row in settlement_rows)
        raw_costs_known = all(_finance_raw_cost_total(finance_diagnostics, key) is not None
                              for key in (*finance_components, "adSpend"))
        if (settlement_rows and all(type(value) is int for value in attributed) and only_allocation_blocked
                and raw_costs_known and not any(unallocated_trades.values())
                and not any(unassigned_finance_components.get(key, 0) for key in ("commission", "acquiring"))):
            campaign_profit = sum(attributed) - settlement_residual + finance_ad_spend_kopecks - campaign_ads["adSpendKopecks"]
    if campaign_ads is not None and campaign_ads.get("state") == "ok" and finance_diagnostics is None:
        facts = [row.get("analytics") or {} for row in rows]
        profits = [fact.get("campaignAttributedProfitKopecks") for fact in facts]
        only_allocation_blocked = all(set(fact.get("settlementBlockers") or []) <= {
            "settlement_account_expenses_unreconciled"} for fact in facts)
        campaign_profit = sum(profits) if only_allocation_blocked and all(type(value) is int for value in profits) else None
    advertising_known = campaign_ads is None or (campaign_ads.get("state") == "ok" and type(campaign_ads.get("adSpendKopecks")) is int)
    policy_rows = [row.get("analytics") or {} for row in rows
                   if (row.get("analytics") or {}).get("repricerEconomicsVersion")]
    if policy_rows:
        participating = [row for row in policy_rows if row.get("financeState") != "no_data"]
        values = [row.get("campaignProfitKopecks") for row in participating]
        campaign_profit = (sum(values) - unassigned_ad_spend_kopecks
            if participating and advertising_known and all(type(value) is int for value in values) else None)
        # Assigned deductions were excluded by the product policy; exclude the
        # account remainder here as well, retaining the original ledger totals.
        expenses_kopecks -= unassigned_finance_components.get("deduction", 0)
    return {
        **({"campaignAdvertisingVersion": "wb-repricer-campaign-advertising-v1",
            "campaignProfitKopecks": campaign_profit, "adSpendState": campaign_ads["state"],
            "financeAdSpendKopecks": finance_ad_spend_kopecks} if campaign_ads is not None else {}),
        "settlementFormulaVersion": repricer_bff_module.SETTLEMENT_PROFIT_VERSION,
        "settlementProfitKopecks": settlement_profit,
        "finalPayoutKopecks": (
            sum(row["finalPayoutKopecks"] for row in settlement_rows) - settlement_residual
            if _page_finance_active() and settlement_rows and all(type(row.get("finalPayoutKopecks")) is int for row in settlement_rows)
            and not any(unassigned_finance_components.get(key, 0) for key in ("commission", "acquiring"))
            else None if settlement_blockers else sum(row["finalPayoutKopecks"] for row in settlement_rows) - settlement_residual
        ),
        "settlementCogsKopecks": sum(row["settlementCogsKopecks"] for row in settlement_rows) if settlement_rows and all(type(row.get("settlementCogsKopecks")) is int for row in settlement_rows) else None,
        "settlementBlockers": settlement_blockers,
        "revenueKopecks": revenue_kopecks,
        "sellerRevenueKopecks": revenue_kopecks,
        "buyerRevenueKopecks": buyer_revenue_kopecks,
        "marginKopecks": margin_kopecks if not fact_tax_reasons else None,
        "cogsKopecks": cogs_kopecks,
        "expensesKopecks": expenses_kopecks if advertising_known else None,
        "buyoutUnits": buyout_units,
        "buyoutAmountKopecks": buyout_amount,
        "buyoutSource": "finance.rrDate.retailAmount.netSalesUnits",
        "ordersUnits": orders_units_total,
        "cancelledOrdersUnits": cancelled_orders_units,
        "funnelOrderCount": funnel_orders_units,
        "salesUnits": sales_units - returns_units,
        "returnsUnits": returns_units,
        "adSpendKopecks": (campaign_ads.get("adSpendKopecks") if campaign_ads is not None
            else None if (finance_diagnostics or {}).get("financialAdvertisingComplete") is False else ad_spend_kopecks),
        "adImpressions": ad_impressions,
        "adClicks": ad_clicks,
        "adCartAdds": ad_cart_adds,
        "adOrders": ad_orders,
        "adRevenueKopecks": ad_revenue_kopecks,
        "adSkuCount": ad_sku_count,
        "unassignedAdSpendKopecks": unassigned_ad_spend_kopecks,
        "adSpendSource": ("wb_ads_fullstats" if campaign_ads is not None else None
            if (finance_diagnostics or {}).get("financialAdvertisingComplete") is False
            else "finance_deduction_wb_promotion" if finance_ad_spend_kopecks is not None else "wb_ads_fullstats"),
        "otherExpensesKopecks": other_expenses_kopecks,
        "taxKopecks": tax_kopecks if not fact_tax_reasons else None,
        "factTaxState": "missing" if fact_tax_reasons else "configured",
        "factTaxReason": sorted(fact_tax_reasons)[0] if fact_tax_reasons else None,
        "workReturnKopecks": work_return_kopecks,
        "storageKopecks": storage_kopecks,
        "acceptanceKopecks": acceptance_kopecks,
        "unassignedStorageKopecks": unassigned_storage_kopecks,
        "unassignedAcceptanceKopecks": unassigned_acceptance_kopecks,
        "unassignedExpensesKopecks": unassigned_finance_expenses_kopecks,
        "unassignedFinanceComponentsKopecks": unassigned_finance_components,
        "commissionKopecks": finance_components["commission"],
        "logisticsKopecks": finance_components["logistics"],
        "penaltyKopecks": finance_components["penalty"],
        "deductionKopecks": finance_components["deduction"],
        "loyaltyCostKopecks": finance_components["loyaltyCost"],
        "acquiringKopecks": finance_components["acquiring"],
        "penaltyChargedKopecks": penalty_charged_kopecks,
        "penaltyReturnedKopecks": penalty_returned_kopecks,
        "deductionChargedKopecks": deduction_charged_kopecks,
        "deductionCompensationKopecks": deduction_compensation_kopecks,
        "additionalPaymentKopecks": additional_payment_kopecks,
        "missingOtherExpensesKopecks": missing_other_expenses_kopecks,
        "avgMarginPct": round(avg_margin_pct, 1) if avg_margin_pct is not None else None,
        "totalBaskets": total_baskets,
        "totalBasketsState": "partial" if any((row.get("analytics") or {}).get("basketsState") in {"partial", "no_data"} for row in rows) else "ok",
        "inSale": in_sale,
        "promoSharePct": promo_share_pct,
        "skuCount": len(rows),
    }


def _repricer_list_summary_from_source_caches(
    organization_id: int,
    *,
    resolved_period_days: int,
    period_suffix: str,
    range_start: date,
    range_end: date,
    finance_diagnostics: dict[str, Any] | None = None,
    require_full_sync_coverage: bool = True,
    prefer_freshest_finance: bool = False,
    settings_overrides: dict[str, dict[str, Any]] | None = None,
    period_cache_memo: dict[tuple[Any, ...], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    goods = list_cached_goods(organization_id)
    finance_cache = _period_source_cache(
        organization_id,
        "finance",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        slim=True,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    period_cache = _period_source_cache(
        organization_id,
        "period_stats",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    ads_cache = _period_source_cache(
        organization_id,
        "ads",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    baskets_cache = _period_source_cache(
        organization_id,
        "baskets",
        period_suffix,
        resolved_period_days,
        range_start,
        range_end,
        require_full_sync_coverage=require_full_sync_coverage,
        prefer_freshest_covering=True,
        memo=period_cache_memo,
    )
    stocks_cache = get_source_cache(organization_id, "stocks", slim=True) or {}
    promotions_cache = get_source_cache(organization_id, "promotions", slim=True) or {}
    thresholds_cache = get_source_cache(organization_id, "promotion_thresholds", slim=True) or {}

    finance_aggregates = finance_cache.get("aggregates") if isinstance(finance_cache.get("aggregates"), dict) else {}
    finance_taxes = _repricer_finance_taxes(organization_id, finance_cache, range_start, range_end, memo=period_cache_memo)
    period_aggregates = period_cache.get("aggregates") if isinstance(period_cache.get("aggregates"), dict) else {}
    ads_aggregates = ads_cache.get("aggregates") if isinstance(ads_cache.get("aggregates"), dict) else {}
    baskets_aggregates = baskets_cache.get("aggregates") if isinstance(baskets_cache.get("aggregates"), dict) else {}
    stock_aggregates = stocks_cache.get("aggregates") if isinstance(stocks_cache.get("aggregates"), dict) else {}
    cached_promotions = promotions_cache.get("promotions") if isinstance(promotions_cache.get("promotions"), list) else []
    cached_promotions = _apply_promotion_threshold_cache(cached_promotions, thresholds_cache)
    membership = promotion_membership(cached_promotions)

    goods_by_nm_id = {
        str(_int_or_zero(good.get("nmID") or good.get("nmId"))): good
        for good in goods
        if isinstance(good, dict) and _int_or_zero(good.get("nmID") or good.get("nmId")) > 0
    }
    summary_goods = list(goods_by_nm_id.values())
    historical_aggregates: dict[str, dict[str, Any]] = {}
    for source in (finance_aggregates, period_aggregates, baskets_aggregates, ads_aggregates):
        for nm_key, aggregate in source.items():
            if nm_key not in goods_by_nm_id and isinstance(aggregate, dict):
                historical_aggregates.setdefault(nm_key, aggregate)
    summary_goods.extend(
        {"nmID": nm_key, "vendorCode": aggregate.get("vendorCode"), "historicalOnly": True}
        for nm_key, aggregate in historical_aggregates.items()
    )
    rows: list[dict[str, Any]] = []
    for good in summary_goods:
        if not isinstance(good, dict):
            continue
        nm_id = _int_or_zero(good.get("nmID") or good.get("nmId"))
        if nm_id <= 0:
            continue
        nm_key = str(nm_id)
        finance = dict(finance_aggregates.get(nm_key) or {})
        article_id = str(good.get("vendorCode") or finance.get("vendorCode") or "").strip()
        meta_override = repricer_bff_module.SKU_META_OVERRIDES.get(article_id) if article_id else None
        meta = {
            "articleId": article_id,
            "nmId": nm_id,
            "status": (meta_override or {}).get("status") or "manual",
            "historicalOnly": bool(good.get("historicalOnly")),
        }

        period = dict(period_aggregates.get(nm_key) or {})
        ads = dict(ads_aggregates.get(nm_key) or {})
        baskets = dict(baskets_aggregates.get(nm_key) or {})
        stock = dict(stock_aggregates.get(nm_key) or {})
        analytics: dict[str, Any] = {}

        seller_revenue_kopecks = _int_or_zero(
            finance.get("sellerRevenueKopecks") or finance.get("revenueGrossKopecks") or finance.get("revenueKopecks")
        )
        from app.repricer_page_finance import report_revenue_kopecks
        revenue_kopecks = _int_or_zero(report_revenue_kopecks(finance))
        profit_revenue_kopecks = revenue_kopecks if finance.get("realizationVersion") else _int_or_zero(finance.get("buyerRevenueKopecks")) or revenue_kopecks
        sales_units = _int_or_zero(finance.get("salesUnits"))
        returns_units = _int_or_zero(finance.get("returnsUnits"))
        settings = dict((settings_overrides or {}).get(article_id) or (repricer_bff_module._sku_cost_settings(article_id, use_demo_data=False) if article_id else {}))
        cogs_total_kopecks = _int_or_zero(finance.get("cogsTotalKopecks"))
        if cogs_total_kopecks == 0 and (sales_units or returns_units):
            cogs_total_kopecks = _int_or_zero(settings.get("cogsKopecks")) * (sales_units - returns_units)

        commission_kopecks = _int_or_zero(finance.get("commissionKopecks"))
        logistics_kopecks = _int_or_zero(finance.get("logisticsKopecks"))
        storage_kopecks = _int_or_zero(finance.get("storageKopecks"))
        acceptance_kopecks = _int_or_zero(finance.get("acceptanceKopecks"))
        penalty_kopecks = _int_or_zero(finance.get("penaltyKopecks"))
        deduction_kopecks = _int_or_zero(finance.get("deductionKopecks"))
        loyalty_cost_kopecks = _int_or_zero(finance.get("loyaltyCostKopecks"))
        acquiring_kopecks = _int_or_zero(finance.get("acquiringKopecks"))
        ad_spend_kopecks = _int_or_zero(
            finance.get("adSpendKopecks")
            if finance.get("financeAdSpendAuthoritative")
            else ads.get("adSpendKopecks") or finance.get("adSpendKopecks")
        )
        other_expenses_kopecks = 0
        tax = finance_taxes.get(nm_key) or {"taxKopecks": None, "factTaxState": "missing", "factTaxReason": "tax_context_missing"}
        tax_kopecks = tax.get("taxKopecks") if tax.get("factTaxState") == "configured" else None
        work_return_kopecks = _int_or_zero(settings.get("workReturnPerSaleKopecks")) * (sales_units - returns_units)
        additional_payment_kopecks = _int_or_zero(finance.get("additionalPaymentKopecks"))
        expenses_kopecks = _int_or_zero(finance.get("expensesKopecks"))
        if "expensesKopecks" not in finance:
            expenses_kopecks = (
                commission_kopecks
                + logistics_kopecks
                + storage_kopecks
                + acceptance_kopecks
                + penalty_kopecks
                + deduction_kopecks
                + loyalty_cost_kopecks
                + acquiring_kopecks
                + ad_spend_kopecks
                + other_expenses_kopecks
                - additional_payment_kopecks
            )

        net_profit_kopecks = (
            profit_revenue_kopecks
            + work_return_kopecks
            - cogs_total_kopecks
            - expenses_kopecks
            - tax_kopecks
        ) if tax_kopecks is not None else None

        funnel_order_count = _int_or_zero(baskets.get("orderCount")) if baskets.get("orderCount") is not None else None

        analytics.update(
            {
                "ordersUnits": funnel_order_count if funnel_order_count is not None else _int_or_zero(period.get("ordersUnits")),
                "ordersSource": "sales_funnel.orderCount" if funnel_order_count is not None else "supplier.orders" if nm_key in period_aggregates else None,
                "cancelledOrdersUnits": _int_or_zero(period.get("cancelledOrdersUnits")),
                "funnelOrderCount": funnel_order_count,
                "reportOrdersUnits": funnel_order_count if funnel_order_count is not None else period.get("ordersUnits"),
                "ordersAmountKopecks": baskets.get("orderSumKopecks"),
                "grossSalesUnits": finance.get("salesUnits") if finance else None,
                "grossSalesAmountKopecks": finance.get("realizationGrossSalesKopecks", finance.get("grossSalesKopecks")) if finance and not finance.get("sellerRevenueMissingRows") else None,
                "buyoutUnits": finance.get("netSalesUnits", sales_units - returns_units) if finance else None,
                "buyoutAmountKopecks": report_revenue_kopecks(finance) if finance else None,
                "salesUnits": sales_units,
                "returnsUnits": returns_units,
                "revenueKopecks": revenue_kopecks,
                "sellerRevenueKopecks": seller_revenue_kopecks,
                "realizationRevenueKopecks": finance.get("realizationRevenueKopecks"),
                "realizationVersion": finance.get("realizationVersion"),
                "buyerRevenueKopecks": profit_revenue_kopecks,
                "cogsTotalKopecks": cogs_total_kopecks,
                "expensesKopecks": expenses_kopecks,
                "netProfitKopecks": net_profit_kopecks,
                "commissionKopecks": commission_kopecks,
                "logisticsKopecks": logistics_kopecks,
                "storageKopecks": storage_kopecks,
                "acceptanceKopecks": acceptance_kopecks,
                "penaltyKopecks": penalty_kopecks,
                "penaltyChargedKopecks": _int_or_zero(finance.get("penaltyChargedKopecks")),
                "penaltyReturnedKopecks": _int_or_zero(finance.get("penaltyReturnedKopecks")),
                "deductionKopecks": deduction_kopecks,
                "deductionChargedKopecks": _int_or_zero(finance.get("deductionChargedKopecks")),
                "deductionCompensationKopecks": _int_or_zero(finance.get("deductionCompensationKopecks")),
                "additionalPaymentKopecks": additional_payment_kopecks,
                "rewardAdjustmentKopecks": _int_or_zero(finance.get("rewardAdjustmentKopecks")),
                "paymentScheduleKopecks": _int_or_zero(finance.get("paymentScheduleKopecks")),
                "loyaltyCostKopecks": loyalty_cost_kopecks,
                "acquiringKopecks": acquiring_kopecks,
                "adSpendKopecks": ad_spend_kopecks,
                "adDataAvailable": bool(ads) or bool(finance.get("financeAdSpendAuthoritative")),
                "adMetricsAvailable": bool(ads),
                "adImpressions": _int_or_zero(ads.get("adImpressions")),
                "adClicks": _int_or_zero(ads.get("adClicks")),
                "adCartAdds": _int_or_zero(ads.get("adCartAdds")),
                "adOrders": _int_or_zero(ads.get("adOrders")),
                "adRevenueKopecks": _int_or_zero(ads.get("adRevenueKopecks")),
                "otherExpensesKopecks": other_expenses_kopecks,
                "taxKopecks": tax_kopecks,
                "factTaxState": tax.get("factTaxState"),
                "factTaxReason": tax.get("factTaxReason"),
                "factNetProfitKopecks": net_profit_kopecks,
                "financeState": "ok" if nm_key in finance_aggregates else "no_data",
                "workReturnKopecks": work_return_kopecks,
                "baskets": _int_or_zero(baskets.get("cartCount")) if baskets.get("cartCount") is not None else None,
                "totalImpressions": baskets.get("impressions"),
                "totalClicks": baskets.get("openCount"),
                "basketsState": baskets.get("coverageState") or ("ok" if baskets.get("cartCount") is not None else "no_data"),
                "wbStockUnits": _int_or_zero(stock.get("wbStockUnits")),
                **membership.analytics(nm_id),
            }
        )
        if revenue_kopecks > 0 and net_profit_kopecks is not None:
            analytics["marginPct"] = round(_int_or_zero(net_profit_kopecks) / revenue_kopecks * 100, 1)
        analytics.update(repricer_bff_module.settlement_profit_metrics(finance, tax))
        rows.append({"meta": meta, "analytics": analytics})

    campaign_ads = None
    if _page_finance_active():
        from app.wb_repricer_reporting import apply_campaign_advertising, campaign_advertising
        campaign_ads = campaign_advertising(ads_cache, range_start, range_end)
        apply_campaign_advertising(rows, finance_cache, campaign_ads)
        from app.wb_repricer_reporting import apply_repricer_economics
        apply_repricer_economics(rows, finance_cache, campaign_ads, finance_taxes)
    summary = _repricer_list_summary(
        rows,
        finance_diagnostics=finance_diagnostics,
        ads_totals=_ads_cache_totals(ads_cache) if ads_cache else None,
        campaign_ads=campaign_ads,
    )
    summary["skuCount"] = len(goods_by_nm_id)
    summary["promoSharePct"] = (
        round(
            sum(
                1
                for row in rows
                if not row["meta"].get("historicalOnly")
                and row["analytics"].get("promotionStatus") == "yes"
            )
            / len(goods_by_nm_id)
            * 100
        )
        if goods_by_nm_id
        else 0
    )
    from app.wb_repricer_reporting import period_coverage
    _apply_period_reporting(summary, organization_id, range_start, range_end, ledger=finance_cache,
        coverage={"finance": period_coverage(finance_cache, range_start, range_end),
                  "baskets": period_coverage(baskets_cache, range_start, range_end)}, memo=period_cache_memo)
    return summary


def _pct_or_none(numerator: int | float | None, denominator: int | float | None) -> float | None:
    try:
        num = float(numerator or 0)
        den = float(denominator or 0)
    except (TypeError, ValueError):
        return None
    if den <= 0:
        return None
    return round(num / den * 100, 1)


_REPRICER_STATS_SOURCE_FIELDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("finance", "financeState", ("ok", "fallback")),
    ("stocks", "stockState", ("ok", "fallback")),
    ("baskets", "basketsState", ("ok", "fallback")),
    ("periodStats", "periodStatsState", ("ok", "fallback")),
    ("spp", "sppState", ("ok", "fallback")),
    ("commission", "commissionState", ("ok", "fallback")),
)


def _repricer_stats_sources(row: dict[str, Any]) -> dict[str, Any]:
    analytics = row.get("analytics") or {}
    missing: list[str] = []
    fresh: list[str] = []
    states: dict[str, Any] = {}
    states["ads"] = analytics.get("adsState", "no_data")
    for source_id, field, ready_values in _REPRICER_STATS_SOURCE_FIELDS:
        state = analytics.get(field)
        if (analytics.get("periodCoverageStates") or {}).get(field) == "partial":
            state = "partial"
        states[source_id] = state
        if state in ready_values:
            fresh.append(source_id)
        else:
            missing.append(source_id)
    status = "ready" if not missing else ("blocked" if any(source in missing for source in ("finance", "spp", "commission")) else "partial")
    return {
        "status": status,
        "missing": missing,
        "fresh": fresh,
        "states": states,
    }


_REPRICER_STATS_SKIP_LABELS = {
    "no_strategy": "Не назначена стратегия",
    "no_nm_id": "Нет артикула WB",
    "manual_mode": "Ручной режим",
    "automation_disabled": "Отправка цен отключена",
    "warmup": "Прогрев товара",
}


def _repricer_stats_price_protection(row: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") or {}
    settings = row.get("settings") or {}
    analytics = row.get("analytics") or {}
    blockers: list[str] = []
    messages: list[str] = []
    effective_pmin = None
    effective_pmin_source = None
    cost_inputs = [_float_or_none(settings.get(key)) for key in ("cogsKopecks", "logisticsKopecks", "wbCommissionPct")]
    try:
        if repricer_bff_module._settings_pmin_override_kopecks(settings) is not None or all(value is not None and value >= 0 for value in cost_inputs):
            effective_pmin, effective_pmin_source = repricer_bff_module._effective_pmin(row)
    except (TypeError, ValueError, OverflowError, KeyError):
        pass
    floor = {"effectivePMinKopecks": effective_pmin, "effectivePMinSource": effective_pmin_source}

    if _int_or_zero(meta.get("currentPriceKopecks")) <= 0:
        blockers.append("current_price")
        messages.append("Нет текущей цены WB")
    if effective_pmin is None or effective_pmin <= 0:
        blockers.append("p_min")
        messages.append("Нет минимальной цены или настроек для её расчёта")
    if analytics.get("sppState") not in {"ok", "fallback"} or _float_or_none(analytics.get("sppPct")) is None or _int_or_zero(analytics.get("buyerPriceNoWalletKopecks") or analytics.get("avgPriceWithSppKopecks")) <= 0:
        blockers.append("spp")
        messages.append("Нет SPP/цены покупателя")
    if analytics.get("commissionState") != "ok":
        blockers.append("commission")
        messages.append("Нет актуального тарифа комиссии WB")
    buyout_pct = _float_or_none(analytics.get("buyoutPct"))
    if buyout_pct is None or not 0 <= buyout_pct <= 100:
        blockers.append("buyout")
        messages.append("Нет подтверждённого процента выкупа")
    if analytics.get("stockState") not in {"ok", "fallback"} or _int_or_zero(analytics.get("wbStockUnits")) <= 0:
        blockers.append("stocks")
        messages.append("Нет доступных остатков WB")

    # These are the executor's early skip conditions, before source/apply guards.
    skip_reason = None
    if not bool(settings.get("automationEnabled", True)):
        skip_reason = "automation_disabled"
    elif meta.get("status") == "warmup":
        skip_reason = "warmup"
    elif meta.get("status") == "manual":
        skip_reason = "manual_mode"
    elif meta.get("nmId") is None:
        skip_reason = "no_nm_id"
    else:
        frontend_id, _ = _resolve_strategy_id(row)
        if frontend_id is None or (frontend_id != "illiquid" and not _has_explicit_strategy(row)):
            skip_reason = "no_strategy"
    if skip_reason is not None:
        return {
            **floor,
            "status": "not_configured" if skip_reason in {"no_strategy", "no_nm_id"} else "paused",
            "skipReason": skip_reason,
            "blockerIds": blockers,
            "message": "; ".join([_REPRICER_STATS_SKIP_LABELS[skip_reason], *messages]),
        }

    if blockers:
        return {
            **floor,
            "status": "blocked",
            "blockerIds": blockers,
            "message": "; ".join(messages),
        }
    if sources.get("status") != "ready":
        return {
            **floor,
            "status": "needs_review",
            "blockerIds": [],
            "message": "Есть неполные вторичные источники",
        }
    return {
        **floor,
        "status": "can_recalculate",
        "blockerIds": [],
        "message": "Можно пересчитать цену",
    }


def _repricer_stats_metrics(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") or {}
    analytics = row.get("analytics") or {}
    ad_data_available = analytics.get("adDataAvailable")
    has_ad_data = True if ad_data_available is None else bool(ad_data_available)
    has_ad_metrics = bool(analytics.get("adMetricsAvailable", has_ad_data))
    impressions = observed_number(analytics.get("adImpressions"))
    clicks = observed_number(analytics.get("adClicks"))
    baskets_raw = analytics.get("baskets")
    baskets = _int_or_zero(baskets_raw) if baskets_raw is not None else None
    orders_source = analytics.get("ordersSource", "explicit")
    orders = _int_or_zero(analytics.get("ordersUnits") or analytics.get("funnelOrderCount")) if orders_source not in {None, "finance_sales_fallback", "fallback"} else None
    revenue_kopecks = _int_or_zero(analytics.get("revenueKopecks", analytics.get("sellerRevenueKopecks"))) if analytics.get("financeState", "ok") in {"ok", "fallback"} else None
    ad_spend_kopecks = observed_number(analytics.get("adSpendKopecks"))
    ad_revenue_kopecks = observed_number(analytics.get("adRevenueKopecks")) if has_ad_metrics else None
    display_impressions: int | None = impressions if has_ad_metrics else None
    display_clicks: int | None = clicks if has_ad_metrics else None
    display_ad_spend_kopecks: int | None = ad_spend_kopecks if has_ad_data else None
    return {
        "totalImpressions": analytics.get("totalImpressions"),
        "totalClicks": analytics.get("totalClicks") if analytics.get("basketsState", "ok") in {"ok", "fallback", "partial"} else None,
        "impressions": display_impressions,
        "clicks": display_clicks,
        "ctrPct": ratio(clicks, impressions) if has_ad_metrics else None,
        "adCartAdds": observed_number(analytics.get("adCartAdds")) if has_ad_metrics else None,
        "adOrders": observed_number(analytics.get("adOrders")) if has_ad_metrics else None,
        "baskets": baskets,
        "orders": orders,
        "cartToOrderCrPct": _pct_or_none(orders, baskets),
        "revenueKopecks": revenue_kopecks,
        "netProfitKopecks": analytics.get("netProfitKopecks"),
        "factNetProfitKopecks": analytics.get("factNetProfitKopecks"),
        "taxKopecks": analytics.get("taxKopecks"),
        "factTaxState": analytics.get("factTaxState"),
        "factTaxReason": analytics.get("factTaxReason"),
        "marginPct": _float_or_none(analytics.get("marginPct")),
        "adSpendKopecks": display_ad_spend_kopecks,
        "adRevenueKopecks": ad_revenue_kopecks,
        "drrPct": ratio(display_ad_spend_kopecks, ad_revenue_kopecks, 2),
        "stockUnits": analytics.get("wbStockUnits") if analytics.get("stockState", "ok") in {"ok", "fallback"} else None,
        "currentPriceKopecks": meta.get("currentPriceKopecks"),
        "avgPriceWithSppKopecks": analytics.get("avgPriceWithSppKopecks"),
        "averagePriceKopecks": analytics.get("avgPriceWithSppKopecks"),
        "averagePriceOrderCount": analytics.get("funnelOrderCount"),
        "medianPriceKopecks": analytics.get("medianPriceKopecks") or analytics.get("avgPriceWithSppKopecks") or meta.get("currentPriceKopecks"),
        "sppPct": _float_or_none(analytics.get("sppPct")),
        "buyoutPct": _float_or_none(analytics.get("buyoutPct")),
        "commissionPct": _float_or_none(analytics.get("commissionDisplayPct")),
    }


def _repricer_stats_flags(row: dict[str, Any], metrics: dict[str, Any], sources: dict[str, Any], price_protection: dict[str, Any]) -> list[str]:
    meta = row.get("meta") or {}
    analytics = row.get("analytics") or {}
    flags: list[str] = []
    if analytics.get("promotionStatus") == "yes":
        flags.append("promo")
    if any(_int_or_zero(metrics.get(key)) > 0 for key in ("impressions", "clicks", "adSpendKopecks", "adOrders")):
        flags.append("ads")
    if sources.get("status") != "ready":
        flags.append("source_not_ready")
    if price_protection.get("status") == "blocked":
        flags.append("price_blocked")
    stock_units = metrics.get("stockUnits")
    if stock_units is not None and _int_or_zero(stock_units) <= 0:
        flags.append("oos_risk")
    margin_pct = metrics.get("marginPct")
    if margin_pct is not None and float(margin_pct) < 10:
        flags.append("margin_risk")
    basket_norm = _int_or_zero(meta.get("basketNorm"))
    if basket_norm > 0 and metrics.get("baskets") is not None and _int_or_zero(metrics["baskets"]) < basket_norm:
        flags.append("below_basket_norm")
    return flags


def _repricer_stats_decision(
    row: dict[str, Any],
    *,
    sources: dict[str, Any],
    price_protection: dict[str, Any],
    flags: list[str],
) -> dict[str, Any]:
    skip_reason = price_protection.get("skipReason")
    if skip_reason in _REPRICER_STATS_SKIP_LABELS:
        not_configured = price_protection.get("status") == "not_configured"
        return {
            "id": "not_configured" if not_configured else "hold",
            "label": _REPRICER_STATS_SKIP_LABELS[skip_reason],
            "tone": "warn" if not_configured else "neutral",
            "reasons": [skip_reason],
        }
    if price_protection.get("status") == "blocked":
        return {
            "id": "price_blocked",
            "label": "Цена заблокирована",
            "tone": "bad",
            "reasons": list(price_protection.get("blockerIds") or []),
        }
    if sources.get("status") != "ready":
        return {
            "id": "check_source",
            "label": "Проверить источники",
            "tone": "warn",
            "reasons": list(sources.get("missing") or []),
        }
    meta = row.get("meta") or {}
    if "oos_risk" in flags:
        return {"id": "oos_risk", "label": "Риск out-of-stock", "tone": "warn", "reasons": ["stocks"]}
    if "margin_risk" in flags:
        return {"id": "margin_risk", "label": "Низкая маржа", "tone": "warn", "reasons": ["margin"]}
    if str(meta.get("status") or "") in {"manual", "paused"}:
        return {"id": "hold", "label": "Ручной режим", "tone": "neutral", "reasons": ["manual_status"]}
    return {"id": "can_recalculate", "label": "Можно пересчитать", "tone": "ok", "reasons": []}


def _repricer_stats_item(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") or {}
    analytics = row.get("analytics") or {}
    sources = _repricer_stats_sources(row)
    price_protection = _repricer_stats_price_protection(row, sources)
    metrics = _repricer_stats_metrics(row)
    flags = _repricer_stats_flags(row, metrics, sources, price_protection)
    decision = _repricer_stats_decision(row, sources=sources, price_protection=price_protection, flags=flags)
    return {
        "articleId": meta.get("articleId"),
        "nmId": meta.get("nmId"),
        "name": meta.get("name"),
        "subject": meta.get("subject"),
        "brand": meta.get("brand"),
        "imageUrl": meta.get("imageUrl") or meta.get("photoUrl") or repricer_bff_module._wb_public_photo_url(_positive_int_or_none(meta.get("nmId"))),
        "photoUrl": meta.get("photoUrl") or meta.get("imageUrl") or repricer_bff_module._wb_public_photo_url(_positive_int_or_none(meta.get("nmId"))),
        "status": meta.get("status"),
        "managerId": meta.get("managerId"),
        "managerName": meta.get("managerName"),
        "strategyId": (row.get("strategy") or {}).get("id"),
        "strategyName": (row.get("strategy") or {}).get("name"),
        "promotionStatus": analytics.get("promotionStatus"),
        "promotionStatusText": analytics.get("promotionStatusText"),
        **{key: analytics.get(key) for key in (
            "promotionName", "promotionId", "promotionStatusScope", "promotionStatusSource",
            "promotionStatusReason", "promotionStatusObservedAt", "promotionStatusExpiresAt",
        )},
        "decision": decision,
        "flags": flags,
        "metrics": metrics,
        "priceProtection": price_protection,
        "sources": sources,
    }


def _repricer_stats_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    impressions = sum(_int_or_zero((item.get("metrics") or {}).get("impressions")) for item in items)
    clicks = sum(_int_or_zero((item.get("metrics") or {}).get("clicks")) for item in items)
    baskets = (
        sum(_int_or_zero((item.get("metrics") or {}).get("baskets")) for item in items)
        if all((item.get("metrics") or {}).get("baskets") is not None for item in items)
        else None
    )
    orders = sum(_int_or_zero((item.get("metrics") or {}).get("orders")) for item in items)
    ad_spend_kopecks = sum(_int_or_zero((item.get("metrics") or {}).get("adSpendKopecks")) for item in items)
    revenue_kopecks = sum(_int_or_zero((item.get("metrics") or {}).get("revenueKopecks")) for item in items)
    metric_fields = (
        "totalImpressions", "impressions", "totalClicks", "clicks", "adCartAdds", "baskets",
        "ctrPct", "cartToOrderCrPct", "orders", "adSpendKopecks", "drrPct", "stockUnits", "averagePriceKopecks",
    )
    ratio_fields = {
        "ctrPct": ("clicks", "impressions"),
        "cartToOrderCrPct": ("orders", "baskets"),
        "drrPct": ("adSpendKopecks", "adRevenueKopecks"),
    }
    funnel_fields = {"totalImpressions", "totalClicks", "baskets", "cartToOrderCrPct", "averagePriceKopecks"}
    ad_fields = {"impressions", "clicks", "adCartAdds", "adSpendKopecks", "ctrPct", "drrPct"}
    metric_totals = {}
    for field in metric_fields:
        if field in ratio_fields:
            numerator, denominator = ratio_fields[field]
            covered = [item for item in items if all((item.get("metrics") or {}).get(key) is not None for key in (numerator, denominator))]
            value = ratio(
                sum((item["metrics"][numerator] for item in covered), 0),
                sum((item["metrics"][denominator] for item in covered), 0),
                2 if field == "drrPct" else 1,
            )
        elif field == "averagePriceKopecks":
            covered = [item for item in items if (item.get("metrics") or {}).get(field) is not None and _int_or_zero((item.get("metrics") or {}).get("averagePriceOrderCount")) > 0]
            order_count = sum(_int_or_zero(item["metrics"]["averagePriceOrderCount"]) for item in covered)
            value = round(sum(item["metrics"][field] * item["metrics"]["averagePriceOrderCount"] for item in covered) / order_count) if order_count else None
        else:
            covered = [item for item in items if (item.get("metrics") or {}).get(field) is not None]
            value = sum(item["metrics"][field] for item in covered) if covered else None
        metric_totals[field] = {
            "value": value,
            "coveredSku": len(covered),
            "partialSku": sum(1 for item in covered if (item.get("sources") or {}).get("states", {}).get(
                "baskets" if field in funnel_fields else "ads" if field in ad_fields else "periodStats") == "partial"),
        }
    return {
        "skuCount": len(items),
        "canRecalculate": sum(1 for item in items if (item.get("decision") or {}).get("id") == "can_recalculate"),
        "priceBlocked": sum(1 for item in items if (item.get("priceProtection") or {}).get("status") == "blocked"),
        "sourceReady": sum(1 for item in items if (item.get("sources") or {}).get("status") == "ready"),
        "sourcePartial": sum(1 for item in items if (item.get("sources") or {}).get("status") == "partial"),
        "sourceBlocked": sum(1 for item in items if (item.get("sources") or {}).get("status") == "blocked"),
        "impressions": impressions,
        "clicks": clicks,
        "adCtrPct": _pct_or_none(clicks, impressions),
        "baskets": baskets,
        "orders": orders,
        "cartToOrderCrPct": _pct_or_none(orders, baskets),
        "adSpendKopecks": ad_spend_kopecks,
        "revenueKopecks": revenue_kopecks,
        "drrPct": metric_totals["drrPct"]["value"],
        "metricTotals": metric_totals,
    }


def _simulator_is_explicit_strategy(row: dict[str, Any]) -> bool:
    strategy = row.get("strategy") or {}
    source = str(
        strategy.get("assignmentSource")
        or row.get("meta", {}).get("assignmentSource")
        or ""
    ).lower().strip()
    return bool(strategy.get("id")) and source not in {"", "derived"}


def _simulator_is_active(row: dict[str, Any]) -> bool:
    meta = row.get("meta") or {}
    article_id = str(meta.get("articleId") or "")
    return (
        (meta.get("status") == "auto" and _simulator_is_explicit_strategy(row))
        or (meta.get("status") == "liquidation" and article_id in repricer_bff_module.LIQUIDATION_ACTIVE)
    )


def _simulator_mode_payload() -> dict[str, Any]:
    settings = get_settings()
    real_apply_enabled = settings.real_price_apply_enabled
    execute_interval_minutes = _algorithm_interval_minutes(
        "syncIntervalMinutes",
        int(getattr(settings, "repricer_execute_interval_minutes", 60) or 60),
    )
    return {
        "wbApiMode": settings.wb_api_mode,
        "realPriceApplyEnabled": real_apply_enabled,
        "localPriceApplyEnabled": settings.repricer_local_price_apply_enabled,
        "schedulerEnabled": settings.repricer_scheduler_enabled,
        "executeIntervalMinutes": execute_interval_minutes,
        "simulationAllowed": True,
        "inputOverridesAllowed": True,
        "simulatorRunApplyAllowed": not real_apply_enabled,
        "simulatorRunMode": "preview_only" if real_apply_enabled else "local_apply",
    }


def _simulator_item(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") or {}
    strategy = row.get("strategy") or {}
    analytics = row.get("analytics") or {}
    settings = row.get("settings") or {}
    article_id = str(meta.get("articleId") or "")
    liquidation = repricer_bff_module.LIQUIDATION_ACTIVE.get(article_id)
    return {
        "articleId": article_id,
        "nmId": meta.get("nmId"),
        "name": meta.get("name"),
        "status": meta.get("status"),
        "strategy": {
            "id": strategy.get("id"),
            "name": strategy.get("name"),
            "type": strategy.get("type"),
            "typedStrategyId": strategy.get("typedStrategyId"),
            "assignmentSource": strategy.get("assignmentSource"),
        },
        "automationEnabled": bool(settings.get("automationEnabled", True)),
        "current": {
            "sellerPriceKopecks": meta.get("currentPriceKopecks"),
            "buyerPriceKopecks": analytics.get("buyerPriceNoWalletKopecks"),
            "sppPct": analytics.get("sppPct"),
            "baskets": analytics.get("baskets") if analytics.get("baskets") is not None else meta.get("basketsLast7d"),
            "basketNorm": meta.get("basketNorm"),
            "ordersUnits": analytics.get("ordersUnits"),
            "salesUnits": analytics.get("salesUnits"),
            "returnsUnits": analytics.get("returnsUnits"),
            "revenueKopecks": analytics.get("revenueKopecks"),
            "buyoutPct": analytics.get("buyoutPct"),
            "stockUnits": analytics.get("wbStockUnits"),
            "marginPct": analytics.get("marginPct"),
            "pMinKopecks": settings.get("pMinKopecks") or settings.get("pminKopecks"),
            "pMaxKopecks": settings.get("pMaxKopecks"),
        },
        "sourceState": {
            "price": meta.get("priceSource"),
            "baskets": analytics.get("basketsState"),
            "periodStats": analytics.get("periodStatsState"),
            "stock": analytics.get("stockState"),
            "spp": analytics.get("sppState"),
        },
        "liquidation": dict(liquidation) if isinstance(liquidation, dict) else None,
    }


def _simulator_dashboard_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    items = [_simulator_item(row) for row in rows if _simulator_is_active(row)]
    mode = _simulator_mode_payload()
    return {
        "mode": mode,
        "summary": {
            "activeTotal": len(items),
            "strategyCount": sum(1 for item in items if item.get("status") == "auto"),
            "liquidationCount": sum(1 for item in items if item.get("status") == "liquidation"),
        },
        "items": items,
        "worker": {
            "beatTask": "repricer.execute_assigned_all_orgs",
            "orgTask": "repricer.execute_assigned_for_org",
            "intervalMinutes": mode["executeIntervalMinutes"],
            "queue": "vella.default",
            "selectionRule": "automationEnabled && (explicit strategy assignment || active liquidation)",
            "priceApplyRule": (
                "Симулятор сохраняет тестовые входы WB; реальную отправку цены делает worker/manual execute"
                if mode["realPriceApplyEnabled"]
                else "WB apply выключен; локальный apply может обновлять локальное состояние и changelog"
            ),
        },
    }


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _worker_run_payload(run: dict[str, Any]) -> dict[str, Any]:
    report = run.get("report") if isinstance(run.get("report"), dict) else {}
    raw_items = report.get("items") if isinstance(report.get("items"), list) else []
    items = [
        {
            "articleId": item.get("articleId"),
            "status": item.get("status"),
            "skipReason": item.get("skipReason"),
            "frontendStrategyId": item.get("frontendStrategyId"),
            "explanation": item.get("explanation"),
            "applyState": item.get("applyState"),
            "draftId": item.get("draftId"),
            "oldPriceKopecks": item.get("oldPriceKopecks"),
            "recommendedPriceKopecks": item.get("recommendedPriceKopecks"),
            "deltaKopecks": item.get("deltaKopecks"),
            "blockedReasons": item.get("blockedReasons") if isinstance(item.get("blockedReasons"), list) else [],
            "blockerDetails": item.get("blockerDetails") if isinstance(item.get("blockerDetails"), list) else [],
        }
        for item in raw_items[:8]
        if isinstance(item, dict)
    ]
    return {
        "runId": run.get("runId"),
        "trigger": run.get("trigger"),
        "createdAt": run.get("createdAt"),
        "executedCount": int(report.get("executedCount") or 0),
        "skippedCount": int(report.get("skippedCount") or 0),
        "blockedCount": int(report.get("blockedCount") or 0),
        "itemCount": len(raw_items),
        "items": items,
    }


def _latest_execution_by_article_id(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        report = run.get("report") if isinstance(run.get("report"), dict) else {}
        raw_items = report.get("items") if isinstance(report.get("items"), list) else []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            article_id = str(item.get("articleId") or "")
            if not article_id or article_id in latest:
                continue
            latest[article_id] = {
                "runId": run.get("runId"),
                "trigger": run.get("trigger"),
                "createdAt": run.get("createdAt"),
                "status": item.get("status"),
                "skipReason": item.get("skipReason"),
                "frontendStrategyId": item.get("frontendStrategyId"),
                "strategyId": item.get("strategyId"),
                "explanation": item.get("explanation"),
                "applyState": item.get("applyState"),
                "draftId": item.get("draftId"),
                "jobId": item.get("jobId"),
                "oldPriceKopecks": item.get("oldPriceKopecks"),
                "recommendedPriceKopecks": item.get("recommendedPriceKopecks"),
                "deltaKopecks": item.get("deltaKopecks"),
                "blockedReasons": item.get("blockedReasons") if isinstance(item.get("blockedReasons"), list) else [],
                "blockerDetails": item.get("blockerDetails") if isinstance(item.get("blockerDetails"), list) else [],
            }
    return latest


def _pending_price_approval_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "approvalId": item.get("approvalId"),
        "draftId": item.get("draftId"),
        "jobId": item.get("jobId"),
        "runId": item.get("runId"),
        "trigger": item.get("trigger"),
        "articleId": item.get("articleId"),
        "nmId": item.get("nmId"),
        "name": item.get("name") or item.get("skuName") or item.get("productName"),
        "skuName": item.get("skuName") or item.get("name") or item.get("productName"),
        "productName": item.get("productName") or item.get("name") or item.get("skuName"),
        "status": item.get("status"),
        "applyState": item.get("applyState"),
        "sourceStatus": item.get("sourceStatus"),
        "wbMutationSent": item.get("wbMutationSent"),
        "priceConfirmed": item.get("priceConfirmed", False),
        "wbUploadId": item.get("wbUploadId"),
        "wbStatus": item.get("wbStatus"),
        "statusLabel": item.get("statusLabel"),
        "blockedReasons": item.get("blockedReasons") if isinstance(item.get("blockedReasons"), list) else [],
        "notes": item.get("notes") if isinstance(item.get("notes"), list) else [],
        "rowErrors": item.get("rowErrors") if isinstance(item.get("rowErrors"), list) else [],
        "error": item.get("error"),
        "source": item.get("source"),
        "scenario": item.get("scenario"),
        "frontendStrategyId": item.get("frontendStrategyId"),
        "strategyId": item.get("strategyId"),
        "strategyName": item.get("strategyName"),
        "oldPriceKopecks": item.get("oldPriceKopecks"),
        "recommendedPriceKopecks": item.get("recommendedPriceKopecks"),
        "deltaKopecks": item.get("deltaKopecks"),
        "explanation": item.get("explanation"),
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
    }


def _update_cached_goods_seller_price(
    organization_id: int,
    *,
    article_id: str,
    nm_id: int | None,
    seller_price_kopecks: int,
    wb_request_id: str,
    buyer_price_kopecks: int | None = None,
    spp_pct: float | None = None,
) -> bool:
    return bool(
        _update_cached_goods_seller_prices(
            organization_id,
            changes=[
                {
                    "articleId": article_id,
                    "nmId": nm_id,
                    "sellerPriceKopecks": seller_price_kopecks,
                    "buyerPriceKopecks": buyer_price_kopecks,
                    "sppPct": spp_pct,
                }
            ],
            wb_request_id=wb_request_id,
        )
    )


def _update_cached_goods_seller_prices(
    organization_id: int,
    *,
    changes: list[dict[str, Any]],
    wb_request_id: str,
) -> int:
    goods = list_cached_goods(organization_id)
    if not goods or not changes:
        return 0
    by_article = {str(item.get("vendorCode") or ""): item for item in goods}
    by_nm_id = {str(item.get("nmID") or ""): item for item in goods}
    updated = 0
    for change in changes:
        article_id = str(change.get("articleId") or "")
        nm_id = change.get("nmId")
        target = by_article.get(article_id) or (by_nm_id.get(str(nm_id)) if nm_id is not None else None)
        if target is None:
            continue
        seller_price_kopecks = int(change["sellerPriceKopecks"])
        buyer_price_kopecks = change.get("buyerPriceKopecks")
        spp_pct = change.get("sppPct")
        sizes = target.get("sizes") if isinstance(target.get("sizes"), list) else []
        size = dict(sizes[0]) if sizes and isinstance(sizes[0], dict) else {}
        size["price"] = seller_price_kopecks
        size["discountedPrice"] = seller_price_kopecks
        buyer = buyer_price_kopecks
        if buyer is None and spp_pct is not None:
            buyer = round(seller_price_kopecks * (1 - float(spp_pct) / 100))
        if buyer is not None:
            size["buyerPriceNoWalletKopecks"] = buyer
            size["buyerPriceKopecks"] = buyer
        if spp_pct is not None:
            size["spp"] = spp_pct
        target["sizes"] = [size]
        updated += 1
    if not updated:
        return 0
    save_goods_page(
        organization_id=organization_id,
        page_offset=0,
        page_limit=max(1000, len(goods)),
        goods=goods,
        wb_request_id=wb_request_id,
        replace_all=True,
    )
    return updated


def _simulator_update_goods_cache(
    organization_id: int,
    article_id: str,
    nm_id: int | None,
    payload: RepricerSimulatorPatchRequest,
) -> bool:
    if payload.sellerPriceKopecks is None:
        return False
    return _update_cached_goods_seller_price(
        organization_id,
        article_id=article_id,
        nm_id=nm_id,
        seller_price_kopecks=payload.sellerPriceKopecks,
        buyer_price_kopecks=payload.buyerPriceKopecks,
        spp_pct=payload.sppPct,
        wb_request_id="repricer-simulator",
    )


def _simulator_period_cache_suffixes(organization_id: int, period_days: int) -> list[str]:
    status = get_source_cache(organization_id, "wb_sync_status", slim=False) or {}
    suffixes: list[str] = []

    def add(value: Any) -> None:
        suffix = str(value or "").strip()
        if suffix and suffix not in suffixes:
            suffixes.append(suffix)

    status_days = _positive_int_or_none(status.get("periodDays"))
    if status_days is None or status_days == period_days:
        add(status.get("periodCacheSuffix"))
    add(period_days)
    return suffixes


def _apply_period_cache_context(cache: dict[str, Any], status: dict[str, Any], suffix: str, period_days: int) -> None:
    if str(status.get("periodCacheSuffix") or "") == suffix:
        if status.get("periodDays") is not None:
            cache.setdefault("periodDays", status.get("periodDays"))
        if status.get("dateFrom"):
            cache.setdefault("dateFrom", status.get("dateFrom"))
        if status.get("dateTo"):
            cache.setdefault("dateTo", status.get("dateTo"))
    else:
        cache.setdefault("periodDays", period_days)


def _simulator_update_source_caches(
    organization_id: int,
    nm_id: int,
    payload: RepricerSimulatorPatchRequest,
    *,
    period_days: int,
) -> None:
    sync_status = get_source_cache(organization_id, "wb_sync_status", slim=False) or {}
    for suffix in _simulator_period_cache_suffixes(organization_id, period_days):
        period_key = f"period_stats_{suffix}"
        period_cache = get_source_cache(organization_id, period_key, slim=False) or {}
        _apply_period_cache_context(period_cache, sync_status, suffix, period_days)
        period_aggregates = period_cache.get("aggregates") if isinstance(period_cache.get("aggregates"), dict) else {}
        period_row = dict(period_aggregates.get(str(nm_id)) or {})
        if payload.ordersUnits is not None:
            period_row["ordersUnits"] = payload.ordersUnits
        if payload.salesUnits is not None:
            period_row["salesUnits"] = payload.salesUnits
        if payload.returnsUnits is not None:
            period_row["returnsUnits"] = payload.returnsUnits
        if payload.revenueKopecks is not None:
            period_row["revenueKopecks"] = payload.revenueKopecks
        if payload.buyoutPct is not None:
            period_row["buyoutPct"] = payload.buyoutPct
        if payload.sppPct is not None:
            period_row["sppPct"] = payload.sppPct
            period_row["sppSource"] = "repricer.simulator"
            period_row["sppObservedAt"] = datetime.now(timezone.utc).isoformat()
        if payload.buyerPriceKopecks is not None:
            period_row["avgPriceWithSppKopecks"] = payload.buyerPriceKopecks
        if period_row:
            period_aggregates[str(nm_id)] = period_row
            period_cache["aggregates"] = period_aggregates
            period_cache["simulatedAt"] = datetime.now(timezone.utc).isoformat()
            save_source_cache(organization_id, period_key, period_cache)

        baskets_key = f"baskets_{suffix}"
        baskets_cache = get_source_cache(organization_id, baskets_key, slim=False) or {}
        _apply_period_cache_context(baskets_cache, sync_status, suffix, period_days)
        baskets_aggregates = baskets_cache.get("aggregates") if isinstance(baskets_cache.get("aggregates"), dict) else {}
        baskets_row = dict(baskets_aggregates.get(str(nm_id)) or {})
        if payload.baskets is not None:
            baskets_row["cartCount"] = payload.baskets
            baskets_aggregates[str(nm_id)] = baskets_row
            baskets_cache["aggregates"] = baskets_aggregates
            baskets_cache["requestedNmIds"] = max(int(baskets_cache.get("requestedNmIds") or 0), 1)
            baskets_cache["matchedNmIds"] = max(int(baskets_cache.get("matchedNmIds") or 0), 1)
            baskets_cache["simulatedAt"] = datetime.now(timezone.utc).isoformat()
            save_source_cache(organization_id, baskets_key, baskets_cache)

    stocks_cache = get_source_cache(organization_id, "stocks", slim=False) or {}
    stock_aggregates = stocks_cache.get("aggregates") if isinstance(stocks_cache.get("aggregates"), dict) else {}
    stock_row = dict(stock_aggregates.get(str(nm_id)) or {})
    if payload.stockUnits is not None:
        stock_row["wbStockUnits"] = payload.stockUnits
        stock_row.setdefault("inWayToClient", 0)
        stock_row.setdefault("inWayFromClient", 0)
        stock_row.setdefault("warehouses", 1)
        stock_aggregates[str(nm_id)] = stock_row
        stocks_cache["aggregates"] = stock_aggregates
        stocks_cache["simulatedAt"] = datetime.now(timezone.utc).isoformat()
        save_source_cache(organization_id, "stocks", stocks_cache)


def _simulator_update_runtime_state(
    article_id: str,
    payload: RepricerSimulatorPatchRequest,
    *,
    scenario: str,
    wb_token: str | None,
    sku_rows: list[dict[str, Any]],
) -> None:
    if payload.basketNorm is not None:
        overrides = repricer_bff_module.SKU_META_OVERRIDES.setdefault(article_id, {})
        overrides["basketNorm"] = payload.basketNorm
        overrides["basketNormSource"] = "manual"
    if payload.strategyId:
        apply_frontend_strategy_assignment(
            [article_id],
            strategy_id_or_name=payload.strategyId,
            scenario=scenario,
            wb_token=wb_token,
            source="simulator",
            sku_rows=sku_rows,
        )
    if payload.makeLiquidationDue and article_id in repricer_bff_module.LIQUIDATION_ACTIVE:
        repricer_bff_module.LIQUIDATION_ACTIVE[article_id]["nextStepAt"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()


def _ensure_repricer_stats_period_caches(
    organization_id: int,
    scenario: str,
    *,
    wb_token: str | None,
    resolved_period_days: int,
    period_suffix: str,
    range_start: datetime,
    range_end: datetime,
) -> dict[str, Any]:
    fetched_sources: list[str] = []
    missing_sources: list[str] = []
    stats_period_key = f"repricer_stats_period_stats_{period_suffix}"
    stats_finance_key = f"repricer_stats_finance_{period_suffix}"
    stats_ads_key = f"repricer_stats_ads_{period_suffix}"
    stats_baskets_key = f"repricer_stats_baskets_{period_suffix}"
    source_revisions = {
        prefix: get_source_cache_range_revision(organization_id, f"{prefix}_",
                                               date_from=range_start.date(), date_to=range_end.date())
        for prefix in ("period_stats", "finance", "ads", "baskets")
    }
    seller_revision = get_source_cache_fetched_at(organization_id, f"funnel_seller_{period_suffix}")
    raw_revision = get_source_cache_range_revision(organization_id, "repricer_finance_",
        date_from=range_start.date(), date_to=range_end.date())
    if raw_revision:
        source_revisions["finance"] = f"{source_revisions['finance']}|raw:{raw_revision}"
    if seller_revision:
        source_revisions["baskets"] = f"seller:{seller_revision}"

    def needs_refresh(prefix, cache):
        revision = source_revisions[prefix]
        return not cache or cache.get("statsProjectionVersion") != 3 or bool(revision and revision != cache.get("materializedSourceRevision", cache.get("fetchedAt")))

    def valid_observation(payload):
        aggregates = payload.get("aggregates")
        return _parse_utc_datetime(payload.get("fetchedAt")) is not None and isinstance(aggregates, dict) and all(isinstance(row, dict) for row in aggregates.values())

    def read_materialized(key):
        cache = get_source_cache(organization_id, key, slim=True) or {}
        if cache.get("source") == "repricer_stats_cache_materialized":
            # The store's fetchedAt is the materialization time, not the source time.
            if _parse_utc_datetime(cache.get("sourceFetchedAt")) is None:
                return {}
            cache["fetchedAt"] = cache["sourceFetchedAt"]
        return cache if _cache_matches_range(cache, range_start, range_end) else {}

    period_cache = read_materialized(stats_period_key)
    if needs_refresh("period_stats", period_cache):
        period_stats_payload = _period_source_cache(organization_id, "period_stats", period_suffix, resolved_period_days, range_start, range_end, slim=False, require_full_sync_coverage=False, prefer_freshest_covering=True)
        aggregates = period_stats_payload.get("aggregates") if isinstance(period_stats_payload.get("aggregates"), dict) else {}
        if valid_observation(period_stats_payload):
            period_cache = {
                **period_stats_payload,
                "aggregates": aggregates,
                "count": len(aggregates),
                "periodDays": resolved_period_days,
                "dateFrom": range_start.date().isoformat(),
                "dateTo": range_end.date().isoformat(),
                "source": "repricer_stats_cache_materialized",
                "sourceFetchedAt": period_stats_payload["fetchedAt"],
                "materializedSourceRevision": source_revisions["period_stats"],
            }
            period_cache = {**compact_cache(period_cache), "statsProjectionVersion": 3}
            stored = save_source_cache(organization_id, stats_period_key, period_cache)
            period_cache.setdefault("fetchedAt", stored.get("fetchedAt"))
            fetched_sources.append("period-stats")
        elif not period_cache:
            missing_sources.append("period-stats")

    finance_cache = _compatible_period_source_cache(
        "finance",
        read_materialized(stats_finance_key),
    )
    if needs_refresh("finance", finance_cache):
        finance_payload = _compatible_period_source_cache(
            "finance",
            _period_source_cache(
                organization_id,
                "finance",
                period_suffix,
                resolved_period_days,
                range_start,
                range_end,
                slim=False,
                require_full_sync_coverage=False,
                prefer_freshest_covering=True,
            ),
        )
        finance_aggregates = finance_payload.get("aggregates") if isinstance(finance_payload.get("aggregates"), dict) else {}
        if valid_observation(finance_payload):
            cached_goods_nm_ids = {str(nm_id) for nm_id in _nm_ids_from_goods(list_cached_goods(organization_id))}
            finance_cache = {
                **finance_payload,
                "periodDays": resolved_period_days,
                "dateFrom": range_start.date().isoformat(),
                "dateTo": range_end.date().isoformat(),
                "cachedGoodsNmIds": len(cached_goods_nm_ids),
                "matchedCachedGoodsNmIds": len(set(finance_aggregates.keys()) & cached_goods_nm_ids),
                "source": "repricer_stats_cache_materialized",
                "sourceFetchedAt": finance_payload["fetchedAt"],
                "materializedSourceRevision": source_revisions["finance"],
            }
            finance_cache = {**compact_cache(finance_cache), "statsProjectionVersion": 3}
            stored = save_source_cache(organization_id, stats_finance_key, finance_cache)
            finance_cache.setdefault("fetchedAt", stored.get("fetchedAt"))
            fetched_sources.append("finance")
        elif not finance_cache:
            missing_sources.append("finance")

    ads_cache = read_materialized(stats_ads_key)
    if needs_refresh("ads", ads_cache):
        ads_payload = _period_source_cache(organization_id, "ads", period_suffix, resolved_period_days, range_start, range_end, slim=False, require_full_sync_coverage=False, prefer_freshest_covering=True)
        ads_aggregates = ads_payload.get("aggregates") if isinstance(ads_payload.get("aggregates"), dict) else {}
        if valid_observation(ads_payload):
            ads_cache = {
                **ads_payload,
                "periodDays": resolved_period_days,
                "dateFrom": range_start.date().isoformat(),
                "dateTo": range_end.date().isoformat(),
                "source": "repricer_stats_cache_materialized",
                "sourceFetchedAt": ads_payload["fetchedAt"],
                "materializedSourceRevision": source_revisions["ads"],
            }
            ads_cache = {**compact_cache(ads_cache), "statsProjectionVersion": 3}
            stored = save_source_cache(organization_id, stats_ads_key, ads_cache)
            ads_cache.setdefault("fetchedAt", stored.get("fetchedAt"))
            fetched_sources.append("ads")
        elif not ads_cache:
            missing_sources.append("ads")

    baskets_cache = read_materialized(stats_baskets_key)
    baskets_payload = (_period_source_cache(organization_id, "baskets", period_suffix, resolved_period_days, range_start, range_end, slim=False, require_full_sync_coverage=False, prefer_freshest_covering=True)
                       if needs_refresh("baskets", baskets_cache) else {})
    cached_observed_at = _parse_utc_datetime(baskets_cache.get("fetchedAt"))
    source_observed_at = _parse_utc_datetime(baskets_payload.get("fetchedAt"))
    source_aggregates = baskets_payload.get("aggregates")
    if (
        isinstance(source_aggregates, dict)
        and all(isinstance(row, dict) for row in source_aggregates.values())
        and source_observed_at is not None
        and (
            not baskets_cache
            or cached_observed_at is None
            or source_observed_at > cached_observed_at
            or (source_observed_at == cached_observed_at and (
                baskets_cache.get("statsProjectionVersion") != 3
                or baskets_cache.get("materializedSourceRevision") != source_revisions["baskets"]))
        )
    ):
        baskets_cache = {
            **baskets_payload,
            "periodDays": resolved_period_days,
            "dateFrom": range_start.date().isoformat(),
            "dateTo": range_end.date().isoformat(),
            "source": "repricer_stats_cache_materialized",
            "sourceFetchedAt": baskets_payload["fetchedAt"],
            "materializedSourceRevision": source_revisions["baskets"],
            "statsProjectionVersion": 3,
        }
        baskets_cache = compact_cache(baskets_cache)
        save_source_cache(organization_id, stats_baskets_key, baskets_cache)
        fetched_sources.append("baskets")
    baskets_aggregates = baskets_cache.get("aggregates") if isinstance(baskets_cache.get("aggregates"), dict) else {}
    if not baskets_aggregates:
        missing_sources.append("baskets")

    return {
        "onDemandFetchedSources": fetched_sources,
        "onDemandFetched": bool(fetched_sources),
        "missingSources": missing_sources,
        "caches": {
            source: {**cache, "aggregates": dict(cache["aggregates"]) if isinstance(cache.get("aggregates"), dict) else {}} if cache else {}
            for source, cache in (("period_stats", period_cache), ("finance", finance_cache), ("ads", ads_cache), ("baskets", baskets_cache))
        },
    }


def _simulator_patch_payload(raw: dict[str, Any] | None) -> RepricerSimulatorPatchRequest:
    if not raw:
        return RepricerSimulatorPatchRequest()
    price_fields = {"sellerPriceKopecks", "buyerPriceKopecks"}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if value in (None, ""):
            continue
        if key in price_fields:
            try:
                if int(value) <= 0:
                    continue
            except (TypeError, ValueError):
                pass
        cleaned[key] = value
    try:
        return RepricerSimulatorPatchRequest.model_validate(cleaned)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SIMULATOR_INPUTS_INVALID",
                "message": "Некорректные входные данные симулятора",
                "details": {"issues": exc.errors()},
            },
        ) from exc


def _patch_simulator_row_inputs(row: dict[str, Any], payload: RepricerSimulatorPatchRequest) -> None:
    meta = row.setdefault("meta", {})
    analytics = row.setdefault("analytics", {})
    if payload.sellerPriceKopecks is not None:
        meta["currentPriceKopecks"] = payload.sellerPriceKopecks
        analytics["sellerDiscountedPriceKopecks"] = payload.sellerPriceKopecks
        analytics["basePriceKopecks"] = payload.sellerPriceKopecks
    if payload.buyerPriceKopecks is not None:
        analytics["buyerPriceNoWalletKopecks"] = payload.buyerPriceKopecks
        analytics["buyerPriceWithWalletKopecks"] = payload.buyerPriceKopecks
        analytics["accountedBuyerPriceKopecks"] = payload.buyerPriceKopecks
        analytics["avgPriceWithSppKopecks"] = payload.buyerPriceKopecks
    if payload.sppPct is not None:
        analytics["sppPct"] = payload.sppPct
        analytics["sppState"] = "ok"
        analytics["sppSource"] = "repricer.simulator"
    elif payload.sellerPriceKopecks is not None and payload.buyerPriceKopecks is None:
        analytics.setdefault("buyerPriceNoWalletKopecks", payload.sellerPriceKopecks)
        analytics.setdefault("buyerPriceWithWalletKopecks", payload.sellerPriceKopecks)
        analytics.setdefault("accountedBuyerPriceKopecks", payload.sellerPriceKopecks)
        analytics.setdefault("avgPriceWithSppKopecks", payload.sellerPriceKopecks)
        analytics.setdefault("sppPct", 0.0)
        analytics.setdefault("sppState", "ok")
        analytics.setdefault("sppSource", "repricer.simulator.fallback_zero")
    if payload.baskets is not None:
        analytics["baskets"] = payload.baskets
        analytics["basketsState"] = "ok"
        meta["basketsLast7d"] = payload.baskets
    if payload.basketNorm is not None:
        meta["basketNorm"] = payload.basketNorm
        meta["basketNormSource"] = "manual"
    if payload.ordersUnits is not None:
        analytics["ordersUnits"] = payload.ordersUnits
        analytics["ordersSource"] = "repricer.simulator"
        analytics["periodStatsState"] = "ok"
    if payload.salesUnits is not None:
        analytics["salesUnits"] = payload.salesUnits
    if payload.returnsUnits is not None:
        analytics["returnsUnits"] = payload.returnsUnits
    if payload.revenueKopecks is not None:
        analytics["revenueKopecks"] = payload.revenueKopecks
        analytics["sellerRevenueKopecks"] = payload.revenueKopecks
    if payload.buyoutPct is not None:
        analytics["buyoutPct"] = payload.buyoutPct
    if payload.stockUnits is not None:
        analytics["wbStockUnits"] = payload.stockUnits
        analytics["stockState"] = "ok"


def _patch_simulator_rows_inputs(rows: list[dict[str, Any]], article_id: str, payload: RepricerSimulatorPatchRequest) -> None:
    row = next((item for item in rows if item.get("meta", {}).get("articleId") == article_id), None)
    if row is not None:
        _patch_simulator_row_inputs(row, payload)


def _apply_simulator_inputs_for_article(
    request: Request,
    *,
    organization_id: int,
    wb_token: str | None,
    scenario: str,
    article_id: str,
    payload: RepricerSimulatorPatchRequest,
    period_days: int,
) -> list[dict[str, Any]]:
    rows = _list_repricer_skus_for_request(request, scenario, max_items=None, period_days=period_days)
    row = next((item for item in rows if item.get("meta", {}).get("articleId") == article_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="SKU_NOT_FOUND")
    nm_id = row.get("meta", {}).get("nmId")
    if nm_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SKU_HAS_NO_NM_ID",
                "message": "У SKU нет nmId WB. Обновите каталог WB перед запуском симулятора.",
                "details": {"articleId": article_id},
            },
        )

    _simulator_update_runtime_state(article_id, payload, scenario=scenario, wb_token=wb_token, sku_rows=rows)
    _simulator_update_goods_cache(organization_id, article_id, int(nm_id), payload)
    _simulator_update_source_caches(organization_id, int(nm_id), payload, period_days=period_days)
    refreshed_rows = _list_repricer_skus_for_request(request, scenario, max_items=None, period_days=period_days)
    _patch_simulator_rows_inputs(refreshed_rows, article_id, payload)
    return refreshed_rows


@router.get("/api/v1/wb-repricer/stats")
def get_repricer_stats(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90, alias="periodDays"),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=SKU_LIST_PAGE_SIZE_DEFAULT, alias="pageSize", ge=25, le=500),
    q: str = Query(default=""),
    status: str = Query(default="all"),
    brand: str = Query(default="all"),
    manager: str = Query(default="all"),
    top_mode: bool = Query(default=True, alias="topMode"),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    _range_start, _range_end, resolved_period_days, _period_suffix = _repricer_period_context(period_days, date_from, date_to)
    from app.wb_catalog_loading import require_goods_cache
    require_goods_cache(organization_id, cached_goods_meta(organization_id))
    filters_active = bool(
        str(q or "").strip()
        or (status and status != "all")
        or (brand and brand != "all")
        or (manager and manager != "all")
    )
    state_revision = _repricer_view_state_revision()
    tax_revision = legacy_finance_tax_revision(organization_id)
    snapshot_eligible = top_mode and not filters_active
    snapshot_options = dict(period_suffix=_period_suffix, include_promotions=False, include_content=False,
                            state_revision=state_revision, tax_revision=tax_revision)
    if snapshot_eligible:
        hit = _load_repricer_sku_snapshot_page(organization_id, scenario, **snapshot_options,
                                             view="stats", page=page, page_size=page_size)
        if hit is not None:
            snapshot, items = hit
            response = dict(snapshot["response"])
            response.update(items=items, itemsReturned=len(items), page=page, pageSize=page_size)
            response["cache"] = {**response["cache"], "statsItemsLimit": page_size, "statsPage": page,
                                 "statsOnDemandFetched": False, "statsOnDemandFetchedSources": []}
            return response
    source_revision = _snapshot_source_revision(organization_id, _range_start, _range_end)
    goods_meta = cached_goods_meta(organization_id)
    goods_price_revision = get_goods_price_revision(organization_id)
    browser_prices_read_at = datetime.now(timezone.utc).isoformat()
    browser_prices_revision = get_source_cache_fetched_at(organization_id, BROWSER_PRICES_KEY)

    def respond(all_rows, cache, *, missing_sources=False, fetched_sources=(), ads_cache=None):
        if ads_cache is None:
            ads_cache = _period_source_cache(organization_id, "ads", _period_suffix, resolved_period_days,
                _range_start, _range_end, require_full_sync_coverage=False, prefer_freshest_covering=True)
        filtered = [row for row in all_rows if _row_matches_sku_filters(
            row, q=q, status=status, brand=brand, manager=manager)]
        if top_mode:
            filtered.sort(key=_repricer_row_sort_key)
        items = [_repricer_stats_item(project_row(row, ads_cache)) for row in filtered]
        summary = _repricer_stats_summary(items)
        if not filters_active and _cache_matches_range(ads_cache, _range_start, _range_end):
            apply_account_totals(summary, ads_cache)
        total = len(filtered) if filters_active else max(len(filtered), int(cache.get("totalCached") or 0))
        source_status = "missing" if missing_sources or summary["sourceBlocked"] else "partial" if summary["sourcePartial"] else "ready"
        cache = {**cache, "statsItemsLimit": page_size, "statsPage": page, "statsTotalFiltered": total,
                 "requestedRange": _range_payload(_range_start, _range_end),
                 "statsSourceRange": _range_payload(_range_start, _range_end), "statsSourceStatus": source_status,
                 "statsRangeAdjusted": False, "statsOnDemandFetched": bool(fetched_sources),
                 "statsOnDemandFetchedSources": list(fetched_sources)}
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        response = {"items": page_items, "total": total, "totalCached": int(cache.get("totalCached") or len(all_rows)),
                    "itemsReturned": len(page_items), "page": page, "pageSize": page_size,
                    "periodDays": resolved_period_days, "dateFrom": _range_start.date().isoformat(),
                    "dateTo": _range_end.date().isoformat(), "summary": summary, "cache": cache}
        if snapshot_eligible:
            key = _repricer_sku_snapshot_key(scenario, period_suffix=_period_suffix,
                                            include_promotions=False, include_content=False, view="stats")
            _save_repricer_list_snapshot(organization_id, key, items, {
                "browserPricesExpireAt": rows_expire_at(filtered),
                "browserPricesRevision": browser_prices_revision,
                "browserPricesReadAt": browser_prices_read_at,
                "sourceRevision": source_revision, "goodsRevision": goods_meta.get("latestFetchedAt"),
                "goodsPriceRevision": goods_price_revision,
                "stateRevision": state_revision,
                "taxRevision": tax_revision,
                "response": {key: value for key, value in response.items() if key != "items"},
            })
        return response

    # Products already materializes these source-backed rows. Reuse them before
    # opening the large source payloads; filtered summaries still use all matches.
    if top_mode:
        products = _load_repricer_sku_snapshot_page(organization_id, scenario, **snapshot_options,
                                                  page=1, page_size=max(1, int(goods_meta.get("totalCached") or 0)))
        if products is not None:
            snapshot, rows = products
            browser_prices_read_at = snapshot.get("browserPricesReadAt")
            browser_prices_revision = snapshot.get("browserPricesRevision")
            cache = dict(snapshot.get("cache") or {})
            missing = any(not cache.get(field) for field in ("periodStatsFetchedAt", "financeFetchedAt", "adsFetchedAt", "basketsFetchedAt"))
            return respond(rows, cache, missing_sources=missing)
    stats_fetch_meta = _ensure_repricer_stats_period_caches(
        organization_id,
        scenario,
        wb_token=None,
        resolved_period_days=resolved_period_days,
        period_suffix=_period_suffix,
        range_start=_range_start,
        range_end=_range_end,
    )
    stats_caches = stats_fetch_meta["caches"]
    goods = list_cached_goods(organization_id)
    content_cache = get_source_cache(organization_id, "content_cards", slim=True) or {}
    promotions_cache = get_source_cache(organization_id, "promotions", slim=True) or {}
    thresholds_cache = get_source_cache(organization_id, "promotion_thresholds", slim=True) or {}
    stocks_cache = get_source_cache(organization_id, "stocks", slim=True) or {}
    cached_promotions = promotions_cache.get("promotions") if isinstance(promotions_cache.get("promotions"), list) else []
    cached_promotions = _apply_promotion_threshold_cache(cached_promotions, thresholds_cache)
    all_rows = list_repricer_skus(
        scenario,
        wb_token=None,
        include_promotions=True,
        include_content=True,
        tolerate_content_errors=True,
        list_view=True,
        max_items=None,
        cached_goods=goods,
        cached_content_cards=content_cache.get("cards") if isinstance(content_cache.get("cards"), list) else [],
        cached_promotions=cached_promotions,
        cached_stock_aggregates=stocks_cache.get("aggregates") if isinstance(stocks_cache.get("aggregates"), dict) else {},
        stocks_cache_loaded=bool(stocks_cache.get("fetchedAt")),
        cached_period_stats=stats_caches["period_stats"].get("aggregates", {}),
        cached_finance_aggregates=stats_caches["finance"].get("aggregates", {}),
        cached_finance_taxes=_repricer_finance_taxes(organization_id, stats_caches["finance"], _range_start, _range_end),
        cached_ads_aggregates=stats_caches["ads"].get("aggregates", {}),
        cached_baskets_aggregates=stats_caches["baskets"].get("aggregates", {}),
        baskets_cache_loaded=bool(stats_caches["baskets"].get("fetchedAt")),
        period_days=resolved_period_days,
        sort_by_demand=top_mode,
        allow_commission_tariff_fetch=False,
        organization_id=organization_id,
    )
    cache = _repricer_list_cache_meta(
        organization_id,
        resolved_period_days,
        include_content=True,
        date_from=_range_start.date(),
        date_to=_range_end.date(),
        require_full_sync_coverage=False,
        period_caches=stats_caches,
    )
    period_meta = stats_caches["period_stats"]
    finance_meta = stats_caches["finance"]
    ads_meta = stats_caches["ads"]
    ads_totals = _ads_cache_totals(ads_meta)
    baskets_meta = stats_caches["baskets"]
    cache["periodStatsFetchedAt"] = period_meta.get("fetchedAt")
    cache["financeFetchedAt"] = finance_meta.get("fetchedAt")
    cache["financeCachedGoodsNmIds"] = finance_meta.get("cachedGoodsNmIds")
    cache["financeMatchedNmIds"] = finance_meta.get("matchedCachedGoodsNmIds")
    cache["adsFetchedAt"] = ads_meta.get("fetchedAt")
    cache["adsCount"] = ads_meta.get("count")
    cache["adsCampaignCount"] = ads_meta.get("campaignCount")
    cache["adsDateFrom"] = ads_meta.get("dateFrom")
    cache["adsDateTo"] = ads_meta.get("dateTo")
    cache["adsSource"] = ads_meta.get("source")
    cache["adsSpendKopecks"] = ads_totals["adSpendKopecks"]
    cache["adsImpressions"] = ads_totals["adImpressions"]
    cache["adsClicks"] = ads_totals["adClicks"]
    cache["adsCartAdds"] = ads_totals["adCartAdds"]
    cache["adsOrders"] = ads_totals["adOrders"]
    cache["adsRevenueKopecks"] = ads_totals["adRevenueKopecks"]
    cache["basketsFetchedAt"] = baskets_meta.get("fetchedAt")
    cache["basketsRequestedNmIds"] = baskets_meta.get("requestedNmIds")
    cache["basketsMatchedNmIds"] = baskets_meta.get("matchedNmIds")
    return respond(all_rows, cache, missing_sources=bool(stats_fetch_meta["missingSources"]),
                   fetched_sources=stats_fetch_meta["onDemandFetchedSources"], ads_cache=ads_meta)


@router.get("/api/v1/wb-repricer/sku")
@repricer_finance_scope
def get_sku_list(
    request: Request,
    scenario: str = Query(default="complete"),
    include_promotions: bool = Query(default=False),
    include_content: bool = Query(default=False),
    period_days: int = Query(default=30, ge=1, le=90, alias="periodDays"),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=SKU_LIST_PAGE_SIZE_DEFAULT, alias="pageSize", ge=25, le=500),
    q: str = Query(default=""),
    status: str = Query(default="all"),
    brand: str = Query(default="all"),
    manager: str = Query(default="all"),
    top_mode: bool = Query(default=True, alias="topMode"),
    filters: str | None = Query(default=None, max_length=4000),
) -> dict[str, Any]:
    try:
        selection = RepricerFilters.model_validate_json(filters) if isinstance(filters, str) else RepricerFilters()
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="Invalid Repricer filters") from error
    trace = _repricer_load_trace_start("wb-repricer-sku")
    organization_id = _hydrate_org_repricer_state(request)
    matches_advanced = compile_filter(selection, saved_attribute_cards(organization_id) if selection.restricted_attrs else None)
    if manager == "mine":
        manager = str(actor_from_request(request).user_id)
    from app.wb_catalog_loading import require_goods_cache
    require_goods_cache(organization_id, cached_goods_meta(organization_id))
    wb_token = _cached_read_wb_token(request) if isinstance(request, Request) else None
    if isinstance(request, Request):
        from app.repricer_page_finance import page_connection, connection_key
        page_connection.set(connection_key(wb_token) if wb_token else None)
    _repricer_load_trace_step(trace, "hydrate_org", organizationId=organization_id)
    _range_start, _range_end, resolved_period_days, _period_suffix = _repricer_period_context(period_days, date_from, date_to)
    period_cache_memo: dict[tuple[Any, ...], dict[str, Any]] = {}
    _repricer_load_trace_step(
        trace,
        "period_context",
        periodDays=resolved_period_days,
        dateFrom=_range_start.date().isoformat(),
        dateTo=_range_end.date().isoformat(),
        periodSuffix=_period_suffix,
    )
    filters_active = bool(
        str(q or "").strip()
        or (status and status != "all")
        or (brand and brand != "all")
        or (manager and manager != "all")
        or selection.active
    )
    snapshot_page = (
        _load_repricer_sku_snapshot_page(
            organization_id,
            scenario,
            period_suffix=_period_suffix,
            include_promotions=include_promotions,
            include_content=include_content,
            page=page,
            page_size=page_size,
        )
        if top_mode and not filters_active
        else None
    )
    _repricer_load_trace_step(
        trace,
        "snapshot_page",
        attempted=bool(top_mode and not filters_active),
        hit=snapshot_page is not None,
        page=page,
        pageSize=page_size,
        filtersActive=filters_active,
        snapshotKey=snapshot_page[0].get("snapshotKey") if snapshot_page is not None else None,
        storage=snapshot_page[0].get("storage") if snapshot_page is not None else None,
        itemsReturned=len(snapshot_page[1]) if snapshot_page is not None else None,
    )
    if snapshot_page is None and top_mode and not filters_active:
        built_snapshot = _build_repricer_sku_snapshot(
            organization_id,
            scenario,
            wb_token=None,
            resolved_period_days=resolved_period_days,
            period_suffix=_period_suffix,
            range_start=_range_start,
            range_end=_range_end,
            include_promotions=include_promotions,
            include_content=include_content,
        )
        snapshot_page = _load_repricer_sku_snapshot_page(
            organization_id,
            scenario,
            period_suffix=_period_suffix,
            include_promotions=include_promotions,
            include_content=include_content,
            page=page,
            page_size=page_size,
        )
        _repricer_load_trace_step(
            trace,
            "snapshot_build",
            built=bool(built_snapshot),
            hit=snapshot_page is not None,
            total=built_snapshot.get("total"),
        )
    if snapshot_page is not None:
        snapshot, items = snapshot_page
        filtered = items
    else:
        goods_meta = cached_goods_meta(organization_id)
        _repricer_load_trace_step(
            trace,
            "goods_meta",
            totalCached=goods_meta.get("totalCached"),
            pagesCached=goods_meta.get("pagesCached"),
            latestFetchedAt=goods_meta.get("latestFetchedAt"),
        )
        if not (int(goods_meta.get("pagesCached") or 0) or int(goods_meta.get("totalCached") or 0)):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "WB_CATALOG_CACHE_REQUIRED",
                    "message": "WB catalog cache is empty. Run or resume cold WB sync to fetch goods before opening repricer products.",
                    "details": {
                        "requestedRange": _range_payload(_range_start, _range_end),
                        "cachedGoods": goods_meta,
                        "trace": _repricer_load_trace_payload(trace),
                    },
                },
            )
        snapshot = None
        list_window_limit = None if filters_active else page * page_size
        all_items = _list_repricer_skus_for_request(
            request,
            scenario,
            wb_token=wb_token,
            include_promotions=include_promotions,
            include_content=include_content,
            period_days=resolved_period_days,
            date_from=date_from,
            date_to=date_to,
            max_items=list_window_limit,
            sort_by_demand=top_mode,
            require_full_sync_coverage=False,
            period_cache_memo=period_cache_memo,
        )
        _repricer_load_trace_step(
            trace,
            "build_rows",
            rows=len(all_items),
            listWindowLimit=list_window_limit,
            topMode=top_mode,
        )
        filtered = [
            row
            for row in all_items
            if _row_matches_sku_filters(row, q=q, status=status, brand=brand, manager=manager) and matches_advanced(row)
        ]
        if top_mode:
            filtered.sort(key=_repricer_row_sort_key)
        start = (page - 1) * page_size
        items = filtered[start : start + page_size]
        _repricer_load_trace_step(
            trace,
            "filter_page",
            filteredRows=len(filtered),
            itemsReturned=len(items),
            filtersActive=filters_active,
        )
    cache = dict(snapshot.get("cache") or {}) if snapshot is not None and top_mode else _repricer_list_cache_meta(
        organization_id,
        resolved_period_days,
        include_content=include_content,
        date_from=date_from,
        date_to=date_to,
        require_full_sync_coverage=False,
        period_cache_memo=period_cache_memo,
    )
    _repricer_load_trace_step(
        trace,
        "cache_meta",
        source="snapshot" if snapshot is not None and top_mode else "db",
        totalCached=cache.get("totalCached"),
        financeFetchedAt=cache.get("financeFetchedAt"),
        basketsFetchedAt=cache.get("basketsFetchedAt"),
    )
    cache["listItemsLimit"] = page_size
    cache["listPage"] = page
    cache["serverFiltersApplied"] = True
    total_filtered = int(snapshot.get("total") or 0) if snapshot_page is not None else len(filtered)
    if not filters_active:
        total_filtered = max(total_filtered, int(cache.get("totalCached") or total_filtered))
    cache["listTotalFiltered"] = total_filtered
    cache["summaryScope"] = "filtered_skus" if filters_active else "catalog"
    if snapshot is not None and top_mode and not filters_active and isinstance(snapshot.get("summary"), dict):
        summary = snapshot["summary"]
    else:
        finance_cache = _period_source_cache(
            organization_id,
            "finance",
            _period_suffix,
            resolved_period_days,
            _range_start,
            _range_end,
            slim=True,
            require_full_sync_coverage=False,
            memo=period_cache_memo,
        )
        finance_diagnostics = _limited_finance_diagnostics(finance_cache, limit=1) if finance_cache else None
        summary = (
            _repricer_list_summary_from_source_caches(
                organization_id,
                resolved_period_days=resolved_period_days,
                period_suffix=_period_suffix,
                range_start=_range_start,
                range_end=_range_end,
                finance_diagnostics=finance_diagnostics,
                require_full_sync_coverage=False,
                period_cache_memo=period_cache_memo,
            )
            if not filters_active
            # Do not add cabinet-wide residual costs to a filtered selection.
            else _repricer_list_summary(filtered, campaign_ads=(
                filtered_campaign_advertising(filtered) if _page_finance_active() else None))
        )
        if filters_active and _page_finance_active():
            _apply_period_reporting(summary, organization_id, _range_start, _range_end,
                ledger=finance_cache, coverage=cache["periodCoverage"],
                nm_ids={str(row["meta"]["nmId"]) for row in filtered}, memo=period_cache_memo)
    _repricer_load_trace_step(
        trace,
        "summary",
        source="snapshot" if snapshot is not None and top_mode and not filters_active and isinstance(snapshot.get("summary"), dict) else "source_caches" if not filters_active else "filtered_rows",
        revenueKopecks=summary.get("revenueKopecks") if isinstance(summary, dict) else None,
        skuCount=summary.get("skuCount") if isinstance(summary, dict) else None,
    )
    trace_payload = _repricer_load_trace_payload(trace)
    if snapshot is not None:
        for row in items:
            meta = row["meta"]
            strategy = repricer_bff_module._frontend_strategy_payload(
                meta["articleId"], current_status=str(meta.get("status") or "manual"),
                baskets_last_7d=int(meta.get("basketsLast7d") or 0), basket_norm=int(meta.get("basketNorm") or 0),
            )
            row["strategy"] = strategy
            meta.update(activeStrategyId=strategy["id"], activeStrategyName=strategy["name"], activeTypedStrategyId=strategy["typedStrategyId"])
    return {
        "items": items,
        "total": total_filtered,
        "totalCached": int(cache.get("totalCached") or total_filtered),
        "itemsReturned": len(items),
        "page": page,
        "pageSize": page_size,
        "summary": summary,
        "cache": cache,
        "trace": trace_payload,
        "dateFrom": _range_start.date().isoformat(),
        "dateTo": _range_end.date().isoformat(),
        "periodDays": resolved_period_days,
    }


def _apply_nomenclature_stock_patch(
    organization_id: int,
    nm_id: int,
    article_id: str,
    stock_patch: dict[str, Any],
    *,
    imported_at: str,
) -> dict[str, Any]:
    if not stock_patch:
        return {}
    cache = get_source_cache(organization_id, "stocks", slim=False) or {}
    aggregates = cache.get("aggregates") if isinstance(cache.get("aggregates"), dict) else {}
    row = dict(aggregates.get(str(nm_id)) or {})
    changed: dict[str, Any] = {}
    if stock_patch.get("stockTotal") is not None:
        value = int(stock_patch["stockTotal"])
        row["wbStockUnits"] = value
        changed["stockTotal"] = value
    if stock_patch.get("stockFbs") is not None:
        value = int(stock_patch["stockFbs"])
        row["stockFbs"] = value
        changed["stockFbs"] = value
    if stock_patch.get("stockFbm") is not None:
        value = int(stock_patch["stockFbm"])
        row["stockFbm"] = value
        changed["stockFbm"] = value
    if not changed:
        return {}
    row.setdefault("inWayToClient", 0)
    row.setdefault("inWayFromClient", 0)
    row.setdefault("warehouses", 1)
    row["vendorCode"] = article_id
    row["source"] = "xlsx_import"
    row["importedAt"] = imported_at
    aggregates[str(nm_id)] = row
    cache["aggregates"] = aggregates
    cache["importedAt"] = imported_at
    save_source_cache(organization_id, "stocks", cache)
    return changed


class RepricerTableExportRequest(BaseModel):
    dateFrom: date
    dateTo: date
    nmIds: list[int] = Field(max_length=100_000)
    filterDescription: str = Field(default="", max_length=16_384)
    sourceSignature: list[str | None] = Field(min_length=7, max_length=7)


@router.post("/api/v1/wb-repricer/table.xlsx")
@repricer_finance_scope
def export_repricer_table(request: Request, payload: RepricerTableExportRequest) -> Response:
    from app.repricer_table_export import render_repricer_table
    actor = actor_from_request(request)
    organization_id = _hydrate_org_repricer_state(request)
    if payload.dateFrom > payload.dateTo or payload.dateTo >= datetime.now(ZoneInfo("Europe/Moscow")).date():
        raise HTTPException(status_code=422, detail="Укажите корректный период завершённых дней WB")
    _start, _end, days, _suffix = _repricer_period_context(30, payload.dateFrom, payload.dateTo)
    if len(set(payload.nmIds)) != len(payload.nmIds) or any(nm <= 0 for nm in payload.nmIds):
        raise HTTPException(status_code=422, detail="Некорректные идентификаторы товаров")
    wb_token = _cached_read_wb_token(request)
    from app.repricer_page_finance import page_connection, connection_key
    page_connection.set(connection_key(wb_token) if wb_token else None)
    cache = _repricer_list_cache_meta(organization_id, days, date_from=payload.dateFrom,
        date_to=payload.dateTo, include_content=True, require_full_sync_coverage=False)
    source_keys = ("periodStatsFetchedAt", "financeFetchedAt", "basketsFetchedAt", "latestFetchedAt", "adsFetchedAt", "stocksFetchedAt", "contentFetchedAt")
    signature = [cache.get(key) for key in source_keys]
    if signature != payload.sourceSignature:
        raise HTTPException(status_code=409, detail="Данные обновились во время экспорта. Повторите выгрузку.")
    # Re-read the authenticated organization's complete source table. The client
    # provides ordering/selection only, never financial cell values or a page cap.
    rows = _list_repricer_skus_for_request(request, "complete", actor=actor, wb_token=wb_token,
        include_promotions=True, include_content=True, period_days=days,
        date_from=payload.dateFrom, date_to=payload.dateTo, max_items=None,
        require_full_sync_coverage=False)
    current_cache = _repricer_list_cache_meta(organization_id, days, date_from=payload.dateFrom,
        date_to=payload.dateTo, include_content=True, require_full_sync_coverage=False)
    if [current_cache.get(key) for key in source_keys] != signature:
        raise HTTPException(status_code=409, detail="Данные обновились во время экспорта. Повторите выгрузку.")
    by_nm = {int(row["meta"]["nmId"]): row for row in rows if row.get("meta", {}).get("nmId")}
    if any(nm not in by_nm for nm in payload.nmIds):
        raise HTTPException(status_code=409, detail="Каталог изменился. Повторите выгрузку.")
    selected = [by_nm[nm] for nm in payload.nmIds]
    for row in selected:
        # List snapshots omit comment bodies; exports must retain full text.
        article = str(row.get("meta", {}).get("articleId") or "")
        row["commentSummary"] = repricer_bff_module._comment_summary(
            repricer_bff_module._sku_comments_for_article(article, False))
    content = render_repricer_table(selected, date_from=payload.dateFrom, date_to=payload.dateTo,
        filters=payload.filterDescription, cache={key: value for key, value in cache.items()
            if key.endswith(("FetchedAt", "CoverageState", "FreshnessState", "ConversionState")) or key == "periodCoverage"})
    filename = f"wb-repricer-{payload.dateFrom}-{payload.dateTo}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/v1/wb-repricer/sku/export-xlsx")
def export_sku_nomenclature_xlsx(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90, alias="periodDays"),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
) -> Response:
    actor = actor_from_request(request)
    _hydrate_org_repricer_state(request)
    sku_rows = _list_repricer_skus_for_request(
        request,
        scenario,
        actor=actor,
        wb_token=_cached_read_wb_token(request),
        include_promotions=True,
        include_content=True,
        period_days=period_days,
        date_from=date_from,
        date_to=date_to,
        max_items=None,
    )
    content = build_repricer_nomenclature_xlsx(sku_rows)
    filename = f"REPRICER_WB_NOMENCLATURE_{date.today().isoformat()}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/v1/wb-repricer/sku/import-xlsx")
async def import_sku_nomenclature_xlsx(
    request: Request,
    scenario: str = Query(default="complete"),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    _ensure_import_permission(actor)
    organization_id = _hydrate_org_repricer_state(request)
    files = await _extract_uploaded_files(request)
    if not files:
        raise HTTPException(status_code=422, detail="REPRICER_NOMENCLATURE_FILE_REQUIRED")
    filename, content = files[0]
    parsed = parse_repricer_nomenclature_excel(content, filename)
    if parsed.status == "error":
        raise HTTPException(status_code=422, detail=parsed.error_text or "REPRICER_NOMENCLATURE_PARSE_ERROR")

    wb_token = _request_wb_token(request)
    sku_rows = _list_repricer_skus_for_request(request, scenario, actor=actor, wb_token=wb_token, max_items=None)
    by_article = {
        str(row.get("meta", {}).get("articleId") or "").strip(): row
        for row in sku_rows
        if str(row.get("meta", {}).get("articleId") or "").strip()
    }
    by_nm_id = {
        int(row.get("meta", {}).get("nmId")): str(row.get("meta", {}).get("articleId") or "").strip()
        for row in sku_rows
        if row.get("meta", {}).get("nmId") is not None
    }
    applied: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    economics_updates: dict[str, dict[str, Any]] = {}
    imported_at = datetime.now(timezone.utc).isoformat()

    for item in parsed.items:
        article_id = str(item.get("articleId") or "").strip()
        nm_id = _positive_int_or_none(item.get("nmId"))
        original_article_id = article_id
        if nm_id is not None and (not article_id or article_id not in by_article):
            article_id = by_nm_id.get(nm_id, article_id)
        if not article_id or article_id not in by_article:
            unmatched.append(item)
            continue

        current_row = by_article[article_id]
        current_settings = dict(current_row.get("settings") or {})
        next_settings = dict(current_settings)
        settings_changed: dict[str, Any] = {}
        settings_patch = item.get("settingsPatch") if isinstance(item.get("settingsPatch"), dict) else {}
        if not settings_patch:
            settings_patch = {
                key: item.get(key)
                for key in ("cogsKopecks", "pMinKopecks", "pMaxKopecks")
                if item.get(key) is not None
            }
        for target_key, value in settings_patch.items():
            if value is not None:
                next_settings[target_key] = value
                settings_changed[target_key] = value

        if settings_changed:
            repricer_bff_module.SKU_SETTINGS_OVERRIDES[article_id] = next_settings
            if _SKU_ECONOMICS_FIELDS.intersection(settings_changed):
                economics_updates[article_id] = next_settings
            repricer_bff_module.SKU_AUDIT_EVENTS.setdefault(article_id, []).append(
                {
                    "id": f"audit-{article_id}-nomenclature-import-{len(repricer_bff_module.SKU_AUDIT_EVENTS.get(article_id, [])) + 1}",
                    "sku": article_id,
                    "createdAt": imported_at,
                    "actor": {"id": actor.user_id, "name": actor.email or "User", "role": actor.permission_profile},
                    "source": "xlsx",
                    "scope": "sku",
                    "action": "Импорт настроек номенклатуры",
                    "reason": filename,
                    "oldValue": {key: current_settings.get(key) for key in settings_changed},
                    "newValue": settings_changed,
                }
            )

        stock_changed: dict[str, Any] = {}
        stock_patch = item.get("stockPatch") if isinstance(item.get("stockPatch"), dict) else {}
        resolved_nm_id = nm_id
        if resolved_nm_id is None:
            row_nm_id = current_row.get("meta", {}).get("nmId")
            resolved_nm_id = _positive_int_or_none(row_nm_id)
        if stock_patch and resolved_nm_id is not None:
            stock_changed = _apply_nomenclature_stock_patch(
                organization_id,
                int(resolved_nm_id),
                article_id,
                stock_patch,
                imported_at=imported_at,
            )

        strategy_id = item.get("strategyId")
        strategy_result: dict[str, Any] | None = None
        if strategy_id is not None:
            try:
                normalized_strategy = str(strategy_id).strip().lower()
                if normalized_strategy in {"none", "no_strategy", "null", "нет", "не задана", "без стратегии"}:
                    strategy_result = unassign_frontend_strategy_assignment(
                        [article_id],
                        scenario=scenario,
                        wb_token=wb_token,
                        source="xlsx",
                        sku_rows=sku_rows,
                    )
                else:
                    strategy_result = apply_frontend_strategy_assignment(
                        [article_id],
                        strategy_id_or_name=strategy_id,
                        scenario=scenario,
                        wb_token=wb_token,
                        source="xlsx",
                        sku_rows=sku_rows,
                    )
            except HTTPException as exc:
                errors.append({"articleId": article_id, "nmId": nm_id, "strategyId": strategy_id, "error": exc.detail})

        if settings_changed or stock_changed or strategy_result is not None:
            applied.append(
                {
                    "articleId": article_id,
                    "originalArticleId": original_article_id if original_article_id != article_id else None,
                    "nmId": nm_id,
                    "strategyId": strategy_id,
                    "settings": settings_changed,
                    "stock": stock_changed,
                    "strategyApplied": strategy_result is not None,
                }
            )

    saved = _flush_org_repricer_state(organization_id)
    if economics_updates:
        if not saved:
            raise HTTPException(status_code=500, detail="SKU_SETTINGS_NOT_SAVED")
        effective_from = _parse_utc_datetime(imported_at)
        if effective_from is None:
            raise HTTPException(status_code=500, detail="SKU_SETTINGS_TIMESTAMP_MISSING")
        economics_service = EconomicsService(session, organization_id)
        unmapped: list[str] = []
        for article_id, settings in economics_updates.items():
            try:
                economics_service.reconcile_legacy_sku_override(
                    article_id,
                    settings,
                    effective_from=effective_from,
                    source="legacy_nomenclature_import",
                    source_reference=_economics_source_reference(
                        "nomenclature",
                        parsed.file_hash,
                        article_id,
                        effective_from.isoformat(),
                    ),
                )
            except EconomicsNotFoundError:
                unmapped.append(article_id)
            except EconomicsValidationError as exc:
                _raise_invalid_economics(exc)
        if unmapped:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ECONOMICS_SKU_MAPPING_MISSING",
                    "message": "Canonical catalog mapping is missing",
                    "articleIds": unmapped,
                },
            )
    history_entry = {
        "filename": filename,
        "fileHash": parsed.file_hash,
        "rowsTotal": parsed.rows_total,
        "rowsParsed": parsed.rows_parsed,
        "appliedCount": len(applied),
        "unmatchedCount": len(unmatched),
        "errorCount": len(errors),
        "importedAt": imported_at,
    }
    _append_import_history(organization_id, "repricer_nomenclature", history_entry)
    save_source_cache(
        organization_id,
        "repricer_nomenclature_excel",
        {"meta": history_entry, "items": parsed.items, "applied": applied, "unmatched": unmatched, "errors": errors},
    )
    return {
        "source": "repricer_nomenclature",
        **history_entry,
        "applied": applied[:50],
        "unmatched": unmatched[:50],
        "errors": errors[:50],
    }


def _parse_sync_finished_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _last_sync_finished_by_profile(history: list[dict[str, Any]]) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    for event in history:
        if event.get("type") != "sync_finished":
            continue
        profile_id = str(event.get("syncProfile") or "")
        if not profile_id:
            continue
        finished_at = _parse_sync_finished_at(event.get("finishedAt"))
        if finished_at is None:
            continue
        current = result.get(profile_id)
        if current is None or finished_at > current:
            result[profile_id] = finished_at
    return result


def _sync_plan_status_item(
    profile: WbSyncProfile,
    *,
    organization_id: int,
    group: str,
    current_status: dict[str, Any],
    last_finished: dict[str, datetime],
    now: datetime,
) -> dict[str, Any]:
    running = bool(current_status.get("running")) and current_status.get("syncProfile") == profile.profile_id
    queued = (
        current_status.get("state") == "queued"
        and current_status.get("syncProfile") == "onboarding-full"
        and group == "onboarding"
    )
    last_run_at = last_finished.get(profile.profile_id)
    source_readiness = _profile_source_readiness(organization_id, profile)
    cache_ready = bool(source_readiness) and all(item.get("ready") for item in source_readiness)
    any_source_ready = any(item.get("ready") for item in source_readiness)
    next_run_at = last_run_at + timedelta(minutes=profile.cadence_minutes) if last_run_at and profile.cadence_minutes else None
    if group == "nightly" and profile.cadence_minutes:
        next_run_at = next_nightly_sync_at(now, not_before=next_run_at)
    due = bool(profile.cadence_minutes and (next_run_at is None or next_run_at <= now))
    if running:
        state = "running"
    elif queued:
        state = "queued"
    elif profile.cadence_minutes:
        state = "due" if due else "scheduled"
    elif cache_ready:
        state = "completed"
    elif last_run_at or any_source_ready:
        state = "partial"
    else:
        state = "pending"
    item = profile.as_status_meta()
    item.update(
        {
            "group": group,
            "lastRunAt": last_run_at.isoformat() if last_run_at else None,
            "nextRunAt": next_run_at.isoformat() if next_run_at else None,
            "due": due,
            "dueInSeconds": max(0, int((next_run_at - now).total_seconds())) if next_run_at else None,
            "running": running,
            "cacheReady": cache_ready,
            "sourceReadiness": source_readiness,
            "state": state,
        }
    )
    return item


def _build_sync_plan_status(organization_id: int, status: dict[str, Any], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    historical_as_of = historical_sync_as_of(now)
    periodic_as_of = now.date()
    last_finished = _last_sync_finished_by_profile(history)
    result: list[dict[str, Any]] = []
    for profile in onboarding_sync_profiles(as_of=historical_as_of):
        result.append(_sync_plan_status_item(profile, organization_id=organization_id, group="onboarding", current_status=status, last_finished=last_finished, now=now))
    for profile in periodic_sync_profiles(as_of=periodic_as_of):
        result.append(_sync_plan_status_item(profile, organization_id=organization_id, group="periodic", current_status=status, last_finished=last_finished, now=now))
    result.append(
        _sync_plan_status_item(
            nightly_reconciliation_profile(as_of=historical_as_of),
            organization_id=organization_id,
            group="nightly",
            current_status=status,
            last_finished=last_finished,
            now=now,
        )
    )
    return result


def _repricer_sync_status_payload(actor: Any) -> dict[str, Any]:
    status = get_wb_sync_status(actor.organization_id)
    history = list_wb_sync_history(actor.organization_id, hours=24 * 8, limit=200)
    goods_meta = cached_goods_meta(actor.organization_id)
    user_token = get_user_wb_token_secret(actor.user_id)
    org_token = get_organization_wb_token_secret(actor.organization_id)
    return {
        **status,
        "organizationId": actor.organization_id,
        "actorUserId": actor.user_id,
        "diagnostics": {
            "userTokenPresent": bool(user_token),
            "organizationTokenPresent": bool(org_token),
            "cachedGoodsCount": goods_meta.get("totalCached") or 0,
            "cachedGoodsFetchedAt": goods_meta.get("latestFetchedAt"),
        },
        "syncPlan": _build_sync_plan_status(actor.organization_id, status, history),
    }


def _update_wb_sync_step(
    organization_id: int,
    source: str,
    patch: dict[str, Any],
    *,
    status_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = get_source_cache(organization_id, "wb_sync_status", slim=False) or {}
    steps = status.get("steps") if isinstance(status.get("steps"), list) else []
    next_steps: list[dict[str, Any]] = []
    found = False
    for item in steps:
        if not isinstance(item, dict):
            continue
        if item.get("source") == source:
            found = True
            next_steps.append({**item, **patch})
        else:
            next_steps.append(item)
    if not found:
        next_steps.append({"source": source, **patch})
    status.update({**(status_patch or {}), "steps": next_steps, "updatedAt": datetime.now(timezone.utc).isoformat()})
    save_source_cache(organization_id, "wb_sync_status", status)
    return status


def _retry_failed_wb_sync_step(
    organization_id: int,
    source: str,
    scenario: str,
    wb_token: str | None,
    *,
    period_days: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    baskets_daily_detail: bool = False,
) -> None:
    try:
        if source == "stocks":
            aggregates = fetch_stock_aggregates(scenario, wb_token=wb_token)
            save_source_cache(organization_id, "stocks", {"aggregates": aggregates, "count": len(aggregates)})
            _update_wb_sync_step(
                organization_id,
                source,
                {
                    "status": "ok",
                    "count": len(aggregates),
                    "error": None,
                    "phase": "completed",
                    "progressPercent": 100,
                    "message": "Этап завершён",
                    "finishedAt": datetime.now(timezone.utc).isoformat(),
                },
            )
        elif source == "baskets":
            def report_progress(step: dict[str, Any]) -> None:
                _update_wb_sync_step(
                    organization_id,
                    source,
                    {
                        **step,
                        "status": "running",
                        "error": None,
                        "message": step.get("message") or "Повторяем корзины",
                    },
                    status_patch={"state": "running", "running": True, "currentSource": source},
                )

            result = refresh_wb_data_sources(
                organization_id=organization_id,
                wb_token=wb_token,
                scenario=scenario,
                period_days=period_days or 30,
                date_from=date_from,
                date_to=date_to,
                trigger="retry-step",
                force=True,
                execute_lock=False,
                sources=["baskets"],
                baskets_include_daily_detail=baskets_daily_detail,
                _progress_callback=report_progress,
            )
            step = next((item for item in result.get("steps", []) if isinstance(item, dict) and item.get("source") == source), {})
            _update_wb_sync_step(
                organization_id,
                source,
                {
                    **step,
                    "status": step.get("status") or ("ok" if result.get("state") == "completed" else "error"),
                    "phase": step.get("phase") or ("completed" if result.get("state") == "completed" else "failed"),
                    "progressPercent": step.get("progressPercent") or 100,
                    "finishedAt": datetime.now(timezone.utc).isoformat(),
                },
            )
    except Exception as exc:
        _update_wb_sync_step(
            organization_id,
            source,
            {
                "status": "error",
                "error": _http_exception_message(exc) if isinstance(exc, HTTPException) else str(exc),
                "phase": "failed",
                "progressPercent": 100,
                "message": _http_exception_message(exc) if isinstance(exc, HTTPException) else str(exc),
                "finishedAt": datetime.now(timezone.utc).isoformat(),
            },
        )
    finally:
        status = get_source_cache(organization_id, "wb_sync_status", slim=False) or {}
        steps = [item for item in (status.get("steps") if isinstance(status.get("steps"), list) else []) if isinstance(item, dict)]
        has_running = any(item.get("status") == "running" for item in steps)
        has_error = any(item.get("status") in {"error", "partial"} for item in steps)
        status.update(
            {
                "running": has_running,
                "state": "running" if has_running else "partial" if has_error else "completed",
                "currentSource": next((item.get("source") for item in steps if item.get("status") == "running"), None),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
        if not has_running:
            status["finishedAt"] = datetime.now(timezone.utc).isoformat()
        save_source_cache(organization_id, "wb_sync_status", status)


@router.get("/api/v1/wb-repricer/sync/status")
def get_repricer_sync_status(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    return _repricer_sync_status_payload(actor)


@router.post("/api/v1/wb-repricer/sync/retry-step")
def post_repricer_sync_retry_step(request: Request, payload: RepricerSyncRetryStepRequest) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    source = payload.source.strip()
    if source not in {"stocks", "baskets"}:
        raise HTTPException(status_code=400, detail={"code": "unsupported_sync_step", "message": f"Этап {source or 'unknown'} пока нельзя повторить"})
    status = get_wb_sync_status(actor.organization_id)
    steps = status.get("steps") if isinstance(status.get("steps"), list) else []
    step = next((item for item in steps if isinstance(item, dict) and item.get("source") == source), None)
    if step is not None and step.get("status") == "running":
        return _repricer_sync_status_payload(actor)
    if step is None or step.get("status") != "error":
        raise HTTPException(status_code=409, detail={"code": "sync_step_not_failed", "message": "Этап можно повторить только после ошибки"})
    _update_wb_sync_step(
        actor.organization_id,
        source,
        {
            "status": "running",
            "error": None,
            "phase": "retrying",
            "progressPercent": 5,
            "progressCurrent": 0,
            "progressTotal": None,
            "message": "Повторяем этап",
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "finishedAt": None,
        },
        status_patch={"state": "running", "running": True, "currentSource": source},
    )
    retry_date_from = date.fromisoformat(str(status["dateFrom"])) if status.get("dateFrom") else None
    retry_date_to = date.fromisoformat(str(status["dateTo"])) if status.get("dateTo") else None
    threading.Thread(
        target=_retry_failed_wb_sync_step,
        kwargs={
            "organization_id": actor.organization_id,
            "source": source,
            "scenario": payload.scenario,
            "wb_token": wb_token,
            "period_days": int(status.get("periodDays") or 30),
            "date_from": retry_date_from,
            "date_to": retry_date_to,
            "baskets_daily_detail": bool(status.get("basketsDailyDetail") or source == "baskets"),
        },
        name=f"wb-sync-retry-{source}-{actor.organization_id}",
        daemon=True,
    ).start()
    return _repricer_sync_status_payload(actor)


@router.get("/api/v1/wb-repricer/report-snapshots/status")
def get_repricer_report_snapshots_status(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    status = get_source_cache(actor.organization_id, "reports_payload_materialization_status", slim=False) or {}
    if not isinstance(status, dict) or not status:
        return {"state": "idle", "running": False, "processed": 0, "total": 0, "percent": 0}
    if status.get("running"):
        observed_at = _parse_utc_datetime(status.get("updatedAt") or status.get("fetchedAt") or status.get("startedAt") or status.get("queuedAt"))
        stale_after = timedelta(minutes=3) if status.get("state") in {"queued", "starting"} else timedelta(hours=2)
        if observed_at and datetime.now(timezone.utc) - observed_at > stale_after:
            return {
                **status,
                "state": "stale",
                "running": False,
                "label": "Сборка снапшотов не стартовала",
                "detail": "Запустите снова после синхронизации WB cache",
            }
    return status


@router.post("/api/v1/wb-repricer/report-snapshots/materialize")
def post_repricer_report_snapshots_materialize(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    current = get_source_cache(actor.organization_id, "reports_payload_materialization_status", slim=False) or {}
    if isinstance(current, dict) and current.get("running"):
        observed_at = _parse_utc_datetime(current.get("updatedAt") or current.get("fetchedAt") or current.get("startedAt") or current.get("queuedAt"))
        stale_after = timedelta(minutes=3) if current.get("state") == "queued" else timedelta(hours=2)
        if observed_at and datetime.now(timezone.utc) - observed_at < stale_after:
            return {**current, "state": current.get("state") or "running", "running": True}
    from app.repricer_tasks import _available_report_snapshot_windows, run_report_snapshots_materialization

    total = len(_available_report_snapshot_windows(actor.organization_id))
    task_id = f"api-bg-report-snapshots-{uuid4()}"
    queued = save_source_cache(
        actor.organization_id,
        "reports_payload_materialization_status",
        {
            "state": "running",
            "running": True,
            "taskId": task_id,
            "processed": 0,
            "total": total,
            "percent": 0,
            "stage": "starting",
            "label": "Запускаем прогрев снапшотов отчетов",
            "detail": "API стартует фоновую сборку без WB API",
            "startedAt": datetime.now(timezone.utc).isoformat(),
        },
    )
    thread = threading.Thread(
        target=run_report_snapshots_materialization,
        kwargs={"organization_id": actor.organization_id, "task_id": task_id},
        name=f"report-snapshots-{actor.organization_id}",
        daemon=True,
    )
    thread.start()
    return queued


@router.post("/api/v1/wb-repricer/sync/run")
def post_repricer_sync_run(request: Request, payload: RepricerSyncRunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    if payload.mode == "onboarding":
        current_status = get_wb_sync_status(actor.organization_id)
        if current_status.get("stale") or current_status.get("state") == "stale":
            abandon_stale_wb_sync(actor.organization_id, reason="manual_onboarding_replaced_stale", status=current_status)
        elif is_wb_sync_running(actor.organization_id):
            abandon_stale_wb_sync(actor.organization_id, reason="manual_onboarding_replaced_running_sync", status=current_status)
        from app.repricer_tasks import run_wb_onboarding_for_org

        task_id = f"api-bg-onboarding-{uuid4()}"
        queue_wb_sync(
            actor.organization_id,
            trigger="manual-onboarding",
            task_id=task_id,
            sync_profile="onboarding-full",
            sync_profile_label="Полная холодная загрузка",
            window_kind="onboarding",
            sources=["goods", "content", "promotions", "stocks", "period-stats", "finance", "ads", "baskets"],
        )
        threading.Thread(
            target=run_wb_onboarding_for_org,
            kwargs={"organization_id": actor.organization_id, "scenario": payload.scenario, "wb_token_override": wb_token},
            name=f"wb-onboarding-{actor.organization_id}",
            daemon=True,
        ).start()
        return _repricer_sync_status_payload(actor)
    hydrate_repricer_bff_state(actor.organization_id, repricer_bff_module)
    token_fingerprint = wb_token_fingerprint(wb_token)
    _range_start, _range_end, resolved_period_days, _period_suffix = _repricer_period_context(payload.periodDays, payload.dateFrom, payload.dateTo)
    try:
        status = begin_wb_sync(
            actor.organization_id,
            trigger="manual",
            period_days=resolved_period_days,
            date_from=payload.dateFrom,
            date_to=payload.dateTo,
            force=payload.force,
            sources=payload.sources,
            token_fingerprint=token_fingerprint,
            sync_profile="manual-window",
            sync_profile_label="Ручное окно",
            window_kind="manual",
            baskets_daily_detail=False,
        )
        background_tasks.add_task(
            _refresh_wb_data_sources_and_flush,
            organization_id=actor.organization_id,
            wb_token=wb_token,
            scenario=payload.scenario,
            period_days=resolved_period_days,
            date_from=payload.dateFrom,
            date_to=payload.dateTo,
            trigger="manual",
            force=True,
            sources=payload.sources,
            initial_status=status,
            token_fingerprint=token_fingerprint,
            sync_profile="manual-window",
            sync_profile_label="Ручное окно",
            window_kind="manual",
            baskets_include_daily_detail=False,
        )
        return status
    except WbSyncAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail="WB_SYNC_RUNNING") from exc


@router.post("/api/v1/wb-repricer/sku/refresh")
def refresh_sku_list_page(
    request: Request,
    scenario: str = Query(default="complete"),
    limit: int = Query(default=1000, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    all: bool = Query(default=True),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    _ensure_wb_sync_not_running(actor.organization_id)
    repricer_bff_module.fetch_commission_tariffs(scenario, wb_token=wb_token, force=True, organization_id=actor.organization_id)
    previous_goods = list_cached_goods(actor.organization_id)
    use_browser_prices = browser_prices_selected(actor.organization_id)
    total_saved = 0
    external_spp_matched_count = 0
    external_spp_rate_limit: ExternalSppRateLimited | None = None
    wb_sync_price_change_count = 0
    next_offset = offset
    cache: dict[str, Any] = {}
    pages: list[dict[str, Any]] = []
    seen_nm_ids: set[int] = set()
    while True:
        page = fetch_catalog_goods_page(
            scenario, wb_token=wb_token, limit=limit, offset=next_offset,
            enrich_spp=not use_browser_prices,
        )
        goods = page.get("goods")
        repricer_bff_module.validate_catalog_goods_page(goods, limit=limit, seen=seen_nm_ids)
        try:
            external_spp_prices = (
                fetch_external_spp_prices(_unique_nm_ids_from_goods(goods))
                if external_spp_rate_limit is None and not use_browser_prices else {}
            )
            external_spp_matched_count += _apply_external_spp_prices_to_goods(goods, external_spp_prices)
        except ExternalSppRateLimited as exc:
            external_spp_rate_limit = exc
            external_spp_matched_count += _apply_external_spp_prices_to_goods(goods, exc.prices)
        except Exception as exc:
            logger.warning("External SPP price fetch failed during manual goods refresh org=%s offset=%s: %s", actor.organization_id, next_offset, exc)
        wb_sync_price_change_count += record_wb_sync_price_change_events(
            previous_goods,
            goods,
            organization_id=actor.organization_id,
            run_id=f"manual-goods-{datetime.now(timezone.utc).isoformat()}-{next_offset}",
        )
        page_saved = len(goods)
        total_saved += page_saved
        pages.append({**page, "goods": goods, "offset": next_offset, "limit": limit})
        if not all or page_saved < limit or page_saved == 0:
            break
        next_offset += limit
    if all and offset == 0:
        cache = replace_goods_pages(organization_id=actor.organization_id, pages=pages)
    else:
        # A single page/continuation must never clear the other cached pages.
        for page in pages:
            cache = save_goods_page(
                organization_id=actor.organization_id, page_offset=page["offset"],
                page_limit=limit, goods=page["goods"], wb_request_id=page.get("wbRequestId"),
            )
    return {
        "items": [],
        "total": int(cache.get("totalCached") or 0),
        "pageSaved": total_saved,
        "offset": offset,
        "limit": limit,
        "all": all,
        "cache": cache,
        "externalSppMatchedCount": external_spp_matched_count,
        "wbSyncPriceChangeCount": wb_sync_price_change_count,
        **({"partial": True, "externalSppStatus": "rate_limited", "externalSppHttpStatus": 429}
           if external_spp_rate_limit is not None else {}),
    }


@router.post("/api/v1/wb-repricer/sku/refresh-content")
def refresh_sku_content(
    request: Request,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    _ensure_wb_sync_not_running(actor.organization_id)
    cards = _fetch_content_cards(scenario, wb_token=wb_token)
    payload = save_source_cache(actor.organization_id, "content_cards", {"cards": cards, "count": len(cards)})
    return {"source": "content_cards", "count": len(cards), "cache": payload}


@router.post("/api/v1/wb-repricer/sku/refresh-promotions")
def refresh_sku_promotions(
    request: Request,
    scenario: str = Query(default="complete"),
    only_if_stale: bool = Query(default=False, alias="onlyIfStale"),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    if only_if_stale is True:
        cached = get_source_cache(actor.organization_id, "promotions", slim=True) or {}
        if not promotion_refresh_metadata(cached)["promotionsRefreshRequired"]:
            return {"source": "promotions", "count": len(cached["promotions"]), "cache": cached, "cached": True}
    _ensure_wb_sync_not_running(actor.organization_id)
    upstream_error: HTTPException | None = None
    try:
        promotions = list_promotions(scenario, wb_token=wb_token, fail_open=False)
    except HTTPException as exc:
        upstream_error = exc
        promotions = list_promotions(scenario, wb_token=wb_token, fail_open=True)
        if not promotions:
            cached = get_source_cache(actor.organization_id, "promotions", slim=True) or {}
            cached_promotions = cached.get("promotions")
            if exc.status_code in {429, 502, 503, 504} and isinstance(cached_promotions, list) and cached_promotions:
                return {
                    "source": "promotions",
                    "count": len(cached_promotions),
                    "cache": cached,
                    "partial": True,
                    "warning": _http_exception_message(exc),
                }
            raise
    thresholds_cache = _promotion_thresholds_cache(actor.organization_id)
    promotions = _apply_promotion_threshold_cache(promotions, thresholds_cache)
    promotions = _promotions_for_cache(promotions)
    payload = save_source_cache(actor.organization_id, "promotions", {"promotions": promotions, "count": len(promotions)}, strict=True)
    response: dict[str, Any] = {"source": "promotions", "count": len(promotions), "cache": payload}
    if upstream_error is not None:
        response["partial"] = True
        response["warning"] = _http_exception_message(upstream_error)
    return response


@router.post("/api/v1/wb-repricer/sku/refresh-stocks")
def refresh_sku_stocks(
    request: Request,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    _ensure_wb_sync_not_running(actor.organization_id)
    aggregates = fetch_stock_aggregates(scenario, wb_token=wb_token)
    payload = save_source_cache(actor.organization_id, "stocks", {"aggregates": aggregates, "count": len(aggregates)})
    return {"source": "stocks", "count": len(aggregates), "cache": payload}


@router.post("/api/v1/wb-repricer/imports/costs-excel")
async def import_costs_excel(
    request: Request,
    mode: ImportMode = Query(default="replace"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    _ensure_import_permission(actor)
    organization_id = _hydrate_org_repricer_state(request)
    files = await _extract_uploaded_files(request)
    if not files:
        raise HTTPException(status_code=422, detail="COST_EXCEL_FILE_REQUIRED")
    filename, content = files[0]
    parsed = parse_cost_excel(content, filename)
    if parsed.status == "error":
        raise HTTPException(status_code=422, detail=parsed.error_text or "COST_EXCEL_PARSE_ERROR")

    by_nm_id, _by_vendor_code = _goods_lookup(organization_id)
    applied: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for item in parsed.items:
        nm_id = item.get("nmId")
        vendor_code = str(item.get("vendorCode") or "").strip()
        article_id = by_nm_id.get(int(nm_id)) if isinstance(nm_id, int) else None
        article_id = article_id or vendor_code
        if not article_id:
            unmatched.append(item)
            continue

        current = repricer_bff_module._sku_cost_settings(article_id, use_demo_data=False)
        next_settings = dict(current)
        if item.get("cogsKopecks") is not None:
            imported_cogs = int(item["cogsKopecks"])
            next_settings["cogsKopecks"] = int(current.get("cogsKopecks") or 0) + imported_cogs if mode == "add" else imported_cogs
        if item.get("pMinKopecks") is not None:
            next_settings["pMinKopecks"] = int(item["pMinKopecks"])
        if item.get("pMaxKopecks") is not None:
            next_settings["pMaxKopecks"] = int(item["pMaxKopecks"])
        repricer_bff_module.SKU_SETTINGS_OVERRIDES[article_id] = next_settings
        repricer_bff_module.SKU_AUDIT_EVENTS.setdefault(article_id, []).append(
            {
                "id": f"audit-{article_id}-cost-import-{len(repricer_bff_module.SKU_AUDIT_EVENTS.get(article_id, [])) + 1}",
                "sku": article_id,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "actor": {"id": actor.user_id, "name": actor.email or "User", "role": actor.permission_profile},
                "source": "excel",
                "scope": "sku",
                "action": "Импорт себестоимости",
                "reason": f"{filename} · {mode}",
                "oldValue": current.get("cogsKopecks"),
                "newValue": next_settings.get("cogsKopecks"),
            }
        )
        applied.append(
            {
                "articleId": article_id,
                "nmId": nm_id,
                "vendorCode": vendor_code or article_id,
                "cogsKopecks": next_settings.get("cogsKopecks"),
                "pMinKopecks": next_settings.get("pMinKopecks"),
                "pMaxKopecks": next_settings.get("pMaxKopecks"),
            }
        )

    _flush_org_repricer_state(organization_id)
    meta = _excel_import_meta(filename, parsed.file_hash, mode, parsed.rows_total, parsed.rows_parsed)
    history_entry = {**meta, "appliedCount": len(applied), "unmatchedCount": len(unmatched)}
    _append_import_history(organization_id, "costs", history_entry)
    save_source_cache(
        organization_id,
        "costs_excel",
        {"meta": history_entry, "items": parsed.items, "applied": applied, "unmatched": unmatched},
    )
    return {
        "source": "costs",
        **history_entry,
        "applied": applied[:50],
        "unmatched": unmatched[:50],
    }


@router.post("/api/v1/wb-repricer/imports/stocks-excel")
async def import_stocks_excel(
    request: Request,
    mode: ImportMode = Query(default="replace"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    _ensure_import_permission(actor)
    organization_id = _hydrate_org_repricer_state(request)
    files = await _extract_uploaded_files(request)
    if not files:
        raise HTTPException(status_code=422, detail="STOCK_EXCEL_FILE_REQUIRED")
    filename, content = files[0]
    parsed = parse_stock_excel(content, filename)
    if parsed.status == "error":
        raise HTTPException(status_code=422, detail=parsed.error_text or "STOCK_EXCEL_PARSE_ERROR")

    _by_nm_id, by_vendor_code = _goods_lookup(organization_id)
    current_cache = get_source_cache(organization_id, "stocks", slim=False) or {}
    current_aggregates = current_cache.get("aggregates") if isinstance(current_cache.get("aggregates"), dict) else {}
    aggregates: dict[str, dict[str, Any]] = {} if mode == "replace" else dict(current_aggregates)
    applied: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for item in parsed.items:
        vendor_code = str(item.get("vendorCode") or "").strip()
        nm_id = by_vendor_code.get(vendor_code.lower())
        if nm_id is None:
            unmatched.append(item)
            continue
        key = str(nm_id)
        current = dict(aggregates.get(key) or {})
        if mode == "add":
            next_row = {
                "wbStockUnits": int(current.get("wbStockUnits") or 0) + int(item.get("stockTotal") or 0),
                "inWayToClient": int(current.get("inWayToClient") or 0) + int(item.get("inWayToClient") or 0),
                "inWayFromClient": int(current.get("inWayFromClient") or 0) + int(item.get("inWayFromClient") or 0),
                "warehouses": max(int(current.get("warehouses") or 0), int(item.get("warehouses") or 0)),
            }
        else:
            next_row = {
                "wbStockUnits": int(item.get("stockTotal") or 0),
                "inWayToClient": int(item.get("inWayToClient") or 0),
                "inWayFromClient": int(item.get("inWayFromClient") or 0),
                "warehouses": int(item.get("warehouses") or 0),
            }
        next_row["source"] = "excel_import"
        next_row["vendorCode"] = vendor_code
        next_row["importedAt"] = datetime.now(timezone.utc).isoformat()
        aggregates[key] = next_row
        applied.append({"nmId": nm_id, **next_row})

    meta = _excel_import_meta(filename, parsed.file_hash, mode, parsed.rows_total, parsed.rows_parsed)
    history_entry = {**meta, "appliedCount": len(applied), "unmatchedCount": len(unmatched)}
    cache_payload = save_source_cache(
        organization_id,
        "stocks",
        {"aggregates": aggregates, "count": len(aggregates), "lastExcelImport": history_entry},
    )
    _append_import_history(organization_id, "stocks", history_entry)
    save_source_cache(
        organization_id,
        "stocks_excel",
        {"meta": history_entry, "items": parsed.items, "applied": applied, "unmatched": unmatched},
    )
    return {
        "source": "stocks",
        **history_entry,
        "cache": cache_payload,
        "applied": applied[:50],
        "unmatched": unmatched[:50],
    }


@router.post("/api/v1/wb-repricer/sku/refresh-period-stats")
def refresh_sku_period_stats(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    _ensure_wb_sync_not_running(actor.organization_id)
    range_start, range_end, resolved_period_days, period_suffix = _repricer_period_context(period_days, date_from, date_to)
    full_sync_covered, full_sync_range = _full_sync_covers_range(actor.organization_id, range_start, range_end)
    if not full_sync_covered:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WB_FULL_SYNC_REQUIRED",
                "message": "Selected range is outside the last full WB sync range. Run full WB sync for this date range before refreshing period stats.",
                "details": {
                    "requestedRange": _range_payload(range_start, range_end),
                    "syncRange": full_sync_range,
                },
            },
        )
    period_stats_payload = fetch_period_stats_aggregates(scenario, wb_token=wb_token, date_from=range_start, date_to=range_end)
    aggregates = period_stats_payload.get("aggregates") if isinstance(period_stats_payload.get("aggregates"), dict) else period_stats_payload
    payload = save_source_cache(
        actor.organization_id,
        f"period_stats_{period_suffix}",
        {
            **(period_stats_payload if isinstance(period_stats_payload, dict) and "aggregates" in period_stats_payload else {}),
            "aggregates": aggregates,
            "count": len(aggregates),
            "periodDays": resolved_period_days,
            "dateFrom": range_start.date().isoformat(),
            "dateTo": range_end.date().isoformat(),
        },
    )
    return {
        "source": "period_stats",
        "count": len(aggregates),
        "periodDays": resolved_period_days,
        "dateFrom": range_start.date().isoformat(),
        "dateTo": range_end.date().isoformat(),
        "cache": payload,
    }


@router.post("/api/v1/wb-repricer/sku/refresh-finance")
def refresh_sku_finance(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    _ensure_wb_sync_not_running(actor.organization_id)
    range_start, range_end, resolved_period_days, period_suffix = _repricer_period_context(period_days, date_from, date_to)
    full_sync_covered, full_sync_range = _full_sync_covers_range(actor.organization_id, range_start, range_end)
    if not full_sync_covered:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WB_FULL_SYNC_REQUIRED",
                "message": "Selected range is outside the last full WB sync range. Run full WB sync for this date range before refreshing finance.",
                "details": {
                    "requestedRange": _range_payload(range_start, range_end),
                    "syncRange": full_sync_range,
                },
            },
        )
    payload, cached, cached_goods_nm_ids_count, matched_cached_goods_nm_ids = _save_finance_source_cache(
        actor.organization_id,
        scenario=scenario,
        wb_token=wb_token,
        range_start=range_start,
        range_end=range_end,
        resolved_period_days=resolved_period_days,
        period_suffix=period_suffix,
    )
    return {
        "source": "finance",
        "count": int(payload.get("count") or 0),
        "rowsCount": int(payload.get("rowsCount") or 0),
        "cachedGoodsNmIds": cached_goods_nm_ids_count,
        "matchedCachedGoodsNmIds": matched_cached_goods_nm_ids,
        "periodDays": resolved_period_days,
        "dateFrom": range_start.date().isoformat(),
        "dateTo": range_end.date().isoformat(),
        "cache": cached,
    }


@router.get("/api/v1/wb-repricer/finance-diagnostics")
def get_finance_diagnostics(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90, alias="periodDays"),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
    limit: int = Query(default=5000, ge=1, le=5000),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    range_start, range_end, resolved_period_days, period_suffix = _repricer_period_context(period_days, date_from, date_to)
    if refresh:
        actor, wb_token = _request_actor_and_wb_token(request)
        _ensure_wb_sync_not_running(actor.organization_id)
        _payload, cache, _cached_goods_nm_ids_count, _matched_cached_goods_nm_ids = _save_finance_source_cache(
            actor.organization_id,
            scenario=scenario,
            wb_token=wb_token,
            range_start=range_start,
            range_end=range_end,
            resolved_period_days=resolved_period_days,
            period_suffix=period_suffix,
        )
        organization_id = actor.organization_id
    else:
        organization_id = _hydrate_org_repricer_state(request)
        cache = _period_source_cache(
            organization_id,
            "finance",
            period_suffix,
            resolved_period_days,
            range_start,
            range_end,
            slim=False,
        )
    response = _finance_diagnostics_response(
        organization_id=organization_id,
        cache=cache,
        range_start=range_start,
        range_end=range_end,
        resolved_period_days=resolved_period_days,
        period_suffix=period_suffix,
        limit=limit,
        refreshed=refresh,
    )
    try:
        rows = _list_repricer_skus_for_request(
            request,
            scenario,
            period_days=resolved_period_days,
            date_from=range_start.date(),
            date_to=range_end.date(),
            max_items=limit,
            sort_by_demand=True,
        )
        response["diagnostics"]["marginBreakdown"] = _margin_breakdown_from_rows(
            rows,
            limit,
            finance_diagnostics=response.get("diagnostics"),
        )
    except HTTPException as exc:
        response["diagnostics"]["marginBreakdownError"] = {
            "statusCode": exc.status_code,
            "detail": exc.detail,
        }
    except Exception as exc:
        logger.exception("failed to build WB repricer margin diagnostics")
        response["diagnostics"]["marginBreakdownError"] = {
            "statusCode": 500,
            "detail": str(exc),
        }
    return response


@router.post("/api/v1/wb-repricer/sku/refresh-ads")
def refresh_sku_ads(
    request: Request,
    background_tasks: BackgroundTasks,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    range_start, range_end, resolved_period_days, _period_suffix = _repricer_period_context(period_days, date_from, date_to)
    full_sync_covered, full_sync_range = _full_sync_covers_range(actor.organization_id, range_start, range_end)
    if not full_sync_covered:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WB_FULL_SYNC_REQUIRED",
                "message": "Selected range is outside the last full WB sync range. Run full WB sync for this date range before refreshing ads.",
                "details": {
                    "requestedRange": _range_payload(range_start, range_end),
                    "syncRange": full_sync_range,
                },
            },
        )
    token_fingerprint = wb_token_fingerprint(wb_token)
    try:
        status = begin_wb_sync(
            actor.organization_id,
            trigger="manual",
            period_days=resolved_period_days,
            date_from=date_from,
            date_to=date_to,
            force=False,
            sources=["ads"],
            token_fingerprint=token_fingerprint,
        )
    except WbSyncAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail="WB_SYNC_RUNNING") from exc
    background_tasks.add_task(
        _refresh_wb_data_sources_and_flush,
        organization_id=actor.organization_id,
        wb_token=wb_token,
        scenario=scenario,
        period_days=resolved_period_days,
        date_from=date_from,
        date_to=date_to,
        trigger="manual",
        force=True,
        sources=["ads"],
        initial_status=status,
        token_fingerprint=token_fingerprint,
    )
    return {
        "source": "ads",
        "count": 0,
        "campaignCount": 0,
        "periodDays": resolved_period_days,
        "dateFrom": range_start.date().isoformat(),
        "dateTo": range_end.date().isoformat(),
        "sync": status,
    }


def _baskets_detail_status_key(run_id: str) -> str:
    return f"baskets_detail_status_{run_id}"


def _baskets_detail_progress_percent(completed: Any, total: Any, *, terminal: bool = False) -> int:
    if terminal:
        return 100
    resolved_total = max(0, int(total or 0))
    return min(99, max(0, round(int(completed or 0) / resolved_total * 100))) if resolved_total else 0


BASKETS_DETAIL_HEARTBEAT_TIMEOUT = timedelta(minutes=30)


class BasketsDetailRunSuperseded(RuntimeError):
    pass


def _baskets_detail_mark_failed_if_stale(organization_id: int, status: dict[str, Any]) -> dict[str, Any]:
    if status.get("state") != "running":
        return status
    updated_at = _parse_utc_datetime(status.get("updatedAt") or status.get("startedAt"))
    if updated_at is None or datetime.now(timezone.utc) - updated_at <= BASKETS_DETAIL_HEARTBEAT_TIMEOUT:
        return status
    terminal = {
        **status,
        "state": "failed",
        "error": "Baskets detail run heartbeat expired",
        "progressPercent": _baskets_detail_progress_percent(status.get("requestsCompleted"), status.get("requestsTotal")),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "finishedAt": datetime.now(timezone.utc).isoformat(),
    }
    run_id = str(status.get("runId") or "")
    if run_id:
        save_source_cache(organization_id, _baskets_detail_status_key(run_id), terminal)
    active = get_source_cache(organization_id, "baskets_detail_active", slim=False) or {}
    if active.get("runId") == run_id:
        save_source_cache(organization_id, "baskets_detail_active", terminal)
    return terminal


def _parse_cache_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _baskets_detail_seed_from_cache(
    organization_id: int,
    *,
    cache_key: str,
    range_start: datetime,
    range_end: datetime,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    exact_cache = get_source_cache(organization_id, cache_key, slim=False) or {}
    exact_from = _parse_cache_date(exact_cache.get("dateFrom"))
    exact_to = _parse_cache_date(exact_cache.get("dateTo"))
    daily: dict[str, dict[str, dict[str, Any]]] = {}
    chunks: list[dict[str, Any]] = []
    chunk_keys: set[tuple[str, int]] = set()
    requested_start = range_start.date()
    requested_end = range_end.date()
    required_dates = {day.isoformat() for day in _iter_date_range(requested_start, requested_end)}

    def merge_cache(cache: dict[str, Any]) -> None:
        cached_daily = cache.get("dailyAggregates")
        if isinstance(cached_daily, dict):
            for day, rows in cached_daily.items():
                if isinstance(day, str) and day in required_dates and isinstance(rows, dict):
                    daily.setdefault(day, rows)
        for chunk in (cache.get("chunks") if isinstance(cache.get("chunks"), list) else []):
            if not isinstance(chunk, dict):
                continue
            day = str(chunk.get("date") or "")
            if day not in required_dates or chunk.get("chunkIndex") is None:
                continue
            try:
                chunk_key = (day, int(chunk.get("chunkIndex")))
            except (TypeError, ValueError):
                continue
            if chunk_key in chunk_keys:
                continue
            chunks.append(dict(chunk))
            chunk_keys.add(chunk_key)

    if (
        exact_from
        and exact_to
        and exact_from == requested_start
        and exact_to == requested_end
        and isinstance(exact_cache.get("dailyAggregates"), dict)
    ):
        merge_cache(exact_cache)

    for cache_meta in list_source_cache_ranges_by_prefix(organization_id, "baskets_", limit=100):
        source_key = str(cache_meta.get("sourceKey") or "")
        if not source_key or source_key == cache_key:
            continue
        available_dates = _daily_aggregate_dates(cache_meta)
        if not available_dates or not ((required_dates - set(daily)) & available_dates):
            continue
        cache_from = _parse_cache_date(cache_meta.get("dateFrom"))
        cache_to = _parse_cache_date(cache_meta.get("dateTo"))
        if cache_from and cache_to and (cache_to < requested_start or cache_from > requested_end):
            continue
        merge_cache(get_source_cache(organization_id, source_key, slim=False) or {})
        if required_dates.issubset(daily):
            break
    return daily, chunks


def _run_baskets_detail_job(
    *,
    organization_id: int,
    run_id: str,
    wb_token: str,
    scenario: str,
    range_start: datetime,
    range_end: datetime,
    period_suffix: str,
    nm_ids: list[int],
) -> None:
    status_key = _baskets_detail_status_key(run_id)
    cache_key = f"baskets_{period_suffix}"
    existing_daily_aggregates, existing_chunks = _baskets_detail_seed_from_cache(
        organization_id,
        cache_key=cache_key,
        range_start=range_start,
        range_end=range_end,
    )

    def run_superseded() -> bool:
        active = get_source_cache(organization_id, "baskets_detail_active", slim=False) or {}
        return bool(active.get("runId") != run_id or active.get("forcedRetryAt"))

    def heartbeat(progress: dict[str, Any]) -> None:
        active = get_source_cache(organization_id, "baskets_detail_active", slim=False) or {}
        if active.get("runId") != run_id or active.get("forcedRetryAt"):
            raise BasketsDetailRunSuperseded()
        current = get_source_cache(organization_id, status_key, slim=False) or {}
        next_status = {**current, **progress, "state": "running", "updatedAt": datetime.now(timezone.utc).isoformat()}
        next_status["progressPercent"] = _baskets_detail_progress_percent(next_status.get("requestsCompleted"), next_status.get("requestsTotal"))
        save_source_cache(organization_id, status_key, next_status)
        save_source_cache(organization_id, "baskets_detail_active", next_status)

    try:
        detail = fetch_baskets_daily_detail(
            scenario,
            wb_token=wb_token,
            date_from=range_start,
            date_to=range_end,
            nm_ids=nm_ids,
            progress_callback=heartbeat,
            existing_daily_aggregates=existing_daily_aggregates,
            existing_chunks=existing_chunks,
        )
        if run_superseded():
            return
        exact_cache = get_source_cache(organization_id, cache_key, slim=False) or {}
        merged = {
            **exact_cache,
            "dailyAggregates": detail.get("dailyAggregates") or {},
            "dailyAggregatesDays": len(detail.get("dailyAggregates") or {}),
            "dateFrom": detail.get("dateFrom"),
            "dateTo": detail.get("dateTo"),
            "periodDays": detail.get("periodDays"),
            "dailyDetailFetchedAt": datetime.now(timezone.utc).isoformat(),
            "dailyDetailStatus": "partial" if detail.get("failedChunks") else "fetched",
            **({"dailyDetailError": f"Не удалось загрузить {len(detail.get('failedChunks') or [])} частей корзин. Повторите загрузку, чтобы добрать их."} if detail.get("failedChunks") else {}),
            "chunks": detail.get("chunks") or exact_cache.get("chunks") or [],
            "failedChunks": detail.get("failedChunks") or [],
        }
        save_source_cache(organization_id, cache_key, merged)
        current = get_source_cache(organization_id, status_key, slim=False) or {}
        terminal = {**current, "state": "completed", "requestsCompleted": detail.get("requestsCompleted"), "requestsTotal": detail.get("requestsTotal"), "progressPercent": 100, "failedChunks": detail.get("failedChunks") or [], "updatedAt": datetime.now(timezone.utc).isoformat(), "finishedAt": datetime.now(timezone.utc).isoformat()}
        if detail.get("failedChunks"):
            terminal["warning"] = f"Не удалось загрузить {len(detail.get('failedChunks') or [])} частей корзин. Повторите загрузку, чтобы добрать их."
        save_source_cache(organization_id, status_key, terminal)
        active = get_source_cache(organization_id, "baskets_detail_active", slim=False) or {}
        if active.get("runId") == run_id:
            save_source_cache(organization_id, "baskets_detail_active", terminal)
    except BasketsDetailRunSuperseded:
        return
    except Exception as exc:
        current = get_source_cache(organization_id, status_key, slim=False) or {}
        terminal = {**current, "state": "failed", "error": str(exc), "progressPercent": _baskets_detail_progress_percent(current.get("requestsCompleted"), current.get("requestsTotal")), "updatedAt": datetime.now(timezone.utc).isoformat(), "finishedAt": datetime.now(timezone.utc).isoformat()}
        save_source_cache(organization_id, status_key, terminal)
        active = get_source_cache(organization_id, "baskets_detail_active", slim=False) or {}
        if active.get("runId") == run_id:
            save_source_cache(organization_id, "baskets_detail_active", terminal)


@router.post("/api/v1/wb-repricer/baskets/detail/start")
def start_baskets_detail(request: Request, payload: BasketsDetailStartRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    if payload.dateTo < payload.dateFrom:
        raise HTTPException(status_code=422, detail="dateTo must be on or after dateFrom")
    requested_days = (payload.dateTo - payload.dateFrom).days + 1
    range_start, range_end, resolved_period_days, period_suffix = _repricer_period_context(requested_days, payload.dateFrom, payload.dateTo)
    full_sync_covered, full_sync_range = _full_sync_covers_range(actor.organization_id, range_start, range_end)
    if not full_sync_covered:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WB_FULL_SYNC_REQUIRED",
                "message": "Selected range is outside the last full WB sync range. Run full WB sync for this date range before loading baskets detail.",
                "details": {
                    "requestedRange": _range_payload(range_start, range_end),
                    "syncRange": full_sync_range,
                },
            },
        )
    cache_key = f"baskets_{period_suffix}"
    exact_cache = get_source_cache(actor.organization_id, cache_key, slim=False) or {}
    daily = exact_cache.get("dailyAggregates")
    covering_cache: dict[str, Any] = {}
    exact_has_cached_detail = (
        isinstance(daily, dict)
        and bool(exact_cache.get("dailyDetailFetchedAt"))
        and str(exact_cache.get("dailyDetailStatus") or "") == "fetched"
        and not exact_cache.get("failedChunks")
    )
    has_cached_detail = exact_has_cached_detail
    if not has_cached_detail:
        for covering_key in _sync_status_covering_keys(actor.organization_id, "baskets", range_start, range_end):
            if covering_key == cache_key:
                continue
            covering_cache = get_source_cache(actor.organization_id, covering_key, slim=False) or {}
            covering_daily = covering_cache.get("dailyAggregates")
            has_cached_detail = (
                isinstance(covering_daily, dict)
                and bool(covering_cache.get("dailyDetailFetchedAt"))
                and str(covering_cache.get("dailyDetailStatus") or "") == "fetched"
                and not covering_cache.get("failedChunks")
            )
            if has_cached_detail:
                covering_cache["sourceKey"] = covering_key
                break
    if payload.force:
        exact_has_cached_detail = False
        has_cached_detail = False
        covering_cache = {}
    active = get_source_cache(actor.organization_id, "baskets_detail_active", slim=False) or {}
    active = _baskets_detail_mark_failed_if_stale(actor.organization_id, active)
    if not has_cached_detail and active.get("state") == "running":
        if payload.force:
            now_iso = datetime.now(timezone.utc).isoformat()
            stopped = {
                **active,
                "state": "failed",
                "error": "Остановлено ручным повтором корзин по дням",
                "finishedAt": now_iso,
                "updatedAt": now_iso,
                "forcedRetryAt": now_iso,
            }
            previous_run_id = str(active.get("runId") or "")
            if previous_run_id:
                save_source_cache(actor.organization_id, _baskets_detail_status_key(previous_run_id), stopped)
            save_source_cache(actor.organization_id, "baskets_detail_active", stopped)
        elif active.get("dateFrom") == payload.dateFrom.isoformat() and active.get("dateTo") == payload.dateTo.isoformat():
            return active
        else:
            raise HTTPException(status_code=409, detail={"code": "BASKETS_DETAIL_ALREADY_RUNNING", "message": "Another baskets detail run is still active", "details": {"activeRunId": active.get("runId"), "dateFrom": active.get("dateFrom"), "dateTo": active.get("dateTo")}})
    if not has_cached_detail and not payload.force:
        combined_daily, combined_chunks = _baskets_detail_seed_from_cache(
            actor.organization_id,
            cache_key=cache_key,
            range_start=range_start,
            range_end=range_end,
        )
        required_dates = {day.isoformat() for day in _iter_date_range(range_start.date(), range_end.date())}
        if required_dates.issubset(combined_daily):
            now_iso = datetime.now(timezone.utc).isoformat()
            exact_cache = {
                **exact_cache,
                "dateFrom": range_start.date().isoformat(),
                "dateTo": range_end.date().isoformat(),
                "periodDays": resolved_period_days,
                "dailyAggregates": {day: combined_daily[day] for day in sorted(required_dates)},
                "dailyAggregatesDays": len(required_dates),
                "dailyDetailFetchedAt": now_iso,
                "dailyDetailStatus": "fetched",
                "dailyDetailCombinedAt": now_iso,
                "chunks": combined_chunks,
                "failedChunks": [],
            }
            exact_cache.pop("dailyDetailError", None)
            save_source_cache(actor.organization_id, cache_key, exact_cache)
            exact_has_cached_detail = True
            has_cached_detail = True
            covering_cache = {}
    run_id = f"baskets_detail_{uuid4().hex[:16]}"
    status = {
        "runId": run_id,
        "state": "completed" if has_cached_detail else "running",
        "cached": has_cached_detail,
        "cacheSourceKey": cache_key if exact_has_cached_detail else covering_cache.get("sourceKey"),
        "dateFrom": payload.dateFrom.isoformat(),
        "dateTo": payload.dateTo.isoformat(),
        "periodDays": resolved_period_days,
        "requestsCompleted": 0,
        "requestsTotal": 0,
        "progressPercent": 100 if has_cached_detail else 0,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    save_source_cache(actor.organization_id, _baskets_detail_status_key(run_id), status)
    if status["cached"]:
        return status
    nm_ids = _nm_ids_from_goods(list_cached_goods(actor.organization_id))
    if not nm_ids:
        raise HTTPException(status_code=400, detail="No cached goods with nmID. Refresh WB goods first, then refresh baskets.")
    status["requestsTotal"] = resolved_period_days * ((len(nm_ids) + 999) // 1000)
    save_source_cache(actor.organization_id, _baskets_detail_status_key(run_id), status)
    save_source_cache(actor.organization_id, "baskets_detail_active", status)
    background_tasks.add_task(_run_baskets_detail_job, organization_id=actor.organization_id, run_id=run_id, wb_token=wb_token, scenario=payload.scenario, range_start=range_start, range_end=range_end, period_suffix=period_suffix, nm_ids=nm_ids)
    return status


@router.get("/api/v1/wb-repricer/baskets/detail/status")
def get_baskets_detail_status(request: Request, runId: str = Query(...)) -> dict[str, Any]:
    actor = actor_from_request(request)
    status = get_source_cache(actor.organization_id, _baskets_detail_status_key(runId), slim=False) or {}
    if not status or status.get("runId") != runId:
        raise HTTPException(status_code=404, detail="Baskets detail run not found")
    return _baskets_detail_mark_failed_if_stale(actor.organization_id, status)


@router.post("/api/v1/wb-repricer/sku/refresh-baskets")
def refresh_sku_baskets(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    _ensure_wb_sync_not_running(actor.organization_id)
    range_start, range_end, resolved_period_days, period_suffix = _repricer_period_context(period_days, date_from, date_to)
    full_sync_covered, full_sync_range = _full_sync_covers_range(actor.organization_id, range_start, range_end)
    if not full_sync_covered:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WB_FULL_SYNC_REQUIRED",
                "message": "Selected range is outside the last full WB sync range. Run full WB sync for this date range before refreshing baskets.",
                "details": {
                    "requestedRange": _range_payload(range_start, range_end),
                    "syncRange": full_sync_range,
                },
            },
        )
    cached_goods = list_cached_goods(actor.organization_id)
    nm_ids = _nm_ids_from_goods(cached_goods)
    if not nm_ids:
        raise HTTPException(
            status_code=400,
            detail="No cached goods with nmID. Refresh WB goods first, then refresh baskets.",
        )
    payload = fetch_baskets_aggregates(
        scenario,
        wb_token=wb_token,
        period_days=resolved_period_days,
        date_from=range_start,
        date_to=range_end,
        nm_ids=nm_ids,
        include_daily=True,
    )
    cached = save_source_cache(
        actor.organization_id,
        f"baskets_{period_suffix}",
        payload,
    )
    response: dict[str, Any] = {
        "source": "baskets",
        "count": int(payload.get("count") or 0),
        "requestedNmIds": int(payload.get("requestedNmIds") or 0),
        "matchedNmIds": int(payload.get("matchedNmIds") or 0),
        "periodDays": resolved_period_days,
        "dateFrom": range_start.date().isoformat(),
        "dateTo": range_end.date().isoformat(),
        "cache": cached,
    }
    if response["requestedNmIds"] > 0 and response["matchedNmIds"] == 0:
        response["partial"] = True
        response["warning"] = (
            "WB Analytics не вернул строки воронки по товарам из кеша. "
            "Проверьте токен категории Analytics, кабинет продавца и выбранный период."
        )
    return response


@router.get("/api/v1/wb-repricer/sku/{articleId}/settings")
def get_sku_settings(request: Request, articleId: str, scenario: str = Query(default="complete")) -> dict[str, Any]:
    _hydrate_org_repricer_state(request)
    return get_repricer_sku_settings(articleId, scenario, wb_token=_request_wb_token(request))


def _configuration_checkpoint_scope(request: Request, article_id: str, nm_id: int | None = None):
    actor = actor_from_request(request)
    if not has_permission(actor, "settings:write"):
        raise HTTPException(403, "NO_ACCESS:settings:write")
    article_id = article_id.strip()
    by_nm, by_article = _goods_lookup(actor.organization_id)
    resolved_nm = by_article.get(article_id.lower())
    if resolved_nm is None:
        raise HTTPException(404, "SKU_NOT_FOUND")
    if nm_id is not None and resolved_nm != nm_id:
        raise HTTPException(409, "SKU_IDENTITY_CHANGED")
    from app.repricer_sync import wb_token_fingerprint
    return actor, by_nm[resolved_nm], resolved_nm, wb_token_fingerprint(_request_wb_token(request))


@router.get("/api/v1/wb-repricer/sources/status")
def get_wb_sources_status(request: Request, marketplaceAccountId: int | None = Query(default=None, gt=0)):
    from app.wb_sources_status import read_sources_status
    actor = actor_from_request(request)
    if not has_permission(actor, "settings:read"):
        raise HTTPException(status_code=403, detail={"code": "NO_ACCESS", "message": "Нет доступа к источникам."})
    return read_sources_status(actor, marketplaceAccountId)


@router.get("/api/v1/wb-repricer/sku/{articleId}/configuration-state")
def get_sku_configuration_state(request: Request, articleId: str) -> dict[str, Any]:
    from app.repricer_configuration_checkpoint import configuration_state
    actor, article, nm, _ = _configuration_checkpoint_scope(request, articleId)
    return {"nmId": nm, "articleId": article, **configuration_state(actor.organization_id, article)}


@router.post("/api/v1/wb-repricer/sku/{articleId}/configuration-checkpoints")
def post_sku_configuration_checkpoint(request: Request, articleId: str, payload: SkuConfigurationCheckpointRequest) -> dict[str, Any]:
    from app.repricer_configuration_checkpoint import create_checkpoint
    actor, article, nm, binding = _configuration_checkpoint_scope(request, articleId, payload.nmId)
    return create_checkpoint(org=actor.organization_id, nm=nm, article=article, binding=binding,
        actor=actor.user_id, expected_revision=payload.expectedRevision)


@router.post("/api/v1/wb-repricer/sku/{articleId}/configuration-checkpoints/{checkpointId}/restore")
def post_restore_sku_configuration_checkpoint(request: Request, articleId: str, checkpointId: str,
                                             payload: SkuConfigurationCheckpointRequest) -> dict[str, Any]:
    from app.repricer_configuration_checkpoint import restore_checkpoint
    actor, article, nm, binding = _configuration_checkpoint_scope(request, articleId, payload.nmId)
    result = restore_checkpoint(org=actor.organization_id, nm=nm, article=article, binding=binding,
        actor=actor.user_id, checkpoint_id=checkpointId, expected_revision=payload.expectedRevision)
    hydrate_repricer_bff_state(actor.organization_id, repricer_bff_module)
    return result


@router.put("/api/v1/wb-repricer/sku/{articleId}/settings")
def put_sku_settings(
    request: Request,
    articleId: str,
    payload: dict[str, Any],
    scenario: str = Query(default="complete"),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    result = put_repricer_sku_settings(articleId, payload, scenario, wb_token=_request_wb_token(request))
    saved = _flush_org_repricer_state(organization_id)
    if _SKU_ECONOMICS_FIELDS.intersection(payload):
        if not saved:
            raise HTTPException(status_code=500, detail="SKU_SETTINGS_NOT_SAVED")
        effective_from = _parse_utc_datetime((result.get("meta") or {}).get("lastSavedAt"))
        if effective_from is None:
            raise HTTPException(status_code=500, detail="SKU_SETTINGS_TIMESTAMP_MISSING")
        try:
            EconomicsService(session, organization_id).reconcile_legacy_sku_override(
                articleId,
                dict(result.get("settings") or {}),
                effective_from=effective_from,
                source_reference=_economics_source_reference(
                    "sku-settings", articleId, effective_from.isoformat()
                ),
            )
        except EconomicsNotFoundError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ECONOMICS_SKU_MAPPING_MISSING",
                    "message": str(exc),
                },
            ) from exc
        except EconomicsValidationError as exc:
            _raise_invalid_economics(exc)
    return result


def _apply_sku_manager_assignment(
    request: Request,
    articleId: str,
    payload: SkuManagerAssignmentRequest,
    actor: Any,
    scenario: str,
) -> dict[str, Any]:
    if not (has_permission(actor, "settings:write") or has_permission(actor, "team:write") or has_permission(actor, "price:send")):
        raise HTTPException(status_code=403, detail="NO_ACCESS:settings:write")
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="REASON_REQUIRED")

    organization_id = actor.organization_id
    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    team_user = _team_user_by_id(organization_id, payload.managerUserId)
    fallback_manager_name = _fallback_manager_name(payload.managerUserId)
    if payload.managerUserId and team_user is None and fallback_manager_name is None:
        raise HTTPException(status_code=404, detail="TEAM_USER_NOT_FOUND")
    if team_user is not None and not team_user.isActive:
        raise HTTPException(status_code=409, detail="TEAM_USER_INACTIVE")

    meta = dict(repricer_bff_module.SKU_META_OVERRIDES.get(articleId) or {})
    previous = {
        "managerId": meta.get("managerId"),
        "managerName": meta.get("managerName"),
        "assignmentSource": meta.get("assignmentSource"),
        "assignedAt": meta.get("assignedAt"),
    }
    next_manager_id = team_user.userId if team_user is not None else payload.managerUserId
    next_manager_name = team_user.fullName if team_user is not None else (fallback_manager_name or "Без ответственного")
    assigned_at = datetime.now(timezone.utc).isoformat()

    overrides = repricer_bff_module.SKU_META_OVERRIDES.setdefault(articleId, {})
    overrides["managerId"] = next_manager_id
    overrides["managerName"] = next_manager_name
    overrides["assignmentSource"] = "manual" if next_manager_id else "none"
    overrides["assignedAt"] = assigned_at
    logger.info(
        "repricer manager assignment saved article=%s manager=%s org=%s",
        articleId,
        next_manager_id or "unassigned",
        organization_id,
    )

    actor_user = _team_user_by_id(organization_id, actor.user_id)
    event = {
        "id": f"audit-{articleId}-manager-{len(repricer_bff_module.SKU_AUDIT_EVENTS.get(articleId, [])) + 1}",
        "sku": articleId,
        "createdAt": assigned_at,
        "actor": {
            "id": actor.user_id,
            "name": actor_user.fullName if actor_user is not None else (actor.email or actor.user_id),
            "role": "manager",
        },
        "source": "manager",
        "scope": "sku",
        "action": "Смена ответственного",
        "reason": reason,
        "oldValue": previous["managerName"] or "без ответственного",
        "newValue": next_manager_name,
    }
    repricer_bff_module.SKU_AUDIT_EVENTS.setdefault(articleId, []).append(event)
    next_state = {
        "managerId": next_manager_id,
        "managerName": next_manager_name,
        "assignmentSource": overrides["assignmentSource"],
        "assignedAt": assigned_at,
    }
    record_audit_event(
        organization_id=organization_id,
        actor_user_id=actor.user_id,
        action="repricer.sku.manager.update",
        object_type="repricer_sku",
        object_id=articleId,
        before_state=previous,
        after_state=next_state,
        reason=reason,
        details={"articleId": articleId, **next_state},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    _flush_org_repricer_state(organization_id)

    return {
        "articleId": articleId,
        "meta": {
            "articleId": articleId,
            **next_state,
        },
        "auditEvents": list(repricer_bff_module.SKU_AUDIT_EVENTS.get(articleId, [])),
    }


@router.post("/api/v1/wb-repricer/sku/{articleId}/manager")
def post_sku_manager(
    request: Request,
    articleId: str,
    payload: SkuManagerAssignmentRequest,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    return _apply_sku_manager_assignment(request, articleId, payload, actor_from_request(request), scenario)


@router.patch("/api/v1/wb-repricer/sku/{articleId}/manager")
def patch_sku_manager(
    request: Request,
    articleId: str,
    payload: SkuManagerAssignmentRequest,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    return _apply_sku_manager_assignment(request, articleId, payload, actor_from_request(request), scenario)


@router.post("/api/v1/wb-repricer/sku/{articleId}/manager-simple")
async def post_sku_manager_simple(
    request: Request,
    articleId: str,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    try:
        raw_payload = json.loads((await request.body()).decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_TEXT_PAYLOAD") from exc
    payload = SkuManagerAssignmentRequest(
        managerUserId=raw_payload.get("managerUserId"),
        reason=str(raw_payload.get("reason") or ""),
    )
    return _apply_sku_manager_assignment(request, articleId, payload, actor_from_request(request), scenario)


@router.patch("/api/v1/wb-repricer/sku/{articleId}/automation")
def patch_sku_automation(
    request: Request,
    articleId: str,
    payload: AutomationPatchRequest,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    # Resolve session activity and catalog identity before the SQLite write lock.
    actor, _ = _request_actor_and_wb_token(request)
    if not has_permission(actor, "settings:write"):
        raise HTTPException(403, "NO_ACCESS:settings:write")
    organization_id = actor.organization_id
    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    article = articleId.strip()
    rows = _list_repricer_skus_for_request(request, scenario, max_items=None)
    current = next((row for row in rows if str(row["meta"]["articleId"]).strip() == article), None)
    if current is None:
        raise HTTPException(404, "SKU_NOT_FOUND")
    from app.repricer_dispatch_store import mutate_assignments
    result = mutate_assignments(organization_id, [article], lambda state: patch_repricer_automation(
        article, payload.automationEnabled, scenario, state=state, current_row=current,
        actor_id=actor.user_id, actor_role=actor.role),
        expected_revisions={article: payload.expectedRevision} if payload.expectedRevision is not None else None)
    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    return result


@router.get("/api/v1/wb-repricer/dashboard")
def get_dashboard(request: Request, scenario: str = Query(default="complete")) -> dict[str, Any]:
    return get_repricer_dashboard(scenario, wb_token=_request_wb_token(request))


@router.get("/api/v1/wb-repricer/templates")
def get_templates(request: Request, scenario: str = Query(default="complete")) -> dict[str, Any]:
    return get_repricer_templates(scenario, wb_token=_request_wb_token(request))


@router.get("/api/v1/wb-repricer/strategies/catalog")
def get_frontend_strategy_catalog(request: Request, scenario: str = Query(default="complete")) -> dict[str, Any]:
    _hydrate_org_repricer_state(request)
    sku_rows = _list_repricer_skus_for_request(request, scenario)
    items = list_frontend_strategy_catalog(scenario, wb_token=_request_wb_token(request), sku_rows=sku_rows)
    return {"items": items, "total": len(items)}


@router.get("/api/v1/wb-repricer/strategy-assignments")
def get_frontend_strategy_assignments(request: Request, scenario: str = Query(default="complete")) -> dict[str, Any]:
    _hydrate_org_repricer_state(request)
    sku_rows = _list_repricer_skus_for_request(request, scenario, max_items=None)
    items = list_frontend_strategy_assignments(scenario, wb_token=_request_wb_token(request), sku_rows=sku_rows)
    return {"items": items, "total": len(items)}


@router.get("/api/v1/wb-repricer/sku-groups")
def get_repricer_sku_groups(request: Request) -> dict[str, Any]:
    _hydrate_org_repricer_state(request)
    items = list_repricer_sku_groups()
    return {"items": items, "total": len(items)}


@router.post("/api/v1/wb-repricer/sku-groups")
def post_repricer_sku_group(request: Request, payload: SkuGroupUpsertRequest) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    group = upsert_repricer_sku_group(payload.model_dump(mode="json"))
    _flush_org_repricer_state(organization_id)
    return group


@router.post("/api/v1/wb-repricer/strategy-assignments/bulk")
def post_frontend_strategy_assignments_bulk(
    request: Request,
    payload: StrategyAssignmentBulkRequest,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    strategy_id_or_name = payload.strategyId or payload.strategyName
    if not strategy_id_or_name:
        raise HTTPException(status_code=422, detail="STRATEGY_REQUIRED")
    organization_id = _hydrate_org_repricer_state(request)
    wb_token = _request_wb_token(request)
    sku_rows = _list_repricer_skus_for_request(request, scenario, max_items=None)
    normalized_strategy = str(strategy_id_or_name or "").strip().lower()
    unassign_requested = normalized_strategy in {"none", "no_strategy", "null", "нет", "не задана", "без стратегии"}
    from app.repricer_dispatch_store import mutate_assignments
    def mutate(state):
        if unassign_requested:
            return unassign_frontend_strategy_assignment(
                payload.articleIds,
                scenario=scenario,
                wb_token=wb_token,
                sku_rows=sku_rows,
                state=state,
            )
        return apply_frontend_strategy_assignment(
            payload.articleIds,
            strategy_id_or_name=strategy_id_or_name,
            scenario=scenario,
            wb_token=wb_token,
            sku_rows=sku_rows,
            config=payload.config,
            state=state,
        )
    result = mutate_assignments(organization_id, payload.articleIds, mutate, expected_revisions=payload.expectedRevisions)
    # Persistence has committed. A follow-up failure must not discard its CAS
    # acknowledgement or invite a retry of an execution that may have dispatched.
    follow_up_phase = "refresh"
    try:
        hydrate_repricer_bff_state(organization_id, repricer_bff_module)
        if payload.executeAfterAssign and not unassign_requested:
            follow_up_phase = "execution"
            actor = actor_from_request(request)
            wb_token = _request_wb_token(request)
            client = _build_repricing_client(scenario, wb_token)
            execution_rows = _list_repricer_skus_for_request(request, scenario, max_items=None)
            execution = execute_repricer_strategies(
                payload.articleIds,
                client=client,
                actor_id=str(getattr(actor, "actor_id", None) or getattr(actor, "user_id", None) or "manager-api"),
                actor_role=str(getattr(actor, "role", None) or "manager"),
                wb_token=wb_token,
                options=StrategyExecuteOptions(scenario=scenario),
                sku_rows=execution_rows,
                organization_id=organization_id,
                trigger="bulk_assign",
            )
            result["execution"] = execution.model_dump(mode="json")
    except Exception as exc:
        # Do not serialize exception text: provider errors may contain secrets.
        logging.getLogger(__name__).error(
            "WB strategy assignment committed; follow-up failed (%s)", type(exc).__name__)
        result["executionError"] = {
            "code": "STRATEGY_EXECUTION_FAILED" if follow_up_phase == "execution" else "STRATEGY_STATE_REFRESH_FAILED",
            "message": ("Назначение стратегии сохранено, но выполнение завершилось ошибкой. Результат изменения цены не подтверждён; проверьте статус перед повторным запуском."
                if follow_up_phase == "execution" else "Назначение стратегии сохранено, но чтение обновлённого состояния завершилось ошибкой. Повторите чтение состояния."),
        }
    return result


@router.post("/api/v1/wb-repricer/strategies/preview")
async def post_preview_repricer_strategies(request: Request) -> dict[str, Any]:
    payload = _preview_request_from_payload(await _request_json_object(request))
    actor = actor_from_request(request)
    if not payload.articleIds:
        raise HTTPException(status_code=422, detail="ARTICLE_IDS_REQUIRED")
    organization_id = _hydrate_org_repricer_state(request)
    wb_token = _request_wb_token(request)
    client = _build_repricing_client(payload.scenario, wb_token)
    if payload.inputs is not None and len(payload.articleIds) == 1:
        simulator_inputs = _simulator_patch_payload(payload.inputs)
        sku_rows = _apply_simulator_inputs_for_article(
            request,
            organization_id=organization_id,
            wb_token=wb_token,
            scenario=payload.scenario,
            article_id=payload.articleIds[0],
            payload=simulator_inputs,
            period_days=payload.periodDays,
        )
        _flush_org_repricer_state(organization_id)
    else:
        sku_rows = _list_repricer_skus_for_request(request, payload.scenario, max_items=None, period_days=payload.periodDays)
    report = preview_repricer_strategies(
        payload.articleIds,
        client=client,
        actor_id=actor.actor_id,
        actor_role=actor.role,
        wb_token=wb_token,
        scenario=payload.scenario,
        sku_rows=sku_rows,
        force=payload.force,
        organization_id=organization_id if payload.inputs is None else None,
        strategy_id_or_name=payload.strategyId if payload.strategyId is not None else payload.strategyName,
        config=payload.config,
    )
    return report.model_dump(mode="json")


@router.post("/api/v1/wb-repricer/strategies/execute")
def post_execute_repricer_strategies(request: Request, payload: StrategyExecuteRequest) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not payload.articleIds:
        raise HTTPException(status_code=422, detail="ARTICLE_IDS_REQUIRED")
    organization_id = _hydrate_org_repricer_state(request)
    wb_token = _request_wb_token(request)
    client = _build_repricing_client(payload.scenario, wb_token)
    sku_rows = _list_repricer_skus_for_request(request, payload.scenario, max_items=None)
    report = execute_repricer_strategies(
        payload.articleIds,
        client=client,
        actor_id=actor.actor_id,
        actor_role=actor.role,
        wb_token=wb_token,
        options=StrategyExecuteOptions(
            scenario=payload.scenario,
            createDrafts=payload.createDrafts,
            autoApprove=payload.autoApprove,
            approvalRef=payload.approvalRef,
            applyPrices=payload.applyPrices,
            simulateLocalPrice=payload.simulateLocalPrice,
            force=payload.force,
        ),
        sku_rows=sku_rows,
        organization_id=organization_id,
        trigger="manual_execute",
    )
    _flush_org_repricer_state(organization_id)
    return report.model_dump(mode="json")


@router.post("/api/v1/wb-repricer/strategies/execute-assigned")
def post_execute_assigned_repricer_strategies(
    request: Request,
    scenario: str = Query(default="complete"),
    apply_prices: bool = Query(default=True, alias="applyPrices"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    organization_id = _hydrate_org_repricer_state(request)
    wb_token = _request_wb_token(request)
    client = _build_repricing_client(scenario, wb_token)
    sku_rows = _list_repricer_skus_for_request(request, scenario, max_items=None)
    report = execute_all_assigned_skus(
        client=client,
        actor_id=actor.actor_id,
        actor_role=actor.role,
        wb_token=wb_token,
        options=StrategyExecuteOptions(scenario=scenario, applyPrices=apply_prices),
        sku_rows=sku_rows,
        organization_id=organization_id,
        trigger="manual_execute_assigned",
    )
    _flush_org_repricer_state(organization_id)
    return report.model_dump(mode="json")


@router.get("/api/v1/wb-repricer/simulator")
def get_repricer_simulator(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90, alias="periodDays"),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    rows = _list_repricer_skus_for_request(request, scenario, max_items=None, period_days=period_days)
    payload = _simulator_dashboard_payload(rows)
    payload["cache"] = _repricer_list_cache_meta(organization_id, period_days, include_content=False)
    return payload


def _algorithm_interval_minutes(key: str, fallback_minutes: int, *, minimum: int = 5) -> int:
    raw = repricer_bff_module.ALGORITHM_SETTINGS_STATE.get(key)
    if key == "syncIntervalMinutes" and (raw is None or raw == ""):
        raw_hours = repricer_bff_module.ALGORITHM_SETTINGS_STATE.get("syncIntervalHours")
        try:
            raw = int(raw_hours) * 60 if raw_hours is not None else fallback_minutes
        except (TypeError, ValueError):
            raw = fallback_minutes
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        interval = fallback_minutes
    return max(minimum, interval)


def _next_interval_boundary(now: datetime, interval_minutes: int) -> datetime:
    interval_seconds = max(1, interval_minutes) * 60
    now_seconds = int(now.timestamp())
    remainder = now_seconds % interval_seconds
    next_seconds = now_seconds + (interval_seconds - remainder if remainder else interval_seconds)
    return datetime.fromtimestamp(next_seconds, tz=timezone.utc)


REPRICER_SCHEDULER_POLL_MINUTES = 5


def _night_median_worker_debug(organization_id: int, sku_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    def _hour_label(value: Any, fallback: int) -> str:
        try:
            hour = int(value)
        except (TypeError, ValueError):
            hour = fallback
        return f"{max(0, min(23, hour)):02d}:00"

    cache = get_source_cache(organization_id, "night_median_baskets", slim=False) or {}
    windows = cache.get("windows") if isinstance(cache.get("windows"), dict) else {}
    keys = sorted(str(key) for key in windows.keys())
    latest_key = keys[-1] if keys else None
    latest_window = windows.get(latest_key) if latest_key else None
    if not isinstance(latest_window, dict):
        latest_window = {}
    samples = latest_window.get("samples") if isinstance(latest_window.get("samples"), dict) else {}
    applied = latest_window.get("applied") if isinstance(latest_window.get("applied"), dict) else {}
    sample_rows: list[dict[str, Any]] = []
    for article_id, sample in samples.items():
        if not isinstance(sample, dict):
            continue
        values = sample.get("values") if isinstance(sample.get("values"), list) else []
        last_value = values[-1] if values and isinstance(values[-1], dict) else {}
        sample_rows.append(
            {
                "articleId": str(article_id),
                "nmId": sample.get("nmId"),
                "samplesCount": len(values),
                "medianBaskets": sample.get("medianBaskets"),
                "lastBaskets": last_value.get("baskets"),
                "lastAt": sample.get("lastAt") or last_value.get("at"),
                "applied": str(article_id) in applied,
                "appliedAt": (applied.get(str(article_id)) or {}).get("at") if isinstance(applied.get(str(article_id)), dict) else None,
            }
        )
    sample_rows.sort(key=lambda item: (not bool(item.get("applied")), str(item.get("articleId") or "")))
    settings_state = repricer_bff_module.ALGORITHM_SETTINGS_STATE
    enabled = bool(settings_state.get("nightMedianEnabled", True))
    collect_enabled = bool(settings_state.get("nightMedianCollectEnabled", True))
    auto_all = bool(settings_state.get("nightMedianAutoEnableAllSkus", settings_state.get("nightMedianGlobal", True)))
    timezone_raw = str(settings_state.get("nightMedianTimezone") or "МСК (UTC+3)").strip()
    timezone_name = {
        "мск (utc+3)": "Europe/Moscow",
        "мск": "Europe/Moscow",
        "utc+3": "Europe/Moscow",
        "utc+03:00": "Europe/Moscow",
        "europe/moscow": "Europe/Moscow",
        "asia/yekaterinburg": "Asia/Yekaterinburg",
        "ekaterinburg": "Asia/Yekaterinburg",
    }.get(timezone_raw.lower(), timezone_raw or "Europe/Moscow")
    try:
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    except Exception:
        timezone_name = "Europe/Moscow"
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    start_hour = int(settings_state.get("nightMedianWindowStartHour") or 23)
    end_hour = int(settings_state.get("nightMedianWindowEndHour") or 6)
    is_window_active = (start_hour <= now_local.hour < end_hour) if start_hour < end_hour else (now_local.hour >= start_hour or now_local.hour < end_hour)
    eligible_count = 0
    manual_night_count = 0
    individual_enabled_count = 0
    active_strategy_count = 0
    eligible_items: list[dict[str, Any]] = []
    for row in sku_rows or []:
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
        strategy = row.get("strategy") if isinstance(row.get("strategy"), dict) else {}
        article_id = str(meta.get("articleId") or "")
        strategy_id = str(strategy.get("id") or meta.get("activeStrategyId") or "")
        source = str(strategy.get("assignmentSource") or meta.get("assignmentSource") or "").lower().strip()
        explicit = bool(strategy_id) and source not in {"", "derived", "none"}
        if explicit and strategy_id != "illiquid":
            active_strategy_count += 1
        if explicit and strategy_id == "night_price_mode":
            manual_night_count += 1
        if explicit and bool(settings.get("nightMedianEnabled", False)):
            individual_enabled_count += 1
        eligible = enabled and explicit and strategy_id != "illiquid" and (auto_all or bool(settings.get("nightMedianEnabled", False)) or strategy_id == "night_price_mode")
        if eligible:
            eligible_count += 1
            if len(eligible_items) < 20:
                eligible_items.append(
                    {
                        "articleId": article_id,
                        "strategyId": strategy_id,
                        "strategyName": strategy.get("name"),
                        "assignmentSource": source,
                        "nightMedianEnabled": bool(settings.get("nightMedianEnabled", False)),
                    }
                )
    phase = "off"
    if enabled and collect_enabled and is_window_active:
        phase = "collecting"
    elif enabled:
        if not latest_key:
            phase = "waiting_for_night"
        elif len(samples) == 0:
            phase = "no_samples_collected"
        else:
            phase = "waiting_for_morning_or_applied"
    return {
        "enabled": enabled,
        "collectEnabled": collect_enabled,
        "autoAllSkus": auto_all,
        "phase": phase,
        "isWindowActive": is_window_active,
        "activeStrategySkuCount": active_strategy_count,
        "eligibleSkuCount": eligible_count,
        "manualNightSkuCount": manual_night_count,
        "individualEnabledSkuCount": individual_enabled_count,
        "eligibleItems": eligible_items,
        "mode": settings_state.get("nightMedianMode") or "conservative",
        "windowLabel": latest_window.get("windowLabel")
        or (
            f"{_hour_label(settings_state.get('nightMedianWindowStartHour'), 23)}-"
            f"{_hour_label(settings_state.get('nightMedianWindowEndHour'), 6)}"
        ),
        "timezone": latest_window.get("timezone") or settings_state.get("nightMedianTimezone") or "МСК (UTC+3)",
        "applyDeltaPct": settings_state.get("nightMedianApplyDeltaPct"),
        "lastCollectedAt": cache.get("lastCollectedAt"),
        "activeWindowKey": cache.get("activeWindowKey"),
        "latestWindowKey": latest_key,
        "latestWindowUpdatedAt": latest_window.get("updatedAt"),
        "windowsCount": len(keys),
        "sampledSkuCount": len(samples),
        "appliedSkuCount": len(applied),
        "pendingSkuCount": max(0, len(samples) - len(applied)),
        "items": sample_rows[:50],
    }


def _night_median_sku_rows(organization_id: int) -> list[dict[str, Any]]:
    rows = []
    for good in list_cached_goods(organization_id):
        article_id = str(good.get("vendorCode") or "")
        if article_id not in repricer_bff_module.FRONTEND_STRATEGY_ASSIGNMENTS:
            continue
        rows.append({
            "meta": {"articleId": article_id},
            "settings": repricer_bff_module._sku_cost_settings(article_id, use_demo_data=False),
            "strategy": repricer_bff_module._frontend_strategy_payload(
                article_id, current_status="", baskets_last_7d=0, basket_norm=0,
            ),
        })
    return rows


@router.get("/api/v1/wb-repricer/worker/status")
def get_repricer_worker_status(
    request: Request,
    limit: int = Query(default=8, ge=1, le=50),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    organization_id = actor.organization_id
    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    _apply_costs_excel_cache_to_sku_settings(organization_id)
    settings = get_settings()
    interval_minutes = _algorithm_interval_minutes(
        "syncIntervalMinutes",
        int(getattr(settings, "repricer_execute_interval_minutes", 60) or 60),
    )
    full_sync_interval_minutes = _algorithm_interval_minutes(
        "fullSyncIntervalMinutes",
        int(getattr(settings, "repricer_wb_sync_interval_minutes", 60) or 60),
    )
    scheduler_poll_interval_minutes = REPRICER_SCHEDULER_POLL_MINUTES
    now = datetime.now(timezone.utc)
    runs = list_execution_runs(organization_id=organization_id, trigger="scheduler", limit=limit)
    pending_approvals = list_pending_price_approvals(organization_id=organization_id, limit=20, include_inflight=True)
    sync_status = get_wb_sync_status(organization_id)
    try:
        night_debug_rows = _night_median_sku_rows(organization_id)
    except Exception:
        logger.exception("Failed to build night median worker debug rows")
        night_debug_rows = []
    worker_auto_apply_prices = bool(
        repricer_bff_module.ALGORITHM_SETTINGS_STATE.get("workerAutoApplyPricesEnabled")
    )
    last_scheduler_at = _parse_utc_datetime(runs[0].get("createdAt")) if runs else None

    next_run_at: datetime | None = None
    seconds_until_next: int | None = None
    next_scheduler_poll_at: datetime | None = None
    seconds_until_next_scheduler_poll: int | None = None
    sync_running = bool(sync_status.get("running")) if isinstance(sync_status, dict) else False
    last_full_sync_at = _parse_utc_datetime(sync_status.get("finishedAt")) if isinstance(sync_status, dict) else None
    next_full_sync_at: datetime | None = None
    seconds_until_next_full_sync: int | None = None
    if settings.repricer_scheduler_enabled:
        next_scheduler_poll_at = _next_interval_boundary(now, scheduler_poll_interval_minutes)
        seconds_until_next_scheduler_poll = max(
            0,
            int((next_scheduler_poll_at - now).total_seconds()),
        )
        if last_scheduler_at is None:
            next_run_at = _next_interval_boundary(now, interval_minutes)
        else:
            interval_seconds = interval_minutes * 60
            elapsed_seconds = max(0, int((now - last_scheduler_at).total_seconds()))
            elapsed_periods = elapsed_seconds // interval_seconds
            next_run_at = last_scheduler_at + timedelta(seconds=interval_seconds * (elapsed_periods + 1))
        seconds_until_next = max(0, int((next_run_at - now).total_seconds())) if next_run_at else None
    if getattr(settings, "repricer_wb_sync_enabled", False) and not sync_running:
        if last_full_sync_at is None:
            next_full_sync_at = _next_interval_boundary(now, full_sync_interval_minutes)
        else:
            full_sync_interval_seconds = full_sync_interval_minutes * 60
            elapsed_full_sync_seconds = max(0, int((now - last_full_sync_at).total_seconds()))
            elapsed_full_sync_periods = elapsed_full_sync_seconds // full_sync_interval_seconds
            next_full_sync_at = last_full_sync_at + timedelta(seconds=full_sync_interval_seconds * (elapsed_full_sync_periods + 1))
        seconds_until_next_full_sync = max(0, int((next_full_sync_at - now).total_seconds())) if next_full_sync_at else None

    return {
        "organizationId": organization_id,
        "mode": {
            "wbApiMode": settings.wb_api_mode,
            "realPriceApplyEnabled": settings.real_price_apply_enabled,
            "schedulerEnabled": settings.repricer_scheduler_enabled,
            "schedulerPollIntervalMinutes": scheduler_poll_interval_minutes,
            "executeIntervalMinutes": interval_minutes,
            "fullSyncEnabled": bool(getattr(settings, "repricer_wb_sync_enabled", False)),
            "fullSyncIntervalMinutes": full_sync_interval_minutes,
            "fullSyncPromotionsEnabled": bool(
                repricer_bff_module.ALGORITHM_SETTINGS_STATE.get("fullSyncPromotionsEnabled", True)
            ),
            "workerAutoApplyPricesEnabled": worker_auto_apply_prices,
        },
        "timing": {
            "serverNow": now.isoformat(),
            "lastSchedulerRunAt": last_scheduler_at.isoformat() if last_scheduler_at else None,
            "nextSchedulerPollAt": next_scheduler_poll_at.isoformat() if next_scheduler_poll_at else None,
            "secondsUntilNextSchedulerPoll": seconds_until_next_scheduler_poll,
            "nextRunAt": next_run_at.isoformat() if next_run_at else None,
            "secondsUntilNextRun": seconds_until_next,
            "lastFullSyncAt": last_full_sync_at.isoformat() if last_full_sync_at else None,
            "nextFullSyncAt": next_full_sync_at.isoformat() if next_full_sync_at else None,
            "secondsUntilNextFullSync": seconds_until_next_full_sync,
        },
        "runs": [_worker_run_payload(run) for run in runs],
        "pendingApprovals": [_pending_price_approval_payload(item) for item in pending_approvals],
        "sync": sync_status,
        "syncHistory": list_wb_sync_history(organization_id, hours=24, limit=100),
        "nightMedian": _night_median_worker_debug(organization_id, night_debug_rows),
    }


@router.get("/api/v1/wb-repricer/price-approvals/pending")
def get_pending_price_approvals(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    items = list_pending_price_approvals(organization_id=organization_id, limit=limit)
    return {"items": [_pending_price_approval_payload(item) for item in items], "total": len(items)}


@router.post("/api/v1/wb-repricer/price-approvals/bulk")
def decide_pending_price_approvals_bulk(
    request: Request,
    payload: PendingPriceApprovalBulkDecisionRequest,
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if payload.decision == "approve":
        _ensure_price_send_permission(actor)
    organization_id = _hydrate_org_repricer_state(request)
    approval_ids = list(dict.fromkeys(value.strip() for value in payload.approvalIds if value.strip()))
    if not approval_ids:
        raise HTTPException(status_code=422, detail="PENDING_PRICE_APPROVAL_IDS_REQUIRED")

    approvals = {
        str(item.get("approvalId")): item
        for item in list_pending_price_approvals(
            organization_id=organization_id,
            limit=100,
        )
    }
    pending: list[tuple[str, dict[str, Any]]] = []
    result_meta: dict[str, dict[str, Any]] = {}
    for approval_id in approval_ids:
        approval = approvals.get(approval_id)
        if approval is None:
            result_meta[approval_id] = {"ok": False, "error": "PENDING_PRICE_APPROVAL_NOT_FOUND"}
            continue
        status = str(approval.get("status") or "pending")
        if status != "pending":
            error = "PENDING_PRICE_APPROVAL_ALREADY_APPLYING" if status == "applying" else "PENDING_PRICE_APPROVAL_ALREADY_CLOSED"
            result_meta[approval_id] = {"ok": False, "error": error}
            continue
        pending.append((approval_id, approval))

    if payload.decision == "reject":
        from app.repricer_dispatch_store import claim_pending
        claimed = claim_pending(organization_id, [item[0] for item in pending], decision="rejected")
        for approval_id, _ in pending:
            if approval_id not in claimed:
                result_meta[approval_id] = {"ok": False, "error": "PENDING_PRICE_APPROVAL_ALREADY_CLOSED"}
        pending = [item for item in pending if item[0] in claimed]
        now = datetime.now(timezone.utc).isoformat()
        updated = update_pending_price_approvals(
            organization_id=organization_id,
            patches={
                approval_id: {
                    "status": "rejected",
                    "rejectedByActorId": actor.actor_id,
                    "rejectedAt": now,
                    "decisionReason": payload.reason,
                }
                for approval_id, _approval in pending
            },
        )
        for approval_id, _approval in pending:
            item = updated.get(approval_id)
            result_meta[approval_id] = (
                {"ok": True, "approval": _pending_price_approval_payload(item)}
                if item is not None
                else {"ok": False, "error": "PENDING_PRICE_APPROVAL_NOT_FOUND"}
            )
        results = [
            {"approvalId": approval_id, **result_meta[approval_id]}
            for approval_id in approval_ids
        ]
        return {
            "decision": payload.decision,
            "requested": len(approval_ids),
            "succeeded": sum(bool(item["ok"]) for item in results),
            "failed": sum(not bool(item["ok"]) for item in results),
            "results": results,
        }

    validated_by_scenario: dict[str, list[tuple[str, dict[str, Any], PriceDraftCreateRequest]]] = {}
    initial_patches: dict[str, dict[str, Any]] = {}
    for approval_id, approval in pending:
        try:
            draft_request = PriceDraftCreateRequest.model_validate(approval.get("draftRequest") or {})
        except ValidationError:
            error = "PENDING_PRICE_APPROVAL_PAYLOAD_INVALID"
            initial_patches[approval_id] = {"status": "failed", "error": error}
            result_meta[approval_id] = {"ok": False, "error": error}
            continue
        validated_by_scenario.setdefault(draft_request.scenario, []).append((approval_id, approval, draft_request))

    wb_token = _request_wb_token(request) if validated_by_scenario else None
    from app.repricer_dispatch_store import claim_pending
    batches: dict[str, list[dict[str, Any]]] = {}
    for scenario, candidates in validated_by_scenario.items():
        try:
            client = _build_repricing_client(scenario, wb_token)
            sku_rows = _list_repricer_skus_for_request(request, scenario, max_items=None)
        except Exception as exc:
            error = str(exc.detail) if isinstance(exc, HTTPException) else (str(exc) or exc.__class__.__name__)
            for approval_id, _approval, _draft_request in candidates:
                initial_patches[approval_id] = {"status": "failed", "applyState": "failed", "error": error}
                result_meta[approval_id] = {"ok": False, "error": error}
            continue
        rows_by_article = {
            str((row.get("meta") or {}).get("articleId") or ""): row
            for row in sku_rows
        }
        for approval_id, approval, draft_request in candidates:
            sku_row = rows_by_article.get(draft_request.articleId)
            try:
                draft = create_price_draft(
                    actor_id=actor.actor_id,
                    actor_role=actor.role,
                    client=client,
                    payload=draft_request,
                    sku_row=sku_row,
                    organization_id=organization_id,
                    pending_approval_id=approval_id,
                )
            except Exception as exc:
                error = str(exc.detail) if isinstance(exc, HTTPException) else (str(exc) or exc.__class__.__name__)
                initial_patches[approval_id] = {"status": "failed", "applyState": "failed", "error": error}
                result_meta[approval_id] = {"ok": False, "error": error}
                continue
            if draft.state == "blocked":
                error = "; ".join(draft.guardReport.blockers) or "PRICE_DRAFT_BLOCKED_BY_GUARDS"
                initial_patches[approval_id] = {
                    "status": "blocked",
                    "draftId": draft.draftId,
                    "blockedReasons": list(draft.guardReport.blockers),
                    "error": error,
                }
                result_meta[approval_id] = {
                    "ok": False,
                    "error": error,
                    "draft": draft.model_dump(mode="json"),
                    "job": None,
                }
                continue

            if approval_id not in claim_pending(organization_id, [approval_id], drafts={approval_id: draft.draftId}):
                result_meta[approval_id] = {"ok": False, "error": "PENDING_PRICE_APPROVAL_ALREADY_CLOSED"}
                continue
            approval_ref = f"worker-price-{approval_id}-{draft.draftId}"
            approved = approve_draft(
                draft_id=draft.draftId,
                actor_id=actor.actor_id,
                request=ApproveDraftRequest(approvalRef=approval_ref, reason=payload.reason),
            )
            initial_patches[approval_id] = {
                "status": "applying",
                "draftId": draft.draftId,
                "approvalRef": approval_ref,
                "approvedByActorId": actor.actor_id,
                "approvedAt": datetime.now(timezone.utc).isoformat(),
            }
            batches.setdefault(scenario, []).append(
                {
                    "approvalId": approval_id,
                    "approval": approval,
                    "draftRequest": draft_request,
                    "draft": draft,
                    "approved": approved,
                    "skuRow": sku_row,
                    "client": client,
                    "approvalRef": approval_ref,
                }
            )

    for patch in initial_patches.values():
        patch["_expectedStatus"] = "applying" if patch.get("status") == "applying" else "pending"
    updated_items = update_pending_price_approvals(
        organization_id=organization_id,
        patches=initial_patches,
    )
    final_patches: dict[str, dict[str, Any]] = {}
    local_commits: list[dict[str, Any]] = []
    settings = get_settings()
    for scenario, batch in batches.items():
        try:
            jobs = apply_approved_drafts(
                draft_ids=[item["draft"].draftId for item in batch],
                client=batch[0]["client"],
                real_apply_enabled=settings.real_price_apply_enabled,
                local_apply_enabled=settings.repricer_local_price_apply_enabled,
                request=ApplyDraftRequest(scenario=scenario),
            )
        except Exception as exc:
            logger.exception("Bulk price approval apply failed")
            error = str(exc.detail) if isinstance(exc, HTTPException) else (str(exc) or exc.__class__.__name__)
            for item in batch:
                approval_id = item["approvalId"]
                final_patches[approval_id] = {
                    "status": "failed",
                    "draftId": item["draft"].draftId,
                    "approvalRef": item["approvalRef"],
                    "applyState": "failed",
                    "error": error,
                }
                result_meta[approval_id] = {
                    "ok": False,
                    "error": error,
                    "draft": item["approved"].model_dump(mode="json"),
                    "job": None,
                }
            continue

        for item, job in zip(batch, jobs, strict=True):
            approval_id = item["approvalId"]
            approval = item["approval"]
            draft_request = item["draftRequest"]
            draft = item["draft"]
            apply_result = job.lastApplyResult
            apply_mode = getattr(apply_result, "applyMode", None) or job.state
            row_errors = [row.model_dump(mode="json") for row in job.rowErrors]
            error = "; ".join(
                [
                    *list(job.blockerIds),
                    *list(job.notes),
                    *[str(row.get("errorText") or "") for row in row_errors if row.get("errorText")],
                ]
            ) or None
            should_commit_local_price = job.state == "local_applied" or (job.state == "accepted" and apply_result.priceConfirmed)
            if should_commit_local_price:
                local_commits.append(
                    {
                        **item,
                        "source": str(
                            apply_mode
                            if job.state in {"accepted", "local_applied"}
                            else f"{apply_mode}_pending"
                        ),
                    }
                )
            final_patches[approval_id] = {
                "status": "applied" if job.state in {"accepted", "local_applied"} else job.state,
                "draftId": draft.draftId,
                "jobId": job.jobId,
                "applyState": job.state,
                "approvalRef": item["approvalRef"],
                "approvedByActorId": actor.actor_id,
                "approvedAt": datetime.now(timezone.utc).isoformat(),
                "decisionReason": payload.reason,
                "blockedReasons": list(job.blockerIds),
                "sourceStatus": job.sourceStatus,
                "wbMutationSent": apply_result.wbMutationSent,
                "wbUploadId": job.wbUploadId,
                "wbStatus": job.wbStatus,
                "statusLabel": apply_result.statusLabel,
                "notes": list(job.notes),
                "rowErrors": row_errors,
                "error": error,
            }
            ok = should_commit_local_price
            result_meta[approval_id] = {
                "ok": ok,
                "error": None if ok else error or f"PRICE_APPLY_{job.state.upper()}",
                "draft": item["approved"].model_dump(mode="json"),
                "job": job.model_dump(mode="json"),
            }

    if local_commits:
        # Simulation updates the explicit local override below, never WB facts.
        for item in local_commits:
            approval = item["approval"]
            draft_request = item["draftRequest"]
            draft = item["draft"]
            sku_row = item["skuRow"] or {}
            if str(approval.get("frontendStrategyId") or "") == "illiquid":
                _commit_illiquid_runtime_step(
                    draft_request.articleId,
                    int(draft.normalized.candidateSellerPriceKopecks),
                    str(approval.get("explanation") or draft_request.reason or ""),
                )
            meta = sku_row.get("meta") or {}
            strategy = sku_row.get("strategy") or {}
            record_repricer_price_change(
                article_id=draft_request.articleId,
                sku_name=str(meta.get("name") or draft_request.articleId),
                old_price_kopecks=int(
                    approval.get("oldPriceKopecks")
                    or draft.normalized.currentSellerPriceKopecks
                    or 0
                ),
                new_price_kopecks=int(draft.normalized.candidateSellerPriceKopecks),
                trigger="algorithm",
                strategy_name=str(
                    strategy.get("name")
                    or approval.get("strategyName")
                    or approval.get("frontendStrategyId")
                    or "worker"
                ),
                reason=str(approval.get("explanation") or draft_request.reason or ""),
                source=item["source"],
                organization_id=organization_id,
            )

    updated_items.update(
        update_pending_price_approvals(
            organization_id=organization_id,
            patches=final_patches,
        )
    )
    if final_patches:
        _flush_org_repricer_state(organization_id)
    results = []
    for approval_id in approval_ids:
        meta = result_meta[approval_id]
        approval = updated_items.get(approval_id) or approvals.get(approval_id)
        results.append(
            {
                "approvalId": approval_id,
                **meta,
                "approval": _pending_price_approval_payload(approval) if approval is not None else None,
            }
        )
    return {
        "decision": payload.decision,
        "requested": len(approval_ids),
        "succeeded": sum(bool(item["ok"]) for item in results),
        "failed": sum(not bool(item["ok"]) for item in results),
        "results": results,
    }


@router.post("/api/v1/wb-repricer/price-approvals/{approvalId}/reject")
def reject_pending_price_approval(
    request: Request,
    approvalId: str,
    payload: PendingPriceApprovalDecisionRequest | None = None,
) -> dict[str, Any]:
    actor = actor_from_request(request)
    organization_id = actor.organization_id
    approval = get_pending_price_approval(organization_id=organization_id, approval_id=approvalId)
    if approval is None:
        raise HTTPException(status_code=404, detail="PENDING_PRICE_APPROVAL_NOT_FOUND")
    approval_status = str(approval.get("status") or "pending")
    if approval_status == "applying":
        raise HTTPException(status_code=409, detail="PENDING_PRICE_APPROVAL_ALREADY_APPLYING")
    if approval_status != "pending":
        raise HTTPException(status_code=409, detail="PENDING_PRICE_APPROVAL_ALREADY_CLOSED")
    from app.repricer_dispatch_store import claim_pending
    if approvalId not in claim_pending(organization_id, [approvalId], decision="rejected"):
        raise HTTPException(status_code=409, detail="PENDING_PRICE_APPROVAL_ALREADY_CLOSED")
    updated = update_pending_price_approval(
        organization_id=organization_id,
        approval_id=approvalId,
        patch={
            "status": "rejected",
            "rejectedByActorId": actor.actor_id,
            "rejectedAt": datetime.now(timezone.utc).isoformat(),
            "decisionReason": payload.reason if payload else None,
        },
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="PENDING_PRICE_APPROVAL_NOT_FOUND")
    return {"approval": _pending_price_approval_payload(updated)}


@router.post("/api/v1/wb-repricer/price-approvals/{approvalId}/approve")
def approve_pending_price_approval(
    request: Request,
    approvalId: str,
    payload: PendingPriceApprovalDecisionRequest | None = None,
) -> dict[str, Any]:
    actor = actor_from_request(request)
    _ensure_price_send_permission(actor)
    organization_id = _hydrate_org_repricer_state(request)
    approval = get_pending_price_approval(organization_id=organization_id, approval_id=approvalId)
    if approval is None:
        raise HTTPException(status_code=404, detail="PENDING_PRICE_APPROVAL_NOT_FOUND")
    if str(approval.get("status") or "pending") != "pending":
        raise HTTPException(status_code=409, detail="PENDING_PRICE_APPROVAL_ALREADY_CLOSED")

    try:
        draft_request = PriceDraftCreateRequest.model_validate(approval.get("draftRequest") or {})
    except ValidationError as exc:
        update_pending_price_approval(
            organization_id=organization_id,
            approval_id=approvalId,
            patch={"status": "failed", "error": "PENDING_PRICE_APPROVAL_PAYLOAD_INVALID", "_expectedStatus": "pending"},
        )
        raise HTTPException(status_code=409, detail="PENDING_PRICE_APPROVAL_PAYLOAD_INVALID") from exc

    wb_token = _request_wb_token(request)
    client = _build_repricing_client(draft_request.scenario, wb_token)
    sku_rows = _list_repricer_skus_for_request(request, draft_request.scenario, max_items=None)
    sku_row = next(
        (
            row
            for row in sku_rows
            if str((row.get("meta") or {}).get("articleId") or "") == draft_request.articleId
        ),
        None,
    )
    draft = create_price_draft(
        actor_id=actor.actor_id,
        actor_role=actor.role,
        client=client,
        payload=draft_request,
        sku_row=sku_row,
        organization_id=organization_id,
        pending_approval_id=approvalId,
    )
    if draft.state == "blocked":
        updated = update_pending_price_approval(
            organization_id=organization_id,
            approval_id=approvalId,
            patch={
                "status": "blocked",
                "_expectedStatus": "pending",
                "draftId": draft.draftId,
                "blockedReasons": list(draft.guardReport.blockers),
            },
        )
        return {
            "approval": _pending_price_approval_payload(updated or approval),
            "draft": draft.model_dump(mode="json"),
            "job": None,
        }

    from app.repricer_dispatch_store import claim_pending
    if approvalId not in claim_pending(organization_id, [approvalId], drafts={approvalId: draft.draftId}):
        raise HTTPException(status_code=409, detail="PENDING_PRICE_APPROVAL_ALREADY_CLOSED")
    approval_ref = (payload.approvalRef if payload else None) or f"worker-price-{approvalId}-{draft.draftId}"
    update_pending_price_approval(
        organization_id=organization_id,
        approval_id=approvalId,
        patch={
            "status": "applying",
            "draftId": draft.draftId,
            "approvalRef": approval_ref,
            "approvedByActorId": actor.actor_id,
            "approvedAt": datetime.now(timezone.utc).isoformat(),
        },
    )
    approved = approve_draft(
        draft_id=draft.draftId,
        actor_id=actor.actor_id,
        request=ApproveDraftRequest(approvalRef=approval_ref, reason=payload.reason if payload else None),
    )
    settings = get_settings()
    try:
        job = apply_approved_draft(
            draft_id=draft.draftId,
            client=client,
            real_apply_enabled=settings.real_price_apply_enabled,
            local_apply_enabled=settings.repricer_local_price_apply_enabled,
            request=ApplyDraftRequest(scenario=draft_request.scenario),
        )
    except Exception as exc:
        update_pending_price_approval(
            organization_id=organization_id,
            approval_id=approvalId,
            patch={
                "status": "failed",
                "draftId": draft.draftId,
                "approvalRef": approval_ref,
                "applyState": "failed",
                "error": str(exc) or exc.__class__.__name__,
            },
        )
        raise
    apply_mode = getattr(job.lastApplyResult, "applyMode", None) or job.state
    final_status = "applied" if job.state in {"accepted", "local_applied"} else job.state
    apply_result = job.lastApplyResult
    row_errors = [row.model_dump(mode="json") for row in job.rowErrors]
    should_commit_local_price = job.state == "local_applied" or (job.state == "accepted" and apply_result.priceConfirmed)
    if should_commit_local_price:
        committed_source = str(apply_mode if job.state in {"accepted", "local_applied"} else f"{apply_mode}_pending")
        if str(approval.get("frontendStrategyId") or "") == "illiquid":
            _commit_illiquid_runtime_step(
                draft_request.articleId,
                int(draft.normalized.candidateSellerPriceKopecks),
                str(approval.get("explanation") or draft_request.reason or ""),
            )
        meta = (sku_row or {}).get("meta") or {}
        strategy = (sku_row or {}).get("strategy") or {}
        record_repricer_price_change(
            article_id=draft_request.articleId,
            sku_name=str(meta.get("name") or draft_request.articleId),
            old_price_kopecks=int(approval.get("oldPriceKopecks") or draft.normalized.currentSellerPriceKopecks or 0),
            new_price_kopecks=int(draft.normalized.candidateSellerPriceKopecks),
            trigger="algorithm",
            strategy_name=str(strategy.get("name") or approval.get("strategyName") or approval.get("frontendStrategyId") or "worker"),
            reason=str(approval.get("explanation") or draft_request.reason or ""),
            source=committed_source,
            organization_id=organization_id,
        )

    updated = update_pending_price_approval(
        organization_id=organization_id,
        approval_id=approvalId,
        patch={
            "status": final_status,
            "draftId": draft.draftId,
            "jobId": job.jobId,
            "applyState": job.state,
            "approvalRef": approval_ref,
            "approvedByActorId": actor.actor_id,
            "approvedAt": datetime.now(timezone.utc).isoformat(),
            "decisionReason": payload.reason if payload else None,
            "blockedReasons": list(job.blockerIds),
            "sourceStatus": job.sourceStatus,
            "wbMutationSent": apply_result.wbMutationSent,
            "wbUploadId": job.wbUploadId,
            "wbStatus": job.wbStatus,
            "statusLabel": apply_result.statusLabel,
            "notes": list(job.notes),
            "rowErrors": row_errors,
            "error": "; ".join([*list(job.blockerIds), *list(job.notes), *[str(row.get("errorText") or "") for row in row_errors if row.get("errorText")]]) or None,
        },
    )
    _flush_org_repricer_state(organization_id)
    return {
        "approval": _pending_price_approval_payload(updated or approval),
        "draft": approved.model_dump(mode="json"),
        "job": job.model_dump(mode="json"),
    }


@router.put("/api/v1/wb-repricer/simulator/sku/{articleId}")
def put_repricer_simulator_sku(
    request: Request,
    articleId: str,
    payload: RepricerSimulatorPatchRequest,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90, alias="periodDays"),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    rows = _list_repricer_skus_for_request(request, scenario, max_items=None, period_days=period_days)
    row = next((item for item in rows if item.get("meta", {}).get("articleId") == articleId), None)
    if row is None:
        raise HTTPException(status_code=404, detail="SKU_NOT_FOUND")
    nm_id = row.get("meta", {}).get("nmId")
    if nm_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SKU_HAS_NO_NM_ID",
                "message": "У SKU нет nmId WB. Обновите каталог WB перед сохранением входов симулятора.",
                "details": {"articleId": articleId},
            },
        )

    wb_token = _request_wb_token(request)
    _simulator_update_runtime_state(articleId, payload, scenario=scenario, wb_token=wb_token, sku_rows=rows)
    goods_updated = _simulator_update_goods_cache(organization_id, articleId, int(nm_id), payload)
    _simulator_update_source_caches(organization_id, int(nm_id), payload, period_days=period_days)
    _flush_org_repricer_state(organization_id)

    refreshed_rows = _list_repricer_skus_for_request(request, scenario, max_items=None, period_days=period_days)
    _patch_simulator_rows_inputs(refreshed_rows, articleId, payload)
    refreshed = next((item for item in refreshed_rows if item.get("meta", {}).get("articleId") == articleId), row)
    return {
        "articleId": articleId,
        "goodsCacheUpdated": goods_updated,
        "item": _simulator_item(refreshed),
        "dashboard": _simulator_dashboard_payload(refreshed_rows),
    }


@router.post("/api/v1/wb-repricer/simulator/run")
async def post_repricer_simulator_run(request: Request) -> dict[str, Any]:
    raw_payload = await _request_json_object(request)
    payload = _simulator_run_request_from_payload(raw_payload)
    settings = get_settings()
    real_apply_enabled = settings.real_price_apply_enabled
    try:
        actor = actor_from_request(request)
        organization_id = _hydrate_org_repricer_state(request)
        wb_token = _request_wb_token(request)
        client = _build_repricing_client(payload.scenario, wb_token)
        run_options = StrategyExecuteOptions(
            scenario=payload.scenario,
            createDrafts=not real_apply_enabled,
            autoApprove=not real_apply_enabled,
            applyPrices=payload.applyPrices and not real_apply_enabled,
            simulateLocalPrice=False,
            force=payload.force,
        )
        simulator_inputs: RepricerSimulatorPatchRequest | None = None
        if payload.inputs is not None and len(payload.articleIds) == 1:
            simulator_inputs = _simulator_patch_payload(payload.inputs)
            rows = _apply_simulator_inputs_for_article(
                request,
                organization_id=organization_id,
                wb_token=wb_token,
                scenario=payload.scenario,
                article_id=payload.articleIds[0],
                payload=simulator_inputs,
                period_days=payload.periodDays,
            )
        else:
            rows = _list_repricer_skus_for_request(request, payload.scenario, max_items=None, period_days=payload.periodDays)
        if payload.articleIds:
            report = execute_repricer_strategies(
                payload.articleIds,
                client=client,
                actor_id=actor.actor_id,
                actor_role=actor.role,
                wb_token=wb_token,
                options=run_options,
                sku_rows=rows,
                organization_id=organization_id,
                trigger="simulator_execute",
            )
        else:
            report = execute_all_assigned_skus(
                client=client,
                actor_id=actor.actor_id,
                actor_role=actor.role,
                wb_token=wb_token,
                options=run_options,
                sku_rows=rows,
                organization_id=organization_id,
                trigger="simulator_execute_assigned",
            )
        _flush_org_repricer_state(organization_id)
        refreshed_rows = _list_repricer_skus_for_request(request, payload.scenario, max_items=None, period_days=payload.periodDays)
        if simulator_inputs is not None and len(payload.articleIds) == 1:
            _patch_simulator_rows_inputs(refreshed_rows, payload.articleIds[0], simulator_inputs)
        return {
            "report": report.model_dump(mode="json"),
            "dashboard": _simulator_dashboard_payload(refreshed_rows),
            "mode": _simulator_mode_payload(),
            "runMode": "preview_only" if real_apply_enabled else "local_apply",
        }
    except HTTPException as exc:
        if exc.status_code == 422:
            logger.warning(
                "wb repricer simulator run returned 422 detail=%r articleIds=%r inputKeys=%r rawKeys=%r",
                exc.detail,
                payload.articleIds,
                sorted((payload.inputs or {}).keys()),
                sorted(raw_payload.keys()),
            )
        raise


@router.put("/api/v1/wb-repricer/templates")
def put_templates(payload: dict[str, Any]) -> dict[str, Any]:
    return put_repricer_templates(payload)


@router.post("/api/v1/wb-repricer/templates/{typeKey}/apply")
def post_apply_template(request: Request, typeKey: str, scenario: str = Query(default="complete")) -> dict[str, int]:
    return apply_repricer_template(typeKey, scenario, wb_token=_request_wb_token(request))


@router.get("/api/v1/wb-repricer/liquidation")
def get_liquidation(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=30, ge=1, le=90, alias="periodDays"),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    rows = _list_repricer_skus_for_request(
        request,
        scenario,
        include_promotions=True,
        include_content=True,
        period_days=period_days,
        max_items=None,
    )
    return get_repricer_liquidation(scenario, wb_token=_request_wb_token(request), sku_rows=rows)


@router.post("/api/v1/wb-repricer/liquidation/start")
def post_liquidation_start(
    request: Request,
    payload: LiquidationStartRequest,
    scenario: str = Query(default="complete"),
) -> dict[str, bool]:
    organization_id = _hydrate_org_repricer_state(request)
    rows = _list_repricer_skus_for_request(
        request,
        scenario,
        include_promotions=True,
        include_content=True,
        max_items=None,
    )
    result = start_repricer_liquidation(
        payload.articleIds,
        scenario,
        wb_token=_request_wb_token(request),
        sku_rows=rows,
        organization_id=organization_id,
    )
    _flush_org_repricer_state(organization_id)
    return result


@router.post("/api/v1/wb-repricer/liquidation/{articleId}/stop")
def post_liquidation_stop(request: Request, articleId: str) -> dict[str, bool]:
    organization_id = _hydrate_org_repricer_state(request)
    result = stop_repricer_liquidation(articleId, organization_id=organization_id)
    _flush_org_repricer_state(organization_id)
    return result


@router.post("/api/v1/wb-repricer/liquidation/{articleId}/run-step")
def post_liquidation_run_step(
    request: Request,
    articleId: str,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=7, ge=1, le=90, alias="periodDays"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    organization_id = _hydrate_org_repricer_state(request)
    active = repricer_bff_module.LIQUIDATION_ACTIVE.get(articleId)
    if active is None:
        raise HTTPException(status_code=404, detail="LIQUIDATION_NOT_ACTIVE")

    active["nextStepAt"] = datetime.now(timezone.utc).isoformat()
    wb_token = _request_wb_token(request)
    client = _build_repricing_client(scenario, wb_token)
    rows = _list_repricer_skus_for_request(
        request,
        scenario,
        include_promotions=True,
        include_content=True,
        period_days=period_days,
        max_items=None,
    )
    report = execute_repricer_strategies(
        [articleId],
        client=client,
        actor_id=actor.actor_id,
        actor_role=actor.role,
        wb_token=wb_token,
        options=StrategyExecuteOptions(
            scenario=scenario,
            createDrafts=True,
            autoApprove=False,
            applyPrices=False,
            simulateLocalPrice=False,
        ),
        sku_rows=rows,
        organization_id=organization_id,
        trigger="manual_liquidation_step",
    )
    _flush_org_repricer_state(organization_id)
    return report.model_dump(mode="json")


@router.post("/api/v1/wb-repricer/liquidation/{articleId}/confirm-negative")
def post_confirm_negative(request: Request, articleId: str) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    result = confirm_repricer_negative_margin(articleId, organization_id=organization_id)
    _flush_org_repricer_state(organization_id)
    return result


@router.get("/api/v1/wb-repricer/algorithm")
def get_algorithm(request: Request) -> dict[str, Any]:
    _hydrate_org_repricer_state(request)
    return get_repricer_algorithm()


class QuarterlyTaxRequest(BaseModel):
    year: int = Field(ge=1900, le=9998, strict=True)
    rates: list[Any] = Field(min_length=4, max_length=4)
    revision: str = Field(min_length=64, max_length=64)


@router.get("/api/v1/wb-repricer/quarterly-tax")
def get_quarterly_tax(request: Request, year: int = Query(ge=1900, le=9998),
                      session: Session = Depends(get_db_session)) -> dict[str, Any]:
    from app.platform.economics.wb_quarterly_tax import settings_in_session, year_view
    actor = actor_from_request(request)
    if not has_permission(actor, "settings:read"):
        raise HTTPException(403, "NO_ACCESS:settings:read")
    try:
        return year_view(settings_in_session(session, actor.organization_id), year)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/v1/wb-repricer/quarterly-tax")
def put_quarterly_tax(request: Request, payload: QuarterlyTaxRequest,
                      session: Session = Depends(get_db_session)) -> dict[str, Any]:
    from app.platform.economics.wb_quarterly_tax import save_year, QuarterlyTaxConflict
    actor = actor_from_request(request)
    if not has_permission(actor, "settings:write"):
        raise HTTPException(403, "NO_ACCESS:settings:write")
    try:
        return save_year(session, actor.organization_id, year=payload.year,
                         rates=payload.rates, expected_revision=payload.revision)
    except QuarterlyTaxConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/v1/wb-repricer/algorithm")
def put_algorithm(
    request: Request,
    payload: dict[str, Any],
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    # Quarterly settings have their own scoped, validated optimistic write path.
    # Legacy clients may echo this field but cannot replace it here.
    payload = {key: value for key, value in payload.items() if key != "wbQuarterlyTax"}
    organization_id = _hydrate_org_repricer_state(request)
    tax_confirmation = {}
    if "taxPct" in payload:
        previous = get_repricer_algorithm()
        try:
            previous_tax = _legacy_values(previous)[0]
            updated_tax = _legacy_values({**previous, **payload})[0]
        except EconomicsValidationError as exc:
            _raise_invalid_economics(exc)
        if updated_tax != previous_tax:
            tax_confirmation = {
                "tax_value_state": "configured",
                "tax_evidence_status": "dated",
            }
    response = put_repricer_algorithm(payload)
    if not _flush_org_repricer_state(organization_id):
        raise HTTPException(status_code=500, detail="ALGORITHM_SETTINGS_NOT_SAVED")
    if _ALGORITHM_ECONOMICS_FIELDS.intersection(payload):
        effective_from = _parse_utc_datetime(response.get("savedAt"))
        if effective_from is None:
            raise HTTPException(status_code=500, detail="ALGORITHM_SETTINGS_TIMESTAMP_MISSING")
        try:
            EconomicsService(session, organization_id).reconcile_legacy_organization(
                response,
                effective_from=effective_from,
                source_reference=_economics_source_reference(
                    "algorithm", effective_from.isoformat()
                ),
                **tax_confirmation,
            )
        except EconomicsValidationError as exc:
            _raise_invalid_economics(exc)
    return response


@router.get("/api/v1/wb-repricer/promotions")
def get_promotions(request: Request, scenario: str = Query(default="complete"), refresh: bool = Query(default=False)) -> list[dict[str, Any]]:
    actor, wb_token = _request_actor_and_wb_token(request)
    thresholds_cache = _promotion_thresholds_cache(actor.organization_id)
    # Page summary must not replace the SKU membership cache used elsewhere.
    cache_key = "promotions_listing"
    cached = get_source_cache(actor.organization_id, cache_key) or {}
    cached_promotions = cached.get("promotions")
    fetched_at = _parse_utc_datetime(get_source_cache_fetched_at(actor.organization_id, cache_key))
    fresh = fetched_at is not None and 0 <= (datetime.now(timezone.utc) - fetched_at).total_seconds() < 300
    if not refresh and fresh and isinstance(cached_promotions, list):
        promotions = _apply_promotion_threshold_cache(cached_promotions, thresholds_cache)
        return [_strip_internal_promotion_fields(item) for item in promotions]
    promotions = list_promotions(scenario, wb_token=wb_token, summary_only=True)
    promotions = _apply_promotion_threshold_cache(promotions, thresholds_cache)
    save_source_cache(actor.organization_id, cache_key, {"promotions": _promotions_for_cache(promotions), "count": len(promotions)})
    return [_strip_internal_promotion_fields(item) for item in promotions]


@router.get("/api/v1/wb-repricer/promotions/{promoId}/skus")
def get_promotions_skus(request: Request, promoId: str, scenario: str = Query(default="complete")) -> list[dict[str, Any]]:
    actor, wb_token = _request_actor_and_wb_token(request)
    thresholds_cache = _promotion_thresholds_cache(actor.organization_id)
    imported = (thresholds_cache.get("byPromotionId") or {}).get(str(promoId))
    if isinstance(imported, dict) and isinstance(imported.get("thresholds"), list):
        sku_rows = _list_repricer_skus_for_request(request, scenario, include_promotions=True)
        by_nm = {
            int(row["meta"]["nmId"]): row
            for row in sku_rows
            if row.get("meta", {}).get("nmId") is not None
        }
        by_article = {str(row["meta"]["articleId"]): row for row in sku_rows}
        rows: list[dict[str, Any]] = []
        for threshold in imported.get("thresholds") or []:
            nm_id = int(threshold.get("nmId") or 0)
            article_id = str(threshold.get("vendorCode") or "")
            sku = by_nm.get(nm_id) or by_article.get(article_id)
            rows.append(
                {
                    "articleId": sku["meta"]["articleId"] if sku else article_id,
                    "nmId": nm_id,
                    "name": sku["meta"]["name"] if sku else article_id or str(nm_id),
                    "currentPriceKopecks": threshold.get("currentPriceKopecks")
                    or (sku["meta"]["currentPriceKopecks"] if sku else None),
                    "promoThresholdKopecks": threshold.get("promoThresholdKopecks"),
                    "promoThresholdDiscountPct": threshold.get("promoThresholdDiscountPct"),
                    "isProtected": bool(threshold.get("promoThresholdKopecks")),
                    "cogsKopecks": sku["settings"]["cogsKopecks"] if sku else 0,
                    "wbCommissionPct": sku["settings"]["wbCommissionPct"] if sku else 0,
                    "logisticsKopecks": sku["settings"]["logisticsKopecks"] if sku else 0,
                }
            )
        return rows
    try:
        return get_promotion_skus(promoId, scenario, wb_token=wb_token)
    except HTTPException as exc:
        if exc.status_code in {400, 409, 429, 500, 502, 503, 504}:
            return []
        raise


@router.post("/api/v1/wb-repricer/promotions/{promoId}/upload-excel")
async def post_promotion_excel(promoId: str, request: Request, scenario: str = Query(default="complete")) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    files = await _extract_uploaded_files(request)
    if not files:
        raise HTTPException(status_code=422, detail="PROMOTION_EXCEL_FILE_REQUIRED")
    filename, content = files[0]
    parsed = parse_promotion_excel(content, filename)
    cached = get_source_cache(actor.organization_id, "promotions") or {}
    promotions = cached.get("promotions") if isinstance(cached.get("promotions"), list) else []
    if not promotions:
        promotions = list_promotions(scenario, wb_token=wb_token, fail_open=True)
    if not any(str(item.get("id")) == str(promoId) for item in promotions):
        legacy = upload_promotion_excel(promoId, filename, scenario, wb_token=wb_token)
        promotions.append(legacy)
    promotion, _cache = _save_promotion_import(
        actor.organization_id,
        promotion_id=promoId,
        parsed=parsed,
        promotions=promotions,
    )
    return _strip_internal_promotion_fields(promotion)


@router.post("/api/v1/wb-repricer/promotions/upload-excel")
async def post_promotions_excel_bulk(request: Request, scenario: str = Query(default="complete")) -> dict[str, Any]:
    actor, wb_token = _request_actor_and_wb_token(request)
    files = await _extract_uploaded_files(request)
    if not files:
        raise HTTPException(status_code=422, detail="PROMOTION_EXCEL_FILE_REQUIRED")
    cached = get_source_cache(actor.organization_id, "promotions") or {}
    promotions = cached.get("promotions") if isinstance(cached.get("promotions"), list) else []
    if not promotions:
        promotions = list_promotions(scenario, wb_token=wb_token, fail_open=True)
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for filename, content in files:
        parsed = parse_promotion_excel(content, filename)
        promotion_id = _find_promotion_for_file(promotions, filename)
        if promotion_id is None:
            if parsed.status == "error":
                unmatched.append(
                    {
                        "filename": filename,
                        "normalizedFilename": normalized_filename(filename),
                        "rowsParsed": parsed.rows_parsed,
                        "errorText": parsed.error_text,
                    }
                )
                continue
            imported_promotion = _imported_promotion_from_excel(parsed)
            promotion_id = str(imported_promotion["id"])
            promotions.append(imported_promotion)
        promotion, _cache = _save_promotion_import(
            actor.organization_id,
            promotion_id=promotion_id,
            parsed=parsed,
            promotions=promotions,
        )
        promotions = _apply_promotion_threshold_cache(promotions, _promotion_thresholds_cache(actor.organization_id))
        payload = {
            "filename": filename,
            "promotionId": promotion_id,
            "promotionName": promotion.get("name"),
            "rowsParsed": parsed.rows_parsed,
            "status": parsed.status,
            "errorText": parsed.error_text,
        }
        if parsed.status == "error":
            errors.append(payload)
        else:
            matched.append(payload)
    return {
        "matched": matched,
        "unmatched": unmatched,
        "errors": errors,
        "matchedCount": len(matched),
        "unmatchedCount": len(unmatched),
        "errorCount": len(errors),
    }


@router.get("/api/v1/wb-repricer/changelog")
def get_changelog(
    request: Request,
    articleId: str | None = Query(default=None),
    trigger: list[str] = Query(default_factory=list),
    from_: str | None = Query(default=None, alias="from"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    return list_repricer_changelog(
        article_id=articleId,
        triggers=trigger,
        from_iso=from_,
        page=page,
        limit=limit,
        organization_id=organization_id,
    )


@router.get("/api/v1/wb-repricer/sku/{articleId}/changelog")
def get_sku_changelog(
    request: Request,
    articleId: str,
    trigger: list[str] = Query(default_factory=list),
    from_: str | None = Query(default=None, alias="from"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    return list_repricer_changelog(
        article_id=articleId,
        triggers=trigger,
        from_iso=from_,
        page=page,
        limit=limit,
        organization_id=organization_id,
    )


def _work_status_changed_recently(item: dict[str, Any], since: datetime) -> bool:
    for event in item.get("recentChanges") or []:
        if not isinstance(event, dict):
            continue
        timestamp = event.get("timestamp")
        if not timestamp:
            continue
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= since:
            return True
    return False


@router.get("/api/v1/wb-repricer/work-status")
def get_work_status(
    request: Request,
    scenario: str = Query(default="complete"),
    period_days: int = Query(default=7, alias="periodDays", ge=1, le=90),
    limit: int = Query(default=120, ge=1, le=300),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    rows = _list_repricer_skus_for_request(
        request,
        scenario,
        include_promotions=True,
        include_content=True,
        period_days=period_days,
        max_items=None,
    )
    active_liquidation_ids = {str(article_id) for article_id in repricer_bff_module.LIQUIDATION_ACTIVE}

    def is_liquidation_row(row: dict[str, Any]) -> bool:
        article_id = str(row.get("meta", {}).get("articleId") or "")
        status = str(row.get("meta", {}).get("status") or "")
        return article_id in active_liquidation_ids or status == "liquidation"

    liquidation_rows = [row for row in rows if is_liquidation_row(row)]
    other_rows = [row for row in rows if not is_liquidation_row(row)]
    selected_rows = liquidation_rows + other_rows[: max(0, limit - len(liquidation_rows))]
    wb_token = _request_wb_token(request)
    execution_runs = list_execution_runs(organization_id=organization_id, limit=50)
    latest_executions = _latest_execution_by_article_id(execution_runs)
    items = [
        {
            **get_repricer_pricing_status(
                str(row.get("meta", {}).get("articleId") or ""),
                scenario,
                wb_token=wb_token,
                sku_rows=rows,
                organization_id=organization_id,
            ),
            "lastExecution": latest_executions.get(str(row.get("meta", {}).get("articleId") or "")),
        }
        for row in selected_rows
        if row.get("meta", {}).get("articleId")
    ]
    selected_article_ids = {str(row.get("meta", {}).get("articleId") or "") for row in selected_rows}
    for article_id in sorted(active_liquidation_ids - selected_article_ids):
        active = repricer_bff_module.LIQUIDATION_ACTIVE.get(article_id) or {}
        changelog = list_repricer_changelog(
            article_id=article_id,
            page=1,
            limit=10,
            organization_id=organization_id,
        )
        events = changelog["items"]
        last_event = events[0] if events else None
        current_price = int(active.get("currentPriceKopecks") or active.get("startPriceKopecks") or 0)
        target_price = int(active.get("targetPriceKopecks") or 0)
        items.insert(
            0,
            {
                "articleId": article_id,
                "skuName": active.get("name") or article_id,
                "stage": "active_liquidation",
                "status": "liquidation",
                "strategy": {
                    "id": "illiquid",
                    "name": "Неликвид",
                    "type": "liquidation",
                    "typedStrategyId": None,
                    "assignmentSource": "runtime",
                    "assignedAt": active.get("startedAt"),
                    "rules": [
                        {"label": "0 заказов", "value": f"-{active.get('stepPct') or 0}%"},
                        {"label": "1 заказ", "value": "держим цену"},
                        {"label": ">1 заказа", "value": f"+{active.get('stepPct') or 0}%"},
                    ],
                },
                "progress": {
                    "kind": "liquidation",
                    "currentDay": 1,
                    "totalDays": 1,
                    "startedAt": active.get("startedAt"),
                    "nextStepAt": active.get("nextStepAt"),
                    "stepPct": float(active.get("stepPct") or 0),
                    "startPriceKopecks": int(active.get("startPriceKopecks") or current_price),
                    "currentPriceKopecks": current_price,
                    "targetPriceKopecks": target_price,
                    "requiresNegativeMarginConfirm": bool(active.get("requiresNegativeMarginConfirm")),
                },
                "current": {
                    "priceKopecks": current_price,
                    "pMinKopecks": target_price,
                    "pMaxKopecks": int(active.get("startPriceKopecks") or current_price),
                    "marginPct": None,
                    "basketsLast7d": 0,
                    "basketNorm": 0,
                    "ordersUnits": None,
                    "stockUnits": None,
                },
                "lastDecision": {
                    "timestamp": last_event.get("timestamp") if isinstance(last_event, dict) else active.get("startedAt"),
                    "trigger": last_event.get("trigger") if isinstance(last_event, dict) else "liquidation",
                    "reason": last_event.get("reason") if isinstance(last_event, dict) else "Ликвидация активна в runtime, но SKU не найден в текущей WB-выборке",
                    "oldPriceKopecks": last_event.get("oldPriceKopecks") if isinstance(last_event, dict) else int(active.get("startPriceKopecks") or current_price or 1),
                    "newPriceKopecks": last_event.get("newPriceKopecks") if isinstance(last_event, dict) else max(1, current_price),
                    "changePct": last_event.get("changePct") if isinstance(last_event, dict) else None,
                },
                "recentChanges": events,
                "changelogTotal": changelog["total"],
                "lastExecution": latest_executions.get(article_id),
            },
        )
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    summary = {
        "total": len(items),
        "activeLiquidation": sum(1 for item in items if item.get("stage") == "active_liquidation"),
        "liquidationPending": sum(1 for item in items if item.get("stage") == "liquidation_pending"),
        "warmup": sum(1 for item in items if item.get("stage") == "warmup"),
        "manualHold": sum(1 for item in items if item.get("stage") == "manual_hold"),
        "activePricing": sum(1 for item in items if item.get("stage") == "active_pricing"),
        "changed24h": sum(1 for item in items if _work_status_changed_recently(item, since)),
        "withoutChangelog": sum(1 for item in items if not item.get("changelogTotal")),
        "failedExecutions": sum(
            1
            for item in items
            if (item.get("lastExecution") or {}).get("status") in {"blocked", "failed", "skipped"}
        ),
        "negativeMarginConfirmRequired": sum(
            1
            for item in items
            if (item.get("progress") or {}).get("kind") == "liquidation"
            and (item.get("progress") or {}).get("requiresNegativeMarginConfirm")
        ),
    }
    return {
        "items": items,
        "summary": summary,
        "pendingApprovals": [_pending_price_approval_payload(item) for item in list_pending_price_approvals(organization_id=organization_id, limit=20)],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "periodDays": period_days,
        "limit": limit,
    }


@router.get("/api/v1/wb-repricer/sku/{articleId}/pricing-status")
def get_sku_pricing_status(
    request: Request,
    articleId: str,
    scenario: str = Query(default="complete"),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    rows = _list_repricer_skus_for_request(request, scenario, max_items=None)
    return get_repricer_pricing_status(
        articleId,
        scenario,
        wb_token=_request_wb_token(request),
        sku_rows=rows,
        organization_id=organization_id,
    )


@router.get("/api/v1/wb-repricer/sku/{articleId}/timeseries")
def get_sku_timeseries(
    request: Request,
    articleId: str,
    scenario: str = Query(default="complete"),
    days: int = Query(default=30, ge=1, le=90),
    include_raw: bool = Query(default=False, alias="includeRaw"),
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
) -> dict[str, Any]:
    organization_id = _hydrate_org_repricer_state(request)
    rows = _list_repricer_skus_for_request(request, scenario, max_items=None, period_days=days)
    return get_repricer_sku_timeseries(
        articleId,
        scenario,
        wb_token=_request_wb_token(request),
        sku_rows=rows,
        organization_id=organization_id,
        days=days,
        include_raw=include_raw,
        date_from=date_from,
        date_to=date_to,
        finance_allowed=organization_id is not None and has_permission(actor_from_request(request), "finance:read"),
    )
