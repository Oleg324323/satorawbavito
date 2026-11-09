from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.avito.stats import AVITO_ITEM_LIST_STATUSES, AvitoStatsAccount, AvitoStatsFetchRequest, LiveAvitoStatsClient


logger = logging.getLogger(__name__)


class AvitoListingDetailRateLimit(RuntimeError):
    pass


AvitoListingStatus = Literal["active", "removed", "old", "blocked", "unknown"]


class AvitoListingsFetchRequest(BaseModel):
    dateFrom: date
    dateTo: date
    accountIds: list[str] = Field(default_factory=list)


class AvitoListingDetailsFetchRequest(BaseModel):
    itemIds: list[str] = Field(default_factory=list)
    accountIds: list[str] = Field(default_factory=list)


class AvitoListingRow(BaseModel):
    itemId: str = Field(min_length=1)
    title: str = Field(min_length=1)
    accountId: str = Field(min_length=1)
    accountName: str = Field(min_length=1)
    category: str | None = None
    url: str | None = None
    imageUrl: str | None = None
    sellerArticle: str | None = None
    size: str | None = None
    color: str | None = None
    status: AvitoListingStatus = "unknown"
    priceKopecks: int | None = Field(default=None, ge=0)
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
    sourceStatus: Literal["fresh", "partial"] = "partial"
    updatedAt: str | None = None


class AvitoListingsError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    blockerIds: list[str] = Field(default_factory=list)
    retryAfterSeconds: int | None = None
    retryAfterUntil: str | None = None


class AvitoListingsFetchResult(BaseModel):
    status: Literal["synced", "partial", "blocked"]
    accounts: list[AvitoStatsAccount] = Field(default_factory=list)
    rows: list[AvitoListingRow] = Field(default_factory=list)
    error: AvitoListingsError | None = None
    diagnostics: dict[str, Any] | None = None


class AvitoListingsClient(Protocol):
    def fetch_listings(self, request: AvitoListingsFetchRequest) -> AvitoListingsFetchResult:
        ...

    def fetch_listing_details(self, request: AvitoListingDetailsFetchRequest) -> AvitoListingsFetchResult:
        ...


def _normalize_status(value: Any) -> AvitoListingStatus:
    raw = str(value or "").strip().lower()
    if raw in {"active", "removed", "old", "blocked"}:
        return raw  # type: ignore[return-value]
    return "unknown"


