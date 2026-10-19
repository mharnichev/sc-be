"""Only disposable migrated local PostgreSQL. No live carrier or financial I/O."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.api.v1.routes import shop_carrier_returns as routes
from app.core.config import Settings, settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import Order, OrderPayment, OrderIntent
from app.models.product import Product
from app.models.shipment import Shipment, TrackingEvent
from app.models.shop_carrier_returns import (ShopCarrierReturn, ShopCarrierReturnAcceptance,
    ShopCarrierReturnReceipt, ShopCarrierUnpaidClosure)
from app.models.shop_fiscal import ShopFinancialReturn, ShopRefundFact, ShopReturnReceipt, ShopFiscalDocument
from app.models.shop_sales import ShopCompletion
from app.schemas.shop_carrier_returns import CarrierCreate, CarrierReceive, CarrierClose
from app.schemas.shop_fiscal import RevisionCommand, ReturnCommand, ReceiptCommand, ShiftCommand
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import shop_carrier_returns as service, shop_sales, shop_fiscal, np_tracking, customer_tracking
from tests.test_shop_fiscal_integration import (template_database, database, anyio_backend, checkout_dependencies,
    seed, invoke, open_shift, pay, receipt, handover, state, refund)  # noqa:F401
from tests.test_shop_sales_integration import attach

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setattr(settings, "shop_carrier_returns_mock_enabled", True)
    monkeypatch.setattr(settings, "shop_fiscal_mock_enabled", True)
    monkeypatch.setattr(settings, "shop_order_operations_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.UnavailableFiscalPolicy())
    monkeypatch.setattr(shop_fiscal, "adapter", shop_fiscal.LocalShopMockAdapter())
    monkeypatch.setattr(service, "adapter", service.LocalShopReturnAdapter())


async def setup(database, *, paid=True, quantity=2, method=None, total=None):
    await open_shift(database)
    order_id, product_id = await seed(database, kind="nova_poshta", quantity=quantity, total=total,
        method=method or ("bank_transfer" if paid else "cashOnDelivery"))
    shipment_id, _ = await attach(database, order_id)
    sale = None
    if paid:
        await pay(database, order_id)
        sale = await receipt(database, order_id)
    await handover(database, order_id)
    if not paid:
        async with database() as session:
            original = await session.get(Shipment, shipment_id)
            await np_tracking.apply_observation(session, original, np_tracking.Observation(original.tracking_number, "103", datetime.now(UTC)), datetime.now(UTC))
            await session.commit()
    return order_id, product_id, shipment_id, sale


async def create(database, order_id, *, quantity=None, key=None):
    current = await invoke(database, service.read, order_id)
    lines = [{"order_item_id": i["order_item_id"], "quantity": quantity or i["quantity"]} for i in current.eligible_lines]
    return await invoke(database, service.create, order_id, CarrierCreate(expected_revision=current.revision,
        original_shipment_id=current.original_shipment_id, lines=lines, evidence="Synthetic reverse parcel", confirm=True), key or str(uuid4()))


async def canonical(database, order_id, case_id):
    return await invoke(database, service.read, order_id, case_id)


async def refresh(database, row):
    return await invoke(database, service.command, row.order_id, row.id, RevisionCommand(expected_revision=row.revision), str(uuid4()), "refresh")


async def receive(database, row, *, disposition="restock", key=None):
    return await invoke(database, service.command, row.order_id, row.id, CarrierReceive(expected_revision=row.revision,
        lines=[{"order_item_id": i["order_item_id"], "quantity":i["quantity"], "disposition":disposition} for i in row.lines],
        evidence="Synthetic physically received", confirm=True), key or str(uuid4()), "receive")


async def counts(database):
    async with database() as session:
        return {m.__tablename__: await session.scalar(select(func.count()).select_from(m)) for m in (
            ShopCarrierReturn, ShopCarrierReturnAcceptance, ShopCarrierReturnReceipt, ShopCarrierUnpaidClosure,
            ShopFinancialReturn, ShopRefundFact, ShopReturnReceipt, OrderPayment, ShopFiscalDocument, ShopCompletion, InventoryMovement, OrderIntent)}


async def test_paid_partial_parcel_receive_then_b2_link_never_restock_twice(database, checkout_dependencies):
    order_id, product_id, shipment_id, sale = await setup(database)
    acknowledged = await create(database, order_id, quantity=1)
    assert acknowledged.status == "unknown"
    row = await canonical(database, order_id, acknowledged.id)
    assert row.status == "returning" and row.tracking_number.startswith("SHOP-RETURN-")
    row = await refresh(database, row)
    assert row.status == "returned" and row.actions["receive"]
    row = await receive(database, row)
    assert row.status == "received" and row.actions["create_financial_return"]
    current = await state(database, order_id)
    assert current.order.fulfillment_status == "simulated_carrier_return_received"
    reverse = next(s for s in current.order.shipments if s.id == row.return_shipment_id)
    assert reverse.shop_carrier_return_case_id == row.id and reverse.provenance == "shop_carrier_return_test" and reverse.authority == "test"
    before = await counts(database)
    item = row.lines[0]
    financial = await invoke(database, shop_fiscal.order_command, order_id, ReturnCommand(expected_revision=current.revision,
        sale_document_id=sale.id, carrier_receipt_id=row.receipt.id, lines=[{"order_item_id":item["order_item_id"],"quantity":1}],reason="Synthetic return"), str(uuid4()), "create_return")
    assert financial.carrier_receipt_id == row.receipt.id
    financial = await refund(database, financial)
    await receipt(database, order_id, return_id=financial.id)
    financial = await invoke(database, shop_fiscal.read, "return", financial.id)
    financial = await invoke(database, shop_fiscal.return_command, financial.id, ReceiptCommand(expected_revision=financial.revision,
        lines=[{"order_item_id":item["order_item_id"],"quantity":1,"disposition":"restock"}],evidence="Link the physical receipt"), str(uuid4()), "physical_receipt")
    assert financial.physical_receipt.carrier_receipt_id == row.receipt.id
    assert (await counts(database))["inventory_movements"] == before["inventory_movements"]
    register = await invoke(database, shop_fiscal.read, "register")
    assert register.reconciliation.carrier_return_case_ids == [] and register.actions["z"]
    z = await invoke(database, shop_fiscal.shift_command, ShiftCommand(expected_shift_revision=register.shift_revision,confirm=True), str(uuid4()), "z")
    assert z.totals["sales_minor"] == 20000 and z.totals["returns_minor"] == 10000
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (9,0)


@pytest.mark.parametrize("method", ["cashOnDelivery","cardOnDelivery"])
async def test_unpaid_refusal_full_receive_close_no_money_or_second_stock_delta(database, checkout_dependencies, method):
    order_id, product_id, _, _ = await setup(database, paid=False, method=method)
    row = await create(database, order_id)
    row = await canonical(database, order_id, row.id)
    row = await refresh(database, row)
    row = await receive(database, row)
    before = await counts(database)
    closed = await invoke(database, service.command, order_id, row.id, CarrierClose(expected_revision=row.revision,
        evidence="Full unpaid refused parcel reconciled",confirm=True), str(uuid4()), "close_unpaid")
    assert closed.status == "closed_unpaid"
    current = await state(database, order_id)
    assert current.order.status == "cancelled" and current.order.payment_status == "unpaid"
    assert current.order.fulfillment_status == "simulated_unpaid_refusal_closed"
    after = await counts(database)
    assert after["inventory_movements"] == before["inventory_movements"]
    assert after["order_payments"] == after["shop_fiscal_documents"] == after["shop_financial_returns"] == after["shop_completions"] == 0
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity,product.reserved_quantity) == (10,0)


async def test_lost_response_independent_acceptance_reconcile_flagoff_same_uuid(database, checkout_dependencies, monkeypatch):
    order_id, _, _, _ = await setup(database)
    class Lost(service.LocalShopReturnAdapter):
        async def submit(self,*args,**kwargs):
            await super().submit(*args,**kwargs)
            raise TimeoutError("Synthetic response lost after independently committed acceptance")
    monkeypatch.setattr(service,"adapter",Lost())
    current = await invoke(database, service.read, order_id)
    body = CarrierCreate(expected_revision=current.revision,original_shipment_id=current.original_shipment_id,
        lines=[{"order_item_id":i["order_item_id"],"quantity":1} for i in current.eligible_lines],evidence="synthetic",confirm=True)
    key = str(uuid4())
    first, second = await asyncio.gather(*(invoke(database,service.create,order_id,body,key) for _ in range(2)))
    assert first.model_dump()==second.model_dump() and first.status=="unknown"
    monkeypatch.setattr(settings,"shop_carrier_returns_mock_enabled",False)
    monkeypatch.setattr(service,"adapter",service.LocalShopReturnAdapter())
    replay=await invoke(database,service.create,order_id,body,key)
    assert replay.model_dump()==first.model_dump()
    row=await canonical(database,order_id,first.id)
    recovered=await invoke(database,service.command,order_id,row.id,RevisionCommand(expected_revision=row.revision),str(uuid4()),"reconcile")
    assert recovered.status=="returning" and recovered.provider_uuid==first.provider_uuid
    assert (await counts(database))["shop_carrier_return_acceptances"]==1


@pytest.mark.parametrize("condition", ["stale","failed","missing_event","not_refused","partial"])
async def test_unpaid_refusal_invalid_basis_has_zero_claims(database, checkout_dependencies, condition):
    order_id, _, shipment_id, _ = await setup(database, paid=False)
    async with database() as session:
        original = await session.get(Shipment, shipment_id)
        if condition == "stale":
            original.last_checked_at = datetime.now(UTC)-timedelta(days=2)
        elif condition == "failed":
            original.last_attempt_status = "error"
        elif condition == "missing_event":
            original.provider_event_at += timedelta(seconds=1)
        elif condition == "not_refused":
            original.tracking_status = "delivered"
        await session.commit()
    before = await counts(database)
    with pytest.raises(HTTPException) as error:
        await create(database,order_id,quantity=1 if condition=="partial" else None)
    assert error.value.status_code==409 and await counts(database)==before


async def test_unknown_before_acceptance_never_dispatches_replacement_and_blocks_z(database, checkout_dependencies, monkeypatch):
    order_id, _, _, _ = await setup(database)
    class BeforeAcceptance(service.LocalShopReturnAdapter):
        calls=0
        async def submit(self,*args,**kwargs):
            self.calls+=1
            raise TimeoutError("Synthetic failure before acceptance")
    adapter=BeforeAcceptance()
    monkeypatch.setattr(service,"adapter",adapter)
    row=await create(database,order_id,quantity=1)
    projected=await state(database,order_id)
    assert projected.order.fulfillment_status=="simulated_carrier_return_unknown"
    assert projected.order.fulfillment_status not in {"simulated_carrier_returning","simulated_carrier_returned","simulated_carrier_return_received"}
    recovered=await invoke(database,service.command,order_id,row.id,RevisionCommand(expected_revision=row.revision),str(uuid4()),"reconcile")
    assert recovered.status=="unknown" and adapter.calls==1
    with pytest.raises(HTTPException) as error:
        await create(database,order_id,quantity=1)
    assert error.value.detail["code"]=="carrier_return_exists" and adapter.calls==1
    register=await invoke(database,shop_fiscal.read,"register")
    assert row.id in register.reconciliation.carrier_return_case_ids and not register.actions["z"]
    assert (await counts(database))["shop_carrier_return_acceptances"]==0


@pytest.mark.parametrize("condition",["wrong_case","wrong_hash","future","missing_date"])
async def test_reverse_observation_untrusted_binding_rejects_without_stock_effect(database,checkout_dependencies,monkeypatch,condition):
    order_id, _, _, _=await setup(database)
    row=await create(database,order_id,quantity=1)
    row=await canonical(database,order_id,row.id)
    class Invalid(service.LocalShopReturnAdapter):
        async def observe(self,*args,**kwargs):
            proof=await super().observe(*args,**kwargs)
            return replace(proof,**{"wrong_case":{"case_id":999},"wrong_hash":{"snapshot_hash":"0"*64},
                "future":{"event_at":datetime.now(UTC)+timedelta(days=1)},"missing_date":{"event_at":None}}[condition])
    monkeypatch.setattr(service,"adapter",Invalid())
    before=await counts(database)
    with pytest.raises(HTTPException) as error:
        await refresh(database,row)
    assert error.value.detail["code"]=="carrier_return_binding_invalid" and await counts(database)==before


async def test_failed_poll_retains_returned_but_never_authorizes_receive(database,checkout_dependencies,monkeypatch):
    order_id, _, _, _=await setup(database)
    row=await create(database,order_id,quantity=1)
    row=await refresh(database,await canonical(database,order_id,row.id))
    class Failed(service.LocalShopReturnAdapter):
        async def observe(self,*args,**kwargs):
            raise TimeoutError("Synthetic failed read")
    monkeypatch.setattr(service,"adapter",Failed())
    row=await refresh(database,row)
    assert row.status=="returned" and row.observation.last_attempt_status=="error" and not row.actions["receive"]
    before=await counts(database)
    with pytest.raises(HTTPException) as error:
        await receive(database,row)
    assert error.value.detail["code"]=="carrier_return_tracking_stale" and await counts(database)==before


async def test_returned_repeat_event_is_deduplicated_and_stale_check_blocks_receipt(database,checkout_dependencies,monkeypatch):
    order_id, _, _, _=await setup(database)
    row=await create(database,order_id,quantity=1)
    row=await canonical(database,order_id,row.id)
    class Constant(service.LocalShopReturnAdapter):
        proof=None
        async def observe(self,*args,**kwargs):
            self.proof=self.proof or await super().observe(*args,**kwargs)
            return self.proof
    monkeypatch.setattr(service,"adapter",Constant())
    row=await refresh(database,row)
    row=await refresh(database,row)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(TrackingEvent).where(TrackingEvent.shipment_id==row.return_shipment_id))==1
        reverse=await session.get(Shipment,row.return_shipment_id)
        reverse.last_checked_at=datetime.now(UTC)-timedelta(days=2)
        await session.commit()
    before=await counts(database)
    with pytest.raises(HTTPException) as error:
        await receive(database,row)
    assert error.value.detail["code"]=="carrier_return_tracking_stale" and await counts(database)==before


@pytest.mark.parametrize("same_key",[True,False])
async def test_receive_race_stock_once_and_unpaid_not_restock_closes(database,checkout_dependencies,same_key):
    order_id,product_id,_,_=await setup(database,paid=False)
    row=await create(database,order_id)
    row=await refresh(database,await canonical(database,order_id,row.id))
    body=CarrierReceive(expected_revision=row.revision,lines=[{"order_item_id":i["order_item_id"],"quantity":i["quantity"],"disposition":"not_restock"} for i in row.lines],evidence="Synthetic damaged parcel",confirm=True)
    key=str(uuid4())
    outcomes=await asyncio.gather(*(invoke(database,service.command,order_id,row.id,body,key if same_key else str(uuid4()),"receive") for _ in range(2)),return_exceptions=True)
    if same_key:
        assert outcomes[0].model_dump()==outcomes[1].model_dump()
    else:
        assert sum(not isinstance(v,Exception) for v in outcomes)==1
        assert next(v for v in outcomes if isinstance(v,HTTPException)).status_code==409
    row=await canonical(database,order_id,row.id)
    assert (await counts(database))["shop_carrier_return_receipts"]==1
    before=await counts(database)
    closed=await invoke(database,service.command,order_id,row.id,CarrierClose(expected_revision=row.revision,evidence="Damaged parcel accounted",confirm=True),str(uuid4()),"close_unpaid")
    assert closed.status=="closed_unpaid" and (await counts(database))["inventory_movements"]==before["inventory_movements"]
    async with database() as session:
        product=await session.get(Product,product_id)
        assert (product.stock_quantity,product.reserved_quantity)==(8,0)


async def test_b1_new_money_complete_blocked_unpaid_cancel_requires_explicit_full_receipt(database,checkout_dependencies):
    order_id,_,_,_=await setup(database,paid=False)
    row=await create(database,order_id)
    row=await canonical(database,order_id,row.id)
    before=await counts(database)
    with pytest.raises(HTTPException) as error:
        await invoke(database,service.command,order_id,row.id,CarrierClose(expected_revision=row.revision,evidence="too early",confirm=True),str(uuid4()),"close_unpaid")
    assert error.value.detail["code"]=="carrier_return_not_received" and await counts(database)==before
    with pytest.raises(HTTPException) as error:
        await pay(database,order_id,"cod")
    assert error.value.detail["code"]=="carrier_return_active" and await counts(database)==before
    current=await state(database,order_id)
    with pytest.raises(HTTPException) as error:
        await invoke(database,shop_sales.command,order_id,ShopPhysicalCommand(expected_revision=current.revision,evidence="forbidden"),str(uuid4()),"complete")
    assert error.value.detail["code"]=="carrier_return_active"
    async with database() as session:
        with pytest.raises(Exception,match="Accepted Shop facts"):
            await session.execute(text("UPDATE orders SET status='cancelled' WHERE id=:id"),{"id":order_id})
        await session.rollback()
    assert await counts(database)==before


async def test_closed_unpaid_raw_reopen_or_money_and_immutable_provenance_rejected(database,checkout_dependencies):
    order_id,_,_,_=await setup(database,paid=False)
    row=await create(database,order_id)
    row=await receive(database,await refresh(database,await canonical(database,order_id,row.id)))
    row=await invoke(database,service.command,order_id,row.id,CarrierClose(expected_revision=row.revision,evidence="close",confirm=True),str(uuid4()),"close_unpaid")
    before=await counts(database)
    async with database() as session:
        for sql,params,reason in [
            ("UPDATE orders SET status='confirmed' WHERE id=:id",{"id":order_id},"cannot reopen"),
            ("UPDATE shop_carrier_returns SET evidence='tamper' WHERE id=:id",{"id":row.id},"immutable"),
            ("UPDATE shop_carrier_unpaid_closures SET evidence='tamper' WHERE case_id=:id",{"id":row.id},"immutable"),
            ("UPDATE order_shipments SET return_of_shipment_id=NULL WHERE id=:id",{"id":row.return_shipment_id},"immutable")]:
            with pytest.raises(Exception,match=reason):
                await session.execute(text(sql),params)
            await session.rollback()
        session.add(OrderPayment(order_id=order_id,amount_minor=1,method="cash",source="operator_confirmation",
            evidence="forbidden",operator_id=1,simulation=True,created_at=datetime.now(UTC)))
        with pytest.raises(Exception,match="cannot receive financial facts"):
            await session.commit()
        await session.rollback()
    assert await counts(database)==before


async def test_b2_received_quantities_and_dispositions_cannot_be_reclaimed(database,checkout_dependencies):
    order_id,_,_,sale=await setup(database)
    row=await create(database,order_id,quantity=1)
    row=await receive(database,await refresh(database,await canonical(database,order_id,row.id)))
    current=await state(database,order_id)
    item=row.lines[0]
    financial=await invoke(database,shop_fiscal.order_command,order_id,ReturnCommand(expected_revision=current.revision,sale_document_id=sale.id,
        carrier_receipt_id=row.receipt.id,lines=[{"order_item_id":item["order_item_id"],"quantity":1}],reason="synthetic"),str(uuid4()),"create_return")
    before=await counts(database)
    current=await state(database,order_id)
    with pytest.raises(HTTPException) as error:
        await invoke(database,shop_fiscal.order_command,order_id,ReturnCommand(expected_revision=current.revision,sale_document_id=sale.id,
            carrier_receipt_id=row.receipt.id,lines=[{"order_item_id":item["order_item_id"],"quantity":1}],reason="overclaim"),str(uuid4()),"create_return")
    assert error.value.detail["code"]=="carrier_receipt_quantity_exceeded" and await counts(database)==before
    with pytest.raises(HTTPException) as error:
        await invoke(database,shop_fiscal.return_command,financial.id,ReceiptCommand(expected_revision=financial.revision,
            lines=[{"order_item_id":item["order_item_id"],"quantity":1,"disposition":"not_restock"}],evidence="wrong disposition"),str(uuid4()),"physical_receipt")
    assert error.value.detail["code"]=="carrier_receipt_disposition_mismatch" and await counts(database)==before
    register=await invoke(database,shop_fiscal.read,"register")
    assert row.id in register.reconciliation.carrier_return_case_ids and financial.id in register.reconciliation.unresolved_return_ids


async def test_unbranded_extra_shipments_and_wrong_reverse_allocation_failclosed(database,checkout_dependencies):
    order_id,_,shipment_id,_=await setup(database)
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        item_id,order_revision=order.items[0].id,order.revision
        legacy=Shipment(order_id=order_id,simulation=True,carrier="mock_nova_poshta",source="create",active=False,
            operation_uuid=str(uuid4()),provider_reference="MOCK-"+uuid4().hex,tracking_number=None,status="created",raw_status="created",progress=0,
            address_snapshot={},packaging={},created_at=datetime.now(UTC),return_of_shipment_id=shipment_id)
        session.add(legacy)
        await session.commit()
        assert legacy.shop_carrier_return_case_id is None
    before=await counts(database)
    with pytest.raises(HTTPException) as error:
        await invoke(database,service.create,order_id,CarrierCreate(expected_revision=order_revision,
            original_shipment_id=shipment_id,lines=[{"order_item_id":item_id,"quantity":1}],evidence="unbranded direct request",confirm=True),str(uuid4()))
    assert error.value.detail["code"]=="carrier_shipment_ambiguous" and await counts(database)==before


async def test_reverse_allocation_immutable_and_corrupt_projection_not_branded(database,checkout_dependencies):
    from app.models.shipment import ShipmentItem
    order_id,_,_,_=await setup(database)
    row=await create(database,order_id,quantity=1)
    row=await canonical(database,order_id,row.id)
    async with database() as session:
        allocation=await session.scalar(select(ShipmentItem).where(ShipmentItem.shipment_id==row.return_shipment_id))
        allocation.quantity=2
        with pytest.raises(Exception,match="Sales facts are immutable"):
            await session.commit()
        await session.rollback()
    before=await counts(database)
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        reverse=next(s for s in order.shipments if s.id==row.return_shipment_id)
        case=reverse.shop_carrier_return_case
        reverse.shipment_items[0].quantity=2
        assert not service.valid_binding(case,order,reverse)
        assert reverse.shop_carrier_return_case_id is None
        await session.rollback()  # pure in-memory bad proof is never persisted
    assert await counts(database)==before
    assert (await canonical(database,order_id,row.id)).status=="returning"


async def test_unpaid_return_does_not_invent_financial_shift_obligation(database,checkout_dependencies):
    order_id,_,_,_=await setup(database,paid=False)
    row=await create(database,order_id)
    row=await canonical(database,order_id,row.id)
    register=await invoke(database,shop_fiscal.read,"register")
    assert register.reconciliation.carrier_return_case_ids==[] and register.actions["z"]
    z=await invoke(database,shop_fiscal.shift_command,ShiftCommand(expected_shift_revision=register.shift_revision,confirm=True),str(uuid4()),"z")
    assert z.totals["sales_minor"]==z.totals["returns_minor"]==0
    row=await receive(database,await refresh(database,row))
    closed=await invoke(database,service.command,order_id,row.id,CarrierClose(expected_revision=row.revision,evidence="unpaid physical close after Z",confirm=True),str(uuid4()),"close_unpaid")
    assert closed.status=="closed_unpaid"


async def test_paid_case_start_register_lock_blocks_concurrent_z(database,checkout_dependencies,monkeypatch):
    order_id,_,_,_=await setup(database)
    register=await invoke(database,shop_fiscal.read,"register")
    held,release=asyncio.Event(),asyncio.Event()
    original=shop_fiscal.register
    async def barrier(session,**kwargs):
        row=await original(session,**kwargs)
        if asyncio.current_task().get_name()=="carrier-create-race" and kwargs.get("lock"):
            held.set()
            await release.wait()
        return row
    monkeypatch.setattr(shop_fiscal,"register",barrier)
    creating=asyncio.create_task(create(database,order_id,quantity=1),name="carrier-create-race")
    await asyncio.wait_for(held.wait(),5)
    closing=asyncio.create_task(invoke(database,shop_fiscal.shift_command,ShiftCommand(expected_shift_revision=register.shift_revision,confirm=True),str(uuid4()),"z"))
    await asyncio.sleep(.03)
    assert not closing.done()
    release.set()
    row=await asyncio.wait_for(creating,10)
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(closing,10)
    assert row.id and error.value.detail["code"]=="shift_unsettled"


async def test_actual_http_readonly_changed_body_and_closed_exact_replay(database,checkout_dependencies,monkeypatch):
    order_id,_,_,_=await setup(database)
    app=FastAPI();app.include_router(routes.backoffice_router,prefix="/api/v1/backoffice")
    async def db():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await session.get(AdminUser,1)
    app.dependency_overrides[get_db_session]=db;app.dependency_overrides[get_current_admin_user]=actor
    path=f"/api/v1/backoffice/orders/{order_id}/carrier-returns"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        before=await counts(database)
        first=await client.get(path)
        assert first.status_code==200
        await client.get(path)
        assert await counts(database)==before
        data=first.json();key=str(uuid4())
        body={"expected_revision":data["revision"],"original_shipment_id":data["original_shipment_id"],
            "lines":[{"order_item_id":data["eligible_lines"][0]["order_item_id"],"quantity":1}],"evidence":"synthetic","confirm":True}
        accepted=await client.post(path,headers={"Idempotency-Key":key},json=body)
        assert accepted.status_code==200 and accepted.json()["status"]=="unknown"
        live=await client.get(path+f'/{accepted.json()["id"]}')
        assert live.json()["status"]=="returning" and live.json()["observation"] is None
        monkeypatch.setattr(settings,"shop_carrier_returns_mock_enabled",False)
        replay=await client.post(path,headers={"Idempotency-Key":key},json=body)
        assert replay.json()==accepted.json()
        changed=await client.post(path,headers={"Idempotency-Key":key},json={**body,"evidence":"changed"})
        assert changed.status_code==409 and changed.json()["detail"]["code"]=="idempotency_conflict"
        closed=await client.post(path+'?enabled=true',headers={"Idempotency-Key":str(uuid4()),"X-Enable-Returns":"true"},json=body)
        assert closed.status_code==503


async def test_production_and_actual_bind_database_mismatch_never_create_mock_facts(database,checkout_dependencies,monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    order_id,_,shipment_id,_=await setup(database)
    current=await invoke(database,service.read,order_id)
    body=CarrierCreate(expected_revision=current.revision,original_shipment_id=shipment_id,
        lines=[{"order_item_id":i["order_item_id"],"quantity":1} for i in current.eligible_lines],evidence="synthetic",confirm=True)
    assert Settings(_env_file=None).shop_carrier_returns_mock_enabled is False
    before=await counts(database)
    monkeypatch.setattr(settings,"app_env","production")
    with pytest.raises(HTTPException) as error:
        await invoke(database,service.create,order_id,body,str(uuid4()))
    assert error.value.detail["code"]=="shop_carrier_return_test_unsafe" and await counts(database)==before
    monkeypatch.setattr(settings,"app_env","test")
    actual=database.kw["bind"].url
    engine=create_async_engine(actual.set(database="misleading_test_"+uuid4().hex),connect_args={"database":actual.database})
    try:
        async with async_sessionmaker(engine,expire_on_commit=False)() as session:
            with pytest.raises(HTTPException) as error:
                await service.create(session,await session.get(AdminUser,1),order_id,body,str(uuid4()))
            assert error.value.detail["code"]=="shop_carrier_return_test_unsafe"
    finally:
        await engine.dispose()
    assert await counts(database)==before


async def test_fresh_permissions_financial_read_without_shipments_and_revoked_replay(database,checkout_dependencies,monkeypatch):
    order_id,_,_,_=await setup(database)
    current=await invoke(database,service.read,order_id)
    async with database() as session:
        staff=AdminUser(email="carrier-staff@example.test",hashed_password="unused",is_active=True,can_manage_shipments=True)
        finance=AdminUser(email="finance-staff@example.test",hashed_password="unused",is_active=True,can_record_sales=True)
        session.add_all([staff,finance]);await session.commit()
        staff_id,finance_id=staff.id,finance.id
    body=CarrierCreate(expected_revision=current.revision,original_shipment_id=current.original_shipment_id,
        lines=[{"order_item_id":i["order_item_id"],"quantity":1} for i in current.eligible_lines],evidence="synthetic",confirm=True)
    key=str(uuid4())
    accepted=await invoke(database,service.create,order_id,body,key,actor_id=staff_id)
    financial_read=await invoke(database,service.read,order_id,actor_id=finance_id)
    assert financial_read.cases[0].id==accepted.id and not financial_read.actions["replay_create"]
    row=await canonical(database,order_id,accepted.id)
    with pytest.raises(HTTPException) as error:
        await invoke(database,service.command,order_id,row.id,RevisionCommand(expected_revision=row.revision),str(uuid4()),"refresh",actor_id=finance_id)
    assert error.value.status_code==403
    async with database() as stale:
        actor=await stale.get(AdminUser,staff_id)
        async with database() as session:
            current=await session.get(AdminUser,staff_id);current.can_manage_shipments=False;await session.commit()
        with pytest.raises(HTTPException) as error:
            await service.create(stale,actor,order_id,body,key)
        assert error.value.status_code==403


async def test_wrong_order_carrier_case_and_physical_receipt_ids_have_zero_effects(database,checkout_dependencies):
    first_id,_,_,sale=await setup(database)
    second_id,_,_,second_sale=await setup(database)
    row=await create(database,first_id,quantity=1)
    row=await receive(database,await refresh(database,await canonical(database,first_id,row.id)))
    before=await counts(database)
    with pytest.raises(HTTPException) as error:
        await invoke(database,service.command,second_id,row.id,RevisionCommand(expected_revision=row.revision),str(uuid4()),"refresh")
    assert error.value.status_code==404 and await counts(database)==before
    current=await state(database,second_id)
    second_item=second_sale.snapshot["items"][0]
    with pytest.raises(HTTPException) as error:
        await invoke(database,shop_fiscal.order_command,second_id,ReturnCommand(expected_revision=current.revision,sale_document_id=second_sale.id,
            carrier_receipt_id=row.receipt.id,lines=[{"order_item_id":second_item["order_item_id"],"quantity":1}],reason="wrong parcel"),str(uuid4()),"create_return")
    assert error.value.status_code==404 and await counts(database)==before


async def test_raw_receipt_without_exact_ledger_and_history_downgrade_refused(database,checkout_dependencies,template_database):
    from alembic import command as alembic_command
    order_id,_,_,_=await setup(database,paid=False)
    row=await create(database,order_id)
    row=await refresh(database,await canonical(database,order_id,row.id))
    before=await counts(database)
    async with database() as session:
        case=await session.get(ShopCarrierReturn,row.id)
        lines=[{"order_item_id":i["order_item_id"],"quantity":i["quantity"],"disposition":"restock"} for i in case.lines]
        snapshot={"case_id":case.id,"case_hash":case.snapshot_hash,"authority":"test","lines":lines}
        session.add(ShopCarrierReturnReceipt(case_id=case.id,lines=lines,snapshot=snapshot,snapshot_hash=service.digest(snapshot),
            evidence="Forged receipt without ledger",operator_id=1,created_at=datetime.now(UTC)))
        with pytest.raises(Exception,match="exact once-only receipt ledger"):
            await session.commit()
        await session.rollback()
    assert await counts(database)==before
    _,_,_,config=template_database
    async with database() as session:
        version_before=await session.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError,match="Accepted Shop carrier return history"):
        await asyncio.to_thread(alembic_command.downgrade,config,"0123_shop_fiscal")
    async with database() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version"))==version_before
    assert await counts(database)==before


async def test_http_wrong_order_cached_receive_rejected_correct_flagoff_replay_preserved(database,checkout_dependencies,monkeypatch):
    first_id,first_product,_,_=await setup(database)
    second_id,second_product,_,_=await setup(database)
    row=await create(database,first_id,quantity=1)
    row=await refresh(database,await canonical(database,first_id,row.id))
    app=FastAPI();app.include_router(routes.backoffice_router,prefix="/api/v1/backoffice")
    async def db():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await session.get(AdminUser,1)
    app.dependency_overrides[get_db_session]=db;app.dependency_overrides[get_current_admin_user]=actor
    key=str(uuid4())
    body=CarrierReceive(expected_revision=row.revision,lines=[{"order_item_id":i["order_item_id"],"quantity":i["quantity"],"disposition":"restock"} for i in row.lines],evidence="Synthetic accepted receipt",confirm=True).model_dump(mode="json")
    path=f"/api/v1/backoffice/orders/{first_id}/carrier-returns/{row.id}/receive"
    wrong=f"/api/v1/backoffice/orders/{second_id}/carrier-returns/{row.id}/receive"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        accepted=await client.post(path,headers={"Idempotency-Key":key},json=body)
        assert accepted.status_code==200 and accepted.json()["receipt"]
        before=await counts(database)
        async with database() as session:
            stocks=[(p.stock_quantity,p.reserved_quantity) for p in (await session.scalars(select(Product).where(Product.id.in_([first_product,second_product])).order_by(Product.id))).all()]
        rejected=await client.post(wrong,headers={"Idempotency-Key":key},json=body)
        assert rejected.status_code==404 and await counts(database)==before
        monkeypatch.setattr(settings,"shop_carrier_returns_mock_enabled",False)
        replay=await client.post(path,headers={"Idempotency-Key":key},json=body)
        assert replay.status_code==200 and replay.json()==accepted.json() and await counts(database)==before
        async with database() as session:
            assert [(p.stock_quantity,p.reserved_quantity) for p in (await session.scalars(select(Product).where(Product.id.in_([first_product,second_product])).order_by(Product.id))).all()]==stocks


async def finish_financial_claim(database,order_id,financial,case):
    financial=await refund(database,financial)
    await receipt(database,order_id,return_id=financial.id)
    financial=await invoke(database,shop_fiscal.read,"return",financial.id)
    dispositions={i["order_item_id"]:i["disposition"] for i in case.receipt.lines}
    return await invoke(database,shop_fiscal.return_command,financial.id,ReceiptCommand(expected_revision=financial.revision,
        lines=[{"order_item_id":i["order_item_id"],"quantity":i["quantity"],"disposition":dispositions[i["order_item_id"]]} for i in financial.lines],
        evidence="Link accepted physical receipt; zero inventory delta"),str(uuid4()),"physical_receipt")


async def test_zero_net_parcel_rejected_before_case_provider_and_positive_bundle_finishes(database,checkout_dependencies,monkeypatch):
    order_id,_,_,sale=await setup(database,quantity=3,total="0.01")
    class Spy(service.LocalShopReturnAdapter):
        calls=0
        async def submit(self,*args,**kwargs):
            self.calls+=1
            return await super().submit(*args,**kwargs)
    adapter=Spy();monkeypatch.setattr(service,"adapter",adapter)
    before=await counts(database)
    async with database() as session:
        shipments_before=await session.scalar(select(func.count()).select_from(Shipment))
    with pytest.raises(HTTPException) as error:
        await create(database,order_id,quantity=1)
    assert error.value.detail["code"]=="unsupported_zero_total" and await counts(database)==before and adapter.calls==0
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Shipment))==shipments_before
    register=await invoke(database,shop_fiscal.read,"register")
    assert register.reconciliation.carrier_return_case_ids==[] and register.actions["z"]
    case=await create(database,order_id,quantity=3)
    case=await receive(database,await refresh(database,await canonical(database,order_id,case.id)))
    current=await state(database,order_id)
    financial=await invoke(database,shop_fiscal.order_command,order_id,ReturnCommand(expected_revision=current.revision,
        sale_document_id=sale.id,carrier_receipt_id=case.receipt.id,
        lines=[{"order_item_id":i["order_item_id"],"quantity":i["quantity"]} for i in case.lines],reason="Bundle the complete one-kopeck return"),str(uuid4()),"create_return")
    assert financial.amount_minor==1 and adapter.calls==1
    await finish_financial_claim(database,order_id,financial,case)
    register=await invoke(database,shop_fiscal.read,"register")
    z=await invoke(database,shop_fiscal.shift_command,ShiftCommand(expected_shift_revision=register.shift_revision,confirm=True),str(uuid4()),"z")
    assert z.totals["sales_minor"]==z.totals["returns_minor"]==1 and z.totals["net_minor"]==0


async def test_np_financial_claim_cannot_orphan_zero_lines_but_bundle_finishes_z(database,checkout_dependencies):
    from decimal import Decimal
    from app.schemas.order import OrderCreate
    from app.services.order import OrderService
    await open_shift(database)
    async with database() as session:
        products=[Product(name=name,slug=uuid4().hex,price=Decimal(price),stock_quantity=10,reserved_quantity=0,allow_backorder=False)
            for name,price in [("One kopeck positive line","0.01"),("Zero-value bundled line","0.00")]]
        session.add_all(products);await session.commit()
        order=await OrderService().create_order(session,OrderCreate(firstName="Synthetic",phoneNumber="+380500001234",
            shippingMethod="pickup",paymentMethod="bank_transfer",items=[{"productId":p.id,"quantity":1} for p in products]))
        order.fulfillment_method="nova_poshta";await session.commit();order_id=order.id
    await attach(database,order_id)
    await pay(database,order_id)
    sale=await receipt(database,order_id)
    await handover(database,order_id)
    case=await create(database,order_id)
    case=await receive(database,await refresh(database,await canonical(database,order_id,case.id)))
    positive=next(i for i in sale.snapshot["items"] if i["total_minor"]==1)
    zero=next(i for i in sale.snapshot["items"] if i["total_minor"]==0)
    before=await counts(database);current=await state(database,order_id)
    with pytest.raises(HTTPException) as error:
        await invoke(database,shop_fiscal.order_command,order_id,ReturnCommand(expected_revision=current.revision,
            sale_document_id=sale.id,carrier_receipt_id=case.receipt.id,
            lines=[{"order_item_id":positive["order_item_id"],"quantity":1}],reason="Would strand the zero-value line"),str(uuid4()),"create_return")
    assert error.value.detail["code"]=="unsupported_zero_total" and await counts(database)==before
    # A zero-only claim is also rejected; both lines must travel in one positive claim.
    with pytest.raises(HTTPException) as error:
        await invoke(database,shop_fiscal.order_command,order_id,ReturnCommand(expected_revision=current.revision,
            sale_document_id=sale.id,carrier_receipt_id=case.receipt.id,
            lines=[{"order_item_id":zero["order_item_id"],"quantity":1}],reason="Zero-only claim"),str(uuid4()),"create_return")
    assert error.value.detail["code"]=="unsupported_zero_total" and await counts(database)==before
    financial=await invoke(database,shop_fiscal.order_command,order_id,ReturnCommand(expected_revision=current.revision,
        sale_document_id=sale.id,carrier_receipt_id=case.receipt.id,
        lines=[{"order_item_id":i["order_item_id"],"quantity":1} for i in sale.snapshot["items"]],reason="Positive bundle includes zero line"),str(uuid4()),"create_return")
    assert financial.amount_minor==1 and sorted(i["total_minor"] for i in financial.lines)==[0,1]
    await finish_financial_claim(database,order_id,financial,case)
    register=await invoke(database,shop_fiscal.read,"register")
    assert register.reconciliation.carrier_return_case_ids==[]
    z=await invoke(database,shop_fiscal.shift_command,ShiftCommand(expected_shift_revision=register.shift_revision,confirm=True),str(uuid4()),"z")
    assert z.totals["net_minor"]==0


async def test_positive_partial_np_claims_remain_allowed(database,checkout_dependencies):
    order_id,_,_,sale=await setup(database,quantity=2)
    case=await create(database,order_id)
    case=await receive(database,await refresh(database,await canonical(database,order_id,case.id)))
    for _ in range(2):
        current=await state(database,order_id)
        financial=await invoke(database,shop_fiscal.order_command,order_id,ReturnCommand(expected_revision=current.revision,
            sale_document_id=sale.id,carrier_receipt_id=case.receipt.id,
            lines=[{"order_item_id":case.lines[0]["order_item_id"],"quantity":1}],reason="Positive partial claim"),str(uuid4()),"create_return")
        assert financial.amount_minor==10000
        await finish_financial_claim(database,order_id,financial,case)
    register=await invoke(database,shop_fiscal.read,"register")
    assert register.reconciliation.carrier_return_case_ids==[] and register.actions["z"]
