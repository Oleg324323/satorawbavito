"""Empty0085 round trip only; no used-evidence deletion or runtime proof."""
from sqlalchemy import create_engine, text

from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster
DECISION_COLUMNS = {
    "history_decision_version": "smallint",
    "history_pre_order_version": "bigint",
    "history_pre_sync_run_id": "bigint",
    "history_pre_observation_id": "bigint",
    "history_outcome": "text",
}
LEGACY_FUNCTIONS = (
    "public.user_orders_request_bytes(public.user_orders_jobs)",
    "public.user_orders_row_guard()",
    "public.user_orders_transition_witness()",
)


def snapshot(engine, revision, *, extended):
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [revision]
        columns = dict(connection.execute(text(
            "SELECT attname,format_type(atttypid,atttypmod) FROM pg_attribute "
            "WHERE attrelid='public.order_sync_memberships'::regclass AND attnum>0 AND NOT attisdropped"
        )).all())
        assert {name: columns[name] for name in DECISION_COLUMNS if name in columns} == (DECISION_COLUMNS if extended else {})
        assert connection.scalar(text("SELECT to_regclass('public.user_orders_history_selection_pages') IS NOT NULL")) is extended
        definitions = tuple(connection.scalar(text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
                                              {"signature": signature}) for signature in LEGACY_FUNCTIONS)
        assert all(type(value) is str for value in definitions)
        assert all(("orders.wb-history.project.v1" in value) is extended for value in definitions)
        return definitions


def test_empty_0085_downgrade_restores_legacy_and_upgrade_reextends(cluster):
    # Existing allocator owns one fresh database and proves removal in finally.
    # migrate() uses isolated_environment, the owned DSN, a60s timeout and no shell.
    with candidate.disposable_database(cluster) as database:
        baseline = candidate.migrate(database.url, "upgrade", "20260910_0084")
        assert baseline.returncode == 0, baseline.stderr
        engine = create_engine(database.url, hide_parameters=True)
        try:
            legacy = snapshot(engine, "20260910_0084", extended=False)
            first_upgrade = candidate.migrate(database.url, "upgrade", "20260910_0085")
            assert first_upgrade.returncode == 0, first_upgrade.stderr
            extended = snapshot(engine, "20260910_0085", extended=True)
            downgraded = candidate.migrate(database.url, "downgrade", "20260910_0084")
            assert downgraded.returncode == 0, downgraded.stderr
            assert snapshot(engine, "20260910_0084", extended=False) == legacy
            second_upgrade = candidate.migrate(database.url, "upgrade", "20260910_0085")
            assert second_upgrade.returncode == 0, second_upgrade.stderr
            assert snapshot(engine, "20260910_0085", extended=True) == extended
        finally:
            engine.dispose()
