"""Persist an isolated direct Mono→Checkbox refund fence and exact evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0155_shop_direct_refunds"
down_revision = "0154_unbound_shop_resolution"
branch_labels = None
depends_on = None

_FUNCTIONS = ("protect_shop_order", "protect_shop_r2_order")
_CLOSE = "AND NOT shop_resolution_can_close(OLD.id)"
_DIRECT = " AND NOT shop_direct_refund_can_close(OLD.id)"
_BLOCKED = (
    "order_handovers", "shop_fulfillment_facts", "shop_completions", "shop_payment_resolution_cases",
    "shop_checkbox_returns", "shop_financial_returns", "shop_carrier_returns", "shop_np_return_cases",
    "order_shipments",
)


def execute_ddl(sql):
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})


def change_close_guards(before, after):
    connection = op.get_bind()
    for name in _FUNCTIONS:
        definition = connection.execute(
            sa.text("SELECT pg_get_functiondef(to_regprocedure(:name))"), {"name": name + "()"}
        ).scalar_one()
        if definition.count(before) != 1:
            raise RuntimeError(f"Unexpected closure guard in {name}")
        connection.exec_driver_sql(definition.replace(before, after, 1), execution_options={"no_parameters": True})


def upgrade():
    op.create_table(
        "shop_mono_checkbox_refunds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("shop_mono_checkbox_receipts.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ext_ref", sa.String(32), nullable=False, unique=True),
        sa.Column("request_key", sa.String(64), nullable=False, unique=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("bank_proof", sa.JSON()),
        sa.Column("receipt_proof", sa.JSON()),
        sa.Column("return_receipt_id", sa.String(36), unique=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(48), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ext_ref ~ '^[a-f0-9]{32}$'", name="ck_shop_direct_refund_ext_ref"),
        sa.CheckConstraint(
            "COALESCE(snapshot->>'authority'='test' AND snapshot->>'kind' IN ('full','partial'), false)",
            name="ck_shop_direct_refund_test",
        ),
        sa.CheckConstraint("(receipt_proof IS NULL)=(return_receipt_id IS NULL)", name="ck_shop_direct_refund_receipt"),
        sa.CheckConstraint("finalized_at IS NULL OR (bank_proof IS NOT NULL AND receipt_proof IS NOT NULL)",
                           name="ck_shop_direct_refund_finalized"),
    )
    execute_ddl("""
CREATE FUNCTION shop_direct_refund_reserves_released(f shop_mono_checkbox_refunds) RETURNS boolean
LANGUAGE sql STABLE AS $$
 SELECT NOT EXISTS (
   SELECT 1 FROM jsonb_array_elements(f.snapshot::jsonb->'reserve_plan') plan
   WHERE (
     SELECT COALESCE(SUM(m.reserved_delta), 0) FROM inventory_movements m
     WHERE m.order_id=f.order_id AND m.order_item_id::text=plan->>'order_item_id'
       AND m.product_id::text=plan->>'product_id'
   ) <> 0 OR ((plan->>'quantity')::int > 0 AND NOT EXISTS (
     SELECT 1 FROM inventory_movements m
     WHERE m.order_id=f.order_id AND m.order_item_id::text=plan->>'order_item_id'
       AND m.product_id::text=plan->>'product_id' AND m.quantity::text=plan->>'quantity'
       AND m.movement_type::text='reservation_release' AND m.on_hand_delta=0 AND m.reserved_delta=-m.quantity
       AND m.reference_key=concat('shop-direct-refund:', f.id, ':item:', plan->>'order_item_id', ':reserve-release')
   ))
 )
$$
""")
    execute_ddl("""
CREATE FUNCTION shop_direct_refund_can_close(oid integer) RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (
   SELECT 1 FROM shop_mono_checkbox_refunds f
   WHERE f.order_id=oid AND f.finalized_at IS NOT NULL AND f.snapshot->>'kind'='full'
     AND f.bank_proof->>'remaining_minor'='0' AND shop_resolution_no_handover(oid)
     AND shop_direct_refund_reserves_released(f)
 )
$$
""")
    change_close_guards(_CLOSE, _CLOSE + _DIRECT)
    execute_ddl("""
