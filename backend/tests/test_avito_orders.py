from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.avito.orders import (
    AvitoOrderItem,
    AvitoOrderRow,
    AvitoOrdersBrowserSnapshot,
    AvitoOrdersFetchRequest,
    AvitoOrdersFetchResult,
    LiveAvitoOrdersClient,
    merge_browser_snapshot_orders,
)
from app.avito.returns import AvitoReturnCandidate
from app.cabinet.store import AvitoCredentialsSecret
from app.control_plane.auth import ActorContext
from app.main import create_app
from tests.test_avito_stats import AvitoStatsHttpResponse
import pytest


@pytest.fixture(autouse=True)
def isolated_order_asset_store(monkeypatch):
    from app.avito import listing_photos
    from app.routers import avito_orders
    monkeypatch.setattr(listing_photos, "photo_index", lambda _: {})
    monkeypatch.setattr(avito_orders, "enrich_labels", lambda *_: None)


class RecordingAvitoOrdersHttpClient:
    def __init__(self, payload: dict) -> None:
        self.payloads = [payload]
        self.gets: list[dict] = []

    def get(self, url: str, **kwargs) -> AvitoStatsHttpResponse:
        self.gets.append({"url": url, **kwargs})
        payload = self.payloads.pop(0) if self.payloads else {}
        if isinstance(payload, tuple):
            body, status_code = payload
            return AvitoStatsHttpResponse(body, status_code)
        return AvitoStatsHttpResponse(payload)


@pytest.mark.parametrize("failure,expected_status,expected_calls", [
    ("ConnectTimeout", "synced", 2), ("ReadTimeout", "blocked", 1),
])
def test_orders_retries_only_failed_connection_once(failure, expected_status, expected_calls):
    import httpx
    calls = []
    class Client:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise getattr(httpx, failure)("test failure")
            return AvitoStatsHttpResponse({"orders": [], "total": 0})
    result = LiveAvitoOrdersClient("test-token").fetch_orders(AvitoOrdersFetchRequest(), http_client=Client())
    assert result.status == expected_status and len(calls) == expected_calls


@pytest.mark.parametrize("accounts, expected_status", [(["a"], "closed"), (["a", "b"], "on_return")])
def test_browser_terminal_status_reconciles_only_unique_saved_account(monkeypatch, accounts, expected_status):
    cache = {(1, "avito_orders_browser_snapshot"): {
        "capturedAt": "2026-10-07T10:00:00Z", "collector": {"checkpoint": True},
        "orders": [], "returns": [{"orderId": "000123", "accountId": account, "status": "on_return",
            "items": [{"itemId": "000456", "title": "Saved product"}]} for account in accounts],
    }}
    monkeypatch.setattr("app.routers.avito_orders.actor_from_request", lambda _: ActorContext(
        actor_id="user:test", user_id="test", organization_id=1, permission_profile="admin",
        permissions=frozenset({"cabinet:read"}), email="test@example.test"))
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda *_: True)
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _: None)
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda org, key, **_: cache.get((org, key)))
    monkeypatch.setattr("app.routers.avito_orders.save_source_cache", lambda org, key, payload: cache.__setitem__((org, key), payload))
    monkeypatch.setattr("app.routers.avito_orders.enrich_existing_return_candidates", lambda *_: None)
    response = TestClient(create_app()).post("/api/v1/avito/orders/browser-snapshot", json={
        "capturedAt": "2026-10-07T11:00:00Z", "collector": {"checkpoint": True},
        "returns": [{"orderId": "000123", "status": "closed", "returnStatus": "received", "items": []},
                    {"orderId": "historical-unknown", "status": "closed", "items": []}],
    })
    assert response.status_code == 200
    rows = cache[(1, "avito_orders_browser_snapshot")]["returns"]
    assert len(rows) == len(accounts)
    assert all(row["status"] == expected_status for row in rows)
    assert all(row["items"][0]["itemId"] == "000456" for row in rows)


