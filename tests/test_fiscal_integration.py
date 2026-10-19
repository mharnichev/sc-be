"""Isolated PostgreSQL mock fiscal approval, reconciliation and concurrency."""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.fiscal import FiscalDocument, FiscalEvent, FiscalJob, MockReceipt
from app.models.order import OrderItem, OrderPayment
from app.schemas.fiscal import (FiscalCommand, FiscalDeliveryCommand, FiscalPreviewCreate,
    FiscalProcessCommand, FiscalSubmissionCreate, MockFiscalInboxCreate, MockShiftCommand)
from app.schemas.order import OrderHandoverCreate, OrderPaymentCreate, POSOrderCreate
from app.services import fiscal, local_sales
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def mock_flags(monkeypatch):
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', True)
    monkeypatch.setattr(settings, 'local_sales_simulation_enabled', True)
    monkeypatch.setattr(settings, 'app_env', 'test')


async def actor(session):
    return await session.get(AdminUser, 1)


async def sale(database, *, completed=False):
    product = await add_product(database, stock=5)
    async with database() as session:
        user = await actor(session)
        order = await local_sales.prepare(session, user, POSOrderCreate(items=[{'product_id': product.id, 'quantity': 1}], simulation=True), str(uuid4()))
        order = await local_sales.command(session, user, order.id, OrderPaymentCreate(expected_revision=order.revision,
            amount_minor=10000, method='cash', source='operator_confirmation', evidence='synthetic payment'), str(uuid4()))
        if completed:
            order = await local_sales.command(session, user, order.id, OrderHandoverCreate(expected_revision=order.revision, evidence='synthetic handover'), str(uuid4()), handover=True)
        await fiscal.shift(session, user, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
        return order


async def preview(database, order_id):
    async with database() as session:
        return await fiscal.create_preview(session, await actor(session), order_id, FiscalPreviewCreate(cash_register_id=fiscal.REGISTER), str(uuid4()))


def approval(value, **kwargs):
    return FiscalSubmissionCreate(preview_id=value.id, expected_revision=value.revision, snapshot_hash=value.snapshot_hash,
        cash_register_id=value.cash_register_id, confirm=True, **kwargs)


async def submit(database, order_id, payload, key=None):
    async with database() as session:
        return await fiscal.submit(session, await actor(session), order_id, payload, key or str(uuid4()))


async def command(database, doc, operation, **kwargs):
    typ = {'process': FiscalProcessCommand, 'cancel': FiscalCommand, 'delivery': FiscalDeliveryCommand}[operation]
    async with database() as session:
        acknowledgement = await fiscal.command(session, await actor(session), doc.id, typ(expected_revision=doc.revision, **kwargs), str(uuid4()), operation)
        if operation == 'process':
            return await fiscal.get_document(session, await actor(session), doc.id)
        return acknowledgement


async def test_completed_sale_approval_delivery_and_keyed_replay(database, checkout_dependencies):
    order = await sale(database, completed=True)
    p = await preview(database, order.id)
    assert p.snapshot['amount_minor'] == 10000 and p.allowed_actions == ['submit']
    key = str(uuid4())
    a, b = await asyncio.gather(submit(database, order.id, approval(p), key), submit(database, order.id, approval(p), key))
    assert a == b and a.status == 'queued'
    with pytest.raises(HTTPException) as error:
        await submit(database, order.id, approval(p))
    assert error.value.detail['code'] == 'preview_consumed'
    issued = await command(database, a, 'process', mock_delivery_outcome='failed')
    assert issued.status == 'issued' and issued.delivery.status == 'failed'
    delivered = await command(database, issued, 'delivery')
    assert delivered.delivery.status == 'delivered' and delivered.delivery.attempts == 2
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert (await local_sales.load(session, order.id)).revision == order.revision
        assert len(await fiscal.list_documents(session, await actor(session), order.id)) == 1


async def test_unknown_reconciles_same_uuid_inbox_and_poll_no_downgrade(database, checkout_dependencies):
    order = await sale(database)
    doc = await submit(database, order.id, approval(await preview(database, order.id), mock_outcome='timeout_after_acceptance'))
    unknown = await command(database, doc, 'process')
    assert unknown.status == 'unknown'
    with pytest.raises(HTTPException) as error:
        await command(database, unknown, 'cancel')
    assert error.value.detail['code'] == 'already_dispatched'
    async def receive(status):
        async with database() as session:
            return await fiscal.inbox(session, await actor(session), MockFiscalInboxCreate(
                provider_operation_id=unknown.provider_uuid, event_id=uuid4(), status=status), str(uuid4()))
    async def poll():
        async with database() as session:
            await fiscal.process_job(session, await actor(session), doc.id)
    await asyncio.gather(receive('issued'), poll())
    result = await receive('processing')
    assert result.status == 'issued' and result.provider_uuid == unknown.provider_uuid
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
        assert (await session.get(FiscalJob, 1)).status == 'done'


async def test_cancel_reapprove_requires_new_preview(database, checkout_dependencies):
    order = await sale(database)
    p = await preview(database, order.id)
    doc = await submit(database, order.id, approval(p))
    cancelled = await command(database, doc, 'cancel')
    assert cancelled.status == 'cancelled'
    with pytest.raises(HTTPException):
        await submit(database, order.id, approval(p))
    doc2 = await submit(database, order.id, approval(await preview(database, order.id)))
    assert doc2.id != doc.id and doc2.provider_uuid != doc.provider_uuid


async def test_snapshot_shift_and_origin_revocation_fail_closed(database, checkout_dependencies):
    order = await sale(database)
    p = await preview(database, order.id)
    async with database() as session:
        item = (await session.scalars(select(OrderItem))).one()
        item.product_name = 'Changed snapshot'
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await submit(database, order.id, approval(p))
    assert error.value.detail['code'] == 'stale_preview'
    doc = await submit(database, order.id, approval(await preview(database, order.id)))
    async with database() as session:
        user = await actor(session)
        user.is_superuser = False
        user.can_fiscalize_sales = False
        session.add(AdminUser(id=2, email='worker@example.test', hashed_password='unused', is_active=True, is_superuser=True))
        await session.commit()
    async with database() as session:
        result = await fiscal.process_job(session, await session.get(AdminUser, 2), doc.id)
        assert result.status == 'error'
    async with database() as session:
        user = await actor(session)
        user.is_superuser = True
        await session.commit()
    async with database() as session:
        result = await fiscal.process_job(session, await actor(session), doc.id)
        assert result.status == 'error'
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 0


async def test_expired_lease_reclaimed_and_cancel_races_dispatch(database, checkout_dependencies):
    order = await sale(database)
    doc = await submit(database, order.id, approval(await preview(database, order.id)))
    async with database() as session:
        await session.execute(update(FiscalJob).values(status='leased', fence=4, lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    async def process():
        async with database() as session:
            return await fiscal.process_job(session, await actor(session), doc.id)
    async def cancel():
        try:
            return await command(database, doc, 'cancel')
        except HTTPException as error:
            assert error.detail['code'] in ('already_dispatched', 'revision_conflict')
    await asyncio.gather(process(), cancel())
    async with database() as session:
        saved = await session.get(FiscalDocument, doc.id)
        count = await session.scalar(select(func.count()).select_from(MockReceipt))
        assert (saved.status, count) in (('issued', 1), ('cancelled', 0))


async def test_shift_generation_expiry_and_immutable_item_recheck(database, checkout_dependencies):
    from app.models.fiscal import FiscalPreview
    order = await sale(database)
    p = await preview(database, order.id)
    async with database() as session:
        user = await actor(session)
        await fiscal.shift(session, user, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=1), str(uuid4()), opening=False)
        await fiscal.shift(session, user, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=2), str(uuid4()), opening=True)
    with pytest.raises(HTTPException) as error:
        await submit(database, order.id, approval(p))
    assert error.value.detail['code'] == 'stale_preview'
    p2 = await preview(database, order.id)
    async with database() as session:
        await session.execute(update(FiscalPreview).where(FiscalPreview.id == p2.id).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await submit(database, order.id, approval(p2))
    assert error.value.detail['code'] == 'preview_expired'
    p3 = await preview(database, order.id)
    doc = await submit(database, order.id, approval(p3))
    async with database() as session:
        item = (await session.scalars(select(OrderItem))).one()
        item.product_name = 'Changed after approval'
        await session.commit()
    invalid = await command(database, doc, 'process')
    assert invalid.status == 'error' and invalid.allowed_actions == ['cancel']
    cancelled = await command(database, invalid, 'cancel')
    assert cancelled.status == 'cancelled'
    new = await submit(database, order.id, approval(await preview(database, order.id)))
    assert new.id != doc.id


@pytest.mark.parametrize('change', ['flag', 'shift', 'origin', 'order'])
async def test_accepted_truth_reconciles_after_guard_changes(database, checkout_dependencies, monkeypatch, change):
    from app.models.order import Order
    order = await sale(database)
    doc = await submit(database, order.id, approval(await preview(database, order.id), mock_outcome='timeout_after_acceptance'))
    unknown = await command(database, doc, 'process')
    async with database() as session:
        session.add(AdminUser(id=2, email='reconciler@example.test', hashed_password='unused', is_active=True, is_superuser=True))
        if change == 'origin':
            user = await actor(session)
            user.is_active = False
        elif change == 'shift':
            await fiscal.shift(session, await actor(session), fiscal.REGISTER, MockShiftCommand(expected_shift_revision=1), str(uuid4()), opening=False)
        elif change == 'order':
            row = await session.get(Order, order.id)
            row.revision += 1
        await session.commit()
    if change == 'flag':
        monkeypatch.setattr(settings, 'checkbox_mock_enabled', False)
    async with database() as session:
        reconciled = await fiscal.process_job(session, await session.get(AdminUser, 2), doc.id)
        assert reconciled.status == 'issued'
        result = fiscal.document_response(session, await session.get(AdminUser, 2), reconciled)
        assert 'print' in result.allowed_actions
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1


async def test_new_dispatch_kill_switch_and_permission_replay(database, checkout_dependencies, monkeypatch):
    order = await sale(database)
    p = await preview(database, order.id)
    key = str(uuid4())
    doc = await submit(database, order.id, approval(p), key)
    async with database() as session:
        user = await actor(session)
        user.is_superuser = False
        user.can_fiscalize_sales = False
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await submit(database, order.id, approval(p), key)
    assert error.value.status_code == 403
    async with database() as session:
        user = await actor(session)
        user.is_superuser = True
        await session.commit()
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', False)
    async with database() as session:
        result = await fiscal.process_job(session, await actor(session), doc.id)
        assert result.status == 'error'
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 0


async def test_inbox_cannot_create_acceptance_and_duplicate_event_is_stable(database, checkout_dependencies):
    order = await sale(database)
    doc = await submit(database, order.id, approval(await preview(database, order.id)))
    body = MockFiscalInboxCreate(provider_operation_id=doc.provider_uuid, event_id=uuid4(), status='issued')
    async with database() as session:
        result = await fiscal.inbox(session, await actor(session), body, str(uuid4()))
        assert result.status == 'queued'
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 0
    async with database() as session:
        result = await fiscal.inbox(session, await actor(session), body, str(uuid4()))
        assert result.status == 'queued'
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await fiscal.inbox(session, await actor(session), body.model_copy(update={'status': 'processing'}), str(uuid4()))
        assert error.value.detail['code'] == 'inbox_conflict'


async def test_routes_auth_strict_confirmation_and_safe_print(database, checkout_dependencies):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.fiscal import backoffice_router
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    application = FastAPI()
    application.include_router(backoffice_router)
    async def sessions():
        async with database() as session:
            yield session
    application.dependency_overrides[get_db_session] = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://mock.test') as client:
        assert (await client.get('/integrations/readiness')).status_code == 401
        async def user():
            async with database() as session:
                return await actor(session)
        application.dependency_overrides[get_current_admin_user] = user
        body = {'preview_id': 1, 'expected_revision': 1, 'snapshot_hash': 'a' * 64, 'cash_register_id': fiscal.REGISTER, 'confirm': False}
        assert (await client.post('/orders/1/fiscal-submissions', json=body, headers={'Idempotency-Key': str(uuid4())})).status_code == 422
        assert (await client.post('/orders/1/fiscal-preview', json={'cash_register_id': fiscal.REGISTER})).status_code == 422
        assert (await client.post('/integrations/checkbox/cash-registers/REAL/open-shift', json={'expected_shift_revision': 0}, headers={'Idempotency-Key': str(uuid4())})).status_code == 409
        order = await sale(database)
        doc = await submit(database, order.id, approval(await preview(database, order.id)))
        issued = await command(database, doc, 'process')
        printed = await client.get(f'/fiscal-documents/{issued.id}/print')
        assert printed.status_code == 200 and fiscal.NOTICE in printed.text
        assert 'no-store' in printed.headers['Cache-Control']
        assert len((await client.get(f'/orders/{order.id}/fiscal-documents')).json()) == 1
        assert (await client.get(f'/orders/{order.id + 1}/fiscal-previews/1')).status_code == 404


async def test_crossed_actor_workers_do_not_deadlock(database, checkout_dependencies):
    order = await sale(database)
    async with database() as session:
        session.add(AdminUser(id=2, email='second@example.test', hashed_password='unused', is_active=True, is_superuser=True))
        await session.commit()
    first = await submit(database, order.id, approval(await preview(database, order.id)))
    async with database() as session:
        product_id = (await session.scalars(select(OrderItem.product_id))).one()
        user = await session.get(AdminUser, 2)
        other = await local_sales.prepare(session, user, POSOrderCreate(items=[{'product_id': product_id, 'quantity': 1}], simulation=True), str(uuid4()))
        other = await local_sales.command(session, user, other.id, OrderPaymentCreate(expected_revision=other.revision, amount_minor=10000,
            method='card', source='operator_confirmation', evidence='second synthetic payment'), str(uuid4()))
        p = await fiscal.create_preview(session, user, other.id, FiscalPreviewCreate(cash_register_id=fiscal.REGISTER), str(uuid4()))
        second = await fiscal.submit(session, user, other.id, approval(p), str(uuid4()))
    async def work(user_id, doc_id):
        async with database() as session:
            return await fiscal.process_job(session, await session.get(AdminUser, user_id), doc_id)
    await asyncio.wait_for(asyncio.gather(work(2, first.id), work(1, second.id)), timeout=15)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 2


async def test_fenced_stale_worker_cannot_overwrite_new_result(database, checkout_dependencies, monkeypatch):
    order = await sale(database)
    doc = await submit(database, order.id, approval(await preview(database, order.id)))
    claimed, release = asyncio.Event(), asyncio.Event()
    original = fiscal.require
    slow_calls = 0
    async def paused_require(session, user, **kwargs):
        nonlocal slow_calls
        if asyncio.current_task().get_name() == 'fiscal_slow_worker':
            slow_calls += 1
            if slow_calls == 2:
                claimed.set()
                await release.wait()
        return await original(session, user, **kwargs)
    monkeypatch.setattr(fiscal, 'require', paused_require)
    async def work():
        async with database() as session:
            return await fiscal.process_job(session, await actor(session), doc.id)
    slow = asyncio.create_task(work(), name='fiscal_slow_worker')
    await asyncio.wait_for(claimed.wait(), timeout=10)
    async with database() as session:
        await session.execute(update(FiscalJob).values(lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    fast = await asyncio.wait_for(work(), timeout=10)
    assert fast.status == 'issued'
    release.set()
    late = await asyncio.wait_for(slow, timeout=10)
    assert late.status == 'issued'
    async with database() as session:
        job = (await session.scalars(select(FiscalJob))).one()
        assert job.fence == 2 and job.status == 'done'
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalEvent).where(FiscalEvent.operation == 'worker_result')) == 1


async def test_different_submission_keys_cannot_duplicate_sale(database, checkout_dependencies):
    order = await sale(database)
    p1, p2 = await preview(database, order.id), await preview(database, order.id)
    async def attempt(p):
        try:
            return await submit(database, order.id, approval(p))
        except HTTPException as error:
            assert error.detail['code'] == 'sale_already_submitted'
    results = await asyncio.gather(attempt(p1), attempt(p2))
    assert sum(item is not None for item in results) == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1


@pytest.mark.parametrize('change', ['shop', 'legacy', 'not_simulated', 'cancelled', 'partial_payment'])
async def test_only_exact_stage2_simulation_sale_is_eligible(database, checkout_dependencies, change):
    from app.models.order import Order, OrderStatus
    order = await sale(database)
    async with database() as session:
        row = await session.get(Order, order.id)
        if change == 'shop':
            row.channel = 'shop'
        elif change == 'legacy':
            row.channel = None
        elif change == 'not_simulated':
            row.simulation = False
        elif change == 'cancelled':
            row.status = OrderStatus.cancelled
        else:
            # Metadata fixture intentionally permits corrupt facts; deployed
            # migration prevents edits, while the server must still fail closed.
            payment = (await session.scalars(select(OrderPayment))).one()
            payment.amount_minor -= 1
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await preview(database, order.id)
    assert error.value.detail['code'] in ('simulation_sale_required', 'simulation_payment_required')


async def test_unpaid_simulation_is_ineligible(database, checkout_dependencies):
    product = await add_product(database, stock=1)
    async with database() as session:
        user = await actor(session)
        order = await local_sales.prepare(session, user, POSOrderCreate(items=[{'product_id': product.id, 'quantity': 1}], simulation=True), str(uuid4()))
        await fiscal.shift(session, user, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
    with pytest.raises(HTTPException) as error:
        await preview(database, order.id)
    assert error.value.detail['code'] == 'simulation_payment_required'


async def test_concurrent_process_key_returns_frozen_acknowledgement(database, checkout_dependencies):
    order = await sale(database)
    doc = await submit(database, order.id, approval(await preview(database, order.id)))
    key = str(uuid4())
    payload = FiscalProcessCommand(expected_revision=doc.revision)
    async def run():
        async with database() as session:
            return await fiscal.command(session, await actor(session), doc.id, payload, key, 'process')
    first, second = await asyncio.gather(run(), run())
    assert first.model_dump(mode='json') == second.model_dump(mode='json')
    assert first.status == 'queued'  # Frozen 202 admission ACK, never result truth.
    assert (await run()).model_dump(mode='json') == first.model_dump(mode='json')
    async with database() as session:
        current = await fiscal.get_document(session, await actor(session), doc.id)
        assert current.status == 'issued'
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
