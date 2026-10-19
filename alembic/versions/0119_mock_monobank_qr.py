"""Add isolated durable monobank QR simulation without rewriting payment facts."""
from alembic import op
import sqlalchemy as sa

revision = '0119_mock_monobank_qr'
down_revision = '0118_ettn_canonical_receipts'
branch_labels = None
depends_on = None


def col(name, kind, *constraints, **kwargs):
    return sa.Column(name, kind, *constraints, nullable=False, **kwargs)


def ref(name, table, **kwargs):
    return col(name, sa.Integer(), sa.ForeignKey(table + '.id', ondelete='RESTRICT'), **kwargs)


def upgrade():
    op.add_column('admin_users', col('can_accept_qr_payments', sa.Boolean(), server_default=sa.false()))
    op.create_table('qr_payment_attempts', col('id', sa.Integer(), primary_key=True), ref('order_id', 'orders'),
        ref('actor_id', 'admin_users'), col('provider_invoice_id', sa.String(36), unique=True),
        sa.Column('cash_register_id', sa.String(32), sa.ForeignKey('mock_cash_registers.id', ondelete='RESTRICT'), nullable=False),
        col('shift_revision', sa.Integer()), col('amount_minor', sa.BigInteger()), col('snapshot', sa.JSON()),
        col('snapshot_hash', sa.String(64)), col('payment_status', sa.String(16)), col('receipt_status', sa.String(16)),
        col('revision', sa.Integer()), col('simulation', sa.Boolean()), col('created_at', sa.DateTime(timezone=True)),
        col('expires_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint('amount_minor > 0 AND simulation = true', name='qr_attempt_mock_positive'),
        sa.CheckConstraint("payment_status IN ('waiting','processing','unknown','paid','declined','expired','cancelled') AND receipt_status IN ('pending','issued','error')", name='qr_attempt_states'))
    op.create_index('uq_qr_active_order', 'qr_payment_attempts', ['order_id'], unique=True,
        postgresql_where=sa.text("payment_status IN ('waiting','processing','unknown','paid')"))
    op.create_table('mock_qr_provider_records', col('provider_invoice_id', sa.String(36), primary_key=True),
        ref('attempt_id', 'qr_payment_attempts', unique=True), col('snapshot_hash', sa.String(64)),
        col('payment_status', sa.String(16)), col('receipt_status', sa.String(16)),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("payment_status IN ('waiting','processing','paid','declined','expired','cancelled') AND receipt_status IN ('pending','issued','error') AND ((payment_status = 'paid') = (accepted_at IS NOT NULL))", name='qr_provider_states'))
    op.create_table('qr_payment_commands', col('id', sa.Integer(), primary_key=True), ref('attempt_id', 'qr_payment_attempts'),
        ref('actor_id', 'admin_users'), col('operation_key', sa.String(64), unique=True),
        col('operation', sa.String(16)), col('payload', sa.JSON()), col('created_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("operation IN ('create','refresh','cancel','simulate')", name='qr_command_operation'))
    op.create_table('qr_payment_events', col('event_id', sa.String(36), primary_key=True), ref('attempt_id', 'qr_payment_attempts'),
        ref('actor_id', 'admin_users'), col('outcome', sa.String(32)), col('created_at', sa.DateTime(timezone=True)))
    op.add_column('fiscal_documents', sa.Column('qr_attempt_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_fiscal_qr_attempt', 'fiscal_documents', 'qr_payment_attempts', ['qr_attempt_id'], ['id'], ondelete='RESTRICT')
    op.create_unique_constraint('uq_fiscal_qr_attempt', 'fiscal_documents', ['qr_attempt_id'])
    op.add_column('mock_refunds', sa.Column('qr_attempt_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_refund_qr_attempt', 'mock_refunds', 'qr_payment_attempts', ['qr_attempt_id'], ['id'], ondelete='RESTRICT')
    op.drop_constraint('mock_refund_only', 'mock_refunds', type_='check')
    op.create_check_constraint('mock_refund_only', 'mock_refunds', "amount_minor >= 0 AND simulation = true AND ((method IN ('cash','card') AND qr_attempt_id IS NULL) OR (method = 'monobank_qr' AND qr_attempt_id IS NOT NULL))")
    for table in ('qr_payment_commands', 'qr_payment_events'):
        op.execute(f'CREATE TRIGGER immutable_qr_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()')
    op.execute("""CREATE FUNCTION protect_qr_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'QR consent history is immutable'; END IF;
      PERFORM 1 FROM orders WHERE id = NEW.order_id FOR UPDATE;
      IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - ARRAY['payment_status','receipt_status','revision']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['payment_status','receipt_status','revision'])
        THEN RAISE EXCEPTION 'QR consent snapshot is immutable'; END IF;
        IF OLD.payment_status IN ('paid','declined','expired','cancelled') AND NEW.payment_status != OLD.payment_status
          OR OLD.receipt_status = 'issued' AND NEW.receipt_status != 'issued'
        THEN RAISE EXCEPTION 'Accepted QR terminal state cannot regress'; END IF;
      ELSE
        IF NEW.payment_status != 'waiting' OR NEW.receipt_status != 'pending' OR NEW.revision != 1
          OR NOT EXISTS (SELECT 1 FROM orders WHERE id = NEW.order_id AND simulation = true AND channel = 'backoffice_pos' AND status::text IN ('pending','confirmed') AND total_amount * 100 = NEW.amount_minor)
          OR EXISTS (SELECT 1 FROM order_payments WHERE order_id = NEW.order_id)
          OR EXISTS (SELECT 1 FROM fiscal_documents WHERE order_id = NEW.order_id AND kind = 'sale' AND status != 'cancelled')
          OR EXISTS (SELECT 1 FROM mock_fiscal_automations WHERE order_id = NEW.order_id AND status != 'cancelled')
          OR NEW.snapshot->>'amount_minor' IS DISTINCT FROM NEW.amount_minor::text
          OR NEW.snapshot->>'source' IS DISTINCT FROM 'mock_monobank_qr'
          OR NEW.snapshot->>'order_id' IS DISTINCT FROM NEW.order_id::text
          OR NEW.snapshot->>'cash_register_id' IS DISTINCT FROM NEW.cash_register_id
          OR NEW.snapshot->>'shift_revision' IS DISTINCT FROM NEW.shift_revision::text
          OR NEW.snapshot->>'currency' IS DISTINCT FROM 'UAH'
          OR NOT EXISTS (SELECT 1 FROM mock_cash_registers WHERE id = NEW.cash_register_id AND credential_kind = 'mock' AND shift_status = 'open' AND shift_revision = NEW.shift_revision)
          OR NEW.snapshot->'consent'->>'actor_id' IS DISTINCT FROM NEW.actor_id::text
          OR NEW.snapshot->'consent'->>'confirm' IS DISTINCT FROM 'true'
        THEN RAISE EXCEPTION 'QR invoice requires unpaid simulated POS consent and exclusive ownership'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_qr_attempt BEFORE INSERT OR UPDATE OR DELETE ON qr_payment_attempts FOR EACH ROW EXECUTE FUNCTION protect_qr_attempt()')
    op.execute("""CREATE FUNCTION protect_qr_truth() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'QR provider truth cannot be deleted'; END IF;
      IF NOT EXISTS (SELECT 1 FROM qr_payment_attempts WHERE id = NEW.attempt_id AND provider_invoice_id = NEW.provider_invoice_id AND snapshot_hash = NEW.snapshot_hash)
      THEN RAISE EXCEPTION 'QR provider truth scope mismatch'; END IF;
      IF TG_OP = 'INSERT' AND (NEW.payment_status != 'waiting' OR NEW.receipt_status != 'pending' OR NEW.accepted_at IS NOT NULL)
      THEN RAISE EXCEPTION 'QR provider invoice must start unpaid'; END IF;
      IF TG_OP = 'UPDATE' AND (
        NEW.provider_invoice_id IS DISTINCT FROM OLD.provider_invoice_id OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
        OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
        OR OLD.payment_status IN ('paid','declined','expired','cancelled') AND NEW.payment_status != OLD.payment_status
        OR OLD.accepted_at IS NOT NULL AND NEW.accepted_at IS DISTINCT FROM OLD.accepted_at
        OR OLD.receipt_status = 'issued' AND NEW.receipt_status != 'issued')
      THEN RAISE EXCEPTION 'Accepted QR truth is immutable and monotonic'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER protect_qr_truth BEFORE INSERT OR UPDATE OR DELETE ON mock_qr_provider_records FOR EACH ROW EXECUTE FUNCTION protect_qr_truth()')
    op.execute("""CREATE FUNCTION guard_qr_fiscal_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q qr_payment_attempts%ROWTYPE; t mock_qr_provider_records%ROWTYPE; p order_payments%ROWTYPE; expected jsonb;
    BEGIN
      PERFORM 1 FROM orders WHERE id = NEW.order_id FOR UPDATE;
      IF TG_TABLE_NAME = 'mock_fiscal_automations' THEN
        IF EXISTS (SELECT 1 FROM qr_payment_attempts WHERE order_id = NEW.order_id AND payment_status IN ('waiting','processing','unknown','paid'))
        THEN RAISE EXCEPTION 'QR owns this fiscal sale'; END IF;
        RETURN NEW;
      END IF;
      IF TG_OP = 'UPDATE' AND NEW.qr_attempt_id IS DISTINCT FROM OLD.qr_attempt_id
      THEN RAISE EXCEPTION 'Canonical QR provenance is immutable'; END IF;
      IF NEW.qr_attempt_id IS NULL THEN
        IF NEW.snapshot->>'source' = 'mock_monobank_qr' OR NEW.snapshot::jsonb ? 'qr_payment'
          OR EXISTS (SELECT 1 FROM qr_payment_attempts WHERE provider_invoice_id = NEW.provider_uuid)
          OR NEW.kind = 'sale' AND EXISTS (SELECT 1 FROM qr_payment_attempts WHERE order_id = NEW.order_id AND payment_status IN ('waiting','processing','unknown','paid'))
        THEN RAISE EXCEPTION 'QR receipt requires canonical owner link'; END IF;
        RETURN NEW;
      END IF;
      SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.qr_attempt_id;
      SELECT * INTO t FROM mock_qr_provider_records WHERE attempt_id = q.id;
      SELECT * INTO p FROM order_payments WHERE id = NEW.payment_id;
      IF q.id IS NULL OR t.attempt_id IS NULL OR p.id IS NULL OR q.payment_status != 'paid' OR q.receipt_status != 'issued'
        OR t.payment_status != 'paid' OR t.receipt_status != 'issued' OR t.snapshot_hash != q.snapshot_hash
        OR NEW.kind != 'sale' OR NEW.status != 'issued' OR NOT NEW.simulation OR NEW.automation_id IS NOT NULL
        OR NEW.order_id != q.order_id OR NEW.actor_id != q.actor_id OR NEW.provider_uuid != q.provider_invoice_id
        OR NEW.cash_register_id != q.cash_register_id OR NEW.shift_revision != q.shift_revision
        OR NEW.approved_at != q.created_at OR NEW.dispatched_at IS DISTINCT FROM t.accepted_at
        OR p.order_id != q.order_id OR p.amount_minor != q.amount_minor OR NOT p.simulation
        OR p.method != 'monobank_qr' OR p.source != 'provider_confirmation' OR p.evidence != q.provider_invoice_id
        OR p.operator_id != q.actor_id OR p.created_at IS DISTINCT FROM t.accepted_at
      THEN RAISE EXCEPTION 'Canonical QR receipt acceptance mismatch'; END IF;
      expected := q.snapshot::jsonb || jsonb_build_object('payment', jsonb_build_object('id',p.id,'amount_minor',p.amount_minor,
        'method',p.method,'source',p.source,'simulation',true), 'qr_payment', jsonb_build_object('attempt_id',q.id,
        'provider_invoice_id',q.provider_invoice_id,'consent_snapshot_hash',q.snapshot_hash));
      IF NEW.snapshot::jsonb IS DISTINCT FROM expected OR NOT EXISTS (SELECT 1 FROM fiscal_previews WHERE id = NEW.preview_id
        AND order_id = q.order_id AND actor_id = q.actor_id AND cash_register_id = q.cash_register_id
        AND shift_revision = q.shift_revision AND snapshot::jsonb = expected AND snapshot_hash = NEW.snapshot_hash)
      THEN RAISE EXCEPTION 'Canonical QR snapshot mismatch'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER guard_qr_fiscal_owner BEFORE INSERT OR UPDATE ON fiscal_documents FOR EACH ROW EXECUTE FUNCTION guard_qr_fiscal_owner()')
    op.execute('CREATE TRIGGER guard_qr_automation_owner BEFORE INSERT OR UPDATE ON mock_fiscal_automations FOR EACH ROW EXECUTE FUNCTION guard_qr_fiscal_owner()')
    op.execute("""CREATE FUNCTION validate_qr_receipt_acceptance() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.qr_attempt_id IS NOT NULL AND (NOT EXISTS (SELECT 1 FROM mock_receipts WHERE document_id = NEW.id
          AND provider_uuid = NEW.provider_uuid AND snapshot_hash = NEW.snapshot_hash AND accepted_at = NEW.dispatched_at)
        OR EXISTS (SELECT 1 FROM fiscal_jobs WHERE document_id = NEW.id))
      THEN RAISE EXCEPTION 'Canonical QR receipt requires acceptance and no dispatch job'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE CONSTRAINT TRIGGER validate_qr_receipt_acceptance AFTER INSERT OR UPDATE ON fiscal_documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_qr_receipt_acceptance()')
    op.execute("""CREATE FUNCTION prevent_qr_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM fiscal_documents WHERE id = NEW.document_id AND qr_attempt_id IS NOT NULL)
      THEN RAISE EXCEPTION 'Canonical QR receipt cannot dispatch again'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER prevent_qr_dispatch BEFORE INSERT OR UPDATE ON fiscal_jobs FOR EACH ROW EXECUTE FUNCTION prevent_qr_dispatch()')
    op.execute("""CREATE FUNCTION guard_qr_sale_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q qr_payment_attempts%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME = 'orders' THEN
        SELECT * INTO q FROM qr_payment_attempts WHERE order_id = NEW.id AND payment_status IN ('waiting','processing','unknown','paid');
        IF q.id IS NOT NULL AND (NEW.total_amount IS DISTINCT FROM OLD.total_amount OR NEW.subtotal_amount IS DISTINCT FROM OLD.subtotal_amount
          OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method
          OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.channel IS DISTINCT FROM OLD.channel
          OR NEW.status IS DISTINCT FROM OLD.status AND NOT (NEW.status::text = 'completed' AND q.payment_status = 'paid' AND q.receipt_status = 'issued'))
        THEN RAISE EXCEPTION 'Active QR owns immutable checkout'; END IF;
      ELSIF TG_TABLE_NAME = 'order_items' THEN
        IF EXISTS (SELECT 1 FROM qr_payment_attempts WHERE (order_id = NEW.order_id OR order_id = OLD.order_id) AND payment_status IN ('waiting','processing','unknown','paid'))
        THEN RAISE EXCEPTION 'QR checkout items are immutable'; END IF;
      ELSIF TG_TABLE_NAME = 'order_payments' THEN
        PERFORM 1 FROM orders WHERE id = NEW.order_id FOR UPDATE;
        SELECT * INTO q FROM qr_payment_attempts WHERE order_id = NEW.order_id AND payment_status IN ('waiting','processing','unknown','paid');
        IF q.id IS NOT NULL AND (NEW.source != 'provider_confirmation' OR NEW.method != 'monobank_qr'
          OR NEW.evidence != q.provider_invoice_id OR NEW.amount_minor != q.amount_minor OR NOT NEW.simulation
          OR NOT EXISTS (SELECT 1 FROM mock_qr_provider_records WHERE attempt_id = q.id AND payment_status = 'paid'))
        THEN RAISE EXCEPTION 'QR payment requires matching accepted invoice'; END IF;
        IF (NEW.source = 'provider_confirmation' OR NEW.method = 'monobank_qr') AND q.id IS NULL
        THEN RAISE EXCEPTION 'Provider QR payment requires its invoice'; END IF;
      ELSIF TG_TABLE_NAME = 'order_handovers' THEN
        PERFORM 1 FROM orders WHERE id = NEW.order_id FOR UPDATE;
        IF EXISTS (SELECT 1 FROM qr_payment_attempts WHERE order_id = NEW.order_id AND payment_status IN ('waiting','processing','unknown','paid')
          AND (payment_status != 'paid' OR receipt_status != 'issued'))
        THEN RAISE EXCEPTION 'QR handover requires paid and issued'; END IF;
      END IF;
      RETURN COALESCE(NEW,OLD);
    END $$""")
    op.execute('CREATE TRIGGER guard_qr_order BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION guard_qr_sale_mutation()')
    op.execute('CREATE TRIGGER guard_qr_items BEFORE INSERT OR UPDATE OR DELETE ON order_items FOR EACH ROW EXECUTE FUNCTION guard_qr_sale_mutation()')
    for table in ('order_payments', 'order_handovers'):
        op.execute(f'CREATE TRIGGER guard_qr_sale BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION guard_qr_sale_mutation()')
    op.execute("""CREATE FUNCTION validate_qr_projection() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q qr_payment_attempts%ROWTYPE; t mock_qr_provider_records%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME = 'qr_payment_attempts' THEN
        SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.id;
      ELSE
        SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.attempt_id;
      END IF;
      SELECT * INTO t FROM mock_qr_provider_records WHERE attempt_id = q.id;
      IF t.attempt_id IS NULL OR t.provider_invoice_id != q.provider_invoice_id OR t.snapshot_hash != q.snapshot_hash
        OR q.payment_status != t.payment_status AND NOT (q.payment_status = 'unknown' AND t.payment_status = 'paid')
        OR q.receipt_status = 'issued' AND t.receipt_status != 'issued'
        OR q.payment_status = 'paid' AND NOT EXISTS (SELECT 1 FROM order_payments WHERE order_id = q.order_id
          AND source = 'provider_confirmation' AND method = 'monobank_qr' AND evidence = q.provider_invoice_id AND amount_minor = q.amount_minor)
        OR q.payment_status = 'paid' AND q.receipt_status = 'issued' AND NOT EXISTS (SELECT 1 FROM fiscal_documents WHERE qr_attempt_id = q.id AND status = 'issued')
      THEN RAISE EXCEPTION 'QR projection must match durable invoice payment and canonical receipt'; END IF;
      RETURN NEW;
    END $$""")
    for table in ('qr_payment_attempts', 'mock_qr_provider_records'):
        op.execute(f'CREATE CONSTRAINT TRIGGER validate_qr_projection AFTER INSERT OR UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_qr_projection()')
    op.execute("""CREATE FUNCTION guard_qr_refund() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE sale fiscal_documents%ROWTYPE; r mock_returns%ROWTYPE;
    BEGIN
      SELECT * INTO r FROM mock_returns WHERE id = NEW.return_id;
      SELECT * INTO sale FROM fiscal_documents WHERE id = r.parent_sale_id;
      IF NEW.qr_attempt_id IS DISTINCT FROM sale.qr_attempt_id OR NEW.amount_minor != r.amount_minor
        OR (sale.qr_attempt_id IS NOT NULL AND (NEW.method != 'monobank_qr' OR sale.status != 'issued'
          OR NOT EXISTS (SELECT 1 FROM mock_qr_provider_records WHERE attempt_id = sale.qr_attempt_id AND payment_status = 'paid' AND receipt_status = 'issued')))
      THEN RAISE EXCEPTION 'QR refund must link original accepted sale and return'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER guard_qr_refund BEFORE INSERT ON mock_refunds FOR EACH ROW EXECUTE FUNCTION guard_qr_refund()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM qr_payment_attempts)')).scalar():
        raise RuntimeError('QR consent/payment history exists; downgrade refused')
    for table, trigger in [('mock_refunds','guard_qr_refund'), ('qr_payment_attempts','validate_qr_projection'), ('mock_qr_provider_records','validate_qr_projection'), ('orders','guard_qr_order'), ('order_items','guard_qr_items'),
        ('order_payments','guard_qr_sale'), ('order_handovers','guard_qr_sale'), ('fiscal_jobs','prevent_qr_dispatch'),
        ('fiscal_documents','validate_qr_receipt_acceptance'), ('fiscal_documents','guard_qr_fiscal_owner'),
        ('mock_fiscal_automations','guard_qr_automation_owner')]:
        op.execute(f'DROP TRIGGER {trigger} ON {table}')
    for function in ('validate_qr_projection','guard_qr_refund','guard_qr_sale_mutation','prevent_qr_dispatch','validate_qr_receipt_acceptance','guard_qr_fiscal_owner'):
        op.execute(f'DROP FUNCTION {function}()')
    op.drop_constraint('mock_refund_only', 'mock_refunds', type_='check')
    op.create_check_constraint('mock_refund_only', 'mock_refunds', "amount_minor >= 0 AND simulation = true AND method IN ('cash','card')")
    op.drop_constraint('fk_refund_qr_attempt','mock_refunds',type_='foreignkey')
    op.drop_column('mock_refunds','qr_attempt_id')
    op.drop_constraint('uq_fiscal_qr_attempt','fiscal_documents',type_='unique')
    op.drop_constraint('fk_fiscal_qr_attempt','fiscal_documents',type_='foreignkey')
    op.drop_column('fiscal_documents','qr_attempt_id')
    for table in ('qr_payment_events','qr_payment_commands','mock_qr_provider_records','qr_payment_attempts'):
        op.drop_table(table)
    op.execute('DROP FUNCTION protect_qr_truth()')
    op.execute('DROP FUNCTION protect_qr_attempt()')
    op.drop_column('admin_users','can_accept_qr_payments')
