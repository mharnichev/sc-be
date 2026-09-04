"""attribute bookings to their creation channel

Revision ID: 0066_booking_sources
Revises: 0065_brand_visibility
Create Date: 2026-09-04 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0066_booking_sources"
down_revision = "0065_brand_visibility"
branch_labels = None
depends_on = None


booking_source = postgresql.ENUM(
    "web",
    "telegram",
    "backoffice",
    "unknown",
    name="bookingsource",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    booking_source.create(bind, checkfirst=True)
    op.add_column(
        "bookings",
        sa.Column(
            "source",
            booking_source,
            nullable=False,
            server_default=sa.text("'unknown'::bookingsource"),
        ),
    )
    op.create_index(
        "ix_bookings_source_created_at",
        "bookings",
        ["source", "created_at"],
    )

    # Successful public-site bookings have a one-to-one funnel success event.
    op.execute(
        """
        UPDATE bookings AS booking
        SET source = 'web'::bookingsource
        WHERE EXISTS (
            SELECT 1
            FROM booking_funnel_events AS event
            WHERE event.booking_id = booking.id
        )
        """
    )
    # Telegram sessions only retain their latest booking id, so this is a safe
    # but intentionally partial historical backfill. Explicit attribution is
    # used for every new booking after this migration.
    op.execute(
        """
        UPDATE bookings AS booking
        SET source = 'telegram'::bookingsource
        WHERE EXISTS (
            SELECT 1
            FROM telegram_bot_sessions AS bot_session
            WHERE NULLIF(bot_session.payload_json ->> 'booking_id', '') ~ '^[0-9]+$'
              AND (bot_session.payload_json ->> 'booking_id')::bigint = booking.id
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_bookings_source_created_at", table_name="bookings")
    op.drop_column("bookings", "source")
    booking_source.drop(op.get_bind(), checkfirst=True)
