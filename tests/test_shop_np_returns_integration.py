"""R3 actual read-only carrier HTTP + Checkbox HTTP mocks on disposable PG."""
import asyncio
from copy import deepcopy
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo
import httpx
import pytest
from fastapi import HTTPException,FastAPI
from pydantic import SecretStr,ValidationError
from sqlalchemy import select,func,text
from sqlalchemy.exc import DBAPIError
from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import OrderPayment,OrderStatus
from app.models.inventory import InventoryMovement,InventoryMovementType
from app.models.product import Product
from app.models.shop_np_returns import ShopNPReturnCase as Case,ShopNPReturnReceipt as Receipt,ShopNPReturnObservation as Observation
from app.models.shop_checkbox import ShopCheckboxPhysicalReceipt as PickupReceipt
from app.schemas import shop_np_returns as dto,shop_checkbox as cb_dto
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import shop_np_returns as service,np_return_provider,shop_sales
from tests import test_shop_checkbox_integration as cb
from tests.np_return_http_stub import MockNPReturn
from tests.test_shop_fiscal_integration import template_database,database,checkout_dependencies  # noqa:F401
from tests.test_shop_sales_integration import seed,execute,manual,attach as outbound,delivered

pytestmark=pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():return "asyncio"


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    checkbox=cb.flags.__wrapped__(monkeypatch)
    monkeypatch.setattr(settings,"shop_payments_enabled",False)
    monkeypatch.setattr(settings,"shop_np_returns_enabled",True)
    monkeypatch.setattr(settings,"shop_np_return_live_transport_enabled",False)
    monkeypatch.setattr(settings,"shop_np_return_api_key",SecretStr("synthetic-np-only"))
    profile=dict(id="isolated-np",token_sha256=sha256(b"synthetic-np-only").hexdigest(),environment="test",approved=True,manual_link_allowed=True)
    monkeypatch.setattr(settings,"shop_np_return_profile",profile)
    stub=MockNPReturn({})
    monkeypatch.setattr(service,"gateway",lambda:np_return_provider.NPReturnGateway(transport=stub.transport))
    return stub,checkbox


def assertion():
    return dict(expected_profile_id=settings.shop_np_return_profile["id"],expected_profile_hash=service.digest(settings.shop_np_return_profile))


def scan(value=None):
    return (value or datetime.now(UTC)).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M:%S")


async def prepared(database,flags,*,paid=True,quantity=2,method="bank_transfer",total=None):
    await cb.open_shift(database)
    order_id,product_id=await seed(database,quantity=quantity,kind="nova_poshta",method=method if paid else "cashOnDelivery")
    if total is not None:
        async with database() as session:
            order=await shop_sales.load_order(session,order_id)
            order.total_amount=Decimal(total);order.discount_amount=order.subtotal_amount-order.total_amount
            await session.commit()
    shipment_id,number=await outbound(database,order_id)
    if paid:
        payload=manual(method=method);payload.amount_minor=int(Decimal(total)*100) if total is not None else quantity*10000
        await execute(database,order_id,payload,"manual_payment")
        sale=await cb.sale(database,order_id)
    else:sale=None
    state=await cb.shop_state(database,order_id)
    dispatched=await execute(database,order_id,ShopPhysicalCommand(expected_revision=state.revision,evidence="Synthetic outbound"),"handover")
    reverse=str(uuid4().int%10**14).zfill(14)
    stub,_=flags
    stub.rows[number]=dict(Number=number,StatusCode="9" if paid else "103",DateScan=scan())
    stub.rows[reverse]=dict(Number=reverse,StatusCode="9",DateScan=scan(),LightReturnNumber=number if paid else "")
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        line=order.items[0]
        return dict(order_id=order_id,product_id=product_id,shipment_id=shipment_id,original_ttn=number,return_ttn=reverse,
            revision=dispatched.revision,item_id=line.id,quantity=quantity,sale=sale)


