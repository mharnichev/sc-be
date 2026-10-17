from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.schemas.product import DeliveryListResponse
from app.services.nova_poshta import NovaPoshtaService
from app.schemas.delivery import BranchOption, DirectoryResponse, SettlementOption, StreetOption
from app.services.nova_poshta_directory import NovaPoshtaDirectory

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


# Stable storefront contract; legacy raw-provider endpoints above stay compatible.
directory = NovaPoshtaDirectory()


@public_router.get("/np/settlements", response_model=DirectoryResponse[SettlementOption])
async def nova_poshta_settlements(
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
):
    return await directory.settlements(session, q, limit)


@public_router.get("/np/branches", response_model=DirectoryResponse[BranchOption])
async def nova_poshta_branches(
    settlement_ref: str = Query(min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$"),
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    return await directory.branches(session, settlement_ref, q, limit)


@public_router.get("/np/streets", response_model=DirectoryResponse[StreetOption])
async def nova_poshta_streets(
    settlement_ref: str = Query(min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$"),
    q: str = Query(min_length=2, max_length=120),
    limit: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
):
    if len(q.strip()) < 2:
        raise HTTPException(status_code=422, detail="Enter at least two street characters")
    return await directory.streets(session, settlement_ref, q, limit)
