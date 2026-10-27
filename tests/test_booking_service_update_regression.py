"""Regression for SQLAlchemy's loaded booking-service relationships."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.api.v1.routes import bookings as routes
from app.core.database import Base
from app.models.booking import BarberService, BaseService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.customer import Customer
from app.models.promotion import Promotion, PromotionEligibilityType, promotion_masters, promotion_base_services
from app.schemas.booking import AdminBookingUpdate, BookingUpdate


@pytest.fixture
def anyio_backend():
    return "asyncio"


class AsyncSessionAdapter:
    """Use a real SQLAlchemy identity map with the route's async interface."""
    def __init__(self, session):
        self.session = session

    async def execute(self, statement, *args, **kwargs):
        return self.session.execute(statement, *args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    def add(self, item):
        self.session.add(item)


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["self", "master", "admin"])
async def test_service_change_updates_loaded_relationship_before_validation_and_response(route, monkeypatch):
    engine = create_engine("sqlite://")
    tables = [Master.__table__, BaseService.__table__, BarberService.__table__, Customer.__table__,
              Promotion.__table__, promotion_masters, promotion_base_services,
              Booking.__table__, BookingServiceItem.__table__]
    Base.metadata.create_all(engine, tables=tables)
    start = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
    with Session(engine, expire_on_commit=False) as db:
        master = Master(full_name="Test master")
        db.add(master)
        db.flush()
        original = BarberService(master_id=master.id, name="Original", duration_minutes=30, price=100)
        replacement = BarberService(master_id=master.id, name="Replacement", duration_minutes=45, price=200)
        db.add_all([original, replacement])
        db.flush()
        promotion = Promotion(code="TEST", name_uk="Test", name_en="Test", discount_percent=20)
        db.add(promotion)
        db.flush()
        booking = Booking(promotion_id=promotion.id, promotion_discount_percent_snapshot=20,
                          promotion_eligibility_type_snapshot=PromotionEligibilityType.all_customers,
                          master_id=master.id, service_id=original.id, customer_name="Test",
                          customer_phone="+380501234567", start_at=start,
                          end_at=start + timedelta(minutes=30), status=BookingStatus.confirmed)
        db.add(booking)
        db.flush()
        db.add(BookingServiceItem(booking_id=booking.id, service_id=original.id, position=0, price_amount=100))
        db.commit()
        booking_id, master_id, replacement_id = booking.id, master.id, replacement.id

    seen = []
    prices_seen = []
    async def noop(*args, **kwargs):
        return None
    async def validate(_session, updated):
        seen.append((updated.service.id, [item.id for item in updated.services], updated.service_prices))
    original_apply = routes.service.promotion_service.apply_to_booking
    async def apply(_session, *, booking, services, service_prices, **kwargs):
        prices_seen.append((booking.service.id, [item.id for item in booking.services],
                            booking.service_prices, service_prices))
        await original_apply(_session, booking=booking, services=services, service_prices=service_prices, **kwargs)
    monkeypatch.setattr(routes.service, "ensure_booking_within_availability", noop)
    monkeypatch.setattr(routes.service, "ensure_slot_available", noop)
    monkeypatch.setattr(routes.service, "ensure_within_working_hours", lambda *args: None)
    monkeypatch.setattr(routes.service, "ensure_within_open_business_days", lambda *args: None)
    monkeypatch.setattr(routes.service.promotion_service, "apply_to_booking", apply)
    monkeypatch.setattr(routes.service.promotion_service, "revalidate_first_visit_booking", validate)
    async def linked_master(_session, _user):
        return SimpleNamespace(id=master_id)
    monkeypatch.setattr(routes, "get_linked_master_for_user", linked_master)

    with Session(engine, expire_on_commit=False) as db:
        session = AsyncSessionAdapter(db)
        if route == "self":
            response = await routes.update_my_booking(booking_id, BookingUpdate(service_ids=[replacement_id]),
                current_master=SimpleNamespace(id=master_id), session=session)
        else:
            response = await routes.admin_update_booking(booking_id, AdminBookingUpdate(service_ids=[replacement_id]),
                current_user=SimpleNamespace(is_superuser=route == "admin"), session=session)
        assert seen == [(replacement_id, [replacement_id], {replacement_id: 200})]
        assert prices_seen == [(replacement_id, [replacement_id], {replacement_id: 200}, {replacement_id: 200})]
        assert response.service_id == replacement_id
        assert response.service_ids == [replacement_id]
        assert [item.id for item in response.services] == [replacement_id]
        assert response.service_prices == {replacement_id: 200}
        assert response.subtotal_amount == 200
        assert response.promotion_discount_percent == 20
        assert response.total_amount == 160
    with Session(engine) as db:
        saved = db.scalars(select(Booking).options(selectinload(Booking.service_items))).one()
        assert saved.service_ids == [replacement_id]
    engine.dispose()
