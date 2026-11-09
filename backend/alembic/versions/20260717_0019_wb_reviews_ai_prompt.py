"""Ensure the WB reviews prompt exists on historical schema variants.

Revision 0017 already owns this column in the canonical migration chain.
Retain the additive repair for historical schemas that lack it at 0018.
Downgrade must preserve the column (and operator data) expected at 0018.
"""

from __future__ import annotations

from alembic import op


revision = "20260717_0019"
down_revision = "20260717_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Database-side guard also supports Alembic's offline SQL generation.
    op.execute("""
        ALTER TABLE rv_review_sync_settings
        ADD COLUMN IF NOT EXISTS ai_prompt TEXT NOT NULL DEFAULT ''
    """)
    op.execute("""
        DO $migration$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'rv_review_sync_settings'
                  AND column_name = 'ai_prompt'
                  AND data_type = 'text'
                  AND is_nullable = 'NO'
                  AND column_default = $default$''::text$default$
            ) THEN
                RAISE EXCEPTION '0019: incompatible rv_review_sync_settings.ai_prompt schema';
            END IF;
        END
        $migration$
    """)


def downgrade() -> None:
    # Only downgrade of the owning revision (0017) may remove this column.
    pass
