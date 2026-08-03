"""add manual booking discounts

Revision ID: 0053_manual_booking_discounts
Revises: 0052_funnel_service_context
Create Date: 2026-08-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0053_manual_booking_discounts"
down_revision = "0052_funnel_service_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column(
            "manual_discount_amount",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bookings", "manual_discount_amount")
