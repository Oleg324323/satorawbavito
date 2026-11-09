"""Synthetic bounded status preview; never an Orders publication proof."""

import json
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.avito import account_orders_http as http
from app.avito.account_orders import (
    AccountOrderStatusError,
    BoundedAvitoOrderStatusClient,
    _source_timestamp,
    _wire_rows,
)
from app.avito.account_orders_http import make_account_avito_order_status_router
from tests.test_account_avito_stats_transport import resolved


def payload():
    return {
        "hasMore": False,
        "orders": [
            {
                "id": "000-order",
                "status": "ready_to_ship",
                "createdAt": "2026-09-01T01:02:03Z",
                "updatedAt": "bad",
                "buyer": {"phone": "synthetic-private"},
                "prices": {"total": 9007199254740993},
                "items": [{"id": "listing", "count": 0}],
            }
        ],
    }


def response(body):
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(body).encode()),
    )


def test_one_bounded_page_reuses_existing_request_and_status_map_without_fake_facts():
    calls = []

    def send(request):
        calls.append(request)
        return response(payload())

    value = BoundedAvitoOrderStatusClient(
        resolved(), transport=httpx.MockTransport(send)
    ).fetch_preview(date_from=date(2026, 9, 1), page=2)
    row = value.rows[0]
    assert row.order_id == "000-order" and row.raw_status == "ready_to_ship"
    assert (
        row.canonical_status == "ready_for_fulfillment"
        and row.mapping_version == "avito-order-status-v1"
    )
    assert row.account_evidence == "credential_scope"
    assert row.created_at == "2026-09-01T01:02:03Z" and row.updated_at is None
    assert len(calls) == 1 and calls[0].method == "GET"
    assert str(calls[0].url).startswith(
        "https://api.avito.ru/order-management/1/orders?"
    )
    assert dict(calls[0].url.params) == {
        "limit": "20",
        "page": "2",
        "dateFrom": "1788220800",
    }
    assert not hasattr(row, "quantity") and not hasattr(row, "price_kopecks")
    assert value.has_more is False


@pytest.mark.parametrize("raw", ["future_provider_status"])
def test_unknown_or_missing_status_never_becomes_ready(raw):
    body = payload()
    if raw is None:
        del body["orders"][0]["status"]
    else:
        body["orders"][0]["status"] = raw
    value = BoundedAvitoOrderStatusClient(
        resolved(), transport=httpx.MockTransport(lambda req: response(body))
    ).fetch_preview(date_from=date(2026, 9, 1), page=1)
    assert value.rows[0].raw_status == raw and value.rows[0].canonical_status is None
    assert value.rows[0].mapping_state == "unmapped"


def test_missing_status_is_not_an_invented_raw_unknown():
    body = payload()
    del body["orders"][0]["status"]
    with pytest.raises(AccountOrderStatusError):
        BoundedAvitoOrderStatusClient(
            resolved(), transport=httpx.MockTransport(lambda req: response(body))
        ).fetch_preview(date_from=date(2026, 9, 1), page=1)


@pytest.mark.parametrize("account", ["999", True, 123.0, None])
def test_present_foreign_or_lossy_account_identity_rejected(account):
    body = payload()
    body["orders"][0]["accountId"] = account
    with pytest.raises(AccountOrderStatusError):
        BoundedAvitoOrderStatusClient(
            resolved(), transport=httpx.MockTransport(lambda req: response(body))
        ).fetch_preview(date_from=date(2026, 9, 1), page=1)


@pytest.mark.parametrize("alias", ["accountId", "userId", "sellerId"])
@pytest.mark.parametrize("value", ["123", "999", None])
def test_unproven_top_level_owner_alias_is_not_silently_ignored(alias, value):
    body = payload()
    body[alias] = value
    with pytest.raises(AccountOrderStatusError):
        BoundedAvitoOrderStatusClient(
            resolved(), transport=httpx.MockTransport(lambda req: response(body))
        ).fetch_preview(date_from=date(2026, 9, 1), page=1)


@pytest.mark.parametrize("account,page", [("02", "1"), ("2", "01")])
def test_noncanonical_numeric_request_is_rejected(monkeypatch, account, page):
    result = client(monkeypatch).get(
        f"/api/v2/avito/accounts/{account}/orders/status-preview?dateFrom=2026-09-01&page={page}"
    )
    assert result.status_code == 422