def test_first_browser_terminal_update_uses_current_credential_queue(monkeypatch):
    from app.routers import avito_orders
    credentials = AvitoCredentialsSecret(client_id="synthetic-client", client_secret="synthetic-secret", cached_access_token=None, access_token_expires_at=None)
    key = avito_orders.scoped_avito_cache_key(avito_orders.AVITO_ORDERS_QUEUE_KEY, "synthetic-client\0synthetic-secret")
    cache = {(1, key): {"rows": [{"orderId": "api-order", "marketplaceId": "000123", "accountId": "a",
        "status": "ready_to_ship", "items": [{"itemId": "000456", "title": "Saved product"}]}]}}
    monkeypatch.setattr(avito_orders, "actor_from_request", lambda _: ActorContext(
        actor_id="user:test", user_id="test", organization_id=1, permission_profile="admin",
        permissions=frozenset({"cabinet:read"}), email="test@example.test"))
    monkeypatch.setattr(avito_orders, "has_permission", lambda *_: True)
    monkeypatch.setattr(avito_orders, "get_organization_avito_credentials_secret", lambda _: credentials)
    monkeypatch.setattr(avito_orders, "get_source_cache", lambda org, key, **_: cache.get((org, key)))
    monkeypatch.setattr(avito_orders, "save_source_cache", lambda org, key, payload: cache.__setitem__((org, key), payload))
    response = TestClient(create_app()).post("/api/v1/avito/orders/browser-snapshot", json={
        "capturedAt": "2026-10-07T11:00:00Z", "collector": {"checkpoint": True},
        "orders": [{"orderId": "000123", "status": "canceled", "items": []}]})
    assert response.status_code == 200
    row = cache[(1, avito_orders.AVITO_ORDERS_BROWSER_SNAPSHOT_KEY)]["orders"][0]
    assert row["accountId"] == "a" and row["status"] == "canceled" and row["orderId"] == "api-order"
    snapshot = AvitoOrdersBrowserSnapshot.model_validate(cache[(1, avito_orders.AVITO_ORDERS_BROWSER_SNAPSHOT_KEY)])
    api_row = AvitoOrderRow.model_validate(cache[(1, key)]["rows"][0])
    avito_orders._reconcile_export_statuses([api_row], avito_orders._browser_snapshot_rows(snapshot, statuses=["canceled"]), None)
    assert api_row.status == "canceled"


def test_live_avito_orders_client_gets_order_management_orders_and_normalizes_rows():
    http_client = RecordingAvitoOrdersHttpClient(
        {
            "orders": [
                {
                    "id": "ord_1",
                    "marketplaceId": "123456",
                    "status": "on_confirmation",
                    "delivery": {"type": "pvz", "serviceName": "Avito Delivery", "trackNumber": "TRACK-1"},
                    "buyer": {"name": "Иван"},
                    "total": {"amount": 1590},
                    "items": [{"id": 8098482225, "title": "Худи", "quantity": 2, "price": {"amount": 795}, "sellerArticle": "BT-42"}],
                    "availableActions": [{"name": "confirm", "required": True}, {"name": "reject", "required": False}],
                }
            ],
            "total": 1,
        }
    )
    client = LiveAvitoOrdersClient(access_token="token", base_url="https://api.avito.ru")

    result = client.fetch_orders(
        AvitoOrdersFetchRequest(dateFrom=date(2026, 7, 1), statuses=["on_confirmation"], limit=20, page=2),
        http_client=http_client,
    )

    assert http_client.gets[0]["url"] == "https://api.avito.ru/order-management/1/orders"
    assert http_client.gets[0]["params"] == {"limit": 20, "page": 2, "dateFrom": 1782864000, "statuses": "on_confirmation"}
    assert result.status == "synced"
    assert result.total == 1
    assert result.orders[0].orderId == "ord_1"
    assert result.orders[0].marketplaceId == "123456"
    assert result.orders[0].deliveryType == "pvz"
    assert result.orders[0].trackNumber == "TRACK-1"
    assert result.orders[0].totalKopecks == 159_000
    assert result.orders[0].items[0].itemId == "8098482225"
    assert result.orders[0].items[0].quantity == 2
    assert result.orders[0].availableActions[0].name == "confirm"
    assert result.orders[0].availableActions[0].required is True


def test_zero_quantity_is_not_turning_into_one_item():
    items = LiveAvitoOrdersClient("token")._items({"items": [{"id": "returned-line", "quantity": 0}]})
    assert items[0].quantity == 0


def test_live_avito_orders_client_returns_safe_diagnostics_on_avito_error():
    http_client = RecordingAvitoOrdersHttpClient(({"error": {"message": "forbidden"}}, 403))
    client = LiveAvitoOrdersClient(access_token="token", base_url="https://api.avito.ru")

    result = client.fetch_orders(AvitoOrdersFetchRequest(dateFrom=date(2026, 7, 15)), http_client=http_client)

    assert result.status == "blocked"
    assert result.error is not None
    assert result.error.code == "forbidden_scope"
    assert result.diagnostics is not None
    assert result.diagnostics["status"] == 403
    assert result.diagnostics == {
        "status": 403, "endpoint": "GET /order-management/1/orders",
        "ordersCount": 0, "reason": "avito_http_error",
    }
    assert "bodyPreview" not in result.diagnostics


