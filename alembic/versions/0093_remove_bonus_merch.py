"""Remove the bonuses/merch catalog section and its products.

Revision ID: 0093_bonus_merch_cleanup
Revises: 0092_clip_trim_cleanup
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0093_bonus_merch_cleanup"
down_revision = "0092_clip_trim_cleanup"
branch_labels = None
depends_on = None


ROOT_CATEGORY_SLUG = "bonusi"


def _category_tree_ids(connection: sa.Connection) -> list[int]:
    category_ids = set(
        connection.execute(
            sa.text("SELECT id FROM categories WHERE slug = :slug"),
            {"slug": ROOT_CATEGORY_SLUG},
        ).scalars()
    )
    frontier = set(category_ids)
    while frontier:
        children = set(
            connection.execute(
                sa.text("SELECT id FROM categories WHERE parent_id IN :parent_ids").bindparams(
                    sa.bindparam("parent_ids", expanding=True)
                ),
                {"parent_ids": tuple(frontier)},
            ).scalars()
        )
        frontier = children - category_ids
        category_ids.update(frontier)
    return sorted(category_ids)


def upgrade() -> None:
    connection = op.get_bind()
    category_ids = _category_tree_ids(connection)
    if not category_ids:
        return

    has_order_history = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM order_items oi "
            "JOIN products p ON p.id = oi.product_id "
            "WHERE p.category_id IN :category_ids)"
        ).bindparams(sa.bindparam("category_ids", expanding=True)),
        {"category_ids": tuple(category_ids)},
    ).scalar_one()
    if has_order_history:
        raise RuntimeError("Cannot remove bonuses/merch products: order history references them")

    connection.execute(
        sa.text("DELETE FROM products WHERE category_id IN :category_ids").bindparams(
            sa.bindparam("category_ids", expanding=True)
        ),
        {"category_ids": tuple(category_ids)},
    )
    connection.execute(
        sa.text("DELETE FROM categories WHERE id IN :category_ids").bindparams(
            sa.bindparam("category_ids", expanding=True)
        ),
        {"category_ids": tuple(category_ids)},
    )


def downgrade() -> None:
    # Product and category records are not reconstructable after removal.
    pass
