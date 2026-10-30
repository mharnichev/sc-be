"""Manual ledger tests against migrated disposable PostgreSQL + mono MockTransport."""
import asyncio
from datetime import UTC, datetime, timedelta, date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes.master_rewards import router
from app.core.database import get_db_session
from app.core.security import create_access_token
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.master_rewards import RewardEntry, RewardSettlement, RewardAllocation, RewardAudit, RewardOperation
from app.models.order import Order, OrderItem, OrderHandover
from app.models.product import Product
from app.schemas.master_rewards import PolicyCommand, EntryCommand, ReviewCommand, SettlementPreview, SettlementCommand
from app.schemas.master_mono import MasterMonoCommand
from app.schemas.order import OrderHandoverCreate
from app.services import master_rewards as service, master_mono, local_sales
from tests.test_inventory_integration import checkout_dependencies
from tests.test_shop_fiscal_integration import template_database, database
from tests.test_master_mono_integration import mock_provider, prepared, invoke, receipt

pytestmark = pytest.mark.anyio


@pytest.fixture(scope='module')
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def sale(database, request):
    return await seed_sale(database, getattr(request, "param", {}))


async def seed_sale(database, config=None):
    """Synthetic existing production-authority Shop history, never create an invoice.

    These rows are fixtures, not promoted MockTransport facts. Real canonical
    payment/fulfillment/completion validators verify their immutable links.
    """
    from app.models.order import OrderPayment, OrderStatus, ProcurementStatus
    from app.models.shop_sales import ShopPaymentConfirmation, ShopFulfillmentFact, ShopCompletion
    from app.services.order_documents import digest
    config=config or {}
    now=config.get("sold_at",datetime.now(UTC))
    prices=[Decimal(value) for value in config.get("prices", ["100.00"])]
    total=Decimal(config.get("total",str(sum(prices))))
    amount=service.minor(total)
    reference='synthetic-bank-record-'+uuid4().hex
    async with database() as session:
        for identifier in (2,3):
            if not await session.get(AdminUser,identifier):
                session.add(AdminUser(id=identifier,email=f'master-{identifier}@example.test',hashed_password='unused',is_active=True,is_superuser=False))
                session.add(Master(admin_user_id=identifier,full_name=f'Synthetic master {identifier}'))
        product=Product(name='Synthetic cosmetic',slug=uuid4().hex,price=Decimal('100.00'),stock_quantity=5,reserved_quantity=0,is_active=True)
        session.add(product);await session.flush()
        order=Order(customer_name='Synthetic',channel='shop',simulation=False,status=OrderStatus.completed,
            fulfillment_method='pickup',revision=1,created_by_user_id=1,subtotal_amount=sum(prices),discount_amount=sum(prices)-total,total_amount=total,payment_method='bank_transfer')
        order.items=[OrderItem(product_id=product.id,quantity=1,quantity_from_stock=1,quantity_to_order=0,quantity_received_for_order=0,
            procurement_status=ProcurementStatus.not_required,base_price=price,price=price,total_price=price,discount_amount=Decimal('0.00'),product_name=product.name) for price in prices]
        session.add(order);await session.flush()
        payment=OrderPayment(order_id=order.id,amount_minor=amount,method='bank_transfer',source='shop_manual',evidence=reference,operator_id=1,simulation=False,created_at=now)
        handover=OrderHandover(order_id=order.id,operator_id=1,evidence='synthetic existing handover',simulation=False,created_at=now)
        session.add_all([payment,handover]);await session.flush()
        snapshot=dict(order_id=order.id,payment_id=payment.id,amount_minor=amount,currency='UAH',method='bank_transfer',namespace='manual:fixture',external_reference=reference,source='manual',authority='manual')
        confirmation=ShopPaymentConfirmation(order_id=order.id,payment_id=payment.id,namespace='manual:fixture',external_reference=reference,source='manual',authority='manual',snapshot=snapshot,snapshot_hash=digest(snapshot),operator_id=1,created_at=now)
        snapshot=dict(order_id=order.id,handover_id=handover.id,shipment_id=None,kind='pickup',authority='production',items=[dict(order_item_id=i.id,product_id=i.product_id,quantity=i.quantity) for i in order.items])
        fulfillment=ShopFulfillmentFact(order_id=order.id,handover_id=handover.id,shipment_id=None,kind='pickup',authority='production',snapshot=snapshot,snapshot_hash=digest(snapshot),operator_id=1,created_at=now)
        session.add_all([confirmation,fulfillment]);await session.flush()
        snapshot=dict(order_id=order.id,payment_confirmation_id=confirmation.id,fulfillment_id=fulfillment.id,authority='production')
        session.add(ShopCompletion(order_id=order.id,payment_confirmation_id=confirmation.id,fulfillment_id=fulfillment.id,authority='production',snapshot=snapshot,snapshot_hash=digest(snapshot),operator_id=1,created_at=now))
        await session.commit()
        master=await session.scalar(select(Master).where(Master.admin_user_id==2))
        return order.id,order.items[0].id,master.id


