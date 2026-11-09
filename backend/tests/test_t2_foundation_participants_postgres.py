"""Representative 0075/0077/0078 acceptance on an owned disposable PostgreSQL.

These are SQL-participant tests, NOT permission/workflow authorization tests.
Real migrations, runtime login, parsers and synthetic encrypted credentials;
no provider, worker, production database or inferred business permissions.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.modules.wb_current_sources_postgres import (
    CurrentSourceError,
    CurrentSourceTransaction,
)
from app.modules.wb_price_snapshots import PriceField, parse_goods_price_page
from app.modules.wb_repricing_assignments import AssignmentChange
from app.modules.wb_repricing_state_postgres import (
    AccountStateScope,
    AccountStateTransaction,
    RepricingStateError,
)
from app.modules.wb_source_requests import CollectionRequest, SourceKind
from app.modules.wb_stock_daily_postgres import StockDailyError, StockDailyTransaction
from app.modules.wb_stock_snapshots import StockCount, parse_warehouse_stock_page
from app.platform.integrations import credential_store
from app.security.marketplace_credentials import CredentialKeyring
from tests import test_orders_schema_candidate as candidate
from tests.test_orders_schema_integration import runtime_script
from tests.test_repricer_approvals_schema import seed

cluster = candidate.cluster


@pytest.fixture(scope="module")
def participants_db(cluster):
    role = "t2_foundation_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        migrated = candidate.migrate(database.url, "upgrade", "head")
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as connection:
                seed(connection)
                connection.exec_driver_sql(
                    "INSERT INTO marketplace_products(marketplace_product_id,organization_id,"
                    "marketplace_account_id,external_product_id) VALUES(99,7,42,'synthetic-99')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO marketplace_offers(marketplace_offer_id,organization_id,"
                    "marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id) "
                    "VALUES(99,7,42,99,'synthetic-99',99)"
                )
            grants = runtime_script(owner, role)
            assert grants.returncode == 0, grants.stderr
            yield owner, sessionmaker(bind=runtime)
        finally:
            runtime.dispose()
            owner.dispose()


@pytest.fixture
def source_context(participants_db, monkeypatch):
    owner, sessions = participants_db
    monkeypatch.setattr(
        credential_store,
        "_load_keyring",
        lambda: CredentialKeyring(
            current_key_version=1,
            keys={1: b"s" * 32},
        ),
    )
    monkeypatch.setattr(
        credential_store, "get_session_factory", lambda: sessionmaker(bind=owner)
    )
    identity = credential_store.MarketplaceAccountCredentialOwner(7, 42, "wb")
    credential_store.put_marketplace_credential(
        identity, "wb_api", {"token": "synthetic-only"}
    )
    paired = credential_store.resolve_marketplace_credential_for_fetch(
        identity, "wb_api"
    )
    with owner.connect() as connection:
        received = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    # DB-derived timestamp keeps daily eligibility deterministic across test dates.
    return owner, sessions, paired, received


def stock_request(limit):
    return CollectionRequest(
        7, 42, SourceKind.wb_warehouse, "wb-warehouse-stocks/v1", limit
    )


def collect_stock(sessions, request, paired, received, raw, *, publish):
    page = parse_warehouse_stock_page(
        raw, request=request, offset=0, received_at=received
    )
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        run = tx.create(
            request_key=uuid4(),
            started_at=received - timedelta(seconds=1),
            resolved_credential=paired,
        )
        assert tx.append_page(run.run_id, page, page_no=0, http_status=200)
        sealed = tx.finalize(run.run_id, publish_expected_version=publish)
    return sealed, page


def test_assignment_two_sessions_cas_replay_and_whole_root_rollback(participants_db):
    owner, sessions = participants_db
    scope = AccountStateScope(7, 42, 99)
    with owner.connect() as connection:
        now = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    first = AssignmentChange(
        7, 42, 99, 77, str(uuid4()), 0, "baskets_orders", None, now, "manual"
    )
    with sessions.begin() as session:
        initial = AccountStateTransaction(
            session, scope, max_request_bytes=8192
        ).replace(first)
    barrier = Barrier(2)

    def compete(strategy):
        change = replace(
            first, command_id=str(uuid4()), expected_version=1, strategy_id=strategy
        )
        barrier.wait(
            timeout=10
        )  # BEFORE account lock; never rendezvous while holding it.
        try:
            with sessions.begin() as session:
                result = AccountStateTransaction(
                    session, scope, max_request_bytes=8192
                ).replace(change)
            return result
        except RepricingStateError as error:
            assert error.code == "state_conflict"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(compete, strategy)
            for strategy in ("illiquid", "night_price_mode")
        ]
        results = [future.result(timeout=20) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1 and winners[0].revision == 2
    with (
        pytest.raises(RuntimeError, match="synthetic-root-abort"),
        sessions.begin() as session,
    ):
        tx = AccountStateTransaction(session, scope, max_request_bytes=8192)
        assert tx.replace(first) == initial  # Historical replay must not regress head.
        tx.replace(replace(first, command_id=str(uuid4()), expected_version=2))
        raise RuntimeError("synthetic-root-abort")
    with sessions.begin() as session:
        tx = AccountStateTransaction(session, scope, max_request_bytes=8192)
        assert tx.get_current() == winners[0]
        assert [row.revision for row in tx.history(limit=10)] == [2, 1]
    with sessions.begin() as session:
        other = AccountStateTransaction(
            session, AccountStateScope(7, 43, 99), max_request_bytes=8192
        )
        assert other.get_current() is None and other.history(limit=10) == ()
    with owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM wb_repricing_state_audit WHERE organization_id=7 "
                    "AND marketplace_account_id=42 AND catalog_sku_id=99 AND domain='assignment'"
                )
            ).scalar_one()
            == 2
        )


def test_current_stock_same_root_rollback_sealed_replay_and_head_cas(source_context):
    _owner, sessions, paired, received = source_context
    request = stock_request(10)
    raw = b'{"data":[{"nmId":100,"warehouseId":5,"quantity":0}]}'
    first, page = collect_stock(sessions, request, paired, received, raw, publish=0)
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        assert tx.current() == (1, first)
        assert not tx.append_page(first.run_id, page, page_no=0, http_status=200)
        assert tx.finalize(first.run_id) == first
        row = tx.read_page(first.run_id, page_no=0).rows[0]
        assert row.chrt_id is None
        assert row.quantity == StockCount("value", 0)
        assert row.in_way_to_client == StockCount("missing", None)
        pending = tx.create(
            request_key=uuid4(), started_at=received, resolved_credential=paired
        )
        tx.append_page(pending.run_id, page, page_no=0, http_status=200)
    with (
        pytest.raises(RuntimeError, match="synthetic-root-abort"),
        sessions.begin() as session,
    ):
        tx = CurrentSourceTransaction(session, request)
        tx.finalize(pending.run_id, publish_expected_version=1)
        raise RuntimeError("synthetic-root-abort")
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        assert tx.current() == (1, first)
        assert tx.latest_attempt().state == "collecting"
    with (
        pytest.raises(CurrentSourceError, match="source_conflict"),
        sessions.begin() as session,
    ):
        CurrentSourceTransaction(session, request).finalize(
            pending.run_id, publish_expected_version=99
        )
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        assert (
            tx.latest_attempt().state == "collecting"
        )  # Failed CAS rolled back seal too.
        second = tx.finalize(pending.run_id, publish_expected_version=1)
    history_only, _ = collect_stock(
        sessions, request, paired, received, b'{"data":[]}', publish=None
    )
    # A complete history-only run must not be publishable in a later root.
    with (
        pytest.raises(CurrentSourceError, match="source_conflict"),
        sessions.begin() as session,
    ):
        CurrentSourceTransaction(session, request).finalize(
            history_only.run_id, publish_expected_version=2
        )
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        assert tx.current() == (2, second) and tx.latest_attempt() == history_only
        assert tx.read_page(first.run_id, page_no=0).rows == (row,)
        empty = tx.create(
            request_key=uuid4(), started_at=received, resolved_credential=paired
        )
        failed = tx.finalize(empty.run_id)
        assert failed.state == "failed"
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        assert tx.latest_attempt() == failed and tx.current() == (2, second)
    for org, account in ((7, 43), (8, 44)):
        other_request = replace(
            request, organization_id=org, marketplace_account_id=account
        )
        with sessions.begin() as session:
            tx = CurrentSourceTransaction(session, other_request)
            assert tx.current() is None and tx.latest_attempt() is None
        with (
            pytest.raises(CurrentSourceError, match="source_invalid"),
            sessions.begin() as session,
        ):
            CurrentSourceTransaction(session, other_request).read_page(
                first.run_id, page_no=0
            )


def test_price_snapshot_exact_kopecks_and_request_replay(source_context):
    _, sessions, paired, received = source_context
    request = CollectionRequest(7, 42, SourceKind.prices, "wb-goods-prices/v1", 20)
    page = parse_goods_price_page(
        b'{"data":{"listGoods":[{"nmID":100,"discount":0,"sizes":'
        b'[{"sizeID":200,"price":123.45,"discountedPrice":0,"clubDiscountedPrice":null}]}]}}',
        organization_id=7,
        marketplace_account_id=42,
        offset=0,
        limit=20,
        received_at=received,
        request_checksum=request.checksum,
    )
    key = uuid4()
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        run = tx.create(
            request_key=key, started_at=received, resolved_credential=paired
        )
        tx.append_page(run.run_id, page, page_no=0, http_status=200)
        complete = tx.finalize(run.run_id, publish_expected_version=0)
    with sessions.begin() as session:
        tx = CurrentSourceTransaction(session, request)
        assert (
            tx.create(request_key=key, started_at=received, resolved_credential=paired)
            == complete
        )
        assert tx.current() == (1, complete)
        product = tx.read_page(run.run_id, page_no=0).rows[0]
        assert product.club_discount == PriceField("missing", None)
        size = product.known_sizes[0]
        assert size.list_price == PriceField("value", 12345)
        assert size.discounted_price == PriceField("value", 0)
        assert size.club_price == PriceField("null", None)
    with (
        pytest.raises(CurrentSourceError, match="source_conflict"),
        sessions.begin() as session,
    ):
        CurrentSourceTransaction(session, request).create(
            request_key=key,
            started_at=received - timedelta(seconds=1),
            resolved_credential=paired,
        )


def test_daily_evidence_consumes_actual_complete_runs_atomically(source_context):
    owner, sessions, paired, received = source_context
    request = stock_request(30)
    before, _ = collect_stock(
        sessions,
        request,
        paired,
        received,
        b'{"data":[{"nmId":100,"warehouseId":5}]}',
        publish=0,
    )
    after, _ = collect_stock(
        sessions,
        request,
        paired,
        received,
        b'{"data":[{"nmId":100,"warehouseId":5,"quantity":0}]}',
        publish=1,
    )
    day = received.astimezone(ZoneInfo("Europe/Moscow")).date()

    def daily(session, req=request):
        return StockDailyTransaction(
            session, req, day, max_evidence_bytes=65536, max_observations=100
        )

    initial_key = uuid4()
    with sessions.begin() as session:
        initial = daily(session).create_initial(
            source_run_id=before.run_id, command_id=initial_key
        )
    proposal_key, decision_key, apply_key = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        evidence = daily(session).propose(
            command_id=proposal_key,
            before_daily_revision=1,
            after_run_id=after.run_id,
            proposer_membership_id=77,
            evidence_document_bytes=b"synthetic observed source revision",
            reviewed_evidence_reference="synthetic-local-evidence",
        )
        evidence_id = UUID(evidence.proposal.evidence_id)
        decision = daily(session).decide(
            evidence_id=evidence_id,
            command_id=decision_key,
            outcome="accepted",
            reviewer_membership_id=78,
            proposal_checksum=evidence.proposal_checksum,
            reason_code="SOURCE_REVISION_CONFIRMED",
        )
    apply_args = {
        "evidence_id": evidence_id,
        "decision_id": decision.decision_id,
        "command_id": apply_key,
        "actor_membership_id": 78,
        "reason_code": "SOURCE_REVISION_CONFIRMED",
    }
    with (
        pytest.raises(RuntimeError, match="synthetic-root-abort"),
        sessions.begin() as session,
    ):
        daily(session).apply(**apply_args)
        raise RuntimeError("synthetic-root-abort")
    with sessions.begin() as session:
        tx = daily(session)
        assert tx.current() == initial
        assert tx.history(limit=10) == (initial,)
        applied = tx.apply(**apply_args)
    with sessions.begin() as session:
        tx = daily(session)
        assert tx.apply(**apply_args) == applied
        assert (
            tx.create_initial(source_run_id=before.run_id, command_id=initial_key)
            == initial
        )
        assert tx.current() == applied and applied.revision == 2
        assert (
            applied.source_run_id == after.run_id and applied.supersedes_revision == 1
        )
        assert tx.history(limit=10) == (applied, initial)
        assert (
            tx.get_evidence(evidence_id).proposal_checksum == evidence.proposal_checksum
        )
        assert tx.get_decision(evidence_id) == decision
        assert CurrentSourceTransaction(session, request).current() == (2, after)
    with (
        pytest.raises(StockDailyError, match="stock_daily_conflict"),
        sessions.begin() as session,
    ):
        daily(session).apply(**{**apply_args, "command_id": uuid4()})
    with sessions.begin() as session:
        other = daily(session, replace(request, marketplace_account_id=43))
        assert other.current() is None and other.history(limit=10) == ()
    with owner.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM wb_stock_daily_revisions WHERE organization_id=7 "
                    "AND marketplace_account_id=42 AND request_checksum=:checksum"
                ),
                {"checksum": request.checksum},
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM wb_stock_daily_audit WHERE organization_id=7 "
                    "AND marketplace_account_id=42 AND request_checksum=:checksum"
                ),
                {"checksum": request.checksum},
            ).scalar_one()
            == 2
        )
