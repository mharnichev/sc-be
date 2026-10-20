"""Online external acquiring and existing COD issuer coexist in one test database."""
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.order import OrderPayment
from app.models.product import Product
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.shop_online_payment import ShopOnlinePaymentRequest
from app.schemas.shop_payments import ManualCODCommand
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import shop_payments, shop_sales, shop_online_payment
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_online_payment_integration import online, prepared, start  # noqa: F401
from tests.test_shop_sales_integration import seed, attach, delivered, execute
from tests import test_shop_checkbox_integration as checkbox
from tests.test_shop_payments_integration import evidence, profile_assertion

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def test_online_invoice_and_cod_settlement_receipt_stock_xz_keep_separate_issuers(
    database, checkout_dependencies, online, monkeypatch,
):
    online_id, online_product, token = await prepared(database)
    cod_id, cod_product = await seed(database, kind="nova_poshta", method="cashOnDelivery")
    stub = checkbox.flags.__wrapped__(monkeypatch)
    profile = dict(settings.shop_checkbox_profile)
    profile["product_tax_codes"] = {str(cod_product): [1]}
    monkeypatch.setattr(settings, "shop_checkbox_profile", profile)
    # The HTTP stand and configured profile must describe the same fiscal goods.
    stub.profile = profile
    monkeypatch.setattr(settings, "shop_payment_manual_policy", dict(
        id="synthetic-cod", issuer="SyntheticBank", account="SyntheticAccount",
        types=["bank_statement", "carrier_settlement"], environment="test"))
    assert (await start(database, online_id, token)).status == "waiting"
    await checkbox.open_shift(database)
    shipment_id, number = await attach(database, cod_id)
    dispatched = await execute(database, cod_id,
        ShopPhysicalCommand(expected_revision=1, evidence="Synthetic COD dispatch"), "handover")
    await delivered(database, shipment_id)
    paid = await checkbox.call(database, shop_payments.command, cod_id,
        ManualCODCommand(expected_revision=dispatched.revision, **profile_assertion(cod=True),
                         evidence=evidence(number)), str(uuid4()), "manual_cod")
    assert paid.payment and paid.payment.method == "cod"
    receipt = await checkbox.sale(database, cod_id)
    assert receipt.acceptance_verified and receipt.is_sent_dps is False
    state = await checkbox.shop_state(database, cod_id)
    completed = await execute(database, cod_id,
        ShopPhysicalCommand(expected_revision=state.revision, evidence="Synthetic COD received"), "complete")
    assert completed.completion
    register = await checkbox.reg(database)
    x = await checkbox.call(database, checkbox.service.register_command,
        checkbox.register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "x")
    assert (await checkbox.process(database, x)).result["totals"]["sales_minor"] == 10000
    register = await checkbox.reg(database)
    z = await checkbox.call(database, checkbox.service.register_command,
        checkbox.register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "z")
    assert (await checkbox.process(database, z)).result["z_report"]["totals"]["sales_minor"] == 10000
    async with database() as session:
        online_order = await shop_sales.load_order(session, online_id)
        cod_order = await shop_sales.load_order(session, cod_id)
        assert shop_online_payment.config_ready()
        assert not online_order.payments and not online_order.shop_checkbox_binding
        assert cod_order.shop_checkbox_binding and not cod_order.shop_online_payment_request
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        online_stock = await session.get(Product, online_product)
        cod_stock = await session.get(Product, cod_product)
        assert (online_stock.stock_quantity, online_stock.reserved_quantity) == (4, 1)
        assert (cod_stock.stock_quantity, cod_stock.reserved_quantity) == (3, 0)
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.product_id == cod_product,
            InventoryMovement.movement_type == InventoryMovementType.shipment)) == 1
    assert online["posts"] == 1 and len(stub.state["receipts"]) == 1
