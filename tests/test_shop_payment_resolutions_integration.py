"""R4 actual disposable PG, GET-only bank mock and actual Checkbox HTTP mock."""
import asyncio
from datetime import UTC,datetime
from hashlib import sha256
from uuid import uuid4
import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select,func,text
from sqlalchemy.exc import DBAPIError
from app.core.config import settings
from app.models.order import OrderStatus,OrderPayment
from app.models.product import Product
from app.models.inventory import InventoryMovement,InventoryMovementType
from app.models.shop_checkbox import ShopCheckboxPhysicalReceipt
from app.models.shop_payment_resolutions import ShopPaymentResolutionCase as Case
from app.schemas import shop_payment_resolutions as dto,shop_payments as pay_dto,shop_checkbox as cb_dto
from app.services import shop_payment_resolutions as service,shop_payments as payments,shop_sales,monobank_provider
from tests import test_shop_checkbox_integration as cb
from tests.monobank_http_stub import MonobankHTTPStub
from tests.test_shop_fiscal_integration import template_database,database,checkout_dependencies
from tests.test_shop_sales_integration import seed
pytestmark=pytest.mark.anyio
@pytest.fixture(scope='module')
def anyio_backend():return 'asyncio'
@pytest.fixture(autouse=True)
def flags(monkeypatch):
    checkbox=cb.flags.__wrapped__(monkeypatch)
    monkeypatch.setattr(settings,'shop_payment_resolutions_enabled',True)
    monkeypatch.setattr(settings,'shop_payments_enabled',False)
    monkeypatch.setattr(settings,'shop_np_returns_enabled',False)
    monkeypatch.setattr(settings,'shop_monobank_token',SecretStr('synthetic-only'))
    monkeypatch.setattr(settings,'shop_monobank_profile',dict(id='r4-mono',merchant_id='synthetic-merchant',token_sha256=sha256(b'synthetic-only').hexdigest(),environment='test',external_fiscalization_disabled=True))
    monkeypatch.setattr(settings,'shop_monobank_live_transport_enabled',False)
    monkeypatch.setattr(settings,'shop_payment_resolution_manual_policy',dict(id='r4-bank-doc',approved=True,environment='test',issuer='SyntheticBank',account='SyntheticAccount',document_types=['bank_closure_statement']))
    stub=MonobankHTTPStub();transport=httpx.MockTransport(stub.handle)
    monkeypatch.setattr(payments,'gateway',lambda:monobank_provider.MonobankGateway(transport=transport))
    return stub,checkbox

def assertion(paid=False):
    p=cb.service.gateway().profile if paid else settings.shop_monobank_profile
    return dict(expected_profile_id=p['id'],expected_profile_hash=service.digest(p))
async def open_case(database,order_id,kind):
    order=await cb.call(database,service.read,'order',order_id)
    return await cb.call(database,service.command,order_id,dto.OpenCommand(expected_revision=order.revision,**assertion(kind=='paid_cancellation'),reason='Synthetic explicit pre-handover resolution',goods_in_custody=True),str(uuid4()),kind)
async def paid_case(database):
    await cb.open_shift(database)
    oid,pid=await cb.paid(database,method='bank_transfer')
    return oid,pid,await open_case(database,oid,'paid_cancellation')
async def invoice_case(database,flags,monkeypatch,*,foreign=False,status='expired'):
    await cb.open_shift(database);monkeypatch.setattr(settings,'shop_payments_enabled',True)
    oid,pid=await seed(database,method='bank_transfer')
    state=await cb.call(database,payments.command,oid,pay_dto.InvoiceIntentCommand(expected_revision=1,**assertion()),str(uuid4()),'invoice_intent')
    stub,_=flags
    inv=stub.register(str(uuid4()) if foreign else state.intent.reference,10000,status=status)
    if status!='success':stub.update(inv,finalAmount=0)
    state=await cb.call(database,payments.command,oid,pay_dto.VerifyInvoiceCommand(expected_revision=state.revision,**assertion(),invoice_id=inv),str(uuid4()),'verify')
    return oid,pid,state,await open_case(database,oid,'invoice_review'),inv
async def inspect(database,case,inv):
    return await cb.call(database,service.command,case.id,dto.InspectCommand(expected_revision=case.revision,**assertion(),invoice_id=inv),str(uuid4()),'inspect')
async def close_unpaid(database,case,state,*,return_command=False):
    p=settings.shop_payment_resolution_manual_policy
    e=dict(type='bank_closure_statement',source='Synthetic reconciled bank statement',issuer=p['issuer'],account=p['account'],document_id=uuid4().hex,line_id='1',order_id=case.order_id,
        intent_id=state.intent.id,reference=state.intent.reference,known_invoice_ids=case.known_invoice_ids,verified_at=datetime.now(UTC),verified=True,no_money=True,no_hold=True,all_reference_invoices_closed=True)
    payload=dto.CloseUnpaidCommand(expected_revision=case.revision,**assertion(),expected_manual_policy_id=p['id'],expected_manual_policy_hash=service.digest(p),evidence=e)
    key=str(uuid4())
    result=await cb.call(database,service.command,case.id,payload,key,'close_unpaid')
    return (result,payload,key) if return_command else result
