from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.models.customer import Customer
from app.schemas.customer_activity import (
    CustomerActivityBookingCancelResponse,
    CustomerActivityBrowserSessionForgetResponse,
    CustomerActivityResponse,
    CustomerActivityWaitlistCancelResponse,
)
from app.services.customer_activity import (
    BROWSER_SESSION_COOKIE_NAME,
    clear_browser_session_cookie,
    customer_activity_service,
    set_browser_session_cookie,
)
from app.services.waitlist_offers import offer_freed_booking_slot

public_router = APIRouter()
PRIVATE_ACTIVITY_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "Vary": "X-Customer-Activity-Token, Cookie",
}


def prevent_customer_activity_caching(response: Response) -> None:
    response.headers.update(PRIVATE_ACTIVITY_HEADERS)


def private_activity_error(exc: HTTPException) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=exc.detail,
        headers={**(exc.headers or {}), **PRIVATE_ACTIVITY_HEADERS},
    )


async def get_activity_customer(
    x_customer_activity_token: str | None = Header(default=None),
    browser_session_token: str | None = Cookie(default=None, alias=BROWSER_SESSION_COOKIE_NAME),
    session: AsyncSession = Depends(get_db_session),
) -> Customer:
    token = x_customer_activity_token if x_customer_activity_token is not None else browser_session_token
    if not token or not 32 <= len(token) <= 512:
        raise private_activity_error(
            HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Activity token is required")
        )
    try:
        return await customer_activity_service.customer_for_token(session, token)
    except HTTPException as exc:
        raise private_activity_error(exc) from exc


@public_router.post(
    "/customer-activity/browser-session/forget",
    response_model=CustomerActivityBrowserSessionForgetResponse,
)
async def forget_customer_activity_browser_session(
    response: Response,
    browser_session_token: str | None = Cookie(default=None, alias=BROWSER_SESSION_COOKIE_NAME),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerActivityBrowserSessionForgetResponse:
    prevent_customer_activity_caching(response)
    if browser_session_token and 32 <= len(browser_session_token) <= 512:
        await customer_activity_service.revoke_browser_session(session, browser_session_token)
    clear_browser_session_cookie(response)
    return CustomerActivityBrowserSessionForgetResponse()


@public_router.get("/customer-activity", response_model=CustomerActivityResponse)
async def get_customer_activity(
    response: Response,
    current_customer: Customer = Depends(get_activity_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerActivityResponse:
    prevent_customer_activity_caching(response)
    return await customer_activity_service.activity(session, current_customer)


@public_router.post(
    "/customer-activity/bookings/{booking_public_id}/cancel",
    response_model=CustomerActivityBookingCancelResponse,
)
async def cancel_customer_booking(
    booking_public_id: str,
    background_tasks: BackgroundTasks,
    response: Response,
    current_customer: Customer = Depends(get_activity_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerActivityBookingCancelResponse:
    prevent_customer_activity_caching(response)
    try:
        booking, freed_slot = await customer_activity_service.cancel_booking(
            session, current_customer, booking_public_id
        )
    except HTTPException as exc:
        raise private_activity_error(exc) from exc
    # Matching owns a fresh transaction and must observe the committed cancellation.
    background_tasks.add_task(offer_freed_booking_slot, freed_slot)
    return CustomerActivityBookingCancelResponse(
        public_id=booking.public_id,
        status=booking.status,
        cancelled_at=booking.cancelled_at,
    )


@public_router.post(
    "/customer-activity/waitlist/{request_public_id}/cancel",
    response_model=CustomerActivityWaitlistCancelResponse,
)
async def cancel_customer_waitlist(
    request_public_id: str,
    background_tasks: BackgroundTasks,
    response: Response,
    current_customer: Customer = Depends(get_activity_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerActivityWaitlistCancelResponse:
    prevent_customer_activity_caching(response)
    try:
        request, freed_slots = await customer_activity_service.cancel_waitlist(
            session, current_customer, request_public_id
        )
    except HTTPException as exc:
        raise private_activity_error(exc) from exc
    for slot in freed_slots:
        background_tasks.add_task(offer_freed_booking_slot, slot)
    return CustomerActivityWaitlistCancelResponse(public_id=request.public_id, status=request.status)
