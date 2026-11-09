from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.chats import AvitoChatsFetchRequest, build_avito_chats_client
from app.avito.listings import AvitoListingsFetchRequest, build_avito_listings_client
from app.avito.reviews import AvitoReviewsFetchRequest, build_avito_reviews_client
from app.avito.stats import AvitoStatsFetchRequest, AvitoStatsItem, build_avito_stats_client
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.repricer_cache.store import get_source_cache, save_source_cache
from app.routers.avito_stats import _cache_key as _stats_cache_key


router = APIRouter(tags=["avito-overview"])

AVITO_OVERVIEW_RATE_LIMIT_COOLDOWN_SECONDS = 70
_ERROR_SECTIONS = ("stats", "listings", "chats", "reviews")
_ERROR_CODES = frozenset({
    "auth_required", "forbidden_scope", "rate_limited", "transport_error",
    "avito_server_error", "avito_request_failed", "avito_reviews_failed",
    "avito_listings_failed", "avito_listing_details_failed", "avito_section_unavailable",
})
_ERROR_BLOCKERS = frozenset({
    "AVITO_AUTH", "AVITO_SCOPE", "AVITO_RATE_LIMIT", "AVITO_STATS",
    "AVITO_CHATS", "AVITO_REVIEWS", "AVITO_RATINGS_SCOPE", "AVITO_LISTINGS",
    "AVITO_LISTING_DETAILS",
})
_STATS_SUMMARY_FIELDS = (
    "impressions", "views", "contacts", "favorites", "spendKopecks", "orders", "buyouts",
    "conversionPct", "orderConversionPct", "buyoutPct",
)


def _has_cached_stats(cached: Any) -> bool:
    if not isinstance(cached, dict) or not isinstance(cached.get("summary"), dict):
        return False
    if cached.get("status") == "synced":
        return True
    source = cached.get("source") if isinstance(cached.get("source"), dict) else {}
    sections = source.get("sections") if isinstance(source.get("sections"), dict) else {}
    stats = sections.get("stats") if isinstance(sections.get("stats"), dict) else {}
    return stats.get("status") == "synced"


def _use_cached_stats(payload: dict[str, Any], cached_stats: dict[str, Any] | None) -> dict[str, Any]:
    if not cached_stats:
        return payload
    payload["summary"].update({key: cached_stats[key] for key in _STATS_SUMMARY_FIELDS if key in cached_stats})
    payload["source"]["cache"]["stats"] = "hit"
    return payload


def _safe_retry_instant(value: Any) -> str | None:
    if type(value) is not str or not value or len(value) > 64:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _safe_section_error(value: Any) -> dict[str, Any]:
    safe = {"code": "avito_section_unavailable", "message": "Avito section unavailable",
            "retryable": True, "blockerIds": []}
    try:
        raw = value if type(value) is dict else {
            key: getattr(value, key, None) for key in ("code", "message", "retryable", "blockerIds")
        }
        rate_limited = _is_rate_limited_error(value if isinstance(value, Exception) else raw)
        code = raw.get("code")
        if type(code) is str and code in _ERROR_CODES:
            safe["code"] = code
        if type(raw.get("retryable")) is bool:
            safe["retryable"] = raw["retryable"]
        blockers = raw.get("blockerIds")
        if type(blockers) in (list, tuple):
            safe["blockerIds"] = sorted({item for item in blockers
                                          if type(item) is str and item in _ERROR_BLOCKERS})
        if rate_limited:
            safe.update(code="rate_limited", message="Avito rate limit is active")
            safe["blockerIds"] = sorted(set(safe["blockerIds"]) | {"AVITO_RATE_LIMIT"})
        instant = _safe_retry_instant(raw.get("retryAfterUntil"))
        if instant is not None:
            safe["retryAfterUntil"] = instant
        seconds = raw.get("retryAfterSeconds")
        if type(seconds) is int and 1 <= seconds <= 2**31 - 1:
            safe["retryAfterSeconds"] = seconds
        return safe
    except Exception:
        return {"code": "avito_section_unavailable", "message": "Avito section unavailable",
                "retryable": True, "blockerIds": []}


