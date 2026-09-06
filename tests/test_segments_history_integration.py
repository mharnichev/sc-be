"""Additional historical and scheduled behavior on the isolated test database."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.booking import BarberService, BookingServiceItem
from app.models.campaign_run import CampaignRun
from app.models.messaging import Campaign, CampaignType, MessageChannel, MessageDeliveryStatus, MessagePurpose, MessageRecipient
from app.schemas.segment import SegmentRules
from app.services.campaign_runs import CampaignRunService
from app.services.messaging import MessagingService
from app.services.segments import SegmentService
from test_segments_integration import (
    AT, KYIV, SandboxProvider, add_booking, add_campaign, add_customer, add_segment,
    anyio_backend, database, inactive_rules,
)


@pytest.mark.anyio
async def test_scheduled_run_snapshots_current_revision_and_customers_only_when_due(database):
    now = datetime.now(KYIV)
    provider = SandboxProvider()
    runs = CampaignRunService(MessagingService(providers={MessageChannel.sms: provider}))
    async with database() as session:
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment])
        await session.commit()
        run = await runs.launch(session, campaign, scheduled_at=now+timedelta(days=1), idempotency_key="scheduled")
        assert run.status == "scheduled"
        assert run.evaluated_at is None
        assert run.segment_snapshots == []
        assert not list(await session.scalars(select(MessageRecipient.id)))
        assert await runs.process_due_runs(session) == 0

        customer = await add_customer(session, imported_last_visit_at=now-timedelta(days=60))
        segment.rules = inactive_rules(conditions=[{"type": "last_visit_age", "min": 1, "max": 12}]).model_dump(mode="json")
        segment.revision = 2
        run.scheduled_at = now-timedelta(minutes=1)
        await session.commit()
        assert await runs.process_due_runs(session) == 1
        await session.refresh(run)
        assert run.status == "snapshotted"
        assert run.evaluated_at >= now
        assert run.segment_snapshots[0]["revision"] == 2
        assert run.audience_count == 1
        assert provider.sent == []
        member = (await session.scalars(select(MessageRecipient).where(MessageRecipient.run_id == run.id))).one()
        assert member.customer_id == customer.id
        frozen = run.segment_snapshots
        segment.revision = 3
        segment.rules = inactive_rules().model_dump(mode="json")
        await session.commit()
        assert await runs.process_due_runs(session) == 0
        await session.refresh(run)
        assert run.segment_snapshots == frozen


@pytest.mark.anyio
async def test_receipt_conditions_preserve_run_purpose_and_ignore_pending_failed_and_future(database):
    async with database() as session:
        customer = await add_customer(session)
        failed_customer = await add_customer(session, 2)
        future_customer = await add_customer(session, 3)
        notification_customer = await add_customer(session, 4)
        segment = await add_segment(session)
        campaign = await add_campaign(session, [segment])
        run = CampaignRun(campaign_id=campaign.id, idempotency_key="accepted-history", status="completed",
                          evaluated_at=AT-timedelta(days=2), campaign_snapshot={"purpose": "marketing"})
        session.add(run)
        await session.flush()
        for who, state, sent_at in (
            (customer, MessageDeliveryStatus.sent, AT-timedelta(days=1)),
            (failed_customer, MessageDeliveryStatus.failed, AT-timedelta(days=1)),
            (future_customer, MessageDeliveryStatus.sent, AT+timedelta(days=1)),
        ):
            session.add(MessageRecipient(
                run_id=run.id, campaign_id=campaign.id, customer_id=who.id,
                idempotency_key=f"history:{who.id}", status=state, channel=MessageChannel.sms, sent_at=sent_at,
            ))
        campaign.purpose = MessagePurpose.transactional
        notification = Campaign(name="Historical notification", type=CampaignType.booking_confirmation,
                                purpose=MessagePurpose.marketing)
        session.add(notification)
        await session.flush()
        session.add(MessageRecipient(campaign_id=notification.id, customer_id=notification_customer.id,
                                     idempotency_key="notification-history", status=MessageDeliveryStatus.sent,
                                     channel=MessageChannel.sms, sent_at=AT-timedelta(days=1)))
        await session.commit()
        service = SegmentService()
        marketing = SegmentRules(conditions=[{"type": "marketing_contact", "period": {"last": 7}}])
        result = await service.preview(session, marketing, evaluated_at=AT)
        assert [item.customer_id for item in result.items] == [customer.id]
        received = SegmentRules(conditions=[{"type": "received_campaign", "campaign_id": campaign.id}])
        result = await service.preview(session, received, evaluated_at=AT)
        assert [item.customer_id for item in result.items] == [customer.id]
        assert result.items[0].conditions[0]["value"] == (AT-timedelta(days=1)).astimezone(
            result.evaluated_at.tzinfo
        ).isoformat()


@pytest.mark.anyio
async def test_service_items_override_stale_primary_service(database):
    async with database() as session:
        customer = await add_customer(session)
        booking = await add_booking(session, customer, AT-timedelta(days=1))
        stale_id = booking.service_id
        actual = BarberService(master_id=booking.master_id, name="Actual received service", duration_minutes=15, price=10)
        session.add(actual)
        await session.flush()
        session.add(BookingServiceItem(booking_id=booking.id, service_id=actual.id, position=1, price_amount=10))
        await session.commit()
        service = SegmentService()
        for service_id, expected in ((stale_id, 0), (actual.id, 1)):
            rules = SegmentRules(conditions=[{
                "type": "received_service", "service_ids": [service_id], "period": {"last": 7},
            }])
            assert (await service.preview(session, rules, evaluated_at=AT)).total == expected


@pytest.mark.anyio
async def test_imported_timestamps_cannot_invent_first_visit_or_latest_master(database):
    async with database() as session:
        older_import = await add_customer(session, imported_last_visit_at=AT-timedelta(days=100))
        older_booking = await add_booking(session, older_import, AT-timedelta(days=10))
        newer_import = await add_customer(session, 2, imported_last_visit_at=AT-timedelta(days=5))
        newer_booking = await add_booking(session, newer_import, AT-timedelta(days=10))
        imported_only = await add_customer(session, 3, imported_last_visit_at=AT-timedelta(days=5))
        await session.commit()
        service = SegmentService()
        first_rules = SegmentRules(conditions=[{"type": "first_visit", "period": {"last": 30}}])
        first = await service.preview(session, first_rules, evaluated_at=AT)
        assert [item.customer_id for item in first.items] == [newer_import.id]
        masters = SegmentRules(conditions=[{
            "type": "visited_master", "master_ids": [older_booking.master_id, newer_booking.master_id],
        }])
        last_master = await service.preview(session, masters, evaluated_at=AT)
        assert [item.customer_id for item in last_master.items] == [older_import.id]
        facts = await service.member_facts(session, [older_import.id, newer_import.id, imported_only.id], first_rules, AT)
        assert facts[older_import.id]["first_completed_visit_at"] is None
        assert facts[imported_only.id]["first_completed_visit_at"] is None
        assert facts[imported_only.id]["completed_visit_count"] == 0
