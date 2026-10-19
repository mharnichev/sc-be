"""Separate local Shop fiscal truth; no legacy fiscal guard changes."""
from alembic import op
import sqlalchemy as sa

revision = "0123_shop_fiscal"
down_revision = "0122_shop_order_operations"
branch_labels = None
depends_on = None


def ref(name, target, *, nullable=False, unique=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(target + ".id", ondelete="RESTRICT"), nullable=nullable, unique=unique)


def identifier():
    return sa.Column("id", sa.Integer(), primary_key=True)


def stamp():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def snapshot():
    return [sa.Column("snapshot", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(64), nullable=False)]


TABLES = ("shop_fiscal_registers", "shop_financial_returns", "shop_fiscal_previews", "shop_fiscal_documents",
    "shop_fiscal_acceptances", "shop_refund_facts", "shop_return_receipts", "shop_fiscal_reports", "shop_fiscal_audits")


def upgrade():
    op.create_table("shop_fiscal_registers", sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("shift_status", sa.String(16), nullable=False), sa.Column("shift_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("id='SHOP-MOCK-KASA-1' AND shift_status IN ('open','closed') AND shift_revision>0", name="ck_shop_fiscal_register"))
    # Circular original-sale / return-document references are completed below.
    op.create_table("shop_financial_returns", identifier(), ref("order_id", "orders"),
        sa.Column("sale_document_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("shift_revision", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False), sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False), ref("operator_id", "admin_users"), stamp(),
        sa.CheckConstraint("amount_minor>0 AND revision>0 AND shift_revision>0 AND btrim(reason)!=''", name="ck_shop_financial_return"))
    op.create_table("shop_fiscal_previews", identifier(), ref("order_id", "orders"),
        ref("return_id", "shop_financial_returns", nullable=True), ref("actor_id", "admin_users"),
        sa.Column("kind", sa.String(16), nullable=False), sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("shift_revision", sa.Integer(), nullable=False), *snapshot(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("shop_fiscal_documents", identifier(), ref("order_id", "orders"),
        ref("payment_confirmation_id", "shop_payment_confirmations"), ref("return_id", "shop_financial_returns", nullable=True),
        ref("preview_id", "shop_fiscal_previews", unique=True), ref("actor_id", "admin_users"),
        sa.Column("kind", sa.String(16), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), sa.Column("provider_uuid", sa.String(36), nullable=False, unique=True),
        sa.Column("shift_revision", sa.Integer(), nullable=False), *snapshot(),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True), stamp(),
        sa.CheckConstraint("kind IN ('sale','return') AND status IN ('queued','unknown','issued','cancelled','error') AND revision>0", name="ck_shop_fiscal_document"),
        sa.CheckConstraint("(kind='sale' AND return_id IS NULL) OR (kind='return' AND return_id IS NOT NULL)", name="ck_shop_fiscal_document_kind"))
    op.create_foreign_key("fk_shop_return_original_sale", "shop_financial_returns", "shop_fiscal_documents", ["sale_document_id"], ["id"], ondelete="RESTRICT")
    op.create_index("uq_shop_fiscal_active_sale", "shop_fiscal_documents", ["payment_confirmation_id"], unique=True,
        postgresql_where=sa.text("kind='sale' AND status NOT IN ('cancelled','error')"))
    op.create_index("uq_shop_fiscal_active_return", "shop_fiscal_documents", ["return_id"], unique=True,
        postgresql_where=sa.text("kind='return' AND status NOT IN ('cancelled','error')"))
    op.create_table("shop_fiscal_acceptances", sa.Column("provider_uuid", sa.String(36), primary_key=True),
        ref("document_id", "shop_fiscal_documents", unique=True), sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("shop_refund_facts", identifier(), ref("return_id", "shop_financial_returns", unique=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False), sa.Column("method", sa.String(32), nullable=False),
        sa.Column("external_reference", sa.String(200), nullable=False, unique=True), sa.Column("evidence", sa.String(500), nullable=False),
        ref("operator_id", "admin_users"), stamp(),
        sa.CheckConstraint("amount_minor>0 AND btrim(external_reference)!='' AND btrim(evidence)!=''", name="ck_shop_refund_fact"))
    op.create_table("shop_return_receipts", identifier(), ref("return_id", "shop_financial_returns", unique=True),
        sa.Column("lines", sa.JSON(), nullable=False), sa.Column("evidence", sa.String(500), nullable=False), ref("operator_id", "admin_users"), stamp())
    op.create_table("shop_fiscal_reports", identifier(), sa.Column("kind", sa.String(1), nullable=False),
        sa.Column("shift_revision", sa.Integer(), nullable=False), sa.Column("totals", sa.JSON(), nullable=False),
        sa.Column("reconciliation", sa.JSON(), nullable=False), ref("operator_id", "admin_users"), stamp(),
        sa.CheckConstraint("kind IN ('x','z')", name="ck_shop_fiscal_report"))
    op.create_index("uq_shop_fiscal_z", "shop_fiscal_reports", ["shift_revision"], unique=True, postgresql_where=sa.text("kind='z'"))
    op.create_table("shop_fiscal_audits", identifier(), ref("operator_id", "admin_users"),
        sa.Column("operation", sa.String(40), nullable=False), sa.Column("facts", sa.JSON(), nullable=False), stamp())
    for table in ("shop_fiscal_previews", "shop_fiscal_acceptances", "shop_refund_facts", "shop_return_receipts", "shop_fiscal_reports", "shop_fiscal_audits"):
        op.execute(f"CREATE TRIGGER immutable_shop_fiscal BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")
    op.execute("""CREATE FUNCTION lock_shop_fiscal_register() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN PERFORM 1 FROM shop_fiscal_registers WHERE id='SHOP-MOCK-KASA-1' FOR UPDATE; RETURN NEW; END $$""")
    # Also covers flag-off recovery and direct canonical payment inserts.
    op.execute("CREATE TRIGGER lock_shop_fiscal_payment BEFORE INSERT ON shop_payment_confirmations FOR EACH ROW EXECUTE FUNCTION lock_shop_fiscal_register()")
    for table in ("shop_financial_returns", "shop_fiscal_documents", "shop_fiscal_acceptances", "shop_refund_facts", "shop_return_receipts", "shop_fiscal_reports"):
        op.execute(f"CREATE TRIGGER lock_shop_fiscal_register BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION lock_shop_fiscal_register()")
    op.execute("""CREATE FUNCTION protect_shop_fiscal_document() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Shop fiscal approvals are immutable'; END IF;
      IF TG_OP='UPDATE' THEN
        IF (to_jsonb(NEW)-ARRAY['status','revision','dispatched_at','issued_at']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['status','revision','dispatched_at','issued_at'])
          OR OLD.dispatched_at IS NOT NULL AND NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at
          OR OLD.issued_at IS NOT NULL AND NEW.issued_at IS DISTINCT FROM OLD.issued_at
          OR OLD.status IN ('issued','cancelled','error') AND NEW.status!=OLD.status
          OR OLD.status='unknown' AND NEW.status NOT IN ('unknown','issued')
          OR NEW.status='unknown' AND NEW.dispatched_at IS NULL
          OR NEW.status='cancelled' AND NEW.dispatched_at IS NOT NULL
        THEN RAISE EXCEPTION 'Shop fiscal approval or dispatch fence cannot be changed'; END IF;
      END IF;
      IF NEW.status='issued' AND NOT EXISTS(SELECT 1 FROM shop_fiscal_acceptances a
        WHERE a.provider_uuid=NEW.provider_uuid AND a.document_id=NEW.id AND a.snapshot_hash=NEW.snapshot_hash)
      THEN RAISE EXCEPTION 'Issued Shop receipt requires independent provider truth'; END IF;
      IF NOT EXISTS(SELECT 1 FROM shop_payment_confirmations p JOIN order_payments m ON m.id=p.payment_id
        JOIN shop_fiscal_previews v ON v.id=NEW.preview_id
        WHERE p.id=NEW.payment_confirmation_id AND p.order_id=NEW.order_id
          AND v.order_id=NEW.order_id AND v.actor_id=NEW.actor_id AND v.kind=NEW.kind
          AND v.return_id IS NOT DISTINCT FROM NEW.return_id AND v.snapshot_hash=NEW.snapshot_hash
          AND v.snapshot::jsonb=NEW.snapshot::jsonb AND v.shift_revision=NEW.shift_revision
          AND NEW.snapshot->>'document_kind'='shop_mock_fiscal_receipt' AND NEW.snapshot->>'authority'='test'
          AND NEW.snapshot->>'is_sent_dps'='false' AND NEW.snapshot->>'register_id'='SHOP-MOCK-KASA-1'
          AND NEW.snapshot->>'payment_confirmation_id'=p.id::text
          AND (NEW.kind!='sale' OR NEW.snapshot->>'amount_minor'=m.amount_minor::text
            AND NEW.snapshot->'payment'->>'id'=m.id::text
            AND NEW.snapshot->'payment'->>'method'=m.method AND NEW.snapshot->'payment'->>'source'=m.source))
      THEN RAISE EXCEPTION 'Shop receipt requires canonical payment and immutable Shop preview'; END IF;
      IF NEW.kind='return' AND NOT EXISTS(SELECT 1 FROM shop_financial_returns r
        JOIN shop_refund_facts f ON f.return_id=r.id JOIN shop_fiscal_documents d ON d.id=r.sale_document_id
        WHERE r.id=NEW.return_id AND r.order_id=NEW.order_id AND d.payment_confirmation_id=NEW.payment_confirmation_id
          AND r.shift_revision=NEW.shift_revision AND r.amount_minor=f.amount_minor
          AND NEW.snapshot->>'amount_minor'=r.amount_minor::text AND NEW.snapshot->>'refund_id'=f.id::text
          AND NEW.snapshot->>'return_id'=r.id::text AND NEW.snapshot->>'original_sale_id'=d.id::text
          AND NEW.snapshot->>'original_sale_hash'=d.snapshot_hash AND (NEW.snapshot->'items')::jsonb=r.lines::jsonb)
      THEN RAISE EXCEPTION 'Shop fiscal return requires exact reserved return and refund binding'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_fiscal_document BEFORE INSERT OR UPDATE OR DELETE ON shop_fiscal_documents FOR EACH ROW EXECUTE FUNCTION protect_shop_fiscal_document()")
    op.execute("""CREATE FUNCTION validate_shop_fiscal_acceptance() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM shop_fiscal_documents d WHERE d.id=NEW.document_id
        AND d.provider_uuid=NEW.provider_uuid AND d.snapshot_hash=NEW.snapshot_hash AND d.dispatched_at IS NOT NULL
        AND d.status='unknown') THEN RAISE EXCEPTION 'Provider truth requires committed Shop dispatch fence'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_fiscal_acceptance BEFORE INSERT ON shop_fiscal_acceptances FOR EACH ROW EXECUTE FUNCTION validate_shop_fiscal_acceptance()")
    op.execute("""CREATE FUNCTION protect_shop_financial_return() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' OR TG_OP='UPDATE' AND (to_jsonb(NEW)-'revision') IS DISTINCT FROM (to_jsonb(OLD)-'revision')
      THEN RAISE EXCEPTION 'Shop return quantity reservation is immutable'; END IF;
      IF TG_OP='INSERT' AND (NOT EXISTS(SELECT 1 FROM shop_fiscal_registers r WHERE r.id='SHOP-MOCK-KASA-1'
          AND r.shift_status='open' AND r.shift_revision=NEW.shift_revision)
        OR NOT EXISTS(SELECT 1 FROM shop_fiscal_documents d JOIN shop_fiscal_acceptances a ON a.document_id=d.id
          JOIN shop_fulfillment_facts f ON f.order_id=d.order_id WHERE d.id=NEW.sale_document_id AND d.kind='sale'
          AND d.status='issued' AND d.order_id=NEW.order_id AND a.provider_uuid=d.provider_uuid AND a.snapshot_hash=d.snapshot_hash))
      THEN RAISE EXCEPTION 'Shop return requires issued original sale, handover and open return shift'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER protect_shop_financial_return BEFORE INSERT OR UPDATE OR DELETE ON shop_financial_returns FOR EACH ROW EXECUTE FUNCTION protect_shop_financial_return()")
    op.execute("""CREATE FUNCTION validate_shop_refund() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM shop_financial_returns r JOIN shop_fiscal_documents d ON d.id=r.sale_document_id
        JOIN shop_fiscal_registers s ON s.id='SHOP-MOCK-KASA-1' AND s.shift_status='open' AND s.shift_revision=r.shift_revision
        WHERE r.id=NEW.return_id AND r.amount_minor=NEW.amount_minor AND d.snapshot->'payment'->>'method'=NEW.method)
      THEN RAISE EXCEPTION 'Shop test refund requires exact original payment and reserved return amount'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_refund BEFORE INSERT ON shop_refund_facts FOR EACH ROW EXECUTE FUNCTION validate_shop_refund()")


def downgrade():
    for table in TABLES[1:]:
        if op.get_bind().execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("Accepted Shop fiscal history exists; downgrade refused")
    op.execute("DROP TRIGGER lock_shop_fiscal_payment ON shop_payment_confirmations")
    op.drop_constraint("fk_shop_return_original_sale", "shop_financial_returns", type_="foreignkey")
    for table in reversed(TABLES):
        op.drop_table(table)
    for function in ("lock_shop_fiscal_register", "protect_shop_fiscal_document", "validate_shop_fiscal_acceptance", "protect_shop_financial_return", "validate_shop_refund"):
        op.execute(f"DROP FUNCTION {function}()")
