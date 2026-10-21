from __future__ import annotations

from typing import Annotated

from decimal import Decimal
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.shop_input import SearchText
from app.dependencies.auth import (
    get_current_admin_user,
    get_catalog_manager,
    get_current_customer,
    get_optional_current_customer,
)
from app.dependencies.common import (
    PaginationDep,
    parse_optional_bool_query,
    parse_optional_int_query,
)
from app.models.brand import Brand
from app.models.category import Category
from app.models.customer import Customer
from app.models.product import Product
from app.models.admin_user import AdminUser
from app.models.shop import ProductImageVariant, ProductReview, ProductReviewComment
from app.repositories.base import BaseRepository
from app.schemas.category import CategoryResponse
from app.schemas.common import PaginatedResponse
from app.schemas.product import (
    BackofficeProductResponse,
    ProductCreate,
    ProductImageReorderRequest,
    ProductImageResponse,
    ProductImageVariantResponse,
    ProductImageUpdate,
    ProductResponse,
    ProductReviewCommentCreate,
    ProductReviewCommentResponse,
    ProductReviewCreate,
    ProductReviewListResponse,
    ProductReviewResponse,
    ProductSearchResponse,
    ProductUpdate,
    ProductViewResponse,
    ShopProductResponse,
)
# Compatibility reexports preserve existing helper imports and route-local patches.
from app.services.catalog_assembly import (
    CATALOG_IMAGE_LIMIT,
    product_stock_order_clause,
    product_stock_sort_key,
    _category_path,
    _dedupe_urls,
    _original_product_image_urls,
    build_shop_product_response,
    is_new_product as _is_new_product,
    loaded_relationship as _loaded_relationship,
    product_image_urls,
    product_order_clauses as _product_order_clauses,
    review_stats as _review_stats,
    volume_variant_responses as _volume_variant_responses,
)
from app.services.catalog_visibility import CatalogVisibility
from app.services.product import ProductService
from app.services.product_images import product_image_service
from app.services.product_popularity import (
    build_visitor_hash,
    product_popularity_service,
)
from app.services.shop_promotion import shop_promotion_service

from app.services.shop_ingress_limits import admit_shop_subject

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


async def _volume_variant_products(
    session: AsyncSession,
    product: Product,
    visibility: CatalogVisibility,
) -> list[Product]:
    if product.variant_group_key is None or product.volume_ml is None:
        return []
    return list(
        (
            await session.execute(
                select(Product)
                .options(selectinload(Product.images), selectinload(Product.image_variants))
                .where(
                    Product.variant_group_key == product.variant_group_key,
                    Product.volume_ml.is_not(None),
                    visibility.visible_product_clause(),
                )
                .order_by(product_stock_order_clause(), Product.volume_ml.asc(), Product.id.asc())
            )
        )
        .scalars()
        .all()
    )


def _active_product_stmt(visibility: CatalogVisibility) -> Select[tuple[Product]]:
    return (
        select(Product)
        .options(
            selectinload(Product.brand),
            selectinload(Product.category),
            selectinload(Product.images),
            selectinload(Product.image_variants),
        )
        .where(visibility.visible_product_clause())
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
    q: Annotated[SearchText, Query(min_length=3, max_length=120)],
    limit: int = Query(default=8, ge=1, le=20),
    session: AsyncSession = Depends(get_db_session),
) -> ProductSearchResponse:
    visibility = await CatalogVisibility.load(session)
    pattern = f"%{q.strip()}%"
    product_stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images), selectinload(Product.image_variants))
        .where(
            visibility.visible_product_clause(),
            or_(
                Product.name.ilike(pattern),
                Product.name_en.ilike(pattern),
                Product.old_name.ilike(pattern),
                Product.model_name.ilike(pattern),
                Product.product_type.ilike(pattern),
                Product.product_type_en.ilike(pattern),
                Product.description.ilike(pattern),
                Product.description_en.ilike(pattern),
                Product.short_description.ilike(pattern),
                Product.short_description_en.ilike(pattern),
                Product.sku.ilike(pattern),
            ),
        )
        .order_by(*_product_order_clauses("name", None))
        .limit(limit)
    )
    category_stmt = (
        select(Category)
        .where(visibility.visible_category_clause(), or_(Category.name.ilike(pattern), Category.name_en.ilike(pattern)))
        .order_by(Category.name.asc())
        .limit(limit)
    )
    products = list((await session.execute(product_stmt)).scalars().all())
    categories = list((await session.execute(category_stmt)).scalars().all())
    categories_by_id = visibility.categories_by_id
    stats = await _review_stats(session, [product.id for product in products])
    prices = await shop_promotion_service.price_products(
        session,
        products,
        category_parents=visibility.category_parents(),
    )
    suggestions = [product.model_name or product.name for product in products[:5]]
    suggestions.extend(category.name for category in categories[:5] if category.name not in suggestions)
    return ProductSearchResponse(
        suggestions=suggestions[:limit],
        products=[
            build_shop_product_response(
                product,
                categories=categories_by_id,
                image_limit=CATALOG_IMAGE_LIMIT,
                stats=stats,
                pricing=prices[product.id],
                visibility_state=visibility.product_state(product),
                is_available_for_purchase=visibility.is_available_for_purchase(product),
            )
            for product in products
        ],
        categories=[CategoryResponse.model_validate(category) for category in categories],
    )


