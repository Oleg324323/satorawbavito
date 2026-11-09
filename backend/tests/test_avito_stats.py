from __future__ import annotations

from datetime import date
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from time import sleep

from fastapi.testclient import TestClient
import httpx
import pytest

from app.avito.stats import AvitoStatsAccount, AvitoStatsDailyPoint, AvitoStatsFetchError, AvitoStatsFetchRequest, AvitoStatsFetchResult, AvitoStatsItem, LiveAvitoStatsClient
from app.cabinet.store import AvitoCredentialsSecret
from app.main import create_app
from tests.auth_helpers import auth_headers


EXPECTED_AVITO_ITEM_ANALYTICS_METRICS = [
    "impressions",
    "views",
    "contactsMessenger",
    "contacts",
    "contactsShowPhone",
    "contactsShowPhoneAndMessenger",
    "favorites",
    "allSpending",
    "orderedItems",
    "deliveredItems",
]


class RecordingAvitoStatsClient:
    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token
        self.requests: list[AvitoStatsFetchRequest] = []

    def fetch_stats(self, request: AvitoStatsFetchRequest) -> AvitoStatsFetchResult:
        self.requests.append(request)
        return AvitoStatsFetchResult(
            status="synced",
            accounts=[
                AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=2),
            ],
            items=[
                AvitoStatsItem(
                    itemId="8098482225",
                    title="Худи yohji yamamoto pour homme",
                    accountId="365024549",
                    accountName="Bless T",
                    category="Одежда, обувь, аксессуары",
                    url="https://www.avito.ru/item/8098482225",
                    impressions=10_000,
                    views=7_850,
                    contacts=88,
                    favorites=640,
                    spendKopecks=1_651_900,
                    orders=15,
                    buyouts=11,
                    sourceStatus="fresh",
                    updatedAt="2026-07-28T06:55:00+00:00",
                ),
                AvitoStatsItem(
                    itemId="8098459618",
                    title="Футболка Devil Nut Evil Skull",
                    accountId="365024549",
                    accountName="Bless T",
                    category="Одежда, обувь, аксессуары",
                    url="https://www.avito.ru/item/8098459618",
                    impressions=220,
                    views=100,
                    contacts=0,
                    favorites=13,
                    spendKopecks=0,
                    orders=0,
                    buyouts=0,
                    sourceStatus="stale",
                    updatedAt="2026-07-28T05:30:00+00:00",
                ),
            ],
            diagnostics={
                "status": 200,
                "groupings": 2,
                "dataTotalCount": 2,
                "requestUrl": "https://api.avito.ru/stats/v2/accounts/365024549/items",
                "requestBody": {
                    "grouping": "item",
                    "metrics": EXPECTED_AVITO_ITEM_ANALYTICS_METRICS,
                },
                "bodyPreview": {"result": {"dataTotalCount": 2, "groupings": [{"id": 8098482225}, {"id": 8098459618}]}},
                "itemsCount": 2,
                "itemIdsSample": ["8098482225", "8098459618"],
            },
        )


class AvitoStatsHttpResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.avito.ru/test")
            response = httpx.Response(self.status_code, request=request, json=self.payload)
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=response)
        return None

    def json(self) -> dict:
        return self.payload


class RecordingAvitoStatsHttpClient:
    def __init__(self, payload: dict) -> None:
        self.payloads = [payload]
        self.posts: list[dict] = []

    def post(self, url: str, **kwargs) -> AvitoStatsHttpResponse:
        self.posts.append({"url": url, **kwargs})
        payload = self.payloads.pop(0) if self.payloads else {}
        if isinstance(payload, tuple):
            body, status_code = payload
            return AvitoStatsHttpResponse(body, status_code)
        return AvitoStatsHttpResponse(payload)


class SequencedAvitoStatsHttpClient(RecordingAvitoStatsHttpClient):
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.posts: list[dict] = []


class RecordingAvitoItemsHttpClient:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.gets: list[dict] = []

    def get(self, url: str, **kwargs) -> AvitoStatsHttpResponse:
        self.gets.append({"url": url, **kwargs})
        return AvitoStatsHttpResponse(self.payloads.pop(0))


