from __future__ import annotations

from calendar import monthrange
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import Date, Numeric, String, case, cast, desc, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master
from app.schemas.statistics import (
    AdminMonthlyStatisticsResponse,
    BarberComparisonItem,
    BarberMonthlyStatisticsResponse,
    BarbersComparisonResponse,
    StatisticsBarberSummary,
    StatisticsClientBreakdown,
    StatisticsServiceItem,
    StatisticsWorkloadDayItem,
    StatisticsWorkloadWeekItem,
)
from app.services.booking import KYIV_TZ

MONEY_ZERO = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


def money(value: Any) -> Decimal:
    if value is None:
        return MONEY_ZERO
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def divide_money(numerator: Decimal, denominator: int) -> Decimal:
    if denominator <= 0:
        return MONEY_ZERO
    return (numerator / Decimal(denominator)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


class StatisticsService:
    revenue_statuses = (BookingStatus.completed,)
    cancelled_statuses = (BookingStatus.cancelled,)

    def month_bounds(self, year: int, month: int) -> tuple[datetime, datetime]:
        if month < 1 or month > 12:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month must be between 1 and 12")
        if year < 2000 or year > 2100:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="year must be between 2000 and 2100")
        start = datetime(year, month, 1, tzinfo=KYIV_TZ)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=KYIV_TZ)
        else:
            end = datetime(year, month + 1, 1, tzinfo=KYIV_TZ)
        return start, end

    async def get_master_or_404(self, session: AsyncSession, barber_id: int) -> Master:
        master = await session.get(Master, barber_id)
        if not master:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Barber not found")
        return master

    async def get_linked_master_or_403(self, session: AsyncSession, admin_user_id: int) -> Master:
        master = (
            await session.execute(
                select(Master).where(Master.admin_user_id == admin_user_id, Master.is_active.is_(True))
            )
        ).scalar_one_or_none()
        if not master:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Current user is not linked to a barber")
        return master

    async def get_barber_monthly_statistics(
        self,
        session: AsyncSession,
        *,
        year: int,
        month: int,
        barber_id: int,
    ) -> BarberMonthlyStatisticsResponse:
        master = await self.get_master_or_404(session, barber_id)
        return await self._get_monthly_statistics(session, year=year, month=month, barber_id=barber_id, master=master)

    async def get_admin_monthly_statistics(
        self,
        session: AsyncSession,
        *,
        year: int,
        month: int,
        barber_id: int | None = None,
    ) -> AdminMonthlyStatisticsResponse:
        master = await self.get_master_or_404(session, barber_id) if barber_id is not None else None
        aggregate = await self._get_monthly_statistics(session, year=year, month=month, barber_id=barber_id, master=master)
        shop_aggregate = (
            aggregate
            if barber_id is None
            else await self._get_monthly_statistics(session, year=year, month=month, barber_id=None, master=None)
        )
        comparison = await self.get_barbers_comparison(session, year=year, month=month)
        return AdminMonthlyStatisticsResponse(
            year=year,
            month=month,
            barber_id=barber_id,
            total_barbershop_monthly_revenue=shop_aggregate.total_income,
            total_clients=shop_aggregate.unique_clients,
            total_completed_appointments=shop_aggregate.completed_appointments,
            total_cancelled_appointments=shop_aggregate.cancelled_appointments,
            aggregate=aggregate,
            top_barbers=comparison.top_performing_barbers,
            most_popular_services=shop_aggregate.most_popular_services,
        )

    async def get_barbers_comparison(
        self,
        session: AsyncSession,
        *,
        year: int,
        month: int,
    ) -> BarbersComparisonResponse:
        start, end = self.month_bounds(year, month)
        client_key = self._client_key()
        revenue_expr = self._revenue_expr()
        rows = (
            await session.execute(
                select(
                    Master.id,
                    Master.full_name,
                    func.coalesce(func.sum(revenue_expr), 0).label("revenue"),
                    func.count(distinct(Booking.id)).label("completed_appointments"),
                    func.count(distinct(client_key)).label("unique_clients"),
                )
                .join(Booking, Booking.master_id == Master.id)
                .join(BookingServiceItem, BookingServiceItem.booking_id == Booking.id)
                .join(BarberService, BarberService.id == BookingServiceItem.service_id)
                .where(
                    Booking.status.in_(self.revenue_statuses),
                    Booking.start_at >= start,
                    Booking.start_at < end,
                )
                .group_by(Master.id, Master.full_name)
                .order_by(desc("revenue"), desc("completed_appointments"), Master.full_name.asc())
            )
        ).all()

        service_rows = (
            await session.execute(
                select(
                    Booking.master_id,
                    BarberService.id,
                    BarberService.name,
                    func.count(Booking.id).label("count"),
                    func.coalesce(func.sum(revenue_expr), 0).label("revenue"),
                )
                .join(BookingServiceItem, BookingServiceItem.booking_id == Booking.id)
                .join(BarberService, BarberService.id == BookingServiceItem.service_id)
                .where(
                    Booking.status.in_(self.revenue_statuses),
                    Booking.start_at >= start,
                    Booking.start_at < end,
                )
                .group_by(Booking.master_id, BarberService.id, BarberService.name)
                .order_by(Booking.master_id.asc(), desc("count"), desc("revenue"), BarberService.name.asc())
            )
        ).all()
        popular_by_master: dict[int, list[StatisticsServiceItem]] = {}
        for row in service_rows:
            popular_by_master.setdefault(row[0], [])
            if len(popular_by_master[row[0]]) < 3:
                popular_by_master[row[0]].append(self._service_item(row[1], row[2], row[3], row[4]))

        items = [
            BarberComparisonItem(
                barber=StatisticsBarberSummary(id=row[0], full_name=row[1]),
                revenue=money(row[2]),
                unique_clients=int(row[4] or 0),
                completed_appointments=int(row[3] or 0),
                average_check=divide_money(money(row[2]), int(row[3] or 0)),
                popular_services=popular_by_master.get(row[0], []),
            )
            for row in rows
        ]
        return BarbersComparisonResponse(year=year, month=month, barbers=items, top_performing_barbers=items[:5])

    async def _get_monthly_statistics(
        self,
        session: AsyncSession,
        *,
        year: int,
        month: int,
        barber_id: int | None,
        master: Master | None,
    ) -> BarberMonthlyStatisticsResponse:
        start, end = self.month_bounds(year, month)
        summary = await self._summary(session, start=start, end=end, barber_id=barber_id)
        services = await self._service_breakdown(session, start=start, end=end, barber_id=barber_id)
        workload_days = await self._workload_by_day(session, start=start, end=end, barber_id=barber_id)
        workload_weeks = await self._workload_by_week(session, start=start, end=end, barber_id=barber_id)
        client_breakdown = await self._client_breakdown(session, start=start, end=end, barber_id=barber_id)
        days_in_month = monthrange(year, month)[1]
        normalized_days = self._normalize_workload_days(year, month, days_in_month, workload_days)
        best_day = max(normalized_days, key=lambda item: (item.revenue, item.completed_appointments), default=None)
        if best_day is not None and best_day.revenue == MONEY_ZERO and best_day.completed_appointments == 0:
            best_day = None

        return BarberMonthlyStatisticsResponse(
            year=year,
            month=month,
            barber=StatisticsBarberSummary(id=master.id, full_name=master.full_name) if master else None,
            total_income=summary["total_income"],
            completed_appointments=summary["completed_appointments"],
            unique_clients=summary["unique_clients"],
            total_services_performed=summary["completed_appointments"],
            most_popular_services=sorted(services, key=lambda item: (-item.count, -item.revenue, item.service_name))[:5],
            revenue_by_service=sorted(services, key=lambda item: (-item.revenue, -item.count, item.service_name)),
            average_check_per_appointment=divide_money(summary["total_income"], summary["completed_appointments"]),
            average_revenue_per_client=divide_money(summary["total_income"], summary["unique_clients"]),
            clients=client_breakdown,
            cancelled_appointments=summary["cancelled_appointments"],
            no_show_appointments=0,
            workload_by_day=normalized_days,
            workload_by_week=workload_weeks,
            best_revenue_day=best_day,
            service_category_breakdown=[],
            tips=MONEY_ZERO,
            bonuses=MONEY_ZERO,
        )

    async def _summary(
        self,
        session: AsyncSession,
        *,
        start: datetime,
        end: datetime,
        barber_id: int | None,
    ) -> dict[str, Any]:
        client_key = self._client_key()
        completed_stmt = (
            select(
                func.coalesce(func.sum(self._revenue_expr()), 0),
                func.count(distinct(Booking.id)),
                func.count(distinct(client_key)),
            )
            .join(BookingServiceItem, BookingServiceItem.booking_id == Booking.id)
            .join(BarberService, BarberService.id == BookingServiceItem.service_id)
            .where(
                Booking.status.in_(self.revenue_statuses),
                Booking.start_at >= start,
                Booking.start_at < end,
            )
        )
        cancelled_stmt = select(func.count(Booking.id)).where(
            Booking.status.in_(self.cancelled_statuses),
            Booking.start_at >= start,
            Booking.start_at < end,
        )
        if barber_id is not None:
            completed_stmt = completed_stmt.where(Booking.master_id == barber_id)
            cancelled_stmt = cancelled_stmt.where(Booking.master_id == barber_id)

        completed_row = (await session.execute(completed_stmt)).one()
        cancelled_count = (await session.execute(cancelled_stmt)).scalar_one()
        return {
            "total_income": money(completed_row[0]),
            "completed_appointments": int(completed_row[1] or 0),
            "unique_clients": int(completed_row[2] or 0),
            "cancelled_appointments": int(cancelled_count or 0),
        }

    async def _service_breakdown(
        self,
        session: AsyncSession,
        *,
        start: datetime,
        end: datetime,
        barber_id: int | None,
    ) -> list[StatisticsServiceItem]:
        stmt = (
            select(
                BarberService.id,
                BarberService.name,
                func.count(Booking.id).label("count"),
                func.coalesce(func.sum(self._revenue_expr()), 0).label("revenue"),
            )
            .join(BookingServiceItem, BookingServiceItem.booking_id == Booking.id)
            .join(BarberService, BarberService.id == BookingServiceItem.service_id)
            .where(
                Booking.status.in_(self.revenue_statuses),
                Booking.start_at >= start,
                Booking.start_at < end,
            )
            .group_by(BarberService.id, BarberService.name)
        )
        if barber_id is not None:
            stmt = stmt.where(Booking.master_id == barber_id)
        rows = (await session.execute(stmt)).all()
        return [self._service_item(row[0], row[1], row[2], row[3]) for row in rows]

    async def _workload_by_day(
        self,
        session: AsyncSession,
        *,
        start: datetime,
        end: datetime,
        barber_id: int | None,
    ) -> list[StatisticsWorkloadDayItem]:
        day_expr = cast(Booking.start_at, Date)
        stmt = (
            select(
                day_expr.label("day"),
                func.count(distinct(Booking.id)),
                func.coalesce(func.sum(self._revenue_expr()), 0),
            )
            .join(BookingServiceItem, BookingServiceItem.booking_id == Booking.id)
            .join(BarberService, BarberService.id == BookingServiceItem.service_id)
            .where(
                Booking.status.in_(self.revenue_statuses),
                Booking.start_at >= start,
                Booking.start_at < end,
            )
            .group_by(day_expr)
            .order_by(day_expr.asc())
        )
        if barber_id is not None:
            stmt = stmt.where(Booking.master_id == barber_id)
        rows = (await session.execute(stmt)).all()
        return [
            StatisticsWorkloadDayItem(date=str(row[0]), completed_appointments=int(row[1] or 0), revenue=money(row[2]))
            for row in rows
        ]

    async def _workload_by_week(
        self,
        session: AsyncSession,
        *,
        start: datetime,
        end: datetime,
        barber_id: int | None,
    ) -> list[StatisticsWorkloadWeekItem]:
        week_expr = func.extract("week", Booking.start_at)
        stmt = (
            select(
                week_expr.label("week"),
                func.count(distinct(Booking.id)),
                func.coalesce(func.sum(self._revenue_expr()), 0),
            )
            .join(BookingServiceItem, BookingServiceItem.booking_id == Booking.id)
            .join(BarberService, BarberService.id == BookingServiceItem.service_id)
            .where(
                Booking.status.in_(self.revenue_statuses),
                Booking.start_at >= start,
                Booking.start_at < end,
            )
            .group_by(week_expr)
            .order_by(week_expr.asc())
        )
        if barber_id is not None:
            stmt = stmt.where(Booking.master_id == barber_id)
        rows = (await session.execute(stmt)).all()
        return [
            StatisticsWorkloadWeekItem(week=int(row[0]), completed_appointments=int(row[1] or 0), revenue=money(row[2]))
            for row in rows
        ]

    async def _client_breakdown(
        self,
        session: AsyncSession,
        *,
        start: datetime,
        end: datetime,
        barber_id: int | None,
    ) -> StatisticsClientBreakdown:
        client_key = self._client_key()
        month_visit = case((Booking.start_at >= start, 1), else_=0)
        filters = [Booking.status.in_(self.revenue_statuses), Booking.start_at < end]
        if barber_id is not None:
            filters.append(Booking.master_id == barber_id)
        client_visits = (
            select(
                client_key.label("client_key"),
                func.min(Booking.start_at).label("first_visit"),
                func.sum(month_visit).label("month_visits"),
            )
            .where(*filters)
            .group_by(client_key)
            .subquery()
        )
        row = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(case((client_visits.c.first_visit >= start, 1), else_=0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((client_visits.c.first_visit < start, 1), else_=0)),
                        0,
                    ),
                ).where(client_visits.c.month_visits > 0)
            )
        ).one()
        return StatisticsClientBreakdown(new_clients=int(row[0] or 0), returning_clients=int(row[1] or 0))

    def _normalize_workload_days(
        self,
        year: int,
        month: int,
        days_in_month: int,
        rows: list[StatisticsWorkloadDayItem],
    ) -> list[StatisticsWorkloadDayItem]:
        by_date = {item.date: item for item in rows}
        return [
            by_date.get(
                f"{year:04d}-{month:02d}-{day:02d}",
                StatisticsWorkloadDayItem(
                    date=f"{year:04d}-{month:02d}-{day:02d}",
                    completed_appointments=0,
                    revenue=MONEY_ZERO,
                ),
            )
            for day in range(1, days_in_month + 1)
        ]

    def _service_item(self, service_id: int, service_name: str, count: int, revenue: Any) -> StatisticsServiceItem:
        return StatisticsServiceItem(
            service_id=service_id,
            service_name=service_name,
            count=int(count or 0),
            revenue=money(revenue),
        )

    def _client_key(self):
        return func.coalesce(cast(Booking.customer_id, String), Booking.customer_phone)

    def _revenue_expr(self):
        service_price = cast(BarberService.price, Numeric(12, 2))
        return case(
            (
                Booking.total_amount.is_not(None) & Booking.subtotal_amount.is_not(None) & (Booking.subtotal_amount > 0),
                service_price * cast(Booking.total_amount, Numeric(12, 2)) / cast(Booking.subtotal_amount, Numeric(12, 2)),
            ),
            else_=service_price,
        )
