from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import Booking, BookingServiceItem, BookingStatus
from app.services.booking import KYIV_TZ, WORK_END, apply_booking_status_update
from app.services.promotion import PromotionService
from app.services.repeat_booking import repeat_booking_service

logger = logging.getLogger(__name__)


def completion_bounds(now: datetime, starts_on: date) -> tuple[datetime, datetime]:
    """Most recent closed Kyiv workday, including recovery after downtime."""
    local = now.astimezone(KYIV_TZ)
    cutoff = datetime.combine(local.date(), WORK_END, tzinfo=KYIV_TZ)
    if local < cutoff:
        cutoff = datetime.combine(local.date() - timedelta(days=1), WORK_END, tzinfo=KYIV_TZ)
    return datetime.combine(starts_on, time.min, tzinfo=KYIV_TZ), cutoff


async def complete_closed_workday_bookings(session: AsyncSession, *, now: datetime | None = None) -> int:
    if not settings.booking_auto_completion_enabled:
        return 0
    at = now or datetime.now(KYIV_TZ)
    lower, cutoff = completion_bounds(at, settings.booking_auto_completion_starts_on)
    if cutoff < lower:
        return 0
    criteria = (
        Booking.status == BookingStatus.confirmed,
        Booking.start_at >= lower,
        Booking.end_at <= cutoff,
    )
    ids = (await session.execute(
        select(Booking.id).where(*criteria).order_by(Booking.start_at, Booking.id)
    )).scalars().all()
    await session.commit()
    completed = 0
    promotion = PromotionService()
    for index, booking_id in enumerate(ids):
        try:
            # Recheck under the same row lock used by manual cancellation/editing.
            # Commit each visit so workers never hold several customer/booking locks.
            booking = (await session.execute(
                select(Booking).where(Booking.id == booking_id, *criteria)
                .options(
                    selectinload(Booking.service),
                    selectinload(Booking.service_items).selectinload(BookingServiceItem.service),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if booking is None:
                await session.rollback()
                continue
            await promotion.sync_first_visit_status(session, booking, BookingStatus.completed)
            # Fresh timestamps keep recovered visits visible to review discovery.
            # Distinct ordered values let repeat offers identify the latest visit.
            completed_at = at - timedelta(microseconds=len(ids) - index - 1)
            apply_booking_status_update(booking, BookingStatus.completed, at=completed_at)
            await repeat_booking_service.mark_repeat_visit_completed(session, booking)
            await session.commit()
            completed += 1
        except Exception:
            await session.rollback()
            logger.exception("Automatic completion failed for booking %s", booking_id)
    return completed


async def run_booking_completion_scheduler() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as session:
                completed = await complete_closed_workday_bookings(session)
                if completed:
                    logger.info("Automatically completed %s bookings", completed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Booking completion scheduler iteration failed")
        await asyncio.sleep(settings.booking_auto_completion_interval_seconds)
