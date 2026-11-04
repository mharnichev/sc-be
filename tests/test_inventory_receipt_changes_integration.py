"""Receipt commands through actual HTTP, production PostgreSQL guards, synthetic data."""
import asyncio
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes import inventory as routes, products
from app.core.database import Base, get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.inventory import (InventoryMovement, InventoryReceipt, InventoryReceiptAllocation,
    InventoryReceiptChange, InventoryReceiptItem, InventoryReceiptStatus)
from app.models.product import Product
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
from tests.test_inventory_integration import anyio_backend, database

pytestmark=pytest.mark.anyio
PREFIX='/api/v1/backoffice/inventory'


def migration_module():
    migration_path=Path(__file__).parents[1]/'alembic/versions/0145_receipt_item_changes.py'
    if not migration_path.exists():
        # The existing local development tree has its separately identified0140 chain.
        migration_path=Path(__file__).parents[1]/'alembic/versions/0141_receipt_item_changes_local.py'
    spec=spec_from_file_location('receipt_changes_migration',migration_path)
    module=module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
async def prepared(database):
    engine=database.kw['bind']
    async with engine.begin() as connection:
        await connection.execute(text('DROP TABLE inventory_receipt_changes'))
        await connection.execute(text('ALTER TABLE products DROP COLUMN purchase_cost_receipt_id'))
        await connection.execute(text('ALTER TABLE products DROP COLUMN purchase_unit_cost'))
        await connection.execute(text('ALTER TABLE inventory_receipts DROP COLUMN revision'))
        def upgrade(sync):
            module=migration_module();module.op=Operations(MigrationContext.configure(sync,opts={'target_metadata':Base.metadata}));module.upgrade()
        await connection.run_sync(upgrade)
    async with database() as session:
        product=Product(name='Synthetic receipt product',slug='synthetic-receipt-product',sku='RECEIPT-TEST',
                        barcode='0012345678901',price=Decimal('900'),shop_price=Decimal('800'),
                        recommended_retail_price=Decimal('1000'),stock_quantity=0,reserved_quantity=0,is_active=True)
        session.add(product)
        session.add_all([AdminUser(id=2,email='receipt-reader@example.test',hashed_password='unused',is_active=True,is_superuser=False,can_post_inventory=False),
                         AdminUser(id=3,email='receipt-master@example.test',hashed_password='unused',is_active=True,is_superuser=False,can_post_inventory=True)])
        await session.flush()
        session.add(Master(full_name='Synthetic restricted master',admin_user_id=3))
        await session.commit()
        pid=product.id
    yield database,pid


@pytest.fixture
async def http(prepared):
    sessions,pid=prepared
    state={'actor':1}
    app=FastAPI();app.include_router(routes.backoffice_router,prefix=PREFIX)
    app.include_router(products.public_router,prefix='/api/v1/public/products')
    app.include_router(products.backoffice_router,prefix='/api/v1/backoffice/products')
    async def get_session():
        async with sessions() as session:yield session
    async def actor(session=Depends(get_db_session)):
        return await session.get(AdminUser,state['actor'])
    app.dependency_overrides[get_db_session]=get_session
    app.dependency_overrides[get_current_admin_user]=actor
    async with AsyncClient(transport=ASGITransport(app),base_url='http://synthetic.test') as client:
        yield client,pid,state,app,sessions


async def request(client,method,path,key='synthetic-command',body=None,status=200):
    response=await client.request(method,PREFIX+path,headers={'Idempotency-Key':key},json=body)
    assert response.status_code==status,response.text
    return response.json() if response.content else None


async def draft(client,pid,quantity=5,cost='10.00',key='base',allocations=None):
    receipt=await request(client,'POST','/receipts',key+'-create',{'reason':'Synthetic receipt'},201)
    return await request(client,'POST',f"/receipts/{receipt['id']}/items",key+'-add',
        dict(product_id=pid,quantity=quantity,purchase_unit_cost=cost,allocations=allocations or []))


