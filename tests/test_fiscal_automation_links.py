"""Canonical ETTN receipts reuse local returns/reports without another sale."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.fiscal import (FiscalDocument, FiscalJob, FiscalPreview, MockReceipt, MockRefund,
    MockReturnReceipt, ReturnRecord)
from app.models.fiscal_automation import FiscalAutomation, MockAutomationRecord
from app.models.order import Order, OrderPayment
from app.models.product import Product
from app.models.shipment import Shipment
from app.schemas.fiscal import FiscalCommand, FiscalDeliveryCommand, MockFiscalInboxCreate, MockShiftCommand
from app.schemas.fiscal_returns import (FiscalShareCreate, FiscalShareRevoke, MockReportCreate,
    ReconciliationAckCreate)
from app.schemas.order import POSOrderCreate, OrderPaymentCreate
from app.schemas.shipment import ShipmentCreate, ShipmentCommand
from app.services import fiscal, fiscal_automation as automation, fiscal_returns as returns, local_sales, shipments
from tests.test_fiscal_automation_integration import enabled, user, setup, preview, arm, run
from tests.test_fiscal_returns_integration import create, refund, receive, return_preview
from tests.test_fiscal_integration import approval, submit, command
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product
from tests.test_shipments_integration import ADDRESS, PACK

pytestmark = pytest.mark.anyio


async def issued(database, *, quantity=3):
    product = await add_product(database, stock=10)
    async with database() as session:
        actor = await user(session)
        order = await local_sales.prepare(session, actor,
            POSOrderCreate(items=[{'product_id': product.id, 'quantity': quantity}], simulation=True), str(uuid4()))
        order = await local_sales.command(session, actor, order.id, OrderPaymentCreate(expected_revision=order.revision,
            amount_minor=quantity * 10000, method='card', source='operator_confirmation', evidence='synthetic paid goods'), str(uuid4()))
        row = await session.get(Order, order.id)
        row.fulfillment_method, row.shipping_payload_json = 'nova_poshta', ADDRESS.copy()
        await session.commit()
        order = await shipments.create(session, actor, order.id,
            ShipmentCreate(expected_revision=order.revision, packaging={**PACK, 'declared_value_minor': quantity * 10000, 'cod_amount_minor': 0}), str(uuid4()))
        sid = order.shipments[0].id
        await fiscal.shift(session, actor, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    async with database() as session:
        order = await shipments.command(session, await user(session), order.id, sid,
            ShipmentCommand(expected_revision=order.revision, evidence='synthetic original carrier handover'), str(uuid4()), 'handover')
        order = await shipments.command(session, await user(session), order.id, sid,
            ShipmentCommand(expected_revision=order.revision, mock_status='delivered'), str(uuid4()), 'refresh')
    result = await run(database, project, 'trigger')
    async with database() as session:
        doc = await fiscal.get_document(session, await user(session), result.fiscal_document_id)
    return order, result, doc, product.id


async def historical(database):
    """Exact stage-6 issued facts before 0118; metadata fixture has no backfill."""
    order, sid, pid = await setup(database)
    project = await run(database, await arm(database, await preview(database, order, sid)), 'process')
    async with database() as session:
        row = await session.get(FiscalAutomation, project.id)
        record = await session.get(MockAutomationRecord, project.provider_uuid)
        result = {'document_kind': 'mock_automation_result', 'is_fiscal_document': False,
            'simulation': True, 'is_sent_dps': False, 'provider_contract': 'unknown',
            'external_id': record.external_id, 'provider_uuid': row.provider_uuid,
            'snapshot_hash': row.snapshot_hash, 'amount_minor': row.snapshot['amount_minor'],
            'payment_id': row.payment_id, 'shipment_id': row.shipment_id,
            'tracking_number': row.snapshot['tracking_number'], 'condition': automation.CONDITION,
            'issued_at': datetime.now(UTC).isoformat(), 'notice': automation.NOTICE}
        row.status, row.result, record.status, record.result = 'issued', result, 'issued', result
        await session.commit()
        project = await automation.get(session, await user(session), project.id)
    return order, project, pid


async def test_trigger_atomically_creates_one_canonical_receipt_and_no_job(database, checkout_dependencies):
    order, project, doc, _ = await issued(database)
    assert project.fiscal_document_id == doc.id and doc.automation_id == project.id and doc.source == 'automation'
    assert doc.provider_uuid == project.provider_uuid
    assert doc.snapshot['automation']['approval_snapshot_hash'] == project.snapshot_hash
    assert doc.snapshot['items'] == project.snapshot['items']
    assert doc.snapshot['amount_minor'] == project.snapshot['amount_minor']
    assert project.result['document_kind'] == 'mock_automation_result' and not project.result['is_fiscal_document']
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalJob)) == 0
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 1
        listed = await fiscal.list_documents(session, await user(session), order.id)
        assert [d.id for d in listed] == [doc.id]
    replay = await run(database, project, 'materialize')
    assert replay.fiscal_document_id == doc.id


async def test_partial_return_refund_restock_fiscal_return_and_reports_reuse_original(database, checkout_dependencies):
    order, project, sale, pid = await issued(database)
    row = await create(database, order, sale, quantity=1)
    assert row.amount_minor == 10000
    refunded = await refund(database, row)
    assert refunded.refund.amount_minor == 10000
    from app.services import mock_cod
    from app.schemas.mock_cod import MockReturnShipmentCreate, MockParcelReceiptCreate
    async with database() as session:
        current = await session.get(Order, order.id)
        parcel = await mock_cod.create_return_shipment(session, await user(session), order.id, order.shipments[0].id,
            MockReturnShipmentCreate(expected_revision=current.revision, confirm=True, evidence='Synthetic returned parcel',
                items=[{'order_item_id': row.items[0].order_item_id, 'quantity': 1}]), str(uuid4()))
        return_id = next(s.id for s in parcel.shipments if s.id != order.shipments[0].id)
        parcel = await shipments.command(session, await user(session), order.id, return_id,
            ShipmentCommand(expected_revision=parcel.revision, mock_status='returned'), str(uuid4()), 'refresh')
        await mock_cod.receive_return(session, await user(session), order.id, return_id,
            MockParcelReceiptCreate(expected_revision=parcel.revision, confirm=True, evidence='Synthetic warehouse inspection',
                items=[{'order_item_id': row.items[0].order_item_id, 'quantity': 1, 'disposition': 'restock'}]), str(uuid4()))
    received = await receive(database, row)
    assert received.receipt.shipment_return_receipt_id is not None
    assert received.receipt.items[0].quantity == 1
    p = await return_preview(database, row)
    returned = await submit(database, order.id, approval(p))
    returned = await command(database, returned, 'process')
    assert returned.kind == 'return' and returned.parent_sale_id == sale.id
    assert returned.automation_id is None and returned.source == 'manual'
    async with database() as session:
        assert (await session.get(Product, pid)).stock_quantity == 8
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(MockRefund)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReturnReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(MockAutomationRecord)) == 1
        report = await returns.report(session, await user(session), fiscal.REGISTER,
            MockReportCreate(kind='x', expected_shift_revision=sale.shift_revision, confirm=True), str(uuid4()))
        assert report.totals.sale_count == 1 and report.totals.return_count == 1
        assert report.totals.sales_minor == 30000 and report.totals.returns_minor == 10000 and report.totals.net_minor == 20000
        assert set(report.totals.document_ids) == {sale.id, returned.id}
        z = await returns.report(session, await user(session), fiscal.REGISTER,
            MockReportCreate(kind='z', expected_shift_revision=sale.shift_revision, confirm=True), str(uuid4()))
        assert z.totals == report.totals
        assert (await session.get(FiscalAutomation, project.id)).result == project.result


async def test_historical_explicit_repair_flag_off_closed_shift_preserves_approved_prices(database, checkout_dependencies, monkeypatch):
    order, project, _ = await historical(database)
    original = deepcopy(project.model_dump(mode='json'))
    async with database() as session:
        reconciliation = await returns.reconciliation(session, await user(session))
        assert [r.resource_id for r in reconciliation.buckets['automation_receipt_missing']] == [str(project.id)]
        assert project.fiscal_document_id is None and 'materialize' in project.allowed_actions
        ack = await returns.acknowledge(session, await user(session), ReconciliationAckCreate(
            case_type='automation_receipt_missing', resource_id=str(project.id), evidence='explicit local recovery reviewed'), str(uuid4()))
        assert ack.resource_id == str(project.id)
        register = await fiscal.cash_register(session)
        register.shift_status, register.shift_revision = 'closed', register.shift_revision + 1
        current = await local_sales.load(session, order.id)
        current.total_amount = 999
        current.items[0].price = 999
        actor = await user(session)
        actor.is_superuser, actor.can_fiscalize_sales = False, False
        session.add(AdminUser(id=2, email='recovery@example.test', hashed_password='unused', is_active=True, is_superuser=False, can_fiscalize_sales=True))
        await session.commit()
    monkeypatch.setattr(settings, 'checkbox_mock_enabled', False)
    monkeypatch.setattr(settings, 'checkbox_ettn_mock_enabled', False)
    key = str(uuid4())
    async def recover():
        async with database() as session:
            return await automation.command(session, await session.get(AdminUser, 2), project.id,
                FiscalCommand(expected_revision=project.revision), key, 'materialize')
    first, second = await asyncio.gather(recover(), recover())
    assert first.model_dump(mode='json') == second.model_dump(mode='json') and first.fiscal_document_id
    async with database() as session:
        doc = await fiscal.get_document(session, await session.get(AdminUser, 2), first.fiscal_document_id)
        assert doc.snapshot['amount_minor'] == 10000 and doc.shift_revision == project.shift_revision
        assert doc.snapshot['items'] == project.snapshot['items']
        after = await automation.get(session, await session.get(AdminUser, 2), project.id)
        assert after.result == original['result'] and after.snapshot == original['snapshot']
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalJob)) == 0
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert not (await returns.reconciliation(session, await session.get(AdminUser, 2))).buckets['automation_receipt_missing']


async def test_materialize_different_key_race_reuses_one_canonical_receipt(database, checkout_dependencies):
    _, project, _ = await historical(database)
    async def attempt():
        try:
            return await run(database, project, 'materialize')
        except HTTPException as error:
            assert error.detail['code'] == 'revision_conflict'
            return None
    first, second = await asyncio.gather(attempt(), attempt())
    assert (first is None) != (second is None)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalPreview)) == 1


@pytest.mark.parametrize('operation', ['process', 'cancel', 'worker', 'inbox'])
async def test_canonical_receipt_cannot_dispatch_cancel_or_accept_manual_inbox(database, checkout_dependencies, operation):
    _, project, doc, _ = await issued(database)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            if operation == 'worker':
                await fiscal.process_job(session, await user(session), doc.id)
            elif operation == 'inbox':
                await fiscal.inbox(session, await user(session), MockFiscalInboxCreate(
                    provider_operation_id=project.provider_uuid, event_id=uuid4(), status='issued'), str(uuid4()))
            else:
                await fiscal.command(session, await user(session), doc.id, FiscalCommand(expected_revision=doc.revision), str(uuid4()), operation)
        assert error.value.detail['code'] == 'automation_document_terminal'
        assert await session.scalar(select(func.count()).select_from(FiscalJob)) == 0
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1


@pytest.mark.parametrize('tamper', ['record', 'record_hash', 'result', 'result_flag_type', 'receipt_time', 'scope', 'snapshot', 'source_link', 'hidden_source'])
async def test_every_consumer_rejects_forged_automation_basis(database, checkout_dependencies, tamper):
    order, project, doc, _ = await issued(database)
    async with database() as session:
        sale = await session.get(FiscalDocument, doc.id)
        record = await session.get(MockAutomationRecord, project.provider_uuid)
        if tamper == 'record':
            record.status = 'armed'
        elif tamper == 'record_hash':
            record.snapshot_hash = 'f' * 64
        elif tamper == 'result':
            record.result = {**record.result, 'amount_minor': 1}
        elif tamper == 'result_flag_type':
            malformed = {**record.result, 'simulation': 1}
            record.result = malformed
            project_row = await session.get(FiscalAutomation, project.id)
            project_row.result = malformed
            # Python True == 1; force the deliberately corrupt JSON fixture to
            # persist instead of SQLAlchemy treating it as an unchanged value.
            flag_modified(record, 'result')
            flag_modified(project_row, 'result')
        elif tamper == 'receipt_time':
            (await session.get(MockReceipt, project.provider_uuid)).accepted_at = datetime.now(UTC)
        elif tamper == 'scope':
            sale.provider_uuid = str(uuid4())
        elif tamper == 'snapshot':
            sale.snapshot = {**sale.snapshot, 'amount_minor': 1}
            sale.snapshot_hash = fiscal.digest(sale.snapshot)
            (await session.get(MockReceipt, project.provider_uuid)).snapshot_hash = sale.snapshot_hash
        elif tamper in ('source_link', 'hidden_source'):
            sale.automation_id = None
            if tamper == 'hidden_source':
                forged = {k: v for k, v in sale.snapshot.items() if k not in ('source', 'automation')}
                sale.snapshot = forged
                sale.snapshot_hash = fiscal.digest(forged)
                (await session.get(MockReceipt, project.provider_uuid)).snapshot_hash = sale.snapshot_hash
        await session.commit()
        actor = await user(session)
        with pytest.raises(HTTPException):
            await fiscal.get_document(session, actor, doc.id)
        with pytest.raises(HTTPException):
            await returns.parent_sale(session, doc.id)
        with pytest.raises(HTTPException):
            await returns.report(session, actor, fiscal.REGISTER,
                MockReportCreate(kind='x', expected_shift_revision=doc.shift_revision, confirm=True), str(uuid4()))
        with pytest.raises(HTTPException) as error:
            await returns.validate_shareable(session, sale)
        assert error.value.status_code == 404
        recon = await returns.reconciliation(session, actor)
        assert str(project.id) in [r.resource_id for r in recon.buckets['automation_receipt_missing']]
        assert recon.totals.sales_minor == 0


async def test_print_delivery_public_share_and_revoke_use_canonical_document(database, checkout_dependencies):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.fiscal import backoffice_router
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    order, project, doc, _ = await issued(database)
    application = FastAPI()
    application.include_router(backoffice_router)
    async def sessions():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await user(session)
    application.dependency_overrides[get_db_session] = sessions
    application.dependency_overrides[get_current_admin_user] = actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://mock.test') as client:
        printable = await client.get(f'/fiscal-documents/{doc.id}/print')
        assert printable.status_code == 200 and fiscal.NOTICE in printable.text
        assert 'no-store' in printable.headers['cache-control']
    async with database() as session:
        actor = await user(session)
        delivered = await fiscal.command(session, actor, doc.id,
            FiscalDeliveryCommand(expected_revision=doc.revision, mock_outcome='failed'), str(uuid4()), 'delivery')
        assert delivered.delivery.status == 'failed'
        share = await returns.share_document(session, actor, doc.id, FiscalShareCreate(), str(uuid4()))
        public = await returns.public_document(session, share.url.rsplit('/', 1)[-1])
        assert public.amount_minor == 30000 and public.is_sent_dps is False
        await returns.share_document(session, actor, doc.id, FiscalShareRevoke(), str(uuid4()), revoke=True)
        with pytest.raises(HTTPException) as error:
            await returns.public_document(session, share.url.rsplit('/', 1)[-1])
        assert error.value.status_code == 404
        assert await session.scalar(select(func.count()).select_from(FiscalJob)) == 0


async def test_materialize_auth_and_issued_basis_required(database, checkout_dependencies):
    order, sid, _ = await setup(database)
    queued = await arm(database, await preview(database, order, sid))
    with pytest.raises(HTTPException) as error:
        await run(database, queued, 'materialize')
    assert error.value.detail['code'] == 'automation_receipt_mismatch'
    async with database() as session:
        actor = await user(session)
        actor.is_superuser, actor.can_fiscalize_sales, actor.can_fiscalize_returns = False, False, True
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await run(database, queued, 'materialize')
    assert error.value.status_code == 403


async def test_materialize_api_requires_auth_key_and_exact_command(database, checkout_dependencies):
    import httpx
    from fastapi import FastAPI
    from app.api.v1.routes.fiscal_automation import backoffice_router
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    _, project, _ = await historical(database)
    application = FastAPI()
    application.include_router(backoffice_router)
    async def sessions():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await user(session)
    application.dependency_overrides[get_db_session] = sessions
    url = f'/fiscal-automations/{project.id}/materialize'
    body = {'expected_revision': project.revision}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://mock.test') as client:
        assert (await client.post(url, json=body, headers={'Idempotency-Key': str(uuid4())})).status_code == 401
        application.dependency_overrides[get_current_admin_user] = actor
        assert (await client.post(url, json=body)).status_code == 422
        assert (await client.post(url, json={**body, 'amount_minor': 1}, headers={'Idempotency-Key': str(uuid4())})).status_code == 422
        key = str(uuid4())
        first = await client.post(url, json=body, headers={'Idempotency-Key': key})
        second = await client.post(url, json=body, headers={'Idempotency-Key': key})
        assert first.status_code == 200 and first.json()['fiscal_document_id']
        assert second.json() == first.json()
