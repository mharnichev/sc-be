"""add master block visibility flag

Revision ID: 0021_master_block_visibility
Revises: 0020_master_localized
Create Date: 2026-05-31 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_master_block_visibility"
down_revision = "0020_master_localized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "masters",
        sa.Column("show_on_master_block", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.alter_column("masters", "show_on_master_block", server_default=None)


def downgrade() -> None:
    op.drop_column("masters", "show_on_master_block")
