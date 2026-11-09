"""Synthetic wire, cursor and HTTP checks; these do not prove PostgreSQL RLS."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.wb_live.products_http import ProductsQuery
from app.wb_live.products_read import (
    ProductPosition,
    ProductsCursorCodec,
    ProductsReadError,
    assemble_page,
    page_statement,
    product_selection,
)

CONTEXT = (91001, "synthetic-user", "synthetic-session")
CODEC = ProductsCursorCodec(b"synthetic-test-only-not-a-secret!!")


def raw_page(count=2):
    return {
        "sources": [
            {
                "source": name,
                "state": "completed" if name == "content" else "queued",
                "processed": count if name == "content" else 0,
                "updated_at": "2026-09-10T00:00:00Z",
                "error_code": None,
                "job_id": "synthetic-job",
                "run_id": "synthetic-" + name,
                "revision": 1,
            }
            for name in ("content", "prices")
        ],
        "items": [
            {
                "nmId": str(9007199254740993 + i),
                "vendorCode": "000-synthetic",
                "title": "Synthetic shirt",
                "brand": None,
                "subjectId": None,
                "subjectName": None,
                "photoUrl": None,
                "contentUpdatedAt": "2026-09-10T00:00:00Z",
                "pricesUpdatedAt": None,
                "sizes": [
                    {
                        "chrtId": "9007199254740999",
                        "techSize": "M",
                        "skus": ["000-synthetic-barcode"],
                        "skusTruncated": False,
                        "priceKopecks": None,
                        "discountedPriceKopecks": "0",
                    }
                ],
                "sizesTruncated": False,
            }
            for i in range(count)
        ],
    }


def assemble(raw=None, query=None, position=None):
    return assemble_page(
        raw_page() if raw is None else raw,
        context=CONTEXT,
        account_id=91101,
        query=query or ProductsQuery(limit=1),
        codec=CODEC,
        position=position,
    )


def test_native_size_prices_null_zero_large_ids_and_partial_content():
    wire = assemble().model_dump(mode="json", by_alias=True)
    assert wire["marketplaceAccountId"] == 91101 and wire["readiness"] == "partial"
    assert wire["items"][0]["nmId"] == "9007199254740993"
    assert wire["items"][0]["sizes"][0]["priceKopecks"] is None
    assert wire["items"][0]["sizes"][0]["discountedPriceKopecks"] == "0"
    assert wire["items"][0]["vendorCode"].startswith("000")
    assert len(wire["items"]) == 1 and wire["nextCursor"]
    assert "profit" not in wire["items"][0] and "stockQty" not in wire["items"][0]


@pytest.mark.parametrize("sort", ["nmId", "vendorCode", "title", "brand"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_cursor_roundtrip_all_sorts(sort, direction):
    query = ProductsQuery(limit=1, sort=sort, direction=direction)
    page = assemble(query=query)
    position = CODEC.parse(
        page.next_cursor, context=CONTEXT, account_id=91101, query=query
    )
    assert position.nm_id == 9007199254740993 and position.revision == page.read_version


@pytest.mark.parametrize(
    "change", ["account", "org", "user", "session", "query", "signature"]
)
def test_cursor_scope_and_tamper(change):
    query = ProductsQuery(limit=1)
    token = assemble(query=query).next_cursor
    context, account = CONTEXT, 91101
    if change == "account":
        account = 91102
    elif change in {"org", "user", "session"}:
        values = list(context)
        values[{"org": 0, "user": 1, "session": 2}[change]] = "other-synthetic"
        context = tuple(values)
    elif change == "query":
        query = ProductsQuery(limit=2)
    else:
        token = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ProductsReadError, match="WB_PRODUCTS_CURSOR_INVALID"):
        CODEC.parse(token, context=context, account_id=account, query=query)


def test_source_revision_change_conflicts_even_when_timestamps_unchanged():
    page = assemble()
    position = CODEC.parse(
        page.next_cursor,
        context=CONTEXT,
        account_id=91101,
        query=ProductsQuery(limit=1),
    )
    raw = raw_page()
    raw["sources"][0]["revision"] += 1
    with pytest.raises(ProductsReadError, match="WB_PRODUCTS_CHANGED"):
        assemble(raw, position=position)


@pytest.mark.parametrize(
    "state,rows,expected",
    [
        ("completed", 0, "empty"),
        ("completed", 2, "ready"),
        ("queued", 0, "partial"),
        ("failed", 0, "error"),
        ("failed", 2, "partial"),
    ],
)
def test_readiness(state, rows, expected):
    raw = raw_page(rows)
    raw["sources"][1]["state"] = state
    assert assemble(raw).readiness == expected


def test_idle_and_redacted_error():
    raw = raw_page(0)
    raw["sources"][0]["error_code"] = "synthetic-private-response"
    assert assemble(raw).sources[0].error_code == "WB_LIVE_UNAVAILABLE"
    assert assemble({"sources": [], "items": []}).readiness == "partial"


def test_bounded_query_and_literal_search():
    for value in (0, 201):
        with pytest.raises(ValidationError):
            ProductsQuery(limit=value)
    with pytest.raises(ValidationError):
        ProductsQuery(sort="profit")
    query = ProductsQuery(q="%_", brand="synthetic' OR true --")
    where, _, params = product_selection(query, None)
    assert params["search"] == "%\\%\\_%"
    assert query.brand not in where
    sql, _ = page_statement(query, None)
    assert "p.organization_id=:org" in str(sql)
    assert "z.organization_id=:org" in str(
        sql
    ) and "z.marketplace_account_id=:account" in str(sql)
    assert "LIMIT 6" in str(sql) and "jsonb_array_elements" not in str(sql)


@pytest.mark.parametrize(
    "direction,value,fragment",
    [
        ("asc", None, "p.nm_id > :after_nm"),
        ("desc", None, "p.title IS NOT NULL"),
        ("asc", "Synthetic", "p.title IS NULL"),
        ("desc", "Synthetic", "p.title < (SELECT b.title"),
    ],
)
def test_null_keyset_boundaries(direction, value, fragment):
    where, order, _ = product_selection(
        ProductsQuery(sort="title", direction=direction),
        ProductPosition("a" * 64, 1, value),
    )
    assert fragment in where and "p.nm_id" in order


def test_maximum_list_wire_budget_with_worst_json_escaped_text():
    raw = raw_page(200)
    for item in raw["items"]:
        item.update(
            title="\x01" * 128,
            vendorCode="\x01" * 128,
            brand="\x01" * 64,
            subjectName="\x01" * 128,
            photoUrl="\x01" * 512,
            truncatedFields=["title", "vendorCode", "brand", "subjectName", "photoUrl"],
        )
        size = dict(
            item["sizes"][0],
            techSize="\x01" * 32,
            skus=[],
            skusTruncated=True,
            truncatedFields=["techSize"],
        )
        item["sizes"] = [dict(size) for _ in range(5)]
        item["sizesTruncated"] = True
    page = assemble(raw, query=ProductsQuery(limit=200))
    assert len(page.model_dump_json(by_alias=True).encode("utf-8")) < 2 * 1024 * 1024


@pytest.fixture
def http_client(monkeypatch):
    from app.wb_live import products_router as module

    app = FastAPI()
    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(wb_live_sync_enabled=True)
    )
    app.include_router(module.router)
    app.dependency_overrides[module.get_products_actor] = lambda: SimpleNamespace()
    app.dependency_overrides[module.get_db_session] = lambda: object()
    app.dependency_overrides[module.get_products_codec] = lambda: CODEC
    calls = []

    def read(*args, **kwargs):
        calls.append(kwargs)
        return assemble(query=kwargs["query"])

    monkeypatch.setattr(module, "read_products_page", read)
    with TestClient(app) as client:
        yield client, calls, module


def test_http_wire_and_no_store(http_client):
    client, calls, _ = http_client
    response = client.get(
        "/api/v2/wb/accounts/91101/products?limit=1&sort=brand&cursor=synthetic"
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert calls[0]["account_id"] == 91101 and calls[0]["cursor"] == "synthetic"


def test_disabled_route_does_not_read(http_client, monkeypatch):
    client, calls, module = http_client
    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(wb_live_sync_enabled=False)
    )
    assert client.get("/api/v2/wb/accounts/91101/products").status_code == 503
    assert not calls


@pytest.mark.parametrize(
    "query,code",
    [("limit=201", 422), ("sort=profit", 422), ("periodDays=7", 400), ("q=a&q=b", 400)],
)
def test_http_rejects_unsupported_filters(http_client, query, code):
    client, calls, _ = http_client
    assert client.get("/api/v2/wb/accounts/91101/products?" + query).status_code == code
    assert not calls


@pytest.mark.parametrize(
    "error,status",
    [
        ("WB_PRODUCTS_CHANGED", 409),
        ("WB_PRODUCTS_CURSOR_INVALID", 400),
        ("WB_PRODUCTS_UNAVAILABLE", 503),
    ],
)
def test_http_closed_errors(http_client, monkeypatch, error, status):
    client, _, module = http_client

    def fail(*args, **kwargs):
        raise ProductsReadError(error)

    monkeypatch.setattr(module, "read_products_page", fail)
    response = client.get("/api/v2/wb/accounts/91101/products")
    assert response.status_code == status and response.json() == {
        "detail": {"code": error}
    }
