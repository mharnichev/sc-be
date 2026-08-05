import asyncio
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.v1.routes.bookings import schedule_waitlist_offer
from app.models.booking import BarberService, Booking, Master, MasterPosition
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.schemas.waitlist import PublicWaitlistOfferClaim
from app.services.waitlist_offers import FreedBookingSlot, WaitlistOfferService
from app.services.booking_recovery_analytics import booking_recovery_analytics_service
from app.services.booking import BookingServiceLayer


def _request(**changes):
    values = {
        "cancel_token_hash": "x" * 64,
        "customer_id": 1,
        "desired_date": date(2026, 8, 10),
        "duration_minutes": 30,
        "notification_consent": True,
        "expires_at": datetime.now(UTC) + timedelta(days=1),
    }
    values.update(changes)
    return WaitlistRequest(**values)


def test_waitlist_offer_tokens_are_hashed_and_not_reversible():
    service = WaitlistOfferService()
    assert service.hold_minutes == 10
    token = "one-time-public-token"
    assert service._hash(token) != token
    assert service._hash(token) == service._hash(token)
    assert service._hash(token) != service._hash("another-token")


def test_waitlist_offer_link_keeps_token_out_of_query_and_claim_path():
    service = WaitlistOfferService()
    token = "a" * 40
    link = service._booking_link(token, "https://soulcuts.com.ua/booking/waitlist-offer/")
    assert link == f"https://soulcuts.com.ua/booking/waitlist-offer#{token}"
    assert "?" not in link
    assert PublicWaitlistOfferClaim(token=token).token == token


def test_waitlist_time_preference_is_inclusive_in_kyiv_time():
    service = WaitlistOfferService()
    request = _request(preferred_time_from=time(10), preferred_time_to=time(11))
    assert service._in_time_preference(request, datetime(2026, 8, 10, 7, tzinfo=UTC))
    assert not service._in_time_preference(request, datetime(2026, 8, 10, 6, 59, tzinfo=UTC))


def test_waitlist_date_range_rejects_slot_outside_requested_range():
    service = WaitlistOfferService()
    request = _request(acceptable_date_from=date(2026, 8, 10), acceptable_date_to=date(2026, 8, 12))
    assert service._in_date_range(request, datetime(2026, 8, 11, 9, tzinfo=UTC))
    assert not service._in_date_range(request, datetime(2026, 8, 13, 9, tzinfo=UTC))


def test_waitlist_without_explicit_range_matches_only_desired_date():
    service = WaitlistOfferService()
    request = _request(desired_date=date(2026, 8, 10))
    assert service._in_date_range(request, datetime(2026, 8, 10, 9, tzinfo=UTC))
    assert not service._in_date_range(request, datetime(2026, 8, 11, 9, tzinfo=UTC))


@pytest.mark.anyio
async def test_waitlist_offer_requires_the_full_advertised_hold_window():
    service = WaitlistOfferService(hold_minutes=10)
    now = datetime(2099, 1, 2, 10, tzinfo=UTC)
    service._now = lambda: now

    result = await service.offer_slot(
        SimpleNamespace(),
        master_id=2,
        start_at=now + timedelta(minutes=9),
        end_at=now + timedelta(minutes=39),
    )

    assert result is None


def test_waitlist_offer_respects_explicit_consent_and_transactional_opt_out():
    service = WaitlistOfferService()
    nonconsented = _request(notification_consent=False)
    assert not service._communication_allowed(nonconsented, None)

    consented = _request(notification_consent=True)
    preference = ClientCommunicationPreference(
        customer_id=1,
        marketing_consent=ConsentStatus.opted_in,
        transactional_consent=ConsentStatus.opted_out,
        do_not_contact=False,
    )
    assert not service._communication_allowed(consented, preference)
    preference.transactional_consent = ConsentStatus.opted_in
    assert service._communication_allowed(consented, preference)


