"""link bookings to customers

Revision ID: 0015_booking_customers
Revises: 0014_localized_service_fields
Create Date: 2026-05-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_booking_customers"
down_revision = "0014_localized_service_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("customer_id", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("customer_email", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_bookings_customer_id_customers",
        "bookings",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_bookings_customer_id", "bookings", ["customer_id"])

    op.execute(
        sa.text(
            """
            UPDATE bookings
            SET customer_id = (
                SELECT customers.id
                FROM customers
                WHERE customers.phone = bookings.customer_phone
                LIMIT 1
            )
            WHERE bookings.customer_id IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM customers
                    WHERE customers.phone = bookings.customer_phone
                )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_bookings_customer_id", table_name="bookings")
    op.drop_constraint("fk_bookings_customer_id_customers", "bookings", type_="foreignkey")
    op.drop_column("bookings", "customer_email")
    op.drop_column("bookings", "customer_id")
    op.drop_column("customers", "notes")
