from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.v1.routes import bookings as booking_routes
from app.api.v1.routes import customers as customer_routes
from app.api.v1.routes.bookings import (
    admin_delete_booking,
    admin_update_booking,
    admin_update_time_block,
    delete_my_booking,
    delete_my_time_block,
    update_my_booking,
    update_my_booking_status,
)
from app.models.booking import (
    BarberService,
    BaseService,
    Booking,
    BookingServiceItem,
    BookingStatus,
    Master,
    MasterAvailabilityWindow,
    MasterPosition,
    MasterTimeBlock,
)
from app.models.customer import Customer
from app.models.promotion import PromotionDiscountType, PromotionEligibilityType
from app.models.booking_funnel import BookingFunnelEvent, BookingFunnelEventSource, BookingFunnelEventType
from app.models.upload import Upload
from app.schemas.booking import (
    AdminBookingUpdate,
    AdminMasterTimeBlockUpdate,
    BarberServiceCreate,
    BarberServiceUpdate,
    BaseServiceCreate,
    BookingBackofficeResponse,
    BookingStatusUpdate,
    BookingUpdate,
    CustomerBookingStatsItem,
    MasterAvailabilityDaysCreate,
    MasterAvailabilityWindowCreate,
    MasterBackofficeResponse,
    MasterCreate,
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
        show_on_master_block=False,
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
    assert response.show_on_master_block is False
    assert response.model_dump(by_alias=True)["showOnMasterBlock"] is False


def test_master_create_accepts_show_on_master_block_camel_case() -> None:
    payload = MasterCreate.model_validate({"full_name": "Гліб", "showOnMasterBlock": False})

    assert payload.show_on_master_block is False
    assert payload.model_dump(by_alias=True)["showOnMasterBlock"] is False


def test_master_create_accepts_booking_redirect_master_id_camel_case() -> None:
    payload = MasterCreate.model_validate({"full_name": "Гліб", "bookingRedirectMasterId": 12})

    assert payload.booking_redirect_master_id == 12
    assert payload.model_dump(by_alias=True)["bookingRedirectMasterId"] == 12


def test_public_master_response_does_not_include_booking_redirect_master_id() -> None:
    now = datetime.now(tz=KYIV_TZ)
    master = Master(
        id=7,
        full_name="Гліб",
        booking_redirect_master_id=12,
        show_on_master_block=True,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    response = MasterResponse.model_validate(master)

    assert "bookingRedirectMasterId" not in response.model_dump(by_alias=True)


def test_backoffice_master_response_includes_booking_redirect_master_id() -> None:
    now = datetime.now(tz=KYIV_TZ)
    master = Master(
        id=7,
        full_name="Гліб",
        booking_redirect_master_id=12,
        show_on_master_block=True,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    response = MasterBackofficeResponse.model_validate(master)

    assert response.booking_redirect_master_id == 12
    assert response.model_dump(by_alias=True)["bookingRedirectMasterId"] == 12


class MasterLookupSession:
    def __init__(self, masters):
        self.masters = masters

    async def get(self, _model, entity_id):
        return self.masters.get(entity_id)


@pytest.mark.anyio
async def test_booking_redirect_cannot_target_same_master() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.ensure_booking_redirect_master_valid(
            MasterLookupSession({}),
            source_master_id=7,
            redirect_master_id=7,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Master cannot redirect bookings to itself"


@pytest.mark.anyio
async def test_booking_redirect_requires_active_target_master() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.ensure_booking_redirect_master_valid(
            MasterLookupSession({2: SimpleNamespace(id=2, is_active=False, booking_redirect_master_id=None)}),
            source_master_id=1,
            redirect_master_id=2,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Redirect master must be active"


@pytest.mark.anyio
async def test_booking_redirect_rejects_cycle_on_existing_master() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.ensure_booking_redirect_master_valid(
            MasterLookupSession(
                {
                    2: SimpleNamespace(id=2, is_active=True, booking_redirect_master_id=3),
                    3: SimpleNamespace(id=3, is_active=True, booking_redirect_master_id=1),
                }
            ),
            source_master_id=1,
            redirect_master_id=2,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Booking redirect cannot create a cycle"


@pytest.mark.anyio
async def test_booking_redirect_rejects_cycle_when_creating_master() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.ensure_booking_redirect_master_valid(
            MasterLookupSession(
                {
                    2: SimpleNamespace(id=2, is_active=True, booking_redirect_master_id=3),
                    3: SimpleNamespace(id=3, is_active=True, booking_redirect_master_id=2),
                }
            ),
            source_master_id=None,
            redirect_master_id=2,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Booking redirect cannot create a cycle"


class SlotService(BookingServiceLayer):
    def __init__(self, bookings=None, blocks=None, availability_windows=None):
        self.master = SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)])
        self.booking_service = SimpleNamespace(id=1, is_active=True, duration_minutes=60)
        self.bookings = bookings or []
        self.blocks = blocks or []
        self.availability_windows = (
            [SimpleNamespace(start_at=at(8), end_at=at(20))]
            if availability_windows is None
            else availability_windows
        )

    async def get_active_master_with_services(self, session, master_id):
        return self.master

    async def get_active_service(self, session, service_id):
        return self.booking_service

    async def list_busy_bookings(self, session, master_id, start_at, end_at):
        return self.bookings

    async def list_time_blocks(self, session, master_id, start_at, end_at):
        return self.blocks

    async def list_availability_windows(self, session, master_id, start_at, end_at):
        return self.availability_windows


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2099, 1, 1, hour, minute, tzinfo=KYIV_TZ)


def monday_at(hour: int, minute: int = 0) -> datetime:
    return datetime(2099, 1, 5, hour, minute, tzinfo=KYIV_TZ)


def past_at(hour: int, minute: int = 0) -> datetime:
    return datetime(2020, 1, 2, hour, minute, tzinfo=KYIV_TZ)


def next_open_date(offset_days: int = 1) -> date:
    target_date = datetime.now(tz=KYIV_TZ).date() + timedelta(days=offset_days)
    while target_date.weekday() == 0:
        target_date += timedelta(days=1)
    return target_date


def outside_availability_horizon_date() -> date:
    service = BookingServiceLayer()
    target_date = service.availability_horizon_end_date() + timedelta(days=1)
    while target_date.weekday() == 0:
        target_date += timedelta(days=1)
    return target_date


@pytest.mark.anyio
async def test_customer_cannot_view_slots_until_barber_opens_availability() -> None:
    slots = await SlotService(availability_windows=[]).get_available_slots(
        None,
        master_id=1,
        service_id=1,
        target_date=date(2099, 1, 1),
    )

    assert slots == []


@pytest.mark.anyio
async def test_customer_can_view_available_barber_slots() -> None:
    slots = await SlotService().get_available_slots(None, master_id=1, service_id=1, target_date=date(2099, 1, 1))

    assert slots[0].start_at == at(8)
    assert slots[0].end_at == at(9)
    assert slots[-1].start_at == at(19)
    assert slots[-1].end_at == at(20)


@pytest.mark.anyio
async def test_available_slots_are_limited_to_open_availability_window() -> None:
    slots = await SlotService(
        availability_windows=[SimpleNamespace(start_at=at(10), end_at=at(12))]
    ).get_available_slots(None, master_id=1, service_id=1, target_date=date(2099, 1, 1))

    assert slots[0].start_at == at(10)
    assert slots[0].end_at == at(11)
    assert slots[-1].start_at == at(11)
    assert slots[-1].end_at == at(12)


@pytest.mark.anyio
async def test_available_slots_do_not_bridge_separate_availability_windows() -> None:
    slots = await SlotService(
        availability_windows=[
            SimpleNamespace(start_at=at(8), end_at=at(9)),
            SimpleNamespace(start_at=at(10), end_at=at(11)),
        ],
    ).get_available_slots(
        None,
        master_id=1,
        service_id=1,
        duration_minutes=90,
        target_date=date(2099, 1, 1),
    )

    assert slots == []


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


@pytest.mark.anyio
async def test_redirected_master_slots_use_target_master_schedule_and_service() -> None:
    target_service = SimpleNamespace(
        id=2,
        master_id=2,
        base_service_id=5,
        is_active=True,
        duration_minutes=45,
        price=900,
    )
    source_service = SimpleNamespace(
        id=1,
        master_id=1,
        base_service_id=5,
        is_active=True,
        duration_minutes=60,
        price=900,
    )
    existing = SimpleNamespace(start_at=at(10), end_at=at(10, 45))

    class RedirectSlotService(BookingServiceLayer):
        def __init__(self):
            self.masters = {
                1: SimpleNamespace(id=1, booking_redirect_master_id=2, services=[source_service]),
                2: SimpleNamespace(id=2, booking_redirect_master_id=None, services=[target_service]),
            }
            self.services = {1: source_service, 2: target_service}
            self.busy_master_ids = []
            self.availability_master_ids = []

        async def get_active_master_with_services(self, session, master_id):
            return self.masters[master_id]

        async def get_active_service(self, session, service_id):
            return self.services[service_id]

        async def list_busy_bookings(self, session, master_id, start_at, end_at):
            self.busy_master_ids.append(master_id)
            return [existing]

        async def list_time_blocks(self, session, master_id, start_at, end_at):
            return []

        async def list_availability_windows(self, session, master_id, start_at, end_at):
            self.availability_master_ids.append(master_id)
            return [SimpleNamespace(start_at=at(8), end_at=at(20))]

    slot_service = RedirectSlotService()
    slots = await slot_service.get_available_slots(None, master_id=1, service_id=1, target_date=date(2099, 1, 1))

    slot_starts = {slot.start_at for slot in slots}
    assert slot_service.availability_master_ids == [2]
    assert slot_service.busy_master_ids == [2]
    assert slots[0].end_at == at(8, 45)
    assert at(9, 30) not in slot_starts
    assert at(10) not in slot_starts
    assert at(10, 45) in slot_starts


def test_cannot_create_booking_outside_working_hours() -> None:
    service = BookingServiceLayer()

    with pytest.raises(HTTPException) as exc_info:
        service.ensure_within_working_hours(at(19, 30), at(20, 30))

    assert exc_info.value.status_code == 400


def test_admin_booking_open_day_check_allows_outside_working_hours() -> None:
    BookingServiceLayer().ensure_within_open_business_days(at(21), at(22))


def test_admin_booking_open_day_check_rejects_monday() -> None:
    with pytest.raises(HTTPException) as exc_info:
        BookingServiceLayer().ensure_within_open_business_days(monday_at(10), monday_at(11))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Barbershop is closed on Mondays"


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
        self.rolled_back = False
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

    async def rollback(self):
        self.rolled_back = True

    async def delete(self, instance):
        self.deleted = instance


class RecordingFakeSession(FakeSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return await super().execute(statement)


class CreateBookingService(BookingServiceLayer):
    def __init__(self, *, conflict_detail: str | None = None):
        super().__init__()
        self.conflict_detail = conflict_detail

    async def get_active_service(self, session, service_id):
        return SimpleNamespace(id=service_id, is_active=True, duration_minutes=60)

    async def ensure_booking_within_availability(self, session, master_id, start_at, end_at):
        return None

    async def ensure_slot_available(self, session, master_id, start_at, end_at):
        if self.conflict_detail:
            raise HTTPException(status_code=409, detail=self.conflict_detail)


def booking_response_item(start_at: datetime, end_at: datetime) -> Booking:
    now = datetime.now(tz=KYIV_TZ)
    master = Master(
        id=1,
        full_name="Master",
        email="master@example.com",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    barber_service = BarberService(
        id=1,
        master_id=1,
        name="Cut",
        duration_minutes=60,
        price=900,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    return Booking(
        id=1,
        master_id=1,
        service_id=1,
        master=master,
        service=barber_service,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=start_at,
        end_at=end_at,
        status=BookingStatus.confirmed,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.anyio
async def test_public_create_booking_rejects_past_slot() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=past_at(10),
    )

    with pytest.raises(HTTPException) as exc_info:
        await CreateBookingService().create_public_booking(FakeSession(), payload)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Slot is in the past"


@pytest.mark.anyio
async def test_create_booking_can_allow_past_slot_for_admin_flow() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=past_at(10),
    )
    customer = SimpleNamespace(id=7, phone="+380501112233", email=None, name="Customer", surname=None)

    session = FakeSession(
        execute_values=[SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]), customer]
    )
    booking = await CreateBookingService().create_public_booking(session, payload, allow_past=True)

    assert booking.start_at == past_at(10)
    assert booking.end_at == past_at(11)
    assert session.committed is True


@pytest.mark.anyio
async def test_create_booking_can_skip_working_hours_for_admin_flow() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(21),
    )
    customer = SimpleNamespace(id=7, phone="+380501112233", email=None, name="Customer", surname=None)

    session = FakeSession(
        execute_values=[SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]), customer]
    )
    booking = await CreateBookingService().create_public_booking(
        session,
        payload,
        require_availability=False,
        require_working_hours=False,
    )

    assert booking.start_at == at(21)
    assert booking.end_at == at(22)
    assert session.committed is True


