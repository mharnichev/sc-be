"""Composed NP delivery/fiscal/return contracts in isolated local PostgreSQL."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.admin_user import AdminUser
from app.models.fiscal import FiscalDocument, FiscalJob, MockReceipt, MockRefund, MockReturnReceipt, ReturnRecord
from app.models.fiscal_automation import FiscalAutomation, MockAutomationRecord
from app.models.inventory import InventoryMovement, MovementType
from app.models.order import Order, OrderHandover, OrderPayment
from app.models.product import Product
from app.models.shipment import Shipment, ShipmentEvent
from app.schemas.fiscal import FiscalCommand, MockShiftCommand
from app.schemas.fiscal_returns import MockReportCreate, ReturnCreate
from app.schemas.order import OrderPaymentCreate, POSOrderCreate
from app.schemas.shipment import ShipmentCreate
from app.schemas.mock_cod import MockReturnShipmentCreate, MockParcelReceiptCreate
from app.services import mock_cod, fiscal, fiscal_automation as automation, fiscal_returns as returns, local_sales, shipments
from app.services.inventory import InventoryService
from tests.test_fiscal_automation_integration import enabled, user, preview, arm, run
from tests.test_fiscal_integration import approval, submit, command as fiscal_command
from tests.test_fiscal_returns_integration import create as create_return, refund, receive, return_preview
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product
from tests.test_shipments_integration import ADDRESS, PACK, setup_order, command as shipment_command

pytestmark = pytest.mark.anyio


async def pos_shipment(database, *, paid=True, quantity=3):
    product = await add_product(database, stock=10)
    async with database() as session:
        actor = await user(session)
        order = await local_sales.prepare(session, actor,
            POSOrderCreate(items=[{'product_id': product.id, 'quantity': quantity}], simulation=True), str(uuid4()))
        if paid:
            order = await local_sales.command(session, actor, order.id,
                OrderPaymentCreate(expected_revision=order.revision, amount_minor=quantity * 10000,
                    method='card', source='operator_confirmation', evidence='Synthetic prepaid NP sale'), str(uuid4()))
        row = await session.get(Order, order.id)
        row.fulfillment_method, row.shipping_payload_json = 'nova_poshta', ADDRESS.copy()
        await session.commit()
        order = await shipments.create(session, actor, order.id,
            ShipmentCreate(expected_revision=order.revision, packaging={**PACK,
                'declared_value_minor': quantity * 10000, 'cod_amount_minor': 0 if paid else quantity * 10000}), str(uuid4()))
        await fiscal.shift(session, actor, fiscal.REGISTER,
            MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
        return order, order.shipments[0].id, product.id


async def stock(database, product_id):
    async with database() as session:
        product = await session.get(Product, product_id)
        return product.stock_quantity, product.reserved_quantity


async def report(database, kind, *, revision=1, key=None):
    async with database() as session:
        return await returns.report(session, await user(session), fiscal.REGISTER,
            MockReportCreate(kind=kind, expected_shift_revision=revision, confirm=True), key or str(uuid4()))


async def count(session, model, *conditions):
    return await session.scalar(select(func.count()).select_from(model).where(*conditions))


@pytest.mark.parametrize('return_quantity', [1, 3])
async def test_prepaid_delivery_then_customer_return_separates_money_stock_and_fiscal_truth(
        database, checkout_dependencies, return_quantity):
    order, sid, pid = await pos_shipment(database)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    assert project.status == 'armed' and project.fiscal_document_id is None
    assert await stock(database, pid) == (10, 3)
    with pytest.raises(HTTPException) as error:
        await run(database, project, 'trigger')
    assert error.value.detail['code'] == 'mock_condition_not_met'

    handover_key = str(uuid4())
    before_handover = order.revision
    order = await shipment_command(database, order.id, sid, before_handover, 'handover',
        key=handover_key, evidence='Synthetic NP carrier acceptance')
    replay = await shipment_command(database, order.id, sid, before_handover, 'handover',
        key=handover_key, evidence='Synthetic NP carrier acceptance')
    assert replay == order and await stock(database, pid) == (7, 0)
    order = await shipment_command(database, order.id, sid, order.revision, mock_status='in_transit')
    stale = await shipment_command(database, order.id, sid, order.revision, mock_status='created')
    assert stale.shipments[0].status == 'in_transit'
    delivery_key, before_delivery = str(uuid4()), stale.revision
    order = await shipment_command(database, order.id, sid, before_delivery,
        key=delivery_key, mock_status='delivered')
    assert order.shipments[0].status == 'delivered'
    assert await shipment_command(database, order.id, sid, before_delivery,
        key=delivery_key, mock_status='delivered') == order
    with pytest.raises(HTTPException) as error:
        await shipment_command(database, order.id, sid, before_delivery, mock_status='delivered')
    assert error.value.detail['code'] == 'revision_conflict'
    # Delivery alone is a carrier fact; the explicit approved trigger issues the receipt.
    async with database() as session:
        assert await count(session, FiscalDocument) == 0
        assert await count(session, OrderPayment) == await count(session, OrderHandover) == 1
    trigger_key = str(uuid4())
    issued = await run(database, project, 'trigger', key=trigger_key)
    assert await run(database, project, 'trigger', key=trigger_key) == issued
    async with database() as session:
        sale = await fiscal.get_document(session, await user(session), issued.fiscal_document_id)
        assert sale.automation_id == project.id and sale.source == 'automation'
        assert sale.snapshot['items'] == project.snapshot['items'] and sale.snapshot['amount_minor'] == 30000
        assert await count(session, FiscalJob) == 0
    before_return = await report(database, 'x')
    assert (before_return.totals.sales_minor, before_return.totals.returns_minor) == (30000, 0)

    return_key = str(uuid4())
    returned = await create_return(database, order, sale, quantity=return_quantity, key=return_key)
    assert await create_return(database, order, sale, quantity=return_quantity, key=return_key) == returned
    refund_key = str(uuid4())
    refunded = await refund(database, returned, key=refund_key)
    assert await refund(database, returned, key=refund_key) == refunded
    assert refunded.refund.amount_minor == return_quantity * 10000
    # Reserving a return and recording its refund do not imply physical receipt.
    assert await stock(database, pid) == (7, 0)
    async with database() as session:
        assert await count(session, MockReturnReceipt) == 0
        assert await count(session, FiscalDocument) == 1
        assert (await returns.reconciliation(session, await user(session))).buckets['receipt_pending']
    # The carrier return is a separate parcel; record its physical receipt once
    # before the financial return acknowledges it without a second stock credit.
    async with database() as session:
        current = await session.get(Order, order.id)
        parcel = await mock_cod.create_return_shipment(session, await user(session), order.id, sid,
            MockReturnShipmentCreate(expected_revision=current.revision, confirm=True, evidence='Synthetic customer return parcel',
                items=[{'order_item_id': order.items[0].id, 'quantity': return_quantity}]), str(uuid4()))
    return_sid = next(s.id for s in parcel.shipments if s.id != sid)
    parcel = await shipment_command(database, order.id, return_sid, parcel.revision, mock_status='returned')
    async with database() as session:
        await mock_cod.receive_return(session, await user(session), order.id, return_sid,
            MockParcelReceiptCreate(expected_revision=parcel.revision, confirm=True, evidence='Synthetic physical return inspected',
                items=[{'order_item_id': order.items[0].id, 'quantity': return_quantity, 'disposition': 'restock'}]), str(uuid4()))
    receipt_key = str(uuid4())
    received = await receive(database, returned, key=receipt_key)
    assert await receive(database, returned, key=receipt_key) == received
    assert await stock(database, pid) == (7 + return_quantity, 0)
    with pytest.raises(HTTPException) as error:
        await receive(database, returned)
    assert error.value.detail['code'] == 'return_receipt_exists'
    fiscal_return = await submit(database, order.id, approval(await return_preview(database, returned)))
    fiscal_return = await fiscal_command(database, fiscal_return, 'process')
    assert fiscal_return.parent_sale_id == sale.id and fiscal_return.return_id == returned.id
    assert fiscal_return.snapshot['amount_minor'] == return_quantity * 10000
    # Repeating terminal carrier updates cannot repeat any of those effects.
    async with database() as session:
        revision = (await session.get(Order, order.id)).revision
    order = await shipment_command(database, order.id, sid, revision, mock_status='created')
    assert order.shipments[0].status == 'delivered'
    x = await report(database, 'x')
    z_key = str(uuid4())
    z = await report(database, 'z', key=z_key)
    assert await report(database, 'z', key=z_key) == z
    assert x.totals == z.totals
    assert (x.totals.sale_count, x.totals.return_count) == (1, 1)
    assert (x.totals.sales_minor, x.totals.returns_minor, x.totals.net_minor) == (
        30000, return_quantity * 10000, (3 - return_quantity) * 10000)
    assert set(x.totals.document_ids) == {sale.id, fiscal_return.id}
    async with database() as session:
        assert await count(session, OrderPayment) == await count(session, OrderHandover) == 1
        assert await count(session, ReturnRecord) == await count(session, MockRefund) == await count(session, MockReturnReceipt) == 1
        assert await count(session, FiscalDocument) == await count(session, MockReceipt) == 2
        assert await count(session, MockAutomationRecord) == 1
        assert await count(session, InventoryMovement, InventoryMovement.movement_type == MovementType.shipment) == 1
        assert await count(session, InventoryMovement, InventoryMovement.movement_type == MovementType.customer_return) == 1
        outcomes = set(await session.scalars(select(ShipmentEvent.outcome)))
        assert {'ignored_stale', 'ignored_terminal'} <= outcomes
        assert (await session.get(FiscalAutomation, project.id)).result == issued.result
        assert (await fiscal.get_document(session, await user(session), sale.id)).snapshot == sale.snapshot


@pytest.mark.parametrize('channel', ['backoffice_pos', 'shop'])
async def test_unpaid_cod_refusal_return_does_not_invent_payment_fiscal_basis_or_restock(
        database, checkout_dependencies, channel):
    if channel == 'backoffice_pos':
        order, sid, pid = await pos_shipment(database, paid=False, quantity=1)
    else:
        oid, pid = await setup_order(database, checkout_dependencies, stock=10)
        async with database() as session:
            order = await shipments.create(session, await user(session), oid,
                ShipmentCreate(expected_revision=1, packaging=PACK), str(uuid4()))
            await fiscal.shift(session, await user(session), fiscal.REGISTER,
                MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
        sid = order.shipments[0].id
    with pytest.raises(HTTPException) as error:
        await preview(database, order, sid)
    assert error.value.detail['code'] == ('simulation_payment_required' if channel == 'backoffice_pos' else 'simulation_sale_required')
    order = await shipment_command(database, order.id, sid, order.revision, 'handover', evidence='Synthetic unpaid COD handover')
    for status in ('in_transit', 'refused', 'returning', 'returned'):
        key, revision = str(uuid4()), order.revision
        order = await shipment_command(database, order.id, sid, revision, key=key, mock_status=status)
        assert order.shipments[0].status == status
        assert await shipment_command(database, order.id, sid, revision, key=key, mock_status=status) == order
        assert await stock(database, pid) == (9, 0)
    order = await shipment_command(database, order.id, sid, order.revision, mock_status='delivered')
    assert order.shipments[0].status == 'returned' and order.payment_status == 'unpaid'
    # Fiscal returns require an original issued sale. Physical unpaid COD returns
    # use an explicit inventory receipt, without inventing a payment or fiscal basis.
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await returns.create_return(session, await user(session), order.id,
                ReturnCreate(parent_sale_id=999999, items=[{'order_item_id': order.items[0].id, 'quantity': 1}],
                    reason='Synthetic unpaid COD carrier return'), str(uuid4()))
        assert error.value.detail['code'] == 'issued_mock_sale_required'
    async with database() as session:
        assert await count(session, InventoryMovement, InventoryMovement.movement_type == MovementType.customer_return) == 0
    warehouse_key = str(uuid4())
    async def warehouse_receipt():
        async with database() as session:
            result = await mock_cod.receive_return(session, await user(session), order.id, sid,
                MockParcelReceiptCreate(expected_revision=order.revision, confirm=True,
                    evidence='Operator physically received the synthetic refused NP parcel',
                    items=[{'order_item_id': order.items[0].id, 'quantity': 1, 'disposition': 'restock'}]), warehouse_key)
            return result.id
    first, replay = await asyncio.gather(warehouse_receipt(), warehouse_receipt())
    assert first == replay == await warehouse_receipt()
    assert await stock(database, pid) == (10, 0)
    x, z = await report(database, 'x'), await report(database, 'z')
    assert x.totals == z.totals
    assert (x.totals.sale_count, x.totals.return_count, x.totals.net_minor) == (0, 0, 0)
    async with database() as session:
        for model in (OrderPayment, FiscalDocument, MockReceipt, FiscalAutomation,
                MockAutomationRecord, ReturnRecord, MockRefund, MockReturnReceipt):
            assert await count(session, model) == 0
        assert await count(session, OrderHandover) == 1
        assert await count(session, InventoryMovement, InventoryMovement.movement_type == MovementType.shipment) == 1
        assert await count(session, InventoryMovement, InventoryMovement.movement_type == MovementType.customer_return) == 1


async def test_armed_prepaid_refusal_cannot_trigger_receipt_and_requires_explicit_disarm_for_reports(
        database, checkout_dependencies):
    order, sid, pid = await pos_shipment(database)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    order = await shipment_command(database, order.id, sid, order.revision, 'handover', evidence='Synthetic prepaid carrier handover')
    for status in ('refused', 'returning', 'returned'):
        order = await shipment_command(database, order.id, sid, order.revision, mock_status=status)
        with pytest.raises(HTTPException) as error:
            await run(database, project, 'trigger')
        assert error.value.detail['code'] == 'mock_shipment_required'
    assert await stock(database, pid) == (7, 0)
    with pytest.raises(HTTPException) as error:
        await report(database, 'z')
    assert error.value.detail['code'] == 'automation_report_unsupported'
    cancelled = await run(database, project, 'cancel')
    assert cancelled.status == 'cancelled'
    z = await report(database, 'z')
    assert (z.totals.sale_count, z.totals.return_count, z.totals.net_minor) == (0, 0, 0)
    async with database() as session:
        assert await count(session, OrderPayment) == 1
        assert await count(session, FiscalDocument) == await count(session, MockReceipt) == 0
        assert await count(session, MockRefund) == await count(session, MockReturnReceipt) == 0
        assert (await returns.reconciliation(session, await user(session))).buckets['payments_without_receipts']


@pytest.mark.parametrize('carrier_state', ['created', 'handed_over', 'in_transit', 'delivered'])
async def test_queued_worker_accepts_carrier_progress_without_changing_approved_scope(
        database, checkout_dependencies, carrier_state):
    order, sid, pid = await pos_shipment(database)
    project = await arm(database, await preview(database, order, sid))
    if carrier_state != 'created':
        order = await shipment_command(database, order.id, sid, order.revision, 'handover',
            evidence='Synthetic carrier acceptance before queued worker')
    if carrier_state != 'handed_over':
        order = await shipment_command(database, order.id, sid, order.revision, mock_status=carrier_state)
    assert order.revision > project.snapshot['order_revision']
    process_key = str(uuid4())
    processed = await run(database, project, 'process', key=process_key)
    assert processed.status == 'armed'
    assert await run(database, project, 'process', key=process_key) == processed
    assert processed.snapshot == project.snapshot and processed.snapshot_hash == project.snapshot_hash
    assert await stock(database, pid) == ((10, 3) if carrier_state == 'created' else (7, 0))
    async with database() as session:
        assert await count(session, MockAutomationRecord) == 1
        assert await count(session, FiscalDocument) == await count(session, MockReceipt) == 0
        assert await count(session, OrderPayment) == 1
    if carrier_state == 'delivered':
        issued = await run(database, processed, 'trigger')
        async with database() as session:
            receipt = await fiscal.get_document(session, await user(session), issued.fiscal_document_id)
            assert receipt.snapshot['items'] == project.snapshot['items']
            assert receipt.snapshot['amount_minor'] == project.snapshot['amount_minor']
            assert await count(session, MockReceipt) == 1


@pytest.mark.parametrize('change', ['items', 'amount', 'payment', 'shift', 'ttn', 'shipment_operation', 'origin', 'returned'])
async def test_queued_worker_carrier_revision_normalization_still_rejects_changed_scope(
        database, checkout_dependencies, change):
    order, sid, _ = await pos_shipment(database)
    project = await arm(database, await preview(database, order, sid))
    order = await shipment_command(database, order.id, sid, order.revision, 'handover',
        evidence='Synthetic carrier acceptance before scope check')
    order = await shipment_command(database, order.id, sid, order.revision, mock_status='in_transit')
    if change == 'returned':
        for status in ('refused', 'returning', 'returned'):
            order = await shipment_command(database, order.id, sid, order.revision, mock_status=status)
    async with database() as session:
        row = await local_sales.load(session, order.id)
        if change == 'items':
            row.items[0].product_name = 'Changed approved item after carrier handover'
        elif change == 'amount':
            row.total_amount -= 1
        elif change == 'payment':
            row.payments[0].method = 'cash'
        elif change == 'shift':
            (await fiscal.cash_register(session)).shift_revision += 1
        elif change == 'ttn':
            (await session.get(Shipment, sid)).tracking_number = 'MOCK-' + uuid4().hex.upper()
        elif change == 'shipment_operation':
            (await session.get(Shipment, sid)).operation_uuid = str(uuid4())
        elif change == 'origin':
            origin = await user(session)
            origin.is_superuser, origin.can_fiscalize_sales = False, False
            session.add(AdminUser(id=2, email='queued-worker@example.test', hashed_password='unused',
                is_active=True, is_superuser=True))
        await session.commit()
    async with database() as session:
        worker = await session.get(AdminUser, 2) if change == 'origin' else await user(session)
        processed = await automation.command(session, worker, project.id,
            FiscalCommand(expected_revision=project.revision), str(uuid4()), 'process')
        assert processed.status == 'error'
        assert processed.snapshot == project.snapshot and processed.snapshot_hash == project.snapshot_hash
        assert await count(session, MockAutomationRecord) == 0
        assert await count(session, FiscalDocument) == await count(session, MockReceipt) == 0
