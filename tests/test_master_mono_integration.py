"""Live master POS over isolated PostgreSQL and injected bank HTTP contracts."""
import base64
import json
import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.fiscal import FiscalDocument
from app.models.master_mono import MasterMonoBinding, MasterMonoReceipt, MasterMonoObservation
from app.models.order import OrderHandover, OrderPayment
from app.models.product import Product
from app.schemas.master_mono import MasterMonoCommand
from app.schemas.order import POSOrderCreate, OrderHandoverCreate
from app.services import local_sales, master_mono as service, monobank_provider
from tests.test_inventory_integration import add_product, checkout_dependencies
from tests.test_shop_fiscal_integration import template_database, database

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mock_provider(monkeypatch):
    for key, value in dict(app_env="test", local_sales_simulation_enabled=True, master_mono_enabled=True, master_mono_invoice_create_enabled=True, master_mono_checkbox_link_confirmed=True,
            master_mono_fiscal_policy={"id":"approved-test-policy","tax_codes":[7],"unit":"шт."},
            master_monobank_live_transport_enabled=False).items():
        monkeypatch.setattr(settings, key, value)
    token = "synthetic-master-mono-token"
    monkeypatch.setattr(settings, "master_monobank_token", SecretStr(token))
    profile = dict(id="master-mock", merchant_id="master-mock-merchant", environment="production",
        token_sha256=sha256(token.encode()).hexdigest(), external_fiscalization_disabled=False)
    monkeypatch.setattr(settings, "master_monobank_profile", profile)
    state = dict(reference=None, amount=10000, status="created", checks=[], calls=[], post_calls=0, create_error=None, invoice_id="master_live_test_invoice", created_at=datetime.now(UTC).isoformat())
    def handler(request):
        state["calls"].append((request.method, request.url.path))
        if request.method == "POST":
            assert request.url.path == "/api/merchant/invoice/create"
            state["post_calls"] += 1
            wire = json.loads(request.content)
            state["reference"] = wire["merchantPaymInfo"]["reference"]
            state["amount"] = wire["amount"]
            state["basket"] = wire["merchantPaymInfo"]["basketOrder"]
            state["created_at"] = datetime.now(UTC).isoformat()
            if state["create_error"]:
                return httpx.Response(state["create_error"], json={})
            return httpx.Response(200, json={"invoiceId":state["invoice_id"],"pageUrl":"https://pay.mbnk.biz/"+state["invoice_id"]})
        assert request.method == "GET"
        if request.url.path == "/api/merchant/details":
            return httpx.Response(200, json={"merchantId":profile["merchant_id"]})
        if request.url.path == "/api/merchant/invoice/fiscal-checks":
            return httpx.Response(200, json={"checks":state["checks"]})
        now = datetime.now(UTC).isoformat()
        return httpx.Response(200, json=dict(invoiceId=request.url.params["invoiceId"], reference=state["reference"],
            amount=state["amount"], finalAmount=state["amount"], ccy=980, status=state["status"], createdDate=state["created_at"], modifiedDate=now))
    transport = httpx.MockTransport(handler)
    real_gateway = service.gateway
    state["gateway_factory"] = real_gateway
    monkeypatch.setattr(service, "gateway", lambda:real_gateway(transport=transport))
    return state


async def invoke(database, function, order_id, *args, actor_id=2):
    async with database() as session:
        return await function(session, await session.get(AdminUser, actor_id), order_id, *args)


async def prepared(database, state, *, start=True):
    product = await add_product(database, stock=5)
    async with database() as session:
        for identifier in (2, 3):
            session.add(AdminUser(id=identifier, email=f"master-{identifier}@example.test", hashed_password="unused",
                is_active=True, is_superuser=False, can_record_sales=True, can_accept_qr_payments=True, can_post_inventory=False))
            session.add(Master(admin_user_id=identifier, full_name=f"Synthetic master {identifier}"))
        await session.commit()
        order = await local_sales.prepare(session, await session.get(AdminUser, 2),
            POSOrderCreate(items=[{"product_id":product.id,"quantity":1}],simulation=False),str(uuid4()))
    initial = await invoke(database, service.read, order.id)
    assert initial.enabled and initial.blocking_reasons == [] and not initial.can_handover
    if not start:
        return product.id, order.id, initial
    started = await invoke(database, service.start, order.id, MasterMonoCommand(expected_revision=order.revision), str(uuid4()))
    async with database() as session:
        binding = await session.scalar(select(MasterMonoBinding).where(MasterMonoBinding.order_id == order.id))
        if binding:
            state["reference"] = binding.reference
    return product.id, order.id, started


