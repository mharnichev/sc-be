"""Stage 5 local mock facts. No payment, carrier or notification transport."""
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from fastapi import HTTPException
from sqlalchemy import func, select, or_, and_

from app.models.admin_user import AdminUser
from app.models.fiscal import (FiscalDocument, FiscalPreview, MockReceipt,
    ReturnRecord, MockRefund, MockReturnReceipt, MockFiscalReport, ReconciliationAcknowledgement,
    MockFiscalShare, MockFiscalShareRevocation)
from app.models.inventory import InventoryMovement, MovementType
from app.models.order import OrderPayment, Order
from app.models.product import Product
from app.schemas.fiscal_returns import (ReturnResponse, MockReportResponse, ReconciliationAckResponse,
    ReconciliationResponse, FiscalShareResponse, PublicMockFiscalDocument)
from app.services import fiscal, local_sales
from app.services.order_documents import digest

READ_PERMISSIONS = ('can_record_sales', 'can_fiscalize_sales', 'can_fiscalize_returns', 'can_post_inventory', 'can_manage_fiscal_settings')


def permitted(actor, permission=None):
    return bool(actor and actor.is_active and (actor.is_superuser or (
        getattr(actor, permission, False) if permission else any(getattr(actor, p, False) for p in READ_PERMISSIONS))))


async def require(session, actor, permission=None, *, mutation=True):
    current = (await session.scalars(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))).one_or_none()
    from app.services.sales_access import deny_master
    if current:
        await deny_master(session, current)
    if not permitted(current, permission):
        raise HTTPException(403, 'Mock return permission required')
    if mutation:
        await fiscal.require(session, current, permission=permission, any_read=permission is None)
    return current


async def parent_sale(session, sale_id, order=None):
    sale = await session.get(FiscalDocument, sale_id)
    receipt = await session.get(MockReceipt, sale.provider_uuid) if sale else None
    if (not sale or sale.kind != 'sale' or sale.status != 'issued' or not sale.simulation
            or sale.snapshot.get('document_kind') != 'mock_fiscal_receipt'
            or digest(sale.snapshot) != sale.snapshot_hash or not receipt
            or receipt.document_id != sale.id or receipt.snapshot_hash != sale.snapshot_hash):
        fiscal.fail('issued_mock_sale_required', 'Original issued mock sale with durable acceptance required')
    await fiscal.validate_issued_document(session, sale)
    if order is None:
        order = await local_sales.load(session, sale.order_id)
    payment = await session.get(OrderPayment, sale.payment_id)
    original = sale.snapshot.get('payment', {})
    from app.services.mock_cod import validated_cod_snapshot
    cod = validated_cod_snapshot(order, payment) if payment else None
    if (sale.order_id != order.id or (not cod and (not order.simulation or order.channel != 'backoffice_pos'))
            or not payment or payment.order_id != order.id or not payment.simulation
            or not ((payment.source == 'operator_confirmation' and payment.method in ('cash', 'card'))
                or (sale.qr_attempt_id is not None and payment.source == 'provider_confirmation' and payment.method == 'monobank_qr')
                or (cod is not None and sale.snapshot.get('cod') == cod))
            or original != {'id': payment.id, 'amount_minor': payment.amount_minor, 'method': payment.method,
                'source': payment.source, 'simulation': True}
            or payment.amount_minor != sale.snapshot['amount_minor']):
        fiscal.fail('simulation_sale_required', 'Original immutable full simulated stage-2 payment required')
    return sale


