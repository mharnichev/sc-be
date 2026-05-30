from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.api.v1.routes import bookings as booking_routes
from app.api.v1.routes import customers as customer_routes
from app.api.v1.routes.bookings import (
    admin_delete_booking,
    admin_update_booking,
    delete_my_booking,
    delete_my_time_block,
    update_my_booking,
    update_my_booking_status,
)
from app.models.booking import BarberService, BaseService, Booking, BookingStatus, Master, MasterPosition
from app.models.customer import Customer
from app.models.upload import Upload
from app.schemas.booking import (
    BarberServiceCreate,
    BarberServiceUpdate,
    BaseServiceCreate,
    BookingStatusUpdate,
    BookingUpdate,
    CustomerBookingStatsItem,
    MasterResponse,
    MasterTimeBlockCreate,
    PublicBookingCreate,
)
from app.services.booking import BookingServiceLayer
from app.utils.seed_services import DEFAULT_BASE_SERVICES, seed_base_services

KYIV_TZ = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_master_response_includes_localized_name_and_position() -> None:
    now = datetime.now(tz=KYIV_TZ)
    master = Master(
        id=7,
        full_name="Гліб",
        last_name="Гарнічев",
        first_name_en="Gleb",
        last_name_en="Garnichev",
        position=MasterPosition.senior_master,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    response = MasterResponse.model_validate(master)

    assert response.first_name_uk == "Гліб"
    assert response.last_name_uk == "Гарнічев"
    assert response.full_name_uk == "Гліб Гарнічев"
    assert response.full_name_en == "Gleb Garnichev"
    assert response.position == MasterPosition.senior_master
    assert response.position_uk == "Старший Майстер"
    assert response.position_en == "Senior Master"


class SlotService(BookingServiceLayer):
    def __init__(self, bookings=None, blocks=None):
        self.master = SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)])
        self.booking_service = SimpleNamespace(id=1, is_active=True, duration_minutes=60)
        self.bookings = bookings or []
        self.blocks = blocks or []

    async def get_active_master_with_services(self, session, master_id):
        return self.master

    async def get_active_service(self, session, service_id):
        return self.booking_service

    async def list_busy_bookings(self, session, master_id, start_at, end_at):
        return self.bookings

    async def list_time_blocks(self, session, master_id, start_at, end_at):
        return self.blocks


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2099, 1, 1, hour, minute, tzinfo=KYIV_TZ)


def monday_at(hour: int, minute: int = 0) -> datetime:
    return datetime(2099, 1, 5, hour, minute, tzinfo=KYIV_TZ)


@pytest.mark.anyio
async def test_customer_can_view_available_barber_slots() -> None:
    slots = await SlotService().get_available_slots(None, master_id=1, service_id=1, target_date=date(2099, 1, 1))

    assert slots[0].start_at == at(8)
    assert slots[0].end_at == at(9)
    assert slots[-1].start_at == at(19)
    assert slots[-1].end_at == at(20)


@pytest.mark.anyio
async def test_available_slots_use_barber_service_duration() -> None:
    slot_service = SlotService()
    slot_service.booking_service.duration_minutes = 90

    slots = await slot_service.get_available_slots(None, master_id=1, service_id=1, target_date=date(2099, 1, 1))

    assert slots[0].start_at == at(8)
    assert slots[0].end_at == at(9, 30)
    assert slots[-1].start_at == at(18, 30)
    assert slots[-1].end_at == at(20)


@pytest.mark.anyio
async def test_available_slots_can_use_custom_booking_duration() -> None:
    slots = await SlotService().get_available_slots(
        None,
        master_id=1,
        service_id=1,
        duration_minutes=120,
        target_date=date(2099, 1, 1),
    )

    assert slots[0].start_at == at(8)
    assert slots[0].end_at == at(10)
    assert slots[-1].start_at == at(18)
    assert slots[-1].end_at == at(20)


@pytest.mark.anyio
async def test_available_slots_use_combined_service_duration() -> None:
    slot_service = SlotService()
    slot_service.master.services = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

    async def get_active_service(_session, service_id):
        durations = {1: 60, 2: 30}
        return SimpleNamespace(id=service_id, is_active=True, duration_minutes=durations[service_id])

    slot_service.get_active_service = get_active_service

    slots = await slot_service.get_available_slots(
        None,
        master_id=1,
        service_id=None,
        service_ids=[1, 2],
        target_date=date(2099, 1, 1),
    )

    assert slots[0].start_at == at(8)
    assert slots[0].end_at == at(9, 30)
    assert slots[-1].start_at == at(18, 30)
    assert slots[-1].end_at == at(20)