async def attach_case(database,flags,*,paid=True,quantity=2,selected=None,key=None):
    data=await prepared(database,flags,paid=paid,quantity=quantity)
    link=None if paid else dict(source="Synthetic refusal document",document_id="refusal-1",line_id=uuid4().hex,
        order_id=data["order_id"],original_shipment_id=data["shipment_id"],original_ttn=data["original_ttn"],return_ttn=data["return_ttn"],verified=True)
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return" if paid else "manual_document",manual_link=link,lines=[dict(order_item_id=data["item_id"],quantity=selected or quantity)],reason="Synthetic returned parcel")
    case=await cb.call(database,service.command,data["order_id"],payload,key or str(uuid4()),"attach")
    return data,case,payload


async def receive(database,case,*,disposition="restock",key=None):
    payload=dto.ReceiveCommand(expected_revision=case.revision,**assertion(),contents_verified=True,evidence="Synthetic separate content inspection",
        lines=[dict(order_item_id=l["order_item_id"],quantity=l["quantity"],disposition=disposition) for l in case.lines])
    result=await cb.call(database,service.command,case.id,payload,key or str(uuid4()),"receive")
    return result,payload


async def z_attempt(database):
    register=await cb.reg(database)
    return await cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=register.revision,confirm=True),str(uuid4()),"z")


async def test_paid_partial_parcel_inspection_single_stock_refund_return_cheque_x_z(database,flags):
    data,case,_=await attach_case(database,flags,selected=1)
    assert case.financial_mode=="paid" and case.status=="arrived" and case.test_context
    assert len(flags[0].calls)==1 and not case.receipt
    with pytest.raises(HTTPException) as error:await z_attempt(database)
    assert error.value.detail["code"]=="shift_unsettled"
    case,_=await receive(database,case)
    case=await cb.call(database,service.command,case.id,dto.ClaimCommand(expected_revision=case.revision,**assertion(),reason="Synthetic partial refund"),str(uuid4()),"financial_claim")
    returned=await cb.call(database,cb.service.read,"return",case.financial_return_id)
    assert returned.carrier_receipt_id==case.receipt["id"] and returned.physical_receipt["carrier_receipt_id"]==case.receipt["id"]
    assert not returned.actions["physical_receipt"]
    returned=await cb.call(database,cb.service.return_command,returned.id,cb_dto.RefundCommand(expected_revision=returned.revision,
        external_reference=uuid4().hex,evidence="Synthetic separate staff refund"),str(uuid4()),"refund")
    preview=await cb.call(database,cb.service.order_command,data["order_id"],cb_dto.RevisionCommand(expected_revision=returned.revision),str(uuid4()),"preview",return_id=returned.id)
    document=await cb.call(database,cb.service.order_command,data["order_id"],cb_dto.ApproveCommand(expected_revision=returned.revision,preview_id=preview.id,confirm=True),str(uuid4()),"approve",return_id=returned.id)
    await cb.process(database,document.operation)
    result=await cb.call(database,service.read,"case",case.id)
    assert result.status=="closed_paid"
    async with database() as session:
        assert (await session.get(Product,data["product_id"])).stock_quantity==3
        assert await session.scalar(select(func.count()).select_from(PickupReceipt))==0
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.movement_type==InventoryMovementType.customer_return))==1
        order=await shop_sales.load_order(session,data["order_id"])
        assert order.fulfillment_status=="simulated_np_return_fiscalized"
    register=await cb.reg(database)
    report=await cb.call(database,cb.service.register_command,cb.register_payload(expected_revision=register.revision,confirm=True),str(uuid4()),"x")
    assert (await cb.process(database,report)).result["totals"]==dict(sales_minor=20000,returns_minor=10000,net_minor=10000,sale_count=1,return_count=1)
    await cb.process(database,await z_attempt(database))


async def test_unpaid_refusal_manual_link_physical_receipt_close_no_money_or_fiscal_document(database,flags,monkeypatch):
    data,case,_=await attach_case(database,flags,paid=False)
    assert case.linkage_authority=="manual" and case.observation["proof"]["linkage_verified"] is False
    with pytest.raises(HTTPException):await z_attempt(database)
    case,_=await receive(database,case)
    payload=dto.CloseCommand(expected_revision=case.revision,**assertion(),confirm=True,evidence="Synthetic full unpaid refusal closed")
    key=str(uuid4())
    result=await cb.call(database,service.command,case.id,payload,key,"close_unpaid")
    assert result.status=="closed_unpaid" and result.closure and not result.financial_return_id
    monkeypatch.setattr(settings,"shop_np_returns_enabled",False)
    assert await cb.call(database,service.command,case.id,payload,key,"close_unpaid")==result
    async with database() as session:
        order=await shop_sales.load_order(session,data["order_id"])
        assert order.status==OrderStatus.cancelled and order.payment_status=="unpaid" and order.fulfillment_status=="simulated_np_refusal_closed"
        assert not order.payments and not order.shop_checkbox_documents and not order.shop_checkbox_returns
        assert (await session.get(Product,data["product_id"])).stock_quantity==4
    await cb.process(database,await z_attempt(database))


