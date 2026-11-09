from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

try:
    import httpx
except Exception:  # pragma: no cover - live mode dependency
    httpx = None  # type: ignore[assignment]


AvitoChatsFetchStatus = Literal["synced", "blocked"]
AvitoMessageDirection = Literal["in", "out", "note"]


class AvitoChatsFetchRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=99)
    offset: int = Field(default=0, ge=0)
    unreadOnly: bool = False
    chatTypes: str = "u2i,u2u,a2u"
    accountIds: list[str] = Field(default_factory=list)
    includeMessages: bool = True


class AvitoMessageRow(BaseModel):
    messageId: str = Field(min_length=1)
    chatId: str = Field(min_length=1)
    authorId: str | None = None
    direction: AvitoMessageDirection = "in"
    type: str = "text"
    text: str = ""
    imageUrl: str | None = None
    createdAt: str | None = None
    isRead: bool | None = None


class AvitoChatRow(BaseModel):
    chatId: str = Field(min_length=1)
    accountId: str = Field(min_length=1)
    accountName: str = Field(min_length=1)
    buyerId: str | None = None
    buyerName: str = "Покупатель"
    itemId: str | None = None
    itemTitle: str | None = None
    itemUrl: str | None = None
    itemPrice: str | None = None
    preview: str = ""
    unread: bool = False
    chatType: str | None = None
    lastMessageAt: str | None = None
    updatedAt: str | None = None


class AvitoChatsFetchError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    blockerIds: list[str] = Field(default_factory=list)


class AvitoChatsFetchResult(BaseModel):
    status: AvitoChatsFetchStatus
    accountId: str | None = None
    accountName: str | None = None
    chats: list[AvitoChatRow] = Field(default_factory=list)
    messages: dict[str, list[AvitoMessageRow]] = Field(default_factory=dict)
    error: AvitoChatsFetchError | None = None
    diagnostics: dict[str, Any] | None = None


class AvitoChatsUpstreamError(RuntimeError):
    def __init__(self, error: AvitoChatsFetchError, http_status: int) -> None:
        super().__init__(error.message)
        self.error = error
        self.http_status = http_status


class AvitoChatsClient(Protocol):
    def fetch_chats(self, request: AvitoChatsFetchRequest) -> AvitoChatsFetchResult:
        ...

    def send_text_message(self, account_id: str, chat_id: str, text: str) -> AvitoMessageRow:
        ...

    def mark_chat_read(self, account_id: str, chat_id: str) -> bool:
        ...


