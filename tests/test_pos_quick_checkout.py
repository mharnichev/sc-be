"""Quick POS durable intent tests: disposable migrated PG and existing mono mock."""
import asyncio
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import func,select
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.product import Product
from app.models.order import Order,OrderHandover,OrderPayment,OrderIntent
from app.models.master_mono import MasterMonoCreateRequest
from app.models.pos import POSCheckoutIntent
from app.schemas.pos import CreateCheckout,RevisionCommand
from app.services import pos,master_mono
from tests.test_shop_fiscal_integration import template_database,database
from tests.test_inventory_integration import checkout_dependencies,add_product
from tests.test_master_mono_integration import mock_provider,receipt

pytestmark=pytest.mark.anyio

@pytest.fixture(scope='module')
def anyio_backend():return 'asyncio'

@pytest.fixture
async def sellers(database):
    async with database() as s:
        for ident in (2,3):
            s.add(AdminUser(id=ident,email=f'pos-{ident}@example.test',hashed_password='unused',is_active=True,is_superuser=False,can_record_sales=True,can_accept_qr_payments=True))
            s.add(Master(admin_user_id=ident,full_name=f'Synthetic seller {ident}'))
        await s.commit()

@pytest.fixture
async def product(database,sellers):return await add_product(database,stock=5)

async def call(database,fn,*args,actor_id=2,**kw):
    async with database() as s:
        return await fn(s,await s.get(AdminUser,actor_id),*args,**kw)

def payload(product,**changes):
    return CreateCheckout(intent_id=uuid4(),product_id=product.id,quantity=1,expected_amount_minor=10000,currency='UAH',**changes)

async def ready(database,state,view):
    state.update(status='success',checks=[receipt()])
    return await call(database,pos.refresh,view['checkout_id'],RevisionCommand(expected_revision=view['revision']),str(uuid4()))

async def test_same_intent_keys_lost_response_and_distinct_real_intents(database,product,checkout_dependencies,mock_provider):
    body=payload(product);key=str(uuid4())
    first=await call(database,pos.create,body,key)
    for retry in (key,str(uuid4())):
        again=await call(database,pos.create,body,retry)
        assert again['order_id']==first['order_id'] and again['checkout_id']==first['checkout_id']
    found=await call(database,pos.by_intent,str(body.intent_id))
    assert found['order_id']==first['order_id'] and mock_provider['post_calls']==1
    mock_provider['invoice_id']='synthetic_distinct_invoice'
    second=await call(database,pos.create,payload(product),str(uuid4()))
    assert second['order_id']!=first['order_id'] and mock_provider['post_calls']==2
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==2
        assert await s.scalar(select(func.count()).select_from(Order))==2

async def test_concurrent_tabs_same_intent_dispatch_once(database,product,checkout_dependencies,mock_provider):
    body=payload(product)
    results=await asyncio.gather(call(database,pos.create,body,str(uuid4())),call(database,pos.create,body,str(uuid4())),return_exceptions=True)
    assert all(isinstance(r,dict) for r in results),results
    assert len({r['order_id'] for r in results})==1
    assert mock_provider['post_calls']==1

async def test_price_change_rolls_back_all_and_explicit_confirmation(database,product,checkout_dependencies,mock_provider):
    body=payload(product).model_copy(update={'expected_amount_minor':9900})
    key=str(uuid4())
    with pytest.raises(HTTPException) as changed:await call(database,pos.create,body,key)
    assert changed.value.detail['code']=='price_changed'
    assert changed.value.detail['current_offer']['amount_minor']==10000
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(Order))==0
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==0
        assert await s.scalar(select(func.count()).select_from(OrderIntent))==0
        assert (await s.get(Product,product.id)).reserved_quantity==0
    assert mock_provider['post_calls']==0
    accepted=await call(database,pos.create,body.model_copy(update={'expected_amount_minor':10000}),key)
    assert accepted['amount_minor']==10000 and mock_provider['post_calls']==1

async def test_unknown_dispatch_never_reposts_or_clears_on_ui_close(database,product,checkout_dependencies,mock_provider):
    mock_provider['create_error']=500
    body=payload(product);view=await call(database,pos.create,body,str(uuid4()))
    assert view['status']=='requires_review' and view['payment']['qr_payload'] is None
    assert not view['actions']['can_resume'] and not view['actions']['can_start_new']
    again=await call(database,pos.create,body,str(uuid4()))
    assert again['order_id']==view['order_id'] and mock_provider['post_calls']==1
    async with database() as s:
        assert (await s.get(Product,product.id)).reserved_quantity==1
        assert await s.scalar(select(func.count()).select_from(MasterMonoCreateRequest))==1

