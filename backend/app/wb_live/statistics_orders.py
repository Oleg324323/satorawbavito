"""Dormant streaming supplier/orders adapter; no transport or publication.

Official reports documentation checked 2026-09-10:
https://dev.wildberries.ru/docs/openapi/reports
Preliminary operational data, 90-day retention, possible unpaid-order omissions.
Not all orders, settlement evidence, current SPP, or final profitability.

Rows yielded before OrdersPageEnd are PROVISIONAL. A consumer must stage them
and atomically publish only after the end marker and its lease/identity guard.
An exception, crash or interrupted iteration is never page completion.
"""

import codecs
import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from app.modules.orders import (
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    Marketplace,
    make_wb_source_line_key,
    map_wb_statistics_status,
)
from app.orders.ingestion import ObservedOrderItem, OrderObservation
from app.wb_api.client import WbApiRequest


class OrdersPageError(ValueError):
    def __init__(self, code="WB_HISTORY_INVALID_PAGE"):
        super().__init__(
            code
            if code
            in {
                "WB_HISTORY_INVALID_PAGE",
                "WB_HISTORY_INVALID_REQUEST",
                "WB_HISTORY_LIMIT",
                "WB_HISTORY_DUPLICATE_IDENTITY",
                "WB_HISTORY_CURSOR_STALLED",
            }
            else "WB_HISTORY_INVALID_PAGE"
        )


@dataclass(frozen=True, slots=True)
class HistoryOrderRow:
    observation: OrderObservation
    nm_id: int
    barcode: str | None
    source_row_checksum: str


@dataclass(frozen=True, slots=True)
class OrdersPageEnd:
    next_date_from: str
    terminal: bool
    row_count: int
    raw_checksum: str
    request_checksum: str


