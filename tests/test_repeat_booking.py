from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.models.booking import BarberService, BaseService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.models.repeat_booking import RepeatBookingEventType, RepeatBookingOffer, RepeatBookingOfferStatus
from app.services.messaging import KYIV_TZ, ProviderSendResult
from app.services.repeat_booking import RepeatBookingService


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value if isinstance(self.value, list) else []


class Session:
    def __init__(self, *values):
        self.values = list(values)
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return Result(self.values.pop(0))

    def add(self, item):
        self.added.append(item)
        if getattr(item, "id", None) is None:
            item.id = 900 + len(self.added)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    @asynccontextmanager
    async def begin_nested(self):
        yield


def completed_booking(*, service_count: int = 1) -> Booking:
    master = Master(id=7, full_name="Олег", is_active=True, show_on_master_block=True)
    services = [
        BarberService(
            id=31 + index,
            master_id=master.id,
            base_service_id=101 + index,
            name=f"Послуга {index + 1}",
            duration_minutes=30,
            price=500,
            is_active=True,
            base_service=BaseService(
                id=101 + index,
                name=f"Base {index + 1}",
                duration_minutes=30,
                price=500,
                is_active=True,
            ),
        )
        for index in range(service_count)
    ]
    master.services = services
    customer = Customer(id=4, phone="+380671112233", name="Іван", is_active=True)
    visit = datetime(2026, 7, 1, 12, tzinfo=UTC)
    booking = Booking(
        id=55,
        master_id=master.id,
        service_id=services[0].id,
        customer_id=customer.id,
        customer_name="Іван",
        customer_phone=customer.phone,
        start_at=visit - timedelta(hours=1),
        end_at=visit,
        status=BookingStatus.completed,
        completed_at=visit,
        master=master,
        service=services[0],
        customer=customer,
    )
    if service_count > 1:
        booking.service_items = [
            BookingServiceItem(service_id=item.id, position=index, service=item)
            for index, item in enumerate(services)
        ]
    return booking


class EligibilityService(RepeatBookingService):
    def __init__(self):
        super().__init__()
        self.preference = ClientCommunicationPreference(
            customer_id=4,
            telegram_chat_id="12345",
            marketing_consent=ConsentStatus.opted_in,
            transactional_consent=ConsentStatus.opted_in,
            do_not_contact=False,
            repeat_booking_opt_out=False,
        )
        self.future = False
        self.newer = False
        self.active = True
        self.capped = False

    async def _preference(self, _session, _customer_id):
        return self.preference

    async def _has_future_booking(self, _session, _customer_id, _now):
        return self.future

    async def _has_newer_completion(self, _session, **_kwargs):
        return self.newer

    async def _active_context(self, _session, master_id, service_ids):
        return SimpleNamespace(id=master_id), [SimpleNamespace(id=value) for value in service_ids], self.active

    async def _frequency_capped(self, _session, _customer_id, _at):
        return self.capped


@pytest.mark.anyio
async def test_repeat_booking_eligibility_accepts_connected_consented_client():
    booking = completed_booking(service_count=2)
    service = EligibilityService()
    assert await service.eligibility_reason(
        SimpleNamespace(), booking, service_ids=booking.service_ids, at=datetime(2026, 7, 29, 12, tzinfo=UTC)
    ) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda service: setattr(service, "future", True), "future_booking_exists"),
        (lambda service: setattr(service, "newer", True), "newer_completed_booking"),
        (lambda service: setattr(service, "active", False), "booking_context_inactive"),
        (lambda service: setattr(service, "capped", True), "frequency_cap"),
        (lambda service: setattr(service.preference, "telegram_chat_id", None), "telegram_not_connected"),
        (lambda service: setattr(service.preference, "repeat_booking_opt_out", True), "opted_out"),
        (lambda service: setattr(service.preference, "marketing_consent", ConsentStatus.opted_out), "opted_out"),
        (lambda service: setattr(service.preference, "do_not_contact", True), "opted_out"),
    ],
)
async def test_repeat_booking_eligibility_exclusion_rules(change, reason):
    service = EligibilityService()
    change(service)
    booking = completed_booking()
    assert await service.eligibility_reason(
        SimpleNamespace(), booking, service_ids=booking.service_ids, at=datetime(2026, 7, 29, 12, tzinfo=UTC)
    ) == reason


