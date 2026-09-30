from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_customer, get_optional_current_customer
from app.dependencies.common import PaginationDep
from app.models.admin_user import AdminUser
from app.models.order import Order
from app.models.customer import Customer
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.order import (
    BackofficeOrderResponse,
    CustomerOrderResponse,
    OrderCreate,
    OrderFulfillmentResponse,
    OrderResponse,
    OrderStatusUpdate,
    OrderSummaryResponse,
)
from app.services.order import OrderService

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Order)
service = OrderService()


@public_router.get("/me", response_model=PaginatedResponse[CustomerOrderResponse])
async def list_my_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CustomerOrderResponse]:
    base = select(Order).where(Order.customer_id == current_customer.id)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.options(selectinload(Order.items)).order_by(Order.created_at.desc(), Order.id.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    orders = (await session.execute(stmt)).scalars().all()
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[CustomerOrderResponse.from_order(order) for order in orders],
    )


@public_router.get("/me/{order_id}", response_model=CustomerOrderResponse)
async def get_my_order(
    order_id: int,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerOrderResponse:
    stmt = select(Order).options(selectinload(Order.items)).where(
        Order.id == order_id, Order.customer_id == current_customer.id,
    )
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return CustomerOrderResponse.from_order(order)


@public_router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> OrderResponse:
    order = await service.create_order(session, payload, current_customer=current_customer)
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    result = await session.execute(stmt)
    refreshed = result.scalar_one()
    return OrderResponse.model_validate(refreshed)


@backoffice_router.get("", response_model=PaginatedResponse[OrderSummaryResponse])
async def list_orders(
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[OrderSummaryResponse]:
    stmt = select(Order).order_by(Order.created_at.desc())
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[OrderSummaryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[OrderSummaryResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/{order_id}/fulfillment", response_model=OrderFulfillmentResponse)
async def get_order_fulfillment(
    order_id: int,
    _: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrderFulfillmentResponse:
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderFulfillmentResponse.model_validate(order)


@backoffice_router.get("/{order_id}", response_model=BackofficeOrderResponse)
async def get_order(
    order_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeOrderResponse:
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return BackofficeOrderResponse.model_validate(order)


@backoffice_router.patch("/{order_id}/status", response_model=BackofficeOrderResponse)
async def update_order_status(
    order_id: int,
    payload: OrderStatusUpdate,
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeOrderResponse:
    await service.update_status(
        session,
        order_id,
        payload.status,
        admin_user_id=current_admin.id,
    )
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    result = await session.execute(stmt)
    refreshed = result.scalar_one()
    return BackofficeOrderResponse.model_validate(refreshed)
