from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.models.blog import BlogSubscription
from app.models.blog import BlogSubscriptionStatus
from app.schemas.blog import BlogSubscriptionPublicResponse, BlogSubscriptionUnsubscribeRequest
from app.services.brevo import BrevoContactSyncService
from app.services.blog import BlogSubscriptionService


def test_blog_subscription_email_is_normalized() -> None:
    service = BlogSubscriptionService()

    assert service.normalize_email("  Reader@Example.COM ") == "reader@example.com"


def test_unsubscribe_request_requires_email_or_token() -> None:
    with pytest.raises(ValidationError):
        BlogSubscriptionUnsubscribeRequest()


def test_public_response_marks_subscribed_state() -> None:
    response = BlogSubscriptionPublicResponse(
        email="reader@example.com",
        status=BlogSubscriptionStatus.subscribed,
        is_subscribed=True,
        subscribed_at=None,
        unsubscribed_at=None,
        unsubscribe_token="token-value",
    )

    assert response.is_subscribed is True


class RecordingBrevoContactSyncService(BrevoContactSyncService):
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def _request_json(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, path, payload))
        return {}


def blog_subscription() -> BlogSubscription:
    now = datetime.now(UTC)
    return BlogSubscription(
        email="reader@example.com",
        status=BlogSubscriptionStatus.subscribed,
        source="blog_modal",
        language="uk",
        unsubscribe_token="unsubscribe-token",
        first_subscribed_at=now,
        subscribed_at=now,
        metadata_json={},
    )


@pytest.mark.anyio
async def test_brevo_sync_is_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "brevo_sync_enabled", False)
    monkeypatch.setattr(settings, "brevo_api_key", "api-key")
    service = RecordingBrevoContactSyncService()

    await service.sync_subscribed_contact(blog_subscription())

    assert service.requests == []


@pytest.mark.anyio
async def test_brevo_subscribe_sync_creates_or_updates_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "brevo_sync_enabled", True)
    monkeypatch.setattr(settings, "brevo_api_key", "api-key")
    monkeypatch.setattr(settings, "brevo_contact_list_id", 42)
    service = RecordingBrevoContactSyncService()

    await service.sync_subscribed_contact(blog_subscription())

    assert service.requests == [
        (
            "POST",
            "/contacts",
            {
                "email": "reader@example.com",
                "emailBlacklisted": False,
                "updateEnabled": True,
                "listIds": [42],
            },
        )
    ]


@pytest.mark.anyio
async def test_brevo_unsubscribe_sync_blacklists_and_unlinks_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "brevo_sync_enabled", True)
    monkeypatch.setattr(settings, "brevo_api_key", "api-key")
    monkeypatch.setattr(settings, "brevo_contact_list_id", 42)
    service = RecordingBrevoContactSyncService()

    await service.sync_unsubscribed_contact(blog_subscription())

    assert service.requests == [
        (
            "PUT",
            "/contacts/reader%40example.com",
            {
                "emailBlacklisted": True,
                "unlinkListIds": [42],
            },
        )
    ]
