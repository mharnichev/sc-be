from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.models.customer import Customer
from app.models.messaging import (
    Campaign, CampaignStatus, CampaignType, ClientCommunicationPreference,
    ConsentStatus, MessageChannel, MessagePurpose,
)
from app.schemas.messaging import CampaignChannelStrategy, CampaignCreate, CampaignResponse, CampaignUpdate
from app.services.campaign_runs import CampaignRunService, choose_channel, marketing_contact_predicate


class NoQuerySession:
    async def execute(self, _):
        raise AssertionError("Validation must reject this payload before a database query")


@pytest.mark.parametrize(
    ("strategy", "telegram", "phone", "expected"),
    [
        ("single", "chat", "+380501234567", MessageChannel.telegram),
        ("single", None, "+380501234567", None),
        ("telegram_then_sms", "chat", "+380501234567", MessageChannel.telegram),
        ("telegram_then_sms", None, "+380501234567", MessageChannel.sms),
        ("sms_then_telegram", "chat", "+380501234567", MessageChannel.sms),
        ("sms_then_telegram", "chat", "", MessageChannel.telegram),
        ("telegram_then_sms", None, "", None),
    ],
)
def test_explicit_channel_strategy_selects_one_reachable_destination(strategy, telegram, phone, expected):
    customer = Customer(phone=phone)
    preference = ClientCommunicationPreference(telegram_chat_id=telegram)
    channel, reason = choose_channel(customer, preference, strategy, MessageChannel.telegram)
    assert channel == expected
    assert reason == ("channel_unreachable" if expected is None else None)


@pytest.mark.parametrize("options", [
    {"segment_ids": [-1]}, {"segment_ids": [True]}, {"segment_ids": "1"},
    {"segment_ids": list(range(1, 22))}, {"channel_strategy": "unread_sms"},
    {"marketing_frequency_days": 0}, {"marketing_frequency_days": True},
    {"exclude_upcoming_booking": "false"}, {"exclude_returned_since_snapshot": 1},
])
def test_reserved_metadata_cannot_bypass_typed_campaign_validation(options):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(CampaignRunService().prepare_campaign_data(
            NoQuerySession(), {"type": CampaignType.manual, "metadata_json": options},
        ))
    assert caught.value.status_code == 422


def test_segment_campaign_creation_is_draft_even_if_active_requested(monkeypatch):
    service = CampaignRunService()
    async def load_segments(*args, **kwargs):
        return [SimpleNamespace(id=3)]
    monkeypatch.setattr(service, "_load_segments", load_segments)
    data = asyncio.run(service.prepare_campaign_data(NoQuerySession(), {
        "type": CampaignType.manual, "status": CampaignStatus.active,
        "purpose": MessagePurpose.marketing, "segment_ids": [3, 3],
    }))
    assert data["status"] == CampaignStatus.draft
    assert data["metadata_json"]["segment_ids"] == [3]
    assert "segment_ids" not in data


@pytest.mark.parametrize("data", [
    {"metadata_json": {"recipient": "master"}},
    {"type": CampaignType.booking_confirmation},
    {"purpose": MessagePurpose.transactional},
])
def test_saved_customer_segments_reject_notification_and_master_targets(data):
    payload = {"type": CampaignType.manual, "purpose": MessagePurpose.marketing, "segment_ids": [3], **data}
    with pytest.raises(HTTPException) as caught:
        asyncio.run(CampaignRunService().prepare_campaign_data(NoQuerySession(), payload))
    assert caught.value.status_code == 422


def test_schema_deduplicates_selected_segments_and_rejects_invalid_ids():
    payload = CampaignCreate(name="Win back", type="manual", segment_ids=[3, 1, 3])
    assert payload.segment_ids == [3, 1]
    with pytest.raises(ValidationError):
        CampaignUpdate(segment_ids=[0])


def test_update_preserves_explicit_delivery_options_when_editing_content():
    campaign = Campaign(type=CampaignType.manual, purpose=MessagePurpose.marketing,
                        metadata_json={"channel_strategy": "telegram_then_sms", "marketing_frequency_days": 14})
    result = asyncio.run(CampaignRunService().prepare_campaign_data(NoQuerySession(), {
        "metadata_json": {"message_body": "A new offer"},
    }, campaign=campaign))
    assert result["metadata_json"]["channel_strategy"] == "telegram_then_sms"
    assert result["metadata_json"]["marketing_frequency_days"] == 14


def test_cross_campaign_cap_uses_acceptance_and_durable_claims_and_frozen_run_purpose():
    expression = marketing_contact_predicate(10, datetime(2026, 9, 6, tzinfo=ZoneInfo("Europe/Kyiv")), 7,
                                            exclude_recipient_id=77)
    sql = str(expression.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "send_started_at" in sql
    assert "sent_at" in sql
    assert "campaign_snapshot" in sql
    assert "message_recipients.id != 77" in sql
    assert "booking_confirmation" not in sql
    assert "campaigns.id = 10" not in sql
