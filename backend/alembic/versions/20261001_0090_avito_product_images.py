"""Persistent tenant-scoped photos for offline Avito XLSX exports."""
from alembic import op
import sqlalchemy as sa

revision = '20261001_0090'
down_revision = '20261001_0089'
branch_labels = depends_on = None


def upgrade():
    op.create_table('avito_product_images',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('url_hash', sa.String(64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('extension', sa.String(8), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('organization_id', 'url_hash', name='uq_avito_product_image_url'))
    op.create_index('ix_avito_product_images_organization_id', 'avito_product_images', ['organization_id'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE avito_product_images ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE avito_product_images FORCE ROW LEVEL SECURITY')
        op.execute("CREATE POLICY tenant_isolation ON avito_product_images USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer) WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer)")


def downgrade():
    op.drop_table('avito_product_images')
