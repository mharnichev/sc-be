"""Safe operational views on synthetic fixtures; PostgreSQL enforces read-only."""
import json
import asyncio
import ast
from contextlib import suppress
from pathlib import Path
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI,HTTPException
from sqlalchemy import select,text,func
from app.api.v1.routes import shop_operations_watch as routes
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.order import Order,OrderStatus
from app.models.shipment import Shipment
from app.models.shop_payments import ShopPaymentIntent
from app.models.shop_payment_resolutions import ShopPaymentResolutionCase,ShopPaymentBankClosure
from app.models.sms_queue import SmsQueueJob
from app.schemas.shop_operations_watch import ShopOperationsWatch
from app.services.shop_operations_watch import snapshot
from tests.test_inventory_integration import anyio_backend,database,checkout_dependencies
pytestmark=pytest.mark.anyio

async def seed(database,*,test_profile=False,manual_test=False,shipment_test=False):
    old=datetime.now(UTC)-timedelta(hours=2)
    async with database() as s:
        o=Order(customer_name='PRIVATE CUSTOMER',customer_phone='+380999999999',comment='PRIVATE BODY',total_amount=Decimal('100'),status=OrderStatus.pending,channel='shop',simulation=False,created_at=old)
        s.add(o);await s.flush()
        if shipment_test:
            s.add(Shipment(order_id=o.id,simulation=True,carrier='mock_nova_poshta',source='mock',active=True,operation_uuid=str(uuid4()),provider_reference=str(uuid4()),status='created',raw_status='1',progress=0,address_snapshot={},packaging={},created_at=old))
        if test_profile or manual_test:
            intent=ShopPaymentIntent(id=str(uuid4()),order_id=o.id,mode='prepayment',reference=str(uuid4()),amount_minor=10000,profile_id='synthetic',profile={'environment':'test' if test_profile else 'production'},profile_hash='a'*64,authority='manual',transport='manual',operator_id=1,created_at=old)
            s.add(intent);await s.flush()
            if manual_test:
                case=ShopPaymentResolutionCase(order_id=o.id,kind='invoice_review',intent_id=intent.id,profile_id='synthetic',profile=intent.profile,profile_hash=intent.profile_hash,protocol='manual_review',test_context=False,revision=1,snapshot={},snapshot_hash='b'*64,reason='PRIVATE REASON',operator_id=1,created_at=old)
                s.add(case);await s.flush();s.add(ShopPaymentBankClosure(case_id=case.id,policy={'environment':'test'},policy_hash='c'*64,identity='d'*64,snapshot={'document':'PRIVATE DOCUMENT'},snapshot_hash='e'*64,operator_id=1,created_at=old))
        s.add(SmsQueueJob(account_key=settings.sms_club_account_key,idempotency_key=str(uuid4()),operation='send',priority=0,payload={'message':'PRIVATE OTP 123456','phone':['380999999999']},context_json={},status='uncertain',available_at=old,expires_at=old+timedelta(hours=3),error_detail='PRIVATE TOKEN',created_at=old))
        s.add(SmsQueueJob(account_key='OTHER-ACCOUNT',idempotency_key=str(uuid4()),operation='send',priority=0,payload={},context_json={},status='uncertain',available_at=old,created_at=old))
        await s.commit();return o.id

@pytest.mark.parametrize('test_profile,manual_test,authority',[(False,False,'unverified'),(True,False,'test'),(False,True,'test')])
async def test_readonly_snapshot_privacy_and_durable_test_authority(database,test_profile,manual_test,authority):
    oid=await seed(database,test_profile=test_profile,manual_test=manual_test)
    async with database() as s:
        await s.execute(text('SET TRANSACTION READ ONLY'))
        result=ShopOperationsWatch.model_validate(await snapshot(s,SimpleNamespace(id=1),process_state={'role':'owner','state':'running','last_error_code':'PRIVATE TOKEN','profile':'PRIVATE PROFILE'})).model_dump(mode='json')
        assert result['automatic_actions'] is False and result['counts']['pending_order']==1 and result['counts']['sms_uncertain']==1
        assert next(x for x in result['issues'] if x['order_id']==oid)['authority']==authority
        assert result['background_process']['last_error_code'] is None
        serialized=json.dumps(result)
        for secret in ['PRIVATE','123456','380999999999','OTHER-ACCOUNT']:assert secret not in serialized
        assert await s.scalar(select(func.count()).select_from(SmsQueueJob))==2

