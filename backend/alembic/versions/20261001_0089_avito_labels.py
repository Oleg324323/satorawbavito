"""Tenant-scoped immutable Avito transport label originals."""
from alembic import op
import sqlalchemy as sa

revision = "20261001_0089"
down_revision = "20260930_0088"
branch_labels = depends_on = None


def upgrade():
    op.create_table("avito_label_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("pdf", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "account_id", "sha256", name="uq_avito_label_document_hash"))
    op.create_table("avito_transport_labels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("avito_label_documents.id"), nullable=False),
        sa.Column("order_number", sa.String(128), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("barcode", sa.String(256), nullable=False),
        sa.Column("barcode_type", sa.String(32), nullable=False),
        sa.Column("barcode_png", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("document_id", "page", "order_number", "barcode", name="uq_avito_label_binding"))
    for table in ("avito_label_documents", "avito_transport_labels"):
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer) WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer)")
    op.create_index("ix_avito_transport_labels_order_number", "avito_transport_labels", ["order_number"])


def downgrade():
    op.drop_table("avito_transport_labels")
    op.drop_table("avito_label_documents")
