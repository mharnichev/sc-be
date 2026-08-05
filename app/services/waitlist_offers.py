from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.booking_recovery import BookingRecoveryEventType
from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.booking_recovery_analytics import booking_recovery_analytics_service
from app.services.messaging import MessagingService
from app.services.sms import SmsDeliveryStatus, SmsService


logger = logging.getLogger(__name__)
ACTIVE_HOLD_STATUSES = (WaitlistOfferStatus.sent, WaitlistOfferStatus.delivered)
OPEN_OFFER_STATUSES = (WaitlistOfferStatus.pending, *ACTIVE_HOLD_STATUSES)


@dataclass(frozen=True)
class FreedBookingSlot:
    master_id: int
    start_at: datetime
    end_at: datetime
    source_booking_id: int | None = None


class WaitlistOfferService:
    """Matches, offers, holds and atomically claims a newly free booking slot."""

    def __init__(
        self,
        *,
        hold_minutes: int | None = None,
        sms_service: SmsService | None = None,
    ) -> None:
        self.hold_minutes = hold_minutes or settings.waitlist_offer_hold_minutes
        self.sms_service = sms_service or SmsService()
        self.booking_service = BookingServiceLayer()

    @staticmethod
    def _hash(token: str) -> str:
        return hmac.new(
            settings.secret_key.encode("utf-8"),
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(KYIV_TZ)

    @staticmethod
    def _booking_link(token: str, booking_link_base: str | None = None) -> str:
        if booking_link_base:
            base = booking_link_base.rstrip("/")
        else:
            base = f"{settings.public_site_url.rstrip('/')}{settings.waitlist_offer_public_path}"
        return f"{base}#{token}"

    @staticmethod
    def _in_time_preference(
        request: WaitlistRequest,
        start_at: datetime,
        end_at: datetime | None = None,
    ) -> bool:
        local = start_at.astimezone(KYIV_TZ).timetz().replace(tzinfo=None)
        local_end = (
            end_at.astimezone(KYIV_TZ).timetz().replace(tzinfo=None)
            if end_at is not None
            else local
        )
        return not (
            (request.preferred_time_from and local < request.preferred_time_from)
            or (request.preferred_time_to and local_end > request.preferred_time_to)
        )

    @staticmethod
    def _in_date_range(request: WaitlistRequest, start_at: datetime) -> bool:
        day = start_at.astimezone(KYIV_TZ).date()
        date_from = request.acceptable_date_from or request.desired_date
        date_to = request.acceptable_date_to or request.desired_date
        return date_from <= day <= date_to

    @staticmethod
    def _scheduled_send_at(now: datetime) -> datetime:
        return MessagingService.adjust_for_quiet_hours(
            now,
            quiet_from=settings.waitlist_quiet_hours_from,
            quiet_to=settings.waitlist_quiet_hours_to,
        )

    @staticmethod
    def _communication_allowed(
        request: WaitlistRequest,
        preference: ClientCommunicationPreference | None,
    ) -> bool:
        if not request.notification_consent:
            return False
        if preference is None:
            return True
        return not (
            preference.do_not_contact
            or preference.blacklisted_at is not None
            or preference.transactional_consent == ConsentStatus.opted_out
        )

    def _matching_services(
        self,
        master: Master,
        requested_services: list[BarberService],
    ) -> list[BarberService] | None:
        active = [item for item in master.services if self.booking_service.is_active_service(item)]
        matched: list[BarberService] = []
        for source in requested_services:
            target = next((item for item in active if item.id == source.id), None)
            if target is None and source.base_service_id is not None:
                target = next(
                    (item for item in active if item.base_service_id == source.base_service_id),
                    None,
                )
            if target is None and source.base_service_id is None:
                source_key = self.booking_service.custom_service_key(source)
                target = next(
                    (
                        item
                        for item in active
                        if item.base_service_id is None
                        and self.booking_service.custom_service_key(item) == source_key
                    ),
                    None,
                )
            if target is None or any(item.id == target.id for item in matched):
                return None
            matched.append(target)
        return matched

    async def expire_holds(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> list[WaitlistOffer]:
        now = now or self._now()
        offers = list(
            (
                await session.execute(
                    select(WaitlistOffer)
                    .where(
                        WaitlistOffer.status.in_(ACTIVE_HOLD_STATUSES),
                        WaitlistOffer.expires_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        for offer in offers:
            offer.status = WaitlistOfferStatus.expired
            offer.closed_at = now
            offer.close_reason = "hold_expired"
            request = await session.get(WaitlistRequest, offer.request_id, with_for_update=True)
            if request and request.status == WaitlistStatus.offered:
                request.status = WaitlistStatus.active
                request.offered_at = None
            await booking_recovery_analytics_service.record(
                session,
                event_type=BookingRecoveryEventType.waitlist_offer_expired,
                event_key=f"waitlist-offer-expired:{offer.id}",
                master_id=offer.master_id,
                waitlist_request_id=offer.request_id,
                waitlist_offer_id=offer.id,
                source_booking_id=offer.source_booking_id,
                occurred_at=now,
            )
        if offers:
            await session.flush()
        return offers

    async def _eligible_requests(
        self,
        session: AsyncSession,
        master: Master,
        start_at: datetime,
        end_at: datetime,
    ) -> list[tuple[WaitlistRequest, list[BarberService]]]:
        now = self._now()
        rows = list(
            (
                await session.execute(
                    select(WaitlistRequest)
                    .options(
                        selectinload(WaitlistRequest.services),
                        selectinload(WaitlistRequest.customer),
                    )
                    .where(
                        WaitlistRequest.status == WaitlistStatus.active,
                        WaitlistRequest.expires_at > now,
                        WaitlistRequest.notification_consent.is_(True),
                        WaitlistRequest.duration_minutes
                        == int((end_at - start_at).total_seconds() // 60),
                        or_(
                            WaitlistRequest.preferred_master_id.is_(None),
                            WaitlistRequest.preferred_master_id == master.id,
                        ),
                        func.coalesce(
                            WaitlistRequest.acceptable_date_from,
                            WaitlistRequest.desired_date,
                        )
                        <= start_at.astimezone(KYIV_TZ).date(),
                        func.coalesce(
                            WaitlistRequest.acceptable_date_to,
                            WaitlistRequest.desired_date,
                        )
                        >= start_at.astimezone(KYIV_TZ).date(),
                    )
                    .order_by(WaitlistRequest.created_at.asc(), WaitlistRequest.id.asc())
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        eligible: list[tuple[WaitlistRequest, list[BarberService]]] = []
        frequency_cutoff = now - timedelta(minutes=settings.waitlist_offer_frequency_minutes)
        for request in rows:
            if not request.notification_consent or request.preferred_master_id not in (None, master.id):
                continue
            if request.duration_minutes != int((end_at - start_at).total_seconds() // 60):
                continue
            if not self._in_date_range(request, start_at) or not self._in_time_preference(
                request,
                start_at,
                end_at,
            ):
                continue
            matched_services = self._matching_services(master, list(request.services))
            if not matched_services:
                continue
            preference = (
                await session.execute(
                    select(ClientCommunicationPreference).where(
                        ClientCommunicationPreference.customer_id == request.customer_id
                    )
                )
            ).scalar_one_or_none()
            if not self._communication_allowed(request, preference):
                continue
            conflicting = (
                await session.execute(
                    select(Booking.id)
                    .where(
                        Booking.customer_id == request.customer_id,
                        Booking.status == BookingStatus.confirmed,
                        Booking.start_at < end_at,
                        Booking.end_at > start_at,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conflicting is not None:
                continue
            previously_offered = (
                await session.execute(
                    select(WaitlistOffer.id)
                    .where(
                        WaitlistOffer.request_id == request.id,
                        WaitlistOffer.master_id == master.id,
                        WaitlistOffer.start_at == start_at,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if previously_offered is not None:
                continue
            if settings.waitlist_offer_frequency_minutes:
                recent_offer = (
                    await session.execute(
                        select(WaitlistOffer.id)
                        .join(WaitlistRequest, WaitlistRequest.id == WaitlistOffer.request_id)
                        .where(
                            WaitlistRequest.customer_id == request.customer_id,
                            WaitlistOffer.sent_at >= frequency_cutoff,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if recent_offer is not None:
                    continue
            eligible.append((request, matched_services))
        day = start_at.astimezone(KYIV_TZ).date()
        return sorted(
            eligible,
            key=lambda item: (
                item[0].preferred_master_id != master.id,
                item[0].desired_date != day,
                not self._in_time_preference(item[0], start_at),
                item[0].created_at,
                item[0].id,
            ),
        )

    async def _send_offer(
        self,
        session: AsyncSession,
        offer: WaitlistOffer,
        token: str,
        *,
        master_name: str,
        booking_link_base: str | None = None,
    ) -> bool:
        request = (
            await session.execute(
                select(WaitlistRequest)
                .options(selectinload(WaitlistRequest.customer))
                .where(WaitlistRequest.id == offer.request_id)
            )
        ).scalar_one_or_none()
        if request is None:
            return False
        customer = request.customer
        booking_link = self._booking_link(token, booking_link_base)
        body = settings.waitlist_offer_sms_template.format(
            master_name=master_name,
            appointment_date=offer.start_at.astimezone(KYIV_TZ).strftime("%d.%m.%Y"),
            appointment_time=offer.start_at.astimezone(KYIV_TZ).strftime("%H:%M"),
            hold_minutes=self.hold_minutes,
            booking_link=booking_link,
        )
        now = self._now()
        try:
            result = await self.sms_service.send_message(
                customer.phone,
                body,
                lifetime_minutes=self.hold_minutes,
                sensitive=True,
            )
        except Exception:
            offer.status = WaitlistOfferStatus.cancelled
            offer.closed_at = now
            offer.close_reason = "sms_send_failed"
            request.status = WaitlistStatus.active
            request.offered_at = None
            await session.commit()
            logger.exception("Waitlist offer SMS failed", extra={"offer_id": offer.id})
            return False
        offer.status = WaitlistOfferStatus.sent
        offer.sent_at = now
        offer.expires_at = now + timedelta(minutes=self.hold_minutes)
        offer.provider_message_id = result.provider_message_id
        request.status = WaitlistStatus.offered
        request.offered_at = now
        await booking_recovery_analytics_service.record(
            session,
            event_type=BookingRecoveryEventType.waitlist_offer_sent,
            event_key=f"waitlist-offer-sent:{offer.id}",
            master_id=offer.master_id,
            waitlist_request_id=offer.request_id,
            waitlist_offer_id=offer.id,
            source_booking_id=offer.source_booking_id,
            occurred_at=now,
        )
        # The stub provider is synchronous. Real SMS delivery is reconciled by
        # sync_delivery_statuses using the provider message ID.
        if result.provider_message_id is None:
            offer.status = WaitlistOfferStatus.delivered
            offer.delivered_at = now
            await booking_recovery_analytics_service.record(
                session,
                event_type=BookingRecoveryEventType.waitlist_offer_delivered,
                event_key=f"waitlist-offer-delivered:{offer.id}",
                master_id=offer.master_id,
                waitlist_request_id=offer.request_id,
                waitlist_offer_id=offer.id,
                source_booking_id=offer.source_booking_id,
                occurred_at=now,
            )
        await session.commit()
        return True

    async def offer_slot(
        self,
        session: AsyncSession,
        *,
        master_id: int,
        start_at: datetime,
        end_at: datetime,
        source_booking_id: int | None = None,
        booking_link_base: str | None = None,
    ) -> WaitlistOffer | None:
        """Select exactly one candidate under the same master lock used by booking."""
        start_at = self.booking_service.normalize_datetime(start_at)
        end_at = self.booking_service.normalize_datetime(end_at)
        if start_at <= self._now() + timedelta(minutes=self.hold_minutes):
            return None
        master = await self.booking_service.get_active_master_with_services(
            session,
            master_id,
            for_update=True,
        )
        await self.booking_service.ensure_booking_within_availability(session, master.id, start_at, end_at)
        await self.booking_service.ensure_slot_available(session, master.id, start_at, end_at)
        held = (
            await session.execute(
                select(WaitlistOffer.id)
                .where(
                    WaitlistOffer.master_id == master.id,
                    WaitlistOffer.start_at == start_at,
                    WaitlistOffer.end_at == end_at,
                    WaitlistOffer.status.in_(OPEN_OFFER_STATUSES),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if held is not None:
            await session.commit()
            return None
        candidates = await self._eligible_requests(session, master, start_at, end_at)
        if not candidates:
            await session.commit()
            return None
        request, _matched_services = candidates[0]
        token = secrets.token_urlsafe(32)
        now = self._now()
        scheduled_at = self._scheduled_send_at(now)
        offer = WaitlistOffer(
            request_id=request.id,
            master_id=master.id,
            start_at=start_at,
            end_at=end_at,
            token_hash=self._hash(token),
            status=WaitlistOfferStatus.pending,
            scheduled_at=scheduled_at,
            expires_at=scheduled_at + timedelta(minutes=self.hold_minutes),
            source_booking_id=source_booking_id,
        )
        session.add(offer)
        request.status = WaitlistStatus.offered
        request.offered_at = now
        await session.flush()
        if scheduled_at > now:
            await session.commit()
            return offer
        sent = await self._send_offer(
            session,
            offer,
            token,
            master_name=master.full_name_uk,
            booking_link_base=booking_link_base,
        )
        if sent:
            return offer
        return await self.offer_slot(
            session,
            master_id=master_id,
            start_at=start_at,
            end_at=end_at,
            source_booking_id=source_booking_id,
            booking_link_base=booking_link_base,
        )

    async def send_due_offers(self, session: AsyncSession, *, now: datetime | None = None) -> int:
        now = now or self._now()
        offers = list(
            (
                await session.execute(
                    select(WaitlistOffer)
                    .options(selectinload(WaitlistOffer.master))
                    .where(
                        WaitlistOffer.status == WaitlistOfferStatus.pending,
                        WaitlistOffer.scheduled_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        sent = 0
        for offer in offers:
            master = await self.booking_service.get_active_master_with_services(
                session,
                offer.master_id,
                for_update=True,
            )
            try:
                if offer.start_at <= now + timedelta(minutes=self.hold_minutes):
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slot is too close for the offer hold")
                await self.booking_service.ensure_booking_within_availability(
                    session,
                    master.id,
                    offer.start_at,
                    offer.end_at,
                )
                await self.booking_service.ensure_slot_available(
                    session,
                    master.id,
                    offer.start_at,
                    offer.end_at,
                    exclude_waitlist_offer_id=offer.id,
                )
            except HTTPException:
                offer.status = WaitlistOfferStatus.cancelled
                offer.closed_at = now
                offer.close_reason = "slot_no_longer_available"
                request = await session.get(WaitlistRequest, offer.request_id)
                if request and request.status == WaitlistStatus.offered:
                    request.status = WaitlistStatus.active
                    request.offered_at = None
                await session.commit()
                continue
            # The plaintext token is intentionally never persisted, so a delayed
            # offer needs a fresh token at send time.
            token = secrets.token_urlsafe(32)
            offer.token_hash = self._hash(token)
            delivered_to_provider = await self._send_offer(
                session,
                offer,
                token,
                master_name=master.full_name_uk,
            )
            if delivered_to_provider:
                sent += 1
            else:
                await self.offer_slot(
                    session,
                    master_id=offer.master_id,
                    start_at=offer.start_at,
                    end_at=offer.end_at,
                    source_booking_id=offer.source_booking_id,
                )
        return sent

    async def sync_delivery_statuses(self, session: AsyncSession) -> int:
        offers = list(
            (
                await session.execute(
                    select(WaitlistOffer).where(
                        WaitlistOffer.status == WaitlistOfferStatus.sent,
                        WaitlistOffer.provider_message_id.is_not(None),
                    )
                )
            ).scalars()
        )
        if not offers:
            return 0
        statuses = await self.sms_service.get_message_statuses(
            [str(item.provider_message_id) for item in offers]
        )
        updated = 0
        retry_slots: list[FreedBookingSlot] = []
        now = self._now()
        for offer in offers:
            delivery = statuses.get(str(offer.provider_message_id))
            if delivery == SmsDeliveryStatus.delivered:
                offer.status = WaitlistOfferStatus.delivered
                offer.delivered_at = now
                await booking_recovery_analytics_service.record(
                    session,
                    event_type=BookingRecoveryEventType.waitlist_offer_delivered,
                    event_key=f"waitlist-offer-delivered:{offer.id}",
                    master_id=offer.master_id,
                    waitlist_request_id=offer.request_id,
                    waitlist_offer_id=offer.id,
                    source_booking_id=offer.source_booking_id,
                    occurred_at=now,
                )
                updated += 1
            elif delivery in (
                SmsDeliveryStatus.expired,
                SmsDeliveryStatus.undeliverable,
                SmsDeliveryStatus.rejected,
            ):
                offer.status = WaitlistOfferStatus.cancelled
                offer.closed_at = now
                offer.close_reason = f"sms_{delivery.value.lower()}"
                request = await session.get(WaitlistRequest, offer.request_id)
                if request and request.status == WaitlistStatus.offered:
                    request.status = WaitlistStatus.active
                    request.offered_at = None
                retry_slots.append(
                    FreedBookingSlot(
                        master_id=offer.master_id,
                        start_at=offer.start_at,
                        end_at=offer.end_at,
                        source_booking_id=offer.source_booking_id,
                    )
                )
                await booking_recovery_analytics_service.record(
                    session,
                    event_type=BookingRecoveryEventType.waitlist_offer_expired,
                    event_key=f"waitlist-offer-delivery-failed:{offer.id}",
                    master_id=offer.master_id,
                    waitlist_request_id=offer.request_id,
                    waitlist_offer_id=offer.id,
                    source_booking_id=offer.source_booking_id,
                    occurred_at=now,
                )
                updated += 1
        if updated:
            await session.commit()
        for slot in retry_slots:
            await self.offer_slot(
                session,
                master_id=slot.master_id,
                start_at=slot.start_at,
                end_at=slot.end_at,
                source_booking_id=slot.source_booking_id,
            )
        return updated

    async def expire_and_offer_next(self, session: AsyncSession) -> int:
        expired = await self.expire_holds(session)
        if not expired:
            return 0
        slots = [
            FreedBookingSlot(
                master_id=item.master_id,
                start_at=item.start_at,
                end_at=item.end_at,
                source_booking_id=item.source_booking_id,
            )
            for item in expired
        ]
        await session.commit()
        offered = 0
        for slot in slots:
            result = await self.offer_slot(
                session,
                master_id=slot.master_id,
                start_at=slot.start_at,
                end_at=slot.end_at,
                source_booking_id=slot.source_booking_id,
            )
            offered += int(result is not None)
        return offered

    async def claim(self, session: AsyncSession, token: str) -> Booking:
        offer = (
            await session.execute(
                select(WaitlistOffer)
                .options(
                    selectinload(WaitlistOffer.request).selectinload(WaitlistRequest.services),
                    selectinload(WaitlistOffer.request).selectinload(WaitlistRequest.customer),
                )
                .where(WaitlistOffer.token_hash == self._hash(token))
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = self._now()
        if (
            not offer
            or offer.status not in ACTIVE_HOLD_STATUSES
            or offer.expires_at <= now
            or offer.start_at <= now
        ):
            if offer and offer.status in ACTIVE_HOLD_STATUSES:
                offer.status = WaitlistOfferStatus.expired
                offer.closed_at = now
                offer.close_reason = "claim_after_expiry"
                request = offer.request
                if request.status == WaitlistStatus.offered:
                    request.status = WaitlistStatus.active
                    request.offered_at = None
                await session.commit()
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Offer is no longer available")

        request = offer.request
        master = await self.booking_service.get_active_master_with_services(
            session,
            offer.master_id,
            for_update=True,
        )
        matched_services = self._matching_services(master, list(request.services))
        if not matched_services:
            offer.status = WaitlistOfferStatus.cancelled
            offer.closed_at = now
            offer.close_reason = "services_no_longer_supported"
            request.status = WaitlistStatus.active
            request.offered_at = None
            await session.commit()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Services are no longer available")
        try:
            await self.booking_service.ensure_booking_within_availability(
                session,
                master.id,
                offer.start_at,
                offer.end_at,
            )
            await self.booking_service.ensure_slot_available(
                session,
                master.id,
                offer.start_at,
                offer.end_at,
                exclude_waitlist_offer_id=offer.id,
            )
        except HTTPException as exc:
            offer.status = WaitlistOfferStatus.cancelled
            offer.closed_at = now
            offer.close_reason = "slot_no_longer_available_at_claim"
            request.status = WaitlistStatus.active
            request.offered_at = None
            await session.commit()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slot is no longer available") from exc

        customer = request.customer
        booking = Booking(
            master_id=master.id,
            service_id=matched_services[0].id,
            customer_id=customer.id,
            customer_name=" ".join(part for part in (customer.name, customer.surname) if part),
            customer_phone=customer.phone,
            customer_email=customer.email,
            start_at=offer.start_at,
            end_at=offer.end_at,
            status=BookingStatus.confirmed,
            service_items=[
                BookingServiceItem(
                    service_id=item.id,
                    position=index,
                    price_amount=int(item.price),
                )
                for index, item in enumerate(matched_services)
            ],
        )
        await self.booking_service.promotion_service.apply_to_booking(
            session,
            booking=booking,
            promotion_code=None,
            customer=customer,
            services=matched_services,
            at=booking.start_at,
        )
        session.add(booking)
        await session.flush()
        offer.status = WaitlistOfferStatus.claimed
        offer.claimed_at = now
        offer.closed_at = now
        offer.close_reason = "claimed"
        request.status = WaitlistStatus.booked
        request.booked_at = now
        request.closed_at = now
        request.close_reason = "offer_claimed"
        await booking_recovery_analytics_service.record(
            session,
            event_type=BookingRecoveryEventType.waitlist_offer_claimed,
            event_key=f"waitlist-offer-claimed:{offer.id}",
            master_id=offer.master_id,
            service_id=matched_services[0].id,
            booking_id=booking.id,
            waitlist_request_id=request.id,
            waitlist_offer_id=offer.id,
            source_booking_id=offer.source_booking_id,
            occurred_at=now,
        )
        source_booking = (
            await session.get(Booking, offer.source_booking_id)
            if offer.source_booking_id is not None
            else None
        )
        latency_seconds = None
        if source_booking and source_booking.cancelled_at:
            latency_seconds = max(0, int((now - source_booking.cancelled_at).total_seconds()))
        await booking_recovery_analytics_service.record(
            session,
            event_type=BookingRecoveryEventType.booking_completed_after_waitlist_offer,
            event_key=f"waitlist-booking-completed:{offer.id}",
            master_id=offer.master_id,
            service_id=matched_services[0].id,
            booking_id=booking.id,
            waitlist_request_id=request.id,
            waitlist_offer_id=offer.id,
            source_booking_id=offer.source_booking_id,
            metric_value=latency_seconds,
            occurred_at=now,
        )
        await session.commit()
        await session.refresh(booking)
        return booking


waitlist_offer_service = WaitlistOfferService()


async def offer_freed_booking_slot(slot: FreedBookingSlot) -> None:
    """Best-effort post-commit hook used by cancellation/reschedule routes."""
    try:
        async with AsyncSessionLocal() as session:
            await waitlist_offer_service.offer_slot(
                session,
                master_id=slot.master_id,
                start_at=slot.start_at,
                end_at=slot.end_at,
                source_booking_id=slot.source_booking_id,
            )
    except Exception:
        logger.exception(
            "Failed to process a freed booking slot",
            extra={"master_id": slot.master_id, "source_booking_id": slot.source_booking_id},
        )


async def run_waitlist_offer_scheduler() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as session:
                from app.services.waitlist import WaitlistService

                await WaitlistService().expire_due_requests(session)
                await waitlist_offer_service.send_due_offers(session)
                await waitlist_offer_service.sync_delivery_statuses(session)
                await waitlist_offer_service.expire_and_offer_next(session)
        except Exception:
            logger.exception("Waitlist offer scheduler iteration failed")
        await asyncio.sleep(settings.waitlist_offer_scheduler_interval_seconds)
