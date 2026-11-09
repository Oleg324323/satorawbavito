"""Preserve WB payable evidence without rewriting historical observations."""

import sqlalchemy as sa

from alembic import op

revision = "20260911_0086"
down_revision = "20260910_0085"
branch_labels = depends_on = None

TABLES = (
    "wb_finance_operations",
    "wb_finance_sync_run_sku_pnl_rollups",
    "wb_finance_sync_run_sku_daily_pnl_rollups",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("payable_kopecks", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    # Match the loyalty migration: check every tenant under transactional DDL.
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    evidence = " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} WHERE payable_kopecks IS NOT NULL LIMIT 1)"
        for table in TABLES
    )
    op.execute(sa.text(f"""
        DO $$ BEGIN
            IF {evidence} THEN
                RAISE EXCEPTION 'cannot downgrade while canonical payable evidence exists';
            END IF;
        END $$
    """))
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for table in reversed(TABLES):
        op.drop_column(table, "payable_kopecks")
