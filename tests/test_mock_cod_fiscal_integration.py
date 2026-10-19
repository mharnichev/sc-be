"""Local COD provenance through manual/ETTN receipts, parcel returns and X/Z."""
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.models.fiscal import FiscalDocument, MockReturnReceipt
from app.models.mock_cod import MockCODConfirmation, MockShipmentReturnReceipt
from app.models.order import Order, OrderPayment
from app.models.product import Product
from app.schemas.fiscal import MockShiftCommand
from app.schemas.fiscal_returns import MockReportCreate
from app.schemas.mock_cod import MockCODPaymentCreate, MockCarrierCommand, MockParcelReceiptCreate, MockReturnShipmentCreate
from app.schemas.shipment import ShipmentCreate
from app.schemas.order import OrderCreate
from app.services.order import OrderService
from app.services import fiscal, fiscal_returns, fiscal_automation, mock_cod, shipments
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product
from tests.test_shipments_integration import ADDRESS, command as shipment_command, PACK
from tests.test_fiscal_integration import actor, preview, submit, approval, command as fiscal_command
from tests.test_fiscal_returns_integration import create as create_return, refund, receive, return_preview
from tests.test_fiscal_automation_integration import preview as auto_preview, arm, run

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    for flag in ('checkbox_mock_enabled', 'checkbox_ettn_mock_enabled', 'local_sales_simulation_enabled', 'np_mock_shipments_enabled'):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, 'app_env', 'test')


@pytest.mark.parametrize('strategy', ['manual', 'automation'])
async def test_shop_cod_receipt_partial_ttn_return_and_reports(database, checkout_dependencies, strategy):
    product = await add_product(database, stock=5)
    pid = product.id
    async with database() as session:
        original = await OrderService().create_order(session, OrderCreate(firstName='Mock customer', phoneNumber='+380990000001',
            items=[{'product_id': pid, 'quantity': 2}]))
        original.fulfillment_method, original.shipping_payload_json = 'nova_poshta', ADDRESS.copy()
        await session.commit()
        oid = original.id
    async with database() as session:
        order = await shipments.create(session, await actor(session), oid,
            ShipmentCreate(expected_revision=1, packaging={**PACK, 'cod_amount_minor': 20000, 'declared_value_minor': 20000}), str(uuid4()))
    sid = order.shipments[0].id
    order = await shipment_command(database, oid, sid, order.revision, 'handover', evidence='MOCK carrier acceptance')
    order = await shipment_command(database, oid, sid, order.revision, mock_status='delivered')
    key = str(uuid4())
    payload = MockCODPaymentCreate(expected_revision=order.revision, provider_event_id='MOCK-COD-'+uuid4().hex,
        evidence='Explicit isolated mock COD collection', confirm=True)
    async with database() as session:
        order = await mock_cod.confirm_cod(session, await actor(session), oid, sid, payload, key)
        assert order.payment_status == 'simulated_paid'
        persisted = await session.get(Order, oid)
        assert persisted.channel == 'shop' and persisted.simulation is False
        await fiscal.shift(session, await actor(session), fiscal.REGISTER,
            MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
    async with database() as session:
        assert (await fiscal_returns.reconciliation(session, await actor(session))).buckets['payments_without_receipts']
        replay = await mock_cod.confirm_cod(session, await actor(session), oid, sid, payload, key)
        assert replay == order
    if strategy == 'manual':
        sale = await fiscal_command(database, await submit(database, oid, approval(await preview(database, oid))), 'process')
    else:
        project = await run(database, await arm(database, await auto_preview(database, order, sid)), 'process')
        project = await run(database, project, 'trigger')
        async with database() as session:
            sale = await fiscal.get_document(session, await actor(session), project.fiscal_document_id)
    assert sale.snapshot['cod']['provider_event_id'] == payload.provider_event_id
    assert sale.snapshot['amount_minor'] == 20000
    async with database() as session:
        persisted = await session.get(Order, oid)
        returned = await mock_cod.create_return_shipment(session, await actor(session), oid, sid,
            MockReturnShipmentCreate(expected_revision=persisted.revision, evidence='Customer mock return accepted by carrier',
                confirm=True, items=[{'order_item_id': order.items[0].id, 'quantity': 1}]), str(uuid4()))
    return_sid = next(s.id for s in returned.shipments if s.id != sid)
    returned = await shipment_command(database, oid, return_sid, returned.revision, mock_status='returned')
    receipt_payload = MockParcelReceiptCreate(expected_revision=returned.revision, evidence='Physically received isolated mock parcel',
        confirm=True, items=[{'order_item_id': order.items[0].id, 'quantity': 1, 'disposition': 'restock'}])
    receipt_key = str(uuid4())
    async with database() as session:
        await mock_cod.receive_return(session, await actor(session), oid, return_sid, receipt_payload, receipt_key)
    financial_return = await create_return(database, order, sale, quantity=1)
    await refund(database, financial_return)
    with pytest.raises(HTTPException) as mismatch:
        await receive(database, financial_return, disposition='not_restock')
    assert mismatch.value.detail['code'] == 'parcel_receipt_required'
    acknowledged = await receive(database, financial_return)
    assert acknowledged.receipt.shipment_return_receipt_id is not None
    # A different financial return/key cannot claim the same physical item again.
    unreceived = await create_return(database, order, sale, quantity=1)
    with pytest.raises(HTTPException) as exhausted:
        await receive(database, unreceived)
    assert exhausted.value.detail['code'] == 'parcel_receipt_required'
    return_doc = await fiscal_command(database,
        await submit(database, oid, approval(await return_preview(database, financial_return))), 'process')
    assert return_doc.parent_sale_id == sale.id
    async with database() as session:
        assert (await session.get(Product, pid)).stock_quantity == 4  # One debit, one physical credit, no fiscal credit.
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(MockCODConfirmation)) == 1
        assert await session.scalar(select(func.count()).select_from(MockShipmentReturnReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReturnReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 2
        assert not (await fiscal_returns.reconciliation(session, await actor(session))).buckets['receipts_without_basis']
        x = await fiscal_returns.report(session, await actor(session), fiscal.REGISTER,
            MockReportCreate(kind='x', expected_shift_revision=1, confirm=True), str(uuid4()))
        z = await fiscal_returns.report(session, await actor(session), fiscal.REGISTER,
            MockReportCreate(kind='z', expected_shift_revision=1, confirm=True), str(uuid4()))
        assert x.totals == z.totals
        assert (x.totals.sales_minor, x.totals.returns_minor, x.totals.net_minor) == (20000, 10000, 10000)
