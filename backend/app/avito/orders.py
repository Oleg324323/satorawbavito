from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, Field
from app.modules.orders import MappingState, map_avito_status

try:
    import httpx
except Exception:  # pragma: no cover - live mode dependency
    httpx = None  # type: ignore[assignment]


AVITO_ORDER_STATUSES = [
    "on_confirmation",
    "ready_to_ship",
    "in_transit",
    "delivered",
    "closed",
    "canceled",
    "on_return",
    "in_dispute",
]

logger = logging.getLogger(__name__)

ReturnMatchReason = Literal[
    "article_size_color",
    "title_size_color",
    "article_size_color_missing",
    "title_size_color_missing",
]


class AvitoReturnMatch(BaseModel):
    returnOrderId: str = Field(min_length=1)
    marketplaceId: str | None = None
    itemId: str | None = None
    title: str = Field(min_length=1)
    sellerArticle: str | None = None
    size: str | None = None
    color: str | None = None
    imageUrl: str | None = None
    quantity: int = Field(default=1, ge=0)
    score: int = Field(ge=0, le=100)
    reason: ReturnMatchReason
    status: str = "on_return"
    returnStatus: str | None = None
    lastSeenAt: str | None = None
    returnItemId: int | None = None
    availableQuantity: int = 0
    receivedQuantity: int = 0
    inspectedQuantity: int = 0


class AvitoOrdersFetchRequest(BaseModel):
    dateFrom: date | None = None
    statuses: list[str] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=20)
    page: int = Field(default=1, ge=1)


class AvitoOrderAction(BaseModel):
    name: str = Field(min_length=1)
    required: bool = False


class AvitoChatMessage(BaseModel):
    id: str = Field(default="", max_length=128)
    role: Literal["seller", "buyer", "unknown"] = "unknown"
    text: str = Field(default="", max_length=1000)
    createdAt: str | None = None
    orderId: str | None = None
    itemId: str | None = None
    quoted: bool = False


class AvitoChatEvidence(BaseModel):
    accountId: str | None = None
    orderId: str | None = None
    itemId: str | None = None
    channelId: str | None = None
    buyerId: str | None = None
    sellerId: str | None = None
    sellerAuthorId: str | None = None
    capturedAt: str | None = None
    state: str = "unavailable"
    reason: str | None = None
    truncated: bool = False
    messages: list[AvitoChatMessage] = Field(default_factory=list, max_length=50)


class AvitoOrderItem(BaseModel):
    itemId: str | None = None
    lineIndex: int | None = Field(default=None, ge=0)
    title: str = "Товар Авито"
    quantity: int = Field(default=1, ge=0)
    priceKopecks: int | None = Field(default=None, ge=0)
    sellerArticle: str | None = None
    brand: str | None = None
    size: str | None = None
    descriptionSize: str | None = None
    sizeMode: str | None = None
    sizeState: str | None = None
    sizeReason: str | None = None
    sizeEvidence: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, str | None] = Field(default_factory=dict)
    color: str | None = None
    imageUrl: str | None = None
    barcode: str | None = None
    photoId: int | None = None
    returnMatches: list[AvitoReturnMatch] = Field(default_factory=list)
    reuseSuggestion: AvitoReturnMatch | None = None


