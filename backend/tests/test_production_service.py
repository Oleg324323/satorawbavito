"""Dormant authenticated P1 service; synthetic publication and owned PG only."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.modules.production import AssignmentCommand
from app.orders.production_service import (
    ProductionServiceError,
    assign_production_work_item,
    create_production_work_item,
    read_production_work_item,
)
from tests import test_orders_publication_service as publication
from tests.test_orders_projection_repository import item_fact
from tests.test_orders_read_service import ACCOUNTS

cluster = publication.cluster
read_db = publication.read_db
prepared = publication.prepared
authority = publication.authority
ACCOUNT = ACCOUNTS[0]


@pytest.mark.parametrize("value", [0, -1, True, "1", 2**63])
def test_invalid_ids_fail_before_sql(value):
    with pytest.raises(ProductionServiceError, match="REQUEST_INVALID"):
        create_production_work_item(
            None,
            principal=None,
            account=None,
            order_item_id=value,
            expected_source_item_version=1,
        )


def test_caller_transaction_is_not_committed_or_replaced():
    with Session() as session, session.begin():
        with pytest.raises(ProductionServiceError, match="CONTEXT_INVALID"):
            read_production_work_item(
                session, principal=None, account=None, work_item_id=1
            )
        assert session.in_transaction()


@pytest.fixture
def case(authority):
    owner, runtime, principal, credential = authority
    observation = item_fact()
    with Session(runtime) as session:
        publication.publish(
            session,
            principal,
            credential,
            publication.manifest(observation),
            "synthetic-" + uuid4().hex,
        )
    with owner.begin() as c:
        c.execute(
            text(
                'UPDATE iam_memberships SET permissions=\'["production:read","production:create","production:assign"]\' WHERE membership_id=:id'
            ),
            {"id": principal.membership_id},
        )
        item = c.execute(
            text("""SELECT i.order_item_id,i.version FROM marketplace_order_items i
            JOIN marketplace_orders o ON (o.organization_id,o.marketplace_account_id,o.order_id)=
            (i.organization_id,i.marketplace_account_id,i.order_id)
            WHERE o.organization_id=91001 AND o.marketplace_account_id=91101
            AND o.external_order_id=:external ORDER BY i.order_item_id LIMIT 1"""),
            {"external": observation.identity.external_order_id},
        ).one()
        skus = tuple(
            c.execute(
                text(
                    "INSERT INTO catalog_skus(organization_id,code) VALUES(91001,:code) RETURNING catalog_sku_id"
                ),
                {"code": "synthetic-production-" + uuid4().hex},
            ).scalar_one()
            for _ in range(2)
        )
    return owner, runtime, principal, item, skus


def create(case):
    _, runtime, principal, item, _ = case
    with Session(runtime) as session:
        return create_production_work_item(
            session,
            principal=principal,
            account=ACCOUNT,
            order_item_id=item.order_item_id,
            expected_source_item_version=item.version,
        )


def assign(case, command):
    with Session(case[1]) as session:
        return assign_production_work_item(
            session, principal=case[2], account=ACCOUNT, command=command
        )


def counts(owner, work):
    with owner.connect() as c:
        return tuple(
            c.execute(
                text(f"SELECT count(*) FROM {table} WHERE work_item_id=:work"),
                {"work": work},
            ).scalar_one()
            for table in (
                "production_work_items",
                "production_assignment_receipts",
                "production_assignment_history",
            )
        )


def test_creation_assignment_original_replay_and_exact_conflicts(case):
    first = create(case)
    assert not first.replayed
    assert create(case) == replace(first, replayed=True)
    with Session(case[1]) as session:
        value = read_production_work_item(
            session, principal=case[2], account=ACCOUNT, work_item_id=first.work_item_id
        )
        assert value.version == 1 and value.catalog_sku_id is None
        assert (
            value.planned_quantity == 0
            and value.remaining_quantity == value.required_quantity
        )
    command = AssignmentCommand(
        first.work_item_id, 1, "synthetic-key", case[4][0], "synthetic reason"
    )
    result = assign(case, command)
    assert result.result.version == 2 and not result.replayed
    second = assign(
        case,
        replace(
            command,
            expected_version=2,
            idempotency_key="synthetic-next",
            catalog_sku_id=case[4][1],
        ),
    )
    assert second.result.version == 3
    replay = assign(case, command)
    assert replay.result == result.result and replay.replayed
    assert counts(case[0], first.work_item_id) == (1, 2, 2)
    with pytest.raises(ProductionServiceError, match="IDEMPOTENCY_CONFLICT"):
        assign(case, replace(command, reason="changed synthetic reason"))
    with pytest.raises(ProductionServiceError, match="VERSION_CONFLICT"):
        assign(case, replace(command, idempotency_key="another synthetic key"))
    assert counts(case[0], first.work_item_id) == (1, 2, 2)


@pytest.mark.parametrize("change", ["permission", "logout", "account", "source"])
def test_replay_rechecks_live_authority_and_source(case, change):
    first = create(case)
    command = AssignmentCommand(
        first.work_item_id, 1, "synthetic-replay", case[4][0], "synthetic reason"
    )
    assign(case, command)
    owner, _, principal, item, _ = case
    with owner.begin() as c:
        if change == "permission":
            c.execute(
                text(
                    "UPDATE iam_memberships SET permissions='[\"production:read\"]' WHERE membership_id=:id"
                ),
                {"id": principal.membership_id},
            )
        elif change == "logout":
            c.execute(
                text(
                    "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
                ),
                {"id": principal.session_id},
            )
        elif change == "account":
            c.execute(
                text(
                    "UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91101"
                )
            )
        else:
            c.execute(
                text(
                    "UPDATE marketplace_order_items SET version=version+1 WHERE order_item_id=:id"
                ),
                {"id": item.order_item_id},
            )
    try:
        with pytest.raises(ProductionServiceError) as caught:
            assign(case, command)
        assert caught.value.code == (
            "PRODUCTION_SOURCE_CHANGED" if change == "source" else "PRODUCTION_DENIED"
        )
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert counts(owner, first.work_item_id) == (1, 1, 1)
    finally:
        if change == "account":
            with owner.begin() as c:
                c.execute(
                    text(
                        "UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101"
                    )
                )


def test_foreign_catalog_and_same_org_other_account_fail_closed(case):
    first = create(case)
    with case[0].begin() as c:
        sku = c.execute(
            text(
                "INSERT INTO catalog_skus(organization_id,code) VALUES(91002,:code) RETURNING catalog_sku_id"
            ),
            {"code": "synthetic-foreign-" + uuid4().hex},
        ).scalar_one()
    with pytest.raises(ProductionServiceError, match="CATALOG_DENIED"):
        assign(
            case,
            AssignmentCommand(
                first.work_item_id, 1, "foreign", sku, "synthetic reason"
            ),
        )
    with (
        Session(case[1]) as session,
        pytest.raises(ProductionServiceError, match="DENIED"),
    ):
        read_production_work_item(
            session,
            principal=case[2],
            account=replace(ACCOUNT, marketplace_account_id=91102),
            work_item_id=first.work_item_id,
        )
    assert counts(case[0], first.work_item_id) == (1, 0, 0)


def test_final_failure_rolls_back_receipt_transition_history_and_audit(case):
    first = create(case)
    command = AssignmentCommand(
        first.work_item_id,
        1,
        "synthetic-rollback",
        case[4][0],
        "synthetic private reason",
    )
    with Session(case[1]) as session:

        def revoke(session):
            session.execute(
                text(
                    "UPDATE iam_memberships SET is_active=false WHERE membership_id=:id"
                ),
                {"id": case[2].membership_id},
            )

        event.listen(session, "before_commit", revoke)
        with pytest.raises(ProductionServiceError, match="DENIED"):
            assign_production_work_item(
                session, principal=case[2], account=ACCOUNT, command=command
            )
    assert counts(case[0], first.work_item_id) == (1, 0, 0)
    with case[0].connect() as c:
        assert c.execute(
            text("SELECT is_active FROM iam_memberships WHERE membership_id=:id"),
            {"id": case[2].membership_id},
        ).scalar_one()
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM lk_audit_events WHERE object_type='production_work_item' AND object_id=:id AND action='production.manually_assigned'"
                ),
                {"id": str(first.work_item_id)},
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    "permissions,role",
    [
        ('["production:read"]', "custom"),
        ("[]", "admin"),
        ('["production:create"]', "custom"),
    ],
)
def test_creation_needs_explicit_create_and_read_not_profile(case, permissions, role):
    with case[0].begin() as c:
        c.execute(
            text(
                "UPDATE iam_memberships SET permissions=CAST(:permissions AS jsonb),role=:role WHERE membership_id=:id"
            ),
            {"permissions": permissions, "role": role, "id": case[2].membership_id},
        )
    with pytest.raises(ProductionServiceError, match="DENIED"):
        create(case)
    with case[0].connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM production_work_items WHERE order_item_id=:id"
                ),
                {"id": case[3].order_item_id},
            ).scalar_one()
            == 0
        )


def test_changed_source_before_creation_is_not_refreshed_or_copied(case):
    with case[0].begin() as c:
        c.execute(
            text(
                "UPDATE marketplace_order_items SET version=version+1 WHERE order_item_id=:id"
            ),
            {"id": case[3].order_item_id},
        )
    with pytest.raises(ProductionServiceError, match="SOURCE_CHANGED"):
        create(case)
    with case[0].connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM production_work_items WHERE order_item_id=:id"
                ),
                {"id": case[3].order_item_id},
            ).scalar_one()
            == 0
        )


def test_history_sql_failure_is_sanitized_and_rolls_back_all_witnesses(case):
    first = create(case)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO production_assignment_history"):
            raise OperationalError(
                "synthetic-private-SQL", {}, Exception("synthetic-private-diagnostic")
            )

    event.listen(case[1], "before_cursor_execute", fail)
    try:
        with pytest.raises(
            ProductionServiceError, match="STORAGE_UNAVAILABLE"
        ) as caught:
            assign(
                case,
                AssignmentCommand(
                    first.work_item_id,
                    1,
                    "synthetic-private-key",
                    case[4][0],
                    "synthetic-private-reason",
                ),
            )
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "synthetic-private" not in str(caught.value)
    finally:
        event.remove(case[1], "before_cursor_execute", fail)
    assert counts(case[0], first.work_item_id) == (1, 0, 0)


@pytest.mark.parametrize("mode", ["create", "same", "changed", "different"])
def test_two_physical_sessions_contend_without_duplicate_transitions(case, mode):
    work = None if mode == "create" else create(case).work_item_id
    pids = Queue()

    def participant(index):
        with Session(case[1]) as session:

            def identify(session, transaction, connection):
                pids.put(
                    connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                )

            event.listen(session, "after_begin", identify)
            try:
                if mode == "create":
                    return create_production_work_item(
                        session,
                        principal=case[2],
                        account=ACCOUNT,
                        order_item_id=case[3].order_item_id,
                        expected_source_item_version=case[3].version,
                    )
                command = AssignmentCommand(
                    work,
                    1,
                    "synthetic-race" + (str(index) if mode == "different" else ""),
                    case[4][0],
                    "synthetic reason" + (str(index) if mode == "changed" else ""),
                )
                return assign_production_work_item(
                    session, principal=case[2], account=ACCOUNT, command=command
                )
            except ProductionServiceError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        with case[0].begin() as holder:
            holder.execute(
                text(
                    "SELECT 1 FROM marketplace_accounts WHERE marketplace_account_id=91101 FOR UPDATE"
                )
            )
            futures = [pool.submit(participant, index) for index in range(2)]
            active = {pids.get(timeout=5), pids.get(timeout=5)}
            assert len(active) == 2
            deadline = monotonic() + 5
            while monotonic() < deadline:
                waiting = [
                    holder.execute(
                        text("SELECT cardinality(pg_blocking_pids(:pid))>0"),
                        {"pid": pid},
                    ).scalar_one()
                    for pid in active
                ]
                if all(waiting):
                    break
                sleep(0.01)
            assert all(waiting), (
                "Both physical callers must wait on retained authorization locks"
            )
        results = [future.result(timeout=10) for future in futures]
    successes = [value for value in results if not isinstance(value, str)]
    if mode in ("create", "same"):
        assert len(successes) == 2 and sorted(
            value.replayed for value in successes
        ) == [False, True]
        if mode == "create":
            assert successes[0].work_item_id == successes[1].work_item_id
            assert counts(case[0], successes[0].work_item_id) == (1, 0, 0)
        else:
            assert successes[0].result == successes[1].result
            assert counts(case[0], work) == (1, 1, 1)
    else:
        assert len(successes) == 1
        assert (
            "IDEMPOTENCY_CONFLICT" if mode == "changed" else "VERSION_CONFLICT"
        ) in results
        assert counts(case[0], work) == (1, 1, 1)