async def full_refund(database,case):
    case=await cb.call(database,service.command,case.id,dto.ClaimCommand(expected_revision=case.revision,**assertion(True),reason='Synthetic full refund'),str(uuid4()),'financial_claim')
    ret=await cb.call(database,cb.service.read,'return',case.financial_return_id)
    assert ret.physical_not_required and ret.physical_receipt is None and not ret.actions['physical_receipt']
    ret=await cb.call(database,cb.service.return_command,ret.id,cb_dto.RefundCommand(expected_revision=ret.revision,external_reference=uuid4().hex,evidence='Synthetic full staff refund'),str(uuid4()),'refund')
    preview=await cb.call(database,cb.service.order_command,case.order_id,cb_dto.RevisionCommand(expected_revision=ret.revision),str(uuid4()),'preview',return_id=ret.id)
    doc=await cb.call(database,cb.service.order_command,case.order_id,cb_dto.ApproveCommand(expected_revision=ret.revision,preview_id=preview.id,confirm=True),str(uuid4()),'approve',return_id=ret.id)
    await cb.process(database,doc.operation)
    return await cb.call(database,service.read,'case',case.id)

async def test_paid_no_sale_full_refund_return_then_once_reserve_release_reports(database,flags,monkeypatch):
    oid,pid,case=await paid_case(database)
    assert case.status=='awaiting_sale'
    async with database() as s:
        p=await s.get(Product,pid);assert p.stock_quantity==5 and p.reserved_quantity==2
    with pytest.raises(HTTPException):
        r=await cb.reg(database);await cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=r.revision,confirm=True),str(uuid4()),'z')
    await cb.sale(database,oid)
    case=await cb.call(database,service.read,'case',case.id)
    case=await full_refund(database,case)
    assert case.status=='ready_to_finalize'
    payload=dto.FinalizeCommand(expected_revision=case.revision,**assertion(True),confirm=True);key=str(uuid4())
    result=await cb.call(database,service.command,case.id,payload,key,'finalize_paid')
    assert result.status=='closed_paid'
    monkeypatch.setattr(settings,'shop_payment_resolutions_enabled',False)
    assert await cb.call(database,service.command,case.id,payload,key,'finalize_paid')==result
    async with database() as s:
        order=await shop_sales.load_order(s,oid);p=await s.get(Product,pid)
        assert order.status==OrderStatus.cancelled and order.payment_status=='simulated_refunded'
        assert p.stock_quantity==5 and p.reserved_quantity==0 and len(order.payments)==1
        assert await s.scalar(select(func.count()).select_from(ShopCheckboxPhysicalReceipt))==0
        assert await s.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.movement_type==InventoryMovementType.customer_return))==0
    r=await cb.reg(database);x=await cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=r.revision,confirm=True),str(uuid4()),'x')
    assert (await cb.process(database,x)).result['totals']==dict(sales_minor=20000,returns_minor=20000,net_minor=0,sale_count=1,return_count=1)
    r=await cb.reg(database);await cb.process(database,await cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=r.revision,confirm=True),str(uuid4()),'z'))

async def test_expired_does_not_close_manual_doc_closes_then_late_success_quarantines(database,flags,monkeypatch):
    oid,pid,state,case,inv=await invoice_case(database,flags,monkeypatch)
    case=await inspect(database,case,inv)
    assert case.status=='review_required' and case.observations[-1]['proof']['status']=='expired'
    case=await close_unpaid(database,case,state)
    assert case.status=='closed_unpaid'
    flags[0].update(inv,status='success')
    case=await inspect(database,case,inv)
    assert case.status=='financial_exception' and case.finalization and case.exceptions
    async with database() as s:
        order=await shop_sales.load_order(s,oid);p=await s.get(Product,pid)
        assert order.status==OrderStatus.cancelled and not order.payments and p.reserved_quantity==0
    r=await cb.reg(database)
    assert case.id in r.reconciliation.unresolved_payment_resolution_ids

async def test_foreign_correction_separate_r2_acceptance_then_paid_cancellation(database,flags,monkeypatch):
    oid,pid,state,case,old=await invoice_case(database,flags,monkeypatch,foreign=True)
    case=await inspect(database,case,old)
    new=flags[0].register(state.intent.reference,10000,status='success')
    case=await inspect(database,case,new)
    case=await cb.call(database,service.command,case.id,dto.CorrectCommand(expected_revision=case.revision,**assertion(),foreign_observation_id=case.observations[0]['id'],replacement_observation_id=case.observations[1]['id']),str(uuid4()),'correct_invoice')
    async with database() as s:
        assert not (await shop_sales.load_order(s,oid)).payments
    current=await cb.call(database,payments.read,oid)
    assert current.intent.invoice_id==old and current.intent.effective_invoice_id==new
    accepted=await cb.call(database,payments.command,oid,pay_dto.VerifyInvoiceCommand(expected_revision=current.revision,**assertion(),invoice_id=new),str(uuid4()),'verify')
    assert accepted.payment and accepted.intent.invoice_id==old
    case=await cb.call(database,service.read,'case',case.id)
    assert case.status=='payment_accepted'
    cancellation=await open_case(database,oid,'paid_cancellation')
    assert cancellation.status=='awaiting_sale'

