"""Backoffice payment origin comes from verified ledger facts, never card selection."""
import pytest
from app.models.order import Order
from app.schemas.order import BackofficeOrderResponse, OrderSummaryResponse
from app.services import shop_sales, mono_checkbox_poll
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_online_payment_integration import online, prepared, start  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return 'asyncio'


async def test_online_origin_is_exposed_after_verification_in_list_and_detail(database, checkout_dependencies, online):
    identifier, _, token = await prepared(database)
    async with database() as session:
        order = await shop_sales.load_order(session, identifier)
        assert order.payment_method == 'onlineCard' and order.payment_channel is None
        assert OrderSummaryResponse.model_validate(order).payment_channel is None
    await start(database, identifier, token)
    online['status'] = 'success'
    assert await mono_checkbox_poll.poll_once(database) == {'checked': 1, 'blocked': 0}
    async with database() as session:
        order = await shop_sales.load_order(session, identifier)
        assert order.payment_method == 'card'
        assert order.payment_channel == 'online_monobank'
        assert OrderSummaryResponse.model_validate(order).payment_channel == 'online_monobank'
        assert BackofficeOrderResponse.model_validate(order).payment_channel == 'online_monobank'
        assert not order.shop_mono_checkbox_receipt  # Paid origin does not claim an issued receipt.
        confirmation = order.shop_payment_confirmation
        confirmation.snapshot = {**confirmation.snapshot, 'original_payment_method': 'cash'}
        assert order.payment_channel is None  # An unverified/tampered snapshot cannot label money.


def test_ordinary_card_or_checkout_choice_does_not_establish_online_payment():
    for method in ['card', 'onlineCard', 'cashOnDelivery', 'monobank_qr']:
        order = Order(channel='shop', simulation=False, payment_method=method)
        assert order.payment_channel is None
