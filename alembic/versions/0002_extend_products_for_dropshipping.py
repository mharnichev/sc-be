"""extend products for dropshipping import

Revision ID: 0002_dropship_ext
Revises: 0001_initial
Create Date: 2026-04-16 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_dropship_ext"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("parent_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_categories_parent_id_categories",
        "categories",
        "categories",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("products", sa.Column("image_url", sa.String(length=500), nullable=True))
    op.add_column("products", sa.Column("external_url", sa.String(length=500), nullable=True))
    op.add_column("products", sa.Column("availability_status", sa.String(length=32), nullable=True))
    op.add_column("products", sa.Column("attributes_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "attributes_json")
    op.drop_column("products", "availability_status")
    op.drop_column("products", "external_url")
    op.drop_column("products", "image_url")
    op.drop_constraint("fk_categories_parent_id_categories", "categories", type_="foreignkey")
    op.drop_column("categories", "parent_id")
