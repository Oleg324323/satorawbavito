"""Genuine history → owner-frozen view → final API read, not publisher-role proof."""

import json

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.orders.cursor import OrdersCursorCodec
from app.orders.history_publication import persist_wb_history_chunk
from app.orders.read_assembly import freeze_orders_view
from app.orders.read_service import discover_saved_orders_snapshot, read_orders_page
from app.orders.router import discover_orders_bindings
from app.platform.integrations.publication_guard import PublicationGuardError
from tests import test_wb_history_projection_decisions as history

cluster = history.cluster
pg_database = history.pg_database
pg_store = history.pg_store
data = history.data
live = history.live
prepared = history.prepared


def test_genuine_history_saved_view_is_readable_by_final_api_without_snapshot_writer_rights(
    pg_database, prepared
):
    owner, api = pg_database
    d, service, claim = prepared
    progress = service.publish_chunk(claim=claim, participant=persist_wb_history_chunk)
    assert progress.publication.state == "partial"
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE iam_memberships SET scope_mode='selected',allowed_account_ids=CAST(:ids AS json) WHERE membership_id=:id"
            ),
            {"ids": json.dumps([d.org]), "id": d.org},
        )
    # Explicit owner-only snapshot preparation; retain the original real actor,
    # principal/account binding and production freeze guard. No synthetic rows.
    with Session(owner) as session:
        principal, accounts = discover_orders_bindings(session, d.actor, (d.org,))
        _, foreign = discover_orders_bindings(session, d.actor, (d.org + 100000,))
        snapshot = freeze_orders_view(
            session,
            principal=principal,
            accounts=accounts,
            coverage_run_ids=(progress.publication.run_id,),
            query_checksum="a" * 64,
        )
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE iam_memberships SET permissions='[\"cabinet:read\"]'::json WHERE membership_id=:id"
            ),
            {"id": d.org},
        )
    codec = OrdersCursorCodec(b"synthetic-history-saved-read-key-32-bytes")
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    with api.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == api._history_api_role
    event.listen(api, "before_cursor_execute", capture)
    try:
        with Session(api) as session:
            discovered = discover_saved_orders_snapshot(
                session, principal=principal, accounts=accounts
            )
            assert (
                discovered.snapshot_id == str(snapshot) and discovered.row_count == "1"
            )
            page = read_orders_page(
                session,
                principal=principal,
                accounts=accounts,
                snapshot_id=snapshot,
                query_checksum=discovered.query_checksum,
                codec=codec,
                limit=1,
            )
            assert len(page.rows) == 1 and page.next_cursor is None
            row = page.rows[0]
            assert row.observation.identity.organization_id == d.org
            assert row.observation.identity.marketplace_account_id == d.org
            assert row.observation.source_kind == "wb-statistics-supplier-orders"
            assert "source_readiness_unproven" in row.readiness_blockers
            assert not session.in_transaction()
        with Session(api) as session, pytest.raises(PublicationGuardError):
            discover_saved_orders_snapshot(
                session, principal=principal, accounts=foreign
            )
    finally:
        event.remove(api, "before_cursor_execute", capture)
    # Saved reads do not reconstruct privileged history evidence or mutate views.
    assert not any(
        sql.lstrip().startswith(("insert", "update", "delete", "merge", "truncate"))
        for sql in statements
    )
    assert not any(
        name in sql
        for sql in statements
        for name in ("wb_live_history_pages", "user_orders_job_audit")
    )
    for table in ("order_read_snapshots", "order_read_snapshot_rows"):
        with owner.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT has_table_privilege(:role,:table,'INSERT,UPDATE,DELETE')"
                    ),
                    {"role": api._history_api_role, "table": table},
                )
                is False
            )
        with pytest.raises(DBAPIError) as denied, api.begin() as connection:
            connection.execute(text(f"DELETE FROM {table} WHERE false"))
        assert denied.value.orig.sqlstate == "42501"
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"
            ),
            {"id": principal.session_id},
        )
    with Session(api) as session, pytest.raises(PublicationGuardError):
        discover_saved_orders_snapshot(session, principal=principal, accounts=accounts)
    with Session(api) as session, pytest.raises(PublicationGuardError):
        read_orders_page(
            session,
            principal=principal,
            accounts=accounts,
            snapshot_id=snapshot,
            query_checksum="a" * 64,
            codec=codec,
            limit=1,
        )
