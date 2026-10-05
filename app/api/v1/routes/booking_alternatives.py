import logging
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
logger = logging.getLogger(__name__)


@public_router.post("/booking-alternatives", response_model=BookingAlternativesResponse)
async def booking_alternatives(
    payload: BookingAlternativesRequest,
    session: AsyncSession = Depends(get_db_session),
) -> BookingAlternativesResponse:
    """Return currently bookable public recovery slots; an empty group is a valid result."""
    # The duration is validated by the lookup against the selected active
    # services.  It is also part of the public opaque context identifier, so
    # request start and a successful response join to the same cohort.
    search_context_id = service._search_context_id(payload, payload.duration_minutes)
    request_key = payload.funnel_session_id or str(uuid4())
    context_key = f"{request_key}:{search_context_id}"
    # Analytics is deliberately optional so an outage can never turn a recovery
    # lookup (or a later booking) into a 500.
    try:
        await booking_recovery_analytics_service.record(
            session,
            event_type=BookingRecoveryEventType.alternatives_requested,
            event_key=f"alternatives-requested:{context_key}",
            anonymous_session_id=payload.funnel_session_id,
            search_context_id=search_context_id,
            master_id=payload.master_id,
            service_id=payload.service_ids[0],
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Booking alternatives request analytics failed")

    response = await service.find_page(session, payload)
    try:
        await booking_recovery_analytics_service.record(
            session,
            event_type=BookingRecoveryEventType.alternatives_returned,
            event_key=f"alternatives-returned:{context_key}:{payload.offset}:{payload.page_size}",
            anonymous_session_id=payload.funnel_session_id,
            search_context_id=search_context_id,
            master_id=payload.master_id,
            service_id=payload.service_ids[0],
            metric_value=len(response.same_master) + len(response.other_masters),
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Booking alternatives response analytics failed")
    return response
