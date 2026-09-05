"""Merge hair and beard shampoo into one catalog category.

Revision ID: 0067_merge_shampoo_categories
Revises: 0066_booking_sources
"""

from alembic import op
import sqlalchemy as sa


revision = "0067_merge_shampoo_categories"
down_revision = "0066_booking_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    categories = sa.table(
        "categories", sa.column("id"), sa.column("name"), sa.column("slug"),
        sa.column("parent_id"), sa.column("updated_at"),
    )
    products = sa.table("products", sa.column("category_id"), sa.column("updated_at"))
    promotions = sa.table(
        "shop_promotion_categories", sa.column("promotion_id"), sa.column("category_id"),
    )
    target_id = connection.execute(sa.select(categories.c.id).where(
        categories.c.slug.in_([
            "kosmetika-dlia-volossia-shampuni", "kosmetika-dlia-volossia-shampun",
        ])
    )).scalar_one_or_none()
    source_id = connection.execute(sa.select(categories.c.id).where(
        categories.c.slug == "kosmetika-dlia-borodi-shampun"
    )).scalar_one_or_none()
    if target_id is None:
        if source_id is not None:
            raise RuntimeError("Cannot merge shampoo: the hair category is missing")
        return

    if source_id is not None:
        connection.execute(products.update().where(products.c.category_id == source_id).values(
            category_id=target_id, updated_at=sa.func.now(),
        ))
        connection.execute(categories.update().where(categories.c.parent_id == source_id).values(
            parent_id=target_id, updated_at=sa.func.now(),
        ))
        existing_promotions = set(connection.execute(sa.select(promotions.c.promotion_id).where(
            promotions.c.category_id == target_id
        )).scalars())
        for promotion_id in connection.execute(sa.select(promotions.c.promotion_id).where(
            promotions.c.category_id == source_id
        )).scalars().all():
            if promotion_id not in existing_promotions:
                connection.execute(promotions.insert().values(
                    promotion_id=promotion_id, category_id=target_id,
                ))
        connection.execute(promotions.delete().where(promotions.c.category_id == source_id))
        connection.execute(categories.delete().where(categories.c.id == source_id))

    connection.execute(categories.update().where(categories.c.id == target_id).values(
        name="ШАМПУНЬ", slug="kosmetika-dlia-volossia-shampun", updated_at=sa.func.now(),
    ))


def downgrade() -> None:
    # The original category of each product cannot be inferred after a merge.
    # Keep the merged data when rolling back the application schema.
    pass
