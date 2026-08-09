from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, time, timedelta
from typing import Final

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.booking import Master
from app.models.customer import Customer
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.schemas.waitlist import PublicWaitlistCreate
from app.services.customer_auth import CustomerAuthService
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.models.booking_recovery import BookingRecoveryEventType
from app.services.booking_recovery_analytics import booking_recovery_analytics_service

WAITLIST_EXPIRY_DAYS: Final[int] = settings.waitlist_request_expiry_days


class WaitlistService:
    def __init__(self) -> None:
        self.customer_auth_service = CustomerAuthService()
        self.booking_service = BookingServiceLayer()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hmac.new(settings.secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(32)

    @classmethod
    def _dedup_key(
        cls,
        *,
        normalized_phone: str,
        payload: PublicWaitlistCreate,
        duration_minutes: int,
    ) -> str:
        raw = "|".join(
            (
                normalized_phone,
                ",".join(str(item) for item in sorted(payload.service_ids)),
                str(payload.preferred_master_id or "any"),
                payload.desired_date.isoformat(),
                (payload.acceptable_date_from or payload.desired_date).isoformat(),
                (payload.acceptable_date_to or payload.desired_date).isoformat(),
                payload.preferred_time_from.isoformat() if payload.preferred_time_from else "any",
                payload.preferred_time_to.isoformat() if payload.preferred_time_to else "any",
                str(duration_minutes),
            )
        )
        return cls._hash_token(f"waitlist-dedup:{raw}")

    async def create(self, session: AsyncSession, payload: PublicWaitlistCreate) -> tuple[WaitlistRequest, str]:
        now = datetime.now(KYIV_TZ)
        today = now.date()
        if payload.desired_date < today:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="desired_date must not be in the past")
        if payload.desired_date > self.booking_service.availability_horizon_end_date():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="desired_date is outside availability horizon")
        if not payload.notification_consent:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Availability notification consent is required")
        normalized_phone = self.customer_auth_service.normalize_phone(payload.customer_phone)
        services = await self.booking_service.get_active_services(session, payload.service_ids)
        master = None
        if payload.preferred_master_id is not None:
            master = await self.booking_service.get_active_master_with_services(session, payload.preferred_master_id)
            if not bool(getattr(master, "show_on_master_block", True)):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preferred master not found")
            self.booking_service.ensure_master_provides_services(master, payload.service_ids)
        required_duration = sum(item.duration_minutes for item in services)
        duration = payload.duration_minutes or required_duration
        if duration != required_duration:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="duration_minutes must equal the selected services duration",
            )
        customer = (await session.execute(select(Customer).where(Customer.phone == normalized_phone))).scalar_one_or_none()
        if customer is None:
            customer = Customer(phone=normalized_phone, name=payload.customer_name, is_active=True)
            session.add(customer)
            await session.flush()
        elif not customer.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer is inactive")
        date_from = payload.acceptable_date_from or payload.desired_date
        date_to = payload.acceptable_date_to or payload.desired_date
        if date_from < today or date_to > self.booking_service.availability_horizon_end_date():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="acceptable date range is outside availability horizon",
            )
        dedup_key_hash = self._dedup_key(
            normalized_phone=normalized_phone,
            payload=payload,
            duration_minutes=duration,
        )
        existing = (
            await session.execute(
                select(WaitlistRequest.id).where(
                    WaitlistRequest.dedup_key_hash == dedup_key_hash,
                    WaitlistRequest.status.in_((WaitlistStatus.active, WaitlistStatus.offered)),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An equivalent active waitlist request already exists")
        token = self._new_token()
        # Requests expire immediately after their final acceptable Kyiv date; the
        # configured TTL is an upper guard for unusually broad future ranges.
        range_expiry = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=KYIV_TZ)
        expires_at = min(range_expiry, now + timedelta(days=WAITLIST_EXPIRY_DAYS))
        request = WaitlistRequest(
            cancel_token_hash=self._hash_token(token), dedup_key_hash=dedup_key_hash, customer_id=customer.id,
            preferred_master_id=master.id if master else None, desired_date=payload.desired_date,
            acceptable_date_from=date_from, acceptable_date_to=date_to,
            preferred_time_from=payload.preferred_time_from, preferred_time_to=payload.preferred_time_to,
            duration_minutes=duration, notification_consent=True, status=WaitlistStatus.active,
            expires_at=expires_at, services=services,
        )
        session.add(request)
        try:
            await session.flush()
            await booking_recovery_analytics_service.record(
                session,
                event_type=BookingRecoveryEventType.waitlist_submitted,
                event_key=f"waitlist-submitted:{request.public_id}",
                master_id=request.preferred_master_id,
                service_id=payload.service_ids[0],
                waitlist_request_id=request.id,
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An equivalent active waitlist request already exists",
            ) from exc
        await session.refresh(request)
        return request, token

    async def cancel(self, session: AsyncSession, cancel_token: str) -> WaitlistRequest:
        token_hash = self._hash_token(cancel_token)
        request = (await session.execute(select(WaitlistRequest).where(WaitlistRequest.cancel_token_hash == token_hash).with_for_update())).scalar_one_or_none()
        if request is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Waitlist request not found")
        request, _ = await self.cancel_request(session, request)
        return request

    async def cancel_with_slots(self, session: AsyncSession, cancel_token: str):
        """Cancel by legacy opaque token and return holds that must be re-offered."""
        token_hash = self._hash_token(cancel_token)
        request = (await session.execute(select(WaitlistRequest).where(WaitlistRequest.cancel_token_hash == token_hash).with_for_update())).scalar_one_or_none()
        if request is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Waitlist request not found")
        return await self.cancel_request(session, request)

    async def cancel_request(self, session: AsyncSession, request: WaitlistRequest):
        """Atomically cancel a request and collect slots after releasing the lock.

        The caller schedules matching only *after* this commit.  That avoids
        finding the just-cancelled hold as a conflicting offer in the same
        transaction, while preserving the original source booking reference.
        """
        if request.status not in (WaitlistStatus.active, WaitlistStatus.offered):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Waitlist request is no longer active")
        request.status = WaitlistStatus.cancelled
        request.closed_at = datetime.now(UTC)
        request.close_reason = "cancelled_by_customer"
        offers = (
            await session.execute(
                select(WaitlistOffer)
                .where(
                    WaitlistOffer.request_id == request.id,
                    WaitlistOffer.status.in_(
                        (WaitlistOfferStatus.pending, WaitlistOfferStatus.sent, WaitlistOfferStatus.delivered)
                    ),
                )
                .with_for_update()
            )
        ).scalars().all()
        # Some lightweight legacy test/session adapters do not distinguish the
        # second query. Production SQLAlchemy always returns a list here.
        if not isinstance(offers, list):
            offers = []
        # Local import keeps the waitlist base service independent from the
        # scheduler module at import time.
        from app.services.waitlist_offers import FreedBookingSlot

        slots = [
            FreedBookingSlot(
                master_id=offer.master_id,
                start_at=offer.start_at,
                end_at=offer.end_at,
                source_booking_id=offer.source_booking_id,
                source_master_id=offer.source_master_id or offer.master_id,
            )
            for offer in offers
        ]
        for offer in offers:
            offer.status = WaitlistOfferStatus.cancelled
            offer.closed_at = request.closed_at
            offer.close_reason = "request_cancelled_by_customer"
        await session.commit()
        await session.refresh(request)
        return request, slots

    async def expire_due_requests(self, session: AsyncSession, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        requests = list((await session.execute(select(WaitlistRequest).where(
            WaitlistRequest.status.in_((WaitlistStatus.active, WaitlistStatus.offered)),
            WaitlistRequest.expires_at <= now,
        ).with_for_update())).scalars())
        for request in requests:
            request.status, request.closed_at, request.close_reason = WaitlistStatus.expired, now, "expired"
        if requests:
            request_ids = [item.id for item in requests]
            await session.execute(
                update(WaitlistOffer)
                .where(
                    WaitlistOffer.request_id.in_(request_ids),
                    WaitlistOffer.status.in_(
                        (WaitlistOfferStatus.pending, WaitlistOfferStatus.sent, WaitlistOfferStatus.delivered)
                    ),
                )
                .values(
                    status=WaitlistOfferStatus.expired,
                    closed_at=now,
                    close_reason="request_expired",
                )
            )
            await session.commit()
        return len(requests)
