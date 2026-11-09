"""Recover missing WB read caches without resetting pricing or financial state."""
from app.repricer_cache.store import cached_goods_meta, get_source_cache


def missing_catalog_sources(organization_id: int) -> tuple[str, ...]:
    meta = cached_goods_meta(organization_id)
    # A completed empty response has a persisted terminal page. No pages is
    # missing, not a confirmed empty marketplace catalog.
    missing = [] if int(meta.get("pagesCached") or 0) else ["goods"]
    content = get_source_cache(organization_id, "content_cards", slim=True) or {}
    if not isinstance(content.get("cards"), list):
        missing.append("content")
    return tuple(missing)


def require_goods_cache(organization_id: int, meta=None) -> None:
    from fastapi import HTTPException
    meta = cached_goods_meta(organization_id) if meta is None else meta
    if not (int(meta.get("pagesCached") or 0) or int(meta.get("totalCached") or 0)):
        raise HTTPException(409, detail={"code": "WB_CATALOG_CACHE_REQUIRED",
            "message": "Каталог WB ещё не загружен. Восстанавливаем данные товаров."})


def recover_catalog(organization_id: int, token: str, progress=None) -> None:
    from app.infra.redis_client import get_redis_client
    from app.repricer_page_finance import connection_key
    from app.repricer_sync import refresh_wb_data_sources
    from app.repricer_tasks import ReportSourceRateLimited, _report_rate_limit_delay, _report_refresh_error
    missing = missing_catalog_sources(organization_id)
    if not missing:
        return
    lock = get_redis_client().lock(f"wb:catalog:recover:{organization_id}:{connection_key(token)}", timeout=1200)
    if not lock.acquire(blocking=False):
        raise ReportSourceRateLimited("Каталог уже загружается другим отчётом", 5)
    try:
        missing = missing_catalog_sources(organization_id)
        if missing:
            result = refresh_wb_data_sources(organization_id=organization_id, wb_token=token,
                sources=missing, execute_lock=False, force=False, _parallelize=False,
                _catalog_recovery=True, _progress_callback=progress)
            error = _report_refresh_error(result)
            if error:
                delay = _report_rate_limit_delay(result)
                if delay is not None:
                    raise ReportSourceRateLimited(error, delay)
                raise RuntimeError(error)
    finally:
        lock.release()


def queue_catalog_recovery(org, key, args, enqueue):
    """One catalog job across ranges/tabs; persist before dispatching a worker."""
    from uuid import uuid4
    from fastapi import HTTPException
    from app.infra.redis_client import get_redis_client
    from app.routers import wb_reports_bff as reports
    lock = get_redis_client().lock(f"wb:catalog:dispatch:{org}:{key}", timeout=30)
    try:
        acquired = lock.acquire(blocking=False)
    except Exception:
        raise HTTPException(503, "Очередь загрузки недоступна") from None
    if not acquired:
        current = reports.get_source_cache(org, key, slim=False) or {}
        return {**current, "state": "queued", "kind": "report_source_refresh",
            "label": "Каталог уже загружается", "reused": True}
    try:
        current = reports.get_source_cache(org, key, slim=False) or {}
        if reports._report_job_is_reusable(current):
            return {**current, "reused": True}
        payload = {"reportId": "stats", "source": "catalog", "kind": "report_source_refresh", "updatedAt": reports._utc_now_iso()}
        if not missing_catalog_sources(org):
            payload.update(state="completed", stage="completed", percent=100,
                label="Каталог загружен из БД", finishedAt=reports._utc_now_iso())
            reports.save_source_cache(org, key, payload)
            return payload
        task_id = str(uuid4())
        payload.update(state="queued", stage="queued", percent=0, taskId=task_id,
            label="Восстанавливаем каталог WB")
        reports.save_source_cache(org, key, payload)
        try:
            enqueue(args=args, kwargs={"refresh_missing_only": True}, task_id=task_id)
        except Exception:
            reports.save_source_cache(org, key, {**payload, "state": "failed", "stage": "failed",
                "label": "Очередь загрузки недоступна", "updatedAt": reports._utc_now_iso()})
            raise HTTPException(503, "Очередь загрузки недоступна") from None
        return payload
    finally:
        lock.release()
