from __future__ import annotations

import asyncio
import logging
from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.booking import Master, MasterAvailabilityWindow, MasterTimeBlock
from app.models.messaging import (
    Campaign,
    CampaignStatus,
    CampaignType,
    MasterScheduleReminder,
    MessageChannel,
)
from app.services.booking import CLOSED_WEEKDAYS, KYIV_TZ, WORK_END, WORK_START
from app.services.messaging import MessagingService, TelegramMessageProvider
from app.services.sms import SmsService


logger = logging.getLogger(__name__)
_SCHEDULER_LOCK_ID = 1_397_966_937

DEFAULT_TEMPLATE = (
    "Привіт, {master_name}! Нагадуємо відкрити робочий час на {month_name}. "
    "Зараз відкрито {coverage_percent}%."
)
DEFAULT_LOW_COVERAGE_MESSAGE = (
    "За можливості збільш доступність хоча б до {target_percent}%."
)
DEFAULT_FOLLOW_UP_PREFIX = "Повторне нагадування."
UKRAINIAN_MONTHS = (
    "січень",
    "лютий",
    "березень",
    "квітень",
    "травень",
    "червень",
    "липень",
    "серпень",
    "вересень",
    "жовтень",
    "листопад",
    "грудень",
)


@dataclass(frozen=True)
class ReminderDelivery:
    channel: MessageChannel
    provider_message_id: str | None


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    current = _month_start(value)
    if current.month == 12:
        return date(current.year + 1, 1, 1)
    return date(current.year, current.month + 1, 1)


