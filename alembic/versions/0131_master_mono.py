"""Separate local master mono checkout; preserve Shop R2 and legacy QR guards."""
from alembic import op
import sqlalchemy as sa

revision = "0131_master_mono"
down_revision = "0130_shop_mono_checkbox"
branch_labels = None
depends_on = None


def upgrade():
    def identity():
        return sa.Column("id",sa.Integer(),primary_key=True)
    def foreign(name, table, *, unique=False):
        return sa.Column(name,sa.Integer(),sa.ForeignKey(table+".id",ondelete="RESTRICT"),nullable=False,unique=unique)
    def value(name,kind):
        return sa.Column(name,kind,nullable=False)
    def proof():
        return [value("proof",sa.JSON()),value("proof_hash",sa.String(64)),value("created_at",sa.DateTime(timezone=True))]
    op.create_table("master_mono_bindings",identity(),foreign("order_id","orders",unique=True),foreign("operator_id","admin_users"),
        sa.Column("invoice_id",sa.String(100),nullable=False,unique=True),sa.Column("reference",sa.String(36),nullable=False,unique=True),
        value("amount_minor",sa.BigInteger()),value("profile_id",sa.String(64)),value("profile_hash",sa.String(64)),value("profile",sa.JSON()),
        value("authority",sa.String(16)),value("transport",sa.String(16)),value("created_at",sa.DateTime(timezone=True)))
    for table in ("master_mono_payments","master_mono_receipts"):
        columns=[identity(),foreign("order_id","orders",unique=True),foreign("binding_id","master_mono_bindings",unique=True),
            foreign("payment_id","order_payments",unique=True),*proof()]
        if table=="master_mono_receipts":
            columns.append(sa.Column("receipt_id",sa.String(36),nullable=False,unique=True))
        op.create_table(table,*columns)
    op.create_table("master_mono_observations",identity(),foreign("order_id","orders"),value("status",sa.String(64)),*proof())
    op.create_index("ix_master_mono_observations_order_id","master_mono_observations",["order_id"])
    op.create_table("master_mono_artifacts",identity(),foreign("order_id","orders"),foreign("receipt_id","master_mono_receipts",unique=True),*proof())
    op.create_table("master_mono_mock_truth",identity(),foreign("binding_id","master_mono_bindings",unique=True),
        value("payment_status",sa.String(16)),value("receipt_status",sa.String(16)),
        sa.Column("receipt_id",sa.String(36),nullable=False,unique=True),value("updated_at",sa.DateTime(timezone=True)))
    op.execute("""CREATE FUNCTION protect_master_mono_truth() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Synthetic provider history cannot be deleted'; END IF;
      IF TG_OP='UPDATE' THEN
        IF NEW.binding_id IS DISTINCT FROM OLD.binding_id OR NEW.receipt_id IS DISTINCT FROM OLD.receipt_id
          OR OLD.payment_status='reversed' AND NEW.payment_status!='reversed'
          OR OLD.payment_status='success' AND NEW.payment_status NOT IN ('success','reversed')
          OR OLD.receipt_status='done' AND NEW.receipt_status!='done'
        THEN RAISE EXCEPTION 'Synthetic provider identity and accepted terminal truth cannot regress'; END IF;
      ELSE
        IF NEW.payment_status!='created' OR NEW.receipt_status!='new'
        THEN RAISE EXCEPTION 'Synthetic provider starts unpaid'; END IF;
      END IF;
      IF NEW.payment_status NOT IN ('created','success','reversed') OR NEW.receipt_status NOT IN ('new','done')
      THEN RAISE EXCEPTION 'Unsupported synthetic provider status'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_master_mono_truth BEFORE INSERT OR UPDATE OR DELETE ON master_mono_mock_truth FOR EACH ROW EXECUTE FUNCTION protect_master_mono_truth()")
    for table in ("master_mono_bindings", "master_mono_payments", "master_mono_receipts", "master_mono_observations", "master_mono_artifacts"):
        op.execute(f"CREATE TRIGGER immutable_master_mono BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE TABLE mono_external_receipt_identities (
        receipt_id varchar(36) PRIMARY KEY, order_id integer NOT NULL REFERENCES orders(id), source varchar(32) NOT NULL)""")
    op.execute("INSERT INTO mono_external_receipt_identities SELECT receipt_id,order_id,'shop' FROM shop_mono_checkbox_receipts")
    op.execute("""CREATE FUNCTION claim_mono_external_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO mono_external_receipt_identities VALUES(NEW.receipt_id,NEW.order_id,TG_TABLE_NAME);
      RETURN NEW;
    END $$""")
    for table in ("master_mono_receipts", "shop_mono_checkbox_receipts"):
        op.execute(f"CREATE TRIGGER claim_mono_external_identity BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION claim_mono_external_identity()")
    op.execute("CREATE TRIGGER immutable_mono_identity BEFORE UPDATE OR DELETE ON mono_external_receipt_identities FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION master_mono_invoice_valid(b master_mono_bindings, p jsonb) RETURNS boolean LANGUAGE SQL STABLE AS $$
      SELECT COALESCE(p->'verified'='true'::jsonb AND p->>'status'='success'
        AND p->>'invoice_id'=b.invoice_id AND p->>'reference'=b.reference AND p->>'profile_id'=b.profile_id
        AND p->>'merchant_id'=b.profile::jsonb->>'merchant_id' AND p->>'authority'='test' AND p->>'transport'='mock_http'
        AND p->>'currency'='UAH' AND p->'amount_minor'=to_jsonb(b.amount_minor)
        AND (p->>'created_at')::timestamptz>=b.created_at-interval '30 seconds'
        AND (p->>'accepted_at')::timestamptz>=(p->>'created_at')::timestamptz
        AND (p->>'accepted_at')::timestamptz<=now()+interval '30 seconds'
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex'),false)
    $$""")
    op.execute("""CREATE FUNCTION master_mono_check_valid(b master_mono_bindings, p jsonb, receipt varchar) RETURNS boolean LANGUAGE SQL IMMUTABLE AS $$
      SELECT COALESCE(p->'verified'='true'::jsonb AND p->>'verification_scope'='provider_invoice_association'
        AND p->>'invoice_id'=b.invoice_id AND p->>'profile_id'=b.profile_id
        AND p->>'merchant_id'=b.profile::jsonb->>'merchant_id' AND p->>'authority'='test' AND p->>'transport'='mock_http'
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex')
        AND jsonb_array_length(p->'checks')=1 AND p->'checks'->0->>'id'=receipt
        AND p->'checks'->0->>'type'='sale' AND p->'checks'->0->>'status'='done'
        AND p->'checks'->0->>'fiscalizationSource'='checkbox',false)
    $$""")
    op.execute("""CREATE FUNCTION validate_master_mono_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE b master_mono_bindings%ROWTYPE; p order_payments%ROWTYPE; r master_mono_receipts%ROWTYPE;
    BEGIN
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF TG_TABLE_NAME='master_mono_bindings' THEN
        IF NOT EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND channel='backoffice_pos' AND simulation
          AND created_by_user_id=NEW.operator_id AND total_amount*100=NEW.amount_minor AND status::text IN ('pending','confirmed'))
          OR NEW.authority!='test' OR NEW.transport!='mock_http' OR NEW.amount_minor<=0
          OR NEW.profile::jsonb->'external_fiscalization_disabled' IS DISTINCT FROM 'false'::jsonb
          OR NEW.profile::jsonb->>'environment' IS DISTINCT FROM 'test'
          OR NEW.profile_id IS DISTINCT FROM NEW.profile::jsonb->>'id'
          OR NEW.profile_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.profile::jsonb),'UTF8')),'hex')
          OR EXISTS(SELECT 1 FROM qr_payment_attempts WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM order_payments WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM order_handovers WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM order_shipments WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM fiscal_documents WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM mock_fiscal_automations WHERE order_id=NEW.order_id)
          OR EXISTS(SELECT 1 FROM shop_payment_intents WHERE order_id=NEW.order_id)
        THEN RAISE EXCEPTION 'Master mono binding requires exclusive simulated owner checkout'; END IF;
        RETURN NEW;
      END IF;
      SELECT * INTO b FROM master_mono_bindings WHERE order_id=NEW.order_id;
      IF b.id IS NULL OR NEW.proof_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.proof::jsonb),'UTF8')),'hex')
      THEN RAISE EXCEPTION 'Master mono fact requires exact immutable binding and proof'; END IF;
      IF TG_TABLE_NAME='master_mono_artifacts' THEN
        SELECT * INTO r FROM master_mono_receipts WHERE id=NEW.receipt_id AND order_id=NEW.order_id;
        IF r.id IS NULL OR NOT master_mono_check_valid(b,NEW.proof::jsonb,r.receipt_id)
        THEN RAISE EXCEPTION 'Master mono PDF requires exact accepted external receipt'; END IF;
        RETURN NEW;
      END IF;
      IF TG_TABLE_NAME='master_mono_observations' THEN
        IF NEW.status='accepted' THEN
          SELECT * INTO r FROM master_mono_receipts WHERE order_id=NEW.order_id;
          IF r.id IS NULL OR NOT master_mono_invoice_valid(b,NEW.proof::jsonb->'payment')
            OR NOT master_mono_check_valid(b,NEW.proof::jsonb->'fiscal',r.receipt_id)
          THEN RAISE EXCEPTION 'Master mono accepted observation must prove exact payment and receipt'; END IF;
        END IF;
        RETURN NEW;
      END IF;
      SELECT * INTO p FROM order_payments WHERE id=NEW.payment_id;
      IF NEW.binding_id IS DISTINCT FROM b.id OR p.order_id IS DISTINCT FROM b.order_id
        OR p.operator_id IS DISTINCT FROM b.operator_id OR p.amount_minor IS DISTINCT FROM b.amount_minor
        OR NOT p.simulation OR p.source IS DISTINCT FROM 'master_mono_provider' OR p.method IS DISTINCT FROM 'card'
        OR p.evidence IS DISTINCT FROM b.invoice_id
      THEN RAISE EXCEPTION 'Master mono payment must match exact binding'; END IF;
      IF TG_TABLE_NAME='master_mono_payments' THEN
        IF NOT master_mono_invoice_valid(b,NEW.proof::jsonb)
        THEN RAISE EXCEPTION 'Master mono invoice proof invalid'; END IF;
      ELSE
        IF NOT EXISTS(SELECT 1 FROM master_mono_payments WHERE binding_id=b.id AND payment_id=p.id)
          OR NOT master_mono_check_valid(b,NEW.proof::jsonb,NEW.receipt_id)
          OR NEW.receipt_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN RAISE EXCEPTION 'Master mono receipt requires exact verified payment and external check'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    for table in ("master_mono_bindings", "master_mono_payments", "master_mono_receipts", "master_mono_observations", "master_mono_artifacts"):
        op.execute(f"CREATE TRIGGER validate_master_mono_fact BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION validate_master_mono_fact()")
    op.execute("""CREATE FUNCTION guard_master_mono_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE oid integer; b master_mono_bindings%ROWTYPE; latest master_mono_observations%ROWTYPE; r master_mono_receipts%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME='orders' THEN oid=NEW.id;
      ELSIF TG_OP='DELETE' THEN oid=OLD.order_id;
      ELSE oid=NEW.order_id; END IF;
      IF TG_TABLE_NAME='order_items' AND TG_OP='UPDATE' THEN
        IF EXISTS(SELECT 1 FROM master_mono_bindings WHERE order_id=OLD.order_id) THEN
          RAISE EXCEPTION 'Master mono owns original checkout items';
        END IF;
      END IF;
      PERFORM 1 FROM orders WHERE id=oid FOR UPDATE;
      SELECT * INTO b FROM master_mono_bindings WHERE order_id=oid;
      IF b.id IS NULL THEN
        IF TG_TABLE_NAME='order_payments' THEN
          IF NEW.source='master_mono_provider' THEN RAISE EXCEPTION 'Master mono payment requires binding'; END IF;
        END IF;
        RETURN COALESCE(NEW,OLD);
      END IF;
      IF TG_TABLE_NAME='orders' THEN
        IF NEW.total_amount IS DISTINCT FROM OLD.total_amount OR NEW.subtotal_amount IS DISTINCT FROM OLD.subtotal_amount
          OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount OR NEW.channel IS DISTINCT FROM OLD.channel
          OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
          OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method
          OR NEW.status IS DISTINCT FROM OLD.status AND NEW.status::text!='completed'
        THEN RAISE EXCEPTION 'Master mono owns immutable checkout'; END IF;
        IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
      ELSIF TG_TABLE_NAME='order_payments' THEN
        IF NEW.source!='master_mono_provider' OR NEW.method!='card' OR NEW.evidence!=b.invoice_id
          OR NEW.amount_minor!=b.amount_minor OR NOT NEW.simulation OR NEW.operator_id!=b.operator_id
        THEN RAISE EXCEPTION 'Master mono owns exact payment'; END IF;
        RETURN NEW;
      ELSIF TG_TABLE_NAME!='order_handovers' THEN
        RAISE EXCEPTION 'Master mono owns checkout; another issuer or mutation is forbidden';
      END IF;
      IF TG_TABLE_NAME='order_handovers' THEN
        IF NEW.operator_id IS DISTINCT FROM b.operator_id OR NOT NEW.simulation THEN RAISE EXCEPTION 'Master mono handover requires exact owner and simulation'; END IF;
      END IF;
      SELECT * INTO latest FROM master_mono_observations WHERE order_id=oid ORDER BY id DESC LIMIT 1;
      SELECT * INTO r FROM master_mono_receipts WHERE order_id=oid;
      IF r.id IS NULL OR latest.status IS DISTINCT FROM 'accepted'
        OR NOT master_mono_invoice_valid(b,latest.proof::jsonb->'payment')
        OR NOT master_mono_check_valid(b,latest.proof::jsonb->'fiscal',r.receipt_id)
      THEN RAISE EXCEPTION 'Master mono handover requires current accepted payment and receipt'; END IF;
      IF b.profile_id='local-master-mono' AND NOT EXISTS(SELECT 1 FROM master_mono_mock_truth
        WHERE binding_id=b.id AND payment_status='success' AND receipt_status='done' AND receipt_id=r.receipt_id)
      THEN RAISE EXCEPTION 'Synthetic provider reversal blocks handover immediately'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER guard_master_mono_owner BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION guard_master_mono_owner()")
    op.execute("CREATE TRIGGER guard_master_mono_owner BEFORE INSERT OR UPDATE OR DELETE ON order_items FOR EACH ROW EXECUTE FUNCTION guard_master_mono_owner()")
    for table in ("order_payments", "order_handovers", "qr_payment_attempts", "fiscal_documents", "mock_fiscal_automations", "shop_fiscal_documents", "shop_checkbox_bindings", "shop_checkbox_operations"):
        op.execute(f"CREATE TRIGGER aaa_guard_master_mono_owner BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION guard_master_mono_owner()")
    op.execute("""CREATE FUNCTION require_master_mono_payment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.source='master_mono_provider' AND NOT EXISTS(SELECT 1 FROM master_mono_payments WHERE payment_id=NEW.id)
      THEN RAISE EXCEPTION 'Master mono payment requires immutable verified proof'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER require_master_mono_payment AFTER INSERT ON order_payments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_master_mono_payment()")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM master_mono_bindings)
      THEN RAISE EXCEPTION 'Preserve master mono financial history; populated ledger cannot downgrade'; END IF; END $$""")
    op.execute("DROP TRIGGER require_master_mono_payment ON order_payments")
    op.execute("DROP FUNCTION require_master_mono_payment()")
    for table in ("order_payments", "order_handovers", "qr_payment_attempts", "fiscal_documents", "mock_fiscal_automations", "shop_fiscal_documents", "shop_checkbox_bindings", "shop_checkbox_operations"):
        op.execute(f"DROP TRIGGER aaa_guard_master_mono_owner ON {table}")
    for table in ("orders", "order_items"):
        op.execute(f"DROP TRIGGER guard_master_mono_owner ON {table}")
    op.execute("DROP FUNCTION guard_master_mono_owner()")
    for table in ("master_mono_bindings", "master_mono_payments", "master_mono_receipts", "master_mono_observations", "master_mono_artifacts"):
        op.execute(f"DROP TRIGGER validate_master_mono_fact ON {table}")
    op.execute("DROP FUNCTION validate_master_mono_fact()")
    for table in ("master_mono_receipts", "shop_mono_checkbox_receipts"):
        op.execute(f"DROP TRIGGER claim_mono_external_identity ON {table}")
    op.execute("DROP FUNCTION claim_mono_external_identity()")
    op.execute("DROP TABLE mono_external_receipt_identities")
    op.execute("DROP FUNCTION master_mono_check_valid(master_mono_bindings,jsonb,varchar)")
    op.execute("DROP FUNCTION master_mono_invoice_valid(master_mono_bindings,jsonb)")
    op.execute("DROP TRIGGER protect_master_mono_truth ON master_mono_mock_truth")
    op.execute("DROP FUNCTION protect_master_mono_truth()")
    for table in ("master_mono_artifacts", "master_mono_observations", "master_mono_receipts", "master_mono_payments", "master_mono_mock_truth", "master_mono_bindings"):
        op.drop_table(table)
