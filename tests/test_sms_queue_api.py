"""SMS queue configuration and authenticated backoffice contracts."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from test_segments_api import client


@pytest.mark.parametrize("rate", [8.01, 9, 10, 1000, 0, -1])
def test_account_request_rate_cannot_remove_safety_margin(rate):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SMS_CLUB_REQUESTS_PER_SECOND=rate)


def test_recipient_throughput_is_distinct_from_account_http_request_rate():
    config = Settings(_env_file=None, SMS_CLUB_REQUESTS_PER_SECOND=8,
                      SMS_CAMPAIGN_RECIPIENTS_PER_MINUTE=60)
    assert config.sms_club_requests_per_second == 8
    assert config.sms_campaign_recipients_per_minute == 60
    assert config.sms_queue_worker_enabled is True


@pytest.mark.parametrize("path", [
    "/api/v1/backoffice/messaging/sms-queue",
    "/api/v1/backoffice/messaging/sms-queue/jobs",
    "/api/v1/backoffice/messaging/sms-queue/jobs/sandbox-job",
    "/api/v1/backoffice/messaging/campaigns/1/queue",
    "/api/v1/backoffice/messaging/campaigns/1/runs/1/queue",
])
def test_queue_inspection_requires_admin_authentication(client, path):
    assert client.get(path).status_code == 401


def test_cancel_unsent_requires_admin_authentication(client):
    assert client.post("/api/v1/backoffice/messaging/campaigns/1/runs/1/cancel-unsent").status_code == 401


def test_queue_openapi_exposes_operational_results_without_sensitive_payloads(client):
    schema = client.get("/openapi.json").json()
    job_fields = schema["components"]["schemas"]["SmsQueueJobResponse"]["properties"]
    assert {"status", "attempts", "available_at", "error_code", "provider_message_id"} <= set(job_fields)
    assert not {"payload", "message", "body", "context_json", "otp_code", "headers", "token"} & set(job_fields)
    progress = schema["components"]["schemas"]["SmsQueueProgress"]["properties"]
    assert {"counts", "paused", "sms_recipients_per_minute", "estimated_completion_at"} <= set(progress)
