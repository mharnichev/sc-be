from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.config import settings
from app.models.booking import Master, MasterAvailabilityWindow, MasterTimeBlock
from app.models.messaging import (
    Campaign,
    CampaignStatus,
    CampaignType,
    MasterScheduleReminder,
    MessageChannel,
    MessagePurpose,
    MessageTemplate,
)
from app.services.booking import KYIV_TZ
from app.services.master_schedule_reminders import MasterScheduleReminderService
from app.services.messaging import ProviderSendResult, TelegramMessageProvider
from app.services.sms import SmsSendResult, SmsService


def at(day: int, hour: int, minute: int = 0, *, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=KYIV_TZ)


def campaign() -> Campaign:
    item = Campaign(
        id=7,
        name="Нагадування майстрам про графік",
        type=CampaignType.master_schedule_reminder,
        status=CampaignStatus.active,
        channel=MessageChannel.telegram,
        purpose=MessagePurpose.transactional,
        metadata_json={
            "recipient": "master",
            "initial_days_before_month_end": 3,
            "initial_send_time": "10:00",
            "follow_up_send_time": "10:00",
            "follow_up_window_days": 3,
            "low_coverage_percent": 30,
            "target_coverage_percent": 50,
            "fallback_to_sms": True,
        },
    )
    item.template = MessageTemplate(
        name="Нагадування майстрам про графік",
        channel=MessageChannel.telegram,
        body=(
            "Привіт, {master_name}! Нагадуємо відкрити робочий час на {month_name}. "
            "Зараз відкрито {coverage_percent}%."
        ),
        is_active=True,
    )
    return item


def master() -> Master:
    return Master(
        id=11,
        full_name="Олег",
        last_name="Коваль",
        telegram_chat_id="telegram-11",
        phone="+380671112233",
        is_active=True,
    )


class RecordingTelegram(TelegramMessageProvider):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, *, destination: str, body: str, reply_markup=None) -> ProviderSendResult:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.sent.append((destination, body))
        return ProviderSendResult(provider_message_id="tg-1", raw_response={"ok": True})


class RecordingSms(SmsService):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, phone: str, body: str, **_kwargs) -> SmsSendResult:
        self.sent.append((phone, body))
        return SmsSendResult(provider_message_id="sms-1", raw_response={"ok": True})


class ScalarResult:
    def __init__(self, values):
        self.values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self.values

    def scalar_one_or_none(self):
        return self.values[0] if self.values else None


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.added: list[object] = []
        self.commits = 0

    async def execute(self, _statement):
        return ScalarResult(self.responses.pop(0))

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1


def test_schedule_windows_cover_end_and_beginning_of_month() -> None:
    metadata = campaign().metadata_json

    assert MasterScheduleReminderService.initial_target_month(
        datetime(2026, 8, 28, 9, 59, tzinfo=KYIV_TZ), metadata
    ) is None
    assert MasterScheduleReminderService.initial_target_month(
        datetime(2026, 8, 28, 10, 0, tzinfo=KYIV_TZ), metadata
    ) == date(2026, 9, 1)
    assert MasterScheduleReminderService.follow_up_target_month(
        datetime(2026, 9, 1, 10, 0, tzinfo=KYIV_TZ), metadata
    ) == date(2026, 9, 1)
    assert MasterScheduleReminderService.follow_up_target_month(
        datetime(2026, 9, 4, 0, 0, tzinfo=KYIV_TZ), metadata
    ) is None


def test_coverage_uses_open_minutes_and_subtracts_overlapping_blocks() -> None:
    windows = [MasterAvailabilityWindow(master_id=11, start_at=at(1, 8), end_at=at(1, 20))]
    blocks = [
        MasterTimeBlock(master_id=11, start_at=at(1, 12), end_at=at(1, 14)),
        MasterTimeBlock(master_id=11, start_at=at(1, 13), end_at=at(1, 15)),
    ]

    minutes, coverage = MasterScheduleReminderService.coverage_percent(date(2026, 9, 1), windows, blocks)

    assert minutes == 9 * 60
    assert coverage == round(minutes * 100 / MasterScheduleReminderService.month_possible_minutes(date(2026, 9, 1)), 1)


