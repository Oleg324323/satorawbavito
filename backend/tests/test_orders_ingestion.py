from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.orders import (
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    OrderContractValidationError,
    make_avito_source_line_key,
    map_avito_status,
)
from app.orders.ingestion import (
    ObservedOrderItem,
    OrderManifest,
    OrderObservation,
    OrderPage,
    compare_observations,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    "previous_fold,incoming_fold,expected", [(0, 1, "changed"), (1, 0, "out_of_order")]
)
def test_observation_order_uses_instants_during_repeated_local_hour(
    previous_fold, incoming_fold, expected
):
    local = datetime(2024, 10, 27, 2, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    previous = replace(
        observation(),
        source_revision=None,
        effective_at=local.replace(fold=previous_fold),
    )
    incoming = replace(previous, effective_at=local.replace(fold=incoming_fold))
    assert previous.effective_at.utcoffset() != incoming.effective_at.utcoffset()
    assert compare_observations(previous, incoming) == expected


def observation(order_id="synthetic-order", account=1001):
    identity = ExternalOrderIdentity(101, account, "avito", order_id)
    items = tuple(
        ObservedOrderItem(
            ExternalOrderItemIdentity(
                identity,
                make_avito_source_line_key(order_id, "synthetic-listing", i),
                "synthetic-listing",
                i,
            ),
            quantity=i + 1,
        )
        for i in range(2)
    )
    return OrderObservation(
        identity,
        "avito-order-management",
        "synthetic-adapter-v1",
        "synthetic-revision-1",
        NOW,
        NOW,
        map_avito_status("ready_to_ship"),
        items,
    )


def manifest(pages=None, **changes):
    value = OrderManifest(
        organization_id=101,
        marketplace_account_id=1001,
        marketplace="avito",
        source_kind="avito-order-management",
        adapter_version="synthetic-adapter-v1",
        source_contract_version="synthetic-pagination-v1",
        source_snapshot="synthetic-snapshot",
        coverage_state="complete",
        expected_order_count=1,
        pages=(OrderPage(1, "synthetic-snapshot", True, (observation(),)),)
        if pages is None
        else pages,
    )
    return replace(value, **changes)


def test_complete_manifest_accepts_exact_single_page_and_is_frozen():
    value = manifest()
    assert value.coverage_state == "complete"
    assert len(value.observations) == 1
    with pytest.raises(FrozenInstanceError):
        value.coverage_state = "partial"


def test_complete_empty_manifest_is_explicit():
    value = OrderManifest(
        101,
        1001,
        "avito",
        "avito-order-management",
        "synthetic-adapter-v1",
        "synthetic-pagination-v1",
        "synthetic-snapshot",
        "complete",
        0,
        (OrderPage(1, "synthetic-snapshot", True, ()),),
    )
    assert value.observations == ()


@pytest.mark.parametrize(
    "pages",
    [
        (),
        (OrderPage(2, "synthetic-snapshot", True, (observation(),)),),
        (OrderPage(1, "synthetic-snapshot", False, (observation(),)),),
        (OrderPage(1, "synthetic-drift", True, (observation(),)),),
        (OrderPage(1, "synthetic-snapshot", True, (observation(account=1002),)),),
        (OrderPage(1, "synthetic-snapshot", True, (observation(), observation())),),
    ],
)
def test_complete_manifest_rejects_missing_drifted_or_duplicate_evidence(pages):
    with pytest.raises(OrderContractValidationError):
        manifest(pages)


def test_partial_manifest_cannot_be_promoted_without_terminal_and_counts():
    value = OrderManifest(
        101,
        1001,
        "avito",
        "avito-order-management",
        "synthetic-adapter-v1",
        "synthetic-pagination-v1",
        "synthetic-snapshot",
        "partial",
        None,
        (OrderPage(1, "synthetic-snapshot", False, (observation(),)),),
    )
    assert value.coverage_state == "partial"
    with pytest.raises(OrderContractValidationError):
        replace(value, coverage_state="complete")


def test_complete_manifest_accepts_pages_in_transport_arrival_order():
    one, two = observation(), observation("synthetic-order-2")
    value = OrderManifest(
        101,
        1001,
        "avito",
        "avito-order-management",
        "synthetic-adapter-v1",
        "synthetic-pagination-v1",
        "synthetic-snapshot",
        "complete",
        2,
        (
            OrderPage(2, "synthetic-snapshot", True, (two,)),
            OrderPage(1, "synthetic-snapshot", False, (one,)),
        ),
    )
    assert value.observations == (one, two)


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", True),
        ("marketplace_account_id", 0),
        ("marketplace", "WB"),
        ("expected_order_count", True),
        ("expected_order_count", 2),
        ("source_contract_version", " "),
        ("coverage_state", "ready"),
    ],
)
def test_manifest_rejects_invalid_contract_fields(field, value):
    with pytest.raises(OrderContractValidationError):
        manifest(**{field: value})


