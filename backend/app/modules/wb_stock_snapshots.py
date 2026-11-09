"""Pure detailed WB-warehouse stock observations; no totals or current publication.

The bounded v1 adapter accepts explicit data arrays only. Legacy alternative
wrappers, async warehouse-name guesses and FBS quantities are not this source.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, DecimalException
from typing import Literal
from zoneinfo import ZoneInfo

from app.modules.wb_source_requests import CollectionRequest, SourceKind


class StockSourceError(ValueError):
    """Safe source error without reflecting provider payload."""


def _integer(value: object, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 9223372036854775807:
        raise StockSourceError("integer outside storage range")


def _time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StockSourceError("aware received time required")


def _context(request: CollectionRequest) -> None:
    if (type(request) is not CollectionRequest or request.source_kind is not SourceKind.wb_warehouse
            or request.parser_version != "wb-warehouse-stocks/v1"):
        raise StockSourceError("compatible warehouse collection required")
    _integer(request.page_limit, 1)
    _integer(request.organization_id, 1)
    _integer(request.marketplace_account_id, 1)


@dataclass(frozen=True, slots=True)
class StockCount:
    presence: Literal["missing", "null", "value"]
    value: int | None

    def __post_init__(self) -> None:
        if self.presence == "value":
            _integer(self.value)
        elif self.presence not in ("missing", "null") or self.value is not None:
            raise StockSourceError("stock presence/value mismatch")


@dataclass(frozen=True, slots=True)
class WarehouseStockObservation:
    nm_id: int
    chrt_id: int | None
    warehouse_id: int
    quantity: StockCount
    in_way_to_client: StockCount
    in_way_from_client: StockCount

    def __post_init__(self) -> None:
        _integer(self.nm_id, 1)
        _integer(self.warehouse_id, 1)
        if self.chrt_id is not None:
            _integer(self.chrt_id, 1)
        if not all(type(value) is StockCount for value in
                   (self.quantity, self.in_way_to_client, self.in_way_from_client)):
            raise StockSourceError("typed stock counts required")

    @property
    def identity(self) -> tuple[int, int | None, int]:
        return self.nm_id, self.chrt_id, self.warehouse_id


@dataclass(frozen=True, slots=True)
class WarehouseStockPage:
    request: CollectionRequest
    offset: int
    received_at: datetime
    raw_checksum: str
    observations: tuple[WarehouseStockObservation, ...]

    def __post_init__(self) -> None:
        _context(self.request)
        _integer(self.offset)
        _time(self.received_at)
        if (type(self.raw_checksum) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.raw_checksum) is None):
            raise StockSourceError("raw SHA-256 required")
        if (type(self.observations) is not tuple
                or not all(type(row) is WarehouseStockObservation for row in self.observations)
                or len(self.observations) > self.request.page_limit):
            raise StockSourceError("bounded immutable observations required")
        if len({row.identity for row in self.observations}) != len(self.observations):
            raise StockSourceError("duplicate stock identity")

    @property
    def terminal(self) -> bool:
        return len(self.observations) < self.request.page_limit

    @property
    def source_observed_at(self) -> None:
        # No documented timestamp interpreted by this bounded source adapter.
        return None


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise StockSourceError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise StockSourceError("non-finite JSON constant")


def _count(row: dict, field: str) -> StockCount:
    if field not in row:
        return StockCount("missing", None)
    if row[field] is None:
        return StockCount("null", None)
    return StockCount("value", row[field])


def parse_warehouse_stock_page(raw_json: bytes, *, request: CollectionRequest,
                               offset: int, received_at: datetime) -> WarehouseStockPage:
    _context(request)
    _integer(offset)
    _time(received_at)
    if type(raw_json) is not bytes:
        raise StockSourceError("raw source bytes required")
    try:
        payload = json.loads(raw_json.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_float=Decimal, parse_constant=_constant)
    except (ValueError, UnicodeError, DecimalException):
        raise StockSourceError("invalid source JSON") from None
    if type(payload) is not dict or type(payload.get("data")) is not list:
        raise StockSourceError("explicit data array required")
    if (("error" in payload and payload["error"] is not False)
            or payload.get("errorText") not in (None, "")):
        raise StockSourceError("provider reported source error")
    if len(payload["data"]) > request.page_limit:
        raise StockSourceError("page exceeds requested limit")
    observations = []
    for row in payload["data"]:
        if type(row) is not dict or ("stockType" in row and row["stockType"] != "wb"):
            raise StockSourceError("warehouse stock row required")
        observations.append(WarehouseStockObservation(
            row.get("nmId"), row.get("chrtId"), row.get("warehouseId"),
            _count(row, "quantity"), _count(row, "inWayToClient"), _count(row, "inWayFromClient"),
        ))
    return WarehouseStockPage(request, offset, received_at, hashlib.sha256(raw_json).hexdigest(),
                              tuple(observations))


@dataclass(frozen=True, slots=True)
class StockRun:
    pages: tuple[WarehouseStockPage, ...]

    def __post_init__(self) -> None:
        if (type(self.pages) is not tuple or not self.pages
                or not all(type(page) is WarehouseStockPage for page in self.pages)):
            raise StockSourceError("immutable nonempty page sequence required")
        identities = set()
        first = self.pages[0]
        previous_time = None
        for index, page in enumerate(self.pages):
            try:
                received_time = page.received_at.astimezone(UTC)
            except OverflowError:
                raise StockSourceError("receipt outside supported UTC range") from None
            if page.request != first.request or page.offset != index * first.request.page_limit:
                raise StockSourceError("noncontiguous or mixed collection")
            if index and (self.pages[index - 1].terminal
                          or received_time < previous_time):
                raise StockSourceError("invalid terminal/time sequence")
            previous_time = received_time
            for row in page.observations:
                if row.identity in identities:
                    raise StockSourceError("duplicate stock identity across pages")
                identities.add(row.identity)

    @property
    def observations(self) -> tuple[WarehouseStockObservation, ...]:
        return tuple(row for page in self.pages for row in page.observations)

    @property
    def complete(self) -> bool:
        return self.pages[-1].terminal

    @property
    def manifest_checksum(self) -> str:
        manifest = ["wb-warehouse-stocks/v1", self.pages[0].request.checksum,
                    [[page.offset, page.request.page_limit, page.raw_checksum] for page in self.pages]]
        return hashlib.sha256(json.dumps(manifest, ensure_ascii=True,
                                         separators=(",", ":")).encode("ascii")).hexdigest()

    @property
    def received_business_date_msk(self) -> date | None:
        """Receipt-based daily eligibility only; never historical stock evidence."""
        try:
            days = {page.received_at.astimezone(ZoneInfo("Europe/Moscow")).date() for page in self.pages}
        except OverflowError:
            return None
        return next(iter(days)) if self.complete and len(days) == 1 else None
