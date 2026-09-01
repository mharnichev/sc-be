from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.routes.products import _review_stats, build_shop_product_response
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_customer
from app.dependencies.common import PaginationDep, parse_optional_bool_query
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference
from app.models.booking import BarberService, Booking, BookingServiceItem, Master
from app.models.order import Order
from app.models.product import Product
from app.models.shop import CustomerCartItem, CustomerWishlistItem
from app.repositories.base import BaseRepository
from app.schemas.auth import (
    CustomerCreate,
    CustomerAuthResponse,
    CustomerOtpRequest,
    CustomerOtpRequestResponse,
    CustomerOtpVerifyRequest,
    CustomerResponse,
    CustomerSummaryResponse,
    CustomerUpdate,
)
from app.schemas.common import PaginatedResponse
from app.schemas.booking import BookingBackofficeResponse, CustomerBookingStatsItem, CustomerBookingStatsResponse
from app.schemas.order import OrderSummaryResponse
from app.schemas.shop import CartItemCreate, CartItemResponse, WishlistItemCreate, WishlistItemResponse
from app.services.customer_auth import CustomerAuthService
from app.services.catalog_visibility import CatalogVisibility
from app.services.shop_promotion import ShopPriceResult, shop_promotion_service

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Customer)
orders_repo = BaseRepository(Order)
service = CustomerAuthService()


@public_router.post("/auth/request-otp", response_model=CustomerOtpRequestResponse, status_code=status.HTTP_202_ACCEPTED)
async def request_customer_otp(
    payload: CustomerOtpRequest,
    session: AsyncSession = Depends(get_db_session),
) -> CustomerOtpRequestResponse:
    result = await service.request_otp(session, payload.phone)
    return CustomerOtpRequestResponse(
        message="OTP code sent",
        expires_in_seconds=result.expires_in_seconds,
        retry_after_seconds=result.retry_after_seconds,
        sends_left_today=result.sends_left_today,
        debug_otp_code=result.debug_otp_code,
    )


@public_router.post("/auth/verify-otp", response_model=CustomerAuthResponse)
async def verify_customer_otp(
    payload: CustomerOtpVerifyRequest,
    session: AsyncSession = Depends(get_db_session),
) -> CustomerAuthResponse:
    result = await service.verify_otp(session, payload.phone, payload.otp_code)
    return CustomerAuthResponse(
        access_token=result.access_token,
        customer=CustomerResponse.model_validate(result.customer),
        is_new_customer=result.is_new_customer,
        attempts_left_today=result.attempts_left_today,
    )


@public_router.get("/me", response_model=CustomerResponse)
async def customer_me(current_customer: Customer = Depends(get_current_customer)) -> CustomerResponse:
    return CustomerResponse.model_validate(current_customer)


