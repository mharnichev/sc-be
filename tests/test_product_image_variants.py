from __future__ import annotations

from app.models.shop import ProductImageVariant


def test_new_variant_attempt_counter_can_be_incremented_before_flush() -> None:
    variant = ProductImageVariant(
        product_id=1,
        source_key="gallery:1",
        source_url="/media/products/1/original.webp",
        source_fingerprint="a" * 64,
        preset="studio_light",
        recipe_version="studio-light-v1",
    )

    variant.attempt_count = (variant.attempt_count or 0) + 1
    assert variant.attempt_count == 1