@public_router.get("/by-slug/{slug}", response_model=ShopProductResponse)
async def get_product_by_slug(slug: str, session: AsyncSession = Depends(get_db_session)) -> ShopProductResponse:
    visibility = await CatalogVisibility.load(session)
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images), selectinload(Product.image_variants))
        .where(Product.slug == slug, visibility.visible_product_clause())
    )
    product = (await session.execute(stmt)).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [product.id])
    variant_products = await _volume_variant_products(session, product, visibility)
    price_products = variant_products or [product]
    prices = await shop_promotion_service.price_products(
        session,
        price_products,
        category_parents=visibility.category_parents(),
    )
    volume_variants = _volume_variant_responses(variant_products, prices, visibility)
    return build_shop_product_response(
        product,
        categories=categories,
        stats=stats,
        pricing=prices[product.id],
        visibility_state=visibility.product_state(product),
        is_available_for_purchase=visibility.is_available_for_purchase(product),
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
    in_stock: bool | None = Query(default=None, description="Filter by positive unreserved local stock"),
    search: Annotated[SearchText | None, Query(max_length=120)] = None,
    q: Annotated[SearchText | None, Query(max_length=120)] = None,
    sort: str | None = Query(default=None),
    ordering: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ShopProductResponse]:
    visibility = await CatalogVisibility.load(session)
    stmt = _active_product_stmt(visibility)
    if category_slug:
        category = (
            await session.execute(
                select(Category).where(Category.slug == category_slug, visibility.visible_category_clause())
            )
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
    if in_stock is not None:
        available_stock = Product.stock_quantity - func.coalesce(Product.reserved_quantity, 0)
        stmt = stmt.where(available_stock > 0 if in_stock else available_stock <= 0)
    term = (q or search or "").strip()
    if term:
        pattern = f"%{term}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(pattern),
                Product.name_en.ilike(pattern),
                Product.old_name.ilike(pattern),
                Product.model_name.ilike(pattern),
                Product.product_type.ilike(pattern),
                Product.product_type_en.ilike(pattern),
                Product.description.ilike(pattern),
                Product.description_en.ilike(pattern),
                Product.short_description.ilike(pattern),
                Product.short_description_en.ilike(pattern),
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
        all_prices = await shop_promotion_service.price_products(
            session,
            all_items,
            category_parents=visibility.category_parents(),
        )
        all_items.sort(
            key=lambda item: (
                product_stock_sort_key(item),
                -all_prices[item.id].price if sort_value in price_sort_descending else all_prices[item.id].price,
                item.id,
            )
        )
        total = len(all_items)
        items = all_items[page_offset : page_offset + page_size]
        prices = {item.id: all_prices[item.id] for item in items}
    else:
        items, total = await _paginate(session, stmt, limit=page_size, offset=page_offset)
        prices = await shop_promotion_service.price_products(
            session,
            items,
            category_parents=visibility.category_parents(),
        )
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [item.id for item in items])
    return PaginatedResponse[ShopProductResponse](
        total=total,
        page=page,
        page_size=response_page_size,
        items=[
            build_shop_product_response(
                item,
                categories=categories,
                image_limit=CATALOG_IMAGE_LIMIT,
                stats=stats,
                pricing=prices[item.id],
                visibility_state=visibility.product_state(item),
                is_available_for_purchase=visibility.is_available_for_purchase(item),
            )
            for item in items
        ],
    )


