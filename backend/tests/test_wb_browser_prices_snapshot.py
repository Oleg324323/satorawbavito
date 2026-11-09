from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow
from app.repricer_cache import store
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app.routers import wb_repricer_bff as router
from app.wb_browser_prices import CONNECTION_KEY, PRICES_KEY
from tests.test_empty_database_migrations import cluster, database  # noqa: F401
from tests.test_repricer_stats_baskets import DATE_FROM, DATE_TO, SUFFIX, _source_payload, stats_runtime  # noqa: F401


def test_browser_batches_do_not_change_source_revision_but_revoke_does(database, monkeypatch):
    _, engine = database
    LkOrganizationRow.__table__.create(engine)
    WbRepricerSourceCacheRow.__table__.create(engine)
    now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(LkOrganizationRow(organization_id=1, slug="browser-snapshot", name="Synthetic"))
        session.commit()
        connection = WbRepricerSourceCacheRow(organization_id=1, source_key=CONNECTION_KEY, payload={}, fetched_at=now)
        prices = WbRepricerSourceCacheRow(organization_id=1, source_key=PRICES_KEY, payload={}, fetched_at=now)
        session.add_all([connection, prices])
        session.commit()
        monkeypatch.setattr(store, "_run_db", lambda fn: fn(session))
        before = store.get_repricer_sources_revision(1)
        prices.fetched_at = now + timedelta(seconds=1)
        session.commit()
        assert store.get_repricer_sources_revision(1) == before
        connection.fetched_at = now + timedelta(seconds=2)
        session.commit()
        assert store.get_repricer_sources_revision(1) != before


def test_price_batch_waits_at_most_60_seconds_while_expiry_and_revoke_are_immediate(monkeypatch):
    now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)
    clock = [now]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    cache = {"version": router.SKU_LIST_SNAPSHOT_VERSION, "storage": "chunked", "sourceRevision": "source",
             "goodsRevision": "goods", "stateRevision": "state", "taxRevision": "tax",
             "browserPricesRevision": "batch1", "browserPricesReadAt": now.isoformat()}
    current_batch = ["batch2"]
    current_source = ["source"]
    monkeypatch.setattr(router, "datetime", Clock)
    monkeypatch.setattr(router, "get_source_cache", lambda *a, **kw: deepcopy(cache))
    monkeypatch.setattr(router, "get_source_cache_fetched_at", lambda org, key: current_batch[0])
    monkeypatch.setattr(router, "get_repricer_sources_revision", lambda org: current_source[0])
    monkeypatch.setattr(router, "cached_goods_meta", lambda org: {"latestFetchedAt": "goods"})
    options = dict(period_suffix="custom", include_promotions=False, include_content=False,
                   state_revision="state", tax_revision="tax")
    load = lambda: router._load_repricer_sku_snapshot(1, "complete", **options)
    clock[0] = now + timedelta(seconds=59)
    assert load() is not None
    clock[0] = now + timedelta(seconds=60)
    assert load() is None
    current_batch[0] = "batch1"
    assert load() is not None  # Unchanged observations do not cause periodic rebuilds.
    clock[0] = now + timedelta(seconds=1)
    current_batch[0] = "batch2"
    cache["browserPricesExpireAt"] = clock[0].isoformat()
    assert load() is None
    del cache["browserPricesExpireAt"]
    current_source[0] = "connection-revoked"
    assert load() is None


@pytest.mark.parametrize("use_products", [False, True])
def test_stats_keeps_browser_read_age_including_reused_products(stats_runtime, monkeypatch, use_products):
    _goods, storage, request = stats_runtime
    now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)
    clock = [now]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(router, "datetime", Clock)
    monkeypatch.setattr(router, "get_repricer_sources_revision", lambda org, **scope: "source")
    request(baskets=_source_payload({"10001": {"cartCount": 4}}))
    storage[1, PRICES_KEY] = {"fetchedAt": "batch1"}
    start, end, days, suffix = router._repricer_period_context(
        7, datetime.fromisoformat(DATE_FROM).date(), datetime.fromisoformat(DATE_TO).date())
    build = router.list_repricer_skus

    def slow_build(*args, **kwargs):
        clock[0] = now + timedelta(seconds=22)
        return build(*args, **kwargs)

    monkeypatch.setattr(router, "list_repricer_skus", slow_build)
    if use_products:
        products = router._build_repricer_sku_snapshot(
            1, "complete", wb_token=None, resolved_period_days=days, period_suffix=suffix,
            range_start=start, range_end=end)
        assert products["browserPricesReadAt"] == now.isoformat()
        clock[0] = now + timedelta(seconds=30)
        storage[1, PRICES_KEY] = {"fetchedAt": "batch2"}

        def no_rebuild(*args, **kwargs):
            raise AssertionError("Stats must reuse Products inside the coalescing window")

        monkeypatch.setattr(router, "list_repricer_skus", no_rebuild)
        monkeypatch.setattr(router, "_ensure_repricer_stats_period_caches", no_rebuild)
    request(baskets=None, params={"topMode": "true"})
    snapshot = storage[1, router._repricer_sku_snapshot_key(
        "complete", period_suffix=SUFFIX, include_promotions=False, include_content=False, view="stats")]
    assert snapshot["browserPricesReadAt"] == now.isoformat()
    assert snapshot["browserPricesRevision"] == "batch1"
    assert snapshot["builtAt"] != snapshot["browserPricesReadAt"]
    storage[1, PRICES_KEY] = {"fetchedAt": "batch2"}
    clock[0] = now + timedelta(seconds=60)
    assert router._load_repricer_sku_snapshot(
        1, "complete", period_suffix=SUFFIX, include_promotions=False, include_content=False, view="stats") is None
