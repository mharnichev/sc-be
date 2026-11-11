"""Bind a direct post-dispatch refund to exactly one inspected NP return receipt."""
from alembic import op

revision = "0157_direct_carrier_refunds"
down_revision = "0156_shop_np_direct_returns"
branch_labels = None
depends_on = None

_STATUS = "o.status::text='paid'"
_CARRIER = "NEW.snapshot::jsonb ? 'carrier_receipt'"
_POST_STATUS = f"({_STATUS} OR ({_CARRIER} AND o.status::text='completed'))"
_PRECONDITION = """OR EXISTS (
      SELECT 1 FROM order_items i WHERE i.order_id=NEW.order_id
      AND i.quantity_from_stock+i.quantity_received_for_order <> (
        SELECT COALESCE(SUM(m.reserved_delta), 0) FROM inventory_movements m
        WHERE m.order_id=i.order_id AND m.order_item_id=i.id AND m.product_id=i.product_id
      )
    ) OR NOT shop_resolution_no_handover(NEW.order_id)
      OR EXISTS(SELECT 1 FROM order_shipments WHERE order_id=NEW.order_id AND active)"""
_CARRIER_VALID = """shop_direct_refund_carrier_valid(
          NEW.order_id, NEW.snapshot::jsonb->'carrier_receipt', NEW.snapshot::jsonb->'items',
          (NEW.snapshot->>'amount_minor')::bigint)"""
_POSTCONDITION = f"""OR (NOT ({_CARRIER}) AND (
      { _PRECONDITION[3:] }
    )) OR ({_CARRIER} AND NOT {_CARRIER_VALID})"""
_FINAL = """NOT shop_resolution_no_handover(NEW.order_id)
      OR remainder=0 AND NOT shop_direct_refund_reserves_released(NEW)"""
_POST_FINAL = f"""(NOT ({_CARRIER}) AND ({_FINAL}))
      OR ({_CARRIER} AND NOT {_CARRIER_VALID})"""


def rewrite(pairs):
    conn = op.get_bind()
    sql = conn.exec_driver_sql(
        "SELECT pg_get_functiondef('protect_shop_direct_refund()'::regprocedure)"
    ).scalar_one()
    for old, new in pairs:
        if sql.count(old) != 1:
            raise RuntimeError("Unexpected direct refund trigger definition")
        sql = sql.replace(old, new, 1)
    conn.exec_driver_sql(sql, execution_options={"no_parameters": True})


def upgrade():
    op.get_bind().exec_driver_sql("""
CREATE FUNCTION shop_direct_refund_carrier_valid(oid integer, carrier jsonb, goods jsonb, amount bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS(
  SELECT 1 FROM shop_np_direct_return_receipts r
  WHERE r.id::text=carrier->>'receipt_id' AND r.order_id=oid
   AND r.proof_hash=carrier->>'proof_hash'
   AND r.quantities::jsonb=carrier->'quantities'
   AND r.snapshot->>'original_ttn'=carrier->>'original_ttn'
   AND r.snapshot->>'return_ttn'=carrier->>'return_ttn'
   AND r.quantities::jsonb = (
    SELECT jsonb_object_agg(q.id::text,q.qty) FROM (
      SELECT i.id, sum((g->>'qty')::int) AS qty
      FROM jsonb_array_elements(goods) g
      JOIN order_items i ON i.order_id=oid AND i.product_id::text=g->>'code'
      GROUP BY i.id
    ) q
   )
   AND goods = (
    SELECT jsonb_agg((part.line-'qty'-'total') || jsonb_build_object(
        'qty', part.take, 'total', part.take*(part.line->>'sum')::bigint) ORDER BY part.position)
    FROM (
      SELECT b.line, b.position, least((b.line->>'qty')::int, greatest(
        COALESCE((r.quantities->>i.id::text)::int,0) - COALESCE(sum((b.line->>'qty')::int)
          OVER (PARTITION BY i.id ORDER BY b.position ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0),0)) AS take
      FROM shop_online_payment_requests a
      CROSS JOIN LATERAL jsonb_array_elements(a.basket::jsonb) WITH ORDINALITY b(line, position)
      JOIN order_items i ON i.order_id=oid AND i.product_id::text=b.line->>'code'
      WHERE a.order_id=oid
    ) part
    WHERE part.take>0
   )
   AND amount=(SELECT sum((g->>'total')::bigint) FROM jsonb_array_elements(goods) g)
 )
$$
""", execution_options={"no_parameters": True})
    rewrite(((_STATUS, _POST_STATUS), (_PRECONDITION, _POSTCONDITION), (_FINAL, _POST_FINAL)))


def downgrade():
    conn = op.get_bind()
    if conn.exec_driver_sql("""
        SELECT EXISTS(SELECT 1 FROM shop_mono_checkbox_refunds
        WHERE snapshot::jsonb ? 'carrier_receipt')
    """).scalar_one():
        raise RuntimeError("Direct carrier refund history exists; downgrade refused")
    rewrite(((_POST_FINAL, _FINAL), (_POSTCONDITION, _PRECONDITION), (_POST_STATUS, _STATUS)))
    conn.exec_driver_sql("DROP FUNCTION shop_direct_refund_carrier_valid(integer, jsonb, jsonb, bigint)")