class LiveAvitoListingsClient(LiveAvitoStatsClient):
    # Photos are imported once by the extension and served from the database.
    # Loading repricer statistics must never scrape each public listing page.
    _PUBLIC_IMAGE_FETCH_LIMIT = 0
    """Read-only Avito listings registry with period item analytics."""

    def fetch_listings(self, request: AvitoListingsFetchRequest) -> AvitoListingsFetchResult:
        self.analytics_error: AvitoListingsError | None = None
        try:
            with self._http_client() as client:
                accounts = self._accounts(client, request.accountIds)
                items = self._items(client, accounts)
                stat_request = AvitoStatsFetchRequest(dateFrom=request.dateFrom, dateTo=request.dateTo, accountIds=request.accountIds)
                rows = self._listing_rows(client, stat_request, accounts, items)
                diagnostics = dict(self.diagnostics or {})
                diagnostics["itemsCount"] = len(items)
                diagnostics["itemIdsSample"] = [item["itemId"] for item in items[:10]]
                diagnostics["itemStatuses"] = AVITO_ITEM_LIST_STATUSES
        except Exception as exc:
            logger.warning("[AVITO_LISTINGS_ERROR] %s", str(exc)[:500])
            return AvitoListingsFetchResult(
                status="blocked",
                error=self._fetch_error(exc, fallback_code="avito_listings_failed", fallback_blocker="AVITO_LISTINGS"),
            )
        return AvitoListingsFetchResult(status="partial" if self.analytics_error else "synced", accounts=accounts, rows=rows, diagnostics=diagnostics, error=self.analytics_error)

    def fetch_listing_details(self, request: AvitoListingDetailsFetchRequest) -> AvitoListingsFetchResult:
        item_ids = list(dict.fromkeys(str(item_id).strip() for item_id in request.itemIds if str(item_id).strip()))
        if not item_ids:
            return AvitoListingsFetchResult(status="synced")
        try:
            with self._http_client() as client:
                accounts = self._accounts(client, request.accountIds)
                items = self._item_details(client, accounts, item_ids)
                rows = self._listing_rows_without_stats(accounts, items)
                diagnostics = {"itemsCount": len(items), "itemIdsSample": item_ids[:10], "itemsEndpoint": "GET /core/v1/accounts/{user_id}/items/{item_id}/"}
        except Exception as exc:
            return AvitoListingsFetchResult(
                status="blocked",
                error=self._fetch_error(exc, fallback_code="avito_listing_details_failed", fallback_blocker="AVITO_LISTING_DETAILS"),
            )
        return AvitoListingsFetchResult(status="synced", accounts=accounts, rows=rows, diagnostics=diagnostics)

    def _http_client(self) -> Any:
        from app.avito.stats import httpx

        if httpx is None:
            raise RuntimeError("httpx is required for LiveAvitoListingsClient")
        return httpx.Client(timeout=self.timeout_seconds)

    @staticmethod
    def _fetch_error(exc: Exception, *, fallback_code: str, fallback_blocker: str) -> AvitoListingsError:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            try:
                code_int = int(status_code)
            except (TypeError, ValueError):
                code_int = 0
            if code_int == 401:
                return AvitoListingsError(code="auth_required", message="Avito HTTP 401", retryable=False, blockerIds=["AVITO_AUTH"])
            if code_int == 403:
                return AvitoListingsError(code="forbidden_scope", message="Avito HTTP 403", retryable=False, blockerIds=["AVITO_SCOPE"])
            if code_int == 429:
                retry = str(getattr(response, "headers", {}).get("Retry-After") or "")
                seconds = None
                try:
                    seconds = max(0, ceil(float(retry)))
                except (ValueError, OverflowError):
                    try:
                        until = parsedate_to_datetime(retry)
                        if until.tzinfo is not None:
                            seconds = max(0, ceil((until - datetime.now(timezone.utc)).total_seconds()))
                    except (TypeError, ValueError, OverflowError):
                        pass
                return AvitoListingsError(code="rate_limited", message="Avito HTTP 429", retryable=True,
                                          blockerIds=["AVITO_RATE_LIMIT"], retryAfterSeconds=seconds)
            if code_int >= 500:
                return AvitoListingsError(code="avito_server_error", message=f"Avito HTTP {code_int}", retryable=True, blockerIds=[fallback_blocker])
            if code_int:
                return AvitoListingsError(code="avito_request_failed", message=f"Avito HTTP {code_int}", retryable=False, blockerIds=[fallback_blocker])
        return AvitoListingsError(code=fallback_code, message=str(exc), retryable=True, blockerIds=[fallback_blocker])

    def _listing_rows(
        self,
        client: Any,
        request: AvitoStatsFetchRequest,
        accounts: list[AvitoStatsAccount],
        items: list[dict[str, Any]],
    ) -> list[AvitoListingRow]:
        result: list[AvitoListingRow] = []
        by_account = {account.accountId: account for account in accounts}
        for account in accounts:
            account_items = [item for item in items if item["accountId"] == account.accountId]
            account.itemCount = len(account_items)
            account.activeItemCount = sum(1 for item in account_items if str(item.get("status") or "active") == "active")
            account.inactiveItemCount = max(0, account.itemCount - account.activeItemCount)
            try:
                stats_by_item = self._v2_item_analytics_for_account(client, request, account.accountId)
            except Exception as exc:
                # Inventory is valid even when optional selected-period
                # analytics fail. Keep metrics unavailable, not zero.
                self.analytics_error = self._fetch_error(exc, fallback_code="avito_analytics_failed", fallback_blocker="AVITO_STATS")
                stats_by_item = {}
            for item in account_items:
                stats = stats_by_item.get(item["itemId"], {})
                account_name = by_account.get(item["accountId"], account).accountName
                result.append(
                    AvitoListingRow(
                        itemId=item["itemId"],
                        title=item["title"],
                        accountId=item["accountId"],
                        accountName=account_name,
                        category=item.get("category"),
                        url=item.get("url"),
                        imageUrl=item.get("imageUrl"),
                        sellerArticle=item.get("sellerArticle"),
                        size=item.get("size"),
                        color=item.get("color"),
                        status=_normalize_status(item.get("status")),
                        priceKopecks=self._price_kopecks(item.get("price") or item.get("priceRub") or item.get("price_rub")),
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
                    )
                )
        return result

    def _listing_rows_without_stats(self, accounts: list[AvitoStatsAccount], items: list[dict[str, Any]]) -> list[AvitoListingRow]:
        result: list[AvitoListingRow] = []
        by_account = {account.accountId: account for account in accounts}
        for item in items:
            account = by_account.get(str(item.get("accountId") or ""))
            result.append(
                AvitoListingRow(
                    itemId=item["itemId"],
                    title=item["title"],
                    accountId=item["accountId"],
                    accountName=(account.accountName if account else item.get("accountName")) or item["accountId"],
                    category=item.get("category"),
                    url=item.get("url"),
                    imageUrl=item.get("imageUrl"),
                    sellerArticle=item.get("sellerArticle"),
                    size=item.get("size"),
                    color=item.get("color"),
                    status=_normalize_status(item.get("status")),
                    priceKopecks=self._price_kopecks(item.get("price") or item.get("priceRub") or item.get("price_rub")),
                    sourceStatus="partial",
                )
            )
        return result

    def _item_details(self, client: Any, accounts: list[AvitoStatsAccount], item_ids: list[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        plain_detail_404_count = 0
        for account in accounts:
            for item_id in item_ids:
                if item_id in seen:
                    continue
                try:
                    payload, plain_detail_found = self._get_item_detail_payload(
                        client,
                        account.accountId,
                        item_id,
                        skip_plain_detail=plain_detail_404_count >= 3,
                    )
                except AvitoListingDetailRateLimit:
                    logger.warning("[AVITO_LISTING_DETAIL_ABORTED] rate_limited")
                    return result
                if plain_detail_found is False:
                    plain_detail_404_count += 1
                if payload is None:
                    continue
                raw = self._detail_payload(payload)
                if not isinstance(raw, dict):
                    continue
                raw.setdefault("id", item_id)
                normalized = self._listing_item_payload(raw, account)
                if normalized.get("itemId"):
                    result.append(normalized)
                    seen.add(str(normalized["itemId"]))
        return result

    def _get_item_detail_payload(self, client: Any, account_id: str, item_id: str, *, skip_plain_detail: bool = False) -> tuple[Any | None, bool | None]:
        urls = [("account", f"{self.base_url}/core/v1/accounts/{account_id}/items/{item_id}/")]
        if not skip_plain_detail:
            urls.append(("plain", f"{self.base_url}/core/v1/items/{item_id}"))
        fallback_payload: Any | None = None
        plain_detail_found: bool | None = None
        for kind, url in urls:
            try:
                response = client.get(url, headers=self._headers())
                status_code = int(getattr(response, "status_code", 200) or 200)
                logger.warning("[AVITO_LISTING_DETAIL_STATUS] %s", status_code)
                logger.warning("[AVITO_LISTING_DETAIL_URL] %s", url)
                if status_code == 429:
                    raise AvitoListingDetailRateLimit()
                if status_code == 404:
                    if kind == "plain":
                        plain_detail_found = False
                    continue
                response.raise_for_status()
                payload = response.json()
                logger.warning("[AVITO_LISTING_DETAIL_BODY] %s", str(payload)[:4000])
                if kind == "plain":
                    plain_detail_found = True
                if self._detail_payload_has_product_fields(payload):
                    return payload, plain_detail_found
                if fallback_payload is None:
                    fallback_payload = payload
            except AvitoListingDetailRateLimit:
                logger.warning("[AVITO_LISTING_DETAIL_RATE_LIMIT] %s", url)
                raise
            except Exception as exc:
                logger.warning("[AVITO_LISTING_DETAIL_ERROR] %s %s", url, str(exc)[:500])
                continue
        return fallback_payload, plain_detail_found

    @staticmethod
    def _detail_payload_has_product_fields(payload: Any) -> bool:
        raw = LiveAvitoListingsClient._detail_payload(payload)
        if not isinstance(raw, dict):
            return False
        direct_keys = {
            "title",
            "name",
            "description",
            "parameters",
            "params",
            "properties",
            "attributes",
            "options",
            "sellerArticle",
            "seller_article",
            "sellerSku",
            "vendorCode",
            "size",
            "color",
            "colour",
            "apparelSize",
            "apparel_size",
        }
        return any(key in raw and raw.get(key) not in (None, "", [], {}) for key in direct_keys)

    @staticmethod
    def _detail_payload(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        for key in ("result", "item", "resource", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload if payload else None

    @staticmethod
    def _price_kopecks(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return max(0, int(round(float(value) * 100)))
        except (TypeError, ValueError):
            return None

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


def build_avito_listings_client(
    access_token: str,
    base_url: str = "https://api.avito.ru",
    timeout_seconds: float = 20.0,
) -> AvitoListingsClient:
    return LiveAvitoListingsClient(access_token=access_token, base_url=base_url, timeout_seconds=timeout_seconds)