@pytest.mark.parametrize("same_key",[False,True])
async def test_concurrent_physical_receive_has_one_receipt_and_ledger(database,flags,same_key):
    data,case,_=await attach_case(database,flags)
    payload=dto.ReceiveCommand(expected_revision=case.revision,**assertion(),contents_verified=True,evidence="Synthetic concurrent inspection",
        lines=[dict(order_item_id=data["item_id"],quantity=2,disposition="restock")])
    key=str(uuid4())
    results=await asyncio.gather(*(cb.call(database,service.command,case.id,payload,key if same_key else str(uuid4()),"receive") for _ in range(2)),return_exceptions=True)
    if same_key:assert results[0]==results[1] and not isinstance(results[0],Exception)
    else:assert sum(isinstance(r,HTTPException) for r in results)==1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt))==1
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.movement_type==InventoryMovementType.customer_return))==1


async def test_not_restock_consumes_parcel_capacity_without_stock_or_second_receipt(database,flags):
    data,case,payload=await attach_case(database,flags)
    case,_=await receive(database,case,disposition="not_restock")
    assert case.receipt["lines"][0]["disposition"]=="not_restock"
    async with database() as session:
        assert (await session.get(Product,data["product_id"])).stock_quantity==2
        assert not await session.scalar(select(InventoryMovement.id).where(InventoryMovement.movement_type==InventoryMovementType.customer_return))
    with pytest.raises(HTTPException):await receive(database,case)
    payload.expected_revision=case.order_revision
    with pytest.raises(HTTPException):await cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach")


@pytest.mark.parametrize("failure",["foreign_link","wrong_number","unknown","missing_refusal_date","unpaid_partial"])
async def test_invalid_link_refusal_or_partial_unpaid_never_adopts_parcel(database,flags,failure):
    unpaid=failure in {"missing_refusal_date","unpaid_partial"}
    data=await prepared(database,flags,paid=not unpaid)
    stub,_=flags
    if failure=="foreign_link":stub.rows[data["return_ttn"]]["LightReturnNumber"]="0"*14
    if failure=="wrong_number":stub.rows[data["return_ttn"]]["Number"]="0"*14
    if failure=="unknown":stub.rows[data["return_ttn"]]["StatusCode"]="999"
    if failure=="missing_refusal_date":stub.rows[data["original_ttn"]].pop("DateScan")
    link=dict(source="Synthetic external evidence",document_id="doc",line_id="line",order_id=data["order_id"],original_shipment_id=data["shipment_id"],
        original_ttn=data["original_ttn"],return_ttn=data["return_ttn"],verified=True) if unpaid else None
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="manual_document" if unpaid else "easy_return",manual_link=link,
        lines=[dict(order_item_id=data["item_id"],quantity=1 if failure=="unpaid_partial" else 2)],reason="Synthetic return")
    with pytest.raises(HTTPException):await cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach")
    async with database() as session:assert not await session.scalar(select(Case.id))


async def test_refusal_103_to_102_chain_and_monotonic_reverse_observations(database,flags):
    data,case,_=await attach_case(database,flags,paid=False)
    stub,_=flags
    stub.rows[data["original_ttn"]].update(StatusCode="102",DateScan=scan(datetime.now(UTC)+timedelta(seconds=2)))
    case=await cb.call(database,service.command,case.id,dto.NPCommand(expected_revision=case.revision,**assertion()),str(uuid4()),"refresh")
    assert case.observation["proof"]["original"]["raw_status"]=="102"
    stub.rows[data["return_ttn"]].update(StatusCode="8",DateScan=scan(datetime.now(UTC)-timedelta(days=1)))
    with pytest.raises(HTTPException) as error:
        await cb.call(database,service.command,case.id,dto.NPCommand(expected_revision=case.revision,**assertion()),str(uuid4()),"refresh")
    assert error.value.detail["code"]=="np_return_proof_invalid"


