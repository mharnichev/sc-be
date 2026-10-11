"""Read-only booking detail contracts, isolated from databases and providers."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.routes import bookings as routes
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_master
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.customer import Customer


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def completed_booking():
    at = datetime(2026, 10, 11, 12, tzinfo=UTC)
    haircut = BarberService(
        id=23, master_id=7, name="Haircut", duration_minutes=60,
        price=1000, is_active=True, created_at=at, updated_at=at,
    )
    booking = Booking(
        id=41, master_id=7, service_id=23, customer_id=19,
        customer_name="Test Customer", customer_phone="+380500000001",
        customer_email="customer@example.com", customer_comment="Saved note",
        start_at=at, end_at=at + timedelta(hours=1),
        status=BookingStatus.completed, completed_at=at + timedelta(hours=8),
        created_at=at, updated_at=at + timedelta(hours=8),
        subtotal_amount=900, promotion_discount_amount=90, manual_discount_amount=10,
        total_amount=800, promotion_id=3, promotion_code_snapshot="SAVE10",
        promotion_name_uk_snapshot="Знижка", promotion_name_en_snapshot="Discount",
        promotion_discount_percent_snapshot=10,
        promotion_application_mode_snapshot="code", promotion_eligibility_type_snapshot="all",
        service=haircut,
        service_items=[BookingServiceItem(service_id=23, position=0, price_amount=900, service=haircut)],
        customer=Customer(id=19, name="Test", surname="Customer", phone="+380500000001",
                          email="customer@example.com"),
    )
    return booking


async def read_detail(booking, *, path="/masters/me/bookings/41", master=None, superuser=False):
    results = [Mock(scalar_one_or_none=Mock(return_value=booking))]
    if not superuser and path == "/bookings/41":
        results.append(Mock(scalar_one_or_none=Mock(return_value=master)))
    session = SimpleNamespace(execute=AsyncMock(side_effect=results), commit=AsyncMock(),
                              flush=AsyncMock(), add=Mock(), delete=AsyncMock())
    app = FastAPI()
    app.include_router(routes.backoffice_router)

    async def database():
        yield session

    app.dependency_overrides[get_db_session] = database
    app.dependency_overrides[get_current_master] = lambda: master
    app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=101, is_superuser=superuser)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.add.assert_not_called()
    session.delete.assert_not_awaited()
    return response, session


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/masters/me/bookings/41", "/bookings/41"])
async def test_completed_booking_can_be_read_with_service_customer_and_price_snapshots(completed_booking, path):
    response, _ = await read_detail(completed_booking, path=path, master=Master(id=7))
    assert response.status_code == 200
    value = response.json()
    assert value["id"] == 41
    assert value["status"] == "completed"
    assert value["completed_at"] == "2026-10-11T20:00:00Z"
    assert value["master_id"] == 7
    assert value["service_ids"] == [23]
    assert value["services"][0]["name"] == "Haircut"
    assert value["services"][0]["price"] == 1000
    assert value["service_prices"] == {"23": 900}
    assert value["customer"] == {"id": 19, "phone": "+380500000001", "email": "customer@example.com",
                                 "name": "Test", "surname": "Customer"}
    assert value["customer_comment"] == "Saved note"
    assert (value["subtotal_amount"], value["discount_amount"], value["total_amount"]) == (900, 100, 800)
    assert value["promotion_code"] == "SAVE10"
    assert value["promotion_discount_percent"] == 10


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/masters/me/bookings/41", "/bookings/41"])
async def test_missing_booking_returns_404(completed_booking, path):
    response, session = await read_detail(None, path=path, master=Master(id=7))
    assert response.status_code == 404
    assert response.json() == {"detail": "Booking not found"}
    assert session.execute.await_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/masters/me/bookings/41", "/bookings/41"])
@pytest.mark.parametrize("master_id,redirect_target,booking_master,source,expected", [
    (7, None, 7, None, 200),
    (8, None, 7, None, 403),
    (8, 7, 7, 8, 200),       # Redirect source owns the booking on the target calendar.
    (8, 7, 8, None, 200),    # Source retains access to its pre-redirect bookings.
    (8, 7, 8, 9, 403),       # Another source's booking is not owned by this redirect source.
    (8, 7, 7, None, 403),    # Redirecting to a calendar does not grant its other bookings.
    (7, None, 7, 8, 200),    # Physical target owns the redirected calendar booking.
    (8, None, 7, 8, 403),    # A source without a configured redirect has no source access.
])
async def test_master_and_linked_user_respect_redirect_ownership(
    completed_booking, path, master_id, redirect_target, booking_master, source, expected,
):
    completed_booking.master_id = booking_master
    completed_booking.redirected_from_master_id = source
    completed_booking.redirected_from_master = Master(id=source, full_name="Source Master") if source else None
    master = Master(id=master_id, booking_redirect_master_id=redirect_target)
    response, _ = await read_detail(completed_booking, path=path, master=master)
    assert response.status_code == expected
    if expected == 200:
        value = response.json()
        assert value["master_id"] == booking_master
        assert value["redirectedFromMasterId"] == source
        assert value["redirectedFromMaster"] == ({"id": source, "full_name": "Source Master"} if source else None)
    else:
        assert response.json() == {"detail": "Cannot view another master's booking"}


@pytest.mark.anyio
async def test_admin_without_linked_master_is_forbidden(completed_booking):
    response, _ = await read_detail(completed_booking, path="/bookings/41", master=None)
    assert response.status_code == 403
    assert response.json() == {"detail": "Current user is not linked to a master"}


@pytest.mark.anyio
async def test_superuser_can_read_any_completed_booking_without_linked_master(completed_booking):
    completed_booking.redirected_from_master_id = 8
    completed_booking.redirected_from_master = Master(id=8, full_name="Source Master")
    response, session = await read_detail(completed_booking, path="/bookings/41", superuser=True)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["master_id"] == 7
    assert response.json()["redirectedFromMasterId"] == 8
    assert session.execute.await_count == 1