def receipt(*, file=True):
    value = dict(id=str(uuid4()), type="sale", status="done", fiscalizationSource="checkbox")
    if file:
        value["file"] = base64.b64encode(b"%PDF-1.4\nsynthetic\n%%EOF").decode()
    return value


async def test_own_auto_refresh_receipt_then_once_only_handover(database, checkout_dependencies, mock_provider):
    product_id, order_id, started = await prepared(database, mock_provider)
    pending = await invoke(database, service.refresh, order_id, MasterMonoCommand(expected_revision=started.revision), str(uuid4()))
    assert not pending.paid and not pending.receipt_accepted
    mock_provider.update(status="success", checks=[receipt()])
    key, command = str(uuid4()), MasterMonoCommand(expected_revision=pending.revision)
    accepted = await invoke(database, service.refresh, order_id, command, key)
    assert accepted.paid and accepted.receipt_accepted and accepted.can_handover and accepted.receipt_pdf_path
    calls = len(mock_provider["calls"])
    assert await invoke(database, service.refresh, order_id, command, key) == accepted
    assert len(mock_provider["calls"]) == calls
    _, content = await invoke(database, service.download_pdf, order_id)
    assert content.startswith(b"%PDF-")
    async with database() as session:
        actor = await session.get(AdminUser, 2)
        payload = OrderHandoverCreate(expected_revision=accepted.revision, evidence="synthetic master handover")
        finished = await local_sales.command(session, actor, order_id, payload, str(uuid4()), handover=True)
        assert finished.fulfillment_status == "handed_over"
        assert finished.fiscal_status == "master_mono_receipt_accepted"
        assert finished.receipt_delivery_status == "master_mono_pdf_available"
        assert await session.scalar(select(func.count()).select_from(OrderPayment).where(OrderPayment.order_id==order_id)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderHandover).where(OrderHandover.order_id==order_id)) == 1
        assert await session.scalar(select(func.count()).select_from(FiscalDocument).where(FiscalDocument.order_id==order_id)) == 0
        assert (await session.get(Product,product_id)).stock_quantity == 4
        observations = (await session.scalars(select(MasterMonoObservation).where(MasterMonoObservation.order_id==order_id))).all()
        assert all("file" not in c for o in observations for c in ((o.proof.get("fiscal") or {}).get("checks") or []))


async def test_foreign_master_permissions_and_disabled_recovery(database, checkout_dependencies, mock_provider, monkeypatch):
    _, order_id, started = await prepared(database, mock_provider)
    for function, args in ((service.read,()),(service.refresh,(MasterMonoCommand(expected_revision=started.revision),str(uuid4()))),(service.download_pdf,())):
        with pytest.raises(HTTPException) as error:
            await invoke(database,function,order_id,*args,actor_id=3)
        assert error.value.status_code == 404
    monkeypatch.setattr(settings,"master_mono_enabled",False)
    disabled = await invoke(database,service.read,order_id)
    assert not disabled.enabled and not disabled.can_handover
    with pytest.raises(HTTPException):
        await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=started.revision),str(uuid4()))


async def test_latest_reversal_blocks_handover_and_late_pdf_is_recovered(database, checkout_dependencies, mock_provider):
    _, order_id, started = await prepared(database, mock_provider)
    check = receipt(file=False)
    mock_provider.update(status="success",checks=[check])
    accepted = await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=started.revision),str(uuid4()))
    assert accepted.can_handover and not accepted.receipt_pdf_path
    check["file"] = base64.b64encode(b"%PDF-1.4\nlate\n%%EOF").decode()
    late = await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=accepted.revision),str(uuid4()))
    assert late.receipt_pdf_path
    mock_provider["status"]="reversed"
    reversed_state = await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=late.revision),str(uuid4()))
    assert reversed_state.paid and not reversed_state.receipt_accepted and not reversed_state.can_handover
    async with database() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text("INSERT INTO order_handovers(order_id,operator_id,evidence,simulation,created_at) VALUES(:id,2,'bad',false,now())"),{"id":order_id})
            await session.commit()