@pytest.mark.anyio
async def test_create_booking_admin_flow_still_rejects_monday() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=monday_at(10),
    )

    session = FakeSession(execute_values=[SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)])])

    with pytest.raises(HTTPException) as exc_info:
        await CreateBookingService().create_public_booking(
            session,
            payload,
            require_availability=False,
            require_working_hours=False,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Barbershop is closed on Mondays"
    assert session.rolled_back is True


@pytest.mark.anyio
async def test_public_booking_with_superuser_token_can_create_past_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=past_at(10),
    )
    booking = booking_response_item(past_at(10), past_at(11))
    captured = {}

    class FakeBookingService:
        async def create_public_booking(
            self,
            session,
            payload,
            *,
            allow_past=False,
            allow_private_promotions=False,
            require_availability=True,
            require_working_hours=True,
            record_funnel_success=False,
        ):
            captured["allow_past"] = allow_past
            captured["allow_private_promotions"] = allow_private_promotions
            captured["require_availability"] = require_availability
            captured["require_working_hours"] = require_working_hours
            captured["record_funnel_success"] = record_funnel_success
            return booking

    monkeypatch.setattr(booking_routes, "service", FakeBookingService())

    response = await booking_routes.create_public_booking(
        payload=payload,
        background_tasks=BackgroundTasks(),
        current_user=SimpleNamespace(is_superuser=True),
        session=FakeSession(execute_values=[booking]),
    )

    assert captured["allow_past"] is True
    assert captured["allow_private_promotions"] is True
    assert captured["record_funnel_success"] is True
    assert captured["require_availability"] is True
    assert captured["require_working_hours"] is True
    assert response.start_at == past_at(10)


