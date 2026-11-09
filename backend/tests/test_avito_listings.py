from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from app.avito.listings import AvitoListingsFetchRequest, AvitoListingsFetchResult, AvitoListingRow, LiveAvitoListingsClient
from app.avito.stats import AvitoStatsAccount
from app.main import create_app
from tests.auth_helpers import auth_headers
from tests.test_avito_stats import RecordingAvitoItemsHttpClient, RecordingAvitoStatsHttpClient
import pytest


def test_inventory_remains_available_when_optional_analytics_fail(monkeypatch):
    client = LiveAvitoListingsClient(access_token="synthetic", base_url="https://api.avito.ru")
    def unavailable(*_):
        raise ValueError("Synthetic analytics unavailable")
    monkeypatch.setattr(client, "_v2_item_analytics_for_account", unavailable)
    rows = client._listing_rows(None, AvitoListingsFetchRequest(dateFrom=date(2026, 9, 1), dateTo=date(2026, 9, 7)),
        [AvitoStatsAccount(accountId="a", accountName="Synthetic")],
        [{"itemId": "000123", "title": "Saved inventory", "accountId": "a", "status": "active", "price": 100}])
    assert len(rows) == 1
    assert rows[0].itemId == "000123"
    assert rows[0].views is None and rows[0].impressions is None
    assert client.analytics_error.code == "avito_analytics_failed"


def test_repeated_catalog_page_is_not_published_as_complete():
    page = {"resources": [{"id": index + 1000, "title": f"Item {index}"} for index in range(99)]}
    client = LiveAvitoListingsClient(access_token="synthetic", base_url="https://api.avito.ru")
    with pytest.raises(ValueError, match="repeated"):
        client._items(RecordingAvitoItemsHttpClient([page, page]), [AvitoStatsAccount(accountId="a", accountName="Synthetic")])