async def post(client,receipt,key='post'):
    return await request(client,'POST',f"/receipts/{receipt['id']}/post",key)


def command(receipt,**kwargs):
    return dict(expected_revision=receipt['revision'],quantity=kwargs.pop('quantity',receipt['items'][0]['effective_quantity']),
                purchase_unit_cost=kwargs.pop('purchase_unit_cost',receipt['items'][0]['effective_purchase_unit_cost']),
                reason='Correct synthetic entry',**kwargs)


async def correction(client,receipt,key='correction',status=200,**kwargs):
    return await request(client,'POST',f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}/corrections",key,
                         command(receipt,**kwargs),status)


async def test_draft_update_delete_replays_audit_and_no_stock(http):
    client,pid,_,_,sessions=http
    receipt=await draft(client,pid)
    item=receipt['items'][0];path=f"/receipts/{receipt['id']}/items/{item['id']}"
    body=command(receipt,quantity=7,purchase_unit_cost='12.5',allocations=[])
    changed=await request(client,'PUT',path,'edit',body)
    assert changed['revision']==receipt['revision']+1
    assert changed['items'][0]['quantity']==7
    assert Decimal(changed['effective_purchase_value'])==Decimal('87.50')
    assert (await request(client,'PUT',path,'edit',dict(body,purchase_unit_cost='12.50')))['revision']==changed['revision']
    await request(client,'PUT',path,'other-edit',body,409)
    await request(client,'PUT',path,'edit',dict(body,quantity=8),409)
    deleted=await request(client,'DELETE',path,'delete',dict(expected_revision=changed['revision'],reason='Wrong draft line'))
    assert deleted['items']==[]
    assert (await request(client,'DELETE',path,'delete',dict(expected_revision=changed['revision'],reason='Wrong draft line')))['revision']==deleted['revision']
    # Retrying the original scan after deletion cannot resurrect the line.
    replay=await request(client,'POST',f"/receipts/{receipt['id']}/items",'base-add',dict(product_id=pid,quantity=5,purchase_unit_cost='10.00',allocations=[]))
    assert replay['items']==[]
    history=await request(client,'GET',f"/receipts/{receipt['id']}/changes")
    assert [r['operation'] for r in history['items']]==['draft_update','draft_delete']
    async with sessions() as session:
        product=await session.get(Product,pid)
        assert (product.stock_quantity,product.reserved_quantity,product.purchase_unit_cost)==(0,0,None)
        assert await session.scalar(select(func.count()).select_from(InventoryMovement))==0


async def test_posted_correction_preserves_originals_and_updates_cost_atomically(http):
    client,pid,_,_,sessions=http
    receipt=await post(client,await draft(client,pid))
    body=command(receipt,quantity=4,purchase_unit_cost='12.00')
    changed=await correction(client,receipt,quantity=4,purchase_unit_cost='12.00')
    assert changed['items'][0]['quantity']==5 and Decimal(changed['items'][0]['purchase_unit_cost'])==10
    assert changed['items'][0]['effective_quantity']==4
    assert Decimal(changed['effective_purchase_value'])==48
    replay=await request(client,'POST',f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}/corrections",'correction',body)
    assert replay['revision']==changed['revision']
    async with sessions() as session:
        product=await session.get(Product,pid)
        assert (product.stock_quantity,product.reserved_quantity,product.purchase_unit_cost)==(4,0,Decimal('12'))
        assert (product.price,product.shop_price,product.recommended_retail_price)==(Decimal('900'),Decimal('800'),Decimal('1000'))
        movements=(await session.execute(select(InventoryMovement).order_by(InventoryMovement.id))).scalars().all()
        assert [(r.on_hand_delta,r.reserved_delta) for r in movements]==[(5,0),(-1,0)]
        audit=(await session.execute(select(InventoryReceiptChange))).scalar_one()
        assert audit.purchase_value_delta==Decimal('-2') and audit.actor_id==1 and audit.movement_id==movements[-1].id
    # Another correction remains valid after this receipt's own earlier adjustment.
    again=await correction(client,changed,key='again',quantity=3,purchase_unit_cost='11')
    assert again['items'][0]['effective_quantity']==3


