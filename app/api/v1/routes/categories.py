from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep
from app.models.category import Category
from app.repositories.base import BaseRepository
from app.schemas.category import CategoryCreate, CategoryResponse, CategoryUpdate
from app.schemas.common import PaginatedResponse

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(Category)


@public_router.get("", response_model=PaginatedResponse[CategoryResponse])
async def list_categories(
    pagination: PaginationDep,
    search: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CategoryResponse]:
    stmt = select(Category).order_by(Category.name.asc())
    stmt = stmt.where(Category.is_active.is_(True))
    if search:
        stmt = stmt.where(Category.name.ilike(f"%{search}%"))
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[CategoryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[CategoryResponse.model_validate(item) for item in items],
    )


@public_router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(category_id: int, session: AsyncSession = Depends(get_db_session)) -> CategoryResponse:
    category = await repo.get(session, category_id)
    if not category or not category.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return CategoryResponse.model_validate(category)


@backoffice_router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CategoryResponse:
    category = await repo.create(session, payload.model_dump())
    return CategoryResponse.model_validate(category)


@backoffice_router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: int,
    payload: CategoryUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CategoryResponse:
    category = await repo.get(session, category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    updated = await repo.update(session, category, payload.model_dump(exclude_unset=True))
    return CategoryResponse.model_validate(updated)


@backoffice_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    category = await repo.get(session, category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    await repo.delete(session, category)
