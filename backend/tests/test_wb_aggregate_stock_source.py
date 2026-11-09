import json
from dataclasses import FrozenInstanceError
from datetime import timedelta

import httpx
import pytest

from app.modules.wb_aggregate_stock_source import (
    AggregateStockRequest,
    WbAggregateStockProvider,
    parse_aggregate_stock_page,
)
from app.modules.wb_source_requests import CollectionRequest, SourceKind
from app.wb_live.provider import WbReadError
from tests.test_wb_live_history_worker import NOW, credential

REQUEST = AggregateStockRequest(1, 2, 10)


def row():
    return {
        "nmId": 123,
        "chrtId": 456,
        "warehouseId": -999999,
        "warehouseName": "Склад WB",
        "regionName": "Склад WB",
        "quantity": 0,
        "inWayToClient": 0,
        "inWayFromClient": 0,
    }


def parse(rows):
    return parse_aggregate_stock_page(
        json.dumps({"data": {"items": rows}}).encode(),
        request=REQUEST,
        offset=0,
        received_at=NOW,
    )


def test_actual_grain_presence_and_uint64_without_warehouse_invention():
    data = row()
    data["quantity"] = 2**64 - 1
    data["inWayToClient"] = None
    del data["inWayFromClient"]
    page = parse([data])
    item = page.items[0]
    assert item.identity == (123, 456, -999999)
    assert item.quantity.value == 2**64 - 1
    assert item.in_way_to_client.presence == "null"
    assert item.in_way_from_client.presence == "missing"
    assert not page.terminal and page.next_offset == 10
    with pytest.raises(FrozenInstanceError):
        item.nm_id = 1


def test_new_collection_identity_does_not_relabel_old_warehouse_contract():
    old = CollectionRequest(1, 2, SourceKind.wb_warehouse, "wb-warehouse-stocks/v1", 10)
    assert old.checksum != REQUEST.checksum
    assert b"stockType" not in REQUEST.canonical_bytes
    assert AggregateStockRequest(1, 3, 10).checksum != REQUEST.checksum
    assert REQUEST.page_body(20) == b'{"limit":10,"offset":20}'


@pytest.mark.parametrize(
    "field,value",
    [
        ("nmId", True),
        ("chrtId", None),
        ("warehouseId", 123),
        ("warehouseId", True),
        ("warehouseName", "invented"),
        ("quantity", -1),
        ("quantity", 2**64),
        ("quantity", 1.0),
    ],
)
def test_invalid_identity_or_quantity_rejected(field, value):
    data = row()
    data[field] = value
    with pytest.raises(WbReadError):
        parse([data])


def test_duplicate_identity_old_wrapper_and_duplicate_keys_rejected():
    with pytest.raises(WbReadError):
        parse([row(), row()])
    for raw in (b'{"data":[]}', b'{"data":{"items":[],"items":[]}}'):
        with pytest.raises(WbReadError):
            parse_aggregate_stock_page(raw, request=REQUEST, offset=0, received_at=NOW)


def test_empty_page_and_204_are_eof_not_historical_evidence():
    for status, raw in ((200, b'{"data":{"items":[]}}'), (204, b"")):
        page = parse_aggregate_stock_page(
            raw, request=REQUEST, offset=20, received_at=NOW, http_status=status
        )
        assert page.terminal and page.next_offset is None
        assert page.http_status == status
    assert not hasattr(page, "business_date")


def test_real_adapter_exact_body_one_request_and_immutable_typed_result():
    calls, admitted = [], []

    def handle(request):
        calls.append(request)
        assert (
            request.method == "POST"
            and request.url.host == "seller-analytics-api.wildberries.ru"
        )
        assert request.content == REQUEST.page_body(0)
        return httpx.Response(200, json={"data": {"items": [row()]}})

    def admit(*args):
        admitted.append(args)
        return True

    provider = WbAggregateStockProvider(
        record_cooldown=lambda *args: True,
        admit_request=admit,
        transport=httpx.MockTransport(handle),
        clock=lambda: NOW,
    )
    page = provider.read_page(request=REQUEST, offset=0, credential=credential())
    assert len(calls) == 1 and admitted == [(1, 2, 20)]
    assert page.items[0].quantity.value == 0
    assert page.next_not_before == NOW + timedelta(seconds=20)


@pytest.mark.parametrize("status", [302, 401, 402, 429, 500])
def test_no_internal_retry_or_redirect(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "https://example.org", "Retry-After": "60"}
        )

    provider = WbAggregateStockProvider(
        record_cooldown=lambda *args: True,
        admit_request=lambda *a: True,
        transport=httpx.MockTransport(handle),
        clock=lambda: NOW,
    )
    with pytest.raises(WbReadError) as error:
        provider.read_page(request=REQUEST, offset=0, credential=credential())
    assert len(calls) == 1 and error.value.retry_after_seconds == 60


def test_scope_and_denied_admission_never_http():
    calls = []
    provider = WbAggregateStockProvider(
        record_cooldown=lambda *args: True,
        admit_request=lambda *a: False,
        transport=httpx.MockTransport(lambda r: calls.append(r)),
    )
    for cred in (credential(), credential(9)):
        with pytest.raises(WbReadError):
            provider.read_page(request=REQUEST, offset=0, credential=cred)
    assert not calls


def test_throttle_headers_persist_before_broken_stream():
    events = []

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            events.append("body")
            raise httpx.ReadError("synthetic broken stream")
            yield b""  # pragma: no cover

    def cooldown(org, account, deadline):
        events.append((org, account, deadline))
        return True

    provider = WbAggregateStockProvider(
        admit_request=lambda *args: True,
        record_cooldown=cooldown,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, headers={"Retry-After": "60"}, stream=BrokenStream()
            )
        ),
        clock=lambda: NOW,
    )
    with pytest.raises(WbReadError) as error:
        provider.read_page(request=REQUEST, offset=0, credential=credential())
    assert events == [(1, 2, NOW + timedelta(seconds=60)), "body"]
    assert error.value.__context__ is None