CREATE FUNCTION protect_shop_direct_refund() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE original_amount bigint; refund_amount bigint; remainder bigint;
BEGIN
 IF TG_OP='DELETE' THEN
    RAISE EXCEPTION 'Direct refund claim is immutable';
 ELSIF TG_OP='INSERT' THEN
    PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
    IF NEW.snapshot_hash IS DISTINCT FROM shop_resolution_hash(NEW.snapshot::jsonb)
      OR jsonb_typeof(NEW.snapshot::jsonb->'items') IS DISTINCT FROM 'array'
      OR NEW.snapshot->>'items_hash' IS DISTINCT FROM shop_resolution_hash(NEW.snapshot::jsonb->'items')
      OR jsonb_typeof(NEW.snapshot::jsonb->'invoice_amount_minor') IS DISTINCT FROM 'number'
      OR jsonb_typeof(NEW.snapshot::jsonb->'amount_minor') IS DISTINCT FROM 'number'
      OR NOT EXISTS (
      SELECT 1 FROM shop_mono_checkbox_receipts s JOIN orders o ON o.id=s.order_id
      WHERE s.id=NEW.sale_id AND o.id=NEW.order_id AND o.status::text='paid' AND s.authority='test'
      AND s.invoice_id=NEW.snapshot->>'invoice_id' AND s.receipt_id=NEW.snapshot->>'sale_receipt_id'
      AND s.snapshot_hash=NEW.snapshot->>'sale_snapshot_hash' AND s.profile_hash=NEW.snapshot->>'profile_hash'
      AND s.snapshot->>'merchant_id'=NEW.snapshot->>'merchant_id'
      AND s.snapshot->>'reference'=NEW.snapshot->>'reference'
      AND s.snapshot->>'amount_minor'=NEW.snapshot->>'invoice_amount_minor'
    ) OR NEW.snapshot::jsonb->'reserve_plan' IS DISTINCT FROM (
      SELECT jsonb_agg(jsonb_build_object('order_item_id', i.id, 'product_id', i.product_id,
                       'quantity', i.quantity_from_stock+i.quantity_received_for_order) ORDER BY i.id)
      FROM order_items i WHERE i.order_id=NEW.order_id
    ) OR EXISTS (
      SELECT 1 FROM order_items i WHERE i.order_id=NEW.order_id
      AND i.quantity_from_stock+i.quantity_received_for_order <> (
        SELECT COALESCE(SUM(m.reserved_delta), 0) FROM inventory_movements m
        WHERE m.order_id=i.order_id AND m.order_item_id=i.id AND m.product_id=i.product_id
      )
    ) OR NOT shop_resolution_no_handover(NEW.order_id)
      OR EXISTS(SELECT 1 FROM order_shipments WHERE order_id=NEW.order_id AND active)
      OR EXISTS(SELECT 1 FROM shop_payment_resolution_cases WHERE order_id=NEW.order_id)
      OR NEW.bank_proof IS NOT NULL OR NEW.receipt_proof IS NOT NULL OR NEW.finalized_at IS NOT NULL THEN
        RAISE EXCEPTION 'Direct refund needs an exclusive, accepted, pre-handover sale';
    END IF;
 ELSIF OLD.finalized_at IS NOT NULL OR NEW.id IS DISTINCT FROM OLD.id OR NEW.order_id IS DISTINCT FROM OLD.order_id
       OR NEW.sale_id IS DISTINCT FROM OLD.sale_id OR NEW.operator_id IS DISTINCT FROM OLD.operator_id
       OR NEW.ext_ref IS DISTINCT FROM OLD.ext_ref OR NEW.request_key IS DISTINCT FROM OLD.request_key
       OR NEW.snapshot::jsonb IS DISTINCT FROM OLD.snapshot::jsonb OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR OLD.bank_proof IS NOT NULL AND NEW.bank_proof::jsonb IS DISTINCT FROM OLD.bank_proof::jsonb
       OR OLD.receipt_proof IS NOT NULL AND NEW.receipt_proof::jsonb IS DISTINCT FROM OLD.receipt_proof::jsonb
       OR OLD.return_receipt_id IS NOT NULL AND NEW.return_receipt_id IS DISTINCT FROM OLD.return_receipt_id THEN
   RAISE EXCEPTION 'Direct refund identity and accepted evidence are immutable';
 END IF;
 original_amount = (NEW.snapshot->>'invoice_amount_minor')::bigint;
 refund_amount = (NEW.snapshot->>'amount_minor')::bigint;
 remainder = original_amount-refund_amount;
 IF refund_amount <= 0 OR remainder < 0 OR (remainder=0) IS DISTINCT FROM (NEW.snapshot->>'kind'='full') THEN
    RAISE EXCEPTION 'Invalid direct refund amount';
 END IF;
 IF NEW.bank_proof IS NOT NULL AND (
     jsonb_typeof(NEW.bank_proof::jsonb) IS DISTINCT FROM 'object'
     OR jsonb_typeof(NEW.bank_proof::jsonb->'amount_minor') IS DISTINCT FROM 'number'
     OR jsonb_typeof(NEW.bank_proof::jsonb->'remaining_minor') IS DISTINCT FROM 'number'
     OR NEW.bank_proof->>'remaining_minor' IS DISTINCT FROM remainder::text
     OR NEW.bank_proof->>'status' IS DISTINCT FROM (CASE WHEN remainder=0 THEN 'reversed' ELSE 'success' END)
     OR NEW.bank_proof->>'modified_at' IS NULL OR NEW.bank_proof->>'observed_at' IS NULL
     OR NEW.bank_proof->>'ext_ref' IS DISTINCT FROM NEW.ext_ref
     OR NEW.bank_proof->>'invoice_id' IS DISTINCT FROM NEW.snapshot->>'invoice_id'
     OR NEW.bank_proof->>'reference' IS DISTINCT FROM NEW.snapshot->>'reference'
     OR NEW.bank_proof->>'amount_minor' IS DISTINCT FROM NEW.snapshot->>'amount_minor'
     OR NEW.bank_proof->>'currency' IS DISTINCT FROM 'UAH'
 ) OR NEW.receipt_proof IS NOT NULL AND (
     jsonb_typeof(NEW.receipt_proof::jsonb) IS DISTINCT FROM 'object'
     OR jsonb_typeof(NEW.receipt_proof::jsonb->'amount_minor') IS DISTINCT FROM 'number'
     OR NEW.receipt_proof->>'profile_hash' IS DISTINCT FROM NEW.snapshot->>'profile_hash'
     OR NEW.receipt_proof->>'merchant_id' IS DISTINCT FROM NEW.snapshot->>'merchant_id'
     OR NEW.receipt_proof->>'items_hash' IS DISTINCT FROM NEW.snapshot->>'items_hash'
     OR NEW.receipt_proof->>'ext_ref' IS DISTINCT FROM NEW.ext_ref
     OR NEW.receipt_proof->>'invoice_id' IS DISTINCT FROM NEW.snapshot->>'invoice_id'
     OR NEW.receipt_proof->>'original_receipt_id' IS DISTINCT FROM NEW.snapshot->>'sale_receipt_id'
     OR NEW.receipt_proof->>'amount_minor' IS DISTINCT FROM NEW.snapshot->>'amount_minor'
     OR NEW.receipt_proof->>'receipt_id' IS DISTINCT FROM NEW.return_receipt_id
     OR NEW.return_receipt_id !~ '^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
     OR EXISTS (SELECT 1 FROM shop_mono_checkbox_receipts WHERE receipt_id=NEW.return_receipt_id)
     OR NEW.receipt_proof->>'issuer' IS DISTINCT FROM 'checkbox'
     OR NEW.receipt_proof->>'currency' IS DISTINCT FROM 'UAH'
     OR NEW.receipt_proof->>'type' IS DISTINCT FROM 'return'
     OR NEW.receipt_proof->>'status' IS DISTINCT FROM 'done'
 ) THEN
    RAISE EXCEPTION 'Direct refund evidence must match the immutable claim';
 END IF;
 IF NEW.finalized_at IS NOT NULL AND (
      NOT shop_resolution_no_handover(NEW.order_id)
      OR remainder=0 AND NOT shop_direct_refund_reserves_released(NEW)
 ) THEN RAISE EXCEPTION 'Direct refund finalization requires exact owned reserve releases'; END IF;
 RETURN NEW;
