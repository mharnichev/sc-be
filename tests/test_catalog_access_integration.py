"""Actual auth/HTTP/PG checks in an explicitly isolated schema; no provider I/O."""
from datetime import UTC,datetime,timedelta
from types import SimpleNamespace
import httpx
import pytest
from fastapi import FastAPI,HTTPException
from sqlalchemy import select,func,text
from app.api.v1.routes import products,inventory
from app.core.database import get_db_session
from app.core.security import create_scoped_access_token
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.product import Product
from app.models.shop import ProductImage,ProductImageVariant
from app.models.upload import Upload
from test_inventory_integration import database,anyio_backend

@pytest.fixture
async def catalog(database):
    async with database() as s:
        s.add_all([AdminUser(id=2,email='master@example.test',hashed_password='unused',is_active=True,is_superuser=False,can_record_sales=True,can_accept_qr_payments=True,can_post_inventory=True,can_manage_fiscal_settings=True),AdminUser(id=3,email='manager@example.test',hashed_password='unused',is_active=True,is_superuser=False),AdminUser(id=4,email='inactive@example.test',hashed_password='unused',is_active=False,is_superuser=False)])
        await s.flush();s.add_all([Master(id=1,full_name='Test master',admin_user_id=2),Master(id=2,full_name='Superuser with master link',admin_user_id=1)])
        s.add(Product(name='Synthetic pomade',slug='synthetic-pomade',sku='SYN-1',barcode='0001',price=100,image_url='/media/original.webp',attributes_json={'image_urls':['/media/original.webp']},stock_quantity=4,is_active=True))
        await s.flush();s.add(ProductImageVariant(product_id=1,source_key='legacy:synthetic',source_url='/media/original.webp',source_fingerprint='a'*64,preset='studio_light',recipe_version='test-only',status='succeeded',output_url='/media/processed.webp',is_preferred=True));await s.commit()
    app=FastAPI();app.include_router(products.backoffice_router,prefix='/api/v1/backoffice/products');app.include_router(inventory.backoffice_router,prefix='/api/v1/backoffice/inventory')
    async def db():
        async with database() as s:yield s
    app.dependency_overrides[get_db_session]=db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated.test') as client:yield client

def auth(uid=2,scope='admin'):
    return {'Authorization':'Bearer '+create_scoped_access_token(str(uid),scope)}

def writes():
    base='/api/v1/backoffice/products'
    return [('POST',base,{'json':{'name':'New synthetic','slug':'new-synthetic','price':12}}),('PUT',base+'/1',{'json':{'name':'Changed','price':1,'image_url':'/changed.webp','is_active':False}}),('DELETE',base+'/1',{}),('POST',base+'/1/images',{'files':{'file':('photo.png',b'synthetic-not-dispatched','image/png')}}),('PUT',base+'/1/images/1/file',{'files':{'file':('photo.png',b'synthetic-not-dispatched','image/png')}}),('PATCH',base+'/1/images/reorder',{'json':{'image_ids':[1]}}),('PATCH',base+'/1/images/1',{'json':{'alt':'changed','is_active':False}}),('DELETE',base+'/1/images/1',{}),('POST','/api/v1/backoffice/inventory/products',{'json':{'name':'Synthetic new','barcode':'0002'},'headers':{'Idempotency-Key':'catalog-test-only'}}),('PUT','/api/v1/backoffice/inventory/products/1/barcode',{'json':{'barcode':'new-code'}}),('PATCH','/api/v1/backoffice/inventory/products/1/settings',{'json':{'barcode':'new-code','allow_backorder':True}})]

async def facts(database):
    async with database() as s:
        return [(await s.execute(text('SELECT md5(row_to_json(p)::text) FROM products p ORDER BY id'))).scalars().all(),*[await s.scalar(select(func.count()).select_from(model)) for model in (ProductImageVariant,ProductImage,Upload)]]