def test_live_avito_stats_client_posts_v2_item_analytics_payload_expected_by_avito():
    http_client = RecordingAvitoStatsHttpClient(
        {
            "result": {
                "groupings": [
                    {
                        "id": 8098482225,
                        "type": "item",
                        "metrics": [
                            {"slug": "impressions", "value": 20},
                            {"slug": "views", "value": 6},
                            {"slug": "contactsMessenger", "value": 2},
                            {"slug": "contacts", "value": 3},
                            {"slug": "contactsShowPhone", "value": 1},
                            {"slug": "contactsShowPhoneAndMessenger", "value": 1},
                            {"slug": "favorites", "value": 8},
                            {"slug": "allSpending", "value": 1200},
                            {"slug": "orderedItems", "value": 2},
                            {"slug": "deliveredItems", "value": 1},
                        ],
                    }
                ]
            }
        }
    )
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")

    rows = client._stats_for_items(
        http_client,
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 1), dateTo=date(2026, 7, 28)),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
        [
            {
                "itemId": "8098482225",
                "title": "Худи yohji yamamoto pour homme",
                "accountId": "365024549",
                "accountName": "Bless T",
                "category": "Одежда, обувь, аксессуары",
                "url": "https://www.avito.ru/item/8098482225",
            }
        ],
    )

    assert http_client.posts[0]["url"] == "https://api.avito.ru/stats/v2/accounts/365024549/items"
    assert http_client.posts[0]["json"] == {
        "dateFrom": "2026-07-01",
        "dateTo": "2026-07-28",
        "grouping": "item",
        "metrics": EXPECTED_AVITO_ITEM_ANALYTICS_METRICS,
        "limit": 1000,
        "offset": 0,
    }
    assert len(http_client.posts) == 1
    assert rows[0].impressions == 20
    assert rows[0].views == 6
    assert rows[0].contactsMessenger == 2
    assert rows[0].contacts == 3
    assert rows[0].contactsShowPhone == 1
    assert rows[0].contactsShowPhoneAndMessenger == 1
    assert rows[0].favorites == 8
    assert rows[0].spendKopecks == 1200
    assert rows[0].orders == 2
    assert rows[0].buyouts == 1


def test_live_avito_stats_client_keeps_items_when_v2_item_analytics_are_empty():
    http_client = SequencedAvitoStatsHttpClient(
        [
            {"result": {"groupings": []}},
        ]
    )
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")

    rows = client._stats_for_items(
        http_client,
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 1), dateTo=date(2026, 7, 28)),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
        [
            {
                "itemId": "8098482225",
                "title": "Худи yohji yamamoto pour homme",
                "accountId": "365024549",
                "accountName": "Bless T",
                "category": "Одежда, обувь, аксессуары",
                "url": "https://www.avito.ru/item/8098482225",
            }
        ],
    )

    assert len(http_client.posts) == 1
    assert http_client.posts[0]["url"] == "https://api.avito.ru/stats/v2/accounts/365024549/items"
    assert rows[0].views is None
    assert rows[0].contacts is None
    assert rows[0].sourceStatus == "partial"


def test_live_avito_stats_client_uses_null_when_v2_metric_is_absent():
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")

    rows = client._stats_for_items(
        RecordingAvitoStatsHttpClient(
            {
                "result": {
                    "groupings": [
                        {"id": 8098482225, "type": "item", "metrics": [{"slug": "views", "value": 0}]},
                    ]
                }
            }
        ),
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 1), dateTo=date(2026, 7, 28)),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
        [
            {
                "itemId": "8098482225",
                "title": "Худи yohji yamamoto pour homme",
                "accountId": "365024549",
                "accountName": "Bless T",
            }
        ],
    )

    assert rows[0].views == 0
    assert rows[0].contacts is None
    assert rows[0].favorites is None


def test_live_avito_stats_client_accepts_avito_items_grouping_type():
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")

    rows = client._stats_for_items(
        RecordingAvitoStatsHttpClient(
            {
                "result": {
                    "dataTotalCount": 1,
                    "groupings": [
                        {
                            "id": 8226644604,
                            "type": "items",
                            "metrics": [
                                {"slug": "impressions", "value": 13168},
                                {"slug": "views", "value": 3562},
                                {"slug": "contacts", "value": 122},
                                {"slug": "favorites", "value": 317},
                            ],
                        }
                    ],
                }
            }
        ),
        AvitoStatsFetchRequest(dateFrom=date(2026, 6, 29), dateTo=date(2026, 7, 28)),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
        [
            {
                "itemId": "8226644604",
                "title": "Avito item",
                "accountId": "365024549",
                "accountName": "Bless T",
            }
        ],
    )

    assert rows[0].impressions == 13_168
    assert rows[0].views == 3_562
    assert rows[0].contacts == 122
    assert rows[0].favorites == 317
    assert rows[0].sourceStatus == "fresh"


