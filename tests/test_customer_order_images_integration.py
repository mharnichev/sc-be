"""History/detail eager loads images in fresh sessions without async lazy IO."""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.v1.routes import orders
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from test_inventory_integration import anyio_backend, database  # noqa: F401


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
        assert history.items[0].items[0].product_image_url == "/current.jpg"
    async with database() as session:
        detail = await orders.get_my_order(order_id, owner, session)
        line = detail.items[0]
        assert (line.product_name, line.price, line.quantity) == ("Historical", Decimal("9"), 2)
        assert line.product_image_url == "/current.jpg"
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
