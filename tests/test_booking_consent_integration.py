"""Public booking consent contracts on disposable local PostgreSQL schemas."""
from datetime import datetime, timedelta, timezone
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from tests.test_repeat_booking_integration import scenario, client, payload  # noqa: F401
from tests.test_segments_integration import database, anyio_backend  # noqa: F401
from app.models.messaging import ClientCommunicationPreference, ConsentStatus, MessagePurpose
from app.models.booking import BarberService, Booking, BookingSource, Master, MasterAvailabilityWindow
from app.schemas.booking import PublicBookingCreate
from app.services.booking import BookingServiceLayer
from app.services.booking_consent import record_booking_sms_consent
from app.services.messaging import MessagingService

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("marketing", [None, ConsentStatus.unknown, ConsentStatus.opted_in])
@pytest.mark.parametrize("transactional", [ConsentStatus.unknown, ConsentStatus.opted_in])
@pytest.mark.parametrize("sms_consent", [False, True])
async def test_booking_confirmation_records_explicit_consent(
    database, scenario, client, marketing, transactional, sms_consent,
):
    if marketing is not None:
        async with database() as session:
            session.add(ClientCommunicationPreference(customer_id=scenario["customer_id"],
                marketing_consent=marketing, transactional_consent=transactional,
                preferred_language="en", telegram_chat_id="123", repeat_booking_opt_out=True))
            await session.commit()
    response = await client[0].post("/bookings", json=payload(scenario, sms_consent=sms_consent))
    assert response.status_code == 201, response.text
    async with database() as session:
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        expected_marketing = ConsentStatus.opted_in if sms_consent else marketing or ConsentStatus.unknown
        assert preference.marketing_consent == expected_marketing
        assert preference.transactional_consent == ConsentStatus.opted_in
        assert preference.booking_sms_consent_booking_id == response.json()["id"]
        assert preference.booking_sms_consent_at <= datetime.now(timezone.utc)
        assert MessagingService().communication_allowed(preference, MessagePurpose.transactional) == (True, None)
        assert MessagingService().communication_allowed(preference, MessagePurpose.review_request) == (True, None)
        assert MessagingService().communication_allowed(preference, MessagePurpose.marketing)[0] is (
            expected_marketing == ConsentStatus.opted_in
        )
        if marketing is not None:
            assert preference.preferred_language == "en"
            assert preference.telegram_chat_id == "123"
            assert preference.repeat_booking_opt_out is True


@pytest.mark.parametrize("restriction", ["marketing", "transactional", "do_not_contact", "blacklisted"])
@pytest.mark.parametrize("sms_consent", [False, True])
async def test_booking_does_not_clear_explicit_contact_restrictions(database, scenario, client, restriction, sms_consent):
    previous_audit = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with database() as session:
        preference = ClientCommunicationPreference(customer_id=scenario["customer_id"],
            marketing_consent=ConsentStatus.unknown, transactional_consent=ConsentStatus.unknown,
            booking_sms_consent_at=previous_audit, booking_sms_consent_booking_id=scenario["source_id"])
        if restriction in ("marketing", "transactional"):
            setattr(preference, restriction + "_consent", ConsentStatus.opted_out)
        elif restriction == "blacklisted":
            preference.blacklisted_at = datetime.now(timezone.utc)
        else:
            preference.do_not_contact = True
        session.add(preference)
        await session.commit()
    response = await client[0].post("/bookings", json=payload(scenario, sms_consent=sms_consent))
    assert response.status_code == 201, response.text
    async with database() as session:
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.booking_sms_consent_at == previous_audit
        assert preference.booking_sms_consent_booking_id == scenario["source_id"]
        assert preference.marketing_consent == (
            ConsentStatus.opted_out if restriction == "marketing" else ConsentStatus.unknown
        )
        assert preference.transactional_consent == (
            ConsentStatus.opted_out if restriction == "transactional" else ConsentStatus.unknown
        )
        if restriction in ("marketing", "transactional"):
            assert getattr(preference, restriction + "_consent") == ConsentStatus.opted_out
        elif restriction == "blacklisted":
            assert preference.blacklisted_at is not None
        else:
            assert preference.do_not_contact is True


@pytest.mark.parametrize("sms_consent", [False, True])
async def test_rejected_booking_does_not_record_consent(database, scenario, client, sms_consent):
    response = await client[0].post("/bookings", json=payload(scenario, expected_total_amount=1, sms_consent=sms_consent))
    assert response.status_code == 409, response.text
    async with database() as session:
        assert (await session.scalars(select(ClientCommunicationPreference))).all() == []


