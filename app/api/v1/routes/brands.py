from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep
from app.models.brand import Brand
from app.repositories.base import BaseRepository
from app.schemas.brand import BrandCreate, BrandResponse, BrandUpdate
from app.schemas.common import PaginatedResponse

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Brand)


@public_router.get("", response_model=PaginatedResponse[BrandResponse])
async def list_brands(
    pagination: PaginationDep,
    search: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BrandResponse]:
    stmt = select(Brand).order_by(Brand.name.asc())
    if search:
        stmt = stmt.where(Brand.name.ilike(f"%{search}%"))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[BrandResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[BrandResponse.model_validate(item) for item in items],
    )


@public_router.get("/{brand_id}", response_model=BrandResponse)
async def get_brand(brand_id: int, session: AsyncSession = Depends(get_db_session)) -> BrandResponse:
    brand = await repo.get(session, brand_id)
    if not brand:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
    return BrandResponse.model_validate(brand)


@backoffice_router.get("", response_model=PaginatedResponse[BrandResponse])
async def backoffice_list_brands(
    pagination: PaginationDep,
    search: str | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BrandResponse]:
    stmt = select(Brand).order_by(Brand.name.asc())
    if search:
        stmt = stmt.where(Brand.name.ilike(f"%{search}%"))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[BrandResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[BrandResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/{brand_id}", response_model=BrandResponse)
async def backoffice_get_brand(
    brand_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BrandResponse:
    brand = await repo.get(session, brand_id)
    if not brand:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
    return BrandResponse.model_validate(brand)


@backoffice_router.post("", response_model=BrandResponse, status_code=status.HTTP_201_CREATED)
async def create_brand(
    payload: BrandCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BrandResponse:
    brand = await repo.create(session, payload.model_dump())
    return BrandResponse.model_validate(brand)


@backoffice_router.put("/{brand_id}", response_model=BrandResponse)
async def update_brand(
    brand_id: int,
    payload: BrandUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BrandResponse:
    brand = await repo.get(session, brand_id)
    if not brand:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
    updated = await repo.update(session, brand, payload.model_dump(exclude_unset=True))
    return BrandResponse.model_validate(updated)


@backoffice_router.delete("/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_brand(
    brand_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    brand = await repo.get(session, brand_id)
    if not brand:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
    await repo.delete(session, brand)
