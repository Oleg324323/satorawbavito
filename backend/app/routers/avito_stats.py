from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from threading import Lock
from zoneinfo import ZoneInfo
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.stats import AvitoStatsClient, AvitoStatsDailyPoint, AvitoStatsFetchRequest, AvitoStatsItem, build_avito_stats_client
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.repricer_cache.store import get_source_cache, save_source_cache, list_source_cache_by_prefix


router = APIRouter(tags=["avito-stats"])
AVITO_STATS_RATE_LIMIT_COOLDOWN_SECONDS = 70
# Bound memory use while coalescing concurrent requests in the local API process.
_STATS_LOCKS = tuple(Lock() for _ in range(64))


def _saved_stats(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("status") in {"synced", "partial"} and isinstance(payload.get("rows"), list)


def _recent_stats(payload: dict[str, Any]) -> bool:
    saved = (payload.get("source") or {}).get("cache", {}).get("savedAt")
    try:
        timestamp = datetime.fromisoformat(str(saved).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return 0 <= (datetime.now(timezone.utc) - timestamp).total_seconds() < AVITO_STATS_RATE_LIMIT_COOLDOWN_SECONDS
    except (TypeError, ValueError):
        return False


def _date_range(date_from: date | None, date_to: date | None, period_days: int) -> tuple[date, date, int]:
    end = date_to or date.today()
    start = date_from or (end - timedelta(days=period_days - 1))
    if start > end:
        raise HTTPException(status_code=422, detail="AVITO_STATS_INVALID_PERIOD")
    days = (end - start).days + 1
    if days > 270:
        raise HTTPException(status_code=422, detail="AVITO_STATS_PERIOD_TOO_DEEP")
    return start, end, days


def _metric_value(value: int | None) -> int:
    return value if value is not None else 0


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _control_flags(row: AvitoStatsItem) -> list[str]:
    flags: list[str] = []
    if row.views is not None and row.views > 0 and row.contacts == 0:
        flags.append("no_contacts")
    if row.sourceStatus == "stale":
        flags.append("stale_source")
    if row.sourceStatus == "partial":
        flags.append("partial_source")
    return flags


def _row_payload(row: AvitoStatsItem) -> dict[str, Any]:
    return {
        **row.model_dump(mode="json"),
        "contactConversionPct": _ratio(row.contacts, row.views),
        "orderConversionPct": _ratio(row.orders, row.contacts),
        "buyoutPct": _ratio(row.buyouts, row.orders),
        "controlFlags": _control_flags(row),
    }


def _summary(rows: list[AvitoStatsItem]) -> dict[str, Any]:
    def total_or_none(metric: str) -> int | None:
        values = [getattr(row, metric) for row in rows]
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    totals = {
        metric: total_or_none(metric)
        for metric in (
            "impressions",
            "views",
            "contactsMessenger",
            "contacts",
            "contactsShowPhone",
            "contactsShowPhoneAndMessenger",
            "favorites",
            "spendKopecks",
            "orders",
            "buyouts",
        )
    }
    return {
        **totals,
        "conversionPct": _ratio(totals["contacts"], totals["views"]),
        "messengerConversionPct": _ratio(totals["contactsMessenger"], totals["views"]),
        "orderConversionPct": _ratio(totals["orders"], totals["contacts"]),
        "buyoutPct": _ratio(totals["buyouts"], totals["orders"]),
        "problemRows": sum(1 for row in rows if _control_flags(row)),
    }


def _timeline(points: list[AvitoStatsDailyPoint]) -> list[dict[str, Any]]:
    buckets: dict[str, list[AvitoStatsDailyPoint]] = {}
    for point in points:
        buckets.setdefault(point.date.isoformat(), []).append(point)
    metrics = ("impressions", "views", "contactsMessenger", "contacts", "contactsShowPhone",
               "contactsShowPhoneAndMessenger", "favorites", "spendKopecks", "orders", "buyouts")
    result = []
    for key, daily in sorted(buckets.items()):
        row: dict[str, Any] = {"date": key}
        for metric in metrics:
            values = [getattr(point, metric) for point in daily]
            row[metric] = sum(values) if all(value is not None for value in values) else None
        result.append(row)
    return result


def _cache_key(start: date, end: date, account_ids: list[str]) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return f"avito_stats:{start.isoformat()}:{end.isoformat()}:{account_part}"


def _rate_limit_key(account_ids: list[str]) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return f"avito_stats_rate_limit:{account_part}"


def _client(access_token: str) -> AvitoStatsClient:
    settings = get_settings()
    try:
        return build_avito_stats_client(
            mode="live",
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _response_payload(
    *,
    status: str,
    start: date,
    end: date,
    days: int,
    rows: list[AvitoStatsItem],
    accounts: list[Any],
    daily: list[AvitoStatsDailyPoint] | None = None,
    error: Any | None = None,
    cache_status: str = "fresh",
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda item: (_metric_value(item.views), _metric_value(item.contacts), _metric_value(item.favorites)), reverse=True)
    settings = get_settings()
    return {
        "status": status,
        "period": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "days": days},
        "summary": _summary(sorted_rows),
        "timeline": _timeline(daily or []),
        "accounts": [account.model_dump(mode="json") for account in accounts],
        "rows": [_row_payload(row) for row in sorted_rows],
        "source": {
            "mode": "live",
            "cache": {
                "status": cache_status,
                "savedAt": datetime.now(timezone.utc).isoformat(),
            },
            "api": {
                "baseUrl": settings.avito_api_base_url,
                "tokenEndpoint": "POST /token",
                "itemsEndpoint": "GET /core/v1/items",
                "statsEndpoint": "POST /stats/v2/accounts/{user_id}/items",
                "maxItemIdsPerRequest": 1000,
                "maxDepthDays": 270,
            },
            "diagnostics": diagnostics,
            "error": error.model_dump(mode="json") if hasattr(error, "model_dump") else error,
        },
    }


def _stale_cached_payload(cached: dict[str, Any], error: Any | None) -> dict[str, Any]:
    payload = dict(cached)
    payload["status"] = "partial"
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "stale"
    source["cache"] = cache
    source["error"] = error.model_dump(mode="json") if hasattr(error, "model_dump") else error
    payload["source"] = source
    return payload


def _cache_hit_payload(cached: dict[str, Any]) -> dict[str, Any]:
    payload = dict(cached)
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "hit"
    source["cache"] = cache
    source["error"] = None
    payload["source"] = source
    return payload


def _daily_cache_for_period(organization_id, scope, start, end, days, account_ids):
    """Reuse a complete wider daily snapshot, with exact credential/account binding.

    Never substitute a month for a quarter, sum overlapping snapshots, or read
    totals-only/other-credential snapshots as a daily source.
    """
    required_start = min(start, min(end, datetime.now(ZoneInfo("Europe/Moscow")).date()
                                   - timedelta(days=1)) - timedelta(days=5))
    for snapshot in list_source_cache_by_prefix(organization_id, "avito_stats:", limit=100):
        period = snapshot.get("period") or {}
        try:
            source_start = date.fromisoformat(period["dateFrom"])
            source_end = date.fromisoformat(period["dateTo"])
        except (KeyError, ValueError, TypeError):
            continue
        expected = scoped_avito_cache_key(_cache_key(source_start, source_end, account_ids) + ":daily", scope)
        if snapshot.get("sourceKey") != expected or snapshot.get("status") != "synced":
            continue
        timeline = {point.get("date"): point for point in snapshot.get("timeline", []) if isinstance(point, dict)}
        required = [(required_start + timedelta(days=i)).isoformat() for i in range((end - required_start).days + 1)]
        if not all(day in timeline for day in required):
            continue
        selected = [timeline[day] for day in required if start.isoformat() <= day <= end.isoformat()]
        metrics = ("impressions", "views", "contactsMessenger", "contacts", "contactsShowPhone",
                   "contactsShowPhoneAndMessenger", "favorites", "spendKopecks", "orders", "buyouts")
        totals = {}
        for metric in metrics:
            values = [point.get(metric) for point in selected]
            totals[metric] = sum(values) if values and all(isinstance(value, (int, float)) for value in values) else None
        row = AvitoStatsItem(itemId="period:totals", title="Итоги периода", accountId="all", accountName="Все аккаунты", **totals)
        payload = _cache_hit_payload(snapshot)
        payload.pop("sourceKey", None)
        payload.pop("fetchedAt", None)
        payload.update(period={"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "days": days},
                       summary=_summary([row]), rows=[_row_payload(row)], timeline=[timeline[day] for day in required])
        payload["source"]["cache"]["derivedFromPeriod"] = period
        return payload
    return None


@router.get("/api/v1/avito/stats")
def get_avito_stats(
    request: Request,
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
    period_days: int = Query(default=30, alias="periodDays", ge=1, le=270),
    account_id: list[str] = Query(default_factory=list, alias="accountId"),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
    include_daily: bool = Query(default=False, alias="includeDaily"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    start, end, days = _date_range(date_from, date_to, period_days)
    credentials = get_user_avito_credentials_secret(actor.user_id) or get_organization_avito_credentials_secret(actor.organization_id)
    if credentials is None:
        raise HTTPException(status_code=409, detail="AVITO_CREDENTIALS_REQUIRED")

    scope = scoped_avito_cache_key("avito_stats_owner", f"{credentials.client_id}\0{credentials.client_secret}")
    with _STATS_LOCKS[hash((actor.organization_id, scope)) % len(_STATS_LOCKS)]:
        return _load_stats(actor, credentials, scope, start, end, days, account_id, force_refresh, include_daily is True)


def _load_stats(actor, credentials, scope, start, end, days, account_id, force_refresh, include_daily=False):
    # Credential-scoped persisted snapshots survive OAuth token renewal. Read them
    # before OAuth so an unavailable provider cannot hide already saved statistics.
    base_key = _cache_key(start, end, account_id)
    source_key = scoped_avito_cache_key(base_key + (":daily" if include_daily else ""), scope)
    cached = get_source_cache(actor.organization_id, source_key, slim=False) or None
    if include_daily and not _saved_stats(cached):
        cached = _daily_cache_for_period(actor.organization_id, scope, start, end, days, account_id)
    if _saved_stats(cached) and (not force_refresh or _recent_stats(cached)):
        return _cache_hit_payload(cached)

    fallback = cached
    if include_daily and not _saved_stats(fallback):
        fallback = get_source_cache(actor.organization_id, scoped_avito_cache_key(base_key, scope), slim=False)

    settings = get_settings()
    try:
        access_token = resolve_user_avito_access_token(
            user_id=actor.user_id,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
    except Exception as exc:
        if _saved_stats(fallback):
            return _stale_cached_payload(fallback, {"code": "auth_required", "message": "AVITO_OAUTH_FAILED"})
        raise HTTPException(status_code=409, detail="AVITO_OAUTH_FAILED") from exc

    if not _saved_stats(cached) and not include_daily:
        legacy = get_source_cache(actor.organization_id, scoped_avito_cache_key(_cache_key(start, end, account_id), access_token), slim=False)
        if _saved_stats(legacy):
            cached = legacy
            save_source_cache(actor.organization_id, source_key, cached)
    if _saved_stats(cached) and (not force_refresh or _recent_stats(cached)):
        return _cache_hit_payload(cached)

    rate_limit_key = scoped_avito_cache_key(_rate_limit_key(account_id), scope)
    cooldown = (get_source_cache(actor.organization_id, rate_limit_key, slim=False)
                or get_source_cache(actor.organization_id, scoped_avito_cache_key(_rate_limit_key(account_id), access_token), slim=False) or {})
    retry_after = cooldown.get("retryAfterUntil") if isinstance(cooldown, dict) else None
    try:
        retry_at = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00")) if retry_after else None
        if retry_at and retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
    except ValueError:
        retry_at = None
    if retry_at and retry_at > datetime.now(timezone.utc):
        fallback = cached if _saved_stats(cached) else fallback
        error = {
            "code": "rate_limited", "message": "Avito rate limit is active", "retryable": True,
            "blockerIds": ["AVITO_RATE_LIMIT"], "retryAfterUntil": retry_at.isoformat(),
        }
        if _saved_stats(fallback):
            return _stale_cached_payload(fallback, error)
        return _response_payload(status="blocked", start=start, end=end, days=days,
                                 rows=[], accounts=[], error=error, cache_status="cooldown")

    # One day-grouped request includes the six completed days needed by trends.
    # Today's partial day is displayed, but never compared to completed days.
    completed_end = min(end, datetime.now(ZoneInfo("Europe/Moscow")).date() - timedelta(days=1))
    fetch_start = min(start, completed_end - timedelta(days=5)) if include_daily else start
    result = _client(access_token).fetch_stats(AvitoStatsFetchRequest(
        dateFrom=fetch_start, dateTo=end, accountIds=account_id, grouping="day" if include_daily else "totals"))
    if result.status == "blocked":
        if include_daily and not _saved_stats(cached):
            # Existing totals remain useful when the new daily query is unavailable.
            cached = get_source_cache(actor.organization_id, scoped_avito_cache_key(base_key, scope), slim=False)
        error = result.error
        if error and error.code == "rate_limited":
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=AVITO_STATS_RATE_LIMIT_COOLDOWN_SECONDS)
            save_source_cache(actor.organization_id, rate_limit_key, {"retryAfterUntil": retry_at.isoformat()})
            error = {**error.model_dump(mode="json"), "retryAfterUntil": retry_at.isoformat()}
        if _saved_stats(cached):
            return _stale_cached_payload(cached, error)
        return _response_payload(
            status=result.status,
            start=start,
            end=end,
            days=days,
            rows=[],
            accounts=[],
            daily=[],
            error=error,
            cache_status="miss",
        )

    if include_daily:
        # The extra comparison days must not inflate the selected-period totals.
        metrics = ("impressions", "views", "contactsMessenger", "contacts", "contactsShowPhone",
                   "contactsShowPhoneAndMessenger", "favorites", "spendKopecks", "orders", "buyouts")
        for row in result.items:
            points = [point for point in result.daily if point.accountId == row.accountId and start <= point.date <= end]
            for metric in metrics:
                values = [getattr(point, metric) for point in points]
                setattr(row, metric, sum(values) if values and all(value is not None for value in values) else None)

    payload = _response_payload(
        status=result.status,
        start=start,
        end=end,
        days=days,
        rows=result.items,
        accounts=result.accounts,
        daily=result.daily,
        error=result.error,
        diagnostics=result.diagnostics,
    )
    save_source_cache(actor.organization_id, source_key, payload)
    return payload
