from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.avito.chats import AvitoChatsFetchRequest, AvitoChatsFetchResult, AvitoChatRow
from app.avito.listings import AvitoListingsFetchRequest, AvitoListingsFetchResult, AvitoListingRow
from app.avito.orders import AvitoOrderAction, AvitoOrderItem, AvitoOrderRow, AvitoOrdersFetchRequest, AvitoOrdersFetchResult
from app.avito.reviews import AvitoReviewRow, AvitoReviewsFetchRequest, AvitoReviewsFetchResult
from app.cabinet.store import AvitoCredentialsSecret
from app.main import create_app


class RecordingChatsClient:
    def __init__(self) -> None:
        self.requests: list[AvitoChatsFetchRequest] = []

    def fetch_chats(self, request: AvitoChatsFetchRequest) -> AvitoChatsFetchResult:
        self.requests.append(request)
        return AvitoChatsFetchResult(
            status="synced",
            accountId="365024549",
            accountName="Bless T",
            chats=[
                AvitoChatRow(
                    chatId="chat-1",
                    accountId="365024549",
                    accountName="Bless T",
                    buyerName="Алиса",
                    itemTitle="Худи live",
                    preview="Есть размер?",
                    unread=True,
                    updatedAt="2026-07-29T10:00:00+00:00",
                )
            ],
        )


class RecordingOrdersClient:
    def __init__(self) -> None:
        self.requests: list[AvitoOrdersFetchRequest] = []

    def fetch_orders(self, request: AvitoOrdersFetchRequest) -> AvitoOrdersFetchResult:
        self.requests.append(request)
        return AvitoOrdersFetchResult(
            status="synced",
            total=1,
            orders=[
                AvitoOrderRow(
                    orderId="ord-1",
                    status="ready_to_ship",
                    deliveryService="СДЭК",
                    trackNumber="TRACK",
                    updatedAt="2026-07-29T09:00:00+00:00",
                    items=[AvitoOrderItem(itemId="809", title="Футболка", quantity=1)],
                    availableActions=[AvitoOrderAction(name="setMarkings", required=True)],
                )
            ],
        )


class RecordingReviewsClient:
    def __init__(self) -> None:
        self.requests: list[AvitoReviewsFetchRequest] = []

    def fetch_reviews(self, request: AvitoReviewsFetchRequest) -> AvitoReviewsFetchResult:
        self.requests.append(request)
        return AvitoReviewsFetchResult(
            status="synced",
            total=1,
            reviews=[
                AvitoReviewRow(
                    reviewId="review-1",
                    score=2,
                    stage="done",
                    text="Плохо",
                    usedInScore=True,
                    canAnswer=True,
                    buyerName="Дмитрий",
                    itemTitle="Лонгслив",
                    createdAt="2026-07-28T09:00:00+00:00",
                )
            ],
        )


class RecordingListingsClient:
    def __init__(self) -> None:
        self.requests: list[AvitoListingsFetchRequest] = []

    def fetch_listings(self, request: AvitoListingsFetchRequest) -> AvitoListingsFetchResult:
        self.requests.append(request)
        return AvitoListingsFetchResult(
            status="synced",
            rows=[
                AvitoListingRow(
                    itemId="809",
                    title="Футболка",
                    accountId="365024549",
                    accountName="Bless T",
                    status="blocked",
                    sourceStatus="fresh",
                )
            ],
        )


def test_avito_notifications_endpoint_aggregates_live_sources_and_caches(monkeypatch):
    chats_client = RecordingChatsClient()
    orders_client = RecordingOrdersClient()
    reviews_client = RecordingReviewsClient()
    listings_client = RecordingListingsClient()
    saved: list[tuple[int, str, dict]] = []

    monkeypatch.setattr("app.routers.avito_notifications.actor_from_request", lambda _request: SimpleNamespace(user_id="viewer", organization_id=10))
    monkeypatch.setattr("app.routers.avito_notifications.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr(
        "app.routers.avito_notifications.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(client_id="client", client_secret="secret", cached_access_token=None, access_token_expires_at=None),
    )
    monkeypatch.setattr("app.routers.avito_notifications.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_notifications.resolve_user_avito_access_token", lambda **_kwargs: "avito-token")
    monkeypatch.setattr("app.routers.avito_notifications.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.avito_notifications.save_source_cache", lambda organization_id, source_key, payload: saved.append((organization_id, source_key, payload)))
    monkeypatch.setattr("app.routers.avito_notifications.build_avito_chats_client", lambda **_kwargs: chats_client)
    monkeypatch.setattr("app.routers.avito_notifications.build_avito_orders_client", lambda **_kwargs: orders_client)
    monkeypatch.setattr("app.routers.avito_notifications.build_avito_reviews_client", lambda **_kwargs: reviews_client)
    monkeypatch.setattr("app.routers.avito_notifications.build_avito_listings_client", lambda **_kwargs: listings_client)

    api = TestClient(create_app())
    response = api.get("/api/v1/avito/notifications", params={"dateFrom": "2026-07-01", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "synced"
    assert payload["summary"]["total"] == 4
    assert payload["summary"]["critical"] == 2
    assert {item["source"] for item in payload["items"]} == {"Сообщения Авито", "Заказы Авито", "Отзывы Авито", "Объявления Авито"}
    assert payload["source"]["api"]["chatsEndpoint"] == "Сообщения Авито"
    assert payload["source"]["api"]["ordersEndpoint"] == "Заказы Авито"
    assert payload["rulesRows"]
    assert chats_client.requests[0].includeMessages is False
    assert orders_client.requests[0].dateFrom == date(2026, 7, 1)
    assert saved[-1][1].startswith("avito_notifications:2026-07-01:l20:all:")
