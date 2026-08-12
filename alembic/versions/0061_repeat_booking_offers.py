"""add Telegram repeat booking offers and lifecycle analytics

Revision ID: 0061_repeat_booking_offers
Revises: 0060_booking_sms_links
Create Date: 2026-08-11 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0061_repeat_booking_offers"
down_revision = "0060_booking_sms_links"
branch_labels = None
depends_on = None


offer_status = postgresql.ENUM(
    "scheduled", "sent", "opened", "started", "booked", "expired", "skipped", "failed",
    name="repeatbookingofferstatus", create_type=False,
)


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    offer_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "client_communication_preferences",
        sa.Column("repeat_booking_opt_out", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "repeat_booking_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("completed_booking_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("preferred_master_id", sa.Integer(), nullable=True),
        sa.Column("service_ids", sa.JSON(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("status", offer_status, nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("delivery_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_booking_id", sa.Integer(), nullable=True),
        *timestamps(),
        sa.ForeignKeyConstraint(["completed_booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["preferred_master_id"], ["masters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["result_booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("completed_booking_id", name="uq_repeat_booking_offers_completed_booking_id"),
        sa.UniqueConstraint("token_hash", name="uq_repeat_booking_offers_token_hash"),
        sa.UniqueConstraint("result_booking_id", name="uq_repeat_booking_offers_result_booking_id"),
    )
    op.create_index(
        op.f("ix_repeat_booking_offers_preferred_master_id"),
        "repeat_booking_offers",
        ["preferred_master_id"],
    )
    op.create_index("ix_repeat_booking_offers_due", "repeat_booking_offers", ["status", "scheduled_at"])
    op.create_index("ix_repeat_booking_offers_expiry", "repeat_booking_offers", ["status", "expires_at"])
    op.create_index("ix_repeat_booking_offers_customer_sent", "repeat_booking_offers", ["customer_id", "sent_at"])

    op.create_table(
        "repeat_booking_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("event_key_hash", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["offer_id"], ["repeat_booking_offers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key_hash", name="uq_repeat_booking_events_event_key_hash"),
    )
    for column in ("offer_id", "reason_code"):
        op.create_index(op.f(f"ix_repeat_booking_events_{column}"), "repeat_booking_events", [column])
    op.create_index("ix_repeat_booking_events_created_type", "repeat_booking_events", ["created_at", "event_type"])


def downgrade() -> None:
    op.drop_table("repeat_booking_events")
    op.drop_table("repeat_booking_offers")
    op.drop_column("client_communication_preferences", "repeat_booking_opt_out")
    offer_status.drop(op.get_bind(), checkfirst=True)
