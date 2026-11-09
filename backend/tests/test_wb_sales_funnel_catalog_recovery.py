"""Funnel recovery regressions. In-memory DB and synthetic provider only."""
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import wb_sales_funnel as funnel
from app.platform.period import Period
from app.wb_sales_funnel_totals import SellerTotals
from app import wb_sales_funnel_catalog as catalog
from app.wb_api.client import WbApiRequest, build_wb_client, build_wb_content_client, build_wb_analytics_client


PERIOD = Period(date(2026, 10, 5), date(2026, 10, 11))


def product(nm=101):
    previous = funnel.previous_period(PERIOD)
    return {"product": {"nmId": nm}, "statistic": {"selected": {
        "period": {"start": str(PERIOD.date_from), "end": str(PERIOD.date_to)},
        "openCount": 100, "cartCount": 20, "orderCount": 8, "orderSum": 1000,
        "buyoutCount": 4, "buyoutSum": 500, "cancelCount": 2, "shareOrderPercent": 3.7,
    }, "past": {
        "period": {"start": str(previous.date_from), "end": str(previous.date_to)},
        "openCount": 80, "cartCount": 10, "orderCount": 4, "orderSum": 500,
        "buyoutCount": 2, "buyoutSum": 250, "cancelCount": 1, "shareOrderPercent": 2,
    }, "comparison": {"openCountDynamic": 25, "cartCountDynamic": 100}}}


