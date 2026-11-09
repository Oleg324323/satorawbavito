"""Encrypted organization OpenAI configuration, no imported local data."""
from alembic import op
import sqlalchemy as sa

revision = "20261008_0092"
down_revision = "20261001_0091"
branch_labels = depends_on = None


def upgrade():
    op.create_table("organization_openai_keys",
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("lk_organizations.organization_id"), primary_key=True),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE organization_openai_keys ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE organization_openai_keys FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_isolation ON organization_openai_keys USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer) WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer)")


def downgrade():
    op.drop_table("organization_openai_keys")