@pytest.mark.anyio
async def test_public_booking_with_master_token_cannot_create_past_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=past_at(10),
    )
    booking = booking_response_item(past_at(10), past_at(11))
    captured = {}

    class FakeBookingService:
        async def create_public_booking(
            self,
            session,
            payload,
            *,
            allow_past=False,
            allow_private_promotions=False,
            require_availability=True,
            require_working_hours=True,
            record_funnel_success=False,
        ):
            captured["allow_past"] = allow_past
            captured["allow_private_promotions"] = allow_private_promotions
            captured["require_availability"] = require_availability
            captured["require_working_hours"] = require_working_hours
            captured["record_funnel_success"] = record_funnel_success
            return booking

    monkeypatch.setattr(booking_routes, "service", FakeBookingService())

    await booking_routes.create_public_booking(
        payload=payload,
        background_tasks=BackgroundTasks(),
        current_user=SimpleNamespace(is_superuser=False),
        session=FakeSession(execute_values=[booking]),
    )

    assert captured["allow_past"] is False
    assert captured["allow_private_promotions"] is False
    assert captured["record_funnel_success"] is True
    assert captured["require_working_hours"] is True


@pytest.mark.anyio
async def test_cannot_create_overlapping_booking() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
    )

    session = FakeSession()
    with pytest.raises(HTTPException) as exc_info:
        await CreateBookingService(conflict_detail="Booking slot overlaps an existing booking").create_public_booking(
            session,
            payload,
        )

    assert exc_info.value.status_code == 409
    assert session.rolled_back is True


@pytest.mark.anyio
async def test_public_booking_requires_open_availability_window() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
    )

    class ClosedAvailabilityCreateBookingService(CreateBookingService):
        async def ensure_booking_within_availability(self, session, master_id, start_at, end_at):
            raise HTTPException(status_code=409, detail="Booking slot is outside master's open availability")

    session = FakeSession()
    with pytest.raises(HTTPException) as exc_info:
        await ClosedAvailabilityCreateBookingService().create_public_booking(session, payload)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Booking slot is outside master's open availability"
    assert session.rolled_back is True


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
async def test_booking_success_is_recorded_server_side_without_contact_data() -> None:
    funnel_session_id = "booking-attempt-01HZY7QX6FD5Q9BNYJ4K"
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Ivan Petrenko",
        customer_phone="+380501112233",
        customer_email="ivan@example.com",
        customer_comment="Private note",
        start_at=at(10),
        funnel_session_id=funnel_session_id,
    )
    session = FakeSession(
        execute_values=[
            SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]),
            None,
            None,
        ]
    )

    booking = await CreateBookingService().create_public_booking(
        session,
        payload,
        record_funnel_success=True,
    )

    event = next(item for item in session.added_items if isinstance(item, BookingFunnelEvent))
    assert event.event_type == BookingFunnelEventType.booking_success
    assert event.source == BookingFunnelEventSource.server
    assert event.booking_id == booking.id
    assert event.master_id == booking.master_id
    assert event.service_id == booking.service_id
    assert event.anonymous_session_hash != funnel_session_id
    assert len(event.anonymous_session_hash or "") == 64
    assert not hasattr(event, "customer_phone")
    assert not hasattr(event, "customer_comment")
    funnel_events = [
        item
        for item in session.added_items
        if isinstance(item, BookingFunnelEvent)
    ]
    assert {item.event_type for item in funnel_events} == {
        BookingFunnelEventType.booking_start,
        BookingFunnelEventType.service_selected,
        BookingFunnelEventType.master_selected,
        BookingFunnelEventType.slot_selected,
        BookingFunnelEventType.contact_entered,
        BookingFunnelEventType.booking_success,
    }
    assert len({item.anonymous_session_hash for item in funnel_events}) == 1
    assert all(
        item.booking_id is None
        for item in funnel_events
        if item.event_type != BookingFunnelEventType.booking_success
    )
    assert session.committed is True


