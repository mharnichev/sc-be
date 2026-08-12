from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.booking import Booking, BookingServiceItem, BookingStatus
from app.models.customer import Customer
from app.models.customer_activity import CustomerActivityAccessToken
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.schemas.customer_activity import (
    CustomerActivityBooking,
    CustomerActivityResponse,
    CustomerActivityWaitlist,
)
from app.services.waitlist import WaitlistService
from app.services.waitlist_offers import FreedBookingSlot

BROWSER_SESSION_COOKIE_NAME = "sc_customer_activity"
BROWSER_SESSION_COOKIE_PATH = "/api/v1/public/customer-activity"
BROWSER_SESSION_SOURCE = "browser_booking"


def set_browser_session_cookie(response: Response, token: str, *, expires_at: datetime) -> None:
    response.set_cookie(
        key=BROWSER_SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.customer_activity_browser_session_ttl_days * 24 * 60 * 60,
        expires=expires_at,
        path=BROWSER_SESSION_COOKIE_PATH,
        secure=settings.app_env == "production",
        httponly=True,
        samesite="lax",
    )


def clear_browser_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=BROWSER_SESSION_COOKIE_NAME,
        path=BROWSER_SESSION_COOKIE_PATH,
        secure=settings.app_env == "production",
        httponly=True,
        samesite="lax",
    )


