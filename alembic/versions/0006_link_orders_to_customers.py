"""link orders to customers

Revision ID: 0006_orders_customers
Revises: 0005_customers_otp
Create Date: 2026-04-16 15:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_orders_customers"
down_revision = "0005_customers_otp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("customer_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_orders_customer_id_customers",
        "orders",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute(
        sa.text(
            """
            UPDATE orders AS o
            SET customer_id = c.id
            FROM customers AS c
            WHERE regexp_replace(o.customer_phone, '[^0-9]', '', 'g')
                = regexp_replace(c.phone, '[^0-9]', '', 'g')
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_orders_customer_id_customers", "orders", type_="foreignkey")
    op.drop_column("orders", "customer_id")
