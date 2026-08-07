from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.schemas.waitlist import PublicWaitlistCancel, PublicWaitlistCancelResponse, PublicWaitlistCreate, PublicWaitlistResponse
from app.services.waitlist import WaitlistService
from app.services.waitlist_offers import offer_freed_booking_slot
from app.services.customer_activity_notifications import customer_activity_notification_service

public_router = APIRouter()
service = WaitlistService()


@public_router.post("/waitlist", response_model=PublicWaitlistResponse, status_code=status.HTTP_201_CREATED)
async def create_waitlist_request(
    payload: PublicWaitlistCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
) -> PublicWaitlistResponse:
    request, cancel_token = await service.create(session, payload)
    background_tasks.add_task(customer_activity_notification_service.send_waitlist_created, request.id)
    return PublicWaitlistResponse(
        public_id=request.public_id,
        status=request.status,
        expires_at=request.expires_at,
        cancel_token=cancel_token,
    )


@public_router.post("/waitlist/cancel", response_model=PublicWaitlistCancelResponse)
async def cancel_waitlist_request(
    payload: PublicWaitlistCancel,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
) -> PublicWaitlistCancelResponse:
    request, freed_slots = await service.cancel_with_slots(session, payload.cancel_token)
    for freed_slot in freed_slots:
        background_tasks.add_task(offer_freed_booking_slot, freed_slot)
    return PublicWaitlistCancelResponse(public_id=request.public_id, status=request.status)
