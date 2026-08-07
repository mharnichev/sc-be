from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.services.booking_alternatives import BookingAlternativesService
from app.schemas.booking import AvailableSlotResponse

KYIV = ZoneInfo("Europe/Kyiv")


def test_request_rejects_duplicate_or_invalid_service_ids() -> None:
    with pytest.raises(ValidationError):
        BookingAlternativesRequest(master_id=1, service_ids=[1, 1], desired_date=date.today(), duration_minutes=30)
    with pytest.raises(ValidationError):
        BookingAlternativesRequest(master_id=0, service_ids=[1], desired_date=date.today(), duration_minutes=30)


def test_public_slot_keeps_kyiv_date_boundary() -> None:
    service = BookingAlternativesService()
    master = SimpleNamespace(id=1, full_name_uk="Майстер", photo_url=None, avatar_url=None, position_uk="Майстер")
    start = datetime(2026, 8, 6, 0, 15, tzinfo=KYIV)
    slot = service._public_master(master)
    assert slot.name == "Майстер"
    assert start.astimezone(KYIV).date() == date(2026, 8, 6)


def test_matching_services_requires_every_selected_service() -> None:
    service = BookingAlternativesService()
    selected = [
        SimpleNamespace(id=10, base_service_id=1, is_active=True),
        SimpleNamespace(id=11, base_service_id=2, is_active=True),
    ]
    candidate = SimpleNamespace(services=[SimpleNamespace(id=20, base_service_id=1, is_active=True)])
    assert service._matching_service_ids(candidate, selected) is None


@pytest.mark.anyio
async def test_no_alternatives_is_an_empty_response(monkeypatch) -> None:
    target = datetime.now(KYIV).date() + timedelta(days=1)
    booking = SimpleNamespace(
        availability_horizon_end_date=lambda: target + timedelta(days=30),
        resolve_booking_master=lambda *_: None,
    )
    service = BookingAlternativesService(booking)
    payload = BookingAlternativesRequest(
        master_id=1, service_ids=[1], desired_date=target, duration_minutes=30,
        another_master_acceptable=False,
    )

    async def resolve(*_args):
        master = SimpleNamespace(id=1, services=[])
        return master, master
    async def resolve_services(*_args):
        return [SimpleNamespace(id=1, duration_minutes=30, base_service_id=1)]
    async def slots(*_args, **_kwargs):
        return []
    booking.resolve_booking_master = resolve
    booking.resolve_booking_services_for_master = resolve_services
    booking.is_closed_business_day = lambda _day: False
    booking.get_available_slots = slots
    result = await service.find(SimpleNamespace(execute=None), payload)
    assert result.same_master == []
    assert result.other_masters == []


class AlternativesBookingDouble:
    def __init__(self, target: date) -> None:
        self.target = target
        self.calls: list[tuple[date, int]] = []
        self.master = SimpleNamespace(
            id=1,
            services=[],
            show_on_master_block=True,
            full_name_uk="Іван",
            photo_url=None,
            avatar_url=None,
            position_uk="Майстер",
        )
        self.services = [
            SimpleNamespace(
                id=10,
                duration_minutes=30,
                base_service_id=100,
                is_active=True,
            ),
            SimpleNamespace(
                id=11,
                duration_minutes=45,
                base_service_id=101,
                is_active=True,
            ),
        ]
        self.master.services = self.services

    def availability_horizon_end_date(self) -> date:
        return self.target + timedelta(days=5)

    async def resolve_booking_master(self, _session, _master_id):
        return self.master, self.master

    async def resolve_booking_services_for_master(self, *_args):
        return self.services

    def is_closed_business_day(self, _day: date) -> bool:
        return False

    async def get_available_slots(
        self,
        _session,
        _master_id,
        _service_id,
        day,
        *,
        service_ids,
        duration_minutes,
    ):
        self.calls.append((day, duration_minutes))
        if day != self.target + timedelta(days=1):
            return []
        start = datetime.combine(day, datetime.min.time(), tzinfo=KYIV).replace(hour=10)
        return [AvailableSlotResponse(start_at=start, end_at=start + timedelta(minutes=duration_minutes))]


@pytest.mark.anyio
async def test_same_master_alternative_starts_after_desired_date_and_uses_total_duration() -> None:
    target = date(2099, 1, 2)
    booking = AlternativesBookingDouble(target)
    result = await BookingAlternativesService(booking).find(
        None,
        BookingAlternativesRequest(
            master_id=1,
            service_ids=[10, 11],
            desired_date=target,
            duration_minutes=90,
            another_master_acceptable=False,
        ),
    )

    assert booking.calls[0] == (target + timedelta(days=1), 90)
    assert result.same_master[0].date == target + timedelta(days=1)
    assert result.same_master[0].duration_minutes == 90
    assert result.same_master[0].end_at - result.same_master[0].start_at == timedelta(minutes=90)


@pytest.mark.anyio
async def test_private_selected_master_is_not_exposed_as_an_alternative() -> None:
    target = date(2099, 1, 2)
    booking = AlternativesBookingDouble(target)
    booking.master.show_on_master_block = False

    with pytest.raises(HTTPException) as exc_info:
        await BookingAlternativesService(booking).find(
            None,
            BookingAlternativesRequest(
                master_id=1,
                service_ids=[10, 11],
                desired_date=target,
                duration_minutes=90,
            ),
        )

    assert exc_info.value.status_code == 404
