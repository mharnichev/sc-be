from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import bad_request, not_found
from app.models.brand import Brand
from app.models.category import Category
from app.models.order import OrderItem
from app.models.product import Product


class ProductService:
    async def create_product(self, session: AsyncSession, data: dict) -> Product:
        await self._validate_brand_and_category(session, data)
        await self._validate_uniqueness(session, slug=data["slug"], sku=data.get("sku"))

        product = Product(**data)
        session.add(product)
        await session.commit()
        await session.refresh(product)
        return product

    async def update_product(self, session: AsyncSession, product: Product, data: dict) -> Product:
        await self._validate_brand_and_category(session, data)
        await self._validate_uniqueness(
            session,
            slug=data.get("slug"),
            sku=data.get("sku"),
            exclude_id=product.id,
        )

        for key, value in data.items():
            setattr(product, key, value)

        await session.commit()
        await session.refresh(product)
        return product

    async def delete_product(self, session: AsyncSession, product: Product) -> None:
        order_item_count = (
            await session.execute(select(func.count()).select_from(OrderItem).where(OrderItem.product_id == product.id))
        ).scalar_one()
        if order_item_count:
            raise bad_request("Product is linked to existing orders and cannot be deleted")

        await session.delete(product)
        await session.commit()

    async def _validate_brand_and_category(self, session: AsyncSession, data: dict) -> None:
        brand_id = data.get("brand_id")
        category_id = data.get("category_id")

        if brand_id is not None:
            brand = await session.get(Brand, brand_id)
            if not brand:
                raise not_found("Brand not found")

        if category_id is not None:
            category = await session.get(Category, category_id)
            if not category:
                raise not_found("Category not found")

    async def _validate_uniqueness(
        self,
        session: AsyncSession,
        *,
        slug: str | None = None,
        sku: str | None = None,
        exclude_id: int | None = None,
    ) -> None:
        if slug:
            stmt = select(Product).where(Product.slug == slug)
            if exclude_id is not None:
                stmt = stmt.where(Product.id != exclude_id)
            existing_slug = (await session.execute(stmt)).scalar_one_or_none()
            if existing_slug:
                raise bad_request("Product slug already exists")

        if sku:
            stmt = select(Product).where(Product.sku == sku)
            if exclude_id is not None:
                stmt = stmt.where(Product.id != exclude_id)
            existing_sku = (await session.execute(stmt)).scalar_one_or_none()
            if existing_sku:
                raise bad_request("Product sku already exists")
