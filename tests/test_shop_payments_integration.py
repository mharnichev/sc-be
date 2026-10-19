"""R2 money boundaries through real MockTransport and disposable migrated PG."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderPayment, OrderItem, OrderStatus
from app.models.shop_payments import ShopPaymentIntent as Intent, ShopPaymentEvidence as Evidence
from app.models.shop_sales import ShopOperationAudit
from app.schemas import shop_payments as dto
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import shop_payments as service, shop_sales, monobank_provider
from app.services.order import OrderService
from app.api.v1.routes import shop_payments as routes
from app.dependencies.auth import get_current_admin_user
from app.core.database import get_db_session
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_sales_integration import seed, manual, execute, attach, delivered

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setattr(settings,"app_env","test")
    monkeypatch.setattr(settings,"shop_order_operations_enabled",True)
    monkeypatch.setattr(settings,"shop_payments_enabled",True)
    monkeypatch.setattr(settings,"shop_fiscal_mock_enabled",False)
    monkeypatch.setattr(settings,"shop_checkbox_enabled",False)
    monkeypatch.setattr(settings,"shop_checkbox_profile",None)
    monkeypatch.setattr(settings,"shop_monobank_live_transport_enabled",False)
    monkeypatch.setattr(shop_sales,"fiscal_policy",shop_sales.IsolatedTestFiscalPolicy())
    monkeypatch.setattr(settings,"shop_monobank_token",SecretStr("r2-synthetic-only"))
    profile=dict(id="synthetic-r2",merchant_id="synthetic-merchant",token_sha256=sha256(b"r2-synthetic-only").hexdigest(),
        environment="test",external_fiscalization_disabled=True)
    monkeypatch.setattr(settings,"shop_monobank_profile",profile)
    monkeypatch.setattr(settings,"shop_payment_manual_policy",dict(id="synthetic-cod",issuer="SyntheticBank",account="SyntheticAccount",
        types=["bank_statement","carrier_settlement"],environment="test"))
    state={"calls":[],"status":"success","reference":None,"amount":10000,"mutate":None}
    def handler(request):
        assert request.method=="GET"
        state["calls"].append(request.url.path)
        if request.url.path=="/api/merchant/details":
            return httpx.Response(200,json={"merchantId":profile["merchant_id"]})
        now=datetime.now(UTC).isoformat()
        body=dict(invoiceId=request.url.params["invoiceId"],reference=state["reference"],ccy=980,amount=state["amount"],
            finalAmount=state["amount"],status=state["status"],createdDate=now,modifiedDate=now)
        if state["mutate"]:
            state["mutate"](body)
        return httpx.Response(200,json=body)
    state["transport"]=httpx.MockTransport(handler)
    monkeypatch.setattr(service,"gateway",lambda:monobank_provider.MonobankGateway(transport=state["transport"]))
    return state


def profile_assertion(cod=False):
    profile=settings.shop_payment_manual_policy if cod else settings.shop_monobank_profile
    return dict(expected_profile_id=profile['id'],expected_profile_hash=service.digest(profile))


async def call(database,fn,*args,**kwargs):
    async with database() as session:
        return await fn(session,await session.get(AdminUser,1),*args,**kwargs)


async def stage(database,flags,**kwargs):
    order_id,_=await seed(database,**kwargs)
    result=await call(database,service.command,order_id,dto.InvoiceIntentCommand(expected_revision=1,**profile_assertion()),str(uuid4()),"invoice_intent")
    flags["reference"]=result.intent.reference
    return order_id,result


async def verify(database,order_id,state,*,invoice="syntheticInvoice",key=None):
    return await call(database,service.command,order_id,dto.VerifyInvoiceCommand(expected_revision=state.revision,invoice_id=invoice,**profile_assertion()),key or str(uuid4()),"verify")


async def assert_empty(database,order_id):
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        assert not order.payments and not order.shop_payment_evidence


async def test_invoice_readonly_staging_success_canonical_money_and_exact_replay(database,flags,monkeypatch):
    order_id,state=await stage(database,flags)
    assert state.intent.reference and state.revision==2 and not state.payment and not flags["calls"]
    key=str(uuid4())
    accepted=await verify(database,order_id,state,key=key)
    assert accepted.payment and accepted.payment.authority=="test" and accepted.revision==3
    assert accepted.payment.source=="provider" and accepted.payment.method=="card"
    count=len(flags["calls"])
    monkeypatch.setattr(settings,"shop_payments_enabled",False)
    assert await verify(database,order_id,state,key=key)==accepted
    assert len(flags["calls"])==count
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        assert shop_sales.canonical_payment(order)
        assert await session.scalar(select(func.count()).select_from(OrderPayment))==1
        assert await session.scalar(select(func.count()).select_from(Evidence))==1
        assert await session.scalar(select(func.count()).select_from(ShopOperationAudit).where(ShopOperationAudit.operation=="r2_verify"))==1


@pytest.mark.parametrize("status",["created","processing","hold","failure","expired","reversed"])
async def test_non_success_never_money_candidate_is_durable_and_only_original_can_retry(database,flags,status):
    order_id,state=await stage(database,flags)
    flags["status"]=status
    result=await verify(database,order_id,state)
    assert result.intent.invoice_id=="syntheticInvoice" and result.verification_status==status and not result.payment
    await assert_empty(database,order_id)
    with pytest.raises(HTTPException) as error:
        await verify(database,order_id,state,invoice="differentInvoice")
    assert error.value.detail["code"]=="invoice_binding_conflict"
    flags["status"]="success"
    assert (await verify(database,order_id,state)).payment


@pytest.mark.parametrize("failure",["amount","reference","merchant","partial","cancelled","date"])
async def test_malformed_wrong_or_ambiguous_provider_proof_never_admits_money(database,flags,failure):
    order_id,state=await stage(database,flags)
    def mutate(body):
        if failure=="amount":body["amount"]+=1
        if failure=="reference":body["reference"]=str(uuid4())
        if failure=="partial":body["finalAmount"]-=1
        if failure=="cancelled":body["cancelList"]=[{"amount":1}]
        if failure=="date":body["modifiedDate"]="broken"
    flags["mutate"]=mutate
    if failure=="merchant":
        profile=deepcopy(settings.shop_monobank_profile);profile["merchant_id"]="other";settings.shop_monobank_profile=profile
    if failure=="merchant":
        with pytest.raises(HTTPException):await verify(database,order_id,state)
    else:
        result=await verify(database,order_id,state)
        assert result.verification_status.startswith("monobank_")
    await assert_empty(database,order_id)


async def test_pending_candidate_blocks_cancel_economic_mutation_and_legacy_bypass_flagoff(database,flags,monkeypatch):
    order_id,state=await stage(database,flags)
    flags["status"]="processing";await verify(database,order_id,state)
    monkeypatch.setattr(settings,"shop_payments_enabled",False)
    with pytest.raises(HTTPException) as error:
        await execute(database,order_id,manual(revision=2),"manual_payment")
    assert error.value.detail["code"]=="shop_payments_workflow_required"
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await OrderService().update_status(session,order_id,OrderStatus.cancelled,admin_user_id=1)
        assert error.value.detail["code"]=="payment_intent_reconciliation_required"
        await session.rollback()
    for sql in ["UPDATE orders SET total_amount=101 WHERE id=:id","UPDATE orders SET status='cancelled' WHERE id=:id",
                "UPDATE order_items SET quantity=2 WHERE order_id=:id","UPDATE shop_payment_intents SET invoice_id='other' WHERE order_id=:id",
                "DELETE FROM shop_payment_intents WHERE order_id=:id"]:
        async with database() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(sql),{"id":order_id});await session.commit()


async def test_concurrent_same_key_verification_single_money(database,flags):
    order_id,state=await stage(database,flags)
    key=str(uuid4())
    results=await asyncio.gather(*(verify(database,order_id,state,key=key) for _ in range(2)))
    assert results[0]==results[1] and results[0].payment


async def test_revocation_and_flagoff_during_provider_get_no_money(database,flags,monkeypatch):
    order_id,state=await stage(database,flags)
    original=flags["transport"]
    async def handler(request):
        if request.url.path.endswith("/status"):
            monkeypatch.setattr(settings,"shop_payments_enabled",False)
        return await original.handle_async_request(request)
    flags["transport"]=httpx.MockTransport(handler)
    with pytest.raises(HTTPException) as error:await verify(database,order_id,state)
    assert error.value.detail["code"]=="shop_payments_disabled"
    await assert_empty(database,order_id)


def evidence(number):
    return dict(type="bank_statement",source="Synthetic statement",issuer="SyntheticBank",account="SyntheticAccount",document_id="statement-1",
        line_id="line-1",gross_amount_minor=10000,fee_minor=200,net_amount_minor=9800,currency="UAH",
        settled_at=datetime.now(UTC),tracking_number=number,verified=True)


async def dispatched(database):
    order_id,_=await seed(database,kind="nova_poshta",method="cashOnDelivery")
    shipment_id,number=await attach(database,order_id)
    result=await execute(database,order_id,ShopPhysicalCommand(expected_revision=1,evidence="Synthetic dispatch"),"handover")
    await delivered(database,shipment_id)
    return order_id,result,number


async def test_manual_cod_gross_fee_net_exact_dispatch_and_token_independence(database,flags,monkeypatch):
    order_id,state,number=await dispatched(database)
    monkeypatch.setattr(settings,"shop_monobank_token",None)
    payload=dto.ManualCODCommand(expected_revision=state.revision,**profile_assertion(cod=True),evidence=evidence(number))
    result=await call(database,service.command,order_id,payload,str(uuid4()),"manual_cod")
    assert result.payment and result.payment.method=="cod" and result.payment.authority=="manual"
    assert not flags["calls"]
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        assert shop_sales.canonical_payment(order)
        assert order.shop_payment_evidence.proof["net_amount_minor"]==9800


@pytest.mark.parametrize("failure",["not_delivered","wrong_ttn","wrong_account","future","amount"])
async def test_manual_cod_rejects_unverified_or_mismatched_settlement(database,flags,failure):
    order_id,state,number=await dispatched(database)
    body=evidence(number)
    if failure=="wrong_ttn":body["tracking_number"]="0"*14
    if failure=="wrong_account":body["account"]="another"
    if failure=="future":body["settled_at"]=datetime.now(UTC)+timedelta(days=1)
    if failure=="amount":body.update(gross_amount_minor=10001,net_amount_minor=9801)
    if failure=="not_delivered":
        async with database() as session:
            await session.execute(text("UPDATE order_shipments SET last_checked_at=now()-interval '7 days' WHERE order_id=:id"),{"id":order_id})
            await session.commit()
    with pytest.raises(HTTPException):
        await call(database,service.command,order_id,dto.ManualCODCommand(expected_revision=state.revision,**profile_assertion(cod=True),evidence=body),str(uuid4()),"manual_cod")
    await assert_empty(database,order_id)


async def test_manual_evidence_line_unique_across_orders(database,flags):
    for attempt in range(2):
        order_id,state,number=await dispatched(database)
        payload=dto.ManualCODCommand(expected_revision=state.revision,**profile_assertion(cod=True),evidence=evidence(number))
        if attempt:
            with pytest.raises(HTTPException) as error:
                await call(database,service.command,order_id,payload,str(uuid4()),"manual_cod")
            assert error.value.detail["code"]=="settlement_reference_used"
        else:
            assert (await call(database,service.command,order_id,payload,str(uuid4()),"manual_cod")).payment


async def test_raw_null_proof_and_wrong_identity_cannot_forge_evidence(database,flags):
    order_id,state=await stage(database,flags)
    accepted=await verify(database,order_id,state)
    for sql in ["UPDATE shop_payment_evidence SET proof='null' WHERE order_id=:id","UPDATE shop_payment_evidence SET identity='forged' WHERE order_id=:id",
                "DELETE FROM shop_payment_evidence WHERE order_id=:id"]:
        async with database() as session:
            with pytest.raises(DBAPIError):await session.execute(text(sql),{"id":order_id});await session.commit()
    assert accepted.payment


def test_cod_attestation_and_amounts_are_strict():
    for value in [False,1,"true"]:
        body=evidence("1"*14);body["verified"]=value
        with pytest.raises(ValidationError):dto.CODEvidence(**body)
    body=evidence("1"*14);body["net_amount_minor"]-=1
    with pytest.raises(ValidationError):dto.CODEvidence(**body)


@pytest.mark.parametrize("failure",["verified_string","amount_string"])
async def test_raw_complete_money_chain_rejects_coercible_proof_types(database,flags,failure):
    order_id,state=await stage(database,flags)
    async with database() as session:
        order=await shop_sales.load_order(session,order_id,lock=True)
        binding=order.shop_payment_intent
        binding.invoice_id="syntheticInvoice";await session.flush()
        proof=await service.gateway().get_invoice(binding.invoice_id,reference=binding.reference,amount_minor=binding.amount_minor)
        if failure=="verified_string":proof["verified"]="true"
        else:proof["amount_minor"]="10000"
        proof["proof_hash"]=service.digest({k:v for k,v in proof.items() if k!="proof_hash"})
        await service.record(session,order,await session.get(AdminUser,1),binding,proof,authority="test",source="provider",
            reference=binding.invoice_id,identity=service.digest([binding.profile_id,binding.invoice_id]),method="card")
        with pytest.raises(DBAPIError,match="Payment evidence lacks exact normalized provenance|Invoice acceptance requires exact verified success proof"):
            await session.commit()
    await assert_empty(database,order_id)


@pytest.mark.parametrize("operation",["invoice_intent","verify","manual_cod"])
async def test_stale_profile_assertions_never_bind_or_admit(database,flags,operation):
    order_id,_=await seed(database,method="cashOnDelivery" if operation=="manual_cod" else "bank_transfer")
    base=dict(expected_revision=1,expected_profile_id="stale-profile",expected_profile_hash="0"*64)
    payload=(dto.ManualCODCommand(**base,evidence=evidence("1"*14)) if operation=="manual_cod" else
        dto.VerifyInvoiceCommand(**base,invoice_id="invoice") if operation=="verify" else dto.InvoiceIntentCommand(**base))
    with pytest.raises(HTTPException) as error:await call(database,service.command,order_id,payload,str(uuid4()),operation)
    assert error.value.detail["code"]=="payment_profile_conflict"
    async with database() as session:
        assert not await session.scalar(select(Intent.id).where(Intent.order_id==order_id))
    assert not flags["calls"]


async def test_current_auth_revoked_during_get_and_cached_replay_denied(database,flags):
    order_id,state=await stage(database,flags)
    original=flags["transport"]
    async def handler(request):
        if request.url.path.endswith("/status"):
            async with database() as session:
                actor=await session.get(AdminUser,1);actor.is_active=False;await session.commit()
        return await original.handle_async_request(request)
    flags["transport"]=httpx.MockTransport(handler)
    with pytest.raises(HTTPException) as error:await verify(database,order_id,state)
    assert error.value.status_code==403
    await assert_empty(database,order_id)


async def test_http_routes_strict_dto_readonly_bootstrap_and_master_denial(database,flags):
    app=FastAPI();app.include_router(routes.backoffice_router,prefix="/api/v1/backoffice")
    async def session_override():
        async with database() as session:yield session
    async def actor_override():
        async with database() as session:return await session.get(AdminUser,1)
    app.dependency_overrides[get_db_session]=session_override
    app.dependency_overrides[get_current_admin_user]=actor_override
    order_id,_=await seed(database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://local.test") as client:
        result=await client.get("/api/v1/backoffice/shop-payments/readiness")
        assert result.status_code==200 and result.json()["provider_available"] and not flags["calls"]
        result=await client.post(f"/api/v1/backoffice/shop-payments/orders/{order_id}/invoice-intent",headers={"Idempotency-Key":str(uuid4())},
            json={"expected_revision":1,**profile_assertion(),"amount_minor":1})
        assert result.status_code==422
        from app.models.booking import Master
        async with database() as session:
            session.add(Master(admin_user_id=1,full_name="Synthetic denied master",is_active=True));await session.commit()
        # The existing superuser exemption remains deliberate across B1/R1/R2.
        assert (await client.get("/api/v1/backoffice/shop-payments/readiness")).status_code==200
        async with database() as session:
            actor=await session.get(AdminUser,1);actor.is_superuser=False;actor.can_record_sales=True;await session.commit()
        assert (await client.get("/api/v1/backoffice/shop-payments/readiness")).status_code==403
    assert not flags["calls"]


async def test_raw_insert_null_proof_and_profile_environment_are_rejected(database,flags):
    order_id,state=await stage(database,flags)
    async with database() as session:
        binding=await session.scalar(select(Intent).where(Intent.order_id==order_id))
        binding.invoice_id="syntheticInvoice"
        payment=OrderPayment(order_id=order_id,amount_minor=10000,method="card",source="shop_provider",evidence="syntheticInvoice",
            operator_id=1,simulation=False,created_at=datetime.now(UTC))
        session.add(payment);await session.flush()
        session.add(Evidence(intent_id=binding.id,order_id=order_id,payment_id=payment.id,identity="0"*64,authority="test",proof=None,
            proof_canonical="null",proof_hash=service.digest(None),operator_id=1,created_at=datetime.now(UTC)))
        with pytest.raises(DBAPIError):await session.commit()
    other,_=await seed(database)
    async with database() as session:
        profile=deepcopy(settings.shop_monobank_profile);profile.pop("environment")
        session.add(Intent(id=str(uuid4()),order_id=other,mode="prepayment",reference=str(uuid4()),amount_minor=10000,profile_id=profile["id"],
            profile=profile,profile_hash=service.digest(profile),authority="provider",transport="https",operator_id=1,created_at=datetime.now(UTC)))
        with pytest.raises(DBAPIError):await session.commit()
    async with database() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE order_items SET order_id=:bound WHERE order_id=:other"),{"bound":order_id,"other":other});await session.commit()
    await assert_empty(database,order_id)


@pytest.mark.parametrize("cod",[False,True])
async def test_r2_money_to_actual_checkbox_sale_handover_complete_x_z(database,flags,monkeypatch,cod):
    from tests import test_shop_checkbox_integration as checkbox
    stub=checkbox.flags.__wrapped__(monkeypatch)
    await checkbox.open_shift(database)
    if cod:
        order_id,state,number=await dispatched(database)
        paid=await call(database,service.command,order_id,dto.ManualCODCommand(expected_revision=state.revision,
            **profile_assertion(cod=True),evidence=evidence(number)),str(uuid4()),"manual_cod")
    else:
        order_id,state=await stage(database,flags)
        paid=await verify(database,order_id,state)
    assert paid.payment and paid.test_context and paid.evidence
    document=await checkbox.sale(database,order_id)
    assert document.acceptance_verified and document.is_sent_dps is False
    state=await checkbox.shop_state(database,order_id)
    if not cod:
        state=await execute(database,order_id,ShopPhysicalCommand(expected_revision=state.revision,evidence="Synthetic pickup"),"handover")
    state=await execute(database,order_id,ShopPhysicalCommand(expected_revision=state.revision,evidence="Synthetic completion"),"complete")
    assert state.completion.authority=="test"
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        assert shop_sales.payment_projection(order)=="simulated_paid"
    register=await checkbox.reg(database)
    report=await checkbox.call(database,checkbox.service.register_command,checkbox.register_payload(expected_revision=register.revision,confirm=True),str(uuid4()),"x")
    assert (await checkbox.process(database,report)).result["totals"]["sales_minor"]==10000
    register=await checkbox.reg(database)
    report=await checkbox.call(database,checkbox.service.register_command,checkbox.register_payload(expected_revision=register.revision,confirm=True),str(uuid4()),"z")
    assert (await checkbox.process(database,report)).result["z_report"]["totals"]["sales_minor"]==10000
    assert len(stub.state["receipts"])==1


async def production_checkbox_seam(database,monkeypatch):
    """Persist a synthetic production identity; no transport implementation exists."""
    from app.services import shop_checkbox
    from app.models.shop_checkbox import ShopCheckboxRegister as Register
    from tests.checkbox_http_stub import sample_profile
    from types import SimpleNamespace
    profile=sample_profile(1)
    profile["tenders"]["card"]=deepcopy(profile["tenders"]["cashless"])
    profile["tenders"]["bank_transfer"]=deepcopy(profile["tenders"]["cashless"])
    provider=SimpleNamespace(available=True,test_only=False,transport="live",profile=profile)
    monkeypatch.setattr(shop_checkbox,"gateway",lambda profile=None:provider)
    monkeypatch.setattr(settings,"shop_checkbox_enabled",True)
    monkeypatch.setattr(settings,"shop_checkbox_profile",profile)
    monkeypatch.setattr(shop_sales,"fiscal_policy",shop_sales.UnavailableFiscalPolicy())
    identity=dict(verified=True,authority="provider",transport="live",
        **{k:profile[k] for k in ("organization_id","register_id","cashier_id")})
    async with database() as session:
        session.add(Register(id=profile["id"],profile=profile,profile_hash=service.digest(profile),authority="provider",transport="live",
            revision=1,identity=identity,verified_at=datetime.now(UTC),shift_id=str(uuid4()),shift_status="open"))
        await session.commit()
    return shop_checkbox,profile


async def test_manual_test_settlement_cannot_enter_existing_production_checkbox_dispatch(database,flags,monkeypatch):
    checkbox,_=await production_checkbox_seam(database,monkeypatch)
    order_id,state,number=await dispatched(database)
    assert state.fulfillment.authority=="production"
    payload=dto.ManualCODCommand(expected_revision=state.revision,**profile_assertion(cod=True),evidence=evidence(number))
    with pytest.raises(HTTPException) as error:
        await call(database,service.command,order_id,payload,str(uuid4()),"manual_cod")
    assert error.value.detail["code"]=="checkbox_profile_conflict"
    await assert_empty(database,order_id)
    assert not flags["calls"]
    async with database() as session:
        assert not await session.scalar(select(Intent.id).where(Intent.order_id==order_id))
        from app.models.shop_checkbox import ShopCheckboxOperation as Operation
        assert not await session.scalar(select(Operation.id))


async def test_test_prepayment_binding_blocks_actual_checkbox_preview_approve_and_sql_dispatch(database,flags,monkeypatch):
    checkbox,profile=await production_checkbox_seam(database,monkeypatch)
    from app.models.shop_checkbox import ShopCheckboxBinding as Binding, ShopCheckboxRegister as Register
    from app.schemas import shop_checkbox as checkbox_dto
    order_id,_=await seed(database)
    async with database() as session:
        session.add(Binding(order_id=order_id,register_id=profile["id"],profile_hash=service.digest(profile),authority="provider",created_at=datetime.now(UTC)))
        await session.commit()
    state=await call(database,service.command,order_id,dto.InvoiceIntentCommand(expected_revision=1,**profile_assertion()),str(uuid4()),"invoice_intent")
    flags["reference"]=state.intent.reference
    fiscal=await call(database,checkbox.read,"order",order_id)
    assert not fiscal.enabled and not fiscal.actions["sale_preview"] and not fiscal.actions["approve_sale"]
    assert "checkbox_profile_conflict" in fiscal.blocking_reasons
    for operation,payload in [("preview",checkbox_dto.RevisionCommand(expected_revision=state.revision)),
        ("approve",checkbox_dto.ApproveCommand(expected_revision=state.revision,preview_id=999,confirm=True))]:
        with pytest.raises(HTTPException) as error:
            await call(database,checkbox.order_command,order_id,payload,str(uuid4()),operation)
        assert error.value.detail["code"]=="checkbox_profile_conflict"
    with pytest.raises(HTTPException) as error:await verify(database,order_id,state)
    assert error.value.detail["code"]=="checkbox_profile_conflict"
    await assert_empty(database,order_id)
    async with database() as session:
        row=await session.get(Register,profile["id"])
        op=checkbox.new_operation(row,await session.get(AdminUser,1),"sale",{"amount_minor":10000},order_id=order_id,provider_uuid=str(uuid4()))
        session.add(op)
        with pytest.raises(DBAPIError,match="R2 test payment context cannot become a production Checkbox operation"):
            await session.commit()


async def test_sql_complete_manual_test_money_chain_rejects_production_checkbox_binding(database,flags,monkeypatch):
    await production_checkbox_seam(database,monkeypatch)
    order_id,state,number=await dispatched(database)
    async def no_admission(*args):
        pass
    # Exercise the database independently of the domain admission guard above.
    monkeypatch.setattr(service,"admit",no_admission)
    async with database() as session:
        order=await shop_sales.load_order(session,order_id,lock=True)
        profile=settings.shop_payment_manual_policy
        binding=Intent(id=str(uuid4()),order_id=order_id,mode="cod",reference=str(uuid4()),amount_minor=10000,
            profile_id=profile["id"],profile=profile,profile_hash=service.digest(profile),authority="manual",transport="manual",
            operator_id=1,created_at=datetime.now(UTC))
        session.add(binding);await session.flush()
        proof=dto.CODEvidence(**evidence(number)).model_dump(mode="json")
        identity=service.digest([proof[k].casefold() for k in ("issuer","account","document_id","line_id")])
        await service.record(session,order,await session.get(AdminUser,1),binding,proof,authority="manual",source="manual",
            reference=identity,identity=identity,method="cod")
        with pytest.raises(DBAPIError,match="R2 test payment context cannot enter a production Checkbox binding"):
            await session.commit()
    await assert_empty(database,order_id)


@pytest.mark.parametrize("uncertain",[False,True])
async def test_actual_checkbox_dispatch_and_recovery_block_test_context_before_provider_io(database,flags,monkeypatch,uncertain):
    checkbox,profile=await production_checkbox_seam(database,monkeypatch)
    from app.models.shop_checkbox import ShopCheckboxBinding as Binding, ShopCheckboxRegister as Register
    from app.schemas import shop_checkbox as checkbox_dto
    order_id,_=await seed(database)
    async with database() as session:
        row=await session.get(Register,profile["id"])
        session.add(Binding(order_id=order_id,register_id=row.id,profile_hash=row.profile_hash,authority="provider",created_at=datetime.now(UTC)))
        op=checkbox.new_operation(row,await session.get(AdminUser,1),"sale",{"amount_minor":10000},order_id=order_id,provider_uuid=str(uuid4()))
        session.add(op);await session.flush()
        if uncertain:
            op.status="unknown";op.dispatched_at=datetime.now(UTC);op.revision=2
        await session.commit()
        operation_id,revision=op.id,op.revision
        # Reproduce an existing queued/uncertain provider operation followed by
        # a copied test intent; current admission already prevents new money.
        payment_profile=settings.shop_monobank_profile
        session.add(Intent(id=str(uuid4()),order_id=order_id,mode="prepayment",reference=str(uuid4()),amount_minor=10000,
            profile_id=payment_profile["id"],profile=payment_profile,profile_hash=service.digest(payment_profile),authority="test",transport="mock_http",
            operator_id=1,created_at=datetime.now(UTC)))
        await session.commit()
    payload=(checkbox_dto.ReconcileCommand(expected_revision=revision) if uncertain else checkbox_dto.RevisionCommand(expected_revision=revision))
    with pytest.raises(HTTPException) as error:
        await call(database,checkbox.operation_command,operation_id,payload,str(uuid4()),"reconcile" if uncertain else "process")
    assert error.value.detail["code"]=="checkbox_profile_conflict"
    # The synthetic production seam has no provider_effect methods: reaching
    # the transport would fail this test instead of issuing any external call.
    assert not flags["calls"]
