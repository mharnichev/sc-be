"""Local checkout intents and immutable preliminary documents; no historical facts inferred."""
from alembic import op
import sqlalchemy as sa

revision = "0111_local_order_documents"
down_revision = "0110_nova_poshta_directory"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("channel", sa.String(32), nullable=True))
    op.add_column("orders", sa.Column("fulfillment_method", sa.String(32), nullable=True))
    op.add_column("orders", sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
    op.create_table("order_intents",
        sa.Column("scope_key", sa.String(64), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True))
    op.create_table("order_preliminary_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(64), nullable=False, unique=True),
        sa.Column("operation_key", sa.String(64), nullable=True, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guest_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("order_id", "version"))


def downgrade():
    op.drop_table("order_preliminary_documents")
    op.drop_table("order_intents")
    op.drop_column("orders", "revision")
    op.drop_column("orders", "fulfillment_method")
    op.drop_column("orders", "channel")
