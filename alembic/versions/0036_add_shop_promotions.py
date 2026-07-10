"""add shop promotions

Revision ID: 0036_shop_promotions
Revises: 0035_shop_ecommerce_api
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0036_shop_promotions"
down_revision = "0035_shop_ecommerce_api"
branch_labels = None
depends_on = None


shop_promotion_trigger = postgresql.ENUM(
    "automatic",
    "promocode",
    name="shoppromotiontrigger",
    create_type=False,
)
shop_promotion_discount_type = postgresql.ENUM(
    "percent",
    "fixed_amount",
    "fixed_price",
    name="shoppromotiondiscounttype",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    shop_promotion_trigger.create(bind, checkfirst=True)
    shop_promotion_discount_type.create(bind, checkfirst=True)

    op.create_table(
        "shop_promotions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("trigger", shop_promotion_trigger, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=True),
        sa.Column("discount_type", shop_promotion_discount_type, nullable=False),
        sa.Column("discount_value", sa.Numeric(10, 2), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column("usage_limit_per_customer", sa.Integer(), nullable=True),
        sa.Column("applies_to_all_products", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("include_subcategories", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shop_promotions")),
    )
    op.create_index("ix_shop_promotions_code", "shop_promotions", ["code"], unique=True)
    op.create_index(
        "ix_shop_promotions_active_period",
        "shop_promotions",
        ["is_active", "starts_at", "ends_at"],
        unique=False,
    )

    for table_name, target_table, target_column in (
        ("shop_promotion_products", "products", "product_id"),
        ("shop_promotion_categories", "categories", "category_id"),
        ("shop_promotion_brands", "brands", "brand_id"),
    ):
        op.create_table(
            table_name,
            sa.Column("promotion_id", sa.Integer(), nullable=False),
            sa.Column(target_column, sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["promotion_id"],
                ["shop_promotions.id"],
                name=op.f(f"fk_{table_name}_promotion_id_shop_promotions"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                [target_column],
                [f"{target_table}.id"],
                name=op.f(f"fk_{table_name}_{target_column}_{target_table}"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("promotion_id", target_column, name=op.f(f"pk_{table_name}")),
        )

    op.add_column("orders", sa.Column("subtotal_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "orders",
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("orders", sa.Column("promo_code", sa.String(length=50), nullable=True))
    op.execute(sa.text("UPDATE orders SET subtotal_amount = total_amount WHERE subtotal_amount IS NULL"))
    op.alter_column("orders", "subtotal_amount", nullable=False)

    op.add_column("order_items", sa.Column("base_price", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "order_items",
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("order_items", sa.Column("shop_promotion_id", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("promotion_name", sa.String(length=255), nullable=True))
    op.add_column("order_items", sa.Column("promotion_code", sa.String(length=50), nullable=True))
    op.execute(sa.text("UPDATE order_items SET base_price = price WHERE base_price IS NULL"))
    op.create_index(op.f("ix_order_items_shop_promotion_id"), "order_items", ["shop_promotion_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_order_items_shop_promotion_id_shop_promotions"),
        "order_items",
        "shop_promotions",
        ["shop_promotion_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_order_items_shop_promotion_id_shop_promotions"),
        "order_items",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_order_items_shop_promotion_id"), table_name="order_items")
    op.drop_column("order_items", "promotion_code")
    op.drop_column("order_items", "promotion_name")
    op.drop_column("order_items", "shop_promotion_id")
    op.drop_column("order_items", "discount_amount")
    op.drop_column("order_items", "base_price")

    op.drop_column("orders", "promo_code")
    op.drop_column("orders", "discount_amount")
    op.drop_column("orders", "subtotal_amount")

    op.drop_table("shop_promotion_brands")
    op.drop_table("shop_promotion_categories")
    op.drop_table("shop_promotion_products")
    op.drop_index("ix_shop_promotions_active_period", table_name="shop_promotions")
    op.drop_index("ix_shop_promotions_code", table_name="shop_promotions")
    op.drop_table("shop_promotions")

    shop_promotion_discount_type.drop(op.get_bind(), checkfirst=True)
    shop_promotion_trigger.drop(op.get_bind(), checkfirst=True)
