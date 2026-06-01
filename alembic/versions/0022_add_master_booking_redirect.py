"""add master booking redirect

Revision ID: 0022_master_booking_redirect
Revises: 0021_master_block_visibility
Create Date: 2026-06-01 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_master_booking_redirect"
down_revision = "0021_master_block_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("masters", sa.Column("booking_redirect_master_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_masters_booking_redirect_master_id",
        "masters",
        ["booking_redirect_master_id"],
    )
    op.create_foreign_key(
        "fk_masters_booking_redirect_master_id_masters",
        "masters",
        "masters",
        ["booking_redirect_master_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("bookings", sa.Column("redirected_from_master_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_bookings_redirected_from_master_id",
        "bookings",
        ["redirected_from_master_id"],
    )
    op.create_foreign_key(
        "fk_bookings_redirected_from_master_id_masters",
        "bookings",
        "masters",
        ["redirected_from_master_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_bookings_redirected_from_master_id_masters", "bookings", type_="foreignkey")
    op.drop_index("ix_bookings_redirected_from_master_id", table_name="bookings")
    op.drop_column("bookings", "redirected_from_master_id")

    op.drop_constraint("fk_masters_booking_redirect_master_id_masters", "masters", type_="foreignkey")
    op.drop_index("ix_masters_booking_redirect_master_id", table_name="masters")
    op.drop_column("masters", "booking_redirect_master_id")
