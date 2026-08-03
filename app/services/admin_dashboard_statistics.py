from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence

from fastapi import HTTPException, status
from sqlalchemy import Numeric, String, and_, case, cast, distinct, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.booking import (
    BarberService,
    Booking,
    BookingServiceItem,
    BookingStatus,
    Master,
    MasterAvailabilityWindow,
    MasterTimeBlock,
)
from app.schemas.statistics import (
    AdminDashboardStatisticsResponse,
    DashboardActionSignal,
    DashboardCapacityLeakage,
    DashboardCountMetric,
    DashboardDatePeriod,
    DashboardDefinitions,
    DashboardExecutiveMetrics,
    DashboardMasterBreakdownItem,
    DashboardMoneyMetric,
    DashboardPeriodMetadata,
    DashboardPrimeTimeWindow,
    DashboardRateMetric,
    DashboardRepeatMetric,
    DashboardRetention,
    DashboardServiceBreakdownItem,
    DashboardSignalThresholds,
)
from app.services.booking import KYIV_TZ
from app.services.booking_funnel import BookingFunnelService
from app.services.master_reviews import MasterReviewService

MONEY_ZERO = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")
RATE_ZERO = Decimal("0.00")
RATE_QUANT = Decimal("0.01")
MAX_DASHBOARD_RANGE_DAYS = 366
MIN_PRIME_WINDOW_MINUTES = 30
MAX_PRIME_WINDOWS = 5


@dataclass(frozen=True)
class DashboardSignalThresholdConfig:
    pending_bookings_min_count: int = 1
    cancellation_min_count: int = 3
    cancellation_min_rate_percent: Decimal = Decimal("15.00")
    cancellation_min_increase_percentage_points: Decimal = Decimal("5.00")
    unfilled_capacity_min_minutes: int = 120
    unfilled_capacity_min_percent: Decimal = Decimal("30.00")
    review_moderation_backlog_min_count: int = 1
    failed_review_delivery_min_count: int = 1


SIGNAL_THRESHOLDS = DashboardSignalThresholdConfig()


@dataclass(frozen=True)
class PeriodBounds:
    date_from: date
    date_to: date
    start: datetime
    end: datetime
    days: int


@dataclass(frozen=True)
class ExecutiveAggregate:
    gross_revenue: Decimal
    completed_visits: int
    unique_clients: int
    average_check: Decimal
    booking_subtotal: Decimal
    promotion_discount_amount: Decimal
    cancelled_visits: int
    scheduled_bookings: int
    pending_upcoming_bookings: int


@dataclass(frozen=True)
class CapacityByMaster:
    available_minutes: int = 0
    booked_minutes: int = 0
    upcoming_available_minutes: int = 0
    upcoming_empty_minutes: int = 0


@dataclass(frozen=True)
class RetentionAggregate:
    new_clients: int
    returning_clients: int
    repeat_metrics: dict[int, tuple[int, int]]
    by_master: dict[int, tuple[int, int]]


Interval = tuple[datetime, datetime]


