"""add booking sms reminder sent at

Revision ID: 0028_booking_sms_reminder
Revises: 0027_master_availability_windows
Create Date: 2026-06-16 23:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028_booking_sms_reminder"
down_revision = "0027_master_availability_windows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("sms_reminder_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "sms_reminder_sent_at")
