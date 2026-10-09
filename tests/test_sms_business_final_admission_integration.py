"""Committed business restrictions at SMS admission, using a synthetic transport."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.models.booking import Master
from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignStatus, CampaignType, ClientCommunicationPreference, ConsentStatus,
    MessageChannel, MessageDeliveryStatus, MessagePurpose, MessageRecipient,
)
from app.models.sms_queue import SmsQueueJob
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.services import sms_notification_queue
from app.services.campaign_dispatch import CampaignDispatchService, KYIV_TZ, sending_interval
from app.services.campaign_runs import marketing_contact_predicate
from app.services.sms_queue import SmsQueueService
from app.services.waitlist_offers import WAITLIST_OFFER_SMS_LOCATION_KEY
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401
from tests.test_sms_queue_integration import SimulatedClock, sms_transport  # noqa: F401


pytestmark = pytest.mark.anyio


async def bounded(operation):
    return await asyncio.wait_for(operation, timeout=5)


def closed_window(clock):
    hour = clock.value.astimezone(KYIV_TZ).hour
    return {"start": f"{(hour + 2) % 24:02}:00", "end": f"{(hour + 3) % 24:02}:00", "days": list(range(7))}


def payload(phone):
    return {"phone": [phone.lstrip("+")], "message": "Synthetic admission test", "src_addr": "Soul Cuts"}


async def seed_campaign(database, clock, *, purpose=MessagePurpose.transactional, window=None):
    async with database() as session:
        customer = Customer(phone="+380500000111", name="Synthetic admission customer", is_active=True)
        campaign_type = {
            MessagePurpose.transactional: CampaignType.booking_confirmation,
            MessagePurpose.review_request: CampaignType.post_visit_review_request,
            MessagePurpose.marketing: CampaignType.manual,
        }[purpose]
        campaign = Campaign(
            name="Synthetic admission campaign", type=campaign_type, status=CampaignStatus.active,
            purpose=purpose, channel=MessageChannel.sms, sending_window=window,
            metadata_json={"marketing_frequency_days": 7},
        )
        session.add_all([customer, campaign])
        await bounded(session.flush())
        preference = ClientCommunicationPreference(
            customer_id=customer.id, marketing_consent=ConsentStatus.opted_in,
            transactional_consent=ConsentStatus.opted_in, do_not_contact=False,
        )
        recipient = MessageRecipient(
            campaign_id=campaign.id, customer_id=customer.id, channel=MessageChannel.sms,
            status=MessageDeliveryStatus.pending, idempotency_key="synthetic-final-admission-recipient",
            rendered_message="Synthetic admission test",
        )
        session.add_all([preference, recipient])
        await bounded(session.commit())
        return dict(campaign_id=campaign.id, customer_id=customer.id, recipient_id=recipient.id, phone=customer.phone)


async def add_recent_marketing_contact(session, context, clock):
    campaign = Campaign(
        name="Synthetic previous marketing", type=CampaignType.manual, purpose=MessagePurpose.marketing,
        status=CampaignStatus.active, channel=MessageChannel.sms,
    )
    session.add(campaign)
    await bounded(session.flush())
    session.add(MessageRecipient(
        campaign_id=campaign.id, customer_id=context["customer_id"], channel=MessageChannel.sms,
        status=MessageDeliveryStatus.sent, sent_at=clock.value - timedelta(hours=1),
        idempotency_key="synthetic-previous-marketing-contact",
    ))


async def campaign_queue(database, clock, context, *, priority=10):
    dispatch = CampaignDispatchService(session_factory=database, clock=lambda: clock.value)
    queue = SmsQueueService(
        session_factory=database, clock=clock,
        before_dispatch=dispatch.sms_job_eligibility, after_outcome=dispatch.sms_job_outcome,
    )
    job = await bounded(queue.enqueue(
        "send", payload(context["phone"]), priority=priority, idempotency_key="synthetic-campaign-admission",
        expires_at=clock.value + timedelta(hours=4),
        context={key: context[key] for key in ("campaign_id", "customer_id", "recipient_id")},
    ))
    return queue, job


async def seed_waitlist(database, clock):
    async with database() as session:
        customer = Customer(phone="+380500000222", name="Synthetic waitlist customer", is_active=True)
        master = Master(full_name="Synthetic waitlist master")
        campaign = Campaign(
            name="Synthetic waitlist campaign", type=CampaignType.booking_confirmation,
            purpose=MessagePurpose.transactional, status=CampaignStatus.active, channel=MessageChannel.sms,
            location_key=WAITLIST_OFFER_SMS_LOCATION_KEY,
        )
        session.add_all([customer, master, campaign])
        await bounded(session.flush())
        preference = ClientCommunicationPreference(
            customer_id=customer.id, marketing_consent=ConsentStatus.opted_in,
            transactional_consent=ConsentStatus.opted_in, do_not_contact=False,
        )
        request = WaitlistRequest(
            customer_id=customer.id, preferred_master_id=master.id, cancel_token_hash="a" * 64,
            dedup_key_hash="b" * 64, desired_date=(clock.value + timedelta(days=1)).date(),
            duration_minutes=30, notification_consent=True, status=WaitlistStatus.active,
            expires_at=clock.value + timedelta(days=1),
        )
        session.add_all([preference, request])
        await bounded(session.flush())
        offer = WaitlistOffer(
            request_id=request.id, master_id=master.id, start_at=clock.value + timedelta(days=1),
            end_at=clock.value + timedelta(days=1, minutes=30), token_hash="c" * 64,
            status=WaitlistOfferStatus.pending, expires_at=clock.value + timedelta(minutes=15),
            scheduled_at=clock.value,
        )
        session.add(offer)
        await bounded(session.commit())
        return dict(customer_id=customer.id, request_id=request.id, offer_id=offer.id, phone=customer.phone)


async def waitlist_queue(database, monkeypatch, clock, context):
    monkeypatch.setattr(sms_notification_queue, "AsyncSessionLocal", database)
    queue = SmsQueueService(
        session_factory=database, clock=clock,
        before_dispatch=sms_notification_queue.sms_job_eligibility,
        after_outcome=sms_notification_queue.sms_job_outcome,
    )
    job = await bounded(queue.enqueue(
        "send", payload(context["phone"]), priority=10, idempotency_key="synthetic-waitlist-admission",
        expires_at=clock.value + timedelta(hours=4),
        context={"waitlist_offer_id": context["offer_id"], "customer_id": context["customer_id"],
                 "safe_body": "Synthetic waitlist offer"},
    ))
    return queue, job


async def change_after_real_eligibility(database, queue, mutate):
    eligible, proceed = asyncio.Event(), asyncio.Event()
    real_eligibility = queue.before_dispatch
    barrier_pids = []

    async def pause_after_eligibility(job):
        decision = await real_eligibility(job)
        assert decision.action == "allow", decision
        # Keep a read-only connection at the barrier so the independent mutation
        # must commit on a different backend, after real eligibility has finished.
        async with database() as barrier_session:
            barrier_pids.append(await bounded(barrier_session.scalar(text("SELECT pg_backend_pid()"))))
            eligible.set()
            await bounded(proceed.wait())
        return decision

    queue.before_dispatch = pause_after_eligibility
    task = asyncio.create_task(queue.process_one())
    try:
        await bounded(eligible.wait())
        async with database() as session:
            mutation_pid = await bounded(session.scalar(text("SELECT pg_backend_pid()")))
            assert mutation_pid != barrier_pids[0]
            await mutate(session)
            await bounded(session.commit())
            assert await bounded(session.scalar(text("SELECT 1"))) == 1
        proceed.set()
        assert await bounded(task)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        queue.before_dispatch = real_eligibility


async def assert_skipped_job(database, job_id, reason):
    async with database() as session:
        job = await bounded(session.get(SmsQueueJob, job_id))
        assert (job.status, job.error_code, job.attempts) == ("skipped", reason, 0)
        assert job.transport_started_at is None
        assert job.provider_message_id is None
        assert job.lease_token is None
        assert job.outcome_projected_at is not None
        assert await bounded(session.scalar(text("SELECT 1"))) == 1


@pytest.mark.parametrize("change,purpose,reason", [
    ("dnc", MessagePurpose.transactional, "Client is marked do-not-contact"),
    ("dnc", MessagePurpose.review_request, "Client is marked do-not-contact"),
    ("transactional_opt_out", MessagePurpose.transactional, "Client opted out of transactional messages"),
    ("marketing_opt_out", MessagePurpose.review_request, "Client opted out of marketing messages"),
    ("inactive", MessagePurpose.transactional, None),
    ("inactive", MessagePurpose.review_request, None),
    ("blacklisted", MessagePurpose.transactional, "Client is blacklisted"),
])
async def test_priority_ten_campaign_rechecks_committed_contact_restriction_before_transport(
    database, sms_transport, change, purpose, reason,
):
    clock = SimulatedClock()
    context = await seed_campaign(database, clock, purpose=purpose)
    queue, job = await campaign_queue(database, clock, context)

    async def mutate(session):
        if change == "inactive":
            customer = await bounded(session.get(Customer, context["customer_id"]))
            customer.is_active = False
            return
        preference = (await bounded(session.scalars(select(ClientCommunicationPreference).where(
            ClientCommunicationPreference.customer_id == context["customer_id"],
        )))).one()
        if change == "dnc":
            preference.do_not_contact = True
        elif change == "blacklisted":
            preference.blacklisted_at = clock.value
        else:
            setattr(preference, "transactional_consent" if change == "transactional_opt_out" else "marketing_consent", ConsentStatus.opted_out)

    await change_after_real_eligibility(database, queue, mutate)
    if change == "inactive":
        assert len(sms_transport.requests) == 1
        async with database() as session:
            persisted = await bounded(session.get(SmsQueueJob, job.id))
            recipient = await bounded(session.get(MessageRecipient, context["recipient_id"]))
            assert (persisted.status, persisted.attempts) == ("accepted", 1)
            assert persisted.transport_started_at == clock.value
            assert persisted.provider_message_id is not None
            assert (recipient.status, recipient.last_error) == (MessageDeliveryStatus.sent, None)
            assert recipient.sent_at is not None
        clock.advance(3600)
        await bounded(queue.process_one())
        assert len(sms_transport.requests) == 1
        return
    assert sms_transport.requests == []
    await assert_skipped_job(database, job.id, reason)
    async with database() as session:
        recipient = await bounded(session.get(MessageRecipient, context["recipient_id"]))
        assert (recipient.status, recipient.last_error) == (MessageDeliveryStatus.skipped, reason)
        assert recipient.sent_at is None
    clock.advance(3600)
    await bounded(queue.process_one())
    assert sms_transport.requests == []


@pytest.mark.parametrize("change,reason", [
    ("offer_expired", "waitlist_offer_expired"),
    ("slot_started", "waitlist_offer_expired"),
    ("request_cancelled", "waitlist_request_inactive"),
    ("request_consent", "waitlist_contact_restricted"),
    ("dnc", "waitlist_contact_restricted"),
    ("transactional_opt_out", "waitlist_contact_restricted"),
    ("inactive", None),
    ("offer_cancelled", "waitlist_offer_inactive"),
])
async def test_waitlist_rechecks_committed_lifecycle_or_contact_change_before_transport(
    database, sms_transport, monkeypatch, change, reason,
):
    clock = SimulatedClock()
    context = await seed_waitlist(database, clock)
    queue, job = await waitlist_queue(database, monkeypatch, clock, context)

    async def mutate(session):
        if change.startswith("offer_") or change == "slot_started":
            offer = await bounded(session.get(WaitlistOffer, context["offer_id"]))
            if change == "offer_expired":
                offer.expires_at = clock.value - timedelta(seconds=1)
            elif change == "slot_started":
                offer.start_at = clock.value - timedelta(seconds=1)
                offer.end_at = clock.value + timedelta(minutes=30)
            else:
                offer.status = WaitlistOfferStatus.cancelled
        elif change.startswith("request_"):
            request = await bounded(session.get(WaitlistRequest, context["request_id"]))
            if change == "request_cancelled":
                request.status = WaitlistStatus.cancelled
            else:
                request.notification_consent = False
        elif change == "inactive":
            customer = await bounded(session.get(Customer, context["customer_id"]))
            customer.is_active = False
        else:
            preference = (await bounded(session.scalars(select(ClientCommunicationPreference).where(
                ClientCommunicationPreference.customer_id == context["customer_id"],
            )))).one()
            if change == "dnc":
                preference.do_not_contact = True
            else:
                preference.transactional_consent = ConsentStatus.opted_out

    await change_after_real_eligibility(database, queue, mutate)
    if change == "inactive":
        assert len(sms_transport.requests) == 1
        async with database() as session:
            persisted = await bounded(session.get(SmsQueueJob, job.id))
            offer = await bounded(session.get(WaitlistOffer, context["offer_id"]))
            request = await bounded(session.get(WaitlistRequest, context["request_id"]))
            assert (persisted.status, persisted.attempts) == ("accepted", 1)
            assert persisted.transport_started_at == clock.value
            assert persisted.provider_message_id is not None
            assert offer.status == WaitlistOfferStatus.sent
            assert request.status == WaitlistStatus.offered
            assert offer.sent_at is not None
        clock.advance(3600)
        await bounded(queue.process_one())
        assert len(sms_transport.requests) == 1
        return
    assert sms_transport.requests == []
    await assert_skipped_job(database, job.id, reason)
    async with database() as session:
        offer = await bounded(session.get(WaitlistOffer, context["offer_id"]))
        request = await bounded(session.get(WaitlistRequest, context["request_id"]))
        assert offer.status == WaitlistOfferStatus.cancelled
        assert offer.sent_at is None
        assert request.status == (WaitlistStatus.cancelled if change == "request_cancelled" else WaitlistStatus.active)
    clock.advance(3600)
    await bounded(queue.process_one())
    assert sms_transport.requests == []


@pytest.mark.parametrize("purpose", [MessagePurpose.transactional, MessagePurpose.review_request, MessagePurpose.marketing])
@pytest.mark.parametrize("active", [True, False])
async def test_campaign_admission_preserves_purpose_specific_consent_window_and_cap_controls(
    database, sms_transport, purpose, active,
):
    clock = SimulatedClock()
    marketing = purpose == MessagePurpose.marketing
    window = None if marketing else closed_window(clock)
    context = await seed_campaign(database, clock, purpose=purpose, window=window)
    async with database() as session:
        customer = await session.get(Customer, context["customer_id"])
        customer.is_active = active
        await session.commit()
    if not marketing:
        async with database() as session:
            preference = (await bounded(session.scalars(select(ClientCommunicationPreference).where(
                ClientCommunicationPreference.customer_id == context["customer_id"],
            )))).one()
            if purpose == MessagePurpose.transactional:
                preference.marketing_consent = ConsentStatus.opted_out
            else:
                preference.transactional_consent = ConsentStatus.opted_out
            await add_recent_marketing_contact(session, context, clock)
            await bounded(session.commit())
            assert await bounded(session.scalar(select(marketing_contact_predicate(context["customer_id"], clock.value, 7))))
        assert sending_interval(clock.value, window)[0] > clock.value
    queue, job = await campaign_queue(database, clock, context, priority=100 if marketing else 10)
    assert await bounded(queue.process_one())
    assert len(sms_transport.requests) == 1
    async with database() as session:
        persisted = await bounded(session.get(SmsQueueJob, job.id))
        recipient = await bounded(session.get(MessageRecipient, context["recipient_id"]))
        assert (persisted.status, persisted.attempts) == ("accepted", 1)
        assert persisted.transport_started_at == clock.value
        assert recipient.status == MessageDeliveryStatus.sent
        assert recipient.sent_at is not None
    clock.advance(3600)
    await bounded(queue.process_one())
    assert len(sms_transport.requests) == 1


@pytest.mark.parametrize("active", [True, False])
async def test_waitlist_admission_still_sends_a_consented_unexpired_offer(database, sms_transport, monkeypatch, active):
    clock = SimulatedClock()
    context = await seed_waitlist(database, clock)
    async with database() as session:
        customer = await session.get(Customer, context["customer_id"])
        customer.is_active = active
        await session.commit()
    queue, job = await waitlist_queue(database, monkeypatch, clock, context)
    assert await bounded(queue.process_one())
    assert len(sms_transport.requests) == 1
    async with database() as session:
        persisted = await bounded(session.get(SmsQueueJob, job.id))
        offer = await bounded(session.get(WaitlistOffer, context["offer_id"]))
        request = await bounded(session.get(WaitlistRequest, context["request_id"]))
        assert (persisted.status, persisted.attempts) == ("accepted", 1)
        assert offer.status == WaitlistOfferStatus.sent
        assert request.status == WaitlistStatus.offered
        assert offer.sent_at is not None
        assert persisted.outcome_projected_at is not None
    clock.advance(3600)
    await bounded(queue.process_one())
    assert len(sms_transport.requests) == 1


@pytest.mark.parametrize("change,reason,status", [
    ("window", "outside_sending_window", "queued"),
    ("cap", "marketing_frequency_cap", "skipped"),
])
async def test_marketing_priority_retains_final_window_and_cap_rechecks(
    database, sms_transport, change, reason, status,
):
    clock = SimulatedClock()
    context = await seed_campaign(database, clock, purpose=MessagePurpose.marketing)
    queue, job = await campaign_queue(database, clock, context, priority=100)

    async def mutate(session):
        if change == "window":
            campaign = await bounded(session.get(Campaign, context["campaign_id"]))
            campaign.sending_window = closed_window(clock)
        else:
            await add_recent_marketing_contact(session, context, clock)

    await change_after_real_eligibility(database, queue, mutate)
    assert sms_transport.requests == []
    async with database() as session:
        persisted = await bounded(session.get(SmsQueueJob, job.id))
        assert (persisted.status, persisted.error_code, persisted.attempts) == (status, reason, 0)
        assert persisted.transport_started_at is None
        assert persisted.outcome_projected_at is not None
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