class FunnelCatalogRecovery(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        funnel.Cache.__table__.create(self.engine)
        funnel.Product.__table__.create(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.factory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.token = "synthetic-catalog-token"
        self.scope = funnel.scope_key(1, self.token)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(funnel, "get_session_factory", return_value=self.factory))
        for module in (catalog, funnel):
            self.stack.enter_context(patch.object(module, "resolve_bound_wb_credential",
                return_value=(1, "synthetic-ref", self.token)))
        self.stack.enter_context(patch("app.wb_api.report_reads.ReportReadSession.check"))
        self.network = self.stack.enter_context(patch("httpx.Client.request",
            side_effect=AssertionError("No network permitted in this regression suite")))

    def checkpoint(self, request, data, client, *, token=None, org=1, age=None):
        identity = json.dumps([client.base_url, sha256((token or self.token).encode()).hexdigest(),
                               request.model_dump(mode="json")], sort_keys=True, separators=(",", ":"))
        key = "wb_report_http_" + sha256(identity.encode()).hexdigest()
        fetched = datetime.now(timezone.utc) - (age or timedelta())
        funnel.upsert(self.db, org, [{"source_key": key, "fetched_at": fetched,
            "payload": {"response": {"ok": True, "statusCode": 200,
                "request": request.model_dump(mode="json"), "data": data}}}])
        return key

    def saved_catalog(self, goods=None, cards=None, **kwargs):
        goods = [{"nmID": 101, "vendorCode": "Артикул 10"},
                 {"nmID": 102, "vendorCode": "Артикул 2"}] if goods is None else goods
        cards = [{**item, "title": "Synthetic product", "brand": "Synthetic brand",
                  "photos": [{"big": "https://example.invalid/product.jpg"}]} for item in goods] if cards is None else cards
        self.checkpoint(WbApiRequest(method="POST", path="/api/v2/list/goods/filter",
            query={"limit": 1000, "offset": 0}, jsonBody={"limit": 1000, "offset": 0}),
            {"data": {"listGoods": goods}}, build_wb_client(token_override=self.token), **kwargs)
        self.checkpoint(WbApiRequest(method="POST", path="/content/v2/get/cards/list",
            jsonBody={"settings": {"cursor": {"limit": 100}}}),
            {"cards": cards}, build_wb_content_client(token_override=self.token), **kwargs)

    def recover(self):
        catalog.ensure_catalog(self.db, 1, 1, self.scope, self.token)

    def collect(self):
        with patch("app.repricer_cache.store.save_source_cache") as status, \
             patch("app.repricer_bff._request_or_raise_sales_funnel_products",
                   side_effect=AssertionError("Saved complete report must not be downloaded again")):
            funnel.collect(1, 1, self.scope, str(PERIOD.date_from), str(PERIOD.date_to), "synthetic-job")
            return status

    def test_ready_totals_do_not_make_missing_catalog_terminal(self):
        totals = SellerTotals(PERIOD)
        totals.add([product()], currency="RUB")
        totals.save(self.db, 1, 1, self.scope, datetime.now(timezone.utc))
        page = funnel.read_page(self.db, 1, 1, self.scope, PERIOD)
        self.assertEqual(page["totals"]["visits"], 100)
        self.assertTrue(page["summaryComplete"])
        self.assertTrue(page["refreshRequired"])
        self.assertTrue(page["productsRefreshRequired"])
        self.assertEqual(page["catalogState"], "missing")

    def test_saved_catalog_recovers_absolute_values_native_dynamics_and_omissions(self):
        self.saved_roundtrip()

    def test_interrupted_recovery_resumes_from_catalog_without_redownloading_report(self):
        self.saved_roundtrip(already_recovered=True)

    def saved_roundtrip(self, already_recovered=False):
        self.saved_catalog()
        previous = funnel.previous_period(PERIOD)
        self.checkpoint(WbApiRequest(method="POST", path=funnel.SOURCE, jsonBody={
            "selectedPeriod": {"start": str(PERIOD.date_from), "end": str(PERIOD.date_to)},
            "pastPeriod": {"start": str(previous.date_from), "end": str(previous.date_to)},
            "nmIds": [], "skipDeletedNm": False, "limit": 1000, "offset": 0}),
            {"data": {"products": [product()], "currency": "RUB"}},
            build_wb_analytics_client(token_override=self.token))
        # The exact state that used to get stuck: finished listing before any
        # local identities, with a reusable summary and native WB responses.
        totals = SellerTotals(PERIOD)
        totals.add([product()], currency="RUB")
        totals.save(self.db, 1, 1, self.scope, datetime.now(timezone.utc))
        funnel.save_enumeration(self.db, 1, 1, self.scope, PERIOD, {101}, datetime.now(timezone.utc))
        original_receipt_time = funnel.fresh_enumeration(self.db, 1, self.scope, PERIOD)["fetchedAt"]
        if already_recovered:
            self.recover()  # A previous worker published identities, then stopped.
        writes = []
        def capture(conn, cursor, sql, params, context, many):
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(sql)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            self.collect()
            page = funnel.read_page(self.db, 1, 1, self.scope, PERIOD)
            comparison = funnel.read_comparison(self.db, 1, 1, self.scope, PERIOD, [101])
            self.collect()
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["catalogState"], "ready")
        self.assertFalse(page["refreshRequired"])
        self.assertTrue(page["reportComplete"])
        self.assertEqual(funnel.fresh_enumeration(self.db, 1, self.scope, PERIOD)["fetchedAt"], original_receipt_time)
        by_id = {item["nmId"]: item for item in page["items"]}
        self.assertEqual(by_id["101"]["metrics"]["visits"], 100)
        self.assertEqual(by_id["101"]["title"], "Synthetic product")
        self.assertIsNone(by_id["102"]["metrics"]["visits"])
        self.assertFalse(by_id["102"]["inReport"])
        self.assertEqual(comparison["items"][0]["dynamics"]["visits"], 25)
        self.assertEqual(page["totals"]["visits"], 100)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 2)
        self.assertTrue(writes)
        self.assertTrue(all("marketplace_products" in sql or "wb_repricer_source_cache" in sql for sql in writes))
        self.network.assert_not_called()

    def test_existing_catalog_is_untouched_and_skips_all_recovery(self):
        self.db.add(funnel.Product(organization_id=1, marketplace_account_id=1,
            external_product_id="999", seller_article="User article", title="User title"))
        self.db.commit()
        self.saved_catalog()
        with patch.object(catalog, "_saved_goods", side_effect=AssertionError("Existing catalog must not recover")):
            self.recover()
        rows = self.db.scalars(select(funnel.Product)).all()
        self.assertEqual([(row.external_product_id, row.title) for row in rows], [("999", "User title")])

    def test_recovery_is_idempotent(self):
        self.saved_catalog()
        self.recover()
        self.recover()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 2)
        self.network.assert_not_called()

    def test_first_load_uses_only_catalog_reads_and_does_not_enrich_spp(self):
        goods = [{"nmID": 201, "vendorCode": "new"}]
        cards = [{"nmID": 202, "vendorCode": "unpriced", "title": "Another catalog card"}]
        with patch("app.repricer_bff.fetch_catalog_goods_page", return_value={"goods": goods}) as fetch_goods, \
             patch("app.repricer_bff._fetch_content_cards", return_value=cards) as fetch_cards:
            self.recover()
        self.assertEqual(fetch_goods.call_count, 1)
        self.assertFalse(fetch_goods.call_args.kwargs["enrich_spp"])
        self.assertEqual(fetch_cards.call_count, 1)
        self.assertEqual(set(self.db.scalars(select(funnel.Product.external_product_id))), {"201", "202"})

    def test_foreign_or_unverifiable_cache_is_not_used(self):
        self.saved_catalog(token="another-synthetic-token")
        self.saved_catalog(org=2)
        funnel.upsert(self.db, 1, [{"source_key": "content_cards", "fetched_at": datetime.now(timezone.utc),
            "payload": {"cards": [{"nmID": 666, "vendorCode": "unverified"}]}}])
        with patch("app.repricer_bff.fetch_catalog_goods_page", side_effect=HTTPException(403)) as fetch:
            with self.assertRaises(HTTPException):
                self.recover()
        fetch.assert_called_once()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 0)
        self.assertEqual(catalog.catalog_state(self.db, 1, 1, self.scope), "missing")

    def test_binding_is_rechecked_before_publication(self):
        self.saved_catalog()
        with patch.object(catalog, "resolve_bound_wb_credential", return_value=(2, "new", "changed-token")):
            with self.assertRaisesRegex(RuntimeError, "WB_ACCOUNT_CHANGED"):
                self.recover()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 0)

    def test_partial_catalog_failure_does_not_publish_identities_or_empty_state(self):
        self.saved_catalog()
        with patch.object(catalog, "_saved_cards", return_value=None), \
             patch("app.repricer_bff._fetch_content_cards", side_effect=HTTPException(429)):
            with self.assertRaises(HTTPException):
                self.recover()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 0)
        self.assertEqual(catalog.catalog_state(self.db, 1, 1, self.scope), "missing")

    def test_confirmed_empty_is_distinct_from_missing_and_expires_normally(self):
        self.saved_catalog(goods=[], cards=[])
        self.recover()
        self.assertEqual(catalog.catalog_state(self.db, 1, 1, self.scope), "empty")
        self.assertFalse(funnel.read_page(self.db, 1, 1, self.scope, PERIOD)["productsRefreshRequired"])
        receipt = self.db.scalar(select(funnel.Cache).where(funnel.Cache.source_key == f"{self.scope}_catalog_v1"))
        receipt.payload = {**receipt.payload, "fetchedAt": (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()}
        self.db.commit()
        self.assertEqual(catalog.catalog_state(self.db, 1, 1, self.scope), "missing")

    def test_filtered_empty_does_not_become_missing_or_empty_account(self):
        self.saved_catalog()
        self.recover()
        page = funnel.read_page(self.db, 1, 1, self.scope, PERIOD, query="not-found")
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["catalogState"], "ready")
        self.assertFalse(page["productsRefreshRequired"])

    def test_sorting_uses_full_catalog_before_pagination(self):
        self.saved_catalog()
        self.recover()
        for direction, ids in [("asc", ["102", "101"]), ("desc", ["101", "102"])]:
            pages = [funnel.read_page(self.db, 1, 1, self.scope, PERIOD,
                sort="sku", direction=direction, page=page, page_size=1) for page in (1, 2)]
            self.assertEqual([page["items"][0]["nmId"] for page in pages], ids)

    def test_a_different_period_cannot_reuse_selected_period_metrics(self):
        self.saved_catalog()
        self.recover()
        funnel.upsert(self.db, 1, funnel.observation_rows(self.scope, PERIOD.date_from, PERIOD.date_to,
            [product()], datetime.now(timezone.utc), seller_wide=True))
        funnel.project(self.db, 1, 1, self.scope, PERIOD)
        other = Period(date(2026, 10, 12), date(2026, 10, 18))
        page = funnel.read_page(self.db, 1, 1, self.scope, other)
        self.assertTrue(page["refreshRequired"])
        self.assertTrue(all(item["metrics"].get("visits") is None for item in page["items"]))

    def test_cancellation_before_publication_leaves_catalog_untouched(self):
        from app.wb_api.report_reads import ReportDemandEnded, active_report_read
        self.saved_catalog()
        check = SimpleNamespace(check=lambda: (_ for _ in ()).throw(ReportDemandEnded()))
        context = active_report_read.set(check)
        try:
            with self.assertRaises(ReportDemandEnded):
                self.recover()
        finally:
            active_report_read.reset(context)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 0)

    def test_duplicate_or_incomplete_saved_listing_is_not_published(self):
        self.saved_catalog(goods=[{"nmID": 101}, {"nmID": 101}])
        with self.assertRaises(HTTPException):
            self.recover()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 0)
        self.assertEqual(catalog.catalog_state(self.db, 1, 1, self.scope), "missing")

    def test_incomplete_saved_catalog_fetches_missing_source_instead_of_publishing_subset(self):
        self.saved_catalog(goods=[{"nmID": nm} for nm in range(1, 1001)], cards=[])
        with patch("app.repricer_bff.fetch_catalog_goods_page", return_value={"goods": [{"nmID": 9999}]}) as fetch:
            self.recover()
        fetch.assert_called_once()
        self.assertEqual(list(self.db.scalars(select(funnel.Product.external_product_id))), ["9999"])

    def test_two_concurrent_refresh_requests_share_one_existing_job(self):
        from app.routers import wb_sales_funnel as api
        actor = SimpleNamespace(organization_id=1, user_id="synthetic")
        jobs, locks = {}, {}
        def acquire(key, value, **kwargs):
            if key in locks:
                return False
            locks[key] = value
            return True
        redis = SimpleNamespace(set=acquire)
        with patch.object(api, "_scope", return_value=(actor, 1, self.scope, PERIOD)), \
             patch.object(api, "get_session_factory", return_value=self.factory), \
             patch.object(api, "get_source_cache", side_effect=lambda org, key, **kw: jobs.get(key)), \
             patch.object(api, "save_source_cache", side_effect=lambda org, key, value, **kw: jobs.update({key: value})), \
             patch.object(api, "get_redis_client", return_value=redis), \
             patch.object(api, "touch_demand"), \
             patch("app.repricer_tasks.refresh_sales_funnel.apply_async") as enqueue:
            results = [api.refresh_funnel(SimpleNamespace(), PERIOD.date_from, PERIOD.date_to,
                marketplaceAccountId=1, nativeComparison=False, aggregateComparison=False) for _ in range(2)]
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual([item["status"] for item in results], ["queued", "queued"])

    def test_concurrent_identity_insert_keeps_existing_user_metadata(self):
        self.saved_catalog()
        def bind(*args, **kwargs):
            self.db.add(funnel.Product(organization_id=1, marketplace_account_id=1,
                external_product_id="101", seller_article="User article", title="User title"))
            self.db.commit()
            return 1, "synthetic-ref", self.token
        with patch.object(catalog, "resolve_bound_wb_credential", side_effect=bind):
            self.recover()
        rows = {row.external_product_id: row for row in self.db.scalars(select(funnel.Product))}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["101"].title, "User title")
        self.assertEqual(rows["101"].seller_article, "User article")

    def test_failed_publication_rolls_back_new_identities(self):
        self.saved_catalog()
        with patch.object(funnel, "upsert", side_effect=RuntimeError("Synthetic cache save failure")):
            with self.assertRaisesRegex(RuntimeError, "Synthetic cache save failure"):
                self.collect()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 0)

    def test_complete_paginated_catalog_reuses_all_saved_pages(self):
        client = build_wb_client(token_override=self.token)
        for offset, ids in [(0, range(1, 1001)), (1000, [1001])]:
            self.checkpoint(WbApiRequest(method="POST", path="/api/v2/list/goods/filter",
                query={"limit": 1000, "offset": offset}, jsonBody={"limit": 1000, "offset": offset}),
                {"data": {"listGoods": [{"nmID": nm, "vendorCode": f"item-{nm}"} for nm in ids]}}, client)
        client = build_wb_content_client(token_override=self.token)
        for cursor, ids in [({}, range(1, 101)), ({"updatedAt": "synthetic-cursor", "nmID": 100}, [1001])]:
            self.checkpoint(WbApiRequest(method="POST", path="/content/v2/get/cards/list",
                jsonBody={"settings": {"cursor": {"limit": 100, **cursor}}}),
                {"cards": [{"nmID": nm, "vendorCode": f"item-{nm}", "title": f"Title {nm}"} for nm in ids],
                 "cursor": {"updatedAt": "synthetic-cursor", "nmID": 100}}, client)
        self.recover()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(funnel.Product)), 1001)
        last = self.db.scalar(select(funnel.Product).where(funnel.Product.external_product_id == "1001"))
        self.assertEqual(last.title, "Title 1001")
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