@public_router.get("/{product_id}/reviews", response_model=ProductReviewListResponse)
async def list_product_reviews(
    product_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> ProductReviewListResponse:
    visibility = await CatalogVisibility.load(session)
    product = (
        await session.execute(
            select(Product).where(Product.id == product_id, visibility.visible_product_clause())
        )
    ).scalar_one_or_none()
    if not product:
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
    await admit_shop_subject('review', f'customer:{current_customer.id}')
    visibility = await CatalogVisibility.load(session)
    product = (
        await session.execute(
            select(Product).where(Product.id == product_id, visibility.visible_product_clause())
        )
    ).scalar_one_or_none()
    if not product:
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
    visibility = await CatalogVisibility.load(session)
    review = (
        await session.execute(
            select(ProductReview)
            .join(Product, Product.id == ProductReview.product_id)
            .where(
                ProductReview.id == review_id,
                ProductReview.product_id == product_id,
                visibility.visible_product_clause(),
            )
        )
    ).scalar_one_or_none()
    if not review:
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
    visibility = await CatalogVisibility.load(session)
    comments = list(
        (
            await session.execute(
                select(ProductReviewComment)
                .options(selectinload(ProductReviewComment.customer))
                .join(ProductReview, ProductReview.id == ProductReviewComment.review_id)
                .join(Product, Product.id == ProductReview.product_id)
                .where(ProductReviewComment.review_id == review_id, visibility.visible_product_clause())
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
    await admit_shop_subject('review', f'customer:{current_customer.id}')
    visibility = await CatalogVisibility.load(session)
    review = (
        await session.execute(
            select(ProductReview)
            .join(Product, Product.id == ProductReview.product_id)
            .where(ProductReview.id == review_id, visibility.visible_product_clause())
        )
    ).scalar_one_or_none()
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
    visibility = await CatalogVisibility.load(session)
    comment = (
        await session.execute(
            select(ProductReviewComment)
            .join(ProductReview, ProductReview.id == ProductReviewComment.review_id)
            .join(Product, Product.id == ProductReview.product_id)
            .where(
                ProductReviewComment.id == comment_id,
                ProductReviewComment.review_id == review_id,
                visibility.visible_product_clause(),
            )
        )
    ).scalar_one_or_none()
    if not comment:
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
    visibility = await CatalogVisibility.load(session)
    product = (
        await session.execute(
            select(Product).where(Product.id == product_id, visibility.visible_product_clause())
        )
    ).scalar_one_or_none()
    if not product:
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
    visibility = await CatalogVisibility.load(session)
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images), selectinload(Product.image_variants))
        .where(Product.id == product_id, visibility.visible_product_clause())
    )
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [product.id])
    variant_products = await _volume_variant_products(session, product, visibility)
    price_products = variant_products or [product]
    prices = await shop_promotion_service.price_products(
        session,
        price_products,
        category_parents=visibility.category_parents(),
    )
    volume_variants = _volume_variant_responses(variant_products, prices, visibility)
    return build_shop_product_response(
        product,
        categories=categories,
        stats=stats,
        pricing=prices[product.id],
        visibility_state=visibility.product_state(product),
        is_available_for_purchase=visibility.is_available_for_purchase(product),
        volume_variants=volume_variants,
    )


def _backoffice_product_response(product: Product, visibility: CatalogVisibility) -> BackofficeProductResponse:
    state = visibility.product_state(product)
    images = [
        ProductImageResponse.model_validate(image)
        for image in sorted(product.images, key=lambda item: (item.sort_order, item.id))
    ]
    display_urls = product_image_urls(product)
    return BackofficeProductResponse(
        **ProductResponse.model_validate(product).model_dump(),
        display_image_url=display_urls[0] if display_urls else None,
        display_image_urls=display_urls,
        is_effectively_visible=state.is_effectively_visible,
        hidden_reason=state.hidden_reason,
        images=images,
    )


@backoffice_router.get("/{product_id}/image-variants", response_model=list[ProductImageVariantResponse])
async def list_product_image_variants(
    product_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProductImageVariantResponse]:
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    result = await session.execute(
        select(ProductImageVariant)
        .where(ProductImageVariant.product_id == product_id)
        .order_by(ProductImageVariant.source_key, ProductImageVariant.created_at.desc())
    )
    return [ProductImageVariantResponse.model_validate(item) for item in result.scalars().all()]