END
$$
""")
    op.execute("""CREATE TRIGGER protect_shop_direct_refund BEFORE INSERT OR UPDATE OR DELETE
        ON shop_mono_checkbox_refunds FOR EACH ROW EXECUTE FUNCTION protect_shop_direct_refund()""")
    execute_ddl("""
CREATE FUNCTION protect_shop_direct_refund_context() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
 IF EXISTS(SELECT 1 FROM shop_mono_checkbox_refunds WHERE order_id=NEW.order_id) THEN
    RAISE EXCEPTION 'Direct refund claim blocks handover and competing workflows';
 END IF;
 RETURN NEW;
END
$$
""")
    for table in _BLOCKED:
        event = "INSERT OR UPDATE OF active, handed_over_at" if table == "order_shipments" else "INSERT"
        op.execute(f"CREATE TRIGGER protect_shop_direct_refund_context BEFORE {event} ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION protect_shop_direct_refund_context()")

    execute_ddl("""
CREATE FUNCTION protect_shop_direct_refund_stock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE f shop_mono_checkbox_refunds;
BEGIN
 SELECT r.* INTO f FROM shop_mono_checkbox_refunds r
 WHERE r.order_id=NEW.order_id OR r.order_id=(SELECT order_id FROM order_items WHERE id=NEW.order_item_id)
 ORDER BY r.id LIMIT 1;
 IF f.id IS NULL THEN RETURN NEW; END IF;
 IF NOT EXISTS (
   SELECT 1 FROM jsonb_array_elements(f.snapshot::jsonb->'reserve_plan') plan
   WHERE f.order_id=NEW.order_id AND f.snapshot->>'kind'='full'
     AND f.bank_proof IS NOT NULL AND f.receipt_proof IS NOT NULL
     AND NEW.order_item_id::text=plan->>'order_item_id' AND NEW.product_id::text=plan->>'product_id'
     AND NEW.quantity::text=plan->>'quantity' AND NEW.movement_type::text='reservation_release'
     AND NEW.on_hand_delta=0 AND NEW.reserved_delta=-NEW.quantity
     AND NEW.reference_key=concat('shop-direct-refund:', f.id, ':item:', plan->>'order_item_id', ':reserve-release')
 ) THEN RAISE EXCEPTION 'Direct refund inventory is frozen'; END IF;
 IF NOT EXISTS(SELECT 1 FROM orders WHERE id=f.order_id AND status::text='cancelled')
    OR NOT shop_direct_refund_can_close(f.order_id)
 THEN RAISE EXCEPTION 'Direct refund stock release and cancellation must commit together'; END IF;
 RETURN NEW;
