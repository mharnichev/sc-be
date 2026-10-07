"""Wizard HTTP payloads against disposable PostgreSQL, without app workers/providers."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.v1.routes.messaging import backoffice_router
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.campaign_run import CampaignRun
from app.models.booking import BarberService, Master
from app.models.messaging import Campaign, MessageRecipient, MessageTemplate
from app.models.promotion import Promotion
from test_segments_integration import anyio_backend, database, add_segment


@pytest.fixture
async def wizard_api(database):
    app = FastAPI()
    app.include_router(backoffice_router, prefix="/api/v1/backoffice/messaging")

    async def session_override():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = session_override
    app.dependency_overrides[get_current_admin_user] = lambda: object()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://wizard.test") as client:
        yield client


@pytest.mark.anyio
@pytest.mark.parametrize("channel,strategy", [("telegram", "single"), ("sms", "single"), ("telegram", "telegram_then_sms")])
async def test_six_step_payload_round_trips_without_launch(database, wizard_api, channel, strategy):
    async with database() as session:
        segment = await add_segment(session)
        await session.commit()
        segment_id = segment.id
    template = await wizard_api.post("/api/v1/backoffice/messaging/templates", json={
        "name": "Wizard template", "channel": channel,
        "language": "uk", "body": "Hello {{client_name}}", "is_active": True,
    })
    assert template.status_code == 201, template.text
    scheduled = datetime.now(UTC) + timedelta(days=2)
    payload = {
        "name": "Wizard full campaign", "type": "re_engagement", "channel": channel,
        "purpose": "marketing", "status": "draft", "template_id": template.json()["id"],
        "segment_ids": [segment_id], "channel_strategy": strategy,
        "exclude_upcoming_booking": False, "exclude_returned_since_snapshot": False,
        "marketing_frequency_days": 12, "review_platform": "instagram",
        "review_url": "https://example.test/review", "discount_code": "SOUL12",
        "timezone": "Europe/Warsaw", "scheduled_at": scheduled.isoformat(),
        "sms_recipients_per_minute": 50,
        "metadata_json": {"message_body": "Edited {{client_name}} {{review_link}} {{discount_code}}",
                          "schedule_mode": "later", "max_messages_per_minute": 50},
    }
    response = await wizard_api.post("/api/v1/backoffice/messaging/campaigns", json=payload)
    assert response.status_code == 201, response.text
    saved = response.json()
    for key in ("name", "type", "channel", "status", "template_id", "segment_ids", "channel_strategy",
                "marketing_frequency_days", "review_platform", "review_url", "discount_code", "timezone"):
        assert saved[key] == payload[key]
    assert saved["metadata_json"]["message_body"] == payload["metadata_json"]["message_body"]
    assert saved["template_body"] == payload["metadata_json"]["message_body"]
    assert datetime.fromisoformat(saved["scheduled_at"]) == scheduled
    detail = await wizard_api.get(f"/api/v1/backoffice/messaging/campaigns/{saved['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["segment_ids"] == [segment_id]
    assert detail.json()["template_body"] == payload["metadata_json"]["message_body"]
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Campaign)) == 1
        assert await session.scalar(select(func.count()).select_from(CampaignRun)) == 0
        assert await session.scalar(select(func.count()).select_from(MessageRecipient)) == 0
        shared = await session.get(MessageTemplate, template.json()["id"])
        assert shared.body == "Hello {{client_name}}"


@pytest.mark.anyio
@pytest.mark.parametrize("channel,strategy", [("telegram", "single"), ("sms", "single"), ("telegram", "telegram_then_sms")])
async def test_personal_offer_draft_accepts_configurable_promotion(database, wizard_api, channel, strategy):
    async with database() as session:
        segment = await add_segment(session)
        master = Master(full_name="Wizard master")
        promotion = Promotion(code="WIZARD100", name_uk="Free visit", name_en="Free visit", discount_percent=100,
                              recipient_offer_only=True)
        session.add_all([master, promotion])
        await session.flush()
        service = BarberService(master_id=master.id, name="Haircut", price=500, duration_minutes=30)
        session.add(service)
        await session.commit()
    response = await wizard_api.post("/api/v1/backoffice/messaging/campaigns", json={
        "name": "Configurable offer", "type": "manual", "channel": channel, "purpose": "marketing",
        "status": "draft", "offer_audience_mode": "segments", "segment_ids": [segment.id],
        "channel_strategy": strategy, "offer_master_id": master.id, "offer_promotion_id": promotion.id,
        "offer_service_ids": [service.id], "master_name_for_message": "Wizard master",
        "metadata_json": {"message_body": "{{master_name}} {{discount_percent}} {{offer_link}}"},
    })
    assert response.status_code == 201, response.text
    assert response.json()["offer_audience_mode"] == "segments"
    assert response.json()["offer_promotion_id"] == promotion.id
    assert response.json()["channel_strategy"] == strategy
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(CampaignRun)) == 0
        assert await session.scalar(select(func.count()).select_from(MessageRecipient)) == 0


@pytest.mark.anyio
async def test_archived_segment_is_rejected_by_real_create_endpoint(database, wizard_api):
    async with database() as session:
        segment = await add_segment(session)
        segment.status = "archived"
        await session.commit()
    response = await wizard_api.post("/api/v1/backoffice/messaging/campaigns", json={
        "name": "Archived audience", "type": "manual", "channel": "telegram", "purpose": "marketing",
        "status": "draft", "segment_ids": [segment.id],
    })
    assert response.status_code == 409, response.text
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Campaign)) == 0
        assert await session.scalar(select(func.count()).select_from(MessageRecipient)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("changes", [
    {"channel": "unknown"}, {"channel_strategy": "unknown"},
    {"segment_ids": [999999]}, {"marketing_frequency_days": 0},
    {"sms_recipients_per_minute": 481}, {"timezone": "Not/AZone"},
])
async def test_invalid_wizard_draft_does_not_persist(database, wizard_api, changes):
    async with database() as session:
        segment = await add_segment(session)
        await session.commit()
    response = await wizard_api.post("/api/v1/backoffice/messaging/campaigns", json={
        "name": "Invalid wizard", "type": "manual", "channel": "telegram", "purpose": "marketing",
        "status": "draft", "segment_ids": [segment.id], "channel_strategy": "single", **changes,
    })
    assert response.status_code in {404, 422}, response.text
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Campaign)) == 0
        assert await session.scalar(select(func.count()).select_from(CampaignRun)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("kind,channel,recipient,body", [
    ("booking_confirmation", "sms", "customer", "Booking {manage_url} {cancel_url}"),
    ("post_visit_review_request", "telegram", "customer", "{{client_name}} {{review_link}}"),
    ("post_visit_review_request", "sms", "customer", "{{client_name}} {{review_link}}"),
    ("master_booking_created", "telegram", "master", "{{customer_name}} {{appointment_date}}"),
])
async def test_notification_wizard_payload_is_persisted_without_delivery(database, wizard_api, kind, channel, recipient, body):
    template = await wizard_api.post("/api/v1/backoffice/messaging/templates", json={
        "name": "Notification template", "channel": channel, "body": body,
    })
    assert template.status_code == 201, template.text
    response = await wizard_api.post("/api/v1/backoffice/messaging/campaigns", json={
        "name": "Notification wizard", "type": kind, "channel": channel,
        "purpose": "review_request" if kind == "post_visit_review_request" else "transactional",
        "status": "draft", "recipient": recipient, "template_id": template.json()["id"],
        "segment_ids": [], "review_platform": "internal" if kind == "post_visit_review_request" else None,
        "metadata_json": {"recipient": recipient, "message_body": body, "schedule_mode": "automated",
                          "quiet_hours_enabled": True, "quiet_hours_from": "21:00", "quiet_hours_to": "09:00"},
    })
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["type"] == kind
    assert saved["recipient"] == recipient
    assert saved["channel"] == channel
    assert saved["status"] == "draft"
    assert saved["metadata_json"]["quiet_hours_from"] == "21:00"
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(CampaignRun)) == 0
        assert await session.scalar(select(func.count()).select_from(MessageRecipient)) == 0


@pytest.mark.anyio
async def test_booking_notification_contract_rejects_missing_action_urls(database, wizard_api):
    response = await wizard_api.post("/api/v1/backoffice/messaging/campaigns", json={
        "name": "Missing URLs", "type": "booking_confirmation", "channel": "sms",
        "purpose": "transactional", "status": "draft", "metadata_json": {"message_body": "Booking confirmed"},
    })
    assert response.status_code == 422, response.text
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Campaign)) == 0
