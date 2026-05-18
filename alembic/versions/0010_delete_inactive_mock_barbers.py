"""delete inactive mock barbers

Revision ID: 0010_inactive_barbers
Revises: 0009_base_barber_services
Create Date: 2026-05-13 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_inactive_barbers"
down_revision = "0009_base_barber_services"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
