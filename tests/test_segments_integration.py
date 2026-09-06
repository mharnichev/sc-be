"""Real PostgreSQL checks; opt in with an explicitly isolated SEGMENTS_TEST_DATABASE_URL.

Never falls back to the application DATABASE_URL. Each test owns a temporary schema.
Providers below only record calls in memory; no application lifespan is started.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models.booking import BarberService, Booking, BookingStatus, Master
from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignStatus, CampaignType, ClientCommunicationPreference,
    ConsentStatus, MessageChannel, MessageDeliveryStatus, MessagePurpose,
    MessageRecipient, MessageTemplate,
)
from app.models.segment import CustomerSegment
from app.models.campaign_run import CampaignRun
from app.schemas.segment import SegmentRules
from app.services.messaging import MessageProvider, MessagingService, ProviderSendResult, _recipient_delivery_load_options

KYIV = ZoneInfo("Europe/Kyiv")
AT = datetime(2026, 9, 6, 12, tzinfo=KYIV)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def database():
    url = os.environ.get("SEGMENTS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set SEGMENTS_TEST_DATABASE_URL to an isolated local PostgreSQL database")
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1", "::1"} or "test" not in (parsed.database or ""):
        pytest.fail("Integration database must be local and have 'test' in its database name")
    schema = "segments_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


class SandboxProvider(MessageProvider):
    def __init__(self, channel=MessageChannel.sms, *, error=None):
        self.channel = channel
        self.error = error
        self.sent = []

    async def send_message(self, *, destination, body, reply_markup=None):
        self.sent.append((destination, body))
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return ProviderSendResult(provider_message_id=f"sandbox-{len(self.sent)}", raw_response={"sandbox": True})


async def add_customer(session, suffix=1, **kwargs):
    customer = Customer(phone=f"+38050000{suffix:04}", name=f"Sandbox {suffix}", **kwargs)
    session.add(customer)
    await session.flush()
    return customer


async def add_booking(session, customer, end_at, status=BookingStatus.completed, master=None, service=None):
    if master is None:
        master = Master(full_name="Sandbox master")
        session.add(master)
        await session.flush()
    if service is None:
        service = BarberService(master_id=master.id, name="Sandbox haircut", duration_minutes=30, price=100)
        session.add(service)
        await session.flush()
    booking = Booking(customer_id=customer.id, customer_name=customer.name, customer_phone=customer.phone,
                      master_id=master.id, service_id=service.id, start_at=end_at-timedelta(minutes=30),
                      end_at=end_at, status=status)
    session.add(booking)
    await session.flush()
    return booking


def inactive_rules(**changes):
    rules = {"combine": "all", "conditions": [{"type": "last_visit_age", "min": 3, "max": 12,
                                                "unit": "calendar_months"}], "exclusions": []}
    rules.update(changes)
    return SegmentRules.model_validate(rules)


async def add_segment(session, name="Inactive", rules=None):
    segment = CustomerSegment(name=name, rules=(rules or inactive_rules()).model_dump(mode="json"))
    session.add(segment)
    await session.flush()
    return segment


async def add_campaign(session, segments, **metadata):
    template = MessageTemplate(name="Sandbox " + uuid4().hex, channel=MessageChannel.sms, body="Hello {{client_name}}")
    session.add(template)
    await session.flush()
    campaign = Campaign(name="Sandbox campaign", type=CampaignType.manual, status=CampaignStatus.draft,
                        channel=MessageChannel.sms, purpose=MessagePurpose.marketing, template_id=template.id,
                        metadata_json={"segment_ids": [segment.id for segment in segments], "marketing_frequency_days": 1, **metadata})
    session.add(campaign)
    await session.flush()
    return await MessagingService().get_campaign(session, campaign.id)


@pytest.mark.anyio
async def test_real_history_boundaries_imports_and_exclusions(database):
    from app.services.segments import SegmentService
    async with database() as session:
        upper = await add_customer(session, 1, imported_last_visit_at=datetime(2026, 6, 6, 12, tzinfo=KYIV))
        lower = await add_customer(session, 2, imported_last_visit_at=datetime(2025, 9, 6, 12, tzinfo=KYIV))
        imported = await add_customer(session, 3, imported_last_visit_at=datetime(2026, 2, 3, 12, tzinfo=KYIV))
        unknown = await add_customer(session, 4)
        empty = await add_customer(session, 5, imported_is_new_client=True)
        cancelled = await add_customer(session, 6)
        completed = await add_customer(session, 7)
        upcoming = await add_customer(session, 8)
        await add_booking(session, cancelled, AT-timedelta(days=180), BookingStatus.cancelled)
        await add_booking(session, completed, AT-timedelta(days=180))
        await add_booking(session, completed, AT-timedelta(days=1), BookingStatus.cancelled)
        await add_booking(session, upcoming, AT-timedelta(days=180))
        await add_booking(session, upcoming, AT+timedelta(days=1), BookingStatus.confirmed)
        await session.commit()
        service = SegmentService()
        rules = inactive_rules(exclusions=[{"type": "upcoming_booking", "present": True}])
        result = await service.preview(session, rules, evaluated_at=AT, limit=2)
        rest = await service.preview(session, rules, evaluated_at=AT, limit=2, offset=2)
        assert result.total == rest.total == 3
        assert {m.customer_id for m in result.items+rest.items} == {lower.id, imported.id, completed.id}
        assert result.evaluated_at == rest.evaluated_at == AT
        imported_facts = await service.member_facts(session, [unknown.id, empty.id, imported.id], rules, AT)
        assert imported_facts[unknown.id]["history_state"] == "unknown"
        assert imported_facts[empty.id]["history_state"] == "no_visits"
        assert imported_facts[imported.id]["completed_visit_count"] == 0
        assert imported_facts[imported.id]["first_completed_visit_at"] is None


@pytest.mark.anyio
async def test_any_count_period_and_service_items(database):
    from app.models.booking import BookingServiceItem
    from app.services.segments import SegmentService
    async with database() as session:
        customer = await add_customer(session)
        booking = await add_booking(session, customer, AT-timedelta(days=10))
        second_service = BarberService(master_id=booking.master_id, name="Sandbox beard", duration_minutes=15, price=50)
        session.add(second_service)
        await session.flush()
        session.add(BookingServiceItem(booking_id=booking.id, service_id=second_service.id, position=1, price_amount=50))
        imported = await add_customer(session, 2, imported_last_visit_at=AT-timedelta(days=10))
        await add_customer(session, 3)
        await session.commit()
        rules = SegmentRules.model_validate({"combine": "any", "conditions": [
            {"type": "received_service", "service_ids": [second_service.id], "period": {"last": 30, "unit": "days"}},
            {"type": "completed_visit_count", "min": 2}]})
        result = await SegmentService().preview(session, rules, evaluated_at=AT)
        assert [m.customer_id for m in result.items] == [customer.id]
        zero = SegmentRules.model_validate({"conditions": [{"type": "completed_visit_count", "max": 0}]})
        zero_result = await SegmentService().preview(session, zero, evaluated_at=AT)
        assert imported.id not in {m.customer_id for m in zero_result.items}


@pytest.mark.anyio
async def test_launch_deduplicates_and_freezes_snapshot_before_sandbox_delivery(database):
    from app.services.campaign_runs import CampaignRunService
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        first = await add_segment(session)
        second = await add_segment(session, "Overlap")
        campaign = await add_campaign(session, [first, second])
        await session.commit()
        provider = SandboxProvider()
        runs = CampaignRunService(MessagingService(providers={MessageChannel.sms: provider}))
        run = await runs.launch(session, campaign, idempotency_key="sandbox-run")
        assert run.audience_count == 1
        assert provider.sent == []
        frozen = list(run.segment_snapshots)
        first.rules = inactive_rules(conditions=[{"type": "completed_visit_count", "min": 999}]).model_dump(mode="json")
        first.revision += 1
        await session.commit()
        duplicate = await runs.launch(session, campaign, idempotency_key="sandbox-run")
        assert duplicate.id == run.id
        assert duplicate.segment_snapshots == frozen
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id).options(*_recipient_delivery_load_options()))).one()
        await runs.send_recipient(session, recipient)
        await runs.send_recipient(session, recipient)
        await session.refresh(recipient)
        assert len(provider.sent) == 1
        assert recipient.status in {MessageDeliveryStatus.sent, MessageDeliveryStatus.delivered}
        assert recipient.customer_id == customer.id
        assert recipient.snapshot_facts is not None


@pytest.mark.anyio
async def test_consent_change_before_send_suppresses_frozen_member(database):
    from app.services.campaign_runs import CampaignRunService
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment])
        await session.commit()
        provider = SandboxProvider()
        runs = CampaignRunService(MessagingService(providers={MessageChannel.sms: provider}))
        run = await runs.launch(session, campaign, idempotency_key="consent-change")
        session.add(ClientCommunicationPreference(customer_id=customer.id, marketing_consent=ConsentStatus.opted_out))
        await session.commit()
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id).options(*_recipient_delivery_load_options()))).one()
        await runs.send_recipient(session, recipient)
        await session.refresh(recipient)
        assert provider.sent == []
        assert recipient.status == MessageDeliveryStatus.skipped
        assert recipient.last_error


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["upcoming_booking", "returned_since_snapshot"])
async def test_return_campaign_rechecks_booking_changes_before_send(database, change):
    from app.services.campaign_runs import CampaignRunService
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment], **{"exclude_"+change: True})
        await session.commit()
        provider = SandboxProvider()
        runs = CampaignRunService(MessagingService(providers={MessageChannel.sms: provider}))
        run = await runs.launch(session, campaign, idempotency_key=change)
        await add_booking(session, customer,
                          datetime.now(KYIV)+timedelta(days=1) if change == "upcoming_booking" else datetime.now(KYIV),
                          BookingStatus.confirmed if change == "upcoming_booking" else BookingStatus.completed)
        await session.commit()
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id))).one()
        await runs.send_recipient(session, recipient)
        assert provider.sent == []
        assert recipient.status == MessageDeliveryStatus.skipped
        assert recipient.last_error == change
        await session.refresh(run)
        assert run.status == "completed"


@pytest.mark.anyio
async def test_concurrent_launch_and_delivery_reservations(database):
    from app.services.campaign_runs import CampaignRunService
    async with database() as session:
        await add_customer(session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        segment = await add_segment(session)
        first = await add_campaign(session, [segment], marketing_frequency_days=7)
        second = await add_campaign(session, [segment], marketing_frequency_days=7)
        await session.commit()
        first_id, second_id = first.id, second.id
    provider = SandboxProvider()
    service = CampaignRunService(MessagingService(providers={MessageChannel.sms: provider}))

    async def launch(campaign_id, key):
        async with database() as session:
            campaign = await service.messaging.get_campaign(session, campaign_id)
            run = await service.launch(session, campaign, idempotency_key=key)
            return run.id

    duplicate_ids = await asyncio.gather(launch(first_id, "same-run"), launch(first_id, "same-run"))
    assert duplicate_ids[0] == duplicate_ids[1]
    other_run_id = await launch(second_id, "other-run")
    async with database() as session:
        recipient_ids = list(await session.scalars(select(MessageRecipient.id).where(
            MessageRecipient.run_id.in_([duplicate_ids[0], other_run_id]))))
    assert len(recipient_ids) == 2

    async def send(recipient_id):
        async with database() as session:
            recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.id == recipient_id)
                                               .options(*_recipient_delivery_load_options()))).one()
            await service.send_recipient(session, recipient)

    outcomes = await asyncio.gather(*(send(recipient_id) for recipient_id in recipient_ids),
                                    send(recipient_ids[0]), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    assert len(provider.sent) == 1
    async with database() as session:
        outcomes = list(await session.scalars(select(MessageRecipient).where(MessageRecipient.id.in_(recipient_ids))))
        assert sum(row.status == MessageDeliveryStatus.skipped for row in outcomes) == 1


@pytest.mark.anyio
async def test_snapshot_pages_share_database_view_and_concurrent_completion(database, monkeypatch):
    from app.services import campaign_runs
    from app.services.segments import segment_service
    async with database() as session:
        await add_customer(session, 1, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        second = await add_customer(session, 2, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment])
        await session.commit()
        second_id = second.id
        original_facts = segment_service.member_facts
        changed = False

        async def mutate_after_first_page(*args, **kwargs):
            nonlocal changed
            result = await original_facts(*args, **kwargs)
            if not changed:
                changed = True
                async with database() as other:
                    updated = await other.get(Customer, second_id)
                    updated.imported_last_visit_at = datetime.now(KYIV)-timedelta(days=1)
                    await other.commit()
            return result

        monkeypatch.setattr(campaign_runs, "BATCH_SIZE", 1)
        monkeypatch.setattr(segment_service, "member_facts", mutate_after_first_page)
        runs = campaign_runs.CampaignRunService()
        run = await runs.launch(session, campaign, idempotency_key="consistent-pages")
        assert changed and run.audience_count == 2
        recipients = list(await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id)))
        second_facts = next(row for row in recipients if row.customer_id == second_id).snapshot_facts["segments"][str(segment.id)]
        assert datetime.fromisoformat(second_facts["last_visit_at"]) < datetime.now(KYIV)-timedelta(days=100)
        recipient_ids = [row.id for row in recipients]
        run_id = run.id

    class BarrierProvider(SandboxProvider):
        def __init__(self):
            super().__init__()
            self.ready = asyncio.Event()

        async def send_message(self, **kwargs):
            result = await super().send_message(**kwargs)
            if len(self.sent) == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=5)
            return result

    provider = BarrierProvider()
    service = campaign_runs.CampaignRunService(MessagingService(providers={MessageChannel.sms: provider}))

    async def send(recipient_id):
        async with database() as session:
            recipient = await session.get(MessageRecipient, recipient_id)
            await service.send_recipient(session, recipient)

    outcomes = await asyncio.gather(*(send(recipient_id) for recipient_id in recipient_ids), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    async with database() as session:
        finished = await session.get(CampaignRun, run_id)
        assert finished.status == "completed"
    assert len(provider.sent) == 2


@pytest.mark.anyio
async def test_ambiguous_delivery_is_not_retried_or_fallen_back(database):
    from app.services.campaign_runs import CampaignRunService
    async with database() as session:
        customer = await add_customer(session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        session.add(ClientCommunicationPreference(customer_id=customer.id, telegram_chat_id="sandbox-chat"))
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment], channel_strategy="telegram_then_sms")
        await session.commit()
        telegram = SandboxProvider(MessageChannel.telegram, error=TimeoutError("sandbox ambiguous acceptance"))
        sms = SandboxProvider()
        runs = CampaignRunService(MessagingService(providers={MessageChannel.telegram: telegram, MessageChannel.sms: sms}))
        run = await runs.launch(session, campaign, idempotency_key="uncertain")
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id).options(*_recipient_delivery_load_options()))).one()
        await runs.send_recipient(session, recipient)
        await runs.send_recipient(session, recipient)
        assert len(telegram.sent) == 1
        assert sms.sent == []
        assert recipient.last_error.startswith("delivery_uncertain")
        await session.refresh(run)
        assert run.status == "completed"


@pytest.mark.anyio
async def test_crashed_worker_claim_becomes_observable_failure_without_resending(database):
    from app.services.campaign_runs import CampaignRunService
    async with database() as session:
        await add_customer(session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment])
        await session.commit()
        provider = SandboxProvider()
        runs = CampaignRunService(MessagingService(providers={MessageChannel.sms: provider}))
        run = await runs.launch(session, campaign, idempotency_key="interrupted-worker")
        recipient = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id))).one()
        recipient.send_started_at = datetime.now(KYIV)-timedelta(minutes=16)
        recipient.attempts = 1
        await session.commit()
        await runs.process_run_messages(session)
        await session.refresh(recipient)
        await session.refresh(run)
        assert recipient.status == MessageDeliveryStatus.failed
        assert recipient.last_error == "delivery_uncertain: worker_interrupted"
        assert run.status == "completed"
        assert await runs.messaging.retry_failed(session, campaign.id) == 0
        assert provider.sent == []


@pytest.mark.anyio
async def test_customer_delete_waits_for_concurrent_snapshot_before_history_check(database):
    from app.services.campaign_runs import CampaignRunService
    from app.services.customer_auth import CustomerAuthService
    async with database() as snapshot_session:
        customer = await add_customer(snapshot_session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        segment = await add_segment(snapshot_session)
        campaign = await add_campaign(snapshot_session, [segment])
        await snapshot_session.commit()
        service = CampaignRunService()
        run = await service.launch(snapshot_session, campaign, idempotency_key="delete-race",
                                   scheduled_at=datetime.now(KYIV)+timedelta(days=1))
        await service.snapshot(snapshot_session, run, campaign)
        assert run.audience_count == 1
        customer_id, run_id = customer.id, run.id
        pid_ready = asyncio.Event()
        delete_pid = None

        async def delete():
            nonlocal delete_pid
            async with database() as session:
                target = await session.get(Customer, customer_id)
                delete_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                pid_ready.set()
                await CustomerAuthService().delete_customer(session, target)

        delete_task = asyncio.create_task(delete())
        try:
            await pid_ready.wait()
            # Observe actual database contention, not a timing-only sleep: the
            # recipient FK remains uncommitted while deletion chooses its policy.
            for _ in range(100):
                waiting = await snapshot_session.scalar(text(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": delete_pid})
                if waiting:
                    break
                await asyncio.sleep(0.02)
            assert waiting, "Deletion should wait for the in-flight recipient foreign-key lock"
            await snapshot_session.commit()
            await asyncio.wait_for(delete_task, timeout=5)
        finally:
            if not delete_task.done():
                delete_task.cancel()
                await asyncio.gather(delete_task, return_exceptions=True)
    async with database() as session:
        preserved = await session.get(Customer, customer_id)
        assert preserved is not None and preserved.is_active is False
        assert len(list(await session.scalars(select(MessageRecipient.id).where(MessageRecipient.run_id == run_id)))) == 1


@pytest.mark.anyio
async def test_feature_migration_upgrade_downgrade_preserves_legacy_recipient(database):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    async with database() as session:
        customer = await add_customer(session)
        campaign = await add_campaign(session, [])
        legacy = MessageRecipient(campaign_id=campaign.id, customer_id=customer.id,
                                  channel=MessageChannel.sms, idempotency_key="legacy-record")
        session.add(legacy)
        await session.commit()
        legacy_id = legacy.id
        for sql in (
            "ALTER TABLE message_recipients DROP COLUMN run_id CASCADE",
            "ALTER TABLE message_recipients DROP COLUMN snapshot_facts",
            "ALTER TABLE message_recipients DROP COLUMN send_started_at",
            "DROP TABLE campaign_runs", "DROP TABLE customer_segments",
            "DROP INDEX IF EXISTS ix_bookings_customer_status_end",
            "DROP INDEX IF EXISTS ix_bookings_customer_status_start",
            "DROP INDEX IF EXISTS ix_message_recipients_customer_sent",
        ):
            await session.execute(text(sql))
        spec = importlib.util.spec_from_file_location("segments_migration", Path(__file__).parents[1] / "alembic/versions/0068_customer_segments.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        def run_migration(connection, direction):
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, direction)()

        connection = await session.connection()
        await connection.run_sync(run_migration, "upgrade")
        assert (await session.execute(text("SELECT run_id FROM message_recipients WHERE id=:id"), {"id": legacy_id})).scalar() is None
        session.add(CustomerSegment(name="Migrated model write", rules=inactive_rules().model_dump(mode="json")))
        await session.flush()
        await connection.run_sync(run_migration, "downgrade")
        assert (await session.execute(text("SELECT idempotency_key FROM message_recipients WHERE id=:id"), {"id": legacy_id})).scalar() == "legacy-record"
        await connection.run_sync(run_migration, "upgrade")
        await session.commit()


@pytest.mark.anyio
async def test_authenticated_http_segment_to_draft_snapshot_delivery_and_results(database, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.core.database import get_db_session
    from app.core.security import create_access_token
    from app.main import app
    from app.models.admin_user import AdminUser
    from app.services.campaign_runs import CampaignRunService
    from app.api.v1.routes import messaging as messaging_routes

    async def sandbox_background():
        async with database() as session:
            await CampaignRunService(MessagingService(providers={MessageChannel.sms: provider})).process_run_messages(session)

    monkeypatch.setattr(messaging_routes, "_process_pending_messages_background", sandbox_background)

    async with database() as session:
        await add_customer(session, imported_last_visit_at=datetime.now(KYIV)-timedelta(days=180))
        admin = AdminUser(email="segments-sandbox@example.test", hashed_password="unused-test-hash")
        session.add(admin)
        await session.commit()
        token = create_access_token(str(admin.id))

    async def isolated_session():
        async with database() as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db_session] = isolated_session
    provider = SandboxProvider()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sandbox.test",
                               headers={"Authorization": f"Bearer {token}"}) as client:
            root = "/api/v1/backoffice"
            created = await client.post(root+"/segments", json={"name": "Return customers", "rules": inactive_rules().model_dump(mode="json")})
            assert created.status_code == 201, created.text
            segment = created.json()
            preview = await client.get(root+f"/segments/{segment['id']}/members")
            assert preview.status_code == 200, preview.text
            assert preview.json()["total"] == 1
            unsupported_age = await client.post(root+"/segments/preview", json={
                "rules": inactive_rules().model_dump(mode="json"), "evaluated_at": "0001-01-01T00:00:00Z"})
            assert unsupported_age.status_code == 422, unsupported_age.text
            draft = await client.post(root+"/messaging/campaigns", json={
                "name": "Sandbox return", "type": "manual", "channel": "sms", "segment_ids": [segment["id"]],
                "metadata_json": {"message_body": "Hello {{client_name}}"}})
            assert draft.status_code == 201, draft.text
            assert draft.json()["status"] == "draft"
            campaign_id = draft.json()["id"]
            async with database() as session:
                assert not list(await session.scalars(select(MessageRecipient.id)))
            launched = await client.post(root+f"/messaging/campaigns/{campaign_id}/runs", json={"idempotency_key": "http-smoke"})
            assert launched.status_code == 201, launched.text
            run_id = launched.json()["id"]
            assert launched.json()["audience_count"] == 1
            async with database() as session:
                assert await CampaignRunService(MessagingService(providers={MessageChannel.sms: provider})).process_run_messages(session) == 1
            assert len(provider.sent) == 1
            inspected = await client.get(root+f"/messaging/campaigns/{campaign_id}/runs/{run_id}")
            assert inspected.status_code == 200, inspected.text
            assert inspected.json()["delivery_counts"] == {"sent": 1}
            members = await client.get(root+f"/messaging/campaigns/{campaign_id}/runs/{run_id}/members")
            assert members.status_code == 200, members.text
            assert members.json()["items"][0]["snapshot_facts"]["segments"]
            edited = await client.patch(root+f"/segments/{segment['id']}", json={"expected_revision": 1, "name": "Renamed segment"})
            assert edited.status_code == 200, edited.text
            stale = await client.patch(root+f"/segments/{segment['id']}", json={"expected_revision": 1, "name": "Stale edit"})
            assert stale.status_code == 409
            archived = await client.post(root+f"/segments/{segment['id']}/archive")
            assert archived.status_code == 200, archived.text
            historical = await client.get(root+f"/messaging/campaigns/{campaign_id}/runs/{run_id}")
            assert historical.json()["segment_snapshots"][0]["revision"] == 1
            inline = await client.post(root+"/messaging/campaigns", json={
                "name": "Legacy inline", "type": "loyalty_vip", "channel": "sms", "audience": {"all_clients": True},
                "metadata_json": {"message_body": "Legacy {{client_name}}"}})
            assert inline.status_code == 201, inline.text
            legacy_id = inline.json()["id"]
            started = await client.post(root+f"/messaging/campaigns/{legacy_id}/start", json={})
            assert started.status_code == 200, started.text
            repeated = await client.post(root+f"/messaging/campaigns/{legacy_id}/start", json={})
            assert repeated.json()["run_id"] == started.json()["run_id"]
            assert started.json()["enqueued"] == 1
            assert len(provider.sent) == 1  # Legacy run shares the cross-campaign marketing cap.
            customer_id = members.json()["items"][0]["customer_id"]
            deleted = await client.delete(root+f"/customers/{customer_id}")
            assert deleted.status_code == 204, deleted.text
            async with database() as session:
                preserved = await session.get(Customer, customer_id)
                assert preserved is not None and preserved.is_active is False
            preserved_members = await client.get(root+f"/messaging/campaigns/{campaign_id}/runs/{run_id}/members")
            assert preserved_members.json()["total"] == 1
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
