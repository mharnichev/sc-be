"""Real local PostgreSQL invoice/payment/receipt/stock/recovery contracts."""
import asyncio
from uuid import uuid4
from datetime import UTC, datetime, timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import OrderPayment, OrderHandover, OrderStatus
from app.models.product import Product
from app.models.fiscal import FiscalDocument, FiscalJob, MockReceipt, MockRefund
from app.models.qr_payment import QRPaymentAttempt, QRProviderRecord, QREvent
from app.schemas.order import POSOrderCreate, OrderHandoverCreate, OrderPaymentCreate
from app.schemas.qr_payment import QRCreate, QRCommandCreate, QRSimulate
from app.schemas.fiscal import MockShiftCommand, FiscalProcessCommand, FiscalPreviewCreate, FiscalSubmissionCreate
from app.schemas.fiscal_returns import ReturnCreate, MockRefundCreate, ReturnReceiptCreate, MockReportCreate
from app.services import fiscal, fiscal_returns, local_sales, qr_payment
from app.services.order import OrderService
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies, add_product

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    for flag in ('monobank_qr_mock_enabled','checkbox_mock_enabled','local_sales_simulation_enabled'):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, 'app_env', 'test')


async def prepare(database, *, quantity=1):
    product = await add_product(database, stock=5)
    async with database() as session:
        user = await session.get(AdminUser, 1)
        order = await local_sales.prepare(session, user, POSOrderCreate(items=[{'product_id': product.id,'quantity': quantity}], simulation=True), str(uuid4()))
        await fiscal.shift(session, user, fiscal.REGISTER, MockShiftCommand(expected_shift_revision=0), str(uuid4()), opening=True)
        attempt = await qr_payment.create(session, user, order.id, QRCreate(expected_revision=order.revision,
            cash_register_id=fiscal.REGISTER, confirm=True), str(uuid4()))
        return product, order, attempt


async def transition(database, attempt, outcome=None, *, op='simulate', key=None, event_id=None):
    payload = QRSimulate(expected_revision=attempt.revision, outcome=outcome, event_id=event_id or uuid4()) if op == 'simulate' else QRCommandCreate(expected_revision=attempt.revision)
    async with database() as session:
        return await qr_payment.command(session, await session.get(AdminUser, 1), attempt.id, payload, key or str(uuid4()), op)


async def test_paid_issued_once_handover_once_and_canonical_x_report(database, checkout_dependencies):
    product, order, attempt = await prepare(database)
    key, event = str(uuid4()), uuid4()
    a, b = await asyncio.gather(transition(database, attempt, 'success', key=key, event_id=event),
        transition(database, attempt, 'success', key=key, event_id=event))
    assert a == b and a.payment_status == 'paid' and a.receipt_status == 'issued'
    assert a.payment_id and a.fiscal_document_id and a.local_payment_path == f'/orders/qr-simulator/{a.id}'
    replay = await transition(database, a, 'processing')
    assert replay.payment_status == 'paid' and replay.receipt_status == 'issued'
    async with database() as session:
        user = await session.get(AdminUser, 1)
        before = await session.get(Product, product.id)
        assert (before.stock_quantity, before.reserved_quantity) == (5, 1)
        sale = await fiscal_returns.parent_sale(session, a.fiscal_document_id)
        assert sale.snapshot['payment']['method'] == 'monobank_qr'
        assert sale.snapshot['payment']['source'] == 'provider_confirmation'
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1
        assert await session.scalar(select(func.count()).select_from(MockReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalJob)) == 0
        current = await local_sales.load(session, order.id)
        payload = OrderHandoverCreate(expected_revision=current.revision, evidence='synthetic QR handover')
        handover_key = str(uuid4())
        completed = await local_sales.command(session,user,order.id,payload,handover_key,handover=True)
        assert (await local_sales.command(session,user,order.id,payload,handover_key,handover=True)) == completed
        await session.refresh(before)
        assert (before.stock_quantity, before.reserved_quantity) == (4, 0)
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
        report = await fiscal_returns.report(session,user,fiscal.REGISTER,MockReportCreate(kind='x',expected_shift_revision=1,confirm=True),str(uuid4()))
        assert report.totals.sale_count == 1 and report.totals.sales_minor == 10000
        assert report.totals.document_ids == [a.fiscal_document_id]