class AvitoOrderRow(BaseModel):
    orderId: str = Field(min_length=1)
    marketplaceId: str | None = None
    jobNumber: str | None = None
    accountId: str | None = None
    accountName: str | None = None
    status: str = "unknown"
    rawStatus: str | None = None
    canonicalStatus: str | None = None
    deliveryType: str | None = None
    deliveryService: str | None = None
    dropoffProvider: str | None = None
    createdAt: str | None = None
    updatedAt: str | None = None
    buyerName: str | None = None
    buyerId: str | None = None
    buyerPhone: str | None = None
    recipientName: str | None = None
    address: str | None = None
    trackNumber: str | None = None
    stickerNumber: str | None = None
    stickerNumberState: str | None = None
    stickerBarcodeType: str | None = None
    stickerLabelId: int | None = None
    stickerDocumentId: int | None = None
    shipmentNumber: str | None = None
    shipmentNumberSource: str | None = None
    shipmentNumberObservedAt: str | None = None
    shipmentNumberFetchedAt: str | None = None
    shipmentNumberState: str | None = None
    shipmentNumberHistory: list[dict[str, str | None]] = Field(default_factory=list)
    statusSource: str | None = None
    statusObservedAt: str | None = None
    statusFetchedAt: str | None = None
    returnStatus: str | None = None
    returnStatusSource: str | None = None
    returnStatusObservedAt: str | None = None
    returnPickupPlace: str | None = None
    returnPickupDeadline: str | None = None
    returnPickupCode: str | None = None
    returnFieldStates: dict[str, str] = Field(default_factory=dict)
    totalKopecks: int | None = Field(default=None, ge=0)
    items: list[AvitoOrderItem] = Field(default_factory=list)
    availableActions: list[AvitoOrderAction] = Field(default_factory=list)
    schedules: list[dict[str, Any]] = Field(default_factory=list)
    sourceStatus: Literal["fresh", "partial"] = "fresh"


class AvitoOrdersBrowserItem(BaseModel):
    itemId: str | None = None
    lineIndex: int | None = Field(default=None, ge=0)
    title: str | None = None
    itemUrl: str | None = None
    quantity: int | None = Field(default=None, ge=0)
    priceKopecks: int | None = Field(default=None, ge=0)
    sellerArticle: str | None = None
    brand: str | None = None
    size: str | None = None
    descriptionSize: str | None = None
    sizeMode: str | None = None
    sizeState: str | None = None
    sizeReason: str | None = None
    sizeEvidence: dict[str, Any] = Field(default_factory=dict)
    chatEvidence: AvitoChatEvidence | None = None
    sources: dict[str, str | None] = Field(default_factory=dict)
    color: str | None = None
    imageUrl: str | None = None
    imageUrls: list[str] = Field(default_factory=list)
    description: str | None = None
    chatText: str | None = None


class AvitoOrdersBrowserOrder(BaseModel):
    orderId: str | None = None
    marketplaceId: str | None = None
    jobNumber: str | None = None
    accountId: str | None = None
    accountName: str | None = None
    status: str | None = None
    statusObservedAt: str | None = None
    deliveryService: str | None = None
    dropoffProvider: str | None = None
    trackNumber: str | None = None
    shipmentNumber: str | None = None
    shipmentNumberState: str | None = None
    returnPickupPlace: str | None = None
    returnPickupDeadline: str | None = None
    returnPickupCode: str | None = None
    returnFieldStates: dict[str, str] = Field(default_factory=dict)
    returnStatus: str | None = None
    buyerName: str | None = None
    buyerId: str | None = None
    recipientName: str | None = None
    pageUrl: str | None = None
    items: list[AvitoOrdersBrowserItem] = Field(default_factory=list)


class AvitoOrdersBrowserSnapshot(BaseModel):
    capturedAt: str | None = None
    pageUrl: str | None = None
    collector: dict[str, Any] | None = None
    aiExtraction: dict[str, Any] | None = None
    orders: list[AvitoOrdersBrowserOrder] = Field(default_factory=list)
    returns: list[AvitoOrdersBrowserOrder] = Field(default_factory=list)


class AvitoOrdersError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    blockerIds: list[str] = Field(default_factory=list)


class AvitoOrdersFetchResult(BaseModel):
    status: Literal["synced", "blocked"]
    orders: list[AvitoOrderRow] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    diagnostics: dict[str, Any] | None = None
    error: AvitoOrdersError | None = None


class AvitoOrdersClient(Protocol):
    def fetch_orders(self, request: AvitoOrdersFetchRequest) -> AvitoOrdersFetchResult:
        ...


