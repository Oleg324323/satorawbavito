from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.listings import AvitoListingsFetchRequest, AvitoListingRow, build_avito_listings_client
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.repricer_cache.store import get_source_cache, save_source_cache


router = APIRouter(tags=["avito-listings"])


def _date_range(date_from: date | None, date_to: date | None, period_days: int) -> tuple[date, date, int]:
    end = date_to or date.today()
    start = date_from or (end - timedelta(days=period_days - 1))
    if start > end:
        raise HTTPException(status_code=422, detail="AVITO_LISTINGS_INVALID_PERIOD")
    days = (end - start).days + 1
    if days > 270:
        raise HTTPException(status_code=422, detail="AVITO_LISTINGS_PERIOD_TOO_DEEP")
    return start, end, days


def _metric(value: int | None) -> int:
    return value if value is not None else 0


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _cache_key(start: date, end: date, account_ids: list[str]) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return f"avito_listings_v2:{start.isoformat()}:{end.isoformat()}:{account_part}"


def _row_payload(row: AvitoListingRow) -> dict[str, Any]:
    return {
        **row.model_dump(mode="json"),
        "contactConversionPct": _ratio(row.contacts, row.views),
    }


def _summary(rows: list[AvitoListingRow], accounts: list[Any]) -> dict[str, Any]:
    return {
        "total": sum(int(getattr(account, "itemCount", 0) or 0) for account in accounts) or len(rows),
        "active": sum(int(getattr(account, "activeItemCount", 0) or 0) for account in accounts),
        "inactive": sum(int(getattr(account, "inactiveItemCount", 0) or 0) for account in accounts),
        "removed": sum(1 for row in rows if row.status == "removed"),
        "old": sum(1 for row in rows if row.status == "old"),
        "blocked": sum(1 for row in rows if row.status == "blocked"),
        "partial": sum(1 for row in rows if row.sourceStatus != "fresh"),
        "views": sum(_metric(row.views) for row in rows),
        "contactsMessenger": sum(_metric(row.contactsMessenger) for row in rows),
        "contacts": sum(_metric(row.contacts) for row in rows),
        "contactsShowPhone": sum(_metric(row.contactsShowPhone) for row in rows),
        "contactsShowPhoneAndMessenger": sum(_metric(row.contactsShowPhoneAndMessenger) for row in rows),
        "favorites": sum(_metric(row.favorites) for row in rows),
        "spendKopecks": sum(_metric(row.spendKopecks) for row in rows),
        "orders": sum(_metric(row.orders) for row in rows),
        "buyouts": sum(_metric(row.buyouts) for row in rows),
    }


def _response_payload(
    *,
    status: str,
    start: date,
    end: date,
    days: int,
    rows: list[AvitoListingRow],
    accounts: list[Any],
    page_size: int,
    cache_status: str = "fresh",
    diagnostics: dict[str, Any] | None = None,
    error: Any | None = None,
) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda item: (_metric(item.views), _metric(item.contacts), _metric(item.favorites)), reverse=True)
    return {
        "status": status,
        "period": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "days": days},
        "summary": _summary(sorted_rows, accounts),
        "accounts": [account.model_dump(mode="json") for account in accounts],
        "rows": [_row_payload(row) for row in sorted_rows[:page_size]],
        "source": {
            "mode": "live",
            "cache": {"status": cache_status, "savedAt": datetime.now(timezone.utc).isoformat()},
            "api": {
                "itemsEndpoint": "GET /core/v1/items",
                "statsEndpoint": "POST /stats/v2/accounts/{user_id}/items",
                "itemStatuses": "active,removed,old",
                "maxDepthDays": 270,
            },
            "diagnostics": diagnostics,
            "error": error,
        },
    }


def _cache_hit_payload(cached: dict[str, Any]) -> dict[str, Any]:
    payload = dict(cached)
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "hit"
    source["cache"] = cache
    source["error"] = None
    payload["source"] = source
    return payload


@router.get("/api/v1/avito/listings")
def get_avito_listings(
    request: Request,
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
    period_days: int = Query(default=30, alias="periodDays", ge=1, le=270),
    page_size: int = Query(default=100, alias="pageSize", ge=1, le=500),
    account_id: list[str] = Query(default_factory=list, alias="accountId"),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    start, end, days = _date_range(date_from, date_to, period_days)
    credentials = get_user_avito_credentials_secret(actor.user_id) or get_organization_avito_credentials_secret(actor.organization_id)
    if credentials is None:
        raise HTTPException(status_code=409, detail="AVITO_CREDENTIALS_REQUIRED")

    settings = get_settings()
    try:
        access_token = resolve_user_avito_access_token(
            user_id=actor.user_id,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="AVITO_OAUTH_FAILED") from exc

    source_key = scoped_avito_cache_key(_cache_key(start, end, account_id), access_token)
    cached = get_source_cache(actor.organization_id, source_key, slim=False) or None
    if not force_refresh and isinstance(cached, dict) and cached.get("rows"):
        return _cache_hit_payload(cached)

    client = build_avito_listings_client(
        access_token=access_token,
        base_url=settings.avito_api_base_url,
        timeout_seconds=settings.avito_api_timeout_seconds,
    )
    result = client.fetch_listings(AvitoListingsFetchRequest(dateFrom=start, dateTo=end, accountIds=account_id))
    payload = _response_payload(
        status=result.status,
        start=start,
        end=end,
        days=days,
        rows=result.rows,
        accounts=result.accounts,
        page_size=page_size,
        diagnostics=result.diagnostics,
        error=result.error.model_dump(mode="json") if hasattr(result.error, "model_dump") else result.error,
    )
    if result.status != "blocked":
        save_source_cache(actor.organization_id, source_key, payload)
    return payload
