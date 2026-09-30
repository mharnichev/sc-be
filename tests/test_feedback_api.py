from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api.v1.routes import feedback
from app.schemas.feedback import FeedbackEmailRequest


class Session:
    def __init__(self, owned_order_id=None):
        self.owned_order_id = owned_order_id
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.owned_order_id


def request(ip="127.0.0.1"):
    return Request({"type": "http", "method": "POST", "path": "/feedback/email", "headers": [],
                    "client": (ip, 1234), "server": ("test", 80), "scheme": "http"})


def payload(**overrides):
    values = {"name": "Jane", "email": "jane@example.com", "text": "Please help", "topic": "order"}
    values.update(overrides)
    return FeedbackEmailRequest.model_validate(values)


@pytest.mark.anyio
async def test_feedback_submission_delivers_and_checks_optional_owned_order(monkeypatch):
    delivered = []
    async def send(item):
        delivered.append(item)
    monkeypatch.setattr(feedback.email_notification_service, "send_feedback", send)
    customer = SimpleNamespace(id=7)
    response = await feedback.send_feedback_email(payload(orderReference=42), request(), customer, Session(42))
    assert response.message == "Feedback accepted"
    assert delivered[0].order_id == 42


@pytest.mark.anyio
async def test_feedback_delivery_failure_is_not_reported_as_success(monkeypatch):
    async def fail(_item):
        raise RuntimeError("smtp unavailable")
    monkeypatch.setattr(feedback.email_notification_service, "send_feedback", fail)
    with pytest.raises(HTTPException) as error:
        await feedback.send_feedback_email(payload(), request(), None, Session())
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_feedback_order_reference_requires_authenticated_ownership(monkeypatch):
    with pytest.raises(HTTPException) as unauthorized:
        await feedback.send_feedback_email(payload(orderReference=42), request(), None, Session())
    assert unauthorized.value.status_code == 401
    with pytest.raises(HTTPException) as not_found:
        await feedback.send_feedback_email(payload(orderReference=42), request(), SimpleNamespace(id=8), Session())
    assert not_found.value.status_code == 404


@pytest.mark.anyio
async def test_feedback_source_rate_limit_applies_across_email_addresses(monkeypatch):
    async def accept(_item):
        return None
    monkeypatch.setattr(feedback.email_notification_service, "send_feedback", accept)
    for index in range(10):
        await feedback.send_feedback_email(
            payload(email=f"shop-contact-{index}@example.com"), request("198.51.100.100"), None, Session(),
        )
    with pytest.raises(HTTPException) as error:
        await feedback.send_feedback_email(
            payload(email="shop-contact-10@example.com"), request("198.51.100.100"), None, Session(),
        )
    assert error.value.status_code == 429


def test_feedback_schema_validates_topic_and_order_reference():
    with pytest.raises(ValidationError):
        payload(topic="arbitrary")
    with pytest.raises(ValidationError):
        payload(orderReference=0)