def test_live_avito_stats_client_splits_v2_item_analytics_ranges_over_30_days():
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")
    http_client = SequencedAvitoStatsHttpClient(
        [
            {
                "result": {
                    "dataTotalCount": 1,
                    "groupings": [
                        {"id": 8098482225, "type": "items", "metrics": [{"slug": "views", "value": 40_000}]},
                    ],
                }
            },
            {
                "result": {
                    "dataTotalCount": 1,
                    "groupings": [
                        {"id": 8098482225, "type": "items", "metrics": [{"slug": "views", "value": 1_460}]},
                    ],
                }
            },
        ]
    )

    rows = client._stats_for_items(
        http_client,
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 4), dateTo=date(2026, 8, 4)),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
        [{"itemId": "8098482225", "title": "Avito item", "accountId": "365024549", "accountName": "Bless T"}],
    )

    assert [post["json"]["dateFrom"] for post in http_client.posts] == ["2026-07-04", "2026-08-03"]
    assert [post["json"]["dateTo"] for post in http_client.posts] == ["2026-08-02", "2026-08-04"]
    assert rows[0].views == 41_460
    assert rows[0].sourceStatus == "fresh"


def test_live_avito_stats_client_uses_totals_grouping_without_item_pages():
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")
    http_client = SequencedAvitoStatsHttpClient(
        [
            {
                "result": {
                    "dataTotalCount": 1,
                    "groupings": [
                        {
                            "type": "totals",
                            "metrics": [
                                {"slug": "views", "value": 40_659},
                                {"slug": "contacts", "value": 770},
                                {"slug": "favorites", "value": 5_746},
                                {"slug": "allSpending", "value": 11_671_300},
                            ],
                        },
                    ],
                }
            }
        ]
    )

    rows, daily = client._stats_totals(
        http_client,
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 4), dateTo=date(2026, 8, 4), grouping="totals"),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
    )

    assert len(http_client.posts) == 1
    assert http_client.posts[0]["json"]["dateFrom"] == "2026-07-04"
    assert http_client.posts[0]["json"]["dateTo"] == "2026-08-04"
    assert http_client.posts[0]["json"]["grouping"] == "totals"
    assert http_client.posts[0]["json"]["limit"] == 1
    assert rows[0].views == 40_659
    assert rows[0].contacts == 770
    assert rows[0].favorites == 5_746
    assert rows[0].spendKopecks == 11_671_300
    assert rows[0].sourceStatus == "fresh"
    assert daily == []


def test_live_avito_stats_client_sums_totals_when_avito_returns_date_groups():
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")
    http_client = SequencedAvitoStatsHttpClient(
        [
            {
                "result": {
                    "dataTotalCount": 7,
                    "groupings": [
                        {
                            "id": 1_754_352_000,
                            "type": "dates",
                            "metrics": [
                                {"slug": "views", "value": 100},
                                {"slug": "contacts", "value": 7},
                                {"slug": "allSpending", "value": 12_000},
                            ],
                        },
                        {
                            "id": 1_754_438_400,
                            "type": "dates",
                            "metrics": [
                                {"slug": "views", "value": 1_360},
                                {"slug": "contacts", "value": 6},
                                {"slug": "favorites", "value": 202},
                                {"slug": "allSpending", "value": 353_800},
                            ],
                        },
                    ],
                }
            }
        ]
    )

    rows, daily = client._stats_totals(
        http_client,
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 29), dateTo=date(2026, 8, 4), grouping="totals"),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
    )

    assert len(http_client.posts) == 1
    assert rows[0].views == 1_460
    assert rows[0].contacts == 13
    assert rows[0].favorites == 202
    assert rows[0].spendKopecks == 365_800
    assert rows[0].sourceStatus == "fresh"
    assert [point.views for point in daily] == [100, 1_360]
    assert [point.contacts for point in daily] == [7, 6]
    assert [point.favorites for point in daily] == [None, 202]


