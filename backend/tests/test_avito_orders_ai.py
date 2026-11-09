import json
from types import SimpleNamespace

from app.avito.orders import AvitoOrdersBrowserSnapshot
from app.avito.orders_ai import enrich_avito_orders_snapshot_with_ai


class FakeResponse:
    def __init__(self, output: dict):
        self.output = output

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(self.output, ensure_ascii=False)}],
                }
            ]
        }


class RecordingClient:
    base_url = "https://api.openai.com/v1"

    def __init__(self, output: dict):
        self.output = output
        self.posts: list[dict] = []

    def post(self, url: str, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return FakeResponse(self.output)


def snapshot_with_two_sizes() -> AvitoOrdersBrowserSnapshot:
    return AvitoOrdersBrowserSnapshot.model_validate(
        {
            "collector": {
                "options": {
                    "sizeMode": "chat_ai",
                    "colorFromDescription": False,
                    "articleFromDescription": False,
                }
            },
            "orders": [
                {
                    "orderId": "order-1", "accountId": "seller", "status": "ready_to_ship",
                    "items": [
                        {
                            "itemId": "1", "title": "Свитшот",
                            "chatEvidence": {"state": "collected", "accountId": "seller", "sellerId": "seller", "buyerId": "buyer", "orderId": "order-1", "itemId": "1", "channelId": "channel", "messages": [
                                {"id": "q", "role": "seller", "text": "Какой размер вам нужен?", "orderId": "order-1", "itemId": "1", "createdAt": "2026-10-07T10:00:00Z"},
                                {"id": "a", "role": "buyer", "text": "Давайте L", "orderId": "order-1", "itemId": "1", "createdAt": "2026-10-07T10:01:00Z"}]},
                            "chatText": "Сначала M\nНет, тогда L\nДа, фиксируем L",
                            "descriptionSize": "54 (XL)",
                        },
                        {
                            "title": "Футболка",
                            "chatText": "Какой размер есть?",
                            "descriptionSize": "46 (M)",
                        },
                    ],
                }
            ],
        }
    )


def settings(api_key: str | None):
    return SimpleNamespace(
        openai_api_key=api_key,
        openai_api_base_url="https://api.openai.com/v1",
        openai_review_model="gpt-4o-mini",
        openai_review_timeout_seconds=20.0,
    )


def test_local_sizes_need_no_provider_request(monkeypatch):
    monkeypatch.setattr("app.avito.orders_ai.get_settings", lambda: settings("test-key"))
    client = RecordingClient(
        {
            "items": [
                {
                    "key": "0:0",
                    "size": "L", "sizeMessageId": "a",
                    "color": None,
                    "sellerArticle": None,
                    "confidence": "high",
                    "notes": "final confirmation",
                },
            ]
        }
    )

    snapshot, meta = enrich_avito_orders_snapshot_with_ai(snapshot_with_two_sizes(), client=client)

    assert not client.posts
    assert snapshot.orders[0].items[0].size == "L"
    assert snapshot.orders[0].items[0].sources["size"] == "chat_ai"
    assert snapshot.orders[0].items[1].size is None
    assert snapshot.orders[0].items[1].sizeState == "needs_review"
    assert meta["aiSizeCount"] == 0 and meta["ruleSizeCount"] == 1
    assert meta["descriptionFallbackCount"] == 0
    assert meta["missingFinalSizeCount"] == 1


def test_missing_openai_key_never_applies_description_fallback(monkeypatch):
    monkeypatch.setattr("app.avito.orders_ai.get_settings", lambda: settings(None))
    snapshot = snapshot_with_two_sizes()

    enriched, meta = enrich_avito_orders_snapshot_with_ai(snapshot)

    assert [item.size for item in enriched.orders[0].items] == ["L", None]
    assert meta["status"] == "completed"
    assert meta["descriptionFallbackCount"] == 0
    assert meta["missingFinalSizeCount"] == 1


class FailingClient:
    base_url = "https://api.openai.com/v1"

    def post(self, *_args, **_kwargs):
        raise TimeoutError("timeout")


def test_failed_ai_request_never_applies_description_fallback(monkeypatch):
    monkeypatch.setattr("app.avito.orders_ai.get_settings", lambda: settings("test-key"))
    enriched, meta = enrich_avito_orders_snapshot_with_ai(snapshot_with_two_sizes(), client=FailingClient())

    assert [item.size for item in enriched.orders[0].items] == ["L", None]
    assert meta["status"] == "completed"
    assert meta["descriptionFallbackCount"] == 0


class MalformedResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"output": []}


class MalformedClient:
    base_url = "https://api.openai.com/v1"

    def post(self, *_args, **_kwargs):
        return MalformedResponse()


def test_malformed_ai_output_never_applies_description_fallback(monkeypatch):
    monkeypatch.setattr("app.avito.orders_ai.get_settings", lambda: settings("test-key"))
    enriched, meta = enrich_avito_orders_snapshot_with_ai(snapshot_with_two_sizes(), client=MalformedClient())

    assert [item.size for item in enriched.orders[0].items] == ["L", None]
    assert meta["status"] == "completed"
    assert meta["descriptionFallbackCount"] == 0
