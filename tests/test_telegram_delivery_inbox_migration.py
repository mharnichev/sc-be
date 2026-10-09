"""Upgrade preserves prior outcomes while fencing unclassified Telegram attempts."""
import importlib.util
from datetime import date
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from test_inventory_integration import anyio_backend, database  # noqa: F401
from app.models.booking import Master
from app.models.customer import Customer
from app.models.messaging import (Campaign, CampaignStatus, CampaignType, MasterMessageDelivery,
    MasterScheduleReminder, MessageChannel, MessageDeliveryStatus, MessagePurpose, MessageRecipient,
    TelegramDeliveryAttempt, TelegramWebhookEvent)
from app.services.telegram_delivery import TelegramDeliveryUncertain, durable_telegram_send

pytestmark = pytest.mark.anyio


async def test_upgrade_fences_only_known_historical_uncertain_attempts_and_restart_does_not_send(database):
    async with database() as session:
        customer = Customer(name="Synthetic", phone="+380501112299")
        master = Master(full_name="Synthetic", telegram_chat_id="synthetic-chat")
        campaign = Campaign(name="Synthetic migration", type=CampaignType.appointment_reminder,
            status=CampaignStatus.active, channel=MessageChannel.telegram, purpose=MessagePurpose.transactional,
            metadata_json={"message_body": "Synthetic"})
        session.add_all([customer, master, campaign])
        await session.flush()
        rows = [MessageRecipient(campaign_id=campaign.id, customer_id=customer.id,
            idempotency_key=f"migration:{name}", channel=channel, status=status,
            attempts=attempts, provider_message_id=provider_id, rendered_message="Synthetic")
            for name, channel, status, attempts, provider_id in [
                ("unknown", MessageChannel.telegram, MessageDeliveryStatus.failed, 2, None),
                ("pending-unknown", MessageChannel.telegram, MessageDeliveryStatus.pending, 1, None),
                ("never-started", MessageChannel.telegram, MessageDeliveryStatus.pending, 0, None),
                ("accepted", MessageChannel.telegram, MessageDeliveryStatus.sent, 1, "synthetic-accepted"),
                ("sms", MessageChannel.sms, MessageDeliveryStatus.failed, 2, None)]]
        session.add_all(rows)
        session.add(MasterMessageDelivery(campaign_id=campaign.id, master_id=master.id,
            trigger="booking_created", channel=MessageChannel.telegram, status=MessageDeliveryStatus.failed,
            idempotency_key="synthetic-historical-master", attempts=2))
        session.add(MasterScheduleReminder(campaign_id=campaign.id, master_id=master.id,
            target_month=date(2026, 9, 1), initial_attempts=2, follow_up_attempts=0))
        await session.commit()
        recipient_ids = [row.id for row in rows]
        schedule_key = f"master-schedule:{campaign.id}:{master.id}:2026-09-01:initial"
    engine = database.kw["bind"]
    path = Path(__file__).parents[1] / "alembic/versions/0103_telegram_delivery_inbox.py"
    spec = importlib.util.spec_from_file_location("telegram_upgrade_under_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramWebhookEvent.__table__.drop)
        await connection.run_sync(TelegramDeliveryAttempt.__table__.drop)
        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
        await connection.run_sync(upgrade)
    async with database() as session:
        attempts = {row.key: row for row in await session.scalars(select(TelegramDeliveryAttempt))}
        assert set(attempts) == {f"recipient:{recipient_ids[0]}:telegram", f"recipient:{recipient_ids[1]}:telegram",
            "synthetic-historical-master", schedule_key}
        assert all(row.state == "ambiguous" for row in attempts.values())
        existing = list(await session.scalars(select(MessageRecipient).order_by(MessageRecipient.id)))
        assert [row.attempts for row in existing] == [2, 1, 0, 1, 2]
        assert existing[3].status == MessageDeliveryStatus.sent
        assert existing[3].provider_message_id == "synthetic-accepted"
        class MustNotSend:
            async def send_message(self, **kwargs):
                raise AssertionError("Historical uncertain send must not be repeated")
        for key in attempts:
            with pytest.raises(TelegramDeliveryUncertain):
                await durable_telegram_send(session, MustNotSend(), key=key, destination="synthetic-chat", body="Synthetic")