def net_lines(state):
    """Allocate order-level discount proportionally with deterministic remainder ties."""
    lines = sorted(state['items'], key=lambda item: item['order_item_id'])
    gross = sum(item['total_minor'] for item in lines)
    discount = state['additional_discount_minor']
    if gross <= 0 or discount < 0 or discount > gross or gross - discount != state['amount_minor']:
        fiscal.fail('invalid_original_amounts', 'Original sale amounts do not reconcile')
    allocations = {item['order_item_id']: discount * item['total_minor'] // gross for item in lines}
    remainder = discount - sum(allocations.values())
    ranking = sorted(lines, key=lambda item: (-(discount * item['total_minor'] % gross), item['order_item_id']))
    for item in ranking[:remainder]:
        allocations[item['order_item_id']] += 1
    return {item['order_item_id']: item['total_minor'] - allocations[item['order_item_id']] for item in lines}


async def return_row(session, return_id, *, lock=False):
    row = await session.get(ReturnRecord, return_id)
    if not row:
        raise HTTPException(404, 'Return not found')
    order = await local_sales.load(session, row.order_id, lock=lock)
    return row, order


async def response(session, actor, row):
    refund = await session.scalar(select(MockRefund).where(MockRefund.return_id == row.id))
    receipt = await session.scalar(select(MockReturnReceipt).where(MockReturnReceipt.return_id == row.id))
    docs = (await session.scalars(select(FiscalDocument).where(FiscalDocument.return_id == row.id).order_by(FiscalDocument.id))).all()
    enabled = fiscal.available(session)
    actions = []
    if enabled:
        if not refund and permitted(actor, 'can_record_sales'):
            actions.append('refund')
        if not receipt and permitted(actor, 'can_post_inventory'):
            from app.models.shipment import Shipment
            from app.models.mock_cod import MockShipmentReturnReceipt
            carrier = await session.scalar(select(Shipment.id).where(Shipment.order_id == row.order_id,
                Shipment.simulation.is_(True), Shipment.carrier == 'mock_nova_poshta').limit(1))
            parcel = await session.scalar(select(MockShipmentReturnReceipt.id).where(
                MockShipmentReturnReceipt.order_id == row.order_id).limit(1)) if carrier else None
            if not carrier or parcel:
                actions.append('receive')
        if refund and not any(d.status != 'cancelled' for d in docs) and permitted(actor, 'can_fiscalize_returns'):
            actions.append('fiscal_preview')
    return ReturnResponse(id=row.id, order_id=row.order_id, parent_sale_id=row.parent_sale_id,
        amount_minor=row.amount_minor, items=row.items, reason=row.reason, created_at=row.created_at,
        refund={k: getattr(refund, k) for k in ('id', 'qr_attempt_id', 'amount_minor', 'method', 'evidence', 'operator_id', 'created_at')} if refund else None,
        receipt={k: getattr(receipt, k) for k in ('id', 'shipment_return_receipt_id', 'items', 'evidence', 'operator_id', 'created_at')} if receipt else None,
        fiscal_document_ids=[d.id for d in docs], allowed_actions=actions,
        blocking_reasons=[] if enabled else ['mock_fiscal_disabled'])


async def list_returns(session, actor, order_id):
    actor = await require(session, actor, mutation=False)
    await local_sales.load(session, order_id)
    rows = (await session.scalars(select(ReturnRecord).where(ReturnRecord.order_id == order_id).order_by(ReturnRecord.id))).all()
    return [await response(session, actor, row) for row in rows]


async def create_return(session, actor, order_id, payload, key):
    actor = await require(session, actor, 'can_record_sales')
    intent = await fiscal.intent_for(session, actor, key, f'return:{order_id}', payload)
    if intent.response_json:
        return ReturnResponse.model_validate(intent.response_json)
    order = await local_sales.load(session, order_id, lock=True)
    sale = await parent_sale(session, payload.parent_sale_id, order)
    if sale.snapshot.get('transport') == 'provider_sandbox':
        fiscal.fail('sandbox_return_unsupported', 'Provider sandbox returns require provider refund support')
    originals = {i['order_item_id']: i for i in sale.snapshot['items']}
    previous = (await session.scalars(select(ReturnRecord).where(ReturnRecord.order_id == order_id))).all()
    reserved = {}
    for row in previous:
        for item in row.items:
            reserved[item['order_item_id']] = reserved.get(item['order_item_id'], 0) + item['quantity']
    ids = [i.order_item_id for i in payload.items]
    if len(ids) != len(set(ids)):
        fiscal.fail('duplicate_return_item', 'Each sold line may appear once')
    amounts = net_lines(sale.snapshot)
    items = []
    for requested in sorted(payload.items, key=lambda i: i.order_item_id):
        original = originals.get(requested.order_item_id)
        before = reserved.get(requested.order_item_id, 0)
        if not original or before + requested.quantity > original['quantity']:
            fiscal.fail('return_capacity_exceeded', 'Return quantity exceeds original sold quantity minus all reserved returns')
        net = amounts[requested.order_item_id]
        amount = net * (before + requested.quantity) // original['quantity'] - net * before // original['quantity']
        items.append({'order_item_id': requested.order_item_id, 'product_id': original['product_id'],
            'name': original['name'], 'quantity': requested.quantity, 'total_minor': amount})
    row = ReturnRecord(order_id=order_id, parent_sale_id=sale.id, actor_id=actor.id, amount_minor=sum(i['total_minor'] for i in items),
        items=items, reason=payload.reason, simulation=True, created_at=datetime.now(UTC))
    session.add(row)
    await session.flush()
    fiscal.audit(session, actor, 'return_reserved', {'return_id': row.id, 'amount_minor': row.amount_minor}, sale)
    result = await response(session, actor, row)
    intent.order_id, intent.response_json = order_id, result.model_dump(mode='json')
    await session.commit()
    return result


async def record_refund(session, actor, return_id, payload, key):
    actor = await require(session, actor, 'can_record_sales')
    intent = await fiscal.intent_for(session, actor, key, f'refund:{return_id}', payload)
    if intent.response_json:
        return ReturnResponse.model_validate(intent.response_json)
    row, order = await return_row(session, return_id, lock=True)
    sale = await parent_sale(session, row.parent_sale_id, order)
    if await session.scalar(select(MockRefund.id).where(MockRefund.return_id == row.id)):
        fiscal.fail('refund_exists', 'This return already has an immutable mock refund')
    qr_id = sale.qr_attempt_id
    method = payload.method or ('cash' if sale.snapshot['payment']['method'] == 'cod' else sale.snapshot['payment']['method'])
    if (qr_id is not None and method != 'monobank_qr') or (qr_id is None and method == 'monobank_qr'):
        fiscal.fail('refund_method_mismatch', 'QR refunds must return to the original invoice')
    session.add(MockRefund(qr_attempt_id=qr_id, return_id=row.id, amount_minor=row.amount_minor, method=method,
        evidence=payload.evidence, operator_id=actor.id, simulation=True, created_at=datetime.now(UTC)))
    fiscal.audit(session, actor, 'mock_refund_recorded', {'return_id': row.id, 'amount_minor': row.amount_minor}, sale)
    await session.flush()
    result = await response(session, actor, row)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode='json')
    await session.commit()
    return result