@pytest.mark.parametrize('rights',[{}, {'can_record_sales':True},{'can_manage_fiscal_settings':True},{'is_active':False,'is_superuser':True}])
async def test_watch_requires_current_oversight_permissions(database,rights):
    async with database() as s:
        actor=AdminUser(id=2,email='watch@example.test',hashed_password='unused',is_active=True,is_superuser=False)
        for k,v in rights.items():setattr(actor,k,v)
        s.add(actor);await s.commit()
        with pytest.raises(HTTPException) as e:await snapshot(s,SimpleNamespace(id=actor.id,is_superuser=True))
        assert e.value.status_code==403

async def test_readonly_endpoint_is_bounded_and_post_is_absent(database):
    await seed(database)
    app=FastAPI();app.include_router(routes.backoffice_router)
    async def db():
        async with database() as s:
            await s.execute(text('SET TRANSACTION READ ONLY'));yield s
    app.dependency_overrides[get_db_session]=db
    app.dependency_overrides[get_current_admin_user]=lambda:SimpleNamespace(id=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        result=await client.get('/shop-operations-watch',params={'limit':1})
        assert result.status_code==200 and result.json()['truncated'] is True and len(result.json()['issues'])==1
        assert (await client.get('/shop-operations-watch',params={'limit':101})).status_code==422
        assert (await client.post('/shop-operations-watch')).status_code==405


async def test_master_with_oversight_flags_cannot_inspect_others(database):
    async with database() as s:
        actor=AdminUser(id=2,email='master-watch@example.test',hashed_password='unused',is_active=True,is_superuser=False,can_record_sales=True,can_manage_fiscal_settings=True)
        s.add(actor);await s.flush();s.add(Master(admin_user_id=actor.id,full_name='Synthetic master'));await s.commit()
        with pytest.raises(HTTPException) as e:await snapshot(s,SimpleNamespace(id=actor.id))
        assert e.value.status_code==403


def future_watch_clock(monkeypatch):
    from app.services import shop_operations_watch as service
    class Future(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.now(UTC)+timedelta(hours=2)
    monkeypatch.setattr(service,'datetime',Future)

async def reasons(database):
    async with database() as session:
        await session.execute(text('SET TRANSACTION READ ONLY'))
        return (await snapshot(session,SimpleNamespace(id=1)))['counts']

async def test_rejected_sale_does_not_hide_unreceipted_payment(database,monkeypatch,checkout_dependencies):
    from tests import test_shop_checkbox_integration as cb
    stub=cb.flags.__wrapped__(monkeypatch)
    oid,doc=await cb.queued_sale(database)
    tax=stub.profile['taxes']['1']['rate'];stub.profile['taxes']['1']['rate']='7'
    await cb.call(database,cb.service.operation_command,doc.operation.id,cb.dto.RevisionCommand(expected_revision=doc.revision),str(uuid4()),'process')
    assert (await cb.call(database,cb.service.read,'operation',doc.operation.id)).status=='rejected'
    future_watch_clock(monkeypatch)
    assert (await reasons(database))['payment_without_sale']==1
    stub.profile['taxes']['1']['rate']=tax
    await cb.sale(database,oid)
    assert (await reasons(database)).get('payment_without_sale',0)==0

async def test_rejected_np_return_does_not_hide_financial_obligation(database,monkeypatch,checkout_dependencies):
    from tests import test_shop_np_returns_integration as np
    from tests import test_shop_checkbox_integration as cb
    flags=np.flags.__wrapped__(monkeypatch)
    data,case,_=await np.attach_case(database,flags,selected=1)
    case,_=await np.receive(database,case)
    case=await cb.call(database,np.service.command,case.id,np.dto.ClaimCommand(expected_revision=case.revision,**np.assertion(),reason='Synthetic refund'),str(uuid4()),'financial_claim')
    ret=await cb.call(database,cb.service.read,'return',case.financial_return_id)
    ret=await cb.call(database,cb.service.return_command,ret.id,cb.dto.RefundCommand(expected_revision=ret.revision,external_reference=uuid4().hex,evidence='Synthetic'),str(uuid4()),'refund')
    async def prepare():
        current=await cb.call(database,cb.service.read,'return',ret.id)
        preview=await cb.call(database,cb.service.order_command,data['order_id'],cb.dto.RevisionCommand(expected_revision=current.revision),str(uuid4()),'preview',return_id=ret.id)
        return await cb.call(database,cb.service.order_command,data['order_id'],cb.dto.ApproveCommand(expected_revision=current.revision,preview_id=preview.id,confirm=True),str(uuid4()),'approve',return_id=ret.id)
    doc=await prepare();tax=flags[1].profile['taxes']['1']['rate'];flags[1].profile['taxes']['1']['rate']='7'
    await cb.call(database,cb.service.operation_command,doc.operation.id,cb.dto.RevisionCommand(expected_revision=doc.revision),str(uuid4()),'process')
    assert (await cb.call(database,cb.service.read,'operation',doc.operation.id)).status=='rejected'
    future_watch_clock(monkeypatch)
    assert (await reasons(database))['np_return_pending']==1
    flags[1].profile['taxes']['1']['rate']=tax
    doc=await prepare();await cb.process(database,doc.operation)
    assert (await reasons(database)).get('np_return_pending',0)==0


async def test_mock_shipment_keeps_non_simulated_order_in_test_context(database):
    oid=await seed(database,shipment_test=True)
    async with database() as s:
        await s.execute(text('SET TRANSACTION READ ONLY'))
        result=await snapshot(s,SimpleNamespace(id=1))
        assert all(row['authority']=='test' for row in result['issues'] if row['order_id']==oid)


def test_scheduler_projection_allowlist_covers_registered_lifespan_labels():
    from app.services.shop_operations_watch import WATCH_SCHEDULER_NAMES
    tree=ast.parse((Path(__file__).resolve().parents[1]/'app/main.py').read_text())
    names={node.elts[0].value for node in ast.walk(tree)
           if isinstance(node,ast.Tuple) and len(node.elts)==2
           and isinstance(node.elts[0],ast.Constant) and isinstance(node.elts[0].value,str)
           and isinstance(node.elts[1],ast.Name) and node.elts[1].id.startswith('run_')}
    assert names==WATCH_SCHEDULER_NAMES


@pytest.mark.parametrize('failed',[False,True])
async def test_watch_serializes_real_worker_names_even_after_failure_without_private_state(database,failed):
    from app.core.background_workers import BackgroundWorkers
    gate=asyncio.Event()
    async def inert():
        await gate.wait()
        raise RuntimeError('PRIVATE TOKEN exception')
    names=['sms-queue-worker','booking-auto-completion-scheduler']
    workers=BackgroundWorkers([(name,inert) for name in names],app_env='test',heartbeat_seconds=.01)
    await workers.start()
    try:
        if failed:
            gate.set()
            async with asyncio.timeout(2):
                while workers.state['state']!='failed':await asyncio.sleep(.001)
        app=FastAPI();app.include_router(routes.backoffice_router)
        app.state.background_workers={**workers.state,'profile':'PRIVATE PROFILE',
            'scheduler_names':[*workers.state['scheduler_names'],'PRIVATE TOKEN',{'secret':'PRIVATE'},None,names[0]]}
        async def db():
            async with database() as s:
                await s.execute(text('SET TRANSACTION READ ONLY'));yield s
        app.dependency_overrides[get_db_session]=db
        app.dependency_overrides[get_current_admin_user]=lambda:SimpleNamespace(id=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
            response=await client.get('/shop-operations-watch')
        assert response.status_code==200,response.text
        process=response.json()['background_process']
        assert process['scheduler_names']==names
        assert process['state']==('failed' if failed else 'running')
        assert process['last_error_code']==('worker_failed' if failed else None)
        assert 'PRIVATE' not in response.text
        app.dependency_overrides.pop(get_current_admin_user)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
            assert (await client.get('/shop-operations-watch')).status_code==401
    finally:
        with suppress(RuntimeError):await workers.stop()


@pytest.mark.parametrize('names',[None,'PRIVATE TOKEN',{'secret':'PRIVATE'},[None,{'secret':'PRIVATE'},'unknown-task']])
async def test_watch_scheduler_names_fail_closed_for_malformed_state(database,names):
    async with database() as session:
        await session.execute(text('SET TRANSACTION READ ONLY'))
        result=ShopOperationsWatch.model_validate(await snapshot(session,SimpleNamespace(id=1),
            process_state={'role':'owner','state':'failed','scheduler_names':names})).model_dump(mode='json')
        assert result['background_process']['scheduler_names']==[]
        assert 'PRIVATE' not in json.dumps(result)
