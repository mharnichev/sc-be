from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.product import Product
from app.models.shop import ProductImage
from app.utils.process_product_images import SourceImage, _source_suffix, source_images


def _product(*, images: list[ProductImage]) -> Product:
    now = datetime.now(UTC)
    return Product(
        id=7,
        name="Product",
        slug="product",
        price=Decimal("10"),
        stock_quantity=1,
        is_active=True,
        image_url="https://legacy.example/main.jpg",
        attributes_json={"image_urls": ["https://legacy.example/gallery.jpg"]},
        images=images,
        created_at=now,
        updated_at=now,
    )


def test_source_images_prefers_active_gallery_without_rewriting_legacy_fields() -> None:
    now = datetime.now(UTC)
    product = _product(
        images=[
            ProductImage(id=9, product_id=7, image_url="/media/products/7/original.webp", sort_order=0, is_active=True, created_at=now, updated_at=now),
            ProductImage(id=10, product_id=7, image_url="/media/products/7/hidden.webp", sort_order=1, is_active=False, created_at=now, updated_at=now),
        ]
    )

    assert source_images(product) == [
        SourceImage(7, 9, "gallery:9", "/media/products/7/original.webp")
    ]


def test_source_images_uses_legacy_urls_only_when_gallery_is_absent() -> None:
    sources = source_images(_product(images=[]))
    assert [item.source_url for item in sources] == ["https://legacy.example/gallery.jpg"]
    assert sources[0].source_key.startswith("legacy:")


def test_downloaded_source_keeps_a_supported_extension() -> None:
    assert _source_suffix("https://example.test/image.jpeg", None) == ".jpeg"
    assert _source_suffix("https://example.test/image", "image/webp; charset=binary") == ".webp"
    assert _source_suffix("https://example.test/image", None) == ".jpg"
