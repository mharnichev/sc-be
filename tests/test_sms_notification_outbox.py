"""Secure-link notification outbox is atomic and never regenerates queued tokens."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.customer_activity import CustomerActivityAccessToken
from app.models.messaging import CampaignStatus, CampaignType, MessageChannel, MessageDeliveryStatus, MessagePurpose, MessageRecipient
from app.models.sms_queue import SmsQueueJob
from app.services.sms import SmsService
from test_segments_integration import add_booking, add_campaign, add_customer, anyio_backend, database
from test_sms_queue_integration import sms_transport


@pytest.mark.anyio
async def test_concurrent_secure_link_enqueues_commit_one_token_and_one_job(database, sms_transport, monkeypatch):
    from app.services import customer_activity_notifications as notifications
    from app.services.campaign_dispatch import CampaignDispatchService
    from app.services.sms_queue import SmsQueueService
    monkeypatch.setattr(notifications, "AsyncSessionLocal", database)
    dispatch = CampaignDispatchService(session_factory=database)
    queue = SmsQueueService(session_factory=database, before_dispatch=dispatch.sms_job_eligibility,
                            after_outcome=dispatch.sms_job_outcome)
    service = notifications.CustomerActivityNotificationService(sms_service=SmsService(queue=queue))
    async with database() as session:
        customer = await add_customer(session)
        booking = await add_booking(session, customer, datetime.now(UTC) + timedelta(days=2))
        campaign = await add_campaign(session, [])
        campaign.type = CampaignType.booking_confirmation
        campaign.purpose = MessagePurpose.transactional
        campaign.status = CampaignStatus.active
        recipient = MessageRecipient(
            campaign_id=campaign.id, customer_id=customer.id, appointment_id=booking.id,
            channel=MessageChannel.sms, status=MessageDeliveryStatus.pending,
            idempotency_key=f"customer-activity:booking_confirmation:booking:{booking.id}",
            rendered_message="Your appointment: {manage_url}; cancel: {cancel_url}", attempts=0,
        )
        session.add(recipient)
        await session.commit()
        recipient_id = recipient.id
    await asyncio.gather(service._dispatch(recipient_id), service._dispatch(recipient_id))
    async with database() as session:
        jobs = list(await session.scalars(select(SmsQueueJob)))
        tokens = list(await session.scalars(select(CustomerActivityAccessToken).where(
            CustomerActivityAccessToken.recipient_id == recipient_id,
        )))
        assert len(jobs) == len(tokens) == 1
        assert tokens[0].revoked_at is None
        body = jobs[0].payload["message"]
        assert "{manage_url}" not in body
        recipient = await session.get(MessageRecipient, recipient_id)
        assert recipient.sms_queue_job_id == jobs[0].id
        assert recipient.status == MessageDeliveryStatus.pending
    assert sms_transport.requests == []
    await queue.process_one()
    await service._dispatch(recipient_id)
    assert [request["payload"]["message"] for request in sms_transport.requests] == [body]
    async with database() as session:
        recipient = await session.get(MessageRecipient, recipient_id)
        assert recipient.status == MessageDeliveryStatus.sent
        assert await session.scalar(select(func.count()).select_from(SmsQueueJob)) == 1
        assert await session.scalar(select(func.count()).select_from(CustomerActivityAccessToken)) == 1
