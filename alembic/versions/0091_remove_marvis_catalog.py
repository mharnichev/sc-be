"""Remove Marvis and its oral-care category from the catalog.

Revision ID: 0091_remove_marvis_catalog
Revises: 0090_tattoo_path_cleanup
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0091_remove_marvis_catalog"
down_revision = "0090_tattoo_path_cleanup"
branch_labels = None
depends_on = None

BRAND_SLUG = "marvis"
CATEGORY_SLUG = "kosmetika-dlia-tila-dlia-porozhnini-rota"


def upgrade() -> None:
    connection = op.get_bind()
    brand_id = connection.execute(
        sa.text("SELECT id FROM brands WHERE slug = :slug"), {"slug": BRAND_SLUG}
    ).scalar_one_or_none()
    category_id = connection.execute(
        sa.text("SELECT id FROM categories WHERE slug = :slug"), {"slug": CATEGORY_SLUG}
    ).scalar_one_or_none()

    if brand_id is not None:
        has_order_history = connection.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM order_items oi "
                "JOIN products p ON p.id = oi.product_id WHERE p.brand_id = :brand_id)"
            ),
            {"brand_id": brand_id},
        ).scalar_one()
        if has_order_history:
            raise RuntimeError("Cannot remove Marvis products: order history references them")

    if category_id is not None:
        has_other_products = connection.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM products "
                "WHERE category_id = :category_id AND brand_id IS DISTINCT FROM :brand_id)"
            ),
            {"category_id": category_id, "brand_id": brand_id},
        ).scalar_one()
        if has_other_products:
            raise RuntimeError("Cannot remove oral-care category: it contains non-Marvis products")
        has_children = connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM categories WHERE parent_id = :category_id)"),
            {"category_id": category_id},
        ).scalar_one()
        if has_children:
            raise RuntimeError("Cannot remove oral-care category: it has child categories")

    if brand_id is not None:
        connection.execute(
            sa.text("DELETE FROM products WHERE brand_id = :brand_id"),
            {"brand_id": brand_id},
        )
        connection.execute(
            sa.text("DELETE FROM brands WHERE id = :brand_id"),
            {"brand_id": brand_id},
        )

    if category_id is not None:
        connection.execute(
            sa.text("DELETE FROM categories WHERE id = :category_id"),
            {"category_id": category_id},
        )


def downgrade() -> None:
    # Deleted product descriptions and the original catalog records cannot be reconstructed.
    pass
