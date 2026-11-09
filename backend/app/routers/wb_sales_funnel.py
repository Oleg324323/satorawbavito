from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from app.control_plane.auth import actor_from_request
from app.infra.db import get_session_factory
from app.infra.redis_client import get_redis_client
from app.platform.finance.access import require_wb_account_scope
from app.platform.integrations.wb_credentials import WbCredentialBindingError, resolve_bound_wb_credential
from app.platform.period import Period, PeriodValidationError
from app.repricer_cache.store import get_source_cache, save_source_cache
from app.wb_api.report_reads import touch_demand
from app.wb_sales_funnel import job_key as funnel_job_key, job_suffix, read_comparison, read_page, scope_key
from app.wb_sales_funnel_totals import read_totals_comparison

router = APIRouter(tags=["wb-sales-funnel"])
SORTS = {"sku", "nmId", "revenueShare", "impressions", "visits", "ctr", "carts", "cartConversion", "orders", "orderConversion", "orderAmount", "buyouts", "buyoutPercent", "buyoutAmount"}


def _scope(request, session, start, end, account):
    actor = actor_from_request(request)
    try:
        period = Period(start, end)
        if end > datetime.now(ZoneInfo("Europe/Moscow")).date():
            raise PeriodValidationError("Будущие даты недоступны")
    except PeriodValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        account, _, token = resolve_bound_wb_credential(session, actor.organization_id, marketplace_account_id=account)
    except WbCredentialBindingError:
        raise HTTPException(409, "Нет однозначно подключённого аккаунта WB") from None
    require_wb_account_scope(session, actor, account)
    return actor, account, scope_key(account, token), period


@router.get("/api/wb/reports/sales-funnel")
def get_funnel(request: Request, dateFrom: date, dateTo: date,
               marketplaceAccountId: int | None = Query(None, ge=1),
               q: str = Query("", max_length=200), brand: str = Query("", max_length=255),
               sort: str = "orders", direction: str = "desc",
               page: int = Query(1, ge=1, le=100000), pageSize: int = Query(50, ge=1, le=150)):
    if sort not in SORTS or direction not in {"asc", "desc"}:
        raise HTTPException(422, "Неизвестная сортировка")
    with get_session_factory()() as session:
        actor, account, scope, period = _scope(request, session, dateFrom, dateTo, marketplaceAccountId)
        result = read_page(session, actor.organization_id, account, scope, period,
                           query=q, brand=brand, sort=sort, direction=direction, page=page, page_size=pageSize)
    return _with_job(request, actor, scope, period, result)


def _with_job(request, actor, scope, period, result, *, native=False, aggregate=False):
    job_key = funnel_job_key(scope, period, native, aggregate)
    result["job"] = get_source_cache(actor.organization_id, job_key, strict=True)
    try:
        touch_demand(request, actor, job_key)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        result["job"] = {"status": "failed", "message": "Очередь недоступна. Сохранённые данные доступны."}
    if result["job"] and result["job"].get("status") in {"queued", "running"}:
        try:
            alive = get_redis_client().exists(f"wb:funnel:job:{actor.organization_id}:{scope}:{job_suffix(period, native, aggregate)}")
        except Exception:
            alive = False
        if not alive:
            result["job"] = {"status": "failed", "message": "Загрузка прервана или очередь недоступна. Сохранённая часть доступна; повторите загрузку."}
    return result


@router.get("/api/wb/reports/sales-funnel/comparison")
def get_funnel_comparison(request: Request, dateFrom: date, dateTo: date,
                         marketplaceAccountId: int | None = Query(None, ge=1),
                         nmIds: list[int] = Query(default=[], max_length=150)):
    if any(nm <= 0 for nm in nmIds):
        raise HTTPException(422, "Некорректный артикул WB")
    with get_session_factory()() as session:
        actor, account, scope, period = _scope(request, session, dateFrom, dateTo, marketplaceAccountId)
        result = read_comparison(session, actor.organization_id, account, scope, period, nmIds)
    return _with_job(request, actor, scope, period, result, native=True)


