"""Additive isolated mock carrier shipments, journal and guest tracking capability."""
from alembic import op
import sqlalchemy as sa

revision = '0113_mock_np_shipments'
down_revision = '0112_local_pos_sales'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('admin_users', sa.Column('can_manage_shipments', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('orders', sa.Column('guest_tracking_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('guest_tracking_revoked_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('order_shipments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('simulation', sa.Boolean(), nullable=False),
        sa.Column('carrier', sa.String(32), nullable=False),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('operation_uuid', sa.String(36), nullable=False, unique=True),
        sa.Column('tracking_number', sa.String(64), nullable=True, unique=True),
        sa.Column('provider_reference', sa.String(64), nullable=False, unique=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('raw_status', sa.String(64), nullable=False),
        sa.Column('progress', sa.Integer(), nullable=False),
        sa.Column('address_snapshot', sa.JSON(), nullable=False),
        sa.Column('packaging', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('handed_over_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('uq_order_active_shipment', 'order_shipments', ['order_id'], unique=True, postgresql_where=sa.text('active = true'))
    op.create_table('shipment_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('shipment_id', sa.Integer(), sa.ForeignKey('order_shipments.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('operation', sa.String(32), nullable=False),
        sa.Column('operation_uuid', sa.String(36), unique=True, nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('raw_status', sa.String(64), nullable=False),
        sa.Column('normalized_status', sa.String(32), nullable=False),
        sa.Column('outcome', sa.String(32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('mock_carrier_records',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('tracking_number', sa.String(64), unique=True, nullable=False),
        sa.Column('address_snapshot', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.execute("CREATE FUNCTION protect_shipment_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.address_snapshot::jsonb IS DISTINCT FROM OLD.address_snapshot::jsonb OR NEW.packaging::jsonb IS DISTINCT FROM OLD.packaging::jsonb OR NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.operation_uuid IS DISTINCT FROM OLD.operation_uuid OR NEW.provider_reference IS DISTINCT FROM OLD.provider_reference OR NEW.simulation IS DISTINCT FROM OLD.simulation THEN RAISE EXCEPTION 'Shipment snapshot is immutable'; END IF; RETURN NEW; END $$")
    op.execute('CREATE TRIGGER immutable_shipment_snapshot BEFORE UPDATE ON order_shipments FOR EACH ROW EXECUTE FUNCTION protect_shipment_snapshot()')
    op.execute('CREATE TRIGGER immutable_shipment_event BEFORE UPDATE OR DELETE ON shipment_events FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')
    op.execute('CREATE TRIGGER immutable_mock_carrier_record BEFORE UPDATE OR DELETE ON mock_carrier_records FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')


def downgrade():
    # Disposable test data only; production must retain audit history.
    op.drop_table('mock_carrier_records')
    op.drop_table('shipment_events')
    op.drop_table('order_shipments')
    op.execute('DROP FUNCTION protect_shipment_snapshot()')
    op.drop_column('orders', 'guest_tracking_revoked_at')
    op.drop_column('orders', 'guest_tracking_expires_at')
    op.drop_column('admin_users', 'can_manage_shipments')
