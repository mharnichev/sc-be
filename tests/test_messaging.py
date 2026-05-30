from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.messaging import (
    ClientCommunicationPreference,
    ConsentStatus,
    MessageChannel,
    MessagePurpose,
)
from app.services.messaging import MessageProvider, MessagingService, ProviderSendResult


class FakeProvider(MessageProvider):
    channel = MessageChannel.telegram

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, *, destination: str, body: str) -> ProviderSendResult:
        self.sent.append((destination, body))
        return ProviderSendResult(provider_message_id="42", raw_response={"ok": True})


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


def test_template_validation_rejects_unknown_variables() -> None:
    service = MessagingService()

    with pytest.raises(HTTPException) as exc_info:
        service.validate_template_body("Hello {{unknown_value}}")

    assert exc_info.value.status_code == 422
    assert "unknown_value" in exc_info.value.detail


def test_marketing_messages_require_explicit_opt_in() -> None:
    service = MessagingService()

    allowed, reason = service.communication_allowed(None, MessagePurpose.marketing)

    assert allowed is False
    assert reason == "Client has no marketing consent"


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