async def corrected(database,flags,monkeypatch):
    oid,pid,state,case,old=await invoice_case(database,flags,monkeypatch,foreign=True)
    case=await inspect(database,case,old)
    new=flags[0].register(state.intent.reference,10000,status='success')
    case=await inspect(database,case,new)
    case=await cb.call(database,service.command,case.id,dto.CorrectCommand(expected_revision=case.revision,**assertion(),foreign_observation_id=case.observations[0]['id'],replacement_observation_id=case.observations[1]['id']),str(uuid4()),'correct_invoice')
    state=await cb.call(database,payments.read,oid)
    state=await cb.call(database,payments.command,oid,pay_dto.VerifyInvoiceCommand(expected_revision=state.revision,**assertion(),invoice_id=new),str(uuid4()),'verify')
    return oid,pid,state,await cb.call(database,service.read,'case',case.id),old,new

async def test_corrected_canonical_payment_resumes_sale_handover_completion(database,flags,monkeypatch):
    from app.schemas.shop_sales import ShopPhysicalCommand
    oid,pid,state,case,old,new=await corrected(database,flags,monkeypatch)
    await cb.sale(database,oid)
    state=await cb.shop_state(database,oid)
    assert state.actions['handover']
    from tests.test_shop_sales_integration import execute
    state=await execute(database,oid,ShopPhysicalCommand(expected_revision=state.revision,evidence='Synthetic custody handover'),'handover')
    state=await execute(database,oid,ShopPhysicalCommand(expected_revision=state.revision,evidence='Synthetic fulfilled'),'complete')
    assert state.fulfillment and state.completion
    async with database() as s:
        product=await s.get(Product,pid);assert product.stock_quantity==3 and product.reserved_quantity==0

@pytest.mark.parametrize('anomaly',['reversed','surplus'])
async def test_accepted_corrected_payment_new_bank_anomaly_fences_fulfillment_and_z(database,flags,monkeypatch,anomaly):
    oid,pid,state,case,old,new=await corrected(database,flags,monkeypatch)
    if anomaly=='reversed':flags[0].update(new,status='reversed');candidate=new
    else:candidate=flags[0].register(state.intent.reference,10000,status='success')
    case=await inspect(database,case,candidate)
    assert case.status=='financial_exception'
    assert case.exceptions[-1]['reason']==('surplus_payment' if anomaly=='surplus' else 'contradictory_bank_money')
    current=await cb.shop_state(database,oid);assert not current.actions['handover']
    with pytest.raises(HTTPException):await open_case(database,oid,'paid_cancellation')
    async with database() as s:assert len((await shop_sales.load_order(s,oid)).payments)==1
    assert case.id in (await cb.reg(database)).reconciliation.unresolved_payment_resolution_ids

async def test_postclosure_hold_is_unsettled_financial_exception(database,flags,monkeypatch):
    _,_,state,case,inv=await invoice_case(database,flags,monkeypatch,status='hold')
    case=await inspect(database,case,inv)
    case=await close_unpaid(database,case,state)
    case=await inspect(database,case,inv)
    assert case.status=='financial_exception' and case.exceptions[-1]['reason']=='hold_after_closure'

async def test_positive_foreign_only_candidate_can_documentarily_close_empty_known_list(database,flags,monkeypatch):
    _,_,state,case,inv=await invoice_case(database,flags,monkeypatch,foreign=True)
    case=await inspect(database,case,inv)
    assert case.known_invoice_ids==[]
    result=await close_unpaid(database,case,state)
    assert result.status=='closed_unpaid' and result.closure['known_invoice_ids']==[]

@pytest.mark.parametrize('form',['upper','compact','braced','urn'])
async def test_equivalent_uuid_reference_is_never_foreign_correction(database,flags,monkeypatch,form):
    _,_,state,case,old=await invoice_case(database,flags,monkeypatch)
    ref=state.intent.reference
    flags[0].update(old,reference={'upper':ref.upper(),'compact':ref.replace('-',''),'braced':'{'+ref+'}','urn':'urn:uuid:'+ref}[form])
    case=await inspect(database,case,old)
    new=flags[0].register(ref,10000,status='created');flags[0].update(new,finalAmount=0)
    case=await inspect(database,case,new)
    with pytest.raises(HTTPException) as error:
        await cb.call(database,service.command,case.id,dto.CorrectCommand(expected_revision=case.revision,**assertion(),foreign_observation_id=case.observations[0]['id'],replacement_observation_id=case.observations[1]['id']),str(uuid4()),'correct_invoice')
    assert error.value.detail['code']=='resolution_correction_invalid'
    async with database() as s:assert await s.scalar(text('SELECT shop_resolution_reference(:r)'),{'r':flags[0].records[old]['reference']})==ref

