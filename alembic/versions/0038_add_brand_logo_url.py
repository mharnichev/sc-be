"""add brand logo URL

Revision ID: 0038_add_brand_logo_url
Revises: 0037_product_top_popularity
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0038_add_brand_logo_url"
down_revision = "0037_product_top_popularity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("brands", sa.Column("logo_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("brands", "logo_url")