async def record_receipt(session, actor, return_id, payload, key):
    actor = await require(session, actor, 'can_post_inventory')
    intent = await fiscal.intent_for(session, actor, key, f'return_receipt:{return_id}', payload)
    if intent.response_json:
        return ReturnResponse.model_validate(intent.response_json)
    row, order = await return_row(session, return_id, lock=True)
    sale = await parent_sale(session, row.parent_sale_id, order)
    if await session.scalar(select(MockReturnReceipt.id).where(MockReturnReceipt.return_id == row.id)):
        fiscal.fail('return_receipt_exists', 'This return already has a physical receipt')
    requested = {i.order_item_id: i for i in payload.items}
    if len(requested) != len(payload.items) or {k: i.quantity for k, i in requested.items()} != {i['order_item_id']: i['quantity'] for i in row.items}:
        fiscal.fail('receipt_quantity_mismatch', 'One physical receipt must explicitly cover every reserved return quantity')
    # Carrier returns have one physical receipt per TTN. This command only links
    # its already recorded quantities to the financial return; it never restocks twice.
    if any(s.simulation and s.carrier == 'mock_nova_poshta' for s in order.shipments):
        from app.models.mock_cod import MockShipmentReturnReceipt
        from app.services.mock_cod import parcel_items
        parcels = (await session.scalars(select(MockShipmentReturnReceipt).where(
            MockShipmentReturnReceipt.order_id == order.id).order_by(MockShipmentReturnReceipt.id))).all()
        candidates = []
        for parcel in parcels:
            shipment = next((s for s in order.shipments if s.id == parcel.shipment_id), None)
            if (not shipment or not shipment.simulation or shipment.carrier != 'mock_nova_poshta'
                    or shipment.status != 'returned' or shipment.tracking_number != parcel.tracking_number):
                continue
            physical = {i['order_item_id']: i for i in parcel.items}
            expected = {i['order_item_id']: i['quantity'] for i in parcel_items(order, shipment)}
            if len(physical) != len(parcel.items) or {k: i['quantity'] for k, i in physical.items()} != expected:
                continue
            linked = (await session.scalars(select(MockReturnReceipt).where(
                MockReturnReceipt.shipment_return_receipt_id == parcel.id))).all()
            used = {}
            for receipt in linked:
                for item in receipt.items:
                    used[item['order_item_id']] = used.get(item['order_item_id'], 0) + item['quantity']
            if all(k in physical and i.disposition == physical[k]['disposition']
                    and i.quantity + used.get(k, 0) <= physical[k]['quantity'] for k, i in requested.items()):
                candidates.append(parcel)
        if len(candidates) != 1:
            fiscal.fail('parcel_receipt_required', 'Receive the returned mock TTN first; quantities and dispositions must match its unclaimed physical receipt')
        parcel = candidates[0]
        session.add(MockReturnReceipt(return_id=row.id, shipment_return_receipt_id=parcel.id,
            items=[i.model_dump() for i in sorted(payload.items, key=lambda i: i.order_item_id)],
            evidence=payload.evidence, operator_id=actor.id, simulation=True, created_at=datetime.now(UTC)))
        fiscal.audit(session, actor, 'return_receipt_linked', {'return_id': row.id, 'shipment_return_receipt_id': parcel.id}, sale)
        await session.flush()
        result = await response(session, actor, row)
        intent.order_id, intent.response_json = order.id, result.model_dump(mode='json')
        await session.commit()
        return result
    # Both an explicit original handover and ledger evidence are required to add stock.
    handed_over = any(h.simulation for h in order.handovers) or any(s.simulation and s.handed_over_at for s in order.shipments)
    restocked = [i for i in row.items if requested[i['order_item_id']].disposition == 'restock']
    if restocked and not handed_over:
        fiscal.fail('handover_required', 'Original simulated handover required for restock')
    products = (await session.scalars(select(Product).where(Product.id.in_(sorted({i['product_id'] for i in restocked})))
        .order_by(Product.id).with_for_update().execution_options(populate_existing=True))).all()
    products = {p.id: p for p in products}
    for item in restocked:
        shipped = await session.scalar(select(func.coalesce(func.sum(-InventoryMovement.on_hand_delta), 0)).where(
            InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == item['order_item_id'],
            InventoryMovement.movement_type == MovementType.shipment, InventoryMovement.on_hand_delta < 0))
        restored = await session.scalar(select(func.coalesce(func.sum(InventoryMovement.on_hand_delta), 0)).where(
            InventoryMovement.order_id == order.id, InventoryMovement.order_item_id == item['order_item_id'],
            InventoryMovement.movement_type == MovementType.customer_return))
        if item['quantity'] + restored > shipped or item['product_id'] not in products:
            fiscal.fail('shipped_quantity_exceeded', 'Restock cannot exceed actual original shipped quantity')
        products[item['product_id']].stock_quantity += item['quantity']
        session.add(InventoryMovement(product_id=item['product_id'], movement_type=MovementType.customer_return,
            quantity=item['quantity'], on_hand_delta=item['quantity'], reserved_delta=0, order_id=order.id,
            order_item_id=item['order_item_id'], performed_by_user_id=actor.id,
            reference_key=f'mock-return:{row.id}:{item["order_item_id"]}', reason=payload.evidence))
    session.add(MockReturnReceipt(return_id=row.id, items=[i.model_dump() for i in sorted(payload.items, key=lambda i: i.order_item_id)],
        evidence=payload.evidence, operator_id=actor.id, simulation=True, created_at=datetime.now(UTC)))
    fiscal.audit(session, actor, 'return_received', {'return_id': row.id}, sale)
    await session.flush()
    result = await response(session, actor, row)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode='json')
    await session.commit()
    return result


