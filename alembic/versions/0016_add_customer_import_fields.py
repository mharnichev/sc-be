"""add customer import fields

Revision ID: 0016_customer_import_fields
Revises: 0015_booking_customers
Create Date: 2026-05-27 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_customer_import_fields"
down_revision = "0015_booking_customers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "imported_total_spent",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("customers", sa.Column("imported_last_visit_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "customers",
        sa.Column(
            "imported_is_new_client",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("customers", "imported_is_new_client")
    op.drop_column("customers", "imported_last_visit_at")
    op.drop_column("customers", "imported_total_spent")
