"""Pure source adapter fixtures; never call WB or declare durable completion."""

import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.modules.orders import CanonicalOrderStatus, MappingState
from app.orders.ingestion import compare_observations
from app.wb_live.statistics_orders import (
    HistoryOrderRow,
    OrdersPageEnd,
    OrdersPageError,
    iter_orders_page,
    orders_minimum_interval_seconds,
    orders_request,
)

NOW = datetime(2026, 9, 10, tzinfo=UTC)
CURSOR = "2026-09-01T00:00:00.12345"


def row(**changes):
    return {
        "srid": "native-unit",
        "nmId": 12,
        "isCancel": False,
        "lastChangeDate": "2026-09-02T00:00:00.12345",
        "barcode": "native-barcode",
        "cancelDate": "0001-01-01T00:00:00",
        **changes,
    }


def events(rows, *, cursor=CURSOR, **limits):
    raw = json.dumps(rows, ensure_ascii=False).encode()
    return list(
        iter_orders_page(
            (raw[index : index + 7] for index in range(0, len(raw), 7)),
            organization_id=1,
            marketplace_account_id=2,
            date_from=cursor,
            observed_at=NOW,
            **limits,
        )
    )


def test_exact_request_owner_rate_and_no_invented_pagination():
    request, manifest = orders_request(1, 2, CURSOR)
    assert request.method == "GET" and request.path == "/api/v1/supplier/orders"
    assert request.query == {"dateFrom": CURSOR, "flag": 0}
    assert request.jsonBody is None
    assert orders_request(1, 3, CURSOR)[1] != manifest
    assert orders_request(2, 2, CURSOR)[1] != manifest
    assert orders_minimum_interval_seconds() == 10800


def test_native_unit_identity_status_and_moscow_time():
    result, end = events([row()])
    assert isinstance(result, HistoryOrderRow) and isinstance(end, OrdersPageEnd)
    observation = result.observation
    assert observation.identity.external_order_id == "native-unit"
    assert observation.items[0].stable_unit_id == "native-unit"
    assert observation.items[0].quantity == 1
    assert (
        observation.items[0].identity.external_item_id == "12"
    )  # Product, not a size ID.
    assert observation.status.mapping_state == MappingState.UNMAPPED
    assert observation.status.canonical_status is None
    assert (
        observation.effective_at.astimezone(UTC).isoformat()
        == "2026-09-01T21:00:00.123450+00:00"
    )
    assert end.next_date_from == row()["lastChangeDate"] and not end.terminal
    assert end.row_count == 1


def test_cancellation_and_revision_reuse_existing_domain():
    original = events([row()])[0].observation
    replay = events([row()])[0].observation
    changed = events([row(isCancel=True, lastChangeDate="2026-09-03T00:00:00")])[
        0
    ].observation
    assert compare_observations(original, replay) == "replay"
    assert changed.status.canonical_status == CanonicalOrderStatus.CANCELLED
    assert compare_observations(original, changed) == "changed"


def test_empty_only_terminal_keeps_exact_cursor():
    (end,) = events([])
    assert end.terminal and end.next_date_from == CURSOR and end.row_count == 0
    assert end.raw_checksum == hashlib.sha256(b"[]").hexdigest()


def test_streaming_80000_rows_not_terminal_or_entire_history_buffer():
    consumed = []

    def chunks():
        yield b"["
        for number in range(80000):
            if number:
                yield b","
            consumed.append(number) if number < 2 else None
            yield json.dumps(row(srid=f"unit-{number}")).encode()
        yield b"]"

    iterator = iter_orders_page(
        chunks(),
        organization_id=1,
        marketplace_account_id=2,
        date_from=CURSOR,
        observed_at=NOW,
    )
    assert isinstance(next(iterator), HistoryOrderRow)
    assert consumed == [0]  # First output before requesting the second row.
    count, end = 1, None
    for event in iterator:
        if isinstance(event, OrdersPageEnd):
            end = event
        else:
            count += 1
    assert count == 80000 and end.row_count == count and not end.terminal