@router.get("/api/wb/reports/sales-funnel/totals-comparison")
def get_funnel_totals_comparison(request: Request, dateFrom: date, dateTo: date,
                                marketplaceAccountId: int | None = Query(None, ge=1)):
    with get_session_factory()() as session:
        actor, account, scope, period = _scope(request, session, dateFrom, dateTo, marketplaceAccountId)
        result = read_totals_comparison(session, actor.organization_id, account, scope, period)
    return _with_job(request, actor, scope, period, result, aggregate=True)


@router.post("/api/wb/reports/sales-funnel/refresh")
def refresh_funnel(request: Request, dateFrom: date, dateTo: date,
                   marketplaceAccountId: int | None = Query(None, ge=1), nativeComparison: bool = False,
                   aggregateComparison: bool = False):
    if nativeComparison and aggregateComparison:
        raise HTTPException(422, "Выберите один вид сравнения.")
    with get_session_factory()() as session:
        actor, account, scope, period = _scope(request, session, dateFrom, dateTo, marketplaceAccountId)
        current = read_page(session, actor.organization_id, account, scope, period, page_size=1)
        selected_job = get_source_cache(actor.organization_id, funnel_job_key(scope, period), strict=True) or {}
        unfinished = selected_job.get("status") not in {None, "complete", "partial"}
        if nativeComparison or aggregateComparison:
            if (current.get("productsRefreshRequired" if nativeComparison else "refreshRequired", not current["complete"])
                or unfinished):
                raise HTTPException(409, "Сначала должна завершиться загрузка выбранного периода.")
            current = (read_totals_comparison(session, actor.organization_id, account, scope, period) if aggregateComparison
                       else read_comparison(session, actor.organization_id, account, scope, period, []))
    job_key = funnel_job_key(scope, period, nativeComparison, aggregateComparison)
    # A failed/cancelled collection is not completion. Its retry may reuse valid
    # absolute caches, but must finish through the existing coordinated job.
    retry_selected = not (nativeComparison or aggregateComparison) and unfinished
    if (current["complete"] and not retry_selected) or (current["total"] == 0 and not current.get("refreshRequired")):
        return {"status": "complete"}
    if current.get("refreshRequired") is False and not retry_selected:
        return {"status": "partial", "settled": True}
    redis = get_redis_client()
    lock_key = f"wb:funnel:job:{actor.organization_id}:{scope}:{job_suffix(period, nativeComparison, aggregateComparison)}"
    owner = str(uuid4())
    try:
        if not redis.set(lock_key, owner, nx=True, ex=1200):
            touch_demand(request, actor, job_key, start=True)
            return get_source_cache(actor.organization_id, job_key, strict=True) or {"status": "queued"}
        from app.repricer_tasks import refresh_sales_funnel
        payload = {"status": "queued", "startedAt": datetime.now(timezone.utc).isoformat()}
        save_source_cache(actor.organization_id, job_key, payload, strict=True)
        touch_demand(request, actor, job_key, start=True)
        refresh_sales_funnel.apply_async(args=(actor.organization_id, account, scope, dateFrom.isoformat(), dateTo.isoformat(), job_key, lock_key, owner),
            **({"kwargs": {"aggregate_comparison": True}} if aggregateComparison else
               {"kwargs": {"native_comparison": True}} if nativeComparison else {}), task_id=owner)
        return payload
    except Exception:
        if redis.get(lock_key) in {owner, owner.encode()}:
            redis.delete(lock_key)
        save_source_cache(actor.organization_id, job_key, {"status": "failed", "message": "Очередь загрузки недоступна. Сохранённые данные доступны."}, strict=True)
        raise HTTPException(503, "Очередь загрузки недоступна. Сохранённые данные доступны.") from None
