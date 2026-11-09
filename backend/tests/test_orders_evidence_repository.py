from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.orders import ExternalOrderIdentity, map_avito_status
from app.orders.evidence_repository import OrdersEvidenceRepository
from app.orders.ingestion import OrderObservation
from tests.test_orders_exact_text_migration import db as _db_fixture
from tests.test_orders_ingestion import NOW
from tests.test_orders_schema_candidate import cluster, scope  # noqa: F401

evidence_db = _db_fixture


def fact():
    return OrderObservation(
        ExternalOrderIdentity(91001, 91101, "avito", "synthetic-" + uuid4().hex),
        "avito-order-management",
        "synthetic-adapter-v1",
        None,
        None,
        NOW,
        map_avito_status("synthetic_unknown"),
        (),
    )


def new_run(session, row):
    return session.execute(
        text("""INSERT INTO order_sync_runs
        (organization_id,marketplace_account_id,marketplace,source_kind,source_run_key,
         adapter_version,mapping_version,source_contract_version,source_snapshot)
        VALUES (:org,:account,:marketplace,:source,:key,:adapter,:mapping,'synthetic-v1','synthetic-snapshot')
        RETURNING sync_run_id"""),
        {
            "org": row.identity.organization_id,
            "account": row.identity.marketplace_account_id,
            "marketplace": row.identity.marketplace,
            "source": row.source_kind,
            "key": uuid4().hex,
            "adapter": row.adapter_version,
            "mapping": row.status.mapping_version,
        },
    ).scalar_one()


def test_replay_new_run_keeps_original_observation_and_receipt(evidence_db):
    _, runtime = evidence_db
    row = fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        first_run = new_run(session, row)
        repo = OrdersEvidenceRepository(session, 91001, 91101)
        first = repo.append(first_run, row)
    incoming = replace(row, observed_at=NOW + timedelta(days=1))
    with Session(runtime) as session, session.begin():
        scope(session)
        second_run = new_run(session, row)
        replay = OrdersEvidenceRepository(session, 91001, 91101).append(
            second_run, incoming
        )
        assert replay.observation_id == first.observation_id
        assert replay.observation.observed_at == NOW
        assert replay.replayed
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM order_sync_memberships WHERE observation_id=:id"
                ),
                {"id": first.observation_id},
            ).scalar_one()
            == 2
        )


def test_terminal_run_and_source_drift_cannot_append(evidence_db):
    _, runtime = evidence_db
    row = fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        repo = OrdersEvidenceRepository(session, 91001, 91101)
        with pytest.raises(ValueError, match="binding"):
            repo.append(run, replace(row, adapter_version="synthetic-other"))
        session.execute(
            text(
                "UPDATE order_sync_runs SET state='failed',completed_at=clock_timestamp() WHERE sync_run_id=:id"
            ),
            {"id": run},
        )
        with pytest.raises(ValueError, match="terminal"):
            repo.append(run, row)


def test_rollback_does_not_leave_evidence(evidence_db):
    _, runtime = evidence_db
    row = fact()
    with Session(runtime) as session:
        with pytest.raises(RuntimeError):  # noqa: SIM117 - exercise rollback boundary
            with session.begin():
                scope(session)
                run = new_run(session, row)
                OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
                raise RuntimeError("synthetic rollback")
        with session.begin():
            scope(session)
            assert (
                session.execute(
                    text(
                        "SELECT count(*) FROM marketplace_orders WHERE external_order_id=:id"
                    ),
                    {"id": row.identity.external_order_id},
                ).scalar_one()
                == 0
            )


def test_two_sessions_replay_create_one_fact_and_two_memberships(evidence_db):
    _, runtime = evidence_db
    row = fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        runs = [new_run(session, row), new_run(session, row)]
    barrier = Barrier(2)

    def append(run):
        with Session(runtime) as session, session.begin():
            scope(session)
            session.execute(text("SET LOCAL lock_timeout='5s'"))
            barrier.wait(timeout=5)
            return OrdersEvidenceRepository(session, 91001, 91101).append(run, row)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(append, runs))
    assert results[0].observation_id == results[1].observation_id
    assert sorted(result.replayed for result in results) == [False, True]


def test_same_run_changed_fact_and_wrong_account_are_rejected(evidence_db):
    _, runtime = evidence_db
    row = fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        repo = OrdersEvidenceRepository(session, 91001, 91101)
        repo.append(run, row)
        with pytest.raises(ValueError, match="membership"):
            repo.append(run, replace(row, source_revision="synthetic-new"))
        with pytest.raises(ValueError, match="scope"):
            OrdersEvidenceRepository(session, 91001, 91102).append(run, row)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_append_rejects_stale_snapshot_isolation(evidence_db, isolation):
    _, runtime = evidence_db
    row = fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
    with (
        runtime.connect().execution_options(isolation_level=isolation) as connection,
        Session(connection) as session,
        session.begin(),
    ):
        scope(session)
        with pytest.raises(ValueError, match="READ COMMITTED"):
            OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
    with Session(runtime) as session, session.begin():
        scope(session)
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM marketplace_orders WHERE external_order_id=:id"
                ),
                {"id": row.identity.external_order_id},
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("use_long_keys", [False, True])
def test_waiter_refreshes_exact_replay_after_account_lock(evidence_db, use_long_keys):
    from tests.test_orders_exact_repositories import long_fact

    _, runtime = evidence_db
    row = long_fact() if use_long_keys else fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        first_run, second_run = new_run(session, row), new_run(session, row)
    pids = Queue()

    def waiter():
        with Session(runtime) as session, session.begin():
            scope(session)
            session.execute(text("SET LOCAL lock_timeout='8s'"))
            pids.put(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
            return OrdersEvidenceRepository(session, 91001, 91101).append(
                second_run, row
            )

    with ThreadPoolExecutor(max_workers=1) as workers:
        with Session(runtime) as session, session.begin():
            scope(session)
            holder = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            session.execute(
                text("""SELECT marketplace_account_id FROM marketplace_accounts
                WHERE organization_id=91001 AND marketplace_account_id=91101 FOR UPDATE""")
            )
            future = workers.submit(waiter)
            waiting = pids.get(timeout=5)
            assert waiting != holder
            deadline = monotonic() + 5
            while monotonic() < deadline:
                blocked = session.execute(
                    text("SELECT :holder=ANY(pg_blocking_pids(:waiting))"),
                    {"holder": holder, "waiting": waiting},
                ).scalar_one()
                if blocked or future.done():
                    break
                sleep(0.01)
            assert blocked, "Repository must lock the account before exact lookup"
            first = OrdersEvidenceRepository(session, 91001, 91101).append(
                first_run, row
            )
        replay = future.result(timeout=5)
    assert replay.replayed
    assert replay.order_id == first.order_id
    assert replay.observation_id == first.observation_id
