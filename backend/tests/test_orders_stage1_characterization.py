from __future__ import annotations

import json
from copy import deepcopy
from itertools import permutations
from pathlib import Path

import pytest

from app.modules.orders import (
    CanonicalOrderStatus,
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    MappingState,
    Marketplace,
    OrderContractValidationError,
    make_avito_source_line_key,
    make_wb_source_line_key,
    map_avito_status,
    map_wb_statistics_status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "orders"


def _stage1(name: str) -> dict:
    fixture = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return fixture["stage1"]


def _order_identity(row: dict, marketplace: Marketplace) -> ExternalOrderIdentity:
    return ExternalOrderIdentity(
        organization_id=row["organizationId"],
        marketplace_account_id=row["marketplaceAccountId"],
        marketplace=marketplace,
        external_order_id=row["externalOrderId"],
    )


def test_avito_shared_ids_remain_distinct_across_orgs_and_accounts() -> None:
    rows = _stage1("avito_orders_synthetic.json")["scopedOrders"][:3]
    identities = {_order_identity(row, Marketplace.AVITO) for row in rows}

    assert {row["externalOrderId"] for row in rows} == {"synthetic-order-cross-scope"}
    assert {(row["organizationId"], row["marketplaceAccountId"]) for row in rows} == {
        (101, 1001),
        (101, 1002),
        (202, 2002),
    }
    assert len(identities) == 3


def test_avito_multi_item_quantity_and_repeated_listing_are_explicit() -> None:
    row = _stage1("avito_orders_synthetic.json")["scopedOrders"][0]
    items = row["items"]
    repeated = items[:2]
    keys = {
        make_avito_source_line_key(
            row["externalOrderId"],
            item["externalItemId"],
            item["occurrenceIndex"],
            item.get("stableOrderLineId"),
        )
        for item in repeated
    }

    assert len(items) == 3
    assert [item["quantity"] for item in items] == [2, 1, 3]
    assert len(keys) == 2


def test_leading_zero_ids_survive_identity_and_source_key_construction() -> None:
    avito_row = _stage1("avito_orders_synthetic.json")["scopedOrders"][3]
    avito_item = avito_row["items"][0]
    identity = _order_identity(avito_row, Marketplace.AVITO)
    line_key = make_avito_source_line_key(
        avito_row["externalOrderId"],
        avito_item["externalItemId"],
        avito_item["occurrenceIndex"],
    )
    wb_row = _stage1("wb_statistics_orders_synthetic.json")["scopedOrders"][3]

    assert identity.external_order_id == "000synthetic-order-leading"
    assert "000synthetic-listing-leading" in line_key
    assert make_wb_source_line_key(wb_row["stableUnitId"]).endswith(
        "000synthetic-srid-unit-leading"
    )


def test_missing_stable_line_evidence_remains_fail_closed() -> None:
    avito_case = _stage1("avito_orders_synthetic.json")["missingStableLineId"]
    avito_key = make_avito_source_line_key(
        avito_case["externalOrderId"],
        avito_case["externalItemId"],
        avito_case["occurrenceIndex"],
        avito_case["stableOrderLineId"],
    )
    wb_case = _stage1("wb_statistics_orders_synthetic.json")["missingStableUnitId"]

    assert "occurrence:2" in avito_key
    with pytest.raises(OrderContractValidationError):
        make_wb_source_line_key(wb_case["stableUnitId"])


def test_unknown_return_and_cancellation_statuses_keep_exact_evidence() -> None:
    avito_cases = _stage1("avito_orders_synthetic.json")["lifecycleCases"]
    mapped = [map_avito_status(case["rawStatus"]) for case in avito_cases]

    assert [value.raw_status for value in mapped] == [
        "synthetic_unknown_lifecycle",
        "on_return",
        "canceled",
    ]
    assert [value.canonical_status for value in mapped] == [
        None,
        CanonicalOrderStatus.RETURNING,
        CanonicalOrderStatus.CANCELLED,
    ]
    assert [value.mapping_state for value in mapped] == [
        MappingState.UNMAPPED,
        MappingState.MAPPED,
        MappingState.MAPPED,
    ]


def test_wb_statistics_rows_characterize_cancellation_without_readiness() -> None:
    rows = _stage1("wb_statistics_orders_synthetic.json")["lifecycleCases"]
    mapped = [
        map_wb_statistics_status(
            row["rawStatus"],
            is_cancelled=row["isCancelled"],
            cancel_evidence_present=row["cancelEvidencePresent"],
        )
        for row in rows
    ]

    assert [value.canonical_status for value in mapped] == [
        CanonicalOrderStatus.CANCELLED,
        CanonicalOrderStatus.CANCELLED,
        None,
    ]
    assert mapped[2].raw_status == "synthetic_unknown_wb_lifecycle"
    assert mapped[2].mapping_state is MappingState.UNMAPPED


@pytest.mark.parametrize(
    "fixture_name",
    ["avito_orders_synthetic.json", "wb_statistics_orders_synthetic.json"],
)
def test_partial_page_manifest_never_marks_absent_orders_cancelled(
    fixture_name: str,
) -> None:
    pagination = _stage1(fixture_name)["pagination"]

    assert pagination["manifestState"] == "partial"
    assert pagination["isComplete"] is False
    assert pagination["absentOrderIds"] == ["synthetic-order-absent-from-partial-page"]
    assert pagination["absenceMeansCancellation"] is False


@pytest.mark.parametrize(
    ("fixture_name", "marketplace"),
    [
        ("avito_orders_synthetic.json", Marketplace.AVITO),
        ("wb_statistics_orders_synthetic.json", Marketplace.WB),
    ],
)
def test_reordering_preserves_identity_while_source_change_is_observable(
    fixture_name: str,
    marketplace: Marketplace,
) -> None:
    replay = _stage1(fixture_name)["replay"]
    original = {
        _order_identity(row, marketplace): row["sourceRevision"]
        for row in replay["originalRows"]
    }
    reordered = {
        _order_identity(row, marketplace): row["sourceRevision"]
        for row in replay["reorderedRows"]
    }
    changed = {
        _order_identity(row, marketplace): row["sourceRevision"]
        for row in replay["changedRows"]
    }

    assert reordered == original
    assert set(changed) == set(original)
    assert changed != original


def _item_identity(order, item):
    return ExternalOrderItemIdentity(
        _order_identity(order, Marketplace.AVITO),
        make_avito_source_line_key(
            order["externalOrderId"],
            item["externalItemId"],
            item["occurrenceIndex"],
            item.get("stableOrderLineId"),
        ),
        item["externalItemId"],
        item["occurrenceIndex"],
    )


def test_explicit_occurrences_survive_every_multi_item_permutation():
    order = _stage1("avito_orders_synthetic.json")["scopedOrders"][0]
    original = {_item_identity(order, item) for item in order["items"]}
    assert len(original) == 3
    for reordered in permutations(order["items"]):
        assert {_item_identity(order, item) for item in reordered} == original


def test_quantity_and_description_changes_do_not_create_new_line_identity():
    order = _stage1("avito_orders_synthetic.json")["scopedOrders"][0]
    original = order["items"][0]
    changed = deepcopy(original)
    changed.update(quantity=7, title="synthetic-changed-title", color="synthetic-color")
    assert _item_identity(order, changed) == _item_identity(order, original)


def test_repeated_listing_cannot_acquire_occurrence_from_missing_input():
    order = _stage1("avito_orders_synthetic.json")["scopedOrders"][0]
    item = order["items"][0]
    with pytest.raises(OrderContractValidationError):
        make_avito_source_line_key(
            order["externalOrderId"], item["externalItemId"], None
        )


def test_wb_shared_units_remain_distinct_in_same_org_different_accounts():
    rows = _stage1("wb_statistics_orders_synthetic.json")["scopedOrders"][:3]
    identities = {
        ExternalOrderItemIdentity(
            _order_identity(row, Marketplace.WB),
            make_wb_source_line_key(row["stableUnitId"]),
            None,
            0,
        )
        for row in rows
    }
    assert len(identities) == 3
