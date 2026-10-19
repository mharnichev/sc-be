"""Opt-in genuine PostgreSQL, isolated UUID schemas, local fixtures only."""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.fiscal import FiscalDocument, MockReceipt
from app.models.fiscal_automation import (AutomationCommand, AutomationEvent, AutomationPreview,
    FiscalAutomation, MockAutomationRecord)
from app.models.inventory import InventoryMovement
from app.models.order import Order, OrderHandover, OrderPayment
from app.models.product import Product
from app.models.shipment import Shipment
from app.schemas.fiscal import FiscalCommand, FiscalPreviewCreate, MockShiftCommand
from app.schemas.fiscal_automation import AutomationCancel, AutomationCreate, AutomationPreviewCreate
from app.schemas.fiscal_returns import MockReportCreate
from app.schemas.shipment import ShipmentCreate
from app.services import fiscal, fiscal_automation as automation, fiscal_returns, shipments
from tests.test_fiscal_integration import sale, approval
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies
from tests.test_shipments_integration import ADDRESS, PACK

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    for flag in ('checkbox_mock_enabled', 'checkbox_ettn_mock_enabled', 'local_sales_simulation_enabled', 'np_mock_shipments_enabled'):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, 'app_env', 'test')


async def user(session):
    return await session.get(AdminUser, 1)


async def setup(database):
    order = await sale(database)
    async with database() as session:
        row = await session.get(Order, order.id)
        row.fulfillment_method, row.shipping_payload_json = 'nova_poshta', ADDRESS.copy()
        await session.commit()
        created = await shipments.create(session, await user(session), row.id,
            ShipmentCreate(expected_revision=row.revision, packaging={**PACK, 'cod_amount_minor': 0}), str(uuid4()))
        return created, created.shipments[0].id, row.items[0].product_id


async def preview(database, order, shipment_id):
    async with database() as session:
        return await automation.create_preview(session, await user(session), order.id,
            AutomationPreviewCreate(cash_register_id=fiscal.REGISTER, shipment_id=shipment_id,
                condition='mock_delivery_confirmed'), str(uuid4()))


def consent(p, outcome='accepted'):
    return AutomationCreate(cash_register_id=p.cash_register_id, preview_id=p.id,
        expected_revision=p.revision, snapshot_hash=p.snapshot_hash, confirm=True, mock_outcome=outcome)


async def arm(database, p, outcome='accepted', key=None):
    async with database() as session:
        return await automation.create(session, await user(session), p.order_id, consent(p, outcome), key or str(uuid4()))


async def run(database, project, op, key=None, outcome='confirmed'):
    body = AutomationCancel(expected_revision=project.revision, mock_outcome=outcome) if op == 'cancel' else FiscalCommand(expected_revision=project.revision)
    async with database() as session:
        return await automation.command(session, await user(session), project.id, body, key or str(uuid4()), op)


async def financial_state(database, product_id):
    async with database() as session:
        product = await session.get(Product, product_id)
        return (product.stock_quantity, product.reserved_quantity,
            await session.scalar(select(func.count()).select_from(OrderPayment)),
            await session.scalar(select(func.count()).select_from(OrderHandover)),
            await session.scalar(select(func.count()).select_from(InventoryMovement)),
            await session.scalar(select(func.count()).select_from(FiscalDocument)),
            await session.scalar(select(func.count()).select_from(MockReceipt)))


async def test_consent_creates_only_durable_command_no_project_before_consent(database, checkout_dependencies):
    order, sid, pid = await setup(database)
    before = await financial_state(database, pid)
    p = await preview(database, order, sid)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(FiscalAutomation)) == 0
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 0
    project = await arm(database, p)
    assert project.status == 'queued' and project.external_id is None
    assert project.snapshot['payment']['id'] == project.payment_id
    assert project.snapshot['tracking_number'] == project.tracking_number
    assert project.snapshot['condition'] == project.condition
    assert project.provider_contract == 'unknown' and project.is_sent_dps is False
    async with database() as session:
        command = (await session.scalars(select(AutomationCommand))).one()
        assert (command.id, command.status, command.operation) == (project.provider_uuid, 'pending', 'create')
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 0
    assert await financial_state(database, pid) == before


