"""Connected synthetic WB account → real worker → durable restricted API read.

Only seller verification and provider HTTP are synthetic. Fresh engine instances
prove object/pool independence, not a PostgreSQL/service restart or live rollout.
"""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.wb_live.connection import WbAccountConnection
from app.wb_live.contracts import JobLocator
from app.wb_live.products_http import ProductsQuery
from app.wb_live.products_read import ProductsCursorCodec, read_products_page
from app.wb_live.provider import ReadOnlyWbProvider
from app.wb_live.repository import WbLiveRepository
from app.wb_live.worker import dispatch_due, run_one_batch
from tests import test_orders_schema_candidate as candidate
from tests import test_wb_live_platform as platform
from tests.test_wb_live_worker import card, content

cluster = candidate.cluster
pg_store = platform.pg_store
data = platform.data
live = platform.live


@pytest.fixture(scope="module")
def pg_database(cluster):
    api, worker, dispatcher = ("first_page_" + uuid4().hex for _ in range(3))
    with candidate.disposable_database(cluster, (api, worker, dispatcher)) as database:
        migrated = candidate.migrate(database.url, "upgrade", "head")
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=api), hide_parameters=True)
        try:
            script = (candidate.ROOT / "ops/wb-live-local-grants.sql").read_text()
            script = "\n".join(line for line in script.splitlines() if not line.startswith("\\"))
            for old, new in (("satorna_wb_live", database.name), ("wb_live_owner", database.owner),
                             ("wb_live_api", api), ("wb_live_worker", worker),
                             ("wb_live_dispatch", dispatcher)):
                script = script.replace(old, new)
            with owner.begin() as connection:
                connection.execute(text(script))
            runtime._first_worker = worker
            runtime._first_dispatcher = dispatcher
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def test_connected_account_first_page_survives_fresh_instances_without_price_fallback(live):
    d = live
    api = d.factory.kw["bind"]
    seller = str(uuid4())
    connection = WbAccountConnection(
        session_factory=d.factory, keyring_loader=d.keys,
        seller_verifier=lambda _token: seller,
    )
    account, _credential = connection.connect(
        d.actor, token="synthetic-first-page-token", display_name="Synthetic first page",
    )
    account_id = account["marketplaceAccountId"]
    assert account_id != d.org and account["externalAccountId"] == seller
    view = d.repo.create_job(d.actor, account_id, "synthetic-first-page-request")
    locator = JobLocator(d.org, account_id, view["jobId"])
    worker = create_engine(api.url.set(username=api._first_worker), hide_parameters=True)
    dispatcher = create_engine(api.url.set(username=api._first_dispatcher), hide_parameters=True)
    requests = []
    try:
        queued = []
        dispatch_repo = WbLiveRepository(sessionmaker(dispatcher), d.keys)
        assert dispatch_due(dispatch_repo, lambda *ids: queued.append(ids)) == {"enqueued": 1}
        assert queued == [(d.org, account_id, locator.job_id)]

        def receive(request):
            requests.append((request.method, request.url.host, request.url.path))
            assert request.headers["Authorization"] == "synthetic-first-page-token"
            # Provider I/O must not retain the account authority lock.
            with d.engine.begin() as session:
                session.execute(text("SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=:a FOR UPDATE NOWAIT"), {"a": account_id})
            return httpx.Response(200, json=content([card(10), card(11)]))

        repo = WbLiveRepository(sessionmaker(worker, expire_on_commit=False), d.keys)
        result = run_one_batch(repo, ReadOnlyWbProvider(transport=httpx.MockTransport(receive)), locator)
        assert result == {"status": "complete"}
        assert requests == [("POST", "content-api.wildberries.ru", "/content/v2/get/cards/list")]
    finally:
        worker.dispose()
        dispatcher.dispose()

    fresh = create_engine(api.url, hide_parameters=True)
    try:
        factory = sessionmaker(fresh, expire_on_commit=False)
        reopened = WbLiveRepository(factory, d.keys)
        assert reopened.create_job(d.actor, account_id, "synthetic-first-page-request")["jobId"] == locator.job_id
        states = {source["source"]: source for source in reopened.status(d.actor, account_id)["sources"]}
        assert states["content"]["processed"] == 2
        assert states["prices"]["processed"] == 0
        codec = ProductsCursorCodec(b"synthetic-first-page-cursor-key-32-bytes")
        query = ProductsQuery(limit=1)
        with Session(fresh) as session:
            first = read_products_page(session, actor=d.actor, account_id=account_id, query=query, codec=codec)
            assert first.readiness == "partial" and first.next_cursor is not None
            second = read_products_page(session, actor=d.actor, account_id=account_id, query=query, codec=codec, cursor=first.next_cursor)
            assert second.next_cursor is None
            assert [first.items[0].nm_id, second.items[0].nm_id] == ["10", "11"]
            assert first.read_version == second.read_version
            assert first.items[0].sizes[0].price_kopecks is None
            assert first.items[0].sizes[0].discounted_price_kopecks is None
            assert not session.in_transaction()
        assert requests == [("POST", "content-api.wildberries.ru", "/content/v2/get/cards/list")]
    finally:
        fresh.dispose()
