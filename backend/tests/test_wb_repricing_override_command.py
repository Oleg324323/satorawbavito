"""Stage4A exact full-row commands, not persistence or formula eligibility."""

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from app.modules.wb_repricing_overrides import OverrideChange, OverrideValues


def values(**changes):
    names = (
        "automation_enabled",
        "allow_negative_margin",
        "night_median_enabled",
        "p_min_kopecks",
        "p_max_kopecks",
        "rrp_kopecks",
        "min_margin_kopecks",
        "max_margin_kopecks",
        "min_margin_pct",
        "max_margin_pct",
        "price_step_pct",
        "price_step_minutes",
        "basket_norm_manual",
        "basket_norm_mode",
    )
    return OverrideValues(**({name: None for name in names} | changes))


def command(**changes):
    fields = {
        "organization_id": 7,
        "marketplace_account_id": 42,
        "catalog_sku_id": 99,
        "actor_membership_id": 77,
        "command_id": "12345678-1234-4234-8234-123456789abc",
        "expected_version": 0,
        "values": values(),
    }
    return OverrideChange(**(fields | changes))


def encode(change):
    return change.canonical_bytes(max_bytes=4096)


def test_full_scoped_payload_explicit_nulls_and_zero_are_different():
    missing = json.loads(encode(command()))
    assert missing["organizationId"] == 7 and missing["marketplaceAccountId"] == 42
    assert missing["catalogSkuId"] == 99 and missing["actorMembershipId"] == 77
    assert missing["expectedVersion"] == "0"
    assert missing["schema"] == "wb-repricing-sku-overrides/v1"
    assert missing["commandKind"] == "replace_overrides"
    assert set(missing["values"].values()) == {None}
    explicit = command(
        values=values(p_min_kopecks=0, basket_norm_manual=0, automation_enabled=False)
    )
    row = json.loads(encode(explicit))["values"]
    assert row["p_min_kopecks"] == "0" and row["basket_norm_manual"] == 0
    assert row["automation_enabled"] is False
    assert explicit.checksum(max_bytes=4096) != command().checksum(max_bytes=4096)
    with pytest.raises(FrozenInstanceError):
        explicit.values.p_min_kopecks = 1


def test_decimal_canonical_form_preserves_value_without_context_rounding():
    change = command(
        values=values(
            min_margin_pct=Decimal("-0.000"),
            max_margin_pct=Decimal("1.2300"),
            price_step_pct=Decimal("0.12345678901234567890123456789"),
        )
    )
    with localcontext() as context:
        context.prec = 2
        encoded = encode(change)
    row = json.loads(encoded)["values"]
    assert row["min_margin_pct"] == "0" and row["max_margin_pct"] == "1.23"
    assert row["price_step_pct"] == "0.12345678901234567890123456789"
    assert encoded == encode(change)
    assert encode(command(values=values(price_step_pct=Decimal("10.00")))) == encode(
        command(values=values(price_step_pct=Decimal("1E1")))
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", 8),
        ("marketplace_account_id", 43),
        ("catalog_sku_id", 100),
        ("actor_membership_id", 78),
        ("expected_version", 1),
        ("command_id", "12345678-1234-4234-8234-123456789abd"),
    ],
)
def test_change_to_owner_actor_or_version_changes_checksum(field, value):
    assert command(**{field: value}).checksum(max_bytes=4096) != command().checksum(
        max_bytes=4096
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("automation_enabled", 1),
        ("allow_negative_margin", "false"),
        ("night_median_enabled", 0),
        ("p_min_kopecks", -1),
        ("p_max_kopecks", True),
        ("rrp_kopecks", 1.0),
        ("min_margin_kopecks", "1"),
        ("max_margin_kopecks", -1),
        ("min_margin_pct", 0.1),
        ("max_margin_pct", Decimal("NaN")),
        ("price_step_pct", Decimal("Infinity")),
        ("price_step_minutes", 0),
        ("price_step_minutes", 2**31),
        ("basket_norm_manual", -1),
        ("basket_norm_mode", "unknown"),
    ],
)
def test_invalid_typed_values_are_not_coerced_or_dropped(field, value):
    with pytest.raises(ValueError):
        values(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "organization_id",
        "marketplace_account_id",
        "catalog_sku_id",
        "actor_membership_id",
    ],
)
@pytest.mark.parametrize("value", [True, 0, "7", 1.0, 2**31])
def test_internal_identity_is_strict_int4(field, value):
    with pytest.raises(ValueError):
        command(**{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"command_id": "bad"},
        {"expected_version": True},
        {"expected_version": -1},
        {"values": {}},
    ],
)
def test_invalid_command_identity_or_shape_is_blocked(changes):
    with pytest.raises(ValueError):
        command(**changes)


