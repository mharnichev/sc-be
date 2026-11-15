"""Record the latest signed Mono status signal for each bound POS invoice."""
import sqlalchemy as sa
from alembic import op

revision = "0165_master_mono_webhook"
down_revision = "0164_shop_balance_entry_date"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("mono_checkbox_poll_kind", "mono_checkbox_poll_state", type_="check")
    op.create_check_constraint("mono_checkbox_poll_kind", "mono_checkbox_poll_state",
                               "kind IN ('shop','master','master_refund')")
    op.create_table(
        "master_mono_webhook_signals",
        sa.Column("order_id", sa.Integer, sa.ForeignKey("master_mono_bindings.order_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("master_mono_webhook_signals")
    op.execute("DELETE FROM mono_checkbox_poll_state WHERE kind = 'master_refund'")
    op.drop_constraint("mono_checkbox_poll_kind", "mono_checkbox_poll_state", type_="check")
    op.create_check_constraint("mono_checkbox_poll_kind", "mono_checkbox_poll_state", "kind IN ('shop','master')")
