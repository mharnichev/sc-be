"""Record an audited close of a verified expired unpaid POS invoice."""

from alembic import op
import sqlalchemy as sa


revision = "0150_master_mono_expired_close"
down_revision = "0149_master_mono_opaque_route"
branch_labels = None
depends_on = None

_OWNER_STATUS = "OR NEW.status IS DISTINCT FROM OLD.status AND NEW.status::text!='completed'"
_CLOSURE_STATUS = """ AND NOT (
            OLD.status::text IN ('pending','confirmed') AND NEW.status::text='cancelled'
            AND NEW.revision=OLD.revision+1 AND EXISTS (
              SELECT 1 FROM master_mono_expired_closures c
              WHERE c.order_id=OLD.id AND c.expected_revision=OLD.revision
            )
          )"""
_OWNER_RETURN = "IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;"
_CLOSURE_RETURN = _OWNER_RETURN.replace(" THEN", " OR NEW.status::text='cancelled' THEN")
_STOCK_START = "SELECT * INTO q FROM master_mono_create_requests WHERE order_id=NEW.order_id;"
_STOCK_CLOSURE = """
      IF NEW.movement_type::text='reservation_release' AND EXISTS (
        SELECT 1 FROM master_mono_expired_closures c JOIN order_items i ON i.order_id=c.order_id
        WHERE c.order_id=NEW.order_id AND NEW.order_item_id=i.id AND NEW.product_id=i.product_id
          AND NEW.admin_user_id=c.actor_id AND NEW.quantity=i.quantity AND NEW.on_hand_delta=0
          AND NEW.reserved_delta= -i.quantity
          AND NEW.reference_key='order:'||c.order_id::text||':item:'||i.id::text||chr(58)||'reservation-release'
      ) THEN RETURN NEW; END IF;"""
_HANDOVER_START = "IF q.id IS NULL THEN RETURN NEW; END IF;"
_HANDOVER_CLOSURE = """
      IF TG_TABLE_NAME='inventory_movements' THEN
        IF NEW.movement_type::text='reservation_release'
          AND EXISTS (SELECT 1 FROM master_mono_expired_closures WHERE order_id=oid)
        THEN RETURN NEW; END IF;
      END IF;"""


def _guards(reverse=False):
    replacements = {
        "guard_master_mono_owner": [(_OWNER_STATUS, _OWNER_STATUS + _CLOSURE_STATUS), (_OWNER_RETURN, _CLOSURE_RETURN)],
        "protect_master_dispatch_stock": [(_STOCK_START, _STOCK_START + _STOCK_CLOSURE)],
        "require_master_live_handover": [(_HANDOVER_START, _HANDOVER_START + _HANDOVER_CLOSURE)],
    }
    for name, edits in replacements.items():
        definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"), {"name": name + "()"})
        for before, after in edits:
            old, new = (after, before) if reverse else (before, after)
            if not definition or definition.count(old) != 1:
                raise RuntimeError(f"Unexpected POS ledger guard: {name}")
            definition = definition.replace(old, new, 1)
        op.execute(definition)