@pytest.mark.parametrize('change', ['hash', 'ttn', 'inactive', 'amount', 'payment', 'shift', 'expired', 'foreign_actor'])
async def test_stale_or_foreign_consent_rejected(database, checkout_dependencies, change):
    order, sid, _ = await setup(database)
    p = await preview(database, order, sid)
    body = consent(p)
    async with database() as session:
        if change == 'hash':
            body = body.model_copy(update={'snapshot_hash': '0' * 64})
        elif change in ('ttn', 'inactive'):
            shipment = await session.get(Shipment, sid)
            if change == 'ttn':
                shipment.tracking_number = 'MOCK-' + '0' * 32
            else:
                shipment.active = False
        elif change == 'amount':
            (await session.get(Order, order.id)).total_amount = 99
        elif change == 'payment':
            (await session.scalars(select(OrderPayment))).one().amount_minor -= 1
        elif change == 'shift':
            (await fiscal.cash_register(session)).shift_revision += 1
        elif change == 'expired':
            (await session.get(AutomationPreview, p.id)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == 'foreign_actor':
            session.add(AdminUser(id=2, email='other@example.test', hashed_password='unused', is_active=True, is_superuser=True))
            (await session.get(AutomationPreview, p.id)).actor_id = 2
        await session.commit()
    with pytest.raises(HTTPException):
        async with database() as session:
            await automation.create(session, await user(session), order.id, body, str(uuid4()))
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(FiscalAutomation)) == 0
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 0


async def test_duplicate_concurrent_consents_and_commands_are_frozen(database, checkout_dependencies):
    order, sid, _ = await setup(database)
    p = await preview(database, order, sid)
    key = str(uuid4())
    a, b = await asyncio.gather(arm(database, p, key=key), arm(database, p, key=key))
    assert a.model_dump(mode='json') == b.model_dump(mode='json')
    process_key = str(uuid4())
    x, y = await asyncio.gather(run(database, a, 'process', process_key), run(database, a, 'process', process_key))
    assert x.model_dump(mode='json') == y.model_dump(mode='json')
    assert x.status == 'armed' and x.external_id.startswith('MOCK-ETTN-')
    assert (await arm(database, p, key=key)).status == 'queued'
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(FiscalAutomation)) == 1
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 1
        assert (await session.scalars(select(AutomationCommand))).one().attempts == 1


@pytest.mark.parametrize('outcome,expected', [('timeout_before_acceptance', 'unknown'), ('timeout_after_acceptance', 'armed')])
async def test_unknown_lookup_never_redispatches(database, checkout_dependencies, outcome, expected):
    order, sid, _ = await setup(database)
    project = await arm(database, await preview(database, order, sid), outcome)
    unknown = await run(database, project, 'process')
    assert unknown.status == 'unknown'
    reconciled = await run(database, unknown, 'reconcile')
    assert reconciled.status == expected and reconciled.provider_uuid == project.provider_uuid
    processed_again = await run(database, reconciled, 'process')
    assert processed_again.status == expected
    async with database() as session:
        assert (await session.scalars(select(AutomationCommand))).one().attempts == 1
        assert await automation.owner(session, order.id) == 'checkbox_ettn'


@pytest.mark.parametrize('outcome,expected', [('timeout_before_acceptance', 'cancel_unknown'), ('timeout_after_acceptance', 'cancelled')])
async def test_cancel_unknown_reconciliation_and_manual_block(database, checkout_dependencies, outcome, expected):
    order, sid, _ = await setup(database)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    cancel_key = str(uuid4())
    cancelled = await run(database, project, 'cancel', cancel_key, outcome=outcome)
    assert cancelled.status == 'cancel_unknown'
    assert (await run(database, project, 'cancel', cancel_key, outcome=outcome)).model_dump(mode='json') == cancelled.model_dump(mode='json')
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await automation.assert_manual_owner(session, order.id)
        assert error.value.detail['code'] == 'automation_owns_payment'
    reconciled = await run(database, cancelled, 'reconcile')
    assert reconciled.status == expected
    async with database() as session:
        assert await automation.owner(session, order.id) == (None if expected == 'cancelled' else 'checkbox_ettn')
        events = (await session.scalars(select(AutomationEvent).where(AutomationEvent.operation == 'cancel_result'))).all()
        assert events[0].payload['mock_outcome'] == outcome