END
$$
""")
    op.execute("""CREATE CONSTRAINT TRIGGER protect_shop_direct_refund_stock AFTER INSERT ON inventory_movements
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION protect_shop_direct_refund_stock()""")
    execute_ddl("""
CREATE FUNCTION protect_shop_direct_refund_items() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_order int; new_order int;
BEGIN
 IF TG_OP!='INSERT' THEN old_order=OLD.order_id; END IF;
 IF TG_OP!='DELETE' THEN new_order=NEW.order_id; END IF;
 PERFORM 1 FROM orders WHERE id IN(old_order,new_order) ORDER BY id FOR UPDATE;
 IF EXISTS(SELECT 1 FROM shop_mono_checkbox_refunds WHERE order_id IN(old_order,new_order)) AND (
   TG_OP!='UPDATE' OR to_jsonb(NEW)-'procurement_status' IS DISTINCT FROM to_jsonb(OLD)-'procurement_status'
 ) THEN RAISE EXCEPTION 'Direct refund goods and reserve ownership are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END
$$
""")
    op.execute("""CREATE TRIGGER protect_shop_direct_refund_items BEFORE INSERT OR UPDATE OR DELETE ON order_items
        FOR EACH ROW EXECUTE FUNCTION protect_shop_direct_refund_items()""")


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS(SELECT 1 FROM shop_mono_checkbox_refunds)")).scalar_one():
        raise RuntimeError("Direct refund history exists; downgrade refused")
    for table in _BLOCKED:
        op.execute(f"DROP TRIGGER protect_shop_direct_refund_context ON {table}")
    change_close_guards(_CLOSE + _DIRECT, _CLOSE)
    op.execute("DROP TRIGGER protect_shop_direct_refund_stock ON inventory_movements")
    op.execute("DROP FUNCTION protect_shop_direct_refund_stock()")
    op.execute("DROP TRIGGER protect_shop_direct_refund_items ON order_items")
    op.execute("DROP FUNCTION protect_shop_direct_refund_items()")
    op.execute("DROP FUNCTION shop_direct_refund_can_close(integer)")
    op.execute("DROP FUNCTION shop_direct_refund_reserves_released(shop_mono_checkbox_refunds)")
    op.execute("DROP FUNCTION protect_shop_direct_refund_context()")
    op.drop_table("shop_mono_checkbox_refunds")
    op.execute("DROP FUNCTION protect_shop_direct_refund()")
