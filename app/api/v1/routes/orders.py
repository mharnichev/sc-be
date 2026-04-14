from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep
from app.models.order import Order
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.order import OrderCreate, OrderResponse, OrderStatusUpdate, OrderSummaryResponse
from app.services.order import OrderService

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Order)
service = OrderService()


@public_router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    session: AsyncSession = Depends(get_db_session),
) -> OrderResponse:
    order = await service.create_order(session, payload)
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


@backoffice_router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrderResponse:
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderResponse.model_validate(order)


@backoffice_router.patch("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: int,
    payload: OrderStatusUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrderResponse:
    order = await repo.get(session, order_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    await repo.update(session, order, {"status": payload.status})
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    result = await session.execute(stmt)
    refreshed = result.scalar_one()
    return OrderResponse.model_validate(refreshed)
