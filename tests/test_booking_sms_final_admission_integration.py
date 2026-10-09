"""Generic booking SMS final preference checks on a durable isolated PG queue."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.models.sms_queue import SmsQueueJob
from app.services.sms_queue import SmsDispatchDecision, SmsQueueService
from tests.test_inventory_integration import anyio_backend, database
from tests.test_sms_business_final_admission_integration import change_after_real_eligibility
from tests.test_sms_queue_integration import SimulatedClock, sms_transport

pytestmark = pytest.mark.anyio
PHONE = "+380501234567"


async def seed_contact(database, *, alias=False, active=True, marketing_opt_out=False, preferences=True):
    async with database() as session:
        customer = Customer(phone="+0501234567" if alias else PHONE, name="Booking SMS owner", is_active=active)
        session.add(customer)
        await session.flush()
        if preferences:
            session.add(ClientCommunicationPreference(
                customer_id=customer.id, do_not_contact=False,
                transactional_consent=ConsentStatus.opted_in,
                marketing_consent=ConsentStatus.opted_out if marketing_opt_out else ConsentStatus.opted_in,
            ))
        await session.commit()
        return customer.id


async def queued_booking_sms(database, clock, *, prefix, customer_id=None):
    # This callback is only the asynchronous pre-admission interception point.
    # Generic notifications must consult current preferences again at final admission.
    async def allow(_job):
        return SmsDispatchDecision()
    queue = SmsQueueService(session_factory=database, clock=clock, before_dispatch=allow)
    key = f"{prefix}:native-final-admission"
    context = {"idempotency_key": key}
    if customer_id is not None:
        context["customer_id"] = customer_id
    job = await queue.enqueue(
        "send", {"phone": [PHONE.lstrip("+")], "message": "Synthetic booking notification", "src_addr": "Soul Cuts"},
        priority=10, idempotency_key=key, context=context,
        expires_at=clock.value+timedelta(hours=1),
    )
    return queue, job


@pytest.mark.parametrize("prefix", ["booking-confirmation", "booking-reminder"])
@pytest.mark.parametrize("restriction,reason", [
    ("dnc", "Client is marked do-not-contact"),
    ("transactional_opt_out", "Client opted out of transactional messages"),
])
@pytest.mark.parametrize("alias", [False, True], ids=["customer-context", "legacy-alias-by-phone"])
async def test_generic_booking_queue_rechecks_committed_restriction_at_admission(
    database, sms_transport, prefix, restriction, reason, alias,
):
    clock = SimulatedClock()
    owner_id = await seed_contact(database, alias=alias)
    queue, job = await queued_booking_sms(database, clock, prefix=prefix, customer_id=None if alias else owner_id)
    async def restrict(session):
        preference = (await session.scalars(select(ClientCommunicationPreference).where(
            ClientCommunicationPreference.customer_id == owner_id))).one()
        if restriction == "dnc":
            preference.do_not_contact = True
        else:
            preference.transactional_consent = ConsentStatus.opted_out
    await change_after_real_eligibility(database, queue, restrict)
    assert sms_transport.requests == []
    async with database() as session:
        persisted = await session.get(SmsQueueJob, job.id)
        assert (persisted.status, persisted.error_code, persisted.attempts) == ("skipped", reason, 0)
        assert persisted.transport_started_at is None
        assert persisted.provider_message_id is None and persisted.lease_token is None
        assert persisted.outcome_projected_at is not None
    clock.advance(3600)
    await queue.process_one()
    assert sms_transport.requests == []


@pytest.mark.parametrize("prefix", ["booking-confirmation", "booking-reminder"])
@pytest.mark.parametrize("mode", ["guest", "known-without-preferences", "active-marketing-optout", "inactive-marketing-optout"])
async def test_generic_booking_notifications_allow_guests_missing_preferences_and_marketing_optout(
    database, sms_transport, prefix, mode,
):
    clock = SimulatedClock()
    owner_id = None
    if mode != "guest":
        owner_id = await seed_contact(database, active=mode != "inactive-marketing-optout",
                                      marketing_opt_out="marketing-optout" in mode,
                                      preferences=mode != "known-without-preferences")
    queue, job = await queued_booking_sms(database, clock, prefix=prefix, customer_id=owner_id)
    assert await queue.process_one()
    assert len(sms_transport.requests) == 1
    assert sms_transport.requests[0]["payload"]["phone"] == [PHONE.lstrip("+")]
    async with database() as session:
        persisted = await session.get(SmsQueueJob, job.id)
        assert (persisted.status, persisted.attempts) == ("accepted", 1)
        assert persisted.transport_started_at == clock.value
        assert persisted.provider_message_id is not None
    clock.advance(3600)
    await queue.process_one()
    assert len(sms_transport.requests) == 1