async def test_profile_flagoff_and_current_permissions_guard_new_work_and_replay(database,flags,monkeypatch):
    data,case,payload=await attach_case(database,flags)
    monkeypatch.setattr(settings,"shop_np_returns_enabled",False)
    with pytest.raises(HTTPException) as error:await receive(database,case)
    assert error.value.detail["code"]=="shop_np_returns_disabled"
    monkeypatch.setattr(settings,"shop_np_returns_enabled",True)
    profile=deepcopy(settings.shop_np_return_profile);profile["id"]="different";monkeypatch.setattr(settings,"shop_np_return_profile",profile)
    with pytest.raises(HTTPException) as error:await receive(database,case)
    assert error.value.detail["code"]=="np_return_profile_conflict"
    async with database() as session:
        actor=await session.get(AdminUser,1);actor.is_active=False;await session.commit()
    with pytest.raises(HTTPException) as error:await cb.call(database,service.read,"case",case.id)
    assert error.value.status_code==403


async def test_sql_immutable_proofs_no_second_stock_and_no_new_money_on_active_case(database,flags):
    data,case,_=await attach_case(database,flags)
    for sql in ["UPDATE shop_np_return_cases SET return_ttn='00000000000000' WHERE id=:id",
                "DELETE FROM shop_np_return_cases WHERE id=:id","UPDATE shop_np_return_observations SET proof='null' WHERE case_id=:id"]:
        async with database() as session:
            with pytest.raises(DBAPIError):await session.execute(text(sql),{"id":case.id});await session.commit()
    async with database() as session:
        session.add(OrderPayment(order_id=data["order_id"],amount_minor=1,method="cash",source="legacy",evidence="forged",operator_id=1,simulation=False,created_at=datetime.now(UTC)))
        with pytest.raises(DBAPIError,match="NP return order cannot receive new money"):await session.commit()
    case,_=await receive(database,case)
    async with database() as session:
        session.add(InventoryMovement(product_id=data["product_id"],movement_type=InventoryMovementType.customer_return,quantity=2,on_hand_delta=2,reserved_delta=0,
            order_id=data["order_id"],order_item_id=data["item_id"],performed_by_user_id=1,reference_key="foreign-return-alias",reason="forged second receipt"))
        with pytest.raises(DBAPIError,match="NP returned stock requires its sole canonical"):await session.commit()


def test_manual_link_and_content_attestation_are_strict_ascii():
    with pytest.raises(ValidationError):dto.ManualLink(source="x",document_id="doc",line_id="line",order_id=1,original_shipment_id=1,
        original_ttn="1"*14,return_ttn="2"*14,verified=1)
    with pytest.raises(ValidationError):dto.AttachCommand(expected_revision=1,**dict(expected_profile_id="x",expected_profile_hash="0"*64),
        original_shipment_id=1,return_ttn="١"*14,linkage_kind="easy_return",lines=[dict(order_item_id=1,quantity=1)],reason="x")


async def test_exact_attach_replay_after_lost_ack_flagoff_then_revoked_permission(database,flags,monkeypatch):
    key=str(uuid4());data,case,payload=await attach_case(database,flags,key=key)
    calls=len(flags[0].calls)
    monkeypatch.setattr(settings,"shop_np_returns_enabled",False)
    assert await cb.call(database,service.command,data["order_id"],payload,key,"attach")==case
    assert len(flags[0].calls)==calls
    assert (await cb.call(database,service.read,"case",case.id)).id==case.id
    async with database() as session:
        actor=await session.get(AdminUser,1);actor.is_superuser=False;actor.can_record_sales=True;actor.can_manage_shipments=False;await session.commit()
    with pytest.raises(HTTPException) as error:await cb.call(database,service.command,data["order_id"],payload,key,"attach")
    assert error.value.status_code==403


