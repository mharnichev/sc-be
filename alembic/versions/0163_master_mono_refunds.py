"""Persist backoffice POS refund claims and reconciliation facts."""
from alembic import op
import sqlalchemy as sa

revision = "0163_master_mono_refunds"
down_revision = "0162_shop_balance_edits"
branch_labels = None
depends_on = None


_STOCK_START = "SELECT * INTO q FROM master_mono_create_requests WHERE order_id=NEW.order_id;"
_HANDOVER_START = "IF q.id IS NULL THEN RETURN NEW; END IF;"
_OWNER_STATUS = "OR NEW.status IS DISTINCT FROM OLD.status AND NEW.status::text!='completed'"
_EDITS = {
    "protect_master_dispatch_stock": (_STOCK_START, _STOCK_START + """
      IF master_mono_refund_stock_allowed(NEW) THEN RETURN NEW; END IF;
      IF q.id IS NOT NULL AND EXISTS (SELECT 1 FROM master_mono_refunds WHERE order_id=NEW.order_id)
      THEN RAISE EXCEPTION 'A POS refund blocks sale stock movements'; END IF;"""),
    "require_master_live_handover": (_HANDOVER_START, _HANDOVER_START + """
      IF TG_TABLE_NAME='inventory_movements' THEN
        IF master_mono_refund_stock_allowed(NEW) THEN RETURN NEW; END IF;
      END IF;"""),
    "guard_master_mono_owner": (_OWNER_STATUS, _OWNER_STATUS + """
          AND NOT (OLD.status::text IN ('pending','confirmed','paid') AND NEW.status::text='cancelled'
            AND NEW.revision=OLD.revision+1
            AND EXISTS (SELECT 1 FROM master_mono_refunds WHERE order_id=OLD.id
                        AND bank_proof IS NOT NULL AND receipt_id IS NOT NULL)
            AND NOT EXISTS (SELECT 1 FROM order_handovers WHERE order_id=OLD.id))"""),
}


def update_guards(*, reverse=False):
    for name, (before, after) in _EDITS.items():
        source = op.get_bind().scalar(
            sa.text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"), {"name": name + "()"}
        )
        old, new = (after, before) if reverse else (before, after)
        if source.count(old) != 1:
            raise RuntimeError(f"Unexpected POS refund guard definition: {name}")
        op.execute(source.replace(old, new, 1))



def upgrade():
    op.create_table(
        "master_mono_refunds",
        sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("operator_id", sa.Integer, sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("ext_ref", sa.String(32), unique=True),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bank_proof", sa.JSON),
        sa.Column("receipt_id", sa.String(36), unique=True),
        sa.Column("receipt_url", sa.Text),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("received_by", sa.Integer, sa.ForeignKey("admin_users.id", ondelete="RESTRICT")),
    )

    op.execute("""
    CREATE FUNCTION master_mono_refund_stock_allowed(m inventory_movements) RETURNS boolean
    LANGUAGE sql AS $$
      SELECT EXISTS (
        SELECT 1 FROM master_mono_refunds r JOIN order_items i ON i.order_id=r.order_id
        WHERE r.order_id=m.order_id AND r.bank_proof IS NOT NULL
          AND i.id=m.order_item_id AND i.product_id=m.product_id AND i.quantity=m.quantity
          AND (
            (m.movement_type::text='reservation_release' AND r.receipt_id IS NOT NULL
             AND m.admin_user_id=r.operator_id AND m.on_hand_delta=0 AND m.reserved_delta= -i.quantity
             AND m.reference_key='order:'||r.order_id||':item:'||i.id||chr(58)||'reservation-release'
             AND NOT EXISTS (SELECT 1 FROM order_handovers WHERE order_id=r.order_id))
            OR
            (m.movement_type::text='customer_return' AND r.received_at IS NOT NULL
             AND m.admin_user_id=r.received_by AND m.on_hand_delta=i.quantity AND m.reserved_delta=0
             AND m.reference_key='master-mono-refund:'||r.order_id||':item:'||i.id
             AND EXISTS (SELECT 1 FROM inventory_movements s WHERE s.order_id=r.order_id
                         AND s.order_item_id=i.id AND s.product_id=i.product_id
                         AND s.movement_type::text='shipment' AND s.on_hand_delta= -i.quantity))
          )
      )
    $$
    """)
    update_guards()


def downgrade():
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM master_mono_refunds) THEN
        RAISE EXCEPTION 'Cannot downgrade with existing POS refund history';
      END IF;
    END $$
    """)
    update_guards(reverse=True)
    op.execute("DROP FUNCTION master_mono_refund_stock_allowed(inventory_movements)")
    op.drop_table("master_mono_refunds")
