from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.avito.chats import (
    AvitoChatRow,
    AvitoChatsFetchRequest,
    AvitoChatsFetchResult,
    AvitoChatsUpstreamError,
    AvitoMessageRow,
    LiveAvitoChatsClient,
)
from app.cabinet.store import AvitoCredentialsSecret
from app.main import create_app


class AvitoChatsHttpResponse:
    def __init__(self, payload: dict | list, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.avito.ru/test")
            response = httpx.Response(self.status_code, request=request, json=self.payload)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)

    def json(self) -> dict | list:
        return self.payload


class RecordingAvitoChatsHttpClient:
    def __init__(self, payloads: list[dict | list | tuple[dict | list, int]]) -> None:
        self.payloads = list(payloads)
        self.gets: list[dict] = []
        self.posts: list[dict] = []

    def get(self, url: str, **kwargs) -> AvitoChatsHttpResponse:
        self.gets.append({"url": url, **kwargs})
        payload = self.payloads.pop(0)
        if isinstance(payload, tuple):
            body, status_code = payload
            return AvitoChatsHttpResponse(body, status_code)
        return AvitoChatsHttpResponse(payload)

    def post(self, url: str, **kwargs) -> AvitoChatsHttpResponse:
        self.posts.append({"url": url, **kwargs})
        payload = self.payloads.pop(0)
        if isinstance(payload, tuple):
            body, status_code = payload
            return AvitoChatsHttpResponse(body, status_code)
        return AvitoChatsHttpResponse(payload)


class RecordingAvitoChatsClient:
    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token
        self.fetch_requests: list[AvitoChatsFetchRequest] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.read_chats: list[tuple[str, str]] = []

    def fetch_chats(self, request: AvitoChatsFetchRequest) -> AvitoChatsFetchResult:
        self.fetch_requests.append(request)
        return AvitoChatsFetchResult(
            status="synced",
            accountId="365024549",
            accountName="Bless T",
            chats=[
                AvitoChatRow(
                    chatId="chat-1",
                    accountId="365024549",
                    accountName="Bless T",
                    buyerId="94235311",
                    buyerName="Ирина",
                    itemId="8098482225",
                    itemTitle="Худи yohji yamamoto",
                    itemUrl="https://avito.ru/item/8098482225",
                    itemPrice="4 900 ₽",
                    preview="Здравствуйте, актуально?",
                    unread=True,
                    lastMessageAt="2026-07-28T12:00:00+00:00",
                    updatedAt="2026-07-28T12:00:00+00:00",
                )
            ],
            messages={
                "chat-1": [
                    AvitoMessageRow(
                        messageId="msg-1",
                        chatId="chat-1",
                        authorId="94235311",
                        direction="in",
                        type="text",
                        text="Здравствуйте, актуально?",
                        createdAt="2026-07-28T12:00:00+00:00",
                        isRead=False,
                    )
                ]
            },
        )

    def send_text_message(self, account_id: str, chat_id: str, text: str) -> AvitoMessageRow:
        self.sent_messages.append((account_id, chat_id, text))
        return AvitoMessageRow(
            messageId="out-1",
            chatId=chat_id,
            authorId=account_id,
            direction="out",
            type="text",
            text=text,
            createdAt="2026-07-28T12:01:00+00:00",
            isRead=True,
        )

    def mark_chat_read(self, account_id: str, chat_id: str) -> bool:
        self.read_chats.append((account_id, chat_id))
        return True


