"""Live master mono dispatch and immutable checkout ownership; preserve old simulations."""
from alembic import op
import sqlalchemy as sa

revision = "0133_master_mono_live"
down_revision = "0132_mono_checkbox_poll"
branch_labels = None
depends_on = None

LEGACY_FUNCTIONS = [
    """CREATE FUNCTION master_mono_invoice_valid(b master_mono_bindings, p jsonb) RETURNS boolean LANGUAGE SQL STABLE AS $$
      SELECT COALESCE(p->'verified'='true'::jsonb AND p->>'status'='success'
        AND p->>'invoice_id'=b.invoice_id AND p->>'reference'=b.reference AND p->>'profile_id'=b.profile_id
        AND p->>'merchant_id'=b.profile::jsonb->>'merchant_id' AND p->>'authority'='test' AND p->>'transport'='mock_http'
        AND p->>'currency'='UAH' AND p->'amount_minor'=to_jsonb(b.amount_minor)
        AND (p->>'created_at')::timestamptz>=b.created_at-interval '30 seconds'
        AND (p->>'accepted_at')::timestamptz>=(p->>'created_at')::timestamptz
        AND (p->>'accepted_at')::timestamptz<=now()+interval '30 seconds'
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex'),false)
    $$""",
    """CREATE FUNCTION master_mono_check_valid(b master_mono_bindings, p jsonb, receipt varchar) RETURNS boolean LANGUAGE SQL IMMUTABLE AS $$
      SELECT COALESCE(p->'verified'='true'::jsonb AND p->>'verification_scope'='provider_invoice_association'
        AND p->>'invoice_id'=b.invoice_id AND p->>'profile_id'=b.profile_id
        AND p->>'merchant_id'=b.profile::jsonb->>'merchant_id' AND p->>'authority'='test' AND p->>'transport'='mock_http'
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex')
        AND jsonb_array_length(p->'checks')=1 AND p->'checks'->0->>'id'=receipt
        AND p->'checks'->0->>'type'='sale' AND p->'checks'->0->>'status'='done'
        AND p->'checks'->0->>'fiscalizationSource'='checkbox',false)
    $$""",
    """CREATE FUNCTION validate_master_mono_fact() RETURNS trigger LANGUAGE plpgsql AS $$
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
    END $$""",
    """CREATE FUNCTION guard_master_mono_owner() RETURNS trigger LANGUAGE plpgsql AS $$
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
    END $$""",
]