def test_repeat_booking_default_and_per_service_cadence(monkeypatch: pytest.MonkeyPatch):
    completed_at = datetime(2026, 7, 1, 7, tzinfo=UTC)  # 10:00 Europe/Kyiv
    monkeypatch.setattr(settings, "repeat_booking_delay_days", 28)
    monkeypatch.setattr(settings, "repeat_booking_service_delay_days", {31: 21, 32: 35})
    assert RepeatBookingService.cadence_days([99]) == 28
    assert RepeatBookingService.cadence_days([31, 32]) == 35
    assert RepeatBookingService.scheduled_time(completed_at, [99]) == completed_at + timedelta(days=28)


def test_repeat_booking_cadence_and_quiet_hours_use_europe_kyiv(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "repeat_booking_delay_days", 28)
    monkeypatch.setattr(settings, "repeat_booking_service_delay_days", {})
    completed_at = datetime(2026, 7, 1, 18, 30, tzinfo=UTC)  # 21:30 Kyiv, quiet
    due = RepeatBookingService.scheduled_time(completed_at, [31])
    assert due.astimezone(KYIV_TZ).strftime("%H:%M") == "10:00"
    assert due.date() == (completed_at + timedelta(days=29)).date()


def test_multi_service_snapshot_preserves_order_and_redirect_mapping():
    booking = completed_booking(service_count=2)
    assert RepeatBookingService.snapshot_service_ids(booking) == [31, 32]

    public = Master(id=8, full_name="Публічний", is_active=True, show_on_master_block=True)
    public.services = [
        BarberService(id=41, master_id=8, base_service_id=101, name="Послуга", duration_minutes=30, price=500),
        BarberService(id=42, master_id=8, base_service_id=102, name="Борода", duration_minutes=30, price=500),
    ]
    booking.redirected_from_master_id = 8
    booking.redirected_from_master = public
    assert RepeatBookingService.snapshot_service_ids(booking) == [41, 42]


def test_repeat_token_is_opaque_hash_only_and_fragment_based():
    raw = RepeatBookingService.new_token()
    digest = RepeatBookingService.hash_token(raw)
    link = RepeatBookingService.link_for_token(raw)
    assert raw not in digest and len(digest) == 64
    assert link.endswith(f"#{raw}") and "?" not in link
    for internal_value in ("customer_id", "booking_id", "master_id", "service_id", "+380"):
        assert internal_value not in link


@pytest.mark.anyio
async def test_scheduling_is_idempotent_for_the_same_completed_visit():
    existing = RepeatBookingOffer(
        id=4, completed_booking_id=55, customer_id=4, preferred_master_id=7,
        service_ids=[31], status=RepeatBookingOfferStatus.scheduled,
        scheduled_at=datetime(2026, 7, 29, 7, tzinfo=UTC),
    )
    session = Session(existing)
    returned = await EligibilityService().schedule_booking(
        session, completed_booking(), at=datetime(2026, 7, 29, 7, tzinfo=UTC)
    )
    assert returned is existing
    assert session.added == []


class CapturingTelegramProvider:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return ProviderSendResult(provider_message_id="tg-77", raw_response={"ok": True})


