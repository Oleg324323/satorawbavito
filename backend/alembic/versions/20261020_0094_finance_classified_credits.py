"""Keep classified settlement advertising/compensation separate in gated v2.

Existing rows remain legacy formula versions and are not promoted by adding
zero defaults. Re-ingestion creates immutable v4 observations; no raw history
is rewritten. Apply only via the normal separately approved release process.
"""
from alembic import op
import sqlalchemy as sa

revision = "20261020_0094"
down_revision = "20261009_0093"
branch_labels = depends_on = None

TABLES = ("wb_finance_operations", "wb_finance_sync_run_sku_pnl_rollups",
          "wb_finance_sync_run_sku_daily_pnl_rollups")


def upgrade():
    for table in TABLES:
        for field in ("advertising_kopecks", "compensation_kopecks"):
            op.add_column(table, sa.Column(field, sa.BigInteger(), nullable=field == "advertising_kopecks",
                **({} if field == "advertising_kopecks" else {"server_default": "0"})))


def downgrade():
    # Like payable evidence, classified credits must not be silently erased.
    # Inspect all tenants under transactional DDL, including FORCE RLS owners.
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    evidence = " OR ".join(f"EXISTS (SELECT 1 FROM {table} WHERE advertising_kopecks IS NOT NULL OR compensation_kopecks <> 0 LIMIT 1)"
                           for table in TABLES)
    op.execute(sa.text(f"""DO $$ BEGIN IF {evidence} THEN
        RAISE EXCEPTION 'cannot downgrade while classified finance evidence exists';
        END IF; END $$"""))
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for table in reversed(TABLES):
        for field in ("compensation_kopecks", "advertising_kopecks"):
            op.drop_column(table, field)