def test_live_avito_orders_client_does_not_log_order_body(caplog, monkeypatch):
    # Other application-entrypoint tests can disable existing loggers globally.
    # Exercise this logger's real output without retaining that unrelated state.
    logger = logging.getLogger("app.avito.orders")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", True)
    http_client = RecordingAvitoOrdersHttpClient(
        {
            "orders": [
                {
                    "id": "private-order-id",
                    "buyer": {"name": "private-buyer-name", "phone": "+79991234567"},
                    "delivery": {"address": "private-delivery-address"},
                    "privateMarker": "private-nested-value",
                }
            ]
        }
    )
    client = LiveAvitoOrdersClient(access_token="token", base_url="https://api.avito.ru")

    with caplog.at_level(logging.WARNING, logger="app.avito.orders"):
        result = client.fetch_orders(AvitoOrdersFetchRequest(), http_client=http_client)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert result.orders[0].buyerName == "private-buyer-name"
    assert result.diagnostics is not None
    assert "bodyPreview" not in result.diagnostics
    assert "private-nested-value" not in str(result.diagnostics)
    assert "[AVITO_ORDERS_STATUS] 200" in messages
    assert "[AVITO_ORDERS_COUNT] 1" in messages
    assert "[AVITO_ORDERS_ENDPOINT] GET /order-management/1/orders" in messages
    assert "[AVITO_ORDERS_REQUEST_URL]" not in messages
    assert "[AVITO_ORDERS_REQUEST_PARAMS]" not in messages
    assert "[AVITO_ORDERS_BODY]" not in messages
    assert "private-buyer-name" not in messages
    assert "+79991234567" not in messages
    assert "private-delivery-address" not in messages
    assert "private-nested-value" not in messages


def test_live_avito_orders_client_parses_avito_prices_delivery_and_schedule_shape():
    http_client = RecordingAvitoOrdersHttpClient(
        {
            "hasMore": True,
            "orders": [
                {
                    "id": "50000000422550798",
                    "marketplaceId": "70000000482858082",
                    "status": "ready_to_ship",
                    "delivery": {"serviceName": "Яндекс Доставка", "serviceType": "pvz", "trackingNumber": "P06516051994"},
                    "prices": {"commission": 161, "discount": 195, "price": 1300, "total": 944},
                    "items": [
                        {
                            "avitoId": "8226657890",
                            "count": 1,
                            "prices": {"commission": 161, "discountSum": 195, "price": 1300, "total": 944},
                            "title": "Футболка Maison de reve",
                        }
                    ],
                    "schedules": {"shipTill": "2026-07-31T21:00:00Z"},
                }
            ],
        }
    )
    client = LiveAvitoOrdersClient(access_token="token", base_url="https://api.avito.ru")

    result = client.fetch_orders(AvitoOrdersFetchRequest(dateFrom=date(2026, 6, 30)), http_client=http_client)

    assert result.status == "synced"
    assert result.orders[0].deliveryType == "pvz"
    assert result.orders[0].deliveryService == "Яндекс Доставка"
    assert result.orders[0].trackNumber == "P06516051994"
    assert result.orders[0].totalKopecks == 94_400
    assert result.orders[0].items[0].priceKopecks == 94_400
    assert result.orders[0].schedules == [{"shipTill": "2026-07-31T21:00:00Z"}]


def test_live_avito_orders_client_parses_shop_name_item_article_size_and_color():
    http_client = RecordingAvitoOrdersHttpClient(
        {
            "orders": [
                {
                    "id": "ord_2",
                    "marketplaceId": "654321",
                    "status": "ready_to_ship",
                    "seller": {"name": "Bless T"},
                    "items": [
                        {
                            "avitoId": "9001",
                            "title": "Лонгслив оверсайз Givenchy Vintage Stars",
                            "count": 1,
                            "seller": {"article": "Лчbt_0212"},
                            "parameters": [
                                {"name": "Размер", "value": "M"},
                                {"name": "Цвет", "value": "черный"},
                            ],
                        }
                    ],
                }
            ],
            "total": 1,
        }
    )
    client = LiveAvitoOrdersClient(access_token="token", base_url="https://api.avito.ru")

    result = client.fetch_orders(AvitoOrdersFetchRequest(dateFrom=date(2026, 7, 1)), http_client=http_client)

    assert result.orders[0].accountName == "Bless T"
    assert result.orders[0].items[0].sellerArticle == "Лчbt_0212"
    assert result.orders[0].items[0].size == "M"
    assert result.orders[0].items[0].color == "черный"


