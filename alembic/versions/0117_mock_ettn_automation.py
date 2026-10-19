"""Local ETTN simulation, consent, durable commands and exclusive ownership."""
from alembic import op
import sqlalchemy as sa

revision = '0117_mock_ettn_automation'
down_revision = '0116_mock_returns'
branch_labels = None
depends_on = None


def ref(name, table, *, nullable=False, unique=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(table + '.id', ondelete='RESTRICT'), nullable=nullable, unique=unique)


def register():
    return sa.Column('cash_register_id', sa.String(32), sa.ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'), nullable=False)


def ident():
    return sa.Column('id', sa.Integer(), primary_key=True)


def state():
    return [sa.Column('snapshot', sa.JSON(), nullable=False), sa.Column('snapshot_hash', sa.String(64), nullable=False)]


def upgrade():
    op.create_table('mock_automation_previews', ident(), ref('order_id', 'orders'), ref('actor_id', 'admin_users'),
        sa.Column('revision', sa.Integer(), nullable=False), register(), sa.Column('shift_revision', sa.Integer(), nullable=False),
        *state(), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('mock_fiscal_automations', ident(), ref('order_id', 'orders'), ref('payment_id', 'order_payments'),
        ref('shipment_id', 'order_shipments'), ref('preview_id', 'mock_automation_previews', unique=True), ref('actor_id', 'admin_users'),
        sa.Column('provider_uuid', sa.String(36), nullable=False, unique=True), sa.Column('external_id', sa.String(64), nullable=True, unique=True),
        register(), sa.Column('shift_revision', sa.Integer(), nullable=False), *state(),
        sa.Column('status', sa.String(32), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('simulation', sa.Boolean(), nullable=False), sa.Column('mock_outcome', sa.String(32), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancellation_confirmed_at', sa.DateTime(timezone=True), nullable=True), sa.Column('result', sa.JSON(), nullable=True),
        sa.CheckConstraint("simulation = true AND cash_register_id = 'MOCK-KASA-1'", name='mock_automation_only'),
        sa.CheckConstraint("status IN ('queued','unknown','armed','cancel_pending','cancel_unknown','cancelled','issued','error')", name='mock_automation_status'),
        sa.CheckConstraint("status != 'cancelled' OR cancellation_confirmed_at IS NOT NULL", name='mock_automation_confirmed_cancel'),
        sa.CheckConstraint("status != 'issued' OR result IS NOT NULL", name='mock_automation_result_required'))
    for field in ('payment', 'order'):
        op.create_index('uq_mock_automation_active_' + field, 'mock_fiscal_automations', [field + '_id'], unique=True,
            postgresql_where=sa.text("status != 'cancelled'"))
    op.create_table('mock_automation_commands', sa.Column('id', sa.String(36), primary_key=True), ref('project_id', 'mock_fiscal_automations'),
        sa.Column('operation', sa.String(16), nullable=False), sa.Column('status', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id', 'operation'),
        sa.CheckConstraint("operation IN ('create','cancel','trigger') AND status IN ('pending','unknown','done','blocked','cancelled') AND attempts >= 0", name='mock_automation_command_state'))
    op.create_table('mock_automation_records', sa.Column('provider_uuid', sa.String(36), primary_key=True),
        ref('project_id', 'mock_fiscal_automations', unique=True), sa.Column('external_id', sa.String(64), nullable=False, unique=True),
        sa.Column('snapshot_hash', sa.String(64), nullable=False), sa.Column('status', sa.String(16), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False), sa.Column('result', sa.JSON(), nullable=True),
        sa.CheckConstraint("status IN ('armed','cancelled','issued')", name='mock_automation_record_status'))
    op.create_table('mock_automation_events', ident(), ref('project_id', 'mock_fiscal_automations', nullable=True),
        ref('actor_id', 'admin_users'), sa.Column('operation', sa.String(32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    for table in ('mock_automation_previews', 'mock_automation_events'):
        op.execute(f'CREATE TRIGGER immutable_mock_automation_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')
    op.execute("""CREATE FUNCTION protect_mock_automation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Mock automation approval is immutable'; END IF;
      IF NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.payment_id IS DISTINCT FROM OLD.payment_id
        OR NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR NEW.preview_id IS DISTINCT FROM OLD.preview_id
        OR NEW.actor_id IS DISTINCT FROM OLD.actor_id OR NEW.provider_uuid IS DISTINCT FROM OLD.provider_uuid
        OR NEW.cash_register_id IS DISTINCT FROM OLD.cash_register_id OR NEW.shift_revision IS DISTINCT FROM OLD.shift_revision
        OR NEW.snapshot::jsonb IS DISTINCT FROM OLD.snapshot::jsonb OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
        OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.approved_at IS DISTINCT FROM OLD.approved_at
        OR NEW.mock_outcome IS DISTINCT FROM OLD.mock_outcome
      THEN RAISE EXCEPTION 'Mock automation approved scope is immutable'; END IF;
      IF OLD.status IN ('issued','cancelled') AND NEW.status IS DISTINCT FROM OLD.status
      THEN RAISE EXCEPTION 'Mock automation terminal state is immutable'; END IF;
      IF OLD.result IS NOT NULL AND NEW.result::jsonb IS DISTINCT FROM OLD.result::jsonb
      THEN RAISE EXCEPTION 'Mock automation result is immutable'; END IF;
      IF OLD.external_id IS NOT NULL AND NEW.external_id IS DISTINCT FROM OLD.external_id
      THEN RAISE EXCEPTION 'Mock automation external identity is immutable'; END IF;
      IF NEW.status = 'cancelled' AND EXISTS (SELECT 1 FROM mock_automation_records WHERE project_id = OLD.id AND status IN ('armed','issued'))
      THEN RAISE EXCEPTION 'Mock automation disarm not confirmed'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_mock_automation BEFORE UPDATE OR DELETE ON mock_fiscal_automations FOR EACH ROW EXECUTE FUNCTION protect_mock_automation()')
    op.execute("""CREATE FUNCTION protect_mock_automation_record() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Mock automation lookup truth is immutable'; END IF;
      IF NEW.provider_uuid IS DISTINCT FROM OLD.provider_uuid OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.external_id IS DISTINCT FROM OLD.external_id OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
        OR NEW.accepted_at IS DISTINCT FROM OLD.accepted_at
        OR (OLD.status IN ('issued','cancelled') AND NEW.status IS DISTINCT FROM OLD.status)
        OR (OLD.result IS NOT NULL AND NEW.result::jsonb IS DISTINCT FROM OLD.result::jsonb)
      THEN RAISE EXCEPTION 'Mock automation truth identity or terminal result is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_mock_automation_record BEFORE UPDATE OR DELETE ON mock_automation_records FOR EACH ROW EXECUTE FUNCTION protect_mock_automation_record()')
    op.execute("""CREATE FUNCTION protect_mock_automation_command() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Mock automation command is immutable'; END IF;
      IF NEW.id IS DISTINCT FROM OLD.id OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.operation IS DISTINCT FROM OLD.operation OR NEW.created_at IS DISTINCT FROM OLD.created_at
      THEN RAISE EXCEPTION 'Mock automation command identity is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_mock_automation_command BEFORE UPDATE OR DELETE ON mock_automation_commands FOR EACH ROW EXECUTE FUNCTION protect_mock_automation_command()')
    # Both directions serialize on the commercial order, like the service paths.
    # This also guards direct writers against owning a payment in both tables.
    op.execute("""CREATE FUNCTION exclusive_mock_fiscal_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM orders WHERE id = NEW.order_id FOR UPDATE;
      IF TG_TABLE_NAME = 'mock_fiscal_automations' THEN
        IF NOT EXISTS (SELECT 1 FROM order_payments WHERE id = NEW.payment_id AND order_id = NEW.order_id AND simulation = true)
          OR NOT EXISTS (SELECT 1 FROM order_shipments WHERE id = NEW.shipment_id AND order_id = NEW.order_id AND simulation = true AND carrier = 'mock_nova_poshta')
        THEN RAISE EXCEPTION 'Mock automation payment and shipment must belong to order'; END IF;
        IF NEW.status != 'cancelled' AND EXISTS (SELECT 1 FROM fiscal_documents
          WHERE (order_id = NEW.order_id OR payment_id = NEW.payment_id) AND kind = 'sale' AND status != 'cancelled')
        THEN RAISE EXCEPTION 'Manual fiscal owner already exists'; END IF;
      ELSIF NEW.kind = 'sale' AND NEW.status != 'cancelled' AND EXISTS (SELECT 1 FROM mock_fiscal_automations
          WHERE (order_id = NEW.order_id OR payment_id = NEW.payment_id) AND status != 'cancelled')
      THEN RAISE EXCEPTION 'Automation fiscal owner already exists'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER exclusive_mock_fiscal_owner BEFORE INSERT OR UPDATE ON mock_fiscal_automations FOR EACH ROW EXECUTE FUNCTION exclusive_mock_fiscal_owner()')
    op.execute('CREATE TRIGGER exclusive_mock_fiscal_owner BEFORE INSERT OR UPDATE ON fiscal_documents FOR EACH ROW EXECUTE FUNCTION exclusive_mock_fiscal_owner()')


def downgrade():
    for table in ('mock_fiscal_automations', 'mock_automation_previews', 'mock_automation_events'):
        if op.get_bind().execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM {table})')).scalar():
            raise RuntimeError('Stage 6 audit facts exist; downgrade refused')
    op.execute('DROP TRIGGER exclusive_mock_fiscal_owner ON fiscal_documents')
    for table in ('mock_automation_events', 'mock_automation_records', 'mock_automation_commands', 'mock_fiscal_automations', 'mock_automation_previews'):
        op.drop_table(table)
    for function in ('exclusive_mock_fiscal_owner', 'protect_mock_automation', 'protect_mock_automation_record', 'protect_mock_automation_command'):
        op.execute('DROP FUNCTION ' + function + '()')
