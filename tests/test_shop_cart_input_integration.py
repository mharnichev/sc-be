"""The additive cart limit also holds for concurrent direct API requests."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.v1.routes import customers
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_customer
from app.models.customer import Customer
from app.models.product import Product
from app.models.shop import CustomerCartItem
from tests.test_inventory_integration import database, anyio_backend  # noqa: F401


@pytest.mark.anyio
async def test_concurrent_additions_cannot_exceed_999(database, monkeypatch):
    monkeypatch.setattr(settings, 'app_env', 'test')
    monkeypatch.setattr(settings, 'shared_ingress_limits_enabled', False)
    async with database() as session:
        customer = Customer(name='Synthetic Buyer', phone='+380671234567')
        product = Product(name='Synthetic product', slug='input-test-product', price=Decimal('100.00'),
                          stock_quantity=1500, reserved_quantity=0, allow_backorder=False)
        session.add_all([customer, product])
        await session.commit()
        customer_id, product_id = customer.id, product.id
    app = FastAPI()
    app.include_router(customers.public_router, prefix='/customers')
    async def sessions():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_current_customer] = lambda: SimpleNamespace(id=customer_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://local.test') as client:
        responses = await asyncio.wait_for(asyncio.gather(*(
            client.post('/customers/cart', json={'product_id': product_id, 'quantity': 600}) for _ in range(2)
        )), timeout=10)
        assert sorted(response.status_code for response in responses) == [201, 422], [r.text for r in responses]
        for invalid in [True, 1000, 1.5, '2']:
            response = await client.post('/customers/cart', json={'product_id': product_id, 'quantity': invalid})
            assert response.status_code == 422
    async with database() as session:
        quantities = (await session.execute(select(CustomerCartItem.quantity).where(
            CustomerCartItem.customer_id == customer_id, CustomerCartItem.product_id == product_id,
        ))).scalars().all()
        assert quantities == [600]
