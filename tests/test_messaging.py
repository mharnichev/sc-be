from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.models.messaging import (
    Campaign,
    CampaignStatus,
    CampaignType,
    ClientCommunicationPreference,
    ConsentStatus,
    MessageChannel,
    MessageDeliveryStatus,
    MessagePurpose,
    MessageRecipient,
    ReviewPlatform,
    ReviewRequest,
    ReviewRequestStatus,
)
from app.services.booking import KYIV_TZ
from app.services.messaging import MessageProvider, MessagingService, ProviderSendResult
from app.services.sms import SmsDeliveryStatus


class FakeProvider(MessageProvider):
    channel = MessageChannel.telegram

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, *, destination: str, body: str, reply_markup: dict | None = None) -> ProviderSendResult:
        self.sent.append((destination, body))
        return ProviderSendResult(provider_message_id="42", raw_response={"ok": True})


class FakeSmsStatusProvider(MessageProvider):
    channel = MessageChannel.sms

    def __init__(self, status_value: SmsDeliveryStatus) -> None:
        self.status_value = status_value
        self.requested_ids: list[str] = []

    async def send_message(
        self,
        *,
        destination: str,
        body: str,
        reply_markup: dict | None = None,
    ) -> ProviderSendResult:
        raise AssertionError("send_message must not be called while synchronizing statuses")

    async def get_delivery_statuses(
        self,
        provider_message_ids: list[str],
    ) -> dict[str, SmsDeliveryStatus]:
        self.requested_ids = list(provider_message_ids)
        return {message_id: self.status_value for message_id in provider_message_ids}


class FakeScalarListResult:
    def __init__(self, values: list) -> None:  # noqa: ANN001
        self.values = values

    def all(self) -> list:
        return self.values


class FakeExecuteListResult:
    def __init__(self, values: list) -> None:  # noqa: ANN001
        self.values = values

    def scalars(self) -> FakeScalarListResult:
        return FakeScalarListResult(self.values)


class FakeScalarResult:
    def __init__(self, value) -> None:  # noqa: ANN001
        self.value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.value


class FakeReminderSession:
    def __init__(self, responses: list) -> None:  # noqa: ANN001
        self.responses = responses
        self.added: list[object] = []
        self.committed = False

    async def execute(self, statement):  # noqa: ANN001, ANN201
        assert self.responses, f"Unexpected statement: {statement}"
        return self.responses.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for index, value in enumerate(self.added, start=1):
            if isinstance(value, MessageRecipient) and value.id is None:
                value.id = index

    async def commit(self) -> None:
        self.committed = True


class FakeDialect:
    name = "sqlite"


class FakeBind:
    dialect = FakeDialect()


class FakeDeliveryStatusSession:
    def __init__(self, responses: list) -> None:  # noqa: ANN001
        self.responses = responses
        self.added: list[object] = []
        self.committed = False

    def get_bind(self) -> FakeBind:
        return FakeBind()

    async def execute(self, statement):  # noqa: ANN001, ANN201
        assert self.responses, f"Unexpected statement: {statement}"
        return FakeExecuteListResult(self.responses.pop(0))

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


def test_template_renderer_replaces_supported_variables() -> None:
    service = MessagingService()

    rendered = service.render_template(
        "Hi {{ client_name }}, please review {{barbershop_name}}: {{review_link}}",
        {
            "client_name": "Anna",
            "barbershop_name": "Soulcuts",
            "review_link": "https://example.test/review",
        },
    )

    assert rendered == "Hi Anna, please review Soulcuts: https://example.test/review"


def test_template_renderer_replaces_legacy_hash_variables() -> None:
    service = MessagingService()

    rendered = service.render_template(
        "#client Нагадуємо, Ви записані #date на #service",
        {
            "client": "Ivan",
            "date": "21.06.2026 10:00",
            "service": "Стрижка",
        },
    )

    assert rendered == "Ivan Нагадуємо, Ви записані 21.06.2026 10:00 на Стрижка"


def test_template_renderer_replaces_single_brace_sms_variables() -> None:
    service = MessagingService()

    rendered = service.render_template(
        "Ви записані до {master_name} о {appointment_time}, {customer_name}.",
        {
            "master_name": "Андрій",
            "appointment_time": "10:00",
            "customer_name": "Олена",
        },
    )

    assert rendered == "Ви записані до Андрій о 10:00, Олена."


def test_template_validation_rejects_unknown_variables() -> None:
    service = MessagingService()

    with pytest.raises(HTTPException) as exc_info:
        service.validate_template_body("Hello {{unknown_value}}")

    assert exc_info.value.status_code == 422
    assert "unknown_value" in exc_info.value.detail


