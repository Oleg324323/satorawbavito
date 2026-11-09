"""Official-source boundary, using only synthetic source dictionaries."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.modules.orders import ExternalOrderIdentity, OrderContractValidationError
from app.orders.avito_status_refresh import (
    POLICY_VERSION,
    normalize_avito_order,
    source_descriptor,
)

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)
IDENTITY = ExternalOrderIdentity(91001, 91101, "avito", "000-synthetic-order")


def source_body():
    return {
        "hasMore": False,
        "orders": [
            {
                "id": IDENTITY.external_order_id,
                "createdAt": "2026-09-08T00:00:00Z",
                "updatedAt": "2026-09-09T00:00:00Z",
                "status": "on_confirmation",
                "items": [
                    {
                        "avitoId": "000-synthetic-listing",
                        "id": "synthetic-seller-sku",
                        "title": "Synthetic product",
                        "count": 2,
                        "prices": {"price": 100},
                    }
                ],
                "prices": {"total": 200},
                "delivery": {"serviceName": "Synthetic carrier"},
                "schedules": {},
            }
        ],
    }


def normalized(body):
    return normalize_avito_order(body, identity=IDENTITY, observed_at=NOW)


def test_official_source_preserves_identity_time_and_nonstatus_fingerprint():
    body = source_body()
    before = deepcopy(body)
    row = normalized(body)
    descriptor = source_descriptor(row)
    assert descriptor["policy"] == POLICY_VERSION == "avito-known-order-status-v1"
    assert descriptor["source_updated_at"] == "2026-09-09T00:00:00Z"
    assert row.effective_at == datetime(2026, 9, 9, tzinfo=UTC)
    assert row.status.canonical_status == "pending_confirmation"
    assert row.items[0].identity.external_item_id == "000-synthetic-listing"
    assert row.items[0].quantity == 2
    assert body == before
    body["orders"][0]["status"] = "synthetic-unknown"
    body["orders"][0]["updatedAt"] = "2026-09-09T01:00:00+00:00"
    newer = normalized(body)
    assert newer.status.raw_status == "synthetic-unknown"
    assert newer.status.canonical_status is None
    assert newer.status.mapping_state == "unmapped"
    assert (
        source_descriptor(newer)["nonstatus_sha256"] == descriptor["nonstatus_sha256"]
    )
    body["orders"][0]["prices"]["total"] = 201
    assert (
        source_descriptor(normalized(body))["nonstatus_sha256"]
        != descriptor["nonstatus_sha256"]
    )


@pytest.mark.parametrize("has_more", [True, None, 0, "false"])
def test_incomplete_or_coerced_terminal_evidence_rejected(has_more):
    body = source_body()
    body["hasMore"] = has_more
    with pytest.raises(OrderContractValidationError):
        normalized(body)


@pytest.mark.parametrize(
    "change",
    [
        "missing_terminal",
        "empty",
        "duplicate",
        "other_order",
        "numeric_id",
        "repeated_listing",
        "quantity_bool",
        "quantity_zero",
        "status_blank",
        "status_space",
        "naive_time",
        "invalid_time",
        "future_time",
    ],
)
def test_unproven_or_invalid_source_is_never_promotable(change):
    body = source_body()
    order = body["orders"][0]
    if change == "missing_terminal":
        del body["hasMore"]
    elif change == "empty":
        body["orders"] = []
    elif change == "duplicate":
        body["orders"].append(deepcopy(order))
    elif change == "other_order":
        order["id"] = "synthetic-other-order"
    elif change == "numeric_id":
        order["id"] = 1
    elif change == "repeated_listing":
        order["items"].append(deepcopy(order["items"][0]))
    elif change.startswith("quantity"):
        order["items"][0]["count"] = True if change == "quantity_bool" else 0
    elif change.startswith("status"):
        order["status"] = "" if change == "status_blank" else " delivered"
    else:
        order["updatedAt"] = {
            "naive_time": "2026-09-09T00:00:00",
            "invalid_time": "synthetic-not-time",
            "future_time": "2099-01-01T00:00:00Z",
        }[change]
    with pytest.raises(OrderContractValidationError):
        normalized(body)
