"""store complete service context for booking funnel no-slot events

Revision ID: 0052_funnel_service_context
Revises: 0051_review_form_open_events
Create Date: 2026-08-02 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0052_funnel_service_context"
down_revision = "0051_review_form_open_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_funnel_events",
        sa.Column("service_ids_key", sa.String(length=255), nullable=True),
    )
    op.execute(
        """
        UPDATE booking_funnel_events
        SET service_ids_key = CAST(service_id AS VARCHAR)
        WHERE event_type = 'no_slot'
          AND service_id IS NOT NULL
          AND service_ids_key IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("booking_funnel_events", "service_ids_key")
