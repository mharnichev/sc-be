"""Remove the tattoo-care category from the catalog.

Revision ID: 0089_tattoo_category_cleanup
Revises: 0088_proraso_fjpomades_cleanup
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0089_tattoo_category_cleanup"
down_revision = "0088_proraso_fjpomades_cleanup"
branch_labels = None
depends_on = None

def _category_id(connection: sa.Connection, slug: str) -> int | None:
    return connection.execute(
        sa.text("SELECT id FROM categories WHERE slug = :slug"),
        {"slug": slug},
    ).scalar_one_or_none()


def upgrade() -> None:
    connection = op.get_bind()
    source_id = _category_id(connection, "kosmetika-dlia-tila-dogliad-za-tatuiuvanniam")
    if source_id is None:
        return
    target_id = _category_id(connection, "kosmetika-dlia-tila")
    if target_id is None:
        raise RuntimeError("Cannot remove tattoo-care category: parent category is missing")

    connection.execute(
        sa.text(
            "UPDATE products SET category_id = :target_id, updated_at = now() "
            "WHERE category_id = :source_id"
        ),
        {"target_id": target_id, "source_id": source_id},
    )

    promotion_ids = connection.execute(
        sa.text(
            "SELECT promotion_id FROM shop_promotion_categories "
            "WHERE category_id = :source_id"
        ),
        {"source_id": source_id},
    ).scalars().all()
    for promotion_id in promotion_ids:
        target_exists = connection.execute(
            sa.text(
                "SELECT 1 FROM shop_promotion_categories "
                "WHERE promotion_id = :promotion_id AND category_id = :target_id"
            ),
            {"promotion_id": promotion_id, "target_id": target_id},
        ).scalar_one_or_none()
        if target_exists:
            connection.execute(
                sa.text(
                    "DELETE FROM shop_promotion_categories "
                    "WHERE promotion_id = :promotion_id AND category_id = :source_id"
                ),
                {"promotion_id": promotion_id, "source_id": source_id},
            )
        else:
            connection.execute(
                sa.text(
                    "UPDATE shop_promotion_categories SET category_id = :target_id "
                    "WHERE promotion_id = :promotion_id AND category_id = :source_id"
                ),
                {"promotion_id": promotion_id, "source_id": source_id, "target_id": target_id},
            )

    connection.execute(
        sa.text("UPDATE categories SET parent_id = :target_id WHERE parent_id = :source_id"),
        {"target_id": target_id, "source_id": source_id},
    )
    connection.execute(
        sa.text("DELETE FROM categories WHERE id = :source_id"),
        {"source_id": source_id},
    )


def downgrade() -> None:
    # The original category assignment cannot be reconstructed after removal.
    pass