async def test_paid_pending_receipt_hides_qr_then_handover_once(database,product,checkout_dependencies,mock_provider):
    view=await call(database,pos.create,payload(product),str(uuid4()))
    mock_provider.update(status='success',checks=[])
    paid=await call(database,pos.refresh,view['checkout_id'],RevisionCommand(expected_revision=view['revision']),str(uuid4()))
    assert paid['payment']['status']=='paid' and paid['status']=='paid_receipt_pending'
    assert paid['payment']['qr_payload'] is None and not paid['actions']['can_handover']
    assert not paid['actions']['can_start_new'] and mock_provider['post_calls']==1
    accepted=await ready(database,mock_provider,paid)
    assert accepted['actions']['can_handover'] and accepted['receipt']['status']=='accepted'
    key=str(uuid4());cmd=RevisionCommand(expected_revision=accepted['revision'])
    done=await call(database,pos.handover,accepted['checkout_id'],cmd,key)
    replay=await call(database,pos.handover,accepted['checkout_id'],cmd,key)
    assert done['handover']==replay['handover'] and done['handover']['actor_user_id']==2
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(OrderHandover))==1
        assert (await s.get(Product,product.id)).stock_quantity==4
        assert (await s.get(Product,product.id)).reserved_quantity==0
        row=await s.scalar(select(OrderHandover));assert row.evidence=='pos_customer_handover'
    assert mock_provider['post_calls']==1

async def test_admin_uses_same_real_flow_and_own_attribution(database,product,checkout_dependencies,mock_provider):
    caps=await call(database,pos.capabilities,actor_id=1)
    assert caps['mode']=='mono_checkbox' and caps['can_start']
    view=await call(database,pos.create,payload(product),str(uuid4()),actor_id=1)
    assert view['employee']['user_id']==1 and view['employee']['master_id'] is None
    async with database() as s:
        assert (await s.get(Order,view['order_id'])).created_by_user_id==1
        assert (await s.scalar(select(MasterMonoCreateRequest))).operator_id==1

async def test_permissions_foreign_scope_and_body_conflicts(database,product,checkout_dependencies,mock_provider):
    body=payload(product);key=str(uuid4());view=await call(database,pos.create,body,key)
    with pytest.raises(HTTPException) as foreign:await call(database,pos.state,view['checkout_id'],actor_id=3)
    assert foreign.value.status_code==404
    with pytest.raises(HTTPException):await call(database,pos.create,body.model_copy(update={'intent_id':uuid4()}),key)
    async with database() as s:
        actor=await s.get(AdminUser,2);actor.can_accept_qr_payments=False;await s.commit()
    with pytest.raises(HTTPException) as denied:await call(database,pos.create,body,key)
    assert denied.value.status_code==403 and mock_provider['post_calls']==1

async def test_last_unit_two_actors_one_reservation(database,product,checkout_dependencies,mock_provider):
    async with database() as s:
        row=await s.get(Product,product.id);row.stock_quantity=1;await s.commit()
    async def attempt(actor):
        try:return await call(database,pos.create,payload(product),str(uuid4()),actor_id=actor)
        except HTTPException as error:return error.status_code
    results=await asyncio.gather(attempt(2),attempt(3),return_exceptions=True)
    assert sum(isinstance(i,dict) for i in results)==1 and 409 in results,results
    assert mock_provider['post_calls']==1
    async with database() as s:assert (await s.get(Product,product.id)).reserved_quantity==1

async def test_server_history_status_filter_total_and_read_only_details(database,product,checkout_dependencies,mock_provider):
    first=await call(database,pos.create,payload(product),str(uuid4()))
    paid=await ready(database,mock_provider,first)
    await call(database,pos.handover,paid['checkout_id'],RevisionCommand(expected_revision=paid['revision']),str(uuid4()))
    mock_provider.update(invoice_id='other_pending_fixture',status='created',checks=[])
    await call(database,pos.create,payload(product),str(uuid4()),actor_id=3)
    rows=await call(database,pos.sales,actor_id=1,order_status='completed',page_size=1)
    assert rows['total']==1 and rows['items'][0]['order_id']==first['order_id']
    assert rows['items'][0]['order_status']=='completed' and len(rows['items'][0]['items'])==1
    detail=await call(database,pos.sale_detail,first['order_id'],actor_id=1)
    assert not any(detail['actions'].values())
    with pytest.raises(HTTPException):await call(database,pos.sales,actor_id=2)

