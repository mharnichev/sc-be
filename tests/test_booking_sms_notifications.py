from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.booking import KYIV_TZ
from app.services.booking_sms_notifications import BookingSmsNotification, BookingSmsNotificationService


class RecordingSmsService:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, phone: str, body: str, **_: object) -> None:
        self.sent.append((phone, body))


class FakeScalars:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def all(self) -> list[object]:
        return self.rows


class FakeExecuteResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> FakeScalars:
        return FakeScalars(self.rows)


class FakeSession:
    def __init__(self, execute_values: list[list[object]]) -> None:
        self.execute_values = execute_values
        self.committed = False

    async def execute(self, statement: object) -> FakeExecuteResult:
        rows = self.execute_values.pop(0) if self.execute_values else []
        return FakeExecuteResult(rows)

    async def commit(self) -> None:
        self.committed = True


def booking_sms_notification() -> BookingSmsNotification:
    return BookingSmsNotification(
        booking_id=42,
        master_name="Гліб",
        customer_name="Іван",
        customer_phone="+380501112233",
        start_at=datetime(2099, 1, 1, 10, 0, tzinfo=KYIV_TZ),
        end_at=datetime(2099, 1, 1, 11, 0, tzinfo=KYIV_TZ),
    )


@pytest.mark.anyio
async def test_booking_sms_confirmation_is_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "booking_sms_notifications_enabled", False)
    sms = RecordingSmsService()

    sent = await BookingSmsNotificationService(sms).send_booking_confirmation(booking_sms_notification())

    assert sent is False
    assert sms.sent == []


@pytest.mark.anyio
async def test_booking_sms_confirmation_uses_ukrainian_booking_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "booking_sms_notifications_enabled", True)
    monkeypatch.setattr(settings, "barbershop_name", "Soulcuts")
    sms = RecordingSmsService()

    sent = await BookingSmsNotificationService(sms).send_booking_confirmation(booking_sms_notification())

    assert sent is True
    assert sms.sent == [
        (
            "+380501112233",
            "Ви записані до майстра Гліб на 01.01.2099 о 10:00. Soulcuts",
        )
    ]


@pytest.mark.anyio
async def test_due_booking_sms_two_hour_reminders_use_soft_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "booking_sms_reminders_enabled", True)
    monkeypatch.setattr(settings, "booking_sms_two_hour_reminders_enabled", True)
    monkeypatch.setattr(settings, "barbershop_name", "Soulcuts")
    sms = RecordingSmsService()
    start_at = datetime.now(KYIV_TZ) + timedelta(hours=2, minutes=5)
    booking = SimpleNamespace(
        id=100,
        customer_name="Олена",
        customer_phone="+380671112233",
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        master=SimpleNamespace(full_name_uk="Андрій", full_name="Andrii"),
        sms_two_hour_reminder_sent_at=None,
    )
    session = FakeSession([[booking]])

    sent = await BookingSmsNotificationService(sms).send_due_booking_reminders(session)

    assert sent == 1
    assert session.committed is True
    assert booking.sms_two_hour_reminder_sent_at is not None
    assert sms.sent == [
        (
            "+380671112233",
            f"М'яке нагадування: сьогодні о {start_at:%H:%M} у вас візит до майстра Андрій. "
            "Будемо раді бачити вас у Soulcuts.",
        )
    ]
