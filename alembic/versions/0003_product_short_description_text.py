"""make short description text

Revision ID: 0003_short_desc_text
Revises: 0002_dropship_ext
Create Date: 2026-04-16 12:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_short_desc_text"
down_revision = "0002_dropship_ext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("products", "short_description", existing_type=sa.String(length=500), type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    op.alter_column("products", "short_description", existing_type=sa.Text(), type_=sa.String(length=500), existing_nullable=True)