def test_low_coverage_message_recommends_fifty_percent_and_follow_up_is_explicit() -> None:
    service = MasterScheduleReminderService()

    initial = service.build_message(campaign(), master(), date(2026, 9, 1), 29.9, follow_up=False)
    follow_up = service.build_message(campaign(), master(), date(2026, 9, 1), 35.0, follow_up=True)

    assert "Олег Коваль" in initial
    assert "вересень" in initial
    assert "хоча б до 50%" in initial
    assert "хоча б до 50%" not in follow_up
    assert follow_up.startswith("Повторне нагадування.")


@pytest.mark.anyio
async def test_delivery_falls_back_from_telegram_to_sms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    telegram = RecordingTelegram(fail=True)
    sms = RecordingSms()

    result = await MasterScheduleReminderService(telegram, sms).deliver(campaign(), master(), "Нагадування")

    assert result.channel == MessageChannel.sms
    assert sms.sent == [("+380671112233", "Нагадування")]


@pytest.mark.anyio
async def test_initial_scheduler_delivery_records_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    telegram = RecordingTelegram()
    session = SequenceSession(
        [
            [campaign()],
            [master()],
            [],
            [MasterAvailabilityWindow(master_id=11, start_at=at(1, 8), end_at=at(1, 20))],
            [],
        ]
    )

    sent = await MasterScheduleReminderService(telegram, RecordingSms()).process_due(
        session,
        now=datetime(2026, 8, 28, 10, 0, tzinfo=KYIV_TZ),
    )

    reminder = session.added[0]
    assert sent == 1
    assert isinstance(reminder, MasterScheduleReminder)
    assert reminder.target_month == date(2026, 9, 1)
    assert reminder.initial_open_minutes == 12 * 60
    assert reminder.initial_channel == MessageChannel.telegram
    assert reminder.initial_sent_at == datetime(2026, 8, 28, 10, 0, tzinfo=KYIV_TZ)
    assert "хоча б до 50%" in telegram.sent[0][1]
    assert session.commits == 1


@pytest.mark.anyio
async def test_follow_up_is_skipped_after_master_increases_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    reminder = MasterScheduleReminder(
        campaign_id=7,
        master_id=11,
        calendar_master_id=11,
        target_month=date(2026, 9, 1),
        initial_open_minutes=12 * 60,
        initial_attempts=1,
        initial_sent_at=datetime(2026, 8, 28, 10, 0, tzinfo=KYIV_TZ),
        follow_up_attempts=0,
    )
    telegram = RecordingTelegram()
    session = SequenceSession(
        [
            [campaign()],
            [master()],
            [reminder],
            [
                MasterAvailabilityWindow(master_id=11, start_at=at(1, 8), end_at=at(1, 20)),
                MasterAvailabilityWindow(master_id=11, start_at=at(2, 8), end_at=at(2, 20)),
            ],
            [],
        ]
    )

    sent = await MasterScheduleReminderService(telegram, RecordingSms()).process_due(
        session,
        now=datetime(2026, 9, 1, 10, 0, tzinfo=KYIV_TZ),
    )

    assert sent == 0
    assert reminder.follow_up_skip_reason == "availability_increased"
    assert telegram.sent == []
    assert session.commits == 1


@pytest.mark.anyio
async def test_follow_up_is_repeated_when_master_did_not_increase_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", "token")
    reminder = MasterScheduleReminder(
        campaign_id=7,
        master_id=11,
        calendar_master_id=11,
        target_month=date(2026, 9, 1),
        initial_open_minutes=12 * 60,
        initial_attempts=1,
        initial_sent_at=datetime(2026, 8, 28, 10, 0, tzinfo=KYIV_TZ),
        follow_up_attempts=0,
    )
    telegram = RecordingTelegram()
    session = SequenceSession(
        [
            [campaign()],
            [master()],
            [reminder],
            [MasterAvailabilityWindow(master_id=11, start_at=at(1, 8), end_at=at(1, 20))],
            [],
        ]
    )

    sent = await MasterScheduleReminderService(telegram, RecordingSms()).process_due(
        session,
        now=datetime(2026, 9, 1, 10, 0, tzinfo=KYIV_TZ),
    )

    assert sent == 1
    assert reminder.follow_up_sent_at == datetime(2026, 9, 1, 10, 0, tzinfo=KYIV_TZ)
    assert reminder.follow_up_attempts == 1
    assert telegram.sent[0][1].startswith("Повторне нагадування.")