async def test_weighted_post_and_old_receipt_correction_do_not_clobber_latest_cost(http):
    client,pid,_,_,sessions=http
    receipt=await draft(client,pid,quantity=2,cost='10',key='weighted')
    receipt=await request(client,'POST',f"/receipts/{receipt['id']}/items",'second-lot',dict(product_id=pid,quantity=1,purchase_unit_cost='20',batch_number='second',allocations=[]))
    receipt=await post(client,receipt,'weighted-post')
    async with sessions() as session:
        assert (await session.get(Product,pid)).purchase_unit_cost==Decimal('13.33')
    later=await post(client,await draft(client,pid,quantity=1,cost='30',key='later'),'later-post')
    await post(client,receipt,'weighted-post')
    await correction(client,receipt,key='older-correction',purchase_unit_cost='11')
    async with sessions() as session:
        p=await session.get(Product,pid)
        assert (p.purchase_unit_cost,p.purchase_cost_receipt_id)==(Decimal('30'),later['id'])
    await correction(client,later,key='latest-correction',purchase_unit_cost='31')
    async with sessions() as session:
        assert (await session.get(Product,pid)).purchase_unit_cost==Decimal('31')


@pytest.mark.parametrize('movement', ['shipment','reservation'])
async def test_downstream_flow_blocks_reduction_but_allows_audited_cost_fix(http,movement):
    client,pid,_,_,sessions=http
    receipt=await post(client,await draft(client,pid,quantity=10))
    async with sessions() as session:
        p=(await session.execute(select(Product).where(Product.id==pid).with_for_update())).scalar_one()
        if movement=='shipment':p.stock_quantity-=1
        else:p.reserved_quantity+=1
        session.add(InventoryMovement(product_id=pid,movement_type=movement,quantity=1,
            on_hand_delta=-1 if movement=='shipment' else 0,reserved_delta=1 if movement=='reservation' else 0,
            reference_key=None,reason='Synthetic downstream flow'))
        await session.commit()
    await correction(client,receipt,quantity=9,status=409)
    changed=await correction(client,receipt,key='cost-fix',purchase_unit_cost='9')
    assert Decimal(changed['items'][0]['effective_purchase_unit_cost'])==9


async def test_cost_privacy_and_zero_null_and_roles(http):
    client,pid,state,app,sessions=http
    initial=await request(client,'GET','/stock?search=RECEIPT-TEST')
    assert initial['items'][0]['purchase_unit_cost'] is None
    receipt=await post(client,await draft(client,pid,cost='0'))
    stock=await request(client,'GET','/stock?search=RECEIPT-TEST')
    assert Decimal(stock['items'][0]['purchase_unit_cost'])==0
    lookup=await request(client,'GET','/products/barcode/0012345678901')
    assert Decimal(lookup['purchase_unit_cost'])==0
    # Root policy forbids replacing a bound barcode; start this bind unassigned.
    async with sessions() as session:
        product=await session.get(Product,pid)
        product.barcode=None
        await session.commit()
    bound=await request(client,'PUT',f'/products/{pid}/barcode','bind-cost',{'barcode':'0098765432100'})
    assert Decimal(bound['purchase_unit_cost'])==0
    created=await request(client,'POST','/products','new-card',{'name':'Synthetic new card','barcode':'0011111111111'},201)
    assert created['purchase_unit_cost'] is None
    detail=await client.get(f'/api/v1/backoffice/products/{pid}')
    assert detail.status_code==200,detail.text
    assert Decimal(detail.json()['purchase_unit_cost'])==0
    for url in [f'/api/v1/public/products/{pid}','/api/v1/public/products','/api/v1/public/products/search?q=Synthetic']:
        response=await client.get(url)
        assert response.status_code==200,response.text
        assert 'purchase_unit_cost' not in response.text and 'purchase_cost_receipt_id' not in response.text
    for actor in [2,3]:
        state['actor']=actor
        await correction(client,receipt,purchase_unit_cost='1',status=403)
    app.dependency_overrides.pop(get_current_admin_user)
    response=await client.post(PREFIX+f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}/corrections",json=command(receipt,purchase_unit_cost='1'),headers={'Idempotency-Key':'no-auth'})
    assert response.status_code==401


