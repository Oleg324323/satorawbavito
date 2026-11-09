from importlib import import_module

import pytest


@pytest.mark.parametrize(
    "name,expected",
    [
        ("WB_SKU_OVERRIDE_READ_PERMISSIONS", frozenset({"settings:read"})),
        (
            "WB_SKU_OVERRIDE_REPLACE_PERMISSIONS",
            frozenset({"settings:read", "settings:write"}),
        ),
    ],
)
def test_canonical_override_requirements_are_exact_and_immutable(name, expected):
    actual = getattr(import_module("app.cabinet.permissions"), name)
    assert type(actual) is frozenset
    assert actual == expected
    with pytest.raises(AttributeError):
        actual.add("price:send")


def test_replace_and_replay_require_read_in_addition_to_write():
    permissions = import_module("app.cabinet.permissions")
    read = permissions.WB_SKU_OVERRIDE_READ_PERMISSIONS
    replace = permissions.WB_SKU_OVERRIDE_REPLACE_PERMISSIONS
    assert read < replace
    assert replace - read == frozenset({"settings:write"})
