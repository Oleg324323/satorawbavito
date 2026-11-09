from __future__ import annotations

import time
import re
from math import ceil, isfinite
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery.exceptions import Ignore, Retry
from fastapi import HTTPException

from app import repricer_bff as repricer_bff_module
from app.avito.auth import resolve_user_avito_access_token
from app.avito.listings import AvitoListingsFetchRequest, build_avito_listings_client
from app.avito.price_apply import LiveAvitoPriceClient
from app.cabinet.store import get_organization_avito_credentials_secret, get_organization_wb_token_secret, get_user_wb_token_secret
from app.config import get_settings
from app.infra.celery_app import REPRICER_SCHEDULER_POLL_MINUTES, celery_app
from app.infra.db import get_session_factory
from app.platform.advertising.raw_backfill import _connected_wb_account_id
from app.platform.economics.backfill import LegacyCostSnapshot, apply_cost_backfill
from app.platform.economics.wb_quarterly_tax import tax_consistent_report
from app.repricer_cache.store import (
    cached_goods_meta,
    finance_cache_uses_current_revenue_basis,
    get_source_cache,
    list_cached_goods,
    list_source_cache_ranges_by_prefix,
    save_source_cache,
)
from app.repricer_execution import StrategyExecuteOptions, execute_all_assigned_skus
from app.repricer_persistence.store import (
    append_execution_run,
    flush_repricer_bff_state,
    hydrate_repricer_bff_state,
    list_execution_runs,
    list_organization_ids,
    load_runtime_state,
)
from app.repricer_sync import abandon_stale_wb_sync, get_wb_sync_status, is_wb_sync_running, list_wb_sync_history, record_wb_sync_history_event, refresh_wb_data_sources
from app.repricer_bff import list_repricer_skus
from app.routers.avito_repricer import (
    AVITO_REPRICER_PENDING_APPROVALS_KEY,
    _row_payload as avito_repricer_row_payload,
    append_avito_repricer_price_history,
    load_avito_repricer_settings,
    load_avito_repricer_strategy_assignments,
)
from app.wb_api.client import RateLimitedWbApiClient, build_wb_client
from app.wb_sync_plan import WbSyncProfile, historical_sync_as_of, nightly_baskets_detail_profiles, nightly_reconciliation_profile, nightly_sync_window_state, onboarding_sync_profiles, periodic_sync_profiles


REPORT_SOURCE_REFRESH_PLANS: dict[str, dict[str, Any]] = {
    "stats": {"sources": ("period-stats", "finance", "ads", "baskets"), "baskets_include_daily_detail": False},
    "digest": {"sources": ("period-stats", "finance", "ads", "baskets"), "baskets_include_daily_detail": True},
    "abc": {"sources": ("period-stats", "finance", "ads", "baskets"), "baskets_include_daily_detail": False},
    "rnp": {"sources": ("finance", "baskets", "ads"), "baskets_include_daily_detail": False},
    "pnl": {"sources": ("finance", "ads"), "baskets_include_daily_detail": False},
    "ads": {"sources": ("ads",), "baskets_include_daily_detail": False},
    "stock": {"sources": ("stocks", "period-stats", "finance"), "baskets_include_daily_detail": False},
    "week-over-week": {"sources": ("stocks", "period-stats", "finance", "ads", "baskets"), "baskets_include_daily_detail": False},
}

NIGHTLY_BASKETS_DETAIL_PAUSE_SECONDS = 120


@celery_app.task(name="reports.refresh_sales_funnel", bind=True, max_retries=80)
def refresh_sales_funnel(self, organization_id, account, scope, start, end, job_key, lock_key, owner, native_comparison=False, aggregate_comparison=False):
    from app.infra.redis_client import get_redis_client
    from app.wb_api.report_reads import ReportDemandEnded
    from app.wb_sales_funnel import collect
    redis = get_redis_client()
    if redis.get(lock_key) not in {owner, owner.encode()}:
        return  # A redelivered/expired task must not resurrect an old load.
    collection = redis.lock(f"wb:funnel:collect:{organization_id}:{scope}", timeout=1200)
    if not collection.acquire(blocking=False):
        redis.expire(lock_key, 1200)
        raise self.retry(countdown=15)
    def heartbeat():
        collection.extend(1200, replace_ttl=True)
        redis.expire(lock_key, 1200)
    rescheduled = False
    try:
        collect(organization_id, account, scope, start, end, job_key, heartbeat=heartbeat,
                **({"native_comparison": True} if native_comparison else {}),
                **({"aggregate_comparison": True} if aggregate_comparison else {}))
    except ReportDemandEnded:
        save_source_cache(organization_id, job_key, {"status": "paused", "message": "Загрузка приостановлена: период больше не открыт."}, strict=True)
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        if code == 429 and self.request.retries < self.max_retries:
            delay = repricer_bff_module._retry_after_from_http_exception(exc) or 60
            delay = max(15, int(delay) + 1)
            save_source_cache(organization_id, job_key, {"status": "queued", "message": "Ожидаем лимит WB; продолжим автоматически.", "retryAfterSeconds": delay}, strict=True)
            heartbeat()
            redis.expire(lock_key, max(1200, delay + 120))
            rescheduled = True
            raise self.retry(countdown=delay) from None
        message = ("WB ограничил запросы. Повторите позже; сохранённые данные не потеряны." if code == 429 else
                   "WB отклонил доступ к воронке. Проверьте права токена." if code in {401, 403} else
                   "Не удалось загрузить все данные WB. Сохранённая часть доступна; повторите загрузку.")
        if str(exc) == "WB_FUNNEL_COMPARISON_PERIOD_MISMATCH":
            message = "WB вернул другой период сравнения. Готовая динамика за выбранную пару недоступна."
        elif str(exc) == "WB_FUNNEL_SELECTED_PERIOD_PENDING":
            message = "Сначала завершите обновление выбранного периода. Его сохранённые значения доступны."
        save_source_cache(organization_id, job_key, {"status": "failed", "message": message}, strict=True)
    finally:
        collection.release()
        if not rescheduled:
            redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, lock_key, owner)


def _enqueue_report_budget_step(org, user, digest, owner, cursor, delay):
    refresh_report_budget_step.apply_async(args=(org, user, digest, owner, cursor), countdown=delay)


@celery_app.task(name="reports.refresh_budget_step", max_retries=0)
def refresh_report_budget_step(organization_id, user_id, digest, owner, cursor):
    from app.wb_report_budgets import run_step
    return run_step(organization_id, user_id, digest, owner, cursor,
                    _report_refresh_wb_token, _enqueue_report_budget_step)


def _report_source_refresh_plan(report_id: str) -> dict[str, Any]:
    plan = REPORT_SOURCE_REFRESH_PLANS.get(report_id)
    if plan is None:
        return {"sources": (), "baskets_include_daily_detail": False}
    return {
        "sources": tuple(plan["sources"]),
        "baskets_include_daily_detail": bool(plan.get("baskets_include_daily_detail")),
    }


def _digest_report_summary(organization_id: int, date_from: date, date_to: date) -> dict[str, Any]:
    from app.routers import wb_repricer_bff as repricer_router

    period_suffix = f"{date_from.isoformat()}_{date_to.isoformat()}"
    snapshot = repricer_router._load_repricer_sku_snapshot(
        organization_id,
        "complete",
        period_suffix=period_suffix,
        include_promotions=False,
        include_content=False,
    )
    snapshot_summary = dict(snapshot.get("summary") or {}) if snapshot and isinstance(snapshot.get("summary"), dict) else {}
    settings_overrides: dict[str, dict[str, Any]] = {}
    if snapshot:
        loaded = repricer_router._load_repricer_sku_snapshot_page(
            organization_id,
            "complete",
            period_suffix=period_suffix,
            include_promotions=False,
            include_content=False,
            page=1,
            page_size=max(1, int(snapshot.get("total") or 1)),
        )
        for row in loaded[1] if loaded else []:
            article_id = str((row.get("meta") or {}).get("articleId") or "").strip()
            settings = row.get("settings")
            if article_id and isinstance(settings, dict):
                settings_overrides[article_id] = settings
    range_start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
    range_end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=timezone.utc)
    period_days = (date_to - date_from).days + 1
    cache_memo: dict[tuple[Any, ...], dict[str, Any]] = {}
    finance_cache = repricer_router._period_source_cache(
        organization_id,
        "finance",
        period_suffix,
        period_days,
        range_start,
        range_end,
        slim=True,
        require_full_sync_coverage=False,
        prefer_freshest_covering=True,
        memo=cache_memo,
    )
    summary = repricer_router._repricer_list_summary_from_source_caches(
        organization_id,
        resolved_period_days=period_days,
        period_suffix=period_suffix,
        range_start=range_start,
        range_end=range_end,
        finance_diagnostics=repricer_router._limited_finance_diagnostics(finance_cache, limit=1) if finance_cache else None,
        require_full_sync_coverage=False,
        prefer_freshest_finance=True,
        settings_overrides=settings_overrides,
        period_cache_memo=cache_memo,
    )
    source_fields = (
        "ordersUnits",
        "salesUnits",
        "returnsUnits",
        "sellerRevenueKopecks",
        "buyerRevenueKopecks",
        "expensesKopecks",
        "adSpendKopecks",
    )
    if snapshot_summary and all(snapshot_summary.get(field) == summary.get(field) for field in source_fields):
        summary = snapshot_summary
    # Repricer unit-margin × orders is not realized financial report profit.
    from app.wb_reports_sprint_d import _get_cached_pnl_response, _pnl_report_cache_key, _period_cache, _complete_finance_ledger
    finance = _period_cache(organization_id, "finance", date_from, date_to)
    summary = {**summary, "marginKopecks": None}
    if _complete_finance_ledger(finance, date_from, date_to):
        key = _pnl_report_cache_key(date_from=date_from, date_to=date_to, group_by="sku",
            requested_state="preliminary", finance_allowed=True)
        pnl = _get_cached_pnl_response(organization_id=organization_id, cache_key=key)
        if pnl is not None and pnl.sourceStatus != "stale":
            summary["marginKopecks"] = pnl.totals.netProfitKopecks
            summary["exampleCosts"] = "WB_USER_EXAMPLE_COSTS" in pnl.blockerIds
    return summary


