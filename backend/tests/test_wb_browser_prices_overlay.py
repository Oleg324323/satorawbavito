from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.wb_browser_prices import apply_browser_prices, rows_expire_at


def test_browser_prices_are_exact_persistent_and_never_fall_back_to_old_provider():
    now = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)
    goods = [{"nmID": 123, "walletPct": 5, "buyerPrice": 900, "sizes": [{
        "sizeID": 456, "price": 2000, "discountedPrice": 1500,
        "buyerPriceSource": "wbcon", "buyerPriceKopecks": 100000,
    }]}]
    original = deepcopy(goods)
    connection = {"marketplaceAccountId": 7, "activeHash": "hash", "ingestionBindingVersion": 1,
                  "expiresAt": (now + timedelta(days=1)).isoformat()}
    observation = {"nmId": 123, "sizeId": 456, "sellerPriceKopecks": 150000,
                   "buyerPriceNoWalletKopecks": 130800, "buyerPriceWithWalletKopecks": None,
                   "observedAt": now.isoformat(), "sellerPriceObservedAt": (now - timedelta(minutes=10)).isoformat()}
    prices = {"marketplaceAccountId": 7, "accountBindingVersion": 1, "items": {"123:456": observation}}
    apply = lambda conn=connection, data=prices: apply_browser_prices(goods, conn, data, now=now)
    result = apply()
    size = result[0]["sizes"][0]
    assert size["buyerPriceNoWalletKopecks"] == 130800
    assert size["buyerPriceWithWalletKopecks"] is None
    assert size["buyerPriceExpiresAt"] == connection["expiresAt"]
    assert "walletPct" not in result[0] and goods == original
    assert apply(None) == original  # Existing tenants keep their source.
    for invalid in (
        {**observation, "sellerPriceKopecks": 150001}, {**observation, "sizeId": 457},
        {**observation, "buyerPriceNoWalletKopecks": True},
        {**observation, "observedAt": (now + timedelta(seconds=31)).isoformat()},
        {**observation, "sellerPriceObservedAt": (now + timedelta(seconds=31)).isoformat()},
    ):
        assert "buyerPriceNoWalletKopecks" not in apply(data={**prices, "items": {"123:456": invalid}})[0]["sizes"][0]
    for conn, data in (({**connection, "activeHash": None}, prices), (connection, {**prices, "marketplaceAccountId": 8}),
                       (connection, {**prices, "accountBindingVersion": 2})):
        result = apply(conn, data)
        assert "buyerPrice" not in result[0] and "buyerPriceKopecks" not in result[0]["sizes"][0]
    assert rows_expire_at([{"analytics": size}]) == size["buyerPriceExpiresAt"]
    yesterday = {**observation, "observedAt": (now - timedelta(days=1)).isoformat(),
                 "sellerPriceObservedAt": (now - timedelta(days=1, minutes=10)).isoformat()}
    assert apply(data={**prices, "items": {"123:456": yesterday}})[0]["sizes"][0]["buyerPriceNoWalletKopecks"] == 130800


def test_cached_snapshot_expires_with_its_browser_prices(monkeypatch):
    from app.routers import wb_repricer_bff as router
    cache = {"version": router.SKU_LIST_SNAPSHOT_VERSION,
             "browserPricesExpireAt": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    monkeypatch.setattr(router, "get_source_cache", lambda *args, **kwargs: cache)
    monkeypatch.setattr(router, "_repricer_view_state_revision", lambda: (_ for _ in ()).throw(AssertionError("expired snapshot must stop here")))
    assert router._load_repricer_sku_snapshot(1, "complete", period_suffix="custom", include_promotions=False, include_content=False) is None


def test_missing_database_never_reenables_external_provider(monkeypatch):
    from app.repricer_cache import store
    def unavailable():
        raise SQLAlchemyError("storage unavailable")
    monkeypatch.setattr(store, "get_session_factory", unavailable)
    monkeypatch.setattr(store, "_redis_get_json_list", lambda _: [{"nmID": 123, "buyerPrice": 900}])
    with pytest.raises(SQLAlchemyError):
        store.list_cached_goods(2)
    with pytest.raises(SQLAlchemyError):
        store.browser_prices_selected(2)
