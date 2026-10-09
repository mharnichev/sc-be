"""Public subscriber proof and analytics tested through HTTP against isolated PG."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app.api.v1.routes import blog
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.blog import BlogBrevoSyncJob, BlogSubscription, BlogSubscriptionEvent, BlogSubscriptionEventType as Event, BlogSubscriptionStatus as State
from app.services.blog import brevo_contact_sync_service
from tests.test_segments_integration import anyio_backend, database


def newsletter_app(database):
    app = FastAPI()
    app.include_router(blog.public_router, prefix="/blog")
    app.include_router(blog.backoffice_router, prefix="/admin/blog")
    async def db_session():
        async with database() as session:
            await session.execute(text("SELECT set_config('TimeZone', 'UTC', true)"))
            yield session
    app.dependency_overrides[get_db_session] = db_session
    app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=99)
    return app


def assert_private_response(response, secret=None):
    assert not {"unsubscribe_token", "confirmation_token"}.intersection(response.json())
    if secret:
        assert secret not in response.text


@pytest.mark.anyio
async def test_newsletter_personal_proof_no_email_fallback_and_public_redaction(database, monkeypatch):
    sync_calls = []
    async def subscribed(item):
        sync_calls.append((item.email, "subscribed"))
    async def unsubscribed(item):
        sync_calls.append((item.email, "unsubscribed"))
    monkeypatch.setattr(brevo_contact_sync_service, "sync_subscribed_contact", subscribed)
    monkeypatch.setattr(brevo_contact_sync_service, "sync_unsubscribed_contact", unsubscribed)
    async with AsyncClient(transport=ASGITransport(app=newsletter_app(database)), base_url="http://test") as client:
        fresh = await client.post("/blog/subscriptions", json={"email": "owner@example.com", "source": "website"})
        assert fresh.status_code == 201
        assert_private_response(fresh)
        repeated = await client.post("/blog/subscriptions", json={"email": "owner@example.com"})
        assert repeated.status_code == 201
        async with database() as session:
            subscription = (await session.scalars(select(BlogSubscription))).one()
            subscription_id, proof = subscription.id, subscription.unsubscribe_token
            assert await session.scalar(select(func.count()).select_from(BlogSubscriptionEvent)) == 1
        for payload, expected in (({"email": "owner@example.com"}, 422),
                                  ({"email": "owner@example.com", "token": "incorrect-personal-token"}, 404),
                                  ({"email": "different@example.com", "token": proof}, 404)):
            response = await client.post("/blog/unsubscribe", json=payload)
            assert response.status_code == expected
        async with database() as session:
            with pytest.raises(HTTPException) as error:
                await blog.service.unsubscribe(session, email="owner@example.com")
            assert error.value.status_code == 422
            assert (await session.get(BlogSubscription, subscription_id)).status == State.subscribed
        response = await client.post("/blog/unsubscribe", json={"email": "OWNER@example.com", "token": proof})
        assert response.status_code == 200
        assert response.json()["is_subscribed"] is False
        assert_private_response(response, proof)
        repeated_unsubscribe = await client.post("/blog/unsubscribe", json={"token": proof})
        assert repeated_unsubscribe.status_code == 200
        assert_private_response(repeated_unsubscribe, proof)
        async with database() as session:
            prior_version = (await session.get(BlogBrevoSyncJob, subscription_id)).desired_version
            prior_events = await session.scalar(select(func.count()).select_from(BlogSubscriptionEvent))
            assert prior_events == 2
        previous_calls = list(sync_calls)
        for payload in ({"email": "owner@example.com"}, {"email": "owner@example.com", "confirmation_token": "incorrect-personal-token"}, {"email": "owner@example.com", "confirmation_token": "неверный-токен"}):
            response = await client.post("/blog/subscribe", json=payload)
            assert response.status_code == 403
        assert sync_calls == previous_calls
        async with database() as session:
            subscription = await session.get(BlogSubscription, subscription_id)
            assert subscription.status == State.unsubscribed
            assert subscription.unsubscribed_at is not None
            assert await session.scalar(select(func.count()).select_from(BlogSubscriptionEvent)) == prior_events
            assert (await session.get(BlogBrevoSyncJob, subscription_id)).desired_version == prior_version
        response = await client.post("/blog/subscribe", json={"email": "owner@example.com", "confirmation_token": proof})
        assert response.status_code == 201 and response.json()["is_subscribed"] is True
        assert_private_response(response, proof)
        async with database() as session:
            kinds = list((await session.scalars(select(BlogSubscriptionEvent.event_type).order_by(BlogSubscriptionEvent.id))).all())
            assert kinds == [Event.subscribed, Event.unsubscribed, Event.resubscribed]
            assert (await session.get(BlogBrevoSyncJob, subscription_id)).desired_version == prior_version + 1
        assert sync_calls[-1] == ("owner@example.com", "subscribed")
        repeat = await client.post("/blog/subscriptions", json={"email": "owner@example.com"})
        assert repeat.status_code == 201
        async with database() as session:
            assert await session.scalar(select(func.count()).select_from(BlogSubscriptionEvent)) == 3


@pytest.mark.anyio
async def test_newsletter_current_state_period_events_and_kyiv_date_are_independent(database):
    historical = datetime(2026, 1, 1, tzinfo=UTC)
    observed = datetime(2026, 7, 1, 21, 15, tzinfo=UTC)  # July 2, 00:15 Kyiv.
    async with database() as session:
        active = BlogSubscription(email="active@example.com", unsubscribe_token="active-private-proof",
                                  status=State.subscribed, first_subscribed_at=historical, subscribed_at=historical, source="website")
        opted_out = BlogSubscription(email="out@example.com", unsubscribe_token="out-private-proof",
                                     status=State.unsubscribed, first_subscribed_at=historical,
                                     subscribed_at=historical, unsubscribed_at=observed, source="archive")
        session.add_all([active, opted_out])
        await session.flush()
        session.add_all([
            BlogSubscriptionEvent(subscription_id=active.id, event_type=Event.subscribed, occurred_at=historical, source="archive"),
            BlogSubscriptionEvent(subscription_id=active.id, event_type=Event.resubscribed, occurred_at=observed, source="website"),
            BlogSubscriptionEvent(subscription_id=opted_out.id, event_type=Event.unsubscribed, occurred_at=observed, source="website"),
        ])
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=newsletter_app(database)), base_url="http://test") as client:
        response = await client.get("/admin/blog/statistics", params={
            "period_start": "2026-07-01T20:00:00+00:00", "period_end": "2026-07-02T20:00:00+00:00",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["current_subscriber_counts"] == {"total": 2, "active": 1, "unsubscribed": 1}
        assert data["period_event_counts"] == {"resubscribed": 1, "unsubscribed": 1}
        assert (data["total_subscribers"], data["active_subscribers"], data["unsubscribed_subscribers"]) == (2, 1, 1)
        assert (data["subscribe_events"], data["unsubscribe_events"], data["net_growth"]) == (1, 1, 0)
        assert data["by_date"] == [{"date": "2026-07-02", "subscribed": 1, "unsubscribed": 1, "net_growth": 0}]
        assert "Europe/Kyiv" in data["metric_definitions"]["by_date"]
        assert "Current" in data["metric_definitions"]["current_subscriber_counts"]
        assert "Events" in data["metric_definitions"]["period_event_counts"]


@pytest.mark.anyio
@pytest.mark.parametrize("start,end,expected", [
    ("2024-01-01T00:00:00+02:00", "2024-12-31T23:59:59+02:00", 200),
    ("2024-01-01T00:00:00+02:00", "2025-01-01T00:00:00+02:00", 422),
    ("2026-01-01T00:00:00", "2026-01-02T00:00:00+02:00", 422),
    ("2026-01-01T00:00:00+02:00", "2026-01-02T00:00:00", 422),
    ("2026-01-03T00:00:00+02:00", "2026-01-02T00:00:00+02:00", 422),
])
async def test_newsletter_analytics_period_validation_is_http_contract(database, start, end, expected):
    async with AsyncClient(transport=ASGITransport(app=newsletter_app(database)), base_url="http://test") as client:
        response = await client.get("/admin/blog/statistics", params={"period_start": start, "period_end": end})
    assert response.status_code == expected
