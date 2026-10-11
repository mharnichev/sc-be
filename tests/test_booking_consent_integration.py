"""Public booking consent contracts on disposable local PostgreSQL schemas."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.test_repeat_booking_integration import scenario, client, payload  # noqa: F401
from tests.test_segments_integration import database, anyio_backend  # noqa: F401
from app.models.messaging import ClientCommunicationPreference, ConsentStatus, MessagePurpose
from app.services.messaging import MessagingService

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("existing", [False, True])
async def test_booking_confirmation_records_service_consent_only(database, scenario, client, existing):
    if existing:
        async with database() as session:
            session.add(ClientCommunicationPreference(customer_id=scenario["customer_id"],
                marketing_consent=ConsentStatus.unknown, transactional_consent=ConsentStatus.unknown,
                preferred_language="en", telegram_chat_id="123", repeat_booking_opt_out=True))
            await session.commit()
    response = await client[0].post("/bookings", json=payload(scenario))
    assert response.status_code == 201, response.text
    async with database() as session:
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.marketing_consent == ConsentStatus.unknown
        assert preference.transactional_consent == ConsentStatus.opted_in
        assert preference.booking_sms_consent_booking_id == response.json()["id"]
        assert preference.booking_sms_consent_at <= datetime.now(timezone.utc)
        assert MessagingService().communication_allowed(preference, MessagePurpose.transactional) == (True, None)
        for purpose in (MessagePurpose.marketing, MessagePurpose.review_request):
            assert MessagingService().communication_allowed(preference, purpose)[0] is False
        if existing:
            assert preference.preferred_language == "en"
            assert preference.telegram_chat_id == "123"
            assert preference.repeat_booking_opt_out is True


@pytest.mark.parametrize("restriction", ["marketing", "transactional", "do_not_contact", "blacklisted"])
async def test_booking_does_not_clear_explicit_contact_restrictions(database, scenario, client, restriction):
    async with database() as session:
        preference = ClientCommunicationPreference(customer_id=scenario["customer_id"],
            marketing_consent=ConsentStatus.unknown, transactional_consent=ConsentStatus.unknown)
        if restriction in ("marketing", "transactional"):
            setattr(preference, restriction + "_consent", ConsentStatus.opted_out)
        elif restriction == "blacklisted":
            preference.blacklisted_at = datetime.now(timezone.utc)
        else:
            preference.do_not_contact = True
        session.add(preference)
        await session.commit()
    response = await client[0].post("/bookings", json=payload(scenario))
    assert response.status_code == 201, response.text
    async with database() as session:
        preference = (await session.scalars(select(ClientCommunicationPreference))).one()
        assert preference.booking_sms_consent_at is None
        assert preference.booking_sms_consent_booking_id is None
        if restriction in ("marketing", "transactional"):
            assert getattr(preference, restriction + "_consent") == ConsentStatus.opted_out
        elif restriction == "blacklisted":
            assert preference.blacklisted_at is not None
        else:
            assert preference.do_not_contact is True


async def test_rejected_booking_does_not_record_consent(database, scenario, client):
    response = await client[0].post("/bookings", json=payload(scenario, expected_total_amount=1))
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
