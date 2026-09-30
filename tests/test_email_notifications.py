from __future__ import annotations

from datetime import datetime

import pytest

from app.core.config import settings
from app.services.booking import KYIV_TZ
from app.services.email_notifications import EmailNotificationService, FeedbackEmail, NewBookingEmail


def new_booking_email() -> NewBookingEmail:
    return NewBookingEmail(
        booking_id=42,
        master_name="Gleb",
        master_email="gleb@example.com",
        service_name="Haircut",
        customer_name="Ivan",
        customer_phone="+380501112233",
        customer_comment="No beard trim",
        start_at=datetime(2099, 1, 1, 10, 0, tzinfo=KYIV_TZ),
        end_at=datetime(2099, 1, 1, 11, 0, tzinfo=KYIV_TZ),
    )


class RecordingEmailNotificationService(EmailNotificationService):
    def __init__(self) -> None:
        self.sent = False

    def _send_message(self, message):  # noqa: ANN001
        self.sent = True


@pytest.mark.anyio
async def test_new_booking_email_notification_is_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "email_notifications_enabled", False)
    service = RecordingEmailNotificationService()

    await service.send_new_booking_to_master(new_booking_email())

    assert service.sent is False


def test_new_booking_email_message_contains_booking_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "smtp_from_email", "bookings@example.com")
    monkeypatch.setattr(settings, "smtp_from_name", "Soulcuts")

    message = EmailNotificationService().build_new_booking_message(new_booking_email())
    body = message.get_content()

    assert message["Subject"] == "Нова запис #42"
    assert message["From"] == "Soulcuts <bookings@example.com>"
    assert message["To"] == "gleb@example.com"
    assert "Майстер: Gleb" in body
    assert "Послуга: Haircut" in body
    assert "Час: 01.01.2099 10:00 - 11:00" in body
    assert "Клієнт: Ivan" in body
    assert "Телефон: +380501112233" in body
    assert "Коментар: No beard trim" in body


def test_feedback_message_routes_to_configured_inbox_and_uses_reply_to(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "smtp_from_email", "shop@example.com")
    monkeypatch.setattr(settings, "feedback_recipient_email", "support@example.com")
    message = EmailNotificationService().build_feedback_message(
        FeedbackEmail("Jane", "jane@example.com", "order", "Where is my parcel?", 12)
    )
    assert message["To"] == "support@example.com"
    assert message["Reply-To"] == "jane@example.com"
    assert "#12" in message.get_content()


def test_smtp_recipient_rejection_is_treated_as_delivery_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    from app.services import email_notifications

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def starttls(self):
            return None
        def send_message(self, _message):
            return {"support@example.com": (550, b"mailbox unavailable")}

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_use_tls", False)
    monkeypatch.setattr(settings, "smtp_from_email", "shop@example.com")
    monkeypatch.setattr(settings, "feedback_recipient_email", "support@example.com")
    monkeypatch.setattr(email_notifications.smtplib, "SMTP", SMTP)
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        EmailNotificationService()._send_message(EmailNotificationService().build_feedback_message(
            FeedbackEmail("Jane", "jane@example.com", "general", "Hello")
        ))


def test_smtp_acceptance_with_no_refused_recipients_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import email_notifications

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def starttls(self):
            return None
        def send_message(self, _message):
            return {}

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_use_tls", False)
    monkeypatch.setattr(settings, "smtp_from_email", "shop@example.com")
    monkeypatch.setattr(settings, "feedback_recipient_email", "support@example.com")
    monkeypatch.setattr(email_notifications.smtplib, "SMTP", SMTP)
    EmailNotificationService()._send_message(EmailNotificationService().build_feedback_message(
        FeedbackEmail("Jane", "jane@example.com", "general", "Hello")
    ))
