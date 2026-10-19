"""Explicit remote sandbox bridge into the existing isolated sales ledger.

An invoice dispatch is committed before HTTP. Ambiguous creation never repeats
POST, and Checkbox recovery always uses the same preallocated receipt UUID.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.models.qr_payment import QRPaymentAttempt, QRProviderRecord, QRCommand
from app.models.sandbox import SandboxRun
from app.models.order import OrderStatus
from app.schemas.qr_payment import QRAttemptResponse
from app.services import fiscal, local_sales, qr_payment, sales_access
from app.services.order_documents import claim_intent, digest, operation_key


def adapter(provider=None):
    if provider is not None:
        return provider
    from app.services.sandbox_providers import get_adapter
    return get_adapter()


async def require(session, actor):
    actor = await qr_payment.require(session, actor, recovery=True)
    if not settings.provider_sandbox_enabled or not local_sales.simulation_available(session):
        fiscal.fail('provider_sandbox_disabled', 'Explicit sandbox flag and isolated test database required')
    return actor


async def readiness(session, actor, *, provider=None):
    await qr_payment.require(session, actor, recovery=True)
    if not settings.provider_sandbox_enabled or not local_sales.simulation_available(session):
        return {'mode': 'provider_sandbox', 'available': False,
            'blocking_reasons': ['provider_sandbox_disabled'], 'verification': {'provider_confirmed': False}}
    data = await adapter(provider).readiness()
    register = await fiscal.cash_register(session)
    if not register or register.shift_status != 'open':
        data = dict(data, available=False, blocking_reasons=[*data.get('blocking_reasons', []), 'local_shift_closed'])
    return dict(data, mode='provider_sandbox')


async def verified(provider, *, recovery=False):
    result = await provider.readiness(require_open_shift=not recovery)
    if not result.get('available'):
        fiscal.fail('sandbox_not_verified', 'Test provider identities and dedicated token provenance must be verified')
    return result


def serial(data):
    return {k: (v.astimezone(UTC).isoformat() if isinstance(v, datetime) else v) for k, v in data.items()}


async def run_for(session, attempt_id, *, lock=False):
    query = select(SandboxRun).where(SandboxRun.attempt_id == attempt_id).execution_options(populate_existing=True)
    return await session.scalar(query.with_for_update() if lock else query)


async def create(session, actor, order_id, payload, key, *, provider=None):
    actor = await require(session, actor)
    order = await local_sales.load(session, order_id, lock=True)
    await sales_access.own_sale(session, actor, order)
    intent_key = operation_key(key, f'sandbox:{actor.id}:create:{order_id}')
    intent = await claim_intent(session, intent_key, digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return await qr_payment.get(session, actor, intent.response_json['id'])
    if order.revision != payload.expected_revision:
        fiscal.fail('revision_conflict', 'Reload order before creating QR')
    if order.status in (OrderStatus.completed, OrderStatus.cancelled) or order.handovers or order.shipments:
        fiscal.fail('qr_sale_unavailable', 'Sandbox requires an open local pickup POS sale')
    if await qr_payment.owner(session, order_id):
        fiscal.fail('qr_attempt_active', 'Recover the existing invoice before creating another QR')
    from app.services.fiscal_automation import owner as fiscal_owner
    if await fiscal_owner(session, order_id):
        fiscal.fail('fiscal_owner_exists', 'Another fiscal strategy owns this sale')
    provider = adapter(provider)
    proof = await verified(provider)  # Checkbox test proof precedes monobank POST.
    register = await fiscal.cash_register(session, lock=True)
    fiscal.register_ready(register, payload.cash_register_id)
    state = fiscal.snapshot(order, register, qr_consent=True)
    pins = {name: proof[name] for name in ('checkbox_cash_register_id', 'checkbox_cashier_id', 'checkbox_organization_id')}
    reference = str(uuid4())
    pins['reference'] = reference
    pins['checkbox_shift_id'] = proof['verification']['shift_id']
    state.update(source='sandbox_monobank_qr', transport='provider_sandbox', sandbox=pins,
        consent={'actor_id': actor.id, 'confirm': True}, provider_contract='remote_test_provider', fee_minor=0)
    now = datetime.now(UTC)
    row = QRPaymentAttempt(order_id=order_id, actor_id=actor.id, provider_invoice_id=reference,
        cash_register_id=register.id, shift_revision=register.shift_revision, amount_minor=state['amount_minor'],
        snapshot=state, snapshot_hash=digest(state), payment_status='waiting', receipt_status='pending',
        revision=1, simulation=True, created_at=now, expires_at=now + timedelta(minutes=10))
    session.add(row)
    await session.flush()
    run = SandboxRun(attempt_id=row.id, receipt_uuid=str(uuid4()),
        invoice_dispatched=True, receipt_dispatched=False, verification=proof['verification'],
        payment_status='waiting', receipt_status='pending', created_at=now)
    session.add(run)
    await session.flush()
    session.add(QRProviderRecord(provider_invoice_id=row.provider_invoice_id, attempt_id=row.id,
        snapshot_hash=row.snapshot_hash, payment_status='waiting', receipt_status='pending'))
    session.add(QRCommand(attempt_id=row.id, actor_id=actor.id, operation='create', operation_key=intent_key,
        payload=payload.model_dump(mode='json'), created_at=now))
    order.revision += 1
    await session.flush()
    result = await qr_payment.response(session, actor, row)
    intent.order_id, intent.response_json = order_id, result.model_dump(mode='json')
    await session.commit()  # Durable dispatch fence: never retry invoice POST.
    from app.services.sandbox_providers import SandboxProviderError
    try:
        actor = await require(session, actor)
        await sales_access.own_sale(session, actor, await local_sales.load(session, order_id, lock=True))
    except HTTPException:
        run.last_error = 'sandbox_dispatch_authorization_revoked'
        row.payment_status = 'unknown'
        await session.commit()
        raise
    try:
        invoice = await provider.create_invoice(reference=row.provider_invoice_id,
            amount_minor=row.amount_minor, expires_seconds=600)
    except SandboxProviderError as error:
        run.invoice_id = error.invoice_id
        run.last_error = error.code
        row.payment_status = 'unknown'
        await session.commit()
        return await qr_payment.response(session, actor, row)
    run.invoice_id, run.payment_url = invoice['invoice_id'], invoice['payment_url']
    await session.commit()
    return await qr_payment.response(session, actor, row)


async def refresh(session, actor, attempt_id, payload, key, *, provider=None):
    actor = await require(session, actor)
    order_id = await session.scalar(select(QRPaymentAttempt.order_id).where(QRPaymentAttempt.id == attempt_id))
    if order_id is None:
        raise HTTPException(404, 'QR attempt not found')
    await sales_access.own_sale(session, actor, await local_sales.load(session, order_id, lock=True))
    intent_key = operation_key(key, f'sandbox:{actor.id}:refresh:{attempt_id}')
    intent = await claim_intent(session, intent_key, digest(payload.model_dump(mode='json')))
    if intent.response_json:
        return QRAttemptResponse.model_validate(intent.response_json)
    row = await session.get(QRPaymentAttempt, attempt_id)
    run = await run_for(session, attempt_id, lock=True)
    if not run:
        fiscal.fail('sandbox_attempt_required', 'This is not a remote sandbox attempt')
    if row.revision != payload.expected_revision:
        fiscal.fail('revision_conflict', 'Reload the same QR attempt')
    provider = adapter(provider)
    from app.services.sandbox_providers import SandboxProviderError
    try:
        proof = await verified(provider, recovery=True)
        if proof['verification'].get('monobank_token_sha256') != run.verification.get('monobank_token_sha256'):
            fiscal.fail('sandbox_credentials_changed', 'Restore the attested token used for this attempt')
        if any(proof.get(name) != value for name, value in row.snapshot['sandbox'].items() if name not in ('reference', 'checkbox_shift_id')):
            fiscal.fail('sandbox_identity_changed', 'Pinned sandbox identities changed')
        if not run.invoice_id:
            run.last_error = 'invoice_create_unknown'
            row.payment_status = 'unknown'
        else:
            remote = await provider.get_invoice(run.invoice_id)
            if (remote['invoice_id'] != run.invoice_id or remote['amount_minor'] != row.amount_minor
                    or remote['currency'] != 'UAH' or remote.get('reference') != row.provider_invoice_id):
                fiscal.fail('sandbox_invoice_mismatch', 'Provider invoice differs from immutable sale')
            if run.payment_status not in ('paid', *qr_payment.TERMINAL_UNPAID):
                if remote['status'] == 'unknown':
                    raise SandboxProviderError('sandbox_invoice_status_unknown')
                run.payment_status = remote['status']
                run.payment_evidence = serial(remote)
                run.accepted_at = remote.get('accepted_at') if run.payment_status == 'paid' else None
            if run.payment_status == 'paid' and not run.accepted_at:
                fiscal.fail('sandbox_acceptance_missing', 'Paid invoice requires provider acceptance time')
            await session.flush([run])
            truth = await session.get(QRProviderRecord, row.provider_invoice_id)
            truth.payment_status, truth.accepted_at = run.payment_status, run.accepted_at
            run.last_error = None
            await session.flush()
            await qr_payment.reconcile(session, row, truth)
            # First persist accepted payment and stable receipt identity before writes.
            if run.payment_status == 'paid' and run.receipt_status != 'issued':
                await session.commit()
                # Reacquire the same global order lock after the acceptance commit.
                await local_sales.load(session, order_id, lock=True)
                run = await run_for(session, attempt_id, lock=True)
                if run.receipt_status != 'issued':
                    receipt = await provider.get_receipt(run.receipt_uuid) if run.receipt_dispatched else None
                    if receipt is None and not run.receipt_dispatched:
                        actor = await require(session, actor)
                        await sales_access.own_sale(session, actor, await local_sales.load(session, order_id, lock=True))
                        shift = await provider.ensure_test_shift()
                        if shift.get('shift_id') != row.snapshot['sandbox']['checkbox_shift_id']:
                            fiscal.fail('sandbox_shift_changed', 'New receipt requires the originally approved test shift')
                        run.receipt_dispatched = True
                        await session.commit()  # A crash now permits GET recovery only.
                        actor = await require(session, actor)
                        await sales_access.own_sale(session, actor, await local_sales.load(session, order_id, lock=True))
                        receipt = await provider.create_receipt(run.receipt_uuid, snapshot=row.snapshot, invoice_id=run.invoice_id)
                    # Serialize provider result with concurrent refresh / revocation.
                    await local_sales.load(session, order_id, lock=True)
                    run = await run_for(session, attempt_id, lock=True)
                    if receipt:
                        if (receipt['receipt_uuid'] != run.receipt_uuid or receipt.get('is_test') is not True
                                or receipt['amount_minor'] != row.amount_minor or receipt.get('currency') != 'UAH'
                                or receipt.get('shift_id') != row.snapshot['sandbox']['checkbox_shift_id']
                                or receipt.get('is_sent_dps') is not False
                                or receipt.get('cash_register_id') != row.snapshot['sandbox']['checkbox_cash_register_id']
                                or receipt.get('cashier_id') != row.snapshot['sandbox']['checkbox_cashier_id']
                                or receipt.get('organization_id') != row.snapshot['sandbox']['checkbox_organization_id']):
                            fiscal.fail('sandbox_receipt_mismatch', 'Provider test receipt differs from immutable sale')
                        run.receipt_status, run.receipt_evidence = receipt['status'], serial(receipt)
                    await session.flush([run])
                    await session.refresh(row)
                    truth = await session.get(QRProviderRecord, row.provider_invoice_id)
                    truth.receipt_status = run.receipt_status
                    await session.flush()
                    await qr_payment.reconcile(session, row, truth)
    except SandboxProviderError as error:
        run.last_error = error.code
        if run.payment_status != 'paid' and row.payment_status not in qr_payment.TERMINAL_UNPAID:
            row.payment_status = 'unknown'
    row.revision += 1
    result = await qr_payment.response(session, actor, row)
    # claim_intent may have committed before network; reloading keeps one response.
    intent = await claim_intent(session, intent_key, digest(payload.model_dump(mode='json')))
    intent.order_id, intent.response_json = order_id, result.model_dump(mode='json')
    await session.commit()
    return result


async def validate(session, row):
    run = await run_for(session, row.id)
    payment = run.payment_evidence if run else None
    receipt = run.receipt_evidence if run else None
    if (not run or run.payment_status != 'paid' or run.receipt_status != 'issued'
            or not run.invoice_id or not payment or not receipt or not run.accepted_at
            or payment.get('invoice_id') != run.invoice_id or payment.get('status') != 'paid'
            or payment.get('reference') != row.provider_invoice_id
            or payment.get('amount_minor') != row.amount_minor or payment.get('currency') != 'UAH'
            or payment.get('accepted_at') != run.accepted_at.astimezone(UTC).isoformat()
            or receipt.get('receipt_uuid') != run.receipt_uuid or receipt.get('status') != 'issued'
            or receipt.get('is_test') is not True or receipt.get('amount_minor') != row.amount_minor
            or receipt.get('currency') != 'UAH' or receipt.get('is_sent_dps') is not False
            or receipt.get('shift_id') != run.verification.get('shift_id')
            or receipt.get('cash_register_id') != row.snapshot['sandbox']['checkbox_cash_register_id']
            or receipt.get('cashier_id') != row.snapshot['sandbox']['checkbox_cashier_id']
            or receipt.get('organization_id') != row.snapshot['sandbox']['checkbox_organization_id']):
        fiscal.fail('sandbox_evidence_missing', 'Remote provider acceptance is required')
    return run


def receipt_summary(document, run):
    from html import escape
    from decimal import Decimal
    rows = ''.join('<tr><td>' + escape(str(item.get('name', ''))) + '</td><td>'
        + str(item['quantity']) + '</td><td>' + str(Decimal(item['total_minor']) / 100)
        + '</td></tr>' for item in document.snapshot['items'])
    return ('<!doctype html><html lang="uk"><meta charset="utf-8"><title>Checkbox TEST summary</title>'
        '<h1>Checkbox TEST · локальна копія підтвердження</h1><p>Тестова операція. Не є розрахунком реальними коштами.</p>'
        '<p>Checkbox receipt UUID: ' + escape(run.receipt_uuid) + '</p><p>Monobank sandbox invoice: '
        + escape(run.invoice_id) + '</p><table>' + rows + '</table><p>UAH '
        + str(Decimal(document.snapshot['amount_minor']) / 100) + '</p></html>')


def print_summary(document):
    # Shared mock report rendering must never impersonate the provider artifact.
    from html import escape
    return ('<!doctype html><html lang="uk"><meta charset="utf-8"><h1>Provider sandbox · локальний запис</h1>'
        '<p>Підтверджений тестовий чек: скористайтеся канонічним QR-посиланням.</p><p>'
        + escape(document.provider_uuid) + '</p></html>')
