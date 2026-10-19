"""Separate canonical Shop Checkbox profile and durable provider operations."""
from alembic import op
import sqlalchemy as sa

revision = "0125_shop_checkbox"
down_revision = "0124_shop_carrier_returns"
branch_labels = None
depends_on = None


def ref(name, target, *, nullable=False, unique=False, primary_key=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(target + ".id", ondelete="RESTRICT"),
        nullable=nullable, unique=unique, primary_key=primary_key)


def identifier():
    return sa.Column("id", sa.Integer(), primary_key=True)


def register_ref():
    return sa.Column("register_id", sa.String(100), sa.ForeignKey("shop_checkbox_registers.id", ondelete="RESTRICT"), nullable=False)


def snapshot():
    return [sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(64), nullable=False)]


def created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


TABLES = ("shop_checkbox_registers", "shop_checkbox_bindings", "shop_checkbox_operations", "shop_checkbox_acceptances",
    "shop_checkbox_returns", "shop_checkbox_previews", "shop_checkbox_documents", "shop_checkbox_refund_facts",
    "shop_checkbox_physical_receipts", "shop_checkbox_audits")


def upgrade():
    op.create_table("shop_checkbox_registers", sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("profile", sa.JSON(), nullable=False), sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False), sa.Column("transport", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("identity", sa.JSON(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True), sa.Column("shift_id", sa.String(36), nullable=True),
        sa.Column("shift_status", sa.String(16), nullable=False),
        sa.CheckConstraint("revision>0 AND shift_status IN ('open','closed','unknown') AND ((authority='test' AND transport='mock_http') OR (authority='provider' AND transport='live'))", name="ck_shop_checkbox_register"))
    op.create_index("uq_shop_checkbox_physical_register", "shop_checkbox_registers", [sa.text("(profile->>'register_id')")], unique=True)
    op.create_table("shop_checkbox_bindings", ref("order_id", "orders", primary_key=True), register_ref(),
        sa.Column("profile_hash", sa.String(64), nullable=False), sa.Column("authority", sa.String(16), nullable=False), created())
    op.create_table("shop_checkbox_operations", identifier(), register_ref(), ref("order_id", "orders", nullable=True),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("transport", sa.String(16), nullable=False), sa.Column("provider_uuid", sa.String(36), nullable=True, unique=True),
        sa.Column("shift_id", sa.String(36), nullable=True), *snapshot(), ref("operator_id", "admin_users"),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True), created(),
        sa.CheckConstraint("kind IN ('verify','shift_open','shift_close','x','z','sale','return') AND revision>0 AND status IN ('queued','unknown','pending','accepted','rejected','cancelled')", name="ck_shop_checkbox_operation"))
    op.create_index("uq_shop_checkbox_pending_register", "shop_checkbox_operations", ["register_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued','unknown','pending')"))
    op.create_table("shop_checkbox_acceptances", ref("operation_id", "shop_checkbox_operations", primary_key=True),
        sa.Column("provider_id", sa.String(100), nullable=False, unique=True), sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("proof", sa.JSON(), nullable=False), sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("proof_canonical", sa.Text(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("shop_checkbox_returns", identifier(), ref("order_id", "orders"),
        sa.Column("sale_document_id", sa.Integer(), nullable=False), register_ref(), sa.Column("shift_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False), sa.Column("reason", sa.String(500), nullable=False),
        ref("operator_id", "admin_users"), created(), sa.CheckConstraint("revision>0 AND amount_minor>0 AND btrim(reason)!=''", name="ck_shop_checkbox_return"))
    op.create_table("shop_checkbox_previews", identifier(), ref("order_id", "orders"), ref("return_id", "shop_checkbox_returns", nullable=True),
        register_ref(), ref("actor_id", "admin_users"), sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False), sa.Column("register_revision", sa.Integer(), nullable=False),
        *snapshot(), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("shop_checkbox_documents", identifier(), ref("order_id", "orders"), ref("payment_confirmation_id", "shop_payment_confirmations"),
        ref("return_id", "shop_checkbox_returns", nullable=True), ref("preview_id", "shop_checkbox_previews", unique=True),
        ref("operation_id", "shop_checkbox_operations", unique=True), sa.Column("kind", sa.String(16), nullable=False),
        sa.CheckConstraint("(kind='sale' AND return_id IS NULL) OR (kind='return' AND return_id IS NOT NULL)", name="ck_shop_checkbox_document"))
    op.create_foreign_key("fk_shop_checkbox_return_original", "shop_checkbox_returns", "shop_checkbox_documents", ["sale_document_id"], ["id"], ondelete="RESTRICT")
    op.create_table("shop_checkbox_refund_facts", identifier(), ref("return_id", "shop_checkbox_returns", unique=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False), sa.Column("method", sa.String(32), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False), sa.Column("external_reference", sa.String(200), nullable=False, unique=True),
        sa.Column("evidence", sa.String(500), nullable=False), ref("operator_id", "admin_users"), created(),
        sa.CheckConstraint("amount_minor>0 AND btrim(external_reference)!='' AND btrim(evidence)!='' AND authority IN ('manual','test')", name="ck_shop_checkbox_refund"))
    op.create_table("shop_checkbox_physical_receipts", identifier(), ref("return_id", "shop_checkbox_returns", unique=True),
        sa.Column("lines", sa.JSON(), nullable=False), sa.Column("evidence", sa.String(500), nullable=False), ref("operator_id", "admin_users"), created())
    op.create_table("shop_checkbox_audits", identifier(), ref("operator_id", "admin_users"), sa.Column("operation", sa.String(40), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False), created())
    for table in ("shop_checkbox_bindings", "shop_checkbox_previews", "shop_checkbox_documents", "shop_checkbox_acceptances",
                  "shop_checkbox_refund_facts", "shop_checkbox_physical_receipts", "shop_checkbox_audits"):
        op.execute(f"CREATE TRIGGER immutable_shop_checkbox BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION protect_shop_checkbox_register() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF (to_jsonb(NEW)-ARRAY['revision','identity','verified_at','shift_id','shift_status']) IS DISTINCT FROM
      (to_jsonb(OLD)-ARRAY['revision','identity','verified_at','shift_id','shift_status'])
      THEN RAISE EXCEPTION 'Checkbox register profile is immutable'; END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER protect_shop_checkbox_register BEFORE UPDATE ON shop_checkbox_registers FOR EACH ROW EXECUTE FUNCTION protect_shop_checkbox_register()")
    op.execute("""CREATE FUNCTION validate_shop_checkbox_binding() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF NOT EXISTS(SELECT 1 FROM orders o JOIN shop_checkbox_registers r ON r.id=NEW.register_id
      WHERE o.id=NEW.order_id AND o.channel='shop' AND NOT o.simulation
      AND r.profile_hash=NEW.profile_hash AND r.authority=NEW.authority)
      OR EXISTS(SELECT 1 FROM shop_fiscal_documents WHERE order_id=NEW.order_id)
      OR EXISTS(SELECT 1 FROM shop_carrier_returns WHERE order_id=NEW.order_id)
      OR EXISTS(SELECT 1 FROM shop_payment_confirmations WHERE order_id=NEW.order_id)
      OR EXISTS(SELECT 1 FROM shop_fulfillment_facts WHERE order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Checkbox binding requires exclusive canonical Shop history'; END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER validate_shop_checkbox_binding BEFORE INSERT ON shop_checkbox_bindings FOR EACH ROW EXECUTE FUNCTION validate_shop_checkbox_binding()")
    op.execute("""CREATE FUNCTION lock_shop_checkbox_money() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target text; BEGIN
      SELECT register_id INTO target FROM shop_checkbox_bindings WHERE order_id=NEW.order_id;
      IF target IS NOT NULL THEN
       PERFORM 1 FROM shop_checkbox_registers WHERE id=target FOR UPDATE;
       IF EXISTS(SELECT 1 FROM shop_checkbox_operations WHERE register_id=target AND status IN ('queued','unknown','pending'))
       THEN RAISE EXCEPTION 'Checkbox register has an unresolved durable operation'; END IF;
      END IF; RETURN NEW; END $$""")
    # Runs before the pre-existing mock register trigger; raw and service lock order agree.
    op.execute("CREATE TRIGGER aa_lock_shop_checkbox_money BEFORE INSERT ON shop_payment_confirmations FOR EACH ROW EXECUTE FUNCTION lock_shop_checkbox_money()")
    op.execute("""CREATE FUNCTION protect_shop_checkbox_operation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
     PERFORM 1 FROM shop_checkbox_registers WHERE id=NEW.register_id FOR UPDATE;
     IF TG_OP='UPDATE' AND ((to_jsonb(NEW)-ARRAY['status','revision','dispatched_at']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','revision','dispatched_at']) OR OLD.status IN ('accepted','rejected','cancelled')
       OR OLD.dispatched_at IS NOT NULL AND NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at
       OR OLD.dispatched_at IS NOT NULL AND NEW.status IN ('queued','cancelled'))
      THEN RAISE EXCEPTION 'Checkbox dispatch and approval identity are immutable'; END IF;
     IF TG_OP='INSERT' AND (NEW.status!='queued' OR NEW.dispatched_at IS NOT NULL)
      THEN RAISE EXCEPTION 'Checkbox operations must start undispatched'; END IF;
     IF NOT EXISTS(SELECT 1 FROM shop_checkbox_registers r WHERE r.id=NEW.register_id
      AND r.authority=NEW.authority AND r.transport=NEW.transport AND r.profile_hash=NEW.snapshot->>'profile_hash'
      AND r.id=NEW.snapshot->>'profile_id' AND r.profile->>'organization_id'=NEW.snapshot->>'organization_id'
      AND r.profile->>'register_id'=NEW.snapshot->>'register_id' AND r.profile->>'cashier_id'=NEW.snapshot->>'cashier_id')
      THEN RAISE EXCEPTION 'Checkbox operation requires exact immutable profile'; END IF;
     IF NEW.status='accepted' AND NOT EXISTS(SELECT 1 FROM shop_checkbox_acceptances a
      WHERE a.operation_id=NEW.id AND a.snapshot_hash=NEW.snapshot_hash)
      THEN RAISE EXCEPTION 'Checkbox accepted operation requires immutable proof'; END IF;
     IF NEW.status='rejected' AND NOT EXISTS(SELECT 1 FROM shop_checkbox_audits a
       WHERE a.facts->>'operation_id'=NEW.id::text AND
       (a.operation='provider_no_dispatch' AND a.facts->>'business_request_dispatched'='false'
        OR a.operation='provider_rejection' AND NEW.kind IN ('sale','return')
          AND a.facts->>'provider_uuid'=NEW.provider_uuid AND a.facts->>'snapshot_hash'=NEW.snapshot_hash
          AND a.facts->>'proof_hash'=encode(sha256(convert_to(a.facts->>'proof_canonical','UTF8')),'hex')
          AND (a.facts->>'proof_canonical')::jsonb=(a.facts->'proof')::jsonb
          AND a.facts->'proof'->>'rejected'='true' AND a.facts->'proof'->>'is_sent_dps'='false'
          AND a.facts->'proof'->>'resource'='receipt' AND a.facts->'proof'->>'status'='ERROR'
          AND a.facts->'proof'->>'verified'='true' AND a.facts->'proof'->>'accepted'='false'
          AND a.facts->'proof'->>'id'=NEW.provider_uuid AND a.facts->'proof'->>'snapshot_hash'=NEW.snapshot_hash
          AND a.facts->'proof'->>'kind'=NEW.kind AND a.facts->'proof'->>'shift_id'=NEW.shift_id
          AND a.facts->'proof'->>'amount_minor'=NEW.snapshot->>'amount_minor'
          AND a.facts->'proof'->>'currency'='UAH'
          AND (a.facts->'proof'->'related_receipt_id')::jsonb IS NOT DISTINCT FROM (NEW.snapshot->'related_receipt_id')::jsonb
          AND a.facts->'proof'->>'authority'=NEW.authority AND a.facts->'proof'->>'transport'=NEW.transport
          AND a.facts->'proof'->>'organization_id'=NEW.snapshot->>'organization_id'
          AND a.facts->'proof'->>'register_id'=NEW.snapshot->>'register_id'
          AND a.facts->'proof'->>'cashier_id'=NEW.snapshot->>'cashier_id'
          AND a.facts->'proof'->>'is_created_offline'='false' AND a.facts->'proof'->>'transaction_status'='ERROR'
          AND COALESCE(a.facts->'proof'->>'fiscal_code','')=''
          AND a.facts->'proof'->>'transaction_offline'='false' AND a.facts->'proof'->>'transaction_response_accepted'='false'))
       THEN RAISE EXCEPTION 'Checkbox rejected operation requires durable no-effect evidence'; END IF;
     RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_checkbox_operation BEFORE INSERT OR UPDATE ON shop_checkbox_operations FOR EACH ROW EXECUTE FUNCTION protect_shop_checkbox_operation()")
    op.execute("CREATE TRIGGER immutable_shop_checkbox_operation BEFORE DELETE ON shop_checkbox_operations FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION validate_shop_checkbox_acceptance() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF NOT EXISTS(SELECT 1 FROM shop_checkbox_operations o JOIN shop_checkbox_registers r ON r.id=o.register_id
      WHERE o.id=NEW.operation_id AND o.dispatched_at IS NOT NULL AND o.status IN ('unknown','pending')
      AND o.snapshot_hash=NEW.snapshot_hash AND NEW.proof->>'verified'='true' AND NEW.proof->>'accepted'='true'
      AND NEW.proof_hash=encode(sha256(convert_to(NEW.proof_canonical,'UTF8')),'hex')
      AND NEW.proof_canonical::jsonb=NEW.proof::jsonb
      AND NEW.proof->>'authority'=o.authority AND NEW.proof->>'transport'=o.transport
      AND NEW.proof->>'organization_id'=r.profile->>'organization_id' AND NEW.proof->>'register_id'=r.profile->>'register_id'
      AND NEW.proof->>'cashier_id'=r.profile->>'cashier_id'
      AND (o.kind NOT IN ('sale','return') OR NEW.proof->>'id'=o.provider_uuid AND NEW.proof->>'snapshot_hash'=o.snapshot_hash
       AND NEW.proof->>'shift_id'=o.shift_id AND NEW.proof->>'amount_minor'=o.snapshot->>'amount_minor'
       AND NEW.proof->>'kind'=o.kind AND NEW.proof->>'currency'='UAH'
       AND NEW.proof->>'is_sent_dps'=CASE WHEN o.authority='test' THEN 'false' ELSE 'true' END))
      THEN RAISE EXCEPTION 'Checkbox acceptance does not bind its original dispatch'; END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER validate_shop_checkbox_acceptance BEFORE INSERT ON shop_checkbox_acceptances FOR EACH ROW EXECUTE FUNCTION validate_shop_checkbox_acceptance()")
    op.execute("""CREATE FUNCTION validate_shop_checkbox_document() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF NOT EXISTS(SELECT 1 FROM shop_checkbox_operations o JOIN shop_checkbox_previews v ON v.id=NEW.preview_id
      JOIN shop_payment_confirmations p ON p.id=NEW.payment_confirmation_id
      JOIN shop_checkbox_bindings b ON b.order_id=p.order_id
      WHERE o.id=NEW.operation_id AND o.order_id=NEW.order_id AND p.order_id=NEW.order_id AND o.kind=NEW.kind
      AND b.register_id=o.register_id AND o.status='queued' AND o.snapshot_hash=v.snapshot_hash
      AND o.snapshot::jsonb=v.snapshot::jsonb AND v.order_id=NEW.order_id AND v.register_id=o.register_id
      AND v.return_id IS NOT DISTINCT FROM NEW.return_id AND o.snapshot->>'payment_confirmation_id'=p.id::text
      AND (NEW.kind!='sale' OR o.snapshot->>'amount_minor'=p.snapshot->>'amount_minor'))
      THEN RAISE EXCEPTION 'Checkbox receipt requires exact canonical payment and approval'; END IF;
      IF NEW.kind='return' AND NOT EXISTS(SELECT 1 FROM shop_checkbox_returns r
       JOIN shop_checkbox_refund_facts f ON f.return_id=r.id JOIN shop_checkbox_physical_receipts h ON h.return_id=r.id
       JOIN shop_checkbox_operations o ON o.id=NEW.operation_id
       WHERE r.id=NEW.return_id AND r.order_id=NEW.order_id AND o.snapshot->>'amount_minor'=r.amount_minor::text
       AND f.amount_minor=r.amount_minor AND o.snapshot->>'return_id'=r.id::text AND (o.snapshot->'items')::jsonb=r.lines::jsonb)
      THEN RAISE EXCEPTION 'Checkbox return receipt requires exact return, refund and physical receipt'; END IF;
      RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER validate_shop_checkbox_document BEFORE INSERT ON shop_checkbox_documents FOR EACH ROW EXECUTE FUNCTION validate_shop_checkbox_document()")
    op.execute("""CREATE FUNCTION protect_shop_checkbox_return() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE sale_state jsonb; line_data jsonb; original jsonb; used bigint; expected numeric;
    BEGIN IF TG_OP='UPDATE' AND (to_jsonb(NEW)-'revision') IS DISTINCT FROM (to_jsonb(OLD)-'revision')
      THEN RAISE EXCEPTION 'Checkbox return allocation is immutable'; END IF;
      IF TG_OP='INSERT' THEN
       PERFORM 1 FROM shop_checkbox_registers WHERE id=NEW.register_id FOR UPDATE;
       IF NOT EXISTS(SELECT 1 FROM shop_checkbox_documents d JOIN shop_checkbox_operations o ON o.id=d.operation_id
        JOIN orders s ON s.id=d.order_id JOIN shop_fulfillment_facts h ON h.order_id=s.id
        JOIN shop_checkbox_registers r ON r.id=NEW.register_id
        WHERE d.id=NEW.sale_document_id AND d.order_id=NEW.order_id AND d.kind='sale' AND o.status='accepted'
        AND o.register_id=NEW.register_id AND o.authority=r.authority AND o.transport=r.transport
        AND o.snapshot->>'profile_hash'=r.profile_hash
        AND EXISTS(SELECT 1 FROM shop_checkbox_bindings b WHERE b.order_id=NEW.order_id AND b.register_id=r.id
          AND b.profile_hash=r.profile_hash AND b.authority=r.authority)
        AND h.kind='pickup' AND s.fulfillment_method='pickup' AND r.shift_status='open' AND r.shift_id=NEW.shift_id
        AND EXISTS(SELECT 1 FROM shop_checkbox_acceptances a WHERE a.operation_id=o.id AND a.snapshot_hash=o.snapshot_hash))
        THEN RAISE EXCEPTION 'Checkbox return requires issued sale and canonical pickup handover'; END IF;
       IF jsonb_typeof(NEW.lines::jsonb)!='array' OR jsonb_array_length(NEW.lines::jsonb)=0
        OR (SELECT count(DISTINCT v->>'order_item_id') FROM jsonb_array_elements(NEW.lines::jsonb) v)!=jsonb_array_length(NEW.lines::jsonb)
        OR NEW.amount_minor!=(SELECT COALESCE(sum((v->>'total_minor')::bigint),0) FROM jsonb_array_elements(NEW.lines::jsonb) v)
        OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.lines::jsonb) l LEFT JOIN order_items i ON i.id=(l->>'order_item_id')::int
         WHERE i.id IS NULL OR i.order_id!=NEW.order_id OR i.product_id!=(l->>'product_id')::int OR (l->>'quantity')::int<=0
         OR (l->>'quantity')::int+COALESCE((SELECT sum((x->>'quantity')::int) FROM shop_checkbox_returns q,
           jsonb_array_elements(q.lines::jsonb) x WHERE q.order_id=NEW.order_id AND x->>'order_item_id'=l->>'order_item_id'),0)>i.quantity)
        THEN RAISE EXCEPTION 'Checkbox return allocation exceeds canonical order'; END IF;
       SELECT o.snapshot::jsonb INTO sale_state FROM shop_checkbox_documents d JOIN shop_checkbox_operations o ON o.id=d.operation_id
        WHERE d.id=NEW.sale_document_id;
       FOR line_data IN SELECT value FROM jsonb_array_elements(NEW.lines::jsonb) LOOP
        SELECT value INTO original FROM jsonb_array_elements(sale_state->'items') WHERE value->>'order_item_id'=line_data->>'order_item_id';
        SELECT COALESCE(sum((v->>'quantity')::bigint),0) INTO used FROM shop_checkbox_returns q,
          jsonb_array_elements(q.lines::jsonb) v WHERE q.order_id=NEW.order_id AND v->>'order_item_id'=line_data->>'order_item_id';
        IF original IS NULL OR jsonb_typeof(line_data->'quantity') IS DISTINCT FROM 'number'
          OR jsonb_typeof(line_data->'total_minor') IS DISTINCT FROM 'number'
          OR (line_data-ARRAY['quantity','total_minor']) IS DISTINCT FROM (original-ARRAY['quantity','total_minor'])
          OR (line_data->>'quantity')::bigint<=0 OR used+(line_data->>'quantity')::bigint>(original->>'quantity')::bigint
          THEN RAISE EXCEPTION 'Checkbox return must preserve exact original discounted goods and tax snapshot'; END IF;
        expected=floor((original->>'total_minor')::numeric*(used+(line_data->>'quantity')::bigint)/(original->>'quantity')::bigint)
          -floor((original->>'total_minor')::numeric*used/(original->>'quantity')::bigint);
        IF (line_data->>'total_minor')::bigint!=expected THEN RAISE EXCEPTION 'Checkbox return requires exact cumulative original kopeck allocation'; END IF;
       END LOOP;
      END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER protect_shop_checkbox_return BEFORE INSERT OR UPDATE ON shop_checkbox_returns FOR EACH ROW EXECUTE FUNCTION protect_shop_checkbox_return()")
    op.execute("CREATE TRIGGER immutable_shop_checkbox_return BEFORE DELETE ON shop_checkbox_returns FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION validate_shop_checkbox_refund() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF NOT EXISTS(SELECT 1 FROM shop_checkbox_returns q JOIN shop_checkbox_registers r ON r.id=q.register_id
      WHERE q.id=NEW.return_id AND q.amount_minor=NEW.amount_minor AND r.shift_status='open' AND r.shift_id=q.shift_id
      AND NEW.authority=CASE WHEN r.authority='test' THEN 'test' ELSE 'manual' END)
      THEN RAISE EXCEPTION 'Checkbox staff refund requires its exact reserved amount and shift'; END IF; RETURN NEW; END $$""")
    op.execute("CREATE TRIGGER validate_shop_checkbox_refund BEFORE INSERT ON shop_checkbox_refund_facts FOR EACH ROW EXECUTE FUNCTION validate_shop_checkbox_refund()")
    op.execute("""CREATE FUNCTION validate_shop_checkbox_physical() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE q shop_checkbox_returns%ROWTYPE; l jsonb; original jsonb; shipped bigint; movement_count bigint;
    BEGIN
     SELECT * INTO q FROM shop_checkbox_returns WHERE id=NEW.return_id;
     PERFORM 1 FROM shop_checkbox_registers WHERE id=q.register_id FOR UPDATE;
     IF jsonb_array_length(NEW.lines::jsonb)!=jsonb_array_length(q.lines::jsonb)
       OR (SELECT count(DISTINCT v->>'order_item_id') FROM jsonb_array_elements(NEW.lines::jsonb) v)!=jsonb_array_length(q.lines::jsonb)
       OR NOT EXISTS(SELECT 1 FROM orders o JOIN shop_fulfillment_facts h ON h.order_id=o.id
         WHERE o.id=q.order_id AND o.fulfillment_method='pickup' AND h.kind='pickup')
       THEN RAISE EXCEPTION 'Checkbox physical receipt requires exact original pickup quantities'; END IF;
     FOR l IN SELECT value FROM jsonb_array_elements(NEW.lines::jsonb) LOOP
      SELECT value INTO original FROM jsonb_array_elements(q.lines::jsonb) WHERE value->>'order_item_id'=l->>'order_item_id';
      IF original IS NULL OR jsonb_typeof(l->'quantity') IS DISTINCT FROM 'number'
       OR l->>'quantity' IS DISTINCT FROM original->>'quantity'
       OR NOT COALESCE(l->>'disposition' IN ('restock','not_restock'),false)
       THEN RAISE EXCEPTION 'Checkbox physical receipt allocation or disposition mismatch'; END IF;
      SELECT COALESCE(sum(-on_hand_delta),0) INTO shipped FROM inventory_movements
       WHERE order_id=q.order_id AND order_item_id=(l->>'order_item_id')::int
       AND product_id=(original->>'product_id')::int AND movement_type::text='shipment'
       AND reference_key='order:'||q.order_id::text||':item:'||(l->>'order_item_id')||':'||'shipment';
      IF shipped<(l->>'quantity')::int THEN RAISE EXCEPTION 'Checkbox return lacks canonical shipment ledger'; END IF;
      SELECT count(*) INTO movement_count FROM inventory_movements
       WHERE reference_key='shop-checkbox-return:'||q.id::text||':'||(l->>'order_item_id');
      IF l->>'disposition'='restock' AND (movement_count!=1 OR NOT EXISTS(SELECT 1 FROM inventory_movements
       WHERE reference_key='shop-checkbox-return:'||q.id::text||':'||(l->>'order_item_id') AND order_id=q.order_id
       AND order_item_id=(l->>'order_item_id')::int AND product_id=(original->>'product_id')::int
       AND movement_type::text='customer_return' AND quantity=(l->>'quantity')::int
       AND on_hand_delta=(l->>'quantity')::int AND reserved_delta=0 AND admin_user_id=NEW.operator_id))
       OR l->>'disposition'='not_restock' AND movement_count!=0
       THEN RAISE EXCEPTION 'Checkbox physical receipt requires its exact once-only restock ledger'; END IF;
     END LOOP; RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_shop_checkbox_physical AFTER INSERT ON shop_checkbox_physical_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_shop_checkbox_physical()")
    op.execute("""CREATE FUNCTION block_shop_checkbox_mixing() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF EXISTS(SELECT 1 FROM shop_checkbox_bindings WHERE order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Checkbox-bound orders cannot use local B2/B3 facts'; END IF; RETURN NEW; END $$""")
    for table in ("shop_fiscal_previews", "shop_fiscal_documents", "shop_financial_returns", "shop_carrier_returns"):
        op.execute(f"CREATE TRIGGER block_shop_checkbox_mixing BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION block_shop_checkbox_mixing()")


def downgrade():
    bind = op.get_bind()
    if any(bind.execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar() for table in TABLES):
        raise RuntimeError("Accepted Shop Checkbox history prevents downgrade")
    for table in ("shop_fiscal_previews", "shop_fiscal_documents", "shop_financial_returns", "shop_carrier_returns"):
        op.execute(f"DROP TRIGGER block_shop_checkbox_mixing ON {table}")
    op.execute("DROP TRIGGER aa_lock_shop_checkbox_money ON shop_payment_confirmations")
    op.drop_constraint("fk_shop_checkbox_return_original", "shop_checkbox_returns", type_="foreignkey")
    for table in reversed(TABLES):
        op.drop_table(table)
    for name in ("protect_shop_checkbox_register", "validate_shop_checkbox_binding", "lock_shop_checkbox_money",
                 "protect_shop_checkbox_operation", "validate_shop_checkbox_acceptance", "validate_shop_checkbox_document",
                 "protect_shop_checkbox_return", "validate_shop_checkbox_refund", "validate_shop_checkbox_physical", "block_shop_checkbox_mixing"):
        op.execute(f"DROP FUNCTION {name}()")
