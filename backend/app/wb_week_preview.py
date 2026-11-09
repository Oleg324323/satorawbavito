"""Week DB-only previews on the API's existing background-task facility.

No provider tasks, credentials or new scheduler. One expiring Redis lease per
organization bounds reconstruction across API processes; normal job polling
can retry after a busy lease or interrupted process. HTTP never waits for it.
"""
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace


def _dependencies(reports, org, start, end):
    from app.wb_week_finance import source_revision
    return (source_revision(org, reports._previous_period(start, end)[0], end),
        reports.legacy_finance_tax_revision(org), reports.quarterly_tax_revision(org))


def enqueue(background_tasks, *, organization_id, user_id, start, end, group_by, source, finance_allowed, cached):
    if background_tasks is None:
        return  # Direct Python callers keep their existing synchronous contract.
    from app.routers import wb_reports_bff as reports
    if reports._report_payload_cache_is_usable("week-over-week", cached,
            organization_id=organization_id, allow_stale=True):
        return
    from app.infra.redis_client import get_redis_client
    from redis.exceptions import RedisError
    lease = get_redis_client().lock(f"wb:week-db-preview:{organization_id}",
        timeout=180, blocking=False, thread_local=False)
    try:
        if not lease.acquire(blocking=False):
            return  # No unbounded task queue; the next ordinary poll retries.
    except RedisError:
        return  # Existing provider job/status remains responsible for failures.
    background_tasks.add_task(_assemble, lease, organization_id, user_id,
        start, end, group_by, source, finance_allowed)


def _assemble(lease, org, user_id, start, end, group_by, source, finance_allowed):
    from redis.exceptions import RedisError
    from app.routers import wb_reports_bff as reports
    key = reports._report_cache_key("week-over-week", start, end, group_by, source,
        organization_id=org, finance_allowed=finance_allowed)
    status_key = "week_db_preview_" + sha256(key.encode()).hexdigest()[:24]
    started = datetime.now(timezone.utc)
    def status(state, **extra):
        reports.save_source_cache(org, status_key, dict(state=state,
            dateFrom=str(start), dateTo=str(end), updatedAt=reports._utc_now_iso(), **extra))
    try:
        status("running")
        dependencies = _dependencies(reports, org, start, end)
        payload = reports._build_week_over_week_fallback_report(
            request=SimpleNamespace(query_params={}),
            actor=SimpleNamespace(organization_id=org, user_id=user_id),
            date_from=start, date_to=end,
            date_range={"preset": "custom", "from": str(start), "to": str(end)},
            finance_allowed=finance_allowed, wb_token=None, include_previous=True)
        if not lease.owned() or dependencies != _dependencies(reports, org, start, end):
            status("superseded")
            return  # Never publish results calculated against changed inputs.
        latest = reports.get_source_cache(org, key, slim=False) or {}
        built_at = reports._parse_report_job_timestamp(latest.get("fetchedAt"))
        if built_at and built_at >= started and reports._report_payload_cache_is_usable(
                "week-over-week", latest, organization_id=org, allow_stale=True):
            status("completed")
            return  # A newer worker publication wins; do not downgrade it.
        if not reports._week_report_has_period_activity(payload):
            status("empty")
            return
        payload.pop("reportJob", None)
        payload.update(loadingSnapshot=True,
            meta={**payload.get("meta", {}), "freshnessState": "partial"},
            databaseAssembly={"state": "completed", "durationMs": round(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000)})
        reports._save_exact_report_payload_cache(organization_id=org,
            report_id="week-over-week", date_from=start, date_to=end,
            group_by=group_by, source=source, report=payload, finance_allowed=finance_allowed)
        status("completed", durationMs=payload["databaseAssembly"]["durationMs"])
    except Exception:
        # Do not expose raw exceptions (which can include source payloads).
        # Failure is visible in the normal status response and retryable.
        status("failed", error="WEEK_DATABASE_PREVIEW_FAILED")
    finally:
        try:
            lease.release()
        except RedisError:
            pass


def status_for(org, key):
    from app.routers import wb_reports_bff as reports
    status_key = "week_db_preview_" + sha256(key.encode()).hexdigest()[:24]
    return reports.get_source_cache(org, status_key, slim=False) or None
