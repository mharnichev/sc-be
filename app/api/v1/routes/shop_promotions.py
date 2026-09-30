from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_optional_current_customer
from app.dependencies.common import PaginationDep, parse_optional_bool_query
from app.models.admin_user import AdminUser
from app.models.brand import Brand
from app.models.category import Category
from app.models.customer import Customer
from app.models.product import Product
from app.models.shop_promotion import (
    SHOP_PROMOTION_TIMEZONE,
    ShopPromotion,
    ShopPromotionStatus,
    ShopPromotionTrigger,
)
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.shop_promotion import (
    ShopPromotionCreate,
    ShopPromotionQuoteItem,
    ShopPromotionQuoteRequest,
    ShopPromotionQuoteResponse,
    ShopPromotionResponse,
    ShopPromotionUpdate,
    ShopPromotionPreviewConflict,
    ShopPromotionPreviewItem,
    ShopPromotionPreviewPromotion,
    ShopPromotionPreviewResponse,
    ShopPromotionProductResponse,
    ShopPromotionProductsResponse,
    normalize_shop_promotion_code,
)
from app.services.customer_auth import CustomerAuthService
from app.services.catalog_visibility import CatalogVisibility
from app.services.shop_promotion import ShopPriceResult, ShopPromotionService, shop_promotion_service

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(ShopPromotion)
customer_auth_service = CustomerAuthService()
response_options = (
    selectinload(ShopPromotion.products),
    selectinload(ShopPromotion.categories),
    selectinload(ShopPromotion.brands),
)


def ensure_superuser(current_user: AdminUser) -> None:
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can manage shop promotions")


async def get_for_response(session: AsyncSession, promotion_id: int) -> ShopPromotion:
    promotion = (
        await session.execute(
            select(ShopPromotion).options(*response_options).where(ShopPromotion.id == promotion_id)
        )
    ).scalar_one_or_none()
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shop promotion not found")
    return promotion


async def load_entities(session: AsyncSession, model, ids: list[int], label: str):
    if not ids:
        return []
    items = list((await session.execute(select(model).where(model.id.in_(ids)))).scalars().all())
    missing = sorted(set(ids) - {item.id for item in items})
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} not found: {', '.join(str(item) for item in missing)}",
        )
    return items


async def ensure_unique_code(
    session: AsyncSession,
    code: str | None,
    *,
    exclude_promotion_id: int | None = None,
) -> None:
    if not code:
        return
    stmt = select(ShopPromotion.id).where(ShopPromotion.code == code)
    if exclude_promotion_id is not None:
        stmt = stmt.where(ShopPromotion.id != exclude_promotion_id)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Shop promotion code already exists")


async def apply_payload(session: AsyncSession, promotion: ShopPromotion, payload: ShopPromotionCreate) -> None:
    data = payload.model_dump(exclude={"product_ids", "category_ids", "brand_ids"})
    for key, value in data.items():
        setattr(promotion, key, value)
    promotion.products = await load_entities(session, Product, payload.product_ids, "Products")
    promotion.categories = await load_entities(session, Category, payload.category_ids, "Categories")
    promotion.brands = await load_entities(session, Brand, payload.brand_ids, "Brands")


