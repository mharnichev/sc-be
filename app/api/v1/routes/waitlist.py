from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.schemas.waitlist import PublicWaitlistCancel, PublicWaitlistCancelResponse, PublicWaitlistCreate, PublicWaitlistResponse
from app.services.waitlist import WaitlistService

public_router = APIRouter()
service = WaitlistService()


@public_router.post("/waitlist", response_model=PublicWaitlistResponse, status_code=status.HTTP_201_CREATED)
async def create_waitlist_request(payload: PublicWaitlistCreate, session: AsyncSession = Depends(get_db_session)) -> PublicWaitlistResponse:
    request, cancel_token = await service.create(session, payload)
    return PublicWaitlistResponse(
        public_id=request.public_id,
        status=request.status,
        expires_at=request.expires_at,
        cancel_token=cancel_token,
    )


@public_router.post("/waitlist/cancel", response_model=PublicWaitlistCancelResponse)
async def cancel_waitlist_request(payload: PublicWaitlistCancel, session: AsyncSession = Depends(get_db_session)) -> PublicWaitlistCancelResponse:
    request = await service.cancel(session, payload.cancel_token)
    return PublicWaitlistCancelResponse(public_id=request.public_id, status=request.status)
