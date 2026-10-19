"""Add explicit Shop operations without enabling checkout or external adapters."""
from alembic import op
import sqlalchemy as sa

revision = "0122_shop_order_operations"
down_revision = "0121_mock_cod_settlement"
branch_labels = None
depends_on = None


def reference(name, table, *, unique=False, nullable=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(table + ".id", ondelete="RESTRICT"), unique=unique, nullable=nullable)


def audit_columns():
    return [reference("operator_id", "admin_users"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]


def snapshot_columns():
    return [sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(64), nullable=False)]


def upgrade():
    op.create_table("shop_payment_confirmations", sa.Column("id", sa.Integer(), primary_key=True),
        reference("order_id", "orders", unique=True), reference("payment_id", "order_payments", unique=True),
        sa.Column("namespace", sa.String(128), nullable=False), sa.Column("external_reference", sa.String(200), nullable=False),
        sa.Column("source", sa.String(16), nullable=False), sa.Column("authority", sa.String(16), nullable=False),
        *snapshot_columns(), *audit_columns(),
        sa.UniqueConstraint("namespace", "external_reference", name="uq_shop_payment_external_reference"))
    op.create_table("shop_fulfillment_facts", sa.Column("id", sa.Integer(), primary_key=True),
        reference("order_id", "orders", unique=True), reference("handover_id", "order_handovers", unique=True),
        reference("shipment_id", "order_shipments", unique=True, nullable=True),
        sa.Column("kind", sa.String(16), nullable=False), sa.Column("authority", sa.String(16), nullable=False),
        *snapshot_columns(), *audit_columns())
    op.create_table("shop_completions", sa.Column("id", sa.Integer(), primary_key=True),
        reference("order_id", "orders", unique=True), reference("payment_confirmation_id", "shop_payment_confirmations"),
        reference("fulfillment_id", "shop_fulfillment_facts"), sa.Column("authority", sa.String(16), nullable=False),
        *snapshot_columns(), *audit_columns())
    op.create_table("shop_operation_audits", sa.Column("id", sa.Integer(), primary_key=True),
        reference("order_id", "orders"), *audit_columns(), sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False, unique=True), sa.Column("facts", sa.JSON(), nullable=False))
    for table in ("shop_payment_confirmations", "shop_fulfillment_facts", "shop_completions", "shop_operation_audits"):
        op.execute(f"CREATE TRIGGER immutable_shop_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION validate_shop_fact() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM 1 FROM orders WHERE id=NEW.order_id FOR UPDATE;
      IF NOT EXISTS (SELECT 1 FROM orders WHERE id=NEW.order_id AND channel='shop' AND NOT simulation AND total_amount>0)
      THEN RAISE EXCEPTION 'Shop facts require a positive non-simulated Shop order'; END IF;
      IF TG_TABLE_NAME='shop_payment_confirmations' THEN
        IF NEW.source NOT IN ('manual','provider') OR NEW.authority NOT IN ('manual','provider','test')
          OR NEW.source='manual' AND NEW.authority!='manual' OR NEW.source='provider' AND NEW.authority NOT IN ('provider','test')
          OR btrim(NEW.namespace)='' OR btrim(NEW.external_reference)=''
          OR NOT EXISTS (SELECT 1 FROM order_payments p JOIN orders o ON o.id=p.order_id
            WHERE p.id=NEW.payment_id AND p.order_id=NEW.order_id AND NOT p.simulation
            AND p.amount_minor=o.total_amount*100 AND p.source='shop_'||NEW.source AND p.evidence=NEW.external_reference
            AND p.operator_id=NEW.operator_id AND NEW.snapshot->>'order_id'=NEW.order_id::text
            AND NEW.snapshot->>'payment_id'=p.id::text AND NEW.snapshot->>'amount_minor'=p.amount_minor::text
            AND NEW.snapshot->>'currency'='UAH' AND NEW.snapshot->>'method'=p.method
            AND NEW.snapshot->>'source'=NEW.source AND NEW.snapshot->>'authority'=NEW.authority
            AND NEW.snapshot->>'namespace'=NEW.namespace AND NEW.snapshot->>'external_reference'=NEW.external_reference)
        THEN RAISE EXCEPTION 'Shop payment provenance mismatch'; END IF;
      ELSIF TG_TABLE_NAME='shop_fulfillment_facts' THEN
        IF NEW.kind NOT IN ('pickup','nova_poshta') OR NEW.authority NOT IN ('production','test')
          OR NOT EXISTS (SELECT 1 FROM order_handovers h JOIN orders o ON o.id=h.order_id
            WHERE h.id=NEW.handover_id AND h.order_id=NEW.order_id AND NOT h.simulation AND h.operator_id=NEW.operator_id
            AND o.fulfillment_method=NEW.kind AND NEW.snapshot->>'order_id'=NEW.order_id::text
            AND NEW.snapshot->>'handover_id'=h.id::text AND NEW.snapshot->>'kind'=NEW.kind
            AND NEW.snapshot->>'authority'=NEW.authority)
          OR NEW.kind='pickup' AND NEW.shipment_id IS NOT NULL
          OR NEW.kind='nova_poshta' AND NOT EXISTS (SELECT 1 FROM order_shipments s WHERE s.id=NEW.shipment_id
            AND s.order_id=NEW.order_id AND NOT s.simulation AND s.carrier='nova_poshta' AND s.source='attach'
            AND s.return_of_shipment_id IS NULL AND s.handed_over_at IS NOT NULL)
        THEN RAISE EXCEPTION 'Shop handover provenance mismatch'; END IF;
      ELSIF TG_TABLE_NAME='shop_completions' THEN
        IF NEW.authority NOT IN ('production','test') OR NOT EXISTS
          (SELECT 1 FROM shop_payment_confirmations p JOIN shop_fulfillment_facts f ON f.order_id=p.order_id
            JOIN orders o ON o.id=p.order_id WHERE p.id=NEW.payment_confirmation_id AND f.id=NEW.fulfillment_id
            AND p.order_id=NEW.order_id AND o.status::text='completed'
            AND NEW.snapshot->>'order_id'=NEW.order_id::text
            AND NEW.snapshot->>'payment_confirmation_id'=p.id::text AND NEW.snapshot->>'fulfillment_id'=f.id::text
            AND NEW.snapshot->>'authority'=NEW.authority
            AND (NEW.authority='test' OR p.authority!='test' AND f.authority!='test'))
        THEN RAISE EXCEPTION 'Shop completion provenance mismatch'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    for table in ("shop_payment_confirmations", "shop_fulfillment_facts", "shop_completions"):
        op.execute(f"CREATE CONSTRAINT TRIGGER validate_shop_fact AFTER INSERT ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_shop_fact()")
    op.execute("""CREATE FUNCTION validate_shop_payment_projection() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.source IN ('shop_manual','shop_provider') AND NOT EXISTS
        (SELECT 1 FROM shop_payment_confirmations p WHERE p.payment_id=NEW.id AND p.order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Shop payment requires immutable confirmation provenance'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE CONSTRAINT TRIGGER validate_shop_payment_projection AFTER INSERT ON order_payments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_shop_payment_projection()")
    op.execute("""CREATE FUNCTION protect_shop_order() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM shop_payment_confirmations WHERE order_id=OLD.id)
        OR EXISTS(SELECT 1 FROM shop_fulfillment_facts WHERE order_id=OLD.id) THEN
        IF NEW.channel IS DISTINCT FROM OLD.channel OR NEW.simulation IS DISTINCT FROM OLD.simulation
          OR NEW.fulfillment_method IS DISTINCT FROM OLD.fulfillment_method OR NEW.total_amount IS DISTINCT FROM OLD.total_amount
          OR NEW.status::text='cancelled' AND OLD.status::text!='cancelled'
        THEN RAISE EXCEPTION 'Accepted Shop facts require explicit refund or return reconciliation'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_order BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION protect_shop_order()")
    # Preserve the mock guard; real COD has its own immutable provenance chain.
    op.execute("""CREATE OR REPLACE FUNCTION validate_mock_cod_payment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.method='cod' OR NEW.source='mock_cod_confirmation') AND NOT EXISTS
        (SELECT 1 FROM mock_cod_confirmations c WHERE c.payment_id=NEW.id AND c.order_id=NEW.order_id)
      THEN
        IF NEW.simulation OR NEW.source NOT IN ('shop_manual','shop_provider') OR NOT EXISTS
          (SELECT 1 FROM shop_payment_confirmations p JOIN orders o ON o.id=p.order_id
            JOIN shop_fulfillment_facts f ON f.order_id=o.id
            JOIN order_handovers h ON h.id=f.handover_id AND h.order_id=o.id
            JOIN order_shipments s ON s.id=f.shipment_id AND s.order_id=o.id
            WHERE p.payment_id=NEW.id AND p.order_id=NEW.order_id AND o.channel='shop' AND NOT o.simulation
              AND NEW.method='cod' AND NEW.amount_minor=o.total_amount*100 AND NEW.operator_id=p.operator_id
              AND NEW.source='shop_'||p.source AND NEW.evidence=p.external_reference
              AND p.source IN ('manual','provider') AND p.authority IN ('manual','provider','test')
              AND p.snapshot->>'order_id'=o.id::text AND p.snapshot->>'payment_id'=NEW.id::text
              AND p.snapshot->>'amount_minor'=NEW.amount_minor::text AND p.snapshot->>'method'='cod'
              AND p.snapshot->>'currency'='UAH' AND p.snapshot->>'external_reference'=p.external_reference
              AND p.snapshot->>'authority'=p.authority AND p.snapshot->>'source'=p.source
              AND f.kind='nova_poshta' AND f.authority IN ('production','test') AND NOT h.simulation
              AND NOT s.simulation AND s.carrier='nova_poshta' AND s.source='attach' AND s.active
              AND s.return_of_shipment_id IS NULL AND s.handed_over_at IS NOT NULL
              AND s.tracking_number=s.provider_reference AND s.last_attempt_status='success' AND s.tracking_status='delivered'
              AND EXISTS(SELECT 1 FROM shipment_tracking_events e WHERE e.shipment_id=s.id
                AND e.normalized_status='delivered' AND e.event_at IS NOT NULL AND e.event_at=s.provider_event_at
                AND e.raw_status=s.raw_status AND e.observed_at<=s.last_checked_at)
              AND NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=o.id AND NOT EXISTS
                (SELECT 1 FROM shipment_items a WHERE a.shipment_id=s.id AND a.order_item_id=i.id AND a.quantity=i.quantity))
              AND NOT EXISTS(SELECT 1 FROM shipment_items a JOIN order_items i ON i.id=a.order_item_id
                WHERE a.shipment_id=s.id AND (i.order_id!=o.id OR a.quantity!=i.quantity)))
        THEN RAISE EXCEPTION 'Mock COD payment requires its durable shipment provenance'; END IF;
      END IF;
      RETURN NEW;
    END $$""")


def downgrade():
    for table in ("shop_payment_confirmations", "shop_fulfillment_facts", "shop_completions", "shop_operation_audits"):
        if op.get_bind().execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Accepted Shop history exists; downgrade refused")
    op.execute("""CREATE OR REPLACE FUNCTION validate_mock_cod_payment() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.method='cod' OR NEW.source='mock_cod_confirmation') AND NOT EXISTS
        (SELECT 1 FROM mock_cod_confirmations c WHERE c.payment_id=NEW.id AND c.order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'Mock COD payment requires its durable shipment provenance'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("DROP TRIGGER protect_shop_order ON orders")
    op.execute("DROP FUNCTION protect_shop_order()")
    op.execute("DROP TRIGGER validate_shop_payment_projection ON order_payments")
    op.execute("DROP FUNCTION validate_shop_payment_projection()")
    for table in ("shop_payment_confirmations", "shop_fulfillment_facts", "shop_completions"):
        op.execute(f"DROP TRIGGER validate_shop_fact ON {table}")
    for table in ("shop_payment_confirmations", "shop_fulfillment_facts", "shop_completions", "shop_operation_audits"):
        op.execute(f"DROP TRIGGER immutable_shop_fact ON {table}")
    op.execute("DROP FUNCTION validate_shop_fact()")
    for table in ("shop_operation_audits", "shop_completions", "shop_fulfillment_facts", "shop_payment_confirmations"):
        op.drop_table(table)