async def test_consent_migration_preserves_preferences(database, scenario):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("booking_sms_consent_migration",
        Path(__file__).resolve().parents[1] / "alembic/versions/0106_booking_sms_consent.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with database() as session:
        session.add(ClientCommunicationPreference(customer_id=scenario["customer_id"],
            marketing_consent=ConsentStatus.opted_out, transactional_consent=ConsentStatus.unknown))
        await session.commit()
        connection = await session.connection()
        def migrate(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                migration.downgrade()
                migration.upgrade()
        await connection.run_sync(migrate)
        await session.commit()
        session.expire_all()
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.marketing_consent == ConsentStatus.opted_out
        assert preference.transactional_consent == ConsentStatus.unknown
        assert preference.booking_sms_consent_at is None


async def test_claimed_waitlist_booking_records_service_consent_only(database, monkeypatch):
    from app.services.booking import KYIV_TZ
    from app.services.waitlist_offers import WaitlistOfferService
    from tests.test_waitlist_expiry_concurrency_integration import seed
    now = datetime.now(KYIV_TZ).replace(microsecond=0)
    monkeypatch.setattr(WaitlistOfferService, "_now", staticmethod(lambda: now))
    row, = await seed(database, now)
    async with database() as session:
        booking = await WaitlistOfferService().claim(session, row["token"])
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.customer_id == booking.customer_id
        assert preference.marketing_consent == ConsentStatus.unknown
        assert preference.transactional_consent == ConsentStatus.opted_in
        assert preference.booking_sms_consent_at is not None
        assert preference.booking_sms_consent_booking_id == booking.id


async def test_booking_preserves_existing_marketing_opt_in(database, scenario, client):
    async with database() as session:
        session.add(ClientCommunicationPreference(customer_id=scenario["customer_id"],
            marketing_consent=ConsentStatus.opted_in, transactional_consent=ConsentStatus.unknown))
        await session.commit()
    response = await client[0].post("/bookings", json=payload(scenario))
    assert response.status_code == 201, response.text
    async with database() as session:
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.marketing_consent == ConsentStatus.opted_in
        assert preference.transactional_consent == ConsentStatus.opted_in


@pytest.mark.parametrize("source", [BookingSource.backoffice, BookingSource.telegram, BookingSource.unknown])
@pytest.mark.parametrize("sms_consent", [False, True])
@pytest.mark.parametrize("existing", [False, True])
async def test_non_web_booking_never_records_consent(database, scenario, source, sms_consent, existing):
    previous_audit = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with database() as session:
        if existing:
            session.add(ClientCommunicationPreference(customer_id=scenario["customer_id"],
                marketing_consent=ConsentStatus.opted_in, transactional_consent=ConsentStatus.unknown,
                booking_sms_consent_at=previous_audit, booking_sms_consent_booking_id=scenario["source_id"]))
            await session.commit()
        booking = await BookingServiceLayer().create_public_booking(
            session, PublicBookingCreate(**payload(scenario, sms_consent=sms_consent)), source=source,
        )
        assert booking.source == source
        preferences = (await session.scalars(select(ClientCommunicationPreference))).all()
        if existing:
            assert len(preferences) == 1
            assert preferences[0].marketing_consent == ConsentStatus.opted_in
            assert preferences[0].transactional_consent == ConsentStatus.unknown
            assert preferences[0].booking_sms_consent_at == previous_audit
            assert preferences[0].booking_sms_consent_booking_id == scenario["source_id"]
        else:
            assert preferences == []


async def test_legacy_service_payload_without_sms_consent_preserves_marketing(database, scenario):
    legacy_payload = PublicBookingCreate(**payload(scenario)).model_dump(exclude={"sms_consent"})
    async with database() as session:
        booking = await BookingServiceLayer().create_public_booking(
            session, SimpleNamespace(**legacy_payload), source=BookingSource.web,
        )
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.marketing_consent == ConsentStatus.unknown
        assert preference.transactional_consent == ConsentStatus.opted_in
        assert preference.booking_sms_consent_booking_id == booking.id


async def test_web_consent_is_not_scoped_to_one_master(database, scenario, client):
    first = await client[0].post("/bookings", json=payload(scenario, sms_consent=True))
    assert first.status_code == 201, first.text
    async with database() as session:
        master = Master(full_name="Another consent integration master")
        session.add(master)
        await session.flush()
        service = BarberService(master_id=master.id, name="Another haircut", duration_minutes=30, price=1000)
        session.add(service)
        session.add(MasterAvailabilityWindow(master_id=master.id, start_at=scenario["at"],
            end_at=scenario["at"] + timedelta(hours=6)))
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        preference.marketing_consent = ConsentStatus.unknown
        preference.transactional_consent = ConsentStatus.unknown
        await session.commit()
        master_id, service_id = master.id, service.id
    second = await client[0].post("/bookings", json=payload(scenario, hours=2,
        master_id=master_id, service_ids=[service_id], expected_total_amount=1000, sms_consent=True))
    assert second.status_code == 201, second.text
    assert master_id != scenario["master_id"]
    async with database() as session:
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.customer_id == scenario["customer_id"]
        assert preference.marketing_consent == ConsentStatus.opted_in
        assert preference.transactional_consent == ConsentStatus.opted_in
        assert preference.booking_sms_consent_booking_id == second.json()["id"]


async def test_late_booking_rejection_rolls_back_explicit_consent(database, scenario, client):
    response = await client[0].post("/bookings", json=payload(scenario, sms_consent=True),
        headers={"X-Repeat-Booking-Token": "z" * 43})
    assert response.status_code == 401, response.text
    async with database() as session:
        assert (await session.scalars(select(ClientCommunicationPreference))).all() == []
        assert (await session.scalars(select(Booking.id))).all() == [scenario["source_id"]]


def contact_restriction(restriction):
    if restriction in ("marketing", "transactional"):
        return {restriction + "_consent": ConsentStatus.opted_out}
    if restriction == "blacklisted":
        return {"blacklisted_at": datetime.now(timezone.utc)}
    return {"do_not_contact": True}


async def wait_for_postgres_lock(database, pid):
    async def wait():
        async with database() as session:
            while not await session.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :pid AND NOT granted)"
            ), {"pid": pid}):
                await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout=5)


