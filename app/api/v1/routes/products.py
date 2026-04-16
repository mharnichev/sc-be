from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep, parse_optional_bool_query, parse_optional_int_query
from app.models.product import Product
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.product import ProductCreate, ProductResponse, ProductUpdate
from app.services.product import ProductService

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Product)
service = ProductService()


@public_router.get("", response_model=PaginatedResponse[ProductResponse])
async def list_products(
    pagination: PaginationDep,
    category_id: int | None = Query(default=None),
    brand_id: int | None = Query(default=None),
    search: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ProductResponse]:
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category))
        .order_by(Product.created_at.desc())
        .where(Product.is_active.is_(True))
    )
    if category_id:
        stmt = stmt.where(Product.category_id == category_id)
    if brand_id:
        stmt = stmt.where(Product.brand_id == brand_id)
    if search:
        stmt = stmt.where(Product.name.ilike(f"%{search}%"))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[ProductResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[ProductResponse.model_validate(item) for item in items],
    )


@public_router.get("/{product_id}", response_model=ProductResponse)
async def get_product(product_id: int, session: AsyncSession = Depends(get_db_session)) -> ProductResponse:
    stmt = (
        select(Product)
        .options(selectinload(Product.brand), selectinload(Product.category))
        .where(Product.id == product_id)
    )
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()
    if not product or not product.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return ProductResponse.model_validate(product)


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
