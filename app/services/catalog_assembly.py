"""Shared catalog ordering, review aggregates, and product presentation.

Routers retain endpoint queries, pagination, pricing orchestration, and writes.
Response construction uses preloaded relationships and supplied batch context.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect as sa_inspect

from app.models.category import Category
from app.models.product import Product
from app.models.shop import ProductReview
from app.schemas.product import (
    CategoryPathItem,
    ProductVolumeVariantResponse,
    PublicProductResponse,
    ShopProductResponse,
)
from app.services.catalog_visibility import CatalogVisibility, VisibilityState
from app.services.shop_promotion import ShopPriceResult

CATALOG_IMAGE_LIMIT = 3


def product_order_clauses(sort: str | None, ordering: str | None) -> list[Any]:
    value = (sort or ordering or "newest").strip()
    mapping = {
        "newest": [Product.created_at.desc(), Product.id.desc()],
        "-created_at": [Product.created_at.desc(), Product.id.desc()],
        "created_at": [Product.created_at.asc(), Product.id.asc()],
        "price_asc": [Product.price.asc(), Product.id.asc()],
        "price": [Product.price.asc(), Product.id.asc()],
        "cheap": [Product.price.asc(), Product.id.asc()],
        "cheaper": [Product.price.asc(), Product.id.asc()],
        "price_desc": [Product.price.desc(), Product.id.desc()],
        "-price": [Product.price.desc(), Product.id.desc()],
        "expensive": [Product.price.desc(), Product.id.desc()],
        "name": [Product.name.asc(), Product.id.asc()],
        "name_asc": [Product.name.asc(), Product.id.asc()],
        "-name": [Product.name.desc(), Product.id.desc()],
        "name_desc": [Product.name.desc(), Product.id.desc()],
        "top": [Product.is_top.desc(), Product.top_score.desc(), Product.id.desc()],
        "popular": [Product.is_top.desc(), Product.top_score.desc(), Product.id.desc()],
        "-popularity": [Product.is_top.desc(), Product.top_score.desc(), Product.id.desc()],
        "-is_top": [Product.is_top.desc(), Product.top_score.desc(), Product.id.desc()],
    }
    if value not in mapping:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid sort value",
        )
    return mapping[value]


def _category_path(category_id: int | None, categories: dict[int, Category]) -> list[CategoryPathItem]:
    if category_id is None:
        return []
    path: list[CategoryPathItem] = []
    seen: set[int] = set()
    current = categories.get(category_id)
    while current and current.id not in seen:
        seen.add(current.id)
        path.append(CategoryPathItem(id=current.id, name=current.name, slug=current.slug))
        current = categories.get(current.parent_id) if current.parent_id else None
    path.reverse()
    return path


def loaded_relationship(instance: Any, relationship_name: str) -> bool:
    try:
        return relationship_name not in sa_inspect(instance).unloaded
    except NoInspectionAvailable:
        return hasattr(instance, relationship_name)


def _original_product_image_urls(product: Product) -> list[str]:
    if loaded_relationship(product, "images"):
        images = sorted(product.images, key=lambda image: (image.sort_order, image.id))
        if images:
            return _dedupe_urls(image.image_url for image in images if image.is_active and image.image_url)

    attrs = product.attributes_json if isinstance(product.attributes_json, dict) else {}
    image_urls = attrs.get("image_urls")
    if isinstance(image_urls, list):
        legacy_gallery = _dedupe_urls(str(item) for item in image_urls if item)
        if legacy_gallery:
            return legacy_gallery
    if isinstance(image_urls, str) and image_urls:
        return [image_urls]
    return [product.image_url] if product.image_url else []


def product_image_urls(product: Product) -> list[str]:
    original_urls = _original_product_image_urls(product)
    if not original_urls or not loaded_relationship(product, "image_variants"):
        return original_urls

    preferred_by_source = {
        variant.source_url: variant.output_url
        for variant in product.image_variants
        if variant.status == "succeeded" and variant.is_preferred and variant.output_url
    }
    return [preferred_by_source.get(url, url) for url in original_urls]


def _dedupe_urls(urls: Any) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def is_new_product(created_at: datetime, *, now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now(created_at.tzinfo)
    elif created_at.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif created_at.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=created_at.tzinfo)

    month_index = created_at.month - 1 + 3
    expires_year = created_at.year + month_index // 12
    expires_month = month_index % 12 + 1
    expires_day = min(created_at.day, monthrange(expires_year, expires_month)[1])
    expires_at = created_at.replace(year=expires_year, month=expires_month, day=expires_day)
    return created_at <= now < expires_at


async def review_stats(session: AsyncSession, product_ids: list[int]) -> dict[int, tuple[Decimal | None, int]]:
    if not product_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ProductReview.product_id,
                func.avg(ProductReview.rating).label("average_rating"),
                func.count(ProductReview.id).label("reviews_count"),
            )
            .where(ProductReview.product_id.in_(product_ids))
            .group_by(ProductReview.product_id)
        )
    ).all()
    stats: dict[int, tuple[Decimal | None, int]] = {}
    for row in rows:
        average = None
        if row.average_rating is not None:
            average = Decimal(str(row.average_rating)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        stats[row.product_id] = (average, int(row.reviews_count))
    return stats


def build_shop_product_response(
    product: Product,
    *,
    categories: dict[int, Category],
    stats: dict[int, tuple[Decimal | None, int]] | None = None,
    pricing: ShopPriceResult | None = None,
    visibility_state: VisibilityState,
    is_available_for_purchase: bool,
    volume_variants: list[ProductVolumeVariantResponse] | None = None,
    image_limit: int | None = None,
    now: datetime | None = None,
) -> ShopProductResponse:
    average_rating, reviews_count = (stats or {}).get(product.id, (None, 0))
    base = PublicProductResponse.model_validate(product).model_dump()
    gallery_urls = product_image_urls(product)[:image_limit]
    effective_price = pricing.price if pricing is not None else Decimal(product.price)
    base_price = pricing.base_price if pricing is not None else Decimal(product.price)
    compare_at_price = base_price if effective_price < base_price else None
    base["price"] = effective_price
    base["image_url"] = gallery_urls[0] if gallery_urls else None
    return ShopProductResponse(
        **base,
        base_price=base_price,
        images=gallery_urls,
        category_tree=_category_path(product.category_id, categories),
        compare_at_price=compare_at_price,
        discount_percent=pricing.discount_percent if pricing is not None else None,
        discount_amount=pricing.discount_amount if pricing is not None else Decimal("0.00"),
        promotion_id=pricing.promotion_id if pricing is not None else None,
        promotion_name=pricing.promotion_name if pricing is not None else None,
        promotion_code=pricing.promotion_code if pricing is not None else None,
        is_new=is_new_product(product.created_at, now=now),
        is_top=bool(product.is_top),
        average_rating=average_rating,
        reviews_count=reviews_count,
        volume_variants=volume_variants or [],
        is_effectively_visible=visibility_state.is_effectively_visible,
        hidden_reason=visibility_state.hidden_reason,
        is_available_for_purchase=is_available_for_purchase,
    )


def volume_variant_responses(
    products: list[Product],
    prices: dict[int, ShopPriceResult],
    visibility: CatalogVisibility,
) -> list[ProductVolumeVariantResponse]:
    variants: list[ProductVolumeVariantResponse] = []
    for product in products:
        if product.volume_ml is None:
            continue
        pricing = prices[product.id]
        compare_at_price = pricing.base_price if pricing.price < pricing.base_price else None
        image_urls = product_image_urls(product)
        variants.append(
            ProductVolumeVariantResponse(
                id=product.id,
                name=product.model_name or product.name,
                slug=product.slug,
                sku=product.sku,
                volume_ml=product.volume_ml,
                volume_label=f"{product.volume_ml} мл",
                price=pricing.price,
                base_price=pricing.base_price,
                compare_at_price=compare_at_price,
                image_url=image_urls[0] if image_urls else None,
                availability_status=product.availability_status,
                is_available=bool(
                    visibility.is_available_for_purchase(product)
                ),
            )
        )
    return variants