@public_router.patch("/me", response_model=CustomerResponse)
async def update_customer_me(
    payload: CustomerUpdate,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await service.update_customer(session, current_customer, payload.model_dump(exclude_unset=True))
    return CustomerResponse.model_validate(customer)


def _cart_item_response(
    item: CustomerCartItem,
    *,
    categories: dict[int, object],
    stats: dict[int, tuple[Decimal | None, int]],
    visibility: CatalogVisibility,
    pricing: ShopPriceResult | None = None,
) -> CartItemResponse:
    visibility_state = visibility.product_state(item.product)
    is_available_for_purchase = visibility.is_available_for_purchase(item.product)
    return CartItemResponse(
        id=item.id,
        product_id=item.product_id,
        quantity=item.quantity,
        is_effectively_visible=visibility_state.is_effectively_visible,
        hidden_reason=visibility_state.hidden_reason,
        is_available_for_purchase=is_available_for_purchase,
        product=build_shop_product_response(
            item.product,
            categories=categories,
            stats=stats,
            pricing=pricing,
            visibility_state=visibility_state,
            is_available_for_purchase=is_available_for_purchase,
        ),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _wishlist_item_response(
    item: CustomerWishlistItem,
    *,
    categories: dict[int, object],
    stats: dict[int, tuple[Decimal | None, int]],
    visibility: CatalogVisibility,
    pricing: ShopPriceResult | None = None,
) -> WishlistItemResponse:
    visibility_state = visibility.product_state(item.product)
    is_available_for_purchase = visibility.is_available_for_purchase(item.product)
    return WishlistItemResponse(
        id=item.id,
        product_id=item.product_id,
        is_effectively_visible=visibility_state.is_effectively_visible,
        hidden_reason=visibility_state.hidden_reason,
        is_available_for_purchase=is_available_for_purchase,
        product=build_shop_product_response(
            item.product,
            categories=categories,
            stats=stats,
            pricing=pricing,
            visibility_state=visibility_state,
            is_available_for_purchase=is_available_for_purchase,
        ),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _base_price_result(product: Product) -> ShopPriceResult:
    base_price = Decimal(product.price)
    return ShopPriceResult(
        base_price=base_price,
        price=base_price,
        discount_amount=Decimal("0.00"),
        discount_percent=None,
    )


async def _customer_item_prices(
    session: AsyncSession,
    products: list[Product],
    visibility: CatalogVisibility,
) -> dict[int, ShopPriceResult]:
    visible_products = [
        product
        for product in products
        if visibility.product_state(product).is_effectively_visible
    ]
    prices = (
        await shop_promotion_service.price_products(
            session,
            visible_products,
            category_parents=visibility.category_parents(),
        )
        if visible_products
        else {}
    )
    return {
        product.id: prices.get(product.id, _base_price_result(product))
        for product in products
    }


@public_router.get("/cart", response_model=PaginatedResponse[CartItemResponse])
async def list_cart_items(
    pagination: PaginationDep,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CartItemResponse]:
    stmt = (
        select(CustomerCartItem)
        .options(
            selectinload(CustomerCartItem.product).selectinload(Product.brand),
            selectinload(CustomerCartItem.product).selectinload(Product.category),
            selectinload(CustomerCartItem.product).selectinload(Product.images),
        )
        .where(CustomerCartItem.customer_id == current_customer.id)
        .order_by(CustomerCartItem.created_at.desc(), CustomerCartItem.id.desc())
    )
    total = (await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    result = await session.execute(stmt.offset((pagination.page - 1) * pagination.page_size).limit(pagination.page_size))
    items = list(result.scalars().all())
    visibility = await CatalogVisibility.load(session)
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [item.product_id for item in items])
    prices = await _customer_item_prices(session, [item.product for item in items], visibility)
    return PaginatedResponse[CartItemResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[
            _cart_item_response(
                item,
                categories=categories,
                stats=stats,
                pricing=prices[item.product_id],
                visibility=visibility,
            )
            for item in items
        ],
    )


@public_router.post("/cart", response_model=CartItemResponse, status_code=status.HTTP_201_CREATED)
async def add_cart_item(
    payload: CartItemCreate,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CartItemResponse:
    product = await session.get(Product, payload.product_id)
    visibility = await CatalogVisibility.load(session)
    if not product or not visibility.product_state(product).is_effectively_visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    item = (
        await session.execute(
            select(CustomerCartItem)
            .options(
                selectinload(CustomerCartItem.product).selectinload(Product.brand),
                selectinload(CustomerCartItem.product).selectinload(Product.category),
                selectinload(CustomerCartItem.product).selectinload(Product.images),
            )
            .where(
                CustomerCartItem.customer_id == current_customer.id,
                CustomerCartItem.product_id == payload.product_id,
            )
        )
    ).scalar_one_or_none()
    if item:
        item.quantity += payload.quantity
    else:
        item = CustomerCartItem(
            customer_id=current_customer.id,
            product_id=payload.product_id,
            quantity=payload.quantity,
        )
        session.add(item)
    await session.commit()
    item = (
        await session.execute(
            select(CustomerCartItem)
            .options(
                selectinload(CustomerCartItem.product).selectinload(Product.brand),
                selectinload(CustomerCartItem.product).selectinload(Product.category),
                selectinload(CustomerCartItem.product).selectinload(Product.images),
            )
            .where(CustomerCartItem.customer_id == current_customer.id, CustomerCartItem.product_id == payload.product_id)
        )
    ).scalar_one()
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [item.product_id])
    prices = await _customer_item_prices(session, [item.product], visibility)
    return _cart_item_response(
        item,
        categories=categories,
        stats=stats,
        pricing=prices[item.product_id],
        visibility=visibility,
    )


@public_router.delete("/cart/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cart_item(
    product_id: int,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    item = (
        await session.execute(
            select(CustomerCartItem).where(
                CustomerCartItem.customer_id == current_customer.id,
                CustomerCartItem.product_id == product_id,
            )
        )
    ).scalar_one_or_none()
    if item:
        await session.delete(item)
        await session.commit()


@public_router.get("/wishlist", response_model=PaginatedResponse[WishlistItemResponse])
async def list_wishlist_items(
    pagination: PaginationDep,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[WishlistItemResponse]:
    stmt = (
        select(CustomerWishlistItem)
        .options(
            selectinload(CustomerWishlistItem.product).selectinload(Product.brand),
            selectinload(CustomerWishlistItem.product).selectinload(Product.category),
            selectinload(CustomerWishlistItem.product).selectinload(Product.images),
        )
        .where(CustomerWishlistItem.customer_id == current_customer.id)
        .order_by(CustomerWishlistItem.created_at.desc(), CustomerWishlistItem.id.desc())
    )
    total = (await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    result = await session.execute(stmt.offset((pagination.page - 1) * pagination.page_size).limit(pagination.page_size))
    items = list(result.scalars().all())
    visibility = await CatalogVisibility.load(session)
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [item.product_id for item in items])
    prices = await _customer_item_prices(session, [item.product for item in items], visibility)
    return PaginatedResponse[WishlistItemResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[
            _wishlist_item_response(
                item,
                categories=categories,
                stats=stats,
                pricing=prices[item.product_id],
                visibility=visibility,
            )
            for item in items
        ],
    )


@public_router.post("/wishlist", response_model=WishlistItemResponse, status_code=status.HTTP_201_CREATED)
async def add_wishlist_item(
    payload: WishlistItemCreate,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> WishlistItemResponse:
    product = await session.get(Product, payload.product_id)
    visibility = await CatalogVisibility.load(session)
    if not product or not visibility.product_state(product).is_effectively_visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    existing = (
        await session.execute(
            select(CustomerWishlistItem).where(
                CustomerWishlistItem.customer_id == current_customer.id,
                CustomerWishlistItem.product_id == payload.product_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Product already exists in wishlist")
    item = CustomerWishlistItem(customer_id=current_customer.id, product_id=payload.product_id)
    session.add(item)
    await session.commit()
    item = (
        await session.execute(
            select(CustomerWishlistItem)
            .options(
                selectinload(CustomerWishlistItem.product).selectinload(Product.brand),
                selectinload(CustomerWishlistItem.product).selectinload(Product.category),
                selectinload(CustomerWishlistItem.product).selectinload(Product.images),
            )
            .where(CustomerWishlistItem.id == item.id)
        )
    ).scalar_one()
    categories = visibility.categories_by_id
    stats = await _review_stats(session, [item.product_id])
    prices = await _customer_item_prices(session, [item.product], visibility)
    return _wishlist_item_response(
        item,
        categories=categories,
        stats=stats,
        pricing=prices[item.product_id],
        visibility=visibility,
    )


@public_router.delete("/wishlist/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wishlist_item(
    product_id: int,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    item = (
        await session.execute(
            select(CustomerWishlistItem).where(
                CustomerWishlistItem.customer_id == current_customer.id,
                CustomerWishlistItem.product_id == product_id,
            )
        )
    ).scalar_one_or_none()
    if item:
        await session.delete(item)
        await session.commit()


@backoffice_router.get("", response_model=PaginatedResponse[CustomerSummaryResponse])
async def backoffice_list_customers(
    pagination: PaginationDep,
    is_active: str | None = Query(default=None),
    is_verified: str | None = Query(default=None),
    telegram_connected: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CustomerSummaryResponse]:
    parsed_is_active = parse_optional_bool_query(is_active, "is_active")
    parsed_is_verified = parse_optional_bool_query(is_verified, "is_verified")
    parsed_telegram_connected = parse_optional_bool_query(telegram_connected, "telegram_connected")

    sortable_fields = {
        "id": Customer.id,
        "phone": Customer.phone,
        "name": Customer.name,
        "surname": Customer.surname,
        "created_at": Customer.created_at,
        "last_login_at": Customer.last_login_at,
    }
    if sort_by not in sortable_fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid sort_by value")
    if sort_order not in {"asc", "desc"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid sort_order value")

    order_clause = asc(sortable_fields[sort_by]) if sort_order == "asc" else desc(sortable_fields[sort_by])
    stmt = select(Customer).order_by(order_clause, Customer.id.desc())
    telegram_connected_exists = (
        select(ClientCommunicationPreference.id)
        .where(
            ClientCommunicationPreference.customer_id == Customer.id,
            ClientCommunicationPreference.telegram_chat_id.is_not(None),
            ClientCommunicationPreference.telegram_chat_id != "",
        )
        .exists()
    )
    if parsed_is_active is not None:
        stmt = stmt.where(Customer.is_active.is_(parsed_is_active))
    if parsed_is_verified is not None:
        if parsed_is_verified:
            stmt = stmt.where(Customer.phone_verified_at.is_not(None))
        else:
            stmt = stmt.where(Customer.phone_verified_at.is_(None))
    if parsed_telegram_connected is not None:
        stmt = stmt.where(telegram_connected_exists if parsed_telegram_connected else ~telegram_connected_exists)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Customer.phone.ilike(pattern),
                Customer.email.ilike(pattern),
                Customer.name.ilike(pattern),
                Customer.surname.ilike(pattern),
            )
        )

    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    item_ids = [item.id for item in items]
    connected_customer_ids: set[int] = set()
    if item_ids:
        connected_customer_ids = set(
            (
                await session.execute(
                    select(ClientCommunicationPreference.customer_id).where(
                        ClientCommunicationPreference.customer_id.in_(item_ids),
                        ClientCommunicationPreference.telegram_chat_id.is_not(None),
                        ClientCommunicationPreference.telegram_chat_id != "",
                    )
                )
            ).scalars().all()
        )
    return PaginatedResponse[CustomerSummaryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[
            CustomerSummaryResponse.model_validate(item).model_copy(
                update={"telegram_connected": item.id in connected_customer_ids}
            )
            for item in items
        ],
    )


@backoffice_router.get("/{customer_id}", response_model=CustomerResponse)
async def backoffice_get_customer(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return CustomerResponse.model_validate(customer)


@backoffice_router.get("/{customer_id}/orders", response_model=PaginatedResponse[OrderSummaryResponse])
async def backoffice_customer_orders(
    customer_id: int,
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[OrderSummaryResponse]:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
    )
    items, total = await orders_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[OrderSummaryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[OrderSummaryResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/{customer_id}/bookings", response_model=PaginatedResponse[BookingBackofficeResponse])
async def backoffice_customer_bookings(
    customer_id: int,
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BookingBackofficeResponse]:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    stmt = (
        select(Booking)
        .options(
            selectinload(Booking.customer),
            selectinload(Booking.redirected_from_master),
            selectinload(Booking.service),
            selectinload(Booking.service_items).selectinload(BookingServiceItem.service),
        )
        .where(Booking.customer_id == customer_id)
        .order_by(Booking.start_at.desc())
    )
    items, total = await BaseRepository(Booking).list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[BookingBackofficeResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[BookingBackofficeResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/{customer_id}/stats", response_model=CustomerBookingStatsResponse)
async def backoffice_customer_stats(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerBookingStatsResponse:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    total_bookings = (
        await session.execute(select(func.count()).select_from(Booking).where(Booking.customer_id == customer_id))
    ).scalar_one()
    last_visit_date = (
        await session.execute(select(func.max(Booking.start_at)).where(Booking.customer_id == customer_id))
    ).scalar_one()
    barber_row = (
        await session.execute(
            select(Master.id, Master.full_name, func.count(Booking.id).label("booking_count"))
            .join(Booking, Booking.master_id == Master.id)
            .where(Booking.customer_id == customer_id)
            .group_by(Master.id, Master.full_name)
            .order_by(desc("booking_count"), Master.full_name.asc())
            .limit(1)
        )
    ).first()
    service_rows = (
        await session.execute(
            select(BarberService.id, BarberService.name, func.count(Booking.id).label("booking_count"))
            .join(BookingServiceItem, BookingServiceItem.service_id == BarberService.id)
            .join(Booking, Booking.id == BookingServiceItem.booking_id)
            .where(Booking.customer_id == customer_id)
            .group_by(BarberService.id, BarberService.name)
            .order_by(desc("booking_count"), BarberService.name.asc())
        )
    ).all()

    return CustomerBookingStatsResponse(
        total_bookings=total_bookings,
        most_visited_barber=(
            CustomerBookingStatsItem(id=barber_row.id, name=barber_row.full_name, count=barber_row.booking_count)
            if barber_row
            else None
        ),
        most_used_services=[
            CustomerBookingStatsItem(id=row.id, name=row.name, count=row.booking_count)
            for row in service_rows
        ],
        last_visit_date=last_visit_date,
    )


@backoffice_router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
async def backoffice_create_customer(
    payload: CustomerCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await service.create_customer(session, payload.model_dump())
    return CustomerResponse.model_validate(customer)


@backoffice_router.put("/{customer_id}", response_model=CustomerResponse)
async def backoffice_update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerResponse:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    updated = await service.update_customer(session, customer, payload.model_dump(exclude_unset=True))
    return CustomerResponse.model_validate(updated)


@backoffice_router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def backoffice_delete_customer(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    customer = await repo.get(session, customer_id)
    if not customer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    await service.delete_customer(session, customer)
