"""Add a dedicated product composition field.

Revision ID: 0070_product_ingredients
Revises: 0069_sms_queue_throttling
"""
from alembic import op
import sqlalchemy as sa

revision = "0070_product_ingredients"
down_revision = "0069_sms_queue_throttling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("ingredients", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "ingredients")
