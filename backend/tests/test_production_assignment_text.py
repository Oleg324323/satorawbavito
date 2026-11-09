import pytest

from app.modules.orders import OrderContractValidationError
from app.modules.production import AssignmentCommand


@pytest.mark.parametrize("field", ["reason", "idempotency_key"])
@pytest.mark.parametrize("codepoint", [0, 0xD800, 0xDFFF])
def test_assignment_rejects_non_postgres_text_without_reflection(field, codepoint):
    bad = "synthetic" + chr(codepoint) + "text"
    values = {
        "work_item_id": 1, "expected_version": 1, "catalog_sku_id": 1,
        "reason": "synthetic", "idempotency_key": "synthetic",
    }
    values[field] = bad
    with pytest.raises(
        OrderContractValidationError,
        match="^Assignment text is not PostgreSQL compatible$",
    ):
        AssignmentCommand(**values)


def test_assignment_keeps_valid_unicode_bytes():
    value = "Синтетический выбор \U0001f600 e\u0301"
    command = AssignmentCommand(1, 1, value, 1, value)
    assert command.reason == command.idempotency_key == value