class FailingTelegramProvider:
    def __init__(self):
        self.calls = 0

    async def send_message(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("telegram rejected message")


class DeliveryService(EligibilityService):
    def __init__(self, provider, now):
        super().__init__()
        self.provider = provider
        self._clock = lambda: now
        self.events = []

    async def record_event(self, _session, offer, event_type, **_kwargs):
        self.events.append((offer.id, event_type))
        return True


@pytest.mark.anyio
async def test_delivery_is_telegram_only_inline_button_and_idempotent(monkeypatch: pytest.MonkeyPatch):
    now = datetime(2026, 7, 29, 7, tzinfo=UTC)
    monkeypatch.setattr(settings, "repeat_booking_quiet_hours_from", "20:00")
    monkeypatch.setattr(settings, "repeat_booking_quiet_hours_to", "10:00")
    provider = CapturingTelegramProvider()
    service = DeliveryService(provider, now)
    booking = completed_booking()
    offer = RepeatBookingOffer(
        id=12,
        completed_booking_id=booking.id,
        customer_id=booking.customer_id,
        preferred_master_id=booking.master_id,
        service_ids=booking.service_ids,
        status=RepeatBookingOfferStatus.scheduled,
        scheduled_at=now,
    )
    async def active(_session, _master_id, _service_ids):
        return booking.master, booking.services, True

    service._active_context = active  # type: ignore[method-assign]
    session = Session(booking)
    assert await service.send_offer(session, offer, at=now)
    assert offer.status == RepeatBookingOfferStatus.sent
    assert offer.provider_message_id == "tg-77"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["destination"] == "12345"
    assert call["reply_markup"]["inline_keyboard"][0][0]["text"] == "Записатися знову"
    assert "inline_keyboard" in call["reply_markup"]
    assert await service.send_offer(session, offer, at=now) is False
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_delivery_defers_during_kyiv_quiet_hours(monkeypatch: pytest.MonkeyPatch):
    now = datetime(2026, 7, 29, 18, tzinfo=UTC)  # 21:00 Kyiv
    monkeypatch.setattr(settings, "repeat_booking_quiet_hours_from", "20:00")
    monkeypatch.setattr(settings, "repeat_booking_quiet_hours_to", "10:00")
    provider = CapturingTelegramProvider()
    service = DeliveryService(provider, now)
    offer = RepeatBookingOffer(
        id=12, completed_booking_id=55, customer_id=4, preferred_master_id=7,
        service_ids=[31], status=RepeatBookingOfferStatus.scheduled,
        scheduled_at=now - timedelta(hours=1),
    )
    assert await service.send_offer(Session(), offer, at=now) is False
    assert offer.scheduled_at > now
    assert provider.calls == []


@pytest.mark.anyio
async def test_telegram_failure_never_falls_back_to_sms(monkeypatch: pytest.MonkeyPatch):
    now = datetime(2026, 7, 29, 7, tzinfo=UTC)
    monkeypatch.setattr(settings, "messaging_max_retry_attempts", 1)
    provider = FailingTelegramProvider()
    service = DeliveryService(provider, now)
    booking = completed_booking()

    async def active(_session, _master_id, _service_ids):
        return booking.master, booking.services, True

    service._active_context = active  # type: ignore[method-assign]
    offer = RepeatBookingOffer(
        id=13, completed_booking_id=booking.id, customer_id=booking.customer_id,
        preferred_master_id=booking.master_id, service_ids=booking.service_ids,
        status=RepeatBookingOfferStatus.scheduled, scheduled_at=now,
    )
    assert await service.send_offer(Session(booking), offer, at=now) is False
    assert provider.calls == 1
    assert offer.status == RepeatBookingOfferStatus.failed
    assert offer.token_hash is None
    assert RepeatBookingEventType.offer_delivery_failed in [event for _, event in service.events]


class TokenService(RepeatBookingService):
    def __init__(self, now):
        super().__init__(now=lambda: now)

    async def _has_newer_completion(self, _session, **_kwargs):
        return False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state,expired,revoked",
    [
        (RepeatBookingOfferStatus.sent, True, False),
        (RepeatBookingOfferStatus.sent, False, True),
        (RepeatBookingOfferStatus.booked, False, False),
    ],
)
async def test_expired_revoked_and_used_repeat_tokens_are_rejected(state, expired, revoked):
    now = datetime(2026, 8, 1, tzinfo=UTC)
    raw = "x" * 43
    source = completed_booking()
    offer = RepeatBookingOffer(
        id=1, completed_booking_id=source.id, customer_id=source.customer_id,
        preferred_master_id=source.master_id, service_ids=source.service_ids,
        token_hash=RepeatBookingService.hash_token(raw), status=state,
        scheduled_at=now - timedelta(days=1),
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(days=1),
        revoked_at=now if revoked else None,
        completed_booking=source,
    )
    session = Session(offer)
    with pytest.raises(HTTPException, match="Invalid or expired"):
        await TokenService(now)._valid_offer(session, raw)
    # Token validation used inside booking creation must never commit the
    # half-created booking when an invalid capability is rejected.
    assert session.commits == 0


class ContextService(TokenService):
    def __init__(self, now, offer, master, services, active):
        super().__init__(now)
        self.offer = offer
        self.master = master
        self.services = services
        self.active = active
        self.events = []

    async def _valid_offer(self, _session, _token, **_kwargs):
        return self.offer

    async def _active_context(self, _session, _master_id, _service_ids):
        return self.master, self.services, self.active

    async def record_event(self, _session, _offer, event_type, **_kwargs):
        self.events.append(event_type)
        return True


