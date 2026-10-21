from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from app.core.shop_request_size import ShopRequestSizeMiddleware
from app.schemas.order import OrderCreate
from app.services import order as order_module
from app.services.order_documents import digest


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_body_limit_with_missing_or_dishonest_content_length():
    app = FastAPI()
    app.add_middleware(ShopRequestSizeMiddleware)
    calls = []

    @app.post('/api/v1/public/feedback/email')
    async def echo(request: Request):
        calls.append(True)
        return {'size': len(await request.body())}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        good = await client.post('/api/v1/public/feedback/email', content=b'a' * 100)
        assert good.json() == {'size': 100}
        big = await client.post('/api/v1/public/feedback/email', content=b'a' * 65537)
        assert big.status_code == 413
        async def chunks():
            yield b'a' * 40000
            yield b'b' * 40000
        chunked = await client.post('/api/v1/public/feedback/email', content=chunks())
        assert chunked.status_code == 413
        dishonest = await client.post('/api/v1/public/feedback/email', content=b'a' * 65537, headers={'Content-Length': '1'})
        assert dishonest.status_code == 413
        assert len(calls) == 1


def body(**changes):
    return {'firstName': ' Олена ', 'lastName': 'Коваль', 'phoneNumber': '+380671234567',
            'items': [{'product_id': 1, 'quantity': 1}], **changes}


@pytest.mark.anyio
async def test_body_guard_replays_exact_bytes_including_legacy_checkout_whitespace():
    from starlette.responses import Response

    app = FastAPI()
    app.add_middleware(ShopRequestSizeMiddleware)

    @app.post('/api/v1/public/orders')
    @app.post('/api/v1/backoffice/orders')
    async def echo(request: Request):
        return Response(await request.body())

    raw = b'{ "comment" : "<b>legacy</b>", "items": [1], "quantity": "2" }\n'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        result = await client.post('/api/v1/public/orders', content=raw)
        assert result.status_code == 200 and result.content == raw
        boundary = await client.post('/api/v1/public/orders', content=b' ' * 65536)
        assert boundary.status_code == 200 and boundary.content == b' ' * 65536
        backoffice = await client.post('/api/v1/backoffice/orders', content=b' ' * 65537)
        assert backoffice.status_code == 200 and len(backoffice.content) == 65537


@pytest.mark.anyio
@pytest.mark.parametrize('action', ['otp_send', 'otp_verify', 'checkout'])
async def test_existing_source_identity_and_limits_are_preserved(monkeypatch, action):
    from app.services import shop_ingress_limits as ingress

    calls = []
    async def admit(*args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr(ingress, '_admit', admit)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/api/v1/public/orders',
                       'client': ('203.0.113.4', 1234),
                       'headers': [(b'x-forwarded-for', b'198.51.100.1'), (b'x-visitor-id', b'override')]})
    factory = object()
    await ingress.admit_shop_ingress(request, action, session_factory=factory)
    field = {'otp_send': 'otp_source_send_limit', 'otp_verify': 'otp_source_verify_limit',
             'checkout': 'checkout_source_limit'}[action]
    assert calls == [((action, f'shop-ingress:v1:{action}:203.0.113.4',
                       getattr(ingress.settings, field), ingress.settings.source_limit_window_seconds),
                      {'session_factory': factory})]


