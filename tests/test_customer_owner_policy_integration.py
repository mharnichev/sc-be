"""Native history upserts, completed-visit statistics, aliases and inactive flags."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.v1.routes import bookings, waitlist, waitlist_offers
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master, MasterAvailabilityWindow
from app.models.customer import Customer
from app.models.promotion import Promotion, PromotionEligibilityType
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.services.promotion import PromotionService
from app.utils.import_customers import CustomerImportRow, CustomerImportStats, upsert_customers
from tests.test_customer_phone_change_integration import CODE, OLD, bearer, http_app, seed_otp, seed_owner
from tests.test_segments_integration import add_booking, add_customer, anyio_backend, database

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.mark.anyio
@pytest.mark.parametrize("overwrite_names", [False, True])
async def test_imported_history_keeps_max_latest_without_accumulating_snapshots(database, overwrite_names):
    latest = datetime(2026, 1, 5, 12, tzinfo=KYIV)
    async with database() as session:
        customer = Customer(phone=OLD, name="Existing", surname="Name", imported_total_spent=500,
                            imported_last_visit_at=latest, imported_is_new_client=False, is_active=False)
        session.add(customer)
        await session.commit()
        customer_id = customer.id
        rows = [
            CustomerImportRow(2, OLD, None, Decimal("0.00"), None, True),
            CustomerImportRow(3, OLD, "Imported Name", Decimal("200.00"), latest-timedelta(days=10), False),
            CustomerImportRow(4, OLD, "Imported Name", Decimal("700.00"), latest+timedelta(days=1), False),
        ]
        for index, row in enumerate(rows):
            stats = CustomerImportStats()
            await session.run_sync(lambda sync: upsert_customers(sync, [row], stats, overwrite_names=overwrite_names))
            await session.commit()
            await session.refresh(customer)
            if index < 2:
                assert customer.imported_total_spent == Decimal("500.00")
                assert customer.imported_last_visit_at == latest
                assert customer.imported_is_new_client is False
                if not overwrite_names:
                    assert customer.name == "Existing"
        await session.refresh(customer)
        assert customer.id == customer_id and customer.is_active is False
        assert customer.imported_total_spent == Decimal("700.00")
        assert customer.imported_last_visit_at == latest+timedelta(days=1)
        assert customer.imported_is_new_client is False
        assert customer.name == ("Imported" if overwrite_names else "Existing")
        assert customer.surname == "Name"
        # Reimporting the same historical snapshot cannot add its spend a second time.
        await session.run_sync(lambda sync: upsert_customers(sync, [rows[-1]], CustomerImportStats(), overwrite_names=overwrite_names))
        await session.commit()
        await session.refresh(customer)
        assert customer.imported_total_spent == Decimal("700.00")
        assert await session.scalar(select(func.count()).select_from(Customer)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("has_completed", [False, True])
async def test_customer_stats_completed_end_only_keep_all_status_booking_total(database, has_completed):
    at = datetime(2026, 1, 7, 12, tzinfo=KYIV)
    async with database() as session:
        owner = await add_customer(session, 1, imported_last_visit_at=at+timedelta(days=900))
        master = Master(full_name="Completed master")
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Completed haircut", duration_minutes=30, price=100)
        session.add(service)
        await session.flush()
        kinds = [BookingStatus.cancelled, BookingStatus.confirmed, BookingStatus.no_show]
        if has_completed:
            kinds += [BookingStatus.completed, BookingStatus.completed]
        expected_last = None
        for index, kind in enumerate(kinds):
            ended = at + timedelta(days=index if kind == BookingStatus.completed else 100+index)
            item = await add_booking(session, owner, ended, status=kind, master=master, service=service)
            session.add(BookingServiceItem(booking_id=item.id, service_id=service.id, position=0, price_amount=100))
            if kind == BookingStatus.completed:
                expected_last = ended
        await session.commit()
        owner_id, master_id, service_id = owner.id, master.id, service.id
    async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
        response = await client.get(f"/admin/customers/{owner_id}/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["total_bookings"] == (5 if has_completed else 3)
    if has_completed:
        assert datetime.fromisoformat(data["last_visit_date"]) == expected_last
        assert data["most_visited_barber"] == {"id": master_id, "name": "Completed master", "count": 2}
        assert data["most_used_services"] == [{"id": service_id, "name": "Completed haircut", "count": 2}]
    else:
        assert data["last_visit_date"] is None
        assert data["most_visited_barber"] is None and data["most_used_services"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("history", ["completed", "imported"])
async def test_alias_recent_history_prevents_inactivity_discount_without_merging(database, history):
    at = datetime(2026, 10, 9, 12, tzinfo=KYIV)
    last = at-timedelta(days=10)
    async with database() as session:
        primary = Customer(phone=OLD, name="Primary")
        alias = Customer(phone="+0501234567", name="Alias", imported_last_visit_at=last if history == "imported" else None)
        session.add_all([primary, alias])
        await session.flush()
        if history == "completed":
            await add_booking(session, alias, last)
        promotion = Promotion(code="RETURN", name_uk="Повернення", name_en="Return", discount_percent=10,
                              eligibility_type=PromotionEligibilityType.inactive_customers, inactive_days=90)
        session.add(promotion)
        await session.commit()
        actual = await PromotionService().customer_last_visit_at(session, primary)
        assert actual == last
        with pytest.raises(HTTPException) as error:
            await PromotionService().ensure_customer_eligible(session, promotion=promotion, customer=primary, at=at)
        assert error.value.status_code == 400
        assert await session.scalar(select(func.count()).select_from(Customer)) == 2
        assert primary.id != alias.id and primary.phone == OLD and alias.phone == "+0501234567"


@pytest.mark.anyio
async def test_inactive_customer_can_login_book_create_waitlist_and_claim_http(database, monkeypatch):
    owner_id, token, _ = await seed_owner(database, active=False)
    await seed_otp(database, phone=OLD)
    now = datetime.now(KYIV)
    day = (now+timedelta(days=1)).date()
    while day.weekday() == 0:
        day += timedelta(days=1)
    start = datetime.combine(day, datetime.min.time(), tzinfo=KYIV)+timedelta(hours=10)
    async with database() as session:
        master = Master(full_name="Public master", is_active=True, show_on_master_block=True)
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Haircut", duration_minutes=30, price=100, is_active=True)
        session.add(service)
        session.add(MasterAvailabilityWindow(master_id=master.id, start_at=start, end_at=start+timedelta(hours=5)))
        await session.commit()
        master_id, service_id = master.id, service.id
    async def no_provider(*args, **kwargs):
        return None
    monkeypatch.setattr(bookings, "AsyncSessionLocal", database)
    monkeypatch.setattr(bookings.email_notification_service, "send_new_booking_to_master", no_provider)
    monkeypatch.setattr(bookings.master_telegram_notification_service, "send_new_booking_to_master", no_provider)
    monkeypatch.setattr(bookings.customer_activity_notification_service, "send_booking_confirmation", no_provider)
    monkeypatch.setattr(waitlist.customer_activity_notification_service, "send_waitlist_created", no_provider)
    app = http_app(database)
    app.include_router(bookings.public_router)
    app.include_router(waitlist.public_router)
    app.include_router(waitlist_offers.public_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/customers/auth/verify-otp", json={"phone": OLD, "otp_code": CODE})
        assert login.status_code == 200
        assert login.json()["customer"]["id"] == owner_id
        assert login.json()["customer"]["is_active"] is False
        assert (await client.get("/customers/me", headers=bearer(login.json()["access_token"]))).status_code == 200
        booked = await client.post("/bookings", json={"master_id": master_id, "service_id": service_id,
                    "customer_name": "Owner", "customer_phone": OLD, "start_at": start.isoformat()})
        assert booked.status_code == 201
        queued = await client.post("/waitlist", json={"customer_name": "Owner", "customer_phone": OLD,
                    "service_ids": [service_id], "preferred_master_id": master_id,
                    "desired_date": day.isoformat(), "notification_consent": True})
        assert queued.status_code == 201
        claim_token = "native-owner-policy-claim-token-123456789"
        async with database() as session:
            request = (await session.scalars(select(WaitlistRequest))).one()
            assert request.customer_id == owner_id
            request.status = WaitlistStatus.offered
            session.add(WaitlistOffer(request_id=request.id, master_id=master_id, source_master_id=master_id,
                        start_at=start+timedelta(hours=2), end_at=start+timedelta(hours=2, minutes=30),
                        token_hash=waitlist_offers.service._hash(claim_token), status=WaitlistOfferStatus.delivered,
                        expires_at=datetime.now(UTC)+timedelta(minutes=15), scheduled_at=datetime.now(UTC)))
            await session.commit()
        claimed = await client.post("/waitlist/offers/claim", json={"token": claim_token})
        assert claimed.status_code == 200
    async with database() as session:
        owner = await session.get(Customer, owner_id)
        assert owner.is_active is False
        assert await session.scalar(select(func.count()).select_from(Customer)) == 1
        assert await session.scalar(select(func.count()).select_from(Booking).where(Booking.customer_id == owner_id)) == 2
        assert (await session.scalars(select(WaitlistRequest.status))).one() == WaitlistStatus.booked
        assert (await session.scalars(select(WaitlistOffer.status))).one() == WaitlistOfferStatus.claimed