@pytest.mark.anyio
async def test_context_prefills_exact_multi_service_or_returns_safe_fallback():
    now = datetime(2026, 8, 1, tzinfo=UTC)
    booking = completed_booking(service_count=2)
    offer = RepeatBookingOffer(
        id=2, completed_booking_id=booking.id, customer_id=booking.customer_id,
        preferred_master_id=booking.master_id, service_ids=booking.service_ids,
        status=RepeatBookingOfferStatus.sent, scheduled_at=now,
        expires_at=now + timedelta(days=30), completed_booking=booking,
    )
    session = Session()
    service = ContextService(now, offer, booking.master, booking.services, True)
    context = await service.context(session, "x" * 43)
    payload = context.model_dump()
    assert payload["preferred_master"]["id"] == 7
    assert [item["id"] for item in payload["services"]] == [31, 32]
    assert payload["can_prefill"] is True and payload["fallback_required"] is False
    assert "customer_id" not in payload and "completed_booking_id" not in payload
    assert RepeatBookingEventType.link_opened in service.events

    fallback = ContextService(now, offer, booking.master, booking.services, False)
    fallback_payload = await fallback.context(Session(), "x" * 43)
    assert fallback_payload.can_prefill is False
    assert fallback_payload.fallback_required is True


@pytest.mark.anyio
async def test_booking_attribution_consumes_token_without_creating_booking_and_completion_is_separate():
    now = datetime(2026, 8, 1, tzinfo=UTC)
    source = completed_booking(service_count=2)
    offer = RepeatBookingOffer(
        id=3, completed_booking_id=source.id, customer_id=source.customer_id,
        preferred_master_id=source.master_id, service_ids=source.service_ids,
        token_hash="h" * 64, status=RepeatBookingOfferStatus.started,
        scheduled_at=now, expires_at=now + timedelta(days=1), completed_booking=source,
    )
    result = Booking(
        id=99, master_id=7, service_id=31, customer_id=4, customer_name="Іван",
        customer_phone="+380671112233", start_at=now + timedelta(days=1),
        end_at=now + timedelta(days=1, hours=1), status=BookingStatus.confirmed,
    )
    service = ContextService(now, offer, source.master, source.services, True)
    attributed = await service.attribute_booking(
        Session(), token="x" * 43, booking=result,
        requested_master_id=7, requested_service_ids=[31, 32],
    )
    assert attributed.status == RepeatBookingOfferStatus.booked
    assert attributed.result_booking_id == 99
    assert attributed.token_hash is None and attributed.revoked_at == now
    assert RepeatBookingEventType.booking_completed not in service.events

    result.status = BookingStatus.completed
    completion_session = Session(offer)
    assert await service.mark_repeat_visit_completed(completion_session, result)
    assert RepeatBookingEventType.booking_completed in service.events


@pytest.mark.anyio
async def test_active_offer_rejects_changed_master_or_service_selection():
    now = datetime(2026, 8, 1, tzinfo=UTC)
    source = completed_booking(service_count=2)
    offer = RepeatBookingOffer(
        id=3, completed_booking_id=source.id, customer_id=source.customer_id,
        preferred_master_id=7, service_ids=[31, 32], token_hash="h" * 64,
        status=RepeatBookingOfferStatus.opened, scheduled_at=now,
        expires_at=now + timedelta(days=1), completed_booking=source,
    )
    result = Booking(
        id=99, master_id=7, service_id=31, customer_id=4, customer_name="Іван",
        customer_phone="+380671112233", start_at=now + timedelta(days=1),
        end_at=now + timedelta(days=1, hours=1), status=BookingStatus.confirmed,
    )
    service = ContextService(now, offer, source.master, source.services, True)
    with pytest.raises(HTTPException, match="no longer matches"):
        await service.attribute_booking(
            Session(), token="x" * 43, booking=result,
            requested_master_id=7, requested_service_ids=[31],
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "date_from,date_to",
    [
        (date(2026, 8, 2), date(2026, 8, 1)),
        (date(2025, 8, 1), date(2026, 8, 3)),
    ],
)
async def test_repeat_booking_analytics_rejects_invalid_ranges(date_from, date_to):
    with pytest.raises(HTTPException) as exc_info:
        await RepeatBookingService().analytics(
            SimpleNamespace(),
            date_from=date_from,
            date_to=date_to,
        )

    assert exc_info.value.status_code == 422
