from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.avito.chats import AvitoChatsFetchRequest, AvitoChatsFetchResult, AvitoChatRow
from app.avito.listings import AvitoListingsFetchRequest, AvitoListingsFetchResult, AvitoListingRow
from app.avito.reviews import AvitoRatingInfo, AvitoReviewsFetchRequest, AvitoReviewsFetchResult, AvitoReviewRow
from app.avito.stats import AvitoStatsAccount, AvitoStatsFetchError, AvitoStatsFetchRequest, AvitoStatsFetchResult, AvitoStatsItem
from app.avito.auth import scoped_avito_cache_key
from app.cabinet.store import AvitoCredentialsSecret
from app.main import create_app


class RecordingStatsClient:
    def __init__(self) -> None:
        self.requests: list[AvitoStatsFetchRequest] = []

    def fetch_stats(self, request: AvitoStatsFetchRequest) -> AvitoStatsFetchResult:
        self.requests.append(request)
        return AvitoStatsFetchResult(
            status="synced",
            accounts=[AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=2, activeItemCount=1, inactiveItemCount=1)],
            items=[
                AvitoStatsItem(
                    itemId="8098482225",
                    title="Худи live",
                    accountId="365024549",
                    accountName="Bless T",
                    impressions=100,
                    views=40,
                    contacts=4,
                    favorites=9,
                    spendKopecks=120000,
                    orders=2,
                    buyouts=1,
                ),
                AvitoStatsItem(
                    itemId="8098482191",
                    title="Футболка live",
                    accountId="365024549",
                    accountName="Bless T",
                    impressions=50,
                    views=10,
                    contacts=0,
                    favorites=1,
                    spendKopecks=30000,
                ),
            ],
        )


class RateLimitedStatsClient:
    def __init__(self) -> None:
        self.requests: list[AvitoStatsFetchRequest] = []

    def fetch_stats(self, request: AvitoStatsFetchRequest) -> AvitoStatsFetchResult:
        self.requests.append(request)
        return AvitoStatsFetchResult(
            status="blocked",
            error=AvitoStatsFetchError(
                code="rate_limited",
                message="Avito HTTP 429",
                retryable=True,
                blockerIds=["AVITO_RATE_LIMIT"],
            ),
        )


class RecordingListingsClient:
    def __init__(self) -> None:
        self.requests: list[AvitoListingsFetchRequest] = []

    def fetch_listings(self, request: AvitoListingsFetchRequest) -> AvitoListingsFetchResult:
        self.requests.append(request)
        return AvitoListingsFetchResult(
            status="synced",
            accounts=[AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=2, activeItemCount=1, inactiveItemCount=1)],
            rows=[
                AvitoListingRow(itemId="8098482225", title="Худи live", accountId="365024549", accountName="Bless T", status="active"),
                AvitoListingRow(itemId="8098482191", title="Футболка live", accountId="365024549", accountName="Bless T", status="old"),
            ],
        )


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
                AvitoChatRow(chatId="chat-1", accountId="365024549", accountName="Bless T", buyerName="Алиса", itemTitle="Худи live", unread=True),
                AvitoChatRow(chatId="chat-2", accountId="365024549", accountName="Bless T", buyerName="Мария", itemTitle="Футболка live", unread=False),
            ],
        )


class RecordingReviewsClient:
    def __init__(self) -> None:
        self.requests: list[AvitoReviewsFetchRequest] = []

    def fetch_reviews(self, request: AvitoReviewsFetchRequest) -> AvitoReviewsFetchResult:
        self.requests.append(request)
        return AvitoReviewsFetchResult(
            status="synced",
            rating=AvitoRatingInfo(isEnabled=True, score=4.8, reviewsCount=12, reviewsWithScoreCount=12),
            total=12,
            reviews=[
                AvitoReviewRow(reviewId="review-1", score=5, stage="done", text="Все ок", usedInScore=True, canAnswer=True),
                AvitoReviewRow(reviewId="review-2", score=2, stage="done", text="Плохо", usedInScore=True, canAnswer=False),
            ],
        )