async def return_snapshot(session, row, register):
    sale = await parent_sale(session, row.parent_sale_id)
    refund = await session.scalar(select(MockRefund).where(MockRefund.return_id == row.id))
    if not refund or not refund.simulation or refund.amount_minor != row.amount_minor:
        fiscal.fail('mock_refund_required', 'Record the separate exact mock refund before fiscal approval')
    return {'document_kind': 'mock_fiscal_receipt', 'kind': 'return', 'simulation': True, 'is_sent_dps': False,
        'notice': fiscal.NOTICE, 'currency': 'UAH', 'order_id': row.order_id, 'return_id': row.id,
        'parent_sale_id': sale.id, 'parent_sale_hash': sale.snapshot_hash, 'refund_id': refund.id,
        'cash_register_id': fiscal.REGISTER, 'credential_kind': 'mock', 'shift_revision': register.shift_revision,
        'items': row.items, 'amount_minor': row.amount_minor,
        'payment': {'id': refund.id, 'amount_minor': refund.amount_minor, 'method': refund.method, 'simulation': True},
        'delivery_channel': 'local_display_print'}


async def create_preview(session, actor, return_id, payload, key):
    actor = await require(session, actor, 'can_fiscalize_returns')
    intent = await fiscal.intent_for(session, actor, key, f'return_preview:{return_id}', payload)
    if intent.response_json:
        return fiscal.FiscalPreviewResponse.model_validate(intent.response_json)
    row, order = await return_row(session, return_id, lock=True)
    register = await fiscal.cash_register(session, lock=True)
    fiscal.register_ready(register, payload.cash_register_id)
    state = await return_snapshot(session, row, register)
    now = datetime.now(UTC)
    preview = FiscalPreview(order_id=order.id, actor_id=actor.id, revision=1, cash_register_id=fiscal.REGISTER,
        shift_revision=register.shift_revision, snapshot=state, snapshot_hash=digest(state), created_at=now,
        expires_at=now + timedelta(minutes=10))
    session.add(preview)
    await session.flush()
    result = await fiscal.preview_response(session, actor, preview)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode='json')
    await session.commit()
    return result