@pytest.mark.anyio
async def test_barbershop_is_closed_on_mondays_for_slots() -> None:
    slots = await SlotService().get_available_slots(None, master_id=1, service_id=1, target_date=date(2099, 1, 5))

    assert slots == []


@pytest.mark.anyio
async def test_existing_booking_removes_overlapping_slots() -> None:
    existing = SimpleNamespace(start_at=at(10), end_at=at(11))

    slots = await SlotService(bookings=[existing]).get_available_slots(
        None,
        master_id=1,
        service_id=1,
        target_date=date(2099, 1, 1),
    )

    slot_starts = {slot.start_at for slot in slots}
    assert at(9, 15) not in slot_starts
    assert at(10) not in slot_starts
    assert at(11) in slot_starts


@pytest.mark.anyio
async def test_time_block_removes_overlapping_slots() -> None:
    block = SimpleNamespace(start_at=at(14), end_at=at(15))

    slots = await SlotService(blocks=[block]).get_available_slots(
        None,
        master_id=1,
        service_id=1,
        target_date=date(2099, 1, 1),
    )

    slot_starts = {slot.start_at for slot in slots}
    assert at(13, 15) not in slot_starts
    assert at(14) not in slot_starts
    assert at(15) in slot_starts


def test_cannot_create_booking_outside_working_hours() -> None:
    service = BookingServiceLayer()

    with pytest.raises(HTTPException) as exc_info:
        service.ensure_within_working_hours(at(19, 30), at(20, 30))

    assert exc_info.value.status_code == 400


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeExecuteResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def first(self):
        return self.value

    def all(self):
        return self.value

    def scalars(self):
        return FakeScalarList(self.value)


class FakeScalarList:
    def __init__(self, value):
        self.value = value

    def all(self):
        return self.value


class FakeSession:
    def __init__(self, *, master=None, get_value=None, execute_values=None):
        self.master = master or SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)])
        self.get_value = get_value
        self.execute_values = list(execute_values or [])
        self.added = None
        self.added_items = []
        self.deleted = None
        self.committed = False
        self.flushed = False

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, _statement):
        if self.execute_values:
            return FakeExecuteResult(self.execute_values.pop(0))
        return FakeExecuteResult(self.master)

    async def get(self, _model, _entity_id):
        return self.get_value

    def add(self, instance):
        self.added = instance
        self.added_items.append(instance)
        if getattr(instance, "id", None) is None:
            instance.id = 1
        if getattr(instance, "created_at", None) is None:
            instance.created_at = datetime.now(tz=KYIV_TZ)
        if getattr(instance, "updated_at", None) is None:
            instance.updated_at = instance.created_at

    async def refresh(self, _instance, attribute_names=None):
        return None

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def delete(self, instance):
        self.deleted = instance


class CreateBookingService(BookingServiceLayer):
    def __init__(self, *, conflict_detail: str | None = None):
        super().__init__()
        self.conflict_detail = conflict_detail

    async def get_active_service(self, session, service_id):
        return SimpleNamespace(id=service_id, is_active=True, duration_minutes=60)

    async def ensure_slot_available(self, session, master_id, start_at, end_at):
        if self.conflict_detail:
            raise HTTPException(status_code=409, detail=self.conflict_detail)


@pytest.mark.anyio
async def test_cannot_create_overlapping_booking() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
    )

    with pytest.raises(HTTPException) as exc_info:
        await CreateBookingService(conflict_detail="Booking slot overlaps an existing booking").create_public_booking(
            FakeSession(),
            payload,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_cannot_create_booking_inside_blocked_interval() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
    )

    with pytest.raises(HTTPException) as exc_info:
        await CreateBookingService(conflict_detail="Booking slot overlaps a blocked interval").create_public_booking(
            FakeSession(),
            payload,
        )

    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_cannot_create_booking_on_monday() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=monday_at(10),
    )

    with pytest.raises(HTTPException) as exc_info:
        await CreateBookingService().create_public_booking(
            FakeSession(),
            payload,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Barbershop is closed on Mondays"


@pytest.mark.anyio
async def test_creating_booking_creates_new_customer() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Ivan Petrenko",
        customer_phone="(380) 50-111-22-33",
        customer_email="ivan@example.com",
        start_at=at(10),
    )
    session = FakeSession(execute_values=[SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]), None, None])

    booking = await CreateBookingService().create_public_booking(session, payload)

    customer = next(item for item in session.added_items if isinstance(item, Customer))
    assert customer.phone == "+380501112233"
    assert customer.email == "ivan@example.com"
    assert customer.name == "Ivan"
    assert customer.surname == "Petrenko"
    assert booking.customer_id == customer.id
    assert booking.customer_phone == customer.phone


