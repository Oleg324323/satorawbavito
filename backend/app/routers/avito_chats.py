from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.avito.auth import resolve_user_avito_access_token, scoped_avito_cache_key
from app.avito.chats import AvitoChatsFetchRequest, AvitoChatsUpstreamError, AvitoChatRow, AvitoMessageRow, build_avito_chats_client
from app.cabinet.store import get_organization_avito_credentials_secret, get_user_avito_credentials_secret
from app.config import get_settings
from app.control_plane.auth import actor_from_request, has_permission
from app.repricer_cache.store import get_source_cache, save_source_cache


router = APIRouter(tags=["avito-chats"])


class AvitoSendMessagePayload(BaseModel):
    accountId: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=1000)


class AvitoChatActionPayload(BaseModel):
    accountId: str = Field(min_length=1)


def _cache_key(limit: int, offset: int, unread_only: bool, account_ids: list[str], access_token: str) -> str:
    account_part = ",".join(sorted(account_ids)) if account_ids else "all"
    return scoped_avito_cache_key(f"avito_chats:{limit}:{offset}:{'unread' if unread_only else 'all'}:{account_part}", access_token)


def _response_payload(
    *,
    status: str,
    chats: list[AvitoChatRow],
    messages: dict[str, list[AvitoMessageRow]],
    account_id: str | None,
    account_name: str | None,
    cache_status: str = "fresh",
    error: Any | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    sorted_chats = sorted(chats, key=lambda chat: chat.updatedAt or chat.lastMessageAt or "", reverse=True)
    return {
        "status": status,
        "summary": {
            "total": len(sorted_chats),
            "unread": sum(1 for chat in sorted_chats if chat.unread),
            "withItems": sum(1 for chat in sorted_chats if chat.itemId),
            "messages": sum(len(rows) for rows in messages.values()),
        },
        "account": {"accountId": account_id, "accountName": account_name},
        "chats": [chat.model_dump(mode="json") for chat in sorted_chats],
        "messages": {chat_id: [message.model_dump(mode="json") for message in rows] for chat_id, rows in messages.items()},
        "source": {
            "mode": "live",
            "cache": {"status": cache_status, "savedAt": datetime.now(timezone.utc).isoformat()},
            "api": {
                "baseUrl": settings.avito_api_base_url,
                "tokenEndpoint": "POST /token",
                "accountEndpoint": "GET /core/v1/accounts/self",
                "chatsEndpoint": "GET /messenger/v2/accounts/{user_id}/chats",
                "chatEndpoint": "GET /messenger/v2/accounts/{user_id}/chats/{chat_id}",
                "messagesEndpoint": "GET /messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
                "sendTextEndpoint": "POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
                "readEndpoint": "POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/read",
                "uploadImagesEndpoint": "POST /messenger/v1/accounts/{user_id}/uploadImages",
                "sendImageEndpoint": "POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages/image",
                "webhookEndpoint": "POST /messenger/v3/webhook",
            },
            "diagnostics": diagnostics,
            "error": error.model_dump(mode="json") if hasattr(error, "model_dump") else error,
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


def _credentials_or_error(request: Request):
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
    return actor, settings, access_token


@router.get("/api/v1/avito/chats")
def get_avito_chats(
    request: Request,
    limit: int = Query(default=50, ge=1, le=99),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    account_id: list[str] = Query(default_factory=list, alias="accountId"),
    force_refresh: bool = Query(default=False, alias="forceRefresh"),
) -> dict[str, Any]:
    actor, settings, access_token = _credentials_or_error(request)
    source_key = _cache_key(limit, offset, unread_only, account_id, access_token)
    cached = get_source_cache(actor.organization_id, source_key, slim=False) or None
    if not force_refresh and isinstance(cached, dict) and cached.get("chats"):
        return _cache_hit_payload(cached)

    client = build_avito_chats_client(access_token=access_token, base_url=settings.avito_api_base_url, timeout_seconds=settings.avito_api_timeout_seconds)
    result = client.fetch_chats(AvitoChatsFetchRequest(limit=limit, offset=offset, unreadOnly=unread_only, accountIds=account_id))
    diagnostics = {
        **(result.diagnostics or {}),
        "dateFiltering": "disabled",
        "requested": {"limit": limit, "offset": offset, "unreadOnly": unread_only, "accountIds": account_id},
    }
    payload = _response_payload(
        status=result.status,
        chats=result.chats,
        messages=result.messages,
        account_id=result.accountId,
        account_name=result.accountName,
        diagnostics=diagnostics,
        error=result.error,
    )
    if result.status != "blocked":
        save_source_cache(actor.organization_id, source_key, payload)
    return payload


@router.post("/api/v1/avito/chats/{chat_id}/messages")
def send_avito_chat_message(chat_id: str, payload: AvitoSendMessagePayload, request: Request) -> dict[str, Any]:
    _actor, settings, access_token = _credentials_or_error(request)
    client = build_avito_chats_client(access_token=access_token, base_url=settings.avito_api_base_url, timeout_seconds=settings.avito_api_timeout_seconds)
    try:
        message = client.send_text_message(payload.accountId, chat_id, payload.text.strip())
    except AvitoChatsUpstreamError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.error.model_dump(mode="json")) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "message": message.model_dump(mode="json")}


@router.post("/api/v1/avito/chats/{chat_id}/read")
def mark_avito_chat_read(chat_id: str, payload: AvitoChatActionPayload, request: Request) -> dict[str, Any]:
    _actor, settings, access_token = _credentials_or_error(request)
    client = build_avito_chats_client(access_token=access_token, base_url=settings.avito_api_base_url, timeout_seconds=settings.avito_api_timeout_seconds)
    try:
        ok = client.mark_chat_read(payload.accountId, chat_id)
    except AvitoChatsUpstreamError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.error.model_dump(mode="json")) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": ok}