async def command(database, operation, payload, *, key=None, actor_id=1, entry_id=None):
    async with database() as session:
        return await service.command(session, await session.get(AdminUser, actor_id), operation, payload, key or str(uuid4()), entry_id=entry_id)


async def accrue(database, sale, *, rate=1250, key=None):
    async with database() as session:
        policy = await service.policy(session)
    if policy['rate_bps'] is None:
        policy = await command(database, 'policy', PolicyCommand(expected_revision=policy['revision'],rate_bps=rate,reason='explicit synthetic policy'))
    async with database() as session:
        order = await session.get(Order, sale[0])
        payload = EntryCommand(order_id=sale[0],order_item_id=sale[1],master_id=sale[2],expected_order_revision=order.revision,
            expected_policy_revision=policy['revision'],reason='manual synthetic assignment')
    return await command(database, 'entry', payload, key=key), payload


def close_payload(entry, **changes):
    return SettlementCommand(master_id=entry['master_id'],entries=[{'id':entry['id'],'expected_revision':entry['revision']}],
        expected_total_minor=entry['reward_minor'], paid_at=datetime.now(UTC)-timedelta(minutes=1),
        reason='synthetic off-system handover',manual_payment_confirmed=True,**changes)


async def test_policy_unset_and_no_automatic_accrual(database, sale):
    async with database() as session:
        assert (await service.policy(session)) == {'revision':0,'rate_bps':None,'currency':'UAH'}
        assert await session.scalar(select(func.count()).select_from(RewardEntry)) == 0
        candidates = await service.candidates(session, sale[0])
        assert len(candidates['items']) == 1
        revision = candidates['items'][0]['order_revision']
    with pytest.raises(HTTPException) as error:
        await command(database,'entry',EntryCommand(order_id=sale[0],order_item_id=sale[1],master_id=sale[2],
            expected_order_revision=revision,expected_policy_revision=0,reason='manual'))
    assert error.value.detail['code'] == 'policy_not_configured'


async def test_snapshot_idempotency_and_explicit_attribution(database, sale):
    entry, payload = await accrue(database, sale)
    key = str(uuid4())
    # No second accrual even with a fresh key.
    with pytest.raises(HTTPException) as conflict:
        await command(database,'entry',payload,key=key)
    assert conflict.value.detail['code'] == 'entry_exists'
    async with database() as session:
        await service.authorize(session,await session.get(AdminUser,1))
        row = await session.get(Product,entry['product_id'])
        row.price = Decimal('999.00')
        row.name = 'changed catalog'
        master = await session.get(Master,entry['master_id'])
        master.full_name = 'changed profile'
        await session.commit()
    await command(database,'policy',PolicyCommand(expected_revision=1,rate_bps=9000,reason='future entries only'))
    async with database() as session:
        stored = await service.entry_view(session,await session.get(RewardEntry,entry['id']))
        assert stored['base_minor'] == 10000 and stored['rate_bps'] == 1250 and stored['reward_minor'] == 1250
        assert stored['master_name'] != 'changed profile' and stored['product_name'] != 'changed catalog'
        assert await session.scalar(select(func.count()).select_from(RewardAudit)) == 3


