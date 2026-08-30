from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.v1.routes import messaging as messaging_routes
from app.dependencies.common import PaginationParams
from app.main import app
from app.models.messaging import Campaign, CampaignType, MessageDeliveryStatus
from app.schemas.messaging import CampaignCreate, CampaignRecipient, CampaignUpdate


def campaign_create(**changes) -> CampaignCreate:
    data = {
        "name": "Recipient API campaign",
        "type": CampaignType.manual,
        "metadata_json": {},
    }
    data.update(changes)
    return CampaignCreate(**data)


def test_campaign_recipient_is_an_explicit_openapi_contract() -> None:
    schema = TestClient(app).get("/openapi.json").json()

    campaign_properties = schema["components"]["schemas"]["CampaignResponse"]["properties"]
    assert campaign_properties["recipient"]["$ref"].endswith("/CampaignRecipient")
    for path in ("/api/v1/backoffice/messaging/campaigns", "/api/v1/backoffice/messaging/sms-campaigns"):
        parameters = schema["paths"][path]["get"]["parameters"]
        assert any(parameter["name"] == "recipient" for parameter in parameters)


def test_campaign_write_data_persists_explicit_and_legacy_master_recipient() -> None:
    explicit = messaging_routes.campaign_write_data(
        campaign_create(recipient=CampaignRecipient.master)
    )
    legacy = messaging_routes.campaign_write_data(
        campaign_create(metadata_json={"recipient": "barber", "trigger": "booking_created"})
    )

    assert "recipient" not in explicit
    assert explicit["metadata_json"]["recipient"] == "master"
    assert legacy["metadata_json"] == {"recipient": "master", "trigger": "booking_created"}


def test_campaign_update_preserves_existing_recipient_when_not_supplied() -> None:
    campaign = Campaign(
        name="Master campaign",
        type=CampaignType.manual,
        metadata_json={"recipient": "master", "trigger": "booking_created"},
    )

    data = messaging_routes.campaign_write_data(CampaignUpdate(name="Renamed campaign"), campaign=campaign)

    assert data["name"] == "Renamed campaign"
    assert data["metadata_json"]["recipient"] == "master"
    assert data["metadata_json"]["trigger"] == "booking_created"


@pytest.mark.anyio
async def test_sms_campaign_list_applies_api_recipient_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def list_campaigns(_session, *, stmt, page, page_size):
        captured["stmt"] = stmt
        return [], 0

    monkeypatch.setattr(messaging_routes.campaign_repo, "list", list_campaigns)

    response = await messaging_routes.list_sms_campaigns(
        PaginationParams(),
        None,
        CampaignRecipient.master,
        object(),
        SimpleNamespace(),
    )

    sql = str(
        captured["stmt"].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert response.total == 0
    assert "metadata_json" in sql
    assert "master" in sql and "barber" in sql


@pytest.mark.anyio
async def test_campaign_delivery_counts_include_customer_master_and_schedule_deliveries() -> None:
    class RowsResult:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class SequenceSession:
        def __init__(self):
            self.rows = [
                [(1, MessageDeliveryStatus.sent, 2), (1, MessageDeliveryStatus.failed, 1)],
                [(1, MessageDeliveryStatus.delivered, 1)],
                [(1, 3, 1, 1)],
            ]

        async def execute(self, _statement):
            return RowsResult(self.rows.pop(0))

    counts = await messaging_routes.campaign_delivery_counts(SequenceSession(), [1])

    assert counts == {1: (7, 2)}
