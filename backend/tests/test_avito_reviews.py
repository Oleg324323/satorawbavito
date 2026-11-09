from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx

from app.avito.reviews import AvitoReviewsFetchRequest, LiveAvitoReviewsClient
from app.cabinet.store import AvitoCredentialsSecret
from app.main import create_app


class AvitoReviewsHttpResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.avito.ru/test")
            response = httpx.Response(self.status_code, request=request, json=self.payload)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)

    def json(self) -> dict:
        return self.payload


class RecordingAvitoReviewsHttpClient:
    def __init__(self, payloads: list[dict | tuple[dict, int]]) -> None:
        self.payloads = list(payloads)
        self.gets: list[dict] = []
        self.posts: list[dict] = []
        self.deletes: list[dict] = []

    def _response(self) -> AvitoReviewsHttpResponse:
        payload = self.payloads.pop(0)
        if isinstance(payload, tuple):
            body, status_code = payload
            return AvitoReviewsHttpResponse(body, status_code)
        return AvitoReviewsHttpResponse(payload)

    def get(self, url: str, **kwargs) -> AvitoReviewsHttpResponse:
        self.gets.append({"url": url, **kwargs})
        return self._response()

    def post(self, url: str, **kwargs) -> AvitoReviewsHttpResponse:
        self.posts.append({"url": url, **kwargs})
        return self._response()

    def delete(self, url: str, **kwargs) -> AvitoReviewsHttpResponse:
        self.deletes.append({"url": url, **kwargs})
        return self._response()


def _review_payload() -> dict:
    return {
        "reviews": [
            {
                "id": 92312343,
                "score": 2,
                "stage": "fell_through",
                "text": "Не подошел размер",
                "usedInScore": True,
                "canAnswer": True,
                "createdAt": 1785230400,
                "sender": {"name": "Ирина"},
                "item": {"id": 8098482225, "title": "Худи yohji yamamoto"},
                "answer": None,
                "images": [{"number": 1, "sizes": [{"size": "100x100", "url": "https://img.avito.ru/1.jpg"}]}],
            }
        ],
        "total": 1,
    }


def test_live_avito_reviews_client_uses_rating_reviews_and_answer_endpoints():
    http_client = RecordingAvitoReviewsHttpClient(
        [
            {"isEnabled": True, "rating": {"score": 4.3, "reviewsWithScoreCount": 12, "reviewsCount": 21}},
            _review_payload(),
            {"id": 777, "createdAt": 1785230460},
            {"success": True},
        ]
    )
    client = LiveAvitoReviewsClient(access_token="token", base_url="https://api.avito.ru")

    result = client.fetch_reviews(AvitoReviewsFetchRequest(limit=50, offset=0), http_client=http_client)
    answer = client.create_answer(review_id="92312343", message="Спасибо за отзыв", http_client=http_client)
    removed = client.delete_answer(answer_id="777", http_client=http_client)

    assert result.status == "synced"
    assert result.rating is not None
    assert result.rating.score == 4.3
    assert result.reviews[0].reviewId == "92312343"
    assert result.reviews[0].buyerName == "Ирина"
    assert result.reviews[0].itemId == "8098482225"
    assert result.reviews[0].images[0] == "https://img.avito.ru/1.jpg"
    assert http_client.gets[0]["url"] == "https://api.avito.ru/ratings/v1/info"
    assert http_client.gets[1]["url"] == "https://api.avito.ru/ratings/v1/reviews"
    assert http_client.gets[1]["params"] == {"limit": 50, "offset": 0}
    assert http_client.posts[0]["url"] == "https://api.avito.ru/ratings/v1/answers"
    assert http_client.posts[0]["json"] == {"reviewId": 92312343, "message": "Спасибо за отзыв"}
    assert http_client.deletes[0]["url"] == "https://api.avito.ru/ratings/v1/answers/777"
    assert answer.answerId == "777"
    assert removed is True