async def test_same_key_replays_after_response_loss_and_conflicting_payload_fails(database, sale):
    key = str(uuid4())
    entry, payload = await accrue(database,sale,key=key)
    assert await command(database,'entry',payload,key=key) == entry
    changed = payload.model_copy(update={'reason':'different request'})
    with pytest.raises(HTTPException) as error:
        await command(database,'entry',changed,key=key)
    assert error.value.detail['code'] == 'idempotency_conflict'
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(RewardEntry)) == 1


async def test_concurrent_creation_and_closure(database, sale):
    policy = await command(database,'policy',PolicyCommand(expected_revision=0,rate_bps=1250,reason='manual'))
    async with database() as session:
        revision = (await session.get(Order,sale[0])).revision
    payload=EntryCommand(order_id=sale[0],order_item_id=sale[1],master_id=sale[2],expected_order_revision=revision,
        expected_policy_revision=policy['revision'],reason='manual')
    async def attempt(op,data,**kw):
        try:
            return await command(database,op,data,**kw)
        except HTTPException as exc:
            return exc.status_code
    results = await asyncio.gather(attempt('entry',payload),attempt('entry',payload),return_exceptions=True)
    assert sorted(isinstance(r,dict) for r in results) == [False,True]
    assert 409 in results
    entry=next(r for r in results if isinstance(r,dict))
    closing=close_payload(entry)
    results=await asyncio.gather(attempt('settlement',closing),attempt('settlement',closing),return_exceptions=True)
    assert sorted(isinstance(r,dict) for r in results) == [False,True]
    assert 409 in results
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(RewardSettlement)) == 1
        assert await session.scalar(select(func.count()).select_from(RewardAllocation)) == 1


async def test_closed_settlement_replay_and_review_no_debt(database, sale):
    entry,_=await accrue(database,sale)
    payload=close_payload(entry)
    key=str(uuid4())
    result=await command(database,'settlement',payload,key=key)
    assert await command(database,'settlement',payload,key=key) == result
    marked=await command(database,'review',ReviewCommand(expected_revision=1,needs_review=True,reason='customer dispute after handover'),entry_id=entry['id'])
    assert marked['needs_review'] and marked['status']=='transferred'
    async with database() as session:
        total=(await service.summary(session,await session.get(AdminUser,1)))['items'][0]
        assert total['transferred_minor']==1250 and total['balance_minor']==0
        assert total['pending_minor']==0 and total['held_minor']==0
        assert await session.scalar(select(func.count()).select_from(RewardEntry))==1
        assert await session.scalar(select(func.sum(RewardSettlement.total_minor)))==1250


async def test_review_blocks_close_and_stale_preview_is_rejected(database,sale):
    entry,_=await accrue(database,sale)
    payload=close_payload(entry)
    async with database() as session:
        assert (await service.preview_settlement(session,payload))[0]['total_minor']==1250
    marked=await command(database,'review',ReviewCommand(expected_revision=1,needs_review=True,reason='manual return issue'),entry_id=entry['id'])
    with pytest.raises(HTTPException):
        await command(database,'settlement',payload)
    cleared=await command(database,'review',ReviewCommand(expected_revision=marked['revision'],needs_review=False,reason='documented resolution'),entry_id=entry['id'])
    with pytest.raises(HTTPException) as conflict:
        await command(database,'settlement',payload)
    assert conflict.value.detail['code']=='revision_conflict'
    result=await command(database,'settlement',close_payload(cleared))
    assert result['total_minor']==1250


