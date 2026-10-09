"""Public response allowlist, without changing newsletter identity policy."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.routes import blog as routes
from app.core.database import get_db_session
from app.models.blog import BlogSubscriptionStatus


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def application(monkeypatch):
    subscription = SimpleNamespace(email='reader@example.com', status=BlogSubscriptionStatus.subscribed,
        subscribed_at=datetime(2026, 1, 1, tzinfo=UTC), unsubscribed_at=None,
        unsubscribe_token='synthetic-private-capability')
    calls = []

    class Service:
        async def subscribe(self, _session, data, **kwargs):
            calls.append(('subscribe', data, kwargs))
            return subscription

        async def unsubscribe(self, _session, **kwargs):
            calls.append(('unsubscribe', kwargs))
            return subscription

        async def get_by_token(self, _session, token):
            calls.append(('token', token))
            return subscription if token == subscription.unsubscribe_token else None

        async def get_by_email(self, _session, email):
            calls.append(('email', email))
            return subscription

    monkeypatch.setattr(routes, 'service', Service())
    app = FastAPI()
    app.include_router(routes.public_router, prefix='/public/blog')

    async def database():
        yield object()

    app.dependency_overrides[get_db_session] = database
    return app, calls, subscription


@pytest.mark.anyio
@pytest.mark.parametrize(('method', 'path', 'payload', 'status'), [
    ('POST', '/subscriptions', {'email': 'reader@example.com'}, 201),
    ('POST', '/subscribe', {'email': 'reader@example.com'}, 201),
    ('POST', '/unsubscribe', {'token': 'synthetic-private-capability'}, 200),
    ('DELETE', '/subscriptions', {'token': 'synthetic-private-capability'}, 200),
    ('DELETE', '/subscriptions/synthetic-private-capability', None, 200),
    ('GET', '/subscriptions/status?email=reader@example.com', None, 200),
    ('GET', '/subscriptions/status?token=synthetic-private-capability', None, 200),
    ('GET', '/subscriptions/status?token=wrong-token&email=reader@example.com', None, 200),
])
async def test_public_aliases_return_status_without_disclosing_capability(application, method, path, payload, status):
    app, calls, subscription = application
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.request(method, '/public/blog' + path, json=payload)
    assert response.status_code == status
    assert response.json() == {'email': 'reader@example.com', 'status': 'subscribed', 'is_subscribed': True,
                               'subscribed_at': '2026-01-01T00:00:00Z', 'unsubscribed_at': None}
    assert subscription.unsubscribe_token not in response.text
    if 'wrong-token' in path:
        assert calls == [('token', 'wrong-token'), ('email', 'reader@example.com')]
    if method == 'DELETE' or path == '/unsubscribe':
        assert calls[0][0] == 'unsubscribe'
        assert calls[0][1]['token'] == subscription.unsubscribe_token


def test_public_response_schema_has_no_bearer_capability_field(application):
    app, _, _ = application
    schema = app.openapi()['components']['schemas']['BlogSubscriptionPublicResponse']
    assert set(schema['properties']) == {'email', 'status', 'is_subscribed', 'subscribed_at', 'unsubscribed_at'}
