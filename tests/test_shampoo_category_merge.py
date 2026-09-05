from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.brand import Brand
from app.models.category import Category
from app.models.product import Product
from app.models.shop_promotion import shop_promotion_categories
from app.utils.import_products import ImportStats, get_or_create_category_tree


def test_shampoo_merge_preserves_products_children_promotions_and_reimport() -> None:
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0067_merge_shampoo_categories.py"
    spec = importlib.util.spec_from_file_location("shampoo_merge", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        Brand.__table__, Category.__table__, Product.__table__, shop_promotion_categories,
    ])
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()  # A clean database has nothing to merge.
        connection.execute(Category.__table__.insert(), [
            dict(id=1, name="КОСМЕТИКА", slug="kosmetika", parent_id=None),
            dict(id=2, name="ДЛЯ ВОЛОССЯ", slug="kosmetika-dlia-volossia", parent_id=1),
            dict(id=3, name="ДЛЯ БОРОДИ", slug="kosmetika-dlia-borodi", parent_id=1),
            dict(id=4, name="ШАМПУНІ", slug="kosmetika-dlia-volossia-shampuni", parent_id=2),
            dict(id=5, name="ШАМПУНЬ", slug="kosmetika-dlia-borodi-shampun", parent_id=3),
            dict(id=6, name="Child", slug="child", parent_id=5),
        ])
        connection.execute(Product.__table__.insert(), [
            dict(id=1, name="Hair", slug="hair", price=10, category_id=4, is_active=True),
            dict(id=2, name="Beard", slug="beard", price=20, category_id=5, is_active=True),
            dict(id=3, name="Hidden", slug="hidden", price=30, category_id=5, is_active=False),
        ])
        connection.execute(shop_promotion_categories.insert(), [
            dict(promotion_id=1, category_id=4), dict(promotion_id=1, category_id=5),
            dict(promotion_id=2, category_id=5),
        ])
        migration.upgrade()
        migration.upgrade()  # Safe to rerun without duplicating links.
        products = connection.execute(select(
            Product.id, Product.category_id, Product.is_active, Product.price,
        ).order_by(Product.id)).all()
        assert products == [(1, 4, True, 10), (2, 4, True, 20), (3, 4, False, 30)]
        categories = connection.execute(select(
            Category.id, Category.name, Category.slug, Category.parent_id,
        ).where(Category.id.in_([4, 5, 6])).order_by(Category.id)).all()
        assert categories == [
            (4, "ШАМПУНЬ", "kosmetika-dlia-volossia-shampun", 2), (6, "Child", "child", 4),
        ]
        assert set(connection.execute(select(shop_promotion_categories)).all()) == {(1, 4), (2, 4)}

    with Session(engine) as session:
        class AsyncSessionAdapter:
            async def execute(self, stmt: Any) -> Any:
                return session.execute(stmt)

        async def check_reimport() -> None:
            cache: dict[str, Category] = {}
            stats = ImportStats()
            for path in [
                "КОСМЕТИКА / ДЛЯ ВОЛОССЯ / ШАМПУНІ",
                "КОСМЕТИКА/ДЛЯ БОРОДИ/ШАМПУНЬ",
                "КОСМЕТИКА/ДЛЯ ВОЛОССЯ/ШАМПУНЬ",
            ]:
                category = await get_or_create_category_tree(AsyncSessionAdapter(), path, cache, stats)
                assert category.id == 4
            assert stats.categories_created == 0
            unrelated = await get_or_create_category_tree(
                AsyncSessionAdapter(), "КОСМЕТИКА/ДЛЯ БОРОДИ", cache, stats,
            )
            assert unrelated.id == 3

        asyncio.run(check_reimport())
    engine.dispose()
