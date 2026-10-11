"""Archive gates on disposable local PostgreSQL schemas; transport is synthetic."""
import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import aliased, selectinload

from app.core.database import OperationalSession
from app.models.booking import Booking, BookingServiceItem, BookingStatus
from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignStatus, CampaignType, MessageChannel, MessageDeliveryStatus,
    MessagePurpose, MessageRecipient,
)
from app.models.sms_queue import SmsQueueJob
from app.services.booking_archive import archive_booking
from app.services.customer_auth import CustomerAuthService
from app.services.sms_queue import SmsDispatchDecision, SmsQueueService
from tests.test_segments_integration import database, anyio_backend, add_booking, add_customer  # noqa: F401
from tests.test_sms_queue_integration import SimulatedClock

pytestmark = pytest.mark.anyio


@pytest.fixture
def operational_database(database):
    return async_sessionmaker(bind=database.kw["bind"], sync_session_class=OperationalSession,
                              expire_on_commit=False)


async def seed(database):
    now = datetime.now(UTC)
    async with database() as session:
        customer = await add_customer(session)
        booking = await add_booking(session, customer, now, status=BookingStatus.completed)
        customer.first_visit_completed_at = now
        session.add(BookingServiceItem(booking_id=booking.id, service_id=booking.service_id,
                                      position=0, price_amount=100))
        campaign = Campaign(name="Archive synthetic campaign", type=CampaignType.booking_confirmation,
            purpose=MessagePurpose.transactional, status=CampaignStatus.active, channel=MessageChannel.sms)
        session.add(campaign)
        await session.flush()
        recipient = MessageRecipient(campaign_id=campaign.id, customer_id=customer.id,
            appointment_id=booking.id, channel=MessageChannel.sms, status=MessageDeliveryStatus.pending,
            idempotency_key="customer-activity:booking_confirmation:archive",
            rendered_message="Synthetic {manage_url}")
        session.add(recipient)
        await session.commit()
        return dict(customer_id=customer.id, booking_id=booking.id, recipient_id=recipient.id,
                    campaign_id=campaign.id, phone=customer.phone)


async def wait_for_lock(database, pid):
    async def wait():
        async with database() as session:
            while not await session.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :pid AND NOT granted)"
            ), {"pid": pid}):
                await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout=5)


