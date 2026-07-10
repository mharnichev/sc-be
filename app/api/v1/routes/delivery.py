from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.schemas.product import DeliveryListResponse
from app.services.nova_poshta import NovaPoshtaService

public_router = APIRouter()
service = NovaPoshtaService()


@public_router.get("/np/cities", response_model=DeliveryListResponse)
async def nova_poshta_cities(
    q: str = Query(min_length=2, max_length=120),
    session: AsyncSession = Depends(get_db_session),
) -> DeliveryListResponse:
    items, cached, updated_at = await service.cities(session, q)
    return DeliveryListResponse(items=items, cached=cached, updated_at=updated_at)


@public_router.get("/np/warehouses", response_model=DeliveryListResponse)
async def nova_poshta_warehouses(
    city_ref: str = Query(min_length=1, max_length=120),
    session: AsyncSession = Depends(get_db_session),
) -> DeliveryListResponse:
    items, cached, updated_at = await service.warehouses(session, city_ref)
    return DeliveryListResponse(items=items, cached=cached, updated_at=updated_at)


@public_router.get("/np/warehouse-types", response_model=DeliveryListResponse)
async def nova_poshta_warehouse_types(
    session: AsyncSession = Depends(get_db_session),
) -> DeliveryListResponse:
    items, cached, updated_at = await service.warehouse_types(session)
    return DeliveryListResponse(items=items, cached=cached, updated_at=updated_at)
