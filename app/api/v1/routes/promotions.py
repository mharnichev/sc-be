from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep, parse_optional_bool_query
from app.models.admin_user import AdminUser
from app.models.promotion import Promotion
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.promotion import PromotionCreate, PromotionResponse, PromotionUpdate
from app.services.promotion import PromotionService

backoffice_router = APIRouter()
repo = BaseRepository(Promotion)
service = PromotionService()


def ensure_superuser(current_user: AdminUser) -> None:
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can manage promotions")


@backoffice_router.get("", response_model=PaginatedResponse[PromotionResponse])
async def list_promotions(
    pagination: PaginationDep,
    is_active: str | None = Query(default=None),
    search: str | None = Query(default=None),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[PromotionResponse]:
    ensure_superuser(current_user)
    parsed_is_active = parse_optional_bool_query(is_active, "is_active")
    stmt = select(Promotion).order_by(Promotion.created_at.desc())
    if parsed_is_active is not None:
        stmt = stmt.where(Promotion.is_active.is_(parsed_is_active))
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Promotion.code.ilike(pattern),
                Promotion.name_uk.ilike(pattern),
                Promotion.name_en.ilike(pattern),
            )
        )
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[PromotionResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[PromotionResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("", response_model=PromotionResponse, status_code=status.HTTP_201_CREATED)
async def create_promotion(
    payload: PromotionCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PromotionResponse:
    ensure_superuser(current_user)
    await service.ensure_unique_code(session, payload.code)
    promotion = Promotion(**payload.model_dump())
    session.add(promotion)
    await session.commit()
    await session.refresh(promotion)
    return PromotionResponse.model_validate(promotion)


@backoffice_router.get("/{promotion_id}", response_model=PromotionResponse)
async def get_promotion(
    promotion_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PromotionResponse:
    ensure_superuser(current_user)
    promotion = await repo.get(session, promotion_id)
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found")
    return PromotionResponse.model_validate(promotion)


@backoffice_router.patch("/{promotion_id}", response_model=PromotionResponse)
async def update_promotion(
    promotion_id: int,
    payload: PromotionUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PromotionResponse:
    ensure_superuser(current_user)
    promotion = await repo.get(session, promotion_id)
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found")
    data = payload.model_dump(exclude_unset=True)
    if "code" in data:
        await service.ensure_unique_code(session, data["code"], exclude_promotion_id=promotion_id)
    data = service.complete_update_data(promotion, data)
    for key, value in data.items():
        setattr(promotion, key, value)
    await session.commit()
    await session.refresh(promotion)
    return PromotionResponse.model_validate(promotion)


@backoffice_router.delete("/{promotion_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_promotion(
    promotion_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    ensure_superuser(current_user)
    promotion = await repo.get(session, promotion_id)
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found")
    promotion.is_active = False
    await session.commit()
