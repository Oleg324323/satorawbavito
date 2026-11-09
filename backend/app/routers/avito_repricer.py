from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.listings import AvitoListingRow, AvitoListingsFetchRequest, build_avito_listings_client
from app.avito.price_apply import LiveAvitoPriceClient
from app.avito.repricer_metrics import row_metrics, enrich_comparisons
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.repricer_cache.store import get_source_cache, save_source_cache
from app.routers.avito_listing_photos import router as photos_router


router = APIRouter(tags=["avito-repricer"])
router.include_router(photos_router)
logger = logging.getLogger(__name__)

AVITO_REPRICER_SETTINGS_KEY = "avito_repricer_settings"
AVITO_REPRICER_PENDING_APPROVALS_KEY = "avito_repricer_price_approvals_pending"
AVITO_REPRICER_STRATEGY_ASSIGNMENTS_KEY = "avito_repricer_strategy_assignments"
AVITO_REPRICER_PRICE_HISTORY_KEY = "avito_repricer_price_history"
AVITO_REPRICER_RATE_LIMIT_COOLDOWN_SECONDS = 70

AVITO_REPRICER_STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "none",
        "name": "Без стратегии",
        "metric": "none",
        "description": "",
        "enabled": False,
    },
    {
        "id": "chat_demand_balanced",
        "name": "Баланс спроса",
        "metric": "contactsMessenger",
        "description": "Базовая стратегия: +5% при сильном спросе, -5-7% при просмотрах без сообщений.",
        "raiseChats": 15,
        "raiseConversionPct": 3.0,
        "raisePct": 5,
        "zeroChatViews": 100,
        "zeroChatLowerPct": -7,
        "lowChatViews": 200,
        "lowChatMax": 2,
        "lowChatLowerPct": -5,
    },
    {
        "id": "chat_recovery_discount",
        "name": "Вернуть спрос",
        "metric": "contactsMessenger",
        "description": "Агрессивнее снижает цену, когда просмотры есть, но люди не пишут.",
        "raiseChats": 18,
        "raiseConversionPct": 4.0,
        "raisePct": 3,
        "zeroChatViews": 80,
        "zeroChatLowerPct": -10,
        "lowChatViews": 150,
        "lowChatMax": 2,
        "lowChatLowerPct": -8,
    },
    {
        "id": "chat_profit_probe",
        "name": "Проверка маржи",
        "metric": "contactsMessenger",
        "description": "Осторожно повышает цену только при очень сильном спросе, снижает мягко.",
        "raiseChats": 25,
        "raiseConversionPct": 5.0,
        "raisePct": 3,
        "zeroChatViews": 180,
        "zeroChatLowerPct": -4,
        "lowChatViews": 260,
        "lowChatMax": 2,
        "lowChatLowerPct": -3,
    },
]


class AvitoRepricerSettingsRequest(BaseModel):
    enabled: bool = True
    autoApplyPricesEnabled: bool = False
    executeIntervalMinutes: int = Field(default=60, ge=5, le=1440)
    periodDays: int = Field(default=30, ge=1, le=270)
    activeWindowEnabled: bool = False
    activeWindowStartHour: int = Field(default=9, ge=0, le=23)
    activeWindowEndHour: int = Field(default=21, ge=0, le=23)
    timezone: str = "Asia/Yekaterinburg"
    maxChangesPerRun: int = Field(default=50, ge=1, le=150)


class AvitoRepricerStrategyAssignRequest(BaseModel):
    strategyId: str = Field(min_length=1)


def default_avito_repricer_settings() -> dict[str, Any]:
    settings = get_settings()
    return {
        "enabled": bool(getattr(settings, "avito_repricer_worker_enabled", True)),
        "autoApplyPricesEnabled": False,
        "executeIntervalMinutes": int(getattr(settings, "avito_repricer_execute_interval_minutes", 60) or 60),
        "periodDays": int(getattr(settings, "avito_repricer_period_days", 30) or 30),
        "activeWindowEnabled": False,
        "activeWindowStartHour": 9,
        "activeWindowEndHour": 21,
        "timezone": "Asia/Yekaterinburg",
        "maxChangesPerRun": 50,
    }


def _normalize_avito_repricer_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    merged = {**default_avito_repricer_settings(), **(payload or {})}
    validated = AvitoRepricerSettingsRequest(**merged)
    return validated.model_dump(mode="json")


