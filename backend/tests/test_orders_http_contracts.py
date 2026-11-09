import json
from dataclasses import replace

import pytest

from app.orders.http_contracts import OrdersReadPageResponse, OrdersReadRowResponse
from tests.test_orders_read_contracts import page, row


@pytest.mark.parametrize("version", [1, 2**53 + 1, 2**63 - 1])
def test_bigint_row_version_is_lossless_decimal_string(version):
    source = page(rows=(replace(row(), row_version=version),))
    response = OrdersReadPageResponse.model_validate(source)
    body = json.loads(response.model_dump_json())
    assert body["rows"][0]["row_version"] == str(version)
    assert int(body["rows"][0]["row_version"]) == version
    assert source.rows[0].row_version == version


def test_wire_openapi_advertises_string_not_json_number():
    assert (
        OrdersReadRowResponse.model_json_schema()["properties"]["row_version"]["type"]
        == "string"
    )


@pytest.mark.parametrize("invalid", [0, -1, True, "01", 2**63, str(2**63)])
def test_wire_version_rejects_invalid_or_noncanonical_values(invalid):
    values = OrdersReadRowResponse.model_validate(row()).model_dump()
    values["row_version"] = invalid
    with pytest.raises(ValueError):
        OrdersReadRowResponse.model_validate(values)
