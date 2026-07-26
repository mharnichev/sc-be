from __future__ import annotations

from calendar import monthrange
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.orm import selectinload
from slugify import slugify

from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_customer, get_optional_current_customer
from app.dependencies.common import PaginationDep, parse_optional_bool_query, parse_optional_int_query
from app.models.brand import Brand
from app.models.category import Category
from app.models.customer import Customer
from app.models.product import Product
from app.models.shop import ProductReview, ProductReviewComment
from app.repositories.base import BaseRepository
from app.schemas.category import CategoryResponse
from app.schemas.common import PaginatedResponse
from app.schemas.product import (
    CategoryPathItem,
    ProductCreate,
    ProductResponse,
    ProductReviewCommentCreate,
    ProductReviewCommentResponse,
    ProductReviewCreate,
    ProductReviewListResponse,
    ProductReviewResponse,
    ProductSearchResponse,
    ProductUpdate,
    ProductViewResponse,
    ProductVolumeVariantResponse,
    ShopProductResponse,
)
from app.services.product_popularity import build_visitor_hash, product_popularity_service
from app.services.product import ProductService
from app.services.shop_promotion import ShopPriceResult, shop_promotion_service

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Product)
service = ProductService()

_EXCLUDED_ATTRIBUTE_KEYS = {
    "images",
    "image_urls",
    "gallery",
    "filters",
    "description",
    "short_description",
    "external_url",
}


async def _paginate(
    session: AsyncSession,
    stmt: Select[tuple[Product]],
    *,
    limit: int,
    offset: int,
) -> tuple[list[Product], int]:
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    result = await session.execute(stmt.offset(offset).limit(limit))
    return list(result.scalars().all()), total


def _pagination_from_aliases(
    pagination: Any,
    *,
    limit: int | None,
    offset: int | None,
) -> tuple[int, int, int, int]:
    page_size = limit or pagination.page_size
    page_offset = offset if offset is not None else (pagination.page - 1) * pagination.page_size
    page = page_offset // page_size + 1
    return page_size, page_offset, page, page_size


def _product_order_clauses(sort: str | None, ordering: str | None) -> list[Any]:
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


async def _categories_by_id(session: AsyncSession) -> dict[int, Category]:
    categories = (await session.execute(select(Category))).scalars().all()
    return {category.id: category for category in categories}


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


def _loaded_relationship(instance: Any, relationship_name: str) -> bool:
    try:
        return relationship_name not in sa_inspect(instance).unloaded
    except Exception:
        return hasattr(instance, relationship_name)


def product_image_urls(product: Product) -> list[str]:
    urls: list[str] = []
    if _loaded_relationship(product, "images"):
        urls.extend(
            image.image_url
            for image in sorted(product.images, key=lambda image: (image.sort_order, image.id))
            if image.is_active and image.image_url
        )

    attrs = product.attributes_json if isinstance(product.attributes_json, dict) else {}
    for key in ("image_urls", "images", "gallery"):
        value = attrs.get(key)
        if isinstance(value, list):
            urls.extend(str(item) for item in value if item)
        elif isinstance(value, str) and value:
            urls.append(value)

    if product.image_url:
        urls.append(product.image_url)

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _discount_percent(price: Decimal, compare_at_price: Decimal | None) -> Decimal | None:
    if compare_at_price is None or compare_at_price <= price:
        return None
    discount = ((compare_at_price - price) / compare_at_price) * Decimal("100")
    return discount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _is_new_product(created_at: datetime, *, now: datetime | None = None) -> bool:
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


