"""Exact saved-view query semantics without provider or database access."""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.control_plane.auth import ActorContext
from app.infra.db import get_db_session
from app.orders.cursor import OrdersCursorCodec
from app.orders.query import OrdersReadFilters
from app.orders.router import get_orders_actor, get_orders_cursor_codec, router
from app.platform.integrations.publication_guard import UserSessionPrincipal


@pytest.mark.parametrize("field", ["raw_status", "external_order_id"])
@pytest.mark.parametrize("value", ["", " x", "x ", "x\x00", "x\ud800"])
def test_filters_reject_nonexact_text(field, value):
    with pytest.raises(ValidationError):
        OrdersReadFilters(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("marketplace", "other"),
        ("canonical_status", "ready"),
        ("mapping_state", "known"),
        ("resolution_state", "automatic"),
    ],
)
def test_filters_reject_unknown_classification(field, value):
    with pytest.raises(ValidationError):
        OrdersReadFilters(**{field: value})


def test_sql_is_allowlisted_and_never_interpolates_external_values():
    filters = OrdersReadFilters(
        external_order_id="000' OR true --",
        raw_status="Unknown",
        mapping_state="unmapped",
    )
    sql, parameters = filters.sql()
    assert "000' OR true --" not in sql
    assert "row,observation,identity,external_order_id" in sql
    assert "row,observation,status,raw_status" in sql
    assert parameters == {
        "filter_external_order_id": "000' OR true --",
        "filter_raw_status": "Unknown",
        "filter_mapping_state": "unmapped",
    }
    assert OrdersReadFilters().sql() == ("", {})
    assert OrdersReadFilters(raw_status="Unknown").raw_status == "Unknown"


def test_filter_cursor_is_bound_and_preserves_existing_unfiltered_tokens():
    principal = UserSessionPrincipal(1, "synthetic", 2, "synthetic-session")
    codec = OrdersCursorCodec(b"synthetic-filter-key-at-least-32-bytes")
    checksum = "a" * 64
    assert OrdersReadFilters().cursor_checksum(checksum) == checksum
    filters = OrdersReadFilters(marketplace="avito", mapping_state="unmapped")
    selected = filters.cursor_checksum(checksum)
    assert selected == OrdersReadFilters(
        mapping_state="unmapped", marketplace="avito"
    ).cursor_checksum(checksum)
    assert selected != filters.cursor_checksum("b" * 64)
    token = codec.issue(
        principal=principal,
        accounts=(3,),
        snapshot_id=4,
        after_position=1,
        query_checksum=selected,
    )
    assert codec.parse(
        token, principal=principal, accounts=(3,), query_checksum=selected
    ) == (4, 1)
    with pytest.raises(ValueError, match="cursor"):
        codec.parse(token, principal=principal, accounts=(3,), query_checksum=checksum)


@pytest.fixture
def client(monkeypatch):
    module = importlib.import_module("app.orders.router")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_orders_actor] = lambda: ActorContext(
        "synthetic",
        "synthetic",
        1,
        "custom",
        frozenset(),
        session_id="synthetic-session",
    )
    app.dependency_overrides[get_db_session] = lambda: object()
    app.dependency_overrides[get_orders_cursor_codec] = lambda: OrdersCursorCodec(
        b"synthetic-filter-key-at-least-32-bytes"
    )

    def never(*args, **kwargs):
        pytest.fail("Invalid query must not reach database discovery")

    monkeypatch.setattr(module, "discover_orders_bindings", never)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "suffix",
    [
        "raw_status=%20bad",
        "raw_status=a&raw_status=b",
        "resolution_state=automatic",
        "sort=title",
        "unknown=value",
    ],
)
def test_invalid_http_filter_does_not_reach_sql(client, suffix):
    response = client.get(
        "/api/v2/orders?account_id=3&query_checksum=" + "a" * 64 + "&" + suffix
    )
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "orders_request_invalid"}}


def test_discovery_rejects_filter_and_documents_saved_view_wire(client):
    response = client.get("/api/v2/orders/snapshots/latest?account_id=3&marketplace=wb")
    assert response.status_code == 400
    spec = client.get("/openapi.json").json()
    fields = spec["components"]["schemas"]["OrdersSavedSnapshotResponse"]["properties"]
    assert fields["selection_kind"]["const"] == "saved_snapshot"
    assert fields["row_count"]["type"] == "string"
    assert fields["snapshot_id"]["type"] == "string"
    query = spec["paths"]["/api/v2/orders"]["get"]["parameters"]
    assert set(OrdersReadFilters.model_fields) <= {field["name"] for field in query}