def upgrade():
    op.create_table("master_mono_create_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reference", sa.String(36), nullable=False, unique=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        *[sa.Column(n, sa.String(64), nullable=False) for n in ("profile_id", "profile_hash", "basket_hash")],
        *[sa.Column(n, sa.JSON(), nullable=False) for n in ("profile", "basket", "policy")],
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("transport", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.add_column("master_mono_bindings", sa.Column("create_request_id", sa.Integer(), sa.ForeignKey("master_mono_create_requests.id", ondelete="RESTRICT"), nullable=True, unique=True))
    op.add_column("master_mono_bindings", sa.Column("page_url", sa.String(240), nullable=True))
    op.add_column("master_mono_bindings", sa.Column("create_proof", sa.JSON(), nullable=True))
    op.execute("CREATE TRIGGER immutable_master_dispatch BEFORE UPDATE OR DELETE ON master_mono_create_requests FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION validate_master_dispatch() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM order_items WHERE order_id=NEW.order_id ORDER BY id FOR UPDATE;
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF NOT EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND channel='backoffice_pos' AND NOT simulation
          AND created_by_user_id=NEW.operator_id AND total_amount*100=NEW.amount_minor AND status::text IN ('pending','confirmed'))
        OR NEW.amount_minor<=0 OR NEW.profile::jsonb->>'environment' IS DISTINCT FROM 'production'
        OR NEW.profile::jsonb->'external_fiscalization_disabled' IS DISTINCT FROM 'false'::jsonb
        OR NEW.profile_id IS DISTINCT FROM NEW.profile::jsonb->>'id'
        OR NEW.profile_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.profile::jsonb),'UTF8')),'hex')
        OR NOT ((NEW.authority='provider' AND NEW.transport='https') OR (NEW.authority='test' AND NEW.transport='mock_http'
          AND current_database() LIKE '%test%'))
        OR NEW.created_at < now()-interval '30 seconds' OR NEW.created_at > now()+interval '30 seconds'
        OR NEW.reference !~ '^[0-9a-f-]{36}$'
        OR jsonb_typeof(NEW.basket::jsonb) IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.basket::jsonb) NOT BETWEEN 1 AND 200
        OR NEW.basket_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.basket::jsonb),'UTF8')),'hex')
        OR (SELECT sum((l->>'total')::bigint) FROM jsonb_array_elements(NEW.basket::jsonb) l) IS DISTINCT FROM NEW.amount_minor
        OR NEW.policy::jsonb->>'unit' IS DISTINCT FROM 'шт.' OR COALESCE(NEW.policy::jsonb->>'id','')=''
        OR jsonb_typeof(NEW.policy::jsonb->'tax_codes') IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.policy::jsonb->'tax_codes')=0
        OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.basket::jsonb) l WHERE (l->>'qty')::int<=0 OR (l->>'sum')::bigint<=0
          OR (l->>'total')::bigint!=(l->>'qty')::int*(l->>'sum')::bigint OR l->>'unit' IS DISTINCT FROM 'шт.'
          OR l->'tax' IS DISTINCT FROM NEW.policy::jsonb->'tax_codes')
        OR NOT EXISTS(SELECT 1 FROM order_items WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM order_items WHERE order_id=NEW.order_id AND (quantity_from_stock!=quantity OR quantity_to_order!=0 OR quantity_received_for_order!=0))
        OR EXISTS(SELECT 1 FROM master_mono_bindings WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM order_payments WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM order_handovers WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM order_shipments WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM qr_payment_attempts WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM fiscal_documents WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM mock_fiscal_automations WHERE order_id=NEW.order_id)
        OR EXISTS(SELECT 1 FROM shop_payment_intents WHERE order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Master invoice dispatch requires exclusive live fully reserved fiscal basket'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_master_dispatch BEFORE INSERT ON master_mono_create_requests FOR EACH ROW EXECUTE FUNCTION validate_master_dispatch()")
    op.execute("""CREATE OR REPLACE FUNCTION master_mono_invoice_valid(b master_mono_bindings, p jsonb) RETURNS boolean LANGUAGE SQL STABLE AS $$
      SELECT COALESCE(p->'verified'='true'::jsonb AND p->>'status'='success'
        AND p->>'invoice_id'=b.invoice_id AND p->>'reference'=b.reference AND p->>'profile_id'=b.profile_id
        AND p->>'merchant_id'=b.profile::jsonb->>'merchant_id' AND p->>'authority'=b.authority AND p->>'transport'=b.transport
        AND p->>'currency'='UAH' AND p->'amount_minor'=to_jsonb(b.amount_minor)
        AND (p->>'created_at')::timestamptz>=b.created_at-interval '30 seconds'
        AND (p->>'accepted_at')::timestamptz>=(p->>'created_at')::timestamptz
        AND (p->>'accepted_at')::timestamptz<=now()+interval '30 seconds'
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex'),false)
    $$""")
    op.execute("""CREATE OR REPLACE FUNCTION master_mono_check_valid(b master_mono_bindings, p jsonb, receipt varchar) RETURNS boolean LANGUAGE SQL IMMUTABLE AS $$
      SELECT COALESCE(p->'verified'='true'::jsonb AND p->>'verification_scope'='provider_invoice_association'
        AND p->>'invoice_id'=b.invoice_id AND p->>'profile_id'=b.profile_id
        AND p->>'merchant_id'=b.profile::jsonb->>'merchant_id' AND p->>'authority'=b.authority AND p->>'transport'=b.transport
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex')
        AND jsonb_array_length(p->'checks')=1 AND p->'checks'->0->>'id'=receipt
        AND p->'checks'->0->>'type'='sale' AND p->'checks'->0->>'status'='done'
        AND p->'checks'->0->>'fiscalizationSource'='checkbox',false)
    $$""")
    op.execute("""CREATE OR REPLACE FUNCTION validate_master_mono_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q master_mono_create_requests%ROWTYPE; b master_mono_bindings%ROWTYPE; p order_payments%ROWTYPE; r master_mono_receipts%ROWTYPE;
    BEGIN
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF TG_TABLE_NAME='master_mono_bindings' THEN
        SELECT * INTO q FROM master_mono_create_requests WHERE id=NEW.create_request_id;
        IF q.id IS NULL OR NEW.order_id IS DISTINCT FROM q.order_id OR NEW.operator_id IS DISTINCT FROM q.operator_id
          OR NEW.reference IS DISTINCT FROM q.reference OR NEW.amount_minor IS DISTINCT FROM q.amount_minor
          OR NEW.profile_id IS DISTINCT FROM q.profile_id OR NEW.profile_hash IS DISTINCT FROM q.profile_hash
          OR NEW.profile::jsonb IS DISTINCT FROM q.profile::jsonb OR NEW.authority IS DISTINCT FROM q.authority
          OR NEW.transport IS DISTINCT FROM q.transport OR NEW.created_at IS DISTINCT FROM q.created_at
          OR NEW.invoice_id !~ '^[A-Za-z0-9_-]{1,100}$'
          OR NEW.page_url IS DISTINCT FROM 'https://pay.mbnk.biz/'||NEW.invoice_id
          OR NEW.create_proof::jsonb->'verified' IS DISTINCT FROM 'true'::jsonb
          OR NEW.create_proof::jsonb->>'invoice_id' IS DISTINCT FROM NEW.invoice_id
          OR NEW.create_proof::jsonb->>'reference' IS DISTINCT FROM q.reference
          OR NEW.create_proof::jsonb->'amount_minor' IS DISTINCT FROM to_jsonb(q.amount_minor)
          OR NEW.create_proof::jsonb->>'currency' IS DISTINCT FROM 'UAH'
          OR NEW.create_proof::jsonb->>'profile_id' IS DISTINCT FROM q.profile_id
          OR NEW.create_proof::jsonb->>'merchant_id' IS DISTINCT FROM q.profile::jsonb->>'merchant_id'
          OR NEW.create_proof::jsonb->>'authority' IS DISTINCT FROM q.authority
          OR NEW.create_proof::jsonb->>'transport' IS DISTINCT FROM q.transport
          OR NEW.create_proof::jsonb->>'proof_hash' IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.create_proof::jsonb-'proof_hash'),'UTF8')),'hex')
          OR NOT EXISTS(SELECT 1 FROM orders WHERE id=q.order_id AND NOT simulation AND channel='backoffice_pos'
              AND created_by_user_id=q.operator_id AND total_amount*100=q.amount_minor AND status::text IN ('pending','confirmed'))
        THEN RAISE EXCEPTION 'Live master invoice requires exact immutable dispatch and acknowledgement'; END IF;
        IF NEW.create_proof::jsonb ? 'observed_at' THEN
          IF (NEW.create_proof::jsonb->>'created_at')::timestamptz < q.created_at-interval '30 seconds'
            OR (NEW.create_proof::jsonb->>'created_at')::timestamptz > q.created_at+interval '15 minutes'
            OR (NEW.create_proof::jsonb->>'observed_at')::timestamptz > now()+interval '30 seconds'
          THEN RAISE EXCEPTION 'Recovered invoice creation time must match dispatch'; END IF;
        ELSIF NEW.create_proof::jsonb->>'page_url' IS DISTINCT FROM NEW.page_url THEN
          RAISE EXCEPTION 'Invoice page must be the exact validated bank URL';
        END IF;
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
        OR p.simulation IS DISTINCT FROM (SELECT simulation FROM orders WHERE id=b.order_id) OR p.source IS DISTINCT FROM 'master_mono_provider' OR p.method IS DISTINCT FROM 'card'
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
    op.execute("""CREATE OR REPLACE FUNCTION guard_master_mono_owner() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q master_mono_create_requests%ROWTYPE; oid integer; b master_mono_bindings%ROWTYPE; latest master_mono_observations%ROWTYPE; r master_mono_receipts%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME='orders' THEN oid=NEW.id;
      ELSIF TG_OP='DELETE' THEN oid=OLD.order_id;
      ELSE oid=NEW.order_id; END IF;
      IF TG_TABLE_NAME='order_items' AND TG_OP='UPDATE' THEN
        IF EXISTS(SELECT 1 FROM master_mono_bindings WHERE order_id=OLD.order_id) OR EXISTS(SELECT 1 FROM master_mono_create_requests WHERE order_id=OLD.order_id) THEN
          RAISE EXCEPTION 'Master mono owns original checkout items';
        END IF;
      END IF;
      PERFORM 1 FROM orders WHERE id=oid FOR UPDATE;
      SELECT * INTO b FROM master_mono_bindings WHERE order_id=oid;
      SELECT * INTO q FROM master_mono_create_requests WHERE order_id=oid;
      IF b.id IS NULL AND q.id IS NULL THEN
        IF TG_TABLE_NAME='order_payments' THEN
          IF NEW.source='master_mono_provider' THEN
            RAISE EXCEPTION 'Master mono payment requires binding';
          END IF;
        END IF;
        RETURN COALESCE(NEW,OLD);
      END IF;
      IF b.id IS NULL AND TG_TABLE_NAME!='orders' THEN
        RAISE EXCEPTION 'Durable master dispatch owns checkout even when provider outcome is unknown';
      END IF;
      IF TG_TABLE_NAME='orders' THEN
        IF NEW.total_amount IS DISTINCT FROM OLD.total_amount OR NEW.subtotal_amount IS DISTINCT FROM OLD.subtotal_amount
          OR NEW.discount_amount IS DISTINCT FROM OLD.discount_amount OR NEW.channel IS DISTINCT FROM OLD.channel
          OR NEW.simulation IS DISTINCT FROM OLD.simulation OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
          OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method
          OR NEW.payment_method IS DISTINCT FROM OLD.payment_method AND (b.id IS NULL OR NEW.payment_method IS DISTINCT FROM 'card')
          OR NEW.status IS DISTINCT FROM OLD.status AND NEW.status::text!='completed'
        THEN RAISE EXCEPTION 'Master mono owns immutable checkout'; END IF;
        IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
      ELSIF TG_TABLE_NAME='order_payments' THEN
        IF NEW.source!='master_mono_provider' OR NEW.method!='card' OR NEW.evidence!=b.invoice_id
          OR NEW.amount_minor!=b.amount_minor OR NEW.simulation IS DISTINCT FROM (SELECT simulation FROM orders WHERE id=oid) OR NEW.operator_id!=b.operator_id
        THEN RAISE EXCEPTION 'Master mono owns exact payment'; END IF;
        RETURN NEW;
      ELSIF TG_TABLE_NAME!='order_handovers' THEN
        RAISE EXCEPTION 'Master mono owns checkout; another issuer or mutation is forbidden';
      END IF;
      IF TG_TABLE_NAME='order_handovers' THEN
        IF NEW.operator_id IS DISTINCT FROM b.operator_id OR NEW.simulation IS DISTINCT FROM (SELECT simulation FROM orders WHERE id=oid) THEN RAISE EXCEPTION 'Master mono handover requires exact owner and simulation'; END IF;
      END IF;
      IF b.id IS NULL THEN RAISE EXCEPTION 'Unknown dispatch cannot hand over'; END IF;
      SELECT * INTO latest FROM master_mono_observations WHERE order_id=oid ORDER BY id DESC LIMIT 1;
      SELECT * INTO r FROM master_mono_receipts WHERE order_id=oid;
      IF r.id IS NULL OR latest.status IS DISTINCT FROM 'accepted'
        OR latest.created_at<now()-interval '30 seconds'
        OR NOT master_mono_invoice_valid(b,latest.proof::jsonb->'payment')
        OR NOT master_mono_check_valid(b,latest.proof::jsonb->'fiscal',r.receipt_id)
      THEN RAISE EXCEPTION 'Master mono handover requires current accepted payment and receipt'; END IF;
      IF b.profile_id='local-master-mono' AND NOT EXISTS(SELECT 1 FROM master_mono_mock_truth
        WHERE binding_id=b.id AND payment_status='success' AND receipt_status='done' AND receipt_id=r.receipt_id)
      THEN RAISE EXCEPTION 'Synthetic provider reversal blocks handover immediately'; END IF;
      RETURN NEW;
    END $$""")
    for table in ("order_shipments", "shop_payment_intents", "shop_payment_confirmations"):
        op.execute(f"CREATE TRIGGER aaa_guard_master_dispatch BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION guard_master_mono_owner()")
    op.execute("""CREATE FUNCTION protect_master_dispatch_stock() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q master_mono_create_requests%ROWTYPE; b master_mono_bindings%ROWTYPE; latest master_mono_observations%ROWTYPE; r master_mono_receipts%ROWTYPE;
    BEGIN
      SELECT * INTO q FROM master_mono_create_requests WHERE order_id=NEW.order_id;
      IF q.id IS NULL THEN RETURN NEW; END IF;
      SELECT * INTO b FROM master_mono_bindings WHERE order_id=q.order_id;
      SELECT * INTO latest FROM master_mono_observations WHERE order_id=q.order_id ORDER BY id DESC LIMIT 1;
      SELECT * INTO r FROM master_mono_receipts WHERE order_id=q.order_id;
      IF b.id IS NULL OR r.id IS NULL OR latest.status IS DISTINCT FROM 'accepted' OR latest.created_at<now()-interval '30 seconds'
        OR NOT master_mono_invoice_valid(b,latest.proof::jsonb->'payment') OR NOT master_mono_check_valid(b,latest.proof::jsonb->'fiscal',r.receipt_id)
        OR NEW.movement_type::text!='shipment' OR NEW.admin_user_id IS DISTINCT FROM q.operator_id
        OR NEW.on_hand_delta!= -NEW.quantity OR NEW.reserved_delta!= -NEW.quantity
        OR NOT EXISTS(SELECT 1 FROM order_items i WHERE i.id=NEW.order_item_id AND i.order_id=q.order_id
          AND i.product_id=NEW.product_id AND i.quantity=NEW.quantity
          AND NEW.reference_key='order:'||q.order_id::text||':item:'||i.id::text||chr(58)||'shipment')
      THEN RAISE EXCEPTION 'Master dispatch freezes reserves until exact owned paid handover'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_master_dispatch_stock BEFORE INSERT ON inventory_movements FOR EACH ROW EXECUTE FUNCTION protect_master_dispatch_stock()")
    op.execute("""CREATE FUNCTION require_master_live_handover() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE oid integer; q master_mono_create_requests%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME='orders' THEN oid=NEW.id; ELSE oid=NEW.order_id; END IF;
      SELECT * INTO q FROM master_mono_create_requests WHERE order_id=oid;
      IF q.id IS NULL THEN RETURN NEW; END IF;
      IF TG_TABLE_NAME='orders' THEN
        IF NEW.status::text!='completed' THEN RETURN NEW; END IF;
      END IF;
      IF NOT EXISTS(SELECT 1 FROM orders WHERE id=oid AND status::text='completed')
        OR NOT EXISTS(SELECT 1 FROM order_handovers WHERE order_id=oid AND operator_id=q.operator_id AND NOT simulation)
        OR EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=oid AND
          (SELECT count(*) FROM inventory_movements m WHERE m.order_id=oid AND m.order_item_id=i.id
           AND m.product_id=i.product_id AND m.movement_type::text='shipment' AND m.quantity=i.quantity
           AND m.on_hand_delta= -i.quantity AND m.reserved_delta= -i.quantity AND m.admin_user_id=q.operator_id
           AND m.reference_key='order:'||oid::text||':item:'||i.id::text||chr(58)||'shipment')!=1)
      THEN RAISE EXCEPTION 'Live master completion requires its exact once-only owner handover and stock movements'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER require_master_live_handover AFTER UPDATE ON orders DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_master_live_handover()")
    for table in ("order_handovers", "inventory_movements"):
        op.execute(f"CREATE CONSTRAINT TRIGGER require_master_live_handover AFTER INSERT ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_master_live_handover()")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM master_mono_create_requests)
      THEN RAISE EXCEPTION 'Preserve live master dispatch history; populated ledger cannot downgrade'; END IF; END $$""")
    for table in ("orders", "order_handovers", "inventory_movements"):
        op.execute(f"DROP TRIGGER require_master_live_handover ON {table}")
    op.execute("DROP FUNCTION require_master_live_handover()")
    op.execute("DROP TRIGGER protect_master_dispatch_stock ON inventory_movements")
    op.execute("DROP FUNCTION protect_master_dispatch_stock()")
    for table in ("order_shipments", "shop_payment_intents", "shop_payment_confirmations"):
        op.execute(f"DROP TRIGGER aaa_guard_master_dispatch ON {table}")
    for sql in LEGACY_FUNCTIONS:
        op.execute(sql.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    op.execute("DROP TRIGGER validate_master_dispatch ON master_mono_create_requests")
    op.execute("DROP FUNCTION validate_master_dispatch()")
    op.drop_column("master_mono_bindings", "create_proof")
    op.drop_column("master_mono_bindings", "page_url")
    op.drop_column("master_mono_bindings", "create_request_id")
    op.drop_table("master_mono_create_requests")