async def test_prepared_crash_resumes_same_order_without_duplicate(database,product,checkout_dependencies,mock_provider,monkeypatch):
    original=pos.ensure_started
    async def crash(*args):raise RuntimeError('synthetic process loss after order commit')
    monkeypatch.setattr(pos,'ensure_started',crash)
    body=payload(product);key=str(uuid4())
    with pytest.raises(RuntimeError):await call(database,pos.create,body,key)
    monkeypatch.setattr(pos,'ensure_started',original)
    recovered=await call(database,pos.by_intent,str(body.intent_id))
    assert recovered['status']=='prepared' and recovered['actions']['can_resume']
    assert mock_provider['post_calls']==0
    result=await call(database,pos.resume,recovered['checkout_id'],RevisionCommand(expected_revision=recovered['revision']),key)
    assert result['order_id']==recovered['order_id'] and mock_provider['post_calls']==1
    async with database() as s:assert await s.scalar(select(func.count()).select_from(Order))==1

async def test_unknown_recovery_attaches_existing_invoice_get_only(database,product,checkout_dependencies,mock_provider):
    from app.schemas.master_mono import MasterMonoRecover
    mock_provider['create_error']=500
    view=await call(database,pos.create,payload(product),str(uuid4()))
    async with database() as s:
        await master_mono.recover(s,await s.get(AdminUser,1),view['order_id'],
            MasterMonoRecover(expected_revision=view['revision'],invoice_id=mock_provider['invoice_id']),str(uuid4()))
    recovered=await call(database,pos.state,view['checkout_id'])
    assert recovered['payment']['invoice_id']==mock_provider['invoice_id']
    assert recovered['payment']['qr_payload'] and mock_provider['post_calls']==1

async def test_receipt_failure_after_received_payment_never_returns_qr(database,product,checkout_dependencies,mock_provider):
    view=await call(database,pos.create,payload(product),str(uuid4()))
    failed=receipt();failed['status']='failed'
    mock_provider.update(status='success',checks=[failed])
    result=await call(database,pos.refresh,view['checkout_id'],RevisionCommand(expected_revision=view['revision']),str(uuid4()))
    assert result['payment']['status']=='paid' and result['receipt']['status']=='failed'
    assert result['status']=='requires_review' and result['payment']['qr_payload'] is None
    assert not result['actions']['can_handover'] and not result['actions']['can_start_new']
    assert mock_provider['post_calls']==1

async def test_pre_dispatch_revoked_grant_blocks_post_and_preserves_fence(database,product,checkout_dependencies,mock_provider,monkeypatch):
    from app.services.monobank_provider import MonobankGateway
    original=MonobankGateway.readiness
    revoked=False
    async def readiness(gateway):
        nonlocal revoked
        result=await original(gateway)
        if not revoked:
            revoked=True
            async with database() as s:
                actor=await s.get(AdminUser,2);actor.can_accept_qr_payments=False;await s.commit()
        return result
    monkeypatch.setattr(MonobankGateway,'readiness',readiness)
    body=payload(product)
    with pytest.raises(HTTPException) as denied:await call(database,pos.create,body,str(uuid4()))
    assert denied.value.status_code==403 and mock_provider['post_calls']==0
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(MasterMonoCreateRequest))==1
        actor=await s.get(AdminUser,2);actor.can_accept_qr_payments=True;await s.commit()
    retry=await call(database,pos.create,body,str(uuid4()))
    assert retry['status']=='requires_review' and mock_provider['post_calls']==0

async def test_canonical_retail_price_real_promotions_and_volume(database,product,mock_provider):
    from app.models.shop_promotion import ShopPromotion,ShopPromotionDiscountType,ShopPromotionTrigger
    async with database() as s:
        p=await s.get(Product,product.id);p.price=Decimal('10.00');p.shop_price=Decimal('20.00');p.recommended_retail_price=Decimal('99.00');p.volume_ml=100
        promotion=ShopPromotion(name='Synthetic automatic',discount_type=ShopPromotionDiscountType.percent,
            discount_value=Decimal('10'),trigger=ShopPromotionTrigger.automatic,is_active=True,products=[p])
        s.add(promotion);await s.commit()
    offers=await call(database,pos.products,q='')
    offer=next(row for row in offers['items'] if row['id']==product.id)
    assert offer['amount_minor']==1800 and offer['variant_label']=='100 мл'
    body=payload(product).model_copy(update={'expected_amount_minor':1800})
    result=await call(database,pos.create,body,str(uuid4()))
    assert result['amount_minor']==1800 and result['items'][0]['unit_minor']==1800
    assert mock_provider['amount']==1800