def _metadata_int(
    metadata: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(metadata.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


def _metadata_float(
    metadata: dict[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(metadata.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


def _metadata_time(metadata: dict[str, Any], key: str, default: time) -> time:
    raw = str(metadata.get(key) or default.strftime("%H:%M"))
    try:
        hour, minute = (int(part) for part in raw.split(":", maxsplit=1))
    except (TypeError, ValueError):
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return time(hour=hour, minute=minute)


class MasterScheduleReminderService:
    def __init__(
        self,
        telegram_provider: TelegramMessageProvider | None = None,
        sms_service: SmsService | None = None,
    ) -> None:
        self.telegram_provider = telegram_provider or TelegramMessageProvider()
        self.sms_service = sms_service or SmsService()
        self.messaging = MessagingService()

    @staticmethod
    def initial_target_month(now: datetime, metadata: dict[str, Any]) -> date | None:
        local_now = now.astimezone(KYIV_TZ)
        current_month = _month_start(local_now.date())
        following_month = _next_month(current_month)
        last_day = following_month - timedelta(days=1)
        days_before_end = _metadata_int(
            metadata,
            "initial_days_before_month_end",
            3,
            minimum=0,
            maximum=14,
        )
        send_time = _metadata_time(metadata, "initial_send_time", time(10, 0))
        due_at = datetime.combine(last_day - timedelta(days=days_before_end), send_time, tzinfo=KYIV_TZ)
        if due_at <= local_now < datetime.combine(following_month, time.min, tzinfo=KYIV_TZ):
            return following_month
        return None

    @staticmethod
    def follow_up_target_month(now: datetime, metadata: dict[str, Any]) -> date | None:
        local_now = now.astimezone(KYIV_TZ)
        target_month = _month_start(local_now.date())
        send_time = _metadata_time(metadata, "follow_up_send_time", time(10, 0))
        window_days = _metadata_int(
            metadata,
            "follow_up_window_days",
            3,
            minimum=1,
            maximum=7,
        )
        due_at = datetime.combine(target_month, send_time, tzinfo=KYIV_TZ)
        window_end = datetime.combine(target_month + timedelta(days=window_days), time.min, tzinfo=KYIV_TZ)
        if due_at <= local_now < window_end:
            return target_month
        return None

    @staticmethod
    def month_possible_minutes(target_month: date) -> int:
        first = _month_start(target_month)
        days = monthrange(first.year, first.month)[1]
        work_minutes = (datetime.combine(first, WORK_END) - datetime.combine(first, WORK_START)).seconds // 60
        open_days = sum(
            1
            for day_number in range(1, days + 1)
            if date(first.year, first.month, day_number).weekday() not in CLOSED_WEEKDAYS
        )
        return open_days * work_minutes

    @staticmethod
    def _normalize(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=KYIV_TZ)
        return value.astimezone(KYIV_TZ)

    @classmethod
    def _merged_intervals(
        cls,
        items: Sequence[MasterAvailabilityWindow] | Sequence[MasterTimeBlock],
        start_at: datetime,
        end_at: datetime,
    ) -> list[tuple[datetime, datetime]]:
        intervals = sorted(
            (
                max(cls._normalize(item.start_at), start_at),
                min(cls._normalize(item.end_at), end_at),
            )
            for item in items
            if cls._normalize(item.start_at) < end_at and cls._normalize(item.end_at) > start_at
        )
        merged: list[tuple[datetime, datetime]] = []
        for interval_start, interval_end in intervals:
            if interval_start >= interval_end:
                continue
            if not merged or interval_start > merged[-1][1]:
                merged.append((interval_start, interval_end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], interval_end))
        return merged

    @classmethod
    def open_minutes(
        cls,
        target_month: date,
        windows: Sequence[MasterAvailabilityWindow],
        blocks: Sequence[MasterTimeBlock],
    ) -> int:
        first = _month_start(target_month)
        days = monthrange(first.year, first.month)[1]
        total_seconds = 0.0
        for day_number in range(1, days + 1):
            current = date(first.year, first.month, day_number)
            if current.weekday() in CLOSED_WEEKDAYS:
                continue
            day_start = datetime.combine(current, WORK_START, tzinfo=KYIV_TZ)
            day_end = datetime.combine(current, WORK_END, tzinfo=KYIV_TZ)
            open_intervals = cls._merged_intervals(windows, day_start, day_end)
            blocked_intervals = cls._merged_intervals(blocks, day_start, day_end)
            total_seconds += sum((end_at - start_at).total_seconds() for start_at, end_at in open_intervals)
            for open_start, open_end in open_intervals:
                total_seconds -= sum(
                    max(0.0, (min(open_end, block_end) - max(open_start, block_start)).total_seconds())
                    for block_start, block_end in blocked_intervals
                    if block_start < open_end and block_end > open_start
                )
        return max(0, round(total_seconds / 60))

    @classmethod
    def coverage_percent(
        cls,
        target_month: date,
        windows: Sequence[MasterAvailabilityWindow],
        blocks: Sequence[MasterTimeBlock],
    ) -> tuple[int, float]:
        minutes = cls.open_minutes(target_month, windows, blocks)
        possible = cls.month_possible_minutes(target_month)
        return minutes, round(minutes * 100 / possible, 1) if possible else 0.0

    @staticmethod
    def calendar_master_id(master: Master, masters_by_id: dict[int, Master]) -> int:
        current = master
        visited = {master.id}
        while current.booking_redirect_master_id is not None:
            target_id = current.booking_redirect_master_id
            if target_id in visited:
                raise ValueError(f"Booking redirect cycle for master {master.id}")
            visited.add(target_id)
            target = masters_by_id.get(target_id)
            if target is None:
                return target_id
            current = target
        return current.id

    async def _coverage(
        self,
        session: AsyncSession,
        calendar_master_id: int,
        target_month: date,
    ) -> tuple[int, float]:
        month_start = datetime.combine(_month_start(target_month), time.min, tzinfo=KYIV_TZ)
        month_end = datetime.combine(_next_month(target_month), time.min, tzinfo=KYIV_TZ)
        windows = (
            await session.execute(
                select(MasterAvailabilityWindow).where(
                    MasterAvailabilityWindow.master_id == calendar_master_id,
                    MasterAvailabilityWindow.start_at < month_end,
                    MasterAvailabilityWindow.end_at > month_start,
                )
            )
        ).scalars().all()
        blocks = (
            await session.execute(
                select(MasterTimeBlock).where(
                    MasterTimeBlock.master_id == calendar_master_id,
                    MasterTimeBlock.start_at < month_end,
                    MasterTimeBlock.end_at > month_start,
                )
            )
        ).scalars().all()
        return self.coverage_percent(target_month, windows, blocks)

    def build_message(
        self,
        campaign: Campaign,
        master: Master,
        target_month: date,
        coverage: float,
        *,
        follow_up: bool,
    ) -> str:
        metadata = campaign.metadata_json or {}
        low_threshold = _metadata_float(
            metadata,
            "low_coverage_percent",
            30.0,
            minimum=0.0,
            maximum=100.0,
        )
        target_percent = _metadata_float(
            metadata,
            "target_coverage_percent",
            50.0,
            minimum=0.0,
            maximum=100.0,
        )
        body = self.messaging.campaign_message_body(campaign) or DEFAULT_TEMPLATE
        variables = {
            "master_name": master.full_name_uk,
            "barber_name": master.full_name_uk,
            "month_name": UKRAINIAN_MONTHS[target_month.month - 1],
            "month": target_month.strftime("%m.%Y"),
            "coverage_percent": f"{coverage:g}",
            "low_coverage_percent": f"{low_threshold:g}",
            "target_percent": f"{target_percent:g}",
        }
        message = self.messaging.render_template(body, variables).strip()
        if coverage < low_threshold:
            low_body = str(metadata.get("low_coverage_message") or DEFAULT_LOW_COVERAGE_MESSAGE)
            message = f"{message}\n\n{self.messaging.render_template(low_body, variables).strip()}"
        if follow_up:
            prefix = str(metadata.get("follow_up_prefix") or DEFAULT_FOLLOW_UP_PREFIX).strip()
            message = f"{prefix}\n\n{message}"
        return message

    async def deliver(self, campaign: Campaign, master: Master, body: str) -> ReminderDelivery:
        fallback_to_sms = bool((campaign.metadata_json or {}).get("fallback_to_sms", True))
        telegram_error: Exception | None = None
        if campaign.channel == MessageChannel.telegram:
            if master.telegram_chat_id and settings.telegram_bot_token:
                try:
                    result = await self.telegram_provider.send_message(
                        destination=master.telegram_chat_id,
                        body=body,
                    )
                    return ReminderDelivery(MessageChannel.telegram, result.provider_message_id)
                except Exception as exc:
                    telegram_error = exc
                    logger.warning(
                        "Master schedule reminder Telegram delivery failed; trying SMS fallback",
                        extra={"master_id": master.id, "error": str(exc)},
                    )
            if not fallback_to_sms:
                raise telegram_error or RuntimeError("Master has no available Telegram destination")
        if campaign.channel in {MessageChannel.telegram, MessageChannel.sms} and master.phone:
            result = await self.sms_service.send_message(master.phone, body)
            return ReminderDelivery(MessageChannel.sms, result.provider_message_id)
        if telegram_error is not None:
            raise telegram_error
        raise RuntimeError("Master has neither an available Telegram chat nor a phone number")

    async def _campaign(self, session: AsyncSession) -> Campaign | None:
        return (
            await session.execute(
                select(Campaign)
                .options(selectinload(Campaign.template))
                .where(
                    Campaign.type == CampaignType.master_schedule_reminder,
                    Campaign.status == CampaignStatus.active,
                )
                .order_by(Campaign.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def process_due(self, session: AsyncSession, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(KYIV_TZ)).astimezone(KYIV_TZ)
        campaign = await self._campaign(session)
        if campaign is None:
            return 0
        metadata = campaign.metadata_json or {}
        initial_month = self.initial_target_month(current, metadata)
        follow_up_month = self.follow_up_target_month(current, metadata)
        if initial_month is None and follow_up_month is None:
            return 0

        masters = (await session.execute(select(Master).order_by(Master.id.asc()))).scalars().all()
        masters_by_id = {master.id: master for master in masters}
        recipients = [master for master in masters if master.is_active]
        target_months = {month for month in (initial_month, follow_up_month) if month is not None}
        records = (
            await session.execute(
                select(MasterScheduleReminder).where(
                    MasterScheduleReminder.campaign_id == campaign.id,
                    MasterScheduleReminder.target_month.in_(target_months),
                )
            )
        ).scalars().all()
        records_by_key = {(record.master_id, record.target_month): record for record in records}
        sent = 0

        if initial_month is not None:
            for master in recipients:
                key = (master.id, initial_month)
                record = records_by_key.get(key)
                if record is not None and record.initial_sent_at is not None:
                    continue
                if record is not None and int(record.initial_attempts or 0) >= settings.messaging_max_retry_attempts:
                    continue
                try:
                    calendar_master_id = self.calendar_master_id(master, masters_by_id)
                except ValueError as exc:
                    logger.warning("Master schedule reminder skipped", extra={"master_id": master.id, "error": str(exc)})
                    continue
                if record is None:
                    record = MasterScheduleReminder(
                        campaign_id=campaign.id,
                        master_id=master.id,
                        calendar_master_id=calendar_master_id,
                        target_month=initial_month,
                    )
                    session.add(record)
                    records_by_key[key] = record
                minutes, coverage = await self._coverage(session, calendar_master_id, initial_month)
                record.initial_attempts = int(record.initial_attempts or 0) + 1
                try:
                    delivery = await self.deliver(
                        campaign,
                        master,
                        self.build_message(campaign, master, initial_month, coverage, follow_up=False),
                    )
                except Exception as exc:
                    record.last_error = str(exc)
                    logger.warning(
                        "Master schedule reminder delivery failed",
                        extra={"master_id": master.id, "target_month": initial_month.isoformat(), "error": str(exc)},
                    )
                    continue
                record.initial_open_minutes = minutes
                record.initial_channel = delivery.channel
                record.initial_provider_message_id = delivery.provider_message_id
                record.initial_sent_at = current
                record.last_error = None
                sent += 1

        if follow_up_month is not None:
            for master in recipients:
                record = records_by_key.get((master.id, follow_up_month))
                if (
                    record is None
                    or record.initial_sent_at is None
                    or record.follow_up_sent_at is not None
                    or record.follow_up_skip_reason is not None
                    or int(record.follow_up_attempts or 0) >= settings.messaging_max_retry_attempts
                ):
                    continue
                calendar_master_id = record.calendar_master_id or self.calendar_master_id(master, masters_by_id)
                minutes, coverage = await self._coverage(session, calendar_master_id, follow_up_month)
                record.follow_up_evaluated_at = current
                if minutes > int(record.initial_open_minutes or 0):
                    record.follow_up_skip_reason = "availability_increased"
                    record.last_error = None
                    continue
                record.follow_up_attempts = int(record.follow_up_attempts or 0) + 1
                try:
                    delivery = await self.deliver(
                        campaign,
                        master,
                        self.build_message(campaign, master, follow_up_month, coverage, follow_up=True),
                    )
                except Exception as exc:
                    record.last_error = str(exc)
                    logger.warning(
                        "Master schedule follow-up delivery failed",
                        extra={"master_id": master.id, "target_month": follow_up_month.isoformat(), "error": str(exc)},
                    )
                    continue
                record.follow_up_channel = delivery.channel
                record.follow_up_provider_message_id = delivery.provider_message_id
                record.follow_up_sent_at = current
                record.last_error = None
                sent += 1

        await session.commit()
        return sent


master_schedule_reminder_service = MasterScheduleReminderService()


async def _try_scheduler_lock(session: AsyncSession) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        (
            await session.execute(select(func.pg_try_advisory_xact_lock(_SCHEDULER_LOCK_ID)))
        ).scalar_one()
    )


async def run_master_schedule_reminder_scheduler() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as session:
                if await _try_scheduler_lock(session):
                    await master_schedule_reminder_service.process_due(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Master schedule reminder scheduler iteration failed")
        await asyncio.sleep(settings.master_schedule_reminder_scheduler_interval_seconds)