def upgrade():
    op.create_table(
        "master_mono_expired_closures",
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("master_mono_create_requests.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("operation_key", sa.String(64), nullable=False, unique=True),
        sa.Column("bank_evidence_id", sa.String(64), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("proof", sa.JSON(), nullable=False),
        sa.Column("fiscal_proof", sa.JSON(), nullable=False),
        sa.Column("fiscal_requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""
    CREATE FUNCTION validate_master_mono_expired_closure() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE sale orders%ROWTYPE; q master_mono_create_requests%ROWTYPE;
            p jsonb := NEW.proof::jsonb; f jsonb := NEW.fiscal_proof::jsonb;
    BEGIN
      SELECT * INTO sale FROM orders WHERE id=NEW.order_id FOR UPDATE;
      SELECT * INTO q FROM master_mono_create_requests WHERE id=NEW.request_id AND order_id=NEW.order_id;
      IF sale.id IS NULL OR q.id IS NULL OR sale.simulation OR sale.channel IS DISTINCT FROM 'backoffice_pos'
        OR sale.status::text NOT IN ('pending','confirmed') OR sale.revision IS DISTINCT FROM NEW.expected_revision
        OR sale.created_by_user_id IS DISTINCT FROM q.operator_id OR sale.total_amount*100 IS DISTINCT FROM q.amount_minor
        OR NEW.operation_key !~ '^[a-f0-9]{64}$'
        OR NOT EXISTS (SELECT 1 FROM admin_users a WHERE a.id=NEW.actor_id AND a.is_active AND a.is_superuser)
        OR EXISTS (SELECT 1 FROM master_mono_bindings WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM order_payments WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM order_handovers WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM order_shipments WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM qr_payment_attempts WHERE order_id=sale.id)
        OR EXISTS (SELECT 1 FROM fiscal_documents WHERE order_id=sale.id)
        OR (SELECT count(*) FROM order_items WHERE order_id=sale.id)<>1
        OR NOT EXISTS (SELECT 1 FROM order_items WHERE order_id=sale.id AND quantity=1
          AND quantity_from_stock=1 AND quantity_to_order=0 AND quantity_received_for_order=0)
        OR EXISTS (SELECT 1 FROM inventory_movements WHERE order_id=sale.id AND movement_type::text='reservation_release')
        OR (SELECT sum(m.reserved_delta) FROM inventory_movements m JOIN order_items i
            ON i.id=m.order_item_id AND i.product_id=m.product_id
            WHERE m.order_id=sale.id AND i.order_id=sale.id) IS DISTINCT FROM 1
      THEN RAISE EXCEPTION 'Expired POS closure requires the original unbound single-unit dispatch'; END IF;

      IF NOT COALESCE(
        p->'verified'='true'::jsonb AND p->>'status'='expired' AND p->'has_cancellations'='false'::jsonb
        AND p->'final_amount_minor' IN ('null'::jsonb,'0'::jsonb)
        AND p->>'invoice_id' ~ '^[A-Za-z0-9_-]{1,100}$'
        AND p->>'reference'=q.reference AND p->'amount_minor'=to_jsonb(q.amount_minor)
        AND p->>'currency'='UAH' AND p->'currency_code'='980'::jsonb
        AND p->>'merchant_id'=q.profile::jsonb->>'merchant_id' AND p->>'profile_id'=q.profile_id
        AND p->>'authority'=q.authority AND p->>'transport'=q.transport
        AND (p->>'created_at')::timestamptz BETWEEN q.created_at-interval '30 seconds' AND q.created_at+interval '15 minutes'
        AND (p->>'modified_at')::timestamptz >= (p->>'created_at')::timestamptz
        AND (p->>'modified_at')::timestamptz <= (p->>'observed_at')::timestamptz+interval '30 seconds'
        AND (p->>'observed_at')::timestamptz BETWEEN statement_timestamp()-interval '30 seconds' AND statement_timestamp()+interval '30 seconds'
        AND NEW.fiscal_requested_at BETWEEN statement_timestamp()-interval '30 seconds' AND (p->>'observed_at')::timestamptz
        AND NEW.bank_evidence_id=p->>'proof_hash'
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex')
        AND f->'verified'='true'::jsonb AND f->>'verification_scope'='provider_invoice_association' AND f->'checks'='[]'::jsonb
        AND f->>'invoice_id'=p->>'invoice_id' AND f->>'merchant_id'=p->>'merchant_id' AND f->>'profile_id'=p->>'profile_id'
        AND f->>'authority'=p->>'authority' AND f->>'transport'=p->>'transport'
        AND f->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(f-'proof_hash'),'UTF8')),'hex')
        AND (SELECT array_agg(DISTINCT i.response_json::jsonb->'dispatch_failure'->>'invoice_candidate')
          FROM order_intents i WHERE i.order_id=sale.id
          AND i.response_json::jsonb->'dispatch_failure'->>'stage'='create_response_url'
          AND i.response_json::jsonb->'dispatch_failure'->'http_status'='200'::jsonb
          AND i.response_json::jsonb->'dispatch_failure'->'candidate_identity_verified'='true'::jsonb
        )=ARRAY[p->>'invoice_id'], false)
      THEN RAISE EXCEPTION 'Expired POS closure requires fresh exact unpaid bank and fiscal proof'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER validate_master_mono_expired_closure BEFORE INSERT ON master_mono_expired_closures
      FOR EACH ROW EXECUTE FUNCTION validate_master_mono_expired_closure();
    CREATE TRIGGER immutable_master_mono_expired_closure BEFORE UPDATE OR DELETE ON master_mono_expired_closures
      FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation();

    CREATE FUNCTION protect_master_mono_closed_movements() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target_order_id integer;
    BEGIN
      IF TG_OP='UPDATE' THEN target_order_id:=NEW.order_id; END IF;
      PERFORM 1 FROM orders o WHERE o.id IN (OLD.order_id,target_order_id)
        AND EXISTS (SELECT 1 FROM master_mono_create_requests q WHERE q.order_id=o.id)
        ORDER BY o.id FOR UPDATE;
      IF EXISTS (SELECT 1 FROM master_mono_expired_closures WHERE order_id IN (OLD.order_id,target_order_id))
      THEN RAISE EXCEPTION 'Closed POS inventory history is immutable'; END IF;
      IF TG_OP='UPDATE' THEN RETURN NEW; END IF;
      RETURN OLD;
    END $$;
    CREATE TRIGGER protect_master_mono_closed_movements BEFORE UPDATE OR DELETE ON inventory_movements
      FOR EACH ROW EXECUTE FUNCTION protect_master_mono_closed_movements();

    CREATE FUNCTION require_master_mono_expired_closure_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.fiscal_requested_at < clock_timestamp()-interval '30 seconds'
        OR (NEW.proof::jsonb->>'observed_at')::timestamptz < clock_timestamp()-interval '30 seconds'
      THEN RAISE EXCEPTION 'Expired POS closure evidence became stale before commit'; END IF;
      IF NOT EXISTS (SELECT 1 FROM orders WHERE id=NEW.order_id AND status::text='cancelled' AND revision=NEW.expected_revision+1)
        OR EXISTS (SELECT 1 FROM master_mono_bindings WHERE order_id=NEW.order_id)
        OR (SELECT count(*) FROM inventory_movements WHERE order_id=NEW.order_id
            AND movement_type::text='reservation_release')<>1
        OR (SELECT sum(m.reserved_delta) FROM inventory_movements m JOIN order_items i
            ON i.id=m.order_item_id AND i.product_id=m.product_id
            WHERE m.order_id=NEW.order_id AND i.order_id=NEW.order_id) IS DISTINCT FROM 0
      THEN RAISE EXCEPTION 'Expired POS closure requires its atomic cancellation and one stock release'; END IF;
      RETURN NEW;
    END $$;
    CREATE CONSTRAINT TRIGGER require_master_mono_expired_closure_complete AFTER INSERT ON master_mono_expired_closures
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_master_mono_expired_closure_complete();
    """)
    _guards()
    _closed_conflicts()


def _closed_conflicts():
    op.create_table(
        "master_mono_closed_conflicts",
        sa.Column("operation_key", sa.String(64), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("master_mono_expired_closures.order_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("proof", sa.JSON()),
        sa.Column("fiscal_proof", sa.JSON()),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_master_mono_closed_conflicts_order_id", "master_mono_closed_conflicts", ["order_id"])
    op.execute("""
    CREATE FUNCTION validate_master_mono_closed_conflict() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE c master_mono_expired_closures%ROWTYPE; original jsonb;
            p jsonb := NEW.proof::jsonb; f jsonb := NEW.fiscal_proof::jsonb;
    BEGIN
      PERFORM 1 FROM orders WHERE id=NEW.order_id AND status::text='cancelled' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Late POS evidence requires a cancelled sale'; END IF;
      SELECT * INTO c FROM master_mono_expired_closures WHERE order_id=NEW.order_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Late POS evidence requires its original closure'; END IF;
      original := c.proof::jsonb;

      IF NEW.operation_key !~ '^[a-f0-9]{64}$'
        OR NEW.requested_at < c.created_at
        OR NEW.requested_at NOT BETWEEN statement_timestamp()-interval '30 seconds' AND statement_timestamp()+interval '30 seconds'
        OR NEW.created_at NOT BETWEEN NEW.requested_at AND statement_timestamp()+interval '30 seconds'
        OR NOT EXISTS (SELECT 1 FROM admin_users a WHERE a.id=NEW.actor_id AND a.is_active AND a.is_superuser)
      THEN RAISE EXCEPTION 'Late POS evidence requires a fresh authorized observation'; END IF;

      IF p IS NOT NULL AND NOT COALESCE(
        p->'verified'='true'::jsonb
        AND p->>'status' IN ('created','processing','hold','success','failure','expired','reversed')
        AND jsonb_typeof(p->'has_cancellations')='boolean'
        AND jsonb_typeof(p->'final_amount_minor') IN ('null','number')
        AND (p->>'created_at')::timestamptz = (original->>'created_at')::timestamptz
        AND (p->>'modified_at')::timestamptz >= (p->>'created_at')::timestamptz
        AND (p->>'modified_at')::timestamptz <= (p->>'observed_at')::timestamptz+interval '30 seconds'
        AND (p->>'observed_at')::timestamptz BETWEEN NEW.requested_at AND NEW.created_at
        AND p->>'invoice_id'=original->>'invoice_id' AND p->>'reference'=original->>'reference'
        AND p->'amount_minor'=original->'amount_minor' AND p->'currency_code'=original->'currency_code'
        AND p->>'currency'=original->>'currency' AND p->>'merchant_id'=original->>'merchant_id'
        AND p->>'profile_id'=original->>'profile_id' AND p->>'authority'=original->>'authority'
        AND p->>'transport'=original->>'transport'
        AND p->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(p-'proof_hash'),'UTF8')),'hex'), false)
      THEN RAISE EXCEPTION 'Late POS bank evidence does not match the closed invoice'; END IF;

      IF f IS NOT NULL AND NOT COALESCE(
        f->'verified'='true'::jsonb AND f->>'verification_scope'='provider_invoice_association'
        AND jsonb_typeof(f->'checks')='array' AND jsonb_array_length(f->'checks')<=100
        AND f->>'invoice_id'=original->>'invoice_id' AND f->>'merchant_id'=original->>'merchant_id'
        AND f->>'profile_id'=original->>'profile_id' AND f->>'authority'=original->>'authority'
        AND f->>'transport'=original->>'transport'
        AND f->>'proof_hash'=encode(sha256(convert_to(shop_payment_canonical(f-'proof_hash'),'UTF8')),'hex'), false)
      THEN RAISE EXCEPTION 'Late POS fiscal evidence does not match the closed invoice'; END IF;

      IF NOT (COALESCE(p->>'status'<>'expired' OR p->'has_cancellations'='true'::jsonb
          OR p->'final_amount_minor' NOT IN ('null'::jsonb,'0'::jsonb), false)
        OR COALESCE(jsonb_array_length(f->'checks')>0,false))
      THEN RAISE EXCEPTION 'Late POS evidence must contradict the closure'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER validate_master_mono_closed_conflict BEFORE INSERT ON master_mono_closed_conflicts
      FOR EACH ROW EXECUTE FUNCTION validate_master_mono_closed_conflict();
    CREATE TRIGGER immutable_master_mono_closed_conflict BEFORE UPDATE OR DELETE ON master_mono_closed_conflicts
      FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation();
    """)


def downgrade():
    op.execute("LOCK TABLE master_mono_expired_closures IN SHARE ROW EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM master_mono_expired_closures)")):
        raise RuntimeError("Audited expired POS closures must be preserved")
    _guards(reverse=True)
    op.drop_table("master_mono_closed_conflicts")
    op.execute("DROP FUNCTION validate_master_mono_closed_conflict()")
    op.execute("DROP TRIGGER protect_master_mono_closed_movements ON inventory_movements")
    op.execute("DROP FUNCTION protect_master_mono_closed_movements()")
    op.drop_table("master_mono_expired_closures")
    op.execute("DROP FUNCTION require_master_mono_expired_closure_complete()")
    op.execute("DROP FUNCTION validate_master_mono_expired_closure()")
