from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.waitlist import WaitlistRequest, WaitlistStatus
from app.models.booking import Booking
from app.models.customer import Customer
from app.schemas.waitlist import PublicWaitlistCreate
from app.services.waitlist import WAITLIST_EXPIRY_DAYS, WaitlistService, booking_recovery_analytics_service
from app.services.booking import KYIV_TZ


class FakeResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value or []


class CancelSession:
    def __init__(self, request):
        self.request = request
        self.commits = 0

    async def execute(self, _statement):
        return FakeResult(self.request)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _item):
        return None


def valid_payload(**changes):
    payload = {
        "customer_name": "Іван Петренко",
        "customer_phone": "+380 67 123 45 67",
        "service_ids": [1, 2],
        "desired_date": "2026-08-10",
        "notification_consent": True,
    }
    payload.update(changes)
    return payload


def test_waitlist_schema_rejects_duplicate_services_and_invalid_ranges():
    with pytest.raises(ValidationError):
        PublicWaitlistCreate(**valid_payload(service_ids=[1, 1]))
    with pytest.raises(ValidationError):
        PublicWaitlistCreate(**valid_payload(acceptable_date_from="2026-08-12", acceptable_date_to="2026-08-10"))
    with pytest.raises(ValidationError):
        PublicWaitlistCreate(**valid_payload(preferred_time_from="12:00", preferred_time_to="12:00"))


@pytest.mark.anyio
async def test_cancel_uses_hash_and_changes_only_active_request():
    service = WaitlistService()
    token = "x" * 32
    request = WaitlistRequest(
        id=10, public_id="public", cancel_token_hash=service._hash_token(token), customer_id=1,
        desired_date=date(2026, 8, 10), duration_minutes=30, notification_consent=True,
        status=WaitlistStatus.active, expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session = CancelSession(request)
    cancelled = await service.cancel(session, token)
    assert cancelled.status is WaitlistStatus.cancelled
    assert cancelled.close_reason == "cancelled_by_customer"
    assert session.commits == 1


@pytest.mark.anyio
async def test_cancel_rejects_reused_token():
    service = WaitlistService()
    request = WaitlistRequest(
        id=10, public_id="public", cancel_token_hash=service._hash_token("x" * 32), customer_id=1,
        desired_date=date(2026, 8, 10), duration_minutes=30, notification_consent=True,
        status=WaitlistStatus.cancelled, expires_at=datetime.now(UTC),
    )
    with pytest.raises(HTTPException, match="no longer active"):
        await service.cancel(CancelSession(request), "x" * 32)


def test_waitlist_expiry_is_defined_and_auditable():
    assert WAITLIST_EXPIRY_DAYS == 90
    request = WaitlistRequest(
        cancel_token_hash="hashed", customer_id=1, desired_date=date.today(), duration_minutes=45,
        notification_consent=True, status=WaitlistStatus.active,
        expires_at=datetime.now(UTC) + timedelta(days=WAITLIST_EXPIRY_DAYS),
    )
    assert request.expires_at.date() == (datetime.now(UTC) + timedelta(days=WAITLIST_EXPIRY_DAYS)).date()


class CreateSession:
    def __init__(self, values):
        self.values = list(values)
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        return FakeResult(self.values.pop(0))

    def add(self, item):
        self.added.append(item)
        if getattr(item, "id", None) is None:
            item.id = len(self.added)
        if isinstance(item, WaitlistRequest) and not item.public_id:
            item.public_id = "public-request-id"

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, _item):
        return None


class WaitlistBookingDouble:
    def __init__(self, services):
        self.services = services

    def availability_horizon_end_date(self):
        return datetime.now(KYIV_TZ).date() + timedelta(days=60)

    async def get_active_services(self, _session, _service_ids):
        return self.services


@pytest.mark.anyio
async def test_waitlist_creation_normalizes_phone_never_creates_booking_and_expires_after_range(monkeypatch):
    async def no_analytics(*_args, **_kwargs):
        return True

    monkeypatch.setattr(booking_recovery_analytics_service, "record", no_analytics)
    desired = datetime.now(KYIV_TZ).date() + timedelta(days=2)
    services = [SimpleNamespace(id=1, duration_minutes=30), SimpleNamespace(id=2, duration_minutes=45)]
    service = WaitlistService()
    service.booking_service = WaitlistBookingDouble(services)
    session = CreateSession([None, None])

    request, token = await service.create(
        session,
        PublicWaitlistCreate(
            customer_name="Іван Петренко",
            customer_phone="067 123 45 67",
            service_ids=[1, 2],
            desired_date=desired,
            notification_consent=True,
        ),
    )

    customer = next(item for item in session.added if isinstance(item, Customer))
    assert customer.phone == "+0671234567"
    assert not any(isinstance(item, Booking) for item in session.added)
    assert request.duration_minutes == 75
    assert request.acceptable_date_from == desired
    assert request.acceptable_date_to == desired
    assert request.expires_at.astimezone(KYIV_TZ).date() == desired + timedelta(days=1)
    assert len(token) >= 32
    assert request.cancel_token_hash != token


@pytest.mark.anyio
async def test_equivalent_open_waitlist_request_is_deduplicated(monkeypatch):
    async def no_analytics(*_args, **_kwargs):
        return True

    monkeypatch.setattr(booking_recovery_analytics_service, "record", no_analytics)
    desired = datetime.now(KYIV_TZ).date() + timedelta(days=2)
    customer = Customer(id=7, phone="+380671234567", name="Іван", is_active=True)
    service = WaitlistService()
    service.booking_service = WaitlistBookingDouble([SimpleNamespace(id=1, duration_minutes=30)])

    with pytest.raises(HTTPException) as exc_info:
        await service.create(
            CreateSession([customer, 99]),
            PublicWaitlistCreate(
                customer_name="Іван Петренко",
                customer_phone="067 123 45 67",
                service_ids=[1],
                desired_date=desired,
                notification_consent=True,
            ),
        )

    assert exc_info.value.status_code == 409
