"""Track physical Avito returns and idempotent inventory operations."""

from alembic import op
import sqlalchemy as sa

revision = "20260930_0088"
down_revision = "20260915_0087"
branch_labels = depends_on = None

COUNTERS = ("received_quantity", "inspected_quantity", "reserved_quantity", "sent_quantity", "written_off_quantity")


def upgrade() -> None:
    op.add_column("avito_return_items", sa.Column("line_index", sa.Integer(), nullable=True))
    for name in COUNTERS:
        op.add_column("avito_return_items", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "avito_return_inventory_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("lk_organizations.organization_id"), nullable=False),
        sa.Column("return_item_id", sa.Integer(), sa.ForeignKey("avito_return_items.return_item_id"), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("linked_order_id", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "operation_id", name="uq_avito_return_inventory_org_operation"),
    )
    op.create_index("ix_avito_return_inventory_events_organization_id", "avito_return_inventory_events", ["organization_id"])
    op.create_index("ix_avito_return_inventory_events_return_item_id", "avito_return_inventory_events", ["return_item_id"])


def downgrade() -> None:
    op.drop_index("ix_avito_return_inventory_events_return_item_id", table_name="avito_return_inventory_events")
    op.drop_index("ix_avito_return_inventory_events_organization_id", table_name="avito_return_inventory_events")
    op.drop_table("avito_return_inventory_events")
    for name in reversed(COUNTERS):
        op.drop_column("avito_return_items", name)
    op.drop_column("avito_return_items", "line_index")