async def test_concurrent_corrections_serialize_and_duplicate_retry_once(http):
    client,pid,_,_,sessions=http
    receipt=await post(client,await draft(client,pid))
    path=f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}/corrections"
    body=command(receipt,quantity=6)
    results=await asyncio.gather(*[client.post(PREFIX+path,json=body,headers={'Idempotency-Key':'same-command'}) for _ in range(3)])
    assert [r.status_code for r in results]==[200]*3
    async with sessions() as session:
        assert (await session.get(Product,pid)).stock_quantity==6
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptChange))==1
    receipt=results[0].json()
    results=await asyncio.gather(*[client.post(PREFIX+path,json=command(receipt,quantity=q),headers={'Idempotency-Key':f'competing-{q}'}) for q in [7,8]])
    assert sorted(r.status_code for r in results)==[200,409]


async def requirement(sessions,pid,quantity=5):
    async with sessions() as session:
        order=Order(customer_name='Synthetic procurement',total_amount=Decimal('100'),status=OrderStatus.paid)
        line=OrderItem(product_id=pid,quantity=quantity,quantity_from_stock=0,quantity_to_order=quantity,
                       quantity_received_for_order=0,procurement_status=ProcurementStatus.ordered,price=Decimal('20'))
        order.items=[line];session.add(order);await session.commit()
        return line.id


async def test_allocation_replacement_and_posted_allocation_protection(http):
    client,pid,_,_,sessions=http
    oid=await requirement(sessions,pid)
    receipt=await draft(client,pid,allocations=[dict(order_item_id=oid,quantity=3)])
    path=f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}"
    receipt=await request(client,'PUT',path,'replace-allocation',command(receipt,quantity=4,allocations=[dict(order_item_id=oid,quantity=2)]))
    assert receipt['items'][0]['allocations']==[dict(order_item_id=oid,quantity=2)]
    receipt=await post(client,receipt)
    await correction(client,receipt,quantity=1,status=409)
    changed=await correction(client,receipt,key='reduce-unallocated',quantity=2)
    assert changed['items'][0]['effective_quantity']==2
    async with sessions() as session:
        p=await session.get(Product,pid);line=await session.get(OrderItem,oid)
        assert (p.stock_quantity,p.reserved_quantity,line.quantity_received_for_order)==(2,2,2)
    await request(client,'PUT',path,'edit-posted',command(changed,quantity=3,allocations=[]),409)
    await request(client,'DELETE',path,'delete-posted',dict(expected_revision=changed['revision'],reason='Not a draft'),409)


async def test_draft_delete_clears_plans_without_receiving_or_reserving(http):
    client,pid,_,_,sessions=http
    oid=await requirement(sessions,pid)
    receipt=await draft(client,pid,allocations=[dict(order_item_id=oid,quantity=3)])
    await request(client,'DELETE',f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}",'delete',dict(expected_revision=receipt['revision'],reason='Remove planned line'))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptAllocation))==0
        assert (await session.get(OrderItem,oid)).quantity_received_for_order==0
        assert (await session.get(Product,pid)).reserved_quantity==0


