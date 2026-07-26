from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.v1.routes import bookings as booking_routes
from app.api.v1.routes import promotions as promotion_routes
from app.models.booking import BarberService, Booking, BookingStatus
from app.models.promotion import Promotion, PromotionDiscountType, PromotionEligibilityType
from app.schemas.booking import AdminBookingCreate, PublicBookingCreate
from app.schemas.promotion import PromotionCreate
from app.services.promotion import PromotionService

KYIV_TZ = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeExecuteResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


class FakeSession:
    def __init__(self, execute_values=None, get_value=None):
        self.execute_values = list(execute_values or [])
        self.get_value = get_value
        self.added = None
        self.committed = False
        self.refreshed = None

    async def execute(self, _statement):
        if self.execute_values:
            return FakeExecuteResult(self.execute_values.pop(0))
        return FakeExecuteResult(None)

    async def get(self, _model, _entity_id):
        return self.get_value

    def add(self, instance):
        self.added = instance
        if getattr(instance, "id", None) is None:
            instance.id = 1
        if getattr(instance, "created_at", None) is None:
            instance.created_at = now()
        if getattr(instance, "updated_at", None) is None:
            instance.updated_at = instance.created_at

    async def commit(self):
        self.committed = True

    async def refresh(self, instance):
        self.refreshed = instance


def now() -> datetime:
    return datetime.now(tz=KYIV_TZ)


def at(hour: int) -> datetime:
    return datetime(2099, 1, 2, hour, tzinfo=KYIV_TZ)


def inactive_promotion() -> Promotion:
    timestamp = now()
    return Promotion(
        id=5,
        code="COMEBACK15",
        name_uk="Повернення клієнта",
        name_en="Comeback client",
        discount_type=PromotionDiscountType.percent,
        discount_percent=15,
        eligibility_type=PromotionEligibilityType.inactive_customers,
        inactive_days=90,
        is_active=True,
        created_at=timestamp,
        updated_at=timestamp,
    )