async def test_raw_checkout_mutation_and_internal_issuer_are_blocked(database, checkout_dependencies, mock_provider):
    _,order_id,_ = await prepared(database,mock_provider)
    async with database() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE orders SET total_amount=1 WHERE id=:id"),{"id":order_id})
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE master_mono_bindings SET invoice_id='other' WHERE order_id=:id"),{"id":order_id})
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE order_items SET order_id=99999 WHERE order_id=:id"),{"id":order_id})


async def test_auto_poll_accepts_then_current_permission_revocation_blocks(database, checkout_dependencies, mock_provider, monkeypatch):
    from app.services.mono_checkbox_poll import poll_once
    monkeypatch.setattr(settings,"mono_checkbox_auto_enabled",True)
    _,order_id,started = await prepared(database,mock_provider)
    mock_provider.update(status="success",checks=[receipt()])
    assert await poll_once(database) == {"checked":1,"blocked":0}
    accepted = await invoke(database,service.read,order_id)
    assert accepted.revision > started.revision and accepted.can_handover
    async with database() as session:
        actor=await session.get(AdminUser,2)
        actor.can_accept_qr_payments=False
        await session.execute(text("UPDATE mono_checkbox_poll_state SET last_attempt_at=now()-interval '1 day'"))
        await session.commit()
    calls=len(mock_provider["calls"])
    assert await poll_once(database) == {"checked":0,"blocked":1}
    assert len(mock_provider["calls"]) == calls
    with pytest.raises(HTTPException) as error:
        await invoke(database,service.read,order_id)
    assert error.value.status_code==403


async def test_concurrent_refresh_records_one_payment_and_receipt(database, checkout_dependencies, mock_provider):
    _,order_id,started = await prepared(database,mock_provider)
    mock_provider.update(status="success",checks=[receipt()])
    async def attempt():
        try:
            return await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=started.revision),str(uuid4()))
        except HTTPException as error:
            return error
    results=await asyncio.gather(attempt(),attempt())
    assert sum(isinstance(result,HTTPException) for result in results)==1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment).where(OrderPayment.order_id==order_id)) == 1
        assert await session.scalar(select(func.count()).select_from(MasterMonoReceipt).where(MasterMonoReceipt.order_id==order_id)) == 1


async def test_receipt_identity_is_shared_with_shop_imports(database, checkout_dependencies, mock_provider):
    _,order_id,started = await prepared(database,mock_provider)
    check=receipt()
    mock_provider.update(status="success",checks=[check])
    await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=started.revision),str(uuid4()))
    async with database() as session:
        assert await session.scalar(text("SELECT order_id FROM mono_external_receipt_identities WHERE receipt_id=:receipt"),{"receipt":check["id"]})==order_id
        with pytest.raises(DBAPIError) as error:
            await session.execute(text("INSERT INTO shop_mono_checkbox_receipts(order_id,receipt_id) VALUES(:order,:receipt)"),{"order":order_id,"receipt":check["id"]})
        assert "mono_external_receipt_identities_pkey" in str(error.value)


async def test_actual_database_mismatch_blocks_reads_and_handover(database, checkout_dependencies, mock_provider, monkeypatch):
    from app.services import shop_sales
    _, order_id, started = await prepared(database, mock_provider)
    async def unsafe(_session):
        return False
    monkeypatch.setattr(shop_sales, "test_database_is_safe", unsafe)
    before = len(mock_provider["calls"])
    for function, args in ((service.read, ()), (service.download_pdf, ()),
            (service.refresh, (MasterMonoCommand(expected_revision=started.revision),str(uuid4())))):
        with pytest.raises(HTTPException) as error:
            await invoke(database, function, order_id, *args)
        assert error.value.detail["code"] == "master_mono_test_unsafe"
    async with database() as session:
        actor = await session.get(AdminUser, 2)
        order = await local_sales.load(session, order_id)
        with pytest.raises(HTTPException):
            await service.validate_handover(session, actor, order)
    assert len(mock_provider["calls"]) == before
