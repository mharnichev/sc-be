"""HTTP contract and access-control checks without background workers or network providers."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.main import app


@pytest.fixture
def client():
    async def no_database():
        yield None
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db_session] = no_database
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize(("method", "path", "payload"), [
    ("get", "/api/v1/backoffice/segments", None),
    ("post", "/api/v1/backoffice/segments", {}),
    ("get", "/api/v1/backoffice/segments/1", None),
    ("patch", "/api/v1/backoffice/segments/1", {}),
    ("post", "/api/v1/backoffice/segments/preview", {}),
    ("get", "/api/v1/backoffice/segments/1/members", None),
    ("post", "/api/v1/backoffice/segments/1/archive", None),
    ("post", "/api/v1/backoffice/messaging/campaigns/1/audience-preview", None),
    ("post", "/api/v1/backoffice/messaging/campaigns/1/runs", {}),
    ("get", "/api/v1/backoffice/messaging/campaigns/1/runs", None),
    ("get", "/api/v1/backoffice/messaging/campaigns/1/runs/1", None),
    ("get", "/api/v1/backoffice/messaging/campaigns/1/runs/1/members", None),
])
def test_all_segment_and_run_endpoints_require_backoffice_auth(client, method, path, payload):
    response = client.request(method, path, json=payload)
    assert response.status_code == 401


@pytest.mark.parametrize("scope", ["customer", "master"])
@pytest.mark.parametrize("path", ["/api/v1/backoffice/segments", "/api/v1/backoffice/messaging/campaigns/1/runs"])
def test_customer_and_master_tokens_cannot_inspect_backoffice_audiences(client, scope, path):
    from app.core.security import create_scoped_access_token
    token = create_scoped_access_token("1", scope)
    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_openapi_contract_includes_rules_versions_runs_and_view_filter(client):
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]
    assert "expected_revision" in components["SegmentUpdate"]["properties"]
    assert "exclusions" in components[next(key for key in components if key.startswith("SegmentRules"))]["properties"]
    assert "snapshot_facts" in components["CampaignRunMemberResponse"]["properties"]
    assert "segment_ids" in components["CampaignCreate"]["properties"]
    campaign_query = schema["paths"]["/api/v1/backoffice/messaging/campaigns"]["get"]["parameters"]
    assert any(item["name"] == "view" for item in campaign_query)
    preview = schema["paths"]["/api/v1/backoffice/segments/preview"]["post"]
    assert preview["security"]


@pytest.mark.parametrize("rules", [
    {"conditions": []},
    {"conditions": [{"type": "sql", "expression": "true"}]},
    {"conditions": [{"type": "last_visit_age", "min": 12, "max": 3}]},
    {"conditions": [{"type": "upcoming_booking"}] * 21},
    {"conditions": [{"type": "received_service", "service_ids": [-1], "period": {"last": 7}}]},
])
def test_invalid_rules_are_http_validation_errors(client, rules):
    app.dependency_overrides[get_current_admin_user] = lambda: object()
    response = client.post("/api/v1/backoffice/segments/preview", json={"rules": rules})
    assert response.status_code == 422


@pytest.mark.parametrize("suffix", ["?limit=0", "?limit=201", "?offset=-1", "?evaluated_at=2026-09-06T12:00:00"])
def test_member_pagination_and_evaluation_timestamp_validation(client, suffix):
    app.dependency_overrides[get_current_admin_user] = lambda: object()
    assert client.get("/api/v1/backoffice/segments/1/members" + suffix).status_code == 422
