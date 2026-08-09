from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.schemas.waitlist import PublicWaitlistOfferClaim
from app.services.customer_activity_notifications import customer_activity_notification_service
from app.services.waitlist_offers import WaitlistOfferService

public_router = APIRouter()
service = WaitlistOfferService()


class WaitlistOfferClaimResponse(BaseModel):
    public_id: str
    start_at: datetime
    end_at: datetime


@public_router.post("/waitlist/offers/claim", response_model=WaitlistOfferClaimResponse)
async def claim_waitlist_offer(
    background_tasks: BackgroundTasks,
    payload: PublicWaitlistOfferClaim,
    session: AsyncSession = Depends(get_db_session),
) -> WaitlistOfferClaimResponse:
    booking = await service.claim(session, payload.token)
    background_tasks.add_task(
        customer_activity_notification_service.send_booking_confirmation,
        booking.id,
    )
    return WaitlistOfferClaimResponse(
        public_id=booking.public_id,
        start_at=booking.start_at,
        end_at=booking.end_at,
    )