def _iso_from_any(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return None
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None


def _date_to_unix_start(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())


def _money_kopecks(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            kopecks = _int_or_none(value.get("kopecks") or value.get("amountKopecks") or value.get("amount_kopecks"))
            if kopecks is not None:
                return kopecks
            rub = _int_or_none(value.get("total") or value.get("rub") or value.get("amount") or value.get("value") or value.get("price"))
            if rub is not None:
                return rub * 100
            continue
        amount = _int_or_none(value)
        if amount is not None:
            return amount * 100
    return None


def _schedule_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _first_dict(raw: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _text_or_none(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _same_identity(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return str(left).strip() == str(right).strip()


def _browser_order_matches(row: AvitoOrderRow, browser_order: AvitoOrdersBrowserOrder) -> bool:
    if row.accountId and browser_order.accountId and not _same_identity(row.accountId, browser_order.accountId):
        return False
    return (
        _same_identity(row.orderId, browser_order.orderId)
        or _same_identity(row.marketplaceId, browser_order.marketplaceId)
    )


def _browser_item_matches(item: AvitoOrderItem, browser_item: AvitoOrdersBrowserItem) -> bool:
    return _same_identity(item.itemId, browser_item.itemId) and (
        item.lineIndex is None or browser_item.lineIndex is None or item.lineIndex == browser_item.lineIndex
    )


def _merge_browser_item(item: AvitoOrderItem, browser_item: AvitoOrdersBrowserItem) -> None:
    if browser_item.sizeMode in {"description", "none"} and item.sizeMode == "chat_ai":
        item.sizeMode = browser_item.sizeMode
        item.size = browser_item.size
        item.sizeState = None
        item.sizeReason = None
        item.sizeEvidence = {}
        item.sources.pop("size", None)
    if browser_item.sizeMode == "chat_ai":
        item.sizeMode = "chat_ai"
        item.sizeState = browser_item.sizeState or "needs_review"
        item.sizeReason = browser_item.sizeReason
        item.sizeEvidence = dict(browser_item.sizeEvidence)
        item.size = browser_item.size if item.sizeState == "confirmed" else None
        item.sources.pop("size", None)
        if item.size:
            item.sources["size"] = "chat_ai"
    fields = ("size", "color", "imageUrl", "sellerArticle", "brand")
    original = {field: getattr(item, field) for field in fields}
    # A later confirmed buyer variant supersedes a listing-only suggestion.
    strong_sources = {"order_row", "order_detail", "chat_ai"}
    for field in ("size", "color"):
        if (getattr(browser_item, field) and browser_item.sources.get(field) in strong_sources
                and item.sources.get(field) in {"description", "description_fallback", "listing", "browser_unspecified"}):
            setattr(item, field, None)
            original[field] = None
    listing_matches = _same_identity(item.itemId, browser_item.itemId)
    sources = dict(browser_item.sources)
    # v0.2 collectors omitted provenance for details-page photos and color.
    # Accept only a matched item, retain the weaker evidence explicitly, and
    # never replace a known ordered variant with these legacy values.
    if listing_matches and browser_item.color and not sources.get('color'):
        sources['color'] = 'browser_unspecified'
    if listing_matches and browser_item.imageUrl and not sources.get('imageUrl'):
        try:
            image = urlsplit(browser_item.imageUrl)
            if image.scheme == 'https' and not image.username and not image.password and image.port in (None, 443) and (image.hostname or '').endswith('.img.avito.st'):
                sources['imageUrl'] = 'browser_unspecified'
        except ValueError:
            pass
    browser_item = browser_item.model_copy(update={'sources': sources})
    if browser_item.itemId and not item.itemId:
        item.itemId = browser_item.itemId
    if browser_item.title and (not item.title or item.title in {"Товар", "Товар Авито"}):
        item.title = browser_item.title
    if browser_item.quantity is not None and not item.quantity:
        item.quantity = max(0, browser_item.quantity)
    if browser_item.priceKopecks is not None and item.priceKopecks is None and browser_item.sources.get("priceKopecks") in {"order_row", "order_detail"}:
        item.priceKopecks = browser_item.priceKopecks
    if browser_item.sellerArticle and not item.sellerArticle and browser_item.sources.get("sellerArticle") in {"order_row", "order_detail"}:
        item.sellerArticle = browser_item.sellerArticle
    if browser_item.brand and not item.brand and browser_item.sources.get("brand") in {"order_row", "order_detail"}:
        item.brand = browser_item.brand
    if browser_item.size and not item.size and (browser_item.sources.get("size") in {"order_row", "order_detail", "chat_ai"} or (listing_matches and browser_item.sources.get("size") in {"description", "description_fallback"})):
        item.size = browser_item.size
    if browser_item.descriptionSize:
        item.descriptionSize = browser_item.descriptionSize
        if not original['size'] and browser_item.sources.get('size') == 'description_fallback':
            item.sources['size'] = 'description_fallback'
    if browser_item.color and not item.color and (browser_item.sources.get("color") in {"order_row", "order_detail"} or (listing_matches and browser_item.sources.get("color") in {"listing", "browser_unspecified"})):
        item.color = browser_item.color
    if browser_item.imageUrl and not item.imageUrl and (browser_item.sources.get("imageUrl") in {"order_row", "order_detail"} or (listing_matches and browser_item.sources.get("imageUrl") in {"listing", "browser_unspecified"})):
        item.imageUrl = browser_item.imageUrl
    for field in fields:
        if not original[field] and getattr(item, field) and browser_item.sources.get(field):
            item.sources[field] = browser_item.sources[field]


def merge_browser_snapshot_orders(rows: list[AvitoOrderRow], snapshot: AvitoOrdersBrowserSnapshot | None) -> None:
    if snapshot is None or (not snapshot.orders and not snapshot.returns):
        return
    browser_orders = [*snapshot.orders, *snapshot.returns]
    for row in rows:
        browser_order = next((candidate for candidate in browser_orders if _browser_order_matches(row, candidate)), None)
        if browser_order is None:
            continue
        if browser_order.accountName and (not row.accountName or row.accountName == "Авито"):
            row.accountName = browser_order.accountName
        if browser_order.deliveryService and not row.deliveryService:
            row.deliveryService = browser_order.deliveryService
        if browser_order.dropoffProvider:
            row.dropoffProvider = browser_order.dropoffProvider
        if browser_order.jobNumber and not row.jobNumber:
            row.jobNumber = browser_order.jobNumber
        if browser_order.returnStatus and snapshot.capturedAt and row.returnStatusSource != "avito_api":
            observed = _parsed_time(snapshot.capturedAt)
            previous = _parsed_time(row.returnStatusObservedAt)
            if observed and (not previous or observed >= previous):
                row.returnStatus = browser_order.returnStatus
                row.returnStatusSource = "browser_return_instruction"
                row.returnStatusObservedAt = snapshot.capturedAt
        for field in ("returnPickupPlace", "returnPickupDeadline", "returnPickupCode"):
            if browser_order.returnFieldStates.get(field) == "ambiguous" and row.returnStatusSource != "avito_api":
                observed = _parsed_time(snapshot.capturedAt)
                previous = _parsed_time(row.returnStatusObservedAt)
                if observed and (not previous or observed >= previous):
                    setattr(row, field, None)
            if getattr(browser_order, field) and not getattr(row, field):
                setattr(row, field, getattr(browser_order, field))
            if not getattr(row, field) and browser_order.returnFieldStates.get(field):
                row.returnFieldStates[field] = browser_order.returnFieldStates[field]
        if browser_order.trackNumber and not row.trackNumber:
            row.trackNumber = browser_order.trackNumber
        if browser_order.shipmentNumber and snapshot.capturedAt and row.shipmentNumberState != "ambiguous":
            observed = _parsed_time(snapshot.capturedAt)
            previous = _parsed_time(row.shipmentNumberObservedAt)
            if observed and (not previous or observed >= previous):
                number = " ".join(browser_order.shipmentNumber.split())
                if row.shipmentNumber and row.shipmentNumber != number:
                    row.shipmentNumberHistory.append({
                        "number": row.shipmentNumber, "source": row.shipmentNumberSource,
                        "observedAt": row.shipmentNumberObservedAt,
                    })
                row.shipmentNumber = number
                row.shipmentNumberSource = "browser_order_instruction"
                row.shipmentNumberObservedAt = snapshot.capturedAt
                row.shipmentNumberFetchedAt = datetime.now(timezone.utc).isoformat()
                row.shipmentNumberState = "confirmed"
        elif browser_order.shipmentNumberState == "ambiguous" and not row.shipmentNumber:
            row.shipmentNumberState = "ambiguous"
        if browser_order.buyerName and not row.buyerName:
            row.buyerName = browser_order.buyerName
        if browser_order.recipientName and not row.recipientName:
            row.recipientName = browser_order.recipientName
        duplicate_ids = {item.itemId for item in row.items if item.itemId and sum(other.itemId == item.itemId for other in row.items) > 1}
        for item in row.items:
            browser_item = next((candidate for candidate in browser_order.items if _browser_item_matches(item, candidate)), None)
            if item.itemId in duplicate_ids and browser_item and browser_item.lineIndex is None:
                browser_item = None
            if browser_item is None and len(row.items) == 1 and len(browser_order.items) == 1 and (not item.itemId or not browser_order.items[0].itemId):
                browser_item = browser_order.items[0]
            if browser_item is not None:
                _merge_browser_item(item, browser_item)


def _parsed_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _param_value(raw: dict[str, Any], *names: str) -> str | None:
    normalized_names = {name.lower() for name in names}
    for key in ("parameters", "params", "properties", "attributes", "options"):
        value = raw.get(key)
        if isinstance(value, dict):
            for name in names:
                found = _text_or_none(value.get(name), value.get(name.lower()), value.get(name.upper()))
                if found:
                    return found
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            label = _text_or_none(item.get("name"), item.get("title"), item.get("label"), item.get("code"), item.get("key"))
            if not label or label.lower() not in normalized_names:
                continue
            found = _text_or_none(item.get("value"), item.get("text"), item.get("displayValue"), item.get("display_value"))
            if found:
                return found
    return None


class LiveAvitoOrdersClient:
    def __init__(self, access_token: str, base_url: str = "https://api.avito.ru", timeout_seconds: float = 20.0) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for LiveAvitoOrdersClient")
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def fetch_orders(self, request: AvitoOrdersFetchRequest, http_client: Any | None = None) -> AvitoOrdersFetchResult:
        try:
            if http_client is not None:
                return self._fetch_orders_with_client(request, http_client)
            with httpx.Client(timeout=self.timeout_seconds) as client:
                return self._fetch_orders_with_client(request, client)
        except httpx.HTTPStatusError as exc:
            return AvitoOrdersFetchResult(status="blocked", error=self._http_error(exc))
        except Exception:
            pass
        return AvitoOrdersFetchResult(
            status="blocked",
            error=AvitoOrdersError(code="transport_error", message="Avito transport error", retryable=True, blockerIds=["AVITO_ORDERS"]),
        )

    def _fetch_orders_with_client(self, request: AvitoOrdersFetchRequest, client: Any) -> AvitoOrdersFetchResult:
        params: dict[str, Any] = {"limit": request.limit, "page": request.page}
        if request.dateFrom is not None:
            params["dateFrom"] = _date_to_unix_start(request.dateFrom)
        if request.statuses:
            params["statuses"] = ",".join(request.statuses)
        if request.ids:
            params["ids"] = ",".join(request.ids)
        url = f"{self.base_url}/order-management/1/orders"
        try:
            response = client.get(url, params=params, headers=self._headers())
        except (httpx.ConnectError, httpx.ConnectTimeout):
            response = client.get(url, params=params, headers=self._headers())
        status_code = int(getattr(response, "status_code", 200) or 200)
        payload = response.json()
        diagnostics = self._avito_orders_diagnostics(status_code, payload, request_url=url, request_params=params)
        self._log_avito_orders_response(diagnostics)
        if status_code >= 400:
            return AvitoOrdersFetchResult(
                status="blocked",
                error=self._http_error_from_status(status_code),
                diagnostics=diagnostics,
            )
        response.raise_for_status()
        rows = self._orders_rows(payload)
        orders = [self._order_row(row) for row in rows]
        return AvitoOrdersFetchResult(
            status="synced",
            orders=orders,
            total=self._total(payload, len(orders)),
            diagnostics={**diagnostics, "rawCount": len(rows) if len(rows) <= 2**31 - 1 else None},
        )

    @staticmethod
    def _avito_orders_diagnostics(status_code: int, payload: Any, *, request_url: str, request_params: dict[str, Any]) -> dict[str, Any]:
        rows = LiveAvitoOrdersClient._orders_rows(payload)
        safe_status = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        # URL/params remain accepted for call compatibility, never copied into
        # diagnostics. Provider keys and first-row fields can contain identifiers.
        return {
            "status": safe_status,
            "endpoint": "GET /order-management/1/orders",
            "ordersCount": len(rows) if len(rows) <= 2**31 - 1 else None,
            "reason": "avito_http_error" if safe_status is not None and safe_status >= 400 else None,
        }

    @staticmethod
    def _log_avito_orders_response(diagnostics: dict[str, Any]) -> None:
        values = diagnostics if type(diagnostics) is dict else {}
        status, count = values.get("status"), values.get("ordersCount")
        safe_status = status if type(status) is int and 100 <= status <= 599 else None
        safe_count = count if type(count) is int and 0 <= count <= 2**31 - 1 else None
        reason = values.get("reason")
        safe_reason = "avito_http_error" if type(reason) is str and reason == "avito_http_error" else None
        logger.warning("[AVITO_ORDERS_STATUS] %s", safe_status)
        logger.warning("[AVITO_ORDERS_ENDPOINT] GET /order-management/1/orders")
        logger.warning("[AVITO_ORDERS_COUNT] %s", safe_count)
        logger.warning("[AVITO_ORDERS_REASON] %s", safe_reason)

    @staticmethod
    def _orders_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("orders", "resources", "items", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        result = payload.get("result")
        if isinstance(result, dict):
            for key in ("orders", "resources", "items", "data"):
                rows = result.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
        return []

    @staticmethod
    def _total(payload: Any, fallback: int) -> int:
        if not isinstance(payload, dict):
            return 0
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        return _int_or_none(payload.get("total") or payload.get("totalCount") or result.get("total") or result.get("totalCount")) or 0

    def _order_row(self, raw: dict[str, Any]) -> AvitoOrderRow:
        delivery = _first_dict(raw, "delivery", "shipment", "logistics")
        shipments = raw.get("shipments") or delivery.get("shipments")
        buyer = _first_dict(raw, "buyer", "customer")
        recipient = _first_dict(raw, "recipient", "receiver")
        return_policy = raw.get("returnPolicy") if isinstance(raw.get("returnPolicy"), dict) else {}
        items = self._items(raw)
        order_id = str(raw.get("id") or raw.get("orderId") or raw.get("order_id") or raw.get("uuid") or "")
        seller = _first_dict(raw, "seller", "shop", "store", "merchant")
        raw_status = str(raw.get("status") or "unknown")
        mapped_status = map_avito_status(raw_status)
        fetched_at = datetime.now(timezone.utc).isoformat()
        observed_at = _iso_from_any(raw.get("updatedAt") or raw.get("updated_at") or raw.get("updated")) or fetched_at
        return AvitoOrderRow(
            orderId=order_id,
            marketplaceId=str(raw.get("marketplaceId") or raw.get("marketplace_id") or raw.get("number") or "") or None,
            jobNumber=_text_or_none(raw.get("jobNumber"), raw.get("job_number")),
            accountId=str(raw.get("accountId") or raw.get("userId") or raw.get("sellerId") or "") or None,
            accountName=_text_or_none(raw.get("accountName"), raw.get("sellerName"), raw.get("shopName"), raw.get("storeName"), seller.get("name"), seller.get("title")),
            status=raw_status if mapped_status.mapping_state is MappingState.MAPPED else "unknown",
            rawStatus=raw_status,
            canonicalStatus=mapped_status.canonical_status.value if mapped_status.canonical_status else None,
            deliveryType=str(delivery.get("type") or delivery.get("serviceType") or raw.get("deliveryType") or raw.get("delivery_type") or "") or None,
            deliveryService=str(delivery.get("service") or delivery.get("serviceName") or raw.get("deliveryService") or "") or None,
            createdAt=_iso_from_any(raw.get("createdAt") or raw.get("created_at") or raw.get("created")),
            updatedAt=_iso_from_any(raw.get("updatedAt") or raw.get("updated_at") or raw.get("updated")),
            buyerName=str(buyer.get("name") or raw.get("buyerName") or "") or None,
            buyerId=str(buyer.get("id") or raw.get("buyerId") or "") or None,
            buyerPhone=str(buyer.get("phone") or raw.get("buyerPhone") or "") or None,
            recipientName=str(recipient.get("name") or raw.get("recipientName") or "") or None,
            address=self._address(raw, delivery),
            trackNumber=str(delivery.get("trackNumber") or delivery.get("trackingNumber") or raw.get("trackNumber") or raw.get("trackingNumber") or "") or None,
            stickerNumber=_text_or_none(delivery.get("stickerNumber"), raw.get("stickerNumber")),
            shipmentNumberState="ambiguous" if isinstance(shipments, list) and len(shipments) > 1 else None,
            statusSource="avito_api",
            statusObservedAt=observed_at,
            statusFetchedAt=fetched_at,
            returnStatus=str(return_policy.get("returnStatus") or return_policy.get("status") or raw.get("returnStatus") or "") or None,
            returnStatusSource="avito_api" if return_policy.get("returnStatus") or return_policy.get("status") or raw.get("returnStatus") else None,
            returnStatusObservedAt=observed_at,
            returnPickupPlace=_text_or_none(return_policy.get("pickupPlace"), return_policy.get("pickupAddress")),
            returnPickupDeadline=_iso_from_any(return_policy.get("pickupDeadline") or return_policy.get("pickupUntil")),
            returnPickupCode=_text_or_none(return_policy.get("pickupCode"), return_policy.get("returnCode")),
            totalKopecks=_money_kopecks(raw.get("prices"), raw.get("total"), raw.get("totalPrice"), raw.get("amount"), raw.get("price")),
            items=items,
            availableActions=self._actions(raw),
            schedules=_schedule_rows(raw.get("schedules")),
            sourceStatus="fresh" if order_id else "partial",
        )

    @staticmethod
    def _address(raw: dict[str, Any], delivery: dict[str, Any]) -> str | None:
        address = delivery.get("address") or raw.get("address")
        if isinstance(address, str):
            return address or None
        if isinstance(address, dict):
            parts = [address.get(key) for key in ("full", "fullAddress", "city", "street", "house")]
            return ", ".join(str(part) for part in parts if part) or None
        return None

    def _items(self, raw: dict[str, Any]) -> list[AvitoOrderItem]:
        rows = raw.get("items") or raw.get("products") or raw.get("goods")
        if not isinstance(rows, list):
            item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
            rows = [item] if item else []
        result: list[AvitoOrderItem] = []
        for line_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            seller = _first_dict(row, "seller", "shop", "store", "merchant")
            quantity = _int_or_none(row.get("quantity") if "quantity" in row else row.get("count"))
            result.append(
                AvitoOrderItem(
                    itemId=str(row.get("id") or row.get("itemId") or row.get("avitoId") or "") or None,
                    lineIndex=line_index,
                    title=str(row.get("title") or row.get("name") or "Товар Авито"),
                    quantity=1 if quantity is None else quantity,
                    priceKopecks=_money_kopecks(row.get("prices"), row.get("price"), row.get("total"), row.get("amount")),
                    sellerArticle=_text_or_none(
                        row.get("sellerArticle"),
                        row.get("seller_article"),
                        row.get("sellerSku"),
                        row.get("seller_sku"),
                        row.get("vendorCode"),
                        row.get("vendor_code"),
                        row.get("articleNumber"),
                        row.get("article_number"),
                        row.get("article"),
                        row.get("sku"),
                        seller.get("article"),
                        seller.get("sellerArticle"),
                        _param_value(row, "Артикул продавца", "sellerArticle", "seller_sku", "vendorCode"),
                    ),
                    brand=_text_or_none(row.get("brand"), _param_value(row, "Бренд", "brand")),
                    size=_text_or_none(row.get("size"), _param_value(row, "Размер", "size")),
                    color=_text_or_none(row.get("color"), _param_value(row, "Цвет", "color")),
                    imageUrl=self._image(row),
                    barcode=_text_or_none(row.get("barcode"), row.get("barCode")),
                )
            )
        return result

    @staticmethod
    def _image(raw: dict[str, Any]) -> str | None:
        image = raw.get("image") or raw.get("photo")
        if isinstance(image, str):
            return image or None
        if isinstance(image, dict):
            return str(image.get("url") or image.get("preview") or "") or None
        images = raw.get("images") if isinstance(raw.get("images"), list) else []
        for item in images:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
        return None

    @staticmethod
    def _actions(raw: dict[str, Any]) -> list[AvitoOrderAction]:
        actions = raw.get("availableActions")
        if not isinstance(actions, list):
            return []
        result: list[AvitoOrderAction] = []
        for action in actions:
            if isinstance(action, str) and action:
                result.append(AvitoOrderAction(name=action, required=False))
            elif isinstance(action, dict) and action.get("name"):
                result.append(AvitoOrderAction(name=str(action["name"]), required=bool(action.get("required"))))
        return result

    @staticmethod
    def _http_error(exc: Any) -> AvitoOrdersError:
        return LiveAvitoOrdersClient._http_error_from_status(exc.response.status_code)

    @staticmethod
    def _http_error_from_status(status_code: int) -> AvitoOrdersError:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            return AvitoOrdersError(code="avito_request_failed", message="Avito request failed", retryable=True, blockerIds=["AVITO_ORDERS"])
        if status_code == 401:
            code, retryable, blocker = "auth_required", False, "AVITO_AUTH"
        elif status_code == 403:
            code, retryable, blocker = "forbidden_scope", False, "AVITO_ORDER_MANAGEMENT_SCOPE"
        elif status_code == 429:
            code, retryable, blocker = "rate_limited", True, "AVITO_RATE_LIMIT"
        elif status_code >= 500:
            code, retryable, blocker = "avito_server_error", True, "AVITO_ORDERS"
        else:
            code, retryable, blocker = "avito_request_failed", False, "AVITO_ORDERS"
        return AvitoOrdersError(code=code, message=f"Avito HTTP {status_code}", retryable=retryable, blockerIds=[blocker])


def build_avito_orders_client(
    access_token: str,
    base_url: str = "https://api.avito.ru",
    timeout_seconds: float = 20.0,
) -> AvitoOrdersClient:
    return LiveAvitoOrdersClient(access_token=access_token, base_url=base_url, timeout_seconds=timeout_seconds)
