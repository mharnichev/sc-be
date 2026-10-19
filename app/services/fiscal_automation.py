"""Explicit local ETTN simulator; provider API/NP token contract remains UNKNOWN.

Acceptance, lookup truth, and command journal share a PostgreSQL transaction.
There is no HTTP, automatic scheduler or notification delivery. Issued local
truth can produce one canonical mock receipt without a new dispatch.
A synchronous process call advances one durable command. Unknown create commands
are lookup-only: they never dispatch a replacement, even with the same UUID.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.fiscal import FiscalDocument, FiscalPreview, MockReceipt
from app.models.fiscal_automation import (AutomationCommand, AutomationEvent, AutomationPreview,
    FiscalAutomation, MockAutomationRecord)
from app.models.shipment import Shipment
from app.models.order import Order, OrderPayment
from app.schemas.fiscal_automation import AutomationListResponse, AutomationPreviewResponse, AutomationResponse
from app.services import fiscal, local_sales
from app.services.order_documents import digest

NOTICE = ('MOCK / СИМУЛЯЦІЯ. Підтвердження дозволяє майбутнє автоматичне створення '
    'локального тестового результату при mock-доставці. Не є фіскальним чеком. '
    'Не передається до ДПС. Live-контракт невідомий.')
CONDITION = 'mock_delivery_confirmed'


def available(session):
    return bool(settings.checkbox_ettn_mock_enabled and fiscal.available(session))


async def require(session, actor, *, recovery=False, mutation=True):
    actor = await fiscal.require(session, actor, mutation=mutation, reconcile=recovery)
    if mutation and not recovery and not available(session):
        fiscal.fail('mock_automation_disabled', 'Separate mock ETTN flag and isolated local PostgreSQL test database required')
    return actor


async def owner(session, order_id):
    if await session.scalar(select(FiscalAutomation.id).where(FiscalAutomation.order_id == order_id,
            FiscalAutomation.status != 'cancelled')):
        return 'checkbox_ettn'
    if await session.scalar(select(FiscalDocument.id).where(FiscalDocument.order_id == order_id,
            FiscalDocument.kind == 'sale', FiscalDocument.status != 'cancelled')):
        return 'sc_manual'
    return None


async def assert_manual_owner(session, order_id):
    from app.services.qr_payment import block_mutation
    await block_mutation(session, order_id)
    if await session.scalar(select(FiscalAutomation.id).where(FiscalAutomation.order_id == order_id,
            FiscalAutomation.status != 'cancelled')):
        fiscal.fail('automation_owns_payment', 'Confirm automation disarmed before choosing manual fiscalization')


async def block_shift_close(session, register_id, shift_revision):
    from app.services.qr_payment import block_shift_close as block_qr
    await block_qr(session, register_id, shift_revision)
    # Caller holds the register lock shared by trigger/materialize/report paths.
    projects = (await session.scalars(select(FiscalAutomation).where(
        FiscalAutomation.cash_register_id == register_id, FiscalAutomation.shift_revision == shift_revision,
        FiscalAutomation.status != 'cancelled'))).all()
    for project in projects:
        if project.status != 'issued':
            fiscal.fail('automation_report_unsupported', 'Reconcile or disarm pending mock automation before shift reporting')
        document = await session.scalar(select(FiscalDocument).where(FiscalDocument.automation_id == project.id))
        if document is None:
            fiscal.fail('automation_receipt_missing', 'Materialize the existing issued automation receipt before reporting')
        await fiscal.validate_issued_document(session, document)


def audit(session, actor, operation, payload, project=None):
    session.add(AutomationEvent(project_id=project.id if project else None, actor_id=actor.id,
        operation=operation, payload=payload, created_at=datetime.now(UTC)))


async def load_shipment(session, order_id, shipment_id, *, lock=False):
    stmt = select(Shipment).where(Shipment.id == shipment_id, Shipment.order_id == order_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    shipment = (await session.scalars(stmt)).one_or_none()
    if (not shipment or not shipment.simulation or shipment.carrier != 'mock_nova_poshta'
            or not shipment.active or not (shipment.tracking_number or '').startswith('MOCK-')
            or shipment.status in ('unknown', 'cancelled', 'returned', 'refused', 'returning', 'requires_review')):
        fiscal.fail('mock_shipment_required', 'Active known mock shipment with a MOCK TTN required')
    return shipment


async def snapshot(session, order, register, shipment_id, *, lock=False):
    state = fiscal.snapshot(order, register)
    shipment = await load_shipment(session, order.id, shipment_id, lock=lock)
    state.update(document_kind='mock_automation_project', automation_kind='mock_ettn', notice=NOTICE,
        shipment_id=shipment.id, tracking_number=shipment.tracking_number,
        shipment_operation_uuid=shipment.operation_uuid, condition=CONDITION)
    return state


async def preview_response(session, actor, preview):
    reasons = [] if available(session) and fiscal.permitted(actor) else ['mock_automation_disabled_or_forbidden']
    if preview.expires_at <= datetime.now(UTC):
        reasons.append('preview_expired')
    if await owner(session, preview.order_id):
        reasons.append('fiscal_owner_exists')
    register = await fiscal.cash_register(session)
    try:
        fiscal.register_ready(register, preview.cash_register_id)
        order = await local_sales.load(session, preview.order_id)
        if digest(await snapshot(session, order, register, preview.snapshot['shipment_id'])) != preview.snapshot_hash:
            reasons.append('stale_preview')
    except HTTPException as error:
        reasons.append(error.detail['code'])
    if await session.scalar(select(FiscalAutomation.id).where(FiscalAutomation.preview_id == preview.id)):
        reasons.append('preview_consumed')
    return AutomationPreviewResponse(id=preview.id, order_id=preview.order_id, revision=preview.revision,
        cash_register_id=preview.cash_register_id, shift_revision=preview.shift_revision,
        snapshot_hash=preview.snapshot_hash, snapshot=preview.snapshot, expires_at=preview.expires_at,
        notice=NOTICE, allowed_actions=[] if reasons else ['arm'], blocking_reasons=reasons)


async def create_preview(session, actor, order_id, payload, key):
    actor = await require(session, actor)
    intent = await fiscal.intent_for(session, actor, key, f'automation-preview:{order_id}', payload)
    if intent.response_json:
        return AutomationPreviewResponse.model_validate(intent.response_json)
    order = await local_sales.load(session, order_id, lock=True)
    register = await fiscal.cash_register(session, lock=True)
    fiscal.register_ready(register, payload.cash_register_id)
    if await owner(session, order_id):
        fiscal.fail('fiscal_owner_exists', 'Disarm the existing fiscal owner before selecting a new strategy')
    state = await snapshot(session, order, register, payload.shipment_id, lock=True)
    now = datetime.now(UTC)
    preview = AutomationPreview(order_id=order_id, actor_id=actor.id, revision=order.revision,
        cash_register_id=register.id, shift_revision=register.shift_revision, snapshot=state,
        snapshot_hash=digest(state), created_at=now, expires_at=now + timedelta(minutes=10))
    session.add(preview)
    await session.flush()
    result = await preview_response(session, actor, preview)
    intent.order_id, intent.response_json = order_id, result.model_dump(mode='json')
    await session.commit()
    return result


async def get_preview(session, actor, order_id, preview_id):
    actor = await require(session, actor, mutation=False)
    preview = await session.get(AutomationPreview, preview_id)
    if not preview or preview.order_id != order_id or preview.actor_id != actor.id:
        raise HTTPException(404, 'Automation preview not found')
    return await preview_response(session, actor, preview)


async def response(session, actor, project):
    enabled = available(session) and fiscal.permitted(actor)
    actions, reasons = [], []
    if not enabled:
        reasons.append('mock_automation_disabled_or_forbidden')
    if project.status == 'queued' and enabled:
        actions.append('process')
    if fiscal.permitted(actor) and project.status not in ('cancelled', 'issued'):
        # Cancellation and lookup remain possible after either feature flag is off.
        actions.extend(['cancel', 'reconcile'])
    if project.status == 'armed' and enabled:
        actions.append('trigger')
    document_id = None
    if project.status == 'issued':
        document = await session.scalar(select(FiscalDocument).where(FiscalDocument.automation_id == project.id))
        try:
            await validate_issued_truth(session, project)
            if document is not None:
                await fiscal.validate_issued_document(session, document)
                document_id = document.id
                actions.append('view_document')
            else:
                reasons.append('automation_receipt_missing')
                if fiscal.permitted(actor):
                    actions.append('materialize')
        except HTTPException:
            reasons.append('automation_receipt_mismatch')
    return AutomationResponse(fiscal_document_id=document_id, id=project.id, order_id=project.order_id, payment_id=project.payment_id,
        shipment_id=project.shipment_id, tracking_number=project.snapshot['tracking_number'], status=project.status,
        revision=project.revision, provider_uuid=project.provider_uuid, external_id=project.external_id,
        cash_register_id=project.cash_register_id, shift_revision=project.shift_revision, condition=CONDITION,
        snapshot_hash=project.snapshot_hash, snapshot=project.snapshot, approved_at=project.approved_at,
        dispatched_at=project.dispatched_at, cancellation_confirmed_at=project.cancellation_confirmed_at,
        result=project.result, notice=NOTICE, allowed_actions=actions, blocking_reasons=reasons)


async def create(session, actor, order_id, payload, key):
    actor = await require(session, actor, recovery=True)
    if payload.confirm is not True:
        fiscal.fail('confirmation_required', 'Explicit authorization of future mock automation required')
    intent = await fiscal.intent_for(session, actor, key, f'automation-create:{order_id}', payload)
    if intent.response_json:
        return AutomationResponse.model_validate(intent.response_json)
    actor = await require(session, actor)
    order = await local_sales.load(session, order_id, lock=True)
    register = await fiscal.cash_register(session, lock=True)
    fiscal.register_ready(register, payload.cash_register_id)
    preview = await session.get(AutomationPreview, payload.preview_id)
    if not preview or preview.order_id != order_id or preview.actor_id != actor.id:
        fiscal.fail('preview_not_found', 'Approval must use your automation preview for this order')
    if preview.expires_at <= datetime.now(UTC):
        fiscal.fail('preview_expired', 'Create a fresh automation preview')
    if payload.expected_revision != order.revision or preview.revision != order.revision:
        fiscal.fail('revision_conflict', 'Reload order and preview')
    if (payload.snapshot_hash != preview.snapshot_hash or payload.cash_register_id != preview.cash_register_id
            or register.shift_revision != preview.shift_revision
            or digest(await snapshot(session, order, register, preview.snapshot['shipment_id'], lock=True)) != preview.snapshot_hash):
        fiscal.fail('stale_preview', 'TTN, payment, order, amount, register or shift changed')
    if await owner(session, order_id):
        fiscal.fail('fiscal_owner_exists', 'Exactly one fiscal strategy may own this payment')
    if await session.scalar(select(FiscalAutomation.id).where(FiscalAutomation.preview_id == preview.id)):
        fiscal.fail('preview_consumed', 'Create a new preview after confirmed cancellation')
    now = datetime.now(UTC)
    project = FiscalAutomation(order_id=order_id, payment_id=order.payments[0].id,
        shipment_id=preview.snapshot['shipment_id'], preview_id=preview.id, actor_id=actor.id,
        provider_uuid=str(uuid4()), cash_register_id=register.id, shift_revision=register.shift_revision,
        snapshot=preview.snapshot, snapshot_hash=preview.snapshot_hash, status='queued', revision=1,
        simulation=True, mock_outcome=payload.mock_outcome, approved_at=now)
    session.add(project)
    await session.flush()
    session.add(AutomationCommand(id=project.provider_uuid, project_id=project.id, operation='create',
        status='pending', attempts=0, created_at=now))
    audit(session, actor, 'approved', {'preview_id': preview.id, 'snapshot_hash': preview.snapshot_hash, 'confirm': True}, project)
    result = await response(session, actor, project)
    intent.order_id, intent.response_json = order_id, result.model_dump(mode='json')
    await session.commit()
    return result


async def get(session, actor, project_id):
    actor = await require(session, actor, mutation=False)
    project = await session.get(FiscalAutomation, project_id)
    if not project:
        raise HTTPException(404, 'Automation project not found')
    return await response(session, actor, project)


async def list_projects(session, actor, order_id):
    actor = await require(session, actor, mutation=False)
    order = await local_sales.load(session, order_id)
    projects = (await session.scalars(select(FiscalAutomation).where(FiscalAutomation.order_id == order_id)
        .order_by(FiscalAutomation.id))).all()
    current_owner = await owner(session, order_id)
    reasons = [] if available(session) else ['mock_automation_disabled']
    if current_owner:
        reasons.append('fiscal_owner_exists')
    try:
        register = await fiscal.cash_register(session)
        fiscal.register_ready(register)
        fiscal.snapshot(order, register)
        shipment = next((row for row in order.shipments if row.active and row.simulation
            and row.carrier == 'mock_nova_poshta'), None)
        if not shipment:
            fiscal.fail('mock_shipment_required', 'Active mock shipment required')
        await load_shipment(session, order_id, shipment.id)
    except HTTPException as error:
        reasons.append(error.detail['code'])
    return AutomationListResponse(available=available(session), owner=current_owner, notice=NOTICE,
        items=[await response(session, actor, row) for row in projects], blocking_reasons=reasons,
        allowed_actions=[] if reasons else ['preview'])


async def lock_project(session, project_id):
    order_id = await session.scalar(select(FiscalAutomation.order_id).where(FiscalAutomation.id == project_id))
    if order_id is None:
        raise HTTPException(404, 'Automation project not found')
    order = await local_sales.load(session, order_id, lock=True)
    register = await fiscal.cash_register(session, lock=True)
    project = (await session.scalars(select(FiscalAutomation).where(FiscalAutomation.id == project_id)
        .with_for_update().execution_options(populate_existing=True))).one()
    commands = (await session.scalars(select(AutomationCommand).where(AutomationCommand.project_id == project_id)
        .with_for_update().execution_options(populate_existing=True))).all()
    return order, register, project, {row.operation: row for row in commands}


async def lookup(session, actor, project, commands):
    record = await session.get(MockAutomationRecord, project.provider_uuid, populate_existing=True)
    if record is not None:
        if record.project_id != project.id or record.snapshot_hash != project.snapshot_hash:
            fiscal.fail('mock_lookup_mismatch', 'Mock lookup identity does not match immutable approved snapshot')
        project.external_id = record.external_id
        if project.status != 'issued':
            if record.status == 'cancelled':
                project.status = 'cancelled'
                project.cancellation_confirmed_at = datetime.now(UTC)
            elif record.status == 'issued':
                project.status, project.result = 'issued', record.result
            elif project.status not in ('cancel_pending', 'cancel_unknown'):
                project.status = 'armed'
        commands['create'].status = 'done'
        if record.status == 'cancelled' and 'cancel' in commands:
            commands['cancel'].status = 'done'
    elif await session.scalar(select(AutomationEvent.id).where(AutomationEvent.project_id == project.id,
            AutomationEvent.operation == 'mock_cancel_accepted')):
        # A cancellation acceptance journal is also proof when creation timed
        # out before acceptance; it fences the never-accepted create command.
        project.status = 'cancelled'
        project.cancellation_confirmed_at = datetime.now(UTC)
        commands['create'].status = 'cancelled'
        commands['cancel'].status = 'done'
    # Absence alone is inconclusive after a timed-out dispatch. No new UUID or
    # dispatch is authorized by a failed lookup in this deliberately limited mock.
    audit(session, actor, 'reconciled', {'found': record is not None, 'status': project.status}, project)
    return record


async def command(session, actor, project_id, payload, key, operation):
    recovery = operation in ('process', 'cancel', 'reconcile', 'materialize')
    actor = await require(session, actor, recovery=recovery)
    intent = await fiscal.intent_for(session, actor, key, f'automation-{operation}:{project_id}', payload)
    if intent.response_json:
        return AutomationResponse.model_validate(intent.response_json)
    order, register, project, commands = await lock_project(session, project_id)
    if payload.expected_revision != project.revision:
        fiscal.fail('revision_conflict', 'Reload automation project')
    if operation == 'process':
        if project.status == 'queued':
            origin = (await session.scalars(select(AdminUser).where(AdminUser.id == project.actor_id)
                .with_for_update(read=True).execution_options(populate_existing=True))).one_or_none()
            reason = None
            if not available(session):
                reason = 'mock_automation_disabled'
            elif not fiscal.permitted(origin):
                reason = 'origin_permission_revoked'
            else:
                try:
                    fiscal.register_ready(register, project.cash_register_id)
                    current = await snapshot(session, order, register, project.shipment_id, lock=True)
                    # The queued worker may run after carrier handover/refresh.
                    # Match trigger's policy: only the order revision may advance;
                    # every financial and shipment identity field must still match.
                    current['order_revision'] = project.snapshot['order_revision']
                    if (not project.simulation or register.shift_revision != project.shift_revision
                            or digest(current) != project.snapshot_hash):
                        reason = 'stale_preview'
                except HTTPException as error:
                    reason = error.detail['code']
            if reason:
                project.status, commands['create'].status = 'error', 'blocked'
                audit(session, actor, 'approval_invalidated', {'code': reason}, project)
            else:
                commands['create'].attempts += 1
                project.dispatched_at = datetime.now(UTC)
                if project.mock_outcome != 'timeout_before_acceptance':
                    record = MockAutomationRecord(provider_uuid=project.provider_uuid, project_id=project.id,
                        external_id='MOCK-ETTN-' + project.provider_uuid, snapshot_hash=project.snapshot_hash,
                        status='armed', accepted_at=datetime.now(UTC))
                    session.add(record)
                    project.external_id = record.external_id
                if project.mock_outcome == 'accepted':
                    project.status, commands['create'].status = 'armed', 'done'
                else:
                    project.status, commands['create'].status = 'unknown', 'unknown'
                audit(session, actor, 'create_result', {'status': project.status, 'provider_uuid': project.provider_uuid}, project)
        elif project.status in ('unknown', 'cancel_unknown', 'cancel_pending'):
            await lookup(session, actor, project, commands)
    elif operation == 'reconcile':
        if project.status not in ('issued', 'cancelled'):
            await lookup(session, actor, project, commands)
    elif operation == 'materialize':
        await materialize_document(session, actor, project)
    elif operation == 'cancel':
        if project.status == 'issued':
            fiscal.fail('automation_already_issued', 'Issued simulation permanently owns the payment; cancellation cannot undo it')
        if project.status != 'cancelled':
            cancel = commands.get('cancel')
            if cancel is None:
                cancel = AutomationCommand(id=str(uuid4()), project_id=project.id, operation='cancel',
                    status='pending', attempts=0, created_at=datetime.now(UTC))
                session.add(cancel)
                commands['cancel'] = cancel
            cancel.attempts += 1
            project.status = 'cancel_pending'
            record = await session.get(MockAutomationRecord, project.provider_uuid, populate_existing=True)
            if record and record.status == 'issued':
                project.status, project.result, project.external_id = 'issued', record.result, record.external_id
                cancel.status = 'done'
            elif payload.mock_outcome == 'timeout_before_acceptance':
                project.status, cancel.status = 'cancel_unknown', 'unknown'
            else:
                if record:
                    record.status = 'cancelled'
                    await session.flush()
                audit(session, actor, 'mock_cancel_accepted', {'command_uuid': cancel.id,
                    'provider_uuid': project.provider_uuid, 'mock_outcome': payload.mock_outcome}, project)
                if payload.mock_outcome == 'timeout_after_acceptance':
                    project.status, cancel.status = 'cancel_unknown', 'unknown'
                else:
                    project.status, cancel.status = 'cancelled', 'done'
                    project.cancellation_confirmed_at = datetime.now(UTC)
                    commands['create'].status = 'cancelled'
            audit(session, actor, 'cancel_result', {'status': project.status, 'command_uuid': cancel.id, 'mock_outcome': payload.mock_outcome}, project)
    elif operation == 'trigger':
        if project.status != 'armed':
            fiscal.fail('automation_not_armed', 'Only a known armed mock project can simulate its approved condition')
        # This explicit fixture is a NEW mock effect, so both flags and current
        # origin permissions, immutable shipment/payment/shift must still pass.
        origin = (await session.scalars(select(AdminUser).where(AdminUser.id == project.actor_id)
                .with_for_update(read=True).execution_options(populate_existing=True))).one_or_none()
        if not fiscal.permitted(origin):
            fiscal.fail('origin_permission_revoked', 'Originating fiscal permission was revoked')
        fiscal.register_ready(register, project.cash_register_id)
        current = await snapshot(session, order, register, project.shipment_id, lock=True)
        # Carrier handover/refresh legitimately advances order.revision. Every
        # financial and shipment identity field still must equal consent.
        current['order_revision'] = project.snapshot['order_revision']
        if register.shift_revision != project.shift_revision or digest(current) != project.snapshot_hash:
            fiscal.fail('stale_preview', 'The immutable mock automation scope changed; disarm it')
        shipment = await load_shipment(session, order.id, project.shipment_id, lock=True)
        if shipment.status != 'delivered':
            fiscal.fail('mock_condition_not_met', 'Explicit mock shipment delivered fixture required first')
        record = await session.get(MockAutomationRecord, project.provider_uuid, populate_existing=True)
        if not record or record.status != 'armed' or record.snapshot_hash != project.snapshot_hash:
            fiscal.fail('mock_lookup_mismatch', 'Reconcile persisted mock project truth before triggering')
        result = {'document_kind': 'mock_automation_result', 'is_fiscal_document': False,
            'simulation': True, 'is_sent_dps': False, 'provider_contract': 'unknown',
            'external_id': record.external_id, 'provider_uuid': project.provider_uuid,
            'snapshot_hash': project.snapshot_hash, 'amount_minor': project.snapshot['amount_minor'],
            'payment_id': project.payment_id, 'shipment_id': project.shipment_id,
            'tracking_number': project.snapshot['tracking_number'], 'condition': CONDITION,
            'issued_at': datetime.now(UTC).isoformat(), 'notice': NOTICE}
        project.status, project.result, record.status, record.result = 'issued', result, 'issued', result
        session.add(AutomationCommand(id=str(uuid4()), project_id=project.id, operation='trigger',
            status='done', attempts=1, created_at=datetime.now(UTC)))
        audit(session, actor, 'mock_condition_result', result, project)
        # Persist issued provider truth before the canonical document insert.
        await session.flush()
        await materialize_document(session, actor, project)
    else:
        raise ValueError('Unsupported local automation operation')
    project.revision += 1
    await session.flush()
    result = await response(session, actor, project)
    intent.order_id, intent.response_json = order.id, result.model_dump(mode='json')
    await session.commit()
    return result


def receipt_snapshot(project):
    """Receipt money/items derive exclusively from the immutable approval."""
    state = deepcopy(project.snapshot)
    state.update(document_kind='mock_fiscal_receipt', notice=fiscal.NOTICE, source='mock_ettn',
        automation={'id': project.id, 'provider_uuid': project.provider_uuid,
            'approval_snapshot_hash': project.snapshot_hash, 'external_id': project.external_id,
            'shipment_id': project.shipment_id, 'tracking_number': project.snapshot['tracking_number'],
            'condition': project.snapshot['condition']})
    return state


async def validate_issued_truth(session, project):
    record = await session.get(MockAutomationRecord, project.provider_uuid, populate_existing=True)
    result = project.result or {}
    payment = await session.get(OrderPayment, project.payment_id)
    order = await session.get(Order, project.order_id)
    from app.services.mock_cod import validated_cod_snapshot
    cod = validated_cod_snapshot(order, payment) if order and payment else None
    if (not order or (not cod and (not order.simulation or order.channel != 'backoffice_pos'))
            or not payment or payment.order_id != project.order_id or not payment.simulation
            or project.snapshot.get('payment') != {'id': payment.id, 'amount_minor': payment.amount_minor,
                'method': payment.method, 'source': payment.source, 'simulation': True}
            or (not cod and (payment.source != 'operator_confirmation' or payment.method not in ('cash', 'card')))
            or (cod is not None and project.snapshot.get('cod') != cod)
            or payment.amount_minor != project.snapshot['amount_minor']):
        fiscal.fail('automation_receipt_mismatch', 'Automation receipt requires its original immutable simulated payment')
    expected = {'document_kind': 'mock_automation_result', 'is_fiscal_document': False,
        'simulation': True, 'is_sent_dps': False, 'provider_contract': 'unknown',
        'external_id': project.external_id, 'provider_uuid': project.provider_uuid,
        'snapshot_hash': project.snapshot_hash, 'amount_minor': project.snapshot['amount_minor'],
        'payment_id': project.payment_id, 'shipment_id': project.shipment_id,
        'tracking_number': project.snapshot['tracking_number'], 'condition': CONDITION}
    if (project.status != 'issued' or not project.simulation or digest(project.snapshot) != project.snapshot_hash
            or not record or record.status != 'issued' or record.project_id != project.id
            or record.snapshot_hash != project.snapshot_hash or record.external_id != project.external_id
            or record.result != project.result or any(result.get(key) != value
                or type(result.get(key)) is not type(value) for key, value in expected.items())):
        fiscal.fail('automation_receipt_mismatch', 'Issued automation requires matching immutable mock acceptance and scope')
    try:
        issued_at = datetime.fromisoformat(result['issued_at'])
        if issued_at.tzinfo is None:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        fiscal.fail('automation_receipt_mismatch', 'Issued mock automation timestamp is invalid')
    return record, issued_at


async def validate_linked_document(session, document):
    project = await session.get(FiscalAutomation, document.automation_id, populate_existing=True)
    if project is None:
        fiscal.fail('automation_receipt_mismatch', 'Canonical receipt automation does not exist')
    record, issued_at = await validate_issued_truth(session, project)
    expected = receipt_snapshot(project)
    if (document.kind != 'sale' or document.status != 'issued' or not document.simulation
            or document.order_id != project.order_id or document.payment_id != project.payment_id
            or document.provider_uuid != project.provider_uuid or document.actor_id != project.actor_id
            or document.cash_register_id != project.cash_register_id or document.shift_revision != project.shift_revision
            or document.approved_at != project.approved_at or document.dispatched_at != issued_at
            or document.snapshot != expected or document.snapshot_hash != digest(expected)):
        fiscal.fail('automation_receipt_mismatch', 'Canonical receipt does not match its original approved automation')
    receipt = await session.get(MockReceipt, document.provider_uuid)
    if not receipt or receipt.accepted_at != issued_at:
        fiscal.fail('automation_receipt_mismatch', 'Canonical acceptance timestamp differs from issued mock truth')
    preview = await session.get(FiscalPreview, document.preview_id)
    if (not preview or preview.order_id != document.order_id or preview.actor_id != project.actor_id
            or preview.cash_register_id != project.cash_register_id or preview.shift_revision != project.shift_revision
            or preview.snapshot != expected or preview.snapshot_hash != document.snapshot_hash):
        fiscal.fail('automation_receipt_mismatch', 'Canonical receipt provenance preview does not match approval')
    return project


async def materialize_document(session, actor, project):
    """Recover one existing issued truth; no provider dispatch or sale mutation.

    The caller holds the order/register/project locks. The derived consumed
    preview preserves the existing receipt FK while retaining original consent
    in automation_id. It is never a new fiscal approval.
    """
    record, issued_at = await validate_issued_truth(session, project)
    existing = await session.scalar(select(FiscalDocument).where(FiscalDocument.automation_id == project.id))
    if existing is not None:
        await fiscal.validate_issued_document(session, existing)
        return existing
    if await session.scalar(select(FiscalDocument.id).where(
            FiscalDocument.order_id == project.order_id, FiscalDocument.kind == 'sale',
            FiscalDocument.status != 'cancelled')):
        fiscal.fail('automation_receipt_mismatch', 'Existing sale receipt prevents replacement or duplicate materialization')
    state = receipt_snapshot(project)
    state_hash = digest(state)
    preview = FiscalPreview(order_id=project.order_id, actor_id=project.actor_id,
        revision=project.snapshot['order_revision'], cash_register_id=project.cash_register_id,
        shift_revision=project.shift_revision, snapshot=state, snapshot_hash=state_hash,
        created_at=project.approved_at, expires_at=project.approved_at)
    session.add(preview)
    await session.flush()
    document = FiscalDocument(automation_id=project.id, kind='sale', order_id=project.order_id,
        payment_id=project.payment_id, preview_id=preview.id, actor_id=project.actor_id,
        provider_uuid=project.provider_uuid, cash_register_id=project.cash_register_id,
        shift_revision=project.shift_revision, snapshot=state, snapshot_hash=state_hash,
        status='issued', revision=1, simulation=True, mock_outcome='accepted',
        approved_at=project.approved_at, dispatched_at=issued_at,
        delivery_status='pending_document', delivery_attempts=0)
    session.add(document)
    await session.flush()
    session.add(MockReceipt(provider_uuid=project.provider_uuid, document_id=document.id,
        snapshot_hash=state_hash, accepted_at=issued_at))
    fiscal.deliver(session, actor, document, 'delivered')
    audit(session, actor, 'receipt_materialized', {'document_id': document.id,
        'provider_uuid': project.provider_uuid, 'approval_snapshot_hash': project.snapshot_hash}, project)
    await session.flush()
    await fiscal.validate_issued_document(session, document)
    return document
