"""Online checkout contracts using HTTP and disposable local PostgreSQL schemas."""
from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.v1.routes import orders, shop_promotions
from app.core.database import get_db_session
from app.core.config import settings
from app.core.security import create_scoped_token
from app.models.customer import Customer
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderIntent, OrderItem, PreliminaryDocument
from app.models.product import Product
from app.models.shop import CustomerCartItem
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
from app.utils.customer_sessions import issue_customer_token
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.fixture
def local_network_only(monkeypatch):
    """Never allow checkout regression tests to contact a provider."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guard(address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("Checkout test attempted a nonlocal connection")

    def connect(sock, address):
        guard(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        guard(address)
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture
def checkout_app(database, monkeypatch):
    monkeypatch.setattr(settings, "shop_checkout_enabled", True)
    app = FastAPI()
    app.include_router(orders.public_router, prefix="/orders")
    app.include_router(shop_promotions.public_router, prefix="/promotions")

    async def isolated_session():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = isolated_session
    return app


@pytest.fixture
async def checkout_client(local_network_only, checkout_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=checkout_app), base_url="http://checkout.test") as client:
        yield client


async def seed(database, *, stock=10, promo=False):
    async with database() as session:
        product = Product(name="Checkout product", slug="checkout-product", price=Decimal("100.00"),
                          stock_quantity=stock, reserved_quantity=0, allow_backorder=False)
        customers = [Customer(name="Synthetic customer", phone=f"+38050111223{index}") for index in range(2)]
        session.add_all([product, *customers])
        if promo:
            session.add(ShopPromotion(name="One per customer", code="ONEPERACCOUNT",
                trigger=ShopPromotionTrigger.promocode, discount_type=ShopPromotionDiscountType.percent,
                discount_value=Decimal("10"), applies_to_all_products=True, usage_limit_per_customer=1))
        await session.commit()
        return product.id, [(customer.id, customer.phone, issue_customer_token(customer)) for customer in customers]


def body(product_id, *, phone="+380501112230", **extra):
    return {"firstName": "Synthetic", "phoneNumber": phone,
            "items": [{"productId": product_id, "quantity": 1}], **extra}


def headers(token=None, *, key=None):
    return {"Idempotency-Key": key or str(uuid4()), **({"Authorization": "Bearer " + token} if token else {})}


async def persisted_state(database):
    async with database() as session:
        return {
            model.__tablename__: (await session.execute(
                select(model.__table__).order_by(*model.__table__.primary_key.columns)
            )).mappings().all()
            for model in (Customer, Product, Order, OrderItem, InventoryMovement, OrderIntent,
                          PreliminaryDocument, CustomerCartItem)
        }


async def test_closed_checkout_preserves_carts_stock_and_accepted_replays(checkout_client, database, monkeypatch):
    product_id, customers = await seed(database, stock=2)
    async with database() as session:
        session.add_all([
            CustomerCartItem(customer_id=customer_id, product_id=product_id, quantity=1)
            for customer_id, _, _ in customers
        ])
        await session.commit()
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    initial = await persisted_state(database)
    closed_response = {"detail": {"code": "shop_checkout_closed",
                                  "message": "Shop checkout is temporarily unavailable"}}
    for token in (None, customers[0][2]):
        request = body(product_id, phone=customers[0][1], shop_checkout_enabled=True)
        for request_headers in ({}, headers(token)):
            # Retain the verified identity even when exercising a missing key.
            if token:
                request_headers["Authorization"] = "Bearer " + token
            response = await checkout_client.post("/orders?SHOP_CHECKOUT_ENABLED=true", json=request,
                headers={**request_headers, "X-Shop-Checkout-Enabled": "true"})
            assert response.status_code == 503, response.text
            assert response.json() == closed_response
    assert await persisted_state(database) == initial

    # Accepted guest and account orders also stay readable while POST replays close.
    monkeypatch.setattr(settings, "shop_checkout_enabled", True)
    accepted = []
    for token in (None, customers[0][2]):
        request = body(product_id, phone=customers[0][1])
        request_headers = headers(token)
        response = await checkout_client.post("/orders", json=request, headers=request_headers)
        assert response.status_code == 201, response.text
        accepted.append((request, request_headers, response.json()))
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    committed = await persisted_state(database)
    for request, request_headers, _ in accepted:
        replay = await checkout_client.post("/orders", json=request, headers=request_headers)
        assert replay.status_code == 503, replay.text
        assert replay.json() == closed_response
    history = await checkout_client.get("/orders/me", headers=headers(customers[0][2]))
    assert history.status_code == 200, history.text
    assert [order["id"] for order in history.json()["items"]] == [accepted[1][2]["id"]]
    tracked = await checkout_client.get("/orders/tracking/" + accepted[0][2]["guest_tracking_url"].rsplit("/", 1)[1])
    assert tracked.status_code == 200, tracked.text
    assert tracked.json() == {"order_id": accepted[0][2]["id"], "shipments": []}
    assert await persisted_state(database) == committed


async def test_guest_auth_checkout_replay_ownership_and_server_prices(checkout_client, database):
    product_id, customers = await seed(database)
    customer_id, phone, token = customers[0]
    key = str(uuid4())
    request = body(product_id, phone=phone, price="0.01", total_amount="0.01", customer_id=customer_id)
    guest = await checkout_client.post("/orders", json=request, headers=headers(key=key))
    assert guest.status_code == 201, guest.text
    guest_data = guest.json()
    assert Decimal(guest_data["total_amount"]) == Decimal("100.00")
    assert guest_data["guest_tracking_url"]
    replay = await checkout_client.post("/orders", json=request, headers=headers(key=key))
    assert replay.json() == guest_data
    changed = await checkout_client.post("/orders", json={**request, "comment": "changed"}, headers=headers(key=key))
    assert changed.status_code == 409
    own = await checkout_client.post("/orders", json=request, headers=headers(token, key=key))
    assert own.status_code == 201, own.text
    assert own.json()["id"] != guest_data["id"]
    assert own.json()["guest_tracking_url"] is None
    assert (await checkout_client.post("/orders", json=request, headers=headers(token, key=key))).json() == own.json()
    history = await checkout_client.get("/orders/me", headers=headers(token))
    assert [item["id"] for item in history.json()["items"]] == [own.json()["id"]]
    for order_id in (guest_data["id"], own.json()["id"]):
        foreign = await checkout_client.get(f"/orders/me/{order_id}", headers=headers(customers[1][2]))
        assert foreign.status_code == 404
    for invalid in ("invalid", create_scoped_token(customer_id, "customer", datetime.now(UTC)-timedelta(seconds=1))):
        denied = await checkout_client.post("/orders", json=request, headers=headers(invalid, key=key))
        assert denied.status_code == 401
    async with database() as session:
        assert (await session.get(Order, guest_data["id"])).customer_id is None
        assert (await session.get(Order, own.json()["id"])).customer_id == customer_id
        assert await session.scalar(select(func.count()).select_from(OrderIntent)) == 2
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 2
        assert (await session.get(Product, product_id)).reserved_quantity == 2


@pytest.mark.parametrize("guest_first", [False, True])
async def test_authenticated_promo_limit_follows_account_not_recipient(checkout_client, database, guest_first):
    product_id, customers = await seed(database, promo=True)
    _, phone, token = customers[0]
    request = body(product_id, phone=phone, promoCode="ONEPERACCOUNT")
    # Unverified guest contact data must not consume the account owner's limit.
    if guest_first:
        guest = await checkout_client.post("/orders", json=request, headers=headers())
        assert guest.status_code == 201, guest.text
    own = await checkout_client.post("/orders", json=request, headers=headers(token))
    assert own.status_code == 201, own.text
    assert Decimal(own.json()["total_amount"]) == Decimal("90.00")
    different_recipient = body(product_id, phone="+380509998877", promoCode="ONEPERACCOUNT")
    denied = await checkout_client.post("/orders", json=different_recipient, headers=headers(token))
    assert denied.status_code == 400, denied.text
    assert denied.json()["detail"] == "Customer promo code limit reached"
    quote = await checkout_client.post("/promotions/quote", headers=headers(token), json={
        "items": request["items"], "promoCode": request["promoCode"], "customerPhone": "+380509998877",
    })
    assert quote.status_code == 400
    # Another verified account may send to the same recipient.
    other = await checkout_client.post("/orders", json=request, headers=headers(customers[1][2]))
    assert other.status_code == 201, other.text
    # Guest checkout continues to enforce the existing recipient-phone limit.
    repeat_guest = await checkout_client.post("/orders", json=request, headers=headers())
    assert repeat_guest.status_code == 400
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 2 + int(guest_first)
        assert (await session.get(Product, product_id)).reserved_quantity == 2 + int(guest_first)


@pytest.mark.parametrize("authenticated", [(False, False), (False, True), (True, True)],
                         ids=["guest-guest", "guest-auth", "auth-auth"])
async def test_http_simultaneous_last_unit(checkout_client, checkout_app, database, authenticated):
    product_id, customers = await seed(database, stock=1)
    async with database() as session:
        session.add_all([
            CustomerCartItem(customer_id=customer_id, product_id=product_id, quantity=1)
            for customer_id, _, _ in customers
        ])
        await session.commit()

    # Hold the first request at HTTP ingress until the other request arrives.
    arrived = 0
    both_arrived = asyncio.Event()

    @checkout_app.middleware("http")
    async def ingress_barrier(request, call_next):
        nonlocal arrived
        if request.method == "POST" and request.url.path == "/orders" and arrived < 2:
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await asyncio.wait_for(both_arrived.wait(), timeout=5)
        return await call_next(request)

    requests = [body(product_id, phone=customer[1], firstName=f"Synthetic {('Alpha', 'Beta')[index]}")
                for index, customer in enumerate(customers)]
    request_headers = [headers(customer[2] if is_authenticated else None)
                       for customer, is_authenticated in zip(customers, authenticated)]
    assert requests[0]["phoneNumber"] != requests[1]["phoneNumber"]
    assert request_headers[0]["Idempotency-Key"] != request_headers[1]["Idempotency-Key"]
    responses = await asyncio.wait_for(asyncio.gather(*(
        checkout_client.post("/orders", json=request, headers=request_header)
        for request, request_header in zip(requests, request_headers)
    )), timeout=15)
    assert arrived == 2
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 201)
    loser = 1 - winner
    insufficient_stock = {"detail": f"Insufficient stock for product {product_id}; backorders are disabled"}
    assert responses[loser].json() == insufficient_stock
    winning_response = responses[winner].json()

    async with database() as session:
        order = (await session.scalars(select(Order))).one()
        assert order.id == winning_response["id"]
        assert order.customer_id == (customers[winner][0] if authenticated[winner] else None)
        assert (order.first_name, order.customer_phone) == (
            requests[winner]["firstName"], requests[winner]["phoneNumber"],
        )
        assert Decimal(order.total_amount) == Decimal("100.00")
        line = (await session.scalars(select(OrderItem))).one()
        assert (line.order_id, line.product_id, line.quantity, line.quantity_from_stock, line.quantity_to_order) == (
            order.id, product_id, 1, 1, 0,
        )
        movement = (await session.scalars(select(InventoryMovement))).one()
        assert (movement.order_id, movement.order_item_id, movement.product_id, movement.movement_type,
                movement.quantity, movement.on_hand_delta, movement.reserved_delta) == (
            order.id, line.id, product_id, InventoryMovementType.reservation, 1, 0, 1,
        )
        intent = (await session.scalars(select(OrderIntent))).one()
        assert (intent.order_id, intent.response_json) == (order.id, winning_response)
        document = (await session.scalars(select(PreliminaryDocument))).one()
        assert (document.order_id, document.version) == (order.id, 1)
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (1, 1)
        carts = list(await session.scalars(select(CustomerCartItem).order_by(CustomerCartItem.customer_id)))
        assert [(cart.customer_id, cart.product_id, cart.quantity) for cart in carts] == [
            (customer[0], product_id, 1) for index, customer in enumerate(customers)
            if index != winner or not authenticated[index]
        ]

    committed = await persisted_state(database)
    replay = await checkout_client.post("/orders", json=requests[winner], headers=request_headers[winner])
    assert replay.status_code == 201, replay.text
    assert replay.json() == winning_response
    assert await persisted_state(database) == committed

    retry_headers = {**request_headers[loser], "Idempotency-Key": str(uuid4())}
    assert retry_headers["Idempotency-Key"] != request_headers[loser]["Idempotency-Key"]
    retry = await checkout_client.post("/orders", json=requests[loser], headers=retry_headers)
    assert retry.status_code == 409, retry.text
    assert retry.json() == insufficient_stock
    assert await persisted_state(database) == committed


async def test_http_concurrent_same_key_creates_one_order(checkout_client, database):
    product_id, customers = await seed(database)
    key = str(uuid4())
    responses = await asyncio.wait_for(asyncio.gather(*(
        checkout_client.post("/orders", json=body(product_id), headers=headers(customers[0][2], key=key))
        for _ in range(2)
    )), 15)
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
        assert (await session.get(Product, product_id)).reserved_quantity == 1


async def test_account_promo_limit_serializes_different_products_and_recipients(checkout_client, database):
    first_id, customers = await seed(database, promo=True)
    async with database() as session:
        second = Product(name="Second checkout product", slug="second-checkout-product", price=Decimal("100.00"),
                         stock_quantity=10, reserved_quantity=0, allow_backorder=False)
        session.add(second)
        await session.commit()
        second_id = second.id
    responses = await asyncio.wait_for(asyncio.gather(*(
        checkout_client.post("/orders", json=body(product_id, phone=phone, promoCode="ONEPERACCOUNT"),
                             headers=headers(customers[0][2]))
        for product_id, phone in ((first_id, "+380501112230"), (second_id, "+380509998877"))
    )), 15)
    assert sorted(response.status_code for response in responses) == [201, 400]
    rejected = next(response for response in responses if response.status_code == 400)
    assert rejected.json()["detail"] == "Customer promo code limit reached"
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryMovement)) == 1
        products = (await session.execute(select(Product))).scalars().all()
        assert sum(product.reserved_quantity for product in products) == 1
