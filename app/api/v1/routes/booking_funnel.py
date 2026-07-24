from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db_session
from app.core.rate_limit import booking_funnel_rate_limiter, privacy_safe_rate_key
from app.schemas.booking_funnel import BookingFunnelEventReceipt, PublicBookingFunnelEventCreate
from app.services.booking_funnel import BookingFunnelService

public_router = APIRouter()
booking_funnel_service = BookingFunnelService()


@public_router.post(
    "/booking-funnel/events",
    response_model=BookingFunnelEventReceipt,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record a privacy-safe booking funnel event",
)
async def record_booking_funnel_event(
    payload: PublicBookingFunnelEventCreate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> BookingFunnelEventReceipt:
    booking_funnel_rate_limiter.check(
        privacy_safe_rate_key(
            request,
            payload.anonymous_session_id,
            "booking-funnel-event",
        ),
        limit=settings.booking_funnel_event_rate_limit,
    )
    recorded = await booking_funnel_service.record_public_event(session, payload)
    return BookingFunnelEventReceipt(
        event_id=payload.event_id,
        status="recorded" if recorded else "duplicate",
    )