async def test_existing_real_multi_item_adoption_is_link_only(database,product,checkout_dependencies,mock_provider,monkeypatch):
    import httpx
    from app.core.config import settings
    from app.schemas.order import POSOrderCreate
    from app.schemas.master_mono import MasterMonoCommand
    from app.schemas.pos import AdoptCheckout
    from app.services import local_sales
    # Intercept only the wire client: retain actual production authority guards/parser.
    transport=master_mono.gateway().transport
    client=httpx.AsyncClient
    factory=mock_provider['gateway_factory']
    def offline_client(*args,**kwargs):
        assert kwargs.get('transport') is None
        return client(*args,**{**kwargs,'transport':transport})
    monkeypatch.setattr(httpx,'AsyncClient',offline_client)
    monkeypatch.setattr(master_mono,'gateway',lambda:factory())
    monkeypatch.setattr(settings,'app_env','production')
    monkeypatch.setattr(settings,'master_monobank_live_transport_enabled',True)
    async with database() as s:
        other=Product(name='Second synthetic product',slug='second-synthetic-pos',price=Decimal('100.00'),stock_quantity=3,reserved_quantity=0,is_active=True)
        s.add(other);await s.commit()
        order=await local_sales.prepare(s,await s.get(AdminUser,2),POSOrderCreate(items=[
            {'product_id':product.id,'quantity':2},{'product_id':other.id,'quantity':1}],simulation=False),str(uuid4()))
        order_id,revision=order.id,order.revision
    await call(database,master_mono.start,order_id,MasterMonoCommand(expected_revision=revision),str(uuid4()))
    before=await call(database,pos.sale_detail,order_id,actor_id=1)
    assert before['source']=='existing_mono_pos' and before['checkout_id'] is None
    assert sorted(i['quantity'] for i in before['items'])==[1,2]
    assert not any(before['actions'].values())
    requests=list(mock_provider['calls'])
    body=AdoptCheckout(intent_id=uuid4(),order_id=order_id)
    with pytest.raises(HTTPException) as foreign:await call(database,pos.adopt,body,str(uuid4()),actor_id=3)
    assert foreign.value.status_code==404
    linked=await call(database,pos.adopt,body,str(uuid4()))
    replay=await call(database,pos.adopt,body,str(uuid4()))
    assert linked['order_id']==order_id and replay['checkout_id']==linked['checkout_id']
    assert linked['payment']['invoice_id']==mock_provider['invoice_id']
    assert sorted(i['quantity'] for i in linked['items'])==[1,2]
    assert mock_provider['calls']==requests and mock_provider['post_calls']==1
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(Order))==1
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==1
        assert (await s.get(Product,product.id)).reserved_quantity==2


async def test_admin_quick_sale_existing_poller_and_stale_reversal_block(database,product,checkout_dependencies,mock_provider,monkeypatch):
    from app.core.config import settings
    from app.services import local_sales
    from app.services.mono_checkbox_poll import poll_once
    monkeypatch.setattr(settings,'mono_checkbox_auto_enabled',True)
    first=await call(database,pos.create,payload(product),str(uuid4()),actor_id=1)
    mock_provider.update(status='success',checks=[receipt()])
    assert await poll_once(database)=={'checked':1,'blocked':0}
    accepted=await call(database,pos.state,first['checkout_id'],actor_id=1)
    assert accepted['status']=='ready_to_handover' and accepted['actions']['can_handover']
    class StaleClock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.now(tz)+timedelta(seconds=31)
    monkeypatch.setattr(local_sales,'datetime',StaleClock)
    mock_provider['status']='reversed'
    with pytest.raises(HTTPException):
        await call(database,pos.handover,accepted['checkout_id'],RevisionCommand(expected_revision=accepted['revision']),str(uuid4()),actor_id=1)
    latest=await call(database,pos.state,first['checkout_id'],actor_id=1)
    assert latest['payment']['status']=='reversed' and latest['status']=='requires_review'
    assert not latest['actions']['can_handover'] and latest['payment']['qr_payload'] is None
    async with database() as s:
        assert (await s.get(Product,product.id)).stock_quantity==5
        assert await s.scalar(select(func.count()).select_from(OrderHandover))==0
    assert mock_provider['post_calls']==1