async def test_foreign_observation_cannot_override_newer_same_reference_identity(database,flags,monkeypatch):
    _,_,state,case,old=await invoice_case(database,flags,monkeypatch,foreign=True)
    case=await inspect(database,case,old);old_obs=case.observations[0]['id']
    flags[0].update(old,reference=state.intent.reference,status='created')
    case=await inspect(database,case,old)
    new=flags[0].register(state.intent.reference,10000,status='created');flags[0].update(new,finalAmount=0)
    case=await inspect(database,case,new)
    with pytest.raises(HTTPException):
        await cb.call(database,service.command,case.id,dto.CorrectCommand(expected_revision=case.revision,**assertion(),foreign_observation_id=old_obs,replacement_observation_id=case.observations[-1]['id']),str(uuid4()),'correct_invoice')
    assert old in case.known_invoice_ids

async def test_safe_actual_database_before_mock_and_current_actor_after_io(database,flags,monkeypatch):
    from app.models.admin_user import AdminUser
    _,_,_,case,inv=await invoice_case(database,flags,monkeypatch)
    calls=len(flags[0].calls)
    original=shop_sales.test_database_is_safe
    async def unsafe(session):return False
    monkeypatch.setattr(shop_sales,'test_database_is_safe',unsafe)
    with pytest.raises(HTTPException) as error:await inspect(database,case,inv)
    assert error.value.detail['code']=='resolution_test_unsafe' and len(flags[0].calls)==calls
    monkeypatch.setattr(shop_sales,'test_database_is_safe',original)
    async def handler(request):
        async with database() as s:
            actor=await s.get(AdminUser,1);actor.is_active=False;await s.commit()
        return flags[0].handle(request)
    transport=httpx.MockTransport(handler)
    monkeypatch.setattr(payments,'gateway',lambda:monobank_provider.MonobankGateway(transport=transport))
    with pytest.raises(HTTPException) as error:await inspect(database,case,inv)
    assert error.value.status_code==403
    async with database() as s:assert await s.scalar(text('SELECT count(*) FROM shop_payment_resolution_observations'))==0

async def test_stale_profile_and_revision_commands_have_no_effect(database,flags,monkeypatch):
    _,_,_,case,inv=await invoice_case(database,flags,monkeypatch)
    calls=len(flags[0].calls)
    payload=dto.InspectCommand(expected_revision=case.revision,**(assertion()|{'expected_profile_hash':'f'*64}),invoice_id=inv)
    with pytest.raises(HTTPException) as error:await cb.call(database,service.command,case.id,payload,str(uuid4()),'inspect')
    assert error.value.detail['code']=='resolution_profile_conflict' and len(flags[0].calls)==calls
    payload=dto.InspectCommand(expected_revision=case.revision+1,**assertion(),invoice_id=inv)
    with pytest.raises(HTTPException):await cb.call(database,service.command,case.id,payload,str(uuid4()),'inspect')
    async with database() as s:assert await s.scalar(text('SELECT count(*) FROM shop_payment_resolution_observations'))==0

@pytest.mark.parametrize('same_key',[False,True])
async def test_concurrent_open_has_one_case_and_exact_replay(database,flags,same_key):
    await cb.open_shift(database);oid,_=await cb.paid(database,method='bank_transfer')
    order=await cb.call(database,service.read,'order',oid)
    p=dto.OpenCommand(expected_revision=order.revision,**assertion(True),reason='Synthetic concurrent cancellation',goods_in_custody=True);key=str(uuid4())
    results=await asyncio.gather(*(cb.call(database,service.command,oid,p,key if same_key else str(uuid4()),'paid_cancellation') for _ in range(2)),return_exceptions=True)
    if same_key:assert results[0]==results[1] and not isinstance(results[0],Exception)
    else:assert sum(isinstance(r,HTTPException) for r in results)==1
    async with database() as s:assert await s.scalar(select(func.count()).select_from(Case))==1

async def test_paid_case_vs_z_register_fence(database,flags):
    await cb.open_shift(database);oid,_=await cb.paid(database,method='bank_transfer');await cb.sale(database,oid)
    state=await cb.call(database,service.read,'order',oid);reg=await cb.reg(database)
    payload=dto.OpenCommand(expected_revision=state.revision,**assertion(True),reason='Synthetic Z race',goods_in_custody=True)
    results=await asyncio.gather(cb.call(database,service.command,oid,payload,str(uuid4()),'paid_cancellation'),cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=reg.revision,confirm=True),str(uuid4()),'z'),return_exceptions=True)
    assert sum(isinstance(r,HTTPException) for r in results)==1
    assert sum(not isinstance(r,Exception) for r in results)==1

