"""Hardcoded, local PostgreSQL Checkbox simulator; no network transport.

Explicit API processing is the only worker entry point. Acceptance and its
journal commit together in this simulator, which shares the application DB.
A lease claim commits separately, so a crashed worker can be fenced and retried.
This makes no distributed-provider atomicity claim.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.fiscal import (FiscalDocument, FiscalEvent, FiscalInbox, FiscalJob,
                               FiscalPreview, MockCashRegister, MockReceipt)
from app.models.order import Order, OrderPayment, OrderStatus
from app.schemas.fiscal import FiscalDocumentResponse, FiscalPreviewResponse
from app.services import local_sales
from app.services.order_documents import claim_intent, digest, operation_key

REGISTER = 'MOCK-KASA-1'
NOTICE = 'MOCK / СИМУЛЯЦІЯ. Не є фіскальним чеком. Не передається до ДПС.'


def fail(code, message):
    raise HTTPException(409, detail={'code': code, 'message': message})


def available(session):
    # Inspect the actual session engine, never an unrelated environment URL.
    bind = session.get_bind()
    url = bind.url
    return bool(settings.checkbox_mock_enabled and settings.app_env in ('local', 'test')
                and url.get_backend_name() == 'postgresql'
                and url.host in ('localhost', '127.0.0.1', '::1')
                and 'test' in (url.database or '').lower())


def permitted(actor, *, settings_permission=False, kind='sale', permission=None, any_read=False):
    if any_read:
        from app.services.fiscal_returns import permitted as can_read
        return can_read(actor)
    field = permission or ('can_manage_fiscal_settings' if settings_permission else ('can_fiscalize_returns' if kind == 'return' else 'can_fiscalize_sales'))
    return bool(actor and actor.is_active and (actor.is_superuser or getattr(actor, field, False)))


async def require(session, actor, *, settings_permission=False, mutation=True, reconcile=False, kind='sale', permission=None, any_read=False):
    # A fresh locked row also serializes revocation against each accepted command.
    current = (await session.execute(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))).scalar_one_or_none()
    from app.services.sales_access import deny_master
    if current:
        await deny_master(session, current)
    if not permitted(current, settings_permission=settings_permission, kind=kind, permission=permission, any_read=any_read):
        raise HTTPException(403, 'Fiscal settings permission required' if settings_permission else 'Fiscal sales permission required')
    if mutation:
        if reconcile:
            bind = session.get_bind()
            safe = (settings.app_env in ('local', 'test') and bind.url.get_backend_name() == 'postgresql'
                and bind.url.host in ('localhost', '127.0.0.1', '::1') and 'test' in (bind.url.database or '').lower())
        else:
            safe = available(session)
        if not safe:
            fail('mock_fiscal_disabled', 'Explicit flag and isolated local PostgreSQL test database required')
        actual_db = await session.scalar(text('SELECT current_database()'))
        if 'test' not in actual_db.lower() or actual_db != session.get_bind().url.database:
            fail('mock_fiscal_disabled', 'Connected database does not match isolated test database')
    return current


async def cash_register(session, *, lock=False):
    # Lazy insert only occurs in an explicitly authorized mutation.
    if lock:
        await session.execute(insert(MockCashRegister).values(id=REGISTER, credential_kind='mock',
            shift_status='closed', shift_revision=0).on_conflict_do_nothing(index_elements=['id']))
    stmt = select(MockCashRegister).where(MockCashRegister.id == REGISTER).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def register_data(register):
    return {'id': REGISTER, 'label': 'MOCK Checkbox · локальна тестова каса', 'credential_kind': 'mock',
            'shift_status': register.shift_status if register else 'closed',
            'shift_revision': register.shift_revision if register else 0}


async def readiness(session, actor):
    from app.services.sales_access import deny_master
    await deny_master(session, actor)
    current = (await session.scalars(select(AdminUser).where(AdminUser.id == actor.id).execution_options(populate_existing=True))).one_or_none()
    if not (permitted(current, any_read=True)):
        raise HTTPException(403, 'Fiscal permission required')
    reasons = [] if available(session) else ['mock_fiscal_disabled']
    return {'mode': 'mock_only', 'available': not reasons, 'blocking_reasons': reasons,
            'cash_registers': [register_data(await cash_register(session))]}


def audit(session, actor, operation, payload, document=None):
    session.add(FiscalEvent(document_id=document.id if document else None, actor_id=actor.id,
        operation=operation, payload=payload, created_at=datetime.now(UTC)))


async def intent_for(session, actor, raw_key, scope, payload):
    return await claim_intent(session, operation_key(raw_key, f'fiscal:{actor.id}:{scope}'), digest(payload.model_dump(mode='json')))


async def shift(session, actor, register_id, payload, key, *, opening):
    actor = await require(session, actor, settings_permission=True)
    if register_id != REGISTER:
        fail('register_forbidden', 'Only the fixed mock register is supported')
    intent = await intent_for(session, actor, key, 'open' if opening else 'close', payload)
    if intent.response_json:
        return intent.response_json
    register = await cash_register(session, lock=True)
    if register.credential_kind != 'mock':
        fail('register_forbidden', 'Mock credentials required')
    if payload.expected_shift_revision != register.shift_revision:
        fail('shift_revision_conflict', 'Reload register state')
    desired = 'open' if opening else 'closed'
    if desired == register.shift_status:
        fail('shift_state_conflict', 'Shift is already in requested state')
    if not opening:
        from app.services.fiscal_automation import block_shift_close
        await block_shift_close(session, register.id, register.shift_revision)
    register.shift_status = desired
    register.shift_revision += 1
    audit(session, actor, 'open_shift' if opening else 'close_shift', register_data(register))
    result = {'mode': 'mock_only', 'available': True, 'blocking_reasons': [], 'cash_registers': [register_data(register)]}
    intent.response_json = result
    await session.commit()
    return result


def exact_minor(value):
    amount = Decimal(value)
    cents = amount * 100
    if not amount.is_finite() or cents != cents.to_integral_value():
        fail('invalid_order_amounts', 'Prices must be exact kopecks')
    return int(cents)


def snapshot(order, register, *, qr_consent=False):
    from app.services.mock_cod import validated_cod_snapshot
    payments = getattr(order, 'payments', ())
    cod = validated_cod_snapshot(order, payments[0]) if not qr_consent and len(payments) == 1 else None
    if order.status == OrderStatus.cancelled or (not cod and (not order.simulation or order.channel != 'backoffice_pos')):
        fail('simulation_sale_required', 'A simulated POS sale or verified local mock COD payment is required')
    total = exact_minor(order.total_amount)
    if qr_consent:
        if order.payments or total <= 0:
            fail('unpaid_sale_required', 'QR requires an unpaid positive full sale')
        payment_data = {'amount_minor': total, 'method': 'monobank_qr',
            'source': 'provider_confirmation', 'simulation': True}
    else:
        if len(order.payments) != 1:
            fail('simulation_payment_required', 'Exactly one immutable simulated payment required')
        payment = order.payments[0]
        if (not payment.simulation or (not cod and (payment.source != 'operator_confirmation' or payment.method not in ('cash', 'card')))
                or payment.amount_minor != total or total <= 0):
            fail('simulation_payment_required', 'Payment must be the exact full stage-2 simulated payment')
        payment_data = {'id': payment.id, 'amount_minor': payment.amount_minor, 'method': payment.method,
            'source': payment.source, 'simulation': True}
    items = []
    for item in sorted(order.items, key=lambda row: row.id):
        base, price, discount = (exact_minor(value) for value in
            (item.base_price if item.base_price is not None else item.price, item.price, item.discount_amount))
        line_total = exact_minor(item.total_price if item.total_price is not None else item.price * item.quantity)
        if (item.quantity <= 0 or min(base, price, discount, line_total) < 0
                or line_total != price * item.quantity or base * item.quantity - discount != line_total):
            fail('invalid_order_amounts', 'Invalid item price, quantity or discount')
        items.append({'order_item_id': item.id, 'product_id': item.product_id, 'name': item.product_name,
            'sku': item.product_sku, 'quantity': item.quantity, 'base_price_minor': base,
            'price_minor': price, 'discount_minor': discount, 'total_minor': line_total,
            'tax': {'code': 'MOCK_NO_TAX', 'rate_basis_points': 0, 'amount_minor': 0}})
    subtotal, discount = exact_minor(order.subtotal_amount), exact_minor(order.discount_amount)
    additional_discount = sum(i['total_minor'] for i in items) - total
    if (not items or subtotal != sum(i['base_price_minor'] * i['quantity'] for i in items)
            or subtotal - discount != total or discount < 0 or additional_discount < 0):
        fail('invalid_order_amounts', 'Order totals and discounts do not reconcile')
    return {'document_kind': 'mock_fiscal_receipt', 'simulation': True, 'notice': NOTICE,
        'is_sent_dps': False, 'currency': 'UAH', 'order_id': order.id, 'order_revision': order.revision,
        'cash_register_id': REGISTER, 'credential_kind': 'mock', 'shift_revision': register.shift_revision,
        'items': items, 'subtotal_minor': subtotal, 'discount_minor': discount,
        'additional_discount_minor': additional_discount, 'amount_minor': total,
        'payment': payment_data,
        **({'cod': cod} if cod else {}),
        'taxes': [{'code': 'MOCK_NO_TAX', 'rate_basis_points': 0, 'amount_minor': 0}],
        'tax_mapping': 'mock_only_no_legal_tax_claim', 'delivery_channel': 'local_display_print'}


def register_ready(register, register_id=REGISTER):
    if register_id != REGISTER or not register or register.credential_kind != 'mock':
        fail('register_forbidden', 'Fixed allowlisted mock register required')
    if register.shift_status != 'open':
        fail('shift_closed', 'Explicitly open the mock shift first')


async def preview_response(session, actor, preview):
    reasons = []
    if not available(session):
        reasons.append('mock_fiscal_disabled')
    if preview.expires_at <= datetime.now(UTC):
        reasons.append('preview_expired')
    order = await local_sales.load(session, preview.order_id)
    register = await cash_register(session)
    if not register or register.shift_status != 'open' or register.shift_revision != preview.shift_revision:
        reasons.append('shift_changed')
    elif preview.snapshot.get('kind') == 'return':
        from app.services.fiscal_returns import return_snapshot
        from app.models.fiscal import ReturnRecord
        row = await session.get(ReturnRecord, preview.snapshot['return_id'])
        try:
            if not row or digest(await return_snapshot(session, row, register)) != preview.snapshot_hash:
                reasons.append('stale_preview')
        except HTTPException as error:
            reasons.append(error.detail['code'])
    elif order.revision != preview.revision:
        reasons.append('stale_preview')
    else:
        try:
            if digest(snapshot(order, register)) != preview.snapshot_hash:
                reasons.append('stale_preview')
        except HTTPException as error:
            reasons.append(error.detail['code'])
    if preview.snapshot.get('kind', 'sale') == 'sale':
        from app.services.fiscal_automation import owner
        if await owner(session, preview.order_id) == 'checkbox_ettn':
            reasons.append('automation_owns_payment')
    if await session.scalar(select(FiscalDocument.id).where(FiscalDocument.preview_id == preview.id)):
        reasons.append('preview_consumed')
    return FiscalPreviewResponse(kind=preview.snapshot.get('kind', 'sale'), parent_sale_id=preview.snapshot.get('parent_sale_id'), return_id=preview.snapshot.get('return_id'), id=preview.id, order_id=preview.order_id, revision=preview.revision,
        cash_register_id=preview.cash_register_id, shift_revision=preview.shift_revision,
        snapshot_hash=preview.snapshot_hash, snapshot=preview.snapshot, expires_at=preview.expires_at,
        allowed_actions=[] if reasons else ['submit'], blocking_reasons=reasons)


async def create_preview(session, actor, order_id, payload, key):
    actor = await require(session, actor)
    intent = await intent_for(session, actor, key, f'preview:{order_id}', payload)
    if intent.response_json:
        return FiscalPreviewResponse.model_validate(intent.response_json)
    order = await local_sales.load(session, order_id, lock=True)
    if order.__dict__.get("master_mono_binding"):
        fail("master_mono_external_issuer_required", "Internal issuance is forbidden for this direct mono sale")
    register = await cash_register(session, lock=True)
    register_ready(register, payload.cash_register_id)
    state = snapshot(order, register)
    now = datetime.now(UTC)
    preview = FiscalPreview(order_id=order.id, actor_id=actor.id, revision=order.revision,
        cash_register_id=REGISTER, shift_revision=register.shift_revision, snapshot=state,
        snapshot_hash=digest(state), created_at=now, expires_at=now + timedelta(minutes=10))
    session.add(preview)
    await session.flush()
    result = await preview_response(session, actor, preview)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode='json')
    await session.commit()
    return result


async def get_preview(session, actor, order_id, preview_id):
    preview = await session.get(FiscalPreview, preview_id)
    await require(session, actor, mutation=False, kind=preview.snapshot.get('kind', 'sale') if preview else 'sale')
    if not preview or preview.order_id != order_id:
        raise HTTPException(404, 'Preview not found')
    return await preview_response(session, actor, preview)


def document_response(session, actor, document):
    reasons = [] if available(session) and permitted(actor, kind=document.kind) else ['mock_fiscal_disabled_or_forbidden']
    actions = []
    if not reasons:
        if document.status == 'queued' and document.dispatched_at is None:
            actions.extend(['process', 'cancel'])
        elif document.status in ('unknown', 'processing', 'submitting'):
            actions.append('process')
        elif document.status == 'issued':
            actions.extend(['print', 'delivery'])
    if permitted(actor, kind=document.kind):
        if document.status == 'issued':
            actions = ['print', 'delivery']
        elif document.status in ('unknown', 'processing', 'submitting') and document.dispatched_at:
            actions = ['process']
    if document.status == 'issued' and available(session) and permitted(actor, kind=document.kind):
        actions.extend(['share', 'revoke_share'])
    if document.status == 'error':
        reasons.append('approval_invalidated')
        if not document.dispatched_at and not reasons[:-1]:
            actions = ['cancel']
    if document.qr_attempt_id is not None:
        actions = ['print'] if document.status == 'issued' and permitted(actor, kind=document.kind) else []
    return FiscalDocumentResponse(qr_attempt_id=document.qr_attempt_id, automation_id=document.automation_id, source='qr' if document.qr_attempt_id is not None else ('automation' if document.automation_id is not None else 'manual'), kind=document.kind, parent_sale_id=document.parent_sale_id, return_id=document.return_id, id=document.id, order_id=document.order_id, status=document.status,
        revision=document.revision, provider_uuid=document.provider_uuid, cash_register_id=document.cash_register_id,
        shift_revision=document.shift_revision, snapshot_hash=document.snapshot_hash, snapshot=document.snapshot,
        delivery={'status': document.delivery_status, 'attempts': document.delivery_attempts, 'last_error': document.delivery_error},
        allowed_actions=actions, blocking_reasons=reasons,
        print_url=f'/api/v1/backoffice/fiscal-documents/{document.id}/print' if document.status == 'issued' else None)


async def submit(session, actor, order_id, payload, key):
    initial_preview = await session.get(FiscalPreview, payload.preview_id)
    kind = initial_preview.snapshot.get('kind', 'sale') if initial_preview else 'sale'
    actor = await require(session, actor, kind=kind, reconcile=True)
    if payload.confirm is not True:
        fail('confirmation_required', 'Explicit approval required')
    intent = await intent_for(session, actor, key, f'submit:{order_id}', payload)
    if intent.response_json:
        return FiscalDocumentResponse.model_validate(intent.response_json)
    actor = await require(session, actor, kind=kind)
    order = await local_sales.load(session, order_id, lock=True)
    if order.__dict__.get("master_mono_binding"):
        fail("master_mono_external_issuer_required", "Internal issuance is forbidden for this direct mono sale")
    register = await cash_register(session, lock=True)
    register_ready(register, payload.cash_register_id)
    preview = await session.get(FiscalPreview, payload.preview_id)
    if not preview or preview.order_id != order_id or preview.actor_id != actor.id:
        fail('preview_not_found', 'Approval must use your preview for this order')
    if preview.expires_at <= datetime.now(UTC):
        fail('preview_expired', 'Create a new preview')
    if kind == 'return':
        from app.services.fiscal_returns import return_snapshot
        from app.models.fiscal import ReturnRecord
        row = await session.get(ReturnRecord, preview.snapshot['return_id'])
        if not row or row.order_id != order_id or payload.expected_revision != preview.revision:
            fail('revision_conflict', 'Return preview revision changed')
        expected_snapshot = await return_snapshot(session, row, register)
    else:
        row = None
        if order.revision != payload.expected_revision or order.revision != preview.revision:
            fail('revision_conflict', 'Reload order and create a new preview')
        expected_snapshot = snapshot(order, register)
    if (register.shift_revision != preview.shift_revision or preview.cash_register_id != payload.cash_register_id
            or payload.snapshot_hash != preview.snapshot_hash or digest(expected_snapshot) != preview.snapshot_hash):
        fail('stale_preview', 'Order, register or shift changed; create a new preview')
    if await session.scalar(select(FiscalDocument.id).where(FiscalDocument.preview_id == preview.id)):
        fail('preview_consumed', 'Create a new preview after cancellation')
    if not row:
        from app.services.fiscal_automation import assert_manual_owner
        await assert_manual_owner(session, order_id)
    scope = FiscalDocument.return_id == row.id if row else FiscalDocument.order_id == order_id
    if await session.scalar(select(FiscalDocument.id).where(scope, FiscalDocument.kind == kind, FiscalDocument.status != 'cancelled')):
        fail('return_already_submitted' if row else 'sale_already_submitted', 'This scope already has a fiscal approval')
    payment_id = (await session.get(FiscalDocument, row.parent_sale_id)).payment_id if row else order.payments[0].id
    document = FiscalDocument(kind=kind, parent_sale_id=row.parent_sale_id if row else None, return_id=row.id if row else None,
        order_id=order_id, payment_id=payment_id, preview_id=preview.id,
        actor_id=actor.id, provider_uuid=str(uuid4()), cash_register_id=REGISTER, shift_revision=preview.shift_revision,
        snapshot=preview.snapshot, snapshot_hash=preview.snapshot_hash, status='queued', revision=1,
        simulation=True, mock_outcome=payload.mock_outcome, approved_at=datetime.now(UTC),
        delivery_status='pending_document', delivery_attempts=0)
    session.add(document)
    await session.flush()
    session.add(FiscalJob(document_id=document.id, status='pending', attempts=0, fence=0, next_attempt_at=datetime.now(UTC)))
    audit(session, actor, 'approved', {'preview_id': preview.id, 'snapshot_hash': preview.snapshot_hash, 'confirm': True}, document)
    result = document_response(session, actor, document)
    intent.order_id, intent.response_json = order_id, result.model_dump(mode='json')
    await session.commit()
    return result


async def get_document(session, actor, document_id):
    document = await session.get(FiscalDocument, document_id)
    actor = await require(session, actor, mutation=False, kind=document.kind if document else 'sale')
    if not document:
        raise HTTPException(404, 'Fiscal document not found')
    if document.status == 'issued':
        await validate_issued_document(session, document)
    return document_response(session, actor, document)


async def list_documents(session, actor, order_id):
    actor = await require(session, actor, mutation=False, any_read=True)
    await local_sales.load(session, order_id)
    documents = (await session.scalars(select(FiscalDocument).where(FiscalDocument.order_id == order_id).order_by(FiscalDocument.id))).all()
    for document in documents:
        if document.status == 'issued':
            await validate_issued_document(session, document)
    return [document_response(session, actor, document) for document in documents]


async def lock_document(session, document_id):
    order_id = await session.scalar(select(FiscalDocument.order_id).where(FiscalDocument.id == document_id))
    if order_id is None:
        raise HTTPException(404, 'Fiscal document not found')
    order = await local_sales.load(session, order_id, lock=True)
    register = await cash_register(session, lock=True)
    document = (await session.scalars(select(FiscalDocument).where(FiscalDocument.id == document_id)
        .with_for_update().execution_options(populate_existing=True))).one()
    job = (await session.scalars(select(FiscalJob).where(FiscalJob.document_id == document_id)
        .with_for_update().execution_options(populate_existing=True))).one_or_none()
    if document.qr_attempt_id is not None:
        fail('qr_owns_receipt', 'Canonical QR receipt cannot dispatch again')
    if document.automation_id is not None:
        await validate_issued_document(session, document)
        if job is not None:
            fail('automation_receipt_mismatch', 'Canonical automation receipt cannot have a dispatch job')
    elif job is None:
        fail('fiscal_job_missing', 'Manual fiscal approval requires its durable dispatch job')
    return order, register, document, job


def deliver(session, actor, document, outcome):
    # Local display/print is the entire adapter. No messaging service is called.
    document.delivery_status = 'queued'
    audit(session, actor, 'delivery_queued', {'channel': 'local_display_print'}, document)
    document.delivery_attempts += 1
    document.delivery_status = outcome
    document.delivery_error = 'mock_delivery_failure' if outcome == 'failed' else None
    audit(session, actor, 'delivery_result', {'status': outcome, 'attempt': document.delivery_attempts}, document)


async def invalidate(session, actor, document, job, code):
    document.status = 'error'
    document.revision += 1
    job.status, job.lease_until = 'blocked', None
    audit(session, actor, 'approval_invalidated', {'code': code}, document)
    await session.commit()


async def process_job(session, actor, document_id, *, delivery_outcome='delivered'):
    """Explicit bounded durable worker: claim/commit then fence and dispatch.

    Concurrent API workers and mock inbox events share register/document/job locks.
    No startup scheduler is registered. Error/invalidated approvals fail closed.
    """
    if await session.scalar(select(FiscalDocument.qr_attempt_id).where(FiscalDocument.id == document_id)) is not None:
        fail('qr_owns_receipt', 'Canonical QR receipt cannot be processed or mutated manually')
    kind = await session.scalar(select(FiscalDocument.kind).where(FiscalDocument.id == document_id))
    actor = await require(session, actor, reconcile=True, kind=kind)
    if await session.scalar(select(FiscalDocument.automation_id).where(FiscalDocument.id == document_id)) is not None:
        fail('automation_document_terminal', 'Automation receipt is already issued and has no worker dispatch')
    order, register, document, job = await lock_document(session, document_id)
    if document.status in ('issued', 'cancelled', 'error'):
        return document
    now = datetime.now(UTC)
    if job.status == 'leased' and job.lease_until and job.lease_until > now:
        return document
    if job.next_attempt_at > now:
        return document
    job.status, job.lease_until = 'leased', now + timedelta(seconds=30)
    job.fence += 1
    job.attempts += 1
    fence = job.fence
    await session.commit()
    # Recheck actual flags, connection and originating permission after claim.
    # The originating actor lock precedes sale/register/document locks consistently.
    try:
        actor = await require(session, actor, reconcile=True, kind=kind)
    except HTTPException as error:
        await session.rollback()
        order, register, document, job = await lock_document(session, document_id)
        if job.fence == fence and document.dispatched_at is None and document.status not in ('issued', 'cancelled', 'error'):
            await invalidate(session, actor, document, job, 'worker_permission_or_configuration_revoked')
        raise error
    origin = (await session.scalars(select(AdminUser).where(AdminUser.id == document.actor_id)
        .with_for_update(read=True).execution_options(populate_existing=True))).one_or_none()
    order, register, document, job = await lock_document(session, document_id)
    if job.fence != fence or job.status != 'leased' or document.status in ('issued', 'cancelled', 'error'):
        return document
    if not job.lease_until or job.lease_until <= datetime.now(UTC):
        return document
    # Accepted provider truth wins over later changes to order, shift or
    # originating permissions. Reconciliation never creates a replacement sale.
    receipt = await session.get(MockReceipt, document.provider_uuid)
    if receipt is not None:
        if receipt.document_id != document.id or receipt.snapshot_hash != document.snapshot_hash:
            fail('mock_receipt_mismatch', 'Stored receipt does not match approval')
        document.status = 'issued'
        job.status, job.lease_until = 'done', None
        deliver(session, actor, document, delivery_outcome)
        audit(session, actor, 'reconciled', {'provider_uuid': document.provider_uuid}, document)
    elif document.dispatched_at is not None:
        # Missing accepted truth remains unknown, even if permission/shift changed.
        document.status = 'unknown'
        job.status, job.lease_until = 'pending', None
        job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=min(2 ** job.attempts, 300))
    else:
        reason = None
        if not available(session):
            reason = 'mock_fiscal_disabled'
        elif not permitted(origin, kind=document.kind):
            reason = 'origin_permission_revoked'
        elif not document.simulation or register.credential_kind != 'mock' or register.shift_status != 'open' or register.shift_revision != document.shift_revision:
            reason = 'shift_changed'
        else:
            try:
                if document.kind == 'return':
                    from app.services.fiscal_returns import return_snapshot
                    from app.models.fiscal import ReturnRecord
                    row = await session.get(ReturnRecord, document.return_id)
                    current_snapshot = await return_snapshot(session, row, register)
                else:
                    current_snapshot = snapshot(order, register)
                if digest(current_snapshot) != document.snapshot_hash:
                    reason = 'stale_preview'
            except HTTPException as error:
                reason = error.detail['code']
        if not reason and document.kind == 'sale':
            from app.services.fiscal_automation import assert_manual_owner
            try:
                await assert_manual_owner(session, document.order_id)
            except HTTPException as error:
                reason = error.detail['code']
        if reason:
            await invalidate(session, actor, document, job, reason)
            return document
        document.dispatched_at = datetime.now(UTC)
        session.add(MockReceipt(provider_uuid=document.provider_uuid, document_id=document.id,
            snapshot_hash=document.snapshot_hash, accepted_at=datetime.now(UTC)))
        audit(session, actor, 'mock_accepted', {'provider_uuid': document.provider_uuid}, document)
        if document.mock_outcome == 'timeout_after_acceptance':
            document.status = 'unknown'
            job.status, job.lease_until = 'pending', None
            job.next_attempt_at = datetime.now(UTC)
        else:
            document.status = 'issued'
            job.status, job.lease_until = 'done', None
            deliver(session, actor, document, delivery_outcome)
    document.revision += 1
    audit(session, actor, 'worker_result', {'status': document.status, 'fence': fence}, document)
    await session.commit()
    return document


async def command(session, actor, document_id, payload, key, operation):
    if await session.scalar(select(FiscalDocument.qr_attempt_id).where(FiscalDocument.id == document_id)) is not None:
        fail('qr_owns_receipt', 'Canonical QR receipt cannot be processed or mutated manually')
    kind = await session.scalar(select(FiscalDocument.kind).where(FiscalDocument.id == document_id))
    actor = await require(session, actor, kind=kind, reconcile=operation in ('process', 'delivery'))
    if operation in ('process', 'cancel') and await session.scalar(select(FiscalDocument.automation_id).where(FiscalDocument.id == document_id)) is not None:
        fail('automation_document_terminal', 'Canonical automation receipt cannot be submitted or cancelled manually')
    intent = await intent_for(session, actor, key, f'{operation}:{document_id}', payload)
    if intent.response_json:
        return FiscalDocumentResponse.model_validate(intent.response_json)
    order, register, document, job = await lock_document(session, document_id)
    if document.revision != payload.expected_revision:
        fail('revision_conflict', 'Reload fiscal document')
    if operation == 'process':
        # Freeze the admission acknowledgement before the worker's lease commit.
        # Every replay (including concurrent requests) returns this exact ACK;
        # GET is the source of the current result. A crash after this commit
        # leaves the durable job available to a later explicit processing call.
        acknowledgement = document_response(session, actor, document)
        intent.order_id = document.order_id
        intent.response_json = acknowledgement.model_dump(mode='json')
        await session.commit()
        await process_job(session, actor, document_id,
            delivery_outcome=payload.mock_delivery_outcome)
        return acknowledgement
    elif operation == 'cancel':
        if (document.status not in ('queued', 'error') or document.dispatched_at is not None
                or await session.get(MockReceipt, document.provider_uuid) is not None):
            fail('already_dispatched', 'Only a queued approval before dispatch may be cancelled')
        document.status = 'cancelled'
        document.revision += 1
        job.status, job.lease_until = 'cancelled', None
        job.fence += 1
        audit(session, actor, 'cancelled', {}, document)
    elif operation == 'delivery':
        if document.status != 'issued':
            fail('receipt_not_issued', 'Delivery requires the existing issued mock document')
        await validate_issued_document(session, document)
        deliver(session, actor, document, payload.mock_outcome)
        document.revision += 1
    result = document_response(session, actor, document)
    intent.response_json = result.model_dump(mode='json')
    await session.commit()
    return result


async def inbox(session, actor, payload, key):
    if await session.scalar(select(FiscalDocument.qr_attempt_id).where(FiscalDocument.provider_uuid == str(payload.provider_operation_id))) is not None:
        fail('qr_owns_receipt', 'QR receipt only accepts its invoice recovery path')
    kind = await session.scalar(select(FiscalDocument.kind).where(FiscalDocument.provider_uuid == str(payload.provider_operation_id)))
    actor = await require(session, actor, reconcile=True, kind=kind)
    if await session.scalar(select(FiscalDocument.automation_id).where(FiscalDocument.provider_uuid == str(payload.provider_operation_id))) is not None:
        fail('automation_document_terminal', 'Canonical automation receipt does not accept manual mock inbox events')
    intent = await intent_for(session, actor, key, 'inbox', payload)
    if intent.response_json:
        return FiscalDocumentResponse.model_validate(intent.response_json)
    document_id = await session.scalar(select(FiscalDocument.id).where(FiscalDocument.provider_uuid == str(payload.provider_operation_id)))
    if document_id is None:
        raise HTTPException(404, 'Mock operation not found')
    origin_id = await session.scalar(select(FiscalDocument.actor_id).where(FiscalDocument.id == document_id))
    origin = (await session.scalars(select(AdminUser).where(AdminUser.id == origin_id).with_for_update(read=True).execution_options(populate_existing=True))).one_or_none()
    order, register, document, job = await lock_document(session, document_id)
    existing = await session.get(FiscalInbox, str(payload.event_id))
    if existing and (existing.provider_uuid != document.provider_uuid or existing.status != payload.status):
        fail('inbox_conflict', 'Event already has a different payload')
    if not existing:
        session.add(FiscalInbox(event_id=str(payload.event_id), provider_uuid=document.provider_uuid,
            status=payload.status, received_at=datetime.now(UTC)))
        # An inbox event cannot manufacture acceptance or authorize dispatch.
        if document.status not in ('issued', 'cancelled', 'error') and document.dispatched_at:
            receipt = await session.get(MockReceipt, document.provider_uuid)
            if (receipt is not None and receipt.document_id == document.id
                    and receipt.snapshot_hash == document.snapshot_hash and payload.status == 'issued'):
                document.status = 'issued'
                document.revision += 1
                job.status, job.lease_until = 'done', None
                job.fence += 1
                deliver(session, actor, document, 'delivered')
            elif payload.status == 'processing' and document.status != 'processing':
                document.status = 'processing'
                document.revision += 1
        audit(session, actor, 'mock_inbox', {'event_id': str(payload.event_id), 'status': payload.status}, document)
    result = document_response(session, actor, document)
    intent.response_json = result.model_dump(mode='json')
    await session.commit()
    return result


async def validate_issued_document(session, document):
    receipt = await session.get(MockReceipt, document.provider_uuid) if document else None
    if (not document or document.status != 'issued' or not document.simulation
            or document.snapshot.get('document_kind') != 'mock_fiscal_receipt'
            or digest(document.snapshot) != document.snapshot_hash or not receipt
            or receipt.document_id != document.id or receipt.snapshot_hash != document.snapshot_hash):
        fail('mock_receipt_mismatch', 'Issued mock receipt requires matching durable acceptance')
    if document.kind == 'sale':
        payment = await session.get(OrderPayment, document.payment_id)
        if document.snapshot.get('cod') or (payment and payment.source == 'mock_cod_confirmation'):
            from app.services.mock_cod import validated_cod_snapshot
            order = await local_sales.load(session, document.order_id)
            provenance = validated_cod_snapshot(order, payment)
            if not provenance or document.snapshot.get('cod') != provenance:
                fail('mock_cod_receipt_mismatch', 'Original immutable mock COD confirmation required')
    if document.qr_attempt_id is not None:
        from app.services.qr_payment import validate_document
        await validate_document(session, document)
    elif document.automation_id is not None:
        from app.services.fiscal_automation import validate_linked_document
        await validate_linked_document(session, document)
    else:
        from app.models.fiscal_automation import FiscalAutomation
        if (document.snapshot.get('source') == 'mock_ettn' or document.snapshot.get('automation')
                or await session.scalar(select(FiscalAutomation.id).where(FiscalAutomation.provider_uuid == document.provider_uuid))):
            fail('automation_receipt_mismatch', 'Automation provenance requires its canonical immutable link')
    if document.kind == 'return':
        from app.services.fiscal_returns import parent_sale
        await parent_sale(session, document.parent_sale_id)
    return document


def print_html(document):
    if document.snapshot.get('transport') == 'provider_sandbox':
        from app.services.sandbox import print_summary
        return print_summary(document)
    rows = ''.join('<tr><td>' + escape(str(i['name'] or '')) + '</td><td>' + str(i['quantity'])
        + '</td><td>' + f"{Decimal(i['total_minor']) / 100:.2f}" + '</td></tr>' for i in document.snapshot['items'])
    return ('<!doctype html><html lang="uk"><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
        '<title>MOCK Checkbox</title><h1>' + NOTICE + '</h1><p>' + escape(getattr(document, 'kind', 'sale')) + '</p><table>' + rows
        + '</table><p>UAH ' + f"{Decimal(document.snapshot['amount_minor']) / 100:.2f}"
        + '</p><p>Локальний тестовий показ / друк.</p></html>')
