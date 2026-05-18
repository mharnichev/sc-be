"""rename old_price to recommended_retail_price

Revision ID: 0004_rrp_rename
Revises: 0003_short_desc_text
Create Date: 2026-04-16 12:55:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "0004_rrp_rename"
down_revision = "0003_short_desc_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("products", "old_price", new_column_name="recommended_retail_price")


def downgrade() -> None:
    op.alter_column("products", "recommended_retail_price", new_column_name="old_price")