class CustomerActivityService:
    """Customer self-service using an expiring, hash-only bearer capability."""

    def __init__(self) -> None:
        self.waitlist_service = WaitlistService()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hmac.new(settings.secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(32)

    async def create_access_token(
        self,
        session: AsyncSession,
        customer_id: int,
        *,
        source: str,
        expires_at: datetime,
        source_booking_id: int | None = None,
        source_waitlist_request_id: int | None = None,
        recipient_id: int | None = None,
        max_lifetime_days: int | None = None,
    ) -> str:
        now = datetime.now(UTC)
        if max_lifetime_days is not None and not 1 <= max_lifetime_days <= 31:
            raise ValueError("A dedicated access-token lifetime cap must be between 1 and 31 days")
        if recipient_id is not None:
            # A retry replaces the previous passwordless session capability for
            # this SMS. A stale/delayed phone message cannot remain valid.
            await session.execute(
                update(CustomerActivityAccessToken)
                .where(
                    CustomerActivityAccessToken.recipient_id == recipient_id,
                    CustomerActivityAccessToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        max_expiry = now + timedelta(
            days=max_lifetime_days
            if max_lifetime_days is not None
            else settings.customer_activity_token_max_days
        )
        expires_at = min(expires_at, max_expiry)
        token = self._new_token()
        session.add(
            CustomerActivityAccessToken(
                token_hash=self._hash_token(token),
                customer_id=customer_id,
                source=source,
                expires_at=expires_at,
                source_booking_id=source_booking_id,
                source_waitlist_request_id=source_waitlist_request_id,
                recipient_id=recipient_id,
            )
        )
        await session.flush()
        return token

    async def create_browser_session(
        self,
        session: AsyncSession,
        customer_id: int,
        *,
        source_booking_id: int,
    ) -> tuple[str, datetime]:
        """Create one fixed-lifetime browser capability without touching SMS tokens."""
        ttl_days = settings.customer_activity_browser_session_ttl_days
        expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
        token = await self.create_access_token(
            session,
            customer_id,
            source=BROWSER_SESSION_SOURCE,
            source_booking_id=source_booking_id,
            expires_at=expires_at,
            max_lifetime_days=ttl_days,
        )
        return token, expires_at

    async def revoke_browser_session(self, session: AsyncSession, token: str) -> None:
        """Revoke only a browser-session capability; missing tokens are a no-op."""
        await session.execute(
            update(CustomerActivityAccessToken)
            .where(
                CustomerActivityAccessToken.token_hash == self._hash_token(token),
                CustomerActivityAccessToken.source == BROWSER_SESSION_SOURCE,
                CustomerActivityAccessToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        await session.commit()

    async def customer_for_token(self, session: AsyncSession, token: str) -> Customer:
        now = datetime.now(UTC)
        access = (
            await session.execute(
                select(CustomerActivityAccessToken)
                .where(
                    CustomerActivityAccessToken.token_hash == self._hash_token(token),
                    CustomerActivityAccessToken.revoked_at.is_(None),
                    CustomerActivityAccessToken.expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            access is None
            or access.revoked_at is not None
            or access.expires_at <= now
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired activity link")
        customer = await session.get(Customer, access.customer_id)
        if customer is None or not customer.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid activity link")
        access.last_used_at = now
        access.use_count += 1
        await session.commit()
        return customer

    @staticmethod
    def urls_for_token(token: str) -> tuple[str, str]:
        """Build view/cancel URLs without creating or persisting another token."""
        root = settings.public_site_url.rstrip("/")
        return (
            f"{root}{settings.customer_activity_public_path}#{token}",
            f"{root}{settings.customer_activity_cancel_public_path}#{token}",
        )

    async def activity(self, session: AsyncSession, customer: Customer) -> CustomerActivityResponse:
        now = datetime.now(UTC)
        bookings = list(
            (
                await session.execute(
                    select(Booking)
                    .options(
                        selectinload(Booking.master),
                        selectinload(Booking.redirected_from_master),
                        selectinload(Booking.service),
                        selectinload(Booking.service_items).selectinload(BookingServiceItem.service),
                    )
                    .where(
                        Booking.customer_id == customer.id,
                        Booking.status == BookingStatus.confirmed,
                        Booking.start_at > now,
                    )
                    .order_by(Booking.start_at.asc(), Booking.id.asc())
                )
            ).scalars()
        )
        requests = list(
            (
                await session.execute(
                    select(WaitlistRequest)
                    .options(
                        selectinload(WaitlistRequest.preferred_master),
                        selectinload(WaitlistRequest.services),
                        selectinload(WaitlistRequest.offers),
                    )
                    .where(
                        WaitlistRequest.customer_id == customer.id,
                        WaitlistRequest.status.in_((WaitlistStatus.active, WaitlistStatus.offered)),
                        WaitlistRequest.expires_at > now,
                    )
                    .order_by(WaitlistRequest.created_at.asc(), WaitlistRequest.id.asc())
                )
            ).scalars()
        )
        return CustomerActivityResponse(
            bookings=[self._booking(item) for item in bookings],
            waitlist=[self._waitlist(item, now=now) for item in requests],
        )

    async def cancel_booking(
        self,
        session: AsyncSession,
        customer: Customer,
        booking_public_id: str,
    ) -> tuple[Booking, FreedBookingSlot]:
        booking = (
            await session.execute(
                select(Booking)
                .where(Booking.public_id == booking_public_id, Booking.customer_id == customer.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if booking is None:
            # Do not turn this endpoint into an oracle for somebody else's booking.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
        now = datetime.now(UTC)
        if booking.status != BookingStatus.confirmed:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Booking is no longer active")
        if booking.start_at <= now:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Past bookings cannot be cancelled")
        freed_slot = FreedBookingSlot(
            master_id=booking.master_id,
            start_at=booking.start_at,
            end_at=booking.end_at,
            source_booking_id=booking.id,
            source_master_id=booking.redirected_from_master_id or booking.master_id,
        )
        booking.status = BookingStatus.cancelled
        booking.cancelled_at = now
        booking.completed_at = None
        await session.commit()
        await session.refresh(booking)
        return booking, freed_slot

    async def cancel_waitlist(
        self,
        session: AsyncSession,
        customer: Customer,
        request_public_id: str,
    ) -> tuple[WaitlistRequest, list[FreedBookingSlot]]:
        request = (
            await session.execute(
                select(WaitlistRequest)
                .where(WaitlistRequest.public_id == request_public_id, WaitlistRequest.customer_id == customer.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if request is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Waitlist request not found")
        return await self.waitlist_service.cancel_request(session, request)

    @staticmethod
    def _booking(booking: Booking) -> CustomerActivityBooking:
        services = list(booking.services)
        public_master = getattr(booking, "redirected_from_master", None) or booking.master
        return CustomerActivityBooking(
            public_id=booking.public_id,
            master_name=(public_master.full_name_uk if public_master is not None else ""),
            service_names=[item.title_uk or item.name for item in services],
            start_at=booking.start_at,
            end_at=booking.end_at,
            status=booking.status,
        )

    @staticmethod
    def _waitlist(request: WaitlistRequest, *, now: datetime) -> CustomerActivityWaitlist:
        active_offer = next(
            (
                item
                for item in request.offers
                if item.status in (WaitlistOfferStatus.pending, WaitlistOfferStatus.sent, WaitlistOfferStatus.delivered)
                and item.expires_at > now
            ),
            None,
        )
        return CustomerActivityWaitlist(
            public_id=request.public_id,
            master_name=(request.preferred_master.full_name_uk if request.preferred_master is not None else None),
            service_names=[item.title_uk or item.name for item in request.services],
            desired_date=request.desired_date,
            preferred_time_from=request.preferred_time_from,
            preferred_time_to=request.preferred_time_to,
            status=request.status,
            expires_at=request.expires_at,
            offered_start_at=active_offer.start_at if active_offer else None,
            offered_end_at=active_offer.end_at if active_offer else None,
            offer_expires_at=active_offer.expires_at if active_offer else None,
        )


customer_activity_service = CustomerActivityService()
