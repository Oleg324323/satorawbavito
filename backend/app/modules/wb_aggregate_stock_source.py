"""Current official WB aggregate-location stock source, not legacy warehouse v1.

Verified 2026-09-10: /en/openapi/analytics postV1StocksReportWbWarehouses.
The provider currently documents warehouseId=-999999 only. Preserve that grain;
never invent a real warehouse, publish historical stock or relabel old hashes.
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx

from app.platform.integrations.credential_store import ResolvedCredentialForFetch
from app.platform.integrations.repricer_job_contract import integer, timestamp
from app.wb_live.provider import WbReadError, _retry_after


def _uint(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise WbReadError("WB_SOURCE_RESPONSE_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class AggregateStockRequest:
    organization_id: int
    marketplace_account_id: int
    page_limit: int = 1000

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        _uint(self.page_limit, 250000, 1)

    @property
    def canonical_bytes(self):
        return json.dumps(
            {
                "schema": "wb-aggregate-size-stock-collection/v1",
                "organizationId": self.organization_id,
                "accountId": self.marketplace_account_id,
                "method": "POST",
                "path": "/api/analytics/v1/stocks-report/wb-warehouses",
                "body": {"limit": self.page_limit},
                "scope": "wb_aggregate_size",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    @property
    def checksum(self):
        return sha256(self.canonical_bytes).hexdigest()

    def page_body(self, offset):
        _uint(offset, 2**32 - 1)
        return json.dumps(
            {"limit": self.page_limit, "offset": offset},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")


@dataclass(frozen=True, slots=True)
class AggregateStockCount:
    presence: str
    value: int | None

    def __post_init__(self):
        if self.presence == "value":
            _uint(self.value, 2**64 - 1)
        elif self.presence not in ("missing", "null") or self.value is not None:
            raise WbReadError("WB_SOURCE_RESPONSE_INVALID")


@dataclass(frozen=True, slots=True)
class AggregateStockItem:
    nm_id: int
    chrt_id: int
    provider_warehouse_id: int
    quantity: AggregateStockCount
    in_way_to_client: AggregateStockCount
    in_way_from_client: AggregateStockCount

    @property
    def identity(self):
        return self.nm_id, self.chrt_id, self.provider_warehouse_id


@dataclass(frozen=True, slots=True)
class AggregateStockPage:
    request: AggregateStockRequest
    offset: int
    http_status: int
    received_at: datetime
    next_not_before: datetime
    raw_checksum: str
    request_checksum: str
    items: tuple[AggregateStockItem, ...]

    @property
    def terminal(self):
        # Conservative EOF: a short nonempty page does not imply full coverage.
        return not self.items

    @property
    def next_offset(self):
        return None if self.terminal else self.offset + self.request.page_limit


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise WbReadError("WB_SOURCE_RESPONSE_INVALID")
        result[key] = value
    return result


def _invalid(value):
    raise WbReadError("WB_SOURCE_RESPONSE_INVALID")


def _count(row, key):
    if key not in row:
        return AggregateStockCount("missing", None)
    return (
        AggregateStockCount("null", None)
        if row[key] is None
        else AggregateStockCount("value", row[key])
    )


def parse_aggregate_stock_page(
    raw, *, request, offset, received_at, http_status=200, retry_after_seconds=20
):
    if (
        type(request) is not AggregateStockRequest
        or type(raw) is not bytes
        or len(raw) > 64 * 1024 * 1024
    ):
        raise WbReadError("WB_SOURCE_RESPONSE_INVALID")
    request_bytes = request.page_body(offset)
    received_at = timestamp(received_at)
    _uint(retry_after_seconds, 86400)
    rows = None
    if type(http_status) is not int:
        raise WbReadError("WB_SOURCE_RESPONSE_INVALID")
    if http_status == 204 and not raw:
        rows = []
    elif http_status == 200:
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_float=_invalid,
                parse_constant=_invalid,
            )
        except (ValueError, UnicodeError, RecursionError):
            raise WbReadError("WB_SOURCE_RESPONSE_INVALID") from None
        if (
            type(payload) is dict
            and payload.get("error", False) is False
            and payload.get("errorText") in (None, "")
            and type(payload.get("data")) is dict
        ):
            rows = payload["data"].get("items")
    if type(rows) is not list or len(rows) > request.page_limit:
        raise WbReadError("WB_SOURCE_RESPONSE_INVALID")
    items, seen = [], set()
    for row in rows:
        if type(row) is not dict:
            raise WbReadError("WB_SOURCE_RESPONSE_INVALID")
        nm, chrt = (
            _uint(row.get("nmId"), 2**63 - 1, 1),
            _uint(row.get("chrtId"), 2**63 - 1, 1),
        )
        if (
            type(row.get("warehouseId")) is not int
            or row["warehouseId"] != -999999
            or row.get("warehouseName") != "Склад WB"
            or row.get("regionName") != "Склад WB"
        ):
            raise WbReadError("WB_SOURCE_GRAIN_UNRESOLVED")
        item = AggregateStockItem(
            nm,
            chrt,
            -999999,
            _count(row, "quantity"),
            _count(row, "inWayToClient"),
            _count(row, "inWayFromClient"),
        )
        if item.identity in seen:
            raise WbReadError("WB_SOURCE_RESPONSE_INVALID")
        seen.add(item.identity)
        items.append(item)
    return AggregateStockPage(
        request,
        offset,
        http_status,
        received_at,
        received_at + timedelta(seconds=max(20, retry_after_seconds)),
        sha256(raw).hexdigest(),
        sha256(request_bytes).hexdigest(),
        tuple(items),
    )


class WbAggregateStockProvider:
    def __init__(self, *, admit_request, record_cooldown, transport=None, clock=None):
        if not callable(admit_request) or not callable(record_cooldown):
            raise WbReadError("WB_SOURCE_ACCESS_DENIED")
        self._admit, self._transport = admit_request, transport
        self._clock = clock or (lambda: datetime.now(UTC))
        self._record_cooldown = record_cooldown

    def read_page(self, *, request, offset, credential):
        if (
            type(request) is not AggregateStockRequest
            or type(credential) is not ResolvedCredentialForFetch
        ):
            raise WbReadError("WB_SOURCE_BINDING_CHANGED")
        owner = credential.binding.owner
        if (
            owner.organization_id,
            owner.marketplace_account_id,
            owner.provider,
            credential.binding.credential_identity.credential_kind,
        ) != (request.organization_id, request.marketplace_account_id, "wb", "wb_api"):
            raise WbReadError("WB_SOURCE_BINDING_CHANGED")
        body = request.page_body(offset)
        # Trusted callback must reserve durable quota and validate entitlement.
        # No permissive default and no process-global sleep/rate limiter.
        if (
            self._admit(request.organization_id, request.marketplace_account_id, 20)
            is not True
        ):
            raise WbReadError("WB_SOURCE_ACCESS_DENIED")
        result, error = None, None
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                started = time.monotonic()
                with client.stream(
                    "POST",
                    "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
                    content=body,
                    headers={
                        "Authorization": credential.secret.reveal()["token"],
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    now = timestamp(self._clock())
                    delay = max(20, _retry_after(response.headers, now))
                    if (
                        self._record_cooldown(
                            request.organization_id,
                            request.marketplace_account_id,
                            now + timedelta(seconds=delay),
                        )
                        is not True
                    ):
                        raise WbReadError("WB_SOURCE_UNAVAILABLE")
                    if response.status_code not in (200, 204):
                        error = WbReadError(
                            "WB_SOURCE_REQUEST_FAILED",
                            retryable=response.status_code == 429,
                            retry_after_seconds=delay,
                        )
                    else:
                        raw = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            if (
                                len(raw) + len(chunk) > 64 * 1024 * 1024
                                or time.monotonic() - started > 45
                            ):
                                raise WbReadError("WB_SOURCE_RESPONSE_LIMIT")
                            raw.extend(chunk)
                        if time.monotonic() - started > 45:
                            raise WbReadError("WB_SOURCE_RESPONSE_LIMIT")
                        result = (bytes(raw), response.status_code, delay)
        except Exception:  # noqa: BLE001 -- never disclose HTTP request or secrets in exception context.
            error = WbReadError(
                "WB_SOURCE_TRANSPORT_FAILED", retryable=True, retry_after_seconds=20
            )
        if error is not None:
            raise error
        raw, status, delay = result
        return parse_aggregate_stock_page(
            raw,
            request=request,
            offset=offset,
            received_at=self._clock(),
            http_status=status,
            retry_after_seconds=delay,
        )