@pytest.mark.anyio
async def test_internal_booking_does_not_join_public_funnel_from_payload_alone() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Ivan Petrenko",
        customer_phone="+380501112233",
        start_at=at(10),
        funnel_session_id="booking-attempt-from-admin-123456",
    )
    session = FakeSession(
        execute_values=[
            SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]),
            None,
            None,
        ]
    )

    await CreateBookingService().create_public_booking(session, payload)

    assert not any(
        isinstance(item, BookingFunnelEvent)
        for item in session.added_items
    )


@pytest.mark.anyio
async def test_public_booking_without_session_records_unattributed_server_success() -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Ivan Petrenko",
        customer_phone="+380501112233",
        start_at=at(10),
    )
    session = FakeSession(
        execute_values=[
            SimpleNamespace(id=1, is_active=True, services=[SimpleNamespace(id=1)]),
            None,
            None,
        ]
    )

    booking = await CreateBookingService().create_public_booking(
        session,
        payload,
        record_funnel_success=True,
    )

    events = [
        item
        for item in session.added_items
        if isinstance(item, BookingFunnelEvent)
    ]
    assert len(events) == 1
    assert events[0].event_type == BookingFunnelEventType.booking_success
    assert events[0].booking_id == booking.id
    assert events[0].anonymous_session_hash is None


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
async def test_creating_booking_for_redirected_master_books_target_master() -> None:
    source_service = SimpleNamespace(
        id=1,
        master_id=1,
        base_service_id=5,
        is_active=True,
        duration_minutes=60,
        price=900,
    )
    target_service = SimpleNamespace(
        id=2,
        master_id=2,
        base_service_id=5,
        is_active=True,
        duration_minutes=45,
        price=900,
    )
    source_master = SimpleNamespace(id=1, booking_redirect_master_id=2, services=[source_service])
    target_master = SimpleNamespace(id=2, booking_redirect_master_id=None, services=[target_service])
    customer = SimpleNamespace(id=7, phone="+380501112233", email=None, name="Customer", surname=None)
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        funnel_session_id="booking-redirect-attempt-123456",
    )

    class RedirectCreateBookingService(CreateBookingService):
        def __init__(self):
            super().__init__()
            self.checked_master_ids = []
            self.availability_master_ids = []

        async def get_active_service(self, session, service_id):
            return {1: source_service, 2: target_service}[service_id]

        async def ensure_booking_within_availability(self, session, master_id, start_at, end_at):
            self.availability_master_ids.append(master_id)

        async def ensure_slot_available(self, session, master_id, start_at, end_at):
            self.checked_master_ids.append(master_id)

    booking_service = RedirectCreateBookingService()
    session = FakeSession(execute_values=[source_master, target_master, customer])
    booking = await booking_service.create_public_booking(
        session,
        payload,
        record_funnel_success=True,
    )

    assert booking_service.availability_master_ids == [2]
    assert booking_service.checked_master_ids == [2]
    assert booking.master_id == 2
    assert booking.redirected_from_master_id == 1
    assert booking.service_id == 2
    assert booking.service_ids == [2]
    assert booking.end_at == at(10, 45)
    success_event = next(
        item
        for item in session.added_items
        if isinstance(item, BookingFunnelEvent)
        and item.event_type == BookingFunnelEventType.booking_success
    )
    assert success_event.master_id == 1


@pytest.mark.anyio
async def test_creating_booking_for_redirected_master_keeps_source_service_when_target_missing() -> None:
    source_service = SimpleNamespace(
        id=1,
        master_id=1,
        base_service_id=5,
        is_active=True,
        duration_minutes=60,
        price=900,
    )
    target_service = SimpleNamespace(
        id=2,
        master_id=2,
        base_service_id=6,
        is_active=True,
        duration_minutes=60,
        price=900,
    )
    source_master = SimpleNamespace(id=1, booking_redirect_master_id=2, services=[source_service])
    target_master = SimpleNamespace(id=2, booking_redirect_master_id=None, services=[target_service])
    customer = SimpleNamespace(id=7, phone="+380501112233", email=None, name="Customer", surname=None)
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
    )

    class MissingRedirectServiceCreateBookingService(CreateBookingService):
        async def get_active_service(self, session, service_id):
            return source_service

    booking = await MissingRedirectServiceCreateBookingService().create_public_booking(
        FakeSession(execute_values=[source_master, target_master, customer]),
        payload,
    )

    assert booking.master_id == 2
    assert booking.redirected_from_master_id == 1
    assert booking.service_id == 1
    assert booking.service_ids == [1]


def test_public_booking_response_does_not_include_redirected_from_master_id() -> None:
    now = datetime.now(tz=KYIV_TZ)
    booking = Booking(
        id=1,
        master_id=2,
        service_id=2,
        redirected_from_master_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        created_at=now,
        updated_at=now,
    )

    response = booking_routes.BookingResponse.model_validate(booking)

    assert "redirectedFromMasterId" not in response.model_dump(by_alias=True)


