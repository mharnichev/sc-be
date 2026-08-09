"""store requested duration for booking funnel no-slot events

Revision ID: 0058_funnel_no_slot_duration
Revises: 0057_waitlist_offer_source
Create Date: 2026-08-08 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0058_funnel_no_slot_duration"
down_revision = "0057_waitlist_offer_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_funnel_events",
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "booking_funnel_events_duration_minutes_range",
        "booking_funnel_events",
        "duration_minutes IS NULL OR (duration_minutes >= 1 AND duration_minutes <= 720)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "booking_funnel_events_duration_minutes_range",
        "booking_funnel_events",
        type_="check",
    )
    op.drop_column("booking_funnel_events", "duration_minutes")
