import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from app.modules.wb_price_snapshots import GoodsPriceRun, parse_goods_price_page
from app.modules.wb_source_requests import CollectionRequest, SourceKind
from app.modules.wb_stock_snapshots import StockRun, parse_warehouse_stock_page


@pytest.mark.parametrize("index", range(10))
def test_pinned_collection_page_and_manifest_bytes(index):
    fixture = json.loads((Path(__file__).parent / "fixtures/wb_collection_golden_v1.json").read_text())
    vector = fixture["vectors"][index]
    request_values = dict(vector["request"])
    request_values["source_kind"] = SourceKind(request_values["source_kind"])
    request = CollectionRequest(**request_values)
    assert request.canonical_bytes == vector["request_canonical_ascii"].encode("ascii")
    assert request.checksum == vector["request_sha256"]
    pages = []
    prices = vector["kind"] == "prices"
    for row in vector["pages"]:
        raw = row["raw_utf8_text"].encode("utf-8")
        assert len(raw) == row["raw_byte_count"]
        assert hashlib.sha256(raw).hexdigest() == row["raw_sha256"]
        received_at = datetime.fromisoformat(row["received_at"])
        if prices:
            page = parse_goods_price_page(
                raw, organization_id=request.organization_id,
                marketplace_account_id=request.marketplace_account_id,
                offset=row["offset"], limit=request.page_limit,
                received_at=received_at, request_checksum=request.checksum,
            )
            observations = page.products
        else:
            page = parse_warehouse_stock_page(raw, request=request, offset=row["offset"], received_at=received_at)
            observations = page.observations
        assert page.terminal is row["terminal"]
        assert page.source_observed_at is row["source_observed_at"] is None
        assert json.loads(json.dumps([asdict(item) for item in observations])) == row["observations"]
        pages.append(page)
    run = GoodsPriceRun(tuple(pages)) if prices else StockRun(tuple(pages))
    manifest = vector["manifest_canonical_ascii"].encode("ascii")
    assert len(manifest) == vector["manifest_byte_count"]
    assert hashlib.sha256(manifest).hexdigest() == vector["manifest_sha256"] == run.manifest_checksum
    assert run.complete is vector["complete"]
    if not prices:
        day = run.received_business_date_msk
        assert (day.isoformat() if day is not None else None) == vector["received_business_date_msk"]
