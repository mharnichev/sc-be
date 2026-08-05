from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.schemas.booking_alternatives import BookingAlternativesRequest, BookingAlternativesResponse
from app.services.booking_alternatives import BookingAlternativesService
from app.models.booking_recovery import BookingRecoveryEventType
from app.services.booking_recovery_analytics import booking_recovery_analytics_service

public_router = APIRouter()
service = BookingAlternativesService()


@public_router.post("/booking-alternatives", response_model=BookingAlternativesResponse)
async def booking_alternatives(
    payload: BookingAlternativesRequest,
    session: AsyncSession = Depends(get_db_session),
) -> BookingAlternativesResponse:
    """Return currently bookable public recovery slots; an empty group is a valid result."""
    response = await service.find(session, payload)
    request_key = payload.funnel_session_id or str(uuid4())
    context_key = (
        f"{request_key}:{payload.master_id}:{payload.desired_date.isoformat()}:"
        f"{','.join(str(item) for item in sorted(payload.service_ids))}:{payload.duration_minutes}"
    )
    await booking_recovery_analytics_service.record(
        session,
        event_type=BookingRecoveryEventType.alternatives_requested,
        event_key=f"alternatives-requested:{context_key}",
        anonymous_session_id=payload.funnel_session_id,
        master_id=payload.master_id,
        service_id=payload.service_ids[0],
    )
    await booking_recovery_analytics_service.record(
        session,
        event_type=BookingRecoveryEventType.alternatives_returned,
        event_key=f"alternatives-returned:{context_key}",
        anonymous_session_id=payload.funnel_session_id,
        master_id=payload.master_id,
        service_id=payload.service_ids[0],
        metric_value=len(response.same_master) + len(response.other_masters),
    )
    await session.commit()
    return response
