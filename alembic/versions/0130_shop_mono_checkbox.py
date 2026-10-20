"""Immutable external mono→Checkbox receipt imports; no receipt issuer."""
from alembic import op
import sqlalchemy as sa

revision = "0130_shop_mono_checkbox"
down_revision = "0129_shop_ingress_limits"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve all existing R2 guards while admitting an explicit boolean mode.
    definition = op.get_bind().execute(sa.text("SELECT pg_get_functiondef('protect_shop_payment_intent()'::regprocedure)")).scalar_one()
    old = "NEW.profile::jsonb->'external_fiscalization_disabled' IS DISTINCT FROM 'true'::jsonb"
    new = "jsonb_typeof(NEW.profile::jsonb->'external_fiscalization_disabled') IS DISTINCT FROM 'boolean'"
    if old not in definition:
        raise RuntimeError("Unexpected R2 payment guard; direct-mode migration refused")
    op.execute(definition.replace(old, new))
    op.create_table("shop_mono_checkbox_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("payment_confirmation_id", sa.Integer(), sa.ForeignKey("shop_payment_confirmations.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("shop_payment_evidence.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("profile_id", sa.String(64), nullable=False), sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("invoice_id", sa.String(128), nullable=False), sa.Column("receipt_id", sa.String(36), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False), sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False), sa.Column("proof", sa.JSON(), nullable=False),
        sa.Column("proof_canonical", sa.Text(), nullable=False), sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("receipt_id", name="uq_shop_mono_checkbox_receipt_identity"))
    op.create_table("shop_mono_checkbox_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(64), nullable=False), sa.Column("proof", sa.JSON(), nullable=True),
        sa.Column("proof_hash", sa.String(64), nullable=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("shop_mono_checkbox_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_id", sa.Integer(), sa.ForeignKey("shop_mono_checkbox_receipts.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("proof", sa.JSON(), nullable=False), sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_shop_mono_checkbox_observations_order_id", "shop_mono_checkbox_observations", ["order_id"])
    for table in ("shop_mono_checkbox_receipts", "shop_mono_checkbox_observations", "shop_mono_checkbox_artifacts"):
        op.execute(f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION reject_shop_mono_internal_issuer() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF EXISTS(SELECT 1 FROM shop_payment_intents b WHERE b.order_id=NEW.order_id
          AND b.profile::jsonb->'external_fiscalization_disabled'='false'::jsonb) THEN
        RAISE EXCEPTION 'External mono Checkbox order cannot use an internal receipt issuer';
      END IF;
      RETURN NEW;
    END $$""")
    for table in ("shop_checkbox_bindings", "shop_checkbox_operations", "shop_fiscal_documents"):
        op.execute(f"CREATE TRIGGER aaa_reject_shop_mono_internal_issuer BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_shop_mono_internal_issuer()")
    op.execute("""CREATE FUNCTION validate_shop_mono_mode() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.profile::jsonb->'external_fiscalization_disabled'='false'::jsonb AND (
          EXISTS(SELECT 1 FROM shop_checkbox_bindings WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM shop_checkbox_operations WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM shop_fiscal_documents WHERE order_id=NEW.order_id)) THEN
        RAISE EXCEPTION 'External mono Checkbox mode conflicts with existing fiscal history';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_mono_mode BEFORE INSERT ON shop_payment_intents FOR EACH ROW EXECUTE FUNCTION validate_shop_mono_mode()")
    op.execute("""CREATE FUNCTION validate_shop_mono_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE b shop_payment_intents%ROWTYPE; e shop_payment_evidence%ROWTYPE; c shop_payment_confirmations%ROWTYPE; check_row jsonb;
    BEGIN
      SELECT * INTO b FROM shop_payment_intents WHERE order_id=NEW.order_id FOR UPDATE;
      SELECT * INTO e FROM shop_payment_evidence WHERE id=NEW.evidence_id;
      SELECT * INTO c FROM shop_payment_confirmations WHERE id=NEW.payment_confirmation_id;
      check_row=NEW.proof::jsonb->'checks'->0;
      IF b.id IS NULL OR e.id IS NULL OR c.id IS NULL OR b.mode IS DISTINCT FROM 'prepayment'
          OR b.profile::jsonb->'external_fiscalization_disabled' IS DISTINCT FROM 'false'::jsonb
          OR e.order_id IS DISTINCT FROM NEW.order_id OR c.order_id IS DISTINCT FROM NEW.order_id
          OR e.intent_id IS DISTINCT FROM b.id OR c.payment_id IS DISTINCT FROM e.payment_id
          OR NEW.profile_id IS DISTINCT FROM b.profile_id OR NEW.profile_hash IS DISTINCT FROM b.profile_hash
          OR NEW.authority IS DISTINCT FROM b.authority OR NEW.invoice_id IS DISTINCT FROM e.proof::jsonb->>'invoice_id'
          OR NEW.proof_canonical IS DISTINCT FROM shop_payment_canonical(NEW.proof::jsonb)
          OR NEW.proof_hash IS DISTINCT FROM encode(sha256(convert_to(NEW.proof_canonical,'UTF8')),'hex')
          OR NEW.snapshot_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.snapshot::jsonb),'UTF8')),'hex')
          OR NEW.proof::jsonb->>'proof_hash' IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.proof::jsonb-'proof_hash'),'UTF8')),'hex')
          OR NEW.proof::jsonb->'verified' IS DISTINCT FROM 'true'::jsonb
          OR NEW.proof::jsonb->>'invoice_id' IS DISTINCT FROM NEW.invoice_id
          OR NEW.proof::jsonb->>'profile_id' IS DISTINCT FROM b.profile_id
          OR NEW.proof::jsonb->>'merchant_id' IS DISTINCT FROM b.profile::jsonb->>'merchant_id'
          OR NEW.proof::jsonb->>'authority' IS DISTINCT FROM b.authority
          OR NEW.proof::jsonb->>'transport' IS DISTINCT FROM b.transport
          OR NEW.proof::jsonb->>'verification_scope' IS DISTINCT FROM 'provider_invoice_association'
          OR jsonb_array_length(NEW.proof::jsonb->'checks') IS DISTINCT FROM 1
          OR check_row->>'id' IS DISTINCT FROM NEW.receipt_id OR check_row->>'type' IS DISTINCT FROM 'sale'
          OR check_row->>'status' IS DISTINCT FROM 'done' OR check_row->>'fiscalizationSource' IS DISTINCT FROM 'checkbox'
          OR NEW.snapshot::jsonb->>'order_id' IS DISTINCT FROM NEW.order_id::text
          OR NEW.snapshot::jsonb->>'payment_confirmation_id' IS DISTINCT FROM c.id::text
          OR NEW.snapshot::jsonb->>'evidence_id' IS DISTINCT FROM e.id::text
          OR NEW.snapshot::jsonb->>'evidence_hash' IS DISTINCT FROM e.proof_hash
          OR NEW.snapshot::jsonb->>'invoice_id' IS DISTINCT FROM NEW.invoice_id
          OR NEW.snapshot::jsonb->>'reference' IS DISTINCT FROM b.reference
          OR NEW.snapshot::jsonb->>'amount_minor' IS DISTINCT FROM b.amount_minor::text
          OR NEW.snapshot::jsonb->'structured_totals_verified' IS DISTINCT FROM 'false'::jsonb
          OR NEW.snapshot::jsonb->'reports_imported' IS DISTINCT FROM 'false'::jsonb
      THEN RAISE EXCEPTION 'External receipt requires exact canonical paid invoice association'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_mono_receipt BEFORE INSERT ON shop_mono_checkbox_receipts FOR EACH ROW EXECUTE FUNCTION validate_shop_mono_receipt()")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM shop_mono_checkbox_receipts) OR EXISTS(SELECT 1 FROM shop_mono_checkbox_observations) OR EXISTS(SELECT 1 FROM shop_payment_intents WHERE profile::jsonb->'external_fiscalization_disabled'='false'::jsonb)")).scalar():
        raise RuntimeError("External fiscal history exists; downgrade refused")
    for table in ("shop_checkbox_bindings", "shop_checkbox_operations", "shop_fiscal_documents"):
        op.execute(f"DROP TRIGGER aaa_reject_shop_mono_internal_issuer ON {table}")
    op.execute("DROP TRIGGER validate_shop_mono_mode ON shop_payment_intents")
    op.drop_table("shop_mono_checkbox_observations")
    op.drop_table("shop_mono_checkbox_artifacts")
    op.drop_table("shop_mono_checkbox_receipts")
    for name in ("validate_shop_mono_receipt", "reject_shop_mono_internal_issuer", "validate_shop_mono_mode"):
        op.execute(f"DROP FUNCTION {name}()")
    definition = op.get_bind().execute(sa.text("SELECT pg_get_functiondef('protect_shop_payment_intent()'::regprocedure)")).scalar_one()
    op.execute(definition.replace("jsonb_typeof(NEW.profile::jsonb->'external_fiscalization_disabled') IS DISTINCT FROM 'boolean'", "NEW.profile::jsonb->'external_fiscalization_disabled' IS DISTINCT FROM 'true'::jsonb"))