def booking_item() -> Booking:
    timestamp = now()
    service = BarberService(
        id=1,
        master_id=1,
        name="Cut",
        duration_minutes=60,
        price=1000,
        is_active=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return Booking(
        id=1,
        master_id=1,
        service_id=1,
        service=service,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        end_at=at(11),
        status=BookingStatus.confirmed,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_promotion_create_normalizes_code_and_defaults_inactive_days() -> None:
    payload = PromotionCreate(
        code=" comeback15 ",
        name_uk="Повернення",
        name_en="Comeback",
        discount_percent=15,
        eligibility_type=PromotionEligibilityType.inactive_customers,
    )

    assert payload.code == "COMEBACK15"
    assert payload.inactive_days == 90


def test_promotion_create_accepts_one_hundred_percent_discount() -> None:
    payload = PromotionCreate(
        code="FREE100",
        name_uk="Безкоштовна послуга",
        name_en="Free service",
        discount_percent=100,
    )

    assert payload.discount_percent == 100


@pytest.mark.anyio
async def test_promotion_service_applies_inactive_customer_discount() -> None:
    booking = booking_item()
    promotion = inactive_promotion()
    customer = SimpleNamespace(id=7, imported_last_visit_at=None)
    services = [SimpleNamespace(price=1000), SimpleNamespace(price=500)]

    await PromotionService().apply_to_booking(
        FakeSession(execute_values=[promotion, None]),
        booking=booking,
        promotion_code="COMEBACK15",
        customer=customer,
        services=services,
        at=booking.start_at,
    )

    assert booking.promotion_id == promotion.id
    assert booking.promotion_code == "COMEBACK15"
    assert booking.promotion_name_uk == "Повернення клієнта"
    assert booking.subtotal_amount == 1500
    assert booking.discount_amount == 225
    assert booking.total_amount == 1275


@pytest.mark.anyio
async def test_promotion_service_applies_full_discount() -> None:
    booking = booking_item()
    promotion = SimpleNamespace(
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
    services = [SimpleNamespace(price=1000), SimpleNamespace(price=500)]

    await PromotionService().apply_to_booking(
        FakeSession(execute_values=[promotion]),
        booking=booking,
        promotion_code="FREE100",
        customer=SimpleNamespace(id=7, imported_last_visit_at=None),
        services=services,
        at=booking.start_at,
        allow_private_promotions=True,
    )

    assert booking.subtotal_amount == 1500
    assert booking.discount_amount == 1500
    assert booking.total_amount == 0


@pytest.mark.anyio
async def test_promotion_service_rejects_private_promotion_for_public_booking() -> None:
    booking = booking_item()
    promotion = SimpleNamespace(
        id=10,
        code="FREE100",
        starts_at=None,
        ends_at=None,
        is_active=True,
        is_public=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        await PromotionService().apply_to_booking(
            FakeSession(execute_values=[promotion]),
            booking=booking,
            promotion_code="FREE100",
            customer=SimpleNamespace(id=7, imported_last_visit_at=None),
            services=[SimpleNamespace(price=1000)],
            at=booking.start_at,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Promotion is not available for public booking"


@pytest.mark.anyio
async def test_promotion_service_discounts_only_scoped_services() -> None:
    booking = booking_item()
    promotion = SimpleNamespace(
        id=9,
        code="ZSU50",
        name_uk="Знижка для захисників",
        name_en="Defender discount",
        discount_type=PromotionDiscountType.percent,
        discount_percent=50,
        eligibility_type=PromotionEligibilityType.military_customers,
        starts_at=None,
        ends_at=None,
        is_active=True,
        applies_to_all_masters=False,
        master_ids=[1],
        applies_to_all_services=False,
        base_service_ids=[10],
    )
    customer = SimpleNamespace(id=7, imported_last_visit_at=None)
    services = [
        SimpleNamespace(price=1000, master_id=1, base_service_id=10),
        SimpleNamespace(price=500, master_id=1, base_service_id=11),
        SimpleNamespace(price=800, master_id=2, base_service_id=10),
    ]

    await PromotionService().apply_to_booking(
        FakeSession(execute_values=[promotion]),
        booking=booking,
        promotion_code="ZSU50",
        customer=customer,
        services=services,
        at=booking.start_at,
    )

    assert booking.subtotal_amount == 2300
    assert booking.discount_amount == 500
    assert booking.total_amount == 1800


@pytest.mark.anyio
async def test_promotion_service_rejects_promo_without_matching_services() -> None:
    booking = booking_item()
    promotion = SimpleNamespace(
        id=9,
        code="ZSU50",
        name_uk="Знижка для захисників",
        name_en="Defender discount",
        discount_type=PromotionDiscountType.percent,
        discount_percent=50,
        eligibility_type=PromotionEligibilityType.military_customers,
        starts_at=None,
        ends_at=None,
        is_active=True,
        applies_to_all_masters=False,
        master_ids=[2],
        applies_to_all_services=False,
        base_service_ids=[10],
    )

    with pytest.raises(HTTPException) as exc_info:
        await PromotionService().apply_to_booking(
            FakeSession(execute_values=[promotion]),
            booking=booking,
            promotion_code="ZSU50",
            customer=SimpleNamespace(id=7, imported_last_visit_at=None),
            services=[SimpleNamespace(price=1000, master_id=1, base_service_id=10)],
            at=booking.start_at,
        )

    assert exc_info.value.status_code == 400
    assert "does not apply" in exc_info.value.detail


@pytest.mark.anyio
async def test_promotion_service_rejects_recent_customer_for_inactive_promo() -> None:
    booking = booking_item()
    promotion = inactive_promotion()
    customer = SimpleNamespace(id=7, imported_last_visit_at=booking.start_at - timedelta(days=10))

    with pytest.raises(HTTPException) as exc_info:
        await PromotionService().apply_to_booking(
            FakeSession(execute_values=[promotion, None]),
            booking=booking,
            promotion_code="COMEBACK15",
            customer=customer,
            services=[SimpleNamespace(price=1000)],
            at=booking.start_at,
        )

    assert exc_info.value.status_code == 400
    assert "inactive for 90 days" in exc_info.value.detail


@pytest.mark.anyio
async def test_master_user_cannot_manage_promotions() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await promotion_routes.create_promotion(
            payload=PromotionCreate(
                code="VIP15",
                name_uk="VIP",
                name_en="VIP",
                discount_percent=15,
            ),
            current_user=SimpleNamespace(is_superuser=False),
            session=FakeSession(),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_admin_booking_route_passes_promotion_code(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = AdminBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        promotionCode="COMEBACK15",
    )
    booking = booking_item()
    captured = {}

    class FakeBookingService:
        async def create_public_booking(self, session, payload, **kwargs):
            captured.update(kwargs)
            return booking

    monkeypatch.setattr(booking_routes, "service", FakeBookingService())

    response = await booking_routes.admin_create_booking(
        payload=payload,
        current_user=SimpleNamespace(id=99, is_superuser=True),
        session=FakeSession(execute_values=[booking]),
    )

    assert captured["promotion_code"] == "COMEBACK15"
    assert captured["allow_past"] is True
    assert captured["allow_private_promotions"] is True
    assert response.id == booking.id


@pytest.mark.anyio
async def test_public_booking_route_passes_promotion_code(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = PublicBookingCreate(
        master_id=1,
        service_id=1,
        customer_name="Customer",
        customer_phone="+380501112233",
        start_at=at(10),
        promotionCode="ZSU50",
    )
    booking = booking_item()
    captured = {}

    class FakeBookingService:
        async def create_public_booking(self, session, payload, **kwargs):
            captured.update(kwargs)
            return booking

    monkeypatch.setattr(booking_routes, "service", FakeBookingService())
    monkeypatch.setattr(booking_routes, "should_send_booking_notifications", lambda _booking: False)

    response = await booking_routes.create_public_booking(
        payload=payload,
        background_tasks=BackgroundTasks(),
        current_user=None,
        session=FakeSession(execute_values=[booking]),
    )

    assert captured["promotion_code"] == "ZSU50"
    assert captured["allow_past"] is False
    assert captured["allow_private_promotions"] is False
    assert response.id == booking.id