async def test_allocation_other_lines_and_terminal_orders_are_revalidated(http):
    client,pid,_,_,sessions=http
    oid=await requirement(sessions,pid,quantity=5)
    receipt=await draft(client,pid,quantity=3,allocations=[dict(order_item_id=oid,quantity=3)])
    receipt=await request(client,'POST',f"/receipts/{receipt['id']}/items",'other-line',dict(product_id=pid,quantity=2,purchase_unit_cost='11',allocations=[dict(order_item_id=oid,quantity=2)]))
    first=receipt['items'][0];path=f"/receipts/{receipt['id']}/items/{first['id']}"
    await request(client,'PUT',path,'over-allocation',command(receipt,quantity=4,allocations=[dict(order_item_id=oid,quantity=4)]),409)
    async with sessions() as session:
        item=await session.get(OrderItem,oid);order=await session.get(Order,item.order_id);order.status=OrderStatus.cancelled;await session.commit()
    await request(client,'PUT',path,'terminal',command(receipt,quantity=3,allocations=[dict(order_item_id=oid,quantity=3)]),409)


async def test_current_cost_weighted_rounding_and_full_audited_reversal(http):
    client,pid,_,_,sessions=http
    receipt=await draft(client,pid,quantity=1,cost='2.34')
    receipt=await request(client,'POST',f"/receipts/{receipt['id']}/items",'rounding-line',dict(product_id=pid,quantity=1,purchase_unit_cost='2.35',allocations=[]))
    receipt=await post(client,receipt)
    async with sessions() as session:assert (await session.get(Product,pid)).purchase_unit_cost==Decimal('2.35')
    receipt=await correction(client,receipt,quantity=0)
    second=receipt['items'][1]
    receipt=await request(client,'POST',f"/receipts/{receipt['id']}/items/{second['id']}/corrections",'reverse-second',dict(expected_revision=receipt['revision'],quantity=0,purchase_unit_cost='2.35',reason='Synthetic full reversal'))
    assert Decimal(receipt['effective_purchase_value'])==0 and len(receipt['items'])==2
    async with sessions() as session:
        p=await session.get(Product,pid)
        assert (p.stock_quantity,p.purchase_unit_cost)==(0,None)
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptItem))==2


async def test_post_racing_edit_has_one_consistent_result(http):
    client,pid,_,_,sessions=http
    receipt=await draft(client,pid)
    path=f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}"
    edited,posted=await asyncio.gather(
        client.put(PREFIX+path,json=command(receipt,quantity=6,purchase_unit_cost='12',allocations=[]),headers={'Idempotency-Key':'race-edit'}),
        client.post(PREFIX+f"/receipts/{receipt['id']}/post",headers={'Idempotency-Key':'race-post'}))
    assert posted.status_code==200,posted.text
    assert edited.status_code in (200,409),edited.text
    current=await request(client,'GET',f"/receipts/{receipt['id']}")
    async with sessions() as session:
        p=await session.get(Product,pid)
        assert (p.stock_quantity,p.purchase_unit_cost)==((6,Decimal('12')) if edited.status_code==200 else (5,Decimal('10')))
        assert current['items'][0]['quantity']==p.stock_quantity
        assert await session.scalar(select(func.count()).select_from(InventoryMovement))==1


async def test_concurrent_different_receipts_and_old_correction_keep_latest_cost(http):
    client,pid,_,_,sessions=http
    first=await post(client,await draft(client,pid,key='first'))
    later=await draft(client,pid,quantity=2,cost='50',key='later')
    corr,newpost=await asyncio.gather(
        client.post(PREFIX+f"/receipts/{first['id']}/items/{first['items'][0]['id']}/corrections",json=command(first,purchase_unit_cost='11'),headers={'Idempotency-Key':'old-concurrent-fix'}),
        client.post(PREFIX+f"/receipts/{later['id']}/post",headers={'Idempotency-Key':'new-concurrent-post'}))
    assert corr.status_code==newpost.status_code==200
    async with sessions() as session:
        p=await session.get(Product,pid)
        assert (p.purchase_unit_cost,p.purchase_cost_receipt_id,p.stock_quantity)==(Decimal('50'),later['id'],7)


