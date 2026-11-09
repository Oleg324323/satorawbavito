"""Page continuity uses actual receipt instants, preserving raw manifests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.modules.wb_price_snapshots import (
    PriceSourceValidationError,
    assemble_goods_price_run,
    parse_goods_price_page,
)
from app.modules.wb_source_requests import CollectionRequest, SourceKind
from app.modules.wb_stock_snapshots import (
    StockRun,
    StockSourceError,
    parse_warehouse_stock_page,
)


def pages(kind, first_time, last_time):
    if kind == "prices":
        args = {
            "organization_id": 7,
            "marketplace_account_id": 42,
            "limit": 1,
            "request_checksum": "a" * 64,
        }
        first = parse_goods_price_page(
            b'{"data":{"listGoods":[{"nmID":101,"sizes":[{"sizeID":201}]}]}}',
            offset=0,
            received_at=first_time,
            **args,
        )
        last = parse_goods_price_page(
            b'{"data":{"listGoods":[]}}', offset=1, received_at=last_time, **args
        )
    else:
        request = CollectionRequest(
            7, 42, SourceKind.wb_warehouse, "wb-warehouse-stocks/v1", 1
        )
        first = parse_warehouse_stock_page(
            b'{"data":[{"nmId":101,"warehouseId":301}]}',
            request=request,
            offset=0,
            received_at=first_time,
        )
        last = parse_warehouse_stock_page(
            b'{"data":[]}', request=request, offset=1, received_at=last_time
        )
    return first, last


def assemble(kind, values):
    return assemble_goods_price_run(values) if kind == "prices" else StockRun(values)


@pytest.mark.parametrize("kind", ["prices", "stocks"])
def test_fall_back_accepts_monotonic_instants_without_changing_raw_hashes(kind):
    zone = ZoneInfo("America/New_York")
    values = pages(
        kind,
        datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0),
        datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1),
    )
    normalized = tuple(
        replace(page, received_at=page.received_at.astimezone(UTC)) for page in values
    )
    result = assemble(kind, values)
    assert result.complete
    assert result.manifest_checksum == assemble(kind, normalized).manifest_checksum
    assert result.pages[0] is values[0] and result.pages[1] is values[1]


@pytest.mark.parametrize("kind", ["prices", "stocks"])
def test_fall_back_rejects_reversed_instants_even_when_wall_clock_increases(kind):
    zone = ZoneInfo("America/New_York")
    values = pages(
        kind,
        datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1),
        datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0),
    )
    with pytest.raises((PriceSourceValidationError, StockSourceError)):
        assemble(kind, values)


@pytest.mark.parametrize("kind", ["prices", "stocks"])
def test_receipt_outside_supported_utc_range_fails_closed(kind):
    edge = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises((PriceSourceValidationError, StockSourceError)):
        assemble(kind, pages(kind, edge, edge))