@celery_app.task(name="reports.build_digest_for_org", bind=True, max_retries=0)
def build_digest_for_org(self, organization_id: int, date_from_iso: str, date_to_iso: str, finance_allowed: bool, wb_token: str | None, *, source_refresh: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one exact digest range outside the request/response lifecycle."""
    from datetime import date as date_type
    from app.routers import wb_reports_bff as reports

    date_from = date_type.fromisoformat(date_from_iso)
    date_to = date_type.fromisoformat(date_to_iso)
    date_range = {"preset": "custom", "from": date_from_iso, "to": date_to_iso}
    job_key = reports._digest_job_cache_key(date_from, date_to)
    refresh_context = {"kind": "report_source_refresh", "sync": source_refresh} if source_refresh is not None else {}
    started_at = reports._utc_now_iso()
    def update_progress(stage: str, label: str, percent: int) -> None:
        reports.save_source_cache(organization_id, job_key, {**refresh_context, "state": "running", "taskId": self.request.id, "dateFrom": date_from_iso, "dateTo": date_to_iso, "startedAt": started_at, "stage": stage, "label": label, "percent": percent, "updatedAt": reports._utc_now_iso()})
    update_progress("queued", "Задача принята, ждём worker", 0)
    try:
        source_revision = reports._digest_source_revision(organization_id, date_from, date_to)
        wb_token = None
        update_progress("cache-snapshot", "Читаем WB sync cache для дайджеста", 8)
        update_progress("cache-snapshot", "Собираем дайджест из WB cache", 75)
        source_snapshot = reports.build_cached_wb_reports_sources_snapshot(
            organization_id=organization_id,
            date_from=date_from,
            date_to=date_to,
        )
        update_progress("ads", "Загружаем рекламу WB и собираем дайджест", 80)
        ads_snapshot = reports._build_digest_ads_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to, wb_token=wb_token)
        update_progress("funnel", "Загружаем воронку WB для дайджеста", 82)
        funnel_snapshot = reports._build_digest_funnel_snapshot(
            organization_id=organization_id,
            date_from=date_from,
            date_to=date_to,
            wb_token=wb_token,
            progress_callback=update_progress,
        )
        plan_fact = reports.build_plan_fact_report(date_from=date_from, date_to=date_to, dimension="manager", finance_allowed=finance_allowed)
        digest = reports._build_digest_payload(
            date_range,
            source_snapshot,
            ads_snapshot,
            plan_fact,
            funnel_snapshot,
            _digest_report_summary(organization_id, date_from, date_to),
        )
        plan = reports.get_source_cache(organization_id, reports._digest_plan_cache_key(date_to.strftime("%Y-%m")), slim=False) or {}
        if isinstance(plan, dict) and plan:
            reports._apply_digest_plan(digest, plan)
        source_from, source_to = reports._digest_source_bounds(date_from, date_to)
        ready, missing = reports._report_daily_sources_ready(organization_id, reports.REPORT_DAILY_SOURCES_BY_ID["digest"], date_from=source_from, date_to=source_to)
        cached = {"digest": digest, "dateFrom": date_from_iso, "dateTo": date_to_iso, "completedAt": reports._utc_now_iso(),
            "sourceComplete": ready, "sourceRevision": source_revision}
        reports.save_source_cache(organization_id, reports._digest_cache_key(date_from, date_to), cached)
        reports.save_source_cache(organization_id, "reports_digest_latest", cached)
        result = {**refresh_context, "state": "completed", "taskId": self.request.id, "dateFrom": date_from_iso, "dateTo": date_to_iso, "stage": "completed", "label": "Воронка продаж готова", "percent": 100, "finishedAt": reports._utc_now_iso()}
        if not ready:
            result.update(state="failed", stage="partial", label="Воронка сохранена частично",
                error=f"Не получены все дни: {', '.join(missing)}. Сохранённая часть доступна; повторите дозагрузку.")
        reports.save_source_cache(organization_id, job_key, result)
        return result
    except Exception as exc:
        result = {**refresh_context, "state": "failed", "stage": "failed", "taskId": self.request.id, "dateFrom": date_from_iso, "dateTo": date_to_iso, "finishedAt": reports._utc_now_iso(), "error": str(exc)[:500]}
        reports.save_source_cache(organization_id, job_key, result)
        raise


class ReportSourceRateLimited(RuntimeError):
    def __init__(self, message: str, delay: int):
        super().__init__(message)
        self.delay = delay


def _finance_rate_limit_delay(exc: HTTPException) -> int:
    """Honor provider retry/reset windows, including local shared cooldowns."""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    details = detail.get("details") or {}
    rate_limit = details.get("rateLimit") if isinstance(details, dict) else None
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
    delays = [61]  # Finance has a one-minute budget; keep a boundary margin.
    for value in (
        rate_limit.get("retryAfterSeconds"), rate_limit.get("resetAfterSeconds"),
        headers.get("retry-after"), headers.get("x-ratelimit-retry"), headers.get("x-ratelimit-reset"),
    ):
        try:
            seconds = float(value)
            if isfinite(seconds) and seconds > 0:
                delays.append(ceil(seconds) + 1)
        except (TypeError, ValueError, OverflowError):
            pass
    return max(delays)


def _report_rate_limit_delay(result: dict[str, Any]) -> int | None:
    errors = [step for step in result.get("steps", [])
              if isinstance(step, dict) and step.get("status") == "error"]
    if not errors:
        return None
    delays = []
    for step in errors:
        message = str(step.get("error") or step.get("message") or "")
        if "rate limit exceeded" not in message.lower() and "HTTP 429" not in message:
            return None  # Authentication and other failures are not retryable limits.
        match = re.search(r"повтор через (\d+) сек", message)
        delays.append(max(60, int(match.group(1)) if match else 60))
    return max(delays)


@tax_consistent_report
def _publish_loading_report_snapshot(organization_id, user_id, report_id,
        date_from, date_to, group_by, source, finance_allowed, *, include_previous=True):
    """Materialize available DB observations in the worker, never in a GET.

    A preview is explicitly provisional, not proof of complete collection.
    Neither builder below receives marketplace credentials.
    """
    from app.routers import wb_reports_bff as reports
    actor = SimpleNamespace(organization_id=organization_id, user_id=user_id)
    if report_id == "week-over-week":
        payload = reports._build_week_over_week_fallback_report(
            request=SimpleNamespace(query_params={}), actor=actor,
            date_from=date_from, date_to=date_to,
            date_range={"preset": "custom", "from": str(date_from), "to": str(date_to)},
            finance_allowed=finance_allowed, wb_token=None, include_previous=include_previous)
        if not reports._week_report_has_period_activity(payload):
            return
    else:
        payload = reports._saved_report_preview(actor=actor, report_id=report_id,
            date_from=date_from, date_to=date_to, group_by=group_by, finance_allowed=finance_allowed,
            **({"source": source} if report_id == "pnl" else {}))
    if payload is not None:
        payload = {**payload, "loadingSnapshot": True,
                   "meta": {**payload.get("meta", {}), "freshnessState": "partial"}}
        payload.pop("reportJob", None)
        reports._save_exact_report_payload_cache(organization_id=organization_id,
            report_id=report_id, date_from=date_from, date_to=date_to,
            group_by=group_by, source=source, report=payload, finance_allowed=finance_allowed)


@celery_app.task(name="reports.refresh_report_sources_for_org", bind=True, max_retries=3)
def refresh_report_sources_for_org(self, organization_id: int, user_id: str, report_id: str, date_from_iso: str, date_to_iso: str, group_by: str, source: str, finance_allowed: bool, wb_token: str | None, *, refresh_missing_only: bool = False) -> dict[str, Any]:
    """Refresh only the WB sources needed by one report range, then rebuild it."""
    from datetime import date as date_type
    from app.routers import wb_reports_bff as reports

    date_from = date_type.fromisoformat(date_from_iso)
    date_to = date_type.fromisoformat(date_to_iso)
    plan = _report_source_refresh_plan(report_id)
    repricer_only = report_id == "stats" and source.startswith("repricer-finance:")
    catalog_only = report_id == "stats" and source.startswith("catalog:")
    statistics_comparison = report_id == "stats" and source == "statistics-comparison"
    if repricer_only:
        plan = {**plan, "sources": ("finance",)}
    elif statistics_comparison:
        plan = {"sources": ("ads", "baskets"), "baskets_include_daily_detail": False}
    job_key = reports._digest_job_cache_key(date_from, date_to) if report_id == "digest" else reports._report_job_cache_key(
        report_id,
        date_from,
        date_to,
        group_by,
        source,
        finance_allowed=finance_allowed,
    )
    task_id = str(getattr(self.request, "id", None) or f"manual-refresh-{uuid4().hex[:12]}")
    started_at = reports._utc_now_iso()
    resolved_wb_token = _report_refresh_wb_token(organization_id, user_id, wb_token)
    previous_job = (reports.get_source_cache(organization_id, job_key, slim=False) or {}) if (repricer_only or report_id in {"abc", "pnl", "week-over-week"}) and self.request.retries else {}
    finance_progress = {key: int(previous_job.get(key) or 0) for key in ("pagesLoaded", "rowsCount")}
    period_phase = "selected"
    last_preview_at = 0.0
    preview_unavailable = False

    def publish_preview(*, force=False):
        nonlocal last_preview_at, preview_unavailable
        if report_id not in {"abc", "pnl", "rnp", "week-over-week"}:
            return
        if not force and time.monotonic() - last_preview_at < 30:
            return
        last_preview_at = time.monotonic()
        try:
            _publish_loading_report_snapshot(organization_id, user_id, report_id,
                date_from, date_to, group_by, source, finance_allowed,
                # Collection stays current-first; already persisted comparison
                # evidence does not depend on which provider batch is running.
                include_previous=True)
            preview_unavailable = False
        except Exception:
            # Incomplete source evidence can prevent a preview. Continue the
            # collection; the final builder still reports any actual failure.
            preview_unavailable = True

    def save_job(stage: str, label: str, percent: int, state: str = "running", **extra: Any) -> dict[str, Any]:
        payload = {
            "state": state,
            "taskId": task_id,
            "kind": "report_source_refresh",
            "reportId": report_id,
            "dateFrom": date_from_iso,
            "dateTo": date_to_iso,
            "groupBy": group_by,
            "stage": stage,
            "label": label,
            "percent": max(0, min(100, int(percent))),
            "startedAt": started_at,
            "updatedAt": reports._utc_now_iso(),
            **({"periodPhase": period_phase} if report_id == "week-over-week" else {}),
            **({"previewUnavailable": True} if preview_unavailable else {}),
            **({"source": source} if report_id == "stats" and not repricer_only else {}),
            **extra,
        }
        reports.save_source_cache(organization_id, job_key, payload)
        return payload

    def source_progress(step: dict[str, Any]) -> None:
        source_name = str(step.get("source") or "WB")
        source_percent = int(step.get("progressPercent") or 0)
        percent = 10 + round(max(0, min(100, source_percent)) * 0.65)
        save_job(
            "refreshing_sources",
            str(step.get("message") or f"Обновляем источник {source_name}"),
            percent,
            "running",
            currentSource=source_name,
            sourceStep=step,
        )
        publish_preview()

    def finance_page_collected(pages: int, count: int) -> None:
        finance_progress.update(pagesLoaded=pages, rowsCount=count)
        save_job("refreshing_sources", "Сохраняем операции WB для «Неделя к неделе»" if report_id == "week-over-week" else "Сохраняем операции WB для P&L" if report_id == "pnl" else "Сохраняем операции WB для ABC" if report_id == "abc" else "Сохраняем операции WB для репрайсера", 50, **finance_progress)

    save_job("queued", "Задача принята", 0, "queued")
    from app.wb_api.report_reads import active_report_read, install_report_read, ReportDemandEnded
    read_context = install_report_read(organization_id, resolved_wb_token, job_key)
    try:
        publish_preview(force=True)
        if not repricer_only:
            from app.wb_catalog_loading import missing_catalog_sources, recover_catalog
            if missing_catalog_sources(organization_id):
                if not resolved_wb_token:
                    raise RuntimeError("no_cabinet_wb_token")
                recover_catalog(organization_id, resolved_wb_token, source_progress)
            if catalog_only:
                from app.repricer_page_finance import connection_key
                if source != f"catalog:{connection_key(resolved_wb_token or '')}":
                    raise RuntimeError("WB connection changed; restart catalog collection")
                return save_job("completed", "Каталог WB сохранён", 100, "completed", finishedAt=reports._utc_now_iso())
        if repricer_only:
            from app.repricer_page_finance import connection_key
            if source != f"repricer-finance:{connection_key(resolved_wb_token or '')}":
                raise RuntimeError("WB connection changed; restart the financial collection")
        if report_id == "ads":
            if not resolved_wb_token:
                raise RuntimeError("no_cabinet_wb_token")
            # Historical statistics must not wait for optional current budgets.
            snapshot = reports.build_ads_attribution_snapshot(
                date_from=date_from, date_to=date_to, group_by="campaign", wb_token=resolved_wb_token,
                include_budgets=False,
                progress_callback=lambda stage, label, percent: save_job(stage, label, percent),
            )
            limited = [item for item in (getattr(snapshot, "diagnostics", None) or {}).get("sources", [])
                       if item.get("statusCode") == 429 and item.get("status") == "blocked"]
            retry_delay = max([20, *[reports._int_value((item.get("rateLimit") or {}).get("retryAfterSeconds")) for item in limited]]) + 1
            if snapshot.source_status == "blocked":
                if limited:
                    raise ReportSourceRateLimited("WB ограничил загрузку рекламы; сохранённые данные остаются доступны", retry_delay)
                raise RuntimeError("WB не вернул рекламные кампании. Проверьте доступ токена и повторите загрузку.")
            report = reports._map_ads_to_report_response({"preset": "custom", "from": date_from_iso, "to": date_to_iso}, snapshot)
            report["campaignDetailsCollected"] = True
            from app.wb_report_ads import save_campaign_performance
            save_campaign_performance(organization_id, date_from, date_to, report)
            stored = reports.save_ads_report_cache(organization_id=organization_id, date_from=date_from, date_to=date_to,
                group_by="campaign", payload=report)
            reports.save_ads_history_snapshots(organization_id=organization_id, rows=stored.get("rows", []),
                daily_rows=stored.get("dailyRows", []), spend_documents=stored.get("spendDocuments", []))
            reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="ads", date_from=date_from,
                date_to=date_to, group_by=group_by, source=source, report=stored, finance_allowed=finance_allowed)
            if snapshot.source_status != "fresh":
                if limited:
                    # Successful HTTP batches are durable report-read checkpoints.
                    # Resume the same job after cooldown instead of finalizing a
                    # partial report and requiring a new full collection.
                    raise ReportSourceRateLimited("WB ограничил часть рекламных пакетов; дозагружаем недостающие", retry_delay)
                failures = [f"{item.get('sourceId')}: HTTP {item.get('statusCode')}" for item in (snapshot.diagnostics or {}).get('sources', []) if item.get('status') == 'blocked']
                return save_job("partial", "Реклама сохранена частично", 100, "failed",
                    error=("; ".join(failures) or "WB не вернул полную статистику кампаний") + ". Полученная часть сохранена; повторите дозагрузку.")
            completed = save_job("completed", "Историческая статистика рекламы сохранена", 100, "completed", finishedAt=reports._utc_now_iso())
            from app.wb_report_budgets import connection, start
            try:
                start(organization_id, user_id, connection(resolved_wb_token),
                      [row.get("campaignId") for row in stored.get("rows", [])], _enqueue_report_budget_step)
            except Exception:
                pass  # Optional queue failure must not fail the published report.
            return completed
        sources = tuple(item for item in plan["sources"] if finance_allowed or item != "finance")
        windows: dict[tuple[date, date], list[str]] = {}
        source_from, source_to = reports._report_source_bounds(report_id, date_from, date_to)
        # Never merge current and comparison dates into one collection window.
        # Planning is DB-only; all current-period sources execute before any
        # previous-period request, including financial and advertising reads.
        periods = ((date_from, date_to), reports._previous_period(date_from, date_to)) if report_id == "week-over-week" else ((source_from, source_to),)
        for phase_index, (left, right) in enumerate(periods):
            for item in sources:
                if report_id == "week-over-week" and phase_index and item == "stocks":
                    continue  # A current stock snapshot is collected only once.
                readiness_source = "baskets-summary" if item == "baskets" and report_id in {"stats", "abc", "rnp"} else item
                if report_id == "week-over-week" and item == "baskets":
                    from app.wb_week_sources import load_funnel
                    coverage = load_funnel(organization_id, left, right)
                    ranges = coverage.get("missingRanges", [(left, right)]) if coverage["needsCollection"] else []
                elif report_id == "pnl" and item == "ads":
                    from app.wb_pnl_advertising import missing_campaign_ranges
                    ranges = missing_campaign_ranges(organization_id, left, right, fresh_only=True)
                elif report_id in {"abc", "pnl", "rnp", "week-over-week"}:
                    from app.wb_abc_loading import missing_source_ranges
                    ranges = missing_source_ranges(organization_id, readiness_source, left, right, fresh_only=True,
                        **({"raw_required": True} if report_id in {"pnl", "week-over-week"} else {}))
                else:
                    ranges = reports._report_missing_source_ranges(organization_id, readiness_source, date_from=left, date_to=right, fresh_only=True) if refresh_missing_only else [(left, right)]
                if repricer_only:
                    from app.repricer_page_finance import missing_page_ranges
                    ranges = missing_page_ranges(organization_id, resolved_wb_token, date_from, date_to, fresh_only=True)
                for window in ranges:
                    windows.setdefault(window, []).append(item)
        if report_id == "rnp" and finance_allowed:
            previous_from, previous_to = reports._previous_period(date_from, date_to)
            for window in reports._report_missing_source_ranges(organization_id, "finance",
                    date_from=previous_from, date_to=previous_to, fresh_only=True):
                windows.setdefault(window, []).append("finance")
        if windows:
            if not resolved_wb_token:
                raise RuntimeError("no_cabinet_wb_token")
            save_job("refreshing_sources", "Обновляем источники WB для отчета", 10, sources=list(sources))
            refresh_result = {"state": "completed", "steps": []}
            def collect_window(start, end, window_sources):
                if report_id == "pnl" and "ads" in window_sources:
                    # Publish independent finance first; advertising has its own
                    # existing fullstats checkpoints, rate limits and demand lease.
                    if "finance" in window_sources:
                        collect_window(start, end, ["finance"])
                    from app.wb_pnl_advertising import collect_campaign_spend
                    collect_campaign_spend(organization_id, resolved_wb_token, start, end,
                        lambda stage, label, percent: save_job(stage, label, percent))
                    publish_preview(force=True)
                    return
                if report_id == "week-over-week" and "baskets" in window_sources:
                    from app.wb_week_sources import collect_funnel
                    try:
                        collect_funnel(organization_id, resolved_wb_token, start, end)
                        publish_preview(force=True)
                    except HTTPException as exc:
                        if exc.status_code != 429:
                            raise
                        raise ReportSourceRateLimited("WB ограничил загрузку воронки; страницы сохранены.",
                            _finance_rate_limit_delay(exc)) from exc
                    window_sources = [item for item in window_sources if item != "baskets"]
                    if not window_sources:
                        return
                abc_finance = report_id in {"abc", "pnl", "rnp", "week-over-week"} and "finance" in window_sources
                if repricer_only or abc_finance:
                    try:
                        finance_payload = repricer_bff_module.fetch_finance_report_aggregates(
                            "complete",
                            wb_token=resolved_wb_token,
                            date_from=datetime.combine(start, datetime.min.time()),
                            date_to=datetime.combine(end, datetime.min.time()),
                            repricer_operations_org=organization_id,
                            page_collected_callback=finance_page_collected,
                        )
                        if abc_finance:
                            from app.platform.finance.service import shadow_ingest_legacy_finance_payload
                            canonical = shadow_ingest_legacy_finance_payload(organization_id, finance_payload)
                            if canonical.get("state") == "failed":
                                raise RuntimeError("Не удалось сохранить финансовый снимок для отчёта")
                        publish_preview(force=True)
                    except HTTPException as exc:
                        if exc.status_code != 429:
                            raise
                        # Each received page is already committed with its cursor.
                        # Release the worker, then resume only the missing pages.
                        raise ReportSourceRateLimited(
                            "WB временно ограничил загрузку финансов. Полученные страницы сохранены.",
                            _finance_rate_limit_delay(exc),
                        ) from exc
                    if repricer_only:
                        return
                    window_sources = [item for item in window_sources if item != "finance"]
                    if not window_sources:
                        return
                window_result = refresh_wb_data_sources(
                    organization_id=organization_id,
                    wb_token=resolved_wb_token,
                    scenario="complete",
                    period_days=(end - start).days + 1,
                    date_from=start,
                    date_to=end,
                    trigger=f"reports-{report_id}-manual-refresh",
                    force=True,
                    execute_lock=False,
                    sources=tuple(window_sources),
                    sync_profile=f"reports-{report_id}-manual-refresh",
                    sync_profile_label=f"Отчет {report_id}: обновление из WB",
                    window_kind="report",
                    baskets_include_daily_detail=bool(plan.get("baskets_include_daily_detail")),
                    _progress_callback=source_progress,
                    _parallelize=False,
                )
                refresh_result["steps"].extend(window_result.get("steps", []))
                refresh_error = _report_refresh_error(window_result)
                if refresh_error:
                    delay = _report_rate_limit_delay(window_result)
                    if delay is not None:
                        raise ReportSourceRateLimited(refresh_error, delay)
                    raise RuntimeError(refresh_error)

            ordered = sorted(windows.items(), key=lambda item: (
                report_id in {"week-over-week", "rnp"} and item[0][0] < date_from, item[0]))
            for (start, end), window_sources in ordered:
                next_phase = "previous" if report_id == "week-over-week" and start < date_from else "selected"
                if next_phase != period_phase:
                    publish_preview(force=True)
                    period_phase = next_phase
                    save_job("week-over-week-previous", "Этап текущего периода завершён; загружаем сравнение", 55)
                source_groups = [window_sources]
                if report_id == "week-over-week":
                    # A terminal funnel/finance failure must not skip another
                    # independently available source in the same date window.
                    source_groups = [[item] for item in ("baskets", "finance") if item in window_sources]
                    remaining = [item for item in window_sources if item not in {"baskets", "finance"}]
                    if remaining:
                        source_groups.append(remaining)
                for source_group in source_groups:
                    try:
                        collect_window(start, end, source_group)
                    except (ReportSourceRateLimited, ReportDemandEnded, Retry, Ignore):
                        raise
                    except Exception as exc:
                        if report_id != "week-over-week" or getattr(exc, "status_code", None) in {401, 403}:
                            raise
                        # Missing coverage stays visible; it is never zero.
                        refresh_result.setdefault("sourceErrors", []).append({
                            "periodPhase": period_phase, "dateFrom": str(start), "dateTo": str(end),
                            "sources": source_group, "error": str(exc)[:300]})
                    publish_preview()
            publish_preview(force=True)
            if repricer_only:
                return save_job("completed", "Финансы репрайсера сохранены", 100, "completed", finishedAt=reports._utc_now_iso())
            settings = get_settings()
            if (
                report_id in {"abc", "pnl"}
                and finance_allowed
                and settings.advertising_shadow_ingest_enabled
                and organization_id in settings.advertising_shadow_ingest_organization_ids
            ):
                # Keep the existing opt-in catalogue/economics bridge, but its
                # Finance-backed report no longer depends on a second Promotion
                # download. Campaign ingestion remains a separate workflow.
                save_job("economics_evidence", "Проверяем сохранённые настройки товаров", 76)
                with get_session_factory()() as session:
                    account_id = _connected_wb_account_id(session, organization_id, wb_token=resolved_wb_token)
                    apply_cost_backfill(
                        session,
                        organization_id=organization_id,
                        marketplace_account_id=account_id,
                        snapshot=LegacyCostSnapshot(
                            captured_at=datetime.now(timezone.utc),
                            goods=list_cached_goods(organization_id),
                            content_cards=(get_source_cache(organization_id, "content_cards", slim=False) or {}).get("cards", []),
                            runtime_state={}, algorithm_settings={}, costs_excel={},
                        ),
                        include_costs=False,
                    )
            save_job("building_report", "Источники обновлены, собираем отчет", 78, sync=refresh_result)
        else:
            if repricer_only:
                return save_job("completed", "Финансы репрайсера сохранены", 100, "completed", finishedAt=reports._utc_now_iso())
            refresh_result = {"state": "skipped", "steps": [], "reason": "no_wb_sources_for_report"}
            save_job("building_report", "Собираем отчет", 78, sync=refresh_result)

        if report_id == "stats":
            ready, missing = reports._report_daily_sources_ready(organization_id,
                tuple("baskets-summary" if item == "baskets" else item for item in sources), date_from=date_from, date_to=date_to)
            if not ready:
                raise RuntimeError(f"Не получены все дни: {', '.join(missing)}. Сохранённые данные доступны; повторите дозагрузку.")
            return save_job("completed", "Данные статистики сохранены", 100, "completed", sync=refresh_result, finishedAt=reports._utc_now_iso())
        if report_id == "digest":
            return self.replace(build_digest_for_org.s(
                organization_id, date_from_iso, date_to_iso, finance_allowed, None,
                source_refresh=refresh_result,
            ))
        return self.replace(build_report_for_org.s(
            organization_id, user_id, report_id, date_from_iso, date_to_iso,
            group_by, source, finance_allowed, None, source_refresh=refresh_result,
        ))
    except ReportDemandEnded:
        return save_job("paused", "Загрузка остановлена после смены экрана", 0, "paused",
            error="Полученные данные сохранены. При открытии раздела сбор продолжится с недостающих данных.")
    except Ignore:
        raise
    except ReportSourceRateLimited as exc:
        retry_count = self.request.retries
        retry_options = {}
        retry_progress = {}
        if report_id in {"abc", "pnl", "week-over-week"}:
            # Provider throttling is a resumable state, not a failed report
            # after three attempts. Keep a bound and the shared WB budget.
            retry_options["max_retries"] = 240
            retry_progress = dict(finance_progress)
        if report_id == "ads" or catalog_only:
            retry_options["max_retries"] = 80
        if repricer_only:
            # Bound consecutive failures, not the total page count of a cold
            # report. A newly committed page proves that collection is moving.
            retry_count = int(previous_job.get("rateLimitRetryCount") or 0) if finance_progress["pagesLoaded"] <= int(previous_job.get("pagesLoaded") or 0) else 0
            retry_options["max_retries"] = self.request.retries + 1
            retry_progress = {**finance_progress, "rateLimitRetryCount": retry_count + 1}
        if retry_count < (80 if report_id == "ads" or catalog_only else 240 if report_id in {"abc", "pnl", "week-over-week"} else self.max_retries):
            save_job("rate_limit", f"WB ограничил запросы. Повтор недостающих источников через {exc.delay} сек", 10,
                     "queued", retryAfterSeconds=exc.delay, retryAttempt=retry_count + 1, **retry_progress)
            raise self.retry(exc=exc, countdown=exc.delay, kwargs={**(self.request.kwargs or {}), "refresh_missing_only": True}, **retry_options)
        save_job("failed", "WB продолжает ограничивать запросы", 100, "failed", error=str(exc)[:500], finishedAt=reports._utc_now_iso())
        raise
    except Retry:
        raise
    except Exception as exc:
        save_job("failed", "Не удалось обновить данные отчета", 100, "failed", error=str(exc)[:500], finishedAt=reports._utc_now_iso())
        raise
    finally:
        active_report_read.reset(read_context)


@celery_app.task(name="reports.build_report_for_org", bind=True, max_retries=240)
@tax_consistent_report
def build_report_for_org(self, organization_id: int, user_id: str, report_id: str, date_from_iso: str, date_to_iso: str, group_by: str, source: str, finance_allowed: bool, wb_token: str | None, *, source_refresh: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a heavy report outside the HTTP request and persist its exact response."""
    from datetime import date as date_type
    from app.routers import wb_reports_bff as reports

    wb_token = None
    date_from = date_type.fromisoformat(date_from_iso)
    date_to = date_type.fromisoformat(date_to_iso)
    date_range = {"preset": "custom", "from": date_from_iso, "to": date_to_iso}
    job_key = reports._report_job_cache_key(
        report_id,
        date_from,
        date_to,
        group_by,
        source,
        finance_allowed=finance_allowed,
    )
    cache_key = reports._report_cache_key(
        report_id,
        date_from,
        date_to,
        group_by,
        source,
        organization_id=organization_id,
        finance_allowed=finance_allowed,
    )
    refresh_context = {"kind": "report_source_refresh", "sync": source_refresh} if source_refresh is not None else {}
    started_at = reports._utc_now_iso()
    def progress(stage: str, label: str, percent: int, state: str = "running") -> None:
        reports.save_source_cache(organization_id, job_key, {**refresh_context, "state": state, "taskId": self.request.id, "reportId": report_id, "dateFrom": date_from_iso, "dateTo": date_to_iso, "groupBy": group_by, "stage": stage, "label": label, "percent": percent, "startedAt": started_at, "updatedAt": reports._utc_now_iso()})
    progress("queued", "Задача принята", 0)
    try:
        required_daily_sources = REPORT_DAILY_SOURCES_BY_ID.get(report_id, ())
        if required_daily_sources:
            source_from, source_to = reports._report_source_bounds(report_id, date_from, date_to)
            ready, missing_sources = reports._report_daily_sources_ready(organization_id, required_daily_sources,
                date_from=source_from, date_to=source_to, report_id=report_id,
                finance_allowed=finance_allowed,
                selected_date_from=date_from if report_id == "week-over-week" else None)
            if not ready:
                label = "WB не вернул полные данные" if source_refresh is not None else "Ждем дневную детализацию WB"
                if missing_sources:
                    label = f"{label}: {', '.join(missing_sources)}"
                progress("partial" if source_refresh is not None else "waiting_daily_detail", label,
                    100 if source_refresh is not None else 20, "failed" if source_refresh is not None else "waiting_daily_detail")
                return reports.get_source_cache(organization_id, job_key, slim=False) or {
                    "state": "failed" if source_refresh is not None else "waiting_daily_detail",
                    "reportId": report_id,
                    "dateFrom": date_from_iso,
                    "dateTo": date_to_iso,
                    "groupBy": group_by,
                    "missingSources": missing_sources,
                }
        if report_id == "stock":
            progress("cache-snapshot", "Читаем остатки из WB sync cache", 20)
            progress("cache-snapshot", "Собираем отчет из WB cache", 80)
            snapshot = reports.build_cached_wb_reports_sources_snapshot(
                organization_id=organization_id,
                date_from=date_from,
                date_to=date_to,
            )
            report = reports._build_stock_report_payload(date_range, snapshot, organization_id=organization_id, wb_token=wb_token)
        elif report_id == "ads":
            progress("ads", "Собираем рекламу из WB sync cache", 20)
            report = reports._build_ads_report_payload(actor=SimpleNamespace(organization_id=organization_id, user_id=user_id), date_from=date_from, date_to=date_to, date_range=date_range, wb_token=None, refresh=True)
        elif report_id == "rnp":
            progress("rnp", "Собираем РНП из WB sync cache", 20)
            payload = reports.build_rnp_report(date_from=date_from, date_to=date_to, group_by=group_by, finance_allowed=finance_allowed, organization_id=organization_id, wb_token=None, force_refresh=False, progress_callback=progress)
            progress("rnp-map", "Готовим таблицу РНП", 95)
            report = reports._map_rnp_to_report_response(payload, date_range)
        elif report_id == "abc":
            progress("abc-sources", "Читаем WB sync cache для ABC", 10)
            sync_result = {"state": "cache-only", "steps": []}
            progress("abc", "Собираем ABC из WB sync cache", 85)
            payload = reports.build_abc_report(
                date_from=date_from,
                date_to=date_to,
                group_by=group_by,
                filters="",
                finance_allowed=finance_allowed,
                organization_id=organization_id,
            )
            progress("abc-map", "Готовим таблицу ABC", 97)
            report = reports._map_abc_to_report_response(payload, date_range)
            report["sync"] = {
                "state": sync_result.get("state"),
                "steps": sync_result.get("steps", []),
            }
        elif report_id == "pnl":
            cash_flow = reports.get_cash_flow_for_period(organization_id=organization_id, period_from=date_from, period_to=date_to, requested_by=user_id)
            if source == "financial" and cash_flow.get("status") not in {"ready", "disabled"}:
                # Financial WB data must not wait indefinitely for optional 1C.
                # No external operating costs are fabricated; the report remains preliminary.
                cash_flow = {"status": "unavailable", "reason": "optional_operating_costs_unavailable", "data": None}
            if cash_flow.get("status") in {"pending", "processing"}:
                label = "1С забрала задачу, ждём операционные расходы" if cash_flow.get("status") == "processing" else "Ждём операционные расходы от 1С"
                progress("waiting_1c", label, 20, "waiting_1c")
                raise self.retry(countdown=3)
            if cash_flow.get("status") not in {"ready", "disabled"} and not (source == "financial" and cash_flow.get("status") == "unavailable"):
                raise RuntimeError(f"Не удалось получить расходы из 1С: {cash_flow.get('status') or 'статус неизвестен'}")
            progress("pnl", "Собираем P&L из данных WB" if cash_flow.get("status") in {"disabled", "unavailable"} else "Собираем P&L с расходами 1С", 35)
            payload = reports.build_pnl_report(date_from=date_from, date_to=date_to, group_by=group_by, requested_state="final" if source == "financial" and cash_flow.get("status") == "ready" else "preliminary", finance_allowed=finance_allowed, organization_id=organization_id, wb_token=None, progress_callback=progress)
            progress("pnl-map", "Готовим таблицу P&L", 97)
            report = reports._map_pnl_to_report_response(payload, date_range, cash_flow, finance_allowed=finance_allowed, financial_only=source == "financial")
        elif report_id == "expenses":
            cash_flow = reports.get_cash_flow_for_period(organization_id=organization_id, period_from=date_from, period_to=date_to, requested_by=user_id)
            if cash_flow.get("status") in {"pending", "processing"}:
                label = "1С забрала задачу, ждём статьи ДДС" if cash_flow.get("status") == "processing" else "Ждём статьи ДДС от 1С"
                progress("waiting_1c", label, 20, "waiting_1c")
                raise self.retry(countdown=3)
            if cash_flow.get("status") not in {"ready", "disabled"}:
                raise RuntimeError(f"Не удалось получить расходы из 1С: {cash_flow.get('status') or 'статус неизвестен'}")
            progress("expenses", "Операционные расходы недоступны" if cash_flow.get("status") == "disabled" else "Собираем статьи ДДС из 1С", 80)
            report = reports._map_cash_flow_to_expenses_response(cash_flow, date_range, group_by)
        elif report_id == "week-over-week":
            previous_from, previous_to = reports._previous_period(date_from, date_to)
            progress("week-over-week-current", "Загружаем текущий период WB Sales Funnel", 5)
            current_payload = reports.build_abc_report(
                date_from=date_from,
                date_to=date_to,
                group_by=group_by,
                filters="",
                finance_allowed=finance_allowed,
                organization_id=organization_id,
            )
            progress("week-over-week-previous", "Загружаем предыдущий период WB Sales Funnel", 45)
            previous_payload = reports.build_abc_report(
                date_from=previous_from,
                date_to=previous_to,
                group_by=group_by,
                filters="",
                finance_allowed=finance_allowed,
                organization_id=organization_id,
            )
            progress("week-over-week-summary", "Считаем дельты и собираем WoW", 99)
            current_rows = reports._abc_payload_rows(current_payload)
            previous_rows = reports._abc_payload_rows(previous_payload)
            if not current_rows or not previous_rows:
                class _TaskQueryParams:
                    def __init__(self, scenario: str = "complete") -> None:
                        self._scenario = scenario

                    def get(self, key: str, default: str | None = None) -> str | None:
                        return self._scenario if key == "scenario" else default

                class _TaskRequest:
                    query_params = _TaskQueryParams()

                task_request = _TaskRequest()
                task_actor = type("TaskActor", (), {"organization_id": organization_id, "user_id": user_id})()
                if not current_rows:
                    repricer_rows = reports._repricer_rows_for_abc_report(
                        request=task_request,
                        actor=task_actor,
                        wb_token=wb_token,
                        date_from=date_from,
                        date_to=date_to,
                    )
                    current_rows = reports._repricer_rows_to_abc_rows(repricer_rows)
                if not previous_rows:
                    repricer_rows = reports._repricer_rows_for_abc_report(
                        request=task_request,
                        actor=task_actor,
                        wb_token=wb_token,
                        date_from=previous_from,
                        date_to=previous_to,
                    )
                    previous_rows = reports._repricer_rows_to_abc_rows(repricer_rows)
            current_period_stats = reports._week_period_stats_aggregates(organization_id, date_from, date_to)
            previous_period_stats = reports._week_period_stats_aggregates(organization_id, previous_from, previous_to)
            current_rows = reports._week_rows_with_period_stats(current_rows, current_period_stats)
            previous_rows = reports._week_rows_with_period_stats(previous_rows, previous_period_stats)
            progress("week-over-week-funnel-current", "Уточняем текущие корзины, заказы и выкупы из WB Sales Funnel", 92)
            current_funnel_metrics = reports._week_funnel_metrics_by_nm(
                organization_id=organization_id,
                date_from=date_from,
                date_to=date_to,
                wb_token=wb_token,
                progress_callback=progress,
            )
            progress("week-over-week-funnel-previous", "Уточняем прошлые корзины, заказы и выкупы из WB Sales Funnel", 96)
            previous_funnel_metrics = reports._week_funnel_metrics_by_nm(
                organization_id=organization_id,
                date_from=previous_from,
                date_to=previous_to,
                wb_token=wb_token,
                progress_callback=progress,
            )
            current_rows = reports._week_rows_with_funnel_metrics(current_rows, current_funnel_metrics)
            previous_rows = reports._week_rows_with_funnel_metrics(previous_rows, previous_funnel_metrics)
            rows = reports._week_rows_from_abc_rows(current_rows, previous_rows, organization_id=organization_id, snapshot_date=date_to)
            if finance_allowed:
                current_pnl = reports._week_pnl_values(organization_id, date_from, date_to)
                previous_pnl = reports._week_pnl_values(organization_id, previous_from, previous_to)
                for row in rows:
                    current = current_pnl.get(reports._int_value(row.get("nmId")), {})
                    previous = previous_pnl.get(reports._int_value(row.get("nmId")), {})
                    row["profit"] = {"kopecks": current.get("profit"), "deltaPct": reports._delta_pct(current.get("profit"), previous.get("profit"))}
                    row["marginPct"] = {"percent": current.get("margin"), "deltaPct": reports._delta_pct(current.get("margin"), previous.get("margin"))}
            report = reports._week_over_week_shell(
                date_range,
                rows,
                source_status=str(getattr(current_payload, "sourceStatus", None) or "partial"),
            )
            report = reports._week_financial_columns(report, organization_id, date_from, date_to, finance_allowed,
                current_pnl=current_pnl if finance_allowed else {}, previous_pnl=previous_pnl if finance_allowed else {})
            report["cache"] = {
                "status": "background-abc",
                "requestedRange": date_range,
                "previousRange": {"from": previous_from.isoformat(), "to": previous_to.isoformat()},
            }
            report["diagnostics"] = {
                "mode": "background_abc",
                "reportId": "week-over-week",
                "requestedRange": date_range,
                "previousRange": {"from": previous_from.isoformat(), "to": previous_to.isoformat()},
                "requests": [
                    {"stage": "current_abc", "endpoint": "build_abc_report", "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "status": "ok", "rows": len(current_rows), "sourceStatus": getattr(current_payload, "sourceStatus", None)},
                    {"stage": "previous_abc", "endpoint": "build_abc_report", "dateFrom": previous_from.isoformat(), "dateTo": previous_to.isoformat(), "status": "ok", "rows": len(previous_rows), "sourceStatus": getattr(previous_payload, "sourceStatus", None)},
                    {"stage": "current_period_stats", "endpoint": "period_stats_cache", "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "status": "ok", "rows": len(current_period_stats), "sourceStatus": "cached" if current_period_stats else "empty"},
                    {"stage": "previous_period_stats", "endpoint": "period_stats_cache", "dateFrom": previous_from.isoformat(), "dateTo": previous_to.isoformat(), "status": "ok", "rows": len(previous_period_stats), "sourceStatus": "cached" if previous_period_stats else "empty"},
                    {"stage": "current_funnel", "endpoint": "sales_funnel_products", "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "status": "ok", "rows": len(current_funnel_metrics), "sourceStatus": "cached"},
                    {"stage": "previous_funnel", "endpoint": "sales_funnel_products", "dateFrom": previous_from.isoformat(), "dateTo": previous_to.isoformat(), "status": "ok", "rows": len(previous_funnel_metrics), "sourceStatus": "cached"},
                ],
            }
            report["sourceStatus"] = str(getattr(current_payload, "sourceStatus", None) or "partial")
            report["confidence"] = getattr(current_payload, "confidence", None)
        else:
            raise ValueError(f"Unsupported background report: {report_id}")
        report = reports._apply_report_rules_to_payload(report, organization_id)
        if report_id == "abc":
            report["economicsVersion"] = reports._abc_economics_version(organization_id)
            cache_key = reports._report_cache_key(
                report_id,
                date_from,
                date_to,
                group_by,
                source,
                organization_id=organization_id,
                finance_allowed=finance_allowed,
            )
        reports._save_exact_report_payload_cache(
            organization_id=organization_id, report_id=report_id,
            date_from=date_from, date_to=date_to, group_by=group_by, source=source,
            report=report, finance_allowed=finance_allowed,
        )
        persisted_report = reports.get_source_cache(organization_id, cache_key, slim=False)
        if not isinstance(persisted_report, dict) or not isinstance(persisted_report.get("report"), dict):
            raise RuntimeError(f"Background report payload was not persisted: {cache_key}")
        result = {**refresh_context, "state": "completed", "taskId": self.request.id, "reportId": report_id, "dateFrom": date_from_iso, "dateTo": date_to_iso, "groupBy": group_by, "source": source, "stage": "completed", "label": "Отчёт готов", "percent": 100, "finishedAt": reports._utc_now_iso()}
        reports.save_source_cache(organization_id, job_key, result)
        return result
    except Retry:
        raise
    except Exception as exc:
        result = {**refresh_context, "state": "failed", "stage": "failed", "taskId": self.request.id, "reportId": report_id, "dateFrom": date_from_iso, "dateTo": date_to_iso, "groupBy": group_by, "finishedAt": reports._utc_now_iso(), "error": str(exc)[:500]}
        reports.save_source_cache(organization_id, job_key, result)
        raise


def _normalize_scheduler_token(raw_token: str | None) -> str | None:
    if not raw_token:
        return None
    token = raw_token.strip()
    return token or None


def _scheduler_wb_token(organization_id: int) -> str | None:
    return _normalize_scheduler_token(get_organization_wb_token_secret(organization_id))


def _report_refresh_wb_token(organization_id: int, user_id: str, wb_token: str | None) -> str | None:
    return (
        _normalize_scheduler_token(wb_token)
        # Reports and their source caches are organization-scoped. Resolve
        # the same connection as the read-only financial projection.
        or _normalize_scheduler_token(_scheduler_wb_token(organization_id))
        or _normalize_scheduler_token(get_user_wb_token_secret(user_id))
    )


def _report_refresh_error(refresh_result: dict[str, Any]) -> str | None:
    if refresh_result.get("state") in {"completed", "skipped"}:
        return None
    steps = refresh_result.get("steps") if isinstance(refresh_result.get("steps"), list) else []
    failed_steps = [
        step for step in steps
        if isinstance(step, dict) and step.get("status") == "error"
    ]
    if failed_steps:
        details = []
        for step in failed_steps[:3]:
            source = str(step.get("source") or "WB")
            error = str(step.get("error") or step.get("message") or "ошибка источника")
            details.append(f"{source}: {error}")
        return "; ".join(details)
    if refresh_result.get("error"):
        return str(refresh_result.get("error"))
    return f"WB source refresh state={refresh_result.get('state') or 'unknown'}"


def _has_strategy_assignments() -> bool:
    assignments = getattr(repricer_bff_module, "FRONTEND_STRATEGY_ASSIGNMENTS", {}) or {}
    return any(isinstance(value, dict) and value.get("strategyId") for value in assignments.values())


def _runtime_state_has_strategy_assignments(organization_id: int) -> bool:
    payload = load_runtime_state(organization_id) or {}
    assignments = payload.get("assignments") or {}
    return any(isinstance(value, dict) and value.get("strategyId") for value in assignments.values())


def _organization_ids() -> list[int]:
    organization_ids = list_organization_ids()
    return organization_ids or [1]


def _execution_organization_ids() -> tuple[list[int], list[dict[str, Any]]]:
    eligible: list[int] = []
    skipped: list[dict[str, Any]] = []
    for organization_id in _organization_ids():
        if not _runtime_state_has_strategy_assignments(organization_id):
            skipped.append({"organizationId": organization_id, "reason": "no_strategy_assignments"})
            continue
        if not _scheduler_wb_token(organization_id):
            skipped.append({"organizationId": organization_id, "reason": "no_cabinet_wb_token"})
            continue
        eligible.append(organization_id)
    return eligible, skipped


def _sync_organization_ids() -> tuple[list[int], list[dict[str, Any]]]:
    eligible: list[int] = []
    skipped: list[dict[str, Any]] = []
    for organization_id in _organization_ids():
        if not _scheduler_wb_token(organization_id):
            skipped.append({"organizationId": organization_id, "reason": "no_cabinet_wb_token"})
            continue
        eligible.append(organization_id)
    return eligible, skipped


def _skip_summary(skipped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in skipped:
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return [{"reason": reason, "count": count} for reason, count in sorted(counts.items())]


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _algorithm_bool_setting(key: str, fallback: bool = False) -> bool:
    raw = repricer_bff_module.ALGORITHM_SETTINGS_STATE.get(key)
    if raw is None:
        return fallback
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(raw)


def _utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _not_due_payload(
    *,
    organization_id: int,
    reason: str,
    last_run_at: datetime | None,
    interval_minutes: int,
) -> dict[str, Any] | None:
    if last_run_at is None:
        return None
    next_run_at = last_run_at + timedelta(minutes=interval_minutes)
    now = _utc_now_datetime()
    # Celery beat can arrive a few seconds before the exact interval boundary.
    # Treat that as due so a 5 minute strategy does not visibly run every 10 minutes.
    if now + timedelta(seconds=30) >= next_run_at:
        return None
    return {
        "organizationId": organization_id,
        "skipped": True,
        "reason": reason,
        "intervalMinutes": interval_minutes,
        "lastRunAt": last_run_at.isoformat(),
        "nextRunAt": next_run_at.isoformat(),
        "secondsUntilNextRun": max(0, int((next_run_at - now).total_seconds())),
    }


def _last_scheduler_execution_at(organization_id: int) -> datetime | None:
    runs = list_execution_runs(organization_id=organization_id, trigger="scheduler", limit=20)
    for run in runs:
        report = run.get("report") if isinstance(run.get("report"), dict) else {}
        if report.get("schedulerSkip"):
            continue
        return _parse_utc_datetime(run.get("createdAt"))
    return None


def _last_avito_scheduler_execution_at(organization_id: int) -> datetime | None:
    cached = get_source_cache(organization_id, "avito_repricer_last_scheduler_run", slim=False) or {}
    if not isinstance(cached, dict):
        return None
    return _parse_utc_datetime(cached.get("finishedAt") or cached.get("startedAt"))


def _avito_window_skip_payload(organization_id: int, settings_payload: dict[str, Any]) -> dict[str, Any] | None:
    if not settings_payload.get("activeWindowEnabled"):
        return None
    timezone_name = str(settings_payload.get("timezone") or "UTC")
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    now_local = _utc_now_datetime().astimezone(tz)
    start_hour = int(settings_payload.get("activeWindowStartHour") or 0)
    end_hour = int(settings_payload.get("activeWindowEndHour") or 0)
    hour = now_local.hour
    inside = start_hour <= hour < end_hour if start_hour < end_hour else hour >= start_hour or hour < end_hour
    if inside:
        return None
    return {
        "organizationId": organization_id,
        "skipped": True,
        "reason": "avito_active_window_closed",
        "localTime": now_local.isoformat(),
        "activeWindowStartHour": start_hour,
        "activeWindowEndHour": end_hour,
        "timezone": timezone_name,
    }


def _avito_reprice_candidates(rows: list[Any], max_changes: int, assignments: dict[str, str] | None = None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        payload = avito_repricer_row_payload(row, assignments)
        if payload.get("strategySignal") not in {"raise", "lower"}:
            continue
        if payload.get("recommendedPriceKopecks") == payload.get("priceKopecks"):
            continue
        candidates.append(payload)
    return candidates[: max(1, max_changes)]


def _append_avito_pending_approvals(organization_id: int, candidates: list[dict[str, Any]], *, apply_mode: str) -> list[dict[str, Any]]:
    cached = get_source_cache(organization_id, AVITO_REPRICER_PENDING_APPROVALS_KEY, slim=False) or {}
    existing = cached.get("items") if isinstance(cached, dict) else []
    items = list(existing) if isinstance(existing, list) else []
    now_iso = _utc_now_datetime().isoformat()
    existing_ids = {str(item.get("itemId")) for item in items if isinstance(item, dict) and item.get("status", "pending") == "pending"}
    created: list[dict[str, Any]] = []
    for candidate in candidates:
        item_id = str(candidate.get("itemId") or "")
        if not item_id or item_id in existing_ids:
            continue
        approval = {
            "approvalId": f"avito-price-{item_id}-{now_iso}",
            "status": "pending",
            "applyMode": apply_mode,
            "createdAt": now_iso,
            "itemId": item_id,
            "title": candidate.get("title"),
            "imageUrl": candidate.get("imageUrl"),
            "accountId": candidate.get("accountId"),
            "accountName": candidate.get("accountName"),
            "currentPriceKopecks": candidate.get("priceKopecks"),
            "recommendedPriceKopecks": candidate.get("recommendedPriceKopecks"),
            "priceDeltaPct": candidate.get("priceDeltaPct"),
            "strategySignal": candidate.get("strategySignal"),
            "contactsMessenger": candidate.get("contactsMessenger"),
            "contacts": candidate.get("contacts"),
            "views": candidate.get("views"),
            "messengerConversionPct": candidate.get("messengerConversionPct"),
            "reason": candidate.get("reason"),
        }
        items.append(approval)
        created.append(approval)
        existing_ids.add(item_id)
    save_source_cache(organization_id, AVITO_REPRICER_PENDING_APPROVALS_KEY, {"items": items, "savedAt": now_iso})
    append_avito_repricer_price_history(
        organization_id,
        [
            {
                **approval,
                "action": "recommended",
                "status": "pending",
                "source": apply_mode,
                "happenedAt": approval["createdAt"],
            }
            for approval in created
        ],
    )
    return created


@celery_app.task(name="repricer.execute_avito_for_org", bind=True, max_retries=1)
def execute_avito_for_org(self, organization_id: int, scenario: str = "complete") -> dict[str, Any]:
    settings = get_settings()
    if not getattr(settings, "avito_repricer_worker_enabled", True):
        return {"organizationId": organization_id, "skipped": True, "reason": "avito_repricer_worker_disabled"}
    repricer_settings = load_avito_repricer_settings(organization_id)
    if not repricer_settings.get("enabled"):
        return {"organizationId": organization_id, "skipped": True, "reason": "avito_repricer_disabled"}

    interval_minutes = int(repricer_settings.get("executeIntervalMinutes") or getattr(settings, "avito_repricer_execute_interval_minutes", 60) or 60)
    not_due = _not_due_payload(
        organization_id=organization_id,
        reason="avito_execute_interval_not_due",
        last_run_at=_last_avito_scheduler_execution_at(organization_id),
        interval_minutes=interval_minutes,
    )
    if not_due:
        return not_due
    window_skip = _avito_window_skip_payload(organization_id, repricer_settings)
    if window_skip:
        return window_skip

    credentials = get_organization_avito_credentials_secret(organization_id)
    if credentials is None:
        return {"organizationId": organization_id, "skipped": True, "reason": "no_cabinet_avito_credentials"}

    started_at = _utc_now_datetime()
    access_token = resolve_user_avito_access_token(
        user_id="repricer-scheduler",
        credentials=credentials,
        base_url=settings.avito_api_base_url,
        timeout_seconds=settings.avito_api_timeout_seconds,
    )
    client = build_avito_listings_client(
        access_token=access_token,
        base_url=settings.avito_api_base_url,
        timeout_seconds=settings.avito_api_timeout_seconds,
    )
    period_days = int(repricer_settings.get("periodDays") or getattr(settings, "avito_repricer_period_days", 30) or 30)
    end = _utc_now_datetime().date()
    start = end - timedelta(days=max(1, min(270, period_days)) - 1)
    result = client.fetch_listings(AvitoListingsFetchRequest(dateFrom=start, dateTo=end))
    max_changes = int(repricer_settings.get("maxChangesPerRun") or 50)
    assignments = load_avito_repricer_strategy_assignments(organization_id)
    candidates = _avito_reprice_candidates(result.rows, max_changes, assignments)
    real_apply_enabled = bool(getattr(settings, "avito_repricer_price_apply_enabled", False))
    auto_apply_enabled = bool(repricer_settings.get("autoApplyPricesEnabled"))
    created_pending: list[dict[str, Any]] = []
    applied_count = 0
    if candidates and auto_apply_enabled and real_apply_enabled:
        price_client = LiveAvitoPriceClient(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
        failed_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                price_client.update_price(str(candidate.get("itemId") or ""), int(candidate.get("recommendedPriceKopecks") or 0))
                applied_count += 1
                append_avito_repricer_price_history(
                    organization_id,
                    [
                        {
                            **candidate,
                            "action": "auto_applied",
                            "status": "applied",
                            "source": "worker_auto_apply",
                            "happenedAt": _utc_now_datetime().isoformat(),
                        }
                    ],
                )
            except Exception as exc:
                failed_candidates.append({**candidate, "applyError": str(exc)[:300]})
        if failed_candidates:
            created_pending = _append_avito_pending_approvals(organization_id, failed_candidates, apply_mode="apply_failed")
    elif candidates:
        created_pending = _append_avito_pending_approvals(organization_id, candidates, apply_mode="manual_approval")

    finished_at = _utc_now_datetime().isoformat()
    run_payload = {
        "organizationId": organization_id,
        "state": "completed",
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at,
        "period": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "days": period_days},
        "rowsCount": len(result.rows),
        "candidateCount": len(candidates),
        "pendingApprovalsCreated": len(created_pending),
        "appliedCount": applied_count,
        "autoApplyPricesEnabled": auto_apply_enabled,
        "priceApplyEnvEnabled": real_apply_enabled,
        "sourceStatus": result.status,
    }
    save_source_cache(organization_id, "avito_repricer_last_scheduler_run", run_payload)
    save_source_cache(organization_id, "avito_repricer_last_worker_result", run_payload)
    return run_payload


@celery_app.task(name="repricer.execute_avito_all_orgs")
def execute_avito_all_orgs(scenario: str = "complete") -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_scheduler_enabled:
        return {"processedOrganizations": 0, "skipped": True, "reason": "scheduler_disabled"}
    if not getattr(settings, "avito_repricer_worker_enabled", True):
        return {"processedOrganizations": 0, "skipped": True, "reason": "avito_repricer_worker_disabled"}
    results: list[dict[str, Any]] = []
    for organization_id in _organization_ids():
        async_result = execute_avito_for_org.delay(organization_id, scenario)
        results.append({"organizationId": organization_id, "taskId": async_result.id})
    return {"processedOrganizations": len(results), "tasks": results}


def _last_full_sync_at(organization_id: int) -> datetime | None:
    status = get_wb_sync_status(organization_id)
    if status.get("running"):
        return None
    return _parse_utc_datetime(status.get("finishedAt"))


def _last_profile_sync_at(organization_id: int, profile_id: str) -> datetime | None:
    for event in list_wb_sync_history(organization_id, hours=48, limit=200):
        if event.get("type") != "sync_finished":
            continue
        if event.get("syncProfile") != profile_id:
            continue
        if event.get("state") not in {"completed", "partial"}:
            continue
        parsed = _parse_utc_datetime(event.get("finishedAt") or event.get("observedAt"))
        if parsed is not None:
            return parsed
    return None


def _parse_profile_cache_date(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


# How far the analytical baseline may lag before we insist on a real
# re-onboarding instead of letting incremental profiles carry on.
ONBOARDING_BASELINE_MAX_LAG_DAYS = 14


def _onboarding_baseline_ready(organization_id: int, *, now: datetime | None = None) -> bool:
    """Is there a usable 30-day analytical baseline for this cabinet?

    This gate only asks whether history exists yet.  Anchoring it on today
    made it ask for data that only the incremental sync produces, so a single
    missed day deadlocked the scheduler for good: the sync got skipped, the
    missing day was never fetched, and the window could never close.

    So we anchor on the newest day that actually closes a 30-day window and
    accept a bounded lag; beyond that the cabinet genuinely needs re-onboarding.
    """
    latest = historical_sync_as_of(now or datetime.now(timezone.utc))
    for lag in range(ONBOARDING_BASELINE_MAX_LAG_DAYS + 1):
        if _working_onboarding_window_ready(organization_id, as_of=latest - timedelta(days=lag)):
            return True
    return False


def _working_onboarding_window_ready(organization_id: int, *, as_of) -> bool:
    if int(cached_goods_meta(organization_id).get("totalCached") or 0) <= 0:
        return False
    start = as_of - timedelta(days=29)
    for source in ("period-stats", "finance", "ads", "baskets"):
        if not _range_source_ready(
            organization_id,
            source,
            start,
            as_of,
            require_baskets_daily_detail=True,
            require_current_finance_basis=False,
        ):
            return False
    return True


RANGED_SYNC_SOURCE_PREFIXES: dict[str, str] = {
    "period-stats": "period_stats_",
    "finance": "finance_",
    "ads": "ads_",
    "baskets": "baskets_",
}


REPORT_DAILY_SOURCES_BY_ID: dict[str, tuple[str, ...]] = {
    "abc": ("period-stats", "finance", "ads", "baskets-summary"),
    "rnp": ("baskets-summary", "finance", "ads"),
    "ads": ("ads",),
    "stock": ("period-stats", "finance"),
    "pnl": ("finance", "ads"),
}


def _baskets_cache_has_daily_detail(cache: dict[str, Any], date_from: Any, date_to: Any) -> bool:
    required_days = (date_to - date_from).days + 1
    dates = _daily_aggregate_dates(cache)
    if dates:
        required_dates = {(date_from + timedelta(days=offset)).isoformat() for offset in range(required_days)}
        return required_dates.issubset(dates)
    return _int_value(cache.get("dailyAggregatesDays")) >= required_days


def _daily_aggregate_dates(cache: dict[str, Any]) -> set[str]:
    dates = cache.get("dailyAggregateDates")
    if isinstance(dates, list):
        return {str(item)[:10] for item in dates if item}
    daily = cache.get("dailyAggregates")
    if isinstance(daily, dict):
        return {str(key)[:10] for key in daily}
    return set()


def _range_source_ready(
    organization_id: int,
    source: str,
    date_from: Any,
    date_to: Any,
    *,
    require_baskets_daily_detail: bool = False,
    require_current_finance_basis: bool = True,
) -> bool:
    prefix = RANGED_SYNC_SOURCE_PREFIXES.get(source)
    if not prefix:
        return False
    caches = list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100)
    for cache in caches:
        if not isinstance(cache, dict):
            continue
        cache_from = _parse_profile_cache_date(cache.get("dateFrom"))
        cache_to = _parse_profile_cache_date(cache.get("dateTo"))
        if not cache_from or not cache_to or cache_from > date_from or cache_to < date_to:
            continue
        if source == "finance" and require_current_finance_basis and not finance_cache_uses_current_revenue_basis(cache):
            continue
        if source == "baskets" and require_baskets_daily_detail and not _baskets_cache_has_daily_detail(cache, date_from, date_to):
            continue
        return True

    # Each sync profile writes its own window, so a 30-day range routinely
    # straddles two of them.  Judging readiness on the days actually stored
    # keeps a fully loaded window from reading as "range_not_covered".
    required_dates = {
        (date_from + timedelta(days=offset)).isoformat()
        for offset in range((date_to - date_from).days + 1)
    }
    available_dates: set[str] = set()
    for cache in caches:
        if not isinstance(cache, dict):
            continue
        if source == "finance" and require_current_finance_basis and not finance_cache_uses_current_revenue_basis(cache):
            continue
        available_dates |= _daily_aggregate_dates(cache)
    return bool(required_dates) and required_dates.issubset(available_dates)


def _int_value(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def _rnp_daily_baskets_ready(organization_id: int, *, date_from: Any, date_to: Any) -> bool:
    return _report_snapshot_source_ready(organization_id, "baskets", date_from, date_to)


def _report_snapshot_source_ready(organization_id: int, source: str, date_from: Any, date_to: Any) -> bool:
    prefix = RANGED_SYNC_SOURCE_PREFIXES.get(source)
    if not prefix:
        return False
    required_days = (date_to - date_from).days + 1
    for cache in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=100):
        if not isinstance(cache, dict):
            continue
        cache_from = _parse_profile_cache_date(cache.get("dateFrom"))
        cache_to = _parse_profile_cache_date(cache.get("dateTo"))
        if not cache_from or not cache_to or cache_from > date_from or cache_to < date_to:
            continue
        if source == "finance" and not finance_cache_uses_current_revenue_basis(cache):
            continue
        if source == "baskets":
            if _baskets_cache_has_daily_detail(cache, date_from, date_to):
                return True
            continue
        if _int_value(cache.get("dailyAggregatesDays")) >= required_days:
            return True
    return False


def _report_snapshot_sources_ready(organization_id: int, sources: tuple[str, ...], date_from: Any, date_to: Any) -> tuple[bool, list[str]]:
    from app.routers.wb_reports_bff import _report_daily_sources_ready
    return _report_daily_sources_ready(organization_id, sources, date_from=date_from, date_to=date_to)


def _source_ready_for_profile(organization_id: int, profile: WbSyncProfile, source: str) -> bool:
    if source == "goods":
        return int(cached_goods_meta(organization_id).get("totalCached") or 0) > 0
    if source == "content":
        return int((get_source_cache(organization_id, "content_cards", slim=True) or {}).get("count") or 0) > 0
    if source == "promotions":
        return int((get_source_cache(organization_id, "promotions", slim=True) or {}).get("count") or 0) > 0
    if source == "stocks":
        return int((get_source_cache(organization_id, "stocks", slim=True) or {}).get("count") or 0) > 0
    if source in RANGED_SYNC_SOURCE_PREFIXES:
        return _range_source_ready(
            organization_id,
            source,
            profile.date_from,
            profile.date_to,
            require_baskets_daily_detail=source == "baskets" and profile.baskets_daily_detail,
        )
    return False


def _resume_missing_profile_sources(organization_id: int, profile: WbSyncProfile) -> tuple[str, ...]:
    return tuple(source for source in profile.sources if not _source_ready_for_profile(organization_id, profile, source))


def _sync_profile_status_kwargs(profile: WbSyncProfile, *, sku_count: int = 0) -> dict[str, Any]:
    meta = profile.as_status_meta(sku_count=sku_count)
    return {
        "sync_profile": profile.profile_id,
        "sync_profile_label": profile.label,
        "window_kind": profile.window_kind,
        "cadence_minutes": profile.cadence_minutes,
        "baskets_include_daily_detail": profile.baskets_daily_detail,
        "estimated_requests": meta.get("estimatedRequests"),
        "estimated_seconds": meta.get("estimatedSeconds"),
        "estimate_explanation": meta.get("estimateExplanation"),
    }


def _run_sync_profile(
    *,
    organization_id: int,
    wb_token: str,
    scenario: str,
    profile: WbSyncProfile,
    trigger: str,
) -> dict[str, Any]:
    result = refresh_wb_data_sources(
        organization_id=organization_id,
        wb_token=wb_token,
        scenario=scenario,
        period_days=profile.period_days,
        date_from=profile.date_from,
        date_to=profile.date_to,
        trigger=trigger,
        sources=profile.sources,
        **_sync_profile_status_kwargs(profile),
    )
    if result.get("state") in {"completed", "partial"}:
        _persist_report_snapshots_sync_step(organization_id, {"state": "running", "reports": [], "dateFrom": profile.date_from.isoformat(), "dateTo": profile.date_to.isoformat()})
        result["reportSnapshots"] = _materialize_report_snapshots_for_profile(organization_id, profile)
        _persist_report_snapshots_sync_step(organization_id, result["reportSnapshots"])
    return result


def _profile_result(profile: WbSyncProfile, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "syncProfile": profile.profile_id,
        "syncProfileLabel": profile.label,
        "runId": result.get("runId"),
        "state": result.get("state"),
        "steps": result.get("steps", []),
        "reportSnapshots": result.get("reportSnapshots"),
    }


def _split_sync_profile_windows(profile: WbSyncProfile, *, max_days: int = 90) -> tuple[WbSyncProfile, ...]:
    if profile.period_days <= max_days:
        return (profile,)
    windows: list[WbSyncProfile] = []
    cursor = profile.date_from
    index = 1
    while cursor <= profile.date_to:
        window_to = min(cursor + timedelta(days=max_days - 1), profile.date_to)
        windows.append(
            WbSyncProfile(
                profile_id=f"{profile.profile_id}-part-{index}",
                label=f"{profile.label} · окно {index}",
                window_kind=profile.window_kind,
                cadence_minutes=profile.cadence_minutes,
                date_from=cursor,
                date_to=window_to,
                sources=profile.sources,
                baskets_daily_detail=profile.baskets_daily_detail,
                notes=profile.notes,
            )
        )
        cursor = window_to + timedelta(days=1)
        index += 1
    return tuple(windows)


def _combined_profile_result(profile: WbSyncProfile, window_results: list[dict[str, Any]]) -> dict[str, Any]:
    latest = window_results[-1] if window_results else {}
    combined = {
        "syncProfile": profile.profile_id,
        "syncProfileLabel": profile.label,
        "runId": latest.get("runId"),
        "state": "completed" if all(item.get("state") == "completed" for item in window_results) else "partial",
        "steps": latest.get("steps", []),
        "windows": window_results,
    }
    if latest.get("reportSnapshots") is not None:
        combined["reportSnapshots"] = latest.get("reportSnapshots")
    return combined


def _persist_report_snapshots_sync_step(organization_id: int, result: dict[str, Any]) -> None:
    status = get_source_cache(organization_id, "wb_sync_status", slim=False) or {}
    if not isinstance(status, dict):
        return
    steps = [step for step in (status.get("steps") if isinstance(status.get("steps"), list) else []) if isinstance(step, dict)]
    reports = result.get("reports") if isinstance(result.get("reports"), list) else []
    extra_windows = result.get("extraWindows") if isinstance(result.get("extraWindows"), list) else []
    processed = int(result.get("processed") or len(reports) or 0)
    total = int(result.get("total") or max(processed, len(reports), 1))
    state = str(result.get("state") or "completed")
    running = state in {"running", "waiting_1c"}
    progress_percent = int(result.get("progressPercent") or (15 if running else 100))
    progress_percent = max(0, min(100, progress_percent))
    step = {
        "source": "report-snapshots",
        "status": "running" if running else "ok" if state == "completed" else "error",
        "phase": "waiting_1c" if state == "waiting_1c" else "building" if running else "completed" if state == "completed" else "failed",
        "progressPercent": progress_percent,
        "progressCurrent": processed,
        "progressTotal": total,
        "count": len(reports),
        "reports": reports,
        "dateFrom": result.get("displayDateFrom") or result.get("dateFrom"),
        "dateTo": result.get("displayDateTo") or result.get("dateTo"),
        "message": (
            str(result.get("message") or "Строим снапшоты отчетов из WB sync cache")
            if running
            else f"Снапшоты отчетов: {', '.join(str(item) for item in reports)}"
            if state == "completed"
            else str(result.get("error") or "Снапшоты отчетов завершились с ошибкой")
        ),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    if extra_windows:
        step["extraWindows"] = extra_windows
    if result.get("error"):
        step["error"] = result.get("error")
    replaced = False
    for index, existing in enumerate(steps):
        if existing.get("source") == "report-snapshots":
            steps[index] = step
            replaced = True
            break
    if not replaced:
        steps.append(step)
    status["steps"] = steps
    status["updatedAt"] = datetime.now(timezone.utc).isoformat()
    save_source_cache(organization_id, "wb_sync_status", status)


def _report_snapshot_progress(
    organization_id: int,
    *,
    date_from: Any,
    date_to: Any,
    reports: list[str],
    message: str,
    processed: int,
    total: int,
    display_date_from: Any | None = None,
    display_date_to: Any | None = None,
) -> None:
    _persist_report_snapshots_sync_step(
        organization_id,
        {
            "state": "running",
            "reports": list(reports),
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "displayDateFrom": (display_date_from or date_from).isoformat(),
            "displayDateTo": (display_date_to or date_to).isoformat(),
            "message": message,
            "processed": processed,
            "total": total,
            "progressPercent": int((processed / max(total, 1)) * 100),
        },
    )


def _extra_report_snapshot_profiles(profile: WbSyncProfile) -> list[WbSyncProfile]:
    today = datetime.now(timezone.utc).date()
    if profile.date_to < today or profile.date_to <= profile.date_from:
        return []
    yesterday = profile.date_to - timedelta(days=1)
    extra_ranges = [(profile.date_from, yesterday)]
    if profile.date_from + timedelta(days=1) <= yesterday:
        extra_ranges.append((profile.date_from + timedelta(days=1), yesterday))
    return [
        WbSyncProfile(
            profile_id=f"{profile.profile_id}-reports-{extra_from.isoformat()}-{extra_to.isoformat()}",
            label=f"{profile.label} · отчеты {extra_from.isoformat()}..{extra_to.isoformat()}",
            window_kind=profile.window_kind,
            cadence_minutes=profile.cadence_minutes,
            date_from=extra_from,
            date_to=extra_to,
            sources=profile.sources,
            baskets_daily_detail=profile.baskets_daily_detail,
            notes=profile.notes,
        )
        for extra_from, extra_to in dict.fromkeys(extra_ranges)
    ]


@tax_consistent_report
def _materialize_report_snapshots_for_profile(organization_id: int, profile: WbSyncProfile, *, persist_progress: bool = True) -> dict[str, Any]:
    if profile.period_days > 31:
        return {"skipped": True, "reason": "profile_window_too_large", "periodDays": profile.period_days}
    try:
        from app.routers import wb_reports_bff as reports
        from app.routers import wb_repricer_bff as repricer_router

        date_from = profile.date_from
        date_to = profile.date_to
        date_range = {"preset": "custom", "from": date_from.isoformat(), "to": date_to.isoformat()}
        range_start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
        range_end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=timezone.utc)
        period_suffix = f"{date_from.isoformat()}_{date_to.isoformat()}"
        actor = SimpleNamespace(organization_id=organization_id, user_id="system-sync")
        saved: list[str] = []
        skipped: list[str] = []
        total_steps = 8

        def progress(message: str, processed: int) -> None:
            if not persist_progress:
                return
            _report_snapshot_progress(organization_id, date_from=date_from, date_to=date_to, reports=saved, message=message, processed=processed, total=total_steps)

        def sources_ready(report_id: str, sources: tuple[str, ...]) -> bool:
            ready, missing = _report_snapshot_sources_ready(organization_id, sources, date_from, date_to)
            if ready:
                return True
            skipped.append(f"{report_id}:waiting-daily-detail:{','.join(missing)}")
            return False

        progress("Снапшоты отчетов: собираем SKU list", 0)
        repricer_router._build_repricer_sku_snapshot(
            organization_id,
            "complete",
            wb_token=None,
            resolved_period_days=profile.period_days,
            period_suffix=period_suffix,
            range_start=range_start,
            range_end=range_end,
            include_promotions=False,
            include_content=False,
        )
        saved.append("sku-list")
        progress("Снапшоты отчетов: SKU list готов", 1)

        if {"period-stats", "baskets"}.intersection(profile.sources) or profile.window_kind == "onboarding":
            progress("Снапшоты отчетов: собираем digest", 1)
            if sources_ready("digest", ("period-stats", "finance", "ads", "baskets")):
                source_snapshot = reports.build_cached_wb_reports_sources_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to)
                ads_snapshot = reports._build_digest_ads_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to, wb_token=None)
                funnel_snapshot = reports._build_digest_funnel_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to, wb_token=None)
                plan_fact = reports.build_plan_fact_report(date_from=date_from, date_to=date_to, dimension="manager", finance_allowed=True)
                digest = reports._build_digest_payload(
                    date_range,
                    source_snapshot,
                    ads_snapshot,
                    plan_fact,
                    funnel_snapshot,
                    _digest_report_summary(organization_id, date_from, date_to),
                )
                plan = reports.get_source_cache(organization_id, reports._digest_plan_cache_key(date_to.strftime("%Y-%m")), slim=False) or {}
                if isinstance(plan, dict) and plan:
                    reports._apply_digest_plan(digest, plan)
                digest_cache = {"digest": digest, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "completedAt": reports._utc_now_iso()}
                reports.save_source_cache(organization_id, reports._digest_cache_key(date_from, date_to), digest_cache)
                reports.save_source_cache(organization_id, "reports_digest_latest", digest_cache)
                saved.append("digest")
                progress("Снапшоты отчетов: digest готов, собираем RNP", 2)
            else:
                progress("Снапшоты отчетов: digest ожидает дневную детализацию, собираем RNP", 2)

            if sources_ready("rnp", ("baskets", "ads")):
                rnp_payload = reports.build_rnp_report(date_from=date_from, date_to=date_to, group_by="sku", finance_allowed=True, organization_id=organization_id, wb_token=None, force_refresh=False)
                rnp_report = reports._apply_report_rules_to_payload(reports._map_rnp_to_report_response(rnp_payload, date_range), organization_id)
                reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="rnp", date_from=date_from, date_to=date_to, group_by="sku", source="operational", report=rnp_report)
                saved.append("rnp")
                progress("Снапшоты отчетов: RNP готов, собираем ABC", 3)
            else:
                progress("Снапшоты отчетов: RNP ожидает дневную детализацию, собираем ABC", 3)

            if sources_ready("abc", ("period-stats", "finance", "ads", "baskets")):
                def abc_progress(event: dict[str, Any]) -> None:
                    processed_rows = _int_value(event.get("processed"))
                    total_rows = _int_value(event.get("total"))
                    if total_rows > 0:
                        progress(f"Снапшоты отчетов: ABC обработано {processed_rows} из {total_rows} SKU", 3)

                abc_payload = reports.build_abc_report(
                    date_from=date_from,
                    date_to=date_to,
                    group_by="sku",
                    filters="",
                    finance_allowed=True,
                    organization_id=organization_id,
                    progress_callback=abc_progress,
                )
                abc_report = reports._apply_report_rules_to_payload(reports._map_abc_to_report_response(abc_payload, date_range), organization_id)
                reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="abc", date_from=date_from, date_to=date_to, group_by="sku", source="operational", report=abc_report, finance_allowed=True)
                saved.append("abc")
                progress("Снапшоты отчетов: ABC готов", 4)
            else:
                progress("Снапшоты отчетов: ABC ожидает дневную детализацию", 4)

        if "stocks" in profile.sources or profile.window_kind == "onboarding":
            progress("Снапшоты отчетов: собираем остатки", max(1, len(saved)))
            if sources_ready("stock", ("period-stats", "finance")):
                snapshot = reports.build_cached_wb_reports_sources_snapshot(organization_id=organization_id, date_from=date_from, date_to=date_to)
                stock_report = reports._apply_report_rules_to_payload(reports._build_stock_report_payload(date_range, snapshot, organization_id=organization_id, wb_token=None), organization_id)
                reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="stock", date_from=date_from, date_to=date_to, group_by="sku", source="operational", report=stock_report)
                saved.append("stock")
                progress("Снапшоты отчетов: остатки готовы", 5)
            else:
                progress("Снапшоты отчетов: остатки ожидают дневную детализацию", 5)

        if "ads" in profile.sources or profile.window_kind == "onboarding":
            progress("Снапшоты отчетов: собираем рекламу", max(1, len(saved)))
            if sources_ready("ads", ("ads",)):
                ads_report = reports._apply_report_rules_to_payload(reports._build_ads_report_payload(actor=actor, date_from=date_from, date_to=date_to, date_range=date_range, wb_token=None, refresh=True), organization_id)
                reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="ads", date_from=date_from, date_to=date_to, group_by="campaign", source="operational", report=ads_report)
                saved.append("ads")
                progress("Снапшоты отчетов: реклама готова", 6)
            else:
                progress("Снапшоты отчетов: реклама ожидает дневную детализацию", 6)

        progress("Снапшоты отчетов: собираем P&L", max(1, len(saved)))
        if sources_ready("pnl", ("finance", "ads")):
            cash_flow = reports.get_cash_flow_for_period(organization_id=organization_id, period_from=date_from, period_to=date_to, requested_by="system-sync")
            pnl_payload = reports.build_pnl_report(
                date_from=date_from,
                date_to=date_to,
                group_by="sku",
                requested_state="preliminary",
                finance_allowed=True,
                organization_id=organization_id,
                wb_token=None,
            )
            pnl_report = reports._apply_report_rules_to_payload(reports._map_pnl_to_report_response(pnl_payload, date_range, cash_flow, finance_allowed=True), organization_id)
            pnl_cache = reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="pnl", date_from=date_from, date_to=date_to, group_by="sku", source="operational", report=pnl_report, finance_allowed=True)
            financial_pnl_report = reports._apply_report_rules_to_payload(reports._map_pnl_to_report_response(
                pnl_payload, date_range, cash_flow, finance_allowed=True, financial_only=True), organization_id)
            reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="pnl", date_from=date_from, date_to=date_to, group_by="sku", source="financial", report=financial_pnl_report, finance_allowed=True)
            reports.save_source_cache(
                organization_id,
                reports._report_job_cache_key(
                    "pnl",
                    date_from,
                    date_to,
                    "sku",
                    finance_allowed=True,
                ),
                reports._completed_report_job_from_cache("pnl", date_from, date_to, "sku", pnl_cache, {"source": "report-snapshots"}),
            )
            saved.append("pnl")
            progress("Снапшоты отчетов: P&L готов, собираем расходы", 7)
        else:
            cash_flow = reports.get_cash_flow_for_period(organization_id=organization_id, period_from=date_from, period_to=date_to, requested_by="system-sync")
            progress("Снапшоты отчетов: P&L ожидает дневную детализацию, собираем расходы", 7)

        expenses_report = reports._map_cash_flow_to_expenses_response(cash_flow, date_range, "sku")
        expenses_cache = reports._save_exact_report_payload_cache(organization_id=organization_id, report_id="expenses", date_from=date_from, date_to=date_to, group_by="sku", source="operational", report=expenses_report)
        reports.save_source_cache(
            organization_id,
            reports._report_job_cache_key("expenses", date_from, date_to, "sku"),
            reports._completed_report_job_from_cache("expenses", date_from, date_to, "sku", expenses_cache, {"source": "report-snapshots"}),
        )
        saved.append("expenses")
        progress("Снапшоты отчетов: расходы готовы", 8)

        extra_windows: list[dict[str, Any]] = []
        if persist_progress:
            for extra_profile in _extra_report_snapshot_profiles(profile):
                try:
                    task = materialize_report_snapshot_extra_window.delay(
                        organization_id,
                        extra_profile.profile_id,
                        extra_profile.label,
                        extra_profile.window_kind,
                        extra_profile.cadence_minutes,
                        extra_profile.date_from.isoformat(),
                        extra_profile.date_to.isoformat(),
                        list(extra_profile.sources),
                        extra_profile.baskets_daily_detail,
                        extra_profile.notes,
                    )
                    extra_windows.append(
                        {
                            "state": "queued",
                            "taskId": task.id,
                            "dateFrom": extra_profile.date_from.isoformat(),
                            "dateTo": extra_profile.date_to.isoformat(),
                        }
                    )
                except Exception as exc:
                    extra_windows.append(
                        {
                            "state": "queue_failed",
                            "dateFrom": extra_profile.date_from.isoformat(),
                            "dateTo": extra_profile.date_to.isoformat(),
                            "error": str(exc)[:300],
                        }
                    )

        result = {"state": "completed", "reports": saved, "skippedReports": skipped, "dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "extraWindows": extra_windows, "processed": len(saved), "total": total_steps}
        if persist_progress:
            _persist_report_snapshots_sync_step(organization_id, result)
        return result
    except Exception as exc:
        return {"state": "failed", "error": str(exc)[:500], "dateFrom": profile.date_from.isoformat(), "dateTo": profile.date_to.isoformat()}


def _materialize_backfill_report_snapshots(organization_id: int, profile: WbSyncProfile) -> dict[str, Any]:
    _persist_report_snapshots_sync_step(
        organization_id,
        {
            "state": "running",
            "reports": [],
            "dateFrom": profile.date_from.isoformat(),
            "dateTo": profile.date_to.isoformat(),
            "message": "Снапшоты отчетов: строим дневные и rolling окна после 180 дней",
            "processed": 0,
            "total": 1,
            "progressPercent": 5,
        },
    )
    result = run_report_snapshots_materialization(organization_id, task_id=f"onboarding-report-snapshots-{uuid4()}")
    payload = {
        "state": result.get("state") or "completed",
        "reports": ["daily", "7d", "14d", "30d"],
        "dateFrom": profile.date_from.isoformat(),
        "dateTo": profile.date_to.isoformat(),
        "message": result.get("label") or "Снапшоты отчетов после 180 дней готовы",
        "processed": result.get("processed"),
        "total": result.get("total"),
        "progressPercent": result.get("percent"),
        **({"error": result.get("error")} if result.get("error") else {}),
    }
    _persist_report_snapshots_sync_step(organization_id, payload)
    return {**result, "dateFrom": profile.date_from.isoformat(), "dateTo": profile.date_to.isoformat()}


@celery_app.task(name="repricer.materialize_report_snapshot_extra_window", bind=True, max_retries=0)
def materialize_report_snapshot_extra_window(
    self,
    organization_id: int,
    profile_id: str,
    label: str,
    window_kind: str,
    cadence_minutes: int,
    date_from_iso: str,
    date_to_iso: str,
    sources: list[str],
    baskets_daily_detail: bool,
    notes: str | None,
) -> dict[str, Any]:
    from datetime import date as date_type

    profile = WbSyncProfile(
        profile_id=profile_id,
        label=label,
        window_kind=window_kind,
        cadence_minutes=cadence_minutes,
        date_from=date_type.fromisoformat(date_from_iso),
        date_to=date_type.fromisoformat(date_to_iso),
        sources=tuple(sources),
        baskets_daily_detail=baskets_daily_detail,
        notes=notes,
    )
    result = _materialize_report_snapshots_for_profile(organization_id, profile, persist_progress=False)
    return {**result, "taskId": self.request.id, "prewarm": True}


REPORT_SNAPSHOTS_STATUS_KEY = "reports_payload_materialization_status"
REPORT_SNAPSHOT_SOURCES: tuple[str, ...] = ("stocks", "period-stats", "finance", "ads", "baskets")


def _cache_range_window(cache: dict[str, Any]) -> tuple[Any, Any] | None:
    date_from = _parse_profile_cache_date(cache.get("dateFrom"))
    date_to = _parse_profile_cache_date(cache.get("dateTo"))
    if not date_from or not date_to or date_from > date_to:
        return None
    return date_from, date_to


def _available_report_snapshot_windows(organization_id: int) -> list[tuple[date, date]]:
    source_bounds: list[tuple[date, date]] = []
    for prefix in ("period_stats_", "finance_", "ads_", "baskets_"):
        source_from = None
        source_to = None
        for cache in list_source_cache_ranges_by_prefix(organization_id, prefix, limit=500):
            if prefix == "finance_" and not finance_cache_uses_current_revenue_basis(cache):
                continue
            parsed = _cache_range_window(cache)
            if not parsed:
                continue
            date_from, date_to = parsed
            source_from = date_from if source_from is None or date_from < source_from else source_from
            source_to = date_to if source_to is None or date_to > source_to else source_to
        if source_from is None or source_to is None:
            return []
        source_bounds.append((source_from, source_to))

    coverage_from = max(item[0] for item in source_bounds)
    anchor = historical_sync_as_of()
    if any(source_to < anchor for _, source_to in source_bounds):
        return []
    return [
        (anchor - timedelta(days=days - 1), anchor)
        for days in (1, 7, 14, 30)
        if anchor - timedelta(days=days - 1) >= coverage_from
    ]


def _save_report_snapshots_status(organization_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("updatedAt", datetime.now(timezone.utc).isoformat())
    return save_source_cache(organization_id, REPORT_SNAPSHOTS_STATUS_KEY, payload)


def run_report_snapshots_materialization(organization_id: int, *, task_id: str | None = None) -> dict[str, Any]:
    task_id = task_id or f"api-bg-report-snapshots-{uuid4()}"
    started_at = datetime.now(timezone.utc).isoformat()
    windows = _available_report_snapshot_windows(organization_id)
    total = len(windows)
    _save_report_snapshots_status(
        organization_id,
        {
            "state": "running",
            "running": True,
            "taskId": task_id,
            "startedAt": started_at,
            "processed": 0,
            "total": total,
            "percent": 0,
            "label": "Готовим снапшоты отчетов из WB cache",
        },
    )
    if not windows:
        result = {
            "state": "completed",
            "running": False,
            "taskId": task_id,
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "processed": 0,
            "total": 0,
            "percent": 100,
            "label": "Нет WB cache окон для прогрева отчетов",
            "detail": "Сначала запустите холодный WB sync: без source cache отчеты прогревать не из чего",
            "reports": [],
        }
        _save_report_snapshots_status(organization_id, result)
        return result

    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    reports: list[dict[str, Any]] = []
    failed = 0
    try:
        for index, (date_from, date_to) in enumerate(windows, start=1):
            period_days = max(1, (date_to - date_from).days + 1)
            _save_report_snapshots_status(
                organization_id,
                {
                    "state": "running",
                    "running": True,
                    "taskId": task_id,
                    "startedAt": started_at,
                    "processed": index - 1,
                    "total": total,
                    "percent": int(((index - 1) / max(total, 1)) * 100),
                    "dateFrom": date_from.isoformat(),
                    "dateTo": date_to.isoformat(),
                    "periodDays": period_days,
                    "stage": "window",
                    "label": f"Строим отчеты из cache для окна {index}/{total}",
                    "detail": "Digest, RNP, ABC, остатки, реклама, P&L и расходы из cache",
                },
            )
            profile = WbSyncProfile(
                profile_id=f"report-snapshots-{date_from.isoformat()}-{date_to.isoformat()}",
                label=f"Снапшоты отчетов {date_from.isoformat()}..{date_to.isoformat()}",
                window_kind="report-snapshots",
                cadence_minutes=None,
                date_from=date_from,
                date_to=date_to,
                sources=REPORT_SNAPSHOT_SOURCES,
            )
            result = _materialize_report_snapshots_for_profile(organization_id, profile)
            reports.append(result)
            if result.get("state") == "failed":
                failed += 1
            if index == 1 or index == total or index % 5 == 0:
                _save_report_snapshots_status(
                    organization_id,
                    {
                        "state": "running",
                        "running": True,
                        "taskId": task_id,
                        "startedAt": started_at,
                        "processed": index,
                        "total": total,
                        "percent": int((index / max(total, 1)) * 100),
                        "dateFrom": date_from.isoformat(),
                        "dateTo": date_to.isoformat(),
                        "periodDays": period_days,
                        "label": f"Собираем снапшоты отчетов: {index}/{total}",
                    },
                )
        state = "completed" if failed == 0 else "partial"
        result = {
            "state": state,
            "running": False,
            "taskId": task_id,
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "processed": total,
            "total": total,
            "failed": failed,
            "percent": 100,
            "label": "Снапшоты отчетов готовы" if failed == 0 else "Снапшоты отчетов готовы частично",
            "reports": reports[-20:],
        }
        _save_report_snapshots_status(organization_id, result)
        return result
    except Exception as exc:
        result = {
            "state": "failed",
            "running": False,
            "taskId": task_id,
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "processed": len(reports),
            "total": total,
            "failed": failed + 1,
            "percent": int((len(reports) / max(total, 1)) * 100),
            "label": "Ошибка прогрева снапшотов отчетов",
            "error": str(exc)[:500],
        }
        _save_report_snapshots_status(organization_id, result)
        raise
    finally:
        flush_repricer_bff_state(organization_id, repricer_bff_module)


@celery_app.task(name="repricer.materialize_report_snapshots_for_org", bind=True, max_retries=0)
def materialize_report_snapshots_for_org(self, organization_id: int) -> dict[str, Any]:
    return run_report_snapshots_materialization(organization_id, task_id=self.request.id)


@celery_app.task(name="repricer.materialize_report_snapshots_all_orgs")
def materialize_report_snapshots_all_orgs() -> dict[str, Any]:
    if not get_settings().repricer_wb_sync_enabled:
        return {"processedOrganizations": 0, "skipped": True, "reason": "wb_sync_disabled"}
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for organization_id in _organization_ids():
        if not _scheduler_wb_token(organization_id):
            skipped.append({"organizationId": organization_id, "reason": "no_cabinet_wb_token"})
            continue
        async_result = materialize_report_snapshots_for_org.delay(organization_id)
        results.append({"organizationId": organization_id, "taskId": async_result.id})
    return {
        "processedOrganizations": len(results),
        "tasks": results,
        "skippedOrganizationsCount": len(skipped),
        "skippedSummary": _skip_summary(skipped),
    }


def _sync_report_range_sources(
    *,
    organization_id: int,
    wb_token: str | None,
    scenario: str,
    report_id: str,
    date_from: Any,
    date_to: Any,
    sources: tuple[str, ...],
) -> dict[str, Any]:
    return refresh_wb_data_sources(
        organization_id=organization_id,
        wb_token=wb_token,
        scenario=scenario,
        period_days=max(1, (date_to - date_from).days + 1),
        date_from=date_from,
        date_to=date_to,
        trigger=f"reports-{report_id}",
        force=True,
        execute_lock=False,
        sources=sources,
        sync_profile=f"reports-{report_id}",
        sync_profile_label=f"Отчет {report_id}: точный диапазон",
        window_kind="report",
        baskets_include_daily_detail=False,
    )


def _scheduler_sync_decision_event(
    *,
    decision: str,
    reason: str,
    interval_minutes: int,
    env_interval_minutes: int,
    status: dict[str, Any],
    last_run_at: datetime | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    next_run_at = last_run_at + timedelta(minutes=interval_minutes) if last_run_at else None
    return {
        "type": "scheduler_decision",
        "decision": decision,
        "reason": reason,
        "trigger": "scheduler",
        "intervalMinutes": interval_minutes,
        "lastRunAt": last_run_at.isoformat() if last_run_at else None,
        "nextRunAt": next_run_at.isoformat() if next_run_at else None,
        "secondsUntilNextRun": max(0, int((next_run_at - now).total_seconds())) if next_run_at else None,
        "settings": {
            "fullSyncEnabled": True,
            "fullSyncIntervalMinutes": interval_minutes,
            "envFullSyncIntervalMinutes": env_interval_minutes,
            "schedulerPollIntervalMinutes": REPRICER_SCHEDULER_POLL_MINUTES,
        },
        "lastStatus": {
            "runId": status.get("runId"),
            "state": status.get("state"),
            "trigger": status.get("trigger"),
            "startedAt": status.get("startedAt"),
            "finishedAt": status.get("finishedAt"),
        },
    }


def _running_sync_payload(organization_id: int, status: dict[str, Any]) -> dict[str, Any]:
    return {
        "organizationId": organization_id,
        "skipped": True,
        "reason": "wb_sync_running",
        "runId": status.get("runId"),
        "trigger": status.get("trigger"),
        "currentSource": status.get("currentSource"),
        "startedAt": status.get("startedAt"),
        "updatedAt": status.get("updatedAt"),
    }


def _persist_scheduler_skip(
    *,
    organization_id: int,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    run_id = f"exec_{uuid4().hex}"
    append_execution_run(
        organization_id=organization_id,
        run_id=run_id,
        trigger="scheduler",
        report_payload={
            "runId": run_id,
            "executedCount": 0,
            "skippedCount": 1,
            "blockedCount": 0,
            "items": [
                {
                    "articleId": "scheduler",
                    "status": "skipped",
                    "skipReason": reason,
                    "explanation": reason,
                    "blockedReasons": [],
                    "blockerDetails": [],
                }
            ],
            "schedulerSkip": {"reason": reason, **(detail or {})},
        },
    )


def _source_aggregates(organization_id: int, source_key: str) -> tuple[dict[str, dict[str, Any]], bool]:
    cache = get_source_cache(organization_id, source_key, slim=False) or {}
    if source_key.startswith("finance_") and not finance_cache_uses_current_revenue_basis(cache):
        return {}, False
    aggregates = cache.get("aggregates") if isinstance(cache.get("aggregates"), dict) else {}
    return aggregates, bool(cache.get("fetchedAt") or cache.get("simulatedAt"))


def _period_source_aggregates(
    organization_id: int,
    prefix: str,
    *,
    period_suffix: str,
    period_days: int,
) -> tuple[dict[str, dict[str, Any]], bool]:
    aggregates, loaded = _source_aggregates(organization_id, f"{prefix}_{period_suffix}")
    if loaded or period_suffix == str(period_days):
        return aggregates, loaded
    return _source_aggregates(organization_id, f"{prefix}_{period_days}")


@celery_app.task(name="repricer.prepare_strategy_sources", max_retries=0)
def prepare_strategy_sources(organization_id: int, fingerprint: str, requests: list) -> dict[str, Any]:
    """Read-only source preparation; never runs strategy execution or uploads."""
    from app.repricer_sync import wb_token_fingerprint, _period_cache_suffix
    from app.wb_api.source_freshness import source_is_fresh
    token = _scheduler_wb_token(organization_id)
    if not token or wb_token_fingerprint(token) != fingerprint:
        return {"state": "blocked", "reason": "credential_binding_changed"}
    results = []
    for prefix, start_text, end_text in requests:
        if prefix not in {"baskets", "period_stats", "stocks", "finance"}:
            raise ValueError("STRATEGY_SOURCE_NOT_ALLOWED")
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        days = (end - start).days + 1
        if days < 1:
            raise ValueError("STRATEGY_SOURCE_RANGE_INVALID")
        if is_wb_sync_running(organization_id):
            return {"state": "pending", "reason": "wb_sync_running", "items": results}
        key = "stocks" if prefix == "stocks" else f"{prefix}_{_period_cache_suffix(days, date_from=start, date_to=end)}"
        existing = get_source_cache(organization_id, key, slim=False) or {}
        if (existing.get("strategyTokenFingerprint") == fingerprint and source_is_fresh(existing)
            and existing.get("coverageState", "ok") in {"ok", "complete"}
            and existing.get("aggregateComplete", True) is True):
            continue
        result = refresh_wb_data_sources(organization_id=organization_id, wb_token=token,
            period_days=days, date_from=start, date_to=end, trigger="strategy_sources",
            sources=["period-stats" if prefix == "period_stats" else prefix], _parallelize=False)
        results.append({"source": prefix, "state": result.get("state")})
        if result.get("state") != "completed":
            break
    return {"state": "completed" if all(item["state"] == "completed" for item in results) else "partial", "items": results}


@celery_app.task(name="repricer.execute_assigned_for_org", bind=True, max_retries=2)
def execute_assigned_for_org(self, organization_id: int, scenario: str = "complete") -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_scheduler_enabled:
        _persist_scheduler_skip(organization_id=organization_id, reason="scheduler_disabled")
        return {"organizationId": organization_id, "skipped": True, "reason": "scheduler_disabled"}
    if is_wb_sync_running(organization_id):
        _persist_scheduler_skip(organization_id=organization_id, reason="wb_sync_running")
        return {"organizationId": organization_id, "skipped": True, "reason": "wb_sync_running"}

    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    try:
        if not _has_strategy_assignments():
            _persist_scheduler_skip(organization_id=organization_id, reason="no_strategy_assignments")
            return {"organizationId": organization_id, "skipped": True, "reason": "no_strategy_assignments"}

        wb_token = _scheduler_wb_token(organization_id)
        if not wb_token:
            _persist_scheduler_skip(organization_id=organization_id, reason="no_cabinet_wb_token")
            return {"organizationId": organization_id, "skipped": True, "reason": "no_cabinet_wb_token"}

        auto_apply_prices = _algorithm_bool_setting("workerAutoApplyPricesEnabled", False)
        interval_minutes = _algorithm_interval_minutes(
            "syncIntervalMinutes",
            int(getattr(settings, "repricer_execute_interval_minutes", 60) or 60),
        )
        not_due = _not_due_payload(
            organization_id=organization_id,
            reason="execute_interval_not_due",
            last_run_at=_last_scheduler_execution_at(organization_id),
            interval_minutes=interval_minutes,
        )
        if not_due:
            _persist_scheduler_skip(organization_id=organization_id, reason="execute_interval_not_due", detail=not_due)
            return not_due

        cached_goods = list_cached_goods(organization_id)
        sync_status = get_wb_sync_status(organization_id)
        # The report most recently opened/synchronized is not a pricing config.
        period_days = int(getattr(settings, "repricer_wb_sync_period_days", 30) or 30)
        from app.repricer_sync import _period_cache_suffix
        last_day = datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1)
        first_day = last_day - timedelta(days=period_days - 1)
        period_suffix = _period_cache_suffix(period_days, date_from=first_day, date_to=last_day)
        cached_period_stats, _period_cache_loaded = _period_source_aggregates(
            organization_id,
            "period_stats",
            period_suffix=period_suffix,
            period_days=period_days,
        )
        cached_baskets_aggregates, baskets_cache_loaded = _period_source_aggregates(
            organization_id,
            "baskets",
            period_suffix=period_suffix,
            period_days=period_days,
        )
        baskets_metadata = get_source_cache(organization_id, f"baskets_{period_suffix}", slim=True) or {}
        if not baskets_metadata.get("fetchedAt") and period_suffix != str(period_days):
            baskets_metadata = get_source_cache(organization_id, f"baskets_{period_days}", slim=True) or {}
        cached_finance_aggregates, _finance_cache_loaded = _period_source_aggregates(
            organization_id,
            "finance",
            period_suffix=period_suffix,
            period_days=period_days,
        )
        cached_ads_aggregates, _ads_cache_loaded = _period_source_aggregates(
            organization_id,
            "ads",
            period_suffix=period_suffix,
            period_days=period_days,
        )
        cached_stock_aggregates, stocks_cache_loaded = _source_aggregates(organization_id, "stocks")
        sku_rows = list_repricer_skus(
            scenario,
            wb_token=wb_token,
            cached_goods=cached_goods,
            cached_stock_aggregates=cached_stock_aggregates,
            stocks_cache_loaded=stocks_cache_loaded,
            cached_period_stats=cached_period_stats,
            cached_finance_aggregates=cached_finance_aggregates,
            cached_ads_aggregates=cached_ads_aggregates,
            cached_baskets_aggregates=cached_baskets_aggregates,
            baskets_cache_loaded=baskets_cache_loaded,
            cached_baskets_observed_at=baskets_metadata.get("fetchedAt"),
            period_days=period_days,
            include_promotions=False,
            include_content=False,
            tolerate_content_errors=True,
            list_view=True,
            max_items=None,
        )
        token_override = wb_token if settings.wb_api_mode == "real" else None
        client = RateLimitedWbApiClient(inner=build_wb_client(scenario, token_override=token_override))
        report = execute_all_assigned_skus(
            client=client,
            actor_id="repricer-scheduler",
            actor_role="system",
            wb_token=wb_token,
            options=StrategyExecuteOptions(
                scenario=scenario,
                createDrafts=True,
                autoApprove=auto_apply_prices,
                applyPrices=auto_apply_prices,
                simulateLocalPrice=False,
            ),
            sku_rows=sku_rows,
            organization_id=organization_id,
            trigger="scheduler",
            persist_run=True,
        )
        return {
            "organizationId": organization_id,
            "runId": report.runId,
            "executedCount": report.executedCount,
            "skippedCount": report.skippedCount,
            "blockedCount": report.blockedCount,
        }
    finally:
        flush_repricer_bff_state(organization_id, repricer_bff_module)


@celery_app.task(name="repricer.execute_assigned_all_orgs")
def execute_assigned_all_orgs(scenario: str = "complete") -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_scheduler_enabled:
        return {"processedOrganizations": 0, "skipped": True, "reason": "scheduler_disabled"}

    organization_ids, skipped_organizations = _execution_organization_ids()

    results: list[dict[str, Any]] = []
    for organization_id in organization_ids:
        async_result = execute_assigned_for_org.delay(organization_id, scenario)
        results.append({"organizationId": organization_id, "taskId": async_result.id})

    return {
        "processedOrganizations": len(results),
        "tasks": results,
        "skippedOrganizationsCount": len(skipped_organizations),
        "skippedSummary": _skip_summary(skipped_organizations),
    }


@celery_app.task(name="repricer.sync_wb_data_for_org", bind=True, max_retries=1)
def sync_wb_data_for_org(self, organization_id: int, scenario: str = "complete", period_days: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_wb_sync_enabled:
        return {"organizationId": organization_id, "skipped": True, "reason": "wb_sync_disabled"}
    wb_token = _scheduler_wb_token(organization_id)
    if not wb_token:
        return {"organizationId": organization_id, "skipped": True, "reason": "no_cabinet_wb_token"}
    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    try:
        env_interval_minutes = int(getattr(settings, "repricer_wb_sync_interval_minutes", 60) or 60)
        interval_minutes = _algorithm_interval_minutes(
            "fullSyncIntervalMinutes",
            env_interval_minutes,
        )
        sync_status = get_wb_sync_status(organization_id)
        if sync_status.get("running"):
            record_wb_sync_history_event(
                organization_id,
                _scheduler_sync_decision_event(
                    decision="skip",
                    reason="wb_sync_running",
                    interval_minutes=interval_minutes,
                    env_interval_minutes=env_interval_minutes,
                    status=sync_status,
                    last_run_at=None,
                ),
            )
            return _running_sync_payload(organization_id, sync_status)
        if sync_status.get("stale") or sync_status.get("state") == "stale":
            abandon_stale_wb_sync(organization_id, reason="scheduler_replaced_stale", status=sync_status)
            record_wb_sync_history_event(
                organization_id,
                _scheduler_sync_decision_event(
                    decision="recover",
                    reason="wb_sync_stale_replaced",
                    interval_minutes=interval_minutes,
                    env_interval_minutes=env_interval_minutes,
                    status=sync_status,
                    last_run_at=None,
                ),
            )

        # Incremental profiles still target today - that is the fresh
        # operational data they exist to fetch.  Only the baseline gate below
        # is allowed to lag.
        as_of = datetime.now(timezone.utc).date()
        if not _onboarding_baseline_ready(organization_id):
            record_wb_sync_history_event(
                organization_id,
                {
                    "type": "scheduler_profile_decision",
                    "decision": "skip",
                    "reason": "onboarding_30d_not_ready",
                    "trigger": "scheduler",
                },
            )
            return {
                "organizationId": organization_id,
                "skipped": True,
                "reason": "onboarding_30d_not_ready",
            }
        profiles = periodic_sync_profiles(as_of=as_of)
        due_profiles: list[WbSyncProfile] = []
        skipped_profiles: list[dict[str, Any]] = []
        for profile in profiles:
            profile_interval_minutes = int(profile.cadence_minutes or interval_minutes)
            last_profile_sync_at = _last_profile_sync_at(organization_id, profile.profile_id)
            profile_not_due = _not_due_payload(
                organization_id=organization_id,
                reason="profile_interval_not_due",
                last_run_at=last_profile_sync_at,
                interval_minutes=profile_interval_minutes,
            )
            if profile_not_due:
                skipped_profiles.append(
                    {
                        "syncProfile": profile.profile_id,
                        "syncProfileLabel": profile.label,
                        "windowKind": profile.window_kind,
                        "cadenceMinutes": profile_interval_minutes,
                        "lastRunAt": profile_not_due.get("lastRunAt"),
                        "nextRunAt": profile_not_due.get("nextRunAt"),
                        "secondsUntilNextRun": profile_not_due.get("secondsUntilNextRun"),
                    }
                )
            else:
                due_profiles.append(profile)

        if not due_profiles:
            record_wb_sync_history_event(
                organization_id,
                {
                    "type": "scheduler_profile_decision",
                    "decision": "not_due",
                    "reason": "profile_intervals_not_due",
                    "trigger": "scheduler",
                    "profiles": skipped_profiles,
                    "settings": {
                        "envFullSyncIntervalMinutes": env_interval_minutes,
                        "schedulerPollIntervalMinutes": REPRICER_SCHEDULER_POLL_MINUTES,
                    },
                },
            )
            return {
                "organizationId": organization_id,
                "skipped": True,
                "reason": "profile_intervals_not_due",
                "profiles": skipped_profiles,
            }

        record_wb_sync_history_event(
            organization_id,
            {
                "type": "scheduler_profile_decision",
                "decision": "due",
                "reason": "profile_interval_due",
                "trigger": "scheduler",
                "profiles": [
                    {
                        "syncProfile": profile.profile_id,
                        "syncProfileLabel": profile.label,
                        "windowKind": profile.window_kind,
                        "cadenceMinutes": profile.cadence_minutes,
                        "dateFrom": profile.date_from.isoformat(),
                        "dateTo": profile.date_to.isoformat(),
                        "sources": list(profile.sources),
                    }
                    for profile in due_profiles
                ],
                "skippedProfiles": skipped_profiles,
                "settings": {
                    "envFullSyncIntervalMinutes": env_interval_minutes,
                    "schedulerPollIntervalMinutes": REPRICER_SCHEDULER_POLL_MINUTES,
                },
            },
        )
        results: list[dict[str, Any]] = []
        for profile in due_profiles:
            result = _run_sync_profile(
                organization_id=organization_id,
                wb_token=wb_token,
                scenario=scenario,
                profile=profile,
                trigger="scheduler",
            )
            results.append(_profile_result(profile, result))
        latest_result = results[-1] if results else {}
        return {
            "organizationId": organization_id,
            "runId": latest_result.get("runId"),
            "state": "completed" if all(item.get("state") == "completed" for item in results) else "partial",
            "steps": latest_result.get("steps", []),
            "profiles": results,
            "skippedProfiles": skipped_profiles,
        }
    finally:
        flush_repricer_bff_state(organization_id, repricer_bff_module)


def run_wb_onboarding_for_org(organization_id: int, scenario: str = "complete", wb_token_override: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_wb_sync_enabled:
        return {"organizationId": organization_id, "skipped": True, "reason": "wb_sync_disabled"}
    wb_token = _normalize_scheduler_token(wb_token_override) or _scheduler_wb_token(organization_id)
    if not wb_token:
        return {"organizationId": organization_id, "skipped": True, "reason": "no_cabinet_wb_token"}
    sync_status = get_wb_sync_status(organization_id)
    if sync_status.get("stale") or sync_status.get("state") == "stale":
        abandon_stale_wb_sync(organization_id, reason="onboarding_replaced_stale", status=sync_status)
    elif sync_status.get("running"):
        if wb_token_override:
            abandon_stale_wb_sync(organization_id, reason="manual_onboarding_replaced_running_sync", status=sync_status)
        else:
            return _running_sync_payload(organization_id, sync_status)

    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    try:
        results: list[dict[str, Any]] = []
        force_all_sources = bool(wb_token_override)
        for profile in onboarding_sync_profiles(as_of=historical_sync_as_of()):
            missing_sources = tuple(profile.sources) if force_all_sources else _resume_missing_profile_sources(organization_id, profile)
            if not missing_sources:
                report_snapshots = (
                    _materialize_backfill_report_snapshots(organization_id, profile)
                    if profile.profile_id == "backfill-180d"
                    else _materialize_report_snapshots_for_profile(organization_id, profile)
                )
                _persist_report_snapshots_sync_step(organization_id, report_snapshots)
                results.append(
                    _profile_result(
                        profile,
                        {
                            "state": "completed",
                            "steps": [
                                {
                                    "source": "cache",
                                    "status": "skipped",
                                    "reason": "profile_cache_already_ready",
                                    "message": "Этап уже есть в WB cache",
                                }
                            ],
                            "reportSnapshots": report_snapshots,
                        },
                    )
                )
                continue
            resume_profile = WbSyncProfile(
                profile_id=profile.profile_id,
                label=profile.label,
                window_kind=profile.window_kind,
                cadence_minutes=profile.cadence_minutes,
                date_from=profile.date_from,
                date_to=profile.date_to,
                sources=missing_sources,
                baskets_daily_detail=profile.baskets_daily_detail,
                notes=profile.notes,
            )
            window_results: list[dict[str, Any]] = []
            for window_profile in _split_sync_profile_windows(resume_profile):
                result = _run_sync_profile(
                    organization_id=organization_id,
                    wb_token=wb_token,
                    scenario=scenario,
                    profile=window_profile,
                    trigger="onboarding",
                )
                window_results.append(_profile_result(window_profile, result))
            profile_result = window_results[0] if len(window_results) == 1 else _combined_profile_result(profile, window_results)
            if profile.profile_id == "backfill-180d" and window_results:
                profile_result["reportSnapshots"] = _materialize_backfill_report_snapshots(organization_id, profile)
            results.append(profile_result)
        latest_result = results[-1] if results else {}
        return {
            "organizationId": organization_id,
            "runId": latest_result.get("runId"),
            "state": "completed" if all(item.get("state") == "completed" for item in results) else "partial",
            "profiles": results,
            "steps": latest_result.get("steps", []),
        }
    finally:
        flush_repricer_bff_state(organization_id, repricer_bff_module)


@celery_app.task(name="repricer.sync_wb_onboarding_for_org", bind=True, max_retries=0)
def sync_wb_onboarding_for_org(self, organization_id: int, scenario: str = "complete") -> dict[str, Any]:
    return run_wb_onboarding_for_org(organization_id, scenario=scenario)


@celery_app.task(name="repricer.sync_wb_nightly_for_org", bind=True, max_retries=0)
def sync_wb_nightly_for_org(self, organization_id: int, scenario: str = "complete") -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_wb_sync_enabled:
        return {"organizationId": organization_id, "skipped": True, "reason": "wb_sync_disabled"}
    window_state = nightly_sync_window_state(datetime.now(timezone.utc))
    if not window_state["active"]:
        window_payload = {**window_state, "nextRunAt": window_state["nextRunAt"].isoformat() if hasattr(window_state.get("nextRunAt"), "isoformat") else window_state.get("nextRunAt")}
        record_wb_sync_history_event(
            organization_id,
            {
                "type": "scheduler_profile_decision",
                "decision": "skip",
                "reason": "nightly_window_closed",
                "trigger": "nightly",
                **window_payload,
            },
        )
        return {
            "organizationId": organization_id,
            "skipped": True,
            "reason": "nightly_window_closed",
            **window_payload,
        }
    wb_token = _scheduler_wb_token(organization_id)
    if not wb_token:
        return {"organizationId": organization_id, "skipped": True, "reason": "no_cabinet_wb_token"}
    sync_status = get_wb_sync_status(organization_id)
    if sync_status.get("stale") or sync_status.get("state") == "stale":
        abandon_stale_wb_sync(organization_id, reason="nightly_replaced_stale", status=sync_status)
    elif sync_status.get("running"):
        return _running_sync_payload(organization_id, sync_status)

    hydrate_repricer_bff_state(organization_id, repricer_bff_module)
    try:
        profile = nightly_reconciliation_profile(as_of=historical_sync_as_of())
        if not _working_onboarding_window_ready(organization_id, as_of=profile.date_to):
            record_wb_sync_history_event(
                organization_id,
                {
                    "type": "scheduler_profile_decision",
                    "decision": "skip",
                    "reason": "onboarding_30d_not_ready",
                    "trigger": "nightly",
                    "syncProfile": profile.profile_id,
                    "syncProfileLabel": profile.label,
                },
            )
            return {
                "organizationId": organization_id,
                "skipped": True,
                "reason": "onboarding_30d_not_ready",
                "syncProfile": profile.profile_id,
                "syncProfileLabel": profile.label,
            }
        last_profile_sync_at = _last_profile_sync_at(organization_id, profile.profile_id)
        not_due = _not_due_payload(
            organization_id=organization_id,
            reason="profile_interval_not_due",
            last_run_at=last_profile_sync_at,
            interval_minutes=int(profile.cadence_minutes or 1440),
        )
        if not_due:
            record_wb_sync_history_event(
                organization_id,
                {
                    "type": "scheduler_profile_decision",
                    "decision": "not_due",
                    "reason": "profile_interval_not_due",
                    "trigger": "nightly",
                    "syncProfile": profile.profile_id,
                    "syncProfileLabel": profile.label,
                    "lastRunAt": not_due.get("lastRunAt"),
                    "nextRunAt": not_due.get("nextRunAt"),
                },
            )
            return {
                **not_due,
                "syncProfile": profile.profile_id,
                "syncProfileLabel": profile.label,
            }

        record_wb_sync_history_event(
            organization_id,
            {
                "type": "scheduler_profile_decision",
                "decision": "due",
                "reason": "nightly_reconciliation_due",
                "trigger": "nightly",
                "syncProfile": profile.profile_id,
                "syncProfileLabel": profile.label,
                "dateFrom": profile.date_from.isoformat(),
                "dateTo": profile.date_to.isoformat(),
                "sources": list(profile.sources),
            },
        )
        result = _run_sync_profile(
            organization_id=organization_id,
            wb_token=wb_token,
            scenario=scenario,
            profile=profile,
            trigger="nightly",
        )
        detail_results: list[dict[str, Any]] = []
        detail_profiles = nightly_baskets_detail_profiles(as_of=profile.date_to)
        for index, detail_profile in enumerate(detail_profiles):
            if index > 0 and NIGHTLY_BASKETS_DETAIL_PAUSE_SECONDS > 0:
                time.sleep(NIGHTLY_BASKETS_DETAIL_PAUSE_SECONDS)
            detail_result = _run_sync_profile(
                organization_id=organization_id,
                wb_token=wb_token,
                scenario=scenario,
                profile=detail_profile,
                trigger="nightly-baskets-detail",
            )
            detail_results.append(_profile_result(detail_profile, detail_result))
        return {
            "organizationId": organization_id,
            "syncProfile": profile.profile_id,
            "syncProfileLabel": profile.label,
            "runId": result.get("runId"),
            "state": "completed" if result.get("state") == "completed" and all(item.get("state") == "completed" for item in detail_results) else "partial",
            "steps": result.get("steps", []),
            "basketsDetailProfiles": detail_results,
        }
    finally:
        flush_repricer_bff_state(organization_id, repricer_bff_module)


@celery_app.task(name="repricer.sync_wb_data_all_orgs")
def sync_wb_data_all_orgs(scenario: str = "complete", period_days: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_wb_sync_enabled:
        return {"processedOrganizations": 0, "skipped": True, "reason": "wb_sync_disabled"}

    organization_ids, skipped_organizations = _sync_organization_ids()

    results: list[dict[str, Any]] = []
    for organization_id in organization_ids:
        async_result = sync_wb_data_for_org.delay(organization_id, scenario, period_days or settings.repricer_wb_sync_period_days)
        results.append({"organizationId": organization_id, "taskId": async_result.id})

    return {
        "processedOrganizations": len(results),
        "tasks": results,
        "skippedOrganizationsCount": len(skipped_organizations),
        "skippedSummary": _skip_summary(skipped_organizations),
    }


@celery_app.task(name="repricer.sync_wb_nightly_all_orgs")
def sync_wb_nightly_all_orgs(scenario: str = "complete") -> dict[str, Any]:
    settings = get_settings()
    if not settings.repricer_wb_sync_enabled:
        return {"processedOrganizations": 0, "skipped": True, "reason": "wb_sync_disabled"}

    organization_ids, skipped_organizations = _sync_organization_ids()

    results: list[dict[str, Any]] = []
    for organization_id in organization_ids:
        async_result = sync_wb_nightly_for_org.delay(organization_id, scenario)
        results.append({"organizationId": organization_id, "taskId": async_result.id})

    return {
        "processedOrganizations": len(results),
        "tasks": results,
        "skippedOrganizationsCount": len(skipped_organizations),
        "skippedSummary": _skip_summary(skipped_organizations),
    }