def _safe_section_errors(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        return {}
    return {name: _safe_section_error(value[name]) for name in _ERROR_SECTIONS if name in value}


def _safe_cached_diagnostics(cached: dict[str, Any]) -> dict[str, Any]:
    payload = dict(cached)
    original_source = payload.get("source")
    source = dict(original_source) if type(original_source) is dict else {}
    errors = _safe_section_errors(source.get("errors"))
    source["errors"] = errors
    original_cache = source.get("cache")
    cache = dict(original_cache) if type(original_cache) is dict else {}
    if "retryAfterUntil" in cache:
        cache["retryAfterUntil"] = _safe_retry_instant(cache["retryAfterUntil"])
    source["cache"] = cache
    payload["source"] = source
    events = payload.get("events")
    if type(events) is list:
        clean_events = []
        for event in events:
            kind = event.get("kind") if type(event) is dict else None
            if type(kind) is str and kind in {f"{name}_blocked" for name in _ERROR_SECTIONS}:
                name = kind[:-8]
                clean_events.extend(_events(stats_rows=[], listings_summary={}, chats_summary={},
                                            reviews_summary={}, section_errors={name: errors.get(name, {})}))
            else:
                clean_events.append(event)
        payload["events"] = clean_events
    return payload


def _date_range(date_from: date | None, date_to: date | None, period_days: int) -> tuple[date, date, int]:
    end = date_to or date.today()
    start = date_from or (end - timedelta(days=period_days - 1))
    if start > end:
        raise HTTPException(status_code=422, detail="AVITO_OVERVIEW_INVALID_PERIOD")
    days = (end - start).days + 1
    if days > 270:
        raise HTTPException(status_code=422, detail="AVITO_OVERVIEW_PERIOD_TOO_DEEP")
    return start, end, days


def _cache_key(start: date, end: date, account_ids: list[str]) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return f"avito_overview:{start.isoformat()}:{end.isoformat()}:{account_part}"


def _rate_limit_key(account_ids: list[str]) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return f"avito_overview_rate_limit:{account_part}"


def _int_or_zero(value: int | None) -> int:
    return value if value is not None else 0


def _sum_optional(rows: list[AvitoStatsItem], field: str) -> int | None:
    values = [getattr(row, field) for row in rows]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _section_error(exc: Exception) -> dict[str, Any]:
    return _safe_section_error(exc)


def _cache_hit_payload(cached: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_cached_diagnostics(cached)
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "hit"
    source["cache"] = cache
    payload["source"] = source
    return payload


def _stale_cached_payload(cached: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_cached_diagnostics(cached)
    payload["status"] = "partial"
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "stale"
    source["cache"] = cache
    source["errors"] = _safe_section_errors(errors)
    payload["source"] = source
    return payload


def _rate_limit_error(message: str = "Avito rate limit is active") -> dict[str, Any]:
    return {
        "code": "rate_limited",
        "message": "Avito rate limit is active",
        "retryable": True,
        "blockerIds": ["AVITO_RATE_LIMIT"],
    }


def _is_rate_limited_error(error: Any) -> bool:
    if isinstance(error, dict):
        code = str(error.get("code") or "").lower()
        message = str(error.get("message") or "").lower()
        blockers = [str(item).lower() for item in error.get("blockerIds") or []]
        return code == "rate_limited" or "429" in message or "too many requests" in message or "avito_rate_limit" in blockers
    message = str(error).lower()
    return "429" in message or "too many requests" in message or "rate_limited" in message


def _has_rate_limited_section(errors: dict[str, Any]) -> bool:
    return any(_is_rate_limited_error(error) for error in errors.values())


def _active_rate_limit(cooldown: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(cooldown, dict):
        return None
    retry_after_until = _safe_retry_instant(cooldown.get("retryAfterUntil"))
    if not retry_after_until:
        return None
    try:
        retry_at = datetime.fromisoformat(str(retry_after_until).replace("Z", "+00:00"))
    except ValueError:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if retry_at <= now:
        return None
    return {
        **_rate_limit_error(),
        "retryAfterUntil": retry_at.isoformat(),
        "retryAfterSeconds": max(1, int((retry_at - now).total_seconds())),
    }


def _save_rate_limit(organization_id: int, account_ids: list[str], access_token: str, *, message: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    retry_after_until = now + timedelta(seconds=AVITO_OVERVIEW_RATE_LIMIT_COOLDOWN_SECONDS)
    payload = {
        "code": "rate_limited",
        "message": "Avito rate limit is active",
        "retryAfterUntil": retry_after_until.isoformat(),
        "savedAt": now.isoformat(),
    }
    save_source_cache(organization_id, scoped_avito_cache_key(_rate_limit_key(account_ids), access_token), payload)
    return payload


def _empty_rate_limited_payload(start: date, end: date, days: int, error: dict[str, Any]) -> dict[str, Any]:
    error = _safe_section_error(error)
    listings_summary = {"total": 0, "active": 0, "inactive": 0, "removed": 0, "old": 0, "blocked": 0}
    chats_summary = {"total": 0, "unread": 0, "withItems": 0}
    reviews_summary = {"total": 0, "unanswered": 0, "answered": 0, "lowRating": 0}
    return {
        "status": "partial",
        "period": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "days": days},
        "summary": {
            "impressions": None,
            "views": None,
            "contacts": None,
            "favorites": None,
            "spendKopecks": None,
            "orders": None,
            "buyouts": None,
            "conversionPct": None,
            "orderConversionPct": None,
            "buyoutPct": None,
            "totalListings": None,
            "activeListings": None,
            "inactiveListings": None,
            "removedListings": 0,
            "oldListings": 0,
            "blockedListings": 0,
            "chats": 0,
            "unreadChats": 0,
            "reviews": 0,
            "unansweredReviews": 0,
            "answeredReviews": 0,
            "lowReviews": 0,
            "ratingScore": None,
            "blockers": 1,
        },
        "accounts": [],
        "topItems": [],
        "events": _events(
            stats_rows=[],
            listings_summary=listings_summary,
            chats_summary=chats_summary,
            reviews_summary=reviews_summary,
            section_errors={"stats": error},
        ),
        "source": {
            "mode": "live",
            "cache": {"status": "cooldown", "retryAfterUntil": error.get("retryAfterUntil")},
            "errors": {"stats": error},
        },
    }


def _top_items(rows: list[AvitoStatsItem]) -> list[dict[str, Any]]:
    sorted_rows = sorted(rows, key=lambda row: (_int_or_zero(row.views), _int_or_zero(row.contacts), _int_or_zero(row.favorites)), reverse=True)
    return [
        {
            "itemId": row.itemId,
            "title": row.title,
            "accountId": row.accountId,
            "accountName": row.accountName,
            "category": row.category,
            "url": row.url,
            "views": row.views,
            "contacts": row.contacts,
            "favorites": row.favorites,
            "spendKopecks": row.spendKopecks,
            "orders": row.orders,
            "buyouts": row.buyouts,
            "conversionPct": _ratio(row.contacts, row.views),
            "sourceStatus": row.sourceStatus,
        }
        for row in sorted_rows[:8]
    ]


def _event_payload(kind: str, severity: str, source: str, title: str, meta: str, action: str) -> dict[str, str]:
    return {"kind": kind, "severity": severity, "source": source, "title": title, "meta": meta, "action": action}


def _events(*, stats_rows: list[AvitoStatsItem], listings_summary: dict[str, int], chats_summary: dict[str, int], reviews_summary: dict[str, int], section_errors: dict[str, Any]) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    no_contacts = [row for row in stats_rows if row.views is not None and row.views > 0 and row.contacts == 0]
    if no_contacts:
        events.append(
            _event_payload(
                "no_contacts",
                "warn",
                "Статистика",
                f"{len(no_contacts)} объявл. с просмотрами без контактов",
                "Проверь цену, фото и описание в карточках.",
                "Открыть статистику",
            )
        )
    inactive = listings_summary.get("inactive", 0)
    if inactive:
        events.append(
            _event_payload(
                "inactive_listings",
                "warn",
                "Объявления",
                f"{inactive} неактивных объявл. в реестре",
                "Данные Avito core/v1/items за текущий аккаунт.",
                "Открыть объявления",
            )
        )
    unread = chats_summary.get("unread", 0)
    if unread:
        events.append(
            _event_payload("unread_chats", "new", "Сообщения", f"{unread} непрочитанных чатов", "Текущий inbox Avito Messenger.", "Открыть сообщения")
        )
    unanswered = reviews_summary.get("unanswered", 0)
    if unanswered:
        events.append(
            _event_payload("unanswered_reviews", "warn", "Отзывы", f"{unanswered} отзывов без ответа", "Avito Ratings API разрешает ответ.", "Открыть отзывы")
        )
    source_names = {
        "stats": "Статистика",
        "listings": "Объявления",
        "chats": "Сообщения",
        "reviews": "Отзывы",
    }
    for name, error in _safe_section_errors(section_errors).items():
        source_name = source_names.get(name, "Источник")
        if _is_rate_limited_error(error):
            title = f"{source_name}: Авито просит паузу"
            meta = "Мы сохранили паузу и покажем кэш до следующей попытки."
            action = "Обновить позже"
        else:
            title = f"{source_name}: нужна проверка"
            meta = "Avito section unavailable"
            action = "Проверить доступы"
        events.append(
            _event_payload(
                f"{name}_blocked",
                "danger",
                source_name,
                title,
                meta,
                action,
            )
        )
    return events[:8]


@router.get("/api/v1/avito/overview")
def get_avito_overview(
    request: Request,
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
    period_days: int = Query(default=30, alias="periodDays", ge=1, le=270),
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
    rate_limit_key = scoped_avito_cache_key(_rate_limit_key(account_id), access_token)
    active_rate_limit = _active_rate_limit(get_source_cache(actor.organization_id, rate_limit_key, slim=False) or None)
    if active_rate_limit is not None and _has_cached_stats(cached):
        return _stale_cached_payload(cached, {"stats": active_rate_limit})
    if not force_refresh and _has_cached_stats(cached):
        return _cache_hit_payload(cached)
    stats_cache = get_source_cache(
        actor.organization_id,
        scoped_avito_cache_key(_stats_cache_key(start, end, account_id), access_token),
        slim=False,
    ) or None
    cached_stats = (
        stats_cache["summary"]
        if isinstance(stats_cache, dict)
        and isinstance(stats_cache.get("summary"), dict)
        and stats_cache.get("rows")
        and stats_cache.get("status") != "blocked"
        else None
    )
    if active_rate_limit is not None:
        return _use_cached_stats(_empty_rate_limited_payload(start, end, days, active_rate_limit), cached_stats)

    section_errors: dict[str, Any] = {}
    accounts: list[Any] = []
    stats_rows: list[AvitoStatsItem] = []
    stats_result_status = "blocked"
    try:
        stats_client = build_avito_stats_client(
            mode="live",
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
        stats_result = stats_client.fetch_stats(AvitoStatsFetchRequest(dateFrom=start, dateTo=end, accountIds=account_id, grouping="item"))
        stats_result_status = stats_result.status
        accounts = stats_result.accounts
        stats_rows = stats_result.items
        if stats_result.error is not None:
            section_errors["stats"] = _safe_section_error(stats_result.error)
            if _is_rate_limited_error(section_errors["stats"]):
                section_errors["stats"].update(_save_rate_limit(actor.organization_id, account_id, access_token))
    except Exception as exc:
        section_errors["stats"] = _section_error(exc)
        if _is_rate_limited_error(section_errors["stats"]):
            section_errors["stats"].update(_save_rate_limit(actor.organization_id, account_id, access_token))

    listings_summary = {
        "total": sum(int(account.itemCount or 0) for account in accounts) or len(stats_rows),
        "active": sum(int(account.activeItemCount or 0) for account in accounts),
        "inactive": sum(int(account.inactiveItemCount or 0) for account in accounts),
        "removed": 0,
        "old": 0,
        "blocked": 0,
    }
    if not accounts and not stats_rows and not _has_rate_limited_section(section_errors):
        try:
            listings_result = build_avito_listings_client(
                access_token=access_token,
                base_url=settings.avito_api_base_url,
                timeout_seconds=settings.avito_api_timeout_seconds,
            ).fetch_listings(AvitoListingsFetchRequest(dateFrom=start, dateTo=end, accountIds=account_id))
            if listings_result.accounts:
                accounts = listings_result.accounts
            listings_summary = {
                "total": sum(int(account.itemCount or 0) for account in listings_result.accounts) or len(listings_result.rows),
                "active": sum(int(account.activeItemCount or 0) for account in listings_result.accounts),
                "inactive": sum(int(account.inactiveItemCount or 0) for account in listings_result.accounts),
                "removed": sum(1 for row in listings_result.rows if row.status == "removed"),
                "old": sum(1 for row in listings_result.rows if row.status == "old"),
                "blocked": sum(1 for row in listings_result.rows if row.status == "blocked"),
            }
            if listings_result.error is not None:
                section_errors["listings"] = _safe_section_error(listings_result.error)
                if _is_rate_limited_error(section_errors["listings"]):
                    _save_rate_limit(actor.organization_id, account_id, access_token, message=section_errors["listings"].get("message"))
        except Exception as exc:
            section_errors["listings"] = _section_error(exc)
            if _is_rate_limited_error(section_errors["listings"]):
                _save_rate_limit(actor.organization_id, account_id, access_token, message=section_errors["listings"].get("message"))

    chats_summary = {"total": 0, "unread": 0, "withItems": 0}
    try:
        chats_result = build_avito_chats_client(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        ).fetch_chats(AvitoChatsFetchRequest(limit=50, offset=0, accountIds=account_id, includeMessages=False))
        chats_summary = {
            "total": len(chats_result.chats),
            "unread": sum(1 for chat in chats_result.chats if chat.unread),
            "withItems": sum(1 for chat in chats_result.chats if chat.itemId),
        }
        if chats_result.error is not None:
            section_errors["chats"] = _safe_section_error(chats_result.error)
    except Exception as exc:
        section_errors["chats"] = _section_error(exc)

    reviews_summary = {"total": 0, "unanswered": 0, "answered": 0, "lowRating": 0}
    rating: dict[str, Any] | None = None
    try:
        reviews_result = build_avito_reviews_client(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        ).fetch_reviews(AvitoReviewsFetchRequest(limit=50, offset=0))
        reviews_summary = {
            "total": reviews_result.total,
            "unanswered": sum(1 for review in reviews_result.reviews if review.answer is None and review.canAnswer),
            "answered": sum(1 for review in reviews_result.reviews if review.answer is not None),
            "lowRating": sum(1 for review in reviews_result.reviews if review.score <= 3),
        }
        rating = reviews_result.rating.model_dump(mode="json") if reviews_result.rating is not None else None
        if reviews_result.error is not None:
            section_errors["reviews"] = _safe_section_error(reviews_result.error)
    except Exception as exc:
        section_errors["reviews"] = _section_error(exc)

    if _has_cached_stats(cached) and section_errors and (not stats_rows or _has_rate_limited_section(section_errors)):
        return _stale_cached_payload(cached, section_errors)

    impressions = _sum_optional(stats_rows, "impressions")
    views = _sum_optional(stats_rows, "views")
    contacts = _sum_optional(stats_rows, "contacts")
    favorites = _sum_optional(stats_rows, "favorites")
    spend = _sum_optional(stats_rows, "spendKopecks")
    orders = _sum_optional(stats_rows, "orders")
    buyouts = _sum_optional(stats_rows, "buyouts")
    payload = {
        "status": "partial" if section_errors else stats_result_status,
        "period": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "days": days},
        "summary": {
            "impressions": impressions,
            "views": views,
            "contacts": contacts,
            "favorites": favorites,
            "spendKopecks": spend,
            "orders": orders,
            "buyouts": buyouts,
            "conversionPct": _ratio(contacts, views),
            "orderConversionPct": _ratio(orders, contacts),
            "buyoutPct": _ratio(buyouts, orders),
            "totalListings": listings_summary["total"] if accounts or stats_rows else None,
            "activeListings": listings_summary["active"] if accounts or stats_rows else None,
            "inactiveListings": listings_summary["inactive"] if accounts or stats_rows else None,
            "removedListings": listings_summary["removed"],
            "oldListings": listings_summary["old"],
            "blockedListings": listings_summary["blocked"],
            "chats": chats_summary["total"],
            "unreadChats": chats_summary["unread"],
            "reviews": reviews_summary["total"],
            "unansweredReviews": reviews_summary["unanswered"],
            "answeredReviews": reviews_summary["answered"],
            "lowReviews": reviews_summary["lowRating"],
            "ratingScore": rating.get("score") if isinstance(rating, dict) else None,
            "blockers": len(section_errors),
        },
        "accounts": [account.model_dump(mode="json") for account in accounts],
        "topItems": _top_items(stats_rows),
        "events": _events(
            stats_rows=stats_rows,
            listings_summary=listings_summary,
            chats_summary=chats_summary,
            reviews_summary=reviews_summary,
            section_errors=section_errors,
        ),
        "source": {
            "mode": "live",
            "cache": {"status": "fresh", "savedAt": datetime.now(timezone.utc).isoformat()},
            "api": {
                "accountEndpoint": "GET /core/v1/accounts/self",
                "itemsEndpoint": "GET /core/v1/items",
                "statsEndpoint": "POST /stats/v2/accounts/{user_id}/items",
                "chatsEndpoint": "GET /messenger/v2/accounts/{user_id}/chats",
                "reviewsEndpoint": "GET /ratings/v1/reviews",
                "ratingEndpoint": "GET /ratings/v1/info",
            },
            "sections": {
                "stats": {"status": stats_result_status},
                "listings": listings_summary,
                "chats": chats_summary,
                "reviews": reviews_summary,
            },
            "errors": section_errors,
        },
    }
    if "stats" in section_errors and not stats_rows:
        _use_cached_stats(payload, cached_stats)
    if stats_result_status == "synced":
        save_source_cache(actor.organization_id, source_key, payload)
    return payload