async def test_own_history_survives_grant_revocation_and_no_reassigned_account_inheritance(database,sale):
    entry,_=await accrue(database,sale)
    async with database() as session:
        master_actor=await session.get(AdminUser,2)
        master_actor.can_record_sales=master_actor.can_accept_qr_payments=False
        master=await session.get(Master,sale[2]);master.is_active=False
        await session.commit()
    async with database() as session:
        own=await service.listing(session,await session.get(AdminUser,2),own=True)
        assert own['items'][0]['id']==entry['id']
        other=await service.listing(session,await session.get(AdminUser,3),own=True)
        assert other['items']==[]
        with pytest.raises(HTTPException):
            await service.listing(session,await session.get(AdminUser,2))
    async with database() as session:
        session.add(AdminUser(id=4,email='new-account@example.test',hashed_password='unused',is_active=True,is_superuser=False))
        await session.flush()
        master=await session.get(Master,sale[2]);master.admin_user_id=4
        await session.commit()
    async with database() as session:
        assert (await service.listing(session,await session.get(AdminUser,4),own=True))['items']==[]
        with pytest.raises(HTTPException):
            await service.listing(session,await session.get(AdminUser,2),own=True)


async def test_inactive_admin_and_stale_auth_denied(database,sale):
    entry,_=await accrue(database,sale)
    async with database() as stale:
        actor=await stale.get(AdminUser,1)
        async with database() as change:
            current=await change.get(AdminUser,1);current.is_active=False;await change.commit()
        with pytest.raises(HTTPException) as denied:
            await service.command(stale,actor,'review',ReviewCommand(expected_revision=1,needs_review=True,reason='manual'),str(uuid4()),entry_id=entry['id'])
        assert denied.value.status_code==403


async def test_db_snapshot_immutability(database,sale):
    entry,_=await accrue(database,sale)
    await command(database,'settlement',close_payload(entry))
    async with database() as session:
        for sql in ('UPDATE master_reward_entries SET reward_minor=1',
                    'DELETE FROM master_reward_entries','UPDATE master_reward_settlements SET total_minor=1',
                    'DELETE FROM master_reward_allocations','DELETE FROM master_reward_audits','DELETE FROM master_reward_operations'):
            with pytest.raises(DBAPIError):
                await session.execute(text(sql))
            await session.rollback()


async def test_api_ownership_validation_private_cache_and_recovery(database,sale):
    entry,payload=await accrue(database,sale)
    app=FastAPI();app.include_router(router,prefix='/api/v1/backoffice')
    async def dependency():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session]=dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://mock.test') as client:
        base='/api/v1/backoffice/master-rewards'
        assert (await client.get(base+'/me/entries')).status_code==401
        headers={'Authorization':'Bearer '+create_access_token('2')}
        response=await client.get(base+'/me/entries?master_id=999',headers=headers)
        assert response.status_code==200 and response.json()['items'][0]['id']==entry['id']
        assert response.headers['Cache-Control']=='private, no-store'
        assert (await client.get(base+'/entries',headers=headers)).status_code==403
        assert (await client.post(base+'/entries',json=payload.model_dump(),headers={**headers,'Idempotency-Key':str(uuid4())})).status_code==403
        admin={'Authorization':'Bearer '+create_access_token('1'),'Idempotency-Key':str(uuid4())}
        response=await client.put(base+'/policy',json={'rate_bps':'confidential-invalid-12345'},headers=admin)
        assert response.status_code==422 and 'confidential' not in response.text
        result=await command(database,'settlement',close_payload(entry),key=admin['Idempotency-Key'])
        recovered=await client.get(base+'/operations/'+admin['Idempotency-Key'],headers=admin)
        assert recovered.json()['response']==result
        admin2=AdminUser(id=5,email='other-admin@example.test',hashed_password='unused',is_active=True,is_superuser=True)
        async with database() as session:
            session.add(admin2);await session.commit()
        assert (await client.get(base+'/operations/'+admin['Idempotency-Key'],headers={'Authorization':'Bearer '+create_access_token('5')})).status_code==404


