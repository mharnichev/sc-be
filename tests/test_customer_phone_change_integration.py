"""Real HTTP/JWT and isolated PostgreSQL proof-of-possession regression tests."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.v1.routes import customer_activity, customers
from app.core.config import settings
from app.core.database import get_db_session
from app.core.security import decode_token, hash_otp_code
from app.dependencies.auth import get_current_admin_user
from app.models.customer import Customer
from app.models.customer_activity import CustomerActivityAccessToken
from app.models.customer_otp_code import CustomerOtpCode
from app.services.customer_activity import BROWSER_SESSION_SOURCE, customer_activity_service
from app.services.customer_auth import CustomerAuthService
from tests.test_segments_integration import anyio_backend, database

OLD = "+380501234567"
NEW = "+380501234568"
CODE = "123456"


def http_app(database):
    app = FastAPI()
    app.include_router(customers.public_router, prefix="/customers")
    app.include_router(customers.backoffice_router, prefix="/admin/customers")
    app.include_router(customer_activity.public_router)
    async def db_session():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = db_session
    app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=99, is_superuser=True)
    return app


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def seed_owner(database, *, active=True):
    async with database() as session:
        owner = Customer(phone=OLD, name="Owner", is_active=active, phone_verified_at=datetime.now(UTC))
        session.add(owner)
        await session.commit()
        auth = CustomerAuthService()
        return owner.id, auth.issue_access_token(owner), auth.issue_access_token(owner)


async def seed_otp(database, *, phone=NEW, expired=False):
    now = datetime.now(UTC)
    async with database() as session:
        otp = CustomerOtpCode(phone=phone, code_hash=hash_otp_code(phone, CODE), sent_at=now,
                              expires_at=now + timedelta(minutes=-1 if expired else 10))
        session.add(otp)
        await session.commit()
        return otp.id


@pytest.mark.anyio
async def test_phone_change_proof_retains_only_current_jwt_and_revokes_old_activity(database, monkeypatch):
    owner_id, current, other = await seed_owner(database, active=False)
    assert current != other
    assert decode_token(current)["jti"] != decode_token(other)["jti"]
    sent = []
    async def send(phone, code):
        sent.append((phone, code))
    monkeypatch.setattr(customers.service.sms_service, "send_otp_code", send)
    monkeypatch.setattr("app.services.customer_auth.generate_otp_code", lambda: CODE)
    async with database() as session:
        browser = await customer_activity_service.create_access_token(session, owner_id, source=BROWSER_SESSION_SOURCE,
                                                                      expires_at=datetime.now(UTC)+timedelta(days=1))
        sms = await customer_activity_service.create_access_token(session, owner_id, source="sms",
                                                                  expires_at=datetime.now(UTC)+timedelta(days=1))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
        for token in (current, other):
            assert (await client.get("/customers/me", headers=bearer(token))).status_code == 200
        for payload, expected in (({"phone": NEW}, 422), ({"phone": None}, 422), ({"phone": "+380 (50) 123-45-67"}, 200)):
            assert (await client.patch("/customers/me", headers=bearer(current), json=payload)).status_code == expected
        response = await client.post("/customers/me/phone-change/request-otp", headers=bearer(current), json={"phone": "050 123 45 68"})
        assert response.status_code == 202
        assert sent == [(NEW, CODE)]
        async with database() as session:
            assert (await session.get(Customer, owner_id)).phone == OLD
            assert (await session.get(Customer, owner_id)).session_version == 0
        response = await client.post("/customers/me/phone-change/confirm", headers=bearer(current), json={"phone": NEW, "otp_code": CODE})
        assert response.status_code == 200
        assert response.json()["id"] == owner_id and response.json()["phone"] == NEW
        assert response.json()["phone_verified_at"] is not None
        assert (await client.get("/customers/me", headers=bearer(current))).status_code == 200
        assert (await client.get("/customers/me", headers=bearer(other))).status_code == 401
        for token in (browser, sms):
            assert (await client.get("/customer-activity", headers={"X-Customer-Activity-Token": token})).status_code == 401
        async with database() as session:
            owner = await session.get(Customer, owner_id)
            assert owner.session_version == 1
            assert owner.is_active is False
            fresh = await customer_activity_service.create_access_token(session, owner_id, source="sms",
                                                                        expires_at=datetime.now(UTC)+timedelta(days=1))
            await session.commit()
            versions = list((await session.scalars(select(CustomerActivityAccessToken.session_version))).all())
            assert sorted(versions) == [0, 0, 1]
            assert await session.scalar(select(func.count()).select_from(Customer)) == 1
        assert (await client.get("/customer-activity", headers={"X-Customer-Activity-Token": fresh})).status_code == 200
        # A later administrative change invalidates even the explicitly retained JWT.
        response = await client.put(f"/admin/customers/{owner_id}", json={"phone": "+380501234569"})
        assert response.status_code == 200 and response.json()["phone_verified_at"] is None
        assert (await client.get("/customers/me", headers=bearer(current))).status_code == 401
        assert (await client.get("/customer-activity", headers={"X-Customer-Activity-Token": fresh})).status_code == 401
        async with database() as session:
            assert (await session.get(Customer, owner_id)).session_version == 2


@pytest.mark.anyio
async def test_occupied_alias_never_merges_accounts_or_consumes_otp(database, monkeypatch):
    owner_id, token, _ = await seed_owner(database)
    otp_id = await seed_otp(database)
    async with database() as session:
        alias = Customer(phone="+0501234568", name="Other person")
        session.add(alias)
        await session.commit()
        alias_id = alias.id
    async def never_send(*args):
        raise AssertionError("An occupied alias must not send a challenge")
    monkeypatch.setattr(customers.service.sms_service, "send_otp_code", never_send)
    async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
        assert (await client.post("/customers/me/phone-change/request-otp", headers=bearer(token), json={"phone": NEW})).status_code == 409
        assert (await client.post("/customers/me/phone-change/confirm", headers=bearer(token), json={"phone": NEW, "otp_code": CODE})).status_code == 409
    async with database() as session:
        assert (await session.get(Customer, owner_id)).phone == OLD
        assert (await session.get(Customer, alias_id)).phone == "+0501234568"
        otp = await session.get(CustomerOtpCode, otp_id)
        assert otp.attempts_count == 0 and otp.verified_at is None
        assert await session.scalar(select(func.count()).select_from(Customer)) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [False, True])
async def test_wrong_or_expired_otp_does_not_change_phone(database, expired):
    owner_id, token, _ = await seed_owner(database)
    otp_id = await seed_otp(database, expired=expired)
    async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
        response = await client.post("/customers/me/phone-change/confirm", headers=bearer(token), json={"phone": NEW, "otp_code": CODE if expired else "000000"})
        assert response.status_code == 400
    async with database() as session:
        owner = await session.get(Customer, owner_id)
        assert owner.phone == OLD and owner.session_version == 0
        otp = await session.get(CustomerOtpCode, otp_id)
        assert otp.verified_at is None
        assert otp.attempts_count == (0 if expired else 1)


@pytest.mark.anyio
async def test_phone_change_daily_attempt_budget_is_durable(database, monkeypatch):
    owner_id, token, _ = await seed_owner(database)
    otp_id = await seed_otp(database)
    monkeypatch.setattr(settings, "otp_max_verify_attempts_per_day", 2)
    async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
        for code, expected in (("000000", 400), ("000000", 400), (CODE, 429)):
            response = await client.post("/customers/me/phone-change/confirm", headers=bearer(token), json={"phone": NEW, "otp_code": code})
            assert response.status_code == expected
    async with database() as session:
        otp = await session.get(CustomerOtpCode, otp_id)
        assert otp.attempts_count == 2 and otp.verified_at is None
        assert (await session.get(Customer, owner_id)).phone == OLD


@pytest.mark.anyio
async def test_parallel_confirmation_consumes_one_challenge_once(database):
    owner_id, token, _ = await seed_owner(database)
    otp_id = await seed_otp(database)
    async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
        async def confirm():
            return await client.post("/customers/me/phone-change/confirm", headers=bearer(token), json={"phone": NEW, "otp_code": CODE})
        responses = await asyncio.wait_for(asyncio.gather(confirm(), confirm()), timeout=20)
        assert sorted(response.status_code for response in responses) == [200, 409]
        replay = await confirm()
        assert replay.status_code == 409
    async with database() as session:
        owner = await session.get(Customer, owner_id)
        otp = await session.get(CustomerOtpCode, otp_id)
        assert owner.phone == NEW and owner.session_version == 1
        assert otp.attempts_count == 1 and otp.verified_at is not None
        assert await session.scalar(select(func.count()).select_from(Customer)) == 1


@pytest.mark.anyio
async def test_old_phone_login_cannot_upgrade_its_jwt_during_post_commit_refresh(database, monkeypatch):
    owner_id, retained, _ = await seed_owner(database)
    await seed_otp(database, phone=OLD)
    await seed_otp(database, phone=NEW)
    committed = asyncio.Event()
    resume = asyncio.Event()
    async with database() as session:
        real_refresh = session.refresh
        async def paused_refresh(instance, *args, **kwargs):
            committed.set()
            await asyncio.wait_for(resume.wait(), 10)
            return await real_refresh(instance, *args, **kwargs)
        monkeypatch.setattr(session, "refresh", paused_refresh)
        task = asyncio.create_task(CustomerAuthService().verify_otp(session, OLD, CODE))
        try:
            await asyncio.wait_for(committed.wait(), 10)
            async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
                changed = await client.post("/customers/me/phone-change/confirm", headers=bearer(retained),
                                            json={"phone": NEW, "otp_code": CODE})
                assert changed.status_code == 200
                resume.set()
                login = await asyncio.wait_for(task, 10)
                assert decode_token(login.access_token)["customer_session_version"] == 0
                assert (await client.get("/customers/me", headers=bearer(login.access_token))).status_code == 401
                assert (await client.get("/customers/me", headers=bearer(retained))).status_code == 200
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_deferred_booking_browser_cookie_cannot_adopt_changed_phone_generation(database, monkeypatch):
    from fastapi import Response
    from app.api.v1.routes import bookings
    from app.models.booking import Booking, Master, BarberService
    owner_id, retained, _ = await seed_owner(database)
    await seed_otp(database)
    async with database() as session:
        master = Master(full_name="Synthetic capability master")
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Synthetic capability service", duration_minutes=30, price=100)
        session.add(service)
        await session.flush()
        start = datetime.now(UTC)+timedelta(days=1)
        booking = Booking(master_id=master.id, service_id=service.id, customer_id=owner_id, customer_phone=OLD,
                          customer_name="Owner", start_at=start, end_at=start+timedelta(minutes=30))
        session.add(booking)
        await session.commit()
        booking_id = booking.id
    monkeypatch.setattr(bookings, "AsyncSessionLocal", database)
    # The booking has committed; its independent cookie issuance is deferred.
    async with AsyncClient(transport=ASGITransport(app=http_app(database)), base_url="http://test") as client:
        assert (await client.post("/customers/me/phone-change/confirm", headers=bearer(retained),
                                  json={"phone": NEW, "otp_code": CODE})).status_code == 200
    response = Response()
    assert await bookings.set_booking_browser_session(response, customer_id=owner_id, booking_id=booking_id) is False
    assert "set-cookie" not in response.headers
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerActivityAccessToken)) == 0
        assert (await session.get(Customer, owner_id)).phone == NEW
