"""Actual FastAPI wire/auth/strict DTO/privacy against disposable migrated PG."""
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select,text,func
from sqlalchemy.exc import DBAPIError
from app.api.v1.routes.pos import router
from app.core.database import get_db_session
from app.core.security import create_access_token
from app.models.admin_user import AdminUser
from app.models.brand import Brand
from app.models.product import Product
from app.models.pos import POSCheckoutIntent
from app.models.order import Order
from tests.test_pos_quick_checkout import database,template_database,checkout_dependencies,mock_provider,sellers,product,call,payload,ready
from app.services import pos

pytestmark=pytest.mark.anyio
@pytest.fixture(scope='module')
def anyio_backend():return 'asyncio'

@pytest.fixture
async def client(database):
    app=FastAPI();app.include_router(router,prefix='/api/v1/backoffice')
    async def dependency():
        async with database() as session:yield session
    app.dependency_overrides[get_db_session]=dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local.test') as c:yield c

def auth(actor=2,**extras):return {'Authorization':'Bearer '+create_access_token(str(actor)),**extras}
BASE='/api/v1/backoffice/pos'

async def test_full_wire_and_forbidden_input(client,database,product,checkout_dependencies,mock_provider):
    assert (await client.get(BASE+'/capabilities')).status_code==401
    for actor in (1,2):
        cap=await client.get(BASE+'/capabilities',headers=auth(actor));assert cap.status_code==200 and cap.json()['mode']=='mono_checkbox'
    body=payload(product).model_dump(mode='json');headers=auth(**{'Idempotency-Key':str(uuid4())})
    for extra in ({'actor_id':1},{'simulation':True},{'customer_name':'extra'},{'quantity':True}):
        denied=await client.post(BASE+'/checkouts',json={**body,**extra},headers=headers)
        assert denied.status_code==422 and denied.json()=={'detail':{'code':'invalid_command'}}
    made=await client.post(BASE+'/checkouts',json=body,headers=headers)
    assert made.status_code==201,made.text
    retried=await client.post(BASE+'/checkouts',json=body,headers=headers)
    assert retried.status_code==200 and retried.json()['order_id']==made.json()['order_id']
    view=made.json();assert len(view['items'])==1 and view['items'][0]['quantity']==1
    assert made.headers['Cache-Control']=='private, no-store'
    assert (await client.get(BASE+'/checkouts/'+view['checkout_id'],headers=auth(3))).status_code==404
    recovered=await client.get(BASE+'/checkouts/by-intent/'+body['intent_id'],headers=auth())
    assert recovered.json()['order_id']==view['order_id']
    invalid=await client.post(BASE+'/checkouts/'+view['checkout_id']+'/handover',json={'expected_revision':view['revision'],'reason':'sensitive-reason'},headers=headers)
    assert invalid.status_code==422 and 'sensitive' not in invalid.text
    assert mock_provider['post_calls']==1

async def test_search_brand_sku_barcode_literal_and_server_price(client,database,product,checkout_dependencies,mock_provider):
    async with database() as s:
        brand=Brand(name='SyntheticBrand',slug=uuid4().hex);s.add(brand);await s.flush()
        p=await s.get(Product,product.id);p.brand_id=brand.id;p.sku='POS_EXACT_17';p.barcode='0000000170001';p.package_size='100 ml';p.image_url='/media/products/synthetic.webp';await s.commit()
    for term in ('SyntheticBrand','POS_EXACT_17','0000000170001'):
        result=await client.get(BASE+'/products',params={'q':term},headers=auth())
        assert result.status_code==200 and result.json()['total']==1,result.text
        item=result.json()['items'][0]
        assert item['amount_minor']==10000 and item['barcode']=='0000000170001' and item['variant_label']=='100 ml'
        assert item['image_url'] and 'effective_shop_price' not in item
    wildcard=await client.get(BASE+'/products',params={'q':'%'},headers=auth())
    assert wildcard.json()['total']==0

async def test_database_identity_immutable_and_source_not_editable(database,product,checkout_dependencies,mock_provider):
    view=await call(database,pos.create,payload(product),str(uuid4()))
    async with database() as s:
        for statement in ('UPDATE pos_checkout_intents SET actor_id=1','DELETE FROM pos_checkout_intents'):
            with pytest.raises(DBAPIError):await s.execute(text(statement))
            await s.rollback()
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==1
        assert (await s.get(Order,view['order_id'])).created_by_user_id==2

async def test_filter_wire_counts_and_date_validation(client,database,product,checkout_dependencies,mock_provider):
    await call(database,pos.create,payload(product),str(uuid4()))
    rows=await client.get(BASE+'/sales',params={'order_status':'pending','page_size':1,'employee_user_id':2},headers=auth(1))
    assert rows.status_code==200 and rows.json()['total']==1 and rows.json()['items'][0]['order_status']=='pending',rows.text
    assert (await client.get(BASE+'/sales',params={'order_status':'ready_to_handover'},headers=auth(1))).status_code==422
    invalid=await client.get(BASE+'/sales',params={'date_from':'2026-11-10','date_to':'2026-11-01'},headers=auth(1))
    assert invalid.status_code==422
    assert (await client.get(BASE+'/sales',headers=auth())).status_code==403