def _iso_from_unix(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _content_text(content: Any, message_type: str) -> tuple[str, str | None]:
    if not isinstance(content, dict):
        return "", None
    if isinstance(content.get("text"), str):
        return content["text"], None
    if message_type == "image":
        sizes = content.get("image", {}).get("sizes") if isinstance(content.get("image"), dict) else None
        if isinstance(sizes, dict):
            image_url = str(sizes.get("1280x960") or sizes.get("640x480") or sizes.get("140x105") or next(iter(sizes.values()), ""))
            return "Изображение", image_url or None
    if message_type == "item":
        item = content.get("item")
        if isinstance(item, dict):
            return str(item.get("title") or "Объявление"), item.get("image_url")
    if message_type == "call":
        return "Звонок Avito", None
    if message_type == "voice":
        return "Голосовое сообщение", None
    return message_type or "Сообщение", None


def _raw_preview(payload: Any) -> dict[str, Any]:
    if type(payload) is dict:
        rows = payload.get("chats") or payload.get("result") or payload.get("messages") or []
        return {
            "type": "dict",
            "rowCount": len(rows) if type(rows) is list else None,
        }
    if type(payload) is list:
        return {
            "type": "list",
            "rowCount": len(payload),
        }
    return {"type": "other"}


def _debug_payload(label: str, payload: Any) -> None:
    try:
        print("[AVITO_CHATS_BODY]", json.dumps(_raw_preview(payload), ensure_ascii=False), flush=True)
    except Exception:
        pass  # Diagnostics failure must not print arbitrary fallback objects.


def _safe_http_status(value: Any) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


class LiveAvitoChatsClient:
    def __init__(self, access_token: str, base_url: str = "https://api.avito.ru", timeout_seconds: float = 20.0) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for LiveAvitoChatsClient")
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json", "Content-Type": "application/json"}

    def fetch_chats(self, request: AvitoChatsFetchRequest, http_client: Any | None = None) -> AvitoChatsFetchResult:
        try:
            if http_client is not None:
                return self._fetch_chats_with_client(request, http_client)
            with httpx.Client(timeout=self.timeout_seconds) as client:
                return self._fetch_chats_with_client(request, client)
        except AvitoChatsUpstreamError as exc:
            return AvitoChatsFetchResult(status="blocked", error=exc.error)
        except httpx.HTTPStatusError as exc:
            return AvitoChatsFetchResult(status="blocked", error=self._http_error(exc))
        except Exception:
            return AvitoChatsFetchResult(status="blocked", error=AvitoChatsFetchError(code="transport_error", message="Avito transport error", retryable=True, blockerIds=["AVITO_CHATS"]))

    def _fetch_chats_with_client(self, request: AvitoChatsFetchRequest, client: Any) -> AvitoChatsFetchResult:
        account_id, account_name = self._account(client, request.accountIds)
        params: dict[str, Any] = {"limit": min(request.limit, 99), "offset": request.offset, "chat_types": request.chatTypes}
        if request.unreadOnly:
            params["unread_only"] = True
        response = client.get(f"{self.base_url}/messenger/v2/accounts/{account_id}/chats", params=params, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        _debug_payload("[AVITO_CHATS_BODY]", payload)
        chats = [self._chat_row(raw, account_id, account_name) for raw in self._chat_rows(payload)]
        messages: dict[str, list[AvitoMessageRow]] = {}
        message_failures: list[dict[str, Any]] = []
        if request.includeMessages:
            for chat in chats:
                try:
                    messages[chat.chatId] = self._messages(client, account_id, chat.chatId)
                except httpx.HTTPStatusError as exc:
                    messages[chat.chatId] = self._fallback_messages_from_chat(chat)
                    failure = self._http_error(exc).model_dump(mode="json")
                    failure["httpStatus"] = _safe_http_status(exc.response.status_code)
                    message_failures.append(failure)
                except (httpx.RequestError, RuntimeError, ValueError, TypeError):
                    # One failed conversation must not erase the successfully
                    # fetched chat list. Never expose raw transport errors/URLs.
                    messages[chat.chatId] = self._fallback_messages_from_chat(chat)
                    message_failures.append({"code": "messages_unavailable", "retryable": True,
                                             "message": "Conversation messages could not be loaded"})
        return AvitoChatsFetchResult(
            status="synced",
            accountId=account_id,
            accountName=account_name,
            chats=chats,
            messages=messages,
            diagnostics={
                "chatsCount": len(chats),
                "messagesCount": sum(len(rows) for rows in messages.values()),
                "rawChats": _raw_preview(payload),
                "rawMessages": None,
                "messagesFailed": message_failures,
            },
        )

    def _account(self, client: Any, account_ids: list[str]) -> tuple[str, str]:
        if len(account_ids) > 1 or any(re.fullmatch(r"[1-9][0-9]{0,19}", value) is None for value in account_ids):
            raise self._account_scope_error()
        response = client.get(f"{self.base_url}/core/v1/accounts/self", headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Avito account response is invalid")
        account_id = str(payload.get("id") or payload.get("user_id") or payload.get("userId") or "")
        if re.fullmatch(r"[1-9][0-9]{0,19}", account_id) is None:
            raise ValueError("Avito account response does not include account id")
        if account_ids and account_ids[0] != account_id:
            raise self._account_scope_error()
        account_name = str(payload.get("name") or payload.get("profile", {}).get("name") or account_id)
        return account_id, account_name

    @staticmethod
    def _account_scope_error() -> AvitoChatsUpstreamError:
        return AvitoChatsUpstreamError(AvitoChatsFetchError(
            code="account_scope_denied", message="Аккаунт Авито не совпадает с подключенным.",
            retryable=False, blockerIds=["AVITO_SCOPE"],
        ), 403)

    @staticmethod
    def _chat_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            rows = payload.get("chats") or payload.get("result") or []
            return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def _chat_row(self, raw: dict[str, Any], account_id: str, account_name: str) -> AvitoChatRow:
        last_message = raw.get("last_message") if isinstance(raw.get("last_message"), dict) else {}
        text, _image_url = _content_text(last_message.get("content"), str(last_message.get("type") or ""))
        users = raw.get("users") if isinstance(raw.get("users"), list) else []
        buyer = next((user for user in users if isinstance(user, dict) and str(user.get("id") or "") != account_id), {})
        context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
        context_value = context.get("value") if isinstance(context.get("value"), dict) else {}
        return AvitoChatRow(
            chatId=str(raw.get("id") or ""),
            accountId=account_id,
            accountName=account_name,
            buyerId=str(buyer.get("id")) if buyer.get("id") is not None else None,
            buyerName=str(buyer.get("name") or "Покупатель"),
            itemId=str(context_value.get("id")) if context_value.get("id") is not None else None,
            itemTitle=context_value.get("title"),
            itemUrl=context_value.get("url"),
            itemPrice=context_value.get("price_string"),
            preview=text,
            unread=last_message.get("direction") == "in" and not bool(last_message.get("is_read") or last_message.get("read")),
            chatType=context.get("type"),
            lastMessageAt=_iso_from_unix(last_message.get("created")),
            updatedAt=_iso_from_unix(raw.get("updated")),
        )

    @staticmethod
    def _fallback_messages_from_chat(chat: AvitoChatRow) -> list[AvitoMessageRow]:
        if not chat.preview:
            return []
        return [
            AvitoMessageRow(
                messageId=f"{chat.chatId}:last",
                chatId=chat.chatId,
                authorId=chat.buyerId,
                direction="in" if chat.unread else "note",
                type="text",
                text=chat.preview,
                createdAt=chat.lastMessageAt or chat.updatedAt,
                isRead=not chat.unread,
            )
        ]

    def _messages(self, client: Any, account_id: str, chat_id: str, diagnostics: dict[str, Any] | None = None) -> list[AvitoMessageRow]:
        response = client.get(f"{self.base_url}/messenger/v3/accounts/{account_id}/chats/{chat_id}/messages/", params={"limit": 99, "offset": 0}, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        # Never index debug output by provider-derived chat identity.
        rows = payload if isinstance(payload, list) else payload.get("messages") if isinstance(payload, dict) else []
        return [self._message_row(chat_id, row) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _message_row(chat_id: str, raw: dict[str, Any]) -> AvitoMessageRow:
        message_type = str(raw.get("type") or "text")
        text, image_url = _content_text(raw.get("content"), message_type)
        return AvitoMessageRow(
            messageId=str(raw.get("id") or ""),
            chatId=chat_id,
            authorId=str(raw.get("author_id")) if raw.get("author_id") is not None else None,
            direction=raw.get("direction") if raw.get("direction") in {"in", "out"} else "in",
            type=message_type,
            text=text,
            imageUrl=image_url,
            createdAt=_iso_from_unix(raw.get("created")),
            isRead=raw.get("is_read") if isinstance(raw.get("is_read"), bool) else None,
        )

    def send_text_message(self, account_id: str, chat_id: str, text: str, http_client: Any | None = None) -> AvitoMessageRow:
        body = {"type": "text", "message": {"text": text}}
        try:
            if http_client is not None:
                self._account(http_client, [account_id])
                response = http_client.post(f"{self.base_url}/messenger/v1/accounts/{account_id}/chats/{chat_id}/messages", json=body, headers=self._headers())
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    self._account(client, [account_id])
                    response = client.post(f"{self.base_url}/messenger/v1/accounts/{account_id}/chats/{chat_id}/messages", json=body, headers=self._headers())
            response.raise_for_status()
            return self._message_row(chat_id, response.json())
        except httpx.HTTPStatusError as exc:
            raise AvitoChatsUpstreamError(self._http_error(exc), _safe_http_status(exc.response.status_code) or 502) from None

    def mark_chat_read(self, account_id: str, chat_id: str, http_client: Any | None = None) -> bool:
        try:
            if http_client is not None:
                self._account(http_client, [account_id])
                response = http_client.post(f"{self.base_url}/messenger/v1/accounts/{account_id}/chats/{chat_id}/read", headers=self._headers())
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    self._account(client, [account_id])
                    response = client.post(f"{self.base_url}/messenger/v1/accounts/{account_id}/chats/{chat_id}/read", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
            return bool(payload.get("ok", True)) if isinstance(payload, dict) else True
        except httpx.HTTPStatusError as exc:
            raise AvitoChatsUpstreamError(self._http_error(exc), _safe_http_status(exc.response.status_code) or 502) from None

    @staticmethod
    def _http_error(exc: Any) -> AvitoChatsFetchError:
        status_code = _safe_http_status(exc.response.status_code)
        if status_code is None:
            return AvitoChatsFetchError(code="avito_request_failed", message="Avito request failed", retryable=True, blockerIds=["AVITO_CHATS"])
        if status_code == 401:
            return AvitoChatsFetchError(code="auth_required", message="Avito HTTP 401", retryable=False, blockerIds=["AVITO_AUTH"])
        if status_code == 403:
            return AvitoChatsFetchError(code="forbidden_scope", message="Avito HTTP 403", retryable=False, blockerIds=["AVITO_SCOPE"])
        if status_code == 429:
            return AvitoChatsFetchError(code="rate_limited", message="Avito HTTP 429", retryable=True, blockerIds=["AVITO_RATE_LIMIT"])
        return AvitoChatsFetchError(code="avito_request_failed", message=f"Avito HTTP {status_code}", retryable=status_code >= 500, blockerIds=["AVITO_CHATS"])


def build_avito_chats_client(access_token: str, base_url: str = "https://api.avito.ru", timeout_seconds: float = 20.0) -> AvitoChatsClient:
    return LiveAvitoChatsClient(access_token=access_token, base_url=base_url, timeout_seconds=timeout_seconds)
