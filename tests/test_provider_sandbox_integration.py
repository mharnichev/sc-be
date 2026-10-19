"""Remote sandbox bridge against actual Alembic history and fake provider I/O."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import OrderPayment, OrderHandover
from app.models.product import Product
from app.models.fiscal import FiscalDocument, MockCashRegister, MockFiscalReport
from app.models.sandbox import SandboxRun
from app.schemas.order import POSOrderCreate, OrderHandoverCreate
from app.schemas.qr_payment import QRCreate, QRCommandCreate, QRSimulate
from app.schemas.fiscal import MockShiftCommand
from app.schemas.fiscal_returns import ReturnCreate, MockReportCreate
from app.services import fiscal, fiscal_returns, local_sales, qr_payment, sandbox
from app.services.sandbox_providers import SandboxProviderError
from tests.test_inventory_integration import anyio_backend, checkout_dependencies, add_product

pytestmark = pytest.mark.anyio
REGISTER_ID='11111111-1111-4111-8111-111111111111'
CASHIER_ID='22222222-2222-4222-8222-222222222222'
ORGANIZATION_ID='33333333-3333-4333-8333-333333333333'
SHIFT_ID='44444444-4444-4444-8444-444444444444'


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    for name in ('provider_sandbox_enabled', 'monobank_qr_mock_enabled', 'checkbox_mock_enabled', 'local_sales_simulation_enabled'):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, 'app_env', 'test')


@pytest.fixture
async def database():
    url = os.environ.get('INVENTORY_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Isolated PostgreSQL required')
    parsed = make_url(url)
    assert parsed.host in ('localhost','127.0.0.1','::1') and 'test' in parsed.database
    database_name = 'sc_sandbox_test_' + uuid4().hex
    admin = create_async_engine(url, isolation_level='AUTOCOMMIT')
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}" TEMPLATE template0'))
    isolated_url = parsed.set(database=database_name).render_as_string(hide_password=False)
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, DATABASE_URL=isolated_url, APP_ENV='test')
    engine = None
    try:
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable,'-m','alembic','-c',str(root/'alembic.ini'),'upgrade','head'],
            cwd=root,env=env,capture_output=True,text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        engine = create_async_engine(isolated_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            session.add(AdminUser(id=1,email='sandbox@example.test',hashed_password='unused',is_active=True,is_superuser=True))
            await session.commit()
        yield sessions
    finally:
        if engine:
            await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        await admin.dispose()



class Provider:
    def __init__(self):
        self.invoice_posts = self.receipt_posts = 0
        self.invoice_id = 'mono-external-NON-UUID-123'
        self.reference = None
        self.fingerprint = 'a'*64
        self.receipt = None
        self.invoice_unknown = self.receipt_unknown = False
        self.status = 'paid'
        self.available = True
        self.shift_open = True
        self.current_shift_id = SHIFT_ID
        self.time = datetime.now(UTC)

    async def readiness(self, *, require_open_shift=True):
        return {'available':self.available and (self.shift_open or not require_open_shift),'blocking_reasons':[] if self.available else ['not_test'],
            'verification':{'monobank_provider_confirmed':False,'monobank_token_sha256':self.fingerprint,'monobank_policy':'operator_attested_test_token_with_pinned_sha256','checkbox_provider_confirmed':True,'shift_id':SHIFT_ID},
            'checkbox_cash_register_id':REGISTER_ID,'checkbox_cashier_id':CASHIER_ID,'checkbox_organization_id':ORGANIZATION_ID}

    async def create_invoice(self, *, reference, amount_minor, expires_seconds):
        self.invoice_posts += 1
        self.reference = reference
        if self.invoice_unknown:
            raise SandboxProviderError('invoice_create_unknown')
        return {'invoice_id':self.invoice_id,'payment_url':'https://pay.mbnk.biz/test','status':'waiting'}

    async def get_invoice(self, invoice_id):
        assert invoice_id == self.invoice_id
        return {'invoice_id':invoice_id,'reference':self.reference,'status':self.status,
            'amount_minor':10000,'currency':'UAH','accepted_at':self.time if self.status=='paid' else None}

    async def ensure_test_shift(self):
        if not self.shift_open:
            raise SandboxProviderError('checkbox_test_shift_required')
        return {'shift_id':self.current_shift_id}

    async def get_receipt(self, receipt_uuid):
        return self.receipt

    async def create_receipt(self, receipt_uuid, *, snapshot, invoice_id):
        self.receipt_posts += 1
        self.receipt = {'receipt_uuid':receipt_uuid,'status':'issued','is_test':True,'is_sent_dps':False,'amount_minor':10000,'currency':'UAH','cash_register_id':REGISTER_ID,'cashier_id':CASHIER_ID,'organization_id':ORGANIZATION_ID,'shift_id':SHIFT_ID}
        if self.receipt_unknown:
            raise SandboxProviderError('receipt_create_unknown')
        return self.receipt


async def prepare(database, provider):
    product = await add_product(database, stock=5)
    key = str(uuid4())
    async with database() as session:
        actor = await session.get(AdminUser,1)
        order = await local_sales.prepare(session,actor,POSOrderCreate(items=[{'product_id':product.id,'quantity':1}],simulation=True),str(uuid4()))
        await fiscal.shift(session,actor,fiscal.REGISTER,MockShiftCommand(expected_shift_revision=0),str(uuid4()),opening=True)
        payload = QRCreate(expected_revision=order.revision,cash_register_id=fiscal.REGISTER,confirm=True)
        attempt = await sandbox.create(session,actor,order.id,payload,key,provider=provider)
        return product,order,attempt,key,payload


async def refresh(database, attempt, provider, *, key=None):
    async with database() as session:
        return await sandbox.refresh(session,await session.get(AdminUser,1),attempt.id,
            QRCommandCreate(expected_revision=attempt.revision),key or str(uuid4()),provider=provider)


async def test_actual_migrations_paid_receipt_stock_report_once(database, checkout_dependencies):
    provider = Provider()
    product, order, attempt, key, payload = await prepare(database,provider)
    assert attempt.mode == 'provider_sandbox' and attempt.local_payment_path == ''
    assert attempt.provider_invoice_id != provider.invoice_id
    async with database() as session:
        replay = await sandbox.create(session,await session.get(AdminUser,1),order.id,payload,key,provider=provider)
        assert replay.id == attempt.id and provider.invoice_posts == 1
    result = await refresh(database,attempt,provider)
    assert result.payment_status == 'paid' and result.receipt_status == 'issued'
    assert provider.invoice_posts == provider.receipt_posts == 1
    again = await refresh(database,result,provider)
    assert again.fiscal_document_id == result.fiscal_document_id and provider.receipt_posts == 1
    async with database() as session:
        actor = await session.get(AdminUser,1)
        current = await local_sales.load(session,order.id)
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 1
        await local_sales.command(session,actor,order.id,OrderHandoverCreate(expected_revision=current.revision,evidence='sandbox'),str(uuid4()),handover=True)
        item = await session.get(Product,product.id)
        assert (item.stock_quantity,item.reserved_quantity) == (4,0)
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
        report = await fiscal_returns.report(session,actor,fiscal.REGISTER,MockReportCreate(kind='x',expected_shift_revision=1,confirm=True),str(uuid4()))
        assert report.totals.sale_count == 1 and report.totals.sales_minor == 10000
        document = await session.get(FiscalDocument,result.fiscal_document_id)
        run = await sandbox.run_for(session,result.id)
        assert run.invoice_id == provider.invoice_id
        html = sandbox.receipt_summary(document,run)
        assert run.receipt_uuid in html and provider.invoice_id in html
        with pytest.raises(HTTPException) as error:
            await fiscal_returns.create_return(session,actor,order.id,ReturnCreate(parent_sale_id=document.id,
                items=[{'order_item_id':current.items[0].id,'quantity':1}],reason='unsupported'),str(uuid4()))
        assert error.value.detail['code']=='sandbox_return_unsupported'
        z_payload = MockReportCreate(kind='z', expected_shift_revision=1, confirm=True)
        z_key = str(uuid4())
        z_report = await fiscal_returns.report(session, actor, fiscal.REGISTER, z_payload, z_key)
        assert z_report.totals == report.totals and z_report.kind == 'z'
        register = await session.get(MockCashRegister, fiscal.REGISTER)
        assert (register.shift_status, register.shift_revision) == ('closed', 2)
        replay_z = await fiscal_returns.report(session, actor, fiscal.REGISTER, z_payload, z_key)
        assert replay_z.id == z_report.id
        assert await session.scalar(select(func.count()).select_from(MockFiscalReport)) == 2
        assert provider.invoice_posts == provider.receipt_posts == 1


async def test_ambiguous_invoice_create_never_posts_twice_or_pays(database,checkout_dependencies):
    provider=Provider();provider.invoice_unknown=True
    _,order,attempt,key,payload=await prepare(database,provider)
    assert attempt.payment_status=='unknown' and not attempt.payment_url
    async with database() as session:
        replay=await sandbox.create(session,await session.get(AdminUser,1),order.id,payload,key,provider=provider)
        assert replay.id==attempt.id
    recovered=await refresh(database,attempt,provider)
    assert recovered.payment_status=='unknown' and recovered.payment_id is None
    assert provider.invoice_posts==1 and provider.receipt_posts==0
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        for kind in ('x', 'z'):
            with pytest.raises(HTTPException) as error:
                await fiscal_returns.report(session, actor, fiscal.REGISTER,
                    MockReportCreate(kind=kind, expected_shift_revision=1, confirm=True), str(uuid4()))
            assert error.value.detail['code'] == 'shift_unsettled_qr'
            await session.rollback()
            actor = await session.get(AdminUser, 1)
        register = await session.get(MockCashRegister, fiscal.REGISTER)
        assert (register.shift_status, register.shift_revision) == ('open', 1)
        assert await session.scalar(select(func.count()).select_from(MockFiscalReport)) == 0
    async with database() as session:
        actor=await session.get(AdminUser,1)
        with pytest.raises(HTTPException) as error:
            await qr_payment.command(session,actor,attempt.id,QRSimulate(expected_revision=recovered.revision,event_id=uuid4(),outcome='success'),str(uuid4()),'simulate')
        assert error.value.detail['code']=='sandbox_command_forbidden'
        await session.rollback()
        with pytest.raises(Exception,match='Local simulation cannot mutate'):
            await session.execute(text("INSERT INTO qr_payment_events(event_id,attempt_id,actor_id,outcome,created_at) VALUES (:event,:attempt,1,'success',now())"),{'event':str(uuid4()),'attempt':attempt.id})
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(OrderPayment))==0


async def test_receipt_timeout_recovers_same_uuid_without_second_payment(database,checkout_dependencies):
    provider=Provider();provider.receipt_unknown=True
    _,_,attempt,_,_=await prepare(database,provider)
    unknown=await refresh(database,attempt,provider)
    assert unknown.payment_status=='paid' and unknown.receipt_status=='pending'
    provider.receipt_unknown=False
    result=await refresh(database,unknown,provider)
    assert result.receipt_status=='issued' and result.checkbox_receipt_id==unknown.checkbox_receipt_id
    assert provider.invoice_posts==provider.receipt_posts==1


async def test_unverified_provider_blocked_before_invoice_post(database,checkout_dependencies):
    provider=Provider();provider.available=False
    with pytest.raises(HTTPException) as error:
        await prepare(database,provider)
    assert error.value.detail['code']=='sandbox_not_verified'
    assert provider.invoice_posts==0


async def test_unknown_provider_status_blocks_payment_and_handover(database,checkout_dependencies):
    provider=Provider();provider.status='unknown'
    _,order,attempt,_,_=await prepare(database,provider)
    result=await refresh(database,attempt,provider)
    assert result.payment_status=='unknown' and result.payment_id is None
    assert result.blocking_reasons==['sandbox_invoice_status_unknown']
    async with database() as session:
        actor=await session.get(AdminUser,1)
        current=await local_sales.load(session,order.id)
        with pytest.raises(HTTPException):
            await local_sales.command(session,actor,order.id,OrderHandoverCreate(expected_revision=current.revision,evidence='blocked'),str(uuid4()),handover=True)
    assert provider.receipt_posts==0


async def test_concurrent_refresh_only_one_remote_receipt_and_canonical_document(database,checkout_dependencies):
    provider=Provider()
    _,_,attempt,_,_=await prepare(database,provider)
    results=await asyncio.gather(refresh(database,attempt,provider),refresh(database,attempt,provider))
    assert all(item.payment_status=='paid' for item in results)
    assert provider.invoice_posts==provider.receipt_posts==1
    async with database() as session:
        row=await qr_payment.get(session,await session.get(AdminUser,1),attempt.id)
        assert row.receipt_status=='issued'
        assert await session.scalar(select(func.count()).select_from(OrderPayment))==1
        assert await session.scalar(select(func.count()).select_from(FiscalDocument))==1


async def test_remote_evidence_bool_strings_and_mismatched_identity_rejected_by_database(database,checkout_dependencies):
    provider=Provider()
    _,_,attempt,_,_=await prepare(database,provider)
    async with database() as session:
        # A local simulator row cannot claim bank acceptance without remote run evidence.
        with pytest.raises(Exception,match='Local provider projection must match'):
            await session.execute(text("UPDATE mock_qr_provider_records SET payment_status='paid',accepted_at=now() WHERE attempt_id=:id"),{'id':attempt.id})
        await session.rollback()
        with pytest.raises(Exception,match='honest monobank provenance'):
            await session.execute(text("UPDATE provider_sandbox_runs SET verification=json_build_object('checkbox_provider_confirmed','true','monobank_provider_confirmed',false) WHERE attempt_id=:id"),{'id':attempt.id})
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(OrderPayment))==0


async def test_same_uuid_wrong_remote_shift_fails_before_canonical_issue(database,checkout_dependencies):
    provider=Provider();provider.receipt_unknown=True
    _,_,attempt,_,_=await prepare(database,provider)
    result=await refresh(database,attempt,provider)
    provider.receipt['shift_id']='different-test-shift'
    with pytest.raises(HTTPException) as error:
        await refresh(database,result,provider)
    assert error.value.detail['code']=='sandbox_receipt_mismatch'
    assert provider.receipt_posts==1


async def test_populated_downgrade_refused_before_any_ddl(database,checkout_dependencies):
    provider=Provider()
    _,_,attempt,_,_=await prepare(database,provider)
    async with database() as session:
        url=session.get_bind().url.render_as_string(hide_password=False)
        version_before = await session.scalar(text('SELECT version_num FROM alembic_version'))
    root=Path(__file__).resolve().parents[1]
    result=await asyncio.to_thread(subprocess.run,
        [sys.executable,'-m','alembic','-c',str(root/'alembic.ini'),'downgrade','0119_mock_monobank_qr'],
        cwd=root,env=dict(os.environ,DATABASE_URL=url,APP_ENV='test'),capture_output=True,text=True)
    assert result.returncode!=0 and 'Provider sandbox history exists; downgrade refused' in result.stderr
    async with database() as session:
        assert await session.scalar(text('SELECT version_num FROM alembic_version')) == version_before
        assert (await session.scalar(select(SandboxRun).where(SandboxRun.attempt_id==attempt.id))).invoice_id==provider.invoice_id


async def test_provider_offset_timestamp_remains_valid_after_reload(database,checkout_dependencies):
    from datetime import timedelta, timezone
    provider=Provider();provider.time=datetime.now(timezone(timedelta(hours=3)))
    _,_,attempt,_,_=await prepare(database,provider)
    result=await refresh(database,attempt,provider)
    async with database() as session:
        document=await session.get(FiscalDocument,result.fiscal_document_id)
        await fiscal.validate_issued_document(session,document)
        run=await sandbox.run_for(session,attempt.id)
        assert run.payment_evidence['accepted_at'].endswith('+00:00')


async def test_revocation_after_durable_create_fence_prevents_invoice_post(database,checkout_dependencies,monkeypatch):
    provider=Provider()
    original=sandbox.require
    calls=0
    async def revoke_before_second_check(session,actor):
        nonlocal calls
        calls+=1
        if calls==2:
            async with database() as other:
                await other.execute(text('UPDATE admin_users SET is_active=false WHERE id=1'))
                await other.commit()
        return await original(session,actor)
    monkeypatch.setattr(sandbox,'require',revoke_before_second_check)
    with pytest.raises(HTTPException) as error:
        await prepare(database,provider)
    assert error.value.status_code==403 and provider.invoice_posts==0
    async with database() as session:
        run=await session.scalar(select(SandboxRun))
        assert run.invoice_dispatched and run.invoice_id is None
        assert run.last_error=='sandbox_dispatch_authorization_revoked'
        assert await session.scalar(select(func.count()).select_from(OrderPayment))==0


async def test_lost_receipt_recovers_after_original_shift_closed(database,checkout_dependencies):
    provider=Provider();provider.receipt_unknown=True
    _,_,attempt,_,_=await prepare(database,provider)
    unknown=await refresh(database,attempt,provider)
    provider.shift_open=False
    result=await refresh(database,unknown,provider)
    assert result.receipt_status=='issued'
    assert provider.receipt_posts==1


async def test_rotated_token_provenance_cannot_recover_or_write_original_run(database,checkout_dependencies):
    provider=Provider()
    _,_,attempt,_,_=await prepare(database,provider)
    provider.fingerprint='b'*64
    with pytest.raises(HTTPException) as error:
        await refresh(database,attempt,provider)
    assert error.value.detail['code']=='sandbox_credentials_changed'
    assert provider.receipt_posts==0


async def test_changed_open_shift_fails_before_receipt_dispatch_fence(database,checkout_dependencies):
    provider=Provider()
    _,_,attempt,_,_=await prepare(database,provider)
    provider.current_shift_id='replacement-open-shift'
    with pytest.raises(HTTPException) as error:
        await refresh(database,attempt,provider)
    assert error.value.detail['code']=='sandbox_shift_changed'
    assert provider.receipt_posts==0
    async with database() as session:
        run=await sandbox.run_for(session,attempt.id)
        assert run.payment_status=='paid' and not run.receipt_dispatched


@pytest.mark.parametrize('missing', ['checkbox_cash_register_id','checkbox_cashier_id','checkbox_organization_id','shift_id'])
async def test_missing_identity_pins_rejected_by_actual_database(database,checkout_dependencies,missing):
    provider=Provider()
    original=provider.readiness
    async def invalid_proof(**kwargs):
        proof=await original(**kwargs)
        if missing=='shift_id':
            proof['verification']['shift_id']=None
        else:
            proof[missing]=None
        return proof
    provider.readiness=invalid_proof
    with pytest.raises(Exception,match='QR invoice requires unpaid simulated POS consent'):
        await prepare(database,provider)
    assert provider.invoice_posts==0


async def test_invalid_payment_link_preserves_invoice_for_status_recovery(database, checkout_dependencies):
    class OpaqueURLFailure(Provider):
        async def create_invoice(self, **kwargs):
            self.invoice_posts += 1
            self.reference = kwargs['reference']
            raise SandboxProviderError('invoice_create_unknown', invoice_id=self.invoice_id)
    provider = OpaqueURLFailure()
    _, order, attempt, key, payload = await prepare(database, provider)
    assert attempt.payment_status == 'unknown'
    async with database() as session:
        run = await session.get(SandboxRun, attempt.id)
        assert run.invoice_id == provider.invoice_id and run.payment_url is None
        replay = await sandbox.create(session, await session.get(AdminUser, 1), order.id, payload, key, provider=provider)
        assert replay.id == attempt.id
    result = await refresh(database, attempt, provider)
    assert result.payment_status == 'paid' and result.receipt_status == 'issued'
    assert provider.invoice_posts == provider.receipt_posts == 1


async def test_provider_confirmed_expiry_allows_empty_x_z_without_payment(database, checkout_dependencies):
    provider = Provider()
    provider.status = 'expired'
    _, _, attempt, _, _ = await prepare(database, provider)
    result = await refresh(database, attempt, provider)
    assert result.payment_status == 'expired'
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        for kind in ('x', 'z'):
            report = await fiscal_returns.report(session, actor, fiscal.REGISTER,
                MockReportCreate(kind=kind, expected_shift_revision=1, confirm=True), str(uuid4()))
            assert report.totals.sale_count == 0 and report.totals.sales_minor == 0
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        assert await session.scalar(select(func.count()).select_from(FiscalDocument)) == 0
        register = await session.get(MockCashRegister, fiscal.REGISTER)
        assert (register.shift_status, register.shift_revision) == ('closed', 2)
    assert provider.invoice_posts == 1 and provider.receipt_posts == 0