def _identity(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise OrdersPageError("WB_HISTORY_INVALID_REQUEST")
    return value


def _text(value, maximum=512):
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise OrdersPageError()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise OrdersPageError() from None
    return value


def _instant(value, *, allow_date=False):
    # Native naive timestamps mean Moscow, never machine local time. Preserve
    # the exact wire string separately; do not increment or round a cursor.
    _text(value, 40)
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?"
    if allow_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T00:00:00"
    if re.fullmatch(pattern, value) is None:
        raise OrdersPageError()
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        raise OrdersPageError() from None
    return (
        instant
        if instant.utcoffset() is not None
        else instant.replace(tzinfo=timezone(timedelta(hours=3)))
    )


def orders_request(organization_id, marketplace_account_id, date_from):
    """Exact GET identity. No invented limit/dateTo or date cursor arithmetic."""
    _identity(organization_id)
    _identity(marketplace_account_id)
    _instant(date_from, allow_date=True)
    request = WbApiRequest(
        method="GET",
        path="/api/v1/supplier/orders",
        query={"dateFrom": date_from, "flag": 0},
    )
    manifest = json.dumps(
        {
            "schema": "wb-orders-read/v1",
            "organizationId": organization_id,
            "marketplaceAccountId": marketplace_account_id,
            "baseUrl": "https://statistics-api.wildberries.ru",
            "method": request.method,
            "path": request.path,
            "query": request.query,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return request, manifest


def orders_minimum_interval_seconds():
    # Unknown/base profile: one request per three hours. Faster entitlement
    # requires an independently verified policy, not parsing a token here.
    return 10800


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OrdersPageError()
        result[key] = value
    return result


def _constant(_value):
    raise OrdersPageError()


def _normalize(row, row_text, org, account, observed_at):
    if type(row) is not dict or type(row.get("isCancel")) is not bool:
        raise OrdersPageError()
    srid = _text(row.get("srid"))
    nm_id = _identity(row.get("nmId"))
    changed = _text(row.get("lastChangeDate"), 40)
    effective = _instant(changed)
    cancel_date = row.get("cancelDate")
    # WB uses year0001 for absent cancellation. Never treat a nonempty sentinel
    # as proof of cancellation, or infer delivered from isCancel=false.
    cancel_evidence = False
    if cancel_date not in (None, ""):
        cancelled_at = _instant(cancel_date)
        cancel_evidence = cancelled_at.year != 1
    barcode = row.get("barcode")
    if barcode is not None:
        if type(barcode) is not str:
            raise OrdersPageError()
        barcode = _text(barcode, 255) if barcode else None
    identity = ExternalOrderIdentity(org, account, Marketplace.WB, srid)
    item = ObservedOrderItem(
        ExternalOrderItemIdentity(
            identity, make_wb_source_line_key(srid), str(nm_id), 0
        ),
        quantity=1,
        stable_unit_id=srid,
    )
    observation = OrderObservation(
        identity=identity,
        source_kind="wb-statistics-supplier-orders",
        adapter_version="wb-statistics-orders-stream-v1",
        source_revision=changed,
        effective_at=effective,
        observed_at=observed_at,
        status=map_wb_statistics_status(None, row["isCancel"], cancel_evidence),
        items=(item,),
        wb_is_cancelled=row["isCancel"],
        wb_cancel_evidence_present=cancel_evidence,
    )
    return HistoryOrderRow(
        observation,
        nm_id,
        barcode,
        hashlib.sha256(row_text.encode("utf-8")).hexdigest(),
    )


def iter_orders_page(
    chunks: Iterable[bytes],
    *,
    organization_id: int,
    marketplace_account_id: int,
    date_from: str,
    observed_at: datetime,
    max_page_bytes=128 * 1024 * 1024,
    max_rows=100_000,
) -> Iterator[HistoryOrderRow | OrdersPageEnd]:
    """Bounded row-at-a-time UTF8/JSON decoding (64KiB chunks, 128KiB row).

    The provider's roughly80k-row page is not a completion threshold. There is
    no whole-history buffer; at most one row plus identity digests is retained.
    Cross-page inclusive-boundary dedup belongs to durable staging by srid and
    source revision/checksum; this adapter never guesses a later dateFrom.
    """
    _, manifest = orders_request(organization_id, marketplace_account_id, date_from)
    if (
        type(observed_at) is not datetime
        or observed_at.utcoffset() is None
        or type(max_page_bytes) is not int
        or not 1 <= max_page_bytes <= 128 * 1024 * 1024
        or type(max_rows) is not int
        or not 1 <= max_rows <= 100_000
    ):
        raise OrdersPageError("WB_HISTORY_INVALID_REQUEST")
    cursor = _instant(date_from, allow_date=True)
    decoder = json.JSONDecoder(
        object_pairs_hook=_pairs, parse_float=Decimal, parse_constant=_constant
    )
    utf8 = codecs.getincrementaldecoder("utf-8")()
    source = iter(chunks)
    exhausted = object()
    digest = hashlib.sha256()
    buffer, state, ended, total, count = "", "start", False, 0, 0
    next_cursor, last_instant, seen = date_from, cursor, set()
    while True:
        buffer = buffer.lstrip(" \t\r\n")
        need_more = not buffer
        if buffer:
            if state == "start":
                if buffer[0] != "[":
                    raise OrdersPageError()
                buffer, state = buffer[1:], "first"
                continue
            if state in {"first", "value"}:
                if state == "first" and buffer[0] == "]":
                    buffer, state = buffer[1:], "end"
                    continue
                if buffer[0] != "{":
                    raise OrdersPageError()
                try:
                    row, end = decoder.raw_decode(buffer)
                except json.JSONDecodeError:
                    need_more = True
                except (ValueError, RecursionError):
                    raise OrdersPageError() from None
                else:
                    row_text, buffer = buffer[:end], buffer[end:]
                    if len(row_text.encode("utf-8")) > 128 * 1024:
                        raise OrdersPageError("WB_HISTORY_LIMIT")
                    result = _normalize(
                        row,
                        row_text,
                        organization_id,
                        marketplace_account_id,
                        observed_at,
                    )
                    order_hash = hashlib.sha256(
                        result.observation.identity.external_order_id.encode()
                    ).digest()
                    if order_hash in seen:
                        raise OrdersPageError("WB_HISTORY_DUPLICATE_IDENTITY")
                    seen.add(order_hash)
                    count += 1
                    if count > max_rows:
                        raise OrdersPageError("WB_HISTORY_LIMIT")
                    instant = result.observation.effective_at.astimezone(UTC)
                    if instant < last_instant:
                        raise OrdersPageError("WB_HISTORY_CURSOR_STALLED")
                    last_instant, next_cursor = instant, row["lastChangeDate"]
                    state = "separator"
                    yield result
                    continue
            elif state == "separator":
                if buffer[0] not in ",]":
                    raise OrdersPageError()
                state = "value" if buffer[0] == "," else "end"
                buffer = buffer[1:]
                continue
            else:
                raise OrdersPageError()  # Trailing data after a valid JSON array.
        if ended:
            if state != "end" or buffer:
                raise OrdersPageError()
            if count and last_instant <= cursor:
                raise OrdersPageError("WB_HISTORY_CURSOR_STALLED")
            yield OrdersPageEnd(
                next_cursor,
                count == 0,
                count,
                digest.hexdigest(),
                hashlib.sha256(manifest).hexdigest(),
            )
            return
        if need_more:
            if len(buffer.encode("utf-8")) > 128 * 1024:
                raise OrdersPageError("WB_HISTORY_LIMIT")
            chunk = next(source, exhausted)
            if chunk is exhausted:
                ended = True
                chunk = b""
            elif type(chunk) is not bytes or len(chunk) > 65536:
                raise OrdersPageError("WB_HISTORY_LIMIT")
            total += len(chunk)
            if total > max_page_bytes:
                raise OrdersPageError("WB_HISTORY_LIMIT")
            digest.update(chunk)
            try:
                buffer += utf8.decode(chunk, final=ended)
            except UnicodeError:
                raise OrdersPageError() from None