@pytest.mark.parametrize('movement',['shipment','customer_return','reservation_release'])
async def test_raw_stock_alias_and_raw_cancel_cannot_bypass_paid_case(database,flags,movement):
    oid,pid,case=await paid_case(database)
    async with database() as s:
        order=await shop_sales.load_order(s,oid);i=order.items[0]
        s.add(InventoryMovement(product_id=pid,movement_type=InventoryMovementType(movement),quantity=1,on_hand_delta=0 if movement=='reservation_release' else -1 if movement=='shipment' else 1,
            reserved_delta=-1 if movement=='reservation_release' else 0,order_id=None,order_item_id=i.id,performed_by_user_id=1,reference_key=uuid4().hex,reason='Synthetic forbidden alias'))
        with pytest.raises(DBAPIError):await s.commit()
    async with database() as s:
        with pytest.raises(DBAPIError):await s.execute(text("UPDATE orders SET status='cancelled' WHERE id=:o"),{'o':oid});await s.commit()

@pytest.mark.parametrize('field',['verified','no_money','no_hold','all_reference_invoices_closed'])
async def test_strict_bank_attestations_reject_truthy_strings_before_effect(database,flags,monkeypatch,field):
    from pydantic import ValidationError
    _,_,state,case,_=await invoice_case(database,flags,monkeypatch)
    p=settings.shop_payment_resolution_manual_policy
    body=dict(type='bank_closure_statement',source='Synthetic',issuer=p['issuer'],account=p['account'],document_id='doc',line_id='1',order_id=case.order_id,intent_id=state.intent.id,reference=state.intent.reference,known_invoice_ids=case.known_invoice_ids,verified_at=datetime.now(UTC),verified=True,no_money=True,no_hold=True,all_reference_invoices_closed=True)
    body[field]='true'
    with pytest.raises(ValidationError):dto.BankClosureEvidence(**body)

@pytest.mark.parametrize('same_key',[False,True])
async def test_concurrent_finalize_has_one_release_and_exact_replay(database,flags,same_key):
    oid,pid,case=await paid_case(database);await cb.sale(database,oid)
    case=await cb.call(database,service.read,'case',case.id);case=await full_refund(database,case)
    payload=dto.FinalizeCommand(expected_revision=case.revision,**assertion(True),confirm=True);key=str(uuid4())
    result=await asyncio.gather(*(cb.call(database,service.command,case.id,payload,key if same_key else str(uuid4()),'finalize_paid') for _ in range(2)),return_exceptions=True)
    if same_key:assert result[0]==result[1] and not isinstance(result[0],Exception)
    else:assert sum(isinstance(r,HTTPException) for r in result)==1
    async with database() as s:
        p=await s.get(Product,pid);assert p.stock_quantity==5 and p.reserved_quantity==0
        assert await s.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.reference_key.like('shop-resolution:%')))==1

async def test_manual_closure_requires_finance_rights_and_revoked_exact_replay_denied(database,flags,monkeypatch):
    from app.models.admin_user import AdminUser
    oid,_,state,case,_=await invoice_case(database,flags,monkeypatch)
    async with database() as s:
        a=await s.get(AdminUser,1);a.is_superuser=False;a.can_record_sales=True;a.can_manage_fiscal_settings=False;await s.commit()
    with pytest.raises(HTTPException) as error:await close_unpaid(database,case,state)
    assert error.value.status_code==403
    async with database() as s:
        a=await s.get(AdminUser,1);a.can_manage_fiscal_settings=True;await s.commit()
    result,payload,key=await close_unpaid(database,case,state,return_command=True)
    monkeypatch.setattr(settings,'shop_payment_resolutions_enabled',False)
    assert await cb.call(database,service.command,case.id,payload,key,'close_unpaid')==result
    async with database() as s:
        a=await s.get(AdminUser,1);a.can_manage_fiscal_settings=False;await s.commit()
    with pytest.raises(HTTPException) as error:
        await cb.call(database,service.command,case.id,payload,key,'close_unpaid')
    assert error.value.status_code==403

async def test_preparing_np_ttn_allowed_but_historical_travel_blocks_cancellation(database,flags):
    from tests.test_shop_sales_integration import attach,manual,execute
    from app.models.shipment import Shipment,TrackingEvent
    await cb.open_shift(database);oid,_=await seed(database,kind='nova_poshta',method='bank_transfer')
    sid,number=await attach(database,oid)
    await execute(database,oid,manual(method='bank_transfer'),'manual_payment')
    async with database() as s:
        ship=await s.get(Shipment,sid);ship.tracking_status='preparing';ship.provider_event_at=datetime.now(UTC)
        s.add(TrackingEvent(shipment_id=sid,event_key=uuid4().hex,raw_status='1',normalized_status='preparing',event_at=datetime.now(UTC),observed_at=datetime.now(UTC)));await s.commit()
    case=await open_case(database,oid,'paid_cancellation');assert case.status=='awaiting_sale'