@pytest.mark.anyio
async def test_master_all_catalog_write_paths_are_403_before_side_effects(catalog,database,monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError('Mutation service reached by master')
    for service,names in [(products.service,['create_product','update_product','delete_product']),(products.product_image_service,['create_image','replace_file','reorder_images','update_image','delete_image']),(inventory.inventory_service,['create_product_card','bind_barcode','update_product_settings'])]:
        for name in names:monkeypatch.setattr(service,name,forbidden)
    before=await facts(database)
    for method,path,kwargs in writes():
        kw=dict(kwargs);kw['headers']={**auth(),**kw.get('headers',{})}
        r=await catalog.request(method,path,**kw);assert r.status_code==403,(method,path,r.text)
    # Missing product does not bypass role denial or expose its existence.
    assert (await catalog.delete('/api/v1/backoffice/products/999',headers=auth())).status_code==403
    assert await facts(database)==before

@pytest.mark.anyio
@pytest.mark.parametrize('uid',[1,3])
async def test_manager_and_superuser_master_reach_all_existing_write_services(catalog,monkeypatch,uid):
    async def reached(*args,**kwargs):raise HTTPException(418,'Synthetic service reached; no writes')
    for service,names in [(products.service,['create_product','update_product','delete_product']),(products.product_image_service,['create_image','replace_file','reorder_images','update_image','delete_image']),(inventory.inventory_service,['create_product_card','bind_barcode'])]:
        for name in names:monkeypatch.setattr(service,name,reached)
    for method,path,kwargs in writes()[:-1]:
        kw=dict(kwargs);kw['headers']={**auth(uid),**kw.get('headers',{})}
        r=await catalog.request(method,path,**kw);assert r.status_code==418,(method,path,r.text)

@pytest.mark.anyio
async def test_actual_auth_requires_active_admin_and_reads_stay_available(catalog):
    for headers in ({},auth(4),auth(2,'customer')):
        assert (await catalog.put('/api/v1/backoffice/products/1',json={'name':'Changed'},headers=headers)).status_code==401
    for uid in (1,2,3):
        for path in ('/api/v1/backoffice/products/1','/api/v1/backoffice/products','/api/v1/backoffice/inventory/stock','/api/v1/backoffice/inventory/products/barcode/0001'):
            r=await catalog.get(path,headers=auth(uid));assert r.status_code==200,r.text
            body=r.json();body=body['items'][0] if 'items' in body else body
            assert body['image_url']=='/media/original.webp'
            assert body['display_image_url']=='/media/processed.webp'
            assert body['display_image_urls']==['/media/processed.webp']

@pytest.mark.anyio
async def test_manager_crud_preserves_original_photo_and_processed_display(catalog):
    r=await catalog.put('/api/v1/backoffice/products/1',json={'name':'Manager renamed'},headers=auth(3));assert r.status_code==200,r.text
    assert r.json()['image_url']=='/media/original.webp' and r.json()['display_image_url']=='/media/processed.webp'
    r=await catalog.post('/api/v1/backoffice/products',json={'name':'Manager new','slug':'manager-new','price':11},headers=auth(3));assert r.status_code==201,r.text
    pid=r.json()['id'];r=await catalog.delete(f'/api/v1/backoffice/products/{pid}',headers=auth(3));assert r.status_code==204,r.text
    assert (await catalog.get(f'/api/v1/backoffice/products/{pid}',headers=auth(3))).status_code==404

@pytest.mark.anyio
async def test_new_master_link_takes_effect_on_next_request(catalog,database):
    assert (await catalog.put('/api/v1/backoffice/products/1',json={'name':'Before role change'},headers=auth(3))).status_code==200
    async with database() as s:
        s.add(Master(id=3,full_name='Newly linked master',admin_user_id=3));await s.commit()
    # Same JWT, current DB role: no process-local permission cache or token role trust.
    assert (await catalog.put('/api/v1/backoffice/products/1',json={'name':'Forbidden'},headers=auth(3))).status_code==403
    async with database() as s:assert (await s.get(Product,1)).name=='Before role change'

@pytest.mark.anyio
async def test_inventory_write_responses_have_real_processed_projection(catalog,database):
    async with database() as s:
        (await s.get(Product,1)).barcode=None
        await s.commit()
    r=await catalog.put('/api/v1/backoffice/inventory/products/1/barcode',json={'barcode':'NEW-0001'},headers=auth(3));assert r.status_code==200,r.text
    assert r.json()['image_url']=='/media/original.webp' and r.json()['display_image_urls']==['/media/processed.webp']
    r=await catalog.patch('/api/v1/backoffice/inventory/products/1/settings',json={'barcode':'NEW-0001','allow_backorder':False},headers=auth(1));assert r.status_code==200,r.text
    assert r.json()['display_image_url']=='/media/processed.webp'
    r=await catalog.post('/api/v1/backoffice/inventory/products',json={'name':'New inventory card','barcode':'NEW-0002','image_url':'/media/new-original.webp'},headers={**auth(3),'Idempotency-Key':'new-card-with-photo'});assert r.status_code==201,r.text
    assert r.json()['display_image_urls']==['/media/new-original.webp']