def test_exact_rounding_and_strict_confirmation():
    assert service.reward(5,5000)==3
    assert service.reward(15,3333)==5
    assert service.reward(1,1)==0
    assert service.reward(9999999999,10000)==9999999999
    assert service.minor(Decimal('13.57'))==1357
    with pytest.raises(ValidationError):
        SettlementCommand(master_id=1,entries=[{'id':1,'expected_revision':1}],expected_total_minor=1,
            paid_at=datetime.now(UTC),reason='manual',manual_payment_confirmed=1)
    with pytest.raises(ValidationError):
        PolicyCommand(expected_revision=0,rate_bps=10.5,reason='manual')
    with pytest.raises(ValidationError):
        SettlementPreview(master_id=1,entries=[{'id':1,'expected_revision':1}]*2,expected_total_minor=1)


async def test_production_shop_completion_authority_and_test_rejection():
    """Actual canonical contract uses production/test, NOT provider/manual."""
    from types import SimpleNamespace as NS
    from app.services.order_documents import digest
    from app.models.order import OrderStatus
    from app.services import shop_sales
    order=NS(id=91,channel='shop',simulation=False,status=OrderStatus.completed,total_amount=Decimal('100.00'))
    order.items=[NS(id=92,product_id=93,quantity=1,price=Decimal('100.00'),total_price=Decimal('100.00'))]
    order.payments=[NS(id=94,order_id=91,amount_minor=10000,simulation=False,method='bank_transfer',source='shop_manual',evidence='synthetic-confirmed-bank-record',operator_id=1)]
    order.handovers=[NS(id=95,order_id=91,simulation=False,operator_id=1)]
    payment_snapshot=dict(order_id=91,payment_id=94,amount_minor=10000,currency='UAH',method='bank_transfer',authority='manual',namespace='manual:bank',external_reference='synthetic-confirmed-bank-record',source='manual')
    order.shop_payment_confirmation=NS(id=96,order_id=91,payment_id=94,source='manual',authority='manual',namespace='manual:bank',external_reference='synthetic-confirmed-bank-record',snapshot=payment_snapshot,snapshot_hash=digest(payment_snapshot),operator_id=1)
    fulfillment_snapshot=dict(order_id=91,handover_id=95,authority='production',kind='pickup',shipment_id=None,items=[dict(order_item_id=92,product_id=93,quantity=1)])
    order.shop_fulfillment_fact=NS(id=97,order_id=91,handover_id=95,authority='production',kind='pickup',shipment_id=None,snapshot=fulfillment_snapshot,snapshot_hash=digest(fulfillment_snapshot),operator_id=1)
    completion_snapshot=dict(order_id=91,payment_confirmation_id=96,fulfillment_id=97,authority='production')
    order.shop_completion=NS(id=98,order_id=91,payment_confirmation_id=96,fulfillment_id=97,authority='production',snapshot=completion_snapshot,snapshot_hash=digest(completion_snapshot))
    class Session:
        async def scalar(self,_):
            return None
    assert shop_sales.canonical_completion(order) is order.shop_completion
    assert await service.qualify(Session(),order) is order.handovers[0]
    order.shop_completion.authority='test'
    completion_snapshot['authority']='test'
    order.shop_completion.snapshot_hash=digest(completion_snapshot)
    assert shop_sales.canonical_completion(order) is order.shop_completion
    with pytest.raises(HTTPException) as denied:
        await service.qualify(Session(),order)
    assert denied.value.detail['code']=='sale_not_eligible'
    order.shop_completion.authority='production'
    completion_snapshot['authority']='production'
    order.shop_completion.snapshot_hash=digest(completion_snapshot)
    order.shop_payment_confirmation.snapshot_hash='tampered'
    with pytest.raises(HTTPException):
        await service.qualify(Session(),order)


