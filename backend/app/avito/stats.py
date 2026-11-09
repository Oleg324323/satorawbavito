from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

try:
    import httpx
except Exception:  # pragma: no cover - live mode dependency
    httpx = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


def _diagnostic_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _diagnostic_status(value: Any) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


def _text_or_none(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
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
            label = _text_or_none(
                item.get("name"),
                item.get("title"),
                item.get("label"),
                item.get("code"),
                item.get("key"),
                item.get("attributeName"),
                item.get("attribute_name"),
                item.get("paramName"),
                item.get("param_name"),
            )
            if not label or label.lower() not in normalized_names:
                continue
            found = _text_or_none(
                item.get("value"),
                item.get("text"),
                item.get("displayValue"),
                item.get("display_value"),
                item.get("attributeValue"),
                item.get("attribute_value"),
                item.get("paramValue"),
                item.get("param_value"),
            )
            if found:
                return found
    return None


AVITO_ITEM_ANALYTICS_METRICS = [
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
AVITO_STATS_MAX_WINDOW_DAYS = 30
AVITO_STATS_PAGE_LIMIT = 1000
AVITO_ITEM_LIST_STATUSES = "active,removed,old"

AvitoStatsGrouping = Literal["item", "totals", "day"]
AvitoStatsSourceStatus = Literal["fresh", "partial", "stale", "blocked"]
AvitoStatsFetchStatus = Literal["synced", "partial", "blocked"]


class AvitoStatsFetchRequest(BaseModel):
    dateFrom: date
    dateTo: date
    accountIds: list[str] = Field(default_factory=list)
    grouping: AvitoStatsGrouping = "item"


class AvitoStatsAccount(BaseModel):
    accountId: str = Field(min_length=1)
    accountName: str = Field(min_length=1)
    itemCount: int = Field(default=0, ge=0)
    activeItemCount: int = Field(default=0, ge=0)
    inactiveItemCount: int = Field(default=0, ge=0)


class AvitoStatsItem(BaseModel):
    itemId: str = Field(min_length=1)
    title: str = Field(min_length=1)
    accountId: str = Field(min_length=1)
    accountName: str = Field(min_length=1)
    category: str | None = None
    url: str | None = None
    imageUrl: str | None = None
    impressions: int | None = Field(default=None, ge=0)
    views: int | None = Field(default=None, ge=0)
    contactsMessenger: int | None = Field(default=None, ge=0)
    contacts: int | None = Field(default=None, ge=0)
    contactsShowPhone: int | None = Field(default=None, ge=0)
    contactsShowPhoneAndMessenger: int | None = Field(default=None, ge=0)
    favorites: int | None = Field(default=None, ge=0)
    spendKopecks: int | None = Field(default=None, ge=0)
    orders: int | None = Field(default=None, ge=0)
    buyouts: int | None = Field(default=None, ge=0)
    sourceStatus: AvitoStatsSourceStatus = "fresh"
    updatedAt: str | None = None


class AvitoStatsDailyPoint(BaseModel):
    date: date
    accountId: str = Field(min_length=1)
    accountName: str = Field(min_length=1)
    impressions: int | None = Field(default=None, ge=0)
    views: int | None = Field(default=None, ge=0)
    contactsMessenger: int | None = Field(default=None, ge=0)
    contacts: int | None = Field(default=None, ge=0)
    contactsShowPhone: int | None = Field(default=None, ge=0)
    contactsShowPhoneAndMessenger: int | None = Field(default=None, ge=0)
    favorites: int | None = Field(default=None, ge=0)
    spendKopecks: int | None = Field(default=None, ge=0)
    orders: int | None = Field(default=None, ge=0)
    buyouts: int | None = Field(default=None, ge=0)


class AvitoStatsFetchError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    blockerIds: list[str] = Field(default_factory=list)


class AvitoStatsFetchResult(BaseModel):
    status: AvitoStatsFetchStatus
    accounts: list[AvitoStatsAccount] = Field(default_factory=list)
    items: list[AvitoStatsItem] = Field(default_factory=list)
    daily: list[AvitoStatsDailyPoint] = Field(default_factory=list)
    error: AvitoStatsFetchError | None = None
    diagnostics: dict[str, Any] | None = None


class AvitoStatsClient(Protocol):
    def fetch_stats(self, request: AvitoStatsFetchRequest) -> AvitoStatsFetchResult:
        ...


class FakeAvitoStatsClient:
    """Local Avito statistics fixture. It never calls external Avito APIs."""

    def fetch_stats(self, request: AvitoStatsFetchRequest) -> AvitoStatsFetchResult:
        accounts = [
            AvitoStatsAccount(accountId="365024549", accountName="Bless T", itemCount=3),
            AvitoStatsAccount(accountId="133273390", accountName="Anomie studio", itemCount=1),
        ]
        allowed = set(request.accountIds)
        if allowed:
            accounts = [account for account in accounts if account.accountId in allowed]

        rows = [
            AvitoStatsItem(
                itemId="8098482225",
                title="Худи yohji yamamoto pour homme",
                accountId="365024549",
                accountName="Bless T",
                category="Одежда, обувь, аксессуары",
                url="https://www.avito.ru/item/8098482225",
                impressions=42_100,
                views=7_850,
                contactsMessenger=64,
                contacts=88,
                contactsShowPhone=21,
                contactsShowPhoneAndMessenger=9,
                favorites=640,
                spendKopecks=1_651_900,
                orders=15,
                buyouts=11,
                updatedAt="2026-07-28T06:55:00+00:00",
            ),
            AvitoStatsItem(
                itemId="8098482191",
                title="Худи alpha industries",
                accountId="365024549",
                accountName="Bless T",
                category="Одежда, обувь, аксессуары",
                url="https://www.avito.ru/item/8098482191",
                impressions=36_400,
                views=6_264,
                contactsMessenger=18,
                contacts=31,
                contactsShowPhone=11,
                contactsShowPhoneAndMessenger=4,
                favorites=510,
                spendKopecks=1_830_200,
                orders=3,
                buyouts=2,
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
                contactsMessenger=0,
                contacts=0,
                contactsShowPhone=0,
                contactsShowPhoneAndMessenger=0,
                favorites=13,
                sourceStatus="stale",
                updatedAt="2026-07-28T05:30:00+00:00",
            ),
            AvitoStatsItem(
                itemId="8066023067",
                title="Футболка Jaded London Mad Doll",
                accountId="133273390",
                accountName="Anomie studio",
                category="Одежда, обувь, аксессуары",
                url="https://www.avito.ru/item/8066023067",
                impressions=180,
                views=75,
                contactsMessenger=1,
                contacts=1,
                contactsShowPhone=0,
                contactsShowPhoneAndMessenger=0,
                favorites=11,
                sourceStatus="partial",
                updatedAt="2026-07-28T06:52:00+00:00",
            ),
        ]
        if allowed:
            rows = [row for row in rows if row.accountId in allowed]
        return AvitoStatsFetchResult(status="synced", accounts=accounts, items=rows)


class LiveAvitoStatsClient:
    _PUBLIC_IMAGE_FETCH_LIMIT = 80

    """HTTP Avito statistics adapter for read-only item stats."""

    def __init__(self, access_token: str, base_url: str = "https://api.avito.ru", timeout_seconds: float = 20.0) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for LiveAvitoStatsClient")
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.diagnostics: dict[str, Any] | None = None

    def fetch_stats(self, request: AvitoStatsFetchRequest) -> AvitoStatsFetchResult:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                accounts = self._accounts(client, request.accountIds)
                daily: list[AvitoStatsDailyPoint] = []
                if request.grouping in {"totals", "day"}:
                    stat_items, daily = self._stats_totals(client, request, accounts)
                else:
                    items = self._items(client, accounts)
                    stat_items = self._stats_for_items(client, request, accounts, items)
                    if self.diagnostics is not None:
                        self.diagnostics["itemsCount"] = len(items)
                        self.diagnostics["itemIdsSample"] = None
        except httpx.HTTPStatusError as exc:
            return self._http_error(exc)
        except Exception:
            return AvitoStatsFetchResult(
                status="blocked",
                error=AvitoStatsFetchError(code="transport_error", message="Avito transport error", retryable=True, blockerIds=["AVITO_STATS"]),
            )
        return AvitoStatsFetchResult(status="synced", accounts=accounts, items=stat_items, daily=daily, diagnostics=self.diagnostics)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json", "Content-Type": "application/json"}

    def _accounts(self, client: Any, account_ids: list[str]) -> list[AvitoStatsAccount]:
        if account_ids:
            return [AvitoStatsAccount(accountId=str(account_id), accountName=str(account_id)) for account_id in account_ids]
        response = client.get(f"{self.base_url}/core/v1/accounts/self", headers=self._headers())
        response.raise_for_status()
        data = response.json()
        account_id = str(data.get("id") or data.get("user_id") or data.get("userId") or "")
        account_name = str(data.get("name") or data.get("profile", {}).get("name") or account_id)
        if not account_id:
            raise ValueError("Avito account response does not include account id")
        return [AvitoStatsAccount(accountId=account_id, accountName=account_name)]

    def _items(self, client: Any, accounts: list[AvitoStatsAccount]) -> list[dict[str, Any]]:
        default_account = accounts[0] if accounts else None
        result: list[dict[str, Any]] = []
        per_page = 99
        page = 1
        public_image_fetches = 0
        seen_pages: set[tuple[str, ...]] = set()
        seen_items: set[tuple[str, str]] = set()
        while True:
            response = client.get(f"{self.base_url}/core/v1/items", params={"per_page": per_page, "page": page, "status": AVITO_ITEM_LIST_STATUSES}, headers=self._headers())
            response.raise_for_status()
            payload = response.json()
            rows = next((payload[key] for key in ("resources", "items", "data") if key in payload), None) if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ValueError("Avito inventory response does not contain a listing array")
            signature = tuple(str(row.get("id") or row.get("itemId") or row.get("item_id") or "") for row in rows if isinstance(row, dict))
            if signature and signature in seen_pages:
                raise ValueError("Avito inventory repeated a page; catalog is incomplete")
            seen_pages.add(signature)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item_id = row.get("id") or row.get("itemId") or row.get("item_id")
                if not item_id:
                    continue
                image_url = self._image_url(row)
                item_url = row.get("url")
                if not image_url and isinstance(item_url, str) and public_image_fetches < self._PUBLIC_IMAGE_FETCH_LIMIT:
                    public_image_fetches += 1
                    image_url = self._public_image_url(client, item_url)
                item = self._listing_item_payload(row, default_account)
                item["imageUrl"] = image_url
                key = (str(item.get("accountId") or ""), str(item["itemId"]))
                if key not in seen_items:
                    result.append(item)
                    seen_items.add(key)
            if len(rows) < per_page:
                break
            page += 1
        return result

    @staticmethod
    def _listing_item_payload(row: dict[str, Any], default_account: AvitoStatsAccount | None) -> dict[str, Any]:
        item_id = row.get("id") or row.get("itemId") or row.get("item_id")
        category = row.get("category", {}).get("name") if isinstance(row.get("category"), dict) else row.get("category")
        seller = row.get("seller") if isinstance(row.get("seller"), dict) else {}
        return {
            "itemId": str(item_id or ""),
            "title": str(row.get("title") or row.get("name") or item_id or ""),
            "accountId": str(row.get("user_id") or row.get("userId") or row.get("accountId") or (default_account.accountId if default_account else "")),
            "accountName": _text_or_none(row.get("accountName"), row.get("shopName"), row.get("storeName"), seller.get("name"), default_account.accountName if default_account else None) or "",
            "category": category,
            "url": row.get("url"),
            "imageUrl": LiveAvitoStatsClient._image_url(row),
            "status": str(row.get("status") or "active"),
            "price": row.get("price") or row.get("priceRub") or row.get("price_rub"),
            "sellerArticle": _text_or_none(
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
                _param_value(row, "Артикул продавца", "sellerArticle", "seller_sku", "vendorCode", "sku"),
            ),
            "size": _text_or_none(
                row.get("size"),
                row.get("productSize"),
                row.get("product_size"),
                row.get("apparelSize"),
                row.get("apparel_size"),
                _param_value(row, "Размер", "size", "apparelSize", "apparel_size", "clothes_size", "Размер одежды"),
            ),
            "color": _text_or_none(
                row.get("color"),
                row.get("colour"),
                row.get("itemColor"),
                row.get("item_color"),
                _param_value(row, "Цвет", "color", "colour", "itemColor", "item_color", "Цвет товара"),
            ),
        }

    def _public_image_url(self, client: Any, item_url: str) -> str | None:
        if not item_url.startswith(("http://", "https://")):
            return None
        try:
            response = client.get(item_url, headers={"Accept": "text/html,application/xhtml+xml"})
            response.raise_for_status()
        except Exception:
            return None
        text = getattr(response, "text", "")
        if not isinstance(text, str) or not text:
            return None
        for pattern in (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']',
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                url = html.unescape(match.group(1).strip())
                if url.startswith(("http://", "https://")):
                    return url
        return None

    def _stats_for_items(
        self,
        client: Any,
        request: AvitoStatsFetchRequest,
        accounts: list[AvitoStatsAccount],
        items: list[dict[str, Any]],
    ) -> list[AvitoStatsItem]:
        by_account = {account.accountId: account for account in accounts}
        result: list[AvitoStatsItem] = []
        for account in accounts:
            account_items = [item for item in items if item["accountId"] == account.accountId]
            account.itemCount = len(account_items)
            account.activeItemCount = sum(1 for item in account_items if str(item.get("status") or "active") == "active")
            account.inactiveItemCount = max(0, account.itemCount - account.activeItemCount)
            stats_by_item = self._v2_item_analytics_for_account(client, request, account.accountId)
            for item in account_items:
                stats = stats_by_item.get(item["itemId"], {})
                account_name = by_account.get(item["accountId"], account).accountName
                result.append(
                    AvitoStatsItem(
                        itemId=item["itemId"],
                        title=item["title"],
                        accountId=item["accountId"],
                        accountName=account_name,
                        category=item.get("category"),
                        url=item.get("url"),
                        imageUrl=item.get("imageUrl"),
                        impressions=self._optional_int_metric(stats, "impressions", "shows"),
                        views=self._optional_int_metric(stats, "views", "uniqViews", "uniq_views"),
                        contactsMessenger=self._optional_int_metric(stats, "contactsMessenger", "messengerContacts", "contacts_messenger"),
                        contacts=self._optional_int_metric(stats, "contacts", "uniqContacts", "uniq_contacts"),
                        contactsShowPhone=self._optional_int_metric(stats, "contactsShowPhone", "contacts_show_phone"),
                        contactsShowPhoneAndMessenger=self._optional_int_metric(stats, "contactsShowPhoneAndMessenger", "contacts_show_phone_and_messenger"),
                        favorites=self._optional_int_metric(stats, "favorites", "uniqFavorites", "uniq_favorites"),
                        spendKopecks=self._optional_int_metric(stats, "allSpending", "spending", "presenceSpending"),
                        orders=self._optional_int_metric(stats, "orderedItems"),
                        buyouts=self._optional_int_metric(stats, "deliveredItems"),
                        sourceStatus="fresh" if stats.get("_source") == "v2_item_analytics" else "partial",
                        updatedAt=None,
                    )
                )
        return result

    def _stats_totals(
        self,
        client: Any,
        request: AvitoStatsFetchRequest,
        accounts: list[AvitoStatsAccount],
    ) -> tuple[list[AvitoStatsItem], list[AvitoStatsDailyPoint]]:
        result: list[AvitoStatsItem] = []
        daily: list[AvitoStatsDailyPoint] = []
        diagnostics_pages: list[dict[str, Any]] = []
        for account in accounts:
            stats = self._v2_totals_analytics_for_account(client, request, account.accountId, diagnostics_pages)
            daily.extend(self._daily_points_from_totals(stats, account))
            result.append(
                AvitoStatsItem(
                    itemId=f"account:{account.accountId}:totals",
                    title=account.accountName,
                    accountId=account.accountId,
                    accountName=account.accountName,
                    impressions=self._optional_int_metric(stats, "impressions", "shows"),
                    views=self._optional_int_metric(stats, "views", "uniqViews", "uniq_views"),
                    contactsMessenger=self._optional_int_metric(stats, "contactsMessenger", "messengerContacts", "contacts_messenger"),
                    contacts=self._optional_int_metric(stats, "contacts", "uniqContacts", "uniq_contacts"),
                    contactsShowPhone=self._optional_int_metric(stats, "contactsShowPhone", "contacts_show_phone"),
                    contactsShowPhoneAndMessenger=self._optional_int_metric(stats, "contactsShowPhoneAndMessenger", "contacts_show_phone_and_messenger"),
                    favorites=self._optional_int_metric(stats, "favorites", "uniqFavorites", "uniq_favorites"),
                    spendKopecks=self._optional_int_metric(stats, "allSpending", "spending", "presenceSpending"),
                    orders=self._optional_int_metric(stats, "orderedItems"),
                    buyouts=self._optional_int_metric(stats, "deliveredItems"),
                    sourceStatus="fresh" if stats.get("_source") == "v2_totals_analytics" else "partial",
                    updatedAt=None,
                )
            )
        if diagnostics_pages:
            self.diagnostics = {
                **diagnostics_pages[-1],
                "pages": len(diagnostics_pages),
                "chunks": [
                    {
                        "dateFrom": None,
                        "dateTo": None,
                        "grouping": "totals",
                        "groupings": page.get("groupings"),
                        "dataTotalCount": page.get("dataTotalCount"),
                    }
                    for page in diagnostics_pages
                ],
            }
        return result, daily

    @staticmethod
    def _image_url(raw: dict[str, Any]) -> str | None:
        def first_url(value: Any, depth: int = 0) -> str | None:
            if depth > 4:
                return None
            if isinstance(value, str):
                stripped = value.strip()
                return stripped if stripped.startswith(("http://", "https://")) else None
            if isinstance(value, list):
                for item in value:
                    found = first_url(item, depth + 1)
                    if found:
                        return found
                return None
            if isinstance(value, dict):
                for key in ("url", "preview", "src", "href", "large", "medium", "small", "default"):
                    found = first_url(value.get(key), depth + 1)
                    if found:
                        return found
                sizes = value.get("sizes")
                if isinstance(sizes, dict):
                    for key in ("1280x960", "1024x768", "640x480", "320x240", "208x156", "140x105"):
                        found = first_url(sizes.get(key), depth + 1)
                        if found:
                            return found
                    found = first_url(list(sizes.values()), depth + 1)
                    if found:
                        return found
                for key in ("image", "photo", "photos", "images", "pictures", "gallery"):
                    found = first_url(value.get(key), depth + 1)
                    if found:
                        return found
            return None

        for key in ("imageUrl", "image_url", "image", "photo", "photos", "images", "pictures", "gallery"):
            found = first_url(raw.get(key))
            if found:
                return found
        return None

    @staticmethod
    def _date_chunks(start: date, end: date, max_days: int = AVITO_STATS_MAX_WINDOW_DAYS) -> list[tuple[date, date]]:
        chunks: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=max_days - 1))
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)
        return chunks

    def _v2_item_analytics_for_account(self, client: Any, request: AvitoStatsFetchRequest, account_id: str) -> dict[str, dict[str, Any]]:
        url = f"{self.base_url}/stats/v2/accounts/{account_id}/items"
        result: dict[str, dict[str, Any]] = {}
        diagnostics_pages: list[dict[str, Any]] = []
        for chunk_start, chunk_end in self._date_chunks(request.dateFrom, request.dateTo):
            offset = 0
            while True:
                body = {
                    "dateFrom": chunk_start.isoformat(),
                    "dateTo": chunk_end.isoformat(),
                    "grouping": "item",
                    "metrics": AVITO_ITEM_ANALYTICS_METRICS,
                    "limit": AVITO_STATS_PAGE_LIMIT,
                    "offset": offset,
                }
                response = client.post(url, json=body, headers=self._headers())
                response.raise_for_status()
                payload = response.json()
                diagnostics = self._avito_stats_diagnostics(response.status_code, payload, request_url=url, request_body=body)
                diagnostics_pages.append(diagnostics)
                self.diagnostics = diagnostics
                self._log_avito_stats_response(diagnostics)
                page_items = self._v2_item_analytics_by_item(payload)
                self._merge_v2_item_metrics(result, page_items)
                # Pagination is business control, not sanitized debug metadata.
                # Retain its existing source interpretation independently.
                pagination_result = payload.get("result") if isinstance(payload, dict) else None
                pagination_groups = pagination_result.get("groupings") if isinstance(pagination_result, dict) else None
                groupings_count = len(pagination_groups) if isinstance(pagination_groups, list) else 0
                total_count_raw = pagination_result.get("dataTotalCount") if isinstance(pagination_result, dict) else None
                total_count = int(total_count_raw) if isinstance(total_count_raw, int) else None
                if groupings_count <= 0:
                    break
                offset += AVITO_STATS_PAGE_LIMIT
                if total_count is not None:
                    if offset >= total_count:
                        break
                elif groupings_count < AVITO_STATS_PAGE_LIMIT:
                    break
        if diagnostics_pages:
            self.diagnostics = {
                **diagnostics_pages[-1],
                "pages": len(diagnostics_pages),
                "chunks": [
                    {
                        "dateFrom": None,
                        "dateTo": None,
                        "offset": None,
                        "groupings": page.get("groupings"),
                        "dataTotalCount": page.get("dataTotalCount"),
                    }
                    for page in diagnostics_pages
                ],
            }
        return result

    def _v2_totals_analytics_for_account(
        self,
        client: Any,
        request: AvitoStatsFetchRequest,
        account_id: str,
        diagnostics_pages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        url = f"{self.base_url}/stats/v2/accounts/{account_id}/items"
        result: dict[str, Any] = {"_source": "v2_totals_analytics"}
        body = {
            "dateFrom": request.dateFrom.isoformat(),
            "dateTo": request.dateTo.isoformat(),
            "grouping": request.grouping,
            "metrics": AVITO_ITEM_ANALYTICS_METRICS,
            "limit": 1000 if request.grouping == "day" else 1,
            "offset": 0,
        }
        response = client.post(url, json=body, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        diagnostics = self._avito_stats_diagnostics(response.status_code, payload, request_url=url, request_body=body)
        diagnostics_pages.append(diagnostics)
        self.diagnostics = diagnostics
        self._log_avito_stats_response(diagnostics)
        self._merge_v2_total_metrics(result, self._v2_totals_analytics_metrics(payload))
        return result

    @staticmethod
    def _merge_v2_item_metrics(target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]) -> None:
        for item_id, metrics in source.items():
            current = target.setdefault(item_id, {"_source": "v2_item_analytics"})
            for key, value in metrics.items():
                if key == "_source":
                    current["_source"] = value
                    continue
                current[key] = int(current.get(key) or 0) + int(value or 0)

    @staticmethod
    def _merge_v2_total_metrics(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if key == "_source":
                continue
            if key == "_daily":
                current = target.setdefault("_daily", [])
                if isinstance(current, list) and isinstance(value, list):
                    current.extend(value)
                continue
            target[key] = int(target.get(key) or 0) + int(value or 0)

    def _v2_totals_analytics_metrics(self, payload: Any) -> dict[str, Any]:
        result_payload = payload.get("result") if isinstance(payload, dict) else None
        groupings = result_payload.get("groupings") if isinstance(result_payload, dict) else None
        metrics: dict[str, Any] = {"_source": "v2_totals_analytics"}
        if isinstance(groupings, list):
            for group in groupings:
                if not isinstance(group, dict):
                    continue
                group_type = str(group.get("type") or "").lower()
                raw_metrics = group.get("metrics")
                group_metrics: dict[str, Any] = {}
                for metric in raw_metrics if isinstance(raw_metrics, list) else []:
                    if isinstance(metric, dict) and metric.get("slug"):
                        slug = str(metric["slug"])
                        value = self._int_metric(metric, "value")
                        group_metrics[slug] = value
                        metrics[slug] = int(metrics.get(slug) or 0) + value
                if group_type in {"date", "dates", "day", "days"} and group_metrics:
                    group_date = self._v2_totals_group_date(group)
                    if group_date is not None:
                        daily = metrics.setdefault("_daily", [])
                        if isinstance(daily, list):
                            daily.append({"date": group_date, **group_metrics})
                if group_type in {"totals", "total"} and len(metrics) > 1:
                    return metrics
        raw_metrics = result_payload.get("metrics") if isinstance(result_payload, dict) else None
        for metric in raw_metrics if isinstance(raw_metrics, list) else []:
            if isinstance(metric, dict) and metric.get("slug"):
                metrics[str(metric["slug"])] = self._int_metric(metric, "value")
        return metrics

    def _daily_points_from_totals(self, stats: dict[str, Any], account: AvitoStatsAccount) -> list[AvitoStatsDailyPoint]:
        raw_points = stats.get("_daily")
        if not isinstance(raw_points, list):
            return []
        points: list[AvitoStatsDailyPoint] = []
        for raw in raw_points:
            if not isinstance(raw, dict) or not isinstance(raw.get("date"), date):
                continue
            points.append(
                AvitoStatsDailyPoint(
                    date=raw["date"],
                    accountId=account.accountId,
                    accountName=account.accountName,
                    impressions=self._optional_int_metric(raw, "impressions", "shows"),
                    views=self._optional_int_metric(raw, "views", "uniqViews", "uniq_views"),
                    contactsMessenger=self._optional_int_metric(raw, "contactsMessenger", "messengerContacts", "contacts_messenger"),
                    contacts=self._optional_int_metric(raw, "contacts", "uniqContacts", "uniq_contacts"),
                    contactsShowPhone=self._optional_int_metric(raw, "contactsShowPhone", "contacts_show_phone"),
                    contactsShowPhoneAndMessenger=self._optional_int_metric(raw, "contactsShowPhoneAndMessenger", "contacts_show_phone_and_messenger"),
                    favorites=self._optional_int_metric(raw, "favorites", "uniqFavorites", "uniq_favorites"),
                    spendKopecks=self._optional_int_metric(raw, "allSpending", "spending", "presenceSpending"),
                    orders=self._optional_int_metric(raw, "orderedItems"),
                    buyouts=self._optional_int_metric(raw, "deliveredItems"),
                )
            )
        return points

    @staticmethod
    def _v2_totals_group_date(group: dict[str, Any]) -> date | None:
        for key in ("date", "day", "period", "value"):
            raw = group.get(key)
            if isinstance(raw, str):
                try:
                    return date.fromisoformat(raw[:10])
                except ValueError:
                    continue
        raw_id = group.get("id")
        if isinstance(raw_id, int) and raw_id > 0:
            try:
                return datetime.fromtimestamp(raw_id, tz=timezone.utc).date()
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(raw_id, str):
            try:
                return date.fromisoformat(raw_id[:10])
            except ValueError:
                return None
        return None

    @staticmethod
    def _avito_stats_diagnostics(status_code: int, payload: Any, *, request_url: str, request_body: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result") if type(payload) is dict else None
        groupings = result.get("groupings") if type(result) is dict else None
        data_total_count = result.get("dataTotalCount") if type(result) is dict else None
        return {
            "status": _diagnostic_status(status_code),
            "requestUrl": None,
            "requestBody": None,
            "bodyPreview": None,
            "resultKeys": None,
            "groupings": len(groupings) if type(groupings) is list else None,
            "dataTotalCount": _diagnostic_count(data_total_count),
            "reason": "avito_returned_timestamp_without_groupings" if type(result) is dict and "timestamp" in result and type(groupings) is not list else None,
        }

    @staticmethod
    def _log_avito_stats_response(diagnostics: dict[str, Any]) -> None:
        if type(diagnostics) is not dict:
            return
        reason = diagnostics.get("reason")
        safe_reason = ("avito_returned_timestamp_without_groupings"
                       if type(reason) is str and reason == "avito_returned_timestamp_without_groupings" else None)
        logger.warning("[AVITO_STATS_STATUS] %s", _diagnostic_status(diagnostics.get("status")))
        logger.warning("[AVITO_STATS_GROUPINGS] %s", _diagnostic_count(diagnostics.get("groupings")))
        logger.warning("[AVITO_STATS_TOTAL] %s", _diagnostic_count(diagnostics.get("dataTotalCount")))
        logger.warning("[AVITO_STATS_REASON] %s", safe_reason)

    def _v2_item_analytics_by_item(self, payload: Any) -> dict[str, dict[str, Any]]:
        groupings = payload.get("result", {}).get("groupings") if isinstance(payload, dict) else None
        if not isinstance(groupings, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for group in groupings:
            if not isinstance(group, dict) or group.get("type") not in {"item", "items"}:
                continue
            item_id = str(group.get("id") or "")
            if not item_id:
                continue
            metrics: dict[str, Any] = {"_source": "v2_item_analytics"}
            raw_metrics = group.get("metrics")
            for metric in raw_metrics if isinstance(raw_metrics, list) else []:
                if isinstance(metric, dict) and metric.get("slug"):
                    metrics[str(metric["slug"])] = self._int_metric(metric, "value")
            result[item_id] = metrics
        return result

    def _stats_by_item(self, payload: Any) -> dict[str, dict[str, Any]]:
        rows = self._stats_rows(payload)
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("itemId") or row.get("item_id") or row.get("itemID") or row.get("id") or "")
            if not item_id:
                continue
            metrics = {"uniqViews": 0, "uniqContacts": 0, "uniqFavorites": 0, "impressions": 0}
            stats_rows = row.get("stats") or row.get("days") or row.get("counters")
            for stat in stats_rows if isinstance(stats_rows, list) else [row]:
                if isinstance(stat, dict):
                    metrics["uniqViews"] += self._int_metric(stat, "uniqViews", "uniq_views", "views")
                    metrics["uniqContacts"] += self._int_metric(stat, "uniqContacts", "uniq_contacts", "contacts")
                    metrics["uniqFavorites"] += self._int_metric(stat, "uniqFavorites", "uniq_favorites", "favorites")
                    metrics["impressions"] += self._int_metric(stat, "impressions", "shows")
            result[item_id] = metrics
        return result

    @staticmethod
    def _stats_rows(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        result_payload = payload.get("result")
        rows = result_payload.get("items") if isinstance(result_payload, dict) else None
        rows = rows or payload.get("items")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, dict):
            normalized: list[dict[str, Any]] = []
            for item_id, row in rows.items():
                if isinstance(row, dict):
                    normalized.append({"item_id": item_id, **row})
                elif isinstance(row, list):
                    normalized.append({"item_id": item_id, "stats": row})
            return normalized
        return []

    @staticmethod
    def _int_metric(row: dict[str, Any], *keys: str) -> int:
        value = LiveAvitoStatsClient._optional_int_metric(row, *keys)
        return value if value is not None else 0

    @staticmethod
    def _optional_int_metric(row: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            if key not in row:
                continue
            try:
                return max(0, int(row.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _http_error(exc: Any) -> AvitoStatsFetchResult:
        status_code = _diagnostic_status(exc.response.status_code)
        if status_code is None:
            return AvitoStatsFetchResult(
                status="blocked",
                error=AvitoStatsFetchError(code="avito_request_failed", message="Avito request failed", retryable=True, blockerIds=["AVITO_STATS"]),
            )
        if status_code == 401:
            code, retryable, blocker = "auth_required", False, "AVITO_AUTH"
        elif status_code == 403:
            code, retryable, blocker = "forbidden_scope", False, "AVITO_SCOPE"
        elif status_code == 429:
            code, retryable, blocker = "rate_limited", True, "AVITO_RATE_LIMIT"
        elif status_code >= 500:
            code, retryable, blocker = "avito_server_error", True, "AVITO_STATS"
        else:
            code, retryable, blocker = "avito_request_failed", False, "AVITO_STATS"
        return AvitoStatsFetchResult(
            status="blocked",
            error=AvitoStatsFetchError(code=code, message=f"Avito HTTP {status_code}", retryable=retryable, blockerIds=[blocker]),
        )


def build_avito_stats_client(
    mode: str,
    access_token: str | None,
    base_url: str = "https://api.avito.ru",
    timeout_seconds: float = 20.0,
) -> AvitoStatsClient:
    if mode == "live":
        if not access_token:
            raise ValueError("AVITO_TOKEN_REQUIRED")
        return LiveAvitoStatsClient(access_token=access_token, base_url=base_url, timeout_seconds=timeout_seconds)
    return FakeAvitoStatsClient()