def as_money(value: object) -> Decimal:
    if value is None:
        return MONEY_ZERO
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def divide_money(value: Decimal, denominator: int) -> Decimal:
    if denominator <= 0:
        return MONEY_ZERO
    return (value / Decimal(denominator)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def percent(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    denominator_decimal = Decimal(str(denominator))
    if denominator_decimal == 0:
        return RATE_ZERO
    return (
        Decimal(str(numerator)) * Decimal("100") / denominator_decimal
    ).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def optional_percent(numerator: int | Decimal, denominator: int | Decimal) -> Decimal | None:
    if Decimal(str(denominator)) == 0:
        return None
    return percent(numerator, denominator)


def safe_percent_change(current: int | Decimal, previous: int | Decimal | None) -> Decimal | None:
    if previous is None:
        return None
    previous_decimal = Decimal(str(previous))
    if previous_decimal == 0:
        return None
    return (
        (Decimal(str(current)) - previous_decimal)
        * Decimal("100")
        / abs(previous_decimal)
    ).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def _aware_kyiv(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KYIV_TZ)
    return value.astimezone(KYIV_TZ)


def _clip_interval(interval: Interval, start: datetime, end: datetime) -> Interval | None:
    clipped_start = max(_aware_kyiv(interval[0]), start)
    clipped_end = min(_aware_kyiv(interval[1]), end)
    return (clipped_start, clipped_end) if clipped_start < clipped_end else None


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    ordered = sorted(
        (
            (_aware_kyiv(start), _aware_kyiv(end))
            for start, end in intervals
            if _aware_kyiv(start) < _aware_kyiv(end)
        ),
        key=lambda item: (item[0], item[1]),
    )
    merged: list[Interval] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def subtract_intervals(base: Iterable[Interval], cuts: Iterable[Interval]) -> list[Interval]:
    result: list[Interval] = []
    merged_cuts = merge_intervals(cuts)
    for base_start, base_end in merge_intervals(base):
        cursor = base_start
        for cut_start, cut_end in merged_cuts:
            if cut_end <= cursor:
                continue
            if cut_start >= base_end:
                break
            if cut_start > cursor:
                result.append((cursor, min(cut_start, base_end)))
            cursor = max(cursor, cut_end)
            if cursor >= base_end:
                break
        if cursor < base_end:
            result.append((cursor, base_end))
    return result


def intersect_intervals(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    left_items = merge_intervals(left)
    right_items = merge_intervals(right)
    intersections: list[Interval] = []
    left_index = 0
    right_index = 0
    while left_index < len(left_items) and right_index < len(right_items):
        start = max(left_items[left_index][0], right_items[right_index][0])
        end = min(left_items[left_index][1], right_items[right_index][1])
        if start < end:
            intersections.append((start, end))
        if left_items[left_index][1] <= right_items[right_index][1]:
            left_index += 1
        else:
            right_index += 1
    return intersections


def interval_minutes(intervals: Iterable[Interval]) -> int:
    return int(
        sum(
            (
                end.astimezone(KYIV_TZ).timestamp()
                - start.astimezone(KYIV_TZ).timestamp()
            )
            / 60
            for start, end in intervals
        )
    )


def build_actionable_signals(
    *,
    pending_upcoming: int,
    cancelled_visits: int,
    cancellation_rate: Decimal,
    previous_cancellation_rate: Decimal | None,
    empty_upcoming_minutes: int,
    empty_upcoming_rate: Decimal,
    moderation_backlog: int,
    failed_review_deliveries: int,
    thresholds: DashboardSignalThresholdConfig = SIGNAL_THRESHOLDS,
) -> list[DashboardActionSignal]:
    signals: list[DashboardActionSignal] = []
    if pending_upcoming >= thresholds.pending_bookings_min_count:
        signals.append(
            DashboardActionSignal(
                severity="warning",
                code="pending_bookings",
                title_uk="Є непідтверджені записи",
                explanation_uk="Майбутні записи очікують підтвердження та можуть бути втрачені без дії адміністратора.",
                metric_value=Decimal(pending_upcoming),
                metric_unit="bookings",
                recommended_backoffice_route="/bookings?status=pending",
            )
        )
    cancellation_increase = (
        cancellation_rate - previous_cancellation_rate
        if previous_cancellation_rate is not None
        else None
    )
    if (
        previous_cancellation_rate is not None
        and cancelled_visits >= thresholds.cancellation_min_count
        and cancellation_rate >= thresholds.cancellation_min_rate_percent
        and cancellation_increase is not None
        and cancellation_increase >= thresholds.cancellation_min_increase_percentage_points
    ):
        signals.append(
            DashboardActionSignal(
                severity="warning",
                code="elevated_cancellations",
                title_uk="Зросла частка скасувань",
                explanation_uk="Частка скасованих записів об’єктивно перевищила попередній рівний період і встановлений поріг.",
                metric_value=cancellation_increase.quantize(RATE_QUANT),
                metric_unit="percentage_points",
                recommended_backoffice_route="/bookings?status=cancelled",
            )
        )
    if (
        empty_upcoming_minutes >= thresholds.unfilled_capacity_min_minutes
        and empty_upcoming_rate >= thresholds.unfilled_capacity_min_percent
    ):
        signals.append(
            DashboardActionSignal(
                severity="info",
                code="unfilled_capacity",
                title_uk="Є незаповнена майбутня місткість",
                explanation_uk="Значна частина вже опублікованого майбутнього робочого часу ще не зайнята записами.",
                metric_value=Decimal(empty_upcoming_minutes),
                metric_unit="minutes",
                recommended_backoffice_route="/time-blocks",
            )
        )
    if moderation_backlog >= thresholds.review_moderation_backlog_min_count:
        signals.append(
            DashboardActionSignal(
                severity="warning",
                code="review_moderation_backlog",
                title_uk="Відгуки очікують модерації",
                explanation_uk="Є внутрішні відгуки зі статусом очікування, які ще не схвалені або відхилені.",
                metric_value=Decimal(moderation_backlog),
                metric_unit="reviews",
                recommended_backoffice_route="/reviews?moderation_status=pending",
            )
        )
    if failed_review_deliveries >= thresholds.failed_review_delivery_min_count:
        signals.append(
            DashboardActionSignal(
                severity="critical",
                code="failed_review_delivery",
                title_uk="Не доставлено запити на відгук",
                explanation_uk="Є запити на відгук зі статусом помилки доставки, які потребують перевірки.",
                metric_value=Decimal(failed_review_deliveries),
                metric_unit="deliveries",
                recommended_backoffice_route="/reviews?request_state=failed",
            )
        )
    return signals


class AdminDashboardStatisticsService:
    def __init__(self) -> None:
        self.booking_funnel_service = BookingFunnelService()
        self.review_service = MasterReviewService()

    def period_bounds(self, date_from: date, date_to: date) -> PeriodBounds:
        if date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from must be on or before date_to",
            )
        days = (date_to - date_from).days + 1
        if days > MAX_DASHBOARD_RANGE_DAYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Date range must not exceed {MAX_DASHBOARD_RANGE_DAYS} days",
            )
        start = datetime.combine(date_from, time.min, tzinfo=KYIV_TZ)
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=KYIV_TZ)
        return PeriodBounds(date_from=date_from, date_to=date_to, start=start, end=end, days=days)

    def previous_period(self, current: PeriodBounds) -> PeriodBounds:
        previous_to = current.date_from - timedelta(days=1)
        previous_from = previous_to - timedelta(days=current.days - 1)
        return self.period_bounds(previous_from, previous_to)

    async def get_dashboard(
        self,
        session: AsyncSession,
        *,
        date_from: date,
        date_to: date,
        compare_to_previous: bool = True,
        master_id: int | None = None,
        now: datetime | None = None,
    ) -> AdminDashboardStatisticsResponse:
        current = self.period_bounds(date_from, date_to)
        previous = self.previous_period(current) if compare_to_previous else None
        now_kyiv = _aware_kyiv(now or datetime.now(KYIV_TZ))
        masters = await self._visible_masters(session, master_id=master_id)
        master_ids = [item[0] for item in masters]

        current_executive = await self._executive_aggregate(
            session,
            period=current,
            master_id=master_id,
            now=now_kyiv,
        )
        previous_executive = (
            await self._executive_aggregate(
                session,
                period=previous,
                master_id=master_id,
                now=now_kyiv,
            )
            if previous is not None
            else None
        )
        capacity_by_master, prime_windows = await self._capacity(
            session,
            period=current,
            masters=masters,
            now=now_kyiv,
        )
        retention = await self._retention(
            session,
            period=current,
            master_id=master_id,
            now=now_kyiv,
        )
        master_financials = await self._master_financials(
            session,
            period=current,
            master_ids=master_ids,
        )
        ratings = await self.review_service.approved_rating_aggregates(session, master_ids)
        services = await self._service_breakdown(
            session,
            period=current,
            master_id=master_id,
        )
        review_counts = await self.review_service.dashboard_operational_counts(
            session,
            master_id=master_id,
        )
        booking_funnel = await self.booking_funnel_service.aggregate(
            session,
            start=current.start,
            end=current.end,
            master_id=master_id,
        )

        total_available = sum(item.available_minutes for item in capacity_by_master.values())
        total_booked = sum(item.booked_minutes for item in capacity_by_master.values())
        upcoming_available = sum(item.upcoming_available_minutes for item in capacity_by_master.values())
        upcoming_empty = sum(item.upcoming_empty_minutes for item in capacity_by_master.values())
        cancellation_rate = percent(
            current_executive.cancelled_visits,
            current_executive.scheduled_bookings,
        )
        previous_cancellation_rate = (
            percent(previous_executive.cancelled_visits, previous_executive.scheduled_bookings)
            if previous_executive is not None
            else None
        )
        cancellation_change = (
            (cancellation_rate - previous_cancellation_rate).quantize(RATE_QUANT)
            if previous_cancellation_rate is not None
            else None
        )
        upcoming_empty_rate = percent(upcoming_empty, upcoming_available)

        return AdminDashboardStatisticsResponse(
            period=self._period_metadata(
                current=current,
                previous=previous,
                master_id=master_id,
            ),
            executive=self._executive_response(current_executive, previous_executive),
            capacity_and_leakage=DashboardCapacityLeakage(
                available_minutes=total_available,
                booked_minutes=total_booked,
                utilisation_rate=percent(total_booked, total_available),
                cancelled_visits=current_executive.cancelled_visits,
                cancellation_rate=DashboardRateMetric(
                    current=cancellation_rate,
                    previous=previous_cancellation_rate,
                    change_percentage_points=cancellation_change,
                ),
                pending_unconfirmed_upcoming_bookings=current_executive.pending_upcoming_bookings,
                empty_upcoming_capacity_minutes=upcoming_empty,
                empty_upcoming_capacity_rate=upcoming_empty_rate,
                prime_time_empty_windows=prime_windows,
                no_show_visits=None,
                no_show_status="unavailable",
            ),
            retention=self._retention_response(retention),
            masters=self._master_breakdown_response(
                masters=masters,
                financials=master_financials,
                capacity=capacity_by_master,
                retention=retention.by_master,
                ratings=ratings,
            ),
            services=services,
            booking_funnel=booking_funnel,
            actionable_signals=build_actionable_signals(
                pending_upcoming=current_executive.pending_upcoming_bookings,
                cancelled_visits=current_executive.cancelled_visits,
                cancellation_rate=cancellation_rate,
                previous_cancellation_rate=previous_cancellation_rate,
                empty_upcoming_minutes=upcoming_empty,
                empty_upcoming_rate=upcoming_empty_rate,
                moderation_backlog=review_counts.moderation_backlog,
                failed_review_deliveries=review_counts.failed_deliveries,
            ),
        )

    async def _visible_masters(
        self,
        session: AsyncSession,
        *,
        master_id: int | None,
    ) -> list[tuple[int, str]]:
        stmt = (
            select(Master.id, Master.full_name)
            .where(
                Master.is_active.is_(True),
                Master.show_on_master_block.is_(True),
            )
            .order_by(Master.full_name.asc(), Master.id.asc())
        )
        if master_id is not None:
            stmt = stmt.where(Master.id == master_id)
        rows = [(int(row[0]), str(row[1])) for row in (await session.execute(stmt)).all()]
        if master_id is not None and not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        return rows

    async def _executive_aggregate(
        self,
        session: AsyncSession,
        *,
        period: PeriodBounds,
        master_id: int | None,
        now: datetime,
    ) -> ExecutiveAggregate:
        total_amount = cast(
            func.coalesce(Booking.total_amount, Booking.subtotal_amount, 0),
            Numeric(14, 2),
        )
        subtotal_amount = cast(
            func.coalesce(Booking.subtotal_amount, Booking.total_amount, 0),
            Numeric(14, 2),
        )
        discount_amount = cast(func.coalesce(Booking.promotion_discount_amount, 0), Numeric(14, 2))
        completed = Booking.status == BookingStatus.completed
        cancelled = Booking.status == BookingStatus.cancelled
        client_key = self._client_key()
        row = (
            await session.execute(
                self._master_filter(
                    select(
                        func.coalesce(func.sum(case((completed, total_amount), else_=0)), 0),
                        func.sum(case((completed, 1), else_=0)),
                        func.count(distinct(case((completed, client_key)))),
                        func.coalesce(func.sum(case((completed, subtotal_amount), else_=0)), 0),
                        func.coalesce(func.sum(case((completed, discount_amount), else_=0)), 0),
                        func.sum(case((cancelled, 1), else_=0)),
                        func.count(Booking.id),
                        func.sum(
                            case(
                                (
                                    (Booking.status == BookingStatus.pending)
                                    & (Booking.start_at >= now)
                                    & (Booking.start_at < period.end),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                    ).where(
                        Booking.start_at >= period.start,
                        Booking.start_at < period.end,
                    ),
                    master_id,
                )
            )
        ).one()
        gross_revenue = as_money(row[0])
        completed_visits = int(row[1] or 0)
        return ExecutiveAggregate(
            gross_revenue=gross_revenue,
            completed_visits=completed_visits,
            unique_clients=int(row[2] or 0),
            average_check=divide_money(gross_revenue, completed_visits),
            booking_subtotal=as_money(row[3]),
            promotion_discount_amount=as_money(row[4]),
            cancelled_visits=int(row[5] or 0),
            scheduled_bookings=int(row[6] or 0),
            pending_upcoming_bookings=int(row[7] or 0),
        )

    async def _capacity(
        self,
        session: AsyncSession,
        *,
        period: PeriodBounds,
        masters: Sequence[tuple[int, str]],
        now: datetime,
    ) -> tuple[dict[int, CapacityByMaster], list[DashboardPrimeTimeWindow]]:
        master_ids = [item[0] for item in masters]
        if not master_ids:
            return {}, []
        window_rows = (
            await session.execute(
                select(
                    MasterAvailabilityWindow.master_id,
                    MasterAvailabilityWindow.start_at,
                    MasterAvailabilityWindow.end_at,
                ).where(
                    MasterAvailabilityWindow.master_id.in_(master_ids),
                    MasterAvailabilityWindow.start_at < period.end,
                    MasterAvailabilityWindow.end_at > period.start,
                )
            )
        ).all()
        block_rows = (
            await session.execute(
                select(
                    MasterTimeBlock.master_id,
                    MasterTimeBlock.start_at,
                    MasterTimeBlock.end_at,
                ).where(
                    MasterTimeBlock.master_id.in_(master_ids),
                    MasterTimeBlock.start_at < period.end,
                    MasterTimeBlock.end_at > period.start,
                )
            )
        ).all()
        booking_rows = (
            await session.execute(
                select(
                    Booking.master_id,
                    Booking.start_at,
                    Booking.end_at,
                ).where(
                    Booking.master_id.in_(master_ids),
                    Booking.status != BookingStatus.cancelled,
                    Booking.start_at < period.end,
                    Booking.end_at > period.start,
                )
            )
        ).all()

        windows_by_master = self._group_clipped_intervals(window_rows, period.start, period.end)
        blocks_by_master = self._group_clipped_intervals(block_rows, period.start, period.end)
        bookings_by_master = self._group_clipped_intervals(booking_rows, period.start, period.end)
        upcoming_start = max(period.start, now)
        master_names = dict(masters)
        result: dict[int, CapacityByMaster] = {}
        prime_windows: list[DashboardPrimeTimeWindow] = []

        for selected_master_id in master_ids:
            available = subtract_intervals(
                windows_by_master.get(selected_master_id, []),
                blocks_by_master.get(selected_master_id, []),
            )
            booked = intersect_intervals(
                available,
                bookings_by_master.get(selected_master_id, []),
            )
            upcoming_available = (
                [
                    clipped
                    for interval in available
                    if (clipped := _clip_interval(interval, upcoming_start, period.end)) is not None
                ]
                if upcoming_start < period.end
                else []
            )
            upcoming_booked = (
                [
                    clipped
                    for interval in bookings_by_master.get(selected_master_id, [])
                    if (clipped := _clip_interval(interval, upcoming_start, period.end)) is not None
                ]
                if upcoming_start < period.end
                else []
            )
            upcoming_empty = subtract_intervals(upcoming_available, upcoming_booked)
            result[selected_master_id] = CapacityByMaster(
                available_minutes=interval_minutes(available),
                booked_minutes=interval_minutes(booked),
                upcoming_available_minutes=interval_minutes(upcoming_available),
                upcoming_empty_minutes=interval_minutes(upcoming_empty),
            )
            prime_windows.extend(
                self._prime_time_windows(
                    master_id=selected_master_id,
                    master_name=master_names[selected_master_id],
                    empty_intervals=upcoming_empty,
                )
            )

        prime_windows.sort(
            key=lambda item: (
                -item.available_minutes,
                item.start_at,
                item.master_id,
            )
        )
        return result, prime_windows[:MAX_PRIME_WINDOWS]

    async def _retention(
        self,
        session: AsyncSession,
        *,
        period: PeriodBounds,
        master_id: int | None,
        now: datetime,
    ) -> RetentionAggregate:
        client_key = self._client_key().label("client_key")
        first_visits_stmt = (
            select(
                client_key,
                func.min(Booking.start_at).label("first_visit"),
            )
            .where(
                Booking.status == BookingStatus.completed,
                Booking.start_at < period.end,
            )
            .group_by(client_key)
        )
        if master_id is not None:
            first_visits_stmt = first_visits_stmt.where(Booking.master_id == master_id)
        first_visits = first_visits_stmt.cte("dashboard_first_visits")

        period_clients_by_master_stmt = (
            select(
                Booking.master_id.label("master_id"),
                self._client_key().label("client_key"),
            )
            .where(
                Booking.status == BookingStatus.completed,
                Booking.start_at >= period.start,
                Booking.start_at < period.end,
            )
            .distinct()
        )
        if master_id is not None:
            period_clients_by_master_stmt = period_clients_by_master_stmt.where(Booking.master_id == master_id)
        period_clients_by_master = period_clients_by_master_stmt.cte("dashboard_period_clients_by_master")
        period_clients = (
            select(period_clients_by_master.c.client_key)
            .distinct()
            .cte("dashboard_period_clients")
        )

        classification = (
            await session.execute(
                select(
                    func.sum(case((first_visits.c.first_visit >= period.start, 1), else_=0)),
                    func.sum(case((first_visits.c.first_visit < period.start, 1), else_=0)),
                )
                .select_from(period_clients)
                .join(first_visits, first_visits.c.client_key == period_clients.c.client_key)
            )
        ).one()
        by_master_rows = (
            await session.execute(
                select(
                    period_clients_by_master.c.master_id,
                    func.sum(case((first_visits.c.first_visit >= period.start, 1), else_=0)),
                    func.sum(case((first_visits.c.first_visit < period.start, 1), else_=0)),
                )
                .select_from(period_clients_by_master)
                .join(
                    first_visits,
                    first_visits.c.client_key == period_clients_by_master.c.client_key,
                )
                .group_by(period_clients_by_master.c.master_id)
            )
        ).all()

        repeat_booking = aliased(Booking)
        second_visit_conditions = [
            repeat_booking.status == BookingStatus.completed,
            self._client_key(repeat_booking) == first_visits.c.client_key,
            repeat_booking.start_at > first_visits.c.first_visit,
        ]
        if master_id is not None:
            second_visit_conditions.append(repeat_booking.master_id == master_id)
        first_and_second = (
            select(
                first_visits.c.client_key,
                first_visits.c.first_visit,
                func.min(repeat_booking.start_at).label("second_visit"),
            )
            .select_from(first_visits)
            .outerjoin(repeat_booking, and_(*second_visit_conditions))
            .group_by(first_visits.c.client_key, first_visits.c.first_visit)
            .cte("dashboard_first_and_second_visits")
        )
        observation_end = min(period.end, max(period.start, now))
        repeat_columns = []
        for window_days in (30, 45, 60):
            eligible = (
                (first_and_second.c.first_visit >= period.start)
                & (first_and_second.c.first_visit < period.end)
                & (
                    first_and_second.c.first_visit
                    <= observation_end - timedelta(days=window_days)
                )
            )
            repeated = eligible & (
                first_and_second.c.second_visit
                <= first_and_second.c.first_visit + timedelta(days=window_days)
            )
            repeat_columns.extend(
                [
                    func.sum(case((repeated, 1), else_=0)),
                    func.sum(case((eligible, 1), else_=0)),
                ]
            )
        repeat_row = (await session.execute(select(*repeat_columns))).one()
        repeat_metrics = {
            window_days: (
                int(repeat_row[index * 2] or 0),
                int(repeat_row[index * 2 + 1] or 0),
            )
            for index, window_days in enumerate((30, 45, 60))
        }
        return RetentionAggregate(
            new_clients=int(classification[0] or 0),
            returning_clients=int(classification[1] or 0),
            repeat_metrics=repeat_metrics,
            by_master={
                int(row[0]): (int(row[1] or 0), int(row[2] or 0))
                for row in by_master_rows
            },
        )

    async def _master_financials(
        self,
        session: AsyncSession,
        *,
        period: PeriodBounds,
        master_ids: Sequence[int],
    ) -> dict[int, tuple[Decimal, int]]:
        if not master_ids:
            return {}
        rows = (
            await session.execute(
                select(
                    Booking.master_id,
                    func.coalesce(
                        func.sum(
                            cast(
                                func.coalesce(Booking.total_amount, Booking.subtotal_amount, 0),
                                Numeric(14, 2),
                            )
                        ),
                        0,
                    ),
                    func.count(Booking.id),
                )
                .where(
                    Booking.master_id.in_(master_ids),
                    Booking.status == BookingStatus.completed,
                    Booking.start_at >= period.start,
                    Booking.start_at < period.end,
                )
                .group_by(Booking.master_id)
            )
        ).all()
        return {
            int(row[0]): (as_money(row[1]), int(row[2] or 0))
            for row in rows
        }

    async def _service_breakdown(
        self,
        session: AsyncSession,
        *,
        period: PeriodBounds,
        master_id: int | None,
    ) -> list[DashboardServiceBreakdownItem]:
        service_price = cast(BookingServiceItem.price_amount, Numeric(14, 2))
        item_count = func.count(BookingServiceItem.id).over(partition_by=Booking.id)
        total_weight = func.sum(service_price).over(partition_by=Booking.id)
        item_stmt = (
            select(
                Booking.id.label("booking_id"),
                BarberService.id.label("service_id"),
                BarberService.name.label("service_name"),
                service_price.label("service_price"),
                item_count.label("item_count"),
                total_weight.label("total_weight"),
                cast(
                    func.coalesce(Booking.total_amount, Booking.subtotal_amount, 0),
                    Numeric(14, 2),
                ).label("gross_revenue"),
                cast(
                    func.coalesce(Booking.subtotal_amount, Booking.total_amount, 0),
                    Numeric(14, 2),
                ).label("subtotal"),
                cast(
                    func.coalesce(Booking.promotion_discount_amount, 0)
                    + func.coalesce(Booking.manual_discount_amount, 0),
                    Numeric(14, 2),
                ).label("discounts"),
            )
            .join(BookingServiceItem, BookingServiceItem.booking_id == Booking.id)
            .join(BarberService, BarberService.id == BookingServiceItem.service_id)
            .where(
                Booking.status == BookingStatus.completed,
                Booking.start_at >= period.start,
                Booking.start_at < period.end,
            )
        )
        if master_id is not None:
            item_stmt = item_stmt.where(Booking.master_id == master_id)
        legacy_item_stmt = (
            select(
                Booking.id.label("booking_id"),
                BarberService.id.label("service_id"),
                BarberService.name.label("service_name"),
                cast(BarberService.price, Numeric(14, 2)).label("service_price"),
                literal(1).label("item_count"),
                cast(BarberService.price, Numeric(14, 2)).label("total_weight"),
                cast(
                    func.coalesce(Booking.total_amount, Booking.subtotal_amount, 0),
                    Numeric(14, 2),
                ).label("gross_revenue"),
                cast(
                    func.coalesce(Booking.subtotal_amount, Booking.total_amount, 0),
                    Numeric(14, 2),
                ).label("subtotal"),
                cast(
                    func.coalesce(Booking.promotion_discount_amount, 0)
                    + func.coalesce(Booking.manual_discount_amount, 0),
                    Numeric(14, 2),
                ).label("discounts"),
            )
            .join(BarberService, BarberService.id == Booking.service_id)
            .where(
                Booking.status == BookingStatus.completed,
                Booking.start_at >= period.start,
                Booking.start_at < period.end,
                ~select(BookingServiceItem.id)
                .where(BookingServiceItem.booking_id == Booking.id)
                .exists(),
            )
        )
        if master_id is not None:
            legacy_item_stmt = legacy_item_stmt.where(Booking.master_id == master_id)
        items = item_stmt.union_all(legacy_item_stmt).subquery("dashboard_service_items")
        allocation_weight = case(
            (
                items.c.total_weight > 0,
                items.c.service_price / items.c.total_weight,
            ),
            else_=Decimal("1") / cast(items.c.item_count, Numeric(14, 2)),
        )
        rows = (
            await session.execute(
                select(
                    items.c.service_id,
                    items.c.service_name,
                    func.count(distinct(items.c.booking_id)),
                    func.sum(items.c.gross_revenue * allocation_weight),
                    func.sum(items.c.subtotal * allocation_weight),
                    func.sum(items.c.discounts * allocation_weight),
                )
                .group_by(items.c.service_id, items.c.service_name)
                .order_by(
                    func.sum(items.c.gross_revenue * allocation_weight).desc(),
                    items.c.service_name.asc(),
                )
            )
        ).all()
        result: list[DashboardServiceBreakdownItem] = []
        for row in rows:
            completed_visits = int(row[2] or 0)
            gross_revenue = as_money(row[3])
            result.append(
                DashboardServiceBreakdownItem(
                    service_id=int(row[0]),
                    service_name=str(row[1]),
                    completed_visits=completed_visits,
                    gross_revenue=gross_revenue,
                    subtotal=as_money(row[4]),
                    discounts=as_money(row[5]),
                    average_realized_revenue_per_completed_service=divide_money(
                        gross_revenue,
                        completed_visits,
                    ),
                )
            )
        return result

    def _executive_response(
        self,
        current: ExecutiveAggregate,
        previous: ExecutiveAggregate | None,
    ) -> DashboardExecutiveMetrics:
        return DashboardExecutiveMetrics(
            gross_revenue=self._money_metric(
                current.gross_revenue,
                previous.gross_revenue if previous else None,
            ),
            completed_visits=self._count_metric(
                current.completed_visits,
                previous.completed_visits if previous else None,
            ),
            unique_clients=self._count_metric(
                current.unique_clients,
                previous.unique_clients if previous else None,
            ),
            average_check=self._money_metric(
                current.average_check,
                previous.average_check if previous else None,
            ),
            booking_subtotal=self._money_metric(
                current.booking_subtotal,
                previous.booking_subtotal if previous else None,
            ),
            promotion_discount_amount=self._money_metric(
                current.promotion_discount_amount,
                previous.promotion_discount_amount if previous else None,
            ),
        )

    def _retention_response(self, aggregate: RetentionAggregate) -> DashboardRetention:
        metrics = {
            window_days: DashboardRepeatMetric(
                window_days=window_days,
                repeated_clients=aggregate.repeat_metrics[window_days][0],
                eligible_clients=aggregate.repeat_metrics[window_days][1],
                repeat_rate=optional_percent(
                    aggregate.repeat_metrics[window_days][0],
                    aggregate.repeat_metrics[window_days][1],
                ),
            )
            for window_days in (30, 45, 60)
        }
        return DashboardRetention(
            new_clients=aggregate.new_clients,
            returning_clients=aggregate.returning_clients,
            repeat_30_day=metrics[30],
            repeat_45_day=metrics[45],
            repeat_60_day=metrics[60],
        )

    def _master_breakdown_response(
        self,
        *,
        masters: Sequence[tuple[int, str]],
        financials: dict[int, tuple[Decimal, int]],
        capacity: dict[int, CapacityByMaster],
        retention: dict[int, tuple[int, int]],
        ratings: dict,
    ) -> list[DashboardMasterBreakdownItem]:
        result: list[DashboardMasterBreakdownItem] = []
        for master_id, master_name in masters:
            revenue, completed_visits = financials.get(master_id, (MONEY_ZERO, 0))
            master_capacity = capacity.get(master_id, CapacityByMaster())
            new_clients, returning_clients = retention.get(master_id, (0, 0))
            rating = ratings.get(master_id)
            result.append(
                DashboardMasterBreakdownItem(
                    master_id=master_id,
                    master_name=master_name,
                    gross_revenue=revenue,
                    completed_visits=completed_visits,
                    average_check=divide_money(revenue, completed_visits),
                    available_minutes=master_capacity.available_minutes,
                    booked_minutes=master_capacity.booked_minutes,
                    utilisation_rate=percent(
                        master_capacity.booked_minutes,
                        master_capacity.available_minutes,
                    ),
                    revenue_per_available_hour=(
                        (revenue * Decimal("60") / Decimal(master_capacity.available_minutes)).quantize(
                            MONEY_QUANT,
                            rounding=ROUND_HALF_UP,
                        )
                        if master_capacity.available_minutes
                        else MONEY_ZERO
                    ),
                    new_clients=new_clients,
                    returning_clients=returning_clients,
                    approved_rating=rating.average_rating if rating else None,
                    approved_review_count=rating.review_count if rating else 0,
                )
            )
        return result

    def _period_metadata(
        self,
        *,
        current: PeriodBounds,
        previous: PeriodBounds | None,
        master_id: int | None,
    ) -> DashboardPeriodMetadata:
        return DashboardPeriodMetadata(
            current=self._date_period(current),
            previous=self._date_period(previous) if previous else None,
            timezone="Europe/Kyiv",
            applied_master_id=master_id,
            comparison_requested=previous is not None,
            max_range_days=MAX_DASHBOARD_RANGE_DAYS,
            definitions=DashboardDefinitions(
                gross_revenue=(
                    "Sum of total_amount snapshots for completed bookings; "
                    "revenue is not profit and no costs are inferred."
                ),
                available_minutes=(
                    "Published availability for active visible masters intersected with the Kyiv-local "
                    "period, minus the union of overlapping time blocks."
                ),
                booked_minutes=(
                    "Union of pending, confirmed and completed booking intervals intersected with net available time."
                ),
                cancellation_rate="Cancelled bookings divided by all bookings scheduled in the period.",
                retention_cohort=(
                    "Clients whose first completed visit in the applied master scope is inside the current period. "
                    "For each 30/45/60-day metric, the denominator includes only clients whose first visit plus the "
                    "full window is on or before min(period end, current Kyiv time); the numerator requires a later "
                    "completed visit within that window."
                ),
                service_allocation=(
                    "Completed booking subtotal, discount and total snapshots are allocated across booking services "
                    "in proportion to current service prices because per-item price snapshots do not exist."
                ),
                no_show="Unavailable because BookingStatus has no no-show value; null is returned instead of zero.",
                prime_time=(
                    "Kyiv local weekday 17:00-20:00 and weekend 10:00-14:00; only upcoming empty intersections "
                    f"of at least {MIN_PRIME_WINDOW_MINUTES} minutes are listed, capped at {MAX_PRIME_WINDOWS}."
                ),
            ),
            signal_thresholds=DashboardSignalThresholds(
                **SIGNAL_THRESHOLDS.__dict__,
            ),
        )

    def _prime_time_windows(
        self,
        *,
        master_id: int,
        master_name: str,
        empty_intervals: Iterable[Interval],
    ) -> list[DashboardPrimeTimeWindow]:
        result: list[DashboardPrimeTimeWindow] = []
        for empty_start, empty_end in empty_intervals:
            current_date = empty_start.date()
            while current_date <= empty_end.date():
                if current_date.weekday() < 5:
                    prime_start_time = time(17, 0)
                    prime_end_time = time(20, 0)
                    definition_code = "weekday_evening"
                else:
                    prime_start_time = time(10, 0)
                    prime_end_time = time(14, 0)
                    definition_code = "weekend_midday"
                prime_start = datetime.combine(current_date, prime_start_time, tzinfo=KYIV_TZ)
                prime_end = datetime.combine(current_date, prime_end_time, tzinfo=KYIV_TZ)
                intersection = _clip_interval((empty_start, empty_end), prime_start, prime_end)
                if intersection is not None:
                    minutes = interval_minutes([intersection])
                    if minutes >= MIN_PRIME_WINDOW_MINUTES:
                        result.append(
                            DashboardPrimeTimeWindow(
                                master_id=master_id,
                                master_name=master_name,
                                start_at=intersection[0],
                                end_at=intersection[1],
                                available_minutes=minutes,
                                definition_code=definition_code,
                            )
                        )
                current_date += timedelta(days=1)
        return result

    def _group_clipped_intervals(
        self,
        rows: Iterable[Sequence],
        start: datetime,
        end: datetime,
    ) -> dict[int, list[Interval]]:
        grouped: dict[int, list[Interval]] = defaultdict(list)
        for row in rows:
            interval = _clip_interval((row[1], row[2]), start, end)
            if interval is not None:
                grouped[int(row[0])].append(interval)
        return grouped

    def _client_key(self, booking_model=Booking):
        return func.coalesce(
            case(
                (
                    booking_model.customer_id.is_not(None),
                    func.concat(literal("customer:"), cast(booking_model.customer_id, String)),
                )
            ),
            func.concat(literal("phone:"), booking_model.customer_phone),
        )

    def _master_filter(self, stmt, master_id: int | None):
        return stmt.where(Booking.master_id == master_id) if master_id is not None else stmt

    def _money_metric(
        self,
        current: Decimal,
        previous: Decimal | None,
    ) -> DashboardMoneyMetric:
        return DashboardMoneyMetric(
            current=current,
            previous=previous,
            percent_change=safe_percent_change(current, previous),
        )

    def _count_metric(
        self,
        current: int,
        previous: int | None,
    ) -> DashboardCountMetric:
        return DashboardCountMetric(
            current=current,
            previous=previous,
            percent_change=safe_percent_change(current, previous),
        )

    def _date_period(self, period: PeriodBounds) -> DashboardDatePeriod:
        return DashboardDatePeriod(
            date_from=period.date_from,
            date_to=period.date_to,
            days=period.days,
        )
