from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.booking_recovery import BookingRecoveryEventType
from app.schemas.booking_recovery import (
    BookingRecoveryEventReceipt,
    BookingRecoverySummary,
    PublicBookingRecoveryEventCreate,
)
from app.services.booking_recovery_analytics import booking_recovery_analytics_service
from app.services.booking import BookingServiceLayer


public_router = APIRouter()
backoffice_router = APIRouter()
booking_service = BookingServiceLayer()


@public_router.post("/booking-recovery/events", response_model=BookingRecoveryEventReceipt)
async def record_booking_recovery_event(
    payload: PublicBookingRecoveryEventCreate,
    session: AsyncSession = Depends(get_db_session),
) -> BookingRecoveryEventReceipt:
    if payload.master_id is not None:
        master_exists = (
            await session.execute(
                select(Master.id).where(
                    Master.id == payload.master_id,
                    Master.is_active.is_(True),
                    Master.show_on_master_block.is_(True),
                )
            )
        ).scalar_one_or_none()
        if master_exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    if payload.service_id is not None:
        await booking_service.get_active_service(session, payload.service_id)
    recorded = await booking_recovery_analytics_service.record(
        session,
        event_type=BookingRecoveryEventType(payload.event_type),
        event_key=f"client:{payload.event_id}",
        anonymous_session_id=payload.anonymous_session_id,
        master_id=payload.master_id,
        service_id=payload.service_id,
        commit=True,
    )
    return BookingRecoveryEventReceipt(
        event_id=payload.event_id,
        status="recorded" if recorded else "duplicate",
    )


@backoffice_router.get("/booking-recovery/summary", response_model=BookingRecoverySummary)
async def get_booking_recovery_summary(
    date_from: date = Query(),
    date_to: date = Query(),
    _current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BookingRecoverySummary:
    return await booking_recovery_analytics_service.summary(
        session,
        date_from=date_from,
        date_to=date_to,
    )