class RecordingOrdersClient:
    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token
        self.requests: list[AvitoOrdersFetchRequest] = []

    def fetch_orders(self, request: AvitoOrdersFetchRequest) -> AvitoOrdersFetchResult:
        self.requests.append(request)
        return AvitoOrdersFetchResult(
            status="synced",
            orders=[
                AvitoOrderRow(
                    orderId="ord_1",
                    marketplaceId="123456",
                    accountName="Bless T",
                    status="ready_to_ship",
                    statusSource="avito_api",
                    deliveryType="pvz",
                    trackNumber="TRACK-1",
                    totalKopecks=159_000,
                    items=[
                        AvitoOrderItem(
                            itemId="8098482225",
                            title="Худи черный размер M",
                            quantity=1,
                            priceKopecks=159_000,
                            sellerArticle="BT-42",
                        )
                    ],
                )
            ],
            total=1,
            diagnostics={"endpoint": "GET /order-management/1/orders"},
        )


def _xlsx_cells(content: bytes) -> dict[str, str]:
    with ZipFile(BytesIO(content)) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml")
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(sheet_xml)
    cells: dict[str, str] = {}
    for cell in root.findall(".//x:c", ns):
        ref = cell.attrib["r"]
        inline = cell.find("x:is/x:t", ns)
        if inline is not None and inline.text is not None:
            cells[ref] = inline.text
            continue
        value = cell.find("x:v", ns)
        if value is not None and value.text is not None:
            cells[ref] = value.text
    return cells


def _xlsx_sheet_names(content: bytes) -> list[str]:
    with ZipFile(BytesIO(content)) as archive:
        workbook_xml = archive.read("xl/workbook.xml")
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(workbook_xml)
    return [sheet.attrib["name"] for sheet in root.findall(".//x:sheet", ns)]


def test_avito_orders_endpoint_uses_saved_credentials_period_statuses_and_cache(monkeypatch):
    recording_client: RecordingOrdersClient | None = None
    cached_payloads: list[tuple[int, str, dict]] = []

    def build_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingOrdersClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_orders.build_avito_orders_client", build_client)
    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:viewer",
            user_id="viewer",
            organization_id=1,
            permission_profile="viewer",
            permissions=frozenset({"cabinet:read"}),
            email="viewer@vella.local",
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda _actor, permission: permission == "cabinet:read")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_orders.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.routers.avito_orders.save_source_cache",
        lambda organization_id, source_key, payload: cached_payloads.append((organization_id, source_key, payload)),
    )
    api = TestClient(create_app())

    response = api.get(
        "/api/v1/avito/orders",
        params={"dateFrom": "2026-07-01", "status": ["ready_to_ship", "delivered"], "page": 2, "limit": 20},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"]["dateFrom"] == "2026-07-01"
    assert payload["filters"] == {"statuses": ["ready_to_ship", "delivered"], "page": 2, "limit": 20}
    assert payload["summary"]["readyToShip"] == 1
    assert payload["summary"]["totalKopecks"] == 159_000
    assert payload["rows"][0]["orderId"] == "ord_1"
    assert payload["source"]["api"]["ordersEndpoint"] == "GET /order-management/1/orders"
    assert recording_client is not None
    assert recording_client.access_token == "avito-bearer-token"
    assert recording_client.requests[0].dateFrom == date(2026, 7, 1)
    assert recording_client.requests[0].statuses == ["ready_to_ship", "delivered"]
    assert cached_payloads[0][1].startswith("avito_orders:2026-07-01:ready_to_ship,delivered:p2:l20:")


def test_avito_orders_endpoint_adds_return_reuse_suggestion(monkeypatch):
    class MatchingOrdersClient(RecordingOrdersClient):
        def fetch_orders(self, request: AvitoOrdersFetchRequest) -> AvitoOrdersFetchResult:
            self.requests.append(request)
            return AvitoOrdersFetchResult(
                status="synced",
                orders=[
                    AvitoOrderRow(
                        orderId="new_1",
                        marketplaceId="9001",
                        status="ready_to_ship",
                        items=[
                            AvitoOrderItem(
                                itemId="item_new",
                                title="Футболка белая Принт 42",
                                sellerArticle="FBBT_42",
                                size="M",
                                color="белая",
                            )
                        ],
                    )
                ],
                total=1,
                diagnostics={"endpoint": "GET /order-management/1/orders"},
            )

    monkeypatch.setattr("app.routers.avito_orders.build_avito_orders_client", lambda *_args, **kwargs: MatchingOrdersClient(access_token=kwargs["access_token"]))
    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:viewer",
            user_id="viewer",
            organization_id=1,
            permission_profile="viewer",
            permissions=frozenset({"cabinet:read"}),
            email="viewer@vella.local",
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda _actor, permission: permission == "cabinet:read")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_orders.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.routers.avito_orders.save_source_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.routers.avito_orders.list_active_return_candidates",
        lambda organization_id: [
            AvitoReturnCandidate(
                returnOrderId="ret_1",
                marketplaceId="7001",
                itemId="item_ret",
                title="Футболка белая Принт 42",
                sellerArticle="FBBT_42",
                size="M",
                color="white",
                status="on_return",
                returnStatus="ready_for_pickup",
                lastSeenAt="2026-08-07T09:00:00+00:00",
                receivedQuantity=1,
                inspectedQuantity=1,
                availableQuantity=1,
            )
        ],
    )
    api = TestClient(create_app())

    response = api.get("/api/v1/avito/orders", params={"dateFrom": "2026-07-01", "forceRefresh": "true"})

    assert response.status_code == 200
    payload = response.json()
    suggestion = payload["rows"][0]["items"][0]["reuseSuggestion"]
    assert suggestion["returnOrderId"] == "ret_1"
    assert suggestion["reason"] == "article_size_color"
    assert suggestion["score"] == 100
    assert payload["rows"][0]["items"][0]["returnMatches"][0]["marketplaceId"] == "7001"
    assert payload["source"]["returnInventory"]["candidates"] == 1


