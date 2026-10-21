"""Authenticated QR ledger. Remote mode delegates to the explicit sandbox bridge.

Invoice truth, commands and events share the isolated PostgreSQL transaction.
A lost response deliberately leaves the projection unknown; refresh only looks
up this exact stored invoice. Receipt recovery never requests another payment.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, text
from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import OrderPayment, OrderStatus
from app.models.fiscal import FiscalDocument, FiscalPreview, MockReceipt
from app.models.qr_payment import QRPaymentAttempt, QRProviderRecord, QRCommand, QREvent
from app.schemas.qr_payment import QRAttemptResponse
from app.services import fiscal, local_sales, sales_access
from app.services.order_documents import claim_intent, digest, operation_key

ACTIVE = ('waiting', 'processing', 'unknown', 'paid')
TERMINAL_UNPAID = ('declined', 'expired', 'cancelled')


def permitted(actor):
    return bool(actor and actor.is_active and (actor.is_superuser or
        (actor.can_record_sales and getattr(actor, 'can_accept_qr_payments', False))))


def safe_database(session):
    url = session.get_bind().url
    return bool(settings.app_env in ('local', 'test') and url.get_backend_name() == 'postgresql'
        and url.host in ('localhost', '127.0.0.1', '::1') and 'test' in (url.database or '').lower())


def available(session):
    return bool(safe_database(session) and settings.monobank_qr_mock_enabled
        and settings.checkbox_mock_enabled and settings.local_sales_simulation_enabled)


async def require(session, actor, *, recovery=False):
    actor = (await session.scalars(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))).one_or_none()
    if not permitted(actor):
        raise HTTPException(403, 'Sales and QR payment permissions required')
    if not safe_database(session) or (not recovery and not available(session)):
        fiscal.fail('mock_qr_disabled', 'Explicit mock flags and isolated local PostgreSQL test database required')
    actual = await session.scalar(text('SELECT current_database()'))
    if actual != session.get_bind().url.database or 'test' not in actual.lower():
        fiscal.fail('mock_qr_disabled', 'Connected database must match isolated test database')
    return actor


async def readiness(session, actor):
    actor = await require(session, actor, recovery=True)
    return {'mode': 'mock_only', 'available': available(session),
        'blocking_reasons': [] if available(session) else ['mock_qr_disabled'],
        'cash_registers': [fiscal.register_data(await fiscal.cash_register(session))]}


async def owner(session, order_id):
    return await session.scalar(select(QRPaymentAttempt).where(QRPaymentAttempt.order_id == order_id,
        QRPaymentAttempt.payment_status.in_(ACTIVE)).order_by(QRPaymentAttempt.id.desc()))


def handover_ready(order):
    active = [a for a in order.qr_payment_attempts if a.payment_status in ACTIVE]
    return not active or all(a.payment_status == 'paid' and a.receipt_status == 'issued' for a in active)


async def block_mutation(session, order_id, *, handover=False):
    row = await owner(session, order_id)
    if row and not (handover and row.payment_status == 'paid' and row.receipt_status == 'issued'):
        fiscal.fail('qr_payment_owns_order', 'Reconcile the existing QR payment and receipt before changing this sale')


async def block_shift_close(session, register_id, shift_revision):
    if await session.scalar(select(QRPaymentAttempt.id).where(QRPaymentAttempt.cash_register_id == register_id,
            QRPaymentAttempt.shift_revision == shift_revision,
            QRPaymentAttempt.payment_status.in_(ACTIVE),
            ~((QRPaymentAttempt.payment_status == 'paid') & (QRPaymentAttempt.receipt_status == 'issued')))):
        fiscal.fail('shift_unsettled_qr', 'Reconcile or cancel the existing QR invoice before closing this mock shift')


async def response(session, actor, row):
    payment_id = await session.scalar(select(OrderPayment.id).where(OrderPayment.order_id == row.order_id,
        OrderPayment.source == 'provider_confirmation', OrderPayment.evidence == row.provider_invoice_id))
    document_id = await session.scalar(select(FiscalDocument.id).where(FiscalDocument.qr_attempt_id == row.id))
    from app.models.sandbox import SandboxRun
    sandbox = await session.scalar(select(SandboxRun).where(SandboxRun.attempt_id == row.id))
    if row.snapshot.get('transport') == 'provider_sandbox' and not sandbox:
        fiscal.fail('sandbox_run_missing', 'Remote sandbox attempt requires its authoritative run')
    actions = ['refresh']
    if row.payment_status not in ('paid', *TERMINAL_UNPAID):
        actions.append('cancel')
    if available(session) and not await sales_access.restricted_master(session, actor) and (row.payment_status not in TERMINAL_UNPAID):
        actions.append('simulate')
    if sandbox:
        actions = ['refresh']
    return QRAttemptResponse(mode='provider_sandbox' if sandbox else 'mock_only',
        payment_url=sandbox.payment_url if sandbox else None,
        checkbox_receipt_id=sandbox.receipt_uuid if sandbox else None,
        verification=sandbox.verification if sandbox else None, id=row.id, order_id=row.order_id, revision=row.revision,
        provider_invoice_id=row.provider_invoice_id, payment_status=row.payment_status,
        receipt_status=row.receipt_status, amount_minor=row.amount_minor, expires_at=row.expires_at,
        snapshot=row.snapshot, local_payment_path='' if sandbox else f'/orders/qr-simulator/{row.id}', payment_id=payment_id,
        fiscal_document_id=document_id, receipt_url=f'/api/v1/backoffice/qr-payment-attempts/{row.id}/receipt' if document_id else None,
        allowed_actions=actions, blocking_reasons=([sandbox.last_error] if sandbox.last_error else []) if sandbox else ([] if available(session) else ['mock_qr_disabled']))


async def get(session, actor, attempt_id):
    actor = await require(session, actor, recovery=True)
    row = await session.get(QRPaymentAttempt, attempt_id)
    if not row:
        raise HTTPException(404, 'QR attempt not found')
    await sales_access.own_sale(session, actor, await local_sales.load(session, row.order_id))
    return await response(session, actor, row)


async def list_attempts(session, actor, order_id):
    actor = await require(session, actor, recovery=True)
    await sales_access.own_sale(session, actor, await local_sales.load(session, order_id))
    rows = (await session.scalars(select(QRPaymentAttempt).where(QRPaymentAttempt.order_id == order_id)
        .order_by(QRPaymentAttempt.id))).all()
    return [await response(session, actor, row) for row in rows]


async def create(session, actor, order_id, payload, key):
    actor = await require(session, actor, recovery=True)
    order = await local_sales.load(session, order_id, lock=True)
    await sales_access.own_sale(session, actor, order)
    if order.__dict__.get("master_mono_binding"):
        fiscal.fail("master_mono_checkout_owned", "Recover the existing direct mono checkout")
    intent_key = operation_key(key, f'qr:{actor.id}:create:{order_id}')
    intent = await claim_intent(session, intent_key, digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return QRAttemptResponse.model_validate(intent.response_json)
    actor = await require(session, actor)
    if order.revision != payload.expected_revision:
        fiscal.fail('revision_conflict', 'Reload order before creating QR')
    if order.status in (OrderStatus.completed, OrderStatus.cancelled) or order.handovers or order.shipments:
        fiscal.fail('qr_sale_unavailable', 'QR requires an open local pickup POS sale')
    if await owner(session, order_id):
        fiscal.fail('qr_attempt_active', 'Recover the existing invoice before creating another QR')
    from app.services.fiscal_automation import owner as fiscal_owner
    if await fiscal_owner(session, order_id):
        fiscal.fail('fiscal_owner_exists', 'Another fiscal strategy already owns this sale')
    register = await fiscal.cash_register(session, lock=True)
    fiscal.register_ready(register, payload.cash_register_id)
    state = fiscal.snapshot(order, register, qr_consent=True)
    state.update(source='mock_monobank_qr', consent={'actor_id': actor.id, 'confirm': True},
        provider_contract='local_simulator_only', fee_minor=0)
    now = datetime.now(UTC)
    row = QRPaymentAttempt(order_id=order_id, actor_id=actor.id, provider_invoice_id=str(uuid4()),
        cash_register_id=register.id, shift_revision=register.shift_revision, amount_minor=state['amount_minor'],
        snapshot=state, snapshot_hash=digest(state), payment_status='waiting', receipt_status='pending',
        revision=1, simulation=True, created_at=now, expires_at=now + timedelta(minutes=10))
    session.add(row)
    await session.flush()
    session.add(QRCommand(attempt_id=row.id, actor_id=actor.id, operation='create', operation_key=intent_key,
        payload=payload.model_dump(mode='json'), created_at=now))
    session.add(QRProviderRecord(provider_invoice_id=row.provider_invoice_id, attempt_id=row.id,
        snapshot_hash=row.snapshot_hash, payment_status='waiting', receipt_status='pending'))
    order.revision += 1
    await session.flush()
    result = await response(session, actor, row)
    intent.order_id, intent.response_json = order_id, result.model_dump(mode='json')
    await session.commit()
    return result


def advance_truth(truth, outcome):
    # Accepted terminal truth is monotonic. HOLD explicitly means no payment.
    if truth.payment_status in TERMINAL_UNPAID:
        return
    if truth.payment_status == 'paid':
        if outcome == 'receipt_issued':
            truth.receipt_status = 'issued'
        return
    if outcome == 'receipt_issued':
        fiscal.fail('payment_required', 'Receipt recovery requires accepted payment')
    if outcome in ('processing', 'hold'):
        truth.payment_status = 'processing'
    elif outcome in ('decline', 'expiry'):
        truth.payment_status = 'declined' if outcome == 'decline' else 'expired'
    else:
        truth.payment_status = 'paid'
        truth.accepted_at = datetime.now(UTC)
        truth.receipt_status = {'success_receipt_error': 'error', 'success_receipt_pending': 'pending'}.get(outcome, 'issued')


def canonical_snapshot(row, payment):
    state = deepcopy(row.snapshot)
    state['payment'] = {'id': payment.id, 'amount_minor': payment.amount_minor, 'method': payment.method,
        'source': payment.source, 'simulation': True}
    state['qr_payment'] = {'attempt_id': row.id, 'provider_invoice_id': row.provider_invoice_id,
        'consent_snapshot_hash': row.snapshot_hash}
    return state


async def reconcile(session, row, truth):
    if (truth.provider_invoice_id != row.provider_invoice_id or truth.attempt_id != row.id
            or truth.snapshot_hash != row.snapshot_hash or digest(row.snapshot) != row.snapshot_hash):
        fiscal.fail('qr_truth_mismatch', 'Stored invoice truth must match immutable consent')
    row.payment_status, row.receipt_status = truth.payment_status, truth.receipt_status
    await session.flush()
    if truth.payment_status != 'paid':
        return
    payment = await session.scalar(select(OrderPayment).where(OrderPayment.order_id == row.order_id))
    if not payment:
        payment = OrderPayment(order_id=row.order_id, amount_minor=row.amount_minor, method='monobank_qr',
            source='provider_confirmation', evidence=row.provider_invoice_id, operator_id=row.actor_id,
            simulation=True, created_at=truth.accepted_at)
        session.add(payment)
        await session.flush()
        order = await local_sales.load(session, row.order_id)
        order.payment_method = 'monobank_qr'
        order.revision += 1
    if (payment.source != 'provider_confirmation' or payment.method != 'monobank_qr'
            or payment.evidence != row.provider_invoice_id or payment.amount_minor != row.amount_minor):
        fiscal.fail('qr_payment_mismatch', 'Accepted invoice must own exactly its full payment')
    if truth.receipt_status != 'issued':
        return
    existing = await session.scalar(select(FiscalDocument).where(FiscalDocument.qr_attempt_id == row.id))
    if existing:
        await validate_document(session, existing)
        return
    state = canonical_snapshot(row, payment)
    state_hash = digest(state)
    preview = FiscalPreview(order_id=row.order_id, actor_id=row.actor_id, revision=state['order_revision'],
        cash_register_id=row.cash_register_id, shift_revision=row.shift_revision,
        snapshot=state, snapshot_hash=state_hash, created_at=row.created_at, expires_at=row.expires_at)
    session.add(preview)
    await session.flush()
    doc = FiscalDocument(qr_attempt_id=row.id, kind='sale', order_id=row.order_id, payment_id=payment.id,
        preview_id=preview.id, actor_id=row.actor_id, provider_uuid=row.provider_invoice_id,
        cash_register_id=row.cash_register_id, shift_revision=row.shift_revision, snapshot=state,
        snapshot_hash=state_hash, status='issued', revision=1, simulation=True, mock_outcome='accepted',
        approved_at=row.created_at, dispatched_at=truth.accepted_at, delivery_status='delivered', delivery_attempts=0)
    session.add(doc)
    await session.flush()
    session.add(MockReceipt(provider_uuid=doc.provider_uuid, document_id=doc.id,
        snapshot_hash=doc.snapshot_hash, accepted_at=truth.accepted_at))
    await session.flush()


async def validate_document(session, document):
    row = await session.get(QRPaymentAttempt, document.qr_attempt_id)
    truth = await session.get(QRProviderRecord, document.provider_uuid)
    payment = await session.get(OrderPayment, document.payment_id)
    preview = await session.get(FiscalPreview, document.preview_id)
    if (not row or not truth or not payment or truth.attempt_id != row.id
            or row.provider_invoice_id != document.provider_uuid or truth.payment_status != 'paid'
            or truth.receipt_status != 'issued' or row.payment_status != 'paid' or row.receipt_status != 'issued'
            or truth.snapshot_hash != row.snapshot_hash or digest(row.snapshot) != row.snapshot_hash
            or payment.source != 'provider_confirmation' or payment.method != 'monobank_qr'
            or payment.evidence != row.provider_invoice_id or payment.order_id != row.order_id
            or payment.amount_minor != row.amount_minor or not payment.simulation
            or payment.operator_id != row.actor_id or payment.created_at != truth.accepted_at
            or document.order_id != row.order_id or document.actor_id != row.actor_id
            or document.kind != 'sale' or document.automation_id is not None
            or document.cash_register_id != row.cash_register_id or document.shift_revision != row.shift_revision
            or document.snapshot != canonical_snapshot(row, payment)
            or document.dispatched_at != truth.accepted_at or document.approved_at != row.created_at
            or not preview or preview.order_id != row.order_id or preview.actor_id != row.actor_id
            or preview.cash_register_id != row.cash_register_id or preview.shift_revision != row.shift_revision
            or preview.snapshot != document.snapshot or preview.snapshot_hash != document.snapshot_hash):
        fiscal.fail('qr_receipt_mismatch', 'Canonical QR receipt must match accepted immutable invoice')
    if row.snapshot.get('transport') == 'provider_sandbox':
        from app.services.sandbox import validate
        await validate(session, row)
    from app.models.fiscal import FiscalJob
    if await session.scalar(select(FiscalJob.id).where(FiscalJob.document_id == document.id)):
        fiscal.fail('qr_receipt_mismatch', 'QR receipt must not dispatch through the manual fiscal worker')


async def command(session, actor, attempt_id, payload, key, operation):
    actor = await require(session, actor, recovery=True)
    from app.models.sandbox import SandboxRun
    candidate = await session.get(QRPaymentAttempt, attempt_id)
    remote = await session.scalar(select(SandboxRun.id).where(SandboxRun.attempt_id == attempt_id))
    if candidate and candidate.snapshot.get('transport') == 'provider_sandbox' and not remote:
        fiscal.fail('sandbox_run_missing', 'Remote sandbox attempt requires its authoritative run')
    if remote:
        if operation != 'refresh':
            fiscal.fail('sandbox_command_forbidden', 'Remote provider attempts support refresh only')
        from app.services.sandbox import refresh
        return await refresh(session, actor, attempt_id, payload, key)
    if operation == 'simulate':
        await sales_access.deny_master(session, actor)
    order_id = candidate.order_id if candidate else None
    if order_id is None:
        raise HTTPException(404, 'QR attempt not found')
    await sales_access.own_sale(session, actor, await local_sales.load(session, order_id, lock=True))
    intent_key = operation_key(key, f'qr:{actor.id}:{operation}:{attempt_id}')
    intent = await claim_intent(session, intent_key, digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return QRAttemptResponse.model_validate(intent.response_json)
    if operation == 'simulate':
        actor = await require(session, actor)
    # Same global lock sequence as fiscal operations and close-shift.
    await fiscal.cash_register(session, lock=True)
    row = (await session.scalars(select(QRPaymentAttempt).where(QRPaymentAttempt.id == attempt_id)
        .with_for_update().execution_options(populate_existing=True))).one()
    truth = (await session.scalars(select(QRProviderRecord).where(QRProviderRecord.provider_invoice_id == row.provider_invoice_id)
        .with_for_update().execution_options(populate_existing=True))).one()
    if row.revision != payload.expected_revision:
        fiscal.fail('revision_conflict', 'Reload the same QR attempt')
    session.add(QRCommand(attempt_id=row.id, actor_id=actor.id, operation=operation, operation_key=intent_key,
        payload=payload.model_dump(mode='json'), created_at=datetime.now(UTC)))
    lost_response = False
    if operation == 'simulate':
        existing = await session.get(QREvent, str(payload.event_id))
        if existing and (existing.attempt_id != row.id or existing.outcome != payload.outcome):
            fiscal.fail('qr_event_conflict', 'Event ID already has different invoice or outcome')
        if not existing:
            session.add(QREvent(event_id=str(payload.event_id), attempt_id=row.id, actor_id=actor.id,
                outcome=payload.outcome, created_at=datetime.now(UTC)))
            advance_truth(truth, payload.outcome)
            lost_response = (payload.outcome == 'timeout_after_acceptance'
                and row.payment_status not in ('paid', *TERMINAL_UNPAID) and truth.payment_status == 'paid')
    elif operation == 'cancel':
        # Always lookup accepted truth before proving cancellation; timer is irrelevant.
        if truth.payment_status != 'paid' and truth.payment_status not in TERMINAL_UNPAID:
            truth.payment_status = 'cancelled'
    if lost_response:
        row.payment_status = 'unknown'
    else:
        await reconcile(session, row, truth)
    row.revision += 1
    await session.flush()
    result = await response(session, actor, row)
    intent.order_id, intent.response_json = row.order_id, result.model_dump(mode='json')
    await session.commit()
    return result
