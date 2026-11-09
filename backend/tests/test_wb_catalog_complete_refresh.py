"""Catalog completeness is shared by every WB reporting page."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import repricer_bff, repricer_sync
from app.repricer_cache import store
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.routers import wb_repricer_bff as router


def good(nm_id):
    # Shared vendor codes must not collapse distinct WB products.
    return {"nmID": nm_id, "vendorCode": "shared-article", "sizes": []}


def page(offset, ids, limit=2):
    return {"offset": offset, "limit": limit, "goods": [good(i) for i in ids]}


@pytest.fixture
def catalog(monkeypatch):
    engine = create_engine("sqlite://")
    WbRepricerGoodsCacheRow.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(store, "_redis_get_json", lambda *a: None)
    monkeypatch.setattr(store, "_redis_get_json_list", lambda *a: None)
    monkeypatch.setattr(store, "_redis_set_json", lambda *a: None)
    monkeypatch.setattr(store, "_redis_set_json_list", lambda *a: None)
    monkeypatch.setattr(store, "_redis_delete", lambda *a: None)
    monkeypatch.setattr(store, "_goods_with_browser_prices", lambda org, goods: goods)
    store.replace_goods_pages(organization_id=1, pages=[page(0, [90])])
    store.replace_goods_pages(organization_id=2, pages=[page(0, [99])])
    yield engine
    engine.dispose()


@pytest.fixture
def manual_refresh(monkeypatch, catalog):
    monkeypatch.setattr(router, "_request_actor_and_wb_token", lambda req: (SimpleNamespace(organization_id=1), None))
    monkeypatch.setattr(router, "_ensure_wb_sync_not_running", lambda org: None)
    monkeypatch.setattr(repricer_bff, "fetch_commission_tariffs", lambda *a, **kw: {})
    monkeypatch.setattr(router, "browser_prices_selected", lambda org: True)
    monkeypatch.setattr(router, "record_wb_sync_price_change_events", lambda *a, **kw: 0)

    def run(*, all=True):
        return router.refresh_sku_list_page(Request({"type": "http"}), scenario="complete", limit=2, offset=0, all=all)
    return run


def ids(org=1):
    return [row["nmID"] for row in store.list_cached_goods(org)]


def test_complete_refresh_publishes_all_pages_together(manual_refresh, monkeypatch):
    def fetch(*a, offset, **kw):
        assert kw["enrich_spp"] is False
        assert ids() == [90]  # Never publish an in-progress catalog.
        return page(offset, [offset + 1, offset + 2] if offset < 4 else [5])
    monkeypatch.setattr(router, "fetch_catalog_goods_page", fetch)
    result = manual_refresh()
    assert result["total"] == 5
    assert ids() == [1, 2, 3, 4, 5]
    assert ids(2) == [99]


@pytest.mark.parametrize("failure", ["rate_limit", "malformed", "repeated"])
def test_failed_refresh_keeps_old_catalog(manual_refresh, monkeypatch, failure):
    def fetch(*a, offset, **kw):
        if offset == 0:
            return page(0, [1, 2])
        if failure == "rate_limit":
            raise HTTPException(429, "WB_RATE_LIMITED")
        return {"goods": None} if failure == "malformed" else page(offset, [1, 2])
    monkeypatch.setattr(router, "fetch_catalog_goods_page", fetch)
    with pytest.raises(HTTPException):
        manual_refresh()
    assert ids() == [90]
    assert ids(2) == [99]


def test_single_page_refresh_preserves_other_pages(manual_refresh, monkeypatch):
    store.replace_goods_pages(organization_id=1, pages=[page(0, [1, 2]), page(2, [3])])
    monkeypatch.setattr(router, "fetch_catalog_goods_page", lambda *a, **kw: page(0, [7, 8]))
    manual_refresh(all=False)
    assert ids() == [7, 8, 3]


def test_publish_rejects_incomplete_pages(catalog):
    for pages in ([page(0, [1, 2])], [page(0, [1]), page(2, [3])], [page(2, [])]):
        with pytest.raises(ValueError):
            store.replace_goods_pages(organization_id=1, pages=pages)
        assert ids() == [90]


def test_publish_failure_rolls_back_deletion(catalog):
    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO wb_repricer_goods_cache"):
            raise IntegrityError(statement, None, RuntimeError("synthetic write failure"))
    event.listen(catalog, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(IntegrityError):
            store.replace_goods_pages(organization_id=1, pages=[page(0, [1])])
    finally:
        event.remove(catalog, "before_cursor_execute", fail_insert)
    assert ids() == [90]


@pytest.mark.parametrize("fail_second_page", [False, True])
def test_queued_sync_also_publishes_only_complete_catalog(catalog, monkeypatch, fail_second_page):
    monkeypatch.setattr(repricer_sync, "browser_prices_selected", lambda org: True)
    monkeypatch.setattr(repricer_sync, "fetch_commission_tariffs", lambda *a, **kw: {})
    monkeypatch.setattr(repricer_sync, "record_wb_sync_price_change_events", lambda *a, **kw: 0)
    monkeypatch.setattr(repricer_sync, "_mark_new_goods_as_warmup", lambda *a: 0)
    def fetch(*a, offset, limit, **kw):
        assert ids() == [90]
        assert kw["enrich_spp"] is False
        if offset and fail_second_page:
            raise HTTPException(429, "WB_RATE_LIMITED")
        return page(offset, range(1, 1001) if offset == 0 else [1001], limit=limit)
    monkeypatch.setattr(repricer_sync, "fetch_catalog_goods_page", fetch)
    result = repricer_sync.refresh_wb_data_sources(
        organization_id=1, wb_token=None, sources=["goods"], execute_lock=False,
    )
    assert result["state"] == ("partial" if fail_second_page else "completed")
    assert ids() == ([90] if fail_second_page else list(range(1, 1002)))
    assert ids(2) == [99]


def test_fallback_catalog_has_no_three_page_cap(monkeypatch):
    monkeypatch.setattr(repricer_bff, "CATALOG_GOODS_CACHE", {})
    monkeypatch.setattr(repricer_bff, "build_wb_client", lambda *a, **kw: object())
    offsets = []
    def fetch(client, *, offset, limit):
        offsets.append(offset)
        return {"listGoods": [good(i) for i in range(offset + 1, min(offset + limit, 3522) + 1)]}
    monkeypatch.setattr(repricer_bff, "_request_catalog_goods_page", fetch)
    assert len(repricer_bff._fetch_catalog_goods("complete")) == 3522
    assert offsets == [0, 1000, 2000, 3000]


def test_fallback_does_not_cache_partial_catalog_after_429(monkeypatch):
    monkeypatch.setattr(repricer_bff, "CATALOG_GOODS_CACHE", {})
    monkeypatch.setattr(repricer_bff, "build_wb_client", lambda *a, **kw: object())
    def fetch(client, *, offset, limit):
        if offset:
            raise HTTPException(429, "WB_RATE_LIMITED")
        return {"listGoods": [good(i) for i in range(1, 1001)]}
    monkeypatch.setattr(repricer_bff, "_request_catalog_goods_page", fetch)
    with pytest.raises(HTTPException):
        repricer_bff._fetch_catalog_goods("complete")
    assert repricer_bff.CATALOG_GOODS_CACHE == {}


@pytest.mark.parametrize("payload", [{}, {"listGoods": None}, {"listGoods": [{"vendorCode": "no-id"}]}])
def test_bad_upstream_page_is_not_an_empty_catalog(monkeypatch, payload):
    monkeypatch.setattr(repricer_bff, "build_wb_client", lambda *a, **kw: object())
    monkeypatch.setattr(repricer_bff, "_request_catalog_goods_page", lambda *a, **kw: payload)
    with pytest.raises(HTTPException):
        repricer_bff.fetch_catalog_goods_page("complete")


def test_browser_prices_do_not_trigger_redundant_catalog_enrichment(monkeypatch):
    monkeypatch.setattr(repricer_bff, "build_wb_client", lambda *a, **kw: object())
    monkeypatch.setattr(repricer_bff, "_request_catalog_goods_page", lambda *a, **kw: {"listGoods": [good(1)]})
    def unexpected(*a, **kw):
        pytest.fail("Browser-selected catalog refresh must not fetch legacy SPP prices")
    monkeypatch.setattr(repricer_bff, "_enrich_goods_page_spp_fields", unexpected)
    assert repricer_bff.fetch_catalog_goods_page("complete", enrich_spp=False)["goods"] == [good(1)]
