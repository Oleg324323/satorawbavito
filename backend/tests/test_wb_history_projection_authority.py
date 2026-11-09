"""Actual0085 authority/decision regressions, owned disposable PostgreSQL only."""
from sqlalchemy import text

from tests import test_wb_history_projection_codecs as codecs

cluster = codecs.cluster
pg_database = codecs.pg_database

DECISION_FIELDS = {
    "history_decision_version", "history_pre_order_version", "history_pre_sync_run_id",
    "history_pre_observation_id", "history_outcome",
}


def test_database_has_required_per_run_decision_proof(pg_database):
    owner, _ = pg_database
    with owner.connect() as connection:
        actual = set(connection.scalars(text("SELECT attname FROM pg_attribute "
            "WHERE attrelid='public.order_sync_memberships'::regclass AND attnum>0 AND NOT attisdropped")))
    assert DECISION_FIELDS <= actual, "DB-derived per-run decision/prestate fields are missing"