def test_waitlist_matching_maps_every_service_to_another_suitable_master():
    service = WaitlistOfferService()
    source = [
        BarberService(id=10, master_id=1, base_service_id=100, name="Стрижка", duration_minutes=30, price=500, is_active=True),
        BarberService(id=11, master_id=1, base_service_id=101, name="Борода", duration_minutes=30, price=400, is_active=True),
    ]
    target = Master(
        id=2,
        full_name="Петро",
        position=MasterPosition.master,
        is_active=True,
        show_on_master_block=True,
        services=[
            BarberService(id=20, master_id=2, base_service_id=100, name="Стрижка", duration_minutes=30, price=550, is_active=True),
            BarberService(id=21, master_id=2, base_service_id=101, name="Борода", duration_minutes=30, price=450, is_active=True),
        ],
    )
    matched = service._matching_services(target, source)
    assert [item.id for item in matched or []] == [20, 21]
    target.services.pop()
    assert service._matching_services(target, source) is None


@pytest.mark.anyio
async def test_expired_hold_is_offered_to_the_next_candidate():
    now = datetime(2099, 1, 2, 10, tzinfo=UTC)
    expired = WaitlistOffer(
        id=4,
        request_id=1,
        master_id=2,
        start_at=now + timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        token_hash="x" * 64,
        status=WaitlistOfferStatus.expired,
        scheduled_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=1),
        source_booking_id=9,
    )
    service = WaitlistOfferService()
    calls = []

    async def expire_holds(_session):
        return [expired]

    async def offer_slot(_session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=5)

    service.expire_holds = expire_holds
    service.offer_slot = offer_slot
    session = SimpleNamespace(commit=lambda: None)

    async def commit():
        return None

    session.commit = commit
    assert await service.expire_and_offer_next(session) == 1
    assert calls == [
        {
            "master_id": 2,
            "start_at": expired.start_at,
            "end_at": expired.end_at,
            "source_booking_id": 9,
        }
    ]


def test_cancellation_hook_schedules_waitlist_matching():
    tasks = BackgroundTasks()
    slot = FreedBookingSlot(
        master_id=2,
        start_at=datetime(2099, 1, 2, 10, tzinfo=UTC),
        end_at=datetime(2099, 1, 2, 11, tzinfo=UTC),
        source_booking_id=7,
    )
    schedule_waitlist_offer(tasks, slot)
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].args == (slot,)


class SequenceScalars:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value


class SequenceExecuteSession:
    def __init__(self, values):
        self.values = list(values)

    async def execute(self, _statement):
        return SequenceScalars(self.values.pop(0))


