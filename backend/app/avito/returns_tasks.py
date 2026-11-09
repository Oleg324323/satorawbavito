from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.avito.auth import resolve_user_avito_access_token
from app.avito.orders import build_avito_orders_client
from app.avito.returns import extract_return_candidates
from app.cabinet.store import get_organization_avito_credentials_secret
from app.config import get_settings
from app.infra.celery_app import celery_app
from app.repricer_cache.store import get_source_cache, save_source_cache
from app.repricer_persistence.store import list_organization_ids


AVITO_RETURNS_SYNC_STATUS_KEY = "avito_returns_sync_status"
AVITO_RETURNS_SYNC_SETTINGS_KEY = "avito_returns_sync_settings"
AVITO_RETURNS_SCHEDULER_USER_ID = "avito-returns-scheduler"
AVITO_RETURNS_ALLOWED_INTERVAL_MINUTES = (5, 15, 30, 60, 180, 360)


def _organization_ids() -> list[int]:
    return list_organization_ids() or [1]


def _save_sync_status(organization_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return save_source_cache(organization_id, AVITO_RETURNS_SYNC_STATUS_KEY, payload)


def _normalize_interval_minutes(value: Any) -> int:
    settings = get_settings()
    fallback = int(getattr(settings, "avito_returns_sync_interval_minutes", 15) or 15)
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = fallback
    if interval in AVITO_RETURNS_ALLOWED_INTERVAL_MINUTES:
        return interval
    return min(AVITO_RETURNS_ALLOWED_INTERVAL_MINUTES, key=lambda allowed: abs(allowed - interval))


def get_returns_sync_settings(organization_id: int) -> dict[str, Any]:
    settings = get_settings()
    cached = get_source_cache(organization_id, AVITO_RETURNS_SYNC_SETTINGS_KEY, slim=False) or {}
    enabled = bool(cached.get("enabled", getattr(settings, "avito_returns_sync_enabled", True)))
    interval_minutes = _normalize_interval_minutes(cached.get("intervalMinutes"))
    period_days = int(cached.get("periodDays") or getattr(settings, "avito_returns_period_days", 30) or 30)
    period_days = max(1, min(183, period_days))
    return {
        "enabled": enabled,
        "intervalMinutes": interval_minutes,
        "periodDays": period_days,
        "allowedIntervals": list(AVITO_RETURNS_ALLOWED_INTERVAL_MINUTES),
        "updatedAt": cached.get("updatedAt"),
        "lastAutoDispatchedAt": cached.get("lastAutoDispatchedAt"),
    }


def save_returns_sync_settings(organization_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_returns_sync_settings(organization_id)
    merged = {
        **current,
        "enabled": bool(payload.get("enabled", current["enabled"])),
        "intervalMinutes": _normalize_interval_minutes(payload.get("intervalMinutes", current["intervalMinutes"])),
        "periodDays": max(1, min(183, int(payload.get("periodDays", current["periodDays"]) or current["periodDays"]))),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    return save_source_cache(organization_id, AVITO_RETURNS_SYNC_SETTINGS_KEY, merged)


def _iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _should_dispatch_auto_sync(organization_id: int, now: datetime) -> bool:
    sync_settings = get_returns_sync_settings(organization_id)
    if not sync_settings["enabled"]:
        return False
    last = _iso_datetime(sync_settings.get("lastAutoDispatchedAt"))
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(minutes=sync_settings["intervalMinutes"])


def _mark_auto_dispatched(organization_id: int, now: datetime) -> None:
    sync_settings = get_returns_sync_settings(organization_id)
    sync_settings["lastAutoDispatchedAt"] = now.isoformat()
    save_source_cache(organization_id, AVITO_RETURNS_SYNC_SETTINGS_KEY, sync_settings)


@celery_app.task(name="avito.sync_returns_for_org", bind=True, max_retries=0)
def sync_returns_for_org(self, organization_id: int, scenario: str = "complete", force: bool = False) -> dict[str, Any]:
    try:
        return _sync_returns_for_org(organization_id, scenario, force)
    except Exception:
        try:
            _save_sync_status(organization_id, {
                "state": "failed", "organizationId": organization_id,
                "error": "avito_returns_sync_failed", "retryable": True,
            })
        except Exception:
            pass
    # Outside both exception handlers: Celery receives neither the original
    # provider/driver exception nor its chained context, even if status save fails.
    raise RuntimeError("avito_returns_sync_failed") from None


def _safe_orders_error(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    codes = {
        "auth_required": (False, "AVITO_AUTH"),
        "forbidden_scope": (False, "AVITO_ORDER_MANAGEMENT_SCOPE"),
        "rate_limited": (True, "AVITO_RATE_LIMIT"),
        "avito_server_error": (True, "AVITO_ORDERS"),
        "avito_request_failed": (False, "AVITO_ORDERS"),
        "transport_error": (True, "AVITO_ORDERS"),
        "active_orders_unverified": (True, "AVITO_ORDERS"),
        "refresh_in_progress": (True, "AVITO_ORDERS"),
        "return_inventory_unavailable": (True, "AVITO_RETURN_INVENTORY"),
    }
    code = value.get("code") if isinstance(value, dict) else getattr(value, "code", None)
    if type(code) is not str or code not in codes:
        code = "avito_request_failed"
    retryable, blocker = codes[code]
    supplied_retryable = value.get("retryable") if isinstance(value, dict) else getattr(value, "retryable", None)
    if type(supplied_retryable) is bool:
        retryable = supplied_retryable
    return {"code": code, "message": "Avito orders source unavailable",
            "retryable": retryable, "blockerIds": [blocker]}


def _sync_returns_for_org(organization_id: int, scenario: str, force: bool) -> dict[str, Any]:
    settings = get_settings()
    sync_settings = get_returns_sync_settings(organization_id)
    if (not settings.avito_returns_sync_enabled or not sync_settings["enabled"]) and not force:
        result = {"organizationId": organization_id, "skipped": True, "reason": "avito_returns_sync_disabled"}
        _save_sync_status(organization_id, {"state": "skipped", **result})
        return result

    credentials = get_organization_avito_credentials_secret(organization_id)
    if credentials is None:
        result = {"organizationId": organization_id, "skipped": True, "reason": "no_avito_credentials"}
        _save_sync_status(organization_id, {"state": "skipped", **result})
        return result

    try:
        access_token = resolve_user_avito_access_token(
            user_id=AVITO_RETURNS_SCHEDULER_USER_ID,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
        client = build_avito_orders_client(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
        from app.routers.avito_orders import _load_orders_queue, _queue_cached_rows
        queue = _load_orders_queue(organization_id, access_token, client, force=force)
        if not queue.get("complete"):
            payload = {"state": "blocked", "organizationId": organization_id, "error": _safe_orders_error(queue.get("error"))}
            _save_sync_status(organization_id, payload)
            return payload
        candidates = extract_return_candidates(_queue_cached_rows(queue))
        payload = {
            "state": "completed",
            "organizationId": organization_id,
            "scenario": scenario,
            "syncedCount": len(candidates),
            "dateFrom": None,
            "statuses": [],
        }
        _save_sync_status(organization_id, payload)
        return payload
    except Exception:
        # The task boundary persists a fixed failure and raises outside context.
        raise


@celery_app.task(name="avito.sync_returns_all_orgs", max_retries=0)
def sync_returns_all_orgs(scenario: str = "complete") -> dict[str, Any]:
    settings = get_settings()
    if not settings.avito_returns_sync_enabled:
        return {"processedOrganizations": 0, "skipped": True, "reason": "avito_returns_sync_disabled"}

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for organization_id in _organization_ids():
        if get_organization_avito_credentials_secret(organization_id) is None:
            skipped.append({"organizationId": organization_id, "reason": "no_avito_credentials"})
            continue
        if not _should_dispatch_auto_sync(organization_id, now):
            skipped.append({"organizationId": organization_id, "reason": "interval_not_elapsed"})
            continue
        _mark_auto_dispatched(organization_id, now)
        async_result = sync_returns_for_org.delay(organization_id, scenario)
        results.append({"organizationId": organization_id, "taskId": async_result.id})
    return {
        "processedOrganizations": len(results),
        "tasks": results,
        "skippedOrganizationsCount": len(skipped),
        "skipped": skipped,
    }
