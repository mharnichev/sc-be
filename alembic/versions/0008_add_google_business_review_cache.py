"""add google business review cache

Revision ID: 0008_google_reviews_cache
Revises: 0007_barber_bookings
Create Date: 2026-05-11 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_google_reviews_cache"
down_revision = "0007_barber_bookings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_business_review_caches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source"),
    )
    op.create_index("ix_google_business_review_caches_source", "google_business_review_caches", ["source"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_google_business_review_caches_source", table_name="google_business_review_caches")
    op.drop_table("google_business_review_caches")
