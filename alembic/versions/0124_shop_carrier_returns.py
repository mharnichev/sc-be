"""Shop-only test carrier return provenance and narrow unpaid refusal closure."""
from alembic import op
import sqlalchemy as sa

revision = "0124_shop_carrier_returns"
down_revision = "0123_shop_fiscal"
branch_labels = None
depends_on = None


def ref(name, target, *, nullable=False, unique=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(target + ".id", ondelete="RESTRICT"), nullable=nullable, unique=unique)


def core():
    return [sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("evidence", sa.String(500), nullable=False), ref("operator_id", "admin_users"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


ORIGINAL_PROTECT = """CREATE OR REPLACE FUNCTION protect_shop_order() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM shop_payment_confirmations WHERE order_id=OLD.id)
 OR EXISTS(SELECT 1 FROM shop_fulfillment_facts WHERE order_id=OLD.id) THEN
  IF NEW.channel IS DISTINCT FROM OLD.channel OR NEW.simulation IS DISTINCT FROM OLD.simulation
   OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method OR NEW.total_amount IS DISTINCT FROM OLD.total_amount
   OR NEW.status::text='cancelled' AND OLD.status::text!='cancelled'
  THEN RAISE EXCEPTION 'Accepted Shop facts require explicit refund or return reconciliation'; END IF;
 END IF;
 RETURN NEW;
END $$"""


def upgrade():
    op.create_table("shop_carrier_returns", sa.Column("id", sa.Integer(), primary_key=True), ref("order_id", "orders"),
        ref("original_shipment_id", "order_shipments", unique=True), ref("return_shipment_id", "order_shipments", unique=True),
        ref("fulfillment_id", "shop_fulfillment_facts"), ref("sale_document_id", "shop_fiscal_documents", nullable=True),
        sa.Column("shift_revision", sa.Integer(), nullable=True), sa.Column("financial_mode", sa.String(16), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False), sa.Column("provider_uuid", sa.String(36), nullable=False, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False), *core(),
        sa.CheckConstraint("authority='test' AND revision>0 AND status IN ('unknown','returning','returned','received','closed_unpaid')", name="ck_shop_carrier_return"),
        sa.CheckConstraint("(financial_mode='paid' AND sale_document_id IS NOT NULL AND shift_revision>0) OR (financial_mode='unpaid_cod' AND sale_document_id IS NULL AND shift_revision IS NULL)", name="ck_shop_carrier_return_financial"))
    op.create_table("shop_carrier_return_acceptances", sa.Column("provider_uuid", sa.String(36), primary_key=True),
        ref("case_id", "shop_carrier_returns", unique=True), sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("tracking_number", sa.String(64), nullable=False, unique=True), sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("shop_carrier_return_receipts", sa.Column("id", sa.Integer(), primary_key=True),
        ref("case_id", "shop_carrier_returns", unique=True), sa.Column("lines", sa.JSON(), nullable=False), *core())
    op.create_table("shop_carrier_unpaid_closures", sa.Column("id", sa.Integer(), primary_key=True),
        ref("case_id", "shop_carrier_returns", unique=True), ref("order_id", "orders", unique=True),
        ref("receipt_id", "shop_carrier_return_receipts", unique=True), *core())
    for table in ("shop_financial_returns", "shop_return_receipts"):
        op.add_column(table, sa.Column("carrier_receipt_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_" + table + "_carrier_receipt", table, "shop_carrier_return_receipts", ["carrier_receipt_id"], ["id"], ondelete="RESTRICT")
    for table in ("shop_carrier_return_acceptances", "shop_carrier_return_receipts", "shop_carrier_unpaid_closures"):
        op.execute(f"CREATE TRIGGER immutable_shop_carrier_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION validate_shop_carrier_case() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     IF TG_OP='DELETE' OR TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['status','revision']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','revision'])
     THEN RAISE EXCEPTION 'Shop carrier return approval is immutable'; END IF;
     IF NOT EXISTS(SELECT 1 FROM orders o JOIN shop_fulfillment_facts f ON f.order_id=o.id
       JOIN order_handovers h ON h.id=f.handover_id JOIN order_shipments s ON s.id=f.shipment_id
       JOIN order_shipments r ON r.id=NEW.return_shipment_id
       WHERE o.id=NEW.order_id AND o.channel='shop' AND NOT o.simulation AND o.fulfillment_method='nova_poshta'
       AND f.id=NEW.fulfillment_id AND f.kind='nova_poshta' AND NOT h.simulation AND h.order_id=o.id
       AND s.id=NEW.original_shipment_id AND s.order_id=o.id AND NOT s.simulation AND s.carrier='nova_poshta'
       AND s.source='attach' AND s.return_of_shipment_id IS NULL AND s.handed_over_at IS NOT NULL
       AND r.order_id=o.id AND r.simulation AND r.carrier='shop_mock_np_return' AND r.source='shop_return'
       AND r.return_of_shipment_id=s.id AND r.operation_uuid=NEW.provider_uuid
       AND NEW.snapshot->>'order_id'=o.id::text AND NEW.snapshot->>'original_shipment_id'=s.id::text
       AND NEW.snapshot->>'return_shipment_id'=r.id::text AND NEW.snapshot->>'fulfillment_id'=f.id::text
       AND NEW.snapshot->>'authority'='test' AND NEW.snapshot->>'financial_mode'=NEW.financial_mode
       AND NEW.snapshot->>'provider_uuid'=NEW.provider_uuid AND (NEW.snapshot->'lines')::jsonb=NEW.lines::jsonb)
     THEN RAISE EXCEPTION 'Shop carrier return requires exact canonical dispatch and reverse binding'; END IF;
     IF jsonb_array_length(NEW.lines::jsonb)=0 OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.lines::jsonb) l
       LEFT JOIN order_items i ON i.id=(l->>'order_item_id')::int AND i.order_id=NEW.order_id
       WHERE i.id IS NULL OR (l->>'quantity')::int<=0 OR (l->>'quantity')::int>i.quantity OR (l->>'product_id')::int!=i.product_id)
       OR (SELECT count(*) FROM jsonb_array_elements(NEW.lines::jsonb))!=(SELECT count(DISTINCT l->>'order_item_id') FROM jsonb_array_elements(NEW.lines::jsonb) l)
     THEN RAISE EXCEPTION 'Shop carrier return allocation mismatch'; END IF;
     IF TG_OP='INSERT' THEN
      PERFORM 1 FROM shop_fiscal_registers WHERE id='SHOP-MOCK-KASA-1' FOR UPDATE;
      IF NEW.financial_mode='paid' AND NOT EXISTS(SELECT 1 FROM shop_fiscal_documents d
       JOIN shop_fiscal_acceptances a ON a.document_id=d.id JOIN shop_payment_confirmations p ON p.id=d.payment_confirmation_id
       JOIN shop_fiscal_registers s ON s.id='SHOP-MOCK-KASA-1' AND s.shift_status='open' AND s.shift_revision=NEW.shift_revision
       WHERE d.id=NEW.sale_document_id AND d.order_id=NEW.order_id AND d.kind='sale' AND d.status='issued'
       AND a.provider_uuid=d.provider_uuid AND a.snapshot_hash=d.snapshot_hash AND p.order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Paid Shop return requires issued canonical sale and assigned open shift'; END IF;
      IF NEW.financial_mode='unpaid_cod' AND (EXISTS(SELECT 1 FROM order_payments WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM shop_completions WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM shop_fiscal_documents WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM fiscal_documents WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM shop_financial_returns WHERE order_id=NEW.order_id)
       OR EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND payment_method NOT IN ('cod','cashOnDelivery','cardOnDelivery','cash_on_delivery','card_on_delivery'))
       OR EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=NEW.order_id AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.lines::jsonb) l WHERE (l->>'order_item_id')::int=i.id AND (l->>'quantity')::int=i.quantity))
       OR NOT EXISTS(SELECT 1 FROM order_shipments s JOIN shipment_tracking_events e ON e.shipment_id=s.id
        WHERE s.id=NEW.original_shipment_id AND s.last_attempt_status='success' AND s.tracking_status IN ('refused','returning')
        AND e.normalized_status=s.tracking_status AND e.event_at IS NOT NULL AND e.event_at=s.provider_event_at
        AND e.raw_status=s.raw_status AND e.observed_at<=s.last_checked_at))
      THEN RAISE EXCEPTION 'Unpaid COD return requires full refused parcel and no financial facts'; END IF;
     END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_carrier_case BEFORE INSERT OR UPDATE OR DELETE ON shop_carrier_returns FOR EACH ROW EXECUTE FUNCTION validate_shop_carrier_case()")
    op.execute("""CREATE FUNCTION validate_shop_carrier_acceptance() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     PERFORM 1 FROM shop_fiscal_registers WHERE id='SHOP-MOCK-KASA-1' FOR UPDATE;
     IF NOT EXISTS(SELECT 1 FROM shop_carrier_returns c JOIN order_shipments s ON s.id=c.return_shipment_id
       WHERE c.id=NEW.case_id AND c.provider_uuid=NEW.provider_uuid AND c.snapshot_hash=NEW.snapshot_hash AND c.status='unknown'
       AND s.operation_uuid=c.provider_uuid AND s.provider_reference=NEW.tracking_number
       AND NEW.tracking_number='SHOP-RETURN-'||replace(c.provider_uuid,'-',''))
     THEN RAISE EXCEPTION 'Shop return acceptance requires committed exact dispatch identity'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_carrier_acceptance BEFORE INSERT ON shop_carrier_return_acceptances FOR EACH ROW EXECUTE FUNCTION validate_shop_carrier_acceptance()")
    op.execute("""CREATE FUNCTION validate_shop_carrier_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     IF NOT EXISTS(SELECT 1 FROM shop_carrier_returns c JOIN order_shipments s ON s.id=c.return_shipment_id
       JOIN shop_carrier_return_acceptances a ON a.case_id=c.id JOIN shipment_tracking_events e ON e.shipment_id=s.id
       WHERE c.id=NEW.case_id AND a.provider_uuid=c.provider_uuid AND a.snapshot_hash=c.snapshot_hash
       AND a.tracking_number=s.tracking_number AND s.status='returned' AND s.tracking_status='returned' AND s.last_attempt_status='success'
       AND e.normalized_status='returned' AND e.event_at IS NOT NULL AND e.event_at=s.provider_event_at
       AND e.raw_status=s.raw_status AND e.observed_at<=s.last_checked_at
       AND NEW.snapshot->>'case_id'=c.id::text AND NEW.snapshot->>'case_hash'=c.snapshot_hash
       AND NEW.snapshot->>'authority'='test' AND (NEW.snapshot->'lines')::jsonb=NEW.lines::jsonb
       AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(c.lines::jsonb) l WHERE NOT EXISTS(
         SELECT 1 FROM jsonb_array_elements(NEW.lines::jsonb) x WHERE x->>'order_item_id'=l->>'order_item_id'
          AND x->>'quantity'=l->>'quantity' AND x->>'disposition' IN ('restock','not_restock')))
       AND jsonb_array_length(c.lines::jsonb)=jsonb_array_length(NEW.lines::jsonb))
     THEN RAISE EXCEPTION 'Shop parcel receipt requires complete accepted returned parcel'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_carrier_receipt BEFORE INSERT ON shop_carrier_return_receipts FOR EACH ROW EXECUTE FUNCTION validate_shop_carrier_receipt()")
    op.execute("""CREATE FUNCTION validate_shop_carrier_receipt_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE line jsonb; c shop_carrier_returns%ROWTYPE; product int; shipped int;
    BEGIN
     SELECT * INTO c FROM shop_carrier_returns WHERE id=NEW.case_id;
     FOR line IN SELECT * FROM jsonb_array_elements(NEW.lines::jsonb) LOOP
      SELECT product_id,quantity INTO product,shipped FROM order_items WHERE id=(line->>'order_item_id')::int AND order_id=c.order_id;
      IF product IS NULL OR NOT EXISTS(SELECT 1 FROM inventory_movements m
        WHERE m.order_id=c.order_id AND m.order_item_id=(line->>'order_item_id')::int AND m.product_id=product
        AND m.movement_type::text='shipment' AND -m.on_hand_delta=shipped
        AND m.reference_key='order:'||c.order_id||':'||'item'||':'||(line->>'order_item_id')||':'||'shipment')
      THEN RAISE EXCEPTION 'Shop return receipt lacks original canonical shipment ledger'; END IF;
      IF line->>'disposition'='restock' AND NOT EXISTS(SELECT 1 FROM inventory_movements m
        WHERE m.reference_key='shop-carrier-return:'||NEW.id||':'||(line->>'order_item_id')
        AND m.order_id=c.order_id AND m.order_item_id=(line->>'order_item_id')::int AND m.product_id=product
        AND m.movement_type::text='customer_return' AND m.quantity=(line->>'quantity')::int
        AND m.on_hand_delta=(line->>'quantity')::int AND m.reserved_delta=0 AND m.admin_user_id=NEW.operator_id)
      THEN RAISE EXCEPTION 'Shop parcel restock requires its exact once-only receipt ledger'; END IF;
      IF line->>'disposition'='not_restock' AND EXISTS(SELECT 1 FROM inventory_movements m
        WHERE m.reference_key='shop-carrier-return:'||NEW.id||':'||(line->>'order_item_id'))
      THEN RAISE EXCEPTION 'Non-restocked Shop parcel cannot add inventory'; END IF;
     END LOOP;
     RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_shop_carrier_receipt_ledger AFTER INSERT ON shop_carrier_return_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_shop_carrier_receipt_ledger()")
    op.execute("""CREATE FUNCTION protect_shop_return_shipment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     IF EXISTS(SELECT 1 FROM shop_carrier_returns WHERE return_shipment_id=OLD.id) AND
       ((to_jsonb(NEW)-ARRAY['status','raw_status','progress','tracking_number','last_checked_at','tracking_status','provider_event_at','last_attempt_at','last_attempt_status','tracking_failures','next_poll_at']) IS DISTINCT FROM
        (to_jsonb(OLD)-ARRAY['status','raw_status','progress','tracking_number','last_checked_at','tracking_status','provider_event_at','last_attempt_at','last_attempt_status','tracking_failures','next_poll_at'])
        OR OLD.tracking_number IS NOT NULL AND NEW.tracking_number IS DISTINCT FROM OLD.tracking_number
        OR NEW.tracking_number IS NOT NULL AND NEW.tracking_number!=NEW.provider_reference)
     THEN RAISE EXCEPTION 'Shop reverse shipment provenance is immutable'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_return_shipment BEFORE UPDATE ON order_shipments FOR EACH ROW EXECUTE FUNCTION protect_shop_return_shipment()")
    op.execute("""CREATE FUNCTION shop_unpaid_return_can_close(p_order int, p_case int) RETURNS boolean LANGUAGE sql AS $$
     SELECT EXISTS(SELECT 1 FROM shop_carrier_returns c JOIN shop_carrier_return_receipts r ON r.case_id=c.id
       JOIN shop_carrier_return_acceptances a ON a.case_id=c.id JOIN orders o ON o.id=c.order_id
       WHERE c.id=p_case AND c.order_id=p_order AND c.financial_mode='unpaid_cod' AND c.authority='test'
       AND c.status IN ('received','closed_unpaid') AND a.provider_uuid=c.provider_uuid AND a.snapshot_hash=c.snapshot_hash
       AND r.snapshot->>'case_hash'=c.snapshot_hash AND r.snapshot->>'authority'='test'
       AND o.channel='shop' AND NOT o.simulation AND o.fulfillment_method='nova_poshta'
       AND o.payment_method IN ('cod','cashOnDelivery','cardOnDelivery','cash_on_delivery','card_on_delivery')
       AND NOT EXISTS(SELECT 1 FROM order_payments WHERE order_id=p_order)
       AND NOT EXISTS(SELECT 1 FROM shop_payment_confirmations WHERE order_id=p_order)
       AND NOT EXISTS(SELECT 1 FROM shop_completions WHERE order_id=p_order)
       AND NOT EXISTS(SELECT 1 FROM shop_financial_returns WHERE order_id=p_order)
       AND NOT EXISTS(SELECT 1 FROM shop_fiscal_documents WHERE order_id=p_order)
       AND NOT EXISTS(SELECT 1 FROM fiscal_documents WHERE order_id=p_order)
       AND NOT EXISTS(SELECT 1 FROM qr_payment_attempts WHERE order_id=p_order)
       AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=p_order AND
        (NOT EXISTS(SELECT 1 FROM jsonb_array_elements(c.lines::jsonb) l WHERE (l->>'order_item_id')::int=i.id AND (l->>'quantity')::int=i.quantity)
         OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(r.lines::jsonb) l WHERE (l->>'order_item_id')::int=i.id AND (l->>'quantity')::int=i.quantity AND l->>'disposition' IN ('restock','not_restock'))
         OR NOT EXISTS(SELECT 1 FROM shipment_items s WHERE s.shipment_id=c.original_shipment_id AND s.order_item_id=i.id AND s.quantity=i.quantity)
         OR (SELECT coalesce(sum(-m.on_hand_delta),0) FROM inventory_movements m WHERE m.order_id=p_order AND m.order_item_id=i.id
              AND m.movement_type::text='shipment' AND m.reference_key='order:'||p_order||':'||'item'||':'||i.id||':'||'shipment')!=i.quantity
         OR (SELECT coalesce(sum(m.reserved_delta),0) FROM inventory_movements m WHERE m.order_id=p_order AND m.order_item_id=i.id)!=0)))
    $$""")
    op.execute("""CREATE FUNCTION validate_shop_unpaid_closure() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     IF NOT shop_unpaid_return_can_close(NEW.order_id,NEW.case_id)
       OR NOT EXISTS(SELECT 1 FROM orders WHERE id=NEW.order_id AND status::text='cancelled')
       OR NOT EXISTS(SELECT 1 FROM shop_carrier_return_receipts r JOIN shop_carrier_returns c ON c.id=r.case_id
          WHERE r.id=NEW.receipt_id AND r.case_id=NEW.case_id AND NEW.snapshot->>'receipt_hash'=r.snapshot_hash
          AND NEW.snapshot->>'case_hash'=c.snapshot_hash AND NEW.snapshot->>'authority'='test')
     THEN RAISE EXCEPTION 'Unpaid Shop closure requires full received parcel and no money or fiscal facts'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_shop_unpaid_closure AFTER INSERT ON shop_carrier_unpaid_closures DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_shop_unpaid_closure()")
    op.execute("""CREATE OR REPLACE FUNCTION protect_shop_order() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     IF EXISTS(SELECT 1 FROM shop_carrier_unpaid_closures WHERE order_id=OLD.id) AND NEW.status::text!='cancelled'
     THEN RAISE EXCEPTION 'Closed unpaid refusal cannot reopen'; END IF;
     IF EXISTS(SELECT 1 FROM shop_payment_confirmations WHERE order_id=OLD.id)
       OR EXISTS(SELECT 1 FROM shop_fulfillment_facts WHERE order_id=OLD.id) THEN
      IF NEW.channel IS DISTINCT FROM OLD.channel OR NEW.simulation IS DISTINCT FROM OLD.simulation
       OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method OR NEW.total_amount IS DISTINCT FROM OLD.total_amount
       OR NEW.status::text='cancelled' AND OLD.status::text!='cancelled' AND NOT EXISTS(
         SELECT 1 FROM shop_carrier_unpaid_closures c WHERE c.order_id=OLD.id AND shop_unpaid_return_can_close(OLD.id,c.case_id))
      THEN RAISE EXCEPTION 'Accepted Shop facts require explicit refund or return reconciliation'; END IF;
     END IF;
     RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION block_closed_shop_return_money() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     IF EXISTS(SELECT 1 FROM shop_carrier_unpaid_closures WHERE order_id=NEW.order_id)
     THEN RAISE EXCEPTION 'Closed unpaid refusal cannot receive financial facts'; END IF;
     RETURN NEW;
    END $$""")
    for table in ("order_payments", "shop_payment_confirmations", "shop_completions", "shop_fiscal_documents", "shop_financial_returns", "fiscal_documents"):
        op.execute(f"CREATE TRIGGER block_closed_shop_return_money BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION block_closed_shop_return_money()")
    op.execute("""CREATE FUNCTION validate_shop_financial_carrier_link() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE line jsonb; cap int; used int;
    BEGIN
     IF (SELECT fulfillment_method FROM orders WHERE id=NEW.order_id)='nova_poshta' THEN
      IF NOT EXISTS(SELECT 1 FROM shop_carrier_return_receipts r JOIN shop_carrier_returns c ON c.id=r.case_id
        WHERE r.id=NEW.carrier_receipt_id AND c.order_id=NEW.order_id AND c.financial_mode='paid' AND c.sale_document_id=NEW.sale_document_id
          AND c.shift_revision=NEW.shift_revision
          AND r.snapshot->>'case_hash'=c.snapshot_hash)
      THEN RAISE EXCEPTION 'NP financial return requires accepted Shop physical parcel receipt'; END IF;
      FOR line IN SELECT * FROM jsonb_array_elements(NEW.lines::jsonb) LOOP
       SELECT (x->>'quantity')::int INTO cap FROM shop_carrier_return_receipts r,
         jsonb_array_elements(r.lines::jsonb) x WHERE r.id=NEW.carrier_receipt_id AND x->>'order_item_id'=line->>'order_item_id';
       SELECT coalesce(sum((x->>'quantity')::int),0) INTO used FROM shop_financial_returns r,
         jsonb_array_elements(r.lines::jsonb) x WHERE r.carrier_receipt_id=NEW.carrier_receipt_id AND x->>'order_item_id'=line->>'order_item_id';
       IF cap IS NULL OR used+(line->>'quantity')::int>cap THEN RAISE EXCEPTION 'Shop received financial quantity exceeded'; END IF;
      END LOOP;
     ELSIF NEW.carrier_receipt_id IS NOT NULL THEN RAISE EXCEPTION 'Pickup cannot use a carrier receipt'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_financial_carrier_link BEFORE INSERT ON shop_financial_returns FOR EACH ROW EXECUTE FUNCTION validate_shop_financial_carrier_link()")
    op.execute("""CREATE FUNCTION validate_shop_fiscal_parcel_link() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE f shop_financial_returns%ROWTYPE; line jsonb; cap int; used int; disposition text;
    BEGIN
     SELECT * INTO f FROM shop_financial_returns WHERE id=NEW.return_id;
     IF f.carrier_receipt_id IS NOT NULL THEN
      IF NEW.carrier_receipt_id IS DISTINCT FROM f.carrier_receipt_id
        OR jsonb_array_length(NEW.lines::jsonb)!=jsonb_array_length(f.lines::jsonb)
        OR EXISTS(SELECT 1 FROM jsonb_array_elements(f.lines::jsonb) l WHERE NOT EXISTS(
          SELECT 1 FROM jsonb_array_elements(NEW.lines::jsonb) x WHERE x->>'order_item_id'=l->>'order_item_id' AND x->>'quantity'=l->>'quantity'))
      THEN RAISE EXCEPTION 'Shop financial receipt must link exact reserved parcel quantities'; END IF;
      FOR line IN SELECT * FROM jsonb_array_elements(NEW.lines::jsonb) LOOP
       SELECT (x->>'quantity')::int,x->>'disposition' INTO cap,disposition FROM shop_carrier_return_receipts r,
         jsonb_array_elements(r.lines::jsonb) x WHERE r.id=NEW.carrier_receipt_id AND x->>'order_item_id'=line->>'order_item_id';
       SELECT coalesce(sum((x->>'quantity')::int),0) INTO used FROM shop_return_receipts r,
         jsonb_array_elements(r.lines::jsonb) x WHERE r.carrier_receipt_id=NEW.carrier_receipt_id AND x->>'order_item_id'=line->>'order_item_id';
       IF cap IS NULL OR used+(line->>'quantity')::int>cap OR disposition IS DISTINCT FROM line->>'disposition'
       THEN RAISE EXCEPTION 'Shop parcel link quantity or disposition mismatch'; END IF;
      END LOOP;
     ELSIF NEW.carrier_receipt_id IS NOT NULL THEN RAISE EXCEPTION 'Pickup receipt cannot claim carrier provenance'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_fiscal_parcel_link BEFORE INSERT ON shop_return_receipts FOR EACH ROW EXECUTE FUNCTION validate_shop_fiscal_parcel_link()")


def downgrade():
    for table in ("shop_carrier_returns", "shop_carrier_return_acceptances", "shop_carrier_return_receipts", "shop_carrier_unpaid_closures"):
        if op.get_bind().execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Accepted Shop carrier return history exists; downgrade refused")
    op.execute(ORIGINAL_PROTECT)
    for table in ("order_payments", "shop_payment_confirmations", "shop_completions", "shop_fiscal_documents", "shop_financial_returns", "fiscal_documents"):
        op.execute(f"DROP TRIGGER block_closed_shop_return_money ON {table}")
    op.execute("DROP TRIGGER validate_shop_financial_carrier_link ON shop_financial_returns")
    op.execute("DROP TRIGGER validate_shop_fiscal_parcel_link ON shop_return_receipts")
    op.execute("DROP TRIGGER protect_shop_return_shipment ON order_shipments")
    for table in ("shop_financial_returns", "shop_return_receipts"):
        op.drop_constraint("fk_" + table + "_carrier_receipt", table, type_="foreignkey")
        op.drop_column(table, "carrier_receipt_id")
    for table in ("shop_carrier_unpaid_closures", "shop_carrier_return_receipts", "shop_carrier_return_acceptances", "shop_carrier_returns"):
        op.drop_table(table)
    for function in ("validate_shop_fiscal_parcel_link", "validate_shop_financial_carrier_link", "block_closed_shop_return_money", "validate_shop_unpaid_closure", "shop_unpaid_return_can_close(int,int)", "protect_shop_return_shipment", "validate_shop_carrier_receipt_ledger", "validate_shop_carrier_receipt", "validate_shop_carrier_acceptance", "validate_shop_carrier_case"):
        op.execute("DROP FUNCTION " + (function if "(" in function else function + "()"))
