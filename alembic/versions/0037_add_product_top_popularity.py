"""add product TOP popularity cache

Revision ID: 0037_product_top_popularity
Revises: 0036_shop_promotions
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0037_product_top_popularity"
down_revision = "0036_shop_promotions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("is_top", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "products",
        sa.Column("top_score", sa.Numeric(8, 6), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("products", sa.Column("top_rank", sa.Integer(), nullable=True))
    op.add_column(
        "products",
        sa.Column("top_unique_views_30d", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "products",
        sa.Column("top_paid_orders_30d", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "products",
        sa.Column("top_purchased_units_30d", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("products", sa.Column("top_calculated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_products_is_top"), "products", ["is_top"], unique=False)
    op.create_index("ix_products_top_sort", "products", ["is_top", "top_score"], unique=False)

    op.create_table(
        "product_views",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("visitor_hash", sa.String(length=64), nullable=False),
        sa.Column("viewed_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_views_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_views")),
        sa.UniqueConstraint(
            "product_id",
            "visitor_hash",
            "viewed_on",
            name="uq_product_views_product_visitor_day",
        ),
    )
    op.create_index(op.f("ix_product_views_viewed_on"), "product_views", ["viewed_on"], unique=False)
    op.create_index(
        "ix_product_views_product_viewed_on",
        "product_views",
        ["product_id", "viewed_on"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_product_views_product_viewed_on", table_name="product_views")
    op.drop_index(op.f("ix_product_views_viewed_on"), table_name="product_views")
    op.drop_table("product_views")

    op.drop_index("ix_products_top_sort", table_name="products")
    op.drop_index(op.f("ix_products_is_top"), table_name="products")
    op.drop_column("products", "top_calculated_at")
    op.drop_column("products", "top_purchased_units_30d")
    op.drop_column("products", "top_paid_orders_30d")
    op.drop_column("products", "top_unique_views_30d")
    op.drop_column("products", "top_rank")
    op.drop_column("products", "top_score")
    op.drop_column("products", "is_top")
