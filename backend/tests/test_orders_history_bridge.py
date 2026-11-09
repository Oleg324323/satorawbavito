"""Pure synthetic captured-page evidence, not a provider or publication test."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.modules.orders import (
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    make_wb_source_line_key,
    map_wb_statistics_status,
)
from app.orders.history_bridge import (
    ADAPTER,
    SOURCE,
    HistoryPageEvidence,
    decode_history_chunk,
)
from app.orders.ingestion import ObservedOrderItem, OrderObservation
from app.orders.serialization import observation_checksum, serialize_observation

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def page(count=1, **changes):
    value = HistoryPageEvidence(
        1,
        2,
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
        "00000000-0000-0000-0000-000000000004",
        1,
        1,
        "a" * 64,
        "b" * 64,
        "2026-09-09",
        "2026-09-10T00:00:00+00:00" if count else "2026-09-09",
        count,
        count == 0,
        NOW,
    )
    return replace(value, **changes)


def observation(
    srid="000synthetic-unit", org=1, account=2, cancelled=False, when=NOW, observed=NOW
):
    identity = ExternalOrderIdentity(org, account, "wb", srid)
    item = ObservedOrderItem(
        ExternalOrderItemIdentity(identity, make_wb_source_line_key(srid), "123", 0),
        1,
        stable_unit_id=srid,
    )
    return OrderObservation(
        identity,
        SOURCE,
        ADAPTER,
        when.isoformat(),
        when,
        observed,
        map_wb_statistics_status(None, cancelled, False),
        (item,),
        wb_is_cancelled=cancelled,
        wb_cancel_evidence_present=False,
    )


def row(value=None, ordinal=0):
    value = value or observation()
    return {
        "ordinal": ordinal,
        "srid": value.identity.external_order_id,
        "nm_id": 123,
        "barcode": "000synthetic-barcode",
        "semantic_checksum": observation_checksum(value),
        "source_row_checksum": "c" * 64,
        "observation": serialize_observation(value),
        "source_revision": value.source_revision,
        "effective_at": value.effective_at,
    }


def test_exact_partial_plan_preserves_identity_unknown_status_and_input_checksum():
    original = row()
    plan = decode_history_chunk(page=page(), first_ordinal=0, rows=(original,))
    assert plan.coverage_state == "partial" and plan.next_ordinal == 1
    assert plan.rows[0].comparison == "new"
    status = plan.rows[0].observation.status
    assert (
        status.raw_status is None
        and status.canonical_status is None
        and status.mapping_state == "unmapped"
    )
    assert plan.rows[0].observation.identity.external_order_id == "000synthetic-unit"
    assert plan == decode_history_chunk(
        page=page(), first_ordinal=0, rows=(deepcopy(original),)
    )
    different = dict(original, source_row_checksum="d" * 64)
    assert (
        plan.input_checksum
        != decode_history_chunk(
            page=page(), first_ordinal=0, rows=(different,)
        ).input_checksum
    )
    original["observation"]["observation"]["source_revision"] = "mutated"
    assert plan.rows[0].observation.source_revision == NOW.isoformat()


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("repeat", "replay"),
        ("cancel", "changed"),
        ("older", "out_of_order"),
        ("tie", "reconciliation_required"),
    ],
)
def test_boundary_comparison_uses_existing_semantics_and_never_advances_current(
    kind, expected
):
    previous = observation()
    incoming = observation(observed=NOW + timedelta(days=1))
    if kind != "repeat":
        when = (
            NOW + timedelta(hours=1)
            if kind == "cancel"
            else NOW - timedelta(hours=1)
            if kind == "older"
            else NOW
        )
        incoming = observation(cancelled=True, when=when)
    plan = decode_history_chunk(
        page=page(), first_ordinal=0, rows=(row(incoming),), previous=(previous,)
    )
    assert plan.rows[0].comparison == expected and plan.coverage_state == "partial"
    if kind != "repeat":
        assert plan.rows[0].observation.status.canonical_status == "cancelled"
    assert (
        plan.input_checksum
        == decode_history_chunk(
            page=page(), first_ordinal=0, rows=(row(incoming),)
        ).input_checksum
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("semantic_checksum", "a" * 64),
        ("ordinal", 1),
        ("nm_id", True),
        ("srid", "different"),
        ("source_revision", "different"),
        ("barcode", "bad\x00"),
        ("effective_at", NOW + timedelta(seconds=1)),
    ],
)
def test_corrupt_staged_row_is_rejected(field, value):
    data = row()
    data[field] = value
    with pytest.raises(ValueError):
        decode_history_chunk(page=page(), first_ordinal=0, rows=(data,))


def test_scope_collision_and_missing_stable_unit_are_not_repaired():
    other = observation(account=3)
    with pytest.raises(ValueError):
        decode_history_chunk(page=page(), first_ordinal=0, rows=(row(other),))
    accepted = decode_history_chunk(
        page=page(marketplace_account_id=3), first_ordinal=0, rows=(row(other),)
    )
    assert accepted.rows[0].observation.identity.marketplace_account_id == 3
    with pytest.raises(ValueError):
        decode_history_chunk(
            page=page(), first_ordinal=0, rows=(row(),), previous=(other,)
        )
    bad = row()
    bad["observation"]["observation"]["items"][0]["stable_unit_id"] = None
    with pytest.raises(ValueError):
        decode_history_chunk(page=page(), first_ordinal=0, rows=(bad,))


def test_page_boundaries_cannot_skip_rows_or_drop_inclusive_duplicates():
    with pytest.raises(ValueError):
        decode_history_chunk(page=page(2), first_ordinal=0, rows=(row(),))
    with pytest.raises(ValueError):
        decode_history_chunk(
            page=page(2), first_ordinal=0, rows=(row(), row(ordinal=1))
        )
    with pytest.raises(ValueError):
        decode_history_chunk(page=page(1001), first_ordinal=1, rows=(row(ordinal=1),))
    last = decode_history_chunk(
        page=page(1001), first_ordinal=1000, rows=(row(ordinal=1000),)
    )
    assert last.first_ordinal == 1000 and last.next_ordinal == 1001


def test_empty_terminal_page_preserves_partial_coverage_and_never_deletes():
    plan = decode_history_chunk(page=page(0), first_ordinal=0, rows=())
    assert (
        plan.rows == () and plan.coverage_state == "partial" and plan.next_ordinal == 0
    )
    with pytest.raises(ValueError):
        decode_history_chunk(
            page=page(0), first_ordinal=0, rows=(), previous=(observation(),)
        )


def test_chunk_checksum_uses_instant_not_connection_timezone():
    first = decode_history_chunk(page=page(), first_ordinal=0, rows=(row(),))
    offset = page(published_at=NOW.astimezone(timezone(timedelta(hours=3))))
    second = decode_history_chunk(page=offset, first_ordinal=0, rows=(row(),))
    assert first.input_checksum == second.input_checksum


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "staging"},
        {"terminal": True},
        {"credential_generation": 0},
        {"account_incarnation": True},
        {"row_count": 100001},
        {"published_at": NOW.replace(tzinfo=None)},
    ],
)
def test_unpublished_or_invalid_capture_is_rejected(changes):
    with pytest.raises(ValueError):
        page(**changes)
