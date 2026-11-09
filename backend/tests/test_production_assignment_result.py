from dataclasses import FrozenInstanceError, replace

import pytest

from app.modules import production


def payload():
    return {
        "work_item_id": 2**53 + 1,
        "version": 2,
        "catalog_sku_id": 1,
        "required_quantity": 3,
        "planned_quantity": 0,
        "remaining_quantity": 3,
        "source_item_version": 1,
    }


def test_assignment_receipt_result_is_exact_frozen_and_lossless():
    raw = payload()
    value = production.deserialize_assignment_result(raw, schema_version=1)
    assert value.work_item_id == 2**53 + 1
    assert production.serialize_assignment_result(value) == raw
    with pytest.raises(FrozenInstanceError):
        value.version = 3
    raw["version"] = 99
    assert value.version == 2


@pytest.mark.parametrize("field", tuple(payload()))
@pytest.mark.parametrize("bad", [None, True, 1.0, "1", -1])
def test_assignment_result_rejects_noninteger_and_negative_fields(field, bad):
    with pytest.raises(ValueError):
        production.deserialize_assignment_result(
            payload() | {field: bad}, schema_version=1
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 1},
        {"required_quantity": 0},
        {"source_item_version": 0},
        {"work_item_id": 0},
        {"catalog_sku_id": 0},
        {"planned_quantity": 4},
        {"remaining_quantity": 2},
        {"work_item_id": 2**63},
        {"version": 2**63},
        {"source_item_version": 2**63},
        {"required_quantity": 2**31},
        {"catalog_sku_id": 2**31},
        {"actor_membership_id": 1},
    ],
)
def test_assignment_result_rejects_invalid_bounds_equation_and_extra_fields(changes):
    with pytest.raises(ValueError):
        production.deserialize_assignment_result(payload() | changes, schema_version=1)


@pytest.mark.parametrize("version", [None, True, "1", 1.0, 2])
def test_assignment_result_schema_is_explicit_and_exact(version):
    with pytest.raises(ValueError):
        production.deserialize_assignment_result(payload(), schema_version=version)


@pytest.mark.parametrize("raw", [None, [], b"{}", "{}"])
def test_assignment_result_requires_decoded_jsonb_object(raw):
    with pytest.raises(ValueError):
        production.deserialize_assignment_result(raw, schema_version=1)


def test_assignment_result_serializer_requires_typed_result():
    with pytest.raises(ValueError):
        production.serialize_assignment_result(payload())


@pytest.mark.parametrize("field", tuple(payload()))
def test_assignment_result_requires_every_field(field):
    raw = payload()
    raw.pop(field)
    with pytest.raises(ValueError):
        production.deserialize_assignment_result(raw, schema_version=1)


def test_assignment_result_maximum_physical_values_roundtrip():
    raw = payload() | {
        "work_item_id": 2**63 - 1,
        "version": 2**63 - 1,
        "source_item_version": 2**63 - 1,
        "catalog_sku_id": 2**31 - 1,
        "required_quantity": 2**31 - 1,
        "remaining_quantity": 2**31 - 1,
    }
    assert (
        production.serialize_assignment_result(
            production.deserialize_assignment_result(raw, schema_version=1)
        )
        == raw
    )


@pytest.mark.parametrize(
    "field,maximum",
    [
        ("work_item_id", 2**63 - 1),
        ("expected_version", 2**63 - 1),
        ("catalog_sku_id", 2**31 - 1),
    ],
)
def test_assignment_command_rejects_out_of_storage_range(field, maximum):
    command = production.AssignmentCommand(1, 1, "synthetic-key", 1, "synthetic-reason")
    assert getattr(replace(command, **{field: maximum}), field) == maximum
    with pytest.raises(ValueError):
        replace(command, **{field: maximum + 1})
