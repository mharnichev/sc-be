"""Saved dynamic segments and immutable campaign run audiences.

Revision ID: 0068_customer_segments
Revises: 0067_merge_shampoo_categories
"""
from alembic import op
import sqlalchemy as sa

revision = "0068_customer_segments"
down_revision = "0067_merge_shampoo_categories"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)]


def upgrade() -> None:
    op.create_table(
        "customer_segments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(8), server_default="active", nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
    )
    op.create_index("ix_customer_segments_name", "customer_segments", ["name"])
    op.create_index("ix_customer_segments_status", "customer_segments", ["status"])
    op.create_table(
        "campaign_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), server_default="scheduled", nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("segment_snapshots", sa.JSON(), nullable=False),
        sa.Column("campaign_snapshot", sa.JSON(), nullable=False),
        sa.Column("audience_count", sa.Integer(), server_default="0", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_campaign_runs_idempotency_key"),
    )
    for column in ("campaign_id", "status", "scheduled_at"):
        op.create_index(f"ix_campaign_runs_{column}", "campaign_runs", [column])
    op.add_column("message_recipients", sa.Column("run_id", sa.Integer(), nullable=True))
    op.add_column("message_recipients", sa.Column("snapshot_facts", sa.JSON(), nullable=True))
    op.add_column("message_recipients", sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_message_recipients_run_id_campaign_runs", "message_recipients", "campaign_runs", ["run_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_message_recipients_run_id", "message_recipients", ["run_id"])
    op.create_unique_constraint("uq_message_recipients_run_customer", "message_recipients", ["run_id", "customer_id"])
    op.create_index("ix_bookings_customer_status_end", "bookings", ["customer_id", "status", "end_at"])
    op.create_index("ix_bookings_customer_status_start", "bookings", ["customer_id", "status", "start_at"])
    op.create_index("ix_message_recipients_customer_sent", "message_recipients", ["customer_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_message_recipients_customer_sent", table_name="message_recipients")
    op.drop_index("ix_bookings_customer_status_start", table_name="bookings")
    op.drop_index("ix_bookings_customer_status_end", table_name="bookings")
    op.drop_constraint("uq_message_recipients_run_customer", "message_recipients", type_="unique")
    op.drop_index("ix_message_recipients_run_id", table_name="message_recipients")
    op.drop_constraint("fk_message_recipients_run_id_campaign_runs", "message_recipients", type_="foreignkey")
    for column in ("send_started_at", "snapshot_facts", "run_id"):
        op.drop_column("message_recipients", column)
    op.drop_table("campaign_runs")
    op.drop_table("customer_segments")
