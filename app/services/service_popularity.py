from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging

from sqlalchemy import bindparam, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import BaseService, BarberService, Booking, BookingServiceItem, BookingStatus

logger = logging.getLogger(__name__)

_POPULARITY_LOCK_ID = 7_130_459_202


def is_refresh_due(last_calculated_at: datetime | None, now: datetime, refresh_interval_days: int) -> bool:
    if last_calculated_at is None:
        return True
    if last_calculated_at.tzinfo is None:
        last_calculated_at = last_calculated_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return last_calculated_at <= now - timedelta(days=refresh_interval_days)


def calculate_popularity_ranks(booking_counts: dict[int, int]) -> dict[int, int]:
    popular_service_ids = [
        service_id
        for service_id, booking_count in sorted(
            booking_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if booking_count > 0
    ]
    return {
        service_id: rank
        for rank, service_id in enumerate(popular_service_ids, start=1)
    }


class ServicePopularityService:
    async def refresh_if_due(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> bool:
        current = now or datetime.now(UTC)
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            locked = (
                await session.execute(select(func.pg_try_advisory_xact_lock(_POPULARITY_LOCK_ID)))
            ).scalar_one()
            if not locked:
                await session.rollback()
                return False

        last_calculated_at = (
            await session.execute(select(func.max(BaseService.popularity_calculated_at)))
        ).scalar_one_or_none()
        if not force and not is_refresh_due(
            last_calculated_at,
            current,
            settings.service_popularity_refresh_interval_days,
        ):
            await session.rollback()
            return False

        try:
            await self.recalculate(session, now=current)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return True

    async def recalculate(self, session: AsyncSession, *, now: datetime) -> None:
        window_start = now - timedelta(days=settings.service_popularity_window_days)
        base_services = list(
            (
                await session.execute(
                    select(BaseService.id, BaseService.is_active)
                )
            ).all()
        )
        booking_rows = (
            await session.execute(
                select(
                    BarberService.base_service_id,
                    func.count(distinct(Booking.id)),
                )
                .select_from(BookingServiceItem)
                .join(Booking, Booking.id == BookingServiceItem.booking_id)
                .join(BarberService, BarberService.id == BookingServiceItem.service_id)
                .where(
                    BarberService.base_service_id.is_not(None),
                    Booking.status == BookingStatus.completed,
                    Booking.start_at >= window_start,
                    Booking.start_at <= now,
                )
                .group_by(BarberService.base_service_id)
            )
        ).all()
        completed_booking_counts = {
            int(base_service_id): int(booking_count)
            for base_service_id, booking_count in booking_rows
            if base_service_id is not None
        }
        eligible_counts = {
            base_service.id: completed_booking_counts.get(base_service.id, 0)
            for base_service in base_services
            if base_service.is_active
        }
        ranks = calculate_popularity_ranks(eligible_counts)

        cache_rows: list[dict[str, object]] = []
        for base_service in base_services:
            cache_rows.append(
                {
                    "_base_service_id": base_service.id,
                    "_popularity_rank": ranks.get(base_service.id) if base_service.is_active else None,
                    "_popularity_booking_count": completed_booking_counts.get(base_service.id, 0),
                    "_popularity_calculated_at": now,
                }
            )

        if not cache_rows:
            return

        base_service_table = BaseService.__table__
        cache_update = (
            base_service_table.update()
            .where(base_service_table.c.id == bindparam("_base_service_id"))
            .values(
                popularity_rank=bindparam("_popularity_rank"),
                popularity_booking_count_30d=bindparam("_popularity_booking_count"),
                popularity_calculated_at=bindparam("_popularity_calculated_at"),
                updated_at=base_service_table.c.updated_at,
            )
        )
        await session.execute(cache_update, cache_rows)


async def run_service_popularity_scheduler() -> None:
    interval_seconds = settings.service_popularity_check_interval_days * 24 * 60 * 60
    while True:
        try:
            async with AsyncSessionLocal() as session:
                refreshed = await service_popularity_service.refresh_if_due(session)
                if refreshed:
                    logger.info("Service popularity cache refreshed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Service popularity cache check failed")
        await asyncio.sleep(interval_seconds)


service_popularity_service = ServicePopularityService()
