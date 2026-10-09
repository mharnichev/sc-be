"""Fresh transactional consent for guest and linked booking SMS sends."""
from datetime import UTC, datetime, timedelta

import pytest

from test_inventory_integration import anyio_backend, database  # noqa: F401
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.services import booking_sms_notifications as module
from app.services.booking_sms_notifications import BookingSmsNotification, BookingSmsNotificationService
from app.services.sms_queue import sms_request_context

pytestmark = pytest.mark.anyio


class Sms:
    def __init__(self):
        self.calls = []
    async def send_message(self, phone, body):
        self.calls.append((phone, body, sms_request_context.get()))


def notification():
    start = datetime(2026, 10, 9, 10, tzinfo=UTC)
    return BookingSmsNotification(booking_id=9901, master_name="Synthetic", customer_name="Synthetic guest",
        customer_phone="+380501112294", start_at=start, end_at=start + timedelta(minutes=30))


async def test_guest_without_preference_can_receive_booking_confirmation_and_reminder(database, monkeypatch):
    monkeypatch.setattr(module, "AsyncSessionLocal", database)
    sms = Sms()
    service = BookingSmsNotificationService(sms)
    assert await service.send_booking_confirmation(notification(), body="Synthetic confirmation") is True
    assert await service.send_booking_reminder(notification(), body="Synthetic reminder") is True
    assert len(sms.calls) == 2 and all(call[2].customer_id is None for call in sms.calls)


@pytest.mark.parametrize("restricted", ["do_not_contact", "transactional_opt_out", "marketing_only_opt_out"])
async def test_phone_matched_customer_preference_is_fresh_and_transactional_specific(database, monkeypatch, restricted):
    monkeypatch.setattr(module, "AsyncSessionLocal", database)
    async with database() as session:
        customer = Customer(name="Synthetic phone matched customer", phone=notification().customer_phone, is_active=False)
        session.add(customer)
        await session.flush()
        session.add(ClientCommunicationPreference(customer_id=customer.id,
            marketing_consent=ConsentStatus.opted_out,
            transactional_consent=ConsentStatus.opted_out if restricted == "transactional_opt_out" else ConsentStatus.opted_in,
            do_not_contact=restricted == "do_not_contact"))
        await session.commit()
        customer_id = customer.id
    sms = Sms()
    service = BookingSmsNotificationService(sms)
    allowed = restricted == "marketing_only_opt_out"
    assert await service.send_booking_confirmation(notification(), body="Synthetic confirmation") is allowed
    assert await service.send_booking_reminder(notification(), body="Synthetic reminder") is allowed
    assert len(sms.calls) == (2 if allowed else 0)
    if allowed:
        assert all(call[2].customer_id == customer_id for call in sms.calls)
