"""Add remote sandbox evidence; preserve existing local simulation facts."""
from alembic import op
import sqlalchemy as sa

revision = '0120_provider_sandbox'
down_revision = '0119_mock_monobank_qr'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('provider_sandbox_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('attempt_id', sa.Integer(), sa.ForeignKey('qr_payment_attempts.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('receipt_uuid', sa.String(36), nullable=False, unique=True),
        sa.Column('invoice_id', sa.String(128), nullable=True, unique=True),
        sa.Column('payment_url', sa.String(512), nullable=True),
        sa.Column('invoice_dispatched', sa.Boolean(), nullable=False),
        sa.Column('receipt_dispatched', sa.Boolean(), nullable=False),
        sa.Column('verification', sa.JSON(), nullable=False),
        sa.Column('payment_status', sa.String(16), nullable=False),
        sa.Column('receipt_status', sa.String(16), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('payment_evidence', sa.JSON(), nullable=True),
        sa.Column('receipt_evidence', sa.JSON(), nullable=True),
        sa.Column('last_error', sa.String(80), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("payment_status IN ('waiting','processing','paid','declined','expired','cancelled') AND receipt_status IN ('pending','issued','error')", name='sandbox_states'))
    op.execute("""CREATE OR REPLACE FUNCTION protect_qr_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
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
          OR COALESCE(NEW.snapshot->>'source','') NOT IN ('mock_monobank_qr','sandbox_monobank_qr') OR (NEW.snapshot->>'source' = 'sandbox_monobank_qr' AND NEW.snapshot->>'transport' IS DISTINCT FROM 'provider_sandbox')
          OR NEW.snapshot->>'transport' = 'provider_sandbox' AND NEW.snapshot->>'source' IS DISTINCT FROM 'sandbox_monobank_qr'
          OR NEW.snapshot->>'source' = 'sandbox_monobank_qr' AND (
            jsonb_typeof(NEW.snapshot::jsonb->'sandbox') IS DISTINCT FROM 'object'
            OR NEW.snapshot->'sandbox'->>'reference' IS DISTINCT FROM NEW.provider_invoice_id
            OR COALESCE(NEW.snapshot->'sandbox'->>'checkbox_cash_register_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            OR COALESCE(NEW.snapshot->'sandbox'->>'checkbox_cashier_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            OR COALESCE(NEW.snapshot->'sandbox'->>'checkbox_organization_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            OR COALESCE(NEW.snapshot->'sandbox'->>'checkbox_shift_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          )
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
    op.execute("""CREATE OR REPLACE FUNCTION validate_qr_projection() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q qr_payment_attempts%ROWTYPE; t mock_qr_provider_records%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME = 'qr_payment_attempts' THEN
        SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.id;
      ELSE
        SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.attempt_id;
      END IF;
      SELECT * INTO t FROM mock_qr_provider_records WHERE attempt_id = q.id;
      IF t.attempt_id IS NULL OR t.provider_invoice_id != q.provider_invoice_id OR t.snapshot_hash != q.snapshot_hash
        OR q.payment_status != t.payment_status AND NOT (q.payment_status = 'unknown' AND (t.payment_status = 'paid' OR q.snapshot->>'source' = 'sandbox_monobank_qr' AND t.payment_status IN ('waiting','processing')))
        OR q.receipt_status = 'issued' AND t.receipt_status != 'issued'
        OR q.payment_status = 'paid' AND NOT EXISTS (SELECT 1 FROM order_payments WHERE order_id = q.order_id
          AND source = 'provider_confirmation' AND method = 'monobank_qr' AND evidence = q.provider_invoice_id AND amount_minor = q.amount_minor)
        OR q.payment_status = 'paid' AND q.receipt_status = 'issued' AND NOT EXISTS (SELECT 1 FROM fiscal_documents WHERE qr_attempt_id = q.id AND status = 'issued')
      THEN RAISE EXCEPTION 'QR projection must match durable invoice payment and canonical receipt'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION guard_sandbox_run() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q qr_payment_attempts%ROWTYPE;
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Remote sandbox history is immutable'; END IF;
      SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.attempt_id;
      IF q.id IS NULL OR q.snapshot->>'source' IS DISTINCT FROM 'sandbox_monobank_qr'
        OR q.snapshot->>'transport' IS DISTINCT FROM 'provider_sandbox'
      THEN RAISE EXCEPTION 'Sandbox evidence requires its own immutable consent'; END IF;
      IF (NEW.verification::jsonb->'checkbox_provider_confirmed') IS DISTINCT FROM 'true'::jsonb
        OR (NEW.verification::jsonb->'monobank_provider_confirmed') IS DISTINCT FROM 'false'::jsonb
        OR COALESCE(NEW.verification->>'monobank_token_sha256','') !~ '^[0-9a-f]{64}$'
        OR NEW.verification->>'monobank_policy' IS DISTINCT FROM 'operator_attested_test_token_with_pinned_sha256'
        OR COALESCE(NEW.verification->>'shift_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR NEW.verification->>'shift_id' IS DISTINCT FROM q.snapshot->'sandbox'->>'checkbox_shift_id'
      THEN RAISE EXCEPTION 'Sandbox requires test register proof and honest monobank provenance'; END IF;
      IF TG_OP = 'INSERT' AND (NEW.payment_status != 'waiting' OR NEW.receipt_status != 'pending'
          OR NEW.accepted_at IS NOT NULL OR NEW.payment_evidence IS NOT NULL OR NEW.receipt_evidence IS NOT NULL)
      THEN RAISE EXCEPTION 'Sandbox run starts without provider acceptance'; END IF;
      IF TG_OP = 'UPDATE' AND (
          NEW.attempt_id != OLD.attempt_id OR NEW.receipt_uuid != OLD.receipt_uuid
          OR NEW.verification::jsonb IS DISTINCT FROM OLD.verification::jsonb OR NEW.created_at != OLD.created_at
          OR OLD.invoice_id IS NOT NULL AND NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
          OR OLD.payment_url IS NOT NULL AND NEW.payment_url IS DISTINCT FROM OLD.payment_url
          OR OLD.invoice_dispatched AND NOT NEW.invoice_dispatched OR OLD.receipt_dispatched AND NOT NEW.receipt_dispatched
          OR OLD.payment_status IN ('paid','declined','expired','cancelled') AND NEW.payment_status != OLD.payment_status
          OR OLD.accepted_at IS NOT NULL AND NEW.accepted_at IS DISTINCT FROM OLD.accepted_at
          OR OLD.payment_status = 'paid' AND NEW.payment_evidence::jsonb IS DISTINCT FROM OLD.payment_evidence::jsonb
          OR OLD.receipt_status = 'issued' AND (NEW.receipt_status != 'issued' OR NEW.receipt_evidence::jsonb IS DISTINCT FROM OLD.receipt_evidence::jsonb))
      THEN RAISE EXCEPTION 'Remote sandbox identity and terminal acceptance are immutable'; END IF;
      IF NEW.payment_status = 'paid' AND (NEW.invoice_id IS NULL OR NEW.accepted_at IS NULL
          OR NEW.payment_evidence->>'invoice_id' IS DISTINCT FROM NEW.invoice_id
          OR NEW.payment_evidence->>'reference' IS DISTINCT FROM q.provider_invoice_id
          OR NEW.payment_evidence->>'status' IS DISTINCT FROM 'paid'
          OR jsonb_typeof(NEW.payment_evidence::jsonb->'amount_minor') IS DISTINCT FROM 'number'
          OR (NEW.payment_evidence->>'accepted_at')::timestamptz IS DISTINCT FROM NEW.accepted_at
          OR NEW.payment_evidence->>'amount_minor' IS DISTINCT FROM q.amount_minor::text
          OR NEW.payment_evidence->>'currency' IS DISTINCT FROM 'UAH')
      THEN RAISE EXCEPTION 'Sandbox paid requires exact remote invoice evidence'; END IF;
      IF NEW.receipt_status = 'issued' AND (NEW.payment_status != 'paid' OR NOT NEW.receipt_dispatched
          OR NEW.receipt_evidence->>'receipt_uuid' IS DISTINCT FROM NEW.receipt_uuid
          OR NEW.receipt_evidence->>'status' IS DISTINCT FROM 'issued'
          OR (NEW.receipt_evidence::jsonb->'is_test') IS DISTINCT FROM 'true'::jsonb
          OR (NEW.receipt_evidence::jsonb->'is_sent_dps') IS DISTINCT FROM 'false'::jsonb
          OR NEW.receipt_evidence->>'shift_id' IS DISTINCT FROM NEW.verification->>'shift_id'
          OR NEW.receipt_evidence->>'shift_id' IS DISTINCT FROM q.snapshot->'sandbox'->>'checkbox_shift_id'
          OR NEW.receipt_evidence->>'cash_register_id' IS DISTINCT FROM q.snapshot->'sandbox'->>'checkbox_cash_register_id'
          OR NEW.receipt_evidence->>'cashier_id' IS DISTINCT FROM q.snapshot->'sandbox'->>'checkbox_cashier_id'
          OR NEW.receipt_evidence->>'organization_id' IS DISTINCT FROM q.snapshot->'sandbox'->>'checkbox_organization_id'
          OR jsonb_typeof(NEW.receipt_evidence::jsonb->'amount_minor') IS DISTINCT FROM 'number'
          OR NEW.receipt_evidence->>'amount_minor' IS DISTINCT FROM q.amount_minor::text
          OR NEW.receipt_evidence->>'currency' IS DISTINCT FROM 'UAH')
      THEN RAISE EXCEPTION 'Sandbox issued requires exact remote test receipt evidence'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER guard_sandbox_run BEFORE INSERT OR UPDATE OR DELETE ON provider_sandbox_runs FOR EACH ROW EXECUTE FUNCTION guard_sandbox_run()')
    op.execute("""CREATE FUNCTION guard_sandbox_projection() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q qr_payment_attempts%ROWTYPE; r provider_sandbox_runs%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME = 'mock_qr_provider_records' THEN
        SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.attempt_id;
      ELSIF TG_TABLE_NAME = 'qr_payment_commands' OR TG_TABLE_NAME = 'qr_payment_events' THEN
        SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.attempt_id;
      ELSIF TG_TABLE_NAME = 'fiscal_documents' THEN
        SELECT * INTO q FROM qr_payment_attempts WHERE id = NEW.qr_attempt_id;
      ELSE
        SELECT * INTO q FROM qr_payment_attempts WHERE order_id = NEW.order_id AND payment_status IN ('waiting','processing','unknown','paid');
      END IF;
      IF q.snapshot->>'source' IS DISTINCT FROM 'sandbox_monobank_qr' THEN RETURN NEW; END IF;
      SELECT * INTO r FROM provider_sandbox_runs WHERE attempt_id = q.id;
      IF r.id IS NULL THEN RAISE EXCEPTION 'Sandbox projection requires authoritative remote run'; END IF;
      IF TG_TABLE_NAME = 'qr_payment_events' THEN RAISE EXCEPTION 'Local simulation cannot mutate a remote provider attempt'; END IF;
      IF TG_TABLE_NAME = 'qr_payment_commands' THEN
        IF NEW.operation IN ('simulate','cancel') THEN RAISE EXCEPTION 'Local simulation cannot mutate a remote provider attempt'; END IF;
      END IF;
      IF TG_TABLE_NAME = 'mock_qr_provider_records' THEN
        IF NEW.payment_status != r.payment_status OR NEW.receipt_status != r.receipt_status OR NEW.accepted_at IS DISTINCT FROM r.accepted_at
        THEN RAISE EXCEPTION 'Local provider projection must match remote evidence'; END IF;
      ELSIF TG_TABLE_NAME = 'order_payments' OR TG_TABLE_NAME = 'order_handovers' THEN
        IF r.payment_status != 'paid' OR r.payment_evidence IS NULL OR (TG_TABLE_NAME = 'order_handovers' AND r.receipt_status != 'issued')
        THEN RAISE EXCEPTION 'Sandbox payment or handover requires remote acceptance'; END IF;
      ELSIF TG_TABLE_NAME = 'fiscal_documents' AND (r.payment_status != 'paid' OR r.receipt_status != 'issued' OR r.receipt_evidence IS NULL) THEN
        RAISE EXCEPTION 'Sandbox fiscal projection requires remote test receipt';
      END IF;
      RETURN NEW;
    END $$""")
    for table in ('mock_qr_provider_records','qr_payment_commands','qr_payment_events','order_payments','order_handovers','fiscal_documents'):
        op.execute(f'CREATE TRIGGER guard_sandbox_projection BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION guard_sandbox_projection()')
    op.execute("""CREATE FUNCTION validate_sandbox_consent() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.snapshot->>'source' = 'sandbox_monobank_qr' AND NOT EXISTS (SELECT 1 FROM provider_sandbox_runs WHERE attempt_id = NEW.id)
      THEN RAISE EXCEPTION 'Sandbox consent requires durable dispatch run'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE CONSTRAINT TRIGGER validate_sandbox_consent AFTER INSERT OR UPDATE ON qr_payment_attempts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_sandbox_consent()')
    op.execute("""CREATE FUNCTION block_sandbox_mock_return() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM fiscal_documents d JOIN qr_payment_attempts q ON q.id = d.qr_attempt_id
        WHERE d.id = NEW.parent_sale_id AND q.snapshot->>'source' = 'sandbox_monobank_qr')
      THEN RAISE EXCEPTION 'Provider sandbox returns require provider refund support'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER block_sandbox_mock_return BEFORE INSERT OR UPDATE ON mock_returns FOR EACH ROW EXECUTE FUNCTION block_sandbox_mock_return()')


def downgrade():
    # Refuse before DDL so accepted remote history can never be removed.
    if op.get_bind().execute(sa.text('SELECT EXISTS (SELECT 1 FROM provider_sandbox_runs)')).scalar():
        raise RuntimeError('Provider sandbox history exists; downgrade refused')
    op.execute('DROP TRIGGER block_sandbox_mock_return ON mock_returns')
    op.execute('DROP FUNCTION block_sandbox_mock_return()')
    op.execute('DROP TRIGGER validate_sandbox_consent ON qr_payment_attempts')
    op.execute('DROP FUNCTION validate_sandbox_consent()')
    for table in ('mock_qr_provider_records','qr_payment_commands','qr_payment_events','order_payments','order_handovers','fiscal_documents'):
        op.execute(f'DROP TRIGGER guard_sandbox_projection ON {table}')
    op.execute('DROP FUNCTION guard_sandbox_projection()')
    op.drop_table('provider_sandbox_runs')
    op.execute('DROP FUNCTION guard_sandbox_run()')
    op.execute("""CREATE OR REPLACE FUNCTION protect_qr_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
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
    op.execute("""CREATE OR REPLACE FUNCTION validate_qr_projection() RETURNS trigger LANGUAGE plpgsql AS $$
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
