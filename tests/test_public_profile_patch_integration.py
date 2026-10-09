"""Public profile write boundaries with real customer JWTs and isolated PostgreSQL."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1.routes import customers as routes
from app.core.database import get_db_session
from app.core.security import create_scoped_access_token
from app.models.customer import Customer
from app.schemas.auth import CustomerUpdate
from tests.test_segments_integration import anyio_backend, database  # noqa: F401


@pytest.fixture
async def profile(database):
    async with database() as session:
        customers = [Customer(phone=f"+38050000000{i}", name=f"Profile {i}",
                              imported_total_spent=Decimal("500.00")) for i in (1, 2)]
        session.add_all(customers)
        await session.commit()
        return [c.id for c in customers]


@pytest.fixture
async def client(database):
    app = FastAPI()
    app.include_router(routes.public_router)

    async def session_dependency():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = session_dependency
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://profile.test") as http:
        yield http


def headers(customer_id, scope="customer"):
    return {"Authorization": "Bearer " + create_scoped_access_token(subject=customer_id, scope=scope)}


@pytest.mark.anyio
@pytest.mark.parametrize("field,value", [
    ("imported_total_spent", 0), ("imported_last_visit_at", "2099-01-01T12:00:00Z"),
    ("imported_is_new_client", True), ("is_active", False),
    ("phone_verified_at", "2099-01-01T12:00:00Z"),
    ("first_visit_completed_at", "2099-01-01T12:00:00Z"), ("id", 999999),
])
async def test_public_profile_cannot_write_internal_fields(database, profile, client, field, value):
    response = await client.patch("/me", json={field: value, "surname": "Allowed"}, headers=headers(profile[0]))
    assert response.status_code == 200, response.text
    async with database() as session:
        customer = await session.get(Customer, profile[0])
        assert customer.imported_total_spent == Decimal("500.00")
        assert customer.imported_last_visit_at is None
        assert customer.imported_is_new_client is False
        assert customer.is_active is True
        assert customer.phone_verified_at is None
        assert customer.first_visit_completed_at is None
        assert customer.surname == "Allowed"
        assert len((await session.scalars(select(Customer))).all()) == 2
        other = await session.get(Customer, profile[1])
        assert other.surname is None


@pytest.mark.anyio
async def test_public_profile_preserves_ordinary_updates_omission_and_nullable_clear(database, profile, client):
    rejected = await client.patch("/me", json={"phone": "+380500000003", "name": "Unapplied"}, headers=headers(profile[0]))
    assert rejected.status_code == 422
    async with database() as session:
        customer = await session.get(Customer, profile[0])
        assert customer.phone == "+380500000001" and customer.name == "Profile 1"
    changes = {"name": "Changed", "surname": "Profile", "email": "synthetic@example.com",
               "birthday": "1990-01-02", "phone": "+380500000001", "notes": "Existing public field"}
    response = await client.patch("/me", json=changes, headers=headers(profile[0]))
    assert response.status_code == 200, response.text
    for field, value in changes.items():
        assert response.json()[field] == value
    empty = await client.patch("/me", json={}, headers=headers(profile[0]))
    assert empty.status_code == 200
    for field, value in changes.items():
        assert empty.json()[field] == value
    clear = await client.patch("/me", json={k: None for k in ["name", "surname", "email", "birthday", "notes"]}, headers=headers(profile[0]))
    assert clear.status_code == 200
    assert all(clear.json()[k] is None for k in ["name", "surname", "email", "birthday", "notes"])
    async with database() as session:
        other = await session.get(Customer, profile[1])
        assert other.name == "Profile 2" and other.phone == "+380500000002"


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [None, "admin", "admin_refresh"])
async def test_public_profile_requires_customer_scope(database, profile, client, scope):
    response = await client.patch("/me", json={"name": "Rejected"}, headers=headers(profile[0], scope) if scope else {})
    assert response.status_code == 401
    async with database() as session:
        assert (await session.get(Customer, profile[0])).name == "Profile 1"


def test_backoffice_profile_schema_retains_internal_updates():
    changes = {"imported_total_spent": Decimal("20.00"), "imported_is_new_client": True,
               "imported_last_visit_at": datetime(2026, 1, 1, tzinfo=UTC), "is_active": False}
    assert CustomerUpdate(**changes).model_dump(exclude_unset=True) == changes