def test_missing_preference_uses_full_consent_default() -> None:
    service = MessagingService()

    allowed, reason = service.communication_allowed(None, MessagePurpose.marketing)

    assert allowed is True
    assert reason is None
    assert service.has_marketing_consent(None) is True


def test_explicit_unknown_marketing_consent_still_blocks_marketing() -> None:
    service = MessagingService()
    preference = ClientCommunicationPreference(
        customer_id=1,
        marketing_consent=ConsentStatus.unknown,
        transactional_consent=ConsentStatus.opted_in,
    )

    allowed, reason = service.communication_allowed(preference, MessagePurpose.marketing)

    assert allowed is False
    assert reason == "Client has no marketing consent"
    assert service.has_marketing_consent(preference) is False


def test_transactional_messages_can_be_sent_without_marketing_consent() -> None:
    service = MessagingService()
    preference = ClientCommunicationPreference(
        customer_id=1,
        marketing_consent=ConsentStatus.opted_out,
        transactional_consent=ConsentStatus.opted_in,
    )

    allowed, reason = service.communication_allowed(preference, MessagePurpose.transactional)

    assert allowed is True
    assert reason is None


def test_do_not_contact_blocks_all_message_purposes() -> None:
    service = MessagingService()
    preference = ClientCommunicationPreference(
        customer_id=1,
        do_not_contact=True,
        marketing_consent=ConsentStatus.opted_in,
        transactional_consent=ConsentStatus.opted_in,
    )

    for purpose in MessagePurpose:
        allowed, reason = service.communication_allowed(preference, purpose)
        assert allowed is False
        assert reason == "Client is marked do-not-contact"


def test_idempotency_key_includes_campaign_customer_and_appointment() -> None:
    service = MessagingService()

    assert service.build_idempotency_key(10, 20, 30) == "campaign:10:customer:20:appointment:30"
    assert service.build_idempotency_key(10, 20) == "campaign:10:customer:20:appointment:none"


@pytest.mark.anyio
async def test_provider_boundary_returns_provider_message_id() -> None:
    provider = FakeProvider()

    result = await provider.send_message(destination="123", body="Test")

    assert provider.sent == [("123", "Test")]
    assert result.provider_message_id == "42"
    assert result.raw_response == {"ok": True}


def _sent_sms_review_request() -> tuple[MessageRecipient, ReviewRequest]:
    now = datetime.now(KYIV_TZ)
    campaign = Campaign(
        id=10,
        name="review",
        type=CampaignType.post_visit_review_request,
        status=CampaignStatus.active,
        channel=MessageChannel.sms,
        purpose=MessagePurpose.review_request,
    )
    recipient = MessageRecipient(
        id=20,
        campaign_id=campaign.id,
        customer_id=30,
        appointment_id=40,
        channel=MessageChannel.sms,
        status=MessageDeliveryStatus.sent,
        idempotency_key="delivery-status-test",
        sent_at=now,
        attempts=1,
        provider_message_id="smsclub-42",
    )
    request_item = ReviewRequest(
        id=50,
        campaign_id=campaign.id,
        appointment_id=40,
        customer_id=30,
        master_id=60,
        recipient_id=recipient.id,
        platform=ReviewPlatform.internal,
        review_url="/masters",
        sent_at=now,
        status=ReviewRequestStatus.sent,
        channel=MessageChannel.sms,
    )
    return recipient, request_item


@pytest.mark.anyio
async def test_smsclub_delivered_status_updates_recipient_and_review_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    recipient, request_item = _sent_sms_review_request()
    session = FakeDeliveryStatusSession([[recipient], [request_item]])
    provider = FakeSmsStatusProvider(SmsDeliveryStatus.delivered)

    updated = await MessagingService({MessageChannel.sms: provider}).sync_sms_delivery_statuses(session)

    assert updated == 1
    assert provider.requested_ids == ["smsclub-42"]
    assert recipient.status == MessageDeliveryStatus.delivered
    assert recipient.delivered_at is not None
    assert recipient.delivery_status_checked_at is not None
    assert request_item.status == ReviewRequestStatus.delivered
    assert request_item.delivered_at == recipient.delivered_at
    assert request_item.events[-1].reason == "smsclub_delivery_confirmed"
    assert session.committed is True


@pytest.mark.anyio
async def test_smsclub_undeliverable_status_marks_recipient_and_review_request_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    recipient, request_item = _sent_sms_review_request()
    session = FakeDeliveryStatusSession([[recipient], [request_item]])
    provider = FakeSmsStatusProvider(SmsDeliveryStatus.undeliverable)

    updated = await MessagingService({MessageChannel.sms: provider}).sync_sms_delivery_statuses(session)

    assert updated == 1
    assert recipient.status == MessageDeliveryStatus.failed
    assert recipient.last_error == "SMS Club delivery status: UNDELIV"
    assert request_item.status == ReviewRequestStatus.failed
    assert request_item.failure_reason == "smsclub_undeliv"
    assert session.committed is True