async def _review_stats(session: AsyncSession, product_ids: list[int]) -> dict[int, tuple[Decimal | None, int]]:
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
    volume_variants: list[ProductVolumeVariantResponse] | None = None,
    now: datetime | None = None,
) -> ShopProductResponse:
    average_rating, reviews_count = (stats or {}).get(product.id, (None, 0))
    base = ProductResponse.model_validate(product).model_dump()
    effective_price = pricing.price if pricing is not None else Decimal(product.price)
    base_price = pricing.base_price if pricing is not None else Decimal(product.price)
    compare_at_price = product.recommended_retail_price
    if effective_price < base_price and (compare_at_price is None or compare_at_price < base_price):
        compare_at_price = base_price
    base["price"] = effective_price
    return ShopProductResponse(
        **base,
        base_price=base_price,
        images=product_image_urls(product),
        category_tree=_category_path(product.category_id, categories),
        compare_at_price=compare_at_price,
        discount_percent=_discount_percent(effective_price, compare_at_price),
        discount_amount=pricing.discount_amount if pricing is not None else Decimal("0.00"),
        promotion_id=pricing.promotion_id if pricing is not None else None,
        promotion_name=pricing.promotion_name if pricing is not None else None,
        promotion_code=pricing.promotion_code if pricing is not None else None,
        is_new=_is_new_product(product.created_at, now=now),
        is_top=bool(product.is_top),
        average_rating=average_rating,
        reviews_count=reviews_count,
        volume_variants=volume_variants or [],
    )


async def _volume_variant_products(session: AsyncSession, product: Product) -> list[Product]:
    if product.variant_group_key is None or product.volume_ml is None:
        return []
    return list(
        (
            await session.execute(
                select(Product)
                .where(Product.variant_group_key == product.variant_group_key, Product.volume_ml.is_not(None))
                .order_by(Product.volume_ml.asc(), Product.id.asc())
            )
        )
        .scalars()
        .all()
    )


def _volume_variant_responses(
    products: list[Product],
    prices: dict[int, ShopPriceResult],
) -> list[ProductVolumeVariantResponse]:
    variants: list[ProductVolumeVariantResponse] = []
    for product in products:
        if product.volume_ml is None:
            continue
        pricing = prices[product.id]
        compare_at_price = product.recommended_retail_price
        if pricing.price < pricing.base_price and (
            compare_at_price is None or compare_at_price < pricing.base_price
        ):
            compare_at_price = pricing.base_price
        image_urls = product_image_urls(product)
        variants.append(
            ProductVolumeVariantResponse(
                id=product.id,
                name=product.name,
                slug=product.slug,
                sku=product.sku,
                volume_ml=product.volume_ml,
                volume_label=f"{product.volume_ml} мл",
                price=pricing.price,
                base_price=pricing.base_price,
                compare_at_price=compare_at_price,
                image_url=image_urls[0] if image_urls else None,
                stock_quantity=product.stock_quantity,
                availability_status=product.availability_status,
                is_available=bool(
                    product.is_active
                    and product.stock_quantity > 0
                    and product.availability_status != "out_of_stock"
                ),
            )
        )
    return variants


async def _active_product_stmt() -> Select[tuple[Product]]:
    return (
        select(Product)
        .options(
            selectinload(Product.brand),
            selectinload(Product.category),
            selectinload(Product.images),
        )
        .where(Product.is_active.is_(True))
    )


def _customer_display_name(customer: Customer | None) -> str | None:
    if customer is None:
        return None
    value = " ".join(part for part in (customer.name, customer.surname) if part).strip()
    return value or customer.phone


