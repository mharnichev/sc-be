"""add booking service items

Revision ID: 0018_booking_service_items
Revises: 0017_booking_completed_at
Create Date: 2026-05-28 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_booking_service_items"
down_revision = "0017_booking_completed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_service_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["barber_services.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", "position", name="uq_booking_service_items_booking_position"),
        sa.UniqueConstraint("booking_id", "service_id", name="uq_booking_service_items_booking_service"),
    )
    op.create_index("ix_booking_service_items_booking_id", "booking_service_items", ["booking_id"])
    op.create_index("ix_booking_service_items_service_id", "booking_service_items", ["service_id"])
    op.execute(
        sa.text(
            """
            INSERT INTO booking_service_items (booking_id, service_id, position)
            SELECT id, service_id, 0
            FROM bookings
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_booking_service_items_service_id", table_name="booking_service_items")
    op.drop_index("ix_booking_service_items_booking_id", table_name="booking_service_items")
    op.drop_table("booking_service_items")
