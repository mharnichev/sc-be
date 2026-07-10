from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.api.v1.routes import brands as brands_routes
from app.dependencies.common import PaginationParams
from app.models.brand import Brand
from app.schemas.brand import BrandCreate, BrandResponse, BrandUpdate


def brand(*, logo_url: str | None) -> Brand:
    now = datetime.now(UTC)
    return Brand(
        id=1,
        name="American Crew",
        slug="american-crew",
        description=None,
        logo_url=logo_url,
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


def test_public_brand_list_can_filter_to_active_products(monkeypatch: Any) -> None:
    class CapturingRepository:
        statement: Any = None

        async def list(self, _session: Any, *, stmt: Any, page: int, page_size: int) -> tuple[list[Brand], int]:
            self.statement = stmt
            assert page == 1
            assert page_size == 100
            return [brand(logo_url="/uploads/brands/american-crew.webp")], 1

    repository = CapturingRepository()
    monkeypatch.setattr(brands_routes, "repo", repository)

    response = asyncio.run(
        brands_routes.list_brands(
            pagination=PaginationParams(page=1, page_size=100),
            search=None,
            has_active_products=True,
            session=object(),
        )
    )

    statement = str(repository.statement)
    assert "EXISTS" in statement
    assert "products.is_active IS true" in statement
    assert response.items[0].logo_url == "/uploads/brands/american-crew.webp"


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
