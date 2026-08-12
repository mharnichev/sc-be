from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.repeat_booking import (
    RepeatBookingAnalyticsSummary,
    RepeatBookingContext,
    RepeatBookingStartResponse,
)
from app.services.repeat_booking import repeat_booking_service


public_router = APIRouter()
backoffice_router = APIRouter()
PRIVATE_REPEAT_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "Vary": "X-Repeat-Booking-Token",
}


def _token(value: str | None) -> str:
    if value is None or not 32 <= len(value) <= 512:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Repeat booking token is required",
            headers=PRIVATE_REPEAT_HEADERS,
        )
    return value


def _private_error(exc: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=exc.detail,
        headers={**(exc.headers or {}), **PRIVATE_REPEAT_HEADERS},
    )


@public_router.get("/repeat-booking/context", response_model=RepeatBookingContext)
async def get_repeat_booking_context(
    response: Response,
    x_repeat_booking_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> RepeatBookingContext:
    response.headers.update(PRIVATE_REPEAT_HEADERS)
    try:
        return await repeat_booking_service.context(session, _token(x_repeat_booking_token))
    except HTTPException as exc:
        raise _private_error(exc) from exc


@public_router.post("/repeat-booking/start", response_model=RepeatBookingStartResponse)
async def start_repeat_booking(
    response: Response,
    x_repeat_booking_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> RepeatBookingStartResponse:
    response.headers.update(PRIVATE_REPEAT_HEADERS)
    try:
        context = await repeat_booking_service.mark_started(session, _token(x_repeat_booking_token))
    except HTTPException as exc:
        raise _private_error(exc) from exc
    return RepeatBookingStartResponse(context=context)


@backoffice_router.get("/repeat-booking/analytics", response_model=RepeatBookingAnalyticsSummary)
async def get_repeat_booking_analytics(
    date_from: date = Query(),
    date_to: date = Query(),
    _current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> RepeatBookingAnalyticsSummary:
    return await repeat_booking_service.analytics(session, date_from=date_from, date_to=date_to)