def test_observation_rejects_duplicate_keys_even_with_different_listing_metadata():
    row = observation()
    first = ObservedOrderItem(
        replace(
            row.items[0].identity,
            source_line_key=make_avito_source_line_key(
                row.identity.external_order_id,
                "synthetic-listing",
                0,
                "synthetic-stable-line",
            ),
        ),
        1,
        stable_order_line_id="synthetic-stable-line",
    )
    duplicate = replace(
        first,
        identity=replace(first.identity, external_item_id="synthetic-other-listing"),
    )
    with pytest.raises(OrderContractValidationError):
        replace(row, items=(first, duplicate))


def test_observation_rejects_item_from_other_order():
    with pytest.raises(OrderContractValidationError):
        replace(observation(), items=observation("synthetic-other-order").items)


@pytest.mark.parametrize("quantity", [0, -1, True, "1"])
def test_quantity_is_strict_positive_integer(quantity):
    with pytest.raises(OrderContractValidationError):
        replace(observation().items[0], quantity=quantity)


def test_comparison_ignores_receipt_time_and_item_arrival_order():
    previous = observation()
    incoming = replace(
        previous, observed_at=NOW + timedelta(days=1), items=previous.items[::-1]
    )
    assert compare_observations(previous, incoming) == "replay"


def test_changed_quantity_is_changed_evidence_with_same_identity():
    previous = observation()
    incoming = replace(
        previous,
        items=(replace(previous.items[0], quantity=7), previous.items[1]),
        source_revision="synthetic-revision-2",
        effective_at=NOW + timedelta(seconds=1),
    )
    assert compare_observations(previous, incoming) == "changed"


def test_older_source_evidence_is_retained_as_out_of_order():
    previous = observation()
    incoming = replace(
        previous,
        effective_at=NOW - timedelta(seconds=1),
        source_revision="synthetic-revision-2",
    )
    assert compare_observations(previous, incoming) == "out_of_order"


def test_opaque_revisions_and_equal_timestamps_require_reconciliation():
    previous = observation()
    assert (
        compare_observations(
            previous, replace(previous, source_revision="synthetic-revision-999")
        )
        == "reconciliation_required"
    )
    assert (
        compare_observations(previous, replace(previous, effective_at=None))
        == "reconciliation_required"
    )


def test_comparison_rejects_different_accounts_and_sources():
    with pytest.raises(OrderContractValidationError):
        compare_observations(observation(), observation(account=1002))
    with pytest.raises(OrderContractValidationError):
        compare_observations(
            observation(), replace(observation(), source_kind="avito-browser")
        )


