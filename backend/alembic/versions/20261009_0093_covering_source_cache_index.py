"""Index authoritative payload periods without scanning saved report bodies."""
from alembic import op

revision = "20261009_0093"
down_revision = "20261008_0092"
branch_labels = depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("""CREATE INDEX CONCURRENTLY ix_wb_source_cache_covering_period
            ON wb_repricer_source_cache
                (organization_id, (payload->>'dateFrom'), (payload->>'dateTo'), fetched_at DESC)
            WHERE (source_key LIKE 'finance_%' OR source_key LIKE 'baskets_%'
                OR source_key LIKE 'period_stats_%' OR source_key LIKE 'ads_%')
              AND json_typeof(payload) = 'object'
              AND json_typeof(payload->'dailyAggregates') = 'object'
              AND (payload->'dailyAggregates')::jsonb <> '{}'::jsonb""")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY ix_wb_source_cache_covering_period")
