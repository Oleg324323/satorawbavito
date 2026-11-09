import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest

from app.modules.wb_source_requests import (
    CollectionRequest,
    CollectionRequestError,
    SourceKind,
)


def prices():
    return CollectionRequest(7, 42, SourceKind.prices, "wb-goods-prices/v1", 100)


def test_price_collection_has_exact_canonical_scope_without_page_offset():
    request = prices()
    expected = (b'{"accountId":42,"body":{},"method":"POST","organizationId":7,'
                b'"pageLimit":100,"parserVersion":"wb-goods-prices/v1",'
                b'"path":"/api/v2/list/goods/filter","query":{},'
                b'"schema":"wb-source-collection/v1","sourceKind":"wb_goods_prices_v1"}')
    assert request.canonical_bytes == expected
    assert request.checksum == hashlib.sha256(expected).hexdigest()
    with pytest.raises(FrozenInstanceError):
        request.marketplace_account_id = 99


def test_warehouse_collection_is_not_price_or_seller_stock():
    request = CollectionRequest(7, 42, SourceKind.wb_warehouse, "wb-warehouse-stocks/v1", 100)
    expected = (b'{"accountId":42,"body":{"stockType":"wb"},"method":"POST",'
                b'"organizationId":7,"pageLimit":100,"parserVersion":"wb-warehouse-stocks/v1",'
                b'"path":"/api/analytics/v1/stocks-report/wb-warehouses","query":{},'
                b'"schema":"wb-source-collection/v1","sourceKind":"wb_warehouse_v1"}')
    assert request.canonical_bytes == expected
    assert request.checksum == hashlib.sha256(expected).hexdigest()
    assert request.checksum != prices().checksum


@pytest.mark.parametrize("field,value", [
    ("organization_id", 8), ("marketplace_account_id", 43),
    ("page_limit", 101), ("parser_version", "wb-goods-prices/v2"),
])
def test_scope_or_parser_or_collection_shape_change_invalidates_replay_key(field, value):
    original = prices()
    assert replace(original, **{field: value}).checksum != original.checksum


@pytest.mark.parametrize("field,value", [
    ("organization_id", True), ("organization_id", 0), ("organization_id", -1),
    ("marketplace_account_id", "external"), ("marketplace_account_id", False),
    ("marketplace_account_id", 0), ("page_limit", 1.0), ("page_limit", 0),
    ("page_limit", True), ("source_kind", "wb_goods_prices_v1"),
    ("source_kind", "seller_fbs_v1"), ("parser_version", ""),
    ("parser_version", " x"), ("parser_version", "x\x00y"),
    ("parser_version", "x\ny"), ("parser_version", "версия"),
    ("parser_version", chr(0xD800)), ("parser_version", chr(0xDFFF)),
])
def test_unsupported_or_unresolved_context_never_gets_a_canonical_hash(field, value):
    with pytest.raises(CollectionRequestError) as caught:
        replace(prices(), **{field: value})
    assert caught.value.__cause__ is None
    if isinstance(value, str) and len(value) > 2:
        assert value not in str(caught.value)


def test_canonical_request_has_bounded_storage_size_without_silent_truncation():
    with pytest.raises(CollectionRequestError):
        replace(prices(), parser_version="v" * 65536)


@pytest.mark.parametrize("field", ["organization_id", "marketplace_account_id", "page_limit"])
def test_unencodable_integer_is_a_safe_collection_validation_error(field):
    with pytest.raises(CollectionRequestError):
        replace(prices(), **{field: 10 ** 5000})


def test_collection_checksum_binds_price_page_and_run_manifest():
    from datetime import UTC, datetime

    from app.modules.wb_price_snapshots import (
        assemble_goods_price_run,
        parse_goods_price_page,
    )

    request = prices()
    page = parse_goods_price_page(
        b'{"data":{"listGoods":[]}}', organization_id=request.organization_id,
        marketplace_account_id=request.marketplace_account_id, offset=0,
        limit=request.page_limit, received_at=datetime(2026, 9, 9, tzinfo=UTC),
        request_checksum=request.checksum,
    )
    run = assemble_goods_price_run((page,))
    assert run.complete
    assert run.pages[0].request_checksum == request.checksum
    other_page = replace(page, request_checksum=replace(request, marketplace_account_id=43).checksum)
    assert assemble_goods_price_run((other_page,)).manifest_checksum != run.manifest_checksum
