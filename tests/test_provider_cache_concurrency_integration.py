"""Provider-cache races in isolated PostgreSQL schemas; all providers are stubbed."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.google_business_review_cache import GoogleBusinessReviewCache
from app.models.product import Product
from app.models.shop import DeliveryCache
from app.schemas.product import DeliveryListResponse
from app.services.google_business_reviews import GoogleBusinessReviewsError, GoogleBusinessReviewsService
from app.services.nova_poshta import NovaPoshtaService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


pytestmark = pytest.mark.anyio
NOVA_KEY = "np:cities:synthetic city"


async def bounded(operation):
    return await asyncio.wait_for(operation, timeout=10)


@pytest.fixture
async def cache_database(database):
    """Reuse the guarded local schema, with enough connections for twenty held reads."""
    async with database() as session:
        schema = await bounded(session.scalar(text("SELECT current_schema()")))
    engine = create_async_engine(
        database.kw["bind"].url, pool_size=20, max_overflow=0,
        connect_args={"server_settings": {
            "search_path": schema, "statement_timeout": "5000", "lock_timeout": "5000",
        }},
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await bounded(engine.dispose())


@pytest.fixture(autouse=True)
def synthetic_settings(monkeypatch):
    monkeypatch.setattr(settings, "nova_poshta_api_key", "synthetic-test-key")
    monkeypatch.setattr(settings, "google_business_reviews_cache_ttl_days", 3)


def model_for(kind):
    return DeliveryCache if kind == "nova" else GoogleBusinessReviewCache


def payload_for(kind, marker):
    if kind == "nova":
        return [{"Ref": f"synthetic-{marker}", "Description": "Synthetic city"}]
    return {
        "average_rating": 4.5, "total_review_count": 1,
        "items": [{"review_id": f"synthetic-{marker}", "comment": "Synthetic review"}],
    }


def service_for(kind):
    return NovaPoshtaService() if kind == "nova" else GoogleBusinessReviewsService()


def stub_fetch(monkeypatch, service, kind, callback):
    monkeypatch.setattr(service, "_call_api" if kind == "nova" else "_fetch_reviews", callback)


async def request(service, session, kind, *, force=False):
    if kind == "nova":
        return await bounded(service.cities(session, "  SyNtHeTiC CiTy  "))
    return await bounded(service.get_reviews(session, force_refresh=force))


async def seed_cache(database, kind, *, fresh):
    now = datetime.now(UTC)
    async with database() as session:
        if kind == "nova":
            cache = DeliveryCache(
                cache_key=NOVA_KEY, payload_json={"items": payload_for(kind, "old")},
                expires_at=now + timedelta(hours=2) if fresh else now - timedelta(hours=2),
            )
        else:
            cache = GoogleBusinessReviewCache(
                source=GoogleBusinessReviewsService.cache_source, payload=payload_for(kind, "old"),
                fetched_at=now - timedelta(days=1 if fresh else 4),
            )
        session.add(cache)
        await bounded(session.commit())
        return cache.id


async def cache_rows(database, kind):
    model = model_for(kind)
    async with database() as session:
        result = await bounded(session.execute(select(model.__table__).order_by(model.id)))
        return [tuple(row) for row in result.all()]


def assert_response(response, kind, marker, *, stale=False):
    if kind == "nova":
        items, cached, updated_at = response
        dto = DeliveryListResponse(items=items, cached=cached, updated_at=updated_at)
        assert dto.items == payload_for(kind, marker)
        assert dto.updated_at is not None
    else:
        assert response.items[0].review_id == f"synthetic-{marker}"
        assert response.items[0].comment == "Synthetic review"
        assert (response.average_rating, response.total_review_count, response.stale) == (4.5, 1, stale)


@pytest.mark.parametrize("kind", ["nova", "google"])
@pytest.mark.parametrize("clients", [2, 20])
async def test_cold_concurrent_provider_cache_recovers_one_persisted_winner(
    cache_database, monkeypatch, kind, clients,
):
    model = model_for(kind)
    all_fetched = asyncio.Event()
    all_insert_lookups = asyncio.Event()
    winner_committed = asyncio.Event()
    fetches, pids = [], []
    misses = [[] for _ in range(clients)]
    rollbacks = [0] * clients
    insert_lookups = []

    async def attempt(index):
        async with cache_database() as session:
            pids.append(await bounded(session.scalar(text("SELECT pg_backend_pid()"))))
            execute, commit, rollback = session.execute, session.commit, session.rollback

            async def observed_execute(statement, *args, **kwargs):
                result = await execute(statement, *args, **kwargs)
                descriptions = getattr(statement, "column_descriptions", ())
                if descriptions and descriptions[0].get("entity") is model and len(misses[index]) < (2 if kind == "nova" else 1):
                    frozen = result.freeze()
                    assert frozen().scalar_one_or_none() is None
                    misses[index].append(True)
                    if kind == "nova" and len(misses[index]) == 2:
                        insert_lookups.append(index)
                        if len(insert_lookups) == clients:
                            all_insert_lookups.set()
                        await bounded(all_insert_lookups.wait())
                        if index != 0:
                            await bounded(winner_committed.wait())
                    return frozen()
                return result

            async def observed_commit():
                result = await commit()
                if index == 0:
                    winner_committed.set()
                return result

            async def observed_rollback():
                rollbacks[index] += 1
                return await rollback()

            monkeypatch.setattr(session, "execute", observed_execute)
            monkeypatch.setattr(session, "commit", observed_commit)
            monkeypatch.setattr(session, "rollback", observed_rollback)
            service = service_for(kind)

            async def fetch(**kwargs):
                if kind == "nova":
                    assert kwargs == {
                        "model_name": "Address", "called_method": "getCities",
                        "method_properties": {"FindByString": "SyNtHeTiC CiTy", "Limit": "50"},
                    }
                fetches.append(index)
                if len(fetches) == clients:
                    all_fetched.set()
                await bounded(all_fetched.wait())
                if kind == "google" and index != 0:
                    await bounded(winner_committed.wait())
                return payload_for(kind, index)

            stub_fetch(monkeypatch, service, kind, fetch)
            try:
                response = await request(service, session, kind)
            except Exception as error:
                return error
            assert session.is_active
            assert await bounded(session.scalar(text("SELECT 1"))) == 1
            await bounded(session.commit())
            return response

    before = datetime.now(UTC)
    responses = await asyncio.wait_for(asyncio.gather(
        *(attempt(index) for index in range(clients)),
    ), timeout=20)
    after = datetime.now(UTC)
    assert len(set(pids)) == clients
    assert sorted(fetches) == list(range(clients))
    assert all(len(observations) == (2 if kind == "nova" else 1) for observations in misses)
    assert all(not isinstance(response, Exception) for response in responses), responses
    assert rollbacks == [0] + [1] * (clients - 1)
    assert len(await cache_rows(cache_database, kind)) == 1
    async with cache_database() as session:
        persisted = (await bounded(session.scalars(select(model)))).one()
        for index, response in enumerate(responses):
            assert_response(response, kind, 0)
            if kind == "nova":
                assert response[1] is (index != 0)
                if index != 0:
                    assert response[2] == persisted.updated_at
            else:
                assert response.fetched_at == persisted.fetched_at
                assert response.cache_expires_at == persisted.fetched_at + timedelta(days=3)
        if kind == "nova":
            assert persisted.cache_key == NOVA_KEY
            assert persisted.payload_json == {"items": payload_for(kind, 0)}
            assert before + timedelta(hours=24) <= persisted.expires_at <= after + timedelta(hours=24)
        else:
            assert persisted.source == GoogleBusinessReviewsService.cache_source
            assert persisted.payload == payload_for(kind, 0)
            assert before <= persisted.fetched_at <= after


@pytest.mark.parametrize("kind", ["nova", "google"])
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "expired"])
async def test_provider_cache_fresh_hit_or_expired_refresh_preserves_contract(
    cache_database, monkeypatch, kind, fresh,
):
    cache_id = await seed_cache(cache_database, kind, fresh=fresh)
    before_rows = await cache_rows(cache_database, kind)
    service, fetches = service_for(kind), []

    async def fetch(**_kwargs):
        assert not fresh, "Fresh cache must not call its provider"
        fetches.append(True)
        return payload_for(kind, "new")

    stub_fetch(monkeypatch, service, kind, fetch)
    before = datetime.now(UTC)
    async with cache_database() as session:
        response = await request(service, session, kind)
    after = datetime.now(UTC)
    assert_response(response, kind, "old" if fresh else "new")
    assert len(fetches) == (0 if fresh else 1)
    if fresh:
        assert await cache_rows(cache_database, kind) == before_rows
    async with cache_database() as session:
        persisted = (await bounded(session.scalars(select(model_for(kind))))).one()
        assert persisted.id == cache_id
        if kind == "nova":
            assert response[1] is fresh
            if not fresh:
                assert before + timedelta(hours=24) <= persisted.expires_at <= after + timedelta(hours=24)
        else:
            assert response.fetched_at == persisted.fetched_at
            assert response.cache_expires_at == persisted.fetched_at + timedelta(days=3)
            if not fresh:
                assert before <= persisted.fetched_at <= after


@pytest.mark.parametrize("has_stale", [False, True])
@pytest.mark.parametrize("status", [502, 503])
async def test_nova_provider_failure_propagates_without_stale_fallback(
    cache_database, monkeypatch, has_stale, status,
):
    if has_stale:
        await seed_cache(cache_database, "nova", fresh=False)
    before = await cache_rows(cache_database, "nova")
    service = NovaPoshtaService()

    async def fetch(**_kwargs):
        raise HTTPException(status_code=status, detail="Synthetic provider failure")

    stub_fetch(monkeypatch, service, "nova", fetch)
    async with cache_database() as session:
        with pytest.raises(HTTPException) as error:
            await request(service, session, "nova")
        assert (error.value.status_code, error.value.detail) == (status, "Synthetic provider failure")
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert await cache_rows(cache_database, "nova") == before


@pytest.mark.parametrize("cache_state", ["absent", "expired", "fresh"])
async def test_nova_missing_api_key_keeps_empty_or_fresh_cache_behavior(
    cache_database, monkeypatch, cache_state,
):
    if cache_state != "absent":
        await seed_cache(cache_database, "nova", fresh=cache_state == "fresh")
    before = await cache_rows(cache_database, "nova")
    monkeypatch.setattr(settings, "nova_poshta_api_key", "")
    service = NovaPoshtaService()

    async def fetch(**_kwargs):
        raise AssertionError("Missing API key must not call the provider")

    stub_fetch(monkeypatch, service, "nova", fetch)
    async with cache_database() as session:
        response = await request(service, session, "nova")
    if cache_state == "fresh":
        assert_response(response, "nova", "old")
        assert response[1] is True
    else:
        assert response == ([], False, None)
    assert await cache_rows(cache_database, "nova") == before


@pytest.mark.parametrize("has_stale", [False, True])
async def test_google_provider_failure_returns_stale_or_explicit_fallback(
    cache_database, monkeypatch, has_stale,
):
    if has_stale:
        await seed_cache(cache_database, "google", fresh=False)
    before = await cache_rows(cache_database, "google")
    service = GoogleBusinessReviewsService()
    monkeypatch.setattr(service, "fallback_payload", payload_for("google", "fallback"))

    async def fetch():
        raise GoogleBusinessReviewsError("Synthetic provider failure")

    stub_fetch(monkeypatch, service, "google", fetch)
    async with cache_database() as session:
        response = await request(service, session, "google")
    assert_response(response, "google", "old" if has_stale else "fallback", stale=True)
    if has_stale:
        async with cache_database() as session:
            persisted = (await bounded(session.scalars(select(GoogleBusinessReviewCache)))).one()
            assert response.fetched_at == persisted.fetched_at
            assert response.cache_expires_at == persisted.fetched_at + timedelta(days=3)
    else:
        assert response.fetched_at is None
        assert response.cache_expires_at is None
    assert await cache_rows(cache_database, "google") == before


async def test_google_force_refresh_fetches_even_when_cache_is_fresh(cache_database, monkeypatch):
    cache_id = await seed_cache(cache_database, "google", fresh=True)
    service, fetches = GoogleBusinessReviewsService(), []

    async def fetch():
        fetches.append(True)
        return payload_for("google", "forced")

    stub_fetch(monkeypatch, service, "google", fetch)
    async with cache_database() as session:
        response = await request(service, session, "google", force=True)
    assert fetches == [True]
    assert_response(response, "google", "forced")
    async with cache_database() as session:
        persisted = (await bounded(session.scalars(select(GoogleBusinessReviewCache)))).one()
        assert persisted.id == cache_id
        assert persisted.payload == payload_for("google", "forced")
        assert response.fetched_at == persisted.fetched_at


@pytest.mark.parametrize("kind", ["nova", "google"])
@pytest.mark.parametrize("database_error", ["other_unique", "check_violation"])
async def test_provider_cache_does_not_suppress_unrelated_database_integrity_errors(
    cache_database, monkeypatch, kind, database_error,
):
    await seed_cache(cache_database, kind, fresh=False)
    before = await cache_rows(cache_database, kind)
    service = service_for(kind)
    async with cache_database() as session:
        execute, lookup_count = session.execute, 0

        async def inject_after_cache_read(statement, *args, **kwargs):
            nonlocal lookup_count
            result = await execute(statement, *args, **kwargs)
            descriptions = getattr(statement, "column_descriptions", ())
            if descriptions and descriptions[0].get("entity") is model_for(kind):
                lookup_count += 1
                if lookup_count == (2 if kind == "nova" else 1):
                    if database_error == "other_unique":
                        session.add(AdminUser(
                            id=1, email=f"synthetic-{uuid4().hex}@example.test", hashed_password="not-used",
                        ))
                    else:
                        session.add(Product(
                            name="Invalid synthetic stock", slug=f"invalid-stock-{uuid4().hex}",
                            price=Decimal("100.00"), stock_quantity=-1, reserved_quantity=0,
                        ))
            return result

        async def fetch(**_kwargs):
            return payload_for(kind, "new")

        monkeypatch.setattr(session, "execute", inject_after_cache_read)
        stub_fetch(monkeypatch, service, kind, fetch)
        with pytest.raises(IntegrityError) as error:
            await request(service, session, kind)
        assert error.value.orig.sqlstate == ("23505" if database_error == "other_unique" else "23514")
        await bounded(session.rollback())
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert await cache_rows(cache_database, kind) == before