def test_naive_timestamps_and_mutable_nested_values_are_rejected():
    with pytest.raises(OrderContractValidationError):
        replace(observation(), observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(OrderContractValidationError):
        replace(observation(), items=list(observation().items))


def test_checksum_is_stable_for_reordered_items_and_receipt_time():
    original = manifest()
    incoming = replace(
        observation(),
        observed_at=NOW + timedelta(hours=2),
        items=observation().items[::-1],
    )
    retry = manifest((OrderPage(1, "synthetic-snapshot", True, (incoming,)),))
    assert len(original.checksum) == 64
    assert retry.checksum == original.checksum


def test_checksum_changes_for_quantity_source_revision_scope_and_coverage():
    original = manifest()
    changed_row = replace(
        observation(), items=(replace(observation().items[0], quantity=8),)
    )
    changed = manifest((OrderPage(1, "synthetic-snapshot", True, (changed_row,)),))
    assert changed.checksum != original.checksum
    assert replace(original, coverage_state="partial").checksum != original.checksum
    source_change = replace(observation(), source_revision="synthetic-revision-2")
    assert (
        manifest((OrderPage(1, "synthetic-snapshot", True, (source_change,)),)).checksum
        != original.checksum
    )


def test_source_and_status_cannot_be_relabeled_as_another_marketplace():
    with pytest.raises(OrderContractValidationError):
        replace(observation(), source_kind="wb-statistics-supplier-orders")
    with pytest.raises(OrderContractValidationError):
        replace(
            observation(),
            status=replace(
                map_avito_status("ready_to_ship"), canonical_status="accepted"
            ),
        )


def test_adapter_version_change_requires_explicit_reconciliation():
    assert (
        compare_observations(
            observation(),
            replace(
                observation(),
                adapter_version="synthetic-adapter-v2",
                effective_at=NOW + timedelta(days=1),
            ),
        )
        == "reconciliation_required"
    )


def test_reused_revision_with_changed_payload_is_not_an_automatic_update():
    original = observation()
    incoming = replace(
        original,
        effective_at=NOW + timedelta(days=1),
        status=map_avito_status("delivered"),
    )
    assert compare_observations(original, incoming) == "reconciliation_required"


def test_gap_early_terminal_and_total_drift_fail_complete_manifest():
    first = OrderPage(1, "synthetic-snapshot", True, (observation(),))
    last = OrderPage(3, "synthetic-snapshot", True, (observation("synthetic-next"),))
    with pytest.raises(OrderContractValidationError):
        manifest((first, last))
    with pytest.raises(OrderContractValidationError):
        manifest((first, replace(last, number=2)))


def test_wb_statistics_observation_still_cannot_claim_fulfillment_ready():
    from app.modules.orders import map_wb_statistics_status

    identity = ExternalOrderIdentity(101, 1001, "wb", "synthetic-wb-order")
    row = OrderObservation(
        identity,
        "wb-statistics-supplier-orders",
        "synthetic-adapter-v1",
        None,
        NOW,
        NOW,
        map_wb_statistics_status(None, False, False),
        (),
        wb_is_cancelled=False,
        wb_cancel_evidence_present=False,
    )
    assert row.status.canonical_status is None
    with pytest.raises(OrderContractValidationError):
        replace(row, status=map_avito_status("ready_to_ship"))
    with pytest.raises(OrderContractValidationError):
        replace(row, status=map_wb_statistics_status(None, True, False))
    with pytest.raises(OrderContractValidationError):
        replace(row, wb_is_cancelled=None)
    with pytest.raises(OrderContractValidationError):
        replace(row, wb_is_cancelled="false")


def test_avito_observation_rejects_wb_cancellation_fields():
    with pytest.raises(OrderContractValidationError):
        replace(observation(), wb_is_cancelled=True)


def test_empty_manifest_still_validates_source_marketplace_binding():
    with pytest.raises(OrderContractValidationError):
        OrderManifest(
            101,
            1001,
            "wb",
            "avito-order-management",
            "synthetic-adapter-v1",
            "synthetic-pagination-v1",
            "synthetic-snapshot",
            "complete",
            0,
            (OrderPage(1, "synthetic-snapshot", True, ()),),
        )


def test_item_key_must_match_explicit_source_identity_evidence():
    with pytest.raises(OrderContractValidationError):
        replace(
            observation().items[0],
            identity=replace(
                observation().items[0].identity,
                source_line_key="synthetic-fabricated-key",
            ),
        )
    from app.modules.orders import make_wb_source_line_key

    identity = ExternalOrderItemIdentity(
        ExternalOrderIdentity(101, 1001, "wb", "synthetic-wb-order"),
        make_wb_source_line_key("synthetic-srid"),
        None,
        0,
    )
    with pytest.raises(OrderContractValidationError):
        ObservedOrderItem(identity, 1)
    item = ObservedOrderItem(identity, 1, stable_unit_id="synthetic-srid")
    assert item.identity == identity
    with pytest.raises(OrderContractValidationError):
        replace(item, stable_unit_id="synthetic-different-unit")
