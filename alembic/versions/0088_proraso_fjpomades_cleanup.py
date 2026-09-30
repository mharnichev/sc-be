"""Remove the legacy Proraso/FJ Pomades promo from product descriptions.

Revision ID: 0088_proraso_fjpomades_cleanup
Revises: 0087_morgans_official
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.utils.product_description_cleanup import remove_proraso_fjpomades_promo


revision = "0088_proraso_fjpomades_cleanup"
down_revision = "0087_morgans_official"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, description FROM products "
            "WHERE description ILIKE '%fjpomades.com/blog%'"
        )
    ).mappings()
    for row in rows:
        cleaned = remove_proraso_fjpomades_promo(row["description"])
        if cleaned != row["description"]:
            bind.execute(
                sa.text("UPDATE products SET description = :description WHERE id = :id"),
                {"id": row["id"], "description": cleaned},
            )


def downgrade() -> None:
    # The removed source block is legacy marketing copy and is not restored.
    pass