def load_avito_repricer_settings(organization_id: int) -> dict[str, Any]:
    cached = get_source_cache(organization_id, AVITO_REPRICER_SETTINGS_KEY, slim=False) or {}
    settings_payload = cached.get("settings") if isinstance(cached, dict) else None
    return _normalize_avito_repricer_settings(settings_payload if isinstance(settings_payload, dict) else None)


def save_avito_repricer_settings(organization_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    settings_payload = _normalize_avito_repricer_settings(payload)
    saved = {
        "settings": settings_payload,
        "savedAt": datetime.now(timezone.utc).isoformat(),
    }
    save_source_cache(organization_id, AVITO_REPRICER_SETTINGS_KEY, saved)
    return settings_payload


def _pending_approvals_payload(organization_id: int) -> dict[str, Any]:
    cached = get_source_cache(organization_id, AVITO_REPRICER_PENDING_APPROVALS_KEY, slim=False) or {}
    items = cached.get("items") if isinstance(cached, dict) else []
    return {"items": items if isinstance(items, list) else [], "loadedAt": datetime.now(timezone.utc).isoformat()}


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _avito_worker_timing_payload(organization_id: int, settings_payload: dict[str, Any], now_iso: str | None = None) -> dict[str, Any]:
    now = _parse_utc_datetime(now_iso) if now_iso else datetime.now(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    cached = get_source_cache(organization_id, "avito_repricer_last_scheduler_run", slim=False) or {}
    last_run_at = _parse_utc_datetime(cached.get("finishedAt") or cached.get("startedAt")) if isinstance(cached, dict) else None
    interval_minutes = int(settings_payload.get("executeIntervalMinutes") or 60)
    next_run_at = last_run_at + timedelta(minutes=interval_minutes) if last_run_at else now
    seconds_until_next = max(0, int((next_run_at - now).total_seconds()))
    return {
        "lastRunAt": last_run_at.isoformat() if last_run_at else None,
        "nextRunAt": next_run_at.isoformat(),
        "secondsUntilNextRun": seconds_until_next,
        "intervalMinutes": interval_minutes,
        "ready": seconds_until_next == 0,
    }


def _price_history_payload(organization_id: int) -> dict[str, Any]:
    cached = get_source_cache(organization_id, AVITO_REPRICER_PRICE_HISTORY_KEY, slim=False) or {}
    items = cached.get("items") if isinstance(cached, dict) else []
    return {"items": items if isinstance(items, list) else [], "loadedAt": datetime.now(timezone.utc).isoformat()}


def append_avito_repricer_price_history(organization_id: int, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        return []
    payload = _price_history_payload(organization_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    prepared: list[dict[str, Any]] = []
    for event in events:
        item_id = str(event.get("itemId") or "")
        if not item_id:
            continue
        happened_at = str(event.get("happenedAt") or now_iso)
        prepared.append(
            {
                "id": str(event.get("id") or f"avito-history-{item_id}-{happened_at}-{len(prepared)}"),
                "happenedAt": happened_at,
                "itemId": item_id,
                "title": event.get("title"),
                "accountId": event.get("accountId"),
                "accountName": event.get("accountName"),
                "imageUrl": event.get("imageUrl"),
                "action": event.get("action") or "recommended",
                "status": event.get("status") or event.get("action") or "recommended",
                "source": event.get("source") or "avito_repricer",
                "approvalId": event.get("approvalId"),
                "oldPriceKopecks": event.get("oldPriceKopecks") if event.get("oldPriceKopecks") is not None else event.get("currentPriceKopecks"),
                "newPriceKopecks": event.get("newPriceKopecks") if event.get("newPriceKopecks") is not None else event.get("recommendedPriceKopecks"),
                "priceDeltaPct": event.get("priceDeltaPct"),
                "strategyId": event.get("strategyId"),
                "strategyName": event.get("strategyName"),
                "strategySignal": event.get("strategySignal"),
                "contactsMessenger": event.get("contactsMessenger"),
                "contacts": event.get("contacts"),
                "views": event.get("views"),
                "messengerConversionPct": event.get("messengerConversionPct"),
                "reason": event.get("reason"),
                "error": event.get("applyError") or event.get("error"),
                "actor": event.get("actor"),
            }
        )
    if not prepared:
        return []
    items = [*prepared, *payload["items"]]
    items = sorted(items, key=lambda item: str(item.get("happenedAt") or ""), reverse=True)[:2000]
    save_source_cache(organization_id, AVITO_REPRICER_PRICE_HISTORY_KEY, {"items": items, "savedAt": now_iso})
    return prepared


def _strategy_by_id(strategy_id: str | None) -> dict[str, Any]:
    return next((strategy for strategy in AVITO_REPRICER_STRATEGIES if strategy["id"] == strategy_id), AVITO_REPRICER_STRATEGIES[0])


def load_avito_repricer_strategy_assignments(organization_id: int) -> dict[str, str]:
    cached = get_source_cache(organization_id, AVITO_REPRICER_STRATEGY_ASSIGNMENTS_KEY, slim=False) or {}
    raw = cached.get("assignments") if isinstance(cached, dict) else {}
    if not isinstance(raw, dict):
        return {}
    valid_ids = {strategy["id"] for strategy in AVITO_REPRICER_STRATEGIES}
    return {str(item_id): str(strategy_id) for item_id, strategy_id in raw.items() if str(strategy_id) in valid_ids}


def save_avito_repricer_strategy_assignment(organization_id: int, item_id: str, strategy_id: str) -> dict[str, Any]:
    strategy = _strategy_by_id(strategy_id)
    assignments = load_avito_repricer_strategy_assignments(organization_id)
    assignments[str(item_id)] = str(strategy["id"])
    payload = {"assignments": assignments, "savedAt": datetime.now(timezone.utc).isoformat()}
    save_source_cache(organization_id, AVITO_REPRICER_STRATEGY_ASSIGNMENTS_KEY, payload)
    return {"itemId": str(item_id), "strategy": strategy, "assignmentsCount": len(assignments)}


def _date_range(date_from: date | None, date_to: date | None, period_days: int) -> tuple[date, date, int]:
    end = date_to or date.today()
    start = date_from or (end - timedelta(days=period_days - 1))
    if start > end:
        raise HTTPException(status_code=422, detail="AVITO_REPRICER_INVALID_PERIOD")
    days = (end - start).days + 1
    if days > 270:
        raise HTTPException(status_code=422, detail="AVITO_REPRICER_PERIOD_TOO_DEEP")
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
    return f"avito_repricer:v2:{start.isoformat()}:{end.isoformat()}:{account_part}"


def _rate_limit_key(account_ids: list[str]) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return f"avito_repricer_rate_limit:{account_part}"


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


def _active_rate_limit(cooldown: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(cooldown, dict):
        return None
    retry_after_until = cooldown.get("retryAfterUntil")
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


def _save_rate_limit(organization_id: int, account_ids: list[str], access_token: str, *, message: str | None = None,
                     error: dict[str, Any] | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    seconds = (error or {}).get("retryAfterSeconds")
    seconds = max(1, seconds) if type(seconds) is int and seconds >= 0 else AVITO_REPRICER_RATE_LIMIT_COOLDOWN_SECONDS
    retry_after_until = now + timedelta(seconds=seconds)
    payload = {
        "code": "rate_limited",
        "message": "Avito HTTP 429",
        "retryAfterUntil": retry_after_until.isoformat(),
        "savedAt": now.isoformat(),
    }
    save_source_cache(organization_id, scoped_avito_cache_key(_rate_limit_key(account_ids), access_token), payload)
    return payload


def _safe_source_error(error: Any) -> dict[str, Any] | None:
    """Explicit diagnostic projection, never a recursive secret-key redactor."""
    if error is None:
        return None
    if isinstance(error, BaseModel):
        try:
            error = error.model_dump(mode="json")
        except Exception:
            error = None
    raw = error if type(error) is dict else {}
    messages = {
        "auth_required": "Avito authentication required",
        "forbidden_scope": "Avito permission required",
        "rate_limited": "Avito rate limit is active",
        "avito_server_error": "Avito server error",
        "avito_request_failed": "Avito request failed",
        "transport_error": "Avito transport error",
    }
    code = raw.get("code")
    if type(code) is not str or code not in messages:
        code = "avito_request_failed"
    blockers = raw.get("blockerIds")
    allowed = ("AVITO_AUTH", "AVITO_SCOPE", "AVITO_RATE_LIMIT", "AVITO_STATS", "AVITO_LISTINGS", "AVITO_CHATS")
    result = {"code": code, "message": messages[code],
              "retryable": raw.get("retryable") if type(raw.get("retryable")) is bool else False,
              "blockerIds": [v for v in blockers if type(v) is str and v in allowed] if type(blockers) is list else []}
    until = raw.get("retryAfterUntil")
    if type(until) is str:
        try:
            parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None:
                result["retryAfterUntil"] = parsed.isoformat()
        except ValueError:
            pass
    seconds = raw.get("retryAfterSeconds")
    if type(seconds) is int and seconds >= 0:
        result["retryAfterSeconds"] = seconds
    return result


def _safe_source_diagnostics(diagnostics: Any) -> dict[str, Any] | None:
    if type(diagnostics) is not dict:
        return None
    result = {}
    for key in ("groupings", "dataTotalCount", "itemsCount", "pages"):
        value = diagnostics.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    status = diagnostics.get("status")
    if type(status) is int and 100 <= status <= 599:
        result["status"] = status
    return result


def _rounded_kopecks(value: int) -> int:
    return max(0, int(round(value / 100) * 100))


def _low_chat_conversion_threshold(strategy: dict[str, Any]) -> float:
    low_chat_views = max(1, int(strategy["lowChatViews"]))
    return round(int(strategy["lowChatMax"]) / low_chat_views * 100, 2)


def _repricer_decision(row: AvitoListingRow, strategy: dict[str, Any] | None = None) -> dict[str, Any]:
    active_strategy = strategy or AVITO_REPRICER_STRATEGIES[0]
    current = row.priceKopecks
    chats = row.contactsMessenger
    views = row.views
    chat_conversion_pct = _ratio(chats, views)
    if active_strategy["id"] == "none":
        return {
            "strategySignal": "keep",
            "recommendedPriceKopecks": current,
            "priceDeltaPct": 0,
            "reason": "Стратегия не назначена: цена не меняется автоматически.",
        }
    if current is None or row.status != "active":
        return {
            "strategySignal": "blocked",
            "recommendedPriceKopecks": current,
            "priceDeltaPct": 0,
            "reason": "Цена меняется только у активных объявлений с текущей ценой.",
        }
    if chats is None:
        return {
            "strategySignal": "no_data",
            "recommendedPriceKopecks": current,
            "priceDeltaPct": 0,
            "reason": "Avito не вернул contactsMessenger, авто-решение не применяется.",
        }
    if chats >= int(active_strategy["raiseChats"]) or (chat_conversion_pct is not None and chat_conversion_pct >= float(active_strategy["raiseConversionPct"])):
        return {
            "strategySignal": "raise",
            "recommendedPriceKopecks": _rounded_kopecks(int(current * (1 + int(active_strategy["raisePct"]) / 100))),
            "priceDeltaPct": int(active_strategy["raisePct"]),
            "reason": f"{active_strategy['name']}: спрос по чатам позволяет проверить повышение цены.",
        }
    if views is not None and views >= int(active_strategy["zeroChatViews"]) and chats == 0:
        return {
            "strategySignal": "lower",
            "recommendedPriceKopecks": _rounded_kopecks(int(current * (1 + int(active_strategy["zeroChatLowerPct"]) / 100))),
            "priceDeltaPct": int(active_strategy["zeroChatLowerPct"]),
            "reason": f"{active_strategy['name']}: просмотры есть, но в чат не пишут.",
        }
    if views is not None and views >= int(active_strategy["lowChatViews"]) and (
        chats <= int(active_strategy["lowChatMax"])
        or (chat_conversion_pct is not None and chat_conversion_pct <= _low_chat_conversion_threshold(active_strategy))
    ):
        return {
            "strategySignal": "lower",
            "recommendedPriceKopecks": _rounded_kopecks(int(current * (1 + int(active_strategy["lowChatLowerPct"]) / 100))),
            "priceDeltaPct": int(active_strategy["lowChatLowerPct"]),
            "reason": f"{active_strategy['name']}: чатов мало при заметном трафике.",
        }
    return {
        "strategySignal": "keep",
        "recommendedPriceKopecks": current,
        "priceDeltaPct": 0,
        "reason": "Сигнал по чатам в норме, цену лучше оставить.",
    }


def _row_payload(row: AvitoListingRow, assignments: dict[str, str] | None = None) -> dict[str, Any]:
    strategy = _strategy_by_id((assignments or {}).get(row.itemId))
    decision = _repricer_decision(row, strategy)
    return {
        **row.model_dump(mode="json"),
        **decision,
        **row_metrics(row.model_dump()),
        "strategyId": strategy["id"],
        "strategyName": strategy["name"],
        "strategyDescription": strategy["description"],
        "messengerConversionPct": _ratio(row.contactsMessenger, row.views),
        "contactConversionPct": _ratio(row.contacts, row.views),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(rows),
        "active": sum(1 for row in rows if row.get("status") == "active"),
        "blockedListings": sum(1 for row in rows if row.get("status") == "blocked"),
        "activePriceKopecks": sum(row["priceKopecks"] for row in rows if row.get("status") == "active" and row.get("priceKopecks") is not None),
        "raiseCandidates": sum(1 for row in rows if row.get("strategySignal") == "raise"),
        "lowerCandidates": sum(1 for row in rows if row.get("strategySignal") == "lower"),
        "keepCandidates": sum(1 for row in rows if row.get("strategySignal") == "keep"),
        "blocked": sum(1 for row in rows if row.get("strategySignal") == "blocked"),
        "noData": sum(1 for row in rows if row.get("strategySignal") == "no_data"),
        "impressions": sum(_metric(row.get("impressions")) for row in rows),
        "views": sum(_metric(row.get("views")) for row in rows),
        "contactsMessenger": sum(_metric(row.get("contactsMessenger")) for row in rows),
        "contacts": sum(_metric(row.get("contacts")) for row in rows),
        "contactsShowPhone": sum(_metric(row.get("contactsShowPhone")) for row in rows),
        "contactsShowPhoneAndMessenger": sum(_metric(row.get("contactsShowPhoneAndMessenger")) for row in rows),
    }


def _response_payload(
    *,
    status: str,
    start: date,
    end: date,
    days: int,
    rows: list[AvitoListingRow],
    accounts: list[Any],
    cache_status: str = "fresh",
    diagnostics: dict[str, Any] | None = None,
    error: Any | None = None,
    organization_id: int | None = None,
) -> dict[str, Any]:
    assignments = load_avito_repricer_strategy_assignments(organization_id) if organization_id is not None else {}
    payload_rows = [_row_payload(row, assignments) for row in rows]
    payload_rows.sort(key=lambda item: (_metric(item.get("views")), _metric(item.get("contactsMessenger")), _metric(item.get("contacts"))), reverse=True)
    settings = get_settings()
    repricer_settings = default_avito_repricer_settings()
    return {
        "status": status,
        "period": {"dateFrom": start.isoformat(), "dateTo": end.isoformat(), "days": days},
        "strategies": AVITO_REPRICER_STRATEGIES,
        "strategyAssignments": assignments,
        "summary": _summary(payload_rows),
        "accounts": [account.model_dump(mode="json") for account in accounts],
        "rows": payload_rows,
        "source": {
            "mode": "live",
            "cache": {"status": cache_status, "savedAt": datetime.now(timezone.utc).isoformat()},
            "api": {
                "baseUrl": settings.avito_api_base_url,
                "itemsEndpoint": "GET /core/v1/items",
                "statsEndpoint": "POST /stats/v2/accounts/{user_id}/items",
                "priceUpdateEndpoint": "POST /core/v1/items/{item_id}/update_price",
                "priceApplyEnabled": bool(getattr(settings, "avito_repricer_price_apply_enabled", False)),
                "primaryMetric": "contactsMessenger",
                "maxItemIdsPerRequest": 1000,
                "maxDepthDays": 270,
            },
            "repricerSettings": repricer_settings,
            "diagnostics": _safe_source_diagnostics(diagnostics),
            "error": _safe_source_error(error),
        },
    }


def _rows_with_current_strategy_assignments(rows: list[Any], assignments: dict[str, str]) -> list[dict[str, Any]]:
    updated_rows: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            listing = AvitoListingRow.model_validate(raw)
        except Exception:
            updated_rows.append(raw)
            continue
        strategy = _strategy_by_id(assignments.get(listing.itemId))
        updated_rows.append(
            {
                **raw,
                **row_metrics(raw),
                **_repricer_decision(listing, strategy),
                "strategyId": strategy["id"],
                "strategyName": strategy["name"],
                "strategyDescription": strategy["description"],
                "messengerConversionPct": _ratio(listing.contactsMessenger, listing.views),
                "contactConversionPct": _ratio(listing.contacts, listing.views),
            }
        )
    updated_rows.sort(key=lambda item: (_metric(item.get("views")), _metric(item.get("contactsMessenger")), _metric(item.get("contacts"))), reverse=True)
    return updated_rows


def _cache_hit_payload(cached: dict[str, Any], organization_id: int | None = None) -> dict[str, Any]:
    payload = dict(cached)
    if organization_id is not None and isinstance(payload.get("rows"), list):
        assignments = load_avito_repricer_strategy_assignments(organization_id)
        rows = _rows_with_current_strategy_assignments(payload["rows"], assignments)
        payload["rows"] = rows
        payload["strategyAssignments"] = assignments
        payload["summary"] = _summary(rows)
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "hit"
    source["cache"] = cache
    # An inventory-only partial snapshot must not become a successful report
    # merely because it was read from cache.
    source["error"] = _safe_source_error(source.get("error"))
    source["diagnostics"] = _safe_source_diagnostics(source.get("diagnostics"))
    payload["source"] = source
    return payload


def _stale_cached_payload(cached: dict[str, Any], error: dict[str, Any], organization_id: int | None = None) -> dict[str, Any]:
    error = _safe_source_error(error) or {}
    payload = _cache_hit_payload(cached, organization_id)
    payload["status"] = "partial"
    source = dict(payload.get("source") or {})
    cache = dict(source.get("cache") or {})
    cache["status"] = "stale"
    cache["retryAfterUntil"] = error.get("retryAfterUntil")
    source["cache"] = cache
    source["error"] = error
    payload["source"] = source
    return payload


@router.get("/api/v1/avito/repricer/settings")
def get_avito_repricer_settings(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    settings = get_settings()
    repricer_settings = load_avito_repricer_settings(actor.organization_id)
    return {
        "settings": repricer_settings,
        "mode": {
            "workerEnvEnabled": bool(getattr(settings, "avito_repricer_worker_enabled", True)),
            "priceApplyEnvEnabled": bool(getattr(settings, "avito_repricer_price_apply_enabled", False)),
        },
        "workerTiming": _avito_worker_timing_payload(actor.organization_id, repricer_settings),
    }


@router.put("/api/v1/avito/repricer/settings")
def put_avito_repricer_settings(request: Request, payload: AvitoRepricerSettingsRequest) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not (has_permission(actor, "settings:write") or has_permission(actor, "team:write") or has_permission(actor, "price:send")):
        raise HTTPException(status_code=403, detail="NO_ACCESS:settings:write")
    settings = save_avito_repricer_settings(actor.organization_id, payload.model_dump(mode="json"))
    return {"settings": settings, "saved": True}


@router.get("/api/v1/avito/repricer/price-approvals/pending")
def get_avito_repricer_pending_approvals(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    return _pending_approvals_payload(actor.organization_id)


@router.get("/api/v1/avito/repricer/items/{itemId}/price-history")
def get_avito_repricer_item_price_history(request: Request, itemId: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    payload = _price_history_payload(actor.organization_id)
    normalized_item_id = str(itemId)
    items = [item for item in payload["items"] if isinstance(item, dict) and str(item.get("itemId")) == normalized_item_id]
    items = sorted(items, key=lambda item: str(item.get("happenedAt") or ""), reverse=True)[:limit]
    return {"itemId": normalized_item_id, "items": items, "loadedAt": payload["loadedAt"]}


def _save_pending_approvals(organization_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {"items": items, "savedAt": datetime.now(timezone.utc).isoformat()}
    save_source_cache(organization_id, AVITO_REPRICER_PENDING_APPROVALS_KEY, payload)
    return payload


@router.post("/api/v1/avito/repricer/price-approvals/{approvalId}/reject")
def reject_avito_repricer_price_approval(request: Request, approvalId: str) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not (has_permission(actor, "settings:write") or has_permission(actor, "team:write") or has_permission(actor, "price:send")):
        raise HTTPException(status_code=403, detail="NO_ACCESS:price:send")
    payload = _pending_approvals_payload(actor.organization_id)
    updated = []
    rejected = None
    for item in payload["items"]:
        if item.get("approvalId") == approvalId:
            rejected = {**item, "status": "rejected", "resolvedAt": datetime.now(timezone.utc).isoformat()}
        else:
            updated.append(item)
    if rejected is None:
        raise HTTPException(status_code=404, detail="AVITO_APPROVAL_NOT_FOUND")
    _save_pending_approvals(actor.organization_id, updated)
    append_avito_repricer_price_history(
        actor.organization_id,
        [
            {
                **rejected,
                "action": "rejected",
                "status": "rejected",
                "source": "manual_approval",
                "actor": actor.user_id,
                "happenedAt": rejected["resolvedAt"],
            }
        ],
    )
    return {"approval": rejected, "pendingCount": len(updated)}


@router.post("/api/v1/avito/repricer/price-approvals/{approvalId}/approve")
def approve_avito_repricer_price_approval(request: Request, approvalId: str) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not (has_permission(actor, "settings:write") or has_permission(actor, "team:write") or has_permission(actor, "price:send")):
        raise HTTPException(status_code=403, detail="NO_ACCESS:price:send")
    settings = get_settings()
    if not bool(getattr(settings, "avito_repricer_price_apply_enabled", False)):
        raise HTTPException(status_code=409, detail={"code": "AVITO_PRICE_APPLY_DISABLED", "message": "Avito price apply is disabled by VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED"})
    payload = _pending_approvals_payload(actor.organization_id)
    approval = next((item for item in payload["items"] if item.get("approvalId") == approvalId), None)
    if approval is None:
        raise HTTPException(status_code=404, detail="AVITO_APPROVAL_NOT_FOUND")
    credentials = get_user_avito_credentials_secret(actor.user_id) or get_organization_avito_credentials_secret(actor.organization_id)
    if credentials is None:
        raise HTTPException(status_code=409, detail={"code": "AVITO_CREDENTIALS_REQUIRED", "message": "Avito credentials are required to apply a price"})
    apply_failed = False
    try:
        access_token = resolve_user_avito_access_token(
            user_id=actor.user_id,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
        result = LiveAvitoPriceClient(
            access_token=access_token,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        ).update_price(str(approval.get("itemId") or ""), int(approval.get("recommendedPriceKopecks") or 0))
    except Exception:
        apply_failed = True
    if apply_failed:
        logger.warning("AVITO_PRICE_APPLY_FAILED")
        raise HTTPException(status_code=409, detail={"code": "AVITO_PRICE_APPLY_FAILED", "message": "Avito price apply failed"}) from None
    updated = [item for item in payload["items"] if item.get("approvalId") != approvalId]
    _save_pending_approvals(actor.organization_id, updated)
    approved = {**approval, "status": "approved", "resolvedAt": datetime.now(timezone.utc).isoformat()}
    append_avito_repricer_price_history(
        actor.organization_id,
        [
            {
                **approved,
                "action": "approved",
                "status": "approved",
                "source": "manual_approval",
                "actor": actor.user_id,
                "happenedAt": approved["resolvedAt"],
            }
        ],
    )
    return {"approval": approved, "result": result, "pendingCount": len(updated)}


@router.get("/api/v1/avito/repricer")
def get_avito_repricer(
    request: Request,
    date_from: date | None = Query(default=None, alias="dateFrom"),
    date_to: date | None = Query(default=None, alias="dateTo"),
    period_days: int = Query(default=30, alias="periodDays", ge=1, le=270),
    account_id: list[str] = Query(default_factory=list, alias="accountId"),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
    include_analytics: bool = Query(default=False, alias="includeAnalytics"),
) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    start, end, days = _date_range(date_from, date_to, period_days)
    credentials = get_user_avito_credentials_secret(actor.user_id) or get_organization_avito_credentials_secret(actor.organization_id)
    if credentials is None:
        raise HTTPException(status_code=409, detail="AVITO_CREDENTIALS_REQUIRED")

    settings = get_settings()
    # Credential-scoped history survives routine OAuth token renewal, but is
    # invalidated when the user connects different credentials.
    scope = credentials.client_id + "\0" + credentials.client_secret
    source_key = scoped_avito_cache_key(_cache_key(start, end, account_id), scope)
    rate_limit_key = scoped_avito_cache_key(_rate_limit_key(account_id), scope)
    cached = get_source_cache(actor.organization_id, source_key, slim=False) or None
    active_rate_limit = _active_rate_limit(get_source_cache(actor.organization_id, rate_limit_key, slim=False) or None)

    def enriched(payload, *, allow_fetch=False, access_token=None):
        if include_analytics is not True or not payload.get("rows"):
            return payload
        client = (build_avito_listings_client(access_token=access_token, base_url=settings.avito_api_base_url,
                                             timeout_seconds=settings.avito_api_timeout_seconds)
                  if allow_fetch else None)
        return enrich_comparisons(payload, actor.organization_id, scope, client, allow_fetch=allow_fetch)

    # Opening a saved report is a database read, including partial snapshots.
    # Only the explicit refresh button retries a failed collection.
    if isinstance(cached, dict) and cached.get("rows"):
        if active_rate_limit is not None:
            return enriched(_stale_cached_payload(cached, active_rate_limit, actor.organization_id))
        if not force_refresh:
            return enriched(_cache_hit_payload(cached, actor.organization_id))

    oauth_failed = False
    try:
        access_token = resolve_user_avito_access_token(
            user_id=actor.user_id,
            credentials=credentials,
            base_url=settings.avito_api_base_url,
            timeout_seconds=settings.avito_api_timeout_seconds,
        )
    except Exception:
        oauth_failed = True
    if oauth_failed:
        if isinstance(cached, dict) and cached.get("rows"):
            return enriched(_stale_cached_payload(cached, {"code": "auth_required"}, actor.organization_id))
        raise HTTPException(status_code=409, detail="AVITO_OAUTH_FAILED") from None

    # Migrate only this exact period/account and the currently verified token;
    # never scan other organizations, credential scopes, or reporting periods.
    if not cached:
        cached = get_source_cache(actor.organization_id, scoped_avito_cache_key(_cache_key(start, end, account_id), access_token), slim=False) or None
        if isinstance(cached, dict) and cached.get("rows"):
            save_source_cache(actor.organization_id, source_key, cached)
    active_rate_limit = active_rate_limit or _active_rate_limit(
        get_source_cache(actor.organization_id, scoped_avito_cache_key(_rate_limit_key(account_id), access_token), slim=False) or None)
    if active_rate_limit is not None:
        if isinstance(cached, dict) and cached.get("rows"):
            return enriched(_stale_cached_payload(cached, active_rate_limit, actor.organization_id))
        return _response_payload(
            status="blocked",
            start=start,
            end=end,
            days=days,
            rows=[],
            accounts=[],
            cache_status="cooldown",
            error=active_rate_limit,
            organization_id=actor.organization_id,
        )
    if not force_refresh and isinstance(cached, dict) and cached.get("rows"):
        return enriched(_cache_hit_payload(cached, actor.organization_id))

    client = build_avito_listings_client(
        access_token=access_token,
        base_url=settings.avito_api_base_url,
        timeout_seconds=settings.avito_api_timeout_seconds,
    )
    result = client.fetch_listings(AvitoListingsFetchRequest(dateFrom=start, dateTo=end, accountIds=account_id))
    error_payload = result.error.model_dump(mode="json") if result.error is not None else None
    rate_limited = _is_rate_limited_error(error_payload)
    error_payload = _safe_source_error(error_payload)
    if rate_limited:
        saved_limit = _save_rate_limit(actor.organization_id, account_id, scope, error=error_payload)
        error_payload = {**_rate_limit_error(), **saved_limit}
    # A failed refresh must not replace valid metrics with inventory-only rows.
    if error_payload and isinstance(cached, dict) and cached.get("rows"):
        return enriched(_stale_cached_payload(cached, error_payload, actor.organization_id))
    payload = _response_payload(
        status=result.status,
        start=start,
        end=end,
        days=days,
        rows=result.rows,
        accounts=result.accounts,
        diagnostics=result.diagnostics,
        error=error_payload or result.error,
        organization_id=actor.organization_id,
    )
    if result.status != "blocked":
        save_source_cache(actor.organization_id, source_key, payload)
    return enriched(payload, allow_fetch=not bool(error_payload), access_token=access_token)


@router.get("/api/v1/avito/repricer/strategies")
def get_avito_repricer_strategies(request: Request) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not has_permission(actor, "cabinet:read"):
        raise HTTPException(status_code=403, detail="NO_ACCESS:cabinet:read")
    return {
        "strategies": AVITO_REPRICER_STRATEGIES,
        "assignments": load_avito_repricer_strategy_assignments(actor.organization_id),
    }


@router.put("/api/v1/avito/repricer/items/{itemId}/strategy")
def put_avito_repricer_item_strategy(request: Request, itemId: str, payload: AvitoRepricerStrategyAssignRequest) -> dict[str, Any]:
    actor = actor_from_request(request)
    if not (has_permission(actor, "settings:write") or has_permission(actor, "team:write") or has_permission(actor, "price:send")):
        raise HTTPException(status_code=403, detail="NO_ACCESS:settings:write")
    if payload.strategyId not in {strategy["id"] for strategy in AVITO_REPRICER_STRATEGIES}:
        raise HTTPException(status_code=422, detail="AVITO_REPRICER_UNKNOWN_STRATEGY")
    return save_avito_repricer_strategy_assignment(actor.organization_id, itemId, payload.strategyId)