def report_response(report):
    return MockReportResponse(id=report.id, kind=report.kind, cash_register_id=report.cash_register_id,
        shift_revision=report.shift_revision, totals=report.totals, created_at=report.created_at, notice=fiscal.NOTICE)


async def list_reports(session, actor, register_id):
    await require(session, actor, 'can_manage_fiscal_settings', mutation=False)
    if register_id != fiscal.REGISTER:
        fiscal.fail('register_forbidden', 'Fixed mock register required')
    rows = (await session.scalars(select(MockFiscalReport).where(MockFiscalReport.cash_register_id == register_id).order_by(MockFiscalReport.id))).all()
    return [report_response(row) for row in rows]


async def report(session, actor, register_id, payload, key):
    actor = await require(session, actor, 'can_manage_fiscal_settings')
    intent = await fiscal.intent_for(session, actor, key, f'report:{register_id}', payload)
    if intent.response_json:
        return MockReportResponse.model_validate(intent.response_json)
    register = await fiscal.cash_register(session, lock=True)
    fiscal.register_ready(register, register_id)
    from app.services.fiscal_automation import block_shift_close
    await block_shift_close(session, register.id, register.shift_revision)
    if register.shift_revision != payload.expected_shift_revision:
        fiscal.fail('shift_revision_conflict', 'Reload current mock shift')
    if payload.kind == 'z':
        generation_documents = (await session.scalars(select(FiscalDocument).where(
            FiscalDocument.cash_register_id == register_id, FiscalDocument.shift_revision == register.shift_revision))).all()
        for doc in generation_documents:
            accepted = await session.get(MockReceipt, doc.provider_uuid)
            if doc.status in ('queued', 'submitting', 'processing', 'unknown') or (accepted and doc.status != 'issued'):
                fiscal.fail('shift_unsettled_documents', 'Finish reconciliation or cancel undispatched approvals before mock Z')
    documents = (await session.scalars(select(FiscalDocument).where(FiscalDocument.cash_register_id == register_id,
        FiscalDocument.shift_revision == register.shift_revision, FiscalDocument.status == 'issued', FiscalDocument.simulation.is_(True)).order_by(FiscalDocument.id))).all()
    # Every issued document must have durable local acceptance, including returns.
    for doc in documents:
        await fiscal.validate_issued_document(session, doc)
    sales = [d for d in documents if d.kind == 'sale']
    returns = [d for d in documents if d.kind == 'return']
    sales_total = sum(d.snapshot['amount_minor'] for d in sales)
    returns_total = sum(d.snapshot['amount_minor'] for d in returns)
    row = MockFiscalReport(kind=payload.kind, cash_register_id=register_id, shift_revision=register.shift_revision,
        actor_id=actor.id, totals={'sale_count': len(sales), 'return_count': len(returns), 'sales_minor': sales_total,
            'returns_minor': returns_total, 'net_minor': sales_total - returns_total, 'document_ids': [d.id for d in documents]}, created_at=datetime.now(UTC))
    session.add(row)
    if payload.kind == 'z':
        register.shift_status = 'closed'
        register.shift_revision += 1
    await session.flush()
    fiscal.audit(session, actor, 'mock_report_' + payload.kind, {'report_id': row.id, 'shift_revision': row.shift_revision})
    result = report_response(row)
    intent.response_json = result.model_dump(mode='json')
    await session.commit()
    return result