@pytest.mark.anyio
async def test_active_waitlist_hold_is_busy_for_normal_booking_availability():
    now = datetime.now(UTC)
    hold = WaitlistOffer(
        id=9,
        request_id=1,
        master_id=2,
        start_at=now + timedelta(hours=2),
        end_at=now + timedelta(hours=3),
        token_hash="h" * 64,
        status=WaitlistOfferStatus.delivered,
        scheduled_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    busy = await BookingServiceLayer().list_busy_bookings(
        SequenceExecuteSession([[], [hold]]),
        master_id=2,
        start_at=hold.start_at,
        end_at=hold.end_at,
    )
    assert busy == [hold]


class ClaimResult:
    def __init__(self, offer):
        self.offer = offer

    def scalar_one_or_none(self):
        return self.offer


class ConcurrentClaimState:
    def __init__(self):
        self.master_lock = asyncio.Lock()
        self.bookings: list[Booking] = []


class ClaimSession:
    def __init__(self, state: ConcurrentClaimState, offer: WaitlistOffer):
        self.state = state
        self.offer = offer
        self.master_locked = False
        self.pending_booking = None

    async def execute(self, _statement):
        return ClaimResult(self.offer)

    def add(self, item):
        if isinstance(item, Booking):
            self.pending_booking = item

    async def flush(self):
        if self.pending_booking is not None and self.pending_booking not in self.state.bookings:
            self.pending_booking.id = len(self.state.bookings) + 1
            self.state.bookings.append(self.pending_booking)

    async def commit(self):
        if self.master_locked:
            self.master_locked = False
            self.state.master_lock.release()

    async def refresh(self, _item):
        return None

    async def get(self, _model, _id, **_kwargs):
        return None


def claim_fixture(service: WaitlistOfferService, *, offer_id: int, customer_id: int, token: str):
    now = service._now()
    customer = Customer(
        id=customer_id,
        phone=f"+3806700000{customer_id:02d}",
        name=f"Клієнт {customer_id}",
        is_active=True,
    )
    source_service = BarberService(
        id=10,
        master_id=2,
        base_service_id=100,
        name="Стрижка",
        duration_minutes=60,
        price=500,
        is_active=True,
    )
    request = WaitlistRequest(
        id=offer_id,
        public_id=f"public-{offer_id}",
        cancel_token_hash=f"cancel-{offer_id}".ljust(64, "x"),
        dedup_key_hash=f"dedup-{offer_id}".ljust(64, "x"),
        customer_id=customer_id,
        desired_date=now.date(),
        acceptable_date_from=now.date(),
        acceptable_date_to=now.date(),
        duration_minutes=60,
        notification_consent=True,
        status=WaitlistStatus.offered,
        expires_at=now + timedelta(days=1),
        services=[source_service],
        customer=customer,
    )
    offer = WaitlistOffer(
        id=offer_id,
        request_id=request.id,
        master_id=2,
        start_at=now + timedelta(hours=2),
        end_at=now + timedelta(hours=3),
        token_hash=service._hash(token),
        status=WaitlistOfferStatus.delivered,
        scheduled_at=now,
        expires_at=now + timedelta(minutes=10),
        request=request,
    )
    return offer


@pytest.mark.anyio
async def test_waitlist_offer_cannot_be_claimed_after_the_slot_started():
    service = WaitlistOfferService()
    token = "z" * 40
    offer = claim_fixture(service, offer_id=3, customer_id=3, token=token)
    offer.start_at = service._now() - timedelta(minutes=1)
    offer.end_at = service._now() + timedelta(minutes=59)

    with pytest.raises(HTTPException) as exc_info:
        await service.claim(ClaimSession(ConcurrentClaimState(), offer), token)

    assert exc_info.value.status_code == 410
    assert offer.status is WaitlistOfferStatus.expired


@pytest.mark.anyio
async def test_two_clients_cannot_claim_the_same_slot_and_token_is_one_time(monkeypatch):
    async def no_analytics(*_args, **_kwargs):
        return True

    monkeypatch.setattr(booking_recovery_analytics_service, "record", no_analytics)
    state = ConcurrentClaimState()
    service = WaitlistOfferService()
    token_a = "a" * 40
    token_b = "b" * 40
    offer_a = claim_fixture(service, offer_id=1, customer_id=1, token=token_a)
    offer_b = claim_fixture(service, offer_id=2, customer_id=2, token=token_b)
    # Simulate a legacy/racing pair of offers for the same interval. The master
    # row lock plus availability recheck must still permit only one booking.
    offer_b.start_at = offer_a.start_at
    offer_b.end_at = offer_a.end_at
    master = Master(
        id=2,
        full_name="Майстер",
        position=MasterPosition.master,
        is_active=True,
        show_on_master_block=True,
        services=[
            BarberService(
                id=20,
                master_id=2,
                base_service_id=100,
                name="Стрижка",
                duration_minutes=60,
                price=550,
                is_active=True,
            )
        ],
    )

    async def lock_master(session, _master_id, *, for_update=False):
        assert for_update
        await state.master_lock.acquire()
        session.master_locked = True
        return master

    async def within(*_args, **_kwargs):
        return None

    async def available(_session, *_args, **_kwargs):
        if state.bookings:
            raise HTTPException(status_code=409, detail="occupied")

    async def apply_promotion(*_args, **_kwargs):
        return None

    service.booking_service.get_active_master_with_services = lock_master
    service.booking_service.ensure_booking_within_availability = within
    service.booking_service.ensure_slot_available = available
    service.booking_service.promotion_service.apply_to_booking = apply_promotion
    sessions = [ClaimSession(state, offer_a), ClaimSession(state, offer_b)]

    results = await asyncio.gather(
        service.claim(sessions[0], token_a),
        service.claim(sessions[1], token_b),
        return_exceptions=True,
    )

    assert len(state.bookings) == 1
    assert sum(isinstance(item, Booking) for item in results) == 1
    assert sum(isinstance(item, HTTPException) for item in results) == 1
    assert {offer_a.status, offer_b.status} == {
        WaitlistOfferStatus.claimed,
        WaitlistOfferStatus.cancelled,
    }

    with pytest.raises(HTTPException) as exc_info:
        await service.claim(ClaimSession(state, offer_a), token_a)
    assert exc_info.value.status_code == 410
