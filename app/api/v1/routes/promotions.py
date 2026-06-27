from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep, parse_optional_bool_query
from app.models.admin_user import AdminUser
from app.models.booking import BaseService, Master
from app.models.promotion import Promotion
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.promotion import PromotionCreate, PromotionResponse, PromotionUpdate
from app.services.promotion import PromotionService

backoffice_router = APIRouter()
repo = BaseRepository(Promotion)
service = PromotionService()
promotion_response_options = (
    selectinload(Promotion.masters),
    selectinload(Promotion.base_services),
)


def ensure_superuser(current_user: AdminUser) -> None:
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can manage promotions")


async def get_promotion_for_response(session: AsyncSession, promotion_id: int) -> Promotion:
    promotion = (
        await session.execute(
            select(Promotion)
            .options(*promotion_response_options)
            .where(Promotion.id == promotion_id)
        )
    ).scalar_one_or_none()
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found")
    return promotion


async def load_entities_by_ids(session: AsyncSession, model, ids: list[int], label: str):
    if not ids:
        return []
    items = (await session.execute(select(model).where(model.id.in_(ids)))).scalars().all()
    found_ids = {item.id for item in items}
    missing_ids = sorted(set(ids) - found_ids)
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} not found: {', '.join(str(item) for item in missing_ids)}",
        )
    return items


def split_scope_data(data: dict) -> tuple[dict, dict]:
    scope_keys = {"master_ids", "base_service_ids"}
    scope_data = {key: data.pop(key) for key in list(data.keys()) if key in scope_keys}
    return data, scope_data


async def apply_promotion_scope(
    session: AsyncSession,
    promotion: Promotion,
    data: dict,
    scope_data: dict,
) -> None:
    applies_to_all_masters = data.get("applies_to_all_masters", promotion.applies_to_all_masters)
    applies_to_all_services = data.get("applies_to_all_services", promotion.applies_to_all_services)
    master_ids = scope_data.get("master_ids", promotion.master_ids)
    base_service_ids = scope_data.get("base_service_ids", promotion.base_service_ids)

    if applies_to_all_masters is False and not master_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="master_ids are required")
    if applies_to_all_services is False and not base_service_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="base_service_ids are required")

    for key, value in data.items():
        setattr(promotion, key, value)

    if promotion.applies_to_all_masters:
        promotion.masters = []
    elif "master_ids" in scope_data or data.get("applies_to_all_masters") is False:
        promotion.masters = await load_entities_by_ids(session, Master, list(master_ids), "Masters")

    if promotion.applies_to_all_services:
        promotion.base_services = []
    elif "base_service_ids" in scope_data or data.get("applies_to_all_services") is False:
        promotion.base_services = await load_entities_by_ids(
            session,
            BaseService,
            list(base_service_ids),
            "Base services",
        )


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
    stmt = (
        select(Promotion)
        .options(*promotion_response_options)
        .order_by(Promotion.created_at.desc())
    )
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
    data, scope_data = split_scope_data(payload.model_dump())
    promotion = Promotion()
    session.add(promotion)
    await apply_promotion_scope(session, promotion, data, scope_data)
    await session.commit()
    promotion = await get_promotion_for_response(session, promotion.id)
    return PromotionResponse.model_validate(promotion)


@backoffice_router.get("/{promotion_id}", response_model=PromotionResponse)
async def get_promotion(
    promotion_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PromotionResponse:
    ensure_superuser(current_user)
    promotion = await get_promotion_for_response(session, promotion_id)
    return PromotionResponse.model_validate(promotion)


@backoffice_router.patch("/{promotion_id}", response_model=PromotionResponse)
async def update_promotion(
    promotion_id: int,
    payload: PromotionUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PromotionResponse:
    ensure_superuser(current_user)
    promotion = await get_promotion_for_response(session, promotion_id)
    data = payload.model_dump(exclude_unset=True)
    if "code" in data:
        await service.ensure_unique_code(session, data["code"], exclude_promotion_id=promotion_id)
    data = service.complete_update_data(promotion, data)
    data, scope_data = split_scope_data(data)
    await apply_promotion_scope(session, promotion, data, scope_data)
    await session.commit()
    promotion = await get_promotion_for_response(session, promotion_id)
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
