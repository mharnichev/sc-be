"""add booking two hour sms reminder sent at

Revision ID: 0029_two_hour_sms_reminder
Revises: 0028_booking_sms_reminder
Create Date: 2026-06-17 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_two_hour_sms_reminder"
down_revision = "0028_booking_sms_reminder"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("sms_two_hour_reminder_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "sms_two_hour_reminder_sent_at")
