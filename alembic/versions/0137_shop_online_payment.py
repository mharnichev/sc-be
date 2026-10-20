"""Durable one-shot Shop checkout invoice dispatch, including unknown outcomes."""
from alembic import op
import sqlalchemy as sa

revision = "0137_shop_online_payment"
down_revision = "0136_category_names_en"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shop_online_payment_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("intent_id", sa.String(36), sa.ForeignKey("shop_payment_intents.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("initiator", sa.String(16), nullable=False),
        sa.Column("reference", sa.String(36), nullable=False, unique=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("transport", sa.String(16), nullable=False),
        sa.Column("basket", sa.JSON(), nullable=False),
        sa.Column("basket_hash", sa.String(64), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.execute("""CREATE FUNCTION validate_shop_online_request() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE b shop_payment_intents%ROWTYPE; o orders%ROWTYPE;
    BEGIN
      SELECT * INTO o FROM orders WHERE id=NEW.order_id FOR UPDATE;
      SELECT * INTO b FROM shop_payment_intents WHERE id=NEW.intent_id AND order_id=NEW.order_id;
      IF b.id IS NULL OR b.invoice_id IS NOT NULL OR b.mode!='prepayment'
        OR o.channel IS DISTINCT FROM 'shop' OR o.simulation OR o.status::text NOT IN ('pending','confirmed')
        OR o.payment_method IS DISTINCT FROM 'onlineCard'
        OR NEW.created_at IS DISTINCT FROM b.created_at
        OR NEW.operator_id IS DISTINCT FROM b.operator_id OR NEW.reference IS DISTINCT FROM b.reference
        OR NEW.amount_minor IS DISTINCT FROM b.amount_minor OR NEW.profile_id IS DISTINCT FROM b.profile_id
        OR NEW.profile_hash IS DISTINCT FROM b.profile_hash OR NEW.profile::jsonb IS DISTINCT FROM b.profile::jsonb
        OR NEW.authority IS DISTINCT FROM b.authority OR NEW.transport IS DISTINCT FROM b.transport
        OR NEW.profile::jsonb->'external_fiscalization_disabled' IS DISTINCT FROM 'false'::jsonb
        OR NEW.basket_hash IS DISTINCT FROM encode(sha256(convert_to(shop_payment_canonical(NEW.basket::jsonb),'UTF8')),'hex')
        OR jsonb_typeof(NEW.basket::jsonb) IS DISTINCT FROM 'array'
        OR (SELECT COALESCE(sum((line->>'total')::bigint),0) FROM jsonb_array_elements(NEW.basket::jsonb) line) IS DISTINCT FROM NEW.amount_minor
        OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.basket::jsonb) line
          WHERE (line->>'qty')::bigint<=0 OR (line->>'sum')::bigint<=0
            OR (line->>'total')::bigint IS DISTINCT FROM (line->>'qty')::bigint*(line->>'sum')::bigint
            OR line->'tax' IS DISTINCT FROM '[8]'::jsonb OR line->>'unit' IS DISTINCT FROM 'шт.')
        OR NEW.policy::jsonb->'tax_codes' IS DISTINCT FROM '[8]'::jsonb
        OR NEW.policy::jsonb->>'unit' IS DISTINCT FROM 'шт.'
        OR NEW.initiator NOT IN ('customer','guest')
        OR (NEW.initiator='customer' AND (NEW.customer_id IS NULL OR NEW.customer_id IS DISTINCT FROM o.customer_id))
        OR (NEW.initiator='guest' AND (NEW.customer_id IS NOT NULL OR o.customer_id IS NOT NULL))
      THEN RAISE EXCEPTION 'Shop online dispatch requires exact immutable checkout and payment intent'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER validate_shop_online_request BEFORE INSERT ON shop_online_payment_requests FOR EACH ROW EXECUTE FUNCTION validate_shop_online_request()")
    op.execute("CREATE TRIGGER immutable_shop_online_request BEFORE UPDATE OR DELETE ON shop_online_payment_requests FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM shop_online_payment_requests)")).scalar():
        raise RuntimeError("Shop online dispatch history exists; downgrade refused")
    op.drop_table("shop_online_payment_requests")
    op.execute("DROP FUNCTION validate_shop_online_request()")