async def test_lost_response_cancel_looks_up_same_invoice_after_kill_switch(database, checkout_dependencies, monkeypatch):
    _, order, attempt = await prepare(database)
    unknown = await transition(database, attempt, 'timeout_after_acceptance')
    assert unknown.payment_status == 'unknown' and unknown.payment_id is None
    async with database() as session:
        report=await fiscal_returns.reconciliation(session,await session.get(AdminUser,1))
        assert len(report.buckets['unknown'])==1
        case=report.buckets['unknown'][0]
        assert (case.resource_id,case.order_id,case.amount_minor)==(f'qr:{attempt.id}',order.id,10000)
        assert set(case.model_dump())=={'resource_id','order_id','amount_minor','acknowledgements'}
        assert report.totals.sales_minor==0
    async with database() as session:
        current = await local_sales.load(session, order.id)
        with pytest.raises(HTTPException) as error:
            await qr_payment.create(session, await session.get(AdminUser,1), order.id, QRCreate(expected_revision=current.revision,cash_register_id=fiscal.REGISTER,confirm=True),str(uuid4()))
        assert error.value.detail['code'] == 'qr_attempt_active'
    monkeypatch.setattr(settings,'monobank_qr_mock_enabled',False)
    recovered = await transition(database, unknown, op='cancel')
    assert recovered.payment_status == 'paid' and recovered.receipt_status == 'issued'
    assert recovered.id == attempt.id and recovered.provider_invoice_id == attempt.provider_invoice_id
    async with database() as session:
        report=await fiscal_returns.reconciliation(session,await session.get(AdminUser,1))
        assert report.buckets['unknown']==[] and report.totals.sales_minor==10000
        assert await session.scalar(select(func.count()).select_from(QRProviderRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1


@pytest.mark.parametrize('outcome',['success_receipt_error','success_receipt_pending'])
async def test_paid_receipt_problem_blocks_handover_then_recovers_same_payment(database, checkout_dependencies, outcome):
    _, order, attempt = await prepare(database)
    paid = await transition(database, attempt, outcome)
    assert paid.payment_status == 'paid' and paid.receipt_status in ('error','pending') and paid.payment_id
    assert paid.fiscal_document_id is None
    async with database() as session:
        user = await session.get(AdminUser,1)
        current = await local_sales.load(session,order.id)
        assert not (await local_sales.response(current,session,user)).sales_actions['can_handover']
        with pytest.raises(HTTPException) as error:
            await local_sales.command(session,user,order.id,OrderHandoverCreate(expected_revision=current.revision,evidence='synthetic blocked'),str(uuid4()),handover=True)
        assert error.value.detail['code'] == 'qr_payment_owns_order'
    issued = await transition(database, paid, 'receipt_issued')
    assert issued.receipt_status == 'issued' and issued.payment_id == paid.payment_id
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1


@pytest.mark.parametrize('outcome,status',[('decline','declined'),('expiry','expired')])
async def test_proven_terminal_allows_new_invoice_but_timer_does_not(database,checkout_dependencies,monkeypatch,outcome,status):
    _, order, attempt = await prepare(database)
    class AfterExpiry:
        @staticmethod
        def now(_tz):
            return attempt.expires_at+timedelta(seconds=1)
    monkeypatch.setattr(qr_payment,'datetime',AfterExpiry)
    # Time elapsing alone never creates a terminal payment state or releases stock.
    checked = await transition(database,attempt,op='refresh')
    assert checked.payment_status == 'waiting'
    declined = await transition(database,checked,outcome)
    assert declined.payment_status == status
    async with database() as session:
        current = await local_sales.load(session,order.id)
        new = await qr_payment.create(session,await session.get(AdminUser,1),order.id,QRCreate(expected_revision=current.revision,cash_register_id=fiscal.REGISTER,confirm=True),str(uuid4()))
        assert new.id != attempt.id and new.provider_invoice_id != attempt.provider_invoice_id
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0


async def test_active_invoice_blocks_manual_payment_legacy_cancel_and_shift_close(database,checkout_dependencies):
    _,order,attempt = await prepare(database)
    async with database() as session:
        user=await session.get(AdminUser,1)
        current=await local_sales.load(session,order.id)
        with pytest.raises(HTTPException) as error:
            await local_sales.command(session,user,order.id,OrderPaymentCreate(expected_revision=current.revision,amount_minor=10000,method='cash',source='operator_confirmation',evidence='synthetic'),str(uuid4()))
        assert error.value.detail['code']=='qr_payment_owns_order'
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await OrderService().update_status(session,order.id,OrderStatus.cancelled,admin_user_id=1)
        assert error.value.detail['code']=='qr_payment_owns_order'
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await fiscal.shift(session,await session.get(AdminUser,1),fiscal.REGISTER,MockShiftCommand(expected_shift_revision=1),str(uuid4()),opening=False)
        assert error.value.detail['code']=='shift_unsettled_qr'
    cancelled=await transition(database,attempt,op='cancel')
    assert cancelled.payment_status=='cancelled'


async def test_permission_revocation_blocks_recovery_and_receipt_worker_cannot_redispatch(database,checkout_dependencies):
    _,order,attempt=await prepare(database)
    issued=await transition(database,attempt,'success')
    async with database() as session:
        user=await session.get(AdminUser,1)
        with pytest.raises(HTTPException) as error:
            await fiscal.command(session,user,issued.fiscal_document_id,FiscalProcessCommand(expected_revision=1),str(uuid4()),'process')
        assert error.value.detail['code']=='qr_owns_receipt'
        user.is_superuser=False
        user.can_record_sales=True
        user.can_accept_qr_payments=False
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await transition(database,issued,op='refresh')
    assert error.value.status_code==403


async def test_partial_return_refund_restock_and_reports_keep_original_qr_link(database,checkout_dependencies):
    product,order,attempt=await prepare(database,quantity=2)
    paid=await transition(database,attempt,'success')
    async with database() as session:
        user=await session.get(AdminUser,1)
        current=await local_sales.load(session,order.id)
        completed=await local_sales.command(session,user,order.id,OrderHandoverCreate(expected_revision=current.revision,evidence='synthetic handover'),str(uuid4()),handover=True)
        item_id=completed.items[0].id
        returned=await fiscal_returns.create_return(session,user,order.id,ReturnCreate(parent_sale_id=paid.fiscal_document_id,items=[{'order_item_id':item_id,'quantity':1}],reason='synthetic partial return'),str(uuid4()))
        assert returned.amount_minor==10000
        refund=await fiscal_returns.record_refund(session,user,returned.id,MockRefundCreate(evidence='synthetic original invoice refund'),str(uuid4()))
        assert refund.refund.method=='monobank_qr' and refund.refund.qr_attempt_id==paid.id
        received=await fiscal_returns.record_receipt(session,user,returned.id,ReturnReceiptCreate(evidence='synthetic physical return',items=[{'order_item_id':item_id,'quantity':1,'disposition':'restock'}]),str(uuid4()))
        stored=await session.get(Product,product.id)
        await session.refresh(stored)
        assert (stored.stock_quantity,stored.reserved_quantity)==(4,0)
        assert await session.scalar(select(func.count()).select_from(MockRefund))==1
        assert received.receipt.items[0].quantity==1
        preview=await fiscal_returns.create_preview(session,user,returned.id,FiscalPreviewCreate(cash_register_id=fiscal.REGISTER),str(uuid4()))
        submitted=await fiscal.submit(session,user,order.id,FiscalSubmissionCreate(preview_id=preview.id,expected_revision=preview.revision,
            snapshot_hash=preview.snapshot_hash,cash_register_id=fiscal.REGISTER,confirm=True),str(uuid4()))
        await fiscal.command(session,user,submitted.id,FiscalProcessCommand(expected_revision=submitted.revision),str(uuid4()),'process')
        issued=await fiscal.get_document(session,user,submitted.id)
        assert issued.kind=='return' and issued.status=='issued' and issued.parent_sale_id==paid.fiscal_document_id
        report=await fiscal_returns.report(session,user,fiscal.REGISTER,MockReportCreate(kind='x',expected_shift_revision=1,confirm=True),str(uuid4()))
        assert (report.totals.sale_count,report.totals.return_count,report.totals.sales_minor,report.totals.returns_minor,report.totals.net_minor)==(1,1,20000,10000,10000)
        assert set(report.totals.document_ids)=={paid.fiscal_document_id,issued.id}



async def test_cancel_success_race_and_delayed_event_replay_cannot_recharge(database,checkout_dependencies):
    _,order,attempt=await prepare(database)
    async def race(outcome=None,op='simulate'):
        try:
            return await transition(database,attempt,outcome,op=op)
        except HTTPException as error:
            assert error.detail['code']=='revision_conflict'
    await asyncio.gather(race('success'),race(op='cancel'))
    async with database() as session:
        result=await qr_payment.get(session,await session.get(AdminUser,1),attempt.id)
        assert result.payment_status in ('paid','cancelled')
        count=await session.scalar(select(func.count()).select_from(OrderPayment))
        assert count==(1 if result.payment_status=='paid' else 0)
    late=await transition(database,result,'timeout_after_acceptance')
    assert late.payment_status==result.payment_status
    assert late.provider_invoice_id==attempt.provider_invoice_id


async def test_api_auth_qr_only_role_exact_event_replay_and_receipt_print(database,checkout_dependencies):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from app.api.v1.routes.qr_payment import backoffice_router
    from app.core.database import get_db_session
    from app.services.auth import AuthService
    _,order,attempt=await prepare(database)
    app=FastAPI()
    app.include_router(backoffice_router,prefix='/api/v1/backoffice')
    async def db():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session]=db
    async with database() as session:
        user=await session.get(AdminUser,1)
        user.is_superuser=False
        user.can_record_sales=True
        user.can_accept_qr_payments=True
        await session.commit()
        token=AuthService().issue_token_pair(user).access_token
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://local.test') as client:
        url=f'/api/v1/backoffice/qr-payment-attempts/{attempt.id}'
        denied=await client.get(url)
        assert denied.status_code in (401,403)
        client.headers['Authorization']='Bearer '+token
        readiness=await client.get('/api/v1/backoffice/integrations/monobank-qr/readiness')
        assert readiness.status_code==200 and readiness.json()['available'] is True
        payload={'expected_revision':attempt.revision,'outcome':'success','event_id':str(uuid4())}
        key=str(uuid4())
        response=await client.post(url+'/simulate',json=payload,headers={'Idempotency-Key':key})
        assert response.status_code==200,response.text
        paid=response.json()
        replay=await client.post(url+'/simulate',json=payload,headers={'Idempotency-Key':key})
        assert replay.status_code==200 and replay.json()==paid
        conflict=await client.post(url+'/simulate',json={**payload,'outcome':'decline'},headers={'Idempotency-Key':key})
        assert conflict.status_code==409 and conflict.json()['detail']['code']=='idempotency_conflict'
        duplicate=await client.post(url+'/simulate',json={**payload,'expected_revision':paid['revision']},headers={'Idempotency-Key':str(uuid4())})
        assert duplicate.status_code==200 and duplicate.json()['payment_id']==paid['payment_id']
        receipt=await client.get(paid['receipt_url'])
        assert receipt.status_code==200 and 'MOCK' in receipt.text
        assert receipt.headers['cache-control']=='private, no-store'
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(QREvent))==1
        assert await session.scalar(select(func.count()).select_from(OrderPayment))==1
