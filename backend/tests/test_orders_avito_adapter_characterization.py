from copy import deepcopy
from datetime import date

import httpx
import pytest

from app.avito.orders import (
    AvitoOrdersBrowserSnapshot,
    AvitoOrdersFetchRequest,
    LiveAvitoOrdersClient,
    merge_browser_snapshot_orders,
)
from app.modules.orders import OrderContractValidationError, map_avito_status


def raw_order():
    return {
        "id": "000synthetic-order",
        "accountId": "000synthetic-account",
        "status": "synthetic_new_status",
        "items": [
            {"id": "000synthetic-listing", "title": "synthetic-first", "quantity": 2},
            {"id": "000synthetic-listing", "title": "synthetic-second", "quantity": 3},
        ],
    }


def fetch(payload, page=1):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    before = deepcopy(payload)
    client = LiveAvitoOrdersClient(
        access_token="synthetic-token", base_url="https://synthetic.invalid"
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        result = client.fetch_orders(
            AvitoOrdersFetchRequest(dateFrom=date(2026, 9, 8), page=page),
            transport,
        )
    assert payload == before
    assert len(requests) == 1
    assert requests[0].url.params["page"] == str(page)
    return result


def test_adapter_preserves_multi_item_repeated_listing_quantity_and_string_ids():
    result = fetch({"orders": [raw_order()], "total": 1})
    assert result.status == "synced"
    row = result.orders[0]
    assert row.orderId == "000synthetic-order"
    assert row.accountId == "000synthetic-account"
    assert [item.itemId for item in row.items] == ["000synthetic-listing"] * 2
    assert [item.quantity for item in row.items] == [2, 3]
    assert map_avito_status(row.status).canonical_status is None


@pytest.mark.parametrize(
    "status,canonical",
    [
        ("canceled", "cancelled"),
        ("on_return", "returning"),
        ("in_dispute", "disputed"),
        ("synthetic_new_status", None),
    ],
)
def test_adapter_status_can_cross_existing_canonical_boundary(status, canonical):
    raw = raw_order()
    raw["status"] = status
    row = fetch({"orders": [raw]}).orders[0]
    assert row.status == status
    assert map_avito_status(row.status).canonical_status == canonical


def test_adapter_preserves_whitespace_but_canonical_boundary_rejects_it():
    raw = raw_order()
    raw["status"] = " ready_to_ship "
    row = fetch({"orders": [raw]}).orders[0]
    assert row.status == " ready_to_ship "
    with pytest.raises(OrderContractValidationError):
        map_avito_status(row.status)


def test_legacy_missing_status_becomes_unknown_not_canonical_ready():
    raw = raw_order()
    del raw["status"]
    row = fetch({"orders": [raw]}).orders[0]
    assert row.status == "unknown"
    assert map_avito_status(row.status).canonical_status is None


def test_partial_provider_page_does_not_fetch_remaining_pages():
    result = fetch({"orders": [raw_order()], "total": 50}, page=2)
    assert len(result.orders) == 1
    assert result.total == 50
    # Legacy 'synced' describes one request, not complete coverage.
    assert result.status == "synced"


def test_missing_total_falls_back_to_page_length_and_is_not_completion_evidence():
    result = fetch({"orders": [raw_order()]}, page=2)
    assert result.total == 1
    assert result.diagnostics["ordersCount"] == 1
    assert result.diagnostics["rawCount"] == 1
    assert "payloadKeys" not in result.diagnostics
    assert "requestParams" not in result.diagnostics


def test_item_reordering_and_quantity_changes_are_not_deduplicated_by_listing():
    raw = raw_order()
    original = fetch({"orders": [raw]}).orders[0]
    changed = deepcopy(raw)
    changed["items"].reverse()
    changed["items"][0]["quantity"] = 7
    result = fetch({"orders": [changed]}).orders[0]
    assert result.orderId == original.orderId
    assert [(item.title, item.quantity) for item in result.items] == [
        ("synthetic-second", 7),
        ("synthetic-first", 2),
    ]


def test_legacy_adapter_does_not_expose_stable_line_or_occurrence_evidence():
    raw = raw_order()
    raw["items"][0].update(orderLineId="synthetic-line", occurrenceIndex=0)
    item = fetch({"orders": [raw]}).orders[0].items[0].model_dump()
    assert "orderLineId" not in item
    assert "occurrenceIndex" not in item


def test_legacy_zero_quantity_is_replaced_with_one_before_rendering():
    raw = raw_order()
    raw["items"][0]["quantity"] = 0
    assert fetch({"orders": [raw]}).orders[0].items[0].quantity == 1


def test_browser_merge_reuses_first_listing_match_for_repeated_lines():
    rows = fetch({"orders": [raw_order()]}).orders
    snapshot = AvitoOrdersBrowserSnapshot.model_validate(
        {
            "orders": [
                {
                    "orderId": "000synthetic-order",
                    "items": [
                        {"itemId": "000synthetic-listing", "quantity": 9},
                        {"itemId": "000synthetic-listing", "quantity": 4},
                    ],
                }
            ],
        }
    )
    merge_browser_snapshot_orders(rows, snapshot)
    assert [item.quantity for item in rows[0].items] == [9, 9]