async def test_safe_actual_database_guard_precedes_carrier_mock(database,flags,monkeypatch):
    _,case,_=await attach_case(database,flags)
    calls=len(flags[0].calls)
    async def unsafe(_session):return False
    monkeypatch.setattr(shop_sales,"test_database_is_safe",unsafe)
    with pytest.raises(HTTPException) as error:
        await cb.call(database,service.command,case.id,dto.NPCommand(expected_revision=case.revision,**assertion()),str(uuid4()),"refresh")
    assert error.value.detail["code"]=="np_return_test_unsafe" and len(flags[0].calls)==calls


async def test_financial_operator_reads_inherited_receipt_and_claims_without_shipment_mutation_rights(database,flags):
    _,case,_=await attach_case(database,flags)
    case,_=await receive(database,case)
    async with database() as session:
        actor=await session.get(AdminUser,1);actor.is_superuser=False;actor.can_record_sales=True
        actor.can_manage_shipments=False;actor.can_post_inventory=False;await session.commit()
    view=await cb.call(database,service.read,"case",case.id)
    assert view.receipt and view.actions["financial_claim"] and not view.actions["receive"]
    result=await cb.call(database,service.command,case.id,dto.ClaimCommand(expected_revision=view.revision,**assertion(),reason="Synthetic finance claim"),str(uuid4()),"financial_claim")
    assert result.financial_return_id