def _patch_router_auth(monkeypatch) -> None:
    actor = SimpleNamespace(user_id="viewer", organization_id=10)
    credentials = AvitoCredentialsSecret(client_id="avito_client_0987654321", client_secret="avito_secret_1234567890", cached_access_token=None, access_token_expires_at=None)
    monkeypatch.setattr("app.routers.avito_chats.actor_from_request", lambda _request: actor)
    monkeypatch.setattr("app.routers.avito_chats.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr("app.routers.avito_chats.get_user_avito_credentials_secret", lambda _user_id: credentials)
    monkeypatch.setattr("app.routers.avito_chats.get_organization_avito_credentials_secret", lambda _organization_id: None)


def test_live_avito_chats_client_uses_v2_chats_v3_messages_and_v1_send_payload():
    http_client = RecordingAvitoChatsHttpClient(
        [
            {"id": 365024549, "name": "Bless T"},
            {
                "chats": [
                    {
                        "id": "chat-1",
                        "updated": 1785230400,
                        "created": 1785226800,
                        "context": {"type": "item", "value": {"id": 8098482225, "title": "Худи", "price_string": "4 900 ₽", "url": "https://avito.ru/i"}},
                        "users": [{"id": 94235311, "name": "Ирина"}],
                        "last_message": {"id": "msg-1", "direction": "in", "type": "text", "created": 1785230400, "content": {"text": "Здравствуйте"}},
                    }
                ]
            },
            [
                {"id": "msg-1", "author_id": 94235311, "direction": "in", "type": "text", "created": 1785230400, "content": {"text": "Здравствуйте"}, "is_read": False}
            ],
            {"id": 365024549, "name": "Bless T"},
            {"id": "out-1", "direction": "out", "type": "text", "created": 1785230460, "content": {"text": "Да, актуально"}},
            {"id": 365024549, "name": "Bless T"},
            {"ok": True},
        ]
    )
    client = LiveAvitoChatsClient(access_token="token", base_url="https://api.avito.ru")

    result = client.fetch_chats(AvitoChatsFetchRequest(limit=50, offset=0), http_client=http_client)
    sent = client.send_text_message("365024549", "chat-1", "Да, актуально", http_client=http_client)
    marked_read = client.mark_chat_read("365024549", "chat-1", http_client=http_client)

    assert result.status == "synced"
    assert result.chats[0].chatId == "chat-1"
    assert result.chats[0].buyerName == "Ирина"
    assert result.messages["chat-1"][0].text == "Здравствуйте"
    assert http_client.gets[0]["url"] == "https://api.avito.ru/core/v1/accounts/self"
    assert http_client.gets[1]["url"] == "https://api.avito.ru/messenger/v2/accounts/365024549/chats"
    assert http_client.gets[1]["params"] == {"limit": 50, "offset": 0, "chat_types": "u2i,u2u,a2u"}
    assert http_client.gets[2]["url"] == "https://api.avito.ru/messenger/v3/accounts/365024549/chats/chat-1/messages/"
    assert http_client.posts[0]["url"] == "https://api.avito.ru/messenger/v1/accounts/365024549/chats/chat-1/messages"
    assert http_client.posts[0]["json"] == {"type": "text", "message": {"text": "Да, актуально"}}
    assert sent.messageId == "out-1"
    assert marked_read is True
    assert http_client.posts[1]["url"] == "https://api.avito.ru/messenger/v1/accounts/365024549/chats/chat-1/read"


def test_live_avito_chats_client_keeps_chats_when_messages_endpoint_fails():
    http_client = RecordingAvitoChatsHttpClient(
        [
            {"id": 365024549, "name": "Bless T"},
            {
                "chats": [
                    {
                        "id": "chat-1",
                        "updated": 1785230400,
                        "created": 1785226800,
                        "context": {"type": "item", "value": {"id": 8098482225, "title": "Худи", "price_string": "4 900 ₽", "url": "https://avito.ru/i"}},
                        "users": [{"id": 94235311, "name": "Ирина"}],
                        "last_message": {"id": "msg-1", "direction": "in", "type": "text", "created": 1785230400, "content": {"text": "Здравствуйте"}},
                    }
                ]
            },
            ({"error": {"message": "Forbidden"}}, 403),
        ]
    )
    client = LiveAvitoChatsClient(access_token="token", base_url="https://api.avito.ru")

    result = client.fetch_chats(AvitoChatsFetchRequest(limit=50, offset=0), http_client=http_client)

    assert result.status == "synced"
    assert result.chats[0].chatId == "chat-1"
    assert result.messages["chat-1"][0].text == "Здравствуйте"
    assert result.messages["chat-1"][0].messageId == "chat-1:last"
    assert result.diagnostics is not None
    assert len(result.diagnostics["messagesFailed"]) == 1
    assert result.diagnostics["messagesFailed"][0]["httpStatus"] == 403
    assert "chat-1" not in str(result.diagnostics["messagesFailed"])


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("private URL"), RuntimeError("LOCAL_READ_ENDPOINT_NOT_ALLOWED"), ValueError("private payload")])
def test_message_failure_does_not_erase_other_chats(monkeypatch, failure):
    http_client = RecordingAvitoChatsHttpClient([
        {"id": 123, "name": "Test account"},
        {"chats": [
            {"id": "u2i~synthetic", "last_message": {"type": "text", "content": {"text": "Last preview"}}},
            {"id": "second-chat"},
        ]},
    ])
    client = LiveAvitoChatsClient(access_token="synthetic")
    def messages(_http, _account, chat_id):
        if chat_id == "u2i~synthetic":
            raise failure
        return [AvitoMessageRow(messageId="message", chatId=chat_id, text="Second chat loaded")]
    monkeypatch.setattr(client, "_messages", messages)
    result = client.fetch_chats(AvitoChatsFetchRequest(), http_client=http_client)
    assert result.status == "synced"
    assert len(result.chats) == 2
    assert result.messages["u2i~synthetic"][0].text == "Last preview"
    assert result.messages["second-chat"][0].text == "Second chat loaded"
    assert len(result.diagnostics["messagesFailed"]) == 1
    assert "private" not in str(result.diagnostics)
    assert "u2i~synthetic" not in str(result.diagnostics)


