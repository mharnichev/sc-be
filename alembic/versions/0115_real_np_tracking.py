"""Real read-only NP tracking; retain all existing status/payment facts."""
from alembic import op
import sqlalchemy as sa

revision = '0115_real_np_tracking'
down_revision = '0114_mock_checkbox'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('provider_event_at', 'last_attempt_at', 'next_poll_at'):
        op.add_column('order_shipments', sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column('order_shipments', sa.Column('tracking_status', sa.String(32), nullable=True))
    op.add_column('order_shipments', sa.Column('last_attempt_status', sa.String(32), nullable=False, server_default='never'))
    op.add_column('order_shipments', sa.Column('tracking_failures', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('order_shipments', sa.Column('return_of_shipment_id', sa.Integer(), sa.ForeignKey('order_shipments.id', ondelete='RESTRICT'), nullable=True))
    op.drop_index('uq_order_active_shipment', table_name='order_shipments')
    op.create_index('uq_order_active_shipment', 'order_shipments', ['order_id'], unique=True,
                    postgresql_where=sa.text('active = true AND simulation = true'))
    op.create_index('ix_real_tracking_due', 'order_shipments', ['next_poll_at', 'id'],
                    postgresql_where=sa.text("active = true AND simulation = false AND carrier = 'nova_poshta'"))
    op.create_table('shipment_tracking_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('shipment_id', sa.Integer(), sa.ForeignKey('order_shipments.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('event_key', sa.String(64), nullable=False, unique=True),
        sa.Column('raw_status', sa.String(64), nullable=False),
        sa.Column('normalized_status', sa.String(32), nullable=False),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_shipment_tracking_events_shipment_id', 'shipment_tracking_events', ['shipment_id'])
    op.create_table('shipment_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('shipment_id', sa.Integer(), sa.ForeignKey('order_shipments.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('order_item_id', sa.Integer(), sa.ForeignKey('order_items.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.UniqueConstraint('shipment_id', 'order_item_id'),
        sa.CheckConstraint('quantity > 0', name='positive_quantity'))
    op.create_index('ix_shipment_items_shipment_id', 'shipment_items', ['shipment_id'])
    op.create_table('shipment_tracking_poll_state', sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('next_request_at', sa.DateTime(timezone=True), nullable=True))
    op.execute('INSERT INTO shipment_tracking_poll_state (id) VALUES (1)')
    for table in ('shipment_tracking_events', 'shipment_items'):
        op.execute(f'CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')

    # Import only unambiguous saved NP numbers. No carrier call or order-state inference.
    op.execute("""
        INSERT INTO order_shipments
          (order_id, simulation, carrier, source, active, operation_uuid, tracking_number,
           provider_reference, status, raw_status, progress, address_snapshot, packaging,
           created_at, last_attempt_status, tracking_failures)
        SELECT o.id, false, 'nova_poshta', 'legacy_import', true,
               md5('np-legacy:' || o.id::text), o.tracking_number, o.tracking_number,
               'requires_review', '', 0, '{}'::json, '{}'::json, now(), 'never', 0
        FROM orders o
        WHERE o.tracking_number ~ '^[0-9]{14}$'
          AND o.shipping_company IN ('nova_poshta', 'novaPoshta', 'novaPost')
          AND NOT o.simulation
          AND NOT EXISTS (SELECT 1 FROM order_shipments s WHERE s.order_id = o.id
                           OR s.tracking_number = o.tracking_number OR s.provider_reference = o.tracking_number)
          AND (SELECT count(*) FROM orders duplicate WHERE duplicate.tracking_number = o.tracking_number) = 1
    """)


def downgrade():
    raise RuntimeError('Tracking history must be retained; restore a backup in a disposable environment instead')
