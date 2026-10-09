"""Fence generated image attempts with recoverable leases.

Revision ID: 0100_image_attempt_leases
Revises: 0099_campaign_offers
"""
from alembic import op
import sqlalchemy as sa


revision = "0100_image_attempt_leases"
down_revision = "0099_campaign_offers"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("product_image_variants", sa.Column("attempt_token", sa.String(36), nullable=True))
    op.add_column("product_image_variants", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("product_image_variants", "lease_expires_at")
    op.drop_column("product_image_variants", "attempt_token")
