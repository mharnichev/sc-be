"""Additive local POS preparation and explicitly simulated immutable facts."""
from alembic import op
import sqlalchemy as sa

revision = "0112_local_pos_sales"
down_revision = "0111_local_order_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("orders", sa.Column("simulation", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.alter_column("orders", "customer_phone", existing_type=sa.String(50), nullable=True)
    op.add_column("admin_users", sa.Column("can_record_sales", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table("order_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("evidence", sa.String(500), nullable=False),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("simulation", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name="order_payment_positive"))
    op.create_table("order_handovers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("evidence", sa.String(500), nullable=False),
        sa.Column("simulation", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.execute("CREATE FUNCTION prevent_sales_fact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Sales facts are immutable'; END $$")
    for table in ("order_payments", "order_handovers"):
        op.execute(f"CREATE TRIGGER immutable_sales_fact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_sales_fact_mutation()")


def downgrade():
    # For disposable test databases only; preserve financial history in deployments.
    op.drop_table("order_handovers")
    op.drop_table("order_payments")
    op.execute("DROP FUNCTION prevent_sales_fact_mutation()")
    op.drop_column("admin_users", "can_record_sales")
    op.drop_column("orders", "simulation")
    op.drop_column("orders", "created_by_user_id")
    # Existing anonymous rows remain readable; do not reintroduce NOT NULL.