def test_avito_orders_browser_snapshot_enriches_live_rows(monkeypatch):
    recording_client: RecordingOrdersClient | None = None
    cache: dict[tuple[int, str], dict] = {}

    def build_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingOrdersClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_orders.build_avito_orders_client", build_client)
    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:viewer",
            user_id="viewer",
            organization_id=1,
            permission_profile="viewer",
            permissions=frozenset({"cabinet:read"}),
            email="viewer@vella.local",
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda _actor, permission: permission == "cabinet:read")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_orders.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda organization_id, key, **_kwargs: cache.get((organization_id, key)))
    monkeypatch.setattr("app.routers.avito_orders.save_source_cache", lambda organization_id, key, payload: cache.__setitem__((organization_id, key), payload))
    api = TestClient(create_app())

    snapshot_response = api.post(
        "/api/v1/avito/orders/browser-snapshot",
        json={
            "capturedAt": "2026-07-22T09:12:00+00:00",
            "pageUrl": "https://www.avito.ru/profile/orders",
            "orders": [
                {
                    "orderId": "ord_1",
                    "marketplaceId": "123456",
                    "status": "ready_to_ship",
                    "items": [
                        {
                            "itemId": "8098482225",
                            "title": "Худи черный размер M",
                            "imageUrl": "https://70.img.avito.st/image.jpg",
                            "sellerArticle": "BT-42-BROWSER",
                            "size": "L",
                            "color": "графит",
                            "sources": {"imageUrl": "order_row", "sellerArticle": "order_detail", "size": "order_detail", "color": "order_detail"},
                        }
                    ],
                }
            ],
        },
    )
    assert snapshot_response.status_code == 200

    response = api.get(
        "/api/v1/avito/orders",
        params={"dateFrom": "2026-07-01", "status": "ready_to_ship", "forceRefresh": "true"},
    )

    assert response.status_code == 200
    payload = response.json()
    item = payload["rows"][0]["items"][0]
    assert item["imageUrl"] == "https://70.img.avito.st/image.jpg"
    assert item["sellerArticle"] == "BT-42"
    assert item["size"] == "L"
    assert item["color"] == "графит"
    assert payload["summary"]["total"] == 1
    assert payload["source"]["mode"] == "api-filtered-by-browser-snapshot"
    assert payload["source"]["browserSnapshot"]["orders"] == 1
    assert recording_client is not None


