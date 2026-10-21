"""Durable Shop dispatch and owner access against disposable migrated localhost PG.

All provider traffic uses httpx.MockTransport, including invoice/create.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_optional_current_customer
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shop_online_payment import ShopOnlinePaymentRequest
from app.models.shop_mono_checkbox import ShopMonoCheckboxReceipt
from app.schemas.shop_online_payment import OnlinePaymentRecovery
from app.services import shop_online_payment as service, shop_payments as payments, shop_sales as sales, mono_checkbox_poll as poll
from app.services.monobank_provider import MonobankGateway
from app.services.order import OrderService
from app.api.v1.routes.shop_online_payment import public_router
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_sales_integration import seed, execute
from app.schemas.shop_sales import ShopPhysicalCommand

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def online(monkeypatch):
    for name, value in dict(app_env="test", shop_order_operations_enabled=True, shop_payments_enabled=True,
        shop_mono_checkbox_enabled=True, shop_checkbox_enabled=False, shop_checkbox_profile=None,
        shop_fiscal_mock_enabled=False, shop_monobank_live_transport_enabled=False, shop_checkout_enabled=False,
        shop_online_payment_enabled=True, shop_online_payment_invoice_create_enabled=True,
        shop_online_payment_link_confirmed=True, shop_online_payment_operator_id=1,
        shop_online_payment_fiscal_policy={"id":"synthetic-shop-tax8","tax_codes":[8],"unit":"шт."},
        mono_checkbox_auto_enabled=True, master_mono_enabled=False).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(sales, "fiscal_policy", sales.UnavailableFiscalPolicy())
    token = "synthetic-online-only"
    profile = dict(id="synthetic-online", merchant_id="synthetic-merchant", environment="production",
        token_sha256=sha256(token.encode()).hexdigest(), external_fiscalization_disabled=False)
    monkeypatch.setattr(settings, "shop_monobank_token", SecretStr(token))
    monkeypatch.setattr(settings, "shop_monobank_profile", profile)
    state = dict(posts=0, calls=[], unknown=False, status="processing", checks=[], reference=None, amount=10000, readiness_hook=None, transport=None)
    def handler(request):
        state['calls'].append((request.method, request.url.path))
        if request.url.path.endswith('/details'):
            if state['readiness_hook']:
                state['readiness_hook']()
            return httpx.Response(200,json={"merchantId":profile['merchant_id']})
        if request.method == 'POST':
            assert request.url.path == '/api/merchant/invoice/create'
            body=json.loads(request.content)
            state.update(posts=state['posts']+1, reference=body['merchantPaymInfo']['reference'], amount=body['amount'])
            if state['unknown']:
                raise httpx.ReadTimeout('synthetic lost acknowledgement')
            return httpx.Response(200,json={"invoiceId":"syntheticOnlineInvoice", "pageUrl":"https://pay.mbnk.biz/syntheticOnlineInvoice"})
        if request.url.path.endswith('/fiscal-checks'):
            return httpx.Response(200,json={"checks":state['checks']})
        now=datetime.now(UTC).isoformat()
        return httpx.Response(200,json=dict(invoiceId=request.url.params['invoiceId'],reference=state['reference'],
            ccy=980,amount=state['amount'],finalAmount=state['amount'],status=state['status'],createdDate=now,modifiedDate=now))
    transport=httpx.MockTransport(handler)
    state['transport']=transport
    monkeypatch.setattr(service,'gateway',lambda:MonobankGateway(transport=transport,create_capability='shop'))
    monkeypatch.setattr(payments,'gateway',lambda:MonobankGateway(transport=transport))
    return state


async def prepared(database):
    identifier, product = await seed(database, method='onlineCard')
    async with database() as session:
        order=await session.get(Order,identifier)
        return identifier,product,order.guest_payment_token


async def start(database, identifier, token, key=None):
    async with database() as session:
        return await service.start(session, identifier, None, token, key or str(uuid4()))


async def test_concurrent_keys_restart_and_lost_response_never_repeat_post(database, checkout_dependencies, online):
    identifier,_,token=await prepared(database)
    online['unknown']=True
    key=str(uuid4())
    results=await asyncio.gather(*(start(database,identifier,token,key) for _ in range(2)))
    assert {r.status for r in results} == {'creation_unknown'}
    online['unknown']=False
    assert (await start(database,identifier,token)).status=='creation_unknown'
    assert online['posts']==1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest))==1
        with pytest.raises(HTTPException) as denied:
            await OrderService().update_status(session,identifier,OrderStatus.cancelled,admin_user_id=1)
        assert denied.value.status_code==409
    async with database() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text('UPDATE shop_online_payment_requests SET reference=reference'))
            await session.commit()


async def test_exact_recovery_uses_get_only(database, checkout_dependencies, online):
    identifier,_,token=await prepared(database)
    online['unknown']=True
    await start(database,identifier,token)
    async with database() as session:
        order=await session.get(Order,identifier)
        actor=await session.get(AdminUser,1)
        online['reference']='00000000-0000-4000-8000-000000000000'
        with pytest.raises(HTTPException):
            await service.recover(session,actor,identifier,OnlinePaymentRecovery(expected_revision=order.revision,invoice_id='syntheticOnlineInvoice'),str(uuid4()))
    async with database() as session:
        order=await session.get(Order,identifier)
        online['reference']=order.shop_online_payment_request.reference
        result=await service.recover(session,await session.get(AdminUser,1),identifier,
            OnlinePaymentRecovery(expected_revision=order.revision,invoice_id='syntheticOnlineInvoice'),str(uuid4()))
        assert result.status=='waiting' and result.page_url
    assert online['posts']==1


async def test_owner_routes_scope_revocation_and_no_provider_reads(database, checkout_dependencies, online):
    identifier,_,token=await prepared(database)
    app=FastAPI();app.include_router(public_router)
    async def db():
        async with database() as session: yield session
    app.dependency_overrides[get_db_session]=db
    app.dependency_overrides[get_optional_current_customer]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        for candidate in [None,'invalid']:
            response=await client.get(f'/orders/{identifier}/online-payment',headers={'X-Order-Payment-Token':candidate} if candidate else {})
            assert response.status_code==404
        async with database() as session:
            order=await session.get(Order,identifier)
            tracking=order.guest_tracking_url.rsplit('/',1)[-1]
        assert (await client.get(f'/orders/{identifier}/online-payment',headers={'X-Order-Payment-Token':tracking})).status_code==404
        result=await client.get(f'/orders/{identifier}/online-payment',headers={'X-Order-Payment-Token':token})
        assert result.status_code==200 and result.headers['cache-control']=='no-store'
        assert result.json()['status']=='not_started' and online['calls']==[]
        app.dependency_overrides[get_optional_current_customer]=lambda:SimpleNamespace(id=999)
        assert (await client.get(f'/orders/{identifier}/online-payment',headers={'X-Order-Payment-Token':token})).status_code==404
        app.dependency_overrides[get_optional_current_customer]=lambda:None
        async with database() as session:
            order=await session.get(Order,identifier);order.guest_tracking_revoked_at=datetime.now(UTC);await session.commit()
        assert (await client.get(f'/orders/{identifier}/online-payment',headers={'X-Order-Payment-Token':token})).status_code==404


@pytest.mark.parametrize('flag',['shop_online_payment_enabled','shop_online_payment_invoice_create_enabled','shop_online_payment_link_confirmed','mono_checkbox_auto_enabled'])
async def test_closed_flags_cannot_dispatch(database,checkout_dependencies,online,monkeypatch,flag):
    identifier,_,token=await prepared(database)
    monkeypatch.setattr(settings,flag,False)
    with pytest.raises(HTTPException):await start(database,identifier,token)
    assert online['posts']==0


async def test_disabled_operator_cod_and_profile_drift_fail_closed(database,checkout_dependencies,online,monkeypatch):
    identifier,_,token=await prepared(database)
    async with database() as session:
        actor=await session.get(AdminUser,1);actor.is_active=False;await session.commit()
    with pytest.raises(HTTPException):await start(database,identifier,token)
    async with database() as session:
        actor=await session.get(AdminUser,1);actor.is_active=True
        order=await session.get(Order,identifier);order.payment_method='cashOnDelivery';await session.commit()
    with pytest.raises(HTTPException):await start(database,identifier,token)
    assert online['posts']==0
    async with database() as session:
        order=await session.get(Order,identifier);order.payment_method='onlineCard';await session.commit()
    await start(database,identifier,token)
    profile=dict(settings.shop_monobank_profile,id='other-profile')
    monkeypatch.setattr(settings,'shop_monobank_profile',profile)
    with pytest.raises(HTTPException):await start(database,identifier,token)
    assert online['posts']==1


async def test_poll_late_receipt_and_physical_stock_once(database,checkout_dependencies,online):
    identifier,product,token=await prepared(database)
    result=await start(database,identifier,token)
    assert result.status=='waiting' and online['posts']==1
    online['status']='success'
    assert await poll.poll_once(database)=={'checked':1,'blocked':0}
    async with database() as session:
        state=await service.read(session,identifier,None,token)
        assert state.status=='paid_receipt_pending' and state.paid
        p=await session.get(Product,product);assert p.stock_quantity==4 and p.reserved_quantity==1
        await session.execute(text("UPDATE mono_checkbox_poll_state SET last_attempt_at=now()-interval '2 hours'"));await session.commit()
    online['checks']=[dict(id=str(uuid4()),status='done',type='sale',fiscalizationSource='checkbox',taxUrl='https://cabinet.tax.gov.ua/cashregs/check?id=synthetic')]
    assert await poll.poll_once(database)=={'checked':1,'blocked':0}
    async with database() as session:
        state=await service.read(session,identifier,None,token)
        assert state.status=='accepted' and state.receipt_accepted
        receipt=await session.scalar(select(ShopMonoCheckboxReceipt))
        assert receipt.snapshot['reports_imported'] is False and receipt.snapshot['structured_totals_verified'] is False
        order=await sales.load_order(session,identifier);revision=order.revision
    command=ShopPhysicalCommand(expected_revision=revision,evidence='synthetic pickup')
    key=str(uuid4())
    await execute(database,identifier,command,'handover',key=key)
    await execute(database,identifier,command,'handover',key=key)
    async with database() as session:
        p=await session.get(Product,product);assert p.stock_quantity==3 and p.reserved_quantity==0
        assert await session.scalar(select(func.count()).select_from(OrderPayment))==1


def test_discount_basket_splits_exact_cents(online):
    order=SimpleNamespace(total_amount=Decimal('9.99'),items=[
        SimpleNamespace(id=1,quantity=2,total_price=Decimal('8.00'),price=Decimal('4.00'),product_name='One',product_id=1),
        SimpleNamespace(id=2,quantity=1,total_price=Decimal('4.00'),price=Decimal('4.00'),product_name='Two',product_id=2)])
    basket,policy=service.basket_for(order)
    assert sum(line['total'] for line in basket)==999
    assert all(line['qty']*line['sum']==line['total'] and line['tax']==[8] for line in basket)
    assert sum(line['qty'] for line in basket)==3


async def test_expired_invoice_hides_payment_link_without_new_creation(database,checkout_dependencies,online):
    identifier,_,token=await prepared(database)
    await start(database,identifier,token)
    online['status']='expired'
    assert await poll.poll_once(database)=={'checked':1,'blocked':0}
    async with database() as session:
        state=await service.read(session,identifier,None,token)
        assert state.status=='requires_review' and state.page_url is None and not state.paid
    assert (await start(database,identifier,token)).status=='requires_review'
    assert online['posts']==1


async def test_authenticated_customer_ownership_and_expired_guest_token(database,checkout_dependencies,online):
    from app.models.customer import Customer
    from app.core.security import create_scoped_token
    identifier,_,token=await prepared(database)
    expired=create_scoped_token(identifier,'guest_order_payment',datetime.now(UTC)-timedelta(seconds=1))
    async with database() as session:
        with pytest.raises(HTTPException):await service.read(session,identifier,None,expired)
        owner=Customer(phone='+380500001231',name='Synthetic')
        session.add(owner);await session.flush()
        order=await session.get(Order,identifier);order.customer_id=owner.id;await session.commit()
        owner_id=owner.id
    async with database() as session:
        assert (await service.read(session,identifier,SimpleNamespace(id=owner_id))).status=='not_started'
        with pytest.raises(HTTPException):await service.read(session,identifier,SimpleNamespace(id=owner_id+1))
        with pytest.raises(HTTPException):await service.read(session,identifier,None,token)
        started=await service.start(session,identifier,SimpleNamespace(id=owner_id),None,str(uuid4()))
        assert started.status=='waiting'
        request=await session.scalar(select(ShopOnlinePaymentRequest))
        assert request.initiator=='customer' and request.customer_id==owner_id


async def test_invalid_supplied_customer_auth_is_not_guest_fallback(database,checkout_dependencies,online):
    identifier,_,token=await prepared(database)
    app=FastAPI();app.include_router(public_router)
    async def db():
        async with database() as session:yield session
    app.dependency_overrides[get_db_session]=db
    app.dependency_overrides[get_optional_current_customer]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        result=await client.get(f'/orders/{identifier}/online-payment',headers={'Authorization':'Bearer invalid','X-Order-Payment-Token':token})
        assert result.status_code==401 and result.headers['cache-control']=='no-store'
    assert online['calls']==[]


@pytest.mark.parametrize('flag',['mono_checkbox_auto_enabled','shop_online_payment_link_confirmed','shop_online_payment_invoice_create_enabled'])
async def test_flags_revoked_during_readiness_prevent_post(database,checkout_dependencies,online,monkeypatch,flag):
    identifier,_,token=await prepared(database)
    online['readiness_hook']=lambda:monkeypatch.setattr(settings,flag,False)
    state=await start(database,identifier,token)
    assert state.status=='requires_review' and online['posts']==0
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(ShopOnlinePaymentRequest))==1


async def test_production_poll_transport_path_uses_https_authority(database,checkout_dependencies,online,monkeypatch):
    identifier,_,token=await prepared(database)
    original_client=httpx.AsyncClient
    def mocked_client(*args,**kwargs):
        kwargs['transport']=online['transport']
        return original_client(*args,**kwargs)
    monkeypatch.setattr(httpx,'AsyncClient',mocked_client)
    monkeypatch.setattr(settings,'app_env','production')
    monkeypatch.setattr(settings,'shop_monobank_live_transport_enabled',True)
    monkeypatch.setattr(service,'gateway',lambda:MonobankGateway(create_capability='shop'))
    monkeypatch.setattr(payments,'gateway',lambda:MonobankGateway())
    assert (await start(database,identifier,token)).status=='waiting'
    online['status']='success'
    online['checks']=[dict(id=str(uuid4()),status='done',type='sale',fiscalizationSource='checkbox')]
    assert await poll.poll_once(database)=={'checked':1,'blocked':0}
    async with database() as session:
        state=await service.read(session,identifier,None,token)
        assert state.receipt_accepted
        receipt=await session.scalar(select(ShopMonoCheckboxReceipt))
        assert receipt.authority=='provider' and receipt.proof['transport']=='https'
    assert online['posts']==1


async def test_linked_barber_cannot_be_shop_automation_operator(database,checkout_dependencies,online):
    from app.models.booking import Master
    identifier,_,token=await prepared(database)
    async with database() as session:
        actor=await session.get(AdminUser,1)
        actor.is_superuser=False;actor.can_record_sales=True
        session.add(Master(admin_user_id=1,full_name='Synthetic barber'))
        await session.commit()
    with pytest.raises(HTTPException) as denied:
        await start(database,identifier,token)
    assert denied.value.status_code==403 and online['posts']==0


async def test_operator_permissions_revoked_after_readiness_prevent_dispatch(database,checkout_dependencies,online,monkeypatch):
    identifier,_,token=await prepared(database)
    original=MonobankGateway.readiness
    async def readiness(provider):
        result=await original(provider)
        async with database() as session:
            actor=await session.get(AdminUser,1);actor.is_active=False;await session.commit()
        return result
    monkeypatch.setattr(MonobankGateway,'readiness',readiness)
    assert (await start(database,identifier,token)).status=='requires_review'
    assert online['posts']==0


async def test_scoped_online_issuer_coexists_with_cod_internal_policy(database,checkout_dependencies,online,monkeypatch):
    from app.services import shop_mono_checkbox as direct, shop_fiscal
    monkeypatch.setattr(settings,'shop_fiscal_mock_enabled',True)
    identifier,_,token=await prepared(database)
    assert (await start(database,identifier,token)).status=='waiting'
    cod_id,_=await seed(database,method='cashOnDelivery')
    async with database() as session:
        online_order=await sales.load_order(session,identifier)
        cod_order=await sales.load_order(session,cod_id)
        assert direct.selected(online_order) and not direct.selected(cod_order)
        assert isinstance(sales.effective_fiscal_policy(online_order),direct.DirectFiscalPolicy)
        assert sales.effective_fiscal_policy(cod_order) is shop_fiscal.bound_policy
        with pytest.raises(HTTPException) as denied:direct.deny_internal_issuer(online_order)
        assert denied.value.detail['code']=='mono_checkbox_external_issuer_required'
        assert online_order.shop_online_payment_request.policy['tax_codes']==[8]
        assert not direct.configuration_reasons(online_order)
        assert not cod_order.shop_payment_intent
    assert online['posts']==1


def test_scoped_selection_retains_existing_internal_facts_and_legacy_global(online,monkeypatch):
    from app.services import shop_mono_checkbox as direct
    order=SimpleNamespace(payment_method='onlineCard',shop_checkbox_binding=object())
    assert not direct.selected(order)
    order.shop_payment_intent=SimpleNamespace(mode='prepayment',profile=settings.shop_monobank_profile,profile_hash=service.digest(settings.shop_monobank_profile))
    assert direct.selected(order)
    assert 'mono_checkbox_fiscal_mode_conflict' in direct.configuration_reasons(order)
    monkeypatch.setattr(settings,'shop_online_payment_enabled',False)
    assert direct.selected(order)  # persisted external binding survives closure
    assert not direct.selected(SimpleNamespace(payment_method='cashOnDelivery',shop_checkbox_binding=object()))
    assert not direct.selected(SimpleNamespace(payment_method='cashOnDelivery',shop_financial_returns=[object()]))
    assert direct.selected(SimpleNamespace(payment_method='cashOnDelivery'))  # unbound legacy global mode


async def test_tax_policy_revoked_during_readiness_prevents_dispatch(database,checkout_dependencies,online,monkeypatch):
    identifier,_,token=await prepared(database)
    online['readiness_hook']=lambda:monkeypatch.setattr(settings,'shop_online_payment_fiscal_policy',{'id':'invalid','tax_codes':[9],'unit':'шт.'})
    assert (await start(database,identifier,token)).status=='requires_review'
    assert online['posts']==0