async def test_cancel_after_unknown_before_create_acceptance_has_durable_proof(database, checkout_dependencies):
    order, sid, _ = await setup(database)
    project = await run(database, await arm(database, await preview(database, order, sid), 'timeout_before_acceptance'), 'process')
    project = await run(database, project, 'cancel', outcome='timeout_after_acceptance')
    assert project.status == 'cancel_unknown'
    project = await run(database, project, 'reconcile')
    assert project.status == 'cancelled' and project.cancellation_confirmed_at
    async with database() as session:
        assert await automation.owner(session, order.id) is None
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 0


async def test_manual_and_automation_race_exactly_one_owner(database, checkout_dependencies):
    order, sid, _ = await setup(database)
    auto_preview = await preview(database, order, sid)
    async with database() as session:
        manual = await fiscal.create_preview(session, await user(session), order.id,
            FiscalPreviewCreate(cash_register_id=fiscal.REGISTER), str(uuid4()))
    async def manual_submit():
        async with database() as session:
            return await fiscal.submit(session, await user(session), order.id, approval(manual), str(uuid4()))
    async def attempt(call):
        try:
            return await call
        except HTTPException as error:
            assert error.detail['code'] in ('automation_owns_payment', 'fiscal_owner_exists')
            return None
    a, b = await asyncio.wait_for(asyncio.gather(attempt(arm(database, auto_preview)), attempt(manual_submit())), timeout=15)
    assert (a is None) != (b is None)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(FiscalAutomation)) + await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1


async def test_queued_manual_owns_scope_and_worker_cannot_bypass_corrupt_ownership(database, checkout_dependencies):
    order, sid, _ = await setup(database)
    p = await preview(database, order, sid)
    async with database() as session:
        mp = await fiscal.create_preview(session, await user(session), order.id, FiscalPreviewCreate(cash_register_id=fiscal.REGISTER), str(uuid4()))
        manual = await fiscal.submit(session, await user(session), order.id, approval(mp), str(uuid4()))
    with pytest.raises(HTTPException) as error:
        await arm(database, p)
    assert error.value.detail['code'] == 'fiscal_owner_exists'
    # Metadata test schemas intentionally lack migration triggers; corrupt fixture
    # exercises worker defense independently of the deployed cross-table trigger.
    async with database() as session:
        session.add(FiscalAutomation(order_id=order.id, payment_id=manual.snapshot['payment']['id'], shipment_id=sid,
            preview_id=p.id, actor_id=1, provider_uuid=str(uuid4()), cash_register_id=fiscal.REGISTER,
            shift_revision=p.shift_revision, snapshot=p.snapshot, snapshot_hash=p.snapshot_hash, status='armed',
            revision=1, simulation=True, mock_outcome='accepted', approved_at=datetime.now(UTC)))
        await session.commit()
        result = await fiscal.process_job(session, await user(session), manual.id)
        assert result.status == 'error'
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 0


@pytest.mark.parametrize('change', ['flag', 'shift', 'ttn', 'origin'])
async def test_first_process_rechecks_consent_and_origin(database, checkout_dependencies, monkeypatch, change):
    order, sid, _ = await setup(database)
    project = await arm(database, await preview(database, order, sid))
    async with database() as session:
        if change == 'flag':
            monkeypatch.setattr(settings, 'checkbox_ettn_mock_enabled', False)
        elif change == 'shift':
            (await fiscal.cash_register(session)).shift_revision += 1
        elif change == 'ttn':
            (await session.get(Shipment, sid)).tracking_number = 'MOCK-' + 'F' * 32
        else:
            origin = await user(session)
            origin.is_superuser, origin.can_fiscalize_sales = False, False
            session.add(AdminUser(id=2, email='worker@example.test', hashed_password='unused', is_active=True, is_superuser=True))
        await session.commit()
    async with database() as session:
        worker = await session.get(AdminUser, 2) if change == 'origin' else await user(session)
        result = await automation.command(session, worker, project.id, FiscalCommand(expected_revision=project.revision), str(uuid4()), 'process')
        assert result.status == 'error'
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 0
        assert await automation.owner(session, order.id) == 'checkbox_ettn'