async def test_db_guards_protect_original_header_lines_allocations_and_history(http):
    client,pid,_,_,sessions=http
    oid=await requirement(sessions,pid)
    receipt=await post(client,await draft(client,pid,allocations=[dict(order_item_id=oid,quantity=2)]))
    changed=await correction(client,receipt,purchase_unit_cost='11')
    draft_doc=await draft(client,pid,key='target')
    async with sessions() as session:
        allocation=await session.scalar(select(InventoryReceiptAllocation.id))
        audit=await session.scalar(select(InventoryReceiptChange.id))
    rid=receipt['id'];iid=receipt['items'][0]['id'];target=draft_doc['items'][0]['id']
    sqls=[f"UPDATE inventory_receipts SET reason='changed' WHERE id={rid}",
          f'DELETE FROM inventory_receipts WHERE id={rid}',
          f'UPDATE inventory_receipt_items SET quantity=99 WHERE id={iid}',
          f'UPDATE inventory_receipt_items SET receipt_id={draft_doc["id"]} WHERE id={iid}',
          f'DELETE FROM inventory_receipt_items WHERE id={iid}',
          f'UPDATE inventory_receipt_allocations SET quantity=1 WHERE id={allocation}',
          f'UPDATE inventory_receipt_allocations SET receipt_item_id={target} WHERE id={allocation}',
          f"UPDATE inventory_receipt_changes SET reason='changed' WHERE id={audit}",
          f'DELETE FROM inventory_receipt_changes WHERE id={audit}']
    for sql in sqls:
        async with sessions() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(sql));await session.commit()
            await session.rollback()
    replay=await correction(client,receipt,purchase_unit_cost='11')
    assert replay['revision']==changed['revision']


async def test_audit_failure_rolls_back_stock_cost_revision_and_movement(http):
    client,pid,_,app,sessions=http
    receipt=await post(client,await draft(client,pid))
    async with sessions() as session:
        await session.execute(text("CREATE FUNCTION reject_receipt_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic audit failure'; END $$"))
        await session.execute(text('CREATE TRIGGER reject_receipt_audit BEFORE INSERT ON inventory_receipt_changes FOR EACH ROW EXECUTE FUNCTION reject_receipt_audit()'))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app,raise_app_exceptions=False),base_url='http://synthetic.test') as failing:
        response=await failing.post(PREFIX+f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}/corrections",json=command(receipt,quantity=6,purchase_unit_cost='11'),headers={'Idempotency-Key':'atomic'})
        assert response.status_code==500
    async with sessions() as session:
        p=await session.get(Product,pid)
        assert (p.stock_quantity,p.purchase_unit_cost)==(5,Decimal('10'))
        assert (await session.get(InventoryReceipt,receipt['id'])).revision==receipt['revision']
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptChange))==0
        assert await session.scalar(select(func.count()).select_from(InventoryMovement))==1


@pytest.mark.parametrize('body',[
    dict(quantity=True,purchase_unit_cost='10',allocations=[]),
    dict(quantity=2,purchase_unit_cost='-1',allocations=[]),
    dict(quantity=2,purchase_unit_cost='1.001',allocations=[]),
    dict(quantity=2,purchase_unit_cost='NaN',allocations=[]),
    dict(quantity=2,purchase_unit_cost='10'),
    dict(quantity=2,purchase_unit_cost='10',allocations=[],product_id=99),
])
async def test_invalid_draft_input_does_not_change_document(http,body):
    client,pid,_,_,sessions=http
    receipt=await draft(client,pid)
    await request(client,'PUT',f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}",'bad',dict(expected_revision=receipt['revision'],reason='Invalid test',**body),422)
    async with sessions() as session:assert await session.scalar(select(func.count()).select_from(InventoryReceiptChange))==0


