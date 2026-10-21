"""Public shop input admission is shared, persistent, and precedes validation."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.public.router import router
from app.api.v1.routes import feedback
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_customer, get_optional_current_customer
from app.models.shop_ingress_limits import ShopIngressAdmission
from app.services import shop_ingress_limits as ingress

pytestmark = pytest.mark.anyio
ACTIONS = ('feedback', 'review', 'profile', 'search', 'cart')


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def database(monkeypatch):
    url = os.environ.get('SHOP_INGRESS_TEST_DATABASE_URL')
    assert url, 'Set SHOP_INGRESS_TEST_DATABASE_URL to a fresh owned local test DB'
    parsed = make_url(url)
    assert parsed.host in {'127.0.0.1', 'localhost', '::1'}
    assert (parsed.database or '').startswith('sc_shop_r6_ingress_test_')
    schema = 'input_ingress_' + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engines = [create_async_engine(url, connect_args={'server_settings': {'search_path': schema}}) for _ in range(2)]
    async with engines[0].begin() as connection:
        await connection.run_sync(ShopIngressAdmission.__table__.create)
    factories = [async_sessionmaker(engine, expire_on_commit=False) for engine in engines]
    monkeypatch.setattr(settings, 'app_env', 'test')
    monkeypatch.setattr(settings, 'shared_ingress_limits_enabled', True)
    monkeypatch.setattr(settings, 'source_limit_window_seconds', 60)
    monkeypatch.setattr(settings, 'secret_key', 'isolated-shop-input-test-key')
    for action in ACTIONS:
        monkeypatch.setattr(settings, f'{action}_source_limit', 2)
    monkeypatch.setattr(ingress, 'AsyncSessionLocal', factories[0])
    try:
        yield SimpleNamespace(first=factories[0], second=factories[1])
    finally:
        # Retain this uniquely owned schema for diagnosis; never clean an existing stand.
        for engine in engines:
            await engine.dispose()
        await admin.dispose()


def request(host='203.0.113.90', headers=()):
    return Request({'type': 'http', 'method': 'POST', 'path': '/',
                    'client': (host, 12345), 'headers': list(headers)})


async def outcomes(call):
    try:
        await call
        return 200
    except HTTPException as error:
        return error.status_code


async def rows(database):
    async with database.first() as session:
        return (await session.scalars(select(ShopIngressAdmission))).all()


@pytest.mark.parametrize('action', ACTIONS)
async def test_new_source_budgets_serialize_across_two_instances(database, action):
    barrier = asyncio.Barrier(8)
    async def worker(index):
        await barrier.wait()
        factory = database.first if index % 2 else database.second
        return await outcomes(ingress.admit_shop_ingress(request(), action, session_factory=factory))
    results = await asyncio.wait_for(asyncio.gather(*(worker(i) for i in range(8))), 15)
    assert results.count(200) == 2
    assert results.count(429) == 6
    assert len(await rows(database)) == 2
    with pytest.raises(HTTPException) as raised:
        await ingress.admit_shop_ingress(request(), action, session_factory=database.second)
    assert raised.value.detail['code'] == 'ingress_limited'
    assert 1 <= int(raised.value.headers['Retry-After']) <= 60


@pytest.mark.parametrize('action,subject,window', [
    ('feedback', 'email:synthetic@example.com', 30),
    ('feedback', 'customer:12', 30), ('review', 'customer:12', 15),
    ('profile', 'customer:12', 1),
])
async def test_subject_cooldown_has_no_ip_component(database, action, subject, window):
    # Independent source admissions succeed but the same subject remains blocked.
    for host in ('203.0.113.91', '203.0.113.92'):
        await ingress.admit_shop_ingress(request(host), action)
    await ingress.admit_shop_subject(action, subject, session_factory=database.first)
    with pytest.raises(HTTPException) as raised:
        await ingress.admit_shop_subject(action, subject, session_factory=database.second)
    assert raised.value.status_code == 429
    assert 1 <= int(raised.value.headers['Retry-After']) <= window
    await ingress.admit_shop_subject(action, subject + '-different', session_factory=database.second)
    assert all(subject not in repr(vars(row)) for row in await rows(database))


async def test_spoofed_headers_and_rotating_email_do_not_reset_source(database):
    for index in range(3):
        req = request(headers=[(b'x-forwarded-for', f'198.51.100.{index}'.encode()),
                               (b'x-real-ip', f'198.51.100.{index}'.encode()),
                               (b'x-visitor-id', str(uuid4()).encode())])
        code = await outcomes(ingress.admit_shop_ingress(req, 'feedback', session_factory=database.second))
        assert code == (200 if index < 2 else 429)
        if code == 200:
            await ingress.admit_shop_subject('feedback', f'email:synthetic{index}@example.com')
    assert len(await rows(database)) == 4


@pytest.mark.parametrize('environment,enabled,enforced', [
    ('test', False, False), ('local', True, True),
    ('production', False, True), ('staging', False, True),
])
async def test_environment_and_failure_closed(database, monkeypatch, environment, enabled, enforced):
    monkeypatch.setattr(settings, 'app_env', environment)
    monkeypatch.setattr(settings, 'shared_ingress_limits_enabled', enabled)
    def unavailable():
        raise RuntimeError('private database credentials must not leak')
    for operation in (
        ingress.admit_shop_ingress(request(), 'search', session_factory=unavailable),
        ingress.admit_shop_subject('feedback', 'email:test@example.com', session_factory=unavailable),
    ):
        if enforced:
            with pytest.raises(HTTPException) as raised:
                await operation
            assert raised.value.status_code == 503
            assert raised.value.detail == {'code': 'ingress_unavailable', 'message': 'Request admission is temporarily unavailable'}
        else:
            await operation
    assert not await rows(database)


@pytest.fixture
def apps(database, monkeypatch):
    from app.dependencies import shop_input
    async def admission(req, action):
        await ingress.admit_shop_ingress(req, action, session_factory=req.app.state.ingress_factory)
    monkeypatch.setattr(shop_input, 'admit_shop_ingress', admission)
    result = []
    for factory in (database.first, database.second):
        app = FastAPI()
        app.state.ingress_factory = factory
        app.include_router(router, prefix='/api/v1/public')
        async def session_dependency():
            async with database.first() as session:
                yield session
        app.dependency_overrides[get_db_session] = session_dependency
        app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id=17)
        app.dependency_overrides[get_optional_current_customer] = lambda: None
        result.append(app)
    return result


@pytest.mark.parametrize('method,path,body,action', [
    ('POST', '/feedback/email', {}, 'feedback'),
    ('POST', '/products/1/reviews', {}, 'review'),
    ('POST', '/products/reviews/1/comments', {}, 'review'),
    ('PATCH', '/customers/me', {'name': 'x' * 1000}, 'profile'),
    ('POST', '/customers/cart', {'product_id': 1, 'quantity': -1}, 'cart'),
    ('POST', '/customers/wishlist', {'product_id': -1}, 'cart'),
    ('GET', '/products/search?q=' + 'x' * 121, None, 'search'),
])
async def test_public_router_invalid_requests_spend_source_before_schema(database, apps, method, path, body, action):
    for index in range(3):
        app = apps[index % 2]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local.test') as client:
            response = await client.request(method, '/api/v1/public' + path, json=body)
        assert response.status_code == (422 if index < 2 else 429), response.text
    assert [row.action for row in await rows(database)] == [action, action]


async def test_feedback_direct_http_same_email_new_ip_cannot_resend(database, apps, monkeypatch):
    calls = []
    async def sent(message):
        calls.append(message)
    monkeypatch.setattr(feedback.email_notification_service, 'send_feedback', sent)
    body = {'name': 'Synthetic User', 'email': 'synthetic@example.com', 'text': 'Please help with this question.'}
    for index, app in enumerate(apps):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(f'203.0.113.{index + 10}', 1000)), base_url='http://local.test') as client:
            response = await client.post('/api/v1/public/feedback/email', json=body)
        assert response.status_code == (202 if index == 0 else 429), response.text
        if index:
            assert 1 <= int(response.headers['Retry-After']) <= 30
    assert len(calls) == 1


async def test_real_search_route_normalizes_query_before_bound_sql(database, apps, monkeypatch):
    from app.api.v1.routes import products

    captured = []
    async def visibility(_session):
        return SimpleNamespace(visible_product_clause=lambda: True, visible_category_clause=lambda: True)
    monkeypatch.setattr(products.CatalogVisibility, 'load', visibility)
    class CapturingSession:
        async def execute(self, statement):
            captured.append(statement.compile().params)
            raise HTTPException(409, 'Captured query before execution')
    async def session_dependency():
        yield CapturingSession()
    apps[0].dependency_overrides[get_db_session] = session_dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url='http://local.test') as client:
        response = await client.get('/api/v1/public/products/search', params={'q': '<b>hello</b>\u200b'})
    assert response.status_code == 409, response.text
    patterns = [value for value in captured[0].values() if isinstance(value, str)]
    assert patterns and all(value == '%hello%' for value in patterns)


async def test_subject_admission_works_while_only_business_connection_is_held(database, apps, monkeypatch):
    from app.core.database import engine as business_engine

    assert ingress.ingress_engine.pool is not business_engine.pool
    engine = create_async_engine(os.environ['SHOP_INGRESS_TEST_DATABASE_URL'],
                                 pool_size=1, max_overflow=0, pool_timeout=0.2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with database.first() as session:
        schema = await session.scalar(text('SELECT current_schema()'))
    admission_engine = create_async_engine(os.environ['SHOP_INGRESS_TEST_DATABASE_URL'],
        pool_size=1, max_overflow=0, pool_timeout=0.2,
        connect_args={'server_settings': {'search_path': schema}})
    admission_factory = async_sessionmaker(admission_engine, expire_on_commit=False)
    monkeypatch.setattr(ingress, 'AsyncSessionLocal', admission_factory)
    apps[0].state.ingress_factory = admission_factory
    held = []
    async def authenticated():
        async with factory() as session:
            await session.execute(text('SELECT 1'))
            held.append(session)
            yield SimpleNamespace(id=781)
            held.pop()
    apps[0].dependency_overrides[get_optional_current_customer] = authenticated
    sent = []
    async def send(message):
        assert held and held[0].in_transaction()
        sent.append(message)
    monkeypatch.setattr(feedback.email_notification_service, 'send_feedback', send)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url='http://local.test') as client:
            response = await asyncio.wait_for(client.post('/api/v1/public/feedback/email', json={
                'name': 'Synthetic User', 'email': 'synthetic@example.com',
                'text': 'Please help with this question.',
            }), timeout=5)
        assert response.status_code == 202, response.text
        assert len(sent) == 1
        assert len(await rows(database)) == 3  # Source, email, authenticated customer.
    finally:
        await admission_engine.dispose()
        await engine.dispose()


async def test_real_ingress_migration_upgrade_and_downgrade_preserve_original_actions(database):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.core.database import Base

    schema = 'input_migration_' + uuid4().hex
    engine = create_async_engine(os.environ['SHOP_INGRESS_TEST_DATABASE_URL'])
    def migration(name):
        path = Path(__file__).resolve().parents[1] / 'alembic' / 'versions' / name
        spec = importlib.util.spec_from_file_location(name[:-3], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    old = migration('0129_shop_ingress_limits.py')
    new = migration('0139_shop_input_guards.py')
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            def migrate(sync_connection):
                operations = Operations(MigrationContext.configure(sync_connection, opts={'target_metadata': Base.metadata}))
                old.op = new.op = operations
                old.upgrade()
                new.upgrade()
            await connection.run_sync(migrate)
            await connection.execute(text("INSERT INTO shop_ingress_admissions (source_key, action) VALUES (:key, 'feedback'), (:key, 'checkout')"), {'key': 'a' * 64})
            assert await connection.scalar(text('SELECT count(*) FROM shop_ingress_admissions')) == 2
            await connection.run_sync(lambda _: new.downgrade())
            assert (await connection.execute(text('SELECT action FROM shop_ingress_admissions'))).scalars().all() == ['checkout']
            async with connection.begin_nested() as savepoint:
                with pytest.raises(IntegrityError) as raised:
                    await connection.execute(text("INSERT INTO shop_ingress_admissions (source_key, action) VALUES (:key, 'feedback')"), {'key': 'b' * 64})
                assert 'ck_shop_ingress_admissions_action' in str(raised.value)
                await savepoint.rollback()
            # Only the admission migration's table exists; no business schema was needed.
            assert (await connection.execute(text('SELECT tablename FROM pg_tables WHERE schemaname = :schema'), {'schema': schema})).scalars().all() == ['shop_ingress_admissions']
    finally:
        await engine.dispose()


@pytest.mark.parametrize('params', [
    {'priceMin': '-1'}, {'priceMin': 'NaN'}, {'priceMin': 'Infinity'},
    {'priceMin': '1.234'}, {'priceMax': '-1'}, {'priceMax': 'NaN'},
    {'priceMax': 'Infinity'}, {'priceMax': '1.234'},
    {'priceMin': '10', 'priceMax': '5'},
    {'price_min': '-1'}, {'price_min': '10', 'price_max': '5'},
])
async def test_category_price_filters_reject_before_business_query(database, apps, params):
    class ForbiddenSession:
        def __getattr__(self, name):
            raise AssertionError('Invalid price must not reach a business database query: ' + name)
    async def session_dependency():
        yield ForbiddenSession()
    apps[0].dependency_overrides[get_db_session] = session_dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url='http://local.test') as client:
        response = await client.get('/api/v1/public/categories/synthetic/products', params=params)
    assert response.status_code == 422, response.text
    assert [row.action for row in await rows(database)] == ['search']


@pytest.mark.parametrize('query', ['ab', '<b></b>', '<script>hello</script>', '\u200b\u200c\u200d'])
async def test_search_minimum_applies_after_sanitization(database, apps, query):
    class ForbiddenSession:
        def __getattr__(self, name):
            raise AssertionError('Invalid search must not reach a business database query: ' + name)
    async def session_dependency():
        yield ForbiddenSession()
    apps[0].dependency_overrides[get_db_session] = session_dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url='http://local.test') as client:
        response = await client.get('/api/v1/public/products/search', params={'q': query})
    assert response.status_code == 422, response.text
    assert [row.action for row in await rows(database)] == ['search']