async def test_issued_terminal_owner_no_financial_or_stock_effect(database, checkout_dependencies):
    order, sid, pid = await setup(database)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    with pytest.raises(HTTPException) as error:
        await run(database, project, 'trigger')
    assert error.value.detail['code'] == 'mock_condition_not_met'
    async with database() as session:
        shipment = await session.get(Shipment, sid)
        shipment.status, shipment.progress = 'delivered', 3  # explicit local condition fixture only
        (await session.get(Order, order.id)).revision += 2
        await session.commit()
    before = await financial_state(database, pid)
    key = str(uuid4())
    issued = await run(database, project, 'trigger', key)
    assert issued.status == 'issued' and issued.result['is_fiscal_document'] is False
    assert (await run(database, project, 'trigger', key)).model_dump(mode='json') == issued.model_dump(mode='json')
    after = await financial_state(database, pid)
    assert after[:5] == before[:5] and after[5:] == (before[5] + 1, before[6] + 1)
    assert issued.fiscal_document_id is not None
    with pytest.raises(HTTPException) as error:
        await run(database, issued, 'cancel')
    assert error.value.detail['code'] == 'automation_already_issued'
    async with database() as session:
        assert await automation.owner(session, order.id) == 'checkbox_ettn'
        p = await fiscal.create_preview(session, await user(session), order.id, FiscalPreviewCreate(cash_register_id=fiscal.REGISTER), str(uuid4()))
        assert 'automation_owns_payment' in p.blocking_reasons and 'submit' not in p.allowed_actions
        with pytest.raises(HTTPException) as error:
            await fiscal.submit(session, await user(session), order.id, approval(p), str(uuid4()))
        assert error.value.detail['code'] == 'automation_owns_payment'


async def test_kill_switch_lookup_cancel_allowed_trigger_new_dispatch_blocked(database, checkout_dependencies, monkeypatch):
    order, sid, _ = await setup(database)
    project = await run(database, await arm(database, await preview(database, order, sid), 'timeout_after_acceptance'), 'process')
    monkeypatch.setattr(settings, 'checkbox_ettn_mock_enabled', False)
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', False)
    project = await run(database, project, 'reconcile')
    assert project.status == 'armed' and project.external_id
    with pytest.raises(HTTPException):
        await run(database, project, 'trigger')
    project = await run(database, project, 'cancel')
    assert project.status == 'cancelled'
    async with database() as session:
        result = await automation.list_projects(session, await user(session), order.id)
        assert result.owner is None and not result.available and not result.allowed_actions


@pytest.mark.parametrize('permission', ['none', 'returns_only', 'sales_only'])
async def test_explicit_sales_permission_separate_from_returns(database, checkout_dependencies, permission):
    order, sid, _ = await setup(database)
    async with database() as session:
        actor = await user(session)
        actor.is_superuser = False
        actor.can_fiscalize_returns = permission == 'returns_only'
        actor.can_fiscalize_sales = permission == 'sales_only'
        await session.commit()
    if permission == 'sales_only':
        assert (await preview(database, order, sid)).allowed_actions == ['arm']
    else:
        with pytest.raises(HTTPException) as error:
            await preview(database, order, sid)
        assert error.value.status_code == 403


@pytest.mark.parametrize('state', ['queued', 'armed', 'unknown'])
async def test_shift_close_x_z_refuse_unintegrated_automation(database, checkout_dependencies, state):
    order, sid, _ = await setup(database)
    p = await arm(database, await preview(database, order, sid), 'timeout_after_acceptance' if state == 'unknown' else 'accepted')
    if state != 'queued':
        p = await run(database, p, 'process')
    for kind in ('x', 'z', 'close'):
        async with database() as session:
            with pytest.raises(HTTPException) as error:
                if kind == 'close':
                    await fiscal.shift(session, await user(session), fiscal.REGISTER,
                        MockShiftCommand(expected_shift_revision=p.shift_revision), str(uuid4()), opening=False)
                else:
                    await fiscal_returns.report(session, await user(session), fiscal.REGISTER,
                        MockReportCreate(kind=kind, expected_shift_revision=p.shift_revision, confirm=True), str(uuid4()))
            assert error.value.detail['code'] == 'automation_report_unsupported'