def test_live_avito_chats_client_preserves_send_access_errors():
    http_client = RecordingAvitoChatsHttpClient([{"id": 365024549}, ({"error": {"message": "subscription required"}}, 403)])
    client = LiveAvitoChatsClient(access_token="token", base_url="https://api.avito.ru")

    try:
        client.send_text_message("365024549", "chat-1", "Да", http_client=http_client)
    except AvitoChatsUpstreamError as exc:
        assert exc.http_status == 403
        assert exc.error.code == "forbidden_scope"
    else:
        raise AssertionError("Expected AvitoChatsUpstreamError")


def test_chat_client_rejects_an_account_outside_the_token_before_read_or_send():
    client = LiveAvitoChatsClient(access_token="token")
    fetch_http = RecordingAvitoChatsHttpClient([{"id": 365024549, "name": "Bless T"}])
    result = client.fetch_chats(AvitoChatsFetchRequest(accountIds=["999"]), http_client=fetch_http)
    assert result.status == "blocked"
    assert result.error.code == "account_scope_denied"
    assert [request["url"] for request in fetch_http.gets] == ["https://api.avito.ru/core/v1/accounts/self"]

    send_http = RecordingAvitoChatsHttpClient([{"id": 365024549, "name": "Bless T"}])
    with pytest.raises(AvitoChatsUpstreamError) as error:
        client.send_text_message("999", "chat-1", "Да", http_client=send_http)
    assert error.value.http_status == 403
    assert error.value.error.code == "account_scope_denied"
    assert send_http.posts == []

    read_http = RecordingAvitoChatsHttpClient([{"id": 365024549, "name": "Bless T"}])
    with pytest.raises(AvitoChatsUpstreamError) as error:
        client.mark_chat_read("999", "chat-1", http_client=read_http)
    assert error.value.http_status == 403
    assert read_http.posts == []


def test_avito_chats_endpoint_uses_saved_credentials_and_caches_payload(monkeypatch):
    recording_client: RecordingAvitoChatsClient | None = None
    cached_payloads: list[tuple[int, str, dict]] = []

    def build_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingAvitoChatsClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_chats.build_avito_chats_client", build_client)
    monkeypatch.setattr("app.routers.avito_chats.resolve_user_avito_access_token", lambda **_kwargs: "avito-live-token")
    monkeypatch.setattr("app.routers.avito_chats.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.avito_chats.save_source_cache", lambda organization_id, source_key, payload: cached_payloads.append((organization_id, source_key, payload)))
    _patch_router_auth(monkeypatch)
    api = TestClient(create_app())

    response = api.get("/api/v1/avito/chats", params={"limit": 50})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["unread"] == 1
    assert payload["chats"][0]["chatId"] == "chat-1"
    assert payload["messages"]["chat-1"][0]["text"] == "Здравствуйте, актуально?"
    assert payload["source"]["api"]["chatsEndpoint"] == "GET /messenger/v2/accounts/{user_id}/chats"
    assert recording_client is not None
    assert recording_client.access_token == "avito-live-token"
    assert cached_payloads[0][1].startswith("avito_chats:50:0:all:all:")
    assert "avito-live-token" not in cached_payloads[0][1]
    assert "cacheKey" not in payload["source"]["diagnostics"]


