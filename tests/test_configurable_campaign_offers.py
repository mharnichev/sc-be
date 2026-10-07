import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.models.booking import Booking
from app.models.customer import Customer
from app.models.messaging import Campaign, CampaignType, MessageChannel, MessagePurpose
from app.schemas.messaging import CampaignCreate, CampaignResponse, CampaignUpdate
from app.services.campaign_offers import CampaignOfferService
from app.services.campaign_runs import CampaignRunService, delivery_options
from app.services.promotion import PromotionService


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_offer_mode_is_typed_and_round_trips_through_metadata():
    assert CampaignCreate(name="Offer", type="manual").offer_audience_mode == "last_visit_3_12"
    assert CampaignCreate(name="Offer", type="manual", offer_audience_mode="segments").offer_audience_mode == "segments"
    assert CampaignUpdate(offer_audience_mode="segments").model_dump(exclude_unset=True) == {"offer_audience_mode": "segments"}
    loaded = CampaignResponse.load_delivery_options({"metadata_json": {"offer_audience_mode": "segments"}})
    assert loaded["offer_audience_mode"] == "segments"
    for schema in (CampaignCreate, CampaignUpdate):
        with pytest.raises(ValidationError):
            schema(name="Offer", type="manual", offer_audience_mode="unknown")


@pytest.mark.parametrize("channel", [MessageChannel.sms, MessageChannel.telegram])
@pytest.mark.parametrize("body", [
    "{{discount_percent}} {{promotion_name_uk}} {{promotion_name_en}} {{offer_link}}",
    "{discount_percent} {promotion_name_uk} {promotion_name_en}",
    "#discount_percent #promotion_name_uk #promotion_name_en",
])
def test_personal_offer_variables_pass_backend_template_validation(channel, body):
    from app.services.messaging import MessagingService
    MessagingService().validate_template_body(body, channel=channel)


@pytest.mark.parametrize("channel,strategy", [("telegram", "single"), ("sms", "single"), ("telegram", "telegram_then_sms")])
def test_segment_offer_channel_contract_and_content_update_preserves_mode(monkeypatch, channel, strategy):
    service = CampaignRunService()

    async def load(*args, **kwargs):
        return [SimpleNamespace(id=3)]

    monkeypatch.setattr(service, "_load_segments", load)
    result = asyncio.run(service.prepare_campaign_data(None, {
        "type": "manual", "offer_master_id": 4, "channel": channel, "purpose": "marketing",
        "offer_audience_mode": "segments", "segment_ids": [3], "channel_strategy": strategy,
    }))
    assert result["metadata_json"]["offer_audience_mode"] == "segments"
    assert "offer_audience_mode" not in result
    campaign = Campaign(**result)
    # Simulate loaded enum values; database construction does not coerce strings.
    campaign.type, campaign.channel, campaign.purpose = CampaignType.manual, MessageChannel(channel), MessagePurpose.marketing
    update = asyncio.run(service.prepare_campaign_data(None, {"name": "Edited"}, campaign=campaign))
    assert update["metadata_json"]["offer_audience_mode"] == "segments"
    assert update["metadata_json"]["channel_strategy"] == strategy
    assert delivery_options(campaign)["offer_audience_mode"] == "segments"


@pytest.mark.parametrize("change", [
    {"offer_audience_mode": "unknown"}, {"offer_audience_mode": None},
    {"segment_ids": []}, {"channel": "email"}, {"channel_strategy": "sms_then_telegram"},
    {"channel": "sms", "channel_strategy": "telegram_then_sms"},
])
def test_invalid_segment_offer_configuration_rejected(change):
    payload = {"type": "manual", "offer_master_id": 4, "channel": "telegram", "purpose": "marketing",
               "offer_audience_mode": "segments", "segment_ids": [3], **change}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(CampaignRunService().prepare_campaign_data(None, payload))
    assert exc.value.status_code == 422


def test_legacy_offer_still_rejects_single_telegram():
    with pytest.raises(HTTPException):
        asyncio.run(CampaignRunService().prepare_campaign_data(None, {
            "type": "manual", "offer_master_id": 4, "channel": "telegram", "purpose": "marketing",
        }))