def test_order_discount_is_allocated_exactly_with_stable_remainder():
    from types import SimpleNamespace as NS
    order=NS(total_amount=Decimal('9.96'),items=[
        NS(id=3,quantity=1,price=Decimal('2.01'),total_price=Decimal('2.01')),
        NS(id=1,quantity=1,price=Decimal('3.33'),total_price=Decimal('3.33')),
        NS(id=2,quantity=1,price=Decimal('4.66'),total_price=Decimal('4.66'))])
    bases=service.net_item_bases(order)
    assert bases=={1:332,2:464,3:200}
    assert sum(bases.values())==996
    assert service.net_item_bases(NS(total_amount=order.total_amount,items=list(reversed(order.items))))==bases
    # Discount one kopeck across identical lines; lowest ID wins the remainder.
    order=NS(total_amount=Decimal('0.03'),items=[NS(id=n,quantity=1,price=Decimal('0.02'),total_price=Decimal('0.02')) for n in (2,1)])
    assert service.net_item_bases(order)=={1:1,2:2}


@pytest.mark.parametrize('sale',[{'prices':['3.33','4.66','2.01'],'total':'9.96'}],indirect=True)
async def test_discounted_multiline_accrual_snapshots(database,sale):
    policy=await command(database,'policy',PolicyCommand(expected_revision=0,rate_bps=3333,reason='explicit rate'))
    async with database() as session:
        choices=(await service.candidates(session,sale[0]))['items']
    assert [i['base_minor'] for i in choices]==[332,464,200]
    entries=[]
    for choice in choices:
        entries.append(await command(database,'entry',EntryCommand(order_id=choice['order_id'],order_item_id=choice['order_item_id'],
            master_id=sale[2],expected_order_revision=choice['order_revision'],expected_policy_revision=policy['revision'],reason='manual item')))
    assert sum(i['base_minor'] for i in entries)==996
    assert [i['reward_minor'] for i in entries]==[111,155,67]
    await command(database,'policy',PolicyCommand(expected_revision=1,rate_bps=9000,reason='future only'))
    async with database() as session:
        product=await session.get(Product,entries[0]['product_id']);product.price=Decimal('999.00');await session.commit()
    async with database() as session:
        stored=(await service.listing(session,await session.get(AdminUser,1)))['items']
        assert sum(i['base_minor'] for i in stored)==996
        assert sum(i['reward_minor'] for i in stored)==333
        assert all(i['rate_bps']==3333 for i in stored)


@pytest.mark.parametrize('sale',[{'sold_at':datetime(2026,10,24,21,30,tzinfo=UTC)}],indirect=True)
async def test_sale_period_is_kyiv_inclusive_and_separate_from_paid_date(database,sale):
    entry,_=await accrue(database,sale)
    await command(database,'settlement',close_payload(entry))
    async with database() as session:
        actor=await session.get(AdminUser,1)
        assert (await service.listing(session,actor,date_from=date(2026,10,25),date_to=date(2026,10,25)))['total']==1
        assert (await service.listing(session,actor,date_to=date(2026,10,24)))['total']==0
        assert (await service.listing(session,actor,settlements=True,date_from=date(2026,10,25),date_to=date(2026,10,25)))['total']==0
        assert (await service.summary(session,actor,date_from=date(2026,10,25),date_to=date(2026,10,25)))['items'][0]['transferred_minor']==1250


async def test_wrong_total_policy_revision_order_revision_and_future_transfer(database,sale):
    entry,payload=await accrue(database,sale)
    for bad in [payload.model_copy(update={'expected_order_revision':999}),payload.model_copy(update={'expected_policy_revision':999})]:
        with pytest.raises(HTTPException):
            await command(database,'entry',bad)
    wrong=close_payload(entry).model_copy(update={'expected_total_minor':1})
    with pytest.raises(HTTPException) as mismatch:
        await command(database,'settlement',wrong)
    assert mismatch.value.detail['code']=='total_mismatch'
    future=close_payload(entry).model_copy(update={'paid_at':datetime.now(UTC)+timedelta(days=1)})
    with pytest.raises(HTTPException):
        await command(database,'settlement',future)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(RewardSettlement))==0


