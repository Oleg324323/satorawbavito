from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.orders import (
    ExternalOrderItemIdentity,
    make_avito_source_line_key,
    map_avito_status,
)
from app.orders.contracts import CatalogResolution
from app.orders.evidence_repository import OrdersEvidenceRepository
from app.orders.ingestion import ObservedOrderItem
from app.orders.projection_repository import OrdersProjectionRepository
from tests.test_orders_evidence_repository import fact, new_run
from tests.test_orders_exact_text_migration import db as _db_fixture
from tests.test_orders_schema_candidate import cluster, scope  # noqa: F401

projection_db = _db_fixture


def item_fact():
    row = fact()
    items = tuple(
        ObservedOrderItem(
            ExternalOrderItemIdentity(
                row.identity,
                make_avito_source_line_key(
                    row.identity.external_order_id, "synthetic-listing", occurrence
                ),
                "synthetic-listing",
                occurrence,
            ),
            3,
        )
        for occurrence in range(2)
    )
    return replace(row, items=items)


def unmapped():
    return CatalogResolution("unmapped", None, None, None, "synthetic-resolution-v1")


def test_item_projection_preserves_repeated_lines_and_stored_quantity(projection_db):
    _, runtime = projection_db
    row = item_fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        stored = OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
        repo = OrdersProjectionRepository(session, 91001, 91101)
        result = [
            repo.set_item(
                run,
                stored.observation_id,
                item.identity.source_line_key,
                resolution=unmapped(),
                expected_version=None,
            )
            for item in row.items
        ]
        assert result[0][0] != result[1][0]
        assert [version for _, version in result] == [1, 1]
        values = session.execute(
            text("""SELECT occurrence_index,quantity,resolution_state,
            catalog_sku_id FROM marketplace_order_items WHERE order_id=:order
            ORDER BY occurrence_index"""),
            {"order": stored.order_id},
        ).all()
        assert [tuple(value) for value in values] == [
            (0, 3, "unmapped", None),
            (1, 3, "unmapped", None),
        ]
        with pytest.raises(ValueError, match="line"):
            repo.set_item(
                run,
                stored.observation_id,
                "synthetic-missing-line",
                resolution=unmapped(),
                expected_version=None,
            )


def test_item_projection_cas_and_caller_rollback(projection_db):
    _, runtime = projection_db
    row = item_fact()
    key = row.items[0].identity.source_line_key
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        stored = OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
        item_id, _ = OrdersProjectionRepository(session, 91001, 91101).set_item(
            run,
            stored.observation_id,
            key,
            resolution=unmapped(),
            expected_version=None,
        )
    with Session(runtime) as session:
        with pytest.raises(RuntimeError), session.begin():
            scope(session)
            repo = OrdersProjectionRepository(session, 91001, 91101)
            assert repo.set_item(
                run,
                stored.observation_id,
                key,
                resolution=unmapped(),
                expected_version=1,
            ) == (item_id, 2)
            with pytest.raises(ValueError, match="version"):
                repo.set_item(
                    run,
                    stored.observation_id,
                    key,
                    resolution=unmapped(),
                    expected_version=1,
                )
            raise RuntimeError("synthetic publication rollback")
        with session.begin():
            scope(session)
            assert (
                session.execute(
                    text(
                        "SELECT version FROM marketplace_order_items WHERE order_item_id=:id"
                    ),
                    {"id": item_id},
                ).scalar_one()
                == 1
            )


def test_projection_uses_stored_fact_and_scoped_membership(projection_db):
    _, runtime = projection_db
    row = replace(fact(), status=map_avito_status("canceled"))
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        stored = OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
        version = OrdersProjectionRepository(session, 91001, 91101).set_parent(
            run, stored.observation_id, expected_version=1
        )
        assert version == 2
        status = session.execute(
            text(
                "SELECT raw_status,canonical_status,last_seen_sync_run_id FROM marketplace_orders WHERE order_id=:id"
            ),
            {"id": stored.order_id},
        ).one()
        assert tuple(status) == ("canceled", "cancelled", run)
        with pytest.raises(ValueError, match="membership"):
            OrdersProjectionRepository(session, 91001, 91102).set_parent(
                run, stored.observation_id, expected_version=2
            )


