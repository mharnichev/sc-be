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
        master = SimpleNamespace(id=1, services=[], show_on_master_block=True)
        return master, master

    source_service = SimpleNamespace(id=1, duration_minutes=30, base_service_id=1)

    async def get_active_services(*_args):
        return [source_service]

    async def resolve_services(*_args, **_kwargs):
        return [source_service]

    async def slots(*_args, **_kwargs):
        return []

    booking.resolve_booking_master = resolve
    booking.get_active_services = get_active_services
    booking.ensure_master_provides_services = lambda *_args: None
    booking.resolve_booking_services_for_master = resolve_services
    booking.resolve_duration_minutes = lambda services, requested: requested
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

    async def get_active_services(self, _session, service_ids):
        return [item for item in self.services if item.id in service_ids]

    def ensure_master_provides_services(self, _master, _service_ids) -> None:
        return None

    async def resolve_booking_services_for_master(self, *_args, **_kwargs):
        return self.services

    @staticmethod
    def resolve_duration_minutes(services, requested):
        required = sum(item.duration_minutes for item in services)
        if requested != required:
            raise HTTPException(status_code=400, detail="duration_minutes must equal the selected services duration")
        return required

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
            duration_minutes=75,
            another_master_acceptable=False,
        ),
    )

    assert booking.calls[0] == (target + timedelta(days=1), 75)
    assert result.same_master[0].date == target + timedelta(days=1)
    assert result.same_master[0].duration_minutes == 75
    assert result.same_master[0].end_at - result.same_master[0].start_at == timedelta(minutes=75)


class AlternativesResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self) -> list[object]:
        return self.values


class AlternativesSession:
    def __init__(self, masters: list[object]) -> None:
        self.masters = masters
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return AlternativesResult(self.masters)


class RedirectAlternativesBookingDouble:
    def __init__(self, target: date) -> None:
        self.target = target
        self.source_service = SimpleNamespace(
            id=10,
            duration_minutes=60,
            base_service_id=100,
            is_active=True,
        )
        self.target_service = SimpleNamespace(
            id=20,
            duration_minutes=45,
            base_service_id=100,
            is_active=True,
        )
        self.requested_master = self._master(
            1,
            "Публічний обраний майстер",
            [self.source_service],
            redirect_master_id=2,
        )
        self.booking_master = self._master(
            2,
            "Прихований технічний календар",
            [self.target_service],
            is_public=False,
        )
        self.mismatched_candidate = self._master(
            3,
            "Майстер з іншою тривалістю",
            [SimpleNamespace(id=30, duration_minutes=45, base_service_id=100, is_active=True)],
        )
        self.redirected_candidate = self._master(
            4,
            "Публічна альтернатива",
            [SimpleNamespace(id=40, duration_minutes=60, base_service_id=100, is_active=True)],
            redirect_master_id=2,
        )
        self.slot_calls: list[tuple[int, list[int], date, int]] = []
        self.duration_service_ids: list[int] = []

    @staticmethod
    def _master(
        master_id: int,
        name: str,
        services: list[object],
        *,
        is_public: bool = True,
        redirect_master_id: int | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=master_id,
            services=services,
            is_active=True,
            show_on_master_block=is_public,
            booking_redirect_master_id=redirect_master_id,
            full_name=name,
            full_name_uk=name,
            photo_url=None,
            avatar_url=None,
            position_uk="Майстер",
        )

    def availability_horizon_end_date(self) -> date:
        return self.target + timedelta(days=3)

    async def resolve_booking_master(self, _session, master_id):
        assert master_id == self.requested_master.id
        return self.requested_master, self.booking_master

    async def get_active_services(self, _session, service_ids):
        assert service_ids == [self.source_service.id]
        return [self.source_service]

    def ensure_master_provides_services(self, master, service_ids) -> None:
        assert master is self.requested_master
        assert service_ids == [self.source_service.id]

    async def resolve_booking_services_for_master(
        self,
        _session,
        requested_master,
        booking_master,
        service_ids,
        *,
        source_services,
    ):
        assert requested_master is self.requested_master
        assert booking_master is self.booking_master
        assert service_ids == [self.source_service.id]
        assert source_services == [self.source_service]
        return [self.target_service]

    def resolve_duration_minutes(self, services, requested):
        self.duration_service_ids = [item.id for item in services]
        required = sum(item.duration_minutes for item in services)
        if requested != required:
            raise HTTPException(status_code=400, detail="duration_minutes must equal the selected services duration")
        return required

    @staticmethod
    def is_active_service(service) -> bool:
        return service.is_active

    @staticmethod
    def custom_service_key(service):
        return (service.id, service.duration_minutes)

    @staticmethod
    def is_closed_business_day(_day: date) -> bool:
        return False

    async def get_available_slots(
        self,
        _session,
        master_id,
        _service_id,
        day,
        *,
        service_ids,
        duration_minutes,
    ):
        self.slot_calls.append((master_id, list(service_ids), day, duration_minutes))
        is_same_master_day = master_id == self.requested_master.id and day == self.target + timedelta(days=1)
        is_other_master_day = master_id == self.redirected_candidate.id and day == self.target
        if not (is_same_master_day or is_other_master_day):
            return []
        start = datetime.combine(day, datetime.min.time(), tzinfo=KYIV).replace(hour=10)
        return [AvailableSlotResponse(start_at=start, end_at=start + timedelta(minutes=duration_minutes))]


@pytest.mark.anyio
async def test_redirected_alternatives_keep_public_source_identity_and_canonical_duration() -> None:
    target = date(2099, 1, 2)
    booking = RedirectAlternativesBookingDouble(target)
    session = AlternativesSession(
        [
            booking.mismatched_candidate,
            booking.redirected_candidate,
        ]
    )

    result = await BookingAlternativesService(booking).find(
        session,
        BookingAlternativesRequest(
            master_id=booking.requested_master.id,
            service_ids=[booking.source_service.id],
            desired_date=target,
            duration_minutes=60,
            another_master_acceptable=True,
        ),
    )

    assert booking.duration_service_ids == [booking.source_service.id]
    assert booking.slot_calls[0] == (
        booking.requested_master.id,
        [booking.source_service.id],
        target + timedelta(days=1),
        60,
    )
    assert result.same_master[0].master.id == booking.requested_master.id
    assert result.same_master[0].master.name == booking.requested_master.full_name_uk
    assert result.same_master[0].duration_minutes == 60
    assert [item.master.id for item in result.other_masters] == [booking.redirected_candidate.id]
    assert result.other_masters[0].master.name == booking.redirected_candidate.full_name_uk
    assert all(call[0] != booking.booking_master.id for call in booking.slot_calls)
    assert all(call[0] != booking.mismatched_candidate.id for call in booking.slot_calls)

    compiled = session.statement.compile()
    integer_params = {value for value in compiled.params.values() if type(value) is int}
    assert integer_params == {booking.requested_master.id}


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
