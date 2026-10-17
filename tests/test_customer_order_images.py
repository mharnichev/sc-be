"""Customer image projection stays independent of shipment/local-sales features."""
from decimal import Decimal
from types import SimpleNamespace

from app.models.order import OrderItem
from app.schemas.order import CustomerOrderResponse
from test_customer_orders_api import order


def test_current_catalog_image_preserves_purchase_snapshot():
    purchase = order()
    product = SimpleNamespace(
        name="Renamed", price=Decimal("99"), image_url="/original.jpg",
        attributes_json={}, images=[], image_variants=[],
    )
    purchase.items[0].product = product
    result = CustomerOrderResponse.from_order(purchase)
    assert result.items[0].product_image_url == "/original.jpg"
    assert (result.items[0].product_name, result.items[0].price, result.items[0].quantity) == ("Pomade", Decimal("9"), 2)
    product.image_url = None
    assert CustomerOrderResponse.from_order(purchase).items[0].product_image_url is None
    purchase.items[0].product = None
    assert CustomerOrderResponse.from_order(purchase).items[0].product_image_url is None


def test_unloaded_product_does_not_trigger_lazy_io():
    purchase = order()
    purchase.items = [OrderItem(id=3, product_id=5, quantity=2, price=Decimal("9"),
        discount_amount=Decimal("0"), product_name="Original", product_sku="P-1")]
    assert CustomerOrderResponse.from_order(purchase).items[0].product_image_url is None


def test_gallery_preferred_variant_and_inactive_images():
    purchase = order()
    purchase.items[0].product = SimpleNamespace(
        image_url="/fallback.jpg", attributes_json={},
        images=[
            SimpleNamespace(id=1, sort_order=0, is_active=False, image_url="/inactive.jpg"),
            SimpleNamespace(id=2, sort_order=1, is_active=True, image_url="/gallery.jpg"),
        ],
        image_variants=[SimpleNamespace(source_url="/gallery.jpg", output_url="/preferred.webp",
            status="succeeded", is_preferred=True)],
    )
    assert CustomerOrderResponse.from_order(purchase).items[0].product_image_url == "/preferred.webp"