@pytest.mark.anyio
@pytest.mark.parametrize('email', ['Alice <other@example.org>', '<other@example.org>', 'Alice &lt;other@example.org&gt;'])
@pytest.mark.parametrize('method,path,payload', [
    ('POST', '/feedback/email', {'name': 'Alice', 'text': 'Please help with this question.'}),
    ('PATCH', '/customers/me', {}),
    ('POST', '/orders', {'firstName': 'Alice', 'phoneNumber': '+380671234567',
                       'items': [{'productId': 1, 'quantity': 1}]}),
])
async def test_display_name_email_rejected_over_real_public_routes(monkeypatch, email, method, path, payload):
    from app.api.v1.public.router import router
    from app.core.config import settings
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_customer, get_optional_current_customer

    monkeypatch.setattr(settings, 'shop_checkout_enabled', True)
    monkeypatch.setattr(settings, 'shop_checkout_pilot_enabled', False)
    monkeypatch.setattr(settings, 'app_env', 'test')
    monkeypatch.setattr(settings, 'shared_ingress_limits_enabled', False)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1/public')
    class ForbiddenSession:
        def __getattr__(self, name):
            raise AssertionError('Invalid email reached business database: ' + name)
    async def session():
        yield ForbiddenSession()
    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id=17)
    app.dependency_overrides[get_optional_current_customer] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.request(method, '/api/v1/public' + path, json={**payload, 'email': email})
    assert response.status_code == 422, response.text
    assert any('email' in error['loc'] for error in response.json()['detail'])


@pytest.mark.anyio
async def test_historical_display_name_email_replay_bypasses_new_email_validation(monkeypatch):
    raw = body(email='Alice <other@example.org>')
    legacy = OrderCreate.model_validate(raw)
    assert legacy.email == 'other@example.org'
    canonical = legacy.model_dump(mode='json', by_alias=False)
    canonical['items'] = sorted(canonical['items'], key=lambda item: item['product_id'])
    hashes = []
    snapshot = {'id': 9, 'total_amount': '123.00', 'payment_status': 'unpaid'}
    async def claim(session, key, payload_hash):
        hashes.append(payload_hash)
        return SimpleNamespace(order_id=9, response_json=snapshot)
    monkeypatch.setattr(order_module, 'claim_intent', claim)
    existing = SimpleNamespace(id=9)
    class Session:
        async def execute(self, statement):
            return SimpleNamespace(scalar_one=lambda: existing)
    result = await order_module.OrderService().create_order(Session(), legacy,
        idempotency_key='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', raw_public_payload=raw)
    assert result is existing and result._checkout_response == snapshot
    assert hashes == [digest(canonical)]


@pytest.mark.anyio
async def test_new_public_order_rejects_invalid_raw_quantity_before_business_work():
    raw = body(items=[{'product_id': 1, 'quantity': True}])
    with pytest.raises(HTTPException) as rejected:
        await order_module.OrderService().create_order(object(), OrderCreate.model_validate(raw), raw_public_payload=raw)
    assert rejected.value.status_code == 422


@pytest.mark.anyio
async def test_historical_exact_replay_returns_before_new_stricter_validation(monkeypatch):
    raw = body(comment='x' * 600)
    legacy = OrderCreate.model_validate(raw)
    canonical = legacy.model_dump(mode='json', by_alias=False)
    canonical['items'] = sorted(canonical['items'], key=lambda item: item['product_id'])
    calls = []
    async def claim(session, key, payload_hash):
        calls.append(payload_hash)
        return SimpleNamespace(order_id=9, response_json={'id': 9})
    monkeypatch.setattr(order_module, 'claim_intent', claim)
    existing = SimpleNamespace(id=9)
    class Session:
        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one=lambda: existing)
    result = await order_module.OrderService().create_order(Session(), legacy,
        idempotency_key='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', raw_public_payload=raw)
    assert result is existing and result._checkout_response == {'id': 9}
    assert calls == [digest(canonical)]  # Includes the original whitespace and legacy lengths.


@pytest.mark.anyio
async def test_new_public_order_is_sanitized_after_intent_hashing(monkeypatch):
    raw = body(comment='<b>Доставити ввечері</b>')
    captured = []
    async def validate_directory(self, session, payload):
        captured.append(payload)
        raise HTTPException(400, 'test-stop-before-stock')
    monkeypatch.setattr(order_module.NovaPoshtaDirectory, 'validate_order', validate_directory)
    with pytest.raises(HTTPException):
        await order_module.OrderService().create_order(object(), OrderCreate.model_validate(raw), raw_public_payload=raw)
    assert captured[0].first_name == 'Олена'
    assert captured[0].comment == 'Доставити ввечері'
