"""Bounded WB reads only; no fallback client, credentials in queues, or retries here.

Official rules checked 2026-09-10 in the rendered WB documentation:
https://dev.wildberries.ru/docs/openapi/item-management
Content: POST cards/list, limit100, ascending native cursor, total<limit ends.
Prices: GET goods/filter, limit1000, offset+=limit, EMPTY page ends.
Unclassified/base tokens use the conservative prices interval of900seconds.
"""

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from app.modules.wb_price_snapshots import GoodsPricePage, parse_goods_price_page
from app.platform.integrations.credential_store import ResolvedCredentialForFetch
from app.wb_api.client import WbApiRequest, parse_rate_limit_headers


class WbReadError(ValueError):
    def __init__(self, code: str, *, retryable=False, retry_after_seconds=0):
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class LivePage:
    source: str
    rows: tuple[dict, ...]
    checkpoint: dict
    complete: bool
    received_at: datetime
    raw_checksum: str
    request_bytes: bytes
    price_page: GoodsPricePage | None = None
    retry_after_seconds: int = 0


def minimum_interval(source: str) -> int:
    if source == "content":
        return 1  # Below100/min; also accommodates the sandbox's1/sec cap.
    if source == "prices":
        return 900  # Safe for base token; never infer paid/service status from a JWT.
    raise WbReadError("WB_SOURCE_UNSUPPORTED")


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    return value


def _text(value, *, maximum=4096, optional=False):
    if value is None and optional:
        return None
    if type(value) is not str or len(value) > maximum or "\x00" in value:
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    return value


