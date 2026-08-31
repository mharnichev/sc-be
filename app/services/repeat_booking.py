from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.messaging import ClientCommunicationPreference, MessagePurpose
from app.models.repeat_booking import (
    RepeatBookingEvent,
    RepeatBookingEventType,
    RepeatBookingOffer,
    RepeatBookingOfferStatus,
)
from app.schemas.repeat_booking import (
    RepeatBookingAnalyticsSummary,
    RepeatBookingContext,
    RepeatBookingMasterContext,
    RepeatBookingServiceContext,
)
from app.services.messaging import KYIV_TZ, MessagingService, TelegramMessageProvider


logger = logging.getLogger(__name__)
_SCHEDULER_LOCK_ID = 1_397_966_936
_OPEN_STATES = {
    RepeatBookingOfferStatus.sent,
    RepeatBookingOfferStatus.opened,
    RepeatBookingOfferStatus.started,
}


class RepeatBookingService:
    def __init__(
        self,
        provider: TelegramMessageProvider | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider or TelegramMessageProvider()
        self.messaging = MessagingService()
        self._clock = now or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return self._clock()

    @staticmethod
    def hash_token(token: str) -> str:
        return hmac.new(settings.secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def link_for_token(token: str) -> str:
        return f"{settings.public_site_url.rstrip('/')}{settings.repeat_booking_public_path}#{token}"

    @staticmethod
    def cadence_days(service_ids: list[int]) -> int:
        overrides = settings.repeat_booking_service_delay_days
        return max((overrides.get(item, settings.repeat_booking_delay_days) for item in service_ids), default=settings.repeat_booking_delay_days)

    @classmethod
    def scheduled_time(cls, completed_at: datetime, service_ids: list[int]) -> datetime:
        due = completed_at + timedelta(days=cls.cadence_days(service_ids))
        return MessagingService.adjust_for_quiet_hours(
            due,
            quiet_from=settings.repeat_booking_quiet_hours_from,
            quiet_to=settings.repeat_booking_quiet_hours_to,
        )

    @staticmethod
    def _event_key(event_type: RepeatBookingEventType, offer_id: int, suffix: str = "once") -> str:
        raw = f"repeat-booking:{event_type.value}:{offer_id}:{suffix}"
        return hmac.new(settings.secret_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()

    async def record_event(
        self,
        session: AsyncSession,
        offer: RepeatBookingOffer,
        event_type: RepeatBookingEventType,
        *,
        reason_code: str | None = None,
        suffix: str = "once",
    ) -> bool:
        event = RepeatBookingEvent(
            offer_id=offer.id,
            event_key_hash=self._event_key(event_type, offer.id, suffix),
            event_type=event_type.value,
            reason_code=reason_code,
            metadata_json={},
        )
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            return False
        return True

    @staticmethod
    def _booking_load_options():
        items = selectinload(Booking.service_items).selectinload(BookingServiceItem.service).selectinload(
            BarberService.base_service
        )
        return (
            selectinload(Booking.customer),
            selectinload(Booking.master).selectinload(Master.services).selectinload(BarberService.base_service),
            selectinload(Booking.redirected_from_master).selectinload(Master.services).selectinload(
                BarberService.base_service
            ),
            selectinload(Booking.service).selectinload(BarberService.base_service),
            items,
        )

    @staticmethod
    def snapshot_service_ids(booking: Booking) -> list[int]:
        """Keep the exact combination, mapping redirected calendars back to the public barber."""
        preferred = booking.redirected_from_master or booking.master
        if preferred is None:
            return list(booking.service_ids)
        available = list(getattr(preferred, "services", []) or [])
        result: list[int] = []
        for source in booking.services:
            if source.master_id == preferred.id:
                result.append(source.id)
                continue
            match = next(
                (
                    item
                    for item in available
                    if source.base_service_id is not None and item.base_service_id == source.base_service_id
                ),
                None,
            )
            result.append(match.id if match is not None else source.id)
        return result or list(booking.service_ids)

    async def _preference(
        self, session: AsyncSession, customer_id: int
    ) -> ClientCommunicationPreference | None:
        return (
            await session.execute(
                select(ClientCommunicationPreference).where(
                    ClientCommunicationPreference.customer_id == customer_id
                )
            )
        ).scalar_one_or_none()

    async def _has_newer_active_booking(
        self,
        session: AsyncSession,
        *,
        customer_id: int,
        source_booking_id: int,
        completed_at: datetime,
    ) -> bool:
        """Treat a started-but-not-completed visit as newer than the source visit."""
        return (
            await session.execute(
                select(Booking.id).where(
                    Booking.customer_id == customer_id,
                    Booking.id != source_booking_id,
                    Booking.status.in_((BookingStatus.pending, BookingStatus.confirmed)),
                    Booking.start_at > completed_at,
                ).limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _has_newer_completion(
        self,
        session: AsyncSession,
        *,
        customer_id: int,
        source_booking_id: int,
        completed_at: datetime,
    ) -> bool:
        return (
            await session.execute(
                select(Booking.id).where(
                    Booking.customer_id == customer_id,
                    Booking.status == BookingStatus.completed,
                    Booking.id != source_booking_id,
                    Booking.completed_at.is_not(None),
                    Booking.completed_at > completed_at,
                ).limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _frequency_capped(
        self, session: AsyncSession, customer_id: int, at: datetime
    ) -> bool:
        if not settings.repeat_booking_frequency_cap_days:
            return False
        return (
            await session.execute(
                select(RepeatBookingOffer.id).where(
                    RepeatBookingOffer.customer_id == customer_id,
                    RepeatBookingOffer.sent_at
                    >= at - timedelta(days=settings.repeat_booking_frequency_cap_days),
                ).limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _active_context(
        self,
        session: AsyncSession,
        master_id: int | None,
        service_ids: list[int],
    ) -> tuple[Master | None, list[BarberService], bool]:
        master = await session.get(Master, master_id) if master_id is not None else None
        services = list(
            (
                await session.execute(
                    select(BarberService)
                    .options(selectinload(BarberService.base_service))
                    .where(BarberService.id.in_(service_ids))
                )
            ).scalars()
        ) if service_ids else []
        by_id = {item.id: item for item in services}
        ordered = [by_id[item] for item in service_ids if item in by_id]
        master_active = bool(master and master.is_active and master.show_on_master_block)
        services_active = len(ordered) == len(service_ids) and all(
            item.master_id == master_id
            and item.is_active
            and (item.base_service_id is None or (item.base_service is not None and item.base_service.is_active))
            for item in ordered
        )
        return master, ordered, master_active and services_active

    async def eligibility_reason(
        self,
        session: AsyncSession,
        booking: Booking,
        *,
        service_ids: list[int],
        at: datetime,
        check_frequency: bool = True,
    ) -> str | None:
        if booking.status != BookingStatus.completed or booking.customer_id is None or booking.completed_at is None:
            return "not_completed"
        preference = await self._preference(session, booking.customer_id)
        allowed, _ = self.messaging.communication_allowed(preference, MessagePurpose.marketing)
        if not allowed or (preference is not None and getattr(preference, "repeat_booking_opt_out", False)):
            return "opted_out"
        if preference is None or not preference.telegram_chat_id:
            return "telegram_not_connected"
        if await self._has_newer_active_booking(
            session,
            customer_id=booking.customer_id,
            source_booking_id=booking.id,
            completed_at=booking.completed_at,
        ):
            return "future_booking_exists"
        if await self._has_newer_completion(
            session,
            customer_id=booking.customer_id,
            source_booking_id=booking.id,
            completed_at=booking.completed_at,
        ):
            return "newer_completed_booking"
        _, _, active = await self._active_context(session, booking.public_master_id, service_ids)
        if not active:
            return "booking_context_inactive"
        if check_frequency and await self._frequency_capped(session, booking.customer_id, at):
            return "frequency_cap"
        return None

    async def schedule_booking(
        self,
        session: AsyncSession,
        booking: Booking,
        *,
        at: datetime | None = None,
    ) -> RepeatBookingOffer:
        existing = (
            await session.execute(
                select(RepeatBookingOffer).where(RepeatBookingOffer.completed_booking_id == booking.id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        now = at or self.now()
        service_ids = self.snapshot_service_ids(booking)
        scheduled_at = self.scheduled_time(booking.completed_at or now, service_ids)
        reason = await self.eligibility_reason(
            session, booking, service_ids=service_ids, at=now
        )
        offer = RepeatBookingOffer(
            completed_booking_id=booking.id,
            customer_id=booking.customer_id,
            preferred_master_id=booking.public_master_id,
            service_ids=service_ids,
            status=RepeatBookingOfferStatus.skipped if reason else RepeatBookingOfferStatus.scheduled,
            scheduled_at=scheduled_at,
            skip_reason=reason,
        )
        session.add(offer)
        await session.flush()
        await self.record_event(
            session,
            offer,
            RepeatBookingEventType.offer_skipped if reason else RepeatBookingEventType.offer_scheduled,
            reason_code=reason,
        )
        return offer

    async def discover_due(self, session: AsyncSession, *, at: datetime | None = None) -> int:
        now = at or self.now()
        earliest_days = min(
            [settings.repeat_booking_delay_days, *settings.repeat_booking_service_delay_days.values()]
        )
        rows = list(
            (
                await session.execute(
                    select(Booking)
                    .outerjoin(RepeatBookingOffer, RepeatBookingOffer.completed_booking_id == Booking.id)
                    .options(*self._booking_load_options())
                    .where(
                        Booking.status == BookingStatus.completed,
                        Booking.customer_id.is_not(None),
                        Booking.completed_at.is_not(None),
                        Booking.completed_at <= now - timedelta(days=earliest_days),
                        RepeatBookingOffer.id.is_(None),
                    )
                    .order_by(Booking.completed_at.asc(), Booking.id.asc())
                    .limit(settings.messaging_batch_size)
                )
            ).scalars()
        )
        created = 0
        for booking in rows:
            service_ids = self.snapshot_service_ids(booking)
            if self.scheduled_time(booking.completed_at, service_ids) > now:
                continue
            try:
                async with session.begin_nested():
                    await self.schedule_booking(session, booking, at=now)
                    created += 1
            except IntegrityError:
                continue
        return created

    async def _skip(
        self, session: AsyncSession, offer: RepeatBookingOffer, reason: str
    ) -> None:
        offer.status = RepeatBookingOfferStatus.skipped
        offer.skip_reason = reason
        offer.token_hash = None
        offer.revoked_at = self.now()
        await self.record_event(session, offer, RepeatBookingEventType.offer_skipped, reason_code=reason)

    async def revoke_superseded_offers(
        self,
        session: AsyncSession,
        booking: Booking,
    ) -> int:
        """Revoke older offers after the customer completes a newer visit."""
        if (
            booking.status != BookingStatus.completed
            or booking.customer_id is None
            or booking.completed_at is None
        ):
            return 0
        offers = (
            await session.execute(
                select(RepeatBookingOffer)
                .join(Booking, RepeatBookingOffer.completed_booking_id == Booking.id)
                .where(
                    RepeatBookingOffer.customer_id == booking.customer_id,
                    RepeatBookingOffer.completed_booking_id != booking.id,
                    RepeatBookingOffer.status.in_(
                        (RepeatBookingOfferStatus.scheduled, *_OPEN_STATES)
                    ),
                    Booking.completed_at.is_not(None),
                    Booking.completed_at < booking.completed_at,
                )
            )
        ).scalars().all()
        for offer in offers:
            await self._skip(session, offer, "newer_completed_booking")
        return len(offers)

    async def send_offer(
        self, session: AsyncSession, offer: RepeatBookingOffer, *, at: datetime | None = None
    ) -> bool:
        now = at or self.now()
        if offer.status != RepeatBookingOfferStatus.scheduled or offer.scheduled_at > now:
            return False
        adjusted_now = MessagingService.adjust_for_quiet_hours(
            now,
            quiet_from=settings.repeat_booking_quiet_hours_from,
            quiet_to=settings.repeat_booking_quiet_hours_to,
        )
        if adjusted_now > now:
            offer.scheduled_at = adjusted_now
            offer.next_retry_at = None
            return False
        booking = (
            await session.execute(
                select(Booking).options(*self._booking_load_options()).where(Booking.id == offer.completed_booking_id)
            )
        ).scalar_one_or_none()
        if booking is None:
            await self._skip(session, offer, "source_booking_missing")
            return False
        reason = await self.eligibility_reason(
            session, booking, service_ids=list(offer.service_ids), at=now
        )
        if reason:
            await self._skip(session, offer, reason)
            return False
        preference = await self._preference(session, offer.customer_id)
        master, _, active = await self._active_context(
            session, offer.preferred_master_id, list(offer.service_ids)
        )
        if preference is None or not preference.telegram_chat_id or master is None or not active:
            await self._skip(session, offer, "booking_context_inactive")
            return False

        token = self.new_token()
        offer.token_hash = self.hash_token(token)
        offer.expires_at = now + timedelta(days=settings.repeat_booking_token_ttl_days)
        offer.revoked_at = None
        offer.delivery_attempts = (offer.delivery_attempts or 0) + 1
        link = self.link_for_token(token)
        customer_name = ((booking.customer.name if booking.customer else None) or booking.customer_name).strip()
        body = settings.repeat_booking_telegram_template.format(
            customer_name=customer_name,
            master_name=master.full_name_uk,
            repeat_booking_link=link,
        )
        markup = {"inline_keyboard": [[{"text": "Записатися знову", "url": link}]]}
        try:
            result = await self.provider.send_message(
                destination=preference.telegram_chat_id,
                body=body,
                reply_markup=markup,
            )
        except Exception as exc:  # pragma: no cover - provider failures vary
            offer.token_hash = None
            offer.failure_reason = str(exc)
            if offer.delivery_attempts >= settings.messaging_max_retry_attempts:
                offer.status = RepeatBookingOfferStatus.failed
            else:
                offer.next_retry_at = now + timedelta(minutes=settings.messaging_retry_delay_minutes)
            await self.record_event(
                session,
                offer,
                RepeatBookingEventType.offer_delivery_failed,
                reason_code="telegram_delivery_failed",
                suffix=str(offer.delivery_attempts),
            )
            logger.warning("Repeat booking Telegram delivery failed", extra={"offer_id": offer.id})
            return False

        offer.status = RepeatBookingOfferStatus.sent
        offer.sent_at = now
        offer.next_retry_at = None
        offer.failure_reason = None
        offer.provider_message_id = result.provider_message_id
        await self.record_event(session, offer, RepeatBookingEventType.offer_sent)
        return True

    async def expire_due(self, session: AsyncSession, *, at: datetime | None = None) -> int:
        now = at or self.now()
        offers = list(
            (
                await session.execute(
                    select(RepeatBookingOffer).where(
                        RepeatBookingOffer.status.in_(_OPEN_STATES),
                        RepeatBookingOffer.expires_at <= now,
                    )
                )
            ).scalars()
        )
        for offer in offers:
            offer.status = RepeatBookingOfferStatus.expired
            offer.token_hash = None
            offer.revoked_at = now
            await self.record_event(session, offer, RepeatBookingEventType.offer_expired)
        return len(offers)

    async def process_due(self, session: AsyncSession, *, at: datetime | None = None) -> int:
        now = at or self.now()
        await self.discover_due(session, at=now)
        await self.expire_due(session, at=now)
        offers = list(
            (
                await session.execute(
                    select(RepeatBookingOffer)
                    .where(
                        RepeatBookingOffer.status == RepeatBookingOfferStatus.scheduled,
                        RepeatBookingOffer.scheduled_at <= now,
                        or_(RepeatBookingOffer.next_retry_at.is_(None), RepeatBookingOffer.next_retry_at <= now),
                    )
                    .order_by(RepeatBookingOffer.scheduled_at.asc(), RepeatBookingOffer.id.asc())
                    .limit(settings.messaging_batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        processed = 0
        for offer in offers:
            await self.send_offer(session, offer, at=now)
            processed += 1
        await session.commit()
        return processed

    async def _valid_offer(
        self,
        session: AsyncSession,
        token: str,
        *,
        for_update: bool = False,
        persist_invalidations: bool = False,
    ) -> RepeatBookingOffer:
        if not 32 <= len(token) <= 512:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired repeat booking link")
        stmt = (
            select(RepeatBookingOffer)
            .options(selectinload(RepeatBookingOffer.completed_booking))
            .where(RepeatBookingOffer.token_hash == self.hash_token(token))
        )
        if for_update:
            stmt = stmt.with_for_update()
        offer = (await session.execute(stmt)).scalar_one_or_none()
        now = self.now()
        if offer is not None and offer.status in _OPEN_STATES and offer.expires_at is not None and offer.expires_at <= now:
            offer.status = RepeatBookingOfferStatus.expired
            offer.token_hash = None
            offer.revoked_at = now
            await self.record_event(session, offer, RepeatBookingEventType.offer_expired)
            if persist_invalidations:
                await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired repeat booking link")
        if (
            offer is None
            or offer.status not in _OPEN_STATES
            or offer.revoked_at is not None
            or offer.expires_at is None
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired repeat booking link")
        source = offer.completed_booking
        if source is None or source.completed_at is None:
            await self._skip(session, offer, "source_booking_missing")
            if persist_invalidations:
                await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired repeat booking link")
        if await self._has_newer_active_booking(
            session,
            customer_id=offer.customer_id,
            source_booking_id=offer.completed_booking_id,
            completed_at=source.completed_at,
        ):
            await self._skip(session, offer, "future_booking_exists")
            if persist_invalidations:
                await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired repeat booking link")
        if await self._has_newer_completion(
            session,
            customer_id=offer.customer_id,
            source_booking_id=offer.completed_booking_id,
            completed_at=source.completed_at,
        ):
            await self._skip(session, offer, "newer_completed_booking")
            if persist_invalidations:
                await session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired repeat booking link")
        return offer

    async def _context_for_offer(
        self,
        session: AsyncSession,
        offer: RepeatBookingOffer,
    ) -> RepeatBookingContext:
        master, services, active = await self._active_context(
            session, offer.preferred_master_id, list(offer.service_ids)
        )
        by_id = {item.id: item for item in services}
        return RepeatBookingContext(
            preferred_master=RepeatBookingMasterContext(
                id=master.id if master is not None else None,
                name=master.full_name_uk if master is not None else None,
                available=bool(master and master.is_active and master.show_on_master_block),
            ),
            services=[
                RepeatBookingServiceContext(
                    id=service_id,
                    name=(by_id[service_id].title_uk or by_id[service_id].name) if service_id in by_id else "",
                    available=bool(
                        service_id in by_id
                        and by_id[service_id].is_active
                        and by_id[service_id].master_id == offer.preferred_master_id
                        and (
                            by_id[service_id].base_service_id is None
                            or (
                                by_id[service_id].base_service is not None
                                and by_id[service_id].base_service.is_active
                            )
                        )
                    ),
                )
                for service_id in offer.service_ids
            ],
            can_prefill=active,
            fallback_required=not active,
            expires_at=offer.expires_at,
        )

    async def context(self, session: AsyncSession, token: str, *, mark_opened: bool = True) -> RepeatBookingContext:
        offer = await self._valid_offer(
            session,
            token,
            for_update=mark_opened,
            persist_invalidations=True,
        )
        payload = await self._context_for_offer(session, offer)
        if mark_opened:
            if offer.opened_at is None:
                offer.opened_at = self.now()
                await self.record_event(session, offer, RepeatBookingEventType.link_opened)
            if offer.status == RepeatBookingOfferStatus.sent:
                offer.status = RepeatBookingOfferStatus.opened
            await session.commit()
        return payload

    async def mark_started(self, session: AsyncSession, token: str) -> RepeatBookingContext:
        offer = await self._valid_offer(
            session,
            token,
            for_update=True,
            persist_invalidations=True,
        )
        context = await self._context_for_offer(session, offer)
        if offer.opened_at is None:
            offer.opened_at = self.now()
            await self.record_event(session, offer, RepeatBookingEventType.link_opened)
        if offer.started_at is None:
            offer.started_at = self.now()
            await self.record_event(session, offer, RepeatBookingEventType.booking_started)
        offer.status = RepeatBookingOfferStatus.started
        await session.commit()
        return context

    async def attribute_booking(
        self,
        session: AsyncSession,
        *,
        token: str,
        booking: Booking,
        requested_master_id: int,
        requested_service_ids: list[int],
    ) -> RepeatBookingOffer:
        offer = await self._valid_offer(session, token, for_update=True)
        if booking.customer_id != offer.customer_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Repeat booking link belongs to another client")
        _, _, original_active = await self._active_context(
            session, offer.preferred_master_id, list(offer.service_ids)
        )
        if original_active and (
            requested_master_id != offer.preferred_master_id
            or set(requested_service_ids) != set(offer.service_ids)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Repeat booking selection no longer matches the offered context",
            )
        if offer.opened_at is None:
            offer.opened_at = self.now()
            await self.record_event(session, offer, RepeatBookingEventType.link_opened)
        if offer.started_at is None:
            offer.started_at = self.now()
            await self.record_event(session, offer, RepeatBookingEventType.booking_started)
        offer.status = RepeatBookingOfferStatus.booked
        offer.booked_at = self.now()
        offer.result_booking_id = booking.id
        offer.revoked_at = self.now()
        offer.token_hash = None
        return offer

    async def mark_repeat_visit_completed(self, session: AsyncSession, booking: Booking) -> bool:
        offer = (
            await session.execute(
                select(RepeatBookingOffer).where(RepeatBookingOffer.result_booking_id == booking.id)
            )
        ).scalar_one_or_none()
        if booking.status != BookingStatus.completed:
            return False
        recorded = bool(
            offer is not None
            and await self.record_event(session, offer, RepeatBookingEventType.booking_completed)
        )
        await self.revoke_superseded_offers(session, booking)
        return recorded

    async def analytics(
        self, session: AsyncSession, *, date_from: date, date_to: date
    ) -> RepeatBookingAnalyticsSummary:
        if date_from > date_to or (date_to - date_from).days > 366:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="date range must be ordered and must not exceed 366 days",
            )
        start = datetime.combine(date_from, time.min, tzinfo=KYIV_TZ)
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=KYIV_TZ)
        rows = (
            await session.execute(
                select(RepeatBookingEvent.event_type, func.count(RepeatBookingEvent.id))
                .where(RepeatBookingEvent.created_at >= start, RepeatBookingEvent.created_at < end)
                .group_by(RepeatBookingEvent.event_type)
            )
        ).all()
        counts = {str(kind): int(count) for kind, count in rows}
        skipped_rows = (
            await session.execute(
                select(RepeatBookingEvent.reason_code, func.count(RepeatBookingEvent.id))
                .where(
                    RepeatBookingEvent.event_type == RepeatBookingEventType.offer_skipped.value,
                    RepeatBookingEvent.created_at >= start,
                    RepeatBookingEvent.created_at < end,
                )
                .group_by(RepeatBookingEvent.reason_code)
            )
        ).all()

        def count(kind: RepeatBookingEventType) -> int:
            return counts.get(kind.value, 0)

        sent = count(RepeatBookingEventType.offer_sent)

        def percent(value: int) -> Decimal | None:
            if not sent:
                return None
            return (Decimal(value * 100) / Decimal(sent)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        opened = count(RepeatBookingEventType.link_opened)
        started = count(RepeatBookingEventType.booking_started)
        completed = count(RepeatBookingEventType.booking_completed)
        return RepeatBookingAnalyticsSummary(
            date_from=date_from,
            date_to=date_to,
            offers_sent=sent,
            links_opened=opened,
            bookings_started=started,
            completed_repeat_visits=completed,
            open_rate_percent=percent(opened),
            start_rate_percent=percent(started),
            completion_rate_percent=percent(completed),
            skipped_by_reason={str(reason or "unknown"): int(total) for reason, total in skipped_rows},
            delivery_failures=count(RepeatBookingEventType.offer_delivery_failed),
        )


repeat_booking_service = RepeatBookingService()


async def _try_scheduler_lock(session: AsyncSession) -> bool:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    return bool(
        (
            await session.execute(select(func.pg_try_advisory_xact_lock(_SCHEDULER_LOCK_ID)))
        ).scalar_one()
    )


async def run_repeat_booking_scheduler() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as session:
                if await _try_scheduler_lock(session):
                    await repeat_booking_service.process_due(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Repeat booking scheduler iteration failed")
        await asyncio.sleep(settings.repeat_booking_scheduler_interval_seconds)