def test_backoffice_booking_response_includes_redirected_from_master() -> None:
    now = datetime.now(tz=KYIV_TZ)
    source_master = Master(
        id=1,
        full_name="Sick Barber",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    booking = Booking(
        id=1,
        master_id=2,
        service_id=2,
        redirected_from_master_id=1,
        redirected_from_master=source_master,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        created_at=now,
        updated_at=now,
    )

    response = BookingBackofficeResponse.model_validate(booking)

    assert response.redirected_from_master_id == 1
    assert response.redirected_from_master is not None
    assert response.redirected_from_master.full_name == "Sick Barber"
    assert response.model_dump(by_alias=True)["redirectedFromMasterId"] == 1


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

    async def fake_ensure_booking_within_availability(session, master_id, start_at, end_at):
        assert master_id == 1
        assert start_at == at(10, 30)
        assert end_at == at(12)

    monkeypatch.setattr(booking_routes.service, "ensure_booking_within_availability", fake_ensure_booking_within_availability)
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
async def test_barber_cannot_move_booking_outside_open_availability(monkeypatch: pytest.MonkeyPatch) -> None:
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
    slot_checked = False

    async def fake_ensure_booking_within_availability(session, master_id, start_at, end_at):
        raise HTTPException(status_code=409, detail="Booking slot is outside master's open availability")

    async def fake_ensure_slot_available(*_args, **_kwargs):
        nonlocal slot_checked
        slot_checked = True

    monkeypatch.setattr(booking_routes.service, "ensure_booking_within_availability", fake_ensure_booking_within_availability)
    monkeypatch.setattr(booking_routes.service, "ensure_slot_available", fake_ensure_slot_available)

    with pytest.raises(HTTPException) as exc_info:
        await update_my_booking(
            booking_id=1,
            payload=BookingUpdate(start_at=at(10, 30), end_at=at(12)),
            current_master=SimpleNamespace(id=1),
            session=FakeSession(get_value=booking),
        )

    assert exc_info.value.status_code == 409
    assert slot_checked is False


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

    async def fake_ensure_booking_within_availability(*_args, **_kwargs):
        raise AssertionError("superuser booking updates should bypass availability checks")

    monkeypatch.setattr(booking_routes.service, "ensure_booking_within_availability", fake_ensure_booking_within_availability)
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


def booking_pricing_item(*, status: BookingStatus = BookingStatus.confirmed) -> Booking:
    now = at(9)
    customer = Customer(
        id=7,
        phone="+380501112233",
        email=None,
        name="Customer",
        surname=None,
        created_at=now,
        updated_at=now,
    )
    barber_service = BarberService(
        id=1,
        master_id=2,
        base_service_id=None,
        name="Стрижка",
        title_uk="Стрижка",
        title_en="Haircut",
        description=None,
        duration_minutes=60,
        price=1500,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    service_item = BookingServiceItem(
        id=11,
        booking_id=1,
        service_id=1,
        position=0,
        price_amount=1500,
        service=barber_service,
        created_at=now,
        updated_at=now,
    )
    return Booking(
        id=1,
        master_id=2,
        service_id=1,
        service=barber_service,
        service_items=[service_item],
        customer_id=7,
        customer=customer,
        customer_name="Customer",
        customer_phone="+380501112233",
        customer_comment=None,
        start_at=at(10),
        end_at=at(11),
        status=status,
        cancelled_at=None,
        completed_at=at(11) if status == BookingStatus.completed else None,
        subtotal_amount=1500,
        promotion_discount_amount=0,
        manual_discount_amount=0,
        total_amount=1500,
        created_at=now,
        updated_at=now,
    )


def full_discount_promotion() -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        code="FREE100",
        name_uk="Безкоштовна послуга",
        name_en="Free service",
        discount_type=PromotionDiscountType.percent,
        discount_percent=100,
        eligibility_type=PromotionEligibilityType.all_customers,
        starts_at=None,
        ends_at=None,
        is_active=True,
        is_public=False,
        applies_to_all_masters=True,
        applies_to_all_services=True,
    )


def test_admin_booking_update_validates_service_prices_and_normalizes_promotion() -> None:
    payload = AdminBookingUpdate(
        service_prices=[{"service_id": 1, "price_amount": 0}],
        promotion_code=" free100 ",
    )

    assert payload.service_prices[0].price_amount == 0
    assert payload.promotion_code == "FREE100"

    with pytest.raises(ValueError):
        AdminBookingUpdate(service_prices=[
            {"service_id": 1, "price_amount": 100},
            {"service_id": 1, "price_amount": 200},
        ])

    with pytest.raises(ValueError):
        AdminBookingUpdate(service_prices=[{"service_id": 1, "price_amount": -1}])

    with pytest.raises(ValueError):
        AdminBookingUpdate(discount_amount=100)


@pytest.mark.anyio
async def test_admin_can_set_service_price_and_select_full_discount_on_completed_booking() -> None:
    booking = booking_pricing_item(status=BookingStatus.completed)
    promotion = full_discount_promotion()
    session = FakeSession(get_value=booking, execute_values=[booking, promotion, booking])

    response = await admin_update_booking(
        booking_id=1,
        payload=AdminBookingUpdate(
            service_prices=[{"service_id": 1, "price_amount": 1200}],
            promotion_code="FREE100",
        ),
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=session,
    )

    assert response.service_prices == {1: 1200}
    assert response.subtotal_amount == 1200
    assert response.discount_amount == 1200
    assert response.total_amount == 0
    assert response.promotion_code == "FREE100"
    assert response.promotion_discount_percent == 100
    assert booking.service_items[0].price_amount == 1200
    assert booking.manual_discount_amount == 0
    assert session.committed is True


@pytest.mark.anyio
async def test_admin_can_remove_selected_promotion_without_resetting_service_price() -> None:
    booking = booking_pricing_item()
    booking.promotion_id = 10
    booking.promotion_code_snapshot = "FREE100"
    booking.promotion_name_uk_snapshot = "Безкоштовна послуга"
    booking.promotion_name_en_snapshot = "Free service"
    booking.promotion_discount_percent_snapshot = 100
    booking.promotion_discount_amount = 1500
    booking.total_amount = 0
    session = FakeSession(get_value=booking, execute_values=[booking, booking])

    response = await admin_update_booking(
        booking_id=1,
        payload=AdminBookingUpdate(
            service_prices=[{"service_id": 1, "price_amount": 1200}],
            promotion_code=None,
        ),
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=session,
    )

    assert response.service_prices == {1: 1200}
    assert response.subtotal_amount == 1200
    assert response.discount_amount == 0
    assert response.total_amount == 1200
    assert response.promotion_code is None
    assert booking.service_items[0].price_amount == 1200


@pytest.mark.anyio
async def test_admin_service_change_recalculates_booking_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking = booking_pricing_item()
    replacement_service = BarberService(
        id=2,
        master_id=2,
        base_service_id=None,
        name="Стрижка та борода",
        title_uk="Стрижка та борода",
        title_en="Haircut and beard",
        description=None,
        duration_minutes=90,
        price=1900,
        is_active=True,
        created_at=at(9),
        updated_at=at(9),
    )
    captured: dict[str, object] = {}

    class PricingService:
        def __init__(self) -> None:
            self.promotion_service = self

        async def get_active_master_with_services(self, _session, _master_id):
            return SimpleNamespace(id=2, services=[replacement_service])

        async def get_active_services(self, _session, _service_ids):
            return [replacement_service]

        def ensure_master_provides_services(self, _master, _service_ids) -> None:
            return None

        def ensure_valid_interval(self, start_at, end_at):
            return start_at, end_at

        def ensure_not_past(self, _start_at) -> None:
            return None

        def ensure_within_open_business_days(self, _start_at, _end_at) -> None:
            return None

        async def ensure_slot_available(self, *_args, **_kwargs) -> None:
            return None

        async def update_booking_services(
            self,
            _session,
            target_booking,
            services,
            service_prices=None,
        ) -> None:
            target_booking.service_id = services[0].id
            target_booking.service = services[0]
            target_booking.service_items = [
                BookingServiceItem(
                    id=12,
                    booking_id=target_booking.id,
                    service_id=services[0].id,
                    position=0,
                    price_amount=int((service_prices or {}).get(services[0].id, services[0].price)),
                    service=services[0],
                    created_at=at(9),
                    updated_at=at(9),
                )
            ]

        async def apply_to_booking(
            self,
            _session,
            *,
            booking,
            promotion_code,
            services,
            service_prices,
            **_kwargs,
        ) -> None:
            captured["promotion_code"] = promotion_code
            captured["service_prices"] = service_prices
            booking.subtotal_amount = sum(service_prices[item.id] for item in services)
            booking.promotion_discount_amount = 0
            booking.manual_discount_amount = 0
            booking.total_amount = booking.subtotal_amount

    monkeypatch.setattr(booking_routes, "service", PricingService())
    session = FakeSession(get_value=booking, execute_values=[booking, booking])

    response = await admin_update_booking(
        booking_id=1,
        payload=AdminBookingUpdate(service_ids=[2]),
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=session,
    )

    assert response.service_ids == [2]
    assert response.service_prices == {2: 1900}
    assert response.subtotal_amount == 1900
    assert response.total_amount == 1900
    assert captured == {"promotion_code": None, "service_prices": {2: 1900}}


@pytest.mark.anyio
async def test_non_admin_cannot_update_booking_prices_or_promotion() -> None:
    booking = booking_pricing_item()

    with pytest.raises(HTTPException) as exc_info:
        await admin_update_booking(
            booking_id=1,
            payload=AdminBookingUpdate(
                service_prices=[{"service_id": 1, "price_amount": 1000}],
                promotion_code="FREE100",
            ),
            current_user=SimpleNamespace(id=10, is_superuser=False),
            session=FakeSession(get_value=booking),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only administrators can update booking prices and promotions"


@pytest.mark.anyio
async def test_admin_booking_prices_must_match_selected_services() -> None:
    booking = booking_pricing_item()
    session = FakeSession(get_value=booking, execute_values=[booking])

    with pytest.raises(HTTPException) as exc_info:
        await admin_update_booking(
            booking_id=1,
            payload=AdminBookingUpdate(
                service_prices=[{"service_id": 2, "price_amount": 1200}],
                promotion_code=None,
            ),
            current_user=SimpleNamespace(id=99, is_superuser=True),
            session=session,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "service_prices must match booking services"
    assert session.committed is False


def test_booking_discount_amount_keeps_legacy_manual_value_readable() -> None:
    booking = Booking(
        promotion_discount_amount=150,
        manual_discount_amount=50,
    )

    assert booking.discount_amount == 200


@pytest.mark.anyio
async def test_admin_can_update_booking_outside_working_hours(monkeypatch: pytest.MonkeyPatch) -> None:
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

    async def fake_ensure_booking_within_availability(*_args, **_kwargs):
        raise AssertionError("superuser booking updates should bypass availability checks")

    monkeypatch.setattr(booking_routes.service, "ensure_booking_within_availability", fake_ensure_booking_within_availability)
    monkeypatch.setattr(booking_routes.service, "ensure_slot_available", fake_ensure_slot_available)

    response = await admin_update_booking(
        booking_id=1,
        payload=BookingUpdate(start_at=at(21), end_at=at(22)),
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=FakeSession(get_value=booking, execute_values=[booking]),
    )

    assert response.start_at == at(21)
    assert response.end_at == at(22)
    assert checked_slot == {
        "master_id": 2,
        "start_at": at(21),
        "end_at": at(22),
        "exclude_booking_id": 1,
    }


@pytest.mark.anyio
async def test_admin_update_booking_still_rejects_monday(monkeypatch: pytest.MonkeyPatch) -> None:
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
    slot_checked = False

    async def fake_ensure_slot_available(*_args, **_kwargs):
        nonlocal slot_checked
        slot_checked = True

    monkeypatch.setattr(booking_routes.service, "ensure_slot_available", fake_ensure_slot_available)

    with pytest.raises(HTTPException) as exc_info:
        await admin_update_booking(
            booking_id=1,
            payload=BookingUpdate(start_at=monday_at(10), end_at=monday_at(11)),
            current_user=SimpleNamespace(id=99, is_superuser=True),
            session=FakeSession(get_value=booking),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Barbershop is closed on Mondays"
    assert slot_checked is False


@pytest.mark.anyio
async def test_master_user_admin_booking_list_is_scoped_to_linked_master() -> None:
    booking = booking_response_item(at(10), at(11))
    session = RecordingFakeSession(execute_values=[SimpleNamespace(id=1), 1, [booking]])

    response = await booking_routes.admin_list_bookings(
        pagination=SimpleNamespace(page=1, page_size=20),
        master_id=None,
        date_from=None,
        date_to=None,
        booking_status=None,
        current_user=SimpleNamespace(id=10, is_superuser=False),
        session=session,
    )

    compiled = str(session.statements[-1].compile(compile_kwargs={"literal_binds": True}))
    assert response.total == 1
    assert response.items[0].master_id == 1
    assert "bookings.master_id = 1" in compiled


@pytest.mark.anyio
async def test_master_user_admin_booking_list_rejects_another_master_filter() -> None:
    session = RecordingFakeSession(execute_values=[SimpleNamespace(id=1)])

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.admin_list_bookings(
            pagination=SimpleNamespace(page=1, page_size=20),
            master_id=2,
            date_from=None,
            date_to=None,
            booking_status=None,
            current_user=SimpleNamespace(id=10, is_superuser=False),
            session=session,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Cannot view another master's bookings"
    assert len(session.statements) == 1


@pytest.mark.anyio
async def test_superuser_admin_booking_list_resolves_redirect_master_filter_to_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booking = booking_response_item(at(10), at(11))
    booking.master_id = 2
    session = RecordingFakeSession(execute_values=[1, [booking]])

    async def fake_resolve_booking_master(_session, master_id, **_kwargs):
        return SimpleNamespace(id=master_id), SimpleNamespace(id=2)

    monkeypatch.setattr(booking_routes.service, "resolve_booking_master", fake_resolve_booking_master)

    response = await booking_routes.admin_list_bookings(
        pagination=SimpleNamespace(page=1, page_size=20),
        master_id=1,
        date_from=None,
        date_to=None,
        booking_status=None,
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=session,
    )

    compiled = str(session.statements[-1].compile(compile_kwargs={"literal_binds": True}))
    assert response.total == 1
    assert response.items[0].master_id == 2
    assert "bookings.master_id = 2" in compiled


@pytest.mark.anyio
async def test_superuser_admin_availability_list_resolves_redirect_master_filter_to_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MasterAvailabilityWindow(
        id=1,
        master_id=2,
        start_at=at(8),
        end_at=at(20),
        created_at=at(7),
        updated_at=at(7),
    )
    session = RecordingFakeSession(execute_values=[[window]])

    async def fake_resolve_booking_master(_session, master_id, **_kwargs):
        return SimpleNamespace(id=master_id), SimpleNamespace(id=2)

    monkeypatch.setattr(booking_routes.service, "resolve_booking_master", fake_resolve_booking_master)

    response = await booking_routes.admin_list_availability(
        date_from=at(0),
        date_to=at(23),
        master_id=1,
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=session,
    )

    compiled = str(session.statements[-1].compile(compile_kwargs={"literal_binds": True}))
    assert response[0].master_id == 2
    assert "master_availability_windows.master_id = 2" in compiled


@pytest.mark.anyio
async def test_superuser_admin_time_block_list_resolves_redirect_master_filter_to_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = MasterTimeBlock(
        id=1,
        master_id=2,
        start_at=at(10),
        end_at=at(11),
        reason=None,
        created_at=at(9),
        updated_at=at(9),
    )
    session = RecordingFakeSession(execute_values=[1, [block]])

    async def fake_resolve_booking_master(_session, master_id, **_kwargs):
        return SimpleNamespace(id=master_id), SimpleNamespace(id=2)

    monkeypatch.setattr(booking_routes.service, "resolve_booking_master", fake_resolve_booking_master)

    response = await booking_routes.admin_list_time_blocks(
        pagination=SimpleNamespace(page=1, page_size=20),
        master_id=1,
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=session,
    )

    compiled = str(session.statements[-1].compile(compile_kwargs={"literal_binds": True}))
    assert response.total == 1
    assert response.items[0].master_id == 2
    assert "master_time_blocks.master_id = 2" in compiled


@pytest.mark.anyio
async def test_admin_can_create_booking_in_past(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=past_at(10),
    )
    booking = booking_response_item(past_at(10), past_at(11))
    captured = {}

    class FakeBookingService:
        async def create_public_booking(
            self,
            session,
            payload,
            *,
            allow_past=False,
            allow_private_promotions=False,
            require_availability=True,
            require_working_hours=True,
        ):
            captured["allow_past"] = allow_past
            captured["allow_private_promotions"] = allow_private_promotions
            captured["require_availability"] = require_availability
            captured["require_working_hours"] = require_working_hours
            return booking

    monkeypatch.setattr(booking_routes, "service", FakeBookingService())

    response = await booking_routes.admin_create_booking(
        payload=payload,
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=FakeSession(execute_values=[booking]),
    )

    assert captured["allow_past"] is True
    assert captured["allow_private_promotions"] is True
    assert captured["require_availability"] is False
    assert captured["require_working_hours"] is False
    assert response.start_at == past_at(10)


@pytest.mark.anyio
async def test_master_user_cannot_create_booking_in_past_through_admin_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=past_at(10),
    )
    captured = {"called": False}

    class FakeBookingService:
        async def create_public_booking(self, session, payload, *, allow_past=False):
            captured["called"] = True

    monkeypatch.setattr(booking_routes, "service", FakeBookingService())

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.admin_create_booking(
            payload=payload,
            current_user=SimpleNamespace(id=10, is_superuser=False),
            session=FakeSession(),
        )

    assert exc_info.value.status_code == 403
    assert captured["called"] is False


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
async def test_admin_can_create_time_block_in_past() -> None:
    session = FakeSession()
    service = BookingServiceLayer()
    block = await service.create_time_block(
        session,
        SimpleNamespace(id=1),
        MasterTimeBlockCreate(start_at=past_at(12), end_at=past_at(13), reason="Past slot"),
    )

    assert block.master_id == 1
    assert block.start_at == past_at(12)
    assert block.end_at == past_at(13)
    assert session.committed is True


@pytest.mark.anyio
async def test_admin_can_update_time_block_to_past_interval() -> None:
    block = MasterTimeBlock(
        id=7,
        master_id=1,
        start_at=at(12),
        end_at=at(13),
        reason="Original",
        created_at=datetime.now(tz=KYIV_TZ),
        updated_at=datetime.now(tz=KYIV_TZ),
    )
    session = FakeSession(get_value=block)

    response = await admin_update_time_block(
        block_id=7,
        payload=AdminMasterTimeBlockUpdate(start_at=past_at(9), end_at=past_at(10), reason="Edited"),
        current_user=SimpleNamespace(is_superuser=True),
        session=session,
    )

    assert response.start_at == past_at(9)
    assert response.end_at == past_at(10)
    assert response.reason == "Edited"
    assert block.start_at == past_at(9)
    assert block.end_at == past_at(10)
    assert session.committed is True


@pytest.mark.anyio
async def test_barber_can_open_full_availability_day() -> None:
    target_date = next_open_date()
    day_start, day_end = BookingServiceLayer().day_bounds(target_date)
    session = FakeSession(execute_values=[None])

    response = await booking_routes.create_my_availability_days(
        payload=MasterAvailabilityDaysCreate(dates=[target_date]),
        current_master=SimpleNamespace(id=1),
        session=session,
    )

    assert len(response) == 1
    assert response[0].master_id == 1
    assert response[0].start_at == day_start
    assert response[0].end_at == day_end
    assert session.committed is True


@pytest.mark.anyio
async def test_barber_can_open_partial_availability_window() -> None:
    target_date = next_open_date()
    start_at = datetime.combine(target_date, datetime.min.time(), tzinfo=KYIV_TZ).replace(hour=10)
    end_at = start_at + timedelta(hours=4)
    session = FakeSession(execute_values=[None])

    response = await booking_routes.create_my_availability_window(
        payload=MasterAvailabilityWindowCreate(start_at=start_at, end_at=end_at),
        current_master=SimpleNamespace(id=1),
        session=session,
    )

    assert response.master_id == 1
    assert response.start_at == start_at
    assert response.end_at == end_at
    assert session.committed is True


@pytest.mark.anyio
async def test_availability_cannot_be_opened_on_monday() -> None:
    monday = date(2099, 1, 5)

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.create_my_availability_days(
            payload=MasterAvailabilityDaysCreate(dates=[monday]),
            current_master=SimpleNamespace(id=1),
            session=FakeSession(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Barbershop is closed on Mondays"


@pytest.mark.anyio
async def test_availability_cannot_be_opened_outside_two_month_horizon() -> None:
    target_date = outside_availability_horizon_date()

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.create_my_availability_days(
            payload=MasterAvailabilityDaysCreate(dates=[target_date]),
            current_master=SimpleNamespace(id=1),
            session=FakeSession(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Availability can only be opened within the next 2 months"


@pytest.mark.anyio
async def test_availability_windows_cannot_overlap() -> None:
    target_date = next_open_date()
    start_at = datetime.combine(target_date, datetime.min.time(), tzinfo=KYIV_TZ).replace(hour=10)
    end_at = start_at + timedelta(hours=2)
    session = FakeSession(execute_values=[1])

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.create_my_availability_window(
            payload=MasterAvailabilityWindowCreate(start_at=start_at, end_at=end_at),
            current_master=SimpleNamespace(id=1),
            session=session,
        )

    assert exc_info.value.status_code == 409
    assert session.rolled_back is True


@pytest.mark.anyio
async def test_barber_cannot_delete_availability_with_active_booking() -> None:
    target_date = next_open_date()
    start_at = datetime.combine(target_date, datetime.min.time(), tzinfo=KYIV_TZ).replace(hour=10)
    window = MasterAvailabilityWindow(id=7, master_id=1, start_at=start_at, end_at=start_at + timedelta(hours=4))
    booking = SimpleNamespace(id=12)
    session = FakeSession(get_value=window, execute_values=[[booking]])

    with pytest.raises(HTTPException) as exc_info:
        await booking_routes.delete_my_availability_window(
            window_id=7,
            current_master=SimpleNamespace(id=1),
            session=session,
        )

    assert exc_info.value.status_code == 409
    assert session.deleted is None


@pytest.mark.anyio
async def test_admin_can_delete_availability_with_active_booking() -> None:
    target_date = next_open_date()
    start_at = datetime.combine(target_date, datetime.min.time(), tzinfo=KYIV_TZ).replace(hour=10)
    window = MasterAvailabilityWindow(id=7, master_id=1, start_at=start_at, end_at=start_at + timedelta(hours=4))
    session = FakeSession(get_value=window, execute_values=[[SimpleNamespace(id=12)]])

    await booking_routes.admin_delete_availability_window(
        window_id=7,
        current_user=SimpleNamespace(is_superuser=True),
        session=session,
    )

    assert session.deleted is window
    assert session.committed is True


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
    promotion = SimpleNamespace(
        id=50,
        code="ZSU50",
        name_uk="Знижка для захисників",
        name_en="Defender discount",
        discount_type="percent",
        discount_percent=50,
        applies_to_all_masters=False,
        master_ids=[10, 11, 12],
        applies_to_all_services=False,
        base_service_ids=[5],
    )

    response = await booking_routes.list_public_service_catalog(session=FakeSession(execute_values=[services, [promotion]]))

    assert len(response) == 2
    assert response[0].name == "Стрижка"
    assert response[0].title_uk == "Стрижка"
    assert response[0].title_en == "Haircut"
    assert response[0].price == 900
    assert response[0].active_promotion is not None
    assert response[0].active_promotion.code == "ZSU50"
    assert response[0].active_promotion.discount_amount == 450
    assert response[0].barber_services[0].active_promotion is not None
    assert response[0].barber_services[0].active_promotion.promotional_price == 450
    assert response[0].barber_ids == [10, 11]
    assert response[0].barber_service_ids == [1, 2]
    assert response[1].price == 1100
    assert response[1].barber_ids == [12]


@pytest.mark.anyio
async def test_public_service_catalog_omits_inactive_base_services() -> None:
    now = datetime.now(tz=KYIV_TZ)
    inactive_base_service = BaseService(
        id=5,
        name="Дитяча стрижка",
        duration_minutes=45,
        price=700,
        is_active=False,
        created_at=now,
        updated_at=now,
    )
    active_service = BarberService(
        id=1,
        master_id=10,
        base_service_id=5,
        base_service=inactive_base_service,
        name="Дитяча стрижка",
        title_uk="Дитяча стрижка",
        title_en="Kids haircut",
        duration_minutes=45,
        price=700,
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    response = await booking_routes.list_public_service_catalog(session=FakeSession(execute_values=[[active_service], []]))

    assert response == []


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