def test_avito_reviews_endpoint_uses_saved_credentials_and_caches_payload(monkeypatch):
    saved: list[tuple[int, str, dict]] = []

    monkeypatch.setattr("app.routers.avito_reviews.actor_from_request", lambda _request: SimpleNamespace(user_id="viewer", organization_id=10))
    monkeypatch.setattr("app.routers.avito_reviews.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr(
        "app.routers.avito_reviews.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_reviews.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_reviews.resolve_user_avito_access_token", lambda **_kwargs: "avito-live-token")
    monkeypatch.setattr("app.routers.avito_reviews.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.avito_reviews.save_source_cache", lambda organization_id, source_key, payload: saved.append((organization_id, source_key, payload)))

    class Client:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        def fetch_reviews(self, request: AvitoReviewsFetchRequest):
            return LiveAvitoReviewsClient(access_token=self.access_token).fetch_reviews(
                request,
                http_client=RecordingAvitoReviewsHttpClient(
                    [
                        {"isEnabled": True, "rating": {"score": 4.3, "reviewsWithScoreCount": 12, "reviewsCount": 21}},
                        _review_payload(),
                    ]
                ),
            )

    monkeypatch.setattr("app.routers.avito_reviews.build_avito_reviews_client", lambda access_token, **_kwargs: Client(access_token))
    api = TestClient(create_app())

    response = api.get("/api/v1/avito/reviews", params={"limit": 50, "offset": 0})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["unanswered"] == 1
    assert payload["rating"]["score"] == 4.3
    assert payload["reviews"][0]["reviewId"] == "92312343"
    assert saved[0][1].startswith("avito_reviews:50:0:")


def test_avito_reviews_ai_draft_uses_review_prompt_settings(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr("app.routers.avito_reviews.actor_from_request", lambda _request: SimpleNamespace(user_id="editor", organization_id=10, actor_id="editor"))
    monkeypatch.setattr("app.routers.avito_reviews.has_permission", lambda _actor, _permission: True)

    class Settings:
        aiPrompt = "Отвечай вежливо и коротко"
        reviewSettings = SimpleNamespace(
            avitoTone="Спокойный, уверенный тон продавца Avito",
            model_dump_json=lambda: '{"automationMode":"draft-first","avitoTone":"Спокойный, уверенный тон продавца Avito"}',
        )

    monkeypatch.setattr("app.routers.avito_reviews.get_sync_settings", lambda _org_id, _actor_id: Settings())

    def fake_generate_openai_review_reply(**kwargs):
        captured.update(kwargs)
        from app.reviews.schemas import ReviewAiGenerationResult

        return ReviewAiGenerationResult(
            replyText="Спасибо за отзыв! Учтем замечание.",
            riskLevel="low",
            requiresApproval=False,
            reasons=[],
            matchedStopTopics=[],
            model="test-openai",
            generationSource="openai",
        )

    monkeypatch.setattr("app.routers.avito_reviews.generate_openai_review_reply", fake_generate_openai_review_reply)
    api = TestClient(create_app())

    response = api.post(
        "/api/v1/avito/reviews/92312343/drafts/generate",
        json={
            "brandVoiceId": "bless-t",
            "regenerate": True,
            "promptInstruction": "Вариант промпта: отзыв 3*2*1* (отказ и возврат)\nИнструкция: попросить написать в чат",
            "review": {
                "reviewId": "92312343",
                "score": 2,
                "stage": "fell_through",
                "text": "Не подошел размер",
                "usedInScore": True,
                "canAnswer": True,
                "buyerName": "Ирина",
                "itemId": "8098482225",
                "itemTitle": "Худи yohji yamamoto",
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"]["feedbackId"] == "92312343"
    assert payload["draft"]["generatedText"] == "Спасибо за отзыв! Учтем замечание."
    assert captured["brand_voice_id"] == "bless-t"
    assert "отзыв 3*2*1*" in captured["template_text"]
    assert "попросить написать в чат" in captured["template_text"]
    assert "Avito" in captured["system_prompt"]
    assert "Спокойный, уверенный тон продавца Avito" in captured["system_prompt"]
    assert captured["feedback"].productName == "Худи yohji yamamoto"
