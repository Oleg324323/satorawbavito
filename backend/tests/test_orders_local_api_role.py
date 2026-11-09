"""Saved Orders view using final API grants, not ingestion/full-queue proof."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.orders.cursor import OrdersCursorCodec
from app.orders.read_service import discover_saved_orders_snapshot, read_orders_page
from app.platform.integrations.publication_guard import PublicationGuardError
from tests import test_notification_preferences_postgres as preferences
from tests import test_orders_read_service as reads

cluster = preferences.cluster
database = preferences.database
prepared = reads.prepared


@pytest.fixture
def read_db(database):
    # Preserve final API grants. The existing fixture prepares its immutable
    # synthetic snapshot using only the owner, not an artificially widened API.
    with database.owner.begin() as connection:
        connection.execute(text("INSERT INTO lk_organizations(organization_id,slug,name) "
                                "VALUES(91001,'orders-api-one','Synthetic')"))
        connection.execute(text("INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,"
            "marketplace,external_account_id,status) VALUES"
            "(91101,91001,'avito','synthetic-a','connected'),"
            "(91102,91001,'avito','synthetic-b','connected')"))
    return database.owner, database.owner


def test_final_api_role_discovers_reads_pages_and_denies_snapshot_writes(database, prepared):
    owner, _, principal, snapshot = prepared
    codec = OrdersCursorCodec(b"synthetic-local-read-key-at-least-32")
    with database.runtime.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == database.roles[0]
    with Session(database.runtime) as session:
        discovered = discover_saved_orders_snapshot(session, principal=principal, accounts=reads.ACCOUNTS)
        assert discovered.snapshot_id == str(snapshot) and discovered.row_count == "2"
        first = read_orders_page(session, principal=principal, accounts=reads.ACCOUNTS,
            snapshot_id=snapshot, query_checksum=discovered.query_checksum, codec=codec, limit=1)
        second = read_orders_page(session, principal=principal, accounts=reads.ACCOUNTS,
            cursor=first.next_cursor, query_checksum=discovered.query_checksum, codec=codec, limit=1)
        assert len(first.rows) == len(second.rows) == 1
        assert second.next_cursor is None and first.rows[0].item_identity != second.rows[0].item_identity
        assert not session.in_transaction()
    for table in ("order_read_snapshots", "order_read_snapshot_rows"):
        with owner.connect() as connection:
            assert connection.scalar(text("SELECT has_table_privilege(:role,:table,'SELECT')"),
                                     {"role": database.roles[0], "table": table}) is True
            assert connection.scalar(text("SELECT has_table_privilege(:role,:table,'INSERT,UPDATE,DELETE')"),
                                     {"role": database.roles[0], "table": table}) is False
        with pytest.raises(DBAPIError) as denied, database.runtime.begin() as connection:
            connection.execute(text(f"DELETE FROM {table} WHERE false"))
        assert denied.value.orig.sqlstate == "42501"
    with owner.begin() as connection:
        connection.execute(text("UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"),
                           {"id": principal.session_id})
    with Session(database.runtime) as session, pytest.raises(PublicationGuardError):
        discover_saved_orders_snapshot(session, principal=principal, accounts=reads.ACCOUNTS)