def _timestamp(value):
    _text(value, maximum=64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() is None:
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    return parsed


def request_for(source: str, checkpoint: dict) -> tuple[str, WbApiRequest]:
    if type(checkpoint) is not dict:
        raise WbReadError("WB_SOURCE_INVALID_CHECKPOINT")
    if source == "prices":
        if set(checkpoint) - {"offset"}:
            raise WbReadError("WB_SOURCE_INVALID_CHECKPOINT")
        offset = _integer(checkpoint.get("offset", 0))
        return "https://discounts-prices-api.wildberries.ru", WbApiRequest(
            method="GET",
            path="/api/v2/list/goods/filter",
            query={"limit": 1000, "offset": offset},
        )
    if source != "content":
        raise WbReadError("WB_SOURCE_UNSUPPORTED")
    if checkpoint:
        if set(checkpoint) != {"updatedAt", "nmID"}:
            raise WbReadError("WB_SOURCE_INVALID_CHECKPOINT")
        _timestamp(checkpoint["updatedAt"])
        _integer(checkpoint["nmID"], 1)
    return "https://content-api.wildberries.ru", WbApiRequest(
        method="POST",
        path="/content/v2/get/cards/list",
        jsonBody={
            "settings": {
                "sort": {"ascending": True},
                "filter": {"withPhoto": -1},
                "cursor": {"limit": 100, **checkpoint},
            }
        },
    )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WbReadError("WB_SOURCE_INVALID_PAGE")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise WbReadError("WB_SOURCE_INVALID_PAGE")


def _json(raw):
    try:
        data = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_float=Decimal,
            parse_constant=_invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        data = None
    if (
        type(data) is not dict
        or data.get("error") not in (None, False)
        or data.get("errorText") not in (None, "")
    ):
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    return data


def _photo(value):
    if value is None:
        return None
    _text(value, maximum=2048)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    # URL is a display value only, never fetched by this worker.
    return value


def _content_row(row):
    if type(row) is not dict or type(row.get("sizes")) is not list:
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    sizes, seen = [], set()
    for size in row["sizes"]:
        if type(size) is not dict or type(size.get("skus")) is not list:
            raise WbReadError("WB_SOURCE_INVALID_PAGE")
        identity = _integer(size.get("chrtID"), 1)
        if identity in seen:
            raise WbReadError("WB_SOURCE_DUPLICATE_IDENTITY")
        seen.add(identity)
        sizes.append(
            {
                "chrtId": identity,
                "techSize": _text(size.get("techSize"), optional=True),
                "skus": [_text(barcode, maximum=255) for barcode in size["skus"]],
            }
        )
    photos = row.get("photos", [])
    if type(photos) is not list or (photos and type(photos[0]) is not dict):
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    updated = row.get("updatedAt")
    _timestamp(updated)
    return {
        "nmId": _integer(row.get("nmID"), 1),
        "vendorCode": _text(row.get("vendorCode"), maximum=255),
        "title": _text(row.get("title")),
        "brand": _text(row.get("brand"), optional=True),
        "subjectId": None
        if row.get("subjectID") is None
        else _integer(row["subjectID"], 1),
        "subjectName": _text(row.get("subjectName"), optional=True),
        "photoUrl": _photo(photos[0].get("tm") or photos[0].get("big"))
        if photos
        else None,
        "sizes": sizes,
        "sourceUpdatedAt": updated,
    }


def parse_page(
    source, checkpoint, raw, *, organization_id, marketplace_account_id, received_at
):
    _integer(organization_id, 1)
    _integer(marketplace_account_id, 1)
    if (
        type(raw) is not bytes
        or type(received_at) is not datetime
        or received_at.utcoffset() is None
    ):
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    _, request = request_for(source, checkpoint)
    manifest = json.dumps(
        {
            "schema": "wb-live-read/v1",
            "organizationId": organization_id,
            "marketplaceAccountId": marketplace_account_id,
            "source": source,
            "method": request.method,
            "path": request.path,
            "query": request.query,
            "body": request.jsonBody,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    data = _json(raw)
    if source == "prices":
        failure = False
        try:
            prices = parse_goods_price_page(
                raw,
                organization_id=organization_id,
                marketplace_account_id=marketplace_account_id,
                offset=checkpoint.get("offset", 0),
                limit=1000,
                received_at=received_at,
                request_checksum=hashlib.sha256(manifest).hexdigest(),
            )
        except ValueError:
            failure = True
        if failure:
            raise WbReadError("WB_SOURCE_INVALID_PAGE")
        rows_list = []
        for product, native in zip(
            prices.products, data["data"]["listGoods"], strict=True
        ):
            row = {"nmId": product.nm_id, "sizes": []}
            if "vendorCode" in native:
                row["vendorCode"] = _text(
                    native["vendorCode"], maximum=255, optional=True
                )
            for key, value in (
                ("discountPct", product.discount),
                ("clubDiscountPct", product.club_discount),
            ):
                if value.presence != "missing":
                    row[key] = value.value
            for size, native_size in zip(
                product.sizes, native.get("sizes", []), strict=True
            ):
                if type(size.source_size_id) is not int or size.source_size_id <= 0:
                    raise WbReadError("WB_SOURCE_IDENTITY_UNRESOLVED")
                item = {"chrtId": size.source_size_id}
                if "techSizeName" in native_size:
                    item["techSize"] = _text(native_size["techSizeName"], optional=True)
                for key, value in (
                    ("priceKopecks", size.list_price),
                    ("discountedPriceKopecks", size.discounted_price),
                    ("clubPriceKopecks", size.club_price),
                ):
                    if value.presence != "missing":
                        item[key] = value.value
                row["sizes"].append(item)
            rows_list.append(row)
        rows = tuple(rows_list)
        return LivePage(
            source,
            rows,
            {"offset": checkpoint.get("offset", 0) + 1000},
            not rows,
            received_at,
            prices.raw_checksum,
            manifest,
            prices,
        )
    cards, cursor = data.get("cards"), data.get("cursor")
    if type(cards) is not list or len(cards) > 100 or type(cursor) is not dict:
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    total = _integer(cursor.get("total"))
    if total != len(cards):
        raise WbReadError("WB_SOURCE_INVALID_PAGE")
    rows = tuple(_content_row(row) for row in cards)
    if len({row["nmId"] for row in rows}) != len(rows):
        raise WbReadError("WB_SOURCE_DUPLICATE_IDENTITY")
    next_cursor = dict(checkpoint)
    if rows:
        next_cursor = {"updatedAt": cursor.get("updatedAt"), "nmID": cursor.get("nmID")}
        stamp = _timestamp(next_cursor["updatedAt"])
        _integer(next_cursor["nmID"], 1)
        if checkpoint and (stamp, next_cursor["nmID"]) <= (
            _timestamp(checkpoint["updatedAt"]),
            checkpoint["nmID"],
        ):
            raise WbReadError("WB_SOURCE_PAGINATION_STALLED")
    elif total >= 100:
        raise WbReadError("WB_SOURCE_PAGINATION_STALLED")
    return LivePage(
        source,
        rows,
        next_cursor,
        total < 100,
        received_at,
        hashlib.sha256(raw).hexdigest(),
        manifest,
    )


def _retry_after(headers, now):
    parsed = parse_rate_limit_headers(dict(headers))
    delays = [0]
    if parsed is not None:
        for value in (parsed.retryAfterSeconds, parsed.resetAfterSeconds):
            if type(value) is int and value >= 0:
                delays.append(value)
    value = headers.get("Retry-After")
    if value:
        try:
            delays.append(max(0, math.ceil(float(value))))
        except (ValueError, OverflowError):
            try:
                delays.append(
                    max(
                        0,
                        math.ceil((parsedate_to_datetime(value) - now).total_seconds()),
                    )
                )
            except (ValueError, TypeError, OverflowError):
                pass
    return max(delays)


class ReadOnlyWbProvider:
    """One bounded request, secret reveal only in transport, no auto redirects/retry."""

    def __init__(self, *, transport=None, max_response_bytes=8 * 1024 * 1024):
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= 16 * 1024 * 1024
        ):
            raise WbReadError("WB_SOURCE_CONFIGURATION_INVALID")
        self._transport, self._budget = transport, max_response_bytes

    def fetch(
        self, source, checkpoint, *, credential, organization_id, marketplace_account_id
    ):
        base, request = request_for(source, checkpoint)
        if type(credential) is not ResolvedCredentialForFetch or (
            credential.binding.owner.organization_id,
            credential.binding.owner.marketplace_account_id,
            credential.binding.owner.provider,
        ) != (organization_id, marketplace_account_id, "wb"):
            raise WbReadError("WB_SOURCE_BINDING_CHANGED")
        failure, raw, delay = None, bytearray(), 0
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                token = credential.secret.reveal()["token"]
                started = time.monotonic()
                with client.stream(
                    request.method,
                    base + request.path,
                    params=request.query or None,
                    json=request.jsonBody,
                    headers={"Authorization": token, "Accept": "application/json"},
                ) as response:
                    delay = _retry_after(response.headers, datetime.now(UTC))
                    status = response.status_code
                    if status != 200:
                        code = (
                            "WB_SOURCE_RATE_LIMITED"
                            if status == 429
                            else "WB_SOURCE_ACCESS_DENIED"
                            if status in (401, 403)
                            else "WB_SOURCE_BILLING_REQUIRED"
                            if status == 402
                            else "WB_SOURCE_REQUEST_FAILED"
                        )
                        failure = WbReadError(
                            code,
                            retryable=status in (408, 429) or 500 <= status <= 599,
                            retry_after_seconds=delay,
                        )
                    else:
                        for chunk in response.iter_bytes(chunk_size=65536):
                            if (
                                len(raw) + len(chunk) > self._budget
                                or time.monotonic() - started > 45
                            ):
                                failure = WbReadError("WB_SOURCE_RESPONSE_LIMIT")
                                break
                            raw.extend(chunk)
        except (httpx.HTTPError, OSError):
            failure = WbReadError("WB_SOURCE_TRANSPORT_FAILED", retryable=True)
        if failure is not None:
            raise failure
        page = parse_page(
            source,
            checkpoint,
            bytes(raw),
            organization_id=organization_id,
            marketplace_account_id=marketplace_account_id,
            received_at=datetime.now(UTC),
        )
        return LivePage(
            page.source,
            page.rows,
            page.checkpoint,
            page.complete,
            page.received_at,
            page.raw_checksum,
            page.request_bytes,
            page.price_page,
            delay,
        )
