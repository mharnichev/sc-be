"""Durable cooldown for bounded local mono/Checkbox reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = "0132_mono_checkbox_poll"
down_revision = "0131_master_mono"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("mono_checkbox_poll_state",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('shop','master')", name="mono_checkbox_poll_kind"))


def downgrade():
    op.drop_table("mono_checkbox_poll_state")