def test_live_avito_stats_client_paginates_v2_item_analytics_groups():
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")
    first_page = [
        {"id": index, "type": "items", "metrics": [{"slug": "views", "value": 1}]}
        for index in range(1, 1001)
    ]
    http_client = SequencedAvitoStatsHttpClient(
        [
            {"result": {"dataTotalCount": 1001, "groupings": first_page}},
            {
                "result": {
                    "dataTotalCount": 1001,
                    "groupings": [
                        {"id": 1001, "type": "items", "metrics": [{"slug": "views", "value": 77}]},
                    ],
                }
            },
        ]
    )

    rows = client._stats_for_items(
        http_client,
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 6), dateTo=date(2026, 8, 4)),
        [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)],
        [{"itemId": "1001", "title": "Paged item", "accountId": "365024549", "accountName": "Bless T"}],
    )

    assert [post["json"]["offset"] for post in http_client.posts] == [0, 1000]
    assert rows[0].views == 77
    assert rows[0].sourceStatus == "fresh"


def test_live_avito_stats_client_metric_reader_uses_later_key_variants():
    assert LiveAvitoStatsClient._int_metric({"views": 9}, "uniqViews", "views") == 9


def test_live_avito_stats_client_paginates_items_with_avito_limit_below_100():
    http_client = RecordingAvitoItemsHttpClient(
        [
            {"resources": [{"id": index, "title": f"Item {index}"} for index in range(1, 100)]},
            {"resources": [{"id": 100, "title": "Item 100"}]},
        ]
    )
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")

    rows = client._items(http_client, [AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)])

    assert len(rows) == 100
    assert http_client.gets[0]["params"] == {"per_page": 99, "page": 1, "status": "active,removed,old"}
    assert http_client.gets[1]["params"] == {"per_page": 99, "page": 2, "status": "active,removed,old"}


def test_live_avito_stats_client_counts_active_and_inactive_items_from_core_statuses():
    client = LiveAvitoStatsClient(access_token="token", base_url="https://api.avito.ru")
    account = AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=0)

    rows = client._stats_for_items(
        RecordingAvitoStatsHttpClient(
            {
                "result": {
                    "groupings": [
                        {
                            "id": 8098482225,
                            "type": "items",
                            "metrics": [{"slug": "views", "value": 6}],
                        },
                        {
                            "id": 8098482191,
                            "type": "items",
                            "metrics": [{"slug": "views", "value": 4}],
                        },
                    ]
                }
            }
        ),
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 1), dateTo=date(2026, 7, 28)),
        [account],
        [
            {"itemId": "8098482225", "title": "Active item", "accountId": "365024549", "accountName": "Bless T", "status": "active"},
            {"itemId": "8098482191", "title": "Removed item", "accountId": "365024549", "accountName": "Bless T", "status": "removed"},
            {"itemId": "8098459618", "title": "Old item", "accountId": "365024549", "accountName": "Bless T", "status": "old"},
        ],
    )

    assert account.itemCount == 3
    assert account.activeItemCount == 1
    assert account.inactiveItemCount == 2
    assert len(rows) == 3


