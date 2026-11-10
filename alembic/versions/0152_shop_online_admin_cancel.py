"""Allow audited cancellation before the Shop Mono dispatch fence."""
from alembic import op
import sqlalchemy as sa

revision = "0152_shop_online_admin_cancel"
down_revision = "0151_master_mono_url_provenance"
branch_labels = None
depends_on = None

OLD_GUARD = "AND NOT shop_resolution_can_close(OLD.id)"
NEW_GUARD = OLD_GUARD + " AND NOT shop_online_cancel_authorized(OLD.id, OLD.revision)"


def replace_r2_guard(old, new):
    source = op.get_bind().execute(sa.text(
        "SELECT pg_get_functiondef(to_regprocedure('protect_shop_r2_order()'))"
    )).scalar_one()
    if not source or source.count(old) != 1:
        raise RuntimeError("Unexpected Shop payment cancellation guard")
    op.execute(source.replace(old, new, 1))


def upgrade():
    op.execute("""
    CREATE FUNCTION shop_online_dispatch_pending(oid integer) RETURNS boolean
    LANGUAGE sql STABLE AS $$
      SELECT EXISTS (
        SELECT 1 FROM shop_online_payment_requests r
        JOIN shop_payment_intents i ON i.id=r.intent_id AND i.order_id=r.order_id
        JOIN shop_operation_audits p ON p.order_id=r.order_id AND p.operation='online_dispatch_prepared'
          AND p.operator_id=r.operator_id
          AND p.facts::jsonb=jsonb_build_object('request_id',r.id,'intent_id',r.intent_id)
        WHERE r.order_id=oid AND i.mode='prepayment' AND i.invoice_id IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM shop_operation_audits a WHERE a.order_id=oid
              AND a.operation IN ('online_dispatch_started','online_invoice_created','online_invoice_recovered')
          )
      )
    $$;
    CREATE FUNCTION shop_online_cancel_eligible(oid integer) RETURNS boolean
    LANGUAGE sql STABLE AS $$
      SELECT EXISTS (
        SELECT 1 FROM orders o
        WHERE o.id=oid AND o.channel='shop' AND NOT o.simulation
          AND o.payment_method='onlineCard' AND o.status::text IN ('pending','confirmed')
          AND shop_resolution_no_handover(oid)
          AND NOT EXISTS (SELECT 1 FROM order_shipments WHERE order_id=oid AND active)
          AND NOT EXISTS (SELECT 1 FROM order_payments WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_payment_confirmations WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_payment_evidence WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_payment_resolution_cases WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_carrier_returns WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_np_return_cases WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_financial_returns WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_fiscal_documents WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM fiscal_documents WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_checkbox_operations WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_checkbox_documents WHERE order_id=oid)
          AND NOT EXISTS (SELECT 1 FROM shop_mono_checkbox_receipts WHERE order_id=oid)
          AND (
            NOT EXISTS (SELECT 1 FROM shop_payment_intents WHERE order_id=oid)
            AND NOT EXISTS (SELECT 1 FROM shop_online_payment_requests WHERE order_id=oid)
            OR shop_online_dispatch_pending(oid)
          )
      )
    $$;
    CREATE FUNCTION protect_shop_cancelled_invoice_binding() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.invoice_id IS NULL AND NEW.invoice_id IS NOT NULL THEN
        PERFORM 1 FROM orders WHERE id=OLD.order_id FOR UPDATE;
        IF EXISTS (
          SELECT 1 FROM shop_operation_audits WHERE order_id=OLD.order_id AND operation='online_cancel_undispatched'
        ) THEN
          RAISE EXCEPTION 'Cannot bind an invoice after undispatched Shop cancellation';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER protect_shop_cancelled_invoice_binding BEFORE UPDATE OF invoice_id ON shop_payment_intents
      FOR EACH ROW EXECUTE FUNCTION protect_shop_cancelled_invoice_binding();
    CREATE FUNCTION shop_online_cancel_authorized(oid integer, expected_revision integer) RETURNS boolean
    LANGUAGE sql STABLE AS $$
      SELECT shop_online_cancel_eligible(oid) AND EXISTS (
        SELECT 1 FROM shop_operation_audits a
        JOIN shop_online_payment_requests r ON r.order_id=a.order_id
        WHERE a.order_id=oid AND a.operation='online_cancel_undispatched'
          AND a.facts::jsonb=jsonb_build_object('request_id',r.id,'expected_revision',expected_revision)
      )
    $$;
    """)
    replace_r2_guard(OLD_GUARD, NEW_GUARD)


def downgrade():
    if op.get_bind().execute(sa.text("""
        SELECT EXISTS (SELECT 1 FROM shop_operation_audits WHERE operation='online_cancel_undispatched')
    """)).scalar_one():
        raise RuntimeError("Cannot remove Shop cancellation protection after recording cancellations")
    replace_r2_guard(NEW_GUARD, OLD_GUARD)
    op.execute("DROP TRIGGER protect_shop_cancelled_invoice_binding ON shop_payment_intents")
    op.execute("DROP FUNCTION protect_shop_cancelled_invoice_binding()")
    op.execute("DROP FUNCTION shop_online_cancel_authorized(integer, integer)")
    op.execute("DROP FUNCTION shop_online_cancel_eligible(integer)")
    op.execute("DROP FUNCTION shop_online_dispatch_pending(integer)")