def test_live_avito_listings_client_fetches_registry_statuses_and_period_metrics():
    client = LiveAvitoListingsClient(access_token="token", base_url="https://api.avito.ru")
    account = AvitoStatsAccount(accountId="365024549", accountName="Bless T")
    http_client = RecordingAvitoStatsHttpClient(
        {
            "result": {
                "groupings": [
                    {
                        "id": 8098482225,
                        "type": "items",
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

    rows = client._listing_rows(
        http_client,
        AvitoListingsFetchRequest(dateFrom=date(2026, 7, 1), dateTo=date(2026, 7, 28)),
        [account],
        [
            {
                "itemId": "8098482225",
                "title": "Live item",
                "accountId": "365024549",
                "accountName": "Bless T",
                "status": "active",
                "price": 1490,
            },
            {
                "itemId": "8098482191",
                "title": "Removed item",
                "accountId": "365024549",
                "accountName": "Bless T",
                "status": "removed",
                "price": 2990,
            },
        ],
    )

    assert account.activeItemCount == 1
    assert account.inactiveItemCount == 1
    assert rows[0].views == 6
    assert rows[0].contactsMessenger == 2
    assert rows[0].contacts == 3
    assert rows[0].contactsShowPhone == 1
    assert rows[0].contactsShowPhoneAndMessenger == 1
    assert rows[0].favorites == 8
    assert rows[0].spendKopecks == 1200
    assert rows[0].orders == 2
    assert rows[0].buyouts == 1
    assert rows[0].priceKopecks == 149_000
    assert rows[1].status == "removed"


def test_live_avito_listings_items_preserve_status_and_price_from_core_items():
    http_client = RecordingAvitoItemsHttpClient(
        [
            {
                "resources": [
                    {"id": 8098482225, "title": "Active item", "status": "active", "price": 1490},
                    {"id": 8098482191, "title": "Old item", "status": "old", "price": 2990},
                ]
            }
        ]
    )
    client = LiveAvitoListingsClient(access_token="token", base_url="https://api.avito.ru")

    rows = client._items(http_client, [AvitoStatsAccount(accountId="365024549", accountName="Bless T")])

    assert http_client.gets[0]["params"] == {"per_page": 99, "page": 1, "status": "active,removed,old"}
    assert rows[0]["status"] == "active"
    assert rows[0]["price"] == 1490
    assert rows[1]["status"] == "old"
    assert rows[1]["price"] == 2990


def test_live_avito_listings_leaves_missing_photos_for_one_time_extension_import():
    http_client = RecordingAvitoItemsHttpClient(
        [
            {
                "resources": [
                    {
                        "id": 8098482225,
                        "title": "Active item",
                        "status": "active",
                        "price": 1490,
                        "url": "https://www.avito.ru/moskva/odezhda/item_8098482225",
                    },
                ]
            },
            {
                "html": '<html><head><meta property="og:image" content="https://img.avito.st/image.jpg"></head></html>',
            },
        ]
    )
    http_client.get = lambda url, **kwargs: (  # type: ignore[method-assign]
        http_client.gets.append({"url": url, **kwargs})
        or (
            type(
                "HtmlResponse" if "www.avito.ru" in url else "JsonResponse",
                (),
                {
                    "text": '<html><head><meta property="og:image" content="https://img.avito.st/image.jpg"></head></html>' if "www.avito.ru" in url else "",
                    "raise_for_status": lambda self: None,
                    "json": lambda self: http_client.payloads.pop(0),
                },
            )()
        )
    )
    client = LiveAvitoListingsClient(access_token="token", base_url="https://api.avito.ru")

    rows = client._items(http_client, [AvitoStatsAccount(accountId="365024549", accountName="Bless T")])

    assert rows[0]["imageUrl"] is None
    assert len(http_client.gets) == 1


class RecordingListingsClient:
    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token
        self.requests: list[AvitoListingsFetchRequest] = []

    def fetch_listings(self, request: AvitoListingsFetchRequest) -> AvitoListingsFetchResult:
        self.requests.append(request)
        return AvitoListingsFetchResult(
            status="synced",
            accounts=[AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=2, activeItemCount=1, inactiveItemCount=1)],
            rows=[
                AvitoListingRow(
                    itemId="8098482225",
                    title="Live item",
                    accountId="365024549",
                    accountName="Bless T",
                    status="active",
                    priceKopecks=149_000,
                    views=6,
                    contactsMessenger=2,
                    contacts=3,
                    contactsShowPhone=1,
                    contactsShowPhoneAndMessenger=1,
                    favorites=8,
                    spendKopecks=1200,
                    orders=2,
                    buyouts=1,
                    sourceStatus="fresh",
                )
            ],
            diagnostics={"itemsCount": 2, "itemStatuses": "active,removed,old"},
        )


def test_avito_listings_endpoint_uses_saved_credentials_period_and_cache(monkeypatch):
    recording_client: RecordingListingsClient | None = None
    cached_payloads: list[tuple[int, str, dict]] = []

    def build_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingListingsClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_listings.build_avito_listings_client", build_client)
    monkeypatch.setattr("app.routers.avito_listings.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    monkeypatch.setattr("app.routers.avito_listings.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.routers.avito_listings.save_source_cache",
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
        "/api/v1/avito/listings",
        params={"dateFrom": "2026-07-01", "dateTo": "2026-07-28"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == {"dateFrom": "2026-07-01", "dateTo": "2026-07-28", "days": 28}
    assert payload["summary"]["active"] == 1
    assert payload["summary"]["inactive"] == 1
    assert payload["summary"]["views"] == 6
    assert payload["summary"]["contactsMessenger"] == 2
    assert payload["rows"][0]["priceKopecks"] == 149_000
    assert payload["rows"][0]["contactsMessenger"] == 2
    assert payload["source"]["diagnostics"]["itemStatuses"] == "active,removed,old"
    assert recording_client is not None
    assert recording_client.access_token == "avito-bearer-token"
    assert recording_client.requests[0].dateFrom == date(2026, 7, 1)
    assert cached_payloads[0][1].startswith("avito_listings_v2:2026-07-01:2026-07-28:all:")
