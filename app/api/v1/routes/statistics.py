from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.statistics import (
    AdminDashboardStatisticsResponse,
    AdminMonthlyStatisticsResponse,
    BarberMonthlyStatisticsResponse,
    BarbersComparisonResponse,
)
from app.services.admin_dashboard_statistics import AdminDashboardStatisticsService
from app.services.statistics import StatisticsService

backoffice_router = APIRouter()
statistics_service = StatisticsService()
admin_dashboard_statistics_service = AdminDashboardStatisticsService()


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


@backoffice_router.get(
    "/statistics/admin/dashboard",
    response_model=AdminDashboardStatisticsResponse,
    summary="Owner revenue, capacity, retention and leakage dashboard",
    description=(
        "Admin-only dashboard for an inclusive Europe/Kyiv calendar-date range. "
        "Gross revenue uses completed booking snapshots and must not be interpreted as profit."
    ),
    responses={
        403: {"description": "The authenticated Backoffice user is not a superuser."},
        404: {"description": "The requested visible active master does not exist."},
        422: {"description": "The date range is invalid or exceeds 366 inclusive days."},
    },
)
async def get_admin_dashboard_statistics(
    date_from: date = Query(
        ...,
        description="First included Europe/Kyiv calendar date (ISO 8601).",
        examples=["2026-06-01"],
    ),
    date_to: date = Query(
        ...,
        description="Last included Europe/Kyiv calendar date (ISO 8601).",
        examples=["2026-06-30"],
    ),
    compare_to_previous: bool = Query(
        default=True,
        description="Include the immediately preceding equal-length comparison period.",
    ),
    master_id: int | None = Query(
        default=None,
        ge=1,
        description="Optionally limit every metric and cohort to one visible active master.",
    ),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminDashboardStatisticsResponse:
    ensure_admin(current_user)
    return await admin_dashboard_statistics_service.get_dashboard(
        session,
        date_from=date_from,
        date_to=date_to,
        compare_to_previous=compare_to_previous,
        master_id=master_id,
    )
