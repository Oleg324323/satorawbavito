"""Durable last-observation semantics; no live account, token or WB request."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.cabinet.orm import LkOrganizationRow, LkSessionRow, LkUserRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.routers import wb_browser_prices as api
from app.wb_browser_prices import apply_browser_prices
from ops.local_wb_browser_prices import install_local_browser_price_storage

PREFIX = "/api/v1/wb/browser-prices"


class UtcDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class DurableBrowserPrices(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        models = (LkOrganizationRow, LkUserRow, LkSessionRow, IamMembershipRow,
                  MarketplaceAccountRow, WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow)
        for model in models:
            for column in model.__table__.columns:
                if isinstance(column.type, DateTime) and column.type.timezone:
                    self.stack.enter_context(patch.object(column, "type", UtcDateTime(timezone=True)))
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        for model in models:
            model.__table__.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.stack.enter_context(patch("app.config.get_settings", return_value=SimpleNamespace(environment="local")))
        self.stack.enter_context(patch("app.infra.db.get_session_factory", return_value=self.factory))
        for name in ("_session", "_now", "_refresh_lock", "require_live_actor"):
            self.stack.enter_context(patch.object(api, name, getattr(api, name)))
        install_local_browser_price_storage()
        self.now = datetime.now(timezone.utc)
        self.goods = [{"vendorCode": "A", "nmID": 101, "sizes": [{"sizeID": 1001, "discountedPrice": 1000}]},
                      {"vendorCode": "B", "nmID": 102, "sizes": [{"sizeID": 2001, "discountedPrice": 2000}]}]
        with self.factory() as session, session.begin():
            session.add(LkOrganizationRow(organization_id=1, slug="durable", name="Synthetic"))
            session.add(LkUserRow(user_id="issuer", organization_id=1, email="durable@example.invalid",
                password_hash="unused", full_name="Synthetic", permission_profile="custom", is_active=True))
            session.add(LkSessionRow(session_id="login", user_id="issuer", issued_at=self.now,
                last_seen_at=self.now, expires_at=self.now + timedelta(days=3)))
            session.add(IamMembershipRow(organization_id=1, user_id="issuer", role="custom",
                permissions=["integrations:write"], scope_mode="all", is_active=True))
            session.add(MarketplaceAccountRow(marketplace_account_id=10, organization_id=1, marketplace="wb",
                external_account_id="synthetic", status="connected"))
            session.add(WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=1000,
                fetched_at=self.now, goods_payload=deepcopy(self.goods)))
        actor = ActorContext("issuer", "issuer", 1, "custom", frozenset(), session_id="login")
        self.stack.enter_context(patch.object(api, "actor_from_request", return_value=actor))
        app = FastAPI()
        app.include_router(api.router)
        self.client = self.stack.enter_context(TestClient(app))
        issued = self.client.post(PREFIX + "/connection", json={"marketplaceAccountId": 10})
        self.assertEqual(issued.status_code, 200)
        self.headers = {"Authorization": "Bearer " + issued.json()["token"]}
        catalog = self.client.get(PREFIX + "/catalog", headers=self.headers)
        self.assertEqual(catalog.status_code, 200)
        self.revision = catalog.json()["goodsRevision"]

    def tearDown(self):
        self.stack.close()
        self.engine.dispose()

    def read(self, key="wb_browser_prices"):
        with self.factory() as session:
            row = session.scalar(select(WbRepricerSourceCacheRow).where(WbRepricerSourceCacheRow.source_key == key))
            return deepcopy(row.payload)

    def observation(self, nm=101, **changes):
        return {"nmId": nm, "sizeId": 1001 if nm == 101 else 2001,
                "sellerPriceKopecks": 100000 if nm == 101 else 200000,
                "buyerPriceNoWalletKopecks": 80000 if nm == 101 else 160000,
                "buyerPriceWithWalletKopecks": None, "observedAt": self.now.isoformat(), **changes}

    def send(self, *items):
        return self.client.post(PREFIX + "/snapshots", headers=self.headers,
                                json={"goodsRevision": self.revision, "items": list(items)})

    def seed_yesterday(self):
        self.assertEqual(self.send(self.observation()).status_code, 200)
        with self.factory() as session, session.begin():
            row = session.scalar(select(WbRepricerSourceCacheRow).where(WbRepricerSourceCacheRow.source_key == "wb_browser_prices"))
            value = deepcopy(row.payload)
            value["items"]["101:1001"].update(
                observedAt=(self.now - timedelta(days=1)).isoformat(),
                sellerPriceObservedAt=(self.now - timedelta(days=1, minutes=10)).isoformat())
            row.payload = value

    def test_yesterdays_observation_remains_displayed_without_a_new_pass(self):
        self.seed_yesterday()
        before = self.read()
        result = apply_browser_prices(self.goods, self.read("wb_browser_prices_connection"), before, now=self.now)
        value = result[0]["sizes"][0]
        self.assertEqual(value.get("buyerPriceNoWalletKopecks"), 80000)
        self.assertEqual(value["buyerPriceObservedAt"], before["items"]["101:1001"]["observedAt"])
        self.assertEqual(self.read(), before)

    def test_new_partial_pass_keeps_older_unseen_products_and_updates_status(self):
        self.seed_yesterday()
        old = self.read()["items"]["101:1001"]
        self.assertEqual(self.send(self.observation(102)).status_code, 200)
        self.assertEqual(set(self.read()["items"]), {"101:1001", "102:2001"})
        self.assertEqual(self.read()["items"]["101:1001"], old)
        status = self.client.get(PREFIX + "/connection?marketplaceAccountId=10")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["knownPrices"], 2)

    def test_new_same_product_replaces_only_that_product_and_late_response_is_ignored(self):
        self.seed_yesterday()
        self.assertEqual(self.send(self.observation(102)).status_code, 200)
        other = self.read()["items"]["102:2001"]
        response = self.send(self.observation(buyerPriceNoWalletKopecks=79000))
        self.assertEqual(response.json()["accepted"], 1)
        after = self.read()
        self.assertEqual(after["items"]["101:1001"]["buyerPriceNoWalletKopecks"], 79000)
        self.assertEqual(after["items"]["102:2001"], other)
        late = self.send(self.observation(observedAt=(self.now - timedelta(seconds=10)).isoformat()))
        self.assertEqual(late.json()["ignored"], 1)
        self.assertEqual(self.read(), after)

    def test_failed_batch_does_not_remove_previous_observations(self):
        self.seed_yesterday()
        before = self.read()
        self.assertEqual(self.send(self.observation(sizeId=999)).status_code, 409)
        self.assertEqual(self.read(), before)

    def test_catalog_change_does_not_delete_an_unseen_products_saved_observation(self):
        self.seed_yesterday()
        before = self.read()["items"]["101:1001"]
        with self.factory() as session, session.begin():
            page = session.scalar(select(WbRepricerGoodsCacheRow))
            goods = deepcopy(page.goods_payload)
            goods[0]["sizes"][0]["discountedPrice"] = 1100
            page.goods_payload = goods
        catalog = self.client.get(PREFIX + "/catalog", headers=self.headers)
        self.assertEqual(catalog.status_code, 200)
        self.revision = catalog.json()["goodsRevision"]
        self.assertEqual(self.send(self.observation(102)).status_code, 200)
        self.assertEqual(self.read()["items"]["101:1001"], before)

    def test_changed_seller_price_and_foreign_binding_are_still_not_applied(self):
        self.seed_yesterday()
        prices, connection = self.read(), self.read("wb_browser_prices_connection")
        changed = deepcopy(self.goods)
        changed[0]["sizes"][0]["discountedPrice"] = 1100
        for goods, data in ((changed, prices), (self.goods, {**prices, "marketplaceAccountId": 20})):
            value = apply_browser_prices(goods, connection, data, now=self.now)[0]["sizes"][0]
            self.assertNotIn("buyerPriceNoWalletKopecks", value)


if __name__ == "__main__":
    unittest.main()
