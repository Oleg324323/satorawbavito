import hashlib
import json
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from app.modules.orders import (
    ExternalOrderIdentity,
    OrderContractValidationError,
    map_wb_statistics_status,
)
from app.orders.contracts import CatalogResolution, DeadlineEvidence, OrderReadRow
from app.orders.ingestion import OrderObservation
from app.orders.serialization import (
    EVIDENCE_SCHEMA_VERSION,
    OBSERVATION_CHECKSUM_VERSION,
    READ_PAYLOAD_SCHEMA_VERSION,
    observation_checksum,
    serialize_observation,
    serialize_read_row,
)
from tests.test_orders_ingestion import NOW, observation


def read_row():
    value = observation()
    return OrderReadRow(
        value,
        value.items[0].identity,
        3,
        CatalogResolution("unmapped", None, None, None, "synthetic-resolution-v1"),
        ("synthetic-unmapped",),
        (
            DeadlineEvidence(
                "synthetic-ship-by",
                NOW,
                None,
                None,
                None,
                "Europe/Moscow",
                "synthetic-source",
                NOW,
            ),
        ),
    )


def test_versions_and_exact_checksum_bytes_are_explicit():
    assert EVIDENCE_SCHEMA_VERSION == READ_PAYLOAD_SCHEMA_VERSION == 1
    assert OBSERVATION_CHECKSUM_VERSION == "orders-observation-v1"
    assert (
        observation_checksum(observation())
        == "791380c1d0b83de53ad16951d7c7c277e7ec87d278e5f9cc90e900eac96146a2"
    )
    payload = serialize_observation(observation())
    assert payload["evidence_schema_version"] == 1
    del payload["observation"]["observed_at"]
    payload["checksum_version"] = OBSERVATION_CHECKSUM_VERSION
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    assert observation_checksum(observation()) == hashlib.sha256(encoded).hexdigest()


def test_receipt_and_item_transport_order_do_not_change_replay():
    original = observation()
    incoming = replace(
        original, observed_at=NOW + timedelta(days=1), items=original.items[::-1]
    )
    assert observation_checksum(incoming) == observation_checksum(original)
    assert (
        serialize_observation(incoming)["observation"]["observed_at"]
        != serialize_observation(original)["observation"]["observed_at"]
    )


def test_equivalent_timezone_is_same_semantic_fact():
    value = observation()
    shifted = replace(value, effective_at=NOW.astimezone(timezone(timedelta(hours=3))))
    assert observation_checksum(shifted) == observation_checksum(value)
    assert (
        serialize_observation(shifted)["observation"]["effective_at"]
        == "2026-09-08T00:00:00.000000+00:00"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision": "synthetic-revision-2"},
        {"effective_at": None},
        {"adapter_version": "synthetic-adapter-v2"},
        {"source_kind": "avito-browser"},
    ],
)
def test_semantic_evidence_changes_checksum(change):
    assert observation_checksum(
        replace(observation(), **change)
    ) != observation_checksum(observation())


def test_quantity_and_tenant_account_are_in_checksum():
    value = observation()
    changed = replace(
        value, items=(replace(value.items[0], quantity=9), value.items[1])
    )
    assert observation_checksum(changed) != observation_checksum(value)
    assert observation_checksum(observation(account=1002)) != observation_checksum(
        value
    )


def test_payload_is_complete_json_and_detached_from_models():
    row = read_row()
    payload = serialize_read_row(row)
    assert payload["payload_schema_version"] == 1
    assert set(payload["row"]) == {
        "observation",
        "item_identity",
        "row_version",
        "resolution",
        "readiness_blockers",
        "deadlines",
    }
    assert payload["row"]["row_version"] == 3
    assert payload["row"]["resolution"]["catalog_sku_id"] is None
    assert payload["row"]["deadlines"][0]["computed_at"] is None
    assert (
        payload["row"]["observation"]
        == serialize_observation(row.observation)["observation"]
    )
    assert len(payload["row"]["observation"]["items"]) == 2
    assert json.loads(json.dumps(payload)) == payload
    payload["row"]["observation"]["items"].clear()
    assert len(serialize_read_row(row)["row"]["observation"]["items"]) == 2


@pytest.mark.parametrize(
    "serialize", [serialize_observation, serialize_read_row, observation_checksum]
)
def test_raw_dicts_are_not_a_serialization_escape_hatch(serialize):
    with pytest.raises(OrderContractValidationError):
        serialize({"buyer_phone": "synthetic-disallowed"})


def test_normalized_nulls_and_stable_line_evidence_are_not_dropped():
    result = serialize_observation(observation())["observation"]
    assert result["wb_is_cancelled"] is None
    assert result["wb_cancel_evidence_present"] is None
    assert set(result["items"][0]) == {
        "identity",
        "quantity",
        "stable_order_line_id",
        "stable_unit_id",
    }
    assert result["items"][0]["identity"]["occurrence_index"] == 0


def test_postgres_jsonb_incompatible_nul_fails_closed():
    value = replace(observation(), source_revision="synthetic\x00revision")
    with pytest.raises(OrderContractValidationError):
        serialize_observation(value)


@pytest.mark.parametrize("cancelled", [False, True])
def test_wb_nullable_status_preserves_explicit_cancellation_evidence(cancelled):
    value = OrderObservation(
        ExternalOrderIdentity(101, 1002, "wb", "000synthetic-order"),
        "wb-statistics-supplier-orders",
        "synthetic-adapter-v1",
        None,
        None,
        NOW,
        map_wb_statistics_status(None, cancelled, False),
        (),
        cancelled,
        False,
    )
    result = serialize_observation(value)["observation"]
    assert result["identity"]["external_order_id"] == "000synthetic-order"
    assert result["status"]["raw_status"] is None
    assert result["status"]["canonical_status"] == ("cancelled" if cancelled else None)
    assert result["wb_is_cancelled"] is cancelled
    assert result["wb_cancel_evidence_present"] is False
    assert result["source_revision"] is result["effective_at"] is None


def test_unknown_status_and_frozen_resolution_are_preserved():
    from app.modules.orders import map_avito_status

    row = read_row()
    unknown = replace(
        row,
        observation=replace(
            row.observation, status=map_avito_status("synthetic_unknown")
        ),
    )
    frozen = serialize_read_row(unknown)
    changed = replace(
        unknown,
        row_version=4,
        resolution=CatalogResolution(
            "manual_override", None, None, 201, "synthetic-v2"
        ),
    )
    assert serialize_read_row(changed) != frozen
    assert frozen["row"]["resolution"]["catalog_sku_id"] is None
    assert frozen["row"]["observation"]["status"]["canonical_status"] is None


def test_subclass_with_extra_fields_is_rejected():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class ExtendedObservation(OrderObservation):
        extra: str = "synthetic-disallowed"

    value = observation()
    extended = ExtendedObservation(
        **{name: getattr(value, name) for name in value.__dataclass_fields__}
    )
    with pytest.raises(OrderContractValidationError):
        serialize_observation(extended)
