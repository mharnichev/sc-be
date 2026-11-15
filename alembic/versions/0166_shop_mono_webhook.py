"""Persist signed Mono notifications for existing Shop payment intents."""
import sqlalchemy as sa
from alembic import op

revision = "0166_shop_mono_webhook"
down_revision = "0165_master_mono_webhook"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shop_mono_webhook_signals",
        sa.Column("order_id", sa.Integer, sa.ForeignKey("shop_payment_intents.order_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("shop_mono_webhook_signals")
