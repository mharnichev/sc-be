from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.v1.routes import brands as brands_routes
from app.dependencies.common import PaginationParams
from app.models.brand import Brand
from app.models.category import Category
from app.models.product import Product
from app.schemas.brand import BrandCreate, BrandResponse, BrandUpdate
from app.services.catalog_visibility import CatalogVisibility


def brand(*, logo_url: str | None, is_active: bool = True) -> Brand:
    now = datetime.now(UTC)
    return Brand(
        id=1,
        name="American Crew",
        slug="american-crew",
        description=None,
        logo_url=logo_url,
        is_active=is_active,
        created_at=now,
        updated_at=now,
    )


def test_brand_schemas_accept_and_return_logo_url() -> None:
    logo_url = "https://cdn.example.com/brands/american-crew.webp"
    create_payload = BrandCreate(
        name="American Crew",
        slug="american-crew",
        logo_url=logo_url,
    )
    update_payload = BrandUpdate(logo_url=None)

    response = BrandResponse.model_validate(brand(logo_url=create_payload.logo_url))

    assert create_payload.logo_url == logo_url
    assert update_payload.model_dump(exclude_unset=True) == {"logo_url": None}
    assert response.logo_url == logo_url
    assert response.is_active is True


def test_brand_update_accepts_visibility_status() -> None:
    update_payload = BrandUpdate(is_active=False)

    assert update_payload.model_dump(exclude_unset=True) == {"is_active": False}


@pytest.mark.parametrize("has_active_products", [True, False])
def test_public_brand_list_always_filters_to_active_products(monkeypatch: Any, has_active_products: bool) -> None:
    class CapturingRepository:
        statement: Any = None

        async def list(self, _session: Any, *, stmt: Any, page: int, page_size: int) -> tuple[list[Brand], int]:
            self.statement = stmt
            assert page == 1
            assert page_size == 100
            return [brand(logo_url="/uploads/brands/american-crew.webp")], 1

    repository = CapturingRepository()
    monkeypatch.setattr(brands_routes, "repo", repository)

    async def load_visibility(_session: Any) -> CatalogVisibility:
        return CatalogVisibility.from_categories([])

    monkeypatch.setattr(brands_routes.CatalogVisibility, "load", load_visibility)

    response = asyncio.run(
        brands_routes.list_brands(
            pagination=PaginationParams(page=1, page_size=100),
            search=None,
            has_active_products=has_active_products,
            session=object(),
        )
    )

    statement = str(repository.statement)
    assert "brands.is_active IS true" in statement
    assert "EXISTS" in statement
    assert "products.is_active IS true" in statement
    assert response.items[0].logo_url == "/uploads/brands/american-crew.webp"


def test_public_brands_visibility_search_and_pagination() -> None:
    engine = create_engine("sqlite://")
    Brand.metadata.create_all(engine, tables=[Brand.__table__, Category.__table__, Product.__table__])
    with Session(engine) as session:
        session.add_all([
            Category(id=1, name="Visible", slug="visible", is_active=True),
            Category(id=2, name="Hidden", slug="hidden", is_active=False),
            Category(id=3, name="Hidden parent", slug="hidden-parent", parent_id=2, is_active=True),
        ])
        for brand_id, name in enumerate([
            "A empty", "B hidden product", "C hidden category", "D hidden ancestor",
            "E mixed", "F unavailable", "G uncategorized", "H inactive brand",
        ], start=1):
            session.add(Brand(id=brand_id, name=name, slug=str(brand_id), is_active=brand_id != 8))
        for product_id, (brand_id, category_id, active) in enumerate([
            (2, 1, False), (3, 2, True), (4, 3, True),
            (5, 1, False), (5, 1, True), (5, 1, True),
            (6, 1, True), (7, None, True), (8, 1, True),
        ], start=1):
            session.add(Product(
                id=product_id, name=str(product_id), slug=str(product_id), price=10,
                brand_id=brand_id, category_id=category_id, is_active=active,
                stock_quantity=0, availability_status="out_of_stock",
            ))
        session.commit()

        class AsyncSessionAdapter:
            async def execute(self, stmt: Any) -> Any:
                return session.execute(stmt)

        async def check() -> None:
            adapter = AsyncSessionAdapter()
            first = await brands_routes.list_brands(
                pagination=PaginationParams(page=1, page_size=2), search=None, session=adapter,
            )
            assert first.total == 3
            assert [item.name for item in first.items] == ["E mixed", "F unavailable"]
            second = await brands_routes.list_brands(
                pagination=PaginationParams(page=2, page_size=2), search=None,
                has_active_products=False, session=adapter,
            )
            assert second.total == 3
            assert [item.name for item in second.items] == ["G uncategorized"]
            hidden = await brands_routes.list_brands(
                pagination=PaginationParams(page=1, page_size=20), search="hidden", session=adapter,
            )
            assert hidden.total == 0
            assert hidden.items == []
            backoffice = await brands_routes.backoffice_list_brands(
                pagination=PaginationParams(page=1, page_size=20), search=None,
                _=object(), session=adapter,
            )
            assert backoffice.total == 8

        asyncio.run(check())
    engine.dispose()


def test_backoffice_brand_create_persists_logo_url(monkeypatch: Any) -> None:
    class CreatingRepository:
        data: dict[str, Any] | None = None

        async def create(self, _session: Any, data: dict[str, Any]) -> Brand:
            self.data = data
            return brand(logo_url=data["logo_url"])

    repository = CreatingRepository()
    monkeypatch.setattr(brands_routes, "repo", repository)
    logo_url = "/uploads/images/american-crew.webp"

    response = asyncio.run(
        brands_routes.create_brand(
            payload=BrandCreate(name="American Crew", slug="american-crew", logo_url=logo_url),
            _=object(),
            session=object(),
        )
    )

    assert repository.data is not None
    assert repository.data["logo_url"] == logo_url
    assert response.logo_url == logo_url


def test_backoffice_brand_update_persists_visibility_status(monkeypatch: Any) -> None:
    class UpdatingRepository:
        data: dict[str, Any] | None = None

        async def get(self, _session: Any, brand_id: int) -> Brand:
            assert brand_id == 1
            return brand(logo_url=None)

        async def update(self, _session: Any, item: Brand, data: dict[str, Any]) -> Brand:
            self.data = data
            item.is_active = data["is_active"]
            return item

    repository = UpdatingRepository()
    monkeypatch.setattr(brands_routes, "repo", repository)

    response = asyncio.run(
        brands_routes.update_brand(
            brand_id=1,
            payload=BrandUpdate(is_active=False),
            _=object(),
            session=object(),
        )
    )

    assert repository.data == {"is_active": False}
    assert response.is_active is False
