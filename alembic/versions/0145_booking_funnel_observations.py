"""Add separate privacy-safe availability diagnostics; no historical event rewrite."""
from alembic import op
import sqlalchemy as sa

revision = "0145_booking_funnel_observations"
down_revision = "0144_master_rewards"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "booking_funnel_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        *[sa.Column(name, sa.String(64), nullable=False) for name in ("event_id_hash", "dedup_key", "payload_hash", "anonymous_session_hash")],
        sa.Column("request_id_hash", sa.String(64)),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("master_id", sa.Integer(), sa.ForeignKey("masters.id", ondelete="SET NULL")),
        sa.Column("service_ids_key", sa.String(255), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("slot_count", sa.Integer()),
        sa.Column("error_code", sa.String(24)),
        sa.Column("cancel_reason", sa.String(16)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id_hash"),
        sa.UniqueConstraint("dedup_key"),
        sa.CheckConstraint("event_type IN ('time_step_viewed','availability_requested','availability_result','availability_failed','availability_cancelled')", name="funnel_observation_type"),
        sa.CheckConstraint("duration_minutes BETWEEN 1 AND 720", name="funnel_observation_duration"),
        sa.CheckConstraint("slot_count IS NULL OR slot_count BETWEEN 0 AND 5000", name="funnel_observation_slots"),
    )
    op.create_index("ix_funnel_observation_period_master", "booking_funnel_observations", ["occurred_at", "master_id"])
    op.create_index("ix_funnel_observation_attempt_request", "booking_funnel_observations", ["anonymous_session_hash", "request_id_hash"])

    op.create_table(
        "booking_funnel_observation_receipts",
        sa.Column("event_id_hash", sa.String(64), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("observation_id", sa.Integer(), sa.ForeignKey("booking_funnel_observations.id", ondelete="CASCADE"), nullable=False),
    )


def downgrade():
    op.drop_table("booking_funnel_observation_receipts")
    op.drop_table("booking_funnel_observations")
