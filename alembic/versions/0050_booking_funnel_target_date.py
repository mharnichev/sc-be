"""store target dates for booking funnel no-slot events

Revision ID: 0050_booking_funnel_target_date
Revises: 0049_sms_delivery_status
Create Date: 2026-07-29 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0050_booking_funnel_target_date"
down_revision = "0049_sms_delivery_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_funnel_events",
        sa.Column("target_date", sa.Date(), nullable=True),
    )
    op.create_index(
        "ix_booking_funnel_events_type_target_date",
        "booking_funnel_events",
        ["event_type", "target_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_booking_funnel_events_type_target_date",
        table_name="booking_funnel_events",
    )
    op.drop_column("booking_funnel_events", "target_date")
