"""Separate anonymous Telegram booking funnel."""
from alembic import op
import sqlalchemy as sa
revision = "0109_telegram_booking_funnel"
down_revision = "0108_booking_archive"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("telegram_booking_funnel_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempt_hash", sa.String(64), nullable=False),
        sa.Column("step", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=True),
        sa.UniqueConstraint("attempt_hash", "step", name="uq_telegram_funnel_attempt_step"),
        sa.UniqueConstraint("booking_id", name="uq_telegram_funnel_booking"),
        sa.CheckConstraint("(step = 'booking_success' AND booking_id IS NOT NULL) OR (step <> 'booking_success' AND booking_id IS NULL)", name="telegram_funnel_success_booking"))
    op.create_index("ix_telegram_funnel_step_occurred", "telegram_booking_funnel_events", ["step", "occurred_at"])


def downgrade():
    op.drop_index("ix_telegram_funnel_step_occurred", table_name="telegram_booking_funnel_events")
    op.drop_table("telegram_booking_funnel_events")
