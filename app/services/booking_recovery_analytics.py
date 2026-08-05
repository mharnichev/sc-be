from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.booking_funnel import BookingFunnelEvent, BookingFunnelEventType
from app.models.booking_recovery import BookingRecoveryEvent, BookingRecoveryEventType
from app.schemas.booking_recovery import BookingRecoverySummary

KYIV_TZ = ZoneInfo("Europe/Kyiv")


class BookingRecoveryAnalyticsService:
    @staticmethod
    def hash_identifier(namespace: str, value: str) -> str:
        payload = f"{namespace}:{value}".encode("utf-8")
        return hmac.new(settings.secret_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    async def record(
        self,
        session: AsyncSession,
        *,
        event_type: BookingRecoveryEventType,
        event_key: str,
        anonymous_session_id: str | None = None,
        master_id: int | None = None,
        service_id: int | None = None,
        booking_id: int | None = None,
        waitlist_request_id: int | None = None,
        waitlist_offer_id: int | None = None,
        source_booking_id: int | None = None,
        metric_value: int | None = None,
        occurred_at: datetime | None = None,
        commit: bool = False,
    ) -> bool:
        event = BookingRecoveryEvent(
            event_key_hash=self.hash_identifier("booking_recovery_event", event_key),
            event_type=event_type.value,
            anonymous_session_hash=(
                self.hash_identifier("booking_recovery_session", anonymous_session_id)
                if anonymous_session_id
                else None
            ),
            master_id=master_id,
            service_id=service_id,
            booking_id=booking_id,
            waitlist_request_id=waitlist_request_id,
            waitlist_offer_id=waitlist_offer_id,
            source_booking_id=source_booking_id,
            metric_value=metric_value,
            occurred_at=occurred_at or datetime.now(KYIV_TZ),
        )
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            return False
        if commit:
            await session.commit()
        return True

    @staticmethod
    def period_bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
        if date_from > date_to:
            raise ValueError("date_from must not be after date_to")
        if (date_to - date_from).days > 366:
            raise ValueError("date range must not exceed 366 days")
        return (
            datetime.combine(date_from, time.min, tzinfo=KYIV_TZ),
            datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=KYIV_TZ),
        )

    async def summary(
        self,
        session: AsyncSession,
        *,
        date_from: date,
        date_to: date,
    ) -> BookingRecoverySummary:
        start, end = self.period_bounds(date_from, date_to)
        no_slot_sessions = int(
            (
                await session.execute(
                    select(func.count(distinct(BookingFunnelEvent.anonymous_session_hash))).where(
                        BookingFunnelEvent.event_type == BookingFunnelEventType.no_slot,
                        BookingFunnelEvent.occurred_at >= start,
                        BookingFunnelEvent.occurred_at < end,
                        BookingFunnelEvent.anonymous_session_hash.is_not(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        rows = (
            await session.execute(
                select(
                    BookingRecoveryEvent.event_type,
                    func.count(BookingRecoveryEvent.id),
                    func.coalesce(func.sum(BookingRecoveryEvent.metric_value), 0),
                )
                .where(
                    BookingRecoveryEvent.occurred_at >= start,
                    BookingRecoveryEvent.occurred_at < end,
                )
                .group_by(BookingRecoveryEvent.event_type)
            )
        ).all()
        refill_count, refill_average = (
            await session.execute(
                select(
                    func.count(BookingRecoveryEvent.id),
                    func.avg(BookingRecoveryEvent.metric_value),
                ).where(
                    BookingRecoveryEvent.event_type
                    == BookingRecoveryEventType.booking_completed_after_waitlist_offer.value,
                    BookingRecoveryEvent.metric_value.is_not(None),
                    BookingRecoveryEvent.occurred_at >= start,
                    BookingRecoveryEvent.occurred_at < end,
                )
            )
        ).one()
        counts = {str(event_type): int(count or 0) for event_type, count, _ in rows}
        sums = {str(event_type): int(value or 0) for event_type, _, value in rows}

        def count(event_type: BookingRecoveryEventType) -> int:
            return counts.get(event_type.value, 0)

        requested = count(BookingRecoveryEventType.alternatives_requested)
        recovered = count(BookingRecoveryEventType.booking_completed_after_alternative)
        recovery_rate = None
        if requested:
            recovery_rate = (
                Decimal(recovered * 100) / Decimal(requested)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return BookingRecoverySummary(
            date_from=date_from,
            date_to=date_to,
            no_slot_sessions=no_slot_sessions,
            alternatives_requested=requested,
            alternatives_returned=count(BookingRecoveryEventType.alternatives_returned),
            alternative_slots_returned=sums.get(BookingRecoveryEventType.alternatives_returned.value, 0),
            alternative_slots_selected=count(BookingRecoveryEventType.alternative_slot_selected),
            bookings_after_alternative=recovered,
            alternative_recovery_rate_percent=recovery_rate,
            waitlist_requests=count(BookingRecoveryEventType.waitlist_submitted),
            offers_sent=count(BookingRecoveryEventType.waitlist_offer_sent),
            offers_delivered=count(BookingRecoveryEventType.waitlist_offer_delivered),
            offers_claimed=count(BookingRecoveryEventType.waitlist_offer_claimed),
            offers_expired=count(BookingRecoveryEventType.waitlist_offer_expired),
            cancelled_slots_refilled=int(refill_count or 0),
            average_cancellation_to_refill_seconds=(
                int(refill_average) if refill_average is not None else None
            ),
        )


booking_recovery_analytics_service = BookingRecoveryAnalyticsService()