def ack_response(row):
    return ReconciliationAckResponse(**{k: getattr(row, k) for k in ('id', 'case_type', 'resource_id', 'evidence', 'actor_id', 'created_at')})


async def reconciliation(session, actor):
    await require(session, actor, mutation=False)
    acknowledgements = [ack_response(a) for a in (await session.scalars(select(ReconciliationAcknowledgement).order_by(ReconciliationAcknowledgement.id))).all()]
    buckets = {key: [] for key in ('payments_without_receipts', 'receipts_without_basis', 'unknown', 'refund_pending', 'fiscal_return_pending', 'receipt_pending', 'open_shifts', 'automation_receipt_missing')}
    def add(kind, resource_id, order_id=None, amount=0):
        resource_id = str(resource_id)
        buckets[kind].append({'resource_id': resource_id, 'order_id': order_id, 'amount_minor': amount,
            'acknowledgements': [a for a in acknowledgements if a.case_type == kind and a.resource_id == resource_id]})
    documents = (await session.scalars(select(FiscalDocument).where(FiscalDocument.simulation.is_(True)).order_by(FiscalDocument.id))).all()
    payments = (await session.scalars(select(OrderPayment).join(Order, Order.id == OrderPayment.order_id).where(
        OrderPayment.simulation.is_(True), or_(and_(Order.simulation.is_(True), Order.channel == 'backoffice_pos'),
            OrderPayment.source == 'mock_cod_confirmation')))).all()
    for payment in payments:
        if not any(d.payment_id == payment.id and d.kind == 'sale' and d.status == 'issued' for d in documents):
            add('payments_without_receipts', payment.id, payment.order_id, payment.amount_minor)
    from app.models.qr_payment import QRPaymentAttempt
    unknown_qr = (await session.execute(select(QRPaymentAttempt.id, QRPaymentAttempt.order_id,
        QRPaymentAttempt.amount_minor).join(Order, Order.id == QRPaymentAttempt.order_id).where(
        QRPaymentAttempt.simulation.is_(True), QRPaymentAttempt.payment_status == 'unknown',
        Order.simulation.is_(True), Order.channel == 'backoffice_pos').order_by(QRPaymentAttempt.id))).all()
    for attempt_id, order_id, amount_minor in unknown_qr:
        add('unknown', f'qr:{attempt_id}', order_id, amount_minor)
    valid_issued = set()
    for doc in documents:
        if doc.status in ('unknown', 'processing', 'submitting'):
            add('unknown', doc.id, doc.order_id, doc.snapshot['amount_minor'])
        if doc.status == 'issued':
            try:
                await fiscal.validate_issued_document(session, doc)
                if doc.kind == 'sale':
                    await parent_sale(session, doc.id)
                else:
                    row = await session.get(ReturnRecord, doc.return_id)
                    refund = await session.scalar(select(MockRefund).where(MockRefund.return_id == doc.return_id))
                    receipt = await session.get(MockReceipt, doc.provider_uuid)
                    if (not row or not refund or not receipt or receipt.document_id != doc.id or receipt.snapshot_hash != doc.snapshot_hash
                            or digest(doc.snapshot) != doc.snapshot_hash or refund.amount_minor != row.amount_minor
                            or doc.snapshot['amount_minor'] != row.amount_minor or doc.parent_sale_id != row.parent_sale_id):
                        fiscal.fail('invalid_return_basis', 'Return receipt lacks basis')
                    await parent_sale(session, row.parent_sale_id)
                valid_issued.add(doc.id)
            except HTTPException:
                add('receipts_without_basis', doc.id, doc.order_id, doc.snapshot['amount_minor'])
    from app.models.fiscal_automation import FiscalAutomation
    automations = (await session.scalars(select(FiscalAutomation))).all()
    for project in automations:
        if project.status == 'issued' and not any(d.automation_id == project.id and d.id in valid_issued for d in documents):
            add('automation_receipt_missing', project.id, project.order_id, project.snapshot['amount_minor'])
        elif project.status in ('unknown', 'cancel_unknown', 'cancel_pending'):
            add('unknown', f'automation:{project.id}', project.order_id, project.snapshot['amount_minor'])
    rows = (await session.scalars(select(ReturnRecord).where(ReturnRecord.simulation.is_(True)))).all()
    refunds = (await session.scalars(select(MockRefund).where(MockRefund.simulation.is_(True)))).all()
    receipts = (await session.scalars(select(MockReturnReceipt).where(MockReturnReceipt.simulation.is_(True)))).all()
    for row in rows:
        if not any(r.return_id == row.id for r in refunds):
            add('refund_pending', row.id, row.order_id, row.amount_minor)
        if not any(d.return_id == row.id and d.kind == 'return' and d.status == 'issued' for d in documents):
            add('fiscal_return_pending', row.id, row.order_id, row.amount_minor)
        if not any(r.return_id == row.id for r in receipts):
            add('receipt_pending', row.id, row.order_id, row.amount_minor)
    register = await fiscal.cash_register(session)
    if register and register.shift_status == 'open':
        add('open_shifts', f'{register.id}:{register.shift_revision}')
    totals = {'sales_minor': sum(d.snapshot['amount_minor'] for d in documents if d.kind == 'sale' and d.id in valid_issued),
        'reserved_returns_minor': sum(r.amount_minor for r in rows), 'refunds_minor': sum(r.amount_minor for r in refunds),
        'fiscal_returns_minor': sum(d.snapshot['amount_minor'] for d in documents if d.kind == 'return' and d.id in valid_issued)}
    return ReconciliationResponse(notice=fiscal.NOTICE, buckets=buckets, totals=totals, acknowledgements=acknowledgements)


