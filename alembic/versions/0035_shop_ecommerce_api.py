"""add shop ecommerce api support

Revision ID: 0035_shop_ecommerce_api
Revises: 0034_add_promotion_scopes
Create Date: 2026-07-07 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0035_shop_ecommerce_api"
down_revision = "0034_add_promotion_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("first_name", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("last_name", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("shipping_company", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("shipping_method", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("shipping_area", sa.String(length=255), nullable=True))
    op.add_column("orders", sa.Column("shipping_city", sa.String(length=255), nullable=True))
    op.add_column("orders", sa.Column("shipping_warehouse_number", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("shipping_street", sa.String(length=255), nullable=True))
    op.add_column("orders", sa.Column("building_number", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("shipping_apartment", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("delivery_address", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("shipping_payload_json", sa.JSON(), nullable=True))
    op.add_column("orders", sa.Column("payment_method", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("tracking_number", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("external_id", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("external_sync_status", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("external_sync_error", sa.Text(), nullable=True))

    op.add_column("order_items", sa.Column("product_name", sa.String(length=255), nullable=True))
    op.add_column("order_items", sa.Column("product_sku", sa.String(length=100), nullable=True))
    op.add_column("order_items", sa.Column("total_price", sa.Numeric(10, 2), nullable=True))

    op.create_table(
        "product_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("upload_id", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("alt", sa.String(length=255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_images_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["uploads.id"],
            name=op.f("fk_product_images_upload_id_uploads"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_images")),
    )
    op.create_index(op.f("ix_product_images_product_id"), "product_images", ["product_id"], unique=False)

    op.create_table(
        "customer_cart_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_customer_cart_items_customer_cart_items_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_customer_cart_items_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_customer_cart_items_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_cart_items")),
        sa.UniqueConstraint("customer_id", "product_id", name="uq_customer_cart_items_customer_product"),
    )
    op.create_index(op.f("ix_customer_cart_items_customer_id"), "customer_cart_items", ["customer_id"], unique=False)
    op.create_index(op.f("ix_customer_cart_items_product_id"), "customer_cart_items", ["product_id"], unique=False)

    op.create_table(
        "customer_wishlist_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_customer_wishlist_items_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_customer_wishlist_items_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_wishlist_items")),
        sa.UniqueConstraint("customer_id", "product_id", name="uq_customer_wishlist_items_customer_product"),
    )
    op.create_index(op.f("ix_customer_wishlist_items_customer_id"), "customer_wishlist_items", ["customer_id"], unique=False)
    op.create_index(op.f("ix_customer_wishlist_items_product_id"), "customer_wishlist_items", ["product_id"], unique=False)

    op.create_table(
        "product_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name=op.f("ck_product_reviews_product_reviews_rating_range")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_product_reviews_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_reviews_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_reviews")),
        sa.UniqueConstraint("product_id", "customer_id", name="uq_product_reviews_product_customer"),
    )
    op.create_index(op.f("ix_product_reviews_customer_id"), "product_reviews", ["customer_id"], unique=False)
    op.create_index(op.f("ix_product_reviews_product_id"), "product_reviews", ["product_id"], unique=False)

    op.create_table(
        "product_review_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("review_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_product_review_comments_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["product_reviews.id"],
            name=op.f("fk_product_review_comments_review_id_product_reviews"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_review_comments")),
    )
    op.create_index(op.f("ix_product_review_comments_customer_id"), "product_review_comments", ["customer_id"], unique=False)
    op.create_index(op.f("ix_product_review_comments_review_id"), "product_review_comments", ["review_id"], unique=False)

    op.create_table(
        "delivery_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cache_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_cache")),
    )
    op.create_index(op.f("ix_delivery_cache_cache_key"), "delivery_cache", ["cache_key"], unique=True)
    op.create_index(op.f("ix_delivery_cache_expires_at"), "delivery_cache", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_delivery_cache_expires_at"), table_name="delivery_cache")
    op.drop_index(op.f("ix_delivery_cache_cache_key"), table_name="delivery_cache")
    op.drop_table("delivery_cache")

    op.drop_index(op.f("ix_product_review_comments_review_id"), table_name="product_review_comments")
    op.drop_index(op.f("ix_product_review_comments_customer_id"), table_name="product_review_comments")
    op.drop_table("product_review_comments")

    op.drop_index(op.f("ix_product_reviews_product_id"), table_name="product_reviews")
    op.drop_index(op.f("ix_product_reviews_customer_id"), table_name="product_reviews")
    op.drop_table("product_reviews")

    op.drop_index(op.f("ix_customer_wishlist_items_product_id"), table_name="customer_wishlist_items")
    op.drop_index(op.f("ix_customer_wishlist_items_customer_id"), table_name="customer_wishlist_items")
    op.drop_table("customer_wishlist_items")

    op.drop_index(op.f("ix_customer_cart_items_product_id"), table_name="customer_cart_items")
    op.drop_index(op.f("ix_customer_cart_items_customer_id"), table_name="customer_cart_items")
    op.drop_table("customer_cart_items")

    op.drop_index(op.f("ix_product_images_product_id"), table_name="product_images")
    op.drop_table("product_images")

    op.drop_column("order_items", "total_price")
    op.drop_column("order_items", "product_sku")
    op.drop_column("order_items", "product_name")

    op.drop_column("orders", "external_sync_error")
    op.drop_column("orders", "external_sync_status")
    op.drop_column("orders", "external_id")
    op.drop_column("orders", "tracking_number")
    op.drop_column("orders", "payment_method")
    op.drop_column("orders", "shipping_payload_json")
    op.drop_column("orders", "delivery_address")
    op.drop_column("orders", "shipping_apartment")
    op.drop_column("orders", "building_number")
    op.drop_column("orders", "shipping_street")
    op.drop_column("orders", "shipping_warehouse_number")
    op.drop_column("orders", "shipping_city")
    op.drop_column("orders", "shipping_area")
    op.drop_column("orders", "shipping_method")
    op.drop_column("orders", "shipping_company")
    op.drop_column("orders", "last_name")
    op.drop_column("orders", "first_name")
