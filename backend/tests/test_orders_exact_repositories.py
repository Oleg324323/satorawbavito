"""Consumer acceptance on T1's actual 0064, using only synthetic source evidence."""

from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.orders import ExternalOrderItemIdentity, make_avito_source_line_key
from app.orders.evidence_repository import OrdersEvidenceRepository
from app.orders.ingestion import ObservedOrderItem
from app.orders.projection_repository import OrdersProjectionRepository
from tests.test_orders_evidence_repository import fact, new_run
from tests.test_orders_exact_text_migration import db as _db_fixture
from tests.test_orders_exact_text_migration import latest_db as _latest_fixture
from tests.test_orders_exact_text_migration import long_key
from tests.test_orders_projection_repository import unmapped
from tests.test_orders_schema_candidate import cluster, scope  # noqa: F401

exact_db = _db_fixture
latest_repo_db = _latest_fixture


def long_fact():
    row = fact()
    identity = replace(
        row.identity, external_order_id=long_key(row.identity.external_order_id)
    )
    stable = long_key("synthetic-line:")
    item = ObservedOrderItem(
        ExternalOrderItemIdentity(
            identity,
            make_avito_source_line_key(
                identity.external_order_id, "synthetic-listing", 0, stable
            ),
            "synthetic-listing",
            0,
        ),
        3,
        stable_order_line_id=stable,
    )
    return replace(
        row,
        identity=identity,
        adapter_version=long_key("synthetic-adapter:"),
        items=(item,),
    )


def test_long_order_adapter_line_and_exact_replay(exact_db):
    _, runtime = exact_db
    row = long_fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        repo = OrdersEvidenceRepository(session, 91001, 91101)
        first = repo.append(run, row)
        item_id, version = OrdersProjectionRepository(session, 91001, 91101).set_item(
            run,
            first.observation_id,
            row.items[0].identity.source_line_key,
            resolution=unmapped(),
            expected_version=None,
        )
        assert version == 1
    with Session(runtime) as session, session.begin():
        scope(session)
        replay = OrdersEvidenceRepository(session, 91001, 91101).append(
            new_run(session, row), row
        )
        assert replay.replayed
        assert replay.order_id == first.order_id
        assert replay.observation_id == first.observation_id
        assert replay.observation == row
        assert (
            session.execute(
                text(
                    "SELECT source_line_key FROM marketplace_order_items WHERE order_item_id=:id"
                ),
                {"id": item_id},
            ).scalar_one()
            == row.items[0].identity.source_line_key
        )
        changed = replace(row, source_revision="synthetic-changed")
        revision = OrdersEvidenceRepository(session, 91001, 91101).append(
            new_run(session, changed), changed
        )
        assert not revision.replayed and revision.observation_id != first.observation_id
        assert revision.order_id == first.order_id


def test_long_identical_external_order_ids_keep_account_scope(exact_db):
    _, runtime = exact_db
    row = replace(long_fact(), items=())
    other = replace(row, identity=replace(row.identity, marketplace_account_id=91102))
    with Session(runtime) as session, session.begin():
        scope(session)
        first = OrdersEvidenceRepository(session, 91001, 91101).append(
            new_run(session, row), row
        )
        second = OrdersEvidenceRepository(session, 91001, 91102).append(
            new_run(session, other), other
        )
        assert first.order_id != second.order_id
        assert first.observation_id != second.observation_id
        assert (
            first.observation.identity.external_order_id
            == second.observation.identity.external_order_id
        )


def test_long_evidence_and_item_rollback_together(exact_db):
    _, runtime = exact_db
    row = long_fact()
    with pytest.raises(RuntimeError), Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        stored = OrdersEvidenceRepository(session, 91001, 91101).append(run, row)
        OrdersProjectionRepository(session, 91001, 91101).set_item(
            run,
            stored.observation_id,
            row.items[0].identity.source_line_key,
            resolution=unmapped(),
            expected_version=None,
        )
        raise RuntimeError("synthetic rollback")
    with Session(runtime) as session, session.begin():
        scope(session)
        assert (
            session.execute(
                text(
                    'SELECT count(*) FROM marketplace_orders WHERE external_order_id COLLATE "C"=:id'
                ),
                {"id": row.identity.external_order_id},
            ).scalar_one()
            == 0
        )


def test_exact_repositories_work_under_latest_runtime_grants(latest_repo_db):
    from tests.test_orders_schema_integration import (
        assert_narrow_privileges,
        runtime_script,
    )

    owner, runtime = latest_repo_db
    result = runtime_script(owner, runtime.url.username)
    assert result.returncode == 0, result.stderr
    with owner.connect() as connection:
        assert_narrow_privileges(connection, runtime.url.username)
    row = long_fact()
    with Session(runtime) as session, session.begin():
        scope(session)
        run = new_run(session, row)
        repo = OrdersEvidenceRepository(session, 91001, 91101)
        stored = repo.append(run, row)
        assert repo.append(run, row).replayed
        projections = OrdersProjectionRepository(session, 91001, 91101)
        assert (
            projections.set_parent(run, stored.observation_id, expected_version=1) == 2
        )
        _, version = projections.set_item(
            run,
            stored.observation_id,
            row.items[0].identity.source_line_key,
            resolution=unmapped(),
            expected_version=None,
        )
        assert version == 1
