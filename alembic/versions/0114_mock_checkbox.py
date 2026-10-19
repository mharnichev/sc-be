"""Add isolated mock fiscal approval, durable outbox, inbox and immutable journal."""
from alembic import op
import sqlalchemy as sa

revision = '0114_mock_checkbox'
down_revision = '0113_mock_np_shipments'
branch_labels = None
depends_on = None


def ref(name, table, *, nullable=False, unique=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(table + '.id', ondelete='RESTRICT'), nullable=nullable, unique=unique)


def register_ref():
    return sa.Column('cash_register_id', sa.String(32), sa.ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'), nullable=False)


def upgrade():
    for name in ('can_fiscalize_sales', 'can_manage_fiscal_settings'):
        op.add_column('admin_users', sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('mock_cash_registers',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('credential_kind', sa.String(16), nullable=False),
        sa.Column('shift_status', sa.String(16), nullable=False),
        sa.Column('shift_revision', sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 'MOCK-KASA-1' AND credential_kind = 'mock'", name='mock_register_only'),
        sa.CheckConstraint("shift_status IN ('open', 'closed') AND shift_revision >= 0", name='mock_shift_state'))
    op.create_table('fiscal_previews', sa.Column('id', sa.Integer(), primary_key=True),
        ref('order_id', 'orders'), ref('actor_id', 'admin_users'),
        sa.Column('revision', sa.Integer(), nullable=False), register_ref(),
        sa.Column('shift_revision', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False), sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('fiscal_documents', sa.Column('id', sa.Integer(), primary_key=True),
        ref('order_id', 'orders'), ref('payment_id', 'order_payments'),
        ref('preview_id', 'fiscal_previews', unique=True), ref('actor_id', 'admin_users'),
        sa.Column('provider_uuid', sa.String(36), nullable=False, unique=True), register_ref(),
        sa.Column('shift_revision', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False), sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('simulation', sa.Boolean(), nullable=False), sa.Column('mock_outcome', sa.String(32), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivery_status', sa.String(32), nullable=False),
        sa.Column('delivery_attempts', sa.Integer(), nullable=False), sa.Column('delivery_error', sa.String(64), nullable=True),
        sa.CheckConstraint('simulation = true', name='mock_fiscal_only'))
    for field in ('order', 'payment'):
        op.create_index('uq_fiscal_active_' + field, 'fiscal_documents', [field + '_id'], unique=True,
            postgresql_where=sa.text("status != 'cancelled'"))
    op.create_table('fiscal_jobs', sa.Column('id', sa.Integer(), primary_key=True),
        ref('document_id', 'fiscal_documents', unique=True), sa.Column('status', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('fence', sa.Integer(), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('mock_receipts', sa.Column('provider_uuid', sa.String(36), primary_key=True),
        ref('document_id', 'fiscal_documents', unique=True), sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('fiscal_events', sa.Column('id', sa.Integer(), primary_key=True),
        ref('document_id', 'fiscal_documents', nullable=True), ref('actor_id', 'admin_users'),
        sa.Column('operation', sa.String(32), nullable=False), sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('fiscal_inbox', sa.Column('event_id', sa.String(36), primary_key=True),
        sa.Column('provider_uuid', sa.String(36), nullable=False), sa.Column('status', sa.String(16), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False))
    for table in ('fiscal_previews', 'mock_receipts', 'fiscal_events', 'fiscal_inbox'):
        op.execute(f'CREATE TRIGGER immutable_fiscal_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')
    op.execute("""CREATE FUNCTION protect_fiscal_approval() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Fiscal approval is immutable'; END IF;
      IF NEW.snapshot::jsonb IS DISTINCT FROM OLD.snapshot::jsonb
        OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
        OR NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.payment_id IS DISTINCT FROM OLD.payment_id
        OR NEW.preview_id IS DISTINCT FROM OLD.preview_id OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
        OR NEW.provider_uuid IS DISTINCT FROM OLD.provider_uuid OR NEW.simulation IS DISTINCT FROM OLD.simulation
        OR NEW.cash_register_id IS DISTINCT FROM OLD.cash_register_id OR NEW.shift_revision IS DISTINCT FROM OLD.shift_revision
        OR NEW.approved_at IS DISTINCT FROM OLD.approved_at OR NEW.mock_outcome IS DISTINCT FROM OLD.mock_outcome
        OR (OLD.dispatched_at IS NOT NULL AND NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at)
        OR (OLD.status IN ('issued', 'cancelled') AND NEW.status IS DISTINCT FROM OLD.status)
        OR (OLD.status = 'error' AND NEW.status IS DISTINCT FROM OLD.status
            AND NOT (NEW.status = 'cancelled' AND OLD.dispatched_at IS NULL))
      THEN RAISE EXCEPTION 'Fiscal approval is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER immutable_fiscal_approval BEFORE UPDATE OR DELETE ON fiscal_documents FOR EACH ROW EXECUTE FUNCTION protect_fiscal_approval()')


def downgrade():
    for table in ('fiscal_inbox', 'fiscal_events', 'mock_receipts', 'fiscal_jobs', 'fiscal_documents', 'fiscal_previews', 'mock_cash_registers'):
        op.drop_table(table)
    op.execute('DROP FUNCTION protect_fiscal_approval()')
    for name in ('can_manage_fiscal_settings', 'can_fiscalize_sales'):
        op.drop_column('admin_users', name)