def test_segment_audience_does_not_add_legacy_predicate(monkeypatch):
    from app.services.segments import segment_service
    calls = []

    def predicate(rules, now):
        calls.append(rules)
        return Customer.id > 0

    monkeypatch.setattr(segment_service, "build_predicate", predicate)
    campaign = Campaign(offer_master_id=4, metadata_json={"offer_audience_mode": "segments"})
    snapshots = [{"rules": {"selected": True}}]
    service = CampaignRunService()
    stmt = service._audience_statement(campaign, snapshots, datetime.now(UTC))
    assert calls == [{"selected": True}]
    assert "customers.id >" in str(stmt.compile(dialect=postgresql.dialect()))
    campaign.metadata_json = {}
    service._audience_statement(campaign, snapshots, datetime.now(UTC))
    assert len(calls) == 3
    assert calls[-1]["conditions"][0]["min"] == 3


@pytest.mark.anyio
@pytest.mark.parametrize("mode,discount,expected", [("segments", 15, 15), ("segments", 100, 100), ("last_visit_3_12", 15, 30)])
async def test_issued_offer_uses_frozen_discount_and_renders_matching_variables(mode, discount, expected):
    class Session:
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, offer):
            self.offer = offer

        async def flush(self):
            pass

    now = datetime.now(UTC)
    snapshot = {"offer_audience_mode": mode, "offer_discount_percent": discount,
                "offer_master_id": 4, "offer_promotion_id": 6, "offer_service_ids": [9],
                "offer_starts_at": now.isoformat(), "offer_expires_at": (now + timedelta(days=7)).isoformat(),
                "promotion_name_uk": "Personal", "promotion_name_en": "Personal", "master_name_for_message": "Master"}
    session = Session()
    offer, token = await CampaignOfferService().issue_offer(session, SimpleNamespace(id=1, campaign_id=2, run_id=3, customer_id=5), snapshot)
    assert offer.discount_percent == expected
    assert offer.conditions_snapshot["offer_audience_mode"] == mode
    # Real legacy run snapshots always contain 30; configurable rendering uses the selected value.
    if mode == "segments":
        assert CampaignOfferService().render_variables(snapshot, token)["discount_percent"] == str(expected)
    assert token not in str(offer.conditions_snapshot)


@pytest.mark.anyio
@pytest.mark.parametrize("returned,upcoming,latest_days,future,expected", [
    (False, False, None, True, None), (False, False, 1, False, None),
    (False, True, None, True, "future_booking"), (False, True, None, False, None),
    (True, False, 1, False, "returned_since_snapshot"), (True, False, 20, False, None),
    (True, False, None, False, None),
])
async def test_segment_redemption_uses_frozen_exclusion_options(monkeypatch, returned, upcoming, latest_days, future, expected):
    now = datetime.now(UTC)
    snapshot = {"offer_audience_mode": "segments", "exclude_returned_since_snapshot": returned,
                "exclude_upcoming_booking": upcoming, "eligibility_evaluated_at": (now - timedelta(days=10)).isoformat()}
    offer = SimpleNamespace(customer_id=1, conditions_snapshot=snapshot)

    async def last_visit(*args):
        assert returned, "No age restriction is permitted without the returned exclusion"
        return now - timedelta(days=latest_days) if latest_days is not None else None

    monkeypatch.setattr(PromotionService, "customer_last_visit_at", last_visit)

    class Session:
        async def get(self, model, identifier):
            return SimpleNamespace(id=identifier)

        async def execute(self, stmt):
            assert upcoming
            assert "bookings.id !=" in str(stmt)
            return SimpleNamespace(scalar_one_or_none=lambda: 99 if future else None)

    assert await CampaignOfferService().current_restriction(Session(), offer, exclude_booking_id=8, at=now) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("latest_days,expected", [(None, "audience_no_longer_eligible"), (1, "audience_no_longer_eligible"), (180, "future_booking")])
async def test_legacy_redemption_keeps_inactivity_and_future_booking_restrictions(monkeypatch, latest_days, expected):
    now = datetime.now(UTC)

    async def last_visit(*args):
        return now - timedelta(days=latest_days) if latest_days is not None else None

    monkeypatch.setattr(PromotionService, "customer_last_visit_at", last_visit)

    class Session:
        async def get(self, *args):
            return SimpleNamespace(id=1)

        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: 99)

    assert await CampaignOfferService().current_restriction(Session(), SimpleNamespace(customer_id=1), at=now) == expected


