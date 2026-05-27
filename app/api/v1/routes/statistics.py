from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.statistics import AdminMonthlyStatisticsResponse, BarberMonthlyStatisticsResponse, BarbersComparisonResponse
from app.services.statistics import StatisticsService

backoffice_router = APIRouter()
statistics_service = StatisticsService()


def ensure_admin(current_user: AdminUser) -> None:
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can view global statistics")


@backoffice_router.get("/statistics/me/monthly", response_model=BarberMonthlyStatisticsResponse)
async def get_my_monthly_statistics(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BarberMonthlyStatisticsResponse:
    master = await statistics_service.get_linked_master_or_403(session, current_user.id)
    return await statistics_service.get_barber_monthly_statistics(
        session,
        year=year,
        month=month,
        barber_id=master.id,
    )


@backoffice_router.get("/statistics/barbers/{barber_id}/monthly", response_model=BarberMonthlyStatisticsResponse)
async def get_barber_monthly_statistics(
    barber_id: int,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BarberMonthlyStatisticsResponse:
    if not current_user.is_superuser:
        master = await statistics_service.get_linked_master_or_403(session, current_user.id)
        if master.id != barber_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view another barber's statistics")
    return await statistics_service.get_barber_monthly_statistics(
        session,
        year=year,
        month=month,
        barber_id=barber_id,
    )


@backoffice_router.get("/statistics/admin/monthly", response_model=AdminMonthlyStatisticsResponse)
async def get_admin_monthly_statistics(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    barber_id: int | None = Query(default=None),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminMonthlyStatisticsResponse:
    ensure_admin(current_user)
    return await statistics_service.get_admin_monthly_statistics(
        session,
        year=year,
        month=month,
        barber_id=barber_id,
    )


@backoffice_router.get("/statistics/admin/barbers-comparison", response_model=BarbersComparisonResponse)
async def get_admin_barbers_comparison(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BarbersComparisonResponse:
    ensure_admin(current_user)
    return await statistics_service.get_barbers_comparison(session, year=year, month=month)
