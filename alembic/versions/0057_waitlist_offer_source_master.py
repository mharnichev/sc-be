"""preserve public source master on waitlist offers

Revision ID: 0057_waitlist_offer_source
Revises: 0056_customer_activity
Create Date: 2026-08-08 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0057_waitlist_offer_source"
down_revision = "0056_customer_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("waitlist_offers", sa.Column("source_master_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_waitlist_offers_source_master_id_masters",
        "waitlist_offers",
        "masters",
        ["source_master_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE waitlist_offers AS offer
        SET source_master_id = COALESCE(booking.redirected_from_master_id, offer.master_id)
        FROM bookings AS booking
        WHERE offer.source_booking_id = booking.id
          AND offer.source_master_id IS NULL
        """
    )
    op.execute("UPDATE waitlist_offers SET source_master_id = master_id WHERE source_master_id IS NULL")
    op.create_index(
        op.f("ix_waitlist_offers_source_master_id"),
        "waitlist_offers",
        ["source_master_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_waitlist_offers_source_master_id"), table_name="waitlist_offers")
    op.drop_constraint(
        "fk_waitlist_offers_source_master_id_masters",
        "waitlist_offers",
        type_="foreignkey",
    )
    op.drop_column("waitlist_offers", "source_master_id")
