"""delete inactive mock barbers with cancelled bookings

Revision ID: 0011_cancelled_barbers
Revises: 0010_inactive_barbers
Create Date: 2026-05-13 12:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_cancelled_barbers"
down_revision = "0010_inactive_barbers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM bookings
            WHERE master_id IN (
                SELECT id
                FROM masters
                WHERE is_active IS FALSE
                    AND NOT EXISTS (
                        SELECT 1
                        FROM bookings active_bookings
                        WHERE active_bookings.master_id = masters.id
                            AND active_bookings.status <> 'cancelled'
                    )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM masters
            WHERE is_active IS FALSE
                AND NOT EXISTS (
                    SELECT 1
                    FROM bookings
                    WHERE bookings.master_id = masters.id
                )
            """
        )
    )


def downgrade() -> None:
    pass