@pytest.mark.parametrize("restriction", ["marketing", "transactional", "do_not_contact", "blacklisted"])
@pytest.mark.parametrize("first_writer", ["restriction_update", "restriction_insert", "consent"])
@pytest.mark.parametrize("sms_consent", [False, True])
async def test_concurrent_contact_restriction_always_wins(
    database, scenario, restriction, first_writer, sms_consent,
):
    customer_id, booking_id = scenario["customer_id"], scenario["source_id"]
    restricted_values = contact_restriction(restriction)
    if first_writer != "restriction_insert":
        async with database() as session:
            session.add(ClientCommunicationPreference(customer_id=customer_id,
                marketing_consent=ConsentStatus.unknown, transactional_consent=ConsentStatus.unknown))
            await session.commit()

    async with database() as first:
        if first_writer == "consent":
            await record_booking_sms_consent(first, customer_id, booking_id, marketing_sms_consent=sms_consent)
        elif first_writer == "restriction_insert":
            values = {"marketing_consent": ConsentStatus.unknown,
                      "transactional_consent": ConsentStatus.unknown, **restricted_values}
            first.add(ClientCommunicationPreference(customer_id=customer_id, **values))
            await first.flush()
        else:
            await first.execute(update(ClientCommunicationPreference)
                .where(ClientCommunicationPreference.customer_id == customer_id).values(**restricted_values))

        pid_ready = asyncio.Future()

        async def second_writer():
            async with database() as second:
                pid_ready.set_result(await second.scalar(text("SELECT pg_backend_pid()")))
                if first_writer == "consent":
                    await second.execute(update(ClientCommunicationPreference)
                        .where(ClientCommunicationPreference.customer_id == customer_id).values(**restricted_values))
                else:
                    await record_booking_sms_consent(second, customer_id, booking_id,
                        marketing_sms_consent=sms_consent)
                await second.commit()

        task = asyncio.create_task(second_writer())
        try:
            await wait_for_postgres_lock(database, await asyncio.wait_for(pid_ready, timeout=5))
            assert not task.done()
            await first.commit()
            await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with database() as session:
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        for field, value in restricted_values.items():
            assert getattr(preference, field) == value
        if first_writer == "consent":
            assert preference.booking_sms_consent_booking_id == booking_id
            assert preference.booking_sms_consent_at is not None
        else:
            assert preference.booking_sms_consent_booking_id is None
            assert preference.booking_sms_consent_at is None
            if restriction != "marketing":
                assert preference.marketing_consent == ConsentStatus.unknown
            if restriction != "transactional":
                assert preference.transactional_consent == ConsentStatus.unknown