def _review_response(review: ProductReview) -> ProductReviewResponse:
    comments_count = len(review.comments) if _loaded_relationship(review, "comments") else 0
    return ProductReviewResponse(
        id=review.id,
        product_id=review.product_id,
        customer_id=review.customer_id,
        customer_name=_customer_display_name(review.customer if _loaded_relationship(review, "customer") else None),
        rating=review.rating,
        comment=review.comment,
        comments_count=comments_count,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _review_comment_response(comment: ProductReviewComment) -> ProductReviewCommentResponse:
    return ProductReviewCommentResponse(
        id=comment.id,
        review_id=comment.review_id,
        customer_id=comment.customer_id,
        customer_name=_customer_display_name(comment.customer if _loaded_relationship(comment, "customer") else None),
        comment=comment.comment,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


@public_router.get("/search", response_model=ProductSearchResponse)
async def search_products(
    q: str = Query(min_length=3),
    limit: int = Query(default=8, ge=1, le=20),
    session: AsyncSession = Depends(get_db_session),
) -> ProductSearchResponse:
    pattern = f"%{q.strip()}%"
    product_stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images))
        .where(
            Product.is_active.is_(True),
            or_(
                Product.name.ilike(pattern),
                Product.description.ilike(pattern),
                Product.short_description.ilike(pattern),
                Product.sku.ilike(pattern),
            ),
        )
        .order_by(Product.name.asc())
        .limit(limit)
    )
    category_stmt = (
        select(Category)
        .where(Category.is_active.is_(True), Category.name.ilike(pattern))
        .order_by(Category.name.asc())
        .limit(limit)
    )
    products = list((await session.execute(product_stmt)).scalars().all())
    categories = list((await session.execute(category_stmt)).scalars().all())
    categories_by_id = await _categories_by_id(session)
    stats = await _review_stats(session, [product.id for product in products])
    prices = await shop_promotion_service.price_products(session, products)
    suggestions = [product.name for product in products[:5]]
    suggestions.extend(category.name for category in categories[:5] if category.name not in suggestions)
    return ProductSearchResponse(
        suggestions=suggestions[:limit],
        products=[
            build_shop_product_response(
                product,
                categories=categories_by_id,
                stats=stats,
                pricing=prices[product.id],
            )
            for product in products
        ],
        categories=[CategoryResponse.model_validate(category) for category in categories],
    )


@public_router.get("/by-slug/{slug}", response_model=ShopProductResponse)
async def get_product_by_slug(slug: str, session: AsyncSession = Depends(get_db_session)) -> ShopProductResponse:
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images))
        .where(Product.slug == slug, Product.is_active.is_(True))
    )
    product = (await session.execute(stmt)).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    categories = await _categories_by_id(session)
    stats = await _review_stats(session, [product.id])
    variant_products = await _volume_variant_products(session, product)
    price_products = variant_products or [product]
    prices = await shop_promotion_service.price_products(session, price_products)
    volume_variants = _volume_variant_responses(variant_products, prices)
    return build_shop_product_response(
        product,
        categories=categories,
        stats=stats,
        pricing=prices[product.id],
        volume_variants=volume_variants,
    )


@public_router.get("", response_model=PaginatedResponse[ShopProductResponse])
async def list_products(
    pagination: PaginationDep,
    category_id: int | None = Query(default=None),
    brand_id: int | None = Query(default=None),
    category_slug: str | None = Query(default=None),
    brand_slug: str | None = Query(default=None),
    is_top: bool | None = Query(default=None),
    search: str | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    ordering: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ShopProductResponse]:
    stmt = await _active_product_stmt()
    if category_slug:
        category = (
            await session.execute(select(Category).where(Category.slug == category_slug, Category.is_active.is_(True)))
        ).scalar_one_or_none()
        if not category:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
        category_id = category.id
    if brand_slug:
        brand = (await session.execute(select(Brand).where(Brand.slug == brand_slug))).scalar_one_or_none()
        if not brand:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
        brand_id = brand.id
    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
    if brand_id:
        stmt = stmt.where(Product.brand_id == brand_id)
    if is_top is not None:
        stmt = stmt.where(Product.is_top.is_(is_top))
    term = (q or search or "").strip()
    if term:
        pattern = f"%{term}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(pattern),
                Product.description.ilike(pattern),
                Product.short_description.ilike(pattern),
                Product.sku.ilike(pattern),
            )
        )
    sort_value = (sort or ordering or "newest").strip()
    stmt = stmt.order_by(*_product_order_clauses(sort, ordering))
    page_size, page_offset, page, response_page_size = _pagination_from_aliases(
        pagination,
        limit=limit,
        offset=offset,
    )
    price_sort_ascending = {"price_asc", "price", "cheap", "cheaper"}
    price_sort_descending = {"price_desc", "-price", "expensive"}
    if sort_value in price_sort_ascending | price_sort_descending:
        all_items = list((await session.execute(stmt)).scalars().all())
        all_prices = await shop_promotion_service.price_products(session, all_items)
        all_items.sort(
            key=lambda item: (
                -all_prices[item.id].price if sort_value in price_sort_descending else all_prices[item.id].price,
                item.id,
            )
        )
        total = len(all_items)
        items = all_items[page_offset : page_offset + page_size]
        prices = {item.id: all_prices[item.id] for item in items}
    else:
        items, total = await _paginate(session, stmt, limit=page_size, offset=page_offset)
        prices = await shop_promotion_service.price_products(session, items)
    categories = await _categories_by_id(session)
    stats = await _review_stats(session, [item.id for item in items])
    return PaginatedResponse[ShopProductResponse](
        total=total,
        page=page,
        page_size=response_page_size,
        items=[
            build_shop_product_response(item, categories=categories, stats=stats, pricing=prices[item.id])
            for item in items
        ],
    )