async def test_downgrade_refuses_private_history(database,sale):
    from alembic import command as migration
    from alembic.config import Config
    await accrue(database,sale)
    with pytest.raises(RuntimeError,match='Private reward history'):
        await asyncio.to_thread(migration.downgrade,Config('alembic.ini'),'0143_auto_checkout_checkpoint')
    async with database() as session:
        assert await session.scalar(text('SELECT version_num FROM alembic_version'))=='0144_master_rewards'
        assert await session.scalar(select(func.count()).select_from(RewardEntry))==1


async def wait_for_lock(database,pid):
    async def observed():
        while True:
            async with database() as probe:
                waiting=await probe.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),{'pid':pid})
            if waiting:
                return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(observed(),3)


async def test_accrual_matches_existing_return_item_then_order_locking(database,sale):
    from app.services import shop_sales
    await command(database,'policy',PolicyCommand(expected_revision=0,rate_bps=1250,reason='manual'))
    async with database() as session:
        revision=(await session.get(Order,sale[0])).revision
    payload=EntryCommand(order_id=sale[0],order_item_id=sale[1],master_id=sale[2],expected_order_revision=revision,expected_policy_revision=1,reason='manual')
    async with database() as returning, database() as accrual:
        await returning.execute(select(OrderItem.id).where(OrderItem.order_id==sale[0]).with_for_update())
        pid=await accrual.scalar(text('SELECT pg_backend_pid()'))
        actor=await accrual.get(AdminUser,1)
        task=asyncio.create_task(service.command(accrual,actor,'entry',payload,str(uuid4())))
        try:
            await wait_for_lock(database,pid)
            # If accrual held Order while waiting for the item/FK, NOWAIT fails.
            await returning.execute(select(Order.id).where(Order.id==sale[0]).with_for_update(nowait=True))
            await shop_sales.load_order(returning,sale[0],lock=True)
        finally:
            await returning.rollback()
            result=await asyncio.wait_for(task,3)
        assert result['order_item_id']==sale[1]


async def test_close_waits_for_return_lock_and_rejects_committed_dispute(database,sale):
    entry,_=await accrue(database,sale)
    payload=close_payload(entry)
    async with database() as returning, database() as closing:
        await returning.execute(select(OrderItem.id).where(OrderItem.order_id==sale[0]).with_for_update())
        pid=await closing.scalar(text('SELECT pg_backend_pid()'))
        actor=await closing.get(AdminUser,1)
        async def run_close():
            try:
                return await service.command(closing,actor,'settlement',payload,str(uuid4()))
            except HTTPException as exc:
                return exc.status_code
        task=asyncio.create_task(run_close())
        try:
            await wait_for_lock(database,pid)
            await returning.execute(select(Order.id).where(Order.id==sale[0]).with_for_update(nowait=True))
            await service.command(returning,await returning.get(AdminUser,1),'review',
                ReviewCommand(expected_revision=1,needs_review=True,reason='manual return/dispute'),str(uuid4()),entry_id=entry['id'])
        finally:
            await returning.rollback()
            result=await asyncio.wait_for(task,3)
        assert result==409
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(RewardSettlement))==0
        assert (await session.get(RewardEntry,entry['id'])).needs_review


async def test_multisale_closure_locks_all_items_before_all_orders(database,sale):
    from sqlalchemy import event
    first,_=await accrue(database,sale)
    second_sale=await seed_sale(database)
    second,_=await accrue(database,second_sale)
    statements=[]
    engine=database.kw['bind']
    def observe(connection,cursor,statement,parameters,context,executemany):
        if 'FOR UPDATE' in statement and ('FROM order_items' in statement or 'FROM orders' in statement):
            statements.append(statement)
    event.listen(engine.sync_engine,'before_cursor_execute',observe)
    try:
        async with database() as session:
            await service.lock_orders(session,[second['order_id'],first['order_id']])
            assert len(statements)==2
            assert 'FROM order_items' in statements[0] and 'ORDER BY order_items.id' in statements[0]
            assert 'FROM orders' in statements[1] and 'ORDER BY orders.id' in statements[1]
    finally:
        event.remove(engine.sync_engine,'before_cursor_execute',observe)
    result=await command(database,'settlement',SettlementCommand(master_id=first['master_id'],
        entries=[{'id':i['id'],'expected_revision':i['revision']} for i in (second,first)],
        expected_total_minor=2500,paid_at=datetime.now(UTC),reason='two existing sales, manual',manual_payment_confirmed=True))
    assert sorted(result['entry_ids'])==sorted([first['id'],second['id']])
    assert result['total_minor']==2500