async def test_all_legacy_replays_use_corrected_full_projection(http):
    client,pid,_,_,sessions=http
    receipt=await correction(client,await post(client,await draft(client,pid)),quantity=4,purchase_unit_cost='12')
    responses=[await request(client,'POST','/receipts','base-create',{'reason':'Synthetic receipt'},201),
               await request(client,'POST',f"/receipts/{receipt['id']}/items",'base-add',dict(product_id=pid,quantity=5,purchase_unit_cost='10.00',allocations=[])),
               await post(client,receipt)]
    for current in responses:
        assert current['revision']==receipt['revision']
        assert Decimal(current['original_purchase_value'])==50
        assert Decimal(current['effective_purchase_value'])==48
        assert current['items'][0]['effective_quantity']==4
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptChange))==1
        assert (await session.get(Product,pid)).stock_quantity==4


async def test_duplicate_draft_lot_is_rejected_and_wrong_item_is_scoped(http):
    client,pid,_,_,sessions=http
    receipt=await draft(client,pid)
    receipt=await request(client,'POST',f"/receipts/{receipt['id']}/items",'second-cost',dict(product_id=pid,quantity=1,purchase_unit_cost='11',allocations=[]))
    await request(client,'PUT',f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}",'duplicate-lot',command(receipt,purchase_unit_cost='11',allocations=[]),409)
    other=await draft(client,pid,key='other-doc')
    await request(client,'PUT',f"/receipts/{other['id']}/items/{receipt['items'][0]['id']}",'wrong-parent',command(other,quantity=2,allocations=[]),404)


async def test_exact_applied_replay_after_role_revoked_stays_403_uncertain(http):
    client,pid,state,_,sessions=http
    receipt=await post(client,await draft(client,pid))
    body=command(receipt,purchase_unit_cost='11')
    path=f"/receipts/{receipt['id']}/items/{receipt['items'][0]['id']}/corrections"
    await request(client,'POST',path,'role-replay',body)
    state['actor']=2
    await request(client,'POST',path,'role-replay',body,403)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InventoryReceiptChange))==1
        assert (await session.get(Product,pid)).purchase_unit_cost==Decimal('11')


async def test_migration_downgrade_empty_roundtrip_and_protect_used_history(prepared):
    sessions,pid=prepared;engine=sessions.kw['bind']
    async with engine.begin() as connection:
        def roundtrip(sync):
            module=migration_module();module.op=Operations(MigrationContext.configure(sync,opts={'target_metadata':Base.metadata}))
            before=sync.execute(text('SELECT id,price,stock_quantity,reserved_quantity FROM products')).all()
            module.downgrade()
            assert sync.execute(text('SELECT id,price,stock_quantity,reserved_quantity FROM products')).all()==before
            module.upgrade()
        await connection.run_sync(roundtrip)
    # Any initialized cost, including0, prevents destructive rollback.
    async with sessions() as session:
        p=await session.get(Product,pid);p.purchase_unit_cost=Decimal('0');await session.commit()
    async with engine.begin() as connection:
        def refused(sync):
            module=migration_module();module.op=Operations(MigrationContext.configure(sync,opts={'target_metadata':Base.metadata}))
            with pytest.raises(RuntimeError,match='Cannot downgrade'):
                module.downgrade()
        await connection.run_sync(refused)