@pytest.mark.anyio
async def test_creating_booking_with_multiple_services_sets_combined_duration() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_ids=[1, 2],
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
    )
    customer = SimpleNamespace(id=7, phone="+380501112233", email=None, name="Customer", surname=None)
    master = SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1), SimpleNamespace(id=2)])

    class MultiServiceCreateBookingService(CreateBookingService):
        async def get_active_service(self, session, service_id):
            durations = {1: 60, 2: 30}
            return SimpleNamespace(id=service_id, is_active=True, duration_minutes=durations[service_id])

    session = FakeSession(execute_values=[master, customer])
    booking = await MultiServiceCreateBookingService().create_public_booking(session, payload)

    assert booking.service_id == 1
    assert booking.service_ids == [1, 2]
    assert booking.end_at == at(11, 30)


@pytest.mark.anyio
async def test_creating_booking_can_use_custom_duration() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        duration_minutes=120,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
    )
    customer = SimpleNamespace(id=7, phone="+380501112233", email=None, name="Customer", surname=None)

    booking = await CreateBookingService().create_public_booking(
        FakeSession(execute_values=[SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]), customer]),
        payload,
    )

    assert booking.end_at == at(12)


@pytest.mark.anyio
async def test_creating_booking_reuses_existing_customer() -> None:
    existing_customer = Customer(id=42, phone="+380501112233", email=None, name="Ivan", is_active=True)
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Ivan Petrenko",
        customer_phone="+380501112233",
        customer_email="ivan@example.com",
        start_at=at(10),
    )
    session = FakeSession(
        execute_values=[SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]), existing_customer, None]
    )

    booking = await CreateBookingService().create_public_booking(session, payload)

    added_customers = [item for item in session.added_items if isinstance(item, Customer)]
    assert added_customers == []
    assert existing_customer.email == "ivan@example.com"
    assert booking.customer_id == 42


@pytest.mark.anyio
async def test_booking_customer_lookup_prevents_duplicate_customers() -> None:
    existing_customer = Customer(id=42, phone="+380501112233", email="ivan@example.com", name="Ivan", is_active=True)
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Ivan Petrenko",
        customer_phone="+380501112233",
        customer_email="ivan@example.com",
        start_at=at(10),
    )
    session = FakeSession(
        execute_values=[
            SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]),
            existing_customer,
        ]
    )

    first_booking = await CreateBookingService().create_public_booking(session, payload)
    second_booking = await CreateBookingService().create_public_booking(
        FakeSession(
            execute_values=[
                SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]),
                existing_customer,
            ]
        ),
        payload,
    )

    assert first_booking.customer_id == existing_customer.id
    assert second_booking.customer_id == existing_customer.id