@backoffice_router.get("/{product_id}/images", response_model=list[ProductImageResponse])
async def list_product_images(
    product_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProductImageResponse]:
    images = await product_image_service.list_images(session, product_id)
    return [ProductImageResponse.model_validate(image) for image in images]


@backoffice_router.post(
    "/{product_id}/images",
    response_model=ProductImageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_product_image(
    product_id: int,
    file: UploadFile = File(...),
    alt: str | None = Form(default=None, max_length=255),
    _: object = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> ProductImageResponse:
    image = await product_image_service.create_image(session, product_id, file, alt)
    return ProductImageResponse.model_validate(image)


@backoffice_router.patch("/{product_id}/images/reorder", response_model=list[ProductImageResponse])
async def reorder_product_images(
    product_id: int,
    payload: ProductImageReorderRequest,
    _: object = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProductImageResponse]:
    images = await product_image_service.reorder_images(session, product_id, payload.image_ids)
    return [ProductImageResponse.model_validate(image) for image in images]


@backoffice_router.put(
    "/{product_id}/images/{image_id}/file",
    response_model=ProductImageResponse,
)
async def replace_product_image_file(
    product_id: int,
    image_id: int,
    file: UploadFile = File(...),
    _: object = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> ProductImageResponse:
    image = await product_image_service.replace_file(session, product_id, image_id, file)
    return ProductImageResponse.model_validate(image)


@backoffice_router.patch("/{product_id}/images/{image_id}", response_model=ProductImageResponse)
async def update_product_image(
    product_id: int,
    image_id: int,
    payload: ProductImageUpdate,
    _: object = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> ProductImageResponse:
    image = await product_image_service.update_image(
        session,
        product_id,
        image_id,
        payload.model_dump(exclude_unset=True),
    )
    return ProductImageResponse.model_validate(image)


@backoffice_router.delete("/{product_id}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_image(
    product_id: int,
    image_id: int,
    _: object = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    await product_image_service.delete_image(session, product_id, image_id)


@backoffice_router.get("", response_model=PaginatedResponse[BackofficeProductResponse])
async def backoffice_list_products(
    pagination: PaginationDep,
    is_active: str | None = Query(default=None),
    availability_status: str | None = Query(default=None),
    category_id: str | None = Query(default=None),
    brand_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BackofficeProductResponse]:
    visibility = await CatalogVisibility.load(session)
    parsed_is_active = parse_optional_bool_query(is_active, "is_active")
    parsed_category_id = parse_optional_int_query(category_id, "category_id")
    parsed_brand_id = parse_optional_int_query(brand_id, "brand_id")
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images), selectinload(Product.image_variants))
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
        stmt = stmt.where(or_(Product.name.ilike(f"%{search}%"), Product.name_en.ilike(f"%{search}%"),
                              Product.product_type_en.ilike(f"%{search}%"), Product.old_name.ilike(f"%{search}%")))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[BackofficeProductResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[_backoffice_product_response(item, visibility) for item in items],
    )


@backoffice_router.get("/{product_id}", response_model=BackofficeProductResponse)
async def backoffice_get_product(
    product_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeProductResponse:
    visibility = await CatalogVisibility.load(session)
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images), selectinload(Product.image_variants))
        .where(Product.id == product_id)
    )
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return _backoffice_product_response(product, visibility)


@backoffice_router.post("", response_model=BackofficeProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    current_user: AdminUser = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeProductResponse:
    product = await service.create_product(session, payload.model_dump(), admin_user_id=current_user.id)
    visibility = await CatalogVisibility.load(session)
    product = (
        await session.execute(
            select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images), selectinload(Product.image_variants))
            .where(Product.id == product.id)
        )
    ).scalar_one()
    return _backoffice_product_response(product, visibility)


@backoffice_router.put("/{product_id}", response_model=BackofficeProductResponse)
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    _: object = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeProductResponse:
    product = await repo.get(session, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    updated = await service.update_product(session, product, payload.model_dump(exclude_unset=True))
    visibility = await CatalogVisibility.load(session)
    updated = (
        await session.execute(
            select(Product)
            .options(selectinload(Product.brand), selectinload(Product.category), selectinload(Product.images), selectinload(Product.image_variants))
            .where(Product.id == updated.id)
        )
    ).scalar_one()
    return _backoffice_product_response(updated, visibility)


@backoffice_router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    _: object = Depends(get_catalog_manager),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    product = await repo.get(session, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    await service.delete_product(session, product)