@public_router.get("/{product_id}/reviews", response_model=ProductReviewListResponse)
async def list_product_reviews(
    product_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> ProductReviewListResponse:
    product = await session.get(Product, product_id)
    if not product or not product.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    reviews = list(
        (
            await session.execute(
                select(ProductReview)
                .options(selectinload(ProductReview.customer), selectinload(ProductReview.comments))
                .where(ProductReview.product_id == product_id)
                .order_by(ProductReview.created_at.desc(), ProductReview.id.desc())
            )
        )
        .scalars()
        .all()
    )
    stats = await _review_stats(session, [product_id])
    average_rating, reviews_count = stats.get(product_id, (None, 0))
    return ProductReviewListResponse(
        total=reviews_count,
        average_rating=average_rating,
        items=[_review_response(review) for review in reviews],
    )


@public_router.post("/{product_id}/reviews", response_model=ProductReviewResponse, status_code=status.HTTP_201_CREATED)
async def upsert_product_review(
    product_id: int,
    payload: ProductReviewCreate,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> ProductReviewResponse:
    product = await session.get(Product, product_id)
    if not product or not product.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    review = (
        await session.execute(
            select(ProductReview)
            .options(selectinload(ProductReview.customer), selectinload(ProductReview.comments))
            .where(ProductReview.product_id == product_id, ProductReview.customer_id == current_customer.id)
        )
    ).scalar_one_or_none()
    if review:
        review.rating = payload.rating
        review.comment = payload.comment
    else:
        review = ProductReview(
            product_id=product_id,
            customer_id=current_customer.id,
            rating=payload.rating,
            comment=payload.comment,
        )
        session.add(review)
    await session.commit()
    refreshed = (
        await session.execute(
            select(ProductReview)
            .options(selectinload(ProductReview.customer), selectinload(ProductReview.comments))
            .where(ProductReview.id == review.id)
        )
    ).scalar_one()
    return _review_response(refreshed)


@public_router.delete("/{product_id}/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_review(
    product_id: int,
    review_id: int,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    review = await session.get(ProductReview, review_id)
    if not review or review.product_id != product_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    if review.customer_id != current_customer.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another customer's review")
    await session.delete(review)
    await session.commit()


@public_router.get("/reviews/{review_id}/comments", response_model=list[ProductReviewCommentResponse])
async def list_review_comments(
    review_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> list[ProductReviewCommentResponse]:
    comments = list(
        (
            await session.execute(
                select(ProductReviewComment)
                .options(selectinload(ProductReviewComment.customer))
                .where(ProductReviewComment.review_id == review_id)
                .order_by(ProductReviewComment.created_at.asc(), ProductReviewComment.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_review_comment_response(comment) for comment in comments]


@public_router.post(
    "/reviews/{review_id}/comments",
    response_model=ProductReviewCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_review_comment(
    review_id: int,
    payload: ProductReviewCommentCreate,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> ProductReviewCommentResponse:
    review = await session.get(ProductReview, review_id)
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    comment = ProductReviewComment(
        review_id=review_id,
        customer_id=current_customer.id,
        comment=payload.comment,
    )
    session.add(comment)
    await session.commit()
    refreshed = (
        await session.execute(
            select(ProductReviewComment)
            .options(selectinload(ProductReviewComment.customer))
            .where(ProductReviewComment.id == comment.id)
        )
    ).scalar_one()
    return _review_comment_response(refreshed)


@public_router.delete("/reviews/{review_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review_comment(
    review_id: int,
    comment_id: int,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    comment = await session.get(ProductReviewComment, comment_id)
    if not comment or comment.review_id != review_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    if comment.customer_id != current_customer.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another customer's comment")
    await session.delete(comment)
    await session.commit()


@public_router.post(
    "/{product_id}/view",
    response_model=ProductViewResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def record_product_view(
    product_id: int,
    request: Request,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> ProductViewResponse:
    product = await session.get(Product, product_id)
    if not product or not product.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    visitor_hash = build_visitor_hash(
        secret=settings.secret_key,
        customer_id=current_customer.id if current_customer is not None else None,
        visitor_id=request.headers.get("x-visitor-id"),
        client_host=request.client.host if request.client is not None else None,
        user_agent=request.headers.get("user-agent"),
    )
    result = await product_popularity_service.record_view(
        session,
        product_id=product.id,
        visitor_hash=visitor_hash,
    )
    return ProductViewResponse(recorded=result.recorded, viewed_on=result.viewed_on)


@public_router.get("/{product_id}", response_model=ShopProductResponse)
async def get_product(product_id: int, session: AsyncSession = Depends(get_db_session)) -> ShopProductResponse:
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images))
        .where(Product.id == product_id)
    )
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()
    if not product or not product.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    categories = await _categories_by_id(session)
    stats = await _review_stats(session, [product.id])
    variant_products = await _volume_variant_products(session, product)
    price_products = variant_products or [product]
    prices = await shop_promotion_service.price_products(session, price_products)
    volume_variants = _volume_variant_responses(variant_products, prices)
    return build_shop_product_response(
        product,
        categories=categories,
        stats=stats,
        pricing=prices[product.id],
        volume_variants=volume_variants,
    )


@backoffice_router.get("", response_model=PaginatedResponse[ProductResponse])
async def backoffice_list_products(
    pagination: PaginationDep,
    is_active: str | None = Query(default=None),
    availability_status: str | None = Query(default=None),
    category_id: str | None = Query(default=None),
    brand_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ProductResponse]:
    parsed_is_active = parse_optional_bool_query(is_active, "is_active")
    parsed_category_id = parse_optional_int_query(category_id, "category_id")
    parsed_brand_id = parse_optional_int_query(brand_id, "brand_id")
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category))
        .order_by(Product.created_at.desc())
    )
    if parsed_is_active is not None:
        stmt = stmt.where(Product.is_active.is_(parsed_is_active))
    if availability_status:
        stmt = stmt.where(Product.availability_status == availability_status)
    if parsed_category_id is not None:
        stmt = stmt.where(Product.category_id == parsed_category_id)
    if parsed_brand_id is not None:
        stmt = stmt.where(Product.brand_id == parsed_brand_id)
    if search:
        stmt = stmt.where(Product.name.ilike(f"%{search}%"))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[ProductResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[ProductResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/{product_id}", response_model=ProductResponse)
async def backoffice_get_product(
    product_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ProductResponse:
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category))
        .where(Product.id == product_id)
    )
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return ProductResponse.model_validate(product)


@backoffice_router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ProductResponse:
    product = await service.create_product(session, payload.model_dump())
    return ProductResponse.model_validate(product)


@backoffice_router.put("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ProductResponse:
    product = await repo.get(session, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    updated = await service.update_product(session, product, payload.model_dump(exclude_unset=True))
    return ProductResponse.model_validate(updated)


@backoffice_router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    product = await repo.get(session, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    await service.delete_product(session, product)