async def test_latest_preparing_cannot_hide_historical_carrier_travel(database,flags):
    from tests.test_shop_sales_integration import attach,manual,execute
    from app.models.shipment import Shipment,TrackingEvent
    await cb.open_shift(database);oid,_=await seed(database,kind='nova_poshta',method='bank_transfer');sid,_=await attach(database,oid)
    await execute(database,oid,manual(method='bank_transfer'),'manual_payment')
    async with database() as s:
        ship=await s.get(Shipment,sid);ship.tracking_status='preparing'
        s.add(TrackingEvent(shipment_id=sid,event_key=uuid4().hex,raw_status='5',normalized_status='in_transit',event_at=datetime.now(UTC),observed_at=datetime.now(UTC)));await s.commit()
    with pytest.raises(HTTPException) as error:await open_case(database,oid,'paid_cancellation')
    assert error.value.detail['code']=='handover_or_carrier_progress'
    async with database() as s:assert not await s.scalar(text('SELECT shop_resolution_no_handover(:o)'),{'o':oid})

async def test_allocated_procurement_receipt_keeps_onhand_and_releases_its_reserve(database,flags):
    await cb.open_shift(database);oid,pid=await cb.paid(database,method='bank_transfer')
    async with database() as s:
        order=await shop_sales.load_order(s,oid);i=order.items[0];p=await s.get(Product,pid)
        # Existing two owned units: one came from stock, one from procurement.
        i.quantity_from_stock=1;i.quantity_to_order=1;i.quantity_received_for_order=1;p.stock_quantity+=1
        s.add(InventoryMovement(product_id=pid,movement_type=InventoryMovementType.receipt,quantity=1,on_hand_delta=1,reserved_delta=0,order_id=oid,order_item_id=i.id,performed_by_user_id=1,reference_key=uuid4().hex,reason='Synthetic procurement arrival'));await s.commit()
    case=await open_case(database,oid,'paid_cancellation');await cb.sale(database,oid);case=await cb.call(database,service.read,'case',case.id);case=await full_refund(database,case)
    result=await cb.call(database,service.command,case.id,dto.FinalizeCommand(expected_revision=case.revision,**assertion(True),confirm=True),str(uuid4()),'finalize_paid')
    assert result.status=='closed_paid'
    async with database() as s:
        p=await s.get(Product,pid);assert p.stock_quantity==6 and p.reserved_quantity==0

@pytest.mark.parametrize('field,value',[('verified','true'),('no_money','true'),('no_hold',None),('all_reference_invoices_closed',1),('order_id','string_id')])
async def test_raw_bank_closure_typed_evidence_cannot_forge_final_cancel(database,flags,monkeypatch,field,value):
    from app.models.shop_payment_resolutions import ShopPaymentBankClosure
    _,_,state,case,_=await invoice_case(database,flags,monkeypatch)
    p=settings.shop_payment_resolution_manual_policy
    body=dict(type='bank_closure_statement',source='Synthetic',issuer=p['issuer'],account=p['account'],document_id=uuid4().hex,line_id='1',order_id=case.order_id,intent_id=state.intent.id,reference=state.intent.reference,known_invoice_ids=case.known_invoice_ids,verified_at=datetime.now(UTC).isoformat(),verified=True,no_money=True,no_hold=True,all_reference_invoices_closed=True)
    body[field]=str(case.order_id) if field=='order_id' else value
    async with database() as s:
        s.add(ShopPaymentBankClosure(case_id=case.id,policy=p,policy_hash=service.digest(p),identity=service.digest([body[k].lower() for k in ('issuer','account','document_id','line_id')]),snapshot=body,snapshot_hash=service.digest(body),operator_id=1,created_at=datetime.now(UTC)))
        with pytest.raises(DBAPIError,match='Bank closure requires'):await s.commit()
    async with database() as s:
        assert (await shop_sales.load_order(s,case.order_id)).status!=OrderStatus.cancelled
        assert await s.scalar(text('SELECT count(*) FROM shop_payment_bank_closures'))==0