def test_same_cursor_provisional_rows_never_get_completion_marker():
    raw = json.dumps([row(lastChangeDate=CURSOR)]).encode()
    iterator = iter_orders_page(
        [raw],
        organization_id=1,
        marketplace_account_id=2,
        date_from=CURSOR,
        observed_at=NOW,
    )
    assert isinstance(next(iterator), HistoryOrderRow)
    with pytest.raises(OrdersPageError, match="WB_HISTORY_CURSOR_STALLED"):
        next(iterator)


@pytest.mark.parametrize(
    "changes",
    [
        {"srid": ""},
        {"srid": " x"},
        {"srid": None},
        {"nmId": 0},
        {"nmId": True},
        {"isCancel": None},
        {"isCancel": "false"},
        {"barcode": 0},
        {"lastChangeDate": "2026-02-30T12:00:00"},
        {"lastChangeDate": "2026-09-02T12:00:00.1234567"},
        {"cancelDate": "not-a-date"},
    ],
)
def test_invalid_native_fields_fail_closed(changes):
    with pytest.raises(OrdersPageError):
        events([row(**changes)])


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b"[",
        b"[,]",
        b"[{}]",
        b"[] true",
        b"[{},]",
        b'[{"srid":"a","srid":"b"}]',
        b"[NaN]",
        b"[\xff]",
        b"null",
    ],
)
def test_malformed_stream_has_no_end_marker(raw):
    with pytest.raises(OrdersPageError):
        for event in iter_orders_page(
            [raw],
            organization_id=1,
            marketplace_account_id=2,
            date_from=CURSOR,
            observed_at=NOW,
        ):
            assert not isinstance(event, OrdersPageEnd)


def test_duplicate_and_reversed_source_order():
    with pytest.raises(OrdersPageError, match="WB_HISTORY_DUPLICATE_IDENTITY"):
        events([row(), row()])
    with pytest.raises(OrdersPageError, match="WB_HISTORY_CURSOR_STALLED"):
        events([row(), row(srid="second", lastChangeDate=CURSOR)])


def test_split_utf8_and_raw_checksum():
    raw = json.dumps([row(srid="заказ")], ensure_ascii=False).encode()
    events_ = list(
        iter_orders_page(
            (bytes([byte]) for byte in raw),
            organization_id=1,
            marketplace_account_id=2,
            date_from=CURSOR,
            observed_at=NOW,
        )
    )
    assert events_[0].observation.identity.external_order_id == "заказ"
    assert events_[-1].raw_checksum == hashlib.sha256(raw).hexdigest()


def test_page_row_and_chunk_limits_are_explicit_errors():
    with pytest.raises(OrdersPageError, match="WB_HISTORY_LIMIT"):
        events([row()], max_page_bytes=10)
    with pytest.raises(OrdersPageError, match="WB_HISTORY_LIMIT"):
        events([row(), row(srid="second")], max_rows=1)
    with pytest.raises(OrdersPageError, match="WB_HISTORY_LIMIT"):
        list(
            iter_orders_page(
                [b" " * 65537],
                organization_id=1,
                marketplace_account_id=2,
                date_from=CURSOR,
                observed_at=NOW,
            )
        )


def test_generator_interruption_has_no_terminal_receipt():
    def broken():
        yield b"[" + json.dumps(row()).encode()
        raise OSError("synthetic interrupted upstream")

    iterator = iter_orders_page(
        broken(),
        organization_id=1,
        marketplace_account_id=2,
        date_from=CURSOR,
        observed_at=NOW,
    )
    assert isinstance(next(iterator), HistoryOrderRow)
    with pytest.raises(OSError):
        next(iterator)


@pytest.mark.parametrize("org,account", [(True, 2), (0, 2), (1, "external-account")])
def test_internal_ownership_required(org, account):
    with pytest.raises(OrdersPageError):
        orders_request(org, account, CURSOR)
