"""History/detail eager loads images in fresh sessions without async lazy IO."""
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.routes import orders
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.schemas.order import OrderCreate
from app.services.order import OrderService
from test_inventory_integration import anyio_backend, checkout_dependencies, database  # noqa: F401


@pytest.mark.anyio
async def test_history_detail_images_and_owner_boundary(database):
    async with database() as session:
        owner = Customer(phone="+380501112233")
        other = Customer(phone="+380501112234")
        product = Product(name="Current name", slug="order-image", price=99,
                          image_url="/current.jpg", stock_quantity=2)
        session.add_all([owner, other, product])
        await session.flush()
        purchase = Order(customer_id=owner.id, customer_name="Jane", customer_phone=owner.phone,
                         total_amount=18, items=[OrderItem(product_id=product.id, quantity=2, quantity_from_stock=2, price=9,
                         product_name="Historical", product_sku="OLD")])
        session.add(purchase)
        await session.commit()
        order_id, product_id = purchase.id, product.id
    async with database() as session:
        history = await orders.list_my_orders(1, 20, owner, session)
        assert history.items[0].callback_requested is None
        assert history.items[0].items[0].product_image_url is None
    async with database() as session:
        detail = await orders.get_my_order(order_id, owner, session)
        line = detail.items[0]
        assert (line.product_name, line.price, line.quantity) == ("Historical", Decimal("9"), 2)
        assert line.product_image_url is None
        with pytest.raises(HTTPException) as error:
            await orders.get_my_order(order_id, other, session)
        assert error.value.status_code == 404
        assert detail.shipments == []
        assert not hasattr(detail, "payments")
        assert not hasattr(detail, "handovers")
        assert not hasattr(detail, "sales_actions")
        assert not hasattr(detail, "external_id")
        assert not hasattr(detail, "external_sync_status")
        assert not hasattr(detail, "external_sync_error")
    async with database() as session:
        product = await session.get(Product, product_id)
        product.image_url = None
        await session.commit()
    async with database() as session:
        assert (await orders.get_my_order(order_id, owner, session)).items[0].product_image_url is None


@pytest.mark.anyio
async def test_checkout_snapshots_survive_catalog_edits_for_guest_and_customer(database, checkout_dependencies):
    async with database() as session:
        owner = Customer(phone="+380501112235")
        product = Product(name="Ordered name", slug="receipt-snapshot", price=Decimal("19.00"),
                          image_url="/ordered.jpg", package_size="100 ml", stock_quantity=3)
        session.add_all([owner, product])
        await session.commit()
        owner_id, product_id = owner.id, product.id

    async with database() as session:
        guest = await OrderService().create_order(session, OrderCreate(
            firstName="Guest", phoneNumber="+380501112236",
            items=[{"productId": product_id, "quantity": 1}],
        ), idempotency_key=str(uuid4()))
        assert guest._checkout_response["callback_requested"] is False
        assert guest._checkout_response["items"][0]["product_image_url"] == "/ordered.jpg"
        guest_id = guest.id

    async with database() as session:
        owner = await session.get(Customer, owner_id)
        customer_order = await OrderService().create_order(session, OrderCreate(
            firstName="Jane", phoneNumber=owner.phone, callbackRequested=True,
            items=[{"productId": product_id, "quantity": 1}],
        ), current_customer=owner, idempotency_key=str(uuid4()))
        assert customer_order._checkout_response["callback_requested"] is True
        assert customer_order._checkout_response["items"][0]["product_variant"] == "100 ml"
        customer_order_id = customer_order.id

    async with database() as session:
        product = await session.get(Product, product_id)
        product.image_url = "/changed.jpg"
        product.package_size = "200 ml"
        await session.commit()

    async with database() as session:
        guest = await session.get(Order, guest_id)
        assert guest.callback_requested is False
        guest_item = (await session.scalars(select(OrderItem).where(OrderItem.order_id == guest_id))).one()
        assert guest_item.product_image_url == "/ordered.jpg"
        assert guest_item.product_variant == "100 ml"
        owner = await session.get(Customer, owner_id)
        detail = await orders.get_my_order(customer_order_id, owner, session)
        assert detail.callback_requested is True
        assert detail.items[0].product_image_url == "/ordered.jpg"
        assert detail.items[0].product_variant == "100 ml"