@pytest.mark.anyio
async def test_configurable_booking_uses_issued_discount_not_current_promotion(monkeypatch):
    now = datetime.now(UTC)
    offer = SimpleNamespace(id=7, customer_id=2, master_id=4, allowed_service_ids=[9], starts_at=now-timedelta(days=1),
        expires_at=now+timedelta(days=5), consumed_booking_id=None, campaign_id=1, run_id=3, recipient_id=5,
        promotion_id=6, promotion_name_uk="Personal", promotion_name_en="Personal", discount_percent=15,
        conditions_snapshot={"offer_audience_mode": "segments"})
    service = CampaignOfferService()

    async def resolve(*args, **kwargs):
        return offer

    monkeypatch.setattr(service, "resolve", resolve)

    class Session:
        async def get(self, *args):
            return SimpleNamespace(is_active=True, discount_percent=80)

        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    booking = Booking(master_id=4, start_at=now+timedelta(days=1))
    await service.apply(Session(), booking=booking, customer=SimpleNamespace(id=2), services=[SimpleNamespace(id=9, price=800)], token="secret", reserve=False)
    assert booking.total_amount == 680
    assert booking.promotion_discount_percent_snapshot == 15
    assert booking.promotion_eligibility_type_snapshot == "all_customers"


@pytest.mark.anyio
@pytest.mark.parametrize("mode,channel,strategy,discount,promotion_ready,sms_required", [
    ("segments", "telegram", "single", 15, True, False),
    ("segments", "sms", "single", 100, True, True),
    ("segments", "telegram", "telegram_then_sms", 15, True, True),
    ("segments", "sms", "single", 0, False, True),
    ("segments", "sms", "single", 101, False, True),
    ("last_visit_3_12", "sms", "single", 15, False, True),
    ("last_visit_3_12", "sms", "telegram_then_sms", 30, True, True),
])
async def test_readiness_discount_and_transport_dependencies(monkeypatch, mode, channel, strategy, discount, promotion_ready, sms_required):
    from sqlalchemy import select
    from app.core.config import settings
    from app.models.booking import Master
    from app.models.promotion import PromotionDiscountType

    now = datetime.now(UTC)
    campaign = Campaign(type=CampaignType.manual, channel=MessageChannel(channel), purpose=MessagePurpose.marketing,
        offer_master_id=4, offer_promotion_id=6, offer_service_ids=[9], master_name_for_message="Master",
        offer_starts_at=now-timedelta(days=1), offer_expires_at=now+timedelta(days=7),
        metadata_json={"offer_audience_mode": mode, "segment_ids": [3], "channel_strategy": strategy},
        sending_window={"start": "10:00", "end": "18:00"})
    promotion = SimpleNamespace(is_active=True, recipient_offer_only=True, discount_percent=discount, discount_type=PromotionDiscountType.percent)
    monkeypatch.setattr(settings, "campaign_run_scheduler_enabled", True)
    monkeypatch.setattr(settings, "sms_queue_worker_enabled", True)
    monkeypatch.setattr(settings, "sms_provider", "smsclub")
    monkeypatch.setattr(settings, "sms_club_token", "sandbox")
    monkeypatch.setattr(settings, "telegram_bot_token", "sandbox")
    monkeypatch.setattr("app.services.campaign_runtime.worker_healthy", lambda *args: True)
    service = CampaignRunService(messaging=SimpleNamespace(campaign_message_body=lambda campaign: "Personal offer"))

    async def load(*args):
        return [SimpleNamespace(id=3, rules={})]

    async def preview(*args):
        return {"communication_eligible_recipients": 1, "personalization_invalid_recipients": 0}

    monkeypatch.setattr(service, "_load_segments", load)
    monkeypatch.setattr(service, "preview", preview)
    monkeypatch.setattr(service, "_audience_statement", lambda *args: select(Customer))

    class Session:
        async def get(self, model, identifier):
            return SimpleNamespace(is_active=True) if model is Master else promotion

        async def scalars(self, stmt):
            return SimpleNamespace(all=lambda: [SimpleNamespace(id=9, is_active=True, master_id=4)])

        async def scalar(self, stmt):
            return 1

    result = await service.readiness(Session(), campaign)
    checks = {item["code"]: item["status"] for item in result["checks"]}
    assert (checks["promotion"] == "ready") is promotion_ready
    assert ("sms_worker" in checks) is sms_required
    assert ("sms_provider" in checks) is sms_required
    assert ("telegram_provider" in checks) is (channel == "telegram" or strategy == "telegram_then_sms")
    assert result["ready"] is promotion_ready


