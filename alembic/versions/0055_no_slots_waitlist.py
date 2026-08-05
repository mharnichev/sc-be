"""add no-slots recovery, waitlist offers and privacy-safe analytics

Revision ID: 0055_no_slots_waitlist
Revises: 0054_booking_service_prices
Create Date: 2026-08-05 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0055_no_slots_waitlist"
down_revision = "0054_booking_service_prices"
branch_labels = None
depends_on = None


waitlist_status = postgresql.ENUM(
    "active",
    "offered",
    "booked",
    "expired",
    "cancelled",
    name="waitliststatus",
    create_type=False,
)
waitlist_offer_status = postgresql.ENUM(
    "pending",
    "sent",
    "delivered",
    "claimed",
    "expired",
    "cancelled",
    name="waitlistofferstatus",
    create_type=False,
)


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()
    waitlist_status.create(bind, checkfirst=True)
    waitlist_offer_status.create(bind, checkfirst=True)

    op.create_table(
        "waitlist_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("cancel_token_hash", sa.String(length=64), nullable=False),
        sa.Column("dedup_key_hash", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("preferred_master_id", sa.Integer(), nullable=True),
        sa.Column("desired_date", sa.Date(), nullable=False),
        sa.Column("acceptable_date_from", sa.Date(), nullable=True),
        sa.Column("acceptable_date_to", sa.Date(), nullable=True),
        sa.Column("preferred_time_from", sa.Time(), nullable=True),
        sa.Column("preferred_time_to", sa.Time(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("notification_consent", sa.Boolean(), nullable=False),
        sa.Column("status", waitlist_status, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=255), nullable=True),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["preferred_master_id"], ["masters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cancel_token_hash", name="uq_waitlist_requests_cancel_token_hash"),
    )
    for column in (
        "customer_id",
        "preferred_master_id",
        "desired_date",
        "status",
        "expires_at",
        "public_id",
        "dedup_key_hash",
    ):
        op.create_index(
            op.f(f"ix_waitlist_requests_{column}"),
            "waitlist_requests",
            [column],
            unique=column == "public_id",
        )
    op.create_index(
        "ix_waitlist_requests_matching",
        "waitlist_requests",
        ["status", "desired_date", "preferred_master_id", "expires_at"],
    )
    op.create_index(
        "uq_waitlist_requests_open_dedup_key",
        "waitlist_requests",
        ["dedup_key_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'offered')"),
        sqlite_where=sa.text("status IN ('active', 'offered')"),
    )

    op.create_table(
        "waitlist_request_services",
        sa.Column("waitlist_request_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["waitlist_request_id"],
            ["waitlist_requests.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["service_id"], ["barber_services.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("waitlist_request_id", "service_id"),
    )

    op.create_table(
        "waitlist_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", waitlist_offer_status, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=255), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("source_booking_id", sa.Integer(), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint("end_at > start_at", name="waitlist_offer_positive_interval"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id"], ["waitlist_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "master_id", "start_at", name="uq_waitlist_offers_request_slot"),
        sa.UniqueConstraint("token_hash", name="uq_waitlist_offers_token_hash"),
    )
    for column in (
        "request_id",
        "master_id",
        "status",
        "expires_at",
        "scheduled_at",
        "provider_message_id",
        "source_booking_id",
    ):
        op.create_index(op.f(f"ix_waitlist_offers_{column}"), "waitlist_offers", [column])
    op.create_index(
        "ix_waitlist_offers_slot",
        "waitlist_offers",
        ["master_id", "start_at", "status"],
    )

    op.create_table(
        "booking_recovery_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_key_hash", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("anonymous_session_hash", sa.String(length=64), nullable=True),
        sa.Column("master_id", sa.Integer(), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("waitlist_request_id", sa.Integer(), nullable=True),
        sa.Column("waitlist_offer_id", sa.Integer(), nullable=True),
        sa.Column("source_booking_id", sa.Integer(), nullable=True),
        sa.Column("metric_value", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["service_id"], ["barber_services.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["waitlist_offer_id"], ["waitlist_offers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["waitlist_request_id"], ["waitlist_requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key_hash", name="uq_booking_recovery_events_event_key_hash"),
    )
    for column in (
        "event_type",
        "anonymous_session_hash",
        "master_id",
        "service_id",
        "booking_id",
        "waitlist_request_id",
        "waitlist_offer_id",
        "source_booking_id",
        "occurred_at",
    ):
        op.create_index(op.f(f"ix_booking_recovery_events_{column}"), "booking_recovery_events", [column])
    op.create_index(
        "ix_booking_recovery_events_type_occurred",
        "booking_recovery_events",
        ["event_type", "occurred_at"],
    )
    op.create_index(
        "ix_booking_recovery_events_session_type",
        "booking_recovery_events",
        ["anonymous_session_hash", "event_type"],
    )


def downgrade() -> None:
    op.drop_table("booking_recovery_events")
    op.drop_table("waitlist_offers")
    op.drop_table("waitlist_request_services")
    op.drop_table("waitlist_requests")

    bind = op.get_bind()
    waitlist_offer_status.drop(bind, checkfirst=True)
    waitlist_status.drop(bind, checkfirst=True)
