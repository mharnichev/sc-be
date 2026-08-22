from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, Response

from app.api.v1.routes import customer_activity as activity_routes
from app.api.v1.routes import waitlist as waitlist_routes
from app.models.booking import BarberService, Booking, BookingStatus, Master
from app.models.customer import Customer
from app.models.customer_activity import CustomerActivityAccessToken
from app.models.messaging import Campaign, MessageDeliveryStatus, MessageLog, MessageRecipient
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus, WaitlistRequest, WaitlistStatus
from app.schemas.customer_activity import CustomerActivityBooking
from app.schemas.messaging import MessageLogResponse, MessageRecipientResponse
from app.services.customer_activity import (
    BROWSER_SESSION_COOKIE_NAME,
    BROWSER_SESSION_COOKIE_PATH,
    BROWSER_SESSION_SOURCE,
    CustomerActivityService,
    customer_activity_service,
)
from app.services.customer_activity_notifications import CustomerActivityNotificationService
from app.services.messaging import MessagingService
from app.services.waitlist_offers import FreedBookingSlot, WaitlistOfferService, booking_recovery_analytics_service
from app.services.sms import SmsDeliveryStatus


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value if isinstance(self.value, list) else []

    def __iter__(self):
        return iter(self.all())


class TokenSession:
    def __init__(self, access, customer):
        self.access = access
        self.customer = customer
        self.commits = 0

    async def execute(self, _stmt):
        return Result(self.access)

    async def get(self, _model, _id):
        return self.customer

    async def commit(self):
        self.commits += 1


def _customer() -> Customer:
    return Customer(id=4, phone="+380671112233", name="Іван", is_active=True)


def test_activity_capability_is_opaque_hash_only_and_fragment_only() -> None:
    service = CustomerActivityService()
    raw = "x" * 43
    assert service._hash_token(raw) != raw
    assert len(service._hash_token(raw)) == 64
    manage, cancel = service.urls_for_token(raw)
    assert "?" not in manage and manage.endswith(f"#{raw}")
    assert "?" not in cancel and cancel.endswith(f"#{raw}")


def test_customer_activity_responses_are_private_and_never_cached() -> None:
    response = Response()
    activity_routes.prevent_customer_activity_caching(response)
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == "X-Customer-Activity-Token, Cookie"
    error = activity_routes.private_activity_error(
        HTTPException(status_code=401, detail="invalid")
    )
    assert error.headers is not None
    assert error.headers["Cache-Control"] == "no-store, private"
    assert error.headers["Vary"] == "X-Customer-Activity-Token, Cookie"


@pytest.mark.anyio
async def test_browser_session_is_hash_only_exactly_30_days_and_does_not_revoke_sms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self):
            self.added = []
            self.executed = []

        async def execute(self, statement):
            self.executed.append(statement)
            return Result(None)

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            return None

    from app.core.config import settings

    monkeypatch.setattr(settings, "customer_activity_browser_session_ttl_days", 30)
    session = Session()
    before = datetime.now(UTC)
    raw_token, expires_at = await CustomerActivityService().create_browser_session(
        session,
        4,
        source_booking_id=99,
    )
    after = datetime.now(UTC)

    access = session.added[0]
    assert session.executed == []
    assert access.source == BROWSER_SESSION_SOURCE
    assert access.customer_id == 4
    assert access.source_booking_id == 99
    assert access.recipient_id is None
    assert access.token_hash == CustomerActivityService._hash_token(raw_token)
    assert raw_token != access.token_hash
    assert raw_token not in repr(vars(access))
    assert before + timedelta(days=30) <= expires_at <= after + timedelta(days=30)
    assert access.expires_at == expires_at


def test_browser_cookie_is_host_only_httponly_secure_and_narrowly_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "customer_activity_browser_session_ttl_days", 30)
    raw_token = "t" * 43
    response = Response()
    expires_at = datetime.now(UTC) + timedelta(days=30)
    activity_routes.set_browser_session_cookie(
        response,
        raw_token,
        expires_at=expires_at,
    )

    header = response.headers["set-cookie"]
    parsed = SimpleCookie()
    parsed.load(header)
    cookie = parsed[BROWSER_SESSION_COOKIE_NAME]
    assert cookie.value == raw_token
    assert cookie["path"] == BROWSER_SESSION_COOKIE_PATH
    assert cookie["max-age"] == str(30 * 24 * 60 * 60)
    assert parsedate_to_datetime(cookie["expires"]) == expires_at.replace(microsecond=0)
    assert cookie["httponly"]
    assert cookie["secure"]
    assert cookie["samesite"].lower() == "lax"
    assert not cookie["domain"]
    assert "Domain=" not in header
    assert "+380" not in header and "Іван" not in header and "booking_id" not in header