async def test_attach_and_z_queue_serialize_on_original_register(database,flags):
    data=await prepared(database,flags)
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return",lines=[dict(order_item_id=data["item_id"],quantity=1)],reason="Synthetic race parcel")
    results=await asyncio.gather(cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach"),z_attempt(database),return_exceptions=True)
    assert sum(isinstance(r,HTTPException) for r in results)==1,results
    assert not any(write[1]=="/api/v1/shifts/close" for write in flags[1].state["writes"])


async def test_item_only_manual_and_raw_return_cannot_bypass_parcel_stock_owner(database,flags):
    data,case,_=await attach_case(database,flags)
    from app.services.inventory import InventoryService
    async with database() as session:
        with pytest.raises(HTTPException):
            await InventoryService().manual_operation(session,movement_type=InventoryMovementType.customer_return,
                product_id=data["product_id"],quantity=1,reason="Synthetic denied alias",admin_user_id=1,idempotency_key=uuid4().hex,
                order_item_id=data["item_id"])
    async with database() as session:
        session.add(InventoryMovement(product_id=data["product_id"],movement_type=InventoryMovementType.customer_return,quantity=1,on_hand_delta=1,reserved_delta=0,
            order_id=None,order_item_id=data["item_id"],performed_by_user_id=1,reference_key="item-only-alias",reason="forged"))
        with pytest.raises(DBAPIError,match="NP returned stock requires its sole canonical"):await session.commit()


@pytest.mark.parametrize("failure",["verified_string","wrong_profile"])
async def test_raw_observation_insert_requires_typed_exact_proof(database,flags,failure):
    _,case,_=await attach_case(database,flags)
    proof=deepcopy(case.observation["proof"])
    if failure=="verified_string":proof["verified"]="true"
    else:proof["profile_id"]="foreign"
    proof["proof_hash"]=service.digest({k:v for k,v in proof.items() if k!="proof_hash"})
    async with database() as session:
        session.add(Observation(case_id=case.id,proof=proof,proof_canonical=service.canonical(proof),proof_hash=service.digest(proof),operator_id=1,created_at=datetime.now(UTC)))
        with pytest.raises(DBAPIError,match="NP observation requires exact fresh typed authenticated provenance"):await session.commit()


async def test_durable_mock_context_rejects_live_transport_flip_before_io(database,flags,monkeypatch):
    profile=deepcopy(settings.shop_np_return_profile);profile["environment"]="production";monkeypatch.setattr(settings,"shop_np_return_profile",profile)
    _,case,_=await attach_case(database,flags)
    assert case.authority=="test" and case.test_context
    monkeypatch.setattr(settings,"shop_np_return_live_transport_enabled",True)
    monkeypatch.setattr(service,"gateway",lambda:np_return_provider.NPReturnGateway())
    with pytest.raises(HTTPException) as error:
        await cb.call(database,service.command,case.id,dto.NPCommand(expected_revision=case.revision,**assertion()),str(uuid4()),"refresh")
    assert error.value.detail["code"]=="np_return_profile_conflict"
    assert len(flags[0].calls)==1


async def test_current_actor_revoked_during_readonly_carrier_io_cannot_append_proof(database,flags,monkeypatch):
    _,case,_=await attach_case(database,flags)
    stub=flags[0]
    async def handler(request):
        async with database() as session:
            actor=await session.get(AdminUser,1);actor.is_active=False;await session.commit()
        return stub.handle(request)
    transport=httpx.MockTransport(handler)
    monkeypatch.setattr(service,"gateway",lambda:np_return_provider.NPReturnGateway(transport=transport))
    with pytest.raises(HTTPException) as error:
        await cb.call(database,service.command,case.id,dto.NPCommand(expected_revision=case.revision,**assertion()),str(uuid4()),"refresh")
    assert error.value.status_code==403
    async with database() as session:assert await session.scalar(select(func.count()).select_from(Observation))==1


async def test_sql_freezes_original_items_allocations_and_generic_reverse_adoption(database,flags):
    data,case,_=await attach_case(database,flags)
    for sql,params in [("UPDATE order_items SET quantity=3 WHERE id=:id",{"id":data["item_id"]}),
        ("DELETE FROM shipment_items WHERE shipment_id=:id",{"id":data["shipment_id"]}),
        ("UPDATE order_shipments SET tracking_number='00000000000000' WHERE id=:id",{"id":data["shipment_id"]})]:
        async with database() as session:
            with pytest.raises(DBAPIError):await session.execute(text(sql),params);await session.commit()
    from app.models.shipment import Shipment
    async with database() as session:
        session.add(Shipment(order_id=data["order_id"],simulation=False,carrier="nova_poshta",source="attach",active=False,
            operation_uuid=str(uuid4()),provider_reference="33333333333333",tracking_number="33333333333333",status="requires_review",raw_status="",progress=0,
            address_snapshot={},packaging={},created_at=datetime.now(UTC),return_of_shipment_id=data["shipment_id"]))
        with pytest.raises(DBAPIError,match="NP return order cannot adopt an additional shipment"):await session.commit()


async def test_return_of_previous_shift_sale_uses_new_open_shift_and_original_receipt(database,flags):
    data=await prepared(database,flags)
    old_shift=(await cb.reg(database)).shift_id
    await cb.process(database,await z_attempt(database))
    await cb.open_shift(database)
    assert (await cb.reg(database)).shift_id!=old_shift
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return",lines=[dict(order_item_id=data["item_id"],quantity=1)],reason="Synthetic prior-shift return")
    case=await cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach")
    case,_=await receive(database,case)
    case=await cb.call(database,service.command,case.id,dto.ClaimCommand(expected_revision=case.revision,**assertion(),reason="Synthetic refund"),str(uuid4()),"financial_claim")
    returned=await cb.call(database,cb.service.read,"return",case.financial_return_id)
    returned=await cb.call(database,cb.service.return_command,returned.id,cb_dto.RefundCommand(expected_revision=returned.revision,
        external_reference=uuid4().hex,evidence="Synthetic staff refund"),str(uuid4()),"refund")
    preview=await cb.call(database,cb.service.order_command,data["order_id"],cb_dto.RevisionCommand(expected_revision=returned.revision),str(uuid4()),"preview",return_id=returned.id)
    document=await cb.call(database,cb.service.order_command,data["order_id"],cb_dto.ApproveCommand(expected_revision=returned.revision,preview_id=preview.id,confirm=True),str(uuid4()),"approve",return_id=returned.id)
    assert document.shift_id!=old_shift and document.snapshot["related_receipt_id"]==data["sale"].provider_uuid
    await cb.process(database,document.operation)
    report=await cb.process(database,await z_attempt(database))
    assert report.result["z_report"]["totals"]==dict(sales_minor=0,returns_minor=10000,net_minor=-10000,sale_count=0,return_count=1)


async def test_np_claim_cannot_create_second_pickup_physical_receipt_or_stock(database,flags):
    _,case,_=await attach_case(database,flags)
    case,_=await receive(database,case)
    case=await cb.call(database,service.command,case.id,dto.ClaimCommand(expected_revision=case.revision,**assertion(),reason="Synthetic claim"),str(uuid4()),"financial_claim")
    async with database() as session:
        session.add(PickupReceipt(return_id=case.financial_return_id,lines=case.receipt["lines"],evidence="forged duplicate physical receipt",operator_id=1,created_at=datetime.now(UTC)))
        with pytest.raises(DBAPIError,match="NP physical receipt is already canonical"):await session.commit()
    returned=await cb.call(database,cb.service.read,"return",case.financial_return_id)
    with pytest.raises(HTTPException) as error:
        await cb.call(database,cb.service.return_command,returned.id,cb_dto.ReceiptCommand(expected_revision=returned.revision,
            lines=case.receipt["lines"],evidence="duplicate physical"),str(uuid4()),"physical_receipt")
    assert error.value.detail["code"]=="physical_receipt_exists"


async def test_http_attach_get_and_exact_recovery_strict_body_without_main_lifespan(database,flags,monkeypatch):
    from app.api.v1.routes import shop_np_returns as routes
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    app=FastAPI();app.include_router(routes.backoffice_router,prefix="/api/v1/backoffice")
    async def sessions():
        async with database() as session:yield session
    async def actors():
        async with database() as session:return await session.get(AdminUser,1)
    app.dependency_overrides[get_db_session]=sessions;app.dependency_overrides[get_current_admin_user]=actors
    data=await prepared(database,flags)
    body=dict(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return",lines=[dict(order_item_id=data["item_id"],quantity=1)],reason="Synthetic HTTP parcel")
    headers={"Idempotency-Key":str(uuid4())};url=f'/api/v1/backoffice/shop-np-returns/orders/{data["order_id"]}/attach'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://local.test") as client:
        assert (await client.get("/api/v1/backoffice/shop-np-returns/readiness")).status_code==200 and not flags[0].calls
        assert (await client.post(url,json={**body,"financial_mode":"paid"},headers=headers)).status_code==422
        first=await client.post(url,json=body,headers=headers)
        assert first.status_code==200,first.text
        canonical=await client.get('/api/v1/backoffice/shop-np-returns/cases/'+str(first.json()["id"]))
        assert canonical.status_code==200 and canonical.json()["return_ttn"]==body["return_ttn"]
        monkeypatch.setattr(settings,"shop_np_returns_enabled",False)
        retry=await client.post(url,json=body,headers=headers)
        assert retry.status_code==200 and retry.json()==first.json() and len(flags[0].calls)==1


async def test_existing_generic_reverse_history_is_not_adopted(database,flags):
    data=await prepared(database,flags)
    await outbound(database,data["order_id"])
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return",lines=[dict(order_item_id=data["item_id"],quantity=1)],reason="Synthetic forbidden adoption")
    with pytest.raises(HTTPException) as error:await cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach")
    assert error.value.detail["code"]=="np_return_history_conflict" and not flags[0].calls


async def test_sql_receipt_cannot_consume_old_delivery_after_newer_observation(database,flags):
    data,case,_=await attach_case(database,flags)
    old=case.observation["id"]
    flags[0].rows[data["return_ttn"]].update(StatusCode="4",DateScan=scan(datetime.now(UTC)+timedelta(seconds=2)))
    case=await cb.call(database,service.command,case.id,dto.NPCommand(expected_revision=case.revision,**assertion()),str(uuid4()),"refresh")
    with pytest.raises(HTTPException) as error:await receive(database,case)
    assert error.value.detail["code"]=="np_return_delivery_required"
    lines=[dict(order_item_id=data["item_id"],quantity=2,disposition="not_restock")]
    async with database() as session:
        stored=await session.get(Case,case.id)
        snapshot=dict(case_id=case.id,case_hash=stored.snapshot_hash,observation_id=old,lines=lines,contents_verified=True,test_context=True)
        session.add(Receipt(case_id=case.id,observation_id=old,lines=lines,snapshot=snapshot,snapshot_hash=service.digest(snapshot),
            evidence="forged old proof consumption",operator_id=1,created_at=datetime.now(UTC)))
        with pytest.raises(DBAPIError,match="NP physical receipt requires dated return delivery"):await session.commit()


async def test_nonreturn_movement_does_not_take_np_exclusive_order_fence(database,flags):
    data,_,_=await attach_case(database,flags)
    async with database() as holder,database() as writer:
        await holder.execute(text("SELECT id FROM orders WHERE id=:id FOR NO KEY UPDATE"),{"id":data["order_id"]})
        await writer.execute(text("SET LOCAL lock_timeout='300ms'"))
        writer.add(InventoryMovement(product_id=data["product_id"],movement_type=InventoryMovementType.write_off,quantity=1,on_hand_delta=-1,reserved_delta=0,
            order_id=data["order_id"],performed_by_user_id=1,reference_key="synthetic-writeoff-fence-probe",reason="synthetic rolled-back lock probe"))
        await writer.flush()
        await writer.rollback();await holder.rollback()


async def test_case_admission_serializes_racing_generic_shipment_insert(database,flags,monkeypatch):
    from app.models.shipment import Shipment
    data=await prepared(database,flags)
    pending=asyncio.Event();release=asyncio.Event();insert_started=asyncio.Event()
    original_view=service.case_view
    async def paused_view(*args,**kwargs):
        result=await original_view(*args,**kwargs)
        pending.set();await release.wait()
        return result
    monkeypatch.setattr(service,"case_view",paused_view)
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return",lines=[dict(order_item_id=data["item_id"],quantity=1)],reason="Synthetic concurrent parcel")
    admission=asyncio.create_task(cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach"))
    await pending.wait()
    async def generic_insert():
        async with database() as session:
            session.add(Shipment(order_id=data["order_id"],simulation=False,carrier="nova_poshta",source="attach",active=False,
                operation_uuid=str(uuid4()),provider_reference="33333333333333",tracking_number="33333333333333",status="requires_review",raw_status="",progress=0,
                address_snapshot={},packaging={},created_at=datetime.now(UTC),return_of_shipment_id=data["shipment_id"]))
            insert_started.set()
            try:await session.commit()
            except DBAPIError as error:return error
    insertion=asyncio.create_task(generic_insert())
    await insert_started.wait();await asyncio.sleep(0.05);release.set()
    case,error=await asyncio.gather(admission,insertion)
    assert case.id and isinstance(error,DBAPIError) and "NP return order cannot adopt an additional shipment" in str(error)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Shipment).where(Shipment.order_id==data["order_id"]))==1


@pytest.mark.parametrize("database_guard",[False,True])
async def test_fractional_partial_cash_parcel_rejected_before_case_receipt_or_refund(database,flags,monkeypatch,database_guard):
    from app.models.shop_checkbox import ShopCheckboxReturn,ShopCheckboxRefundFact
    data=await prepared(database,flags,quantity=3,method="cash",total="299.00")
    assert data["sale"].snapshot["payment"]["type"]=="CASH" and data["sale"].amount_minor==29900
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return",lines=[dict(order_item_id=data["item_id"],quantity=1)],reason="Synthetic fractional cash return")
    if database_guard:
        # Validate the durable boundary independently of the earlier domain gate.
        monkeypatch.setattr(service,"cash_return_policy",lambda *args:None)
        with pytest.raises(DBAPIError,match="NP cash parcel requires a supported whole-hryvnia refund amount"):
            await cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach")
    else:
        with pytest.raises(HTTPException) as error:await cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach")
        assert error.value.detail["code"]=="checkbox_cash_rounding_unsupported"
    async with database() as session:
        for model in (Case,Observation,Receipt,ShopCheckboxReturn,ShopCheckboxRefundFact):
            assert await session.scalar(select(func.count()).select_from(model))==0
        assert (await session.get(Product,data["product_id"])).stock_quantity==1
        assert not await session.scalar(select(InventoryMovement.id).where(InventoryMovement.movement_type==InventoryMovementType.customer_return))


async def test_fractional_partial_cashless_parcel_retains_exact_kopeck_value(database,flags):
    data=await prepared(database,flags,quantity=3,method="bank_transfer",total="299.00")
    payload=dto.AttachCommand(expected_revision=data["revision"],**assertion(),original_shipment_id=data["shipment_id"],return_ttn=data["return_ttn"],
        linkage_kind="easy_return",lines=[dict(order_item_id=data["item_id"],quantity=1)],reason="Synthetic exact cashless return")
    case=await cb.call(database,service.command,data["order_id"],payload,str(uuid4()),"attach")
    assert case.lines[0]["total_minor"]==9966
