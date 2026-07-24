"""default all client communication consent to opted in

Revision ID: 0043_full_client_consent
Revises: 0042_booking_funnel
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0043_full_client_consent"
down_revision = "0042_booking_funnel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "client_communication_preferences",
        "marketing_consent",
        server_default=sa.text("'opted_in'::consentstatus"),
    )
    op.alter_column(
        "client_communication_preferences",
        "transactional_consent",
        server_default=sa.text("'opted_in'::consentstatus"),
    )
    op.alter_column(
        "client_communication_preferences",
        "do_not_contact",
        server_default=sa.false(),
    )

    # A missing preference means that Soul Cuts received the number directly
    # from the client during booking. Preserve any explicit opt-out records.
    op.execute(
        """
        INSERT INTO client_communication_preferences (
            customer_id,
            marketing_consent,
            transactional_consent,
            do_not_contact,
            created_at,
            updated_at
        )
        SELECT
            customers.id,
            'opted_in'::consentstatus,
            'opted_in'::consentstatus,
            false,
            now(),
            now()
        FROM customers
        LEFT JOIN client_communication_preferences preferences
            ON preferences.customer_id = customers.id
        WHERE preferences.id IS NULL
        ON CONFLICT (customer_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE client_communication_preferences
        SET
            marketing_consent = 'opted_in'::consentstatus,
            transactional_consent = 'opted_in'::consentstatus,
            updated_at = now()
        WHERE
            marketing_consent = 'unknown'::consentstatus
            OR transactional_consent = 'unknown'::consentstatus
        """
    )


def downgrade() -> None:
    # Backfilled preferences may have been edited after this migration, so the
    # downgrade deliberately preserves data and only restores prior defaults.
    op.alter_column(
        "client_communication_preferences",
        "do_not_contact",
        server_default=None,
    )
    op.alter_column(
        "client_communication_preferences",
        "transactional_consent",
        server_default=None,
    )
    op.alter_column(
        "client_communication_preferences",
        "marketing_consent",
        server_default=None,
    )