def test_chat_cache_is_bound_to_the_current_avito_token(monkeypatch):
    token = ["first-token"]
    cache: dict[str, dict] = {}
    fetched: list[str] = []

    def build_client(*_args, **kwargs):
        fetched.append(kwargs["access_token"])
        return RecordingAvitoChatsClient(access_token=kwargs["access_token"])

    monkeypatch.setattr("app.routers.avito_chats.build_avito_chats_client", build_client)
    monkeypatch.setattr("app.routers.avito_chats.resolve_user_avito_access_token", lambda **_kwargs: token[0])
    monkeypatch.setattr("app.routers.avito_chats.get_source_cache", lambda _org, key, **_kwargs: cache.get(key))
    monkeypatch.setattr("app.routers.avito_chats.save_source_cache", lambda _org, key, payload: cache.update({key: payload}))
    _patch_router_auth(monkeypatch)
    api = TestClient(create_app())

    assert api.get("/api/v1/avito/chats").status_code == 200
    token[0] = "second-token"
    assert api.get("/api/v1/avito/chats").status_code == 200
    assert fetched == ["first-token", "second-token"]
    assert len(cache) == 2
    assert all(token_value not in key for key in cache for token_value in token + ["first-token"])

    monkeypatch.setattr("app.routers.avito_chats.get_user_avito_credentials_secret", lambda _user_id: None)
    assert api.get("/api/v1/avito/chats").status_code == 409
    assert fetched == ["first-token", "second-token"]


def test_avito_chats_endpoint_ignores_period_params_because_messenger_is_current_inbox(monkeypatch):
    recording_client: RecordingAvitoChatsClient | None = None
    cached_payloads: list[tuple[int, str, dict]] = []

    def build_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingAvitoChatsClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_chats.build_avito_chats_client", build_client)
    monkeypatch.setattr("app.routers.avito_chats.resolve_user_avito_access_token", lambda **_kwargs: "avito-live-token")
    monkeypatch.setattr("app.routers.avito_chats.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.avito_chats.save_source_cache", lambda organization_id, source_key, payload: cached_payloads.append((organization_id, source_key, payload)))
    _patch_router_auth(monkeypatch)
    api = TestClient(create_app())

    response = api.get("/api/v1/avito/chats", params={"limit": 50, "dateFrom": "2026-07-01", "dateTo": "2026-07-10"})

    assert response.status_code == 200
    assert response.json()["summary"]["total"] == 1
    assert "period" not in response.json()
    assert response.json()["source"]["diagnostics"]["dateFiltering"] == "disabled"
    assert cached_payloads[0][1].startswith("avito_chats:50:0:all:all:")


def test_avito_chats_endpoint_sends_text_message_and_marks_read(monkeypatch):
    recording_client = RecordingAvitoChatsClient(access_token="token")
    monkeypatch.setattr("app.routers.avito_chats.build_avito_chats_client", lambda *_args, **_kwargs: recording_client)
    monkeypatch.setattr("app.routers.avito_chats.resolve_user_avito_access_token", lambda **_kwargs: "avito-live-token")
    _patch_router_auth(monkeypatch)
    api = TestClient(create_app())

    send_response = api.post("/api/v1/avito/chats/chat-1/messages", json={"accountId": "365024549", "text": "Да, актуально"})
    read_response = api.post("/api/v1/avito/chats/chat-1/read", json={"accountId": "365024549"})

    assert send_response.status_code == 200
    assert send_response.json()["message"]["text"] == "Да, актуально"
    assert read_response.status_code == 200
    assert read_response.json()["ok"] is True
    assert recording_client.sent_messages == [("365024549", "chat-1", "Да, актуально")]
    assert recording_client.read_chats == [("365024549", "chat-1")]
