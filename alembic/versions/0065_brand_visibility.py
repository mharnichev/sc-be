"""add brand visibility

Revision ID: 0065_brand_visibility
Revises: 0064_master_telegram_only
Create Date: 2026-09-01 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0065_brand_visibility"
down_revision = "0064_master_telegram_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "brands",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("brands", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_column("brands", "is_active")