def test_avito_orders_browser_snapshot_filters_extra_live_rows(monkeypatch):
    class ExtraRowsClient(RecordingOrdersClient):
        def fetch_orders(self, request: AvitoOrdersFetchRequest) -> AvitoOrdersFetchResult:
            self.requests.append(request)
            return AvitoOrdersFetchResult(
                status="synced",
                orders=[
                    AvitoOrderRow(orderId="api_1", marketplaceId="70000000487456344", status="ready_to_ship", items=[AvitoOrderItem(title="Товар")]),
                    AvitoOrderRow(orderId="api_2", marketplaceId="70000000400000000", status="ready_to_ship", items=[AvitoOrderItem(title="Лишний товар")]),
                ],
                total=2,
                diagnostics={"endpoint": "GET /order-management/1/orders"},
            )

    cache: dict[tuple[int, str], dict] = {
        (
            1,
            "avito_orders_browser_snapshot",
        ): {
            "orders": [
                {
                    "orderId": "70000000487456344",
                    "marketplaceId": "70000000487456344",
                    "items": [{"title": "Лонгслив мерч Kai angel damage", "imageUrl": "https://70.img.avito.st/order.jpg"}],
                }
            ],
        },
    }

    monkeypatch.setattr("app.routers.avito_orders.build_avito_orders_client", lambda *_args, **kwargs: ExtraRowsClient(access_token=kwargs["access_token"]))
    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:viewer",
            user_id="viewer",
            organization_id=1,
            permission_profile="viewer",
            permissions=frozenset({"cabinet:read"}),
            email="viewer@vella.local",
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda _actor, permission: permission == "cabinet:read")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_orders.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda organization_id, key, **_kwargs: cache.get((organization_id, key)))
    api = TestClient(create_app())

    response = api.get("/api/v1/avito/orders", params={"dateFrom": "2026-07-01", "status": "ready_to_ship"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] == 1
    assert [row["marketplaceId"] for row in payload["rows"]] == ["70000000487456344"]
    assert payload["rows"][0]["items"][0]["title"] == "Лонгслив мерч Kai angel damage"


def test_avito_orders_extension_token_can_be_regenerated_and_used_for_snapshot(monkeypatch):
    cache: dict[tuple[int, str], dict] = {}

    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:admin",
            user_id="admin",
            organization_id=1,
            permission_profile="admin",
            permissions=frozenset({"cabinet:read", "integrations:write"}),
            email="admin@vella.local",
        ),
    )
    monkeypatch.setattr(
        "app.routers.avito_orders.has_permission",
        lambda _actor, permission: permission in {"cabinet:read", "integrations:write"},
    )
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda organization_id, key, **_kwargs: cache.get((organization_id, key)))
    monkeypatch.setattr("app.routers.avito_orders.save_source_cache", lambda organization_id, key, payload: cache.__setitem__((organization_id, key), payload) or payload)
    monkeypatch.setattr(
        "app.routers.avito_orders.get_source_cache_by_source_key",
        lambda key, **_kwargs: next(
            (dict(payload, organizationId=organization_id, sourceKey=source_key) for (organization_id, source_key), payload in cache.items() if source_key == key),
            None,
        ),
    )
    api = TestClient(create_app())

    empty_status = api.get("/api/v1/avito/orders/extension-token")
    assert empty_status.status_code == 200
    assert empty_status.json()["configured"] is False

    issued = api.post("/api/v1/avito/orders/extension-token/regenerate")
    assert issued.status_code == 200
    issued_payload = issued.json()
    token = issued_payload["token"]
    assert token.startswith("sat_avito_")
    assert issued_payload["configured"] is True
    assert issued_payload["tokenPrefix"] == token[:18]

    posted = api.post(
        "/api/v1/avito/orders/browser-snapshot",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "capturedAt": "2026-07-22T09:12:00+00:00",
            "pageUrl": "https://www.avito.ru/profile/orders",
            "orders": [{"orderId": "ord_ext", "marketplaceId": "999999", "status": "ready_to_ship", "items": [{"title": "Футболка", "size": "M"}]}],
        },
    )

    assert posted.status_code == 200
    assert posted.json()["browserSnapshot"]["orders"] == 1
    assert cache[(1, "avito_orders_browser_snapshot")]["orders"][0]["orderId"] == "ord_ext"
    assert cache[(1, "avito_orders_extension_token")]["lastUsedAt"]