@backoffice_router.get("", response_model=PaginatedResponse[ShopPromotionResponse])
async def list_shop_promotions(
    pagination: PaginationDep,
    is_active: str | None = Query(default=None),
    trigger: ShopPromotionTrigger | None = Query(default=None),
    search: str | None = Query(default=None),
    product_id: int | None = Query(default=None, ge=1),
    category_id: int | None = Query(default=None, ge=1),
    brand_id: int | None = Query(default=None, ge=1),
    promotion_status: ShopPromotionStatus | None = Query(default=None, alias="status"),
    period: ShopPromotionStatus | None = Query(default=None),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ShopPromotionResponse]:
    ensure_superuser(current_user)
    if promotion_status is not None and period is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Use either status or period, not both")
    selected_status = promotion_status or period
    now = datetime.now(SHOP_PROMOTION_TIMEZONE)
    parsed_is_active = parse_optional_bool_query(is_active, "is_active")
    stmt = select(ShopPromotion).options(*response_options).order_by(
        ShopPromotion.priority.asc(), ShopPromotion.created_at.desc()
    )
    if parsed_is_active is not None:
        stmt = stmt.where(ShopPromotion.is_active.is_(parsed_is_active))
    if trigger is not None:
        stmt = stmt.where(ShopPromotion.trigger == trigger)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(ShopPromotion.name.ilike(pattern), ShopPromotion.code.ilike(pattern)))
    if product_id is not None:
        stmt = stmt.where(ShopPromotion.products.any(Product.id == product_id))
    if category_id is not None:
        stmt = stmt.where(ShopPromotion.categories.any(Category.id == category_id))
    if brand_id is not None:
        stmt = stmt.where(ShopPromotion.brands.any(Brand.id == brand_id))
    if selected_status is not None:
        if selected_status == ShopPromotionStatus.disabled:
            stmt = stmt.where(ShopPromotion.is_active.is_(False))
        elif selected_status == ShopPromotionStatus.scheduled:
            stmt = stmt.where(
                ShopPromotion.is_active.is_(True),
                ShopPromotion.starts_at.is_not(None),
                ShopPromotion.starts_at > now,
            )
        elif selected_status == ShopPromotionStatus.expired:
            stmt = stmt.where(
                ShopPromotion.is_active.is_(True),
                ShopPromotion.ends_at.is_not(None),
                ShopPromotion.ends_at <= now,
            )
        else:
            stmt = stmt.where(
                ShopPromotion.is_active.is_(True),
                or_(ShopPromotion.starts_at.is_(None), ShopPromotion.starts_at <= now),
                or_(ShopPromotion.ends_at.is_(None), ShopPromotion.ends_at > now),
            )
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[ShopPromotionResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[ShopPromotionResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("", response_model=ShopPromotionResponse, status_code=status.HTTP_201_CREATED)
async def create_shop_promotion(
    payload: ShopPromotionCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ShopPromotionResponse:
    ensure_superuser(current_user)
    await ensure_unique_code(session, payload.code)
    promotion = ShopPromotion()
    await apply_payload(session, promotion, payload)
    # Populate relationships while the instance is still transient. Adding it
    # to an AsyncSession first makes SQLAlchemy lazy-load the empty
    # many-to-many collections during assignment, which raises MissingGreenlet.
    session.add(promotion)
    await session.commit()
    return ShopPromotionResponse.model_validate(await get_for_response(session, promotion.id))


@backoffice_router.post("/preview", response_model=ShopPromotionPreviewResponse)
async def preview_shop_promotion(
    payload: ShopPromotionCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ShopPromotionPreviewResponse:
    """Calculate a proposed product discount at the current Europe/Kyiv time without saving it."""
    ensure_superuser(current_user)
    proposed = ShopPromotion(id=0)
    await apply_payload(session, proposed, payload)
    now = datetime.now(SHOP_PROMOTION_TIMEZONE)
    existing = list(
        (
            await session.execute(
                select(ShopPromotion)
                .options(*response_options)
                .where(ShopPromotion.trigger == ShopPromotionTrigger.automatic)
            )
        )
        .scalars()
        .all()
    )
    visibility = await CatalogVisibility.load(session)
    products = list((await session.execute(select(Product).order_by(Product.id.asc()))).scalars().all())
    products = [
        product
        for product in products
        if ShopPromotionService.matches_product(
            proposed,
            product,
            category_parents=visibility.category_parents(),
        )
    ]
    result_items: list[ShopPromotionPreviewItem] = []
    for product in products:
        current_price = ShopPromotionService.calculate_product_price(
            product,
            existing,
            category_parents=visibility.category_parents(),
            at=now,
        )
        final_price = ShopPromotionService.calculate_product_price(
            product,
            [*existing, proposed],
            category_parents=visibility.category_parents(),
            at=now,
        )
        matching_existing = [
            other
            for other in existing
            if ShopPromotionService.is_active_at(other, now)
            and ShopPromotionService.matches_product(other, product, category_parents=visibility.category_parents())
            and ShopPromotionService.apply_promotion(final_price.base_price, other) < final_price.base_price
        ]
        conflicts = [
            ShopPromotionPreviewConflict(
                promotion_id=other.id,
                name=other.name,
                trigger=other.trigger,
                priority=other.priority,
                price=ShopPromotionService.apply_promotion(final_price.base_price, other),
            )
            for other in matching_existing
        ]

        def promotion_summary(price_result: ShopPriceResult) -> ShopPromotionPreviewPromotion | None:
            if price_result.promotion_id is None:
                return None
            return ShopPromotionPreviewPromotion(
                id=None if price_result.promotion_id == 0 else price_result.promotion_id,
                name=price_result.promotion_name or "",
                code=price_result.promotion_code,
                trigger=price_result.promotion_trigger or ShopPromotionTrigger.automatic,
                price=price_result.price,
            )

        result_items.append(
            ShopPromotionPreviewItem(
                product_id=product.id,
                product_name=product.name,
                base_price=final_price.base_price,
                new_price=final_price.price,
                discount_amount=final_price.discount_amount,
                currently_applied_promotion=promotion_summary(current_price),
                applied_promotion=promotion_summary(final_price),
                conflicts=conflicts,
            )
        )
    return ShopPromotionPreviewResponse(
        evaluated_at=now,
        affected_products_count=len(result_items),
        products=result_items,
    )


@backoffice_router.get("/{promotion_id}/products", response_model=ShopPromotionProductsResponse)
async def list_shop_promotion_products(
    promotion_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ShopPromotionProductsResponse:
    """Return every catalog product matched by a saved promotion scope."""
    ensure_superuser(current_user)
    promotion = await get_for_response(session, promotion_id)
    visibility = await CatalogVisibility.load(session)
    products = list((await session.execute(select(Product).order_by(Product.id.asc()))).scalars().all())
    matched_products = [
        product
        for product in products
        if ShopPromotionService.matches_product(
            promotion,
            product,
            category_parents=visibility.category_parents(),
        )
    ]
    product_states = visibility.product_states(matched_products)
    return ShopPromotionProductsResponse(
        affected_products_count=len(matched_products),
        products=[
            ShopPromotionProductResponse(
                product_id=product.id,
                product_name=product.name,
                sku=product.sku,
                base_price=product.price,
                is_effectively_visible=product_states[product.id].is_effectively_visible,
                hidden_reason=product_states[product.id].hidden_reason,
            )
            for product in matched_products
        ],
    )


@backoffice_router.get("/{promotion_id}", response_model=ShopPromotionResponse)
async def get_shop_promotion(
    promotion_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ShopPromotionResponse:
    ensure_superuser(current_user)
    return ShopPromotionResponse.model_validate(await get_for_response(session, promotion_id))


@backoffice_router.patch("/{promotion_id}", response_model=ShopPromotionResponse)
async def update_shop_promotion(
    promotion_id: int,
    payload: ShopPromotionUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ShopPromotionResponse:
    ensure_superuser(current_user)
    promotion = await get_for_response(session, promotion_id)
    current = ShopPromotionResponse.model_validate(promotion).model_dump(
        exclude={"id", "created_at", "updated_at"}
    )
    current.update(payload.model_dump(exclude_unset=True))
    validated = ShopPromotionCreate.model_validate(current)
    await ensure_unique_code(session, validated.code, exclude_promotion_id=promotion_id)
    await apply_payload(session, promotion, validated)
    await session.commit()
    return ShopPromotionResponse.model_validate(await get_for_response(session, promotion_id))


@backoffice_router.delete("/{promotion_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shop_promotion(
    promotion_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    ensure_superuser(current_user)
    promotion = await get_for_response(session, promotion_id)
    promotion.is_active = False
    await session.commit()


@public_router.post("/quote", response_model=ShopPromotionQuoteResponse)
async def quote_shop_promotion(
    payload: ShopPromotionQuoteRequest,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> ShopPromotionQuoteResponse:
    product_ids = [item.product_id for item in payload.items]
    if len(product_ids) != len(set(product_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duplicate products are not allowed")
    products = list(
        (
            await session.execute(
                select(Product).where(Product.id.in_(product_ids))
            )
        ).scalars().all()
    )
    if len(products) != len(product_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more products are invalid")
    visibility = await CatalogVisibility.load(session)
    if any(not state.is_effectively_visible for state in visibility.product_states(products).values()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more products are hidden from the shop",
        )
    products_by_id = {product.id: product for product in products}
    phone = payload.customer_phone or (current_customer.phone if current_customer else None)
    normalized_phone = customer_auth_service.normalize_phone(phone) if phone else None
    prices = await shop_promotion_service.price_products(
        session,
        products,
        category_parents=visibility.category_parents(),
        promo_code=payload.promo_code,
        customer_phone=normalized_phone,
        validate_code_usage=bool(payload.promo_code),
    )

    subtotal = Decimal("0.00")
    total = Decimal("0.00")
    items: list[ShopPromotionQuoteItem] = []
    for requested in payload.items:
        product = products_by_id[requested.product_id]
        price = prices[product.id]
        subtotal += price.base_price * requested.quantity
        total += price.price * requested.quantity
        items.append(
            ShopPromotionQuoteItem(
                product_id=product.id,
                quantity=requested.quantity,
                base_price=price.base_price,
                price=price.price,
                discount_amount=price.discount_amount * requested.quantity,
                promotion_id=price.promotion_id,
                promotion_name=price.promotion_name,
                promotion_code=price.promotion_code,
            )
        )
    applied_code = next((item.promotion_code for item in items if item.promotion_code), None)
    return ShopPromotionQuoteResponse(
        subtotal_amount=ShopPromotionService._money(subtotal),
        discount_amount=ShopPromotionService._money(subtotal - total),
        total_amount=ShopPromotionService._money(total),
        requested_code=normalize_shop_promotion_code(payload.promo_code) if payload.promo_code else None,
        applied_code=applied_code,
        items=items,
    )