async def acknowledge(session, actor, payload, key):
    actor = await require(session, actor)
    intent = await fiscal.intent_for(session, actor, key, 'reconciliation_ack', payload)
    if intent.response_json:
        return ReconciliationAckResponse.model_validate(intent.response_json)
    current = await reconciliation(session, actor)
    if not any(case.resource_id == payload.resource_id for case in current.buckets[payload.case_type]):
        fiscal.fail('reconciliation_case_not_found', 'Only a current unresolved case may be acknowledged')
    row = ReconciliationAcknowledgement(case_type=payload.case_type, resource_id=payload.resource_id,
        evidence=payload.evidence, actor_id=actor.id, created_at=datetime.now(UTC))
    session.add(row)
    await session.flush()
    result = ack_response(row)
    fiscal.audit(session, actor, 'reconciliation_ack', {'acknowledgement_id': row.id})
    intent.response_json = result.model_dump(mode='json')
    await session.commit()
    return result


async def share_document(session, actor, document_id, payload, key, *, revoke=False):
    kind = await session.scalar(select(FiscalDocument.kind).where(FiscalDocument.id == document_id))
    if kind is None:
        raise HTTPException(404, 'Fiscal document not found')
    actor = await require(session, actor, 'can_fiscalize_returns' if kind == 'return' else 'can_fiscalize_sales')
    intent = await fiscal.intent_for(session, actor, key, f'{"revoke" if revoke else "share"}:{document_id}', payload)
    if intent.response_json:
        return FiscalShareResponse.model_validate(intent.response_json)
    order, register, document, job = await fiscal.lock_document(session, document_id)
    await validate_shareable(session, document)
    latest = await session.scalar(select(MockFiscalShare).where(MockFiscalShare.document_id == document.id).order_by(MockFiscalShare.generation.desc()).limit(1))
    if revoke:
        if latest and not await session.scalar(select(MockFiscalShareRevocation.id).where(MockFiscalShareRevocation.share_id == latest.id)):
            session.add(MockFiscalShareRevocation(share_id=latest.id, actor_id=actor.id, created_at=datetime.now(UTC)))
        result = FiscalShareResponse(document_id=document.id, generation=latest.generation if latest else 0,
            url=None, expires_at=None, revoked=True)
    else:
        raw = token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(seconds=payload.expires_in_seconds)
        grant = MockFiscalShare(document_id=document.id, generation=latest.generation + 1 if latest else 1,
            token_hash=sha256(raw.encode()).hexdigest(), scope='mock_fiscal_document', actor_id=actor.id,
            created_at=datetime.now(UTC), expires_at=expires)
        session.add(grant)
        result = FiscalShareResponse(document_id=document.id, generation=grant.generation,
            url=f'/api/v1/public/mock-fiscal-documents/{raw}', expires_at=expires, revoked=False)
    fiscal.audit(session, actor, 'share_revoked' if revoke else 'share_created', {'generation': result.generation}, document)
    intent.response_json = result.model_dump(mode='json')
    await session.commit()
    return result


