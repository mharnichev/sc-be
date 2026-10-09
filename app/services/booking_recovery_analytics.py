from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import and_, distinct, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.models.booking_funnel import BookingFunnelEvent, BookingFunnelEventType
from app.models.booking_recovery import BookingRecoveryEvent, BookingRecoveryEventType
from app.schemas.booking_recovery import BookingRecoveryStrictConversion, BookingRecoverySummary

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
        search_context_id: str | None = None,
        offer_id: str | None = None,
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
            search_context_hash=(
                self.hash_identifier("booking_recovery_context", search_context_id)
                if search_context_id
                else None
            ),
            offer_hash=(
                self.hash_identifier("booking_recovery_offer", offer_id) if offer_id else None
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
        if date_to == date.max:
            raise ValueError("date_to must allow an exclusive next-day boundary")
        if (date_to - date_from).days + 1 > 366:
            raise ValueError("date range must not exceed 366 days")
        return (
            datetime.combine(date_from, time.min, tzinfo=KYIV_TZ),
            datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=KYIV_TZ),
        )

    async def _strict_conversion(
        self, session: AsyncSession, *, start: datetime, end: datetime,
    ) -> BookingRecoveryStrictConversion:
        observed_until = datetime.now(KYIV_TZ)
        request = aliased(BookingRecoveryEvent)
        view = aliased(BookingRecoveryEvent)
        selection = aliased(BookingRecoveryEvent)
        completion = aliased(BookingRecoveryEvent)
        view_conditions = (
            view.event_type == BookingRecoveryEventType.alternative_slot_viewed.value,
            view.anonymous_session_hash == request.anonymous_session_hash,
            view.search_context_hash == request.search_context_hash,
            view.offer_hash.is_not(None),
            view.occurred_at > request.occurred_at,
            view.occurred_at <= observed_until,
        )
        selection_conditions = (
            selection.event_type == BookingRecoveryEventType.alternative_slot_selected.value,
            selection.anonymous_session_hash == view.anonymous_session_hash,
            selection.search_context_hash == view.search_context_hash,
            selection.offer_hash == view.offer_hash,
            selection.occurred_at > view.occurred_at,
            selection.occurred_at <= observed_until,
        )
        completion_conditions = (
            completion.event_type == BookingRecoveryEventType.booking_completed_after_alternative.value,
            completion.anonymous_session_hash == selection.anonymous_session_hash,
            completion.search_context_hash == selection.search_context_hash,
            completion.offer_hash == selection.offer_hash,
            completion.occurred_at > selection.occurred_at,
            completion.occurred_at <= observed_until,
        )
        viewed = exists(select(1).select_from(view).where(*view_conditions).correlate(request))
        selected = exists(
            select(1).select_from(view).join(selection, and_(*selection_conditions))
            .where(*view_conditions).correlate(request)
        )
        completed = exists(
            select(1).select_from(view).join(selection, and_(*selection_conditions))
            .join(completion, and_(*completion_conditions))
            .where(*view_conditions).correlate(request)
        )
        row = (await session.execute(
            select(
                func.count(distinct(request.anonymous_session_hash)),
                func.count(distinct(request.anonymous_session_hash)).filter(viewed),
                func.count(distinct(request.anonymous_session_hash)).filter(selected),
                func.count(distinct(request.anonymous_session_hash)).filter(completed),
            ).where(
                request.event_type == BookingRecoveryEventType.alternatives_requested.value,
                request.anonymous_session_hash.is_not(None),
                request.search_context_hash.is_not(None),
                request.occurred_at >= start,
                request.occurred_at < end,
                request.occurred_at <= observed_until,
            )
        )).one()
        requested_count, viewed_count, selected_count, completed_count = (int(value or 0) for value in row)

        def percent(numerator: int, denominator: int) -> Decimal | None:
            if not denominator:
                return None
            return (Decimal(numerator * 100) / Decimal(denominator)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            )

        return BookingRecoveryStrictConversion(
            follow_up_observed_until=observed_until,
            requested_sessions=requested_count,
            viewed_sessions=viewed_count,
            selected_sessions=selected_count,
            completed_sessions=completed_count,
            selection_rate_percent=percent(selected_count, viewed_count),
            completion_rate_percent=percent(completed_count, requested_count),
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
        engagement_collection_started_at = (
            await session.execute(
                select(func.min(BookingRecoveryEvent.occurred_at)).where(
                    BookingRecoveryEvent.event_type.in_(
                        (
                            BookingRecoveryEventType.alternative_slot_viewed.value,
                            BookingRecoveryEventType.alternative_slot_selected.value,
                        )
                    ),
                    BookingRecoveryEvent.search_context_hash.is_not(None),
                )
            )
        ).scalar_one_or_none()
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
        viewed = count(BookingRecoveryEventType.alternative_slot_viewed)
        selected = count(BookingRecoveryEventType.alternative_slot_selected)
        recovery_rate = None
        if requested:
            recovery_rate = (
                Decimal(recovered * 100) / Decimal(requested)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        selection_rate = None
        if viewed:
            selection_rate = (
                Decimal(selected * 100) / Decimal(viewed)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        cohort_request = aliased(BookingRecoveryEvent)
        is_cohort_pair = exists(
            select(1).where(
                cohort_request.event_type == BookingRecoveryEventType.alternatives_requested.value,
                cohort_request.search_context_hash.is_not(None),
                cohort_request.anonymous_session_hash.is_not(None),
                cohort_request.occurred_at >= start,
                cohort_request.occurred_at < end,
                cohort_request.occurred_at <= BookingRecoveryEvent.occurred_at,
                cohort_request.search_context_hash == BookingRecoveryEvent.search_context_hash,
                cohort_request.anonymous_session_hash == BookingRecoveryEvent.anonymous_session_hash,
            )
        )
        viewed_event = aliased(BookingRecoveryEvent)
        has_view_for_pair = exists(
            select(1).where(
                viewed_event.event_type == BookingRecoveryEventType.alternative_slot_viewed.value,
                viewed_event.search_context_hash == BookingRecoveryEvent.search_context_hash,
                viewed_event.anonymous_session_hash == BookingRecoveryEvent.anonymous_session_hash,
            )
        )
        cohort_requested, cohort_viewed, cohort_selected, cohort_completed = (
            await session.execute(
                select(
                    func.count(distinct(BookingRecoveryEvent.anonymous_session_hash)).filter(
                        BookingRecoveryEvent.event_type
                        == BookingRecoveryEventType.alternatives_requested.value
                    ),
                    func.count(distinct(BookingRecoveryEvent.anonymous_session_hash)).filter(
                        BookingRecoveryEvent.event_type
                        == BookingRecoveryEventType.alternative_slot_viewed.value
                    ),
                    func.count(distinct(BookingRecoveryEvent.anonymous_session_hash)).filter(
                        and_(
                            BookingRecoveryEvent.event_type
                            == BookingRecoveryEventType.alternative_slot_selected.value,
                            has_view_for_pair,
                        )
                    ),
                    func.count(distinct(BookingRecoveryEvent.anonymous_session_hash)).filter(
                        BookingRecoveryEvent.event_type
                        == BookingRecoveryEventType.booking_completed_after_alternative.value
                    ),
                ).where(is_cohort_pair)
            )
        ).one()
        cohort_requested = int(cohort_requested or 0)
        cohort_viewed = int(cohort_viewed or 0)
        cohort_selected = int(cohort_selected or 0)
        cohort_completed = int(cohort_completed or 0)
        cohort_selection_rate = None
        if cohort_viewed:
            cohort_selection_rate = (
                Decimal(cohort_selected * 100) / Decimal(cohort_viewed)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cohort_completion_rate = None
        if cohort_requested:
            cohort_completion_rate = (
                Decimal(cohort_completed * 100) / Decimal(cohort_requested)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        strict_conversion = await self._strict_conversion(session, start=start, end=end)

        return BookingRecoverySummary(
            date_from=date_from,
            date_to=date_to,
            no_slot_sessions=no_slot_sessions,
            alternatives_requested=requested,
            alternatives_returned=count(BookingRecoveryEventType.alternatives_returned),
            alternative_slots_returned=sums.get(BookingRecoveryEventType.alternatives_returned.value, 0),
            alternative_slots_viewed=viewed,
            alternative_slots_selected=selected,
            bookings_after_alternative=recovered,
            alternative_recovery_rate_percent=recovery_rate,
            alternative_selection_rate_percent=selection_rate,
            alternative_cohort_requested_sessions=cohort_requested,
            alternative_cohort_viewed_sessions=cohort_viewed,
            alternative_cohort_selected_sessions=cohort_selected,
            alternative_cohort_completed_sessions=cohort_completed,
            alternative_cohort_selection_rate_percent=cohort_selection_rate,
            alternative_cohort_completion_rate_percent=cohort_completion_rate,
            alternative_strict_conversion=strict_conversion,
            alternative_engagement_collection_started_at=engagement_collection_started_at,
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