@pytest.mark.anyio
async def test_barber_can_only_access_own_bookings() -> None:
    booking = Booking(
        id=1,
        master_id=2,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_my_booking_status(
            booking_id=1,
            payload=BookingStatusUpdate(status=BookingStatus.cancelled),
            current_master=SimpleNamespace(id=1),
            session=FakeSession(get_value=booking),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_barber_can_update_own_booking_time(monkeypatch: pytest.MonkeyPatch) -> None:
    booking = Booking(
        id=1,
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        customer_comment=None,
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        created_at=at(9),
        updated_at=at(9),
    )
    checked_slot = {}

    async def fake_ensure_slot_available(session, master_id, start_at, end_at, exclude_booking_id=None):
        checked_slot.update(
            {
                "master_id": master_id,
                "start_at": start_at,
                "end_at": end_at,
                "exclude_booking_id": exclude_booking_id,
            }
        )

    monkeypatch.setattr(booking_routes.service, "ensure_slot_available", fake_ensure_slot_available)

    response = await update_my_booking(
        booking_id=1,
        payload=BookingUpdate(start_at=at(10, 30), end_at=at(12)),
        current_master=SimpleNamespace(id=1),
        session=FakeSession(get_value=booking, execute_values=[booking]),
    )

    assert response.start_at == at(10, 30)
    assert response.end_at == at(12)
    assert booking.start_at == at(10, 30)
    assert booking.end_at == at(12)
    assert checked_slot == {
        "master_id": 1,
        "start_at": at(10, 30),
        "end_at": at(12),
        "exclude_booking_id": 1,
    }


@pytest.mark.anyio
async def test_barber_cannot_update_another_masters_booking() -> None:
    booking = Booking(
        id=1,
        master_id=2,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_my_booking(
            booking_id=1,
            payload=BookingUpdate(start_at=at(10, 30), end_at=at(12)),
            current_master=SimpleNamespace(id=1),
            session=FakeSession(get_value=booking),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_barber_cannot_update_completed_booking() -> None:
    booking = Booking(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.completed,
    )

    with pytest.raises(HTTPException) as exc_info:
        await update_my_booking(
            booking_id=1,
            payload=BookingUpdate(start_at=at(10, 30), end_at=at(12)),
            current_master=SimpleNamespace(id=1),
            session=FakeSession(get_value=booking),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_barber_can_delete_own_booking() -> None:
    booking = Booking(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
    )
    session = FakeSession(get_value=booking)

    await delete_my_booking(
        booking_id=1,
        current_master=SimpleNamespace(id=1),
        session=session,
    )

    assert session.deleted is booking
    assert session.committed is True


@pytest.mark.anyio
async def test_barber_cannot_delete_completed_booking() -> None:
    booking = Booking(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.completed,
    )
    session = FakeSession(get_value=booking)

    with pytest.raises(HTTPException) as exc_info:
        await delete_my_booking(
            booking_id=1,
            current_master=SimpleNamespace(id=1),
            session=session,
        )

    assert exc_info.value.status_code == 400
    assert session.deleted is None


@pytest.mark.anyio
async def test_admin_can_update_booking_time(monkeypatch: pytest.MonkeyPatch) -> None:
    booking = Booking(
        id=1,
        master_id=2,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        customer_comment=None,
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        created_at=at(9),
        updated_at=at(9),
    )
    checked_slot = {}

    async def fake_ensure_slot_available(session, master_id, start_at, end_at, exclude_booking_id=None):
        checked_slot.update(
            {
                "master_id": master_id,
                "start_at": start_at,
                "end_at": end_at,
                "exclude_booking_id": exclude_booking_id,
            }
        )

    monkeypatch.setattr(booking_routes.service, "ensure_slot_available", fake_ensure_slot_available)

    response = await admin_update_booking(
        booking_id=1,
        payload=BookingUpdate(start_at=at(10, 30), end_at=at(12)),
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=FakeSession(get_value=booking, execute_values=[booking]),
    )

    assert response.start_at == at(10, 30)
    assert response.end_at == at(12)
    assert booking.start_at == at(10, 30)
    assert booking.end_at == at(12)
    assert checked_slot == {
        "master_id": 2,
        "start_at": at(10, 30),
        "end_at": at(12),
        "exclude_booking_id": 1,
    }


@pytest.mark.anyio
async def test_admin_can_delete_booking() -> None:
    booking = Booking(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
    )
    session = FakeSession(get_value=booking)

    await admin_delete_booking(
        booking_id=1,
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=session,
    )

    assert session.deleted is booking
    assert session.committed is True


def test_booking_status_update_marks_completed_result() -> None:
    booking = Booking(
        id=1,
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        cancelled_at=at(12),
    )

    booking_routes.apply_booking_status_update(booking, BookingStatus.completed)

    assert booking.status == BookingStatus.completed
    assert booking.completed_at is not None
    assert booking.cancelled_at is None


def test_booking_status_update_marks_cancelled_result() -> None:
    booking = Booking(
        id=1,
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        completed_at=at(12),
    )

    booking_routes.apply_booking_status_update(booking, BookingStatus.cancelled)

    assert booking.status == BookingStatus.cancelled
    assert booking.cancelled_at is not None
    assert booking.completed_at is None


def test_booking_status_update_clears_result_for_confirmed_booking() -> None:
    booking = Booking(
        id=1,
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.completed,
        completed_at=at(12),
    )

    booking_routes.apply_booking_status_update(booking, BookingStatus.confirmed)

    assert booking.status == BookingStatus.confirmed
    assert booking.completed_at is None
    assert booking.cancelled_at is None


@pytest.mark.anyio
async def test_listing_customer_booking_history() -> None:
    now = datetime.now(tz=KYIV_TZ)
    customer = Customer(id=42, phone="+380501112233", name="Ivan", is_active=True)
    booking = Booking(
        id=1,
        master_id=1,
        service_id=1,
        customer_id=42,
        customer_name="Ivan",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        created_at=now,
        updated_at=now,
    )

    response = await customer_routes.backoffice_customer_bookings(
        customer_id=42,
        pagination=SimpleNamespace(page=1, page_size=20),
        session=FakeSession(get_value=customer, execute_values=[1, [booking]]),
    )

    assert response.total == 1
    assert response.items[0].id == 1
    assert response.items[0].customer_id == 42


@pytest.mark.anyio
async def test_calculating_customer_booking_stats() -> None:
    customer = Customer(id=42, phone="+380501112233", name="Ivan", is_active=True)

    response = await customer_routes.backoffice_customer_stats(
        customer_id=42,
        session=FakeSession(
            get_value=customer,
            execute_values=[
                3,
                at(12),
                SimpleNamespace(id=7, full_name="Gleb", booking_count=2),
                [
                    SimpleNamespace(id=1, name="Haircut", booking_count=2),
                    SimpleNamespace(id=2, name="Beard trim", booking_count=1),
                ],
            ],
        ),
    )

    assert response.total_bookings == 3
    assert response.most_visited_barber == CustomerBookingStatsItem(id=7, name="Gleb", count=2)
    assert [(item.id, item.name, item.count) for item in response.most_used_services] == [
        (1, "Haircut", 2),
        (2, "Beard trim", 1),
    ]
    assert response.last_visit_date == at(12)


@pytest.mark.anyio
async def test_barber_can_create_and_delete_own_time_blocks() -> None:
    session = FakeSession()
    service = BookingServiceLayer()
    block = await service.create_time_block(
        session,
        SimpleNamespace(id=1),
        MasterTimeBlockCreate(start_at=at(12), end_at=at(13), reason="Lunch"),
    )

    assert block.master_id == 1
    assert session.committed is True

    delete_session = FakeSession(get_value=SimpleNamespace(id=block.id, master_id=1))
    await delete_my_time_block(
        block_id=block.id,
        current_master=SimpleNamespace(id=1),
        session=delete_session,
    )

    assert delete_session.deleted.master_id == 1
    assert delete_session.committed is True


@pytest.mark.anyio
async def test_creating_base_service_as_admin_does_not_copy_to_existing_barbers() -> None:
    session = FakeSession()

    response = await booking_routes.admin_create_base_service(
        payload=BaseServiceCreate(title_uk="Стрижка", title_en="Haircut", duration_minutes=60, price=900),
        current_user=SimpleNamespace(is_superuser=True),
        session=session,
    )

    assert response.name == "Стрижка"
    assert response.title_uk == "Стрижка"
    assert response.title_en == "Haircut"
    assert response.price == 900
    copied = [item for item in session.added_items if isinstance(item, BarberService)]
    assert copied == []
    assert session.committed is True


@pytest.mark.anyio
async def test_admin_can_delete_barber() -> None:
    master = Master(id=7, full_name="Mock Barber", is_active=True)
    session = FakeSession(get_value=master, execute_values=[None])

    await booking_routes.admin_delete_master(
        master_id=7,
        current_user=SimpleNamespace(is_superuser=True),
        session=session,
    )

    assert session.deleted is master
    assert master.is_active is True
    assert session.committed is True


@pytest.mark.anyio
async def test_admin_soft_deletes_barber_with_bookings() -> None:
    master = Master(id=7, full_name="Booked Barber", is_active=True)
    session = FakeSession(get_value=master, execute_values=[1])

    await booking_routes.admin_delete_master(
        master_id=7,
        current_user=SimpleNamespace(is_superuser=True),
        session=session,
    )

    assert session.deleted is None
    assert master.is_active is False
    assert session.committed is True


@pytest.mark.anyio
async def test_barber_image_cleanup_removes_unshared_uploads() -> None:
    master = Master(
        id=7,
        full_name="Image Barber",
        photo_url="/media/barbers/photo.jpg",
        photo_upload_id=1,
        avatar_url="/media/barbers/avatars/avatar.jpg",
        avatar_upload_id=2,
    )
    uploads = {
        1: Upload(id=1, file_name="photo.jpg", file_path="/tmp/photo.jpg"),
        2: Upload(id=2, file_name="avatar.jpg", file_path="/tmp/avatar.jpg"),
    }

    class ImageCleanupSession:
        def __init__(self):
            self.deleted = []
            self.flushed = False

        async def flush(self):
            self.flushed = True

        async def execute(self, _statement):
            return FakeExecuteResult(None)

        async def get(self, _model, entity_id):
            return uploads.get(entity_id)

        async def delete(self, instance):
            self.deleted.append(instance)

    session = ImageCleanupSession()

    file_paths = await booking_routes.cleanup_master_images(session, master)

    assert master.photo_url is None
    assert master.photo_upload_id is None
    assert master.avatar_url is None
    assert master.avatar_upload_id is None
    assert file_paths == ["/tmp/photo.jpg", "/tmp/avatar.jpg"]
    assert session.deleted == [uploads[1], uploads[2]]
    assert session.flushed is True


@pytest.mark.anyio
async def test_replacing_barber_photo_cleans_previous_upload() -> None:
    master = Master(
        id=7,
        full_name="Image Barber",
        photo_url="/media/barbers/new.jpg",
        photo_upload_id=2,
    )
    old_upload = Upload(id=1, file_name="old.jpg", file_path="/tmp/old.jpg")

    class ImageReplacementSession:
        def __init__(self):
            self.deleted = []
            self.flushed = False

        async def flush(self):
            self.flushed = True

        async def execute(self, _statement):
            return FakeExecuteResult(None)

        async def get(self, _model, entity_id):
            return old_upload if entity_id == 1 else None

        async def delete(self, instance):
            self.deleted.append(instance)

    session = ImageReplacementSession()

    file_paths = await booking_routes.cleanup_replaced_master_uploads(session, master, {1})

    assert file_paths == ["/tmp/old.jpg"]
    assert session.deleted == [old_upload]
    assert session.flushed is True


@pytest.mark.anyio
async def test_replacing_barber_photo_keeps_shared_upload() -> None:
    master = Master(
        id=7,
        full_name="Image Barber",
        photo_url="/media/barbers/new.jpg",
        photo_upload_id=2,
    )
    old_upload = Upload(id=1, file_name="old.jpg", file_path="/tmp/old.jpg")

    class SharedUploadSession:
        def __init__(self):
            self.deleted = []
            self.flushed = False

        async def flush(self):
            self.flushed = True

        async def execute(self, _statement):
            return FakeExecuteResult(99)

        async def get(self, _model, entity_id):
            return old_upload if entity_id == 1 else None

        async def delete(self, instance):
            self.deleted.append(instance)

    session = SharedUploadSession()

    file_paths = await booking_routes.cleanup_replaced_master_uploads(session, master, {1})

    assert file_paths == []
    assert session.deleted == []
    assert session.flushed is True


@pytest.mark.anyio
async def test_listing_base_services() -> None:
    now = datetime.now(tz=KYIV_TZ)
    services = [
        BaseService(id=1, name="A", duration_minutes=30, price=100, is_active=True, created_at=now, updated_at=now),
        BaseService(id=2, name="B", duration_minutes=60, price=200, is_active=True, created_at=now, updated_at=now),
    ]

    response = await booking_routes.admin_list_base_services(
        current_user=SimpleNamespace(is_superuser=True),
        session=FakeSession(execute_values=[services]),
    )

    assert [item.name for item in response] == ["A", "B"]


@pytest.mark.anyio
async def test_public_service_catalog_groups_equivalent_barber_services() -> None:
    now = datetime.now(tz=KYIV_TZ)
    services = [
        BarberService(
            id=1,
            master_id=10,
            base_service_id=5,
            name="Стрижка",
            title_uk="Стрижка",
            title_en="Haircut",
            duration_minutes=60,
            price=900,
            is_active=True,
            created_at=now,
            updated_at=now,
        ),
        BarberService(
            id=2,
            master_id=11,
            base_service_id=5,
            name="Стрижка",
            title_uk="Стрижка",
            title_en="Haircut",
            duration_minutes=60,
            price=900,
            is_active=True,
            created_at=now,
            updated_at=now,
        ),
        BarberService(
            id=3,
            master_id=12,
            base_service_id=5,
            name="Стрижка",
            title_uk="Стрижка",
            title_en="Haircut",
            duration_minutes=60,
            price=1100,
            is_active=True,
            created_at=now,
            updated_at=now,
        ),
    ]

    response = await booking_routes.list_public_service_catalog(session=FakeSession(execute_values=[services]))

    assert len(response) == 2
    assert response[0].name == "Стрижка"
    assert response[0].title_uk == "Стрижка"
    assert response[0].title_en == "Haircut"
    assert response[0].price == 900
    assert response[0].barber_ids == [10, 11]
    assert response[0].barber_service_ids == [1, 2]
    assert response[1].price == 1100
    assert response[1].barber_ids == [12]


@pytest.mark.anyio
async def test_creating_barber_copies_default_services_idempotently() -> None:
    service_layer = BookingServiceLayer()
    session = FakeSession(
        execute_values=[
            [
                BaseService(
                    id=1,
                    name="Стрижка",
                    title_uk="Стрижка",
                    title_en="Haircut",
                    duration_minutes=60,
                    price=900,
                    is_active=True,
                ),
                BaseService(
                    id=2,
                    name="Гоління",
                    title_uk="Гоління",
                    title_en="Shave",
                    duration_minutes=30,
                    price=800,
                    is_active=True,
                ),
            ],
            [1],
        ]
    )

    copied = await service_layer.copy_active_base_services_to_master(session, SimpleNamespace(id=10))

    assert len(copied) == 1
    assert copied[0].base_service_id == 2
    assert copied[0].master_id == 10
    assert copied[0].title_uk == "Гоління"
    assert copied[0].title_en == "Shave"
    assert session.flushed is True


@pytest.mark.anyio
async def test_updating_barber_specific_service_price(monkeypatch) -> None:
    now = datetime.now(tz=KYIV_TZ)
    item = BarberService(
        id=1,
        master_id=10,
        name="Стрижка",
        duration_minutes=60,
        price=900,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    async def fake_can_manage(_session, _user, barber_id):
        assert barber_id == 10
        return SimpleNamespace(id=10)

    async def fake_get(_session, service_id):
        assert service_id == 1
        return item

    async def fake_update(_session, instance, data):
        instance.price = data["price"]
        return instance

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)
    monkeypatch.setattr(booking_routes.barber_service_repo, "get", fake_get)
    monkeypatch.setattr(booking_routes.barber_service_repo, "update", fake_update)

    response = await booking_routes.update_barber_service(
        barber_id=10,
        service_id=1,
        payload=BarberServiceUpdate(price=1200),
        current_user=SimpleNamespace(is_superuser=False),
        session=FakeSession(execute_values=[None]),
    )

    assert response.price == 1200


@pytest.mark.anyio
async def test_barber_can_update_own_base_linked_service(monkeypatch) -> None:
    now = datetime.now(tz=KYIV_TZ)
    item = BarberService(
        id=1,
        master_id=10,
        base_service_id=5,
        name="Стрижка",
        duration_minutes=60,
        price=900,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    async def fake_can_manage(_session, _user, barber_id):
        return SimpleNamespace(id=barber_id)

    async def fake_get(_session, service_id):
        return item

    async def fake_update(_session, instance, data):
        for key, value in data.items():
            setattr(instance, key, value)
        return instance

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)
    monkeypatch.setattr(booking_routes.barber_service_repo, "get", fake_get)
    monkeypatch.setattr(booking_routes.barber_service_repo, "update", fake_update)

    response = await booking_routes.update_barber_service(
        barber_id=10,
        service_id=1,
        payload=BarberServiceUpdate(price=1200, is_active=False),
        current_user=SimpleNamespace(is_superuser=False),
        session=FakeSession(execute_values=[None]),
    )

    assert response.is_active is False
    assert response.price == 1200


@pytest.mark.anyio
async def test_master_can_update_own_service_duration_from_profile() -> None:
    now = datetime.now(tz=KYIV_TZ)
    item = BarberService(
        id=1,
        master_id=10,
        base_service_id=5,
        name="Стрижка",
        duration_minutes=60,
        price=900,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    response = await booking_routes.update_my_service(
        service_id=1,
        payload=BarberServiceUpdate(duration_minutes=90),
        current_master=SimpleNamespace(id=10),
        session=FakeSession(get_value=item, execute_values=[None]),
    )

    assert response.duration_minutes == 90


@pytest.mark.anyio
async def test_master_cannot_update_another_master_service_from_profile() -> None:
    item = SimpleNamespace(id=1, master_id=11, base_service_id=5, name="Стрижка")

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.update_my_service(
            service_id=1,
            payload=BarberServiceUpdate(duration_minutes=90),
            current_master=SimpleNamespace(id=10),
            session=FakeSession(get_value=item),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_barber_cannot_relink_service_to_base(monkeypatch) -> None:
    item = SimpleNamespace(id=1, master_id=10, base_service_id=5)

    async def fake_can_manage(_session, _user, barber_id):
        return SimpleNamespace(id=barber_id)

    async def fake_get(_session, service_id):
        return item

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)
    monkeypatch.setattr(booking_routes.barber_service_repo, "get", fake_get)

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.update_barber_service(
            barber_id=10,
            service_id=1,
            payload=BarberServiceUpdate(base_service_id=6),
            current_user=SimpleNamespace(is_superuser=False),
            session=FakeSession(),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_preventing_access_to_another_barbers_services() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.ensure_can_manage_barber_services(
            FakeSession(get_value=SimpleNamespace(id=2), master=SimpleNamespace(id=1, is_active=True)),
            SimpleNamespace(is_superuser=False, id=1),
            barber_id=2,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_adding_custom_barber_service(monkeypatch) -> None:
    async def fake_can_manage(_session, _user, barber_id):
        return SimpleNamespace(id=barber_id)

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)
    session = FakeSession(execute_values=[None])

    response = await booking_routes.create_barber_service(
        barber_id=7,
        payload=BarberServiceCreate(title_uk="Кастом", title_en="Custom", duration_minutes=45, price=500),
        current_user=SimpleNamespace(is_superuser=False),
        session=session,
    )

    assert response.barber_id == 7
    assert response.base_service_id is None
    assert session.added.name == "Кастом"
    assert session.added.title_uk == "Кастом"
    assert session.added.title_en == "Custom"
    assert session.committed is True


@pytest.mark.anyio
async def test_adding_barber_service_from_base_uses_base_defaults(monkeypatch) -> None:
    now = datetime.now(tz=KYIV_TZ)
    base_service = BaseService(
        id=4,
        name="Стрижка",
        title_uk="Стрижка",
        title_en="Haircut",
        duration_minutes=60,
        price=900,
        description="Base description",
        description_uk="Базовий опис",
        description_en="Base description",
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    async def fake_can_manage(_session, _user, barber_id):
        return SimpleNamespace(id=barber_id)

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)
    session = FakeSession(get_value=base_service, execute_values=[None])

    response = await booking_routes.create_barber_service(
        barber_id=7,
        payload=BarberServiceCreate(base_service_id=4, price=1200),
        current_user=SimpleNamespace(is_superuser=False),
        session=session,
    )

    assert response.base_service_id == 4
    assert response.source_type == "base"
    assert response.name == "Стрижка"
    assert response.title_uk == "Стрижка"
    assert response.title_en == "Haircut"
    assert response.description_uk == "Базовий опис"
    assert response.description_en == "Base description"
    assert response.duration_minutes == 60
    assert response.price == 1200


@pytest.mark.anyio
async def test_duplicate_base_service_for_barber_is_rejected(monkeypatch) -> None:
    base_service = BaseService(id=4, name="Стрижка", duration_minutes=60, price=900, is_active=True)

    async def fake_can_manage(_session, _user, barber_id):
        return SimpleNamespace(id=barber_id)

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.create_barber_service(
            barber_id=7,
            payload=BarberServiceCreate(base_service_id=4),
            current_user=SimpleNamespace(is_superuser=False),
            session=FakeSession(
                get_value=base_service,
                execute_values=[SimpleNamespace(id=1, master_id=7, base_service_id=4)],
            ),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_deleting_barber_service(monkeypatch) -> None:
    item = SimpleNamespace(id=3, master_id=7, base_service_id=None, is_active=True)

    async def fake_can_manage(_session, _user, barber_id):
        return SimpleNamespace(id=barber_id)

    async def fake_get(_session, service_id):
        assert service_id == 3
        return item

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)
    monkeypatch.setattr(booking_routes.barber_service_repo, "get", fake_get)

    session = FakeSession()
    await booking_routes.delete_barber_service(
        barber_id=7,
        service_id=3,
        current_user=SimpleNamespace(is_superuser=False),
        session=session,
    )

    assert item.is_active is False
    assert session.committed is True


@pytest.mark.anyio
async def test_barber_can_disable_base_linked_service(monkeypatch) -> None:
    item = SimpleNamespace(id=3, master_id=7, base_service_id=1, is_active=True)

    async def fake_can_manage(_session, _user, barber_id):
        return SimpleNamespace(id=barber_id)

    async def fake_get(_session, service_id):
        return item

    monkeypatch.setattr(booking_routes, "ensure_can_manage_barber_services", fake_can_manage)
    monkeypatch.setattr(booking_routes.barber_service_repo, "get", fake_get)

    session = FakeSession()
    await booking_routes.delete_barber_service(
        barber_id=7,
        service_id=3,
        current_user=SimpleNamespace(is_superuser=False),
        session=session,
    )

    assert item.is_active is False
    assert session.committed is True


@pytest.mark.anyio
async def test_admin_can_sync_missing_default_services_for_barber() -> None:
    session = FakeSession(
        get_value=SimpleNamespace(id=7),
        execute_values=[
            [
                BaseService(id=1, name="Стрижка", duration_minutes=60, price=900, is_active=True),
                BaseService(
                    id=2,
                    name="Гоління",
                    title_uk="Гоління",
                    title_en="Shave",
                    duration_minutes=30,
                    price=800,
                    is_active=True,
                ),
            ],
            [1],
        ],
    )

    response = await booking_routes.admin_sync_default_barber_services(
        barber_id=7,
        current_user=SimpleNamespace(is_superuser=True),
        session=session,
    )

    assert response.created_count == 1
    copied = [item for item in session.added_items if isinstance(item, BarberService)]
    assert len(copied) == 1
    assert copied[0].base_service_id == 2
    assert copied[0].title_uk == "Гоління"
    assert copied[0].title_en == "Shave"
    assert session.committed is True


@pytest.mark.anyio
async def test_seed_base_services_is_idempotent() -> None:
    session = FakeSession(execute_values=[[name for name, _, _ in DEFAULT_BASE_SERVICES]])

    created = await seed_base_services(session)

    assert created == 0
    assert session.added_items == []


@pytest.mark.anyio
async def test_seed_base_services_sets_english_text() -> None:
    session = FakeSession(execute_values=[[]])

    created = await seed_base_services(session)

    assert created == len(DEFAULT_BASE_SERVICES)
    haircut = next(item for item in session.added_items if item.name == "Стрижка")
    assert haircut.title_en == "Haircut"
    assert haircut.description_en == "Classic haircut with shape, texture, and styling."