async def validate_shareable(session, document):
    try:
        await fiscal.validate_issued_document(session, document)
    except HTTPException:
        raise HTTPException(404, 'Mock document not found') from None
    order = await local_sales.load(session, document.order_id)
    receipt = await session.get(MockReceipt, document.provider_uuid)
    if (document.status != 'issued' or not document.simulation or not order.simulation or order.channel != 'backoffice_pos'
            or document.kind not in ('sale', 'return') or document.snapshot.get('document_kind') != 'mock_fiscal_receipt'
            or digest(document.snapshot) != document.snapshot_hash or not receipt
            or receipt.document_id != document.id or receipt.snapshot_hash != document.snapshot_hash):
        raise HTTPException(404, 'Mock document not found')


async def public_document(session, token):
    # Capability reads perform no insert, journal, dispatch or delivery mutation.
    if not 32 <= len(token) <= 128:
        raise HTTPException(404, 'Mock document not found')
    grant = await session.scalar(select(MockFiscalShare).where(MockFiscalShare.token_hash == sha256(token.encode()).hexdigest(),
        MockFiscalShare.scope == 'mock_fiscal_document'))
    if not grant or grant.expires_at <= datetime.now(UTC):
        raise HTTPException(404, 'Mock document not found')
    latest = await session.scalar(select(func.max(MockFiscalShare.generation)).where(MockFiscalShare.document_id == grant.document_id))
    if latest != grant.generation or await session.scalar(select(MockFiscalShareRevocation.id).where(MockFiscalShareRevocation.share_id == grant.id)):
        raise HTTPException(404, 'Mock document not found')
    document = await session.get(FiscalDocument, grant.document_id)
    if not document:
        raise HTTPException(404, 'Mock document not found')
    await validate_shareable(session, document)
    return PublicMockFiscalDocument(kind=document.kind, items=[{k: item[k] for k in ('name', 'quantity', 'total_minor')} for item in document.snapshot['items']],
        amount_minor=document.snapshot['amount_minor'], notice=fiscal.NOTICE)
