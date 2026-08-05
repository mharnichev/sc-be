from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.models.booking import Master
from app.schemas.waitlist import PublicWaitlistOfferClaim
from app.services.booking_sms_notifications import BookingSmsNotification, booking_sms_notification_service
from app.services.waitlist_offers import WaitlistOfferService

public_router = APIRouter()
service = WaitlistOfferService()


class WaitlistOfferClaimResponse(BaseModel):
    booking_id: int
    start_at: datetime
    end_at: datetime


@public_router.post("/waitlist/offers/claim", response_model=WaitlistOfferClaimResponse)
async def claim_waitlist_offer(
    background_tasks: BackgroundTasks,
    payload: PublicWaitlistOfferClaim,
    session: AsyncSession = Depends(get_db_session),
) -> WaitlistOfferClaimResponse:
    booking = await service.claim(session, payload.token)
    master = await session.get(Master, booking.master_id)
    notification = BookingSmsNotification(
        booking_id=booking.id,
        master_name=master.full_name_uk if master is not None else "Soul Cuts",
        customer_name=booking.customer_name,
        customer_phone=booking.customer_phone,
        start_at=booking.start_at,
        end_at=booking.end_at,
    )
    body = await booking_sms_notification_service.booking_confirmation_body(session, notification)
    background_tasks.add_task(
        booking_sms_notification_service.send_booking_confirmation,
        notification,
        body=body,
    )
    return WaitlistOfferClaimResponse(
        booking_id=booking.id,
        start_at=booking.start_at,
        end_at=booking.end_at,
    )
