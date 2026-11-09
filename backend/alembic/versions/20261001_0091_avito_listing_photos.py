"""Persistent per-listing photos collected once by the authorized extension."""
from alembic import op
import sqlalchemy as sa

revision = '20261001_0091'
down_revision = '20261001_0090'
branch_labels = depends_on = None


def upgrade():
    op.create_table('avito_listing_photos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('account_id', sa.String(128), nullable=False),
        sa.Column('item_id', sa.String(128), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('thumbnail', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('organization_id', 'account_id', 'item_id', name='uq_avito_listing_photo'))
    op.create_index('ix_avito_listing_photos_organization_id', 'avito_listing_photos', ['organization_id'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE avito_listing_photos ENABLE ROW LEVEL SECURITY')
        op.execute('ALTER TABLE avito_listing_photos FORCE ROW LEVEL SECURITY')
        op.execute("CREATE POLICY tenant_isolation ON avito_listing_photos USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer) WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer)")


def downgrade():
    op.drop_table('avito_listing_photos')