@pytest.mark.anyio
async def test_activity_auth_accepts_cookie_and_preserves_header_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    async def customer_for_token(_session, token):
        seen.append(token)
        return _customer()

    monkeypatch.setattr(customer_activity_service, "customer_for_token", customer_for_token)
    session = SimpleNamespace()
    cookie_token = "c" * 43
    header_token = "h" * 43

    customer = await activity_routes.get_activity_customer(None, cookie_token, session)
    assert customer.id == 4
    await activity_routes.get_activity_customer(header_token, cookie_token, session)
    assert seen == [cookie_token, header_token]


@pytest.mark.anyio
async def test_activity_auth_rejects_missing_and_expired_cookie() -> None:
    with pytest.raises(HTTPException) as missing:
        await activity_routes.get_activity_customer(None, None, SimpleNamespace())
    assert missing.value.status_code == 401
    assert missing.value.headers["Vary"] == "X-Customer-Activity-Token, Cookie"

    raw_token = "x" * 43
    expired = CustomerActivityAccessToken(
        id=1,
        token_hash=CustomerActivityService._hash_token(raw_token),
        customer_id=4,
        source=BROWSER_SESSION_SOURCE,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(HTTPException) as invalid:
        await activity_routes.get_activity_customer(
            None,
            raw_token,
            TokenSession(expired, _customer()),
        )
    assert invalid.value.status_code == 401
    assert invalid.value.headers["Cache-Control"] == "no-store, private"


@pytest.mark.anyio
async def test_forget_browser_session_revokes_cookie_only_clears_it_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoked = []

    async def revoke(_session, token):
        revoked.append(token)

    monkeypatch.setattr(customer_activity_service, "revoke_browser_session", revoke)
    raw_token = "b" * 43
    first_response = Response()
    first = await activity_routes.forget_customer_activity_browser_session(
        first_response,
        raw_token,
        SimpleNamespace(),
    )
    second_response = Response()
    second = await activity_routes.forget_customer_activity_browser_session(
        second_response,
        None,
        SimpleNamespace(),
    )

    assert first.success is True and second.success is True
    assert revoked == [raw_token]
    for response in (first_response, second_response):
        header = response.headers["set-cookie"]
        assert f"{BROWSER_SESSION_COOKIE_NAME}=\"\"" in header
        assert "Max-Age=0" in header
        assert f"Path={BROWSER_SESSION_COOKIE_PATH}" in header
        assert response.headers["cache-control"] == "no-store, private"


@pytest.mark.anyio
async def test_browser_revocation_is_hash_scoped_and_cannot_revoke_sms_source() -> None:
    class Session:
        def __init__(self):
            self.statement = None
            self.commits = 0

        async def execute(self, statement):
            self.statement = statement
            return Result(None)

        async def commit(self):
            self.commits += 1

    raw_token = "r" * 43
    session = Session()
    await CustomerActivityService().revoke_browser_session(session, raw_token)
    compiled = session.statement.compile()
    statement = str(compiled)

    assert session.commits == 1
    assert "customer_activity_access_tokens.token_hash" in statement
    assert "customer_activity_access_tokens.source" in statement
    assert CustomerActivityService._hash_token(raw_token) in compiled.params.values()
    assert BROWSER_SESSION_SOURCE in compiled.params.values()
    assert raw_token not in statement


@pytest.mark.anyio
@pytest.mark.parametrize("expired,revoked", [(True, False), (False, True)])
async def test_activity_capability_rejects_expired_or_revoked_token(expired: bool, revoked: bool) -> None:
    now = datetime.now(UTC)
    access = CustomerActivityAccessToken(
        id=1,
        token_hash=CustomerActivityService._hash_token("x" * 43),
        customer_id=4,
        source="test",
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(days=1),
        revoked_at=now if revoked else None,
    )
    with pytest.raises(HTTPException, match="Invalid or expired"):
        await CustomerActivityService().customer_for_token(TokenSession(access, _customer()), "x" * 43)


@pytest.mark.anyio
async def test_retry_token_revokes_prior_recipient_capability_and_caps_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        def __init__(self):
            self.executed = 0
            self.statements = []
            self.added = []

        async def execute(self, statement):
            self.executed += 1
            self.statements.append(statement)
            return Result(None)

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            return None

    from app.core.config import settings
    monkeypatch.setattr(settings, "customer_activity_token_max_days", 1)
    session = Session()
    await CustomerActivityService().create_access_token(
        session, 4, source="booking_confirmation", recipient_id=11,
        expires_at=datetime.now(UTC) + timedelta(days=365),
    )
    access = session.added[0]
    assert session.executed == 1  # revoke prior active token for this recipient
    compiled = session.statements[0].compile()
    assert "customer_activity_access_tokens.recipient_id" in str(compiled)
    assert 11 in compiled.params.values()
    assert access.recipient_id == 11
    assert access.expires_at <= datetime.now(UTC) + timedelta(days=1, seconds=1)


def test_activity_booking_dto_has_no_customer_phone_or_numeric_id() -> None:
    public_master = Master(id=3, full_name="Глеб Аноцький")
    booking = Booking(
        id=99,
        public_id="7b3d54e6-4d17-420f-bd4f-941cf9fe0442",
        master_id=1,
        service_id=2,
        customer_id=4,
        customer_name="Іван",
        customer_phone="+380671112233",
        start_at=datetime.now(UTC) + timedelta(days=1),
        end_at=datetime.now(UTC) + timedelta(days=1, minutes=30),
        status=BookingStatus.confirmed,
        master=Master(id=1, full_name="Технічний календар", show_on_master_block=False),
        redirected_from_master_id=3,
        redirected_from_master=public_master,
        service=BarberService(id=2, master_id=1, name="Стрижка", duration_minutes=30, price=500),
    )
    dto = CustomerActivityService._booking(booking)
    assert isinstance(dto, CustomerActivityBooking)
    assert dto.public_id == booking.public_id
    assert dto.master_name == "Глеб Аноцький"
    assert "customer_id" not in dto.model_dump()
    assert "customer_phone" not in dto.model_dump()
    assert 99 not in dto.model_dump().values()


@pytest.mark.anyio
async def test_customer_cancel_rejects_missing_past_and_non_active_booking() -> None:
    service = CustomerActivityService()
    customer = _customer()

    class Session:
        def __init__(self, booking):
            self.booking = booking

        async def execute(self, _stmt):
            return Result(self.booking)

    with pytest.raises(HTTPException, match="not found"):
        await service.cancel_booking(Session(None), customer, "missing")
    past = Booking(
        public_id="past", master_id=1, service_id=1, customer_id=customer.id,
        customer_name="Іван", customer_phone=customer.phone,
        start_at=datetime.now(UTC) - timedelta(minutes=1), end_at=datetime.now(UTC), status=BookingStatus.confirmed,
    )
    with pytest.raises(HTTPException, match="Past bookings"):
        await service.cancel_booking(Session(past), customer, "past")
    inactive = Booking(
        public_id="inactive", master_id=1, service_id=1, customer_id=customer.id,
        customer_name="Іван", customer_phone=customer.phone,
        start_at=datetime.now(UTC) + timedelta(days=1), end_at=datetime.now(UTC) + timedelta(days=1, minutes=30),
        status=BookingStatus.cancelled,
    )
    with pytest.raises(HTTPException, match="no longer active"):
        await service.cancel_booking(Session(inactive), customer, "inactive")


@pytest.mark.anyio
async def test_booking_cancellation_query_keeps_resolved_customer_ownership_check() -> None:
    class Session:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result(None)

    session = Session()
    with pytest.raises(HTTPException, match="Booking not found"):
        await CustomerActivityService().cancel_booking(session, _customer(), "public-booking-id")

    compiled = session.statement.compile()
    statement = str(compiled)
    assert "bookings.public_id" in statement
    assert "bookings.customer_id" in statement
    assert "public-booking-id" in compiled.params.values()
    assert _customer().id in compiled.params.values()


@pytest.mark.anyio
async def test_customer_booking_cancellation_schedules_master_telegram_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_at = datetime.now(UTC) + timedelta(days=1)
    booking = SimpleNamespace(
        id=11,
        public_id="public-booking-id",
        status=BookingStatus.cancelled,
        cancelled_at=datetime.now(UTC),
        customer_name="Іван",
        start_at=start_at,
        master=SimpleNamespace(telegram_chat_id="111"),
        services=[SimpleNamespace(title_uk="Стрижка", name="Haircut")],
        service=None,
    )
    slot = FreedBookingSlot(1, start_at, start_at + timedelta(minutes=30), source_booking_id=11)

    async def cancel_booking(_session, _customer, _public_id):
        return booking, slot

    monkeypatch.setattr(customer_activity_service, "cancel_booking", cancel_booking)
    tasks = BackgroundTasks()

    response = await activity_routes.cancel_customer_booking(
        "public-booking-id",
        tasks,
        Response(),
        _customer(),
        SimpleNamespace(),
    )

    assert response.status is BookingStatus.cancelled
    assert len(tasks.tasks) == 2
    notification = tasks.tasks[1].args[0]
    assert notification.telegram_chat_id == "111"
    assert notification.service_name == "Стрижка"
    assert notification.customer_name == "Іван"


@pytest.mark.anyio
async def test_legacy_waitlist_cancel_schedules_every_released_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    slots = [FreedBookingSlot(1, datetime.now(UTC) + timedelta(days=1), datetime.now(UTC) + timedelta(days=1, minutes=30))]
    request = SimpleNamespace(public_id="waitlist-id", status=WaitlistStatus.cancelled)

    async def cancel_with_slots(_session, _token):
        return request, slots

    monkeypatch.setattr(waitlist_routes.service, "cancel_with_slots", cancel_with_slots)
    tasks = BackgroundTasks()
    response = await waitlist_routes.cancel_waitlist_request(
        SimpleNamespace(cancel_token="x" * 32), tasks, SimpleNamespace()
    )
    assert response.public_id == "waitlist-id"
    assert len(tasks.tasks) == 1


@pytest.mark.anyio
async def test_activity_waitlist_cancel_schedules_reoffer_after_service_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    slots = [FreedBookingSlot(1, datetime.now(UTC) + timedelta(days=1), datetime.now(UTC) + timedelta(days=1, minutes=30))]
    request = SimpleNamespace(public_id="waitlist-id", status=WaitlistStatus.cancelled)

    async def cancel_waitlist(_session, _customer, _public_id):
        return request, slots

    monkeypatch.setattr(customer_activity_service, "cancel_waitlist", cancel_waitlist)
    tasks = BackgroundTasks()
    response = await activity_routes.cancel_customer_waitlist(
        "waitlist-id", tasks, Response(), _customer(), SimpleNamespace()
    )
    assert response.status is WaitlistStatus.cancelled
    assert len(tasks.tasks) == 1


@pytest.mark.anyio
async def test_offer_message_redacts_fragment_link_even_mid_template() -> None:
    campaign = Campaign(id=9, name="offers", type="manual", status="active", channel="sms", purpose="transactional")
    request = WaitlistRequest(
        id=3, customer_id=4, cancel_token_hash="h" * 64, desired_date=date.today(), duration_minutes=30,
        notification_consent=True, status=WaitlistStatus.offered, expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    offer = WaitlistOffer(
        id=7, request_id=3, master_id=1, start_at=datetime.now(UTC) + timedelta(days=1),
        end_at=datetime.now(UTC) + timedelta(days=1, minutes=30), token_hash="h" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=10), scheduled_at=datetime.now(UTC), status=WaitlistOfferStatus.sent,
    )

    class Session:
        def __init__(self):
            self.values = [campaign, None]
            self.added = []

        async def execute(self, _stmt):
            return Result(self.values.pop(0))

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            for item in self.added:
                if isinstance(item, MessageRecipient) and item.id is None:
                    item.id = 1

    raw_link = "https://example.test/booking/waitlist-offer#raw-capability"
    body = f"Підтвердити ({raw_link}) зараз."
    session = Session()
    await WaitlistOfferService()._record_offer_message(
        session, offer=offer, request=request, body=body, booking_link=raw_link, provider_message_id="provider-1"
    )
    recipient = next(item for item in session.added if isinstance(item, MessageRecipient))
    log = next(item for item in session.added if isinstance(item, MessageLog))
    assert raw_link not in recipient.rendered_message
    assert "[secure-link]" in recipient.rendered_message
    assert recipient.waitlist_request_id == request.id and recipient.waitlist_offer_id == offer.id
    assert log.waitlist_offer_id == offer.id


def test_message_dtos_expose_waitlist_links_without_phone() -> None:
    assert "waitlist_request_id" in MessageRecipientResponse.model_fields
    assert "waitlist_offer_id" in MessageRecipientResponse.model_fields
    assert "waitlist_request_id" in MessageLogResponse.model_fields
    assert "customer_phone" not in MessageRecipientResponse.model_fields


def test_customer_activity_sms_uses_template_positions_without_hardcoded_suffix() -> None:
    body = (
        "Запис підтверджено. Скасування: {cancel_url}. "
        "Усі записи: {manage_url}."
    )

    rendered = CustomerActivityNotificationService()._render_activity_message(
        body,
        manage_url="https://example.test/booking/manage#token",
        cancel_url="https://example.test/booking/cancel#token",
    )

    assert rendered == (
        "Запис підтверджено. Скасування: "
        "https://example.test/booking/cancel#token. Усі записи: "
        "https://example.test/booking/manage#token."
    )
    assert rendered.count("/booking/manage#token") == 1
    assert rendered.count("/booking/cancel#token") == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("delivery", "expected"),
    [(SmsDeliveryStatus.delivered, MessageDeliveryStatus.delivered), (SmsDeliveryStatus.undeliverable, MessageDeliveryStatus.failed)],
)
async def test_offer_delivery_sync_updates_linked_message_recipient_and_log(
    monkeypatch: pytest.MonkeyPatch,
    delivery: SmsDeliveryStatus,
    expected: MessageDeliveryStatus,
) -> None:
    now = datetime.now(UTC)
    offer = WaitlistOffer(
        id=7, request_id=3, master_id=1, start_at=now + timedelta(days=1), end_at=now + timedelta(days=1, minutes=30),
        token_hash="h" * 64, expires_at=now + timedelta(minutes=10), scheduled_at=now,
        status=WaitlistOfferStatus.sent, provider_message_id="provider-1",
    )
    recipient = MessageRecipient(
        id=11, campaign_id=9, customer_id=4, waitlist_request_id=3, waitlist_offer_id=7,
        channel="sms", status=MessageDeliveryStatus.sent, idempotency_key="waitlist-offer:7",
    )
    request = WaitlistRequest(
        id=3, customer_id=4, cancel_token_hash="h" * 64, desired_date=date.today(), duration_minutes=30,
        notification_consent=True, status=WaitlistStatus.offered, expires_at=now + timedelta(days=1),
    )

    class Session:
        def __init__(self):
            self.values = [[offer], recipient]
            self.added = []

        async def execute(self, _stmt):
            return Result(self.values.pop(0))

        async def get(self, _model, _id):
            return request

        def add(self, item):
            self.added.append(item)

        async def commit(self):
            return None

    class Sms:
        async def get_message_statuses(self, _ids):
            return {"provider-1": delivery}

    async def no_analytics(*_args, **_kwargs):
        return None

    monkeypatch.setattr(booking_recovery_analytics_service, "record", no_analytics)
    service = WaitlistOfferService(sms_service=Sms())
    async def no_reoffer(*_args, **_kwargs):
        return None
    monkeypatch.setattr(service, "offer_slot", no_reoffer)
    session = Session()
    updated = await service.sync_delivery_statuses(session)
    assert updated == 1
    assert recipient.status is expected
    linked_logs = [item for item in session.added if isinstance(item, MessageLog)]
    assert len(linked_logs) == 1
    assert linked_logs[0].waitlist_request_id == request.id
    assert linked_logs[0].waitlist_offer_id == offer.id


def test_generic_delivery_log_preserves_waitlist_relations() -> None:
    recipient = MessageRecipient(
        id=11,
        campaign_id=9,
        customer_id=4,
        waitlist_request_id=3,
        waitlist_offer_id=7,
        channel="sms",
        status=MessageDeliveryStatus.sent,
        idempotency_key="waitlist-offer:7",
    )
    log = MessagingService()._log_from_recipient(recipient, MessageDeliveryStatus.delivered)
    assert log.waitlist_request_id == 3
    assert log.waitlist_offer_id == 7
