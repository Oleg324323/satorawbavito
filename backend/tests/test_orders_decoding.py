from copy import deepcopy

import pytest

from app.modules.orders import OrderContractValidationError
from app.orders.serialization import (
    deserialize_observation,
    deserialize_read_row,
    serialize_observation,
    serialize_read_row,
)
from tests.test_orders_ingestion import observation
from tests.test_orders_serialization import read_row


@pytest.mark.parametrize(
    "model,encode,decode",
    [
        (observation, serialize_observation, deserialize_observation),
        (read_row, serialize_read_row, deserialize_read_row),
    ],
)
def test_storage_round_trip_is_lossless_and_does_not_mutate(model, encode, decode):
    original = model()
    payload = encode(original)
    before = deepcopy(payload)
    assert decode(payload) == original
    assert payload == before
    assert encode(decode(payload)) == payload


@pytest.mark.parametrize("version", [True, 1.0, "1", 0, 2, None])
def test_unknown_or_coerced_version_is_rejected(version):
    payload = serialize_observation(observation())
    payload["evidence_schema_version"] = version
    with pytest.raises(OrderContractValidationError):
        deserialize_observation(payload)


@pytest.mark.parametrize(
    "path,value",
    [
        (("observation", "items", 0, "quantity"), True),
        (("observation", "items", 0, "quantity"), "2"),
        (
            ("observation", "items", 0, "identity", "source_line_key"),
            "synthetic-forged",
        ),
        (
            (
                "observation",
                "items",
                0,
                "identity",
                "order_identity",
                "marketplace_account_id",
            ),
            999,
        ),
        (("observation", "status", "canonical_status"), "cancelled"),
        (("observation", "status", "mapping_version"), "synthetic-forged"),
        (("observation", "observed_at"), "2026-09-08"),
        (("observation", "observed_at"), 1),
        (("observation", "observed_at"), "2026-09-08T03:00:00+03:00"),
        (("observation", "items"), {}),
    ],
)
def test_corrupt_observation_is_not_coerced_or_repaired(path, value):
    payload = serialize_observation(observation())
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(OrderContractValidationError):
        deserialize_observation(payload)


@pytest.mark.parametrize(
    "path",
    [(), ("observation",), ("observation", "identity"), ("observation", "items", 0)],
)
@pytest.mark.parametrize("operation", ["extra", "missing"])
def test_every_object_requires_exact_keys(path, operation):
    payload = serialize_observation(observation())
    target = payload
    for part in path:
        target = target[part]
    if operation == "extra":
        target["synthetic_extra"] = "forbidden"
    else:
        target.pop(next(iter(target)))
    with pytest.raises(OrderContractValidationError):
        deserialize_observation(payload)


def test_invalid_frozen_resolution_and_row_identity_fail_closed():
    payload = serialize_read_row(read_row())
    payload["row"]["resolution"]["catalog_sku_id"] = 123
    with pytest.raises(OrderContractValidationError):
        deserialize_read_row(payload)
    payload = serialize_read_row(read_row())
    payload["row"]["item_identity"]["source_line_key"] = "synthetic-other"
    with pytest.raises(OrderContractValidationError):
        deserialize_read_row(payload)
