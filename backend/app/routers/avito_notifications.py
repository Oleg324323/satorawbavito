from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.chats import AvitoChatsFetchRequest, build_avito_chats_client
from app.avito.listings import AvitoListingsFetchRequest, build_avito_listings_client
from app.avito.notifications import AvitoNotificationsResult, build_avito_notifications_result
from app.avito.orders import AvitoOrdersFetchRequest, build_avito_orders_client
from app.avito.reviews import AvitoReviewsFetchRequest, build_avito_reviews_client
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.repricer_cache.store import get_source_cache, save_source_cache


router = APIRouter(tags=["avito-notifications"])


def _date_from(value: date | None, period_days: int) -> tuple[date, int]:
    start = value or (date.today() - timedelta(days=period_days - 1))
    days = (date.today() - start).days + 1
    if days <= 0:
        raise HTTPException(status_code=422, detail="AVITO_NOTIFICATIONS_INVALID_PERIOD")
    if days > 183:
        raise HTTPException(status_code=422, detail="AVITO_NOTIFICATIONS_PERIOD_TOO_DEEP")
    return start, days


def _cache_key(start: date, limit: int, account_ids: list[str]) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return f"avito_notifications:{start.isoformat()}:l{limit}:{account_part}"


def _read_marks(organization_id: int, read_key: str) -> dict[str, str]:
    cached = get_source_cache(organization_id, read_key, slim=False) or {}
    marks = cached.get("marks") if isinstance(cached, dict) else {}
    return {str(key): str(value) for key, value in marks.items()} if isinstance(marks, dict) else {}


def _save_read_state(organization_id: int, read_key: str, *, marks: dict[str, str], visible_ids: list[str] | None = None) -> None:
    cached = get_source_cache(organization_id, read_key, slim=False) or {}
    payload = dict(cached) if isinstance(cached, dict) else {}
    payload["marks"] = marks
    if visible_ids is not None:
        payload["visibleIds"] = visible_ids
    save_source_cache(organization_id, read_key, payload)


def _cache_hit_payload(cached: dict[str, Any], read_marks: dict[str, str]) -> dict[str, Any]:
    payload = dict(cached)
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "hit"
    source["cache"] = cache
    payload["source"] = source
    items = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        if next_item.get("id") in read_marks:
            next_item["readAt"] = read_marks[str(next_item["id"])]
        items.append(next_item)
    payload["items"] = items
    payload["summary"] = {
        **(payload.get("summary") or {}),
        "unread": sum(1 for item in items if not item.get("readAt")),
    }
    return payload


def _section_error(exc: Exception) -> dict[str, Any]:
    return {"code": exc.__class__.__name__, "message": str(exc), "retryable": True}


def _credentials_or_error(request: Request) -> tuple[Any, Any, str]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
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
    read_key = scoped_avito_cache_key("avito_notifications:read_marks", f"{credentials.client_id}\0{credentials.client_secret}")
    return actor, settings, access_token, read_key


@router.get("/api/v1/avito/notifications")
def get_avito_notifications(
    request: Request,
    date_from: date | None = Query(default=None, alias="dateFrom"),
    period_days: int = Query(default=30, alias="periodDays", ge=1, le=183),
    limit: int = Query(default=80, ge=1, le=200),
    account_id: list[str] = Query(default_factory=list, alias="accountId"),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
) -> dict[str, Any]:
    actor, settings, access_token, read_key = _credentials_or_error(request)
    start, days = _date_from(date_from, period_days)
    source_key = scoped_avito_cache_key(_cache_key(start, limit, account_id), access_token)
    read_marks = _read_marks(actor.organization_id, read_key)
    cached = get_source_cache(actor.organization_id, source_key, slim=False) or None
    if not force_refresh and isinstance(cached, dict) and cached.get("status") != "blocked" and isinstance(cached.get("items"), list):
        return _cache_hit_payload(cached, read_marks)

    errors: dict[str, Any] = {}
    chats = None
    orders = None
    reviews = None
    listings = None

    try:
        chats = build_avito_chats_client(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        ).fetch_chats(AvitoChatsFetchRequest(limit=50, offset=0, accountIds=account_id, includeMessages=False))
        if chats.error is not None:
            errors["chats"] = chats.error.model_dump(mode="json")
    except Exception as exc:
        errors["chats"] = _section_error(exc)

    try:
        orders = build_avito_orders_client(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        ).fetch_orders(AvitoOrdersFetchRequest(dateFrom=start, limit=20, page=1))
        if orders.error is not None:
            errors["orders"] = orders.error.model_dump(mode="json")
    except Exception as exc:
        errors["orders"] = _section_error(exc)

    try:
        reviews = build_avito_reviews_client(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        ).fetch_reviews(AvitoReviewsFetchRequest(limit=50, offset=0))
        if reviews.error is not None:
            errors["reviews"] = reviews.error.model_dump(mode="json")
    except Exception as exc:
        errors["reviews"] = _section_error(exc)

    try:
        listings = build_avito_listings_client(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        ).fetch_listings(AvitoListingsFetchRequest(dateFrom=start, dateTo=date.today(), accountIds=account_id))
        if listings.error is not None:
            errors["listings"] = listings.error.model_dump(mode="json")
    except Exception as exc:
        errors["listings"] = _section_error(exc)

    result: AvitoNotificationsResult = build_avito_notifications_result(
        chats=chats,
        orders=orders,
        reviews=reviews,
        listings=listings,
        errors=errors,
        read_marks=read_marks,
        limit=limit,
    )
    payload = result.model_dump(mode="json")
    payload["period"] = {"dateFrom": start.isoformat(), "days": days}
    payload["source"]["cache"]["status"] = "fresh"
    _save_read_state(actor.organization_id, read_key, marks=read_marks, visible_ids=[item["id"] for item in payload["items"] if item.get("id")])
    if result.status != "blocked":
        save_source_cache(actor.organization_id, source_key, payload)
    return payload


@router.post("/api/v1/avito/notifications/{notification_id}/read")
def mark_avito_notification_read(notification_id: str, request: Request) -> dict[str, Any]:
    actor, _settings, _access_token, read_key = _credentials_or_error(request)
    marks = _read_marks(actor.organization_id, read_key)
    marks[notification_id] = datetime.now(timezone.utc).isoformat()
    _save_read_state(actor.organization_id, read_key, marks=marks)
    return {"ok": True, "id": notification_id, "readAt": marks[notification_id]}


@router.post("/api/v1/avito/notifications/read-all")
def mark_all_avito_notifications_read(request: Request) -> dict[str, Any]:
    actor, _settings, _access_token, read_key = _credentials_or_error(request)
    marks = _read_marks(actor.organization_id, read_key)
    cached_rows = []
    # Mark visible cached notifications; freshly fetched ids will be added one-by-one if new events arrive later.
    cached = get_source_cache(actor.organization_id, read_key, slim=False) or {}
    visible = cached.get("visibleIds") if isinstance(cached, dict) else None
    now = datetime.now(timezone.utc).isoformat()
    if isinstance(visible, list):
        cached_rows = [str(item) for item in visible if item]
    for item_id in cached_rows:
        marks[item_id] = now
    _save_read_state(actor.organization_id, read_key, marks=marks)
    return {"ok": True, "readAt": now, "count": len(cached_rows)}
