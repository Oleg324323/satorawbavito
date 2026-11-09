"""Offline recovery wire tests; database-backed service behavior is separate."""

import pytest

from app.orders import production_http as http
from app.orders.production_service import ProductionServiceError, ProductionWorkItem
from tests import test_production_http as wire

client = wire.client
PATH = wire.BASE + "/by-order-item/9007199254740995"
QUERY = "?expectedSourceItemVersion=9007199254740996"


def test_lookup_preserves_exact_source_tuple_and_response(client, monkeypatch):
    def read(session, **values):
        client.calls.append(values)
        return ProductionWorkItem(
            9007199254740993,
            1,
            2,
            9007199254740994,
            9007199254740995,
            9007199254740996,
            2,
            0,
            2,
            None,
            1,
            wire.NOW,
            wire.NOW,
            None,
        )

    monkeypatch.setattr(
        http, "read_production_work_item_by_source", read, raising=False
    )
    response = client.client.get(PATH + QUERY)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["schemaVersion"] == "production-work-item-v1"
    assert response.json()["item"]["workItemId"] == "9007199254740993"
    assert response.json()["item"]["sourceItemVersion"] == "9007199254740996"
    assert client.calls == [
        {
            "principal": wire.PRINCIPAL,
            "account": wire.ACCOUNT,
            "order_item_id": 9007199254740995,
            "expected_source_item_version": 9007199254740996,
        }
    ]


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?expectedSourceItemVersion=",
        "?expectedSourceItemVersion=01",
        "?expectedSourceItemVersion=0",
        "?expectedSourceItemVersion=9223372036854775808",
        "?expectedSourceItemVersion=1&expectedSourceItemVersion=1",
        "?expectedSourceItemVersion=1&extra=1",
    ],
)
def test_lookup_rejects_missing_noncanonical_duplicate_and_extra_query(client, query):
    response = client.client.get(PATH + query)
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "PRODUCTION_REQUEST_INVALID"}}
    assert not client.calls


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("PRODUCTION_NOT_FOUND", 404),
        ("PRODUCTION_SOURCE_CHANGED", 409),
        ("PRODUCTION_SOURCE_INVALID", 409),
        ("PRODUCTION_DENIED", 403),
        ("PRODUCTION_STORAGE_UNAVAILABLE", 503),
    ],
)
def test_lookup_maps_read_errors_without_write_uncertainty(
    client, monkeypatch, code, status
):
    def read(*args, **kwargs):
        raise ProductionServiceError(code)

    monkeypatch.setattr(
        http, "read_production_work_item_by_source", read, raising=False
    )
    response = client.client.get(PATH + QUERY)
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code}}
    assert response.headers["cache-control"] == "no-store"