def test_avito_orders_extension_token_regeneration_revokes_previous_token(monkeypatch):
    cache: dict[tuple[int, str], dict] = {}

    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:admin",
            user_id="admin",
            organization_id=1,
            permission_profile="admin",
            permissions=frozenset({"cabinet:read", "integrations:write"}),
            email="admin@vella.local",
        ),
    )
    monkeypatch.setattr(
        "app.routers.avito_orders.has_permission",
        lambda _actor, permission: permission in {"cabinet:read", "integrations:write"},
    )
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda organization_id, key, **_kwargs: cache.get((organization_id, key)))
    monkeypatch.setattr("app.routers.avito_orders.save_source_cache", lambda organization_id, key, payload: cache.__setitem__((organization_id, key), payload) or payload)
    monkeypatch.setattr(
        "app.routers.avito_orders.get_source_cache_by_source_key",
        lambda key, **_kwargs: next(
            (dict(payload, organizationId=organization_id, sourceKey=source_key) for (organization_id, source_key), payload in cache.items() if source_key == key),
            None,
        ),
    )
    api = TestClient(create_app())

    old_token = api.post("/api/v1/avito/orders/extension-token/regenerate").json()["token"]
    new_token = api.post("/api/v1/avito/orders/extension-token/regenerate").json()["token"]
    assert old_token != new_token

    posted = api.post(
        "/api/v1/avito/orders/browser-snapshot",
        headers={"Authorization": f"Bearer {old_token}"},
        json={"orders": [{"orderId": "old", "items": []}]},
    )

    assert posted.status_code == 401