async def test_changed_ttn_while_armed_blocks_strategy_and_requires_confirmed_disarm(database, checkout_dependencies):
    order, sid, _ = await setup(database)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    async with database() as session:
        shipment = await session.get(Shipment, sid)
        shipment.tracking_number, shipment.status = 'MOCK-' + 'A' * 32, 'delivered'
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await run(database, project, 'trigger')
    assert error.value.detail['code'] == 'stale_preview'
    with pytest.raises(HTTPException) as error:
        await preview(database, order, sid)
    assert error.value.detail['code'] == 'fiscal_owner_exists'
    project = await run(database, project, 'cancel', outcome='timeout_before_acceptance')
    async with database() as session:
        with pytest.raises(HTTPException):
            await automation.assert_manual_owner(session, order.id)
    project = await run(database, project, 'cancel', outcome='confirmed')
    assert project.status == 'cancelled'
    async with database() as session:
        p = await fiscal.create_preview(session, await user(session), order.id,
            FiscalPreviewCreate(cash_register_id=fiscal.REGISTER), str(uuid4()))
        manual = await fiscal.submit(session, await user(session), order.id, approval(p), str(uuid4()))
        assert manual.status == 'queued'
        assert await automation.owner(session, order.id) == 'sc_manual'
        cancels = (await session.scalars(select(AutomationCommand).where(AutomationCommand.operation == 'cancel'))).all()
        assert len(cancels) == 1 and cancels[0].attempts == 2


async def test_cancel_vs_trigger_race_has_one_consistent_winner(database, checkout_dependencies):
    order, sid, _ = await setup(database)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    async with database() as session:
        (await session.get(Shipment, sid)).status = 'delivered'
        await session.commit()
    async def attempt(op):
        try:
            return await run(database, project, op)
        except HTTPException as error:
            assert error.detail['code'] == 'revision_conflict'
            return None
    a, b = await asyncio.wait_for(asyncio.gather(attempt('cancel'), attempt('trigger')), timeout=15)
    assert (a is None) != (b is None)
    winner = a or b
    async with database() as session:
        record = await session.get(MockAutomationRecord, project.provider_uuid)
        assert record.status == winner.status
        assert await automation.owner(session, order.id) == ('checkbox_ettn' if winner.status == 'issued' else None)


async def test_api_auth_confirmation_idempotency_recovery_and_owner_projection(database, checkout_dependencies):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.fiscal_automation import backoffice_router
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    application = FastAPI()
    application.include_router(backoffice_router)
    async def sessions():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await user(session)
    application.dependency_overrides[get_db_session] = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://mock.test') as client:
        assert (await client.get('/orders/1/fiscal-automations')).status_code == 401
        application.dependency_overrides[get_current_admin_user] = actor
        order, sid, _ = await setup(database)
        body = {'cash_register_id': fiscal.REGISTER, 'shipment_id': sid, 'condition': 'mock_delivery_confirmed'}
        url = f'/orders/{order.id}/fiscal-automation-preview'
        assert (await client.post(url, json=body)).status_code == 422
        preview_result = await client.post(url, json=body, headers={'Idempotency-Key': str(uuid4())})
        assert preview_result.status_code == 200
        p = preview_result.json()
        assert p['allowed_actions'] == ['arm'] and p['provider_contract'] == 'unknown'
        recovered = await client.get(f"/orders/{order.id}/fiscal-automation-previews/{p['id']}")
        assert recovered.status_code == 200 and recovered.json()['snapshot_hash'] == p['snapshot_hash']
        assert (await client.get(f"/orders/{order.id + 1}/fiscal-automation-previews/{p['id']}")).status_code == 404
        create_body = {'cash_register_id': fiscal.REGISTER, 'preview_id': p['id'], 'expected_revision': p['revision'], 'snapshot_hash': p['snapshot_hash']}
        create_url = f'/orders/{order.id}/fiscal-automations'
        assert (await client.post(create_url, json=create_body, headers={'Idempotency-Key': str(uuid4())})).status_code == 422
        create_body['confirm'] = False
        assert (await client.post(create_url, json=create_body, headers={'Idempotency-Key': str(uuid4())})).status_code == 422
        create_body['confirm'] = True
        result = await client.post(create_url, json=create_body, headers={'Idempotency-Key': str(uuid4())})
        assert result.status_code == 200 and result.json()['status'] == 'queued'
        project = result.json()
        owner = (await client.get(create_url)).json()
        assert owner['owner'] == 'checkbox_ettn' and owner['items'][0]['id'] == project['id']
        assert not owner['allowed_actions']
        process = await client.post(f"/fiscal-automations/{project['id']}/process", json={'expected_revision': project['revision']}, headers={'Idempotency-Key': str(uuid4())})
        assert process.status_code == 200 and process.json()['status'] == 'armed'
        current = await client.get(f"/fiscal-automations/{project['id']}")
        assert current.json()['external_id'].startswith('MOCK-ETTN-')