@pytest.mark.anyio
async def test_render_for_customer_uses_customer_and_campaign_values() -> None:
    service = MessagingService()
    customer = SimpleNamespace(name="Ivan", surname="Petrenko", phone="+380")
    campaign = SimpleNamespace(review_url="https://reviews.test", discount_code="VIP10")

    rendered, variables = await service.render_for_customer(
        None,
        "Hi {{client_name}}, review: {{review_link}}, code {{discount_code}}",
        customer,
        campaign,
    )

    assert rendered == "Hi Ivan Petrenko, review: https://reviews.test, code VIP10"
    assert variables["client_name"] == "Ivan Petrenko"


@pytest.mark.anyio
async def test_review_request_template_uses_first_name_without_surname() -> None:
    service = MessagingService()
    customer = SimpleNamespace(name="Ivan", surname="Petrenko", phone="+380")
    campaign = SimpleNamespace(
        type=CampaignType.post_visit_review_request,
        review_url=None,
        discount_code=None,
    )

    rendered, variables = await service.render_for_customer(
        None,
        "#client, please review your visit",
        customer,
        campaign,
    )

    assert rendered == "Ivan, please review your visit"
    assert variables["client"] == "Ivan"
    assert variables["client_name"] == "Ivan"
    assert variables["customer_name"] == "Ivan"


@pytest.mark.anyio
async def test_enqueue_recipient_prefers_campaign_metadata_message_body() -> None:
    service = MessagingService()
    customer = SimpleNamespace(id=77, name="Ivan", surname="", phone="+380501112233")
    campaign = SimpleNamespace(
        id=10,
        channel=MessageChannel.telegram,
        purpose=MessagePurpose.transactional,
        template=SimpleNamespace(body="Old body #client"),
        metadata_json={"message_body": "New body #client"},
        review_url=None,
        discount_code=None,
    )
    preference = ClientCommunicationPreference(
        customer_id=customer.id,
        telegram_chat_id="987654321",
        transactional_consent=ConsentStatus.opted_in,
    )
    session = FakeReminderSession([FakeScalarResult(None), FakeScalarResult(preference)])

    created = await service.enqueue_recipient(session, campaign, customer, None)

    recipients = [item for item in session.added if isinstance(item, MessageRecipient)]
    assert created == 1
    assert recipients[0].rendered_message == "New body Ivan"


@pytest.mark.anyio
async def test_create_appointment_reminders_enqueues_upcoming_bookings() -> None:
    service = MessagingService()
    customer = SimpleNamespace(id=77, name="Ivan", surname="", phone="+380501112233")
    campaign = SimpleNamespace(
        id=10,
        type=CampaignType.appointment_reminder,
        status=CampaignStatus.active,
        channel=MessageChannel.telegram,
        purpose=MessagePurpose.transactional,
        template=SimpleNamespace(body="#client Нагадуємо, Ви записані #date до #master_name на #service"),
        template_id=5,
        review_url=None,
        discount_code=None,
        metadata_json={"lead_hours": 24, "window_minutes": 60},
    )
    booking = SimpleNamespace(
        id=73723,
        customer_id=customer.id,
        customer=customer,
        master=SimpleNamespace(full_name="Технічний календар"),
        redirected_from_master=SimpleNamespace(full_name="Глеб"),
        service=None,
        services=[SimpleNamespace(name="Haircut", title_uk="Стрижка")],
        start_at=datetime.now(KYIV_TZ) + timedelta(hours=24, minutes=10),
    )
    preference = ClientCommunicationPreference(
        customer_id=customer.id,
        telegram_chat_id="987654321",
        transactional_consent=ConsentStatus.opted_in,
    )
    session = FakeReminderSession(
        [
            FakeExecuteListResult([campaign]),
            FakeExecuteListResult([booking]),
            FakeScalarResult(None),
            FakeScalarResult(preference),
        ]
    )

    created = await service.create_appointment_reminders_for_upcoming_bookings(session)

    recipients = [item for item in session.added if isinstance(item, MessageRecipient)]
    assert created == 1
    assert session.committed is True
    assert len(recipients) == 1
    assert recipients[0].status == MessageDeliveryStatus.pending
    assert recipients[0].rendered_message is not None
    assert recipients[0].rendered_message.startswith("Ivan Нагадуємо, Ви записані ")
    assert " до Глеб " in recipients[0].rendered_message
    assert "Технічний календар" not in recipients[0].rendered_message
    assert recipients[0].rendered_message.endswith(" на Стрижка")
