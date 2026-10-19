"""Pinned Shop invoice candidates and structured COD provenance; no provider writes."""
from alembic import op
import sqlalchemy as sa

revision = "0126_shop_payments"
down_revision = "0125_shop_checkbox"
branch_labels = None
depends_on = None


def fk(name, target, **kw):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(target + ".id", ondelete="RESTRICT"), nullable=False, **kw)


def upgrade():
    op.create_table("shop_payment_intents", sa.Column("id", sa.String(36), primary_key=True),
        fk("order_id", "orders", unique=True),sa.Column("mode",sa.String(16),nullable=False),
        sa.Column("reference",sa.String(36),nullable=False,unique=True),sa.Column("amount_minor",sa.Integer(),nullable=False),
        sa.Column("profile_id",sa.String(64),nullable=False),sa.Column("profile",sa.JSON(),nullable=False),
        sa.Column("profile_hash",sa.String(64),nullable=False),sa.Column("invoice_id",sa.String(128),nullable=True),
        sa.Column("authority",sa.String(16),nullable=False),sa.Column("transport",sa.String(16),nullable=False),
        fk("operator_id","admin_users"),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint("profile_id","invoice_id",name="uq_shop_invoice_binding"))
    op.create_table("shop_payment_evidence",sa.Column("id",sa.Integer(),primary_key=True),
        sa.Column("intent_id",sa.String(36),sa.ForeignKey("shop_payment_intents.id",ondelete="RESTRICT"),nullable=False,unique=True),
        fk("order_id","orders",unique=True),fk("payment_id","order_payments",unique=True),
        sa.Column("identity",sa.String(64),nullable=False,unique=True),sa.Column("authority",sa.String(16),nullable=False),
        sa.Column("proof",sa.JSON(),nullable=False),sa.Column("proof_canonical",sa.Text(),nullable=False),
        sa.Column("proof_hash",sa.String(64),nullable=False),fk("operator_id","admin_users"),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))
    op.execute("""CREATE FUNCTION shop_payment_canonical(j jsonb) RETURNS text LANGUAGE SQL IMMUTABLE STRICT AS $$
      SELECT CASE jsonb_typeof(j)
      WHEN 'object' THEN '{'||COALESCE((SELECT string_agg(to_jsonb(key)::text||':'||shop_payment_canonical(value),',' ORDER BY key) FROM jsonb_each(j)),'')||'}'
      WHEN 'array' THEN '['||COALESCE((SELECT string_agg(shop_payment_canonical(value),',' ORDER BY ord) FROM jsonb_array_elements(j) WITH ORDINALITY a(value,ord)),'')||']'
      ELSE j::text END
    $$""")
    op.execute("""CREATE FUNCTION protect_shop_payment_intent() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='UPDATE' THEN
        IF (to_jsonb(NEW)-'invoice_id') IS DISTINCT FROM (to_jsonb(OLD)-'invoice_id')
          OR OLD.invoice_id IS NOT NULL AND NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
          OR NEW.invoice_id IS NULL THEN RAISE EXCEPTION 'Payment intent and first invoice candidate are immutable'; END IF;
      ELSE
        PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
        IF NOT EXISTS(SELECT 1 FROM orders o WHERE o.id=NEW.order_id AND o.channel='shop' AND NOT o.simulation
          AND o.fulfillment_method IN ('pickup','nova_poshta') AND o.total_amount>0 AND NEW.amount_minor=o.total_amount*100
          AND o.status::text NOT IN ('cancelled','completed')
          AND NOT EXISTS(SELECT 1 FROM order_payments WHERE order_id=o.id)
          AND NOT EXISTS(SELECT 1 FROM qr_payment_attempts WHERE order_id=o.id))
          OR NEW.mode NOT IN ('prepayment','cod') OR NEW.profile::jsonb->>'id' IS DISTINCT FROM NEW.profile_id
          OR NEW.reference !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
          OR NEW.profile_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.profile::jsonb),'UTF8')),'hex')
          OR NEW.profile::jsonb->>'environment' IS NULL OR NEW.profile::jsonb->>'environment' NOT IN ('test','production')
          OR NEW.mode='prepayment' AND (NEW.authority NOT IN ('test','provider') OR NEW.transport IS DISTINCT FROM (CASE WHEN NEW.authority='test' THEN 'mock_http' ELSE 'https' END) OR COALESCE(NEW.profile::jsonb->>'merchant_id','')='' OR NEW.profile::jsonb->'external_fiscalization_disabled' IS DISTINCT FROM 'true'::jsonb
            OR NEW.authority='provider' AND NEW.profile::jsonb->>'environment' IS DISTINCT FROM 'production')
          OR NEW.mode='cod' AND (NEW.authority!='manual' OR NEW.transport!='manual' OR COALESCE(NEW.profile::jsonb->>'issuer','')='' OR COALESCE(NEW.profile::jsonb->>'account','')='')
        THEN RAISE EXCEPTION 'Payment intent requires an exact new canonical Shop binding'; END IF;
      END IF;
      IF NEW.invoice_id IS NOT NULL AND (NEW.mode!='prepayment' OR NEW.invoice_id !~ '^[A-Za-z0-9_-]{1,100}$')
      THEN RAISE EXCEPTION 'Invalid invoice candidate'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_payment_intent BEFORE INSERT OR UPDATE ON shop_payment_intents FOR EACH ROW EXECUTE FUNCTION protect_shop_payment_intent()")
    op.execute("CREATE TRIGGER immutable_shop_payment_intent BEFORE DELETE ON shop_payment_intents FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("CREATE TRIGGER immutable_shop_payment_evidence BEFORE UPDATE OR DELETE ON shop_payment_evidence FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION validate_shop_payment_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE b shop_payment_intents%ROWTYPE; p order_payments%ROWTYPE; s order_shipments%ROWTYPE;
    BEGIN
      SELECT * INTO b FROM shop_payment_intents WHERE id=NEW.intent_id FOR UPDATE;
      SELECT * INTO p FROM order_payments WHERE id=NEW.payment_id;
      IF (b.authority='test' OR b.profile::jsonb->>'environment'='test') AND EXISTS(
        SELECT 1 FROM shop_checkbox_bindings cb JOIN shop_checkbox_registers cr ON cr.id=cb.register_id
        WHERE cb.order_id=NEW.order_id AND (cb.authority IS DISTINCT FROM 'test' OR cr.authority IS DISTINCT FROM 'test'))
      THEN RAISE EXCEPTION 'R2 test payment context cannot enter a production Checkbox binding'; END IF;
      IF b.id IS NULL OR p.id IS NULL OR NEW.order_id IS DISTINCT FROM b.order_id OR p.order_id IS DISTINCT FROM b.order_id
        OR p.simulation OR p.amount_minor IS DISTINCT FROM b.amount_minor OR p.operator_id IS DISTINCT FROM NEW.operator_id
        OR NEW.proof::jsonb IS NULL OR jsonb_typeof(NEW.proof::jsonb) IS DISTINCT FROM 'object'
        OR NEW.proof_canonical::jsonb IS DISTINCT FROM NEW.proof::jsonb
        OR NEW.proof_hash IS DISTINCT FROM encode(sha256(convert_to(NEW.proof_canonical,'UTF8')),'hex')
        OR NEW.proof_canonical IS DISTINCT FROM shop_payment_canonical(NEW.proof::jsonb)
        OR NEW.proof::jsonb->>'currency' IS DISTINCT FROM 'UAH' OR NEW.proof::jsonb->'verified' IS DISTINCT FROM 'true'::jsonb
      THEN RAISE EXCEPTION 'Payment evidence lacks exact normalized provenance'; END IF;
      IF b.mode='prepayment' THEN
        IF b.invoice_id IS NULL OR NEW.authority IS DISTINCT FROM b.authority OR p.source!='shop_provider' OR p.method!='card'
          OR jsonb_typeof(NEW.proof::jsonb->'amount_minor') IS DISTINCT FROM 'number'
          OR COALESCE(NEW.proof::jsonb->>'amount_minor','') !~ '^[1-9][0-9]*$'
          OR NEW.proof::jsonb->>'authority' IS DISTINCT FROM NEW.authority
          OR NEW.proof::jsonb->>'status' IS DISTINCT FROM 'success'
          OR NEW.proof::jsonb->>'reference' IS DISTINCT FROM b.reference
          OR NEW.proof::jsonb->>'invoice_id' IS DISTINCT FROM b.invoice_id
          OR NEW.proof::jsonb->>'profile_id' IS DISTINCT FROM b.profile_id
          OR NEW.proof::jsonb->>'merchant_id' IS DISTINCT FROM b.profile::jsonb->>'merchant_id'
          OR (NEW.proof::jsonb->>'amount_minor')::bigint IS DISTINCT FROM b.amount_minor
          OR NEW.proof::jsonb->>'accepted_at' IS NULL
          OR NEW.proof::jsonb->>'created_at' IS NULL
          OR (NEW.proof::jsonb->>'created_at')::timestamptz<b.created_at-interval '30 seconds'
          OR (NEW.proof::jsonb->>'created_at')::timestamptz>(NEW.proof::jsonb->>'accepted_at')::timestamptz
          OR (NEW.proof::jsonb->>'accepted_at')::timestamptz<b.created_at-interval '30 seconds'
          OR (NEW.proof::jsonb->>'accepted_at')::timestamptz>now()+interval '30 seconds'
          OR NEW.proof::jsonb->>'transport' IS DISTINCT FROM (CASE WHEN NEW.authority='test' THEN 'mock_http' ELSE 'https' END)
          OR NEW.proof::jsonb->>'proof_hash' IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.proof::jsonb-'proof_hash'),'UTF8')),'hex')
          OR NEW.identity IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(jsonb_build_array(b.profile_id,b.invoice_id)),'UTF8')),'hex')
          OR p.evidence IS DISTINCT FROM b.invoice_id
        THEN RAISE EXCEPTION 'Invoice acceptance requires exact verified success proof'; END IF;
      ELSE
        SELECT ship.* INTO s FROM shop_fulfillment_facts f JOIN order_shipments ship ON ship.id=f.shipment_id
          WHERE f.order_id=b.order_id AND f.kind='nova_poshta';
        IF NEW.authority!='manual' OR p.source!='shop_manual' OR p.method!='cod' OR s.id IS NULL
          OR jsonb_typeof(NEW.proof::jsonb->'gross_amount_minor') IS DISTINCT FROM 'number'
          OR jsonb_typeof(NEW.proof::jsonb->'fee_minor') IS DISTINCT FROM 'number'
          OR jsonb_typeof(NEW.proof::jsonb->'net_amount_minor') IS DISTINCT FROM 'number'
          OR COALESCE(NEW.proof::jsonb->>'gross_amount_minor','') !~ '^[1-9][0-9]*$'
          OR COALESCE(NEW.proof::jsonb->>'fee_minor','') !~ '^(0|[1-9][0-9]*)$'
          OR COALESCE(NEW.proof::jsonb->>'net_amount_minor','') !~ '^(0|[1-9][0-9]*)$'
          OR NEW.proof::jsonb->>'issuer' IS DISTINCT FROM b.profile::jsonb->>'issuer'
          OR NEW.proof::jsonb->>'account' IS DISTINCT FROM b.profile::jsonb->>'account'
          OR NOT COALESCE((b.profile::jsonb->'types') ? (NEW.proof::jsonb->>'type'),false)
          OR COALESCE(btrim(NEW.proof::jsonb->>'source'),'')='' OR COALESCE(btrim(NEW.proof::jsonb->>'document_id'),'')=''
          OR COALESCE(btrim(NEW.proof::jsonb->>'line_id'),'')=''
          OR NEW.proof::jsonb->>'tracking_number' IS DISTINCT FROM s.tracking_number
          OR (NEW.proof::jsonb->>'gross_amount_minor')::bigint IS DISTINCT FROM b.amount_minor
          OR (NEW.proof::jsonb->>'fee_minor')::bigint IS NULL OR (NEW.proof::jsonb->>'net_amount_minor')::bigint IS NULL
          OR (NEW.proof::jsonb->>'fee_minor')::bigint<0 OR (NEW.proof::jsonb->>'net_amount_minor')::bigint<0
          OR (NEW.proof::jsonb->>'gross_amount_minor')::bigint IS DISTINCT FROM (NEW.proof::jsonb->>'fee_minor')::bigint+(NEW.proof::jsonb->>'net_amount_minor')::bigint
          OR NEW.proof::jsonb->>'settled_at' IS NULL OR (NEW.proof::jsonb->>'settled_at')::timestamptz<s.provider_event_at
          OR (NEW.proof::jsonb->>'settled_at')::timestamptz>now()+interval '30 seconds'
          OR s.provider_event_at IS NULL OR s.last_checked_at IS NULL
          OR s.last_attempt_status IS DISTINCT FROM 'success' OR s.tracking_status IS DISTINCT FROM 'delivered'
          OR s.last_checked_at<now()-interval '24 hours' OR s.last_checked_at>now()+interval '30 seconds'
          OR p.evidence IS DISTINCT FROM NEW.identity
          OR NEW.identity IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(jsonb_build_array(translate(NEW.proof::jsonb->>'issuer','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),translate(NEW.proof::jsonb->>'account','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),translate(NEW.proof::jsonb->>'document_id','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),translate(NEW.proof::jsonb->>'line_id','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))),'UTF8')),'hex')
        THEN RAISE EXCEPTION 'COD payment requires exact structured settlement provenance'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_shop_payment_evidence AFTER INSERT ON shop_payment_evidence DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_shop_payment_evidence()")
    op.execute("""CREATE FUNCTION require_shop_r2_payment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM shop_payment_intents WHERE order_id=NEW.order_id) THEN
        IF NOT EXISTS(SELECT 1 FROM shop_payment_evidence e JOIN shop_payment_intents b ON b.id=e.intent_id
          WHERE e.order_id=NEW.order_id AND e.payment_id=NEW.payment_id AND e.operator_id=NEW.operator_id
            AND NEW.snapshot::jsonb->'verification'->>'r2_evidence_id'=e.id::text
            AND NEW.snapshot::jsonb->'verification'->>'intent_id'=b.id
            AND NEW.snapshot::jsonb->'verification'->>'proof_hash'=e.proof_hash AND NEW.authority=e.authority
            AND NEW.namespace=CASE WHEN b.mode='cod' THEN 'shop_r2_cod' ELSE 'shop_r2_monobank:'||b.profile_id END)
        THEN RAISE EXCEPTION 'Bound R2 order requires canonical R2 evidence'; END IF;
      ELSIF NEW.namespace LIKE 'shop_r2_%' THEN RAISE EXCEPTION 'R2 payment binding is required'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER require_shop_r2_payment AFTER INSERT ON shop_payment_confirmations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_shop_r2_payment()")
    op.execute("""CREATE FUNCTION protect_shop_r2_order() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM shop_payment_intents WHERE order_id=OLD.id) AND (
        NEW.total_amount IS DISTINCT FROM OLD.total_amount OR NEW.subtotal_amount IS DISTINCT FROM OLD.subtotal_amount
        OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount OR NEW.channel IS DISTINCT FROM OLD.channel
        OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method
        OR NEW.payment_method IS DISTINCT FROM OLD.payment_method AND NOT EXISTS(SELECT 1 FROM shop_payment_evidence WHERE order_id=OLD.id)
        OR NEW.status::text='cancelled' AND OLD.status::text!='cancelled')
      THEN RAISE EXCEPTION 'Bound payment intent requires explicit financial reconciliation'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_r2_order BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION protect_shop_r2_order()")
    op.execute("""CREATE FUNCTION protect_shop_r2_item() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE oid integer;
    BEGIN
      oid=CASE WHEN TG_OP='INSERT' THEN NEW.order_id ELSE OLD.order_id END;
      PERFORM 1 FROM orders WHERE id=oid OR (TG_OP='UPDATE' AND id=NEW.order_id) ORDER BY id FOR UPDATE;
      IF EXISTS(SELECT 1 FROM shop_payment_intents WHERE order_id=oid OR (TG_OP='UPDATE' AND order_id=NEW.order_id)) AND
        (TG_OP!='UPDATE' OR NEW.order_id IS DISTINCT FROM OLD.order_id OR NEW.product_id IS DISTINCT FROM OLD.product_id
        OR NEW.quantity IS DISTINCT FROM OLD.quantity OR NEW.price IS DISTINCT FROM OLD.price
        OR NEW.total_price IS DISTINCT FROM OLD.total_price OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount)
      THEN RAISE EXCEPTION 'Payment intent order composition is immutable'; END IF;
      RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_r2_item BEFORE INSERT OR UPDATE OR DELETE ON order_items FOR EACH ROW EXECUTE FUNCTION protect_shop_r2_item()")
    op.execute("""CREATE FUNCTION prevent_r2_test_checkbox_promotion() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.order_id IS NOT NULL AND EXISTS(SELECT 1 FROM shop_payment_intents i WHERE i.order_id=NEW.order_id
        AND (i.authority='test' OR i.profile::jsonb->>'environment'='test'))
        AND (NEW.authority IS DISTINCT FROM 'test' OR EXISTS(SELECT 1 FROM shop_checkbox_registers r
          WHERE r.id=NEW.register_id AND r.authority IS DISTINCT FROM 'test'))
      THEN RAISE EXCEPTION 'R2 test payment context cannot become a production Checkbox operation'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER prevent_r2_test_checkbox_promotion BEFORE INSERT OR UPDATE ON shop_checkbox_operations FOR EACH ROW EXECUTE FUNCTION prevent_r2_test_checkbox_promotion()")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM shop_payment_intents)")).scalar():
        raise RuntimeError("Accepted Shop payment intent history exists; downgrade refused")
    for trigger, table in (("prevent_r2_test_checkbox_promotion","shop_checkbox_operations"),("protect_shop_r2_item","order_items"),("protect_shop_r2_order","orders"),
        ("require_shop_r2_payment","shop_payment_confirmations"),("validate_shop_payment_evidence","shop_payment_evidence"),
        ("immutable_shop_payment_evidence","shop_payment_evidence"),("immutable_shop_payment_intent","shop_payment_intents"),
        ("protect_shop_payment_intent","shop_payment_intents")):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for name in ("prevent_r2_test_checkbox_promotion","protect_shop_r2_item","protect_shop_r2_order","require_shop_r2_payment","validate_shop_payment_evidence","protect_shop_payment_intent"):
        op.execute(f"DROP FUNCTION {name}()")
    op.execute("DROP FUNCTION shop_payment_canonical(jsonb)")
    op.drop_table("shop_payment_evidence")
    op.drop_table("shop_payment_intents")