def test_two_sessions_parent_cas_has_one_winner(projection_db):
    _, runtime = projection_db
    row = fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        evidence = OrdersEvidenceRepository(session, 91001, 91101)
        runs = [new_run(session, row), new_run(session, row)]
        stored = [evidence.append(run, row) for run in runs]
    barrier = Barrier(2)

    def update(pair):
        run, record = pair
        with Session(runtime) as session, session.begin():
            scope(session)
            session.execute(text("SET LOCAL lock_timeout='5s'"))
            barrier.wait(timeout=5)
            try:
                return OrdersProjectionRepository(session, 91001, 91101).set_parent(
                    run, record.observation_id, expected_version=1
                )
            except ValueError as exc:
                assert "version" in str(exc)
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, zip(runs, stored, strict=True)))
    assert results.count(2) == results.count("conflict") == 1


def test_terminal_run_cannot_mutate_projection(projection_db):
    _, runtime = projection_db
    row = fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        stored = OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
        session.execute(
            text(
                "UPDATE order_sync_runs SET state='partial',completed_at=clock_timestamp() WHERE sync_run_id=:id"
            ),
            {"id": run},
        )
        with pytest.raises(ValueError, match="terminal"):
            OrdersProjectionRepository(session, 91001, 91101).set_parent(
                run, stored.observation_id, expected_version=1
            )


def test_projection_rolls_back_with_caller_transaction(projection_db):
    _, runtime = projection_db
    row = replace(fact(), status=map_avito_status("canceled"))
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        stored = OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
    with Session(runtime) as session:
        with pytest.raises(RuntimeError), session.begin():
            scope(session)
            OrdersProjectionRepository(session, 91001, 91101).set_parent(
                run, stored.observation_id, expected_version=1
            )
            raise RuntimeError("synthetic publication failure")
        with session.begin():
            scope(session)
            current = session.execute(
                text(
                    "SELECT version,canonical_status,last_seen_sync_run_id FROM marketplace_orders WHERE order_id=:id"
                ),
                {"id": stored.order_id},
            ).one()
            assert tuple(current) == (1, None, None)


def test_two_sessions_item_cas_publish_one_quantity_revision(projection_db):
    _, runtime = projection_db
    row = item_fact()
    key = row.items[0].identity.source_line_key
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        evidence = OrdersEvidenceRepository(session, 91001, 91101)
        stored = evidence.append(run, row)
        item_id, _ = OrdersProjectionRepository(session, 91001, 91101).set_item(
            run,
            stored.observation_id,
            key,
            resolution=unmapped(),
            expected_version=None,
        )
        changed = replace(row, items=(replace(row.items[0], quantity=5), row.items[1]))
        pairs = []
        for _ in range(2):
            new = new_run(session, changed)
            pairs.append((new, evidence.append(new, changed).observation_id))
    barrier = Barrier(2)

    def update(pair):
        with Session(runtime) as session, session.begin():
            scope(session)
            session.execute(text("SET LOCAL lock_timeout='5s'"))
            pid = session.execute(text("SELECT pg_backend_pid()")).scalar_one()
            barrier.wait(timeout=5)
            try:
                result = OrdersProjectionRepository(session, 91001, 91101).set_item(
                    *pair, key, resolution=unmapped(), expected_version=1
                )
                return pid, result[1]
            except ValueError as exc:
                assert "version" in str(exc)
                return pid, "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(update, pairs))
    assert results[0][0] != results[1][0]
    assert [result[1] for result in results].count(2) == 1
    assert [result[1] for result in results].count("conflict") == 1
    with Session(runtime) as session, session.begin():
        scope(session)
        assert tuple(
            session.execute(
                text(
                    "SELECT quantity,version FROM marketplace_order_items WHERE order_item_id=:id"
                ),
                {"id": item_id},
            ).one()
        ) == (5, 2)


def test_item_resolution_cannot_bind_another_account_product(projection_db):
    _, runtime = projection_db
    row = item_fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        sku = session.execute(
            text("""INSERT INTO catalog_skus(organization_id,code)
            VALUES (91001,:code) RETURNING catalog_sku_id"""),
            {"code": row.identity.external_order_id},
        ).scalar_one()
        product = session.execute(
            text("""INSERT INTO marketplace_products
            (organization_id,marketplace_account_id,external_product_id)
            VALUES (91001,91102,:external) RETURNING marketplace_product_id"""),
            {"external": row.identity.external_order_id},
        ).scalar_one()
    with pytest.raises(IntegrityError), Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        stored = OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
        OrdersProjectionRepository(session, 91001, 91101).set_item(
            run,
            stored.observation_id,
            row.items[0].identity.source_line_key,
            resolution=CatalogResolution(
                "resolved", product, None, sku, "synthetic-v1"
            ),
            expected_version=None,
        )
