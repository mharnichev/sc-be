from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import bad_request, not_found
from app.models.category import Category
from app.models.product import Product


class CategoryService:
    async def create_category(self, session: AsyncSession, data: dict) -> Category:
        await self._validate_slug_uniqueness(session, slug=data["slug"])
        parent_id = data.get("parent_id")
        if parent_id is not None:
            await self._get_existing_category(session, parent_id)

        category = Category(**data)
        session.add(category)
        await session.commit()
        await session.refresh(category)
        return category

    async def update_category(self, session: AsyncSession, category: Category, data: dict) -> Category:
        slug = data.get("slug")
        if slug and slug != category.slug:
            await self._validate_slug_uniqueness(session, slug=slug, exclude_id=category.id)

        if "parent_id" in data:
            parent_id = data["parent_id"]
            if parent_id == category.id:
                raise bad_request("Category cannot be its own parent")
            if parent_id is not None:
                await self._get_existing_category(session, parent_id)
                await self._ensure_no_cycle(session, category_id=category.id, parent_id=parent_id)

        for key, value in data.items():
            setattr(category, key, value)

        await session.commit()
        await session.refresh(category)
        return category

    async def delete_category(self, session: AsyncSession, category: Category) -> None:
        children_count = (
            await session.execute(select(func.count()).select_from(Category).where(Category.parent_id == category.id))
        ).scalar_one()
        if children_count:
            raise bad_request("Category has child categories and cannot be deleted")

        products_count = (
            await session.execute(select(func.count()).select_from(Product).where(Product.category_id == category.id))
        ).scalar_one()
        if products_count:
            raise bad_request("Category has linked products and cannot be deleted")

        await session.delete(category)
        await session.commit()

    async def _validate_slug_uniqueness(
        self,
        session: AsyncSession,
        *,
        slug: str,
        exclude_id: int | None = None,
    ) -> None:
        stmt = select(Category).where(Category.slug == slug)
        if exclude_id is not None:
            stmt = stmt.where(Category.id != exclude_id)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            raise bad_request("Category slug already exists")

    async def _get_existing_category(self, session: AsyncSession, category_id: int) -> Category:
        category = await session.get(Category, category_id)
        if not category:
            raise not_found("Parent category not found")
        return category

    async def _ensure_no_cycle(self, session: AsyncSession, *, category_id: int, parent_id: int) -> None:
        current_parent_id = parent_id
        while current_parent_id is not None:
            if current_parent_id == category_id:
                raise bad_request("Category parent relationship would create a cycle")
            parent = await session.get(Category, current_parent_id)
            if parent is None:
                break
            current_parent_id = parent.parent_id