@pytest.mark.parametrize('endpoint', ['detail','list','create-replay'])
async def test_projection_refreshes_revision_and_values_if_writer_commits_after_initial_read(http,monkeypatch,endpoint):
    client,pid,_,_,sessions=http
    receipt=await post(client,await draft(client,pid))
    original=routes.project_receipts
    active=True
    async def interleave(session,receipts):
        nonlocal active
        if active:
            active=False
            await correction(client,receipt,quantity=4,purchase_unit_cost='12')
        return await original(session,receipts)
    monkeypatch.setattr(routes,'project_receipts',interleave)
    if endpoint=='detail':result=await request(client,'GET',f"/receipts/{receipt['id']}")
    elif endpoint=='list':result=(await request(client,'GET','/receipts'))['items'][0]
    elif endpoint=='create-replay':result=await request(client,'POST','/receipts','base-create',{'reason':'Synthetic receipt'},201)
    else:result=await post(client,receipt)
    assert result['revision']==receipt['revision']+1
    assert result['items'][0]['effective_quantity']==4
    assert Decimal(result['effective_purchase_value'])==48


@pytest.mark.parametrize('reader_kind',['detail','post-replay'])
async def test_projection_read_lock_blocks_writer_until_consistent_document_is_captured(http,monkeypatch,reader_kind):
    from fastapi import Request
    import app.services.inventory_receipt_changes as service
    client,pid,_,app,sessions=http
    receipt=await post(client,await draft(client,pid))
    pids={};pid_ready=asyncio.Event()
    async def tracked(request:Request):
        async with sessions() as session:
            current=await session.scalar(text('SELECT pg_backend_pid()'))
            if request.url.path.endswith('/corrections'):
                pids['writer']=current;pid_ready.set()
            else:pids['reader']=current
            yield session
    app.dependency_overrides[get_db_session]=tracked
    original=service.latest_corrections
    writer=None;active=True
    async def interleave(session,ids):
        nonlocal writer,active
        if active:
            active=False
            writer=asyncio.create_task(correction(client,receipt,quantity=4,purchase_unit_cost='12'))
            await asyncio.wait_for(pid_ready.wait(),timeout=5)
            async def wait_blocked():
                async with sessions() as observer:
                    while not await observer.scalar(text('SELECT :reader = ANY(pg_blocking_pids(:writer))'),pids):
                        await asyncio.sleep(.01)
            await asyncio.wait_for(wait_blocked(),timeout=5)
            assert not writer.done()
        return await original(session,ids)
    monkeypatch.setattr(service,'latest_corrections',interleave)
    result=(await request(client,'GET',f"/receipts/{receipt['id']}")) if reader_kind=='detail' else await post(client,receipt)
    assert result['revision']==receipt['revision']
    assert result['items'][0]['effective_quantity']==5
    assert (await asyncio.wait_for(writer,timeout=5))['revision']==receipt['revision']+1


async def test_legacy_null_cost_adopts_only_proven_latest_receipt_on_explicit_correction(http):
    client,pid,_,_,sessions=http
    first=await post(client,await draft(client,pid,key='legacy-first'))
    latest=await post(client,await draft(client,pid,cost='30',key='legacy-latest'),'legacy-latest-post')
    async with sessions() as session:
        p=await session.get(Product,pid);p.purchase_unit_cost=None;p.purchase_cost_receipt_id=None;await session.commit()
    await correction(client,first,key='legacy-old-fix',purchase_unit_cost='12')
    async with sessions() as session:
        p=await session.get(Product,pid);assert p.purchase_unit_cost is None and p.purchase_cost_receipt_id is None
    await correction(client,latest,key='legacy-latest-fix',purchase_unit_cost='31')
    async with sessions() as session:
        p=await session.get(Product,pid);assert (p.purchase_unit_cost,p.purchase_cost_receipt_id)==(Decimal('31'),latest['id'])


async def test_manual_unowned_cost_is_not_overwritten_by_legacy_correction(http):
    client,pid,_,_,sessions=http
    receipt=await post(client,await draft(client,pid))
    async with sessions() as session:
        p=await session.get(Product,pid);p.purchase_unit_cost=Decimal('0');p.purchase_cost_receipt_id=None;await session.commit()
    await correction(client,receipt,purchase_unit_cost='12')
    async with sessions() as session:
        p=await session.get(Product,pid);assert p.purchase_unit_cost==0 and p.purchase_cost_receipt_id is None
