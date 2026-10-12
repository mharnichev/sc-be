from __future__ import annotations
import hashlib
import hmac
import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from app.core.config import settings
from app.models.booking import Booking, BookingSource, BookingStatus
from app.models.telegram_booking_funnel import TelegramBookingFunnelEvent as Event
from app.schemas.telegram_booking_funnel import (TELEGRAM_CONTEXT_STEPS, TELEGRAM_FUNNEL_STEPS, TelegramBookingFunnelAggregate, TelegramFunnelStep, TelegramFunnelTransition)

logger = logging.getLogger(__name__)
KYIV = ZoneInfo("Europe/Kyiv")


def _attempt_hash(value: str) -> str:
    canonical = str(UUID(value))
    key = (settings.booking_funnel_hash_secret or settings.secret_key).encode()
    return hmac.new(key, f"telegram-booking-attempt:{canonical}".encode(), hashlib.sha256).hexdigest()


async def record_telegram_step(session, bot_session, step: str, *, booking_id: int | None = None) -> bool:
    """Record once inside the caller transaction; telemetry never owns its commit."""
    if step not in TELEGRAM_FUNNEL_STEPS + TELEGRAM_CONTEXT_STEPS:
        return False
    if (step == "booking_success") != (booking_id is not None):
        return False
    try:
        attempt_hash = _attempt_hash((bot_session.payload_json or {})["telegram_funnel_attempt_id"])
        # Flush caller changes first; nested rollback must affect only telemetry.
        async with session.begin_nested():
            if booking_id is not None:
                booking = await session.get(Booking, booking_id)
                if booking is None or booking.source != BookingSource.telegram:
                    return False
            result = await session.execute(insert(Event).values(attempt_hash=attempt_hash,
                step=step, booking_id=booking_id, occurred_at=datetime.now(UTC))
                .on_conflict_do_nothing().returning(Event.id))
            return result.scalar_one_or_none() is not None
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
    except Exception:
        # No identifiers or payloads in logs (including database exception details).
        logger.warning("Telegram booking telemetry could not be recorded")
        return False


def _percent(value: int, total: int) -> Decimal | None:
    return (Decimal(value) * 100 / Decimal(total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if total else None


async def aggregate_telegram_funnel(session, *, date_from: date, date_to: date) -> TelegramBookingFunnelAggregate:
    if date_to < date_from or (date_to - date_from).days >= 366:
        raise HTTPException(422, "Date range must contain 1 to 366 inclusive days")
    start = datetime.combine(date_from, time.min, KYIV)
    end = datetime.combine(date_to + timedelta(days=1), time.min, KYIV)
    cohort = select(Event.attempt_hash).where(Event.step == "booking_start", Event.occurred_at >= start, Event.occurred_at < end)
    # One grouped row per attempt; no personal data or raw event payload is loaded.
    milestones = [func.min(case((Event.step == step, Event.occurred_at))).label(step) for step in TELEGRAM_FUNNEL_STEPS]
    contexts = [func.max(case((Event.step == step, 1), else_=0)).label(step) for step in TELEGRAM_CONTEXT_STEPS]
    success = (Event.step == "booking_success") & (Booking.id.is_not(None)) & (Booking.source == BookingSource.telegram)
    rows = (await session.execute(select(*milestones, *contexts,
        func.max(case((success, 1), else_=0)).label("valid_success"),
        func.max(case((success & (Booking.status == BookingStatus.completed) & Booking.completed_at.is_not(None), 1), else_=0)).label("completed"))
        .outerjoin(Booking, Booking.id == Event.booking_id).where(Event.attempt_hash.in_(cohort))
        .group_by(Event.attempt_hash).execution_options(include_archived_bookings=True))).mappings().all()
    counts = dict.fromkeys(TELEGRAM_FUNNEL_STEPS, 0)
    contextual = dict.fromkeys(TELEGRAM_CONTEXT_STEPS, 0)
    completed = 0
    created = 0
    for row in rows:
        created += row["valid_success"]
        completed += row["completed"]
        for step in contextual:
            contextual[step] += row[step]
        previous = None
        for step in TELEGRAM_FUNNEL_STEPS:
            occurred = row[step]
            if occurred is None or (previous is not None and occurred < previous):
                break
            if step == "booking_success" and not row["valid_success"]:
                break
            counts[step] += 1
            previous = occurred
    transitions = [TelegramFunnelTransition(from_step=a, to_step=b,
        conversion_percent=_percent(counts[b], counts[a]), drop_off_count=counts[a]-counts[b],
        drop_off_percent=_percent(counts[a]-counts[b], counts[a]))
        for a, b in zip(TELEGRAM_FUNNEL_STEPS, TELEGRAM_FUNNEL_STEPS[1:])]
    return TelegramBookingFunnelAggregate(date_from=date_from, date_to=date_to,
        steps=[TelegramFunnelStep(step=k, count=v) for k,v in counts.items()], transitions=transitions,
        started=counts["booking_start"], succeeded=counts["booking_success"],
        conversion_percent=_percent(counts["booking_success"], counts["booking_start"]),
        completed_visits=completed, created_bookings=created, tracking_gap_successes=created-counts["booking_success"], contextual_steps=[TelegramFunnelStep(step=k,count=v) for k,v in contextual.items()])
