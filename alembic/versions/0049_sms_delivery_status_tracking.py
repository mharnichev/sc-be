"""track SMS provider delivery statuses

Revision ID: 0049_sms_delivery_status
Revises: 0048_service_popularity
Create Date: 2026-07-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0049_sms_delivery_status"
down_revision = "0048_service_popularity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE messagedeliverystatus ADD VALUE IF NOT EXISTS 'delivered' AFTER 'sent'")
    op.add_column(
        "message_recipients",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "message_recipients",
        sa.Column("delivery_status_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_message_recipients_sms_delivery_sync",
        "message_recipients",
        ["channel", "status", "delivery_status_checked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_recipients_sms_delivery_sync", table_name="message_recipients")
    op.drop_column("message_recipients", "delivery_status_checked_at")
    op.drop_column("message_recipients", "delivered_at")
    # PostgreSQL enum values cannot be removed without rebuilding the type.
    # Keeping the unused value makes this downgrade non-destructive for existing rows.