@pytest.mark.parametrize('forgery',['extra','duplicate','wrong_product'])
async def test_raw_finalization_plan_cannot_release_foreign_or_duplicate_reserves(database,flags,forgery):
    from copy import deepcopy
    from app.models.admin_user import AdminUser
    from app.models.shop_payment_resolutions import ShopPaymentResolutionFinalization
    oid,pid,case=await paid_case(database);await cb.sale(database,oid)
    case=await cb.call(database,service.read,'case',case.id);case=await full_refund(database,case)
    other_oid,other_pid=await seed(database,method='bank_transfer')
    async with database() as s:
        other=await shop_sales.load_order(s,other_oid);other_item=other.items[0]
        foreign_id=other_item.id
    async with database() as s:
        order=await shop_sales.load_order(s,oid,lock=True);stored=await s.get(Case,case.id);actor=await s.get(AdminUser,1)
        item,refund,doc=await service.paid_obligations(s,stored)
        plan=await service.release(s,order,stored,actor)
        if forgery=='duplicate':plan.append(deepcopy(plan[0]))
        elif forgery=='wrong_product':plan[0]['product_id']=other_pid
        else:plan.append(dict(order_item_id=foreign_id,product_id=other_pid,quantity=1,reference_key=f'shop-resolution:{case.id}:item:{foreign_id}:reserve-release'))
        snap=dict(case_id=case.id,order_id=oid,kind=stored.kind,case_hash=stored.snapshot_hash,reserve_release=plan,test_context=True,financial_return_id=item.id,return_document_id=doc.id)
        s.add(ShopPaymentResolutionFinalization(case_id=case.id,order_id=oid,snapshot=snap,snapshot_hash=service.digest(snap),operator_id=1,created_at=datetime.now(UTC)))
        await s.flush();order.status=OrderStatus.cancelled
        with pytest.raises(DBAPIError,match='Finalization requires exact'):await s.commit()
    async with database() as s:
        assert (await s.get(Product,pid)).reserved_quantity==2
        assert (await s.get(Product,other_pid)).reserved_quantity==1
        assert await s.scalar(text('SELECT count(*) FROM shop_payment_resolution_finalizations'))==0

