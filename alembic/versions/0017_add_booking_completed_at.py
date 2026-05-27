"""add booking completed timestamp

Revision ID: 0017_booking_completed_at
Revises: 0016_customer_import_fields
Create Date: 2026-05-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_booking_completed_at"
down_revision = "0016_customer_import_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "completed_at")
