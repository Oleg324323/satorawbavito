import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from app.modules.wb_source_requests import CollectionRequest, SourceKind
from app.modules.wb_stock_snapshots import (
    StockRun,
    StockSourceError,
    parse_warehouse_stock_page,
)

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)
REQUEST = CollectionRequest(7, 42, SourceKind.wb_warehouse, "wb-warehouse-stocks/v1", 3)


def parse(rows, **changes):
    kwargs = {"request": REQUEST, "offset": 0, "received_at": NOW, **changes}
    return parse_warehouse_stock_page(json.dumps({"data": rows}).encode(), **kwargs)


def row(**changes):
    return {"nmId": 101, "chrtId": 201, "warehouseId": 301, "quantity": 5, **changes}


def test_preserves_every_size_warehouse_grain_without_available_stock_formula():
    page = parse([row(), row(chrtId=202), row(warehouseId=302)])
    assert [r.identity for r in page.observations] == [(101, 201, 301), (101, 202, 301), (101, 201, 302)]
    assert not page.terminal
    assert not hasattr(page.observations[0], "available_units")
    assert page.source_observed_at is None
    with pytest.raises(FrozenInstanceError):
        page.offset = 3


def test_missing_null_and_explicit_zero_survive_without_stockcount_fallback():
    page = parse([{"nmId": 101, "warehouseId": 301, "stockCount": 999,
                   "inWayToClient": None, "inWayFromClient": 0}])
    value = page.observations[0]
    assert value.chrt_id is None
    assert (value.quantity.presence, value.quantity.value) == ("missing", None)
    assert (value.in_way_to_client.presence, value.in_way_to_client.value) == ("null", None)
    assert (value.in_way_from_client.presence, value.in_way_from_client.value) == ("value", 0)


@pytest.mark.parametrize("field,bad", [
    ("quantity", -1), ("quantity", True), ("quantity", 1.0), ("quantity", "1"),
    ("quantity", 2 ** 63), ("nmId", 0), ("nmId", None), ("nmId", True),
    ("warehouseId", None), ("warehouseId", "301"), ("chrtId", 0),
    ("stockType", "seller"), ("stockType", None),
])
def test_invalid_identity_or_count_rejects_entire_page(field, bad):
    with pytest.raises(StockSourceError):
        parse([row(**{field: bad})])


@pytest.mark.parametrize("raw", [b'{}', b'{"data":null}', b'{"data":[null]}',
                                 b'{"data":[],"error":true}', b'{"data":[],"errorText":"bad"}',
                                 b'{"data":[],"data":[null]}', b'{"data":{}}', b''])
def test_bad_or_error_wrapper_never_becomes_complete_empty_page(raw):
    with pytest.raises(StockSourceError):
        parse_warehouse_stock_page(raw, request=REQUEST, offset=0, received_at=NOW)


@pytest.mark.parametrize("rows", [[row(), row()], [row(), row(quantity=0)], [row()] * 4])
def test_duplicate_or_oversized_page_is_not_silently_collapsed(rows):
    with pytest.raises(StockSourceError):
        parse(rows)


def test_complete_manifest_requires_short_terminal_and_binds_original_bytes():
    first = parse([row(), row(chrtId=202), row(chrtId=203)])
    prefix = StockRun((first,))
    assert not prefix.complete and prefix.received_business_date_msk is None
    terminal = parse([], offset=3, received_at=NOW + timedelta(minutes=1))
    run = StockRun((first, terminal))
    assert run.complete and len(run.observations) == 3
    assert run.received_business_date_msk == date(2026, 9, 9)
    assert run.manifest_checksum != prefix.manifest_checksum
    raw = b'{ "data" : [] }'
    other = parse_warehouse_stock_page(raw, request=REQUEST, offset=3, received_at=terminal.received_at)
    assert StockRun((first, other)).manifest_checksum != run.manifest_checksum


@pytest.mark.parametrize("change", ["gap", "account", "time", "duplicate", "after_terminal", "nonzero_start"])
def test_run_rejects_mixed_or_noncontiguous_pages(change):
    first = parse([row(), row(chrtId=202), row(chrtId=203)])
    last = parse([], offset=3)
    pages = (first, last)
    if change == "gap":
        pages = (first, replace(last, offset=6))
    elif change == "account":
        pages = (first, replace(last, request=replace(REQUEST, marketplace_account_id=43)))
    elif change == "time":
        pages = (first, replace(last, received_at=NOW - timedelta(seconds=1)))
    elif change == "duplicate":
        pages = (first, parse([row()], offset=3))
    elif change == "after_terminal":
        pages = (parse([]), last)
    elif change == "nonzero_start":
        pages = (last,)
    with pytest.raises(StockSourceError):
        StockRun(pages)


def test_midnight_collection_does_not_fabricate_one_daily_observation():
    first = parse([row(), row(chrtId=202), row(chrtId=203)],
                  received_at=datetime(2026, 9, 9, 20, 59, tzinfo=UTC))
    last = parse([], offset=3, received_at=datetime(2026, 9, 9, 21, 1, tzinfo=UTC))
    run = StockRun((first, last))
    assert run.complete
    assert run.received_business_date_msk is None


@pytest.mark.parametrize("changes", [
    {"request": replace(REQUEST, source_kind=SourceKind.prices)},
    {"request": replace(REQUEST, parser_version="unknown")},
    {"offset": True}, {"received_at": NOW.replace(tzinfo=None)},
])
def test_explicit_parser_context_cannot_be_guessed(changes):
    with pytest.raises(StockSourceError):
        parse([], **changes)


def test_hydration_cannot_bypass_immutable_page_and_run_validation():
    page = parse([])
    for kwargs in ({"observations": []}, {"raw_checksum": "bad"}, {"offset": -1}):
        with pytest.raises(StockSourceError):
            replace(page, **kwargs)
    for pages in ([], (), (None,)):
        with pytest.raises(StockSourceError):
            StockRun(pages)


@pytest.mark.parametrize("field", ["organization_id", "marketplace_account_id"])
def test_stock_boundary_rejects_owner_outside_canonical_storage_range(field):
    with pytest.raises(StockSourceError):
        parse([], request=replace(REQUEST, **{field: 2 ** 63}))


def test_unrepresentable_local_date_is_not_daily_eligible():
    run = StockRun((parse([], received_at=datetime.max.replace(tzinfo=UTC)),))
    assert run.received_business_date_msk is None