async def test_test_bank_closure_on_provider_intent_cannot_promote_or_inspect_live(database,flags,monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from app.models.admin_user import AdminUser
    from app.models.shop_checkbox import ShopCheckboxBinding,ShopCheckboxRegister
    from app.models.shop_payments import ShopPaymentIntent
    from tests.test_shop_payments_integration import production_checkbox_seam
    _,register_profile=await production_checkbox_seam(database,monkeypatch)
    monkeypatch.setattr(settings,'shop_payments_enabled',True)
    profile=deepcopy(settings.shop_monobank_profile);profile['environment']='production'
    monkeypatch.setattr(settings,'shop_monobank_profile',profile)
    provider=SimpleNamespace(available=True,test_only=False,profile=profile)
    monkeypatch.setattr(payments,'gateway',lambda:provider)
    oid,_=await seed(database,method='bank_transfer');intent_id=str(uuid4());reference=str(uuid4())
    async with database() as s:
        s.add(ShopCheckboxBinding(order_id=oid,register_id=register_profile['id'],profile_hash=service.digest(register_profile),authority='provider',created_at=datetime.now(UTC)))
        s.add(ShopPaymentIntent(id=intent_id,order_id=oid,mode='prepayment',reference=reference,amount_minor=10000,profile_id=profile['id'],profile=profile,profile_hash=service.digest(profile),authority='provider',transport='https',invoice_id='synthetic_unknown',operator_id=1,created_at=datetime.now(UTC)))
        await s.commit()
    case=await open_case(database,oid,'invoice_review');assert not case.test_context
    state=SimpleNamespace(intent=SimpleNamespace(id=intent_id,reference=reference))
    case=await close_unpaid(database,case,state);assert case.test_context and case.status=='closed_unpaid'
    # The production seam deliberately has no inspect_invoice/provider methods.
    # Any transport access would fail the test, rather than issue a request.
    with pytest.raises(HTTPException) as error:await inspect(database,case,'synthetic_unknown')
    assert error.value.detail['code']=='resolution_profile_conflict'
    async with database() as s:
        order=await shop_sales.load_order(s,oid);r=await s.get(ShopCheckboxRegister,register_profile['id'])
        with pytest.raises(HTTPException):await cb.service.binding(s,order,r)
    async with database() as s:
        r=await s.get(ShopCheckboxRegister,register_profile['id']);op=cb.service.new_operation(r,await s.get(AdminUser,1),'sale',{'amount_minor':10000},order_id=oid,provider_uuid=str(uuid4()));s.add(op)
        with pytest.raises(DBAPIError,match='Resolution test context cannot become'):await s.commit()
    monkeypatch.setattr(settings,'shop_payment_resolution_manual_policy',None)
    monkeypatch.setattr(settings,'shop_payment_resolutions_enabled',False)
    async with database() as s:assert service.test_context(await shop_sales.load_order(s,oid))

async def test_http_strict_commands_and_restricted_master_denied(database,flags):
    from fastapi import FastAPI
    from app.api.v1.routes.shop_payment_resolutions import backoffice_router
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    from app.models.admin_user import AdminUser
    from app.models.booking import Master
    await cb.open_shift(database);oid,_=await cb.paid(database,method='bank_transfer')
    app=FastAPI();app.include_router(backoffice_router,prefix='/api/v1/backoffice')
    async def session_override():
        async with database() as s:yield s
    async def actor_override():
        async with database() as s:return await s.get(AdminUser,1)
    app.dependency_overrides[get_db_session]=session_override;app.dependency_overrides[get_current_admin_user]=actor_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://local.test') as client:
        path=f'/api/v1/backoffice/shop-payment-resolutions/orders/{oid}'
        state=await client.get(path);assert state.status_code==200
        body=dict(expected_revision=state.json()['revision'],**assertion(True),reason='Synthetic',goods_in_custody='true')
        assert (await client.post(path+'/paid-cancellation',json=body,headers={'Idempotency-Key':str(uuid4())})).status_code==422
        body['goods_in_custody']=True;body['amount_minor']=1
        assert (await client.post(path+'/paid-cancellation',json=body,headers={'Idempotency-Key':str(uuid4())})).status_code==422
        async with database() as s:
            s.add(Master(admin_user_id=1,full_name='Synthetic restricted master',is_active=True));a=await s.get(AdminUser,1);a.is_superuser=False;a.can_record_sales=True;a.can_manage_fiscal_settings=True;await s.commit()
        assert (await client.get(path)).status_code==403

@pytest.mark.parametrize('unknown',[False,True])
async def test_cancel_case_preserves_original_pending_sale_uuid_recovery(database,flags,unknown):
    oid,document=await cb.queued_sale(database)
    original_uuid=document.provider_uuid
    if unknown:
        flags[1].lose_after='/api/v1/receipts/sell'
        await cb.call(database,cb.service.operation_command,document.operation.id,cb_dto.RevisionCommand(expected_revision=document.operation.revision),str(uuid4()),'process')
        op=await cb.call(database,cb.service.read,'operation',document.operation.id)
        assert op.status=='unknown'
    case=await open_case(database,oid,'paid_cancellation')
    assert case.status=='awaiting_sale' and case.sale_document_id==document.id
    if unknown:
        op=await cb.call(database,cb.service.operation_command,document.operation.id,cb_dto.ReconcileCommand(expected_revision=op.revision),str(uuid4()),'reconcile')
        assert op.acceptance_verified
    else:await cb.process(database,document.operation)
    recovered=await cb.call(database,cb.service.read,'document',document.id)
    assert recovered.provider_uuid==original_uuid and recovered.acceptance_verified
    case=await cb.call(database,service.read,'case',case.id)
    assert case.status=='awaiting_claim'
    assert [w[1] for w in flags[1].state['writes']].count('/api/v1/receipts/sell')==1

@pytest.mark.parametrize('arrival',['after_unknown','before_accepted_reply'])
async def test_original_dispatched_z_recovery_survives_late_bank_exception(database,flags,monkeypatch,arrival):
    from app.models.admin_user import AdminUser
    from app.models.shop_checkbox import ShopCheckboxRegister
    from app.services import checkbox_provider
    oid,_,_,case,_,invoice=await corrected(database,flags,monkeypatch)
    await cb.sale(database,oid)
    row=await cb.reg(database)
    close=await cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=row.revision,confirm=True),str(uuid4()),'z')
    stub=flags[1]
    if arrival=='after_unknown':
        stub.lose_after='/api/v1/shifts/close'
        acknowledgement=await cb.call(database,cb.service.operation_command,close.id,cb_dto.RevisionCommand(expected_revision=close.revision),str(uuid4()),'process')
        assert acknowledgement.status=='unknown'
        flags[0].update(invoice,status='reversed')
        case=await inspect(database,case,invoice)
        op=await cb.call(database,cb.service.read,'operation',close.id)
        assert op.status=='unknown'
        result=await cb.call(database,cb.service.operation_command,close.id,cb_dto.ReconcileCommand(expected_revision=op.revision),str(uuid4()),'reconcile')
        assert result.acceptance_verified
    else:
        injected=False
        async def handle(request):
            nonlocal injected,case
            reply=stub.handle(request)
            if request.method=='POST' and request.url.path=='/api/v1/shifts/close' and not injected:
                injected=True
                flags[0].update(invoice,status='reversed')
                case=await inspect(database,case,invoice)
            return reply
        transport=httpx.MockTransport(handle)
        monkeypatch.setattr(checkbox_provider,'transport_factory',lambda:transport)
        await cb.call(database,cb.service.operation_command,close.id,cb_dto.RevisionCommand(expected_revision=close.revision),str(uuid4()),'process')
        assert injected
    state=await cb.call(database,cb.service.read,'operation',close.id)
    assert state.status=='accepted' and state.acceptance_verified and state.shift_id==close.shift_id
    row=await cb.reg(database)
    assert row.shift_status=='closed' and case.id in row.reconciliation.unresolved_payment_resolution_ids
    assert case.status=='financial_exception'
    assert [w[1] for w in stub.state['writes']].count('/api/v1/shifts/close')==1
    with pytest.raises(HTTPException):
        await cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=row.revision,confirm=True),str(uuid4()),'z')
    async with database() as session:
        stored=await session.get(ShopCheckboxRegister,row.id)
        new_close=cb.service.new_operation(stored,await session.get(AdminUser,1),'z',close.result or {'shift_id':close.shift_id})
        session.add(new_close)
        with pytest.raises(DBAPIError,match='Unresolved payment resolutions block fiscal shift closing'):
            await session.commit()
