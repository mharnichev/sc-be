"""add monthly service popularity cache

Revision ID: 0048_service_popularity
Revises: 0047_free_service_promotion
Create Date: 2026-07-26 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0048_service_popularity"
down_revision = "0047_free_service_promotion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("base_services", sa.Column("popularity_rank", sa.Integer(), nullable=True))
    op.add_column(
        "base_services",
        sa.Column(
            "popularity_booking_count_30d",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "base_services",
        sa.Column("popularity_calculated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_base_services_popularity_sort",
        "base_services",
        ["popularity_rank", "name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_base_services_popularity_sort", table_name="base_services")
    op.drop_column("base_services", "popularity_calculated_at")
    op.drop_column("base_services", "popularity_booking_count_30d")
    op.drop_column("base_services", "popularity_rank")
