"""Record dated tax confirmation separately from legacy expense assumptions."""

import sqlalchemy as sa

from alembic import op

revision = "20260915_0087"
down_revision = "20260911_0086"
branch_labels = depends_on = None

TABLE = "organization_economics_versions"
CONSTRAINT = "ck_organization_economics_tax_evidence"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("tax_value_state", sa.String(16), nullable=True))
    op.add_column(TABLE, sa.Column("tax_evidence_status", sa.String(32), nullable=True))
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "(tax_value_state IS NULL AND tax_evidence_status IS NULL) OR "
        "(tax_value_state IS NOT NULL AND tax_evidence_status IS NOT NULL "
        "AND tax_value_state = 'configured' AND tax_evidence_status = 'dated' "
        "AND tax_basis_points IS NOT NULL)",
    )


def downgrade() -> None:
    # Transactional DDL: check every tenant before removing immutable evidence.
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(sa.text(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE} WHERE tax_value_state IS NOT NULL LIMIT 1) THEN
                RAISE EXCEPTION 'cannot downgrade while confirmed tax evidence exists';
            END IF;
        END $$
    """))
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.drop_column(TABLE, "tax_evidence_status")
    op.drop_column(TABLE, "tax_value_state")
