"""store opaque recovery analytics context and offer identifiers

Revision ID: 0098_recovery_analytics
Revises: 0097_master_booking_notification
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0098_recovery_analytics"
down_revision = "0097_master_booking_notification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_recovery_events", sa.Column("search_context_hash", sa.String(length=64), nullable=True))
    op.add_column("booking_recovery_events", sa.Column("offer_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_booking_recovery_events_context_type", "booking_recovery_events", ["search_context_hash", "event_type"])
    op.create_index("ix_booking_recovery_events_offer_type", "booking_recovery_events", ["offer_hash", "event_type"])


def downgrade() -> None:
    op.drop_index("ix_booking_recovery_events_offer_type", table_name="booking_recovery_events")
    op.drop_index("ix_booking_recovery_events_context_type", table_name="booking_recovery_events")
    op.drop_column("booking_recovery_events", "offer_hash")
    op.drop_column("booking_recovery_events", "search_context_hash")
