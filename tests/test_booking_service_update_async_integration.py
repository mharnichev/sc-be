"""Async PostgreSQL/HTTP regression: no synchronous adapter or provider lifespan."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.v1.routes import bookings as routes
from app.models.admin_user import AdminUser
from app.models.booking import (
    BarberService, Booking, BookingServiceItem, BookingStatus, Master, MasterAvailabilityWindow,
)
from app.models.customer import Customer
from app.models.promotion import Promotion, PromotionEligibilityType
from tests.test_segments_integration import anyio_backend, database  # noqa: F401

pytestmark = pytest.mark.anyio


async def seed(database, *, customer=True, promotion=True):
    start = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=2)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    while start.weekday() == 6:
        start += timedelta(days=1)
    async with database() as session:
        admin = AdminUser(email="admin@example.test", hashed_password="unused", is_superuser=True)
        barber = AdminUser(email="barber@example.test", hashed_password="unused", is_superuser=False)
        session.add_all([admin, barber])
        await session.flush()
        master = Master(full_name="Async test master", admin_user_id=barber.id)
        client = Customer(phone="+380500001234", name="Async test customer") if customer else None
        session.add(master)
        if client:
            session.add(client)
        await session.flush()
        original = BarberService(master_id=master.id, name="Original", duration_minutes=30, price=100)
        replacement = BarberService(master_id=master.id, name="Replacement", duration_minutes=45, price=200)
        discount = Promotion(code="ASYNC", name_uk="Test", name_en="Test", discount_percent=20) if promotion else None
        session.add_all([original, replacement])
        if discount:
            session.add(discount)
        await session.flush()
        booking = Booking(
            master_id=master.id, service_id=original.id, customer_id=client.id if client else None,
            customer_name="Async test customer", customer_phone="+380500001234",
            start_at=start, end_at=start + timedelta(minutes=30), status=BookingStatus.confirmed,
            promotion_id=discount.id if discount else None,
            promotion_discount_percent_snapshot=20 if discount else None,
            promotion_eligibility_type_snapshot=PromotionEligibilityType.all_customers if discount else None,
            subtotal_amount=100, promotion_discount_amount=20 if discount else 0,
            manual_discount_amount=0, total_amount=80 if discount else 100,
        )
        session.add(booking)
        await session.flush()
        session.add_all([
            BookingServiceItem(booking_id=booking.id, service_id=original.id, position=0, price_amount=100),
            MasterAvailabilityWindow(master_id=master.id, start_at=start - timedelta(hours=1),
                                     end_at=start + timedelta(hours=2)),
        ])
        await session.commit()
        return dict(booking=booking.id, master=master.id, admin=admin.id, barber=barber.id,
                    original=original.id, replacement=replacement.id,
                    customer=client.id if client else None, promotion=discount.id if discount else None,
                    start=start)


def client_for(database, ids, actor):
    app = FastAPI()
    app.include_router(routes.backoffice_router, prefix="/api/v1/backoffice")

    async def session_dependency():
        async with database() as session:
            yield session

    async def user_dependency():
        async with database() as session:
            return await session.get(AdminUser, ids["admin" if actor == "admin" else "barber"])

    async def master_dependency():
        async with database() as session:
            return await session.get(Master, ids["master"])

    app.dependency_overrides[routes.get_db_session] = session_dependency
    app.dependency_overrides[routes.get_current_admin_user] = user_dependency
    app.dependency_overrides[routes.get_current_master] = master_dependency
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def path(ids, actor):
    prefix = "/masters/me" if actor == "self" else ""
    return f"/api/v1/backoffice{prefix}/bookings/{ids['booking']}"


@pytest.mark.parametrize("actor", ["admin", "master", "self"])
@pytest.mark.parametrize("has_customer", [True, False])
@pytest.mark.parametrize("has_promotion", [True, False])
async def test_async_service_change_preserves_customer_pricing_and_response(
    database, actor, has_customer, has_promotion,
):
    ids = await seed(database, customer=has_customer, promotion=has_promotion)
    async with client_for(database, ids, actor) as client:
        response = await client.patch(path(ids, actor), json={"service_ids": [ids["replacement"]]})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["service_id"] == ids["replacement"]
    assert data["service_ids"] == [ids["replacement"]]
    assert [item["id"] for item in data["services"]] == [ids["replacement"]]
    assert data["service_prices"] == {str(ids["replacement"]): 200}
    assert data["customer_id"] == ids["customer"]
    assert (data["customer"]["id"] if has_customer else data["customer"]) == ids["customer"]
    assert data["subtotal_amount"] == 200
    assert data["promotion_id"] == ids["promotion"]
    assert data["promotion_discount_percent"] == (20 if has_promotion else None)
    assert data["discount_amount"] == (40 if has_promotion else 0)
    assert data["total_amount"] == (160 if has_promotion else 200)
    async with database() as session:
        saved = (await session.scalars(select(Booking).where(Booking.id == ids["booking"])
                                      .options(*routes.booking_response_options()))).one()
        assert saved.service_ids == [ids["replacement"]]
        assert saved.service_prices == {ids["replacement"]: 200}
        assert saved.customer_id == ids["customer"]
        assert (saved.customer.id if saved.customer else None) == ids["customer"]
        assert saved.subtotal_amount == 200
        assert saved.total_amount == (160 if has_promotion else 200)
        assert saved.end_at == ids["start"] + timedelta(minutes=45)


@pytest.mark.parametrize("reason,status", [("foreign_master", 403), ("busy", 409), ("outside_availability", 409)])
async def test_async_service_change_keeps_access_and_availability_guards(database, reason, status):
    ids = await seed(database)
    async with database() as session:
        if reason == "foreign_master":
            other = Master(full_name="Other master")
            session.add(other)
            await session.flush()
            booking = await session.get(Booking, ids["booking"])
            booking.master_id = other.id
        elif reason == "busy":
            session.add(Booking(master_id=ids["master"], service_id=ids["original"], customer_name="Other",
                                customer_phone="+380500005678", start_at=ids["start"] + timedelta(minutes=30),
                                end_at=ids["start"] + timedelta(minutes=60), status=BookingStatus.confirmed))
        else:
            window = (await session.scalars(select(MasterAvailabilityWindow))).one()
            window.end_at = ids["start"] + timedelta(minutes=30)
        await session.commit()
    async with client_for(database, ids, "master") as client:
        response = await client.patch(path(ids, "master"), json={"service_ids": [ids["replacement"]]})
    assert response.status_code == status, response.text
    async with database() as session:
        saved = (await session.scalars(select(Booking).where(Booking.id == ids["booking"])
                                      .options(*routes.booking_response_options()))).one()
        assert saved.service_ids == [ids["original"]]
        assert saved.service_prices == {ids["original"]: 100}
        assert saved.total_amount == 80
