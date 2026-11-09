"""Saved-view filtering/discovery on the owned synthetic PostgreSQL fixture."""

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.orders.bindings import bound_high_water_mark
from app.orders.snapshot_repository import OrdersSnapshotRepository
from tests.test_orders_http import client_for
from tests.test_orders_read_service import ACCOUNTS
from tests.test_orders_read_service import prepared as _prepared_fixture
from tests.test_orders_read_service import read_db as _read_db_fixture
from tests.test_orders_schema_candidate import cluster, scope  # noqa: F401
from tests.test_orders_snapshot_repository import coverage, stored_rows

prepared = _prepared_fixture
read_db = _read_db_fixture


def test_saved_snapshot_filters_before_limit_and_cursor_query_binding(prepared):
    _, runtime, principal, _ = prepared
    with Session(runtime) as session, session.begin():
        scope(session)
        groups = [stored_rows(session) for _ in range(3)]
        groups.sort(key=lambda rows: rows[0].observation.identity.external_order_id)
        selected = groups[-1][0].observation.identity.external_order_id
        all_rows = tuple(row for group in groups for row in group)
        snapshot = OrdersSnapshotRepository(session, 91001).freeze(
            all_rows,
            coverage(),
            bound_high_water_mark("c" * 64, 91001, ACCOUNTS),
            "c" * 64,
            parent_versions={group[0].observation.identity: 2 for group in groups},
        )
    statements = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(runtime, "before_cursor_execute", trace)
    try:
        with client_for(runtime, principal) as client:
            meta = client.get(
                "/api/v2/orders/snapshots/latest", params={"account_id": 91101}
            )
            assert meta.status_code == 200, meta.text
            assert meta.json()["selection_kind"] == "saved_snapshot"
            assert meta.json()["snapshot_id"] == str(snapshot)
            assert meta.json()["query_checksum"] == "c" * 64
            assert meta.json()["row_count"] == "6"
            params = {
                "account_id": 91101,
                "snapshot_id": snapshot,
                "query_checksum": "c" * 64,
                "external_order_id": selected,
                "marketplace": "avito",
                "limit": 1,
            }
            first = client.get("/api/v2/orders", params=params)
            assert first.status_code == 200, first.text
            first = first.json()
            assert len(first["rows"]) == 1 and first["next_cursor"]
            assert (
                first["rows"][0]["observation"]["identity"]["external_order_id"]
                == selected
            )
            params.pop("snapshot_id")
            params["cursor"] = first["next_cursor"]
            second = client.get("/api/v2/orders", params=params)
            assert second.status_code == 200, second.text
            assert (
                len(second.json()["rows"]) == 1 and second.json()["next_cursor"] is None
            )
            assert (
                first["rows"][0]["item_identity"]
                != second.json()["rows"][0]["item_identity"]
            )
            params["marketplace"] = "wb"
            assert client.get("/api/v2/orders", params=params).status_code == 400
            params.pop("cursor")
            params["snapshot_id"] = snapshot
            empty = client.get("/api/v2/orders", params=params)
            assert empty.status_code == 200 and empty.json()["rows"] == []
    finally:
        event.remove(runtime, "before_cursor_execute", trace)
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )
    matching = [sql for sql in statements if "filter_external_order_id" in sql]
    assert matching and all(
        sql.index("filter_external_order_id") < sql.index("LIMIT") for sql in matching
    )


def test_metadata_rechecks_live_permission_and_rejects_rebound_snapshot(prepared):
    owner, runtime, principal, _ = prepared
    with owner.begin() as c:
        c.execute(
            text(
                "UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91101"
            )
        )
    try:
        with client_for(runtime, principal) as client:
            response = client.get(
                "/api/v2/orders/snapshots/latest", params={"account_id": 91101}
            )
            assert response.status_code == 409
    finally:
        with owner.begin() as c:
            c.execute(
                text(
                    "UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101"
                )
            )
    with owner.begin() as c:
        c.execute(
            text("UPDATE iam_memberships SET permissions='[]' WHERE membership_id=:id"),
            {"id": principal.membership_id},
        )
    with client_for(runtime, principal) as client:
        response = client.get(
            "/api/v2/orders/snapshots/latest", params={"account_id": 91101}
        )
        assert response.status_code == 403
