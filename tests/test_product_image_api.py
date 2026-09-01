from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.routes import products as products_routes
from app.main import app
from app.models.product import Product
from app.models.shop import ProductImage
from app.schemas.product import ProductImageResponse, ProductImageUpdate
from app.services.catalog_visibility import CatalogVisibility


def _timestamp() -> datetime:
    return datetime.now(UTC)


def _product(*, product_id: int = 1, images: list[ProductImage] | None = None) -> Product:
    product = Product(
        id=product_id,
        name="Clipper",
        slug=f"clipper-{product_id}",
        price=Decimal("100.00"),
        stock_quantity=1,
        is_active=True,
        image_url="https://legacy.example/primary.jpg",
        attributes_json={"image_urls": ["https://legacy.example/gallery.jpg"]},
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )
    if images is not None:
        product.images = images
    return product


def _image(*, image_id: int, product_id: int = 1, url: str, sort_order: int, active: bool = True) -> ProductImage:
    timestamp = _timestamp()
    return ProductImage(
        id=image_id,
        product_id=product_id,
        upload_id=image_id,
        image_url=url,
        alt=f"Image {image_id}",
        sort_order=sort_order,
        is_active=active,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_shop_gallery_uses_active_product_images_without_legacy_urls() -> None:
    product = _product(
        images=[
            _image(image_id=2, url="https://cdn.example/second.webp", sort_order=1),
            _image(image_id=1, url="https://cdn.example/first.webp", sort_order=0),
            _image(image_id=3, url="https://cdn.example/hidden.webp", sort_order=2, active=False),
        ]
    )

    assert products_routes.product_image_urls(product) == [
        "https://cdn.example/first.webp",
        "https://cdn.example/second.webp",
    ]


def test_shop_gallery_falls_back_to_image_urls_then_product_image_url() -> None:
    attrs_product = _product(images=[])
    assert products_routes.product_image_urls(attrs_product) == ["https://legacy.example/gallery.jpg"]

    legacy_product = _product(images=[])
    legacy_product.attributes_json = {}
    assert products_routes.product_image_urls(legacy_product) == ["https://legacy.example/primary.jpg"]


def test_backoffice_product_response_contains_sorted_images() -> None:
    product = _product(
        images=[
            _image(image_id=2, url="https://cdn.example/second.webp", sort_order=1),
            _image(image_id=1, url="https://cdn.example/first.webp", sort_order=0),
        ]
    )
    response = products_routes._backoffice_product_response(product, CatalogVisibility.from_categories([]))

    assert [image.id for image in response.images] == [1, 2]
    assert all(isinstance(image, ProductImageResponse) for image in response.images)


def test_product_image_routes_delegate_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    image = _image(image_id=1, url="https://cdn.example/first.webp", sort_order=0)
    session = object()
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def list_images(*args: object) -> list[ProductImage]:
        calls.append(("list", args))
        return [image]

    async def reorder_images(*args: object) -> list[ProductImage]:
        calls.append(("reorder", args))
        return [image]

    monkeypatch.setattr(products_routes.product_image_service, "list_images", list_images)
    monkeypatch.setattr(products_routes.product_image_service, "reorder_images", reorder_images)

    listed = asyncio.run(products_routes.list_product_images(7, object(), session))
    reordered = asyncio.run(
        products_routes.reorder_product_images(
            7,
            SimpleNamespace(image_ids=[1]),
            object(),
            session,
        )
    )

    assert listed[0].id == 1
    assert reordered[0].sort_order == 0
    assert calls[0][0] == "list"
    assert calls[0][1] == (session, 7)
    assert calls[1][0] == "reorder"
    assert calls[1][1] == (session, 7, [1])


def test_product_image_endpoints_are_in_openapi() -> None:
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert "/api/v1/backoffice/products/{product_id}/images" in paths
    assert "/api/v1/backoffice/products/{product_id}/images/reorder" in paths
    assert "/api/v1/backoffice/products/{product_id}/images/{image_id}" in paths
    assert "/api/v1/backoffice/products/{product_id}/images/{image_id}/file" in paths


def test_product_image_patch_rejects_null_active_but_allows_omission() -> None:
    assert ProductImageUpdate().model_dump(exclude_unset=True) == {}

    with pytest.raises(ValidationError):
        ProductImageUpdate.model_validate({"is_active": None})
