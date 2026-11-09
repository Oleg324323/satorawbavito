"""Missing local membership recovery through the existing cabinet bridge.

Disposable SQLite only: no working account, credential, browser or WB request.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.cabinet import store
from app.cabinet.orm import LkOrganizationRow, LkSessionRow, LkUserRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.routers import wb_browser_prices as browser
from ops.local_wb_browser_prices import install_local_browser_price_storage

PREFIX = "/api/v1/wb/browser-prices"


class PreviewUtcDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class ExtensionMembershipRepair(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        models = (LkOrganizationRow, LkUserRow, LkSessionRow, IamMembershipRow,
                  MarketplaceAccountRow, WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow)
        # Match the real local launcher's UTC representation, not a PG bypass.
        for model in models:
            for column in model.__table__.columns:
                if isinstance(column.type, DateTime) and column.type.timezone:
                    self.stack.enter_context(patch.object(column, "type", PreviewUtcDateTime(timezone=True)))
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        for model in models:
            model.__table__.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.stack.enter_context(patch("app.config.get_settings", return_value=SimpleNamespace(environment="local")))
        self.stack.enter_context(patch("app.infra.db.get_session_factory", return_value=self.factory))
        for name in ("_session", "_now", "_refresh_lock", "require_live_actor"):
            self.stack.enter_context(patch.object(browser, name, getattr(browser, name)))
        install_local_browser_price_storage()
        self.now = datetime.now(timezone.utc)
        with self.factory() as session, session.begin():
            session.add_all([LkOrganizationRow(organization_id=i, slug=f"repair-{i}", name="Synthetic") for i in (1, 2)])
            session.add(LkUserRow(user_id="repair-user", organization_id=1, email="repair@example.invalid",
                password_hash="unused", full_name="Synthetic", permission_profile="custom", is_active=True))
            session.add(LkSessionRow(session_id="repair-login", user_id="repair-user", issued_at=self.now,
                last_seen_at=self.now, expires_at=self.now + timedelta(hours=1)))
            session.add_all([MarketplaceAccountRow(marketplace_account_id=i, organization_id=org, marketplace="wb",
                external_account_id=f"synthetic-{i}", status="connected") for i, org in ((10, 1), (20, 2))])
        actor = ActorContext("repair-user", "repair-user", 1, "custom", frozenset(), session_id="repair-login")
        self.stack.enter_context(patch.object(browser, "actor_from_request", return_value=actor))
        app = FastAPI()
        app.include_router(browser.router)
        self.client = self.stack.enter_context(TestClient(app))

    def tearDown(self):
        self.stack.close()
        self.engine.dispose()

    def restore(self):
        with self.factory() as session, session.begin():
            user = session.get(LkUserRow, "repair-user")
            store._sync_membership(session, user, ["integrations:write"])

    def test_missing_membership_recovery_allows_account_and_key_generation(self):
        self.assertEqual(self.client.get(PREFIX + "/accounts").status_code, 403)
        self.restore()
        accounts = self.client.get(PREFIX + "/accounts")
        self.assertEqual(accounts.status_code, 200)
        self.assertEqual([value["marketplaceAccountId"] for value in accounts.json()["data"]], [10])
        issued = self.client.post(PREFIX + "/connection", json={"marketplaceAccountId": 10})
        self.assertEqual(issued.status_code, 200)
        body = issued.json()
        self.assertTrue(body["configured"])
        self.assertTrue(body["token"].startswith("sat_wbp_"))
        status = self.client.get(PREFIX + "/connection?marketplaceAccountId=10")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["configured"])
        self.assertNotIn("token", status.json())
        with self.factory() as session:
            payloads = session.scalars(select(WbRepricerSourceCacheRow.payload)).all()
        self.assertNotIn(body["token"], json.dumps(payloads))

    def test_repeated_restore_is_idempotent_and_does_not_expand_permissions(self):
        self.restore()
        self.restore()
        with self.factory() as session:
            members = session.scalars(select(IamMembershipRow)).all()
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0].role, "custom")
            self.assertEqual(members[0].permissions, ["integrations:write"])
            self.assertEqual(session.get(LkUserRow, "repair-user").permission_profile, "custom")

    def test_foreign_account_still_denied_after_restore(self):
        self.restore()
        self.assertEqual(self.client.post(PREFIX + "/connection", json={"marketplaceAccountId": 20}).status_code, 403)
        with self.factory() as session:
            self.assertEqual(session.scalars(select(WbRepricerSourceCacheRow)).all(), [])

    def test_expired_login_still_cannot_generate_a_key(self):
        self.restore()
        with self.factory() as session, session.begin():
            session.get(LkSessionRow, "repair-login").expires_at = self.now - timedelta(minutes=1)
        self.assertEqual(self.client.post(PREFIX + "/connection", json={"marketplaceAccountId": 10}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