def test_no_new_formula_clamp_or_cross_field_rule_is_invented():
    row = values(
        p_min_kopecks=200,
        p_max_kopecks=100,
        min_margin_pct=Decimal(-99),
        price_step_pct=Decimal(0),
    )
    assert json.loads(encode(command(values=row)))["values"]["min_margin_pct"] == "-99"
    assert row.p_min_kopecks == 200  # Existing formula guards decide eligibility.


@pytest.mark.parametrize("number", [Decimal("1E100000000"), Decimal("1E-100000000")])
def test_compact_exponent_cannot_expand_beyond_explicit_byte_budget(number):
    with pytest.raises(ValueError):
        command(values=values(price_step_pct=number)).canonical_bytes(max_bytes=4096)


@pytest.mark.parametrize("limit", [True, 0, -1, "4096", 4096.0])
def test_encoding_budget_must_be_explicit_positive_integer(limit):
    with pytest.raises(ValueError):
        command().canonical_bytes(max_bytes=limit)


def test_exact_budget_and_large_money_version_are_lossless():
    change = replace(
        command(), expected_version=2**80, values=values(p_min_kopecks=2**80)
    )
    raw = encode(change)
    assert json.loads(raw)["values"]["p_min_kopecks"] == "1208925819614629174706176"
    assert json.loads(raw)["expectedVersion"] == "1208925819614629174706176"
    assert change.canonical_bytes(max_bytes=len(raw)) == raw
    with pytest.raises(ValueError):
        change.canonical_bytes(max_bytes=len(raw) - 1)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("-123.4500", "-123.45"),
        ("0.0012300", "0.00123"),
        ("12000", "12000"),
        ("-0.0100", "-0.01"),
        ("1E-1", "0.1"),
    ],
)
def test_fixed_point_decimal_positions(source, expected):
    change = command(values=values(price_step_pct=Decimal(source)))
    assert json.loads(encode(change))["values"]["price_step_pct"] == expected


def test_all_null_wire_contract_golden_checksum():
    # Pinned v1 bytes: changing the wire format requires explicit compatibility review.
    assert command().checksum(max_bytes=4096) == (
        "b1f127a99e8a23a223df1ad78fa9f20d9411a3627021c030c3521837cb8eb253"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("automation_enabled", False),
        ("allow_negative_margin", False),
        ("night_median_enabled", False),
        ("p_min_kopecks", 0),
        ("p_max_kopecks", 0),
        ("rrp_kopecks", 0),
        ("min_margin_kopecks", 0),
        ("max_margin_kopecks", 0),
        ("min_margin_pct", Decimal(0)),
        ("max_margin_pct", Decimal(0)),
        ("price_step_pct", Decimal(0)),
        ("price_step_minutes", 1),
        ("basket_norm_manual", 0),
        ("basket_norm_mode", "auto"),
    ],
)
def test_every_override_field_participates_in_checksum(field, value):
    assert command(values=values(**{field: value})).checksum(max_bytes=4096) != (
        command().checksum(max_bytes=4096)
    )


def test_module_has_only_pure_standard_library_imports():
    import ast
    from pathlib import Path

    import app.modules.wb_repricing_overrides as module

    tree = ast.parse(Path(module.__file__).read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    assert imports <= {"hashlib", "json", "re", "dataclasses", "decimal"}


def test_literal_override_sql_vectors():
    import hashlib
    from pathlib import Path

    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "wb_repricing_override_golden_v1.json"
        ).read_text(encoding="ascii")
    )
    assert fixture["schema"] == "wb-repricing-overrides-golden/v1"
    assert len(fixture["vectors"]) == 6
    for vector in fixture["vectors"]:
        data = dict(vector["inputs"])
        typed = dict(data.pop("values"))
        for key, value in typed.items():
            if key.endswith("_pct") and value is not None:
                typed[key] = Decimal(value)
        actual = OverrideChange(values=OverrideValues(**typed), **data).canonical_bytes(
            max_bytes=8192
        )
        expected = vector["canonical_ascii"].encode("ascii")
        assert actual == expected, vector["name"]
        assert len(expected) == vector["byte_count"]
        assert hashlib.sha256(expected).hexdigest() == vector["sha256"]