def test_avito_orders_picking_list_xlsx_matches_avito_order_rows(monkeypatch):
    fixture = RecordingOrdersClient().fetch_orders(AvitoOrdersFetchRequest(date_from=date.today()))
    cached = {'complete': True, 'lastSuccessfulRefresh': datetime.now(timezone.utc).isoformat(),
              'rows': [row.model_dump(mode='json') for row in fixture.orders]}
    monkeypatch.setattr('app.routers.avito_orders.get_source_cache', lambda org, key, **kw: cached if key.startswith('avito_orders_queue_v1') else None)
    monkeypatch.setattr(
        "app.routers.avito_orders._listing_dicts_from_cache",
        lambda *_args: [{
            "itemId": "8098482225", "size": "M", "color": "черный",
            "imageUrl": "https://synthetic.invalid/item.png",
        }],
    )
    recording_client: RecordingOrdersClient | None = None

    def build_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingOrdersClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_orders.build_avito_orders_client", build_client)
    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:viewer",
            user_id="viewer",
            organization_id=1,
            permission_profile="viewer",
            permissions=frozenset({"cabinet:read"}),
            email="viewer@vella.local",
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda _actor, permission: permission == "cabinet:read")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_orders.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    api = TestClient(create_app())

    response = api.get(
        "/api/v1/avito/orders/picking-list.xlsx",
        params={"dateFrom": "2026-07-01", "status": "ready_to_ship"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "avito-picking-list-" in response.headers["content-disposition"]
    assert _xlsx_sheet_names(response.content) == ["Лист подбора"]
    cells = _xlsx_cells(response.content)
    assert [cells[f"{column}1"] for column in "ABCDEFGHIJK"] == [
        "№", "Фото", "Наименование", "Размер", "Цвет", "Количество, шт.",
        "Номер отправления", "Стикер", "Номер заказа", "ID товара Авито", "Пункт приема",
    ]
    assert cells["A2"] == "1"
    assert cells["B2"] == "Фото не получено"
    assert cells["C2"] == "Худи черный размер M"
    assert cells["D2"] == "Не получено"
    assert cells["E2"] == "Не получено"
    assert cells["F2"] == "1"
    assert cells["G2"] == "Не собрано"
    assert cells["H2"] == "Этикетка не получена"
    assert cells["I2"] == "123456"
    assert recording_client is None  # No provider client or full sync during export.


def test_avito_orders_picking_list_xlsx_uses_browser_snapshot(monkeypatch):
    cache: dict[tuple[int, str], dict] = {
        (
            1,
            "avito_orders_browser_snapshot",
        ): {
            "capturedAt": "2026-07-22T09:12:00+00:00",
            "pageUrl": "https://www.avito.ru/profile/orders",
            "orders": [
                {
                    "orderId": "ord_1",
                    "marketplaceId": "123456",
                    "items": [
                        {
                            "itemId": "8098482225",
                            "imageUrl": "https://70.img.avito.st/image.jpg",
                            "sellerArticle": "BT-42-XLSX",
                            "size": "XL",
                            "color": "молочный",
                            "sources": {"imageUrl": "order_row", "sellerArticle": "order_detail", "size": "order_detail", "color": "order_detail"},
                        }
                    ],
                }
            ],
        },
    }

    monkeypatch.setattr("app.routers.avito_orders.build_avito_orders_client", lambda *_args, **kwargs: RecordingOrdersClient(access_token=kwargs["access_token"]))
    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:viewer",
            user_id="viewer",
            organization_id=1,
            permission_profile="viewer",
            permissions=frozenset({"cabinet:read"}),
            email="viewer@vella.local",
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda _actor, permission: permission == "cabinet:read")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_orders.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    fixture = RecordingOrdersClient().fetch_orders(AvitoOrdersFetchRequest(date_from=date.today()))
    queue = {'complete': True, 'lastSuccessfulRefresh': datetime.now(timezone.utc).isoformat(),
             'rows': [row.model_dump(mode='json') for row in fixture.orders]}
    monkeypatch.setattr("app.routers.avito_orders.get_source_cache", lambda organization_id, key, **_kwargs: queue if key.startswith('avito_orders_queue_v1') else cache.get((organization_id, key)))
    api = TestClient(create_app())

    response = api.get(
        "/api/v1/avito/orders/picking-list.xlsx",
        params={"dateFrom": "2026-07-01", "status": "ready_to_ship"},
    )

    assert response.status_code == 200
    cells = _xlsx_cells(response.content)
    assert cells["D2"] == "XL"
    assert cells["E2"] == "молочный"
    assert cells["J2"] == "8098482225"


def test_avito_orders_endpoint_ignores_blocked_cache_and_refetches(monkeypatch):
    recording_client: RecordingOrdersClient | None = None

    def build_client(*_args, **kwargs):
        nonlocal recording_client
        recording_client = RecordingOrdersClient(access_token=kwargs["access_token"])
        return recording_client

    monkeypatch.setattr("app.routers.avito_orders.build_avito_orders_client", build_client)
    monkeypatch.setattr(
        "app.routers.avito_orders.actor_from_request",
        lambda _request: ActorContext(
            actor_id="user:viewer",
            user_id="viewer",
            organization_id=1,
            permission_profile="viewer",
            permissions=frozenset({"cabinet:read"}),
            email="viewer@vella.local",
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.has_permission", lambda _actor, permission: permission == "cabinet:read")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_user_avito_credentials_secret",
        lambda _user_id: AvitoCredentialsSecret(
            client_id="avito_client_0987654321",
            client_secret="avito_secret_1234567890",
            cached_access_token=None,
            access_token_expires_at=None,
        ),
    )
    monkeypatch.setattr("app.routers.avito_orders.get_organization_avito_credentials_secret", lambda _organization_id: None)
    monkeypatch.setattr("app.routers.avito_orders.resolve_user_avito_access_token", lambda **_kwargs: "avito-bearer-token")
    monkeypatch.setattr(
        "app.routers.avito_orders.get_source_cache",
        lambda _org, source_key, **_kwargs: None
        if source_key == "avito_orders_browser_snapshot" else {
            "status": "blocked",
            "period": {"dateFrom": "2026-07-15", "days": 15},
            "filters": {"statuses": [], "page": 1, "limit": 20},
            "rows": [],
            "source": {"error": {"code": "forbidden_scope", "message": "Avito HTTP 403"}},
        },
    )
    monkeypatch.setattr("app.routers.avito_orders.save_source_cache", lambda *_args, **_kwargs: None)
    api = TestClient(create_app())

    response = api.get("/api/v1/avito/orders", params={"dateFrom": "2026-07-15"})

    assert response.status_code == 200
    assert response.json()["status"] == "synced"
    assert response.json()["rows"][0]["orderId"] == "ord_1"
    assert recording_client is not None
    assert len(recording_client.requests) == 1


def test_browser_snapshot_preserves_size_fallback_and_provenance():
    snapshot = AvitoOrdersBrowserSnapshot.model_validate(
        {
            "orders": [
                {
                    "orderId": "order-1",
                    "items": [
                        {
                            "itemId": "8098284629",
                            "title": "Свитшот",
                            "descriptionSize": "54 (XL)",
                            "sources": {"size": "description_fallback"},
                        }
                    ],
                }
            ]
        }
    )
    rows = [
        AvitoOrderRow(
            orderId="order-1",
            items=[AvitoOrderItem(itemId="8098284629", title="Свитшот")],
        )
    ]

    merge_browser_snapshot_orders(rows, snapshot)

    assert snapshot.orders[0].items[0].descriptionSize == "54 (XL)"
    assert rows[0].items[0].descriptionSize == "54 (XL)"
    assert rows[0].items[0].sources == {"size": "description_fallback"}
