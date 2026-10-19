"""Durable local MOCK COD settlement and TTN warehouse receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0121_mock_cod_settlement'
down_revision = '0120_provider_sandbox'
branch_labels = None
depends_on = None


def reference(name, table, unique=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(table + '.id', ondelete='RESTRICT'), nullable=False, unique=unique)


def upgrade():
    op.add_column('order_shipments',sa.Column('return_evidence',sa.String(500),nullable=True))
    op.add_column('order_shipments',sa.Column('return_operator_id',sa.Integer(),nullable=True))
    op.create_foreign_key('fk_mock_return_operator','order_shipments','admin_users',['return_operator_id'],['id'],ondelete='RESTRICT')
    op.create_table('mock_cod_confirmations',
        sa.Column('id', sa.Integer(), primary_key=True), reference('order_id','orders', True),
        reference('shipment_id','order_shipments', True), reference('payment_id','order_payments', True),
        sa.Column('provider_event_id',sa.String(128),nullable=False,unique=True),
        sa.Column('tracking_number',sa.String(64),nullable=False),
        sa.Column('shipment_uuid',sa.String(36),nullable=False),
        sa.Column('amount_minor',sa.BigInteger(),nullable=False),
        sa.Column('original_channel',sa.String(32),nullable=False),
        sa.Column('original_simulation',sa.Boolean(),nullable=False), reference('operator_id','admin_users'),
        sa.Column('evidence',sa.String(500),nullable=False), sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("amount_minor > 0 AND provider_event_id LIKE 'MOCK-%'", name='mock_cod_scope'))
    op.create_table('mock_shipment_return_receipts',
        sa.Column('id',sa.Integer(),primary_key=True), reference('order_id','orders'), reference('shipment_id','order_shipments', True),
        sa.Column('tracking_number',sa.String(64),nullable=False), sa.Column('items',sa.JSON(),nullable=False),
        reference('operator_id','admin_users'), sa.Column('evidence',sa.String(500),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('uq_mock_return_original_shipment','order_shipments',['return_of_shipment_id'],unique=True,
        postgresql_where=sa.text('simulation = true AND return_of_shipment_id IS NOT NULL'))
    op.add_column('mock_return_receipts',sa.Column('shipment_return_receipt_id',sa.Integer(),nullable=True))
    op.create_foreign_key('fk_mock_fiscal_parcel_receipt','mock_return_receipts','mock_shipment_return_receipts',
        ['shipment_return_receipt_id'],['id'],ondelete='RESTRICT')
    for table in ('mock_cod_confirmations','mock_shipment_return_receipts'):
        op.execute(f'CREATE TRIGGER immutable_mock_carrier_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')
    op.execute("""CREATE FUNCTION validate_mock_cod_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF NOT EXISTS (SELECT 1 FROM orders o JOIN order_shipments s ON s.order_id=o.id
          JOIN order_payments p ON p.order_id=o.id
          WHERE o.id=NEW.order_id AND s.id=NEW.shipment_id AND p.id=NEW.payment_id
          AND o.channel IN ('shop','backoffice_pos') AND o.status::text != 'cancelled'
          AND o.channel=NEW.original_channel AND o.simulation=NEW.original_simulation
          AND o.total_amount*100=NEW.amount_minor AND s.simulation AND s.carrier='mock_nova_poshta'
          AND s.active AND s.status='delivered' AND s.return_of_shipment_id IS NULL AND s.handed_over_at IS NOT NULL
          AND s.tracking_number LIKE 'MOCK-%' AND s.tracking_number=NEW.tracking_number
          AND s.operation_uuid=NEW.shipment_uuid AND (s.packaging->>'cod_amount_minor')::bigint=NEW.amount_minor
          AND p.simulation AND p.method='cod' AND p.source='mock_cod_confirmation' AND p.amount_minor=NEW.amount_minor
          AND p.operator_id=NEW.operator_id AND p.evidence=NEW.evidence AND p.created_at=NEW.created_at
          AND EXISTS (SELECT 1 FROM order_handovers h WHERE h.order_id=o.id AND h.simulation))
      THEN RAISE EXCEPTION 'Mock COD requires matching delivered shipment and full payment'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER validate_mock_cod_fact BEFORE INSERT ON mock_cod_confirmations FOR EACH ROW EXECUTE FUNCTION validate_mock_cod_fact()')
    op.execute("""CREATE FUNCTION validate_mock_cod_payment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.method='cod' OR NEW.source='mock_cod_confirmation') AND NOT EXISTS
        (SELECT 1 FROM mock_cod_confirmations c WHERE c.payment_id=NEW.id AND c.order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Mock COD payment requires its durable shipment provenance'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE CONSTRAINT TRIGGER validate_mock_cod_payment AFTER INSERT ON order_payments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_mock_cod_payment()')
    op.execute("""CREATE FUNCTION validate_mock_parcel_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF NOT EXISTS (SELECT 1 FROM order_shipments s WHERE s.id=NEW.shipment_id AND s.order_id=NEW.order_id
          AND s.simulation AND s.carrier='mock_nova_poshta' AND s.status='returned' AND s.handed_over_at IS NOT NULL
          AND s.tracking_number LIKE 'MOCK-%' AND s.tracking_number=NEW.tracking_number)
        OR EXISTS (SELECT 1 FROM mock_shipment_return_receipts WHERE order_id=NEW.order_id)
        OR EXISTS (SELECT 1 FROM mock_return_receipts r JOIN mock_returns f ON f.id=r.return_id WHERE f.order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Mock physical receipt requires a returned parcel without prior receipt'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER validate_mock_parcel_receipt BEFORE INSERT ON mock_shipment_return_receipts FOR EACH ROW EXECUTE FUNCTION validate_mock_parcel_receipt()')
    op.execute("""CREATE FUNCTION protect_cod_order_origin() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.channel IS DISTINCT FROM OLD.channel OR NEW.simulation IS DISTINCT FROM OLD.simulation)
        AND EXISTS (SELECT 1 FROM mock_cod_confirmations WHERE order_id=NEW.id)
      THEN RAISE EXCEPTION 'Mock COD order origin is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_cod_order_origin BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION protect_cod_order_origin()')
    op.execute("""CREATE FUNCTION protect_mock_return_consent() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.return_of_shipment_id IS DISTINCT FROM OLD.return_of_shipment_id
        OR NEW.return_evidence IS DISTINCT FROM OLD.return_evidence OR NEW.return_operator_id IS DISTINCT FROM OLD.return_operator_id)
        AND (OLD.source='mock_return' OR NEW.source='mock_return')
      THEN RAISE EXCEPTION 'Mock return carrier consent is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_mock_return_consent BEFORE UPDATE ON order_shipments FOR EACH ROW EXECUTE FUNCTION protect_mock_return_consent()')


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM order_shipments WHERE simulation=true AND (source='mock_return' OR return_of_shipment_id IS NOT NULL))")).scalar():
        raise RuntimeError('Mock return carrier consent exists; downgrade refused')
    for table in ('mock_cod_confirmations','mock_shipment_return_receipts'):
        if op.get_bind().execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {table})')).scalar():
            raise RuntimeError('Mock carrier settlement history exists; downgrade refused')
    op.execute('DROP TRIGGER protect_cod_order_origin ON orders')
    op.execute('DROP FUNCTION protect_cod_order_origin()')
    op.execute('DROP TRIGGER protect_mock_return_consent ON order_shipments')
    op.execute('DROP FUNCTION protect_mock_return_consent()')
    op.execute('DROP TRIGGER validate_mock_cod_payment ON order_payments')
    op.execute('DROP FUNCTION validate_mock_cod_payment()')
    op.drop_constraint('fk_mock_fiscal_parcel_receipt','mock_return_receipts',type_='foreignkey')
    op.drop_column('mock_return_receipts','shipment_return_receipt_id')
    op.drop_index('uq_mock_return_original_shipment',table_name='order_shipments')
    op.drop_constraint('fk_mock_return_operator','order_shipments',type_='foreignkey')
    op.drop_column('order_shipments','return_operator_id')
    op.drop_column('order_shipments','return_evidence')
    op.drop_table('mock_shipment_return_receipts')
    op.drop_table('mock_cod_confirmations')
    op.execute('DROP FUNCTION validate_mock_parcel_receipt()')
    op.execute('DROP FUNCTION validate_mock_cod_fact()')