def test_avito_stats_endpoint_uses_saved_avito_credentials_and_caches_payload(monkeypatch):
    recording_client: RecordingAvitoStatsClient | None = None
    cached_payloads: list[tuple[int, str, dict]] = []

    def build_recording_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingAvitoStatsClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_stats.build_avito_stats_client", build_recording_client)
    monkeypatch.setattr(
        "app.routers.avito_stats.resolve_user_avito_access_token",
        lambda **_kwargs: "avito-live-bearer-token",
    )
    monkeypatch.setattr("app.routers.avito_stats.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.routers.avito_stats.save_source_cache",
        lambda organization_id, source_key, payload: cached_payloads.append((organization_id, source_key, payload)),
    )
    api = TestClient(create_app())
    headers = auth_headers(api, "viewer")
    put_credentials = api.put(
        "/api/v1/cabinet/avito-credentials",
        json={"clientId": "avito_client_0987654321", "clientSecret": "avito_secret_1234567890"},
        headers=headers,
    )
    assert put_credentials.status_code == 200

    response = api.get(
        "/api/v1/avito/stats",
        params={"dateFrom": "2026-07-01", "dateTo": "2026-07-28"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == {"dateFrom": "2026-07-01", "dateTo": "2026-07-28", "days": 28}
    assert payload["summary"]["views"] == 7_950
    assert payload["summary"]["contacts"] == 88
    assert payload["summary"]["favorites"] == 653
    assert payload["summary"]["orders"] == 15
    assert payload["summary"]["buyouts"] == 11
    assert payload["summary"]["spendKopecks"] == 1_651_900
    assert payload["summary"]["conversionPct"] == 1.11
    assert payload["rows"][0]["itemId"] == "8098482225"
    assert payload["rows"][0]["contactConversionPct"] == 1.12
    assert payload["rows"][1]["controlFlags"] == ["no_contacts", "stale_source"]
    assert payload["source"]["diagnostics"]["status"] == 200
    assert payload["source"]["diagnostics"]["groupings"] == 2
    assert payload["source"]["diagnostics"]["dataTotalCount"] == 2
    assert payload["source"]["diagnostics"]["requestBody"]["metrics"] == EXPECTED_AVITO_ITEM_ANALYTICS_METRICS
    assert payload["source"]["diagnostics"]["itemsCount"] == 2
    assert payload["source"]["diagnostics"]["itemIdsSample"] == ["8098482225", "8098459618"]
    assert recording_client is not None
    assert recording_client.access_token == "avito-live-bearer-token"
    assert recording_client.requests[0].dateFrom == date(2026, 7, 1)
    assert recording_client.requests[0].dateTo == date(2026, 7, 28)
    assert cached_payloads
    assert cached_payloads[0][1].startswith("avito_stats:2026-07-01:2026-07-28:all:")
    assert cached_payloads[0][2]["rows"][0]["itemId"] == "8098482225"


def test_avito_stats_endpoint_reports_missing_avito_credentials():
    api = TestClient(create_app())
    headers = auth_headers(api, "viewer")
    api.delete("/api/v1/cabinet/avito-credentials", headers=headers)

    response = api.get("/api/v1/avito/stats", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "AVITO_CREDENTIALS_REQUIRED"


def test_avito_stats_endpoint_pauses_after_rate_limit(monkeypatch):
    cache: dict[str, dict] = {}
    calls = 0

    class RateLimitedClient:
        def fetch_stats(self, _request):
            nonlocal calls
            calls += 1
            return AvitoStatsFetchResult(
                status="blocked",
                error=AvitoStatsFetchError(code="rate_limited", message="Avito HTTP 429", retryable=True,
                                           blockerIds=["AVITO_RATE_LIMIT"]),
            )

    monkeypatch.setattr("app.routers.avito_stats.get_user_avito_credentials_secret", lambda _user: AvitoCredentialsSecret(
        client_id="synthetic-client", client_secret="synthetic-secret", cached_access_token=None, access_token_expires_at=None,
    ))
    monkeypatch.setattr("app.routers.avito_stats.resolve_user_avito_access_token", lambda **_kwargs: "current-token")
    monkeypatch.setattr("app.routers.avito_stats.get_source_cache", lambda _org, key, **_kwargs: cache.get(key))
    monkeypatch.setattr("app.routers.avito_stats.save_source_cache", lambda _org, key, payload: cache.update({key: payload}))
    monkeypatch.setattr("app.routers.avito_stats.build_avito_stats_client", lambda **_kwargs: RateLimitedClient())

    api = TestClient(create_app())
    headers = auth_headers(api, "viewer")
    params = {"dateFrom": "2026-07-01", "dateTo": "2026-07-28", "forceRefresh": "true"}
    first = api.get("/api/v1/avito/stats", params=params, headers=headers)
    second = api.get("/api/v1/avito/stats", params=params, headers=headers)

    assert first.status_code == second.status_code == 200
    assert calls == 1
    assert first.json()["source"]["error"]["retryAfterUntil"]
    assert second.json()["source"]["cache"]["status"] == "cooldown"
    assert second.json()["source"]["error"]["code"] == "rate_limited"


def test_avito_stats_endpoint_reuses_exact_period_cache_for_current_token(monkeypatch):
    cached_payload = {
        "status": "synced",
        "period": {"dateFrom": "2026-07-01", "dateTo": "2026-07-28", "days": 28},
        "summary": {
            "impressions": 10,
            "views": 7,
            "contacts": 1,
            "favorites": 0,
            "spendKopecks": 0,
            "orders": 0,
            "buyouts": 0,
            "conversionPct": 14.29,
            "orderConversionPct": 0,
            "buyoutPct": 0,
            "problemRows": 0,
        },
        "accounts": [],
        "rows": [{"itemId": "cached-1", "title": "Cached Avito row"}],
        "source": {"mode": "live", "cache": {"status": "fresh", "savedAt": "2026-07-28T00:00:00+00:00"}},
    }

    keys = []
    monkeypatch.setattr("app.routers.avito_stats.get_source_cache", lambda _org, key, **_kwargs: keys.append(key) or cached_payload)
    monkeypatch.setattr("app.routers.avito_stats.get_user_avito_credentials_secret", lambda _user: AvitoCredentialsSecret(
        client_id="synthetic-client", client_secret="synthetic-secret", cached_access_token=None, access_token_expires_at=None,
    ))
    monkeypatch.setattr("app.routers.avito_stats.resolve_user_avito_access_token", lambda **_kwargs: "current-token")
    api = TestClient(create_app())
    headers = auth_headers(api, "viewer")

    response = api.get(
        "/api/v1/avito/stats",
        params={"dateFrom": "2026-07-01", "dateTo": "2026-07-28"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"][0]["itemId"] == "cached-1"
    assert payload["source"]["cache"]["status"] == "hit"
    assert keys[0].startswith("avito_stats:2026-07-01:2026-07-28:all:")


@pytest.mark.parametrize("empty", [False, True])
def test_stats_coalesces_refreshes_and_reads_cache_before_oauth(monkeypatch, empty):
    from app.routers import avito_stats as route
    cache = {}
    calls = []
    actor = SimpleNamespace(user_id="synthetic-user", organization_id=101)
    credentials = AvitoCredentialsSecret(client_id="test-client", client_secret="test-secret",
                                        cached_access_token=None, access_token_expires_at=None)

    class Client:
        def fetch_stats(self, request):
            calls.append(request)
            sleep(0.03)  # Overlap the callers, not their provider requests.
            result = RecordingAvitoStatsClient().fetch_stats(request)
            if empty:
                result.items = []
            return result

    monkeypatch.setattr(route, "actor_from_request", lambda _: actor)
    monkeypatch.setattr(route, "has_permission", lambda *_: True)
    monkeypatch.setattr(route, "get_user_avito_credentials_secret", lambda _: credentials)
    monkeypatch.setattr(route, "resolve_user_avito_access_token", lambda **_: "first-test-token")
    monkeypatch.setattr(route, "get_source_cache", lambda org, key, **_: cache.get((org, key)))
    monkeypatch.setattr(route, "save_source_cache", lambda org, key, payload: cache.update({(org, key): payload}))
    monkeypatch.setattr(route, "_client", lambda _: Client())

    def load(force=True, start=date(2026, 7, 1)):
        return route.get_avito_stats(None, start, date(2026, 7, 28), 28, [], force)

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: load(), range(4)))
    assert len(calls) == 1
    assert all(response["status"] == "synced" for response in responses)
    assert [response["source"]["cache"]["status"] for response in responses].count("fresh") == 1

    def oauth_unavailable(**_):
        raise AssertionError("A saved snapshot must not require OAuth")
    monkeypatch.setattr(route, "resolve_user_avito_access_token", oauth_unavailable)
    assert load()["source"]["cache"]["status"] == "hit"
    assert load(False)["rows"] == responses[0]["rows"]
    assert len(calls) == 1

    monkeypatch.setattr(route, "resolve_user_avito_access_token", lambda **_: "renewed-test-token")
    # Another period, organization or credential must never reuse this snapshot.
    load(start=date(2026, 7, 2))
    actor.organization_id = 102
    load()
    credentials = AvitoCredentialsSecret(client_id="another-test-client", client_secret="test-secret",
                                        cached_access_token=None, access_token_expires_at=None)
    load()
    assert len(calls) == 4


def test_stats_keeps_saved_rows_when_forced_refresh_is_rate_limited(monkeypatch):
    from app.routers import avito_stats as route
    cache = {}
    actor = SimpleNamespace(user_id="synthetic-user", organization_id=103)
    credentials = AvitoCredentialsSecret(client_id="test-client", client_secret="test-secret",
                                        cached_access_token=None, access_token_expires_at=None)
    scope = "synthetic-scope"
    calls = []
    def fetch(request):
        calls.append(request)
        if len(calls) == 1:
            return RecordingAvitoStatsClient().fetch_stats(request)
        return AvitoStatsFetchResult(status="blocked", error=AvitoStatsFetchError(
            code="rate_limited", message="Avito HTTP 429", retryable=True, blockerIds=["AVITO_RATE_LIMIT"]))
    monkeypatch.setattr(route, "get_source_cache", lambda org, key, **_: cache.get((org, key)))
    monkeypatch.setattr(route, "save_source_cache", lambda org, key, payload: cache.update({(org, key): payload}))
    monkeypatch.setattr(route, "resolve_user_avito_access_token", lambda **_: "test-token")
    monkeypatch.setattr(route, "_client", lambda _: SimpleNamespace(fetch_stats=fetch))
    def load():
        return route._load_stats(actor, credentials, scope, date(2026, 7, 1), date(2026, 7, 28), 28, [], True)
    first = load()
    first["source"]["cache"]["savedAt"] = "2000-01-01T00:00:00+00:00"
    for _ in range(2):
        response = load()
        assert response["status"] == "partial"
        assert response["rows"] == first["rows"]
        assert response["source"]["error"]["code"] == "rate_limited"
    assert len(calls) == 2
    assert first["source"]["error"] is None


def test_day_grouping_requests_real_daily_metrics_in_one_call():
    client = LiveAvitoStatsClient(access_token="synthetic-token", base_url="https://api.avito.ru")
    http = RecordingAvitoStatsHttpClient({"result": {"groupings": [
        {"id": "2026-07-27", "type": "day", "metrics": [{"slug": "views", "value": 10}]},
        {"id": "2026-07-28", "type": "day", "metrics": [{"slug": "views", "value": 20}]},
    ]}})
    rows, daily = client._stats_totals(http,
        AvitoStatsFetchRequest(dateFrom=date(2026, 7, 27), dateTo=date(2026, 7, 28), grouping="day"),
        [AvitoStatsAccount(accountId="account", accountName="Test")])
    assert len(http.posts) == 1
    assert http.posts[0]["json"]["grouping"] == "day"
    assert http.posts[0]["json"]["limit"] == 1000
    assert rows[0].views == 30
    assert [(point.date, point.views) for point in daily] == [(date(2026, 7, 27), 10), (date(2026, 7, 28), 20)]


def test_daily_summary_excludes_extra_comparison_days_and_cache_is_separate(monkeypatch):
    from app.routers import avito_stats as route
    cache, calls = {}, []
    monkeypatch.setattr(route, "list_source_cache_by_prefix", lambda *_, **__: [])
    actor = SimpleNamespace(user_id="test-user", organization_id=104)
    credentials = AvitoCredentialsSecret(client_id="test-client", client_secret="test-secret",
                                        cached_access_token=None, access_token_expires_at=None)
    def fetch(request):
        calls.append(request)
        return AvitoStatsFetchResult(status="synced",
            accounts=[AvitoStatsAccount(accountId="test", accountName="Test")],
            items=[AvitoStatsItem(itemId="totals:test", title="Test", accountId="test", accountName="Test", views=210)],
            daily=[AvitoStatsDailyPoint(date=date(2026, 7, day), accountId="test", accountName="Test",
                                       views=(day - 22) * 10, contacts=1) for day in range(23, 29)])
    monkeypatch.setattr(route, "get_source_cache", lambda org, key, **_: cache.get((org, key)))
    monkeypatch.setattr(route, "save_source_cache", lambda org, key, payload: cache.update({(org, key): payload}))
    monkeypatch.setattr(route, "resolve_user_avito_access_token", lambda **_: "synthetic-token")
    monkeypatch.setattr(route, "_client", lambda _: SimpleNamespace(fetch_stats=fetch))
    def load(daily):
        return route._load_stats(actor, credentials, "test-scope", date(2026, 7, 28), date(2026, 7, 28), 1, [], False, daily)
    result = load(True)
    assert calls[0].dateFrom == date(2026, 7, 23)
    assert calls[0].dateTo == date(2026, 7, 28)
    assert calls[0].grouping == "day"
    assert result["summary"]["views"] == 60
    assert result["summary"]["contacts"] == 1
    assert result["summary"]["favorites"] is None
    assert len(result["timeline"]) == 6
    assert load(True)["source"]["cache"]["status"] == "hit"
    assert len(calls) == 1
    load(False)
    assert calls[-1].grouping == "totals"
    assert len(calls) == len(cache) == 2


def test_timeline_keeps_unknown_metrics_unknown_when_aggregating_accounts():
    from app.routers.avito_stats import _timeline
    result = _timeline([
        AvitoStatsDailyPoint(date=date(2026, 7, 28), accountId="a", accountName="A", views=10, contacts=0),
        AvitoStatsDailyPoint(date=date(2026, 7, 28), accountId="b", accountName="B", views=20, contacts=None),
    ])
    assert result[0]["views"] == 30
    assert result[0]["contacts"] is None
    assert result[0]["favorites"] is None


@pytest.mark.parametrize("failure", ["oauth", "cooldown", "provider"])
def test_daily_failure_keeps_existing_totals_snapshot(monkeypatch, failure):
    from app.routers import avito_stats as route
    monkeypatch.setattr(route, "list_source_cache_by_prefix", lambda *_, **__: [])
    actor = SimpleNamespace(user_id="test-user", organization_id=105)
    credentials = AvitoCredentialsSecret(client_id="test-client", client_secret="test-secret",
                                        cached_access_token=None, access_token_expires_at=None)
    start, end, scope = date(2026, 7, 1), date(2026, 7, 28), "test-scope"
    cached = {"status": "synced", "summary": {"views": 123}, "rows": [{"views": 123}],
              "timeline": [], "period": {"dateFrom": start.isoformat(), "dateTo": end.isoformat()},
              "source": {"cache": {"status": "fresh", "savedAt": "2026-07-28T00:00:00+00:00"}}}
    total_key = route.scoped_avito_cache_key(route._cache_key(start, end, []), scope)
    def get_cache(_org, key, **_):
        if key == total_key:
            return cached
        if failure == "cooldown" and key.startswith("avito_stats_rate_limit:"):
            return {"retryAfterUntil": "2099-01-01T00:00:00+00:00"}
    def oauth(**_):
        if failure == "oauth":
            raise RuntimeError("synthetic auth unavailable")
        return "synthetic-token"
    def fetch(_):
        assert failure == "provider"
        return AvitoStatsFetchResult(status="blocked", error=AvitoStatsFetchError(
            code="rate_limited", message="Avito HTTP 429", retryable=True))
    monkeypatch.setattr(route, "get_source_cache", get_cache)
    monkeypatch.setattr(route, "save_source_cache", lambda *_: None)
    monkeypatch.setattr(route, "resolve_user_avito_access_token", oauth)
    monkeypatch.setattr(route, "_client", lambda _: SimpleNamespace(fetch_stats=fetch))
    result = route._load_stats(actor, credentials, scope, start, end, 28, [], False, True)
    assert result["status"] == "partial"
    assert result["summary"]["views"] == 123
    assert result["timeline"] == []
    assert cached["source"]["cache"]["status"] == "fresh"


def test_reuses_quarter_snapshot_for_week_without_oauth_or_provider(monkeypatch):
    from app.routers import avito_stats as route
    start, end, scope = date(2026, 7, 2), date(2026, 10, 1), "test-scope"
    from datetime import timedelta
    points = [{"date": (start + timedelta(days=i)).isoformat(), "views": 10, "contacts": 2, "spendKopecks": 100}
              for i in range(92)]
    snapshot = {"status": "synced", "period": {"dateFrom": str(start), "dateTo": str(end), "days": 92},
                "summary": {"views": 920}, "rows": [{"views": 920}], "timeline": points,
                "sourceKey": route.scoped_avito_cache_key(route._cache_key(start, end, []) + ":daily", scope),
                "source": {"cache": {"status": "fresh", "savedAt": "2026-10-01T14:00:00+00:00"}}}
    monkeypatch.setattr(route, "list_source_cache_by_prefix", lambda *_, **__: [snapshot])
    monkeypatch.setattr(route, "get_source_cache", lambda *_, **__: None)
    def no_oauth(**_):
        raise AssertionError("No OAuth or provider reads needed for a cached subrange")
    monkeypatch.setattr(route, "resolve_user_avito_access_token", no_oauth)
    actor = SimpleNamespace(user_id="test-user", organization_id=106)
    result = route._load_stats(actor, None, scope, date(2026, 9, 25), end, 7, [], False, True)
    assert result["period"] == {"dateFrom": "2026-09-25", "dateTo": "2026-10-01", "days": 7}
    assert result["summary"]["views"] == 70
    assert result["summary"]["contacts"] == 14
    assert result["summary"]["conversionPct"] == 20
    assert result["source"]["cache"]["status"] == "hit"
    assert snapshot["summary"]["views"] == 920
    assert snapshot["source"]["cache"]["status"] == "fresh"
    # Another credential or account selection cannot use this snapshot.
    assert route._daily_cache_for_period(106, "other", date(2026, 9, 25), end, 7, []) is None
    assert route._daily_cache_for_period(106, scope, date(2026, 9, 25), end, 7, ["other"]) is None
    # Wider ranges or incomplete daily history are not silently reported as complete.
    assert route._daily_cache_for_period(106, scope, date(2026, 6, 1), end, 123, []) is None
    snapshot["timeline"] = points[:-1]
    assert route._daily_cache_for_period(106, scope, date(2026, 9, 25), end, 7, []) is None