@pytest.mark.anyio
@pytest.mark.parametrize("channel,strategy", [("sms", "single"), ("telegram", "single"), ("telegram", "telegram_then_sms")])
async def test_snapshot_persists_mode_discount_and_segment_evidence_without_legacy_facts(monkeypatch, channel, strategy):
    from sqlalchemy import select
    from app.models.campaign_offer import CampaignOffer
    from app.models.campaign_run import CampaignRun
    from app.models.messaging import MessageRecipient
    from app.models.promotion import PromotionDiscountType
    from app.services.messaging import MessagingService
    from app.services.segments import segment_service

    now = datetime.now(UTC)
    customer = Customer(id=5, name="Client", phone="+380501111111", is_active=True)
    promotion = SimpleNamespace(is_active=True, recipient_offer_only=True, discount_percent=25,
                                discount_type=PromotionDiscountType.percent, name_uk="Personal UK", name_en="Personal EN")
    segment = SimpleNamespace(id=3, name="Selected segment", revision=2, rules={"selected": True})
    campaign = Campaign(id=2, name="Personal", type=CampaignType.manual, channel=MessageChannel(channel),
        purpose=MessagePurpose.marketing, offer_master_id=4, offer_promotion_id=6, offer_service_ids=[9],
        master_name_for_message="Master", offer_starts_at=now-timedelta(days=1), offer_expires_at=now+timedelta(days=7),
        metadata_json={"offer_audience_mode": "segments", "segment_ids": [3], "channel_strategy": strategy})
    run = CampaignRun(id=1, campaign_id=2, audience_count=0, campaign_snapshot={})
    messaging = MessagingService()
    body = "{{discount_percent}}% {{promotion_name_uk}} {{promotion_name_en}} {{offer_link}}"
    monkeypatch.setattr(messaging, "campaign_message_body", lambda campaign: body)

    async def preference(*args):
        return SimpleNamespace(telegram_chat_id="chat")

    async def render(session, template, customer, campaign, *, extra_variables):
        return messaging.render_template(template, extra_variables), extra_variables

    async def load(*args, **kwargs):
        return [segment]

    async def facts(session, ids, rules, at):
        assert rules == segment.rules, "The legacy 3-12 month rules must not be evaluated"
        return {5: {"selected": True}}

    monkeypatch.setattr(messaging, "get_preference", preference)
    monkeypatch.setattr(messaging, "communication_allowed", lambda *args: (True, None))
    monkeypatch.setattr(messaging, "render_for_customer", render)
    service = CampaignRunService(messaging)
    monkeypatch.setattr(service, "_load_segments", load)
    monkeypatch.setattr(service, "_audience_statement", lambda *args: select(Customer))
    monkeypatch.setattr(segment_service, "member_facts", facts)

    class Session:
        def __init__(self):
            self.items = []
            self.pages = 0

        def add(self, item):
            if isinstance(item, MessageRecipient):
                item.id = 7
            self.items.append(item)

        async def flush(self):
            pass

        async def get(self, model, identifier):
            return promotion

        async def scalar(self, stmt):
            return False

        async def execute(self, stmt):
            if stmt.column_descriptions[0]["entity"] is Customer:
                self.pages += 1
                rows = [customer] if self.pages == 1 else []
                return SimpleNamespace(scalars=lambda: rows)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    session = Session()
    await service.snapshot(session, run, campaign, now=now)
    recipient = next(item for item in session.items if isinstance(item, MessageRecipient))
    offer = next(item for item in session.items if isinstance(item, CampaignOffer))
    assert recipient.rendered_message.startswith("25% Personal UK Personal EN")
    assert "offer_token=" in recipient.rendered_message
    assert recipient.snapshot_facts["segments"] == {"3": {"selected": True}}
    assert run.campaign_snapshot["offer_discount_percent"] == offer.discount_percent == 25
    assert offer.conditions_snapshot["offer_audience_mode"] == "segments"
    assert run.audience_count == 1
    assert run.status == "snapshotted"
    promotion.discount_percent = 80
    assert offer.discount_percent == 25