@pytest.fixture
async def production_pos_sale(database,checkout_dependencies,mock_provider,monkeypatch):
    """Exercise real production authority/parser paths with an offline HTTP client.

    Gateway has transport=None and real production guards. ONLY AsyncClient's
    wire boundary is replaced by existing mono fixture MockTransport; no fact
    is relabelled or mutated after acceptance, and no real socket is permitted.
    """
    from app.core.config import settings
    from app.models.master_mono import MasterMonoBinding
    transport=master_mono.gateway().transport
    original_client=httpx.AsyncClient
    original_gateway=mock_provider['gateway_factory']
    def fixture_client(*args,**kwargs):
        assert kwargs.get('transport') is None
        return original_client(*args,**{**kwargs,'transport':transport})
    monkeypatch.setattr(httpx,'AsyncClient',fixture_client)
    monkeypatch.setattr(master_mono,'gateway',lambda:original_gateway())
    monkeypatch.setattr(settings,'app_env','production')
    monkeypatch.setattr(settings,'master_monobank_live_transport_enabled',True)
    assert not master_mono.gateway().test_only
    _,order_id,started=await prepared(database,mock_provider)
    mock_provider.update(status='success',checks=[receipt()])
    accepted=await invoke(database,master_mono.refresh,order_id,MasterMonoCommand(expected_revision=started.revision),str(uuid4()))
    async with database() as session:
        result=await local_sales.command(session,await session.get(AdminUser,2),order_id,
            OrderHandoverCreate(expected_revision=accepted.revision,evidence='synthetic POS handover'),str(uuid4()),handover=True)
        binding=await session.scalar(select(MasterMonoBinding).where(MasterMonoBinding.order_id==order_id))
        assert binding.authority=='provider' and binding.transport=='https'
        master=await session.scalar(select(Master).where(Master.admin_user_id==3))
        return (order_id,result.items[0].id,master.id),mock_provider


async def test_production_pos_manual_admission_no_provider_io_and_provenance_rejection(database,production_pos_sale,monkeypatch):
    from copy import deepcopy
    sale,state=production_pos_sale
    count=len(state['calls'])
    def no_provider_io(*args,**kwargs):
        raise AssertionError('Manual rewards must not consult providers')
    monkeypatch.setattr(httpx,'AsyncClient',no_provider_io)
    async with database() as session:
        order=await service.load_order(session,sale[0])
        assert master_mono.canonical_payment(order)
        assert master_mono.accepted_receipt(order)
        assert await service.qualify(session,order) is order.handovers[0]
        for kind in ('test','transport','simulation'):
            rejected=deepcopy(order)
            if kind=='test':
                rejected.master_mono_binding.authority='test'
            elif kind=='transport':
                rejected.master_mono_binding.transport='mock_http'
            else:
                rejected.simulation=True
            with pytest.raises(HTTPException) as invalid:
                await service.qualify(session,rejected)
            assert invalid.value.detail['code']=='sale_not_eligible'
    entry,_=await accrue(database,sale,rate=5000)
    assert entry['reward_minor']==5000
    # Admin's explicit seller is user3, whereas original POS creator was user2.
    async with database() as session:
        assert (await service.listing(session,await session.get(AdminUser,2),own=True))['items']==[]
        assert (await service.listing(session,await session.get(AdminUser,3),own=True))['items'][0]['id']==entry['id']
    assert len(state['calls'])==count