def test_avito_overview_aggregates_live_sections_and_caches(monkeypatch):
    stats_client = RecordingStatsClient()
    listings_client = RecordingListingsClient()
    chats_client = RecordingChatsClient()
    reviews_client = RecordingReviewsClient()
    saved: list[tuple[int, str, dict]] = []

    monkeypatch.setattr("app.routers.avito_overview.actor_from_request", lambda _request: SimpleNamespace(user_id="viewer", organization_id=10))
    monkeypatch.setattr("app.routers.avito_overview.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr(
        "app.routers.avito_overview.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(client_id="client", client_secret="secret", cached_access_token=None, access_token_expires_at=None),
    )
    monkeypatch.setattr("app.routers.avito_overview.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_overview.resolve_user_avito_access_token", lambda **_kwargs: "avito-token")
    monkeypatch.setattr("app.routers.avito_overview.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.avito_overview.save_source_cache", lambda organization_id, source_key, payload: saved.append((organization_id, source_key, payload)))
    monkeypatch.setattr("app.routers.avito_overview.build_avito_stats_client", lambda **_kwargs: stats_client)
    monkeypatch.setattr("app.routers.avito_overview.build_avito_listings_client", lambda **_kwargs: listings_client)
    monkeypatch.setattr("app.routers.avito_overview.build_avito_chats_client", lambda **_kwargs: chats_client)
    monkeypatch.setattr("app.routers.avito_overview.build_avito_reviews_client", lambda **_kwargs: reviews_client)

    api = TestClient(create_app())
    response = api.get(
        "/api/v1/avito/overview",
        params={"dateFrom": "2026-07-01", "dateTo": "2026-07-28"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["impressions"] == 150
    assert payload["summary"]["views"] == 50
    assert payload["summary"]["contacts"] == 4
    assert payload["summary"]["spendKopecks"] == 150000
    assert payload["summary"]["orders"] == 2
    assert payload["summary"]["buyouts"] == 1
    assert payload["summary"]["activeListings"] == 1
    assert payload["summary"]["inactiveListings"] == 1
    assert payload["summary"]["chats"] == 2
    assert payload["summary"]["unreadChats"] == 1
    assert payload["summary"]["reviews"] == 12
    assert payload["summary"]["lowReviews"] == 1
    assert payload["topItems"][0]["title"] == "Худи live"
    assert any(event["source"] == "Объявления" for event in payload["events"])
    assert stats_client.requests[0].dateFrom == date(2026, 7, 1)
    assert stats_client.requests[0].grouping == "item"
    assert chats_client.requests[0].includeMessages is False
    assert listings_client.requests == []
    assert saved[0][1].startswith("avito_overview:2026-07-01:2026-07-28:all:")


def test_avito_overview_reuses_stale_cache_during_rate_limit_cooldown(monkeypatch):
    stats_client = RateLimitedStatsClient()
    listings_client = RecordingListingsClient()
    chats_client = RecordingChatsClient()
    reviews_client = RecordingReviewsClient()
    cache_store: dict[str, dict] = {
        scoped_avito_cache_key("avito_overview:2026-07-01:2026-07-28:all", "avito-token"): {
            "status": "synced",
            "period": {"dateFrom": "2026-07-01", "dateTo": "2026-07-28", "days": 28},
            "summary": {"views": 10, "contacts": 1},
            "accounts": [],
            "topItems": [],
            "events": [],
            "source": {"mode": "live", "cache": {"status": "fresh"}},
        }
    }

    monkeypatch.setattr("app.routers.avito_overview.actor_from_request", lambda _request: SimpleNamespace(user_id="viewer", organization_id=10))
    monkeypatch.setattr("app.routers.avito_overview.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr(
        "app.routers.avito_overview.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(client_id="client", client_secret="secret", cached_access_token=None, access_token_expires_at=None),
    )
    monkeypatch.setattr("app.routers.avito_overview.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_overview.resolve_user_avito_access_token", lambda **_kwargs: "avito-token")
    monkeypatch.setattr("app.routers.avito_overview.get_source_cache", lambda _organization_id, source_key, **_kwargs: cache_store.get(source_key))
    monkeypatch.setattr("app.routers.avito_overview.save_source_cache", lambda _organization_id, source_key, payload: cache_store.__setitem__(source_key, payload))
    monkeypatch.setattr("app.routers.avito_overview.build_avito_stats_client", lambda **_kwargs: stats_client)
    monkeypatch.setattr("app.routers.avito_overview.build_avito_listings_client", lambda **_kwargs: listings_client)
    monkeypatch.setattr("app.routers.avito_overview.build_avito_chats_client", lambda **_kwargs: chats_client)
    monkeypatch.setattr("app.routers.avito_overview.build_avito_reviews_client", lambda **_kwargs: reviews_client)

    api = TestClient(create_app())
    params = {"dateFrom": "2026-07-01", "dateTo": "2026-07-28", "forceRefresh": "true"}
    first = api.get("/api/v1/avito/overview", params=params)
    second = api.get("/api/v1/avito/overview", params=params)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(stats_client.requests) == 1
    assert listings_client.requests == []
    payload = second.json()
    assert payload["source"]["cache"]["status"] == "stale"
    assert payload["source"]["errors"]["stats"]["code"] == "rate_limited"


def test_avito_overview_recovers_after_rate_limit_without_caching_empty_stats(monkeypatch):
    stats_client = [RateLimitedStatsClient()]
    stats_key = scoped_avito_cache_key("avito_stats:2026-07-01:2026-07-28:all", "avito-token")
    cache_store: dict[str, dict] = {
        stats_key: {
            "status": "synced",
            "period": {"dateFrom": "2026-07-01", "dateTo": "2026-07-28", "days": 28},
            "summary": {"impressions": 150, "views": 50, "contacts": 4, "favorites": 10,
                        "spendKopecks": 150000, "orders": 2, "buyouts": 1,
                        "conversionPct": 8.0, "orderConversionPct": 50.0, "buyoutPct": 50.0},
            "rows": [{"itemId": "account:365024549:totals"}],
            "accounts": [],
        },
    }
    monkeypatch.setattr("app.routers.avito_overview.actor_from_request", lambda _request: SimpleNamespace(user_id="viewer", organization_id=10))
    monkeypatch.setattr("app.routers.avito_overview.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr("app.routers.avito_overview.get_user_avito_credentials_secret", lambda _user_id: AvitoCredentialsSecret(
        client_id="client", client_secret="secret", cached_access_token=None, access_token_expires_at=None,
    ))
    monkeypatch.setattr("app.routers.avito_overview.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_overview.resolve_user_avito_access_token", lambda **_kwargs: "avito-token")
    monkeypatch.setattr("app.routers.avito_overview.get_source_cache", lambda _organization_id, key, **_kwargs: cache_store.get(key))
    monkeypatch.setattr("app.routers.avito_overview.save_source_cache", lambda _organization_id, key, payload: cache_store.__setitem__(key, payload))
    monkeypatch.setattr("app.routers.avito_overview.build_avito_stats_client", lambda **_kwargs: stats_client[0])
    monkeypatch.setattr("app.routers.avito_overview.build_avito_listings_client", lambda **_kwargs: RecordingListingsClient())
    monkeypatch.setattr("app.routers.avito_overview.build_avito_chats_client", lambda **_kwargs: RecordingChatsClient())
    monkeypatch.setattr("app.routers.avito_overview.build_avito_reviews_client", lambda **_kwargs: RecordingReviewsClient())

    api = TestClient(create_app())
    params = {"dateFrom": "2026-07-01", "dateTo": "2026-07-28"}
    first = api.get("/api/v1/avito/overview", params=params).json()
    assert first["summary"]["views"] == 50
    assert first["summary"]["activeListings"] is None
    assert first["source"]["errors"]["stats"]["retryAfterUntil"]
    assert scoped_avito_cache_key("avito_overview:2026-07-01:2026-07-28:all", "avito-token") not in cache_store
    during_cooldown = api.get("/api/v1/avito/overview", params=params).json()
    assert during_cooldown["source"]["cache"]["status"] == "cooldown"
    assert during_cooldown["summary"]["views"] == 50
    assert len(stats_client[0].requests) == 1

    cache_store.pop(scoped_avito_cache_key("avito_overview_rate_limit:all", "avito-token"))
    stats_client[0] = RecordingStatsClient()
    second = api.get("/api/v1/avito/overview", params=params).json()
    assert second["summary"]["views"] == 50
    assert second["summary"]["activeListings"] == 1
    assert second["topItems"][0]["title"] == "Худи live"
