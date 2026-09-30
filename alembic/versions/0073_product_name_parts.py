"""Store separated product name fields.

Revision ID: 0073_product_name_parts
Revises: 0072_product_image_variants
"""
from alembic import op
import sqlalchemy as sa

revision = "0073_product_name_parts"
down_revision = "0072_product_image_variants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("old_name", sa.String(length=255), nullable=True))
    op.add_column("products", sa.Column("model_name", sa.String(length=255), nullable=True))
    op.add_column("products", sa.Column("product_type", sa.String(length=255), nullable=True))
    op.add_column("products", sa.Column("package_size", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "package_size")
    op.drop_column("products", "product_type")
    op.drop_column("products", "model_name")
    op.drop_column("products", "old_name")