@pytest.mark.parametrize(
    "case", ["too_many", "duplicate", "integer_id", "redirect", "expired", "oversized"]
)
def test_invalid_source_or_authority_is_closed_no_extra_get(case):
    body, calls = payload(), []
    if case == "too_many":
        body["orders"] *= 21
    elif case == "duplicate":
        body["orders"] *= 2
    elif case == "integer_id":
        body["orders"][0]["id"] = 1

    def send(request):
        calls.append(request)
        if case == "redirect":
            return httpx.Response(
                302, headers={"location": "https://example.invalid/private"}
            )
        if case == "oversized":
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-length": str(8 * 1024 * 1024),
                },
                stream=httpx.ByteStream(b""),
            )
        return response(body)

    with pytest.raises(AccountOrderStatusError) as error:
        BoundedAvitoOrderStatusClient(
            resolved(expired=case == "expired"), transport=httpx.MockTransport(send)
        ).fetch_preview(date_from=date(2026, 9, 1), page=1)
    assert len(calls) == (0 if case == "expired" else 1)
    assert error.value.__context__ is None


def client(monkeypatch):
    monkeypatch.setattr(
        http, "get_marketplace_credential_actor", lambda request: "actor"
    )

    class Service:
        def preview(self, actor, *, marketplace_account_id, date_from, page):
            assert actor == "actor" and marketplace_account_id == 2 and page == 1
            return {"coverageState": "partial", "hasMore": None, "rows": []}

    app = FastAPI()
    app.include_router(
        make_account_avito_order_status_router(service_dependency=lambda: Service())
    )
    return TestClient(app)


def test_preview_route_never_claims_full_snapshot(monkeypatch):
    result = client(monkeypatch).get(
        "/api/v2/avito/accounts/2/orders/status-preview?dateFrom=2026-09-01&page=1"
    )
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["data"]["coverageState"] == "partial"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "dateFrom=2026-09-01",
        "dateFrom=bad&page=1",
        "dateFrom=2026-09-01&page=0",
        "dateFrom=2026-09-01&page=1&limit=200",
        "dateFrom=2026-09-01&page=1&accountId=9",
    ],
)
def test_closed_query_shape_no_raw_echo(monkeypatch, query):
    response = client(monkeypatch).get(
        "/api/v2/avito/accounts/2/orders/status-preview?" + query
    )
    assert response.status_code == 422 and response.json() == {
        "detail": {"code": "AVITO_ORDER_STATUS_INVALID_REQUEST"}
    }


@pytest.mark.parametrize("has_more", [True, False, None, "false", 0])
def test_only_actual_boolean_has_more_and_no_sensitive_projection(has_more):
    body = payload()
    body["hasMore"] = has_more
    body["orders"][0]["accountId"] = "123"
    value = BoundedAvitoOrderStatusClient(
        resolved(), transport=httpx.MockTransport(lambda req: response(body))
    ).fetch_preview(date_from=date(2026, 9, 1), page=1)
    assert value.has_more is (has_more if type(has_more) is bool else None)
    wire = _wire_rows(value, "123")
    assert wire[0]["accountEvidence"] == "provider_account_id"
    assert "synthetic-private" not in str(wire)
    assert set(wire[0]) == {
        "orderId",
        "rawStatus",
        "canonicalStatus",
        "mappingState",
        "mappingVersion",
        "createdAt",
        "updatedAt",
        "accountEvidence",
    }


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-01T00:00:00+00:99",
        "2026-02-30T00:00:00Z",
        "2026-09-01T00:00:00",
        1788220800,
    ],
)
def test_invalid_timestamp_is_unknown_not_normalized_guess(timestamp):
    assert _source_timestamp(timestamp) is None


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-01T00:00:00.1Z",
        "2026-09-01T00:00:00.123456+05:30",
        "2026-09-01T00:00:00.123456789-03:30",
    ],
)
def test_valid_source_timestamps_keep_exact_fraction_and_offset(timestamp):
    assert _source_timestamp(timestamp) == timestamp


def test_undeclared_body_budget_closes_without_reading_tail():
    closed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(65):
                yield b" " * 65536
            pytest.fail("read beyond bounded body")

        def close(self):
            closed.append(True)

    with pytest.raises(AccountOrderStatusError):
        BoundedAvitoOrderStatusClient(
            resolved(),
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, headers={"content-type": "application/json"}, stream=Body()
                )
            ),
        ).fetch_preview(date_from=date(2026, 9, 1), page=1)
    assert closed == [True]