async def test_0108_upgrade_and_downgrade_preserve_postgres_booking_rows(operational_database):
    context = await seed(operational_database)
    path = Path(__file__).parents[1] / "alembic/versions/0108_booking_archive.py"
    spec = importlib.util.spec_from_file_location("archive_pg_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with operational_database() as session:
        connection = await session.connection()

        def migrate(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert "archived_at" not in {column["name"] for column in inspect(connection).get_columns("bookings")}
                assert connection.execute(text("SELECT id FROM bookings")).scalar_one() == context["booking_id"]
                migration.upgrade()
                assert connection.execute(text("SELECT id, archived_at FROM bookings")).one() == (context["booking_id"], None)
                column = next(column for column in inspect(connection).get_columns("bookings") if column["name"] == "archived_at")
                assert column["type"].timezone is True
                assert column["nullable"] is True
                assert any(index["name"] == "ix_bookings_archived_at" for index in inspect(connection).get_indexes("bookings"))

        await connection.run_sync(migrate)
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(BookingServiceItem)) == 1


async def test_operational_async_filter_hides_archived_rows_counts_aliases_and_relationships(operational_database):
    context = await seed(operational_database)
    async with operational_database() as session:
        booking = await session.get(Booking, context["booking_id"], with_for_update=True)
        await archive_booking(session, booking)
        await session.commit()
    async with operational_database() as session:
        assert await session.get(Booking, context["booking_id"]) is None
        assert await session.scalar(select(func.count()).select_from(Booking)) == 0
        alias = aliased(Booking)
        assert await session.scalar(select(func.count(alias.id))) == 0
        visits = select(Booking.customer_id, func.count(Booking.id).label("total")).group_by(Booking.customer_id).subquery()
        assert (await session.scalars(select(visits.c.total))).all() == []
        customer = (await session.scalars(select(Customer).options(selectinload(Customer.bookings)))).one()
        assert customer.bookings == []
        assert await session.scalar(select(func.count()).select_from(Booking)
            .execution_options(include_archived_bookings=True)) == 1
        assert await session.scalar(select(func.count()).select_from(BookingServiceItem)) == 1


async def test_delete_customer_with_only_archived_visit_preserves_audit(operational_database):
    context = await seed(operational_database)
    async with operational_database() as session:
        booking = await session.get(Booking, context["booking_id"], with_for_update=True)
        await archive_booking(session, booking)
        await session.commit()
    async with operational_database() as session:
        customer = await session.get(Customer, context["customer_id"])
        await CustomerAuthService().delete_customer(session, customer)
    async with operational_database() as session:
        customer = await session.get(Customer, context["customer_id"])
        assert customer is not None and customer.is_active is False
        booking = (await session.scalars(select(Booking).execution_options(include_archived_bookings=True))).one()
        assert booking.customer_id == customer.id and booking.archived_at is not None
        assert await session.scalar(select(func.count()).select_from(BookingServiceItem)) == 1
        assert await session.get(MessageRecipient, context["recipient_id"]) is not None


@pytest.mark.parametrize("mode", ["booking-confirmation", "booking-reminder", "campaign"])
async def test_archive_commit_while_admission_waits_customer_prevents_transport(operational_database, mode):
    context = await seed(operational_database)
    clock = SimulatedClock()
    requests = []
    admission_pid = asyncio.Future()

    async def allow(_job):
        return SmsDispatchDecision()

    async def outcome(_job):
        pass

    async def transport(_job):
        requests.append(_job.id)
        return {"success_request": {"info": {"synthetic": context["phone"]}}}

    queue = SmsQueueService(session_factory=operational_database, clock=clock,
                            before_dispatch=allow, after_outcome=outcome, transport=transport)
    key = f"{mode}:{context['booking_id']}:archive"
    job_context = {"idempotency_key": key, "customer_id": context["customer_id"]}
    if mode == "campaign":
        job_context.update(campaign_id=context["campaign_id"], recipient_id=context["recipient_id"])
    job = await queue.enqueue("send", {"phone": [context["phone"].lstrip("+")], "message": "Synthetic"},
        idempotency_key=key, context=job_context)
    real_lock = queue._lock_admission_context

    async def capture_pid(session, claimed):
        admission_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
        return await real_lock(session, claimed)

    queue._lock_admission_context = capture_pid
    async with operational_database() as archiver:
        booking = await archiver.get(Booking, context["booking_id"], with_for_update=True)
        await archiver.get(Customer, context["customer_id"], with_for_update=True)
        task = asyncio.create_task(queue.process_one())
        try:
            await wait_for_lock(operational_database, await asyncio.wait_for(admission_pid, timeout=5))
            assert not task.done()
            await archive_booking(archiver, booking)
            await archiver.commit()
            assert await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert requests == []
    async with operational_database() as session:
        persisted = await session.get(SmsQueueJob, job.id)
        assert persisted.status == "skipped" and persisted.error_code == "booking_archived"
        assert persisted.transport_started_at is None


async def test_activity_outbox_and_archive_finish_without_foreign_key_deadlock(operational_database, monkeypatch):
    from app.core.config import settings
    from app.services import customer_activity_notifications
    from app.services.customer_activity import customer_activity_service
    from app.services.customer_activity_notifications import CustomerActivityNotificationService
    from app.services.sms import SmsService

    context = await seed(operational_database)
    clock = SimulatedClock()
    requests = []
    token_started, continue_token = asyncio.Event(), asyncio.Event()
    archive_pid = asyncio.Future()

    async def transport(job):
        requests.append(job.id)
        raise AssertionError("An archived outbox must never reach transport")

    async def allow(_job):
        return SmsDispatchDecision()

    async def outcome(_job):
        pass

    queue = SmsQueueService(session_factory=operational_database, clock=clock,
        transport=transport, before_dispatch=allow, after_outcome=outcome)
    sms = SmsService()
    monkeypatch.setattr(sms, "_get_queue", lambda: queue)
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "synthetic-no-network")
    monkeypatch.setattr(customer_activity_notifications, "AsyncSessionLocal", operational_database)
    real_token = customer_activity_service.create_access_token

    async def paused_token(*args, **kwargs):
        token_started.set()
        await asyncio.wait_for(continue_token.wait(), timeout=5)
        return await real_token(*args, **kwargs)

    monkeypatch.setattr(customer_activity_service, "create_access_token", paused_token)

    async def archive():
        async with operational_database() as session:
            archive_pid.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            booking = await session.get(Booking, context["booking_id"], with_for_update=True)
            await archive_booking(session, booking)
            await session.commit()

    delivery = asyncio.create_task(CustomerActivityNotificationService(sms)._dispatch(context["recipient_id"]))
    tasks = [delivery]
    try:
        await asyncio.wait_for(token_started.wait(), timeout=5)
        archiver = asyncio.create_task(archive())
        tasks.append(archiver)
        await wait_for_lock(operational_database, await asyncio.wait_for(archive_pid, timeout=5))
        continue_token.set()
        assert await asyncio.wait_for(delivery, timeout=5) is False
        await asyncio.wait_for(archiver, timeout=5)
        assert await queue.process_one()
        assert requests == []
        async with operational_database() as session:
            recipient = await session.get(MessageRecipient, context["recipient_id"])
            assert recipient.status == MessageDeliveryStatus.skipped
            job = await session.get(SmsQueueJob, recipient.sms_queue_job_id)
            assert job.status == "skipped" and job.error_code == "booking_archived"
    finally:
        continue_token.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
