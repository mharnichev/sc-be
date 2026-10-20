"""Canonical Checkbox domain through actual MockTransport; disposable local PostgreSQL."""
from copy import deepcopy
import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
import httpx
from fastapi import FastAPI
from fastapi import HTTPException

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.product import Product
from app.models.shop_checkbox import (ShopCheckboxRegister as Register, ShopCheckboxOperation as Operation,
    ShopCheckboxDocument as Document, ShopCheckboxAcceptance as Acceptance, ShopCheckboxReturn as FinancialReturn,
    ShopCheckboxPhysicalReceipt as PhysicalReceipt, ShopCheckboxBinding as Binding, ShopCheckboxAudit as Audit)
from app.schemas import shop_checkbox as dto
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import shop_checkbox as service, shop_sales, checkbox_provider
from tests.checkbox_http_stub import CheckboxHTTPStub, sample_profile
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_sales_integration import seed, manual, execute, attach, delivered

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_order_operations_enabled", True)
    monkeypatch.setattr(settings, "shop_checkbox_enabled", True)
    monkeypatch.setattr(settings, "shop_checkbox_live_transport_enabled", False)
    monkeypatch.setattr(settings, "shop_fiscal_mock_enabled", False)
    monkeypatch.setattr(settings, "shop_carrier_returns_mock_enabled", False)
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.UnavailableFiscalPolicy())
    profile = sample_profile(1)
    profile["tenders"]["card"] = deepcopy(profile["tenders"]["cashless"])
    profile["tenders"]["bank_transfer"] = deepcopy(profile["tenders"]["cashless"])
    monkeypatch.setattr(settings, "shop_checkbox_profile", profile)
    for name in ("login", "password", "license_key"):
        monkeypatch.setattr(settings, "shop_checkbox_" + name, SecretStr("synthetic-only"))
    stub = CheckboxHTTPStub(profile)
    monkeypatch.setattr(checkbox_provider, "transport_factory", lambda: stub.transport)
    return stub


def register_payload(**kwargs):
    return dto.RegisterCommand(expected_profile_id=settings.shop_checkbox_profile['id'], **kwargs)


def verify_payload(**kwargs):
    return dto.VerifyCommand(expected_profile_id=settings.shop_checkbox_profile['id'], **kwargs)


async def call(database, fn, *args, **kwargs):
    async with database() as session:
        actor = await session.get(AdminUser, kwargs.pop("actor_id", 1))
        return await fn(session, actor, *args, **kwargs)


async def reg(database):
    return await call(database, service.read, "register")


async def process(database, queued):
    ack = await call(database, service.operation_command, queued.id,
        dto.RevisionCommand(expected_revision=queued.revision), str(uuid4()), "process")
    state = await call(database, service.read, "operation", queued.id)
    assert ack.status == "unknown"
    assert state.status == "accepted", state
    assert state.authority == "test" and state.transport == "mock_http"
    assert state.acceptance_verified
    return state


async def open_shift(database):
    verify = await call(database, service.register_command, verify_payload(confirm=True), str(uuid4()), "verify")
    await process(database, verify)
    register = await reg(database)
    queued = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True),
        str(uuid4()), "shift_open")
    await process(database, queued)


async def sale(database, order_id):
    state = await call(database, service.read, "order", order_id)
    preview = await call(database, service.order_command, order_id,
        dto.RevisionCommand(expected_revision=state.revision), str(uuid4()), "preview")
    doc = await call(database, service.order_command, order_id,
        dto.ApproveCommand(expected_revision=state.revision, preview_id=preview.id, confirm=True), str(uuid4()), "approve")
    await process(database, doc.operation)
    return await call(database, service.read, "document", doc.id)


async def paid(database, quantity=2, kind="pickup", method="cash"):
    order_id, product_id = await seed(database, quantity=quantity, stock=5, kind=kind, method=method)
    payment = manual(method=method)
    payment.amount_minor *= quantity
    await execute(database, order_id, payment, "manual_payment")
    return order_id, product_id


async def test_mock_http_sale_pickup_return_tax_report_and_single_stock_effect(database, flags):
    await open_shift(database)
    order_id, product_id = await paid(database)
    doc = await sale(database, order_id)
    assert doc.is_sent_dps is False and doc.acceptance_verified
    state = await shop_state(database, order_id)
    assert state.fiscal_profile == "checkbox" and state.actions["handover"]
    dispatched = await execute(database, order_id, ShopPhysicalCommand(expected_revision=state.revision, evidence="test pickup"), "handover")
    line = doc.snapshot["items"][0]
    returned = await call(database, service.order_command, order_id, dto.ReturnCommand(expected_revision=dispatched.revision,
        sale_document_id=doc.id, lines=[{"order_item_id": line["order_item_id"], "quantity": 1}], reason="test partial"), str(uuid4()), "create_return")
    returned = await call(database, service.return_command, returned.id, dto.RefundCommand(expected_revision=returned.revision,
        external_reference=uuid4().hex, evidence="synthetic staff refund; no bank transfer"), str(uuid4()), "refund")
    returned = await call(database, service.return_command, returned.id, dto.ReceiptCommand(expected_revision=returned.revision,
        evidence="synthetic pickup receipt", lines=[{"order_item_id": line["order_item_id"], "quantity": 1, "disposition": "restock"}]),
        str(uuid4()), "physical_receipt")
    preview = await call(database, service.order_command, order_id, dto.RevisionCommand(expected_revision=returned.revision),
        str(uuid4()), "preview", return_id=returned.id)
    result = await call(database, service.order_command, order_id, dto.ApproveCommand(expected_revision=returned.revision,
        preview_id=preview.id, confirm=True), str(uuid4()), "approve", return_id=returned.id)
    await process(database, result.operation)
    register = await reg(database)
    x = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "x")
    x = await process(database, x)
    assert x.result["totals"] == dict(sales_minor=20000, returns_minor=10000, net_minor=10000, sale_count=1, return_count=1)
    assert x.result["fiscal_code"] is None
    register = await reg(database)
    z = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "z")
    z = await process(database, z)
    assert z.result["z_report"]["totals"] == x.result["totals"]
    assert (await reg(database)).shift_status == "closed"
    async with database() as session:
        product = await session.get(Product, product_id)
        assert product.stock_quantity == 4 and product.reserved_quantity == 0
        assert await session.scalar(select(func.count()).select_from(Document)) == 2
    assert [w[1] for w in flags.state["writes"]].count("/api/v1/receipts/sell") == 2


async def shop_state(database, order_id):
    return await call(database, shop_sales.read, order_id)


async def prepared(database):
    await open_shift(database)
    order_id, product_id = await paid(database)
    document = await sale(database, order_id)
    return order_id, product_id, document


async def queued_sale(database):
    await open_shift(database)
    order_id, _ = await paid(database)
    state = await shop_state(database, order_id)
    preview = await call(database, service.order_command, order_id, dto.RevisionCommand(expected_revision=state.revision), str(uuid4()), "preview")
    doc = await call(database, service.order_command, order_id, dto.ApproveCommand(expected_revision=state.revision, preview_id=preview.id, confirm=True), str(uuid4()), "approve")
    return order_id, doc


async def test_lost_receipt_response_recovery_flagoff_exact_cached_http_replay(database, flags, monkeypatch, tmp_path):
    from app.api.v1.routes.shop_checkbox import backoffice_router
    from app.core.database import get_db_session
    from app.dependencies.auth import get_current_admin_user
    order_id, doc = await queued_sale(database)
    durable = tmp_path / "checkbox-provider-store.json"
    flags.path = durable
    flags.lose_after = "/api/v1/receipts/sell"
    payload = dto.RevisionCommand(expected_revision=doc.revision)
    key = str(uuid4())
    ack = await call(database, service.operation_command, doc.operation.id, payload, key, "process")
    assert ack.status == "unknown"
    assert not (await call(database, service.read, "document", doc.id)).acceptance_verified
    restored = CheckboxHTTPStub(flags.profile, durable)
    monkeypatch.setattr(checkbox_provider, "transport_factory", lambda: restored.transport)
    monkeypatch.setattr(settings, "shop_checkbox_enabled", False)
    app = FastAPI();app.include_router(backoffice_router, prefix="/api/v1/backoffice")
    async def db():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await session.get(AdminUser, 1)
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        path = f"/api/v1/backoffice/shop-checkbox/documents/{doc.id}/process"
        replay = await client.post(path, json=payload.model_dump(), headers={"Idempotency-Key": key})
        assert replay.status_code == 200 and replay.json() == ack.model_dump(mode="json")
        changed = await client.post(path, json={"expected_revision": payload.expected_revision+1}, headers={"Idempotency-Key": key})
        assert changed.status_code == 409
        current = await call(database, service.read, "operation", doc.operation.id)
        recovered = await client.post(f"/api/v1/backoffice/shop-checkbox/operations/{current.id}/reconcile",
            json={"expected_revision": current.revision}, headers={"Idempotency-Key": str(uuid4())})
        assert recovered.status_code == 200 and recovered.json()["acceptance_verified"]
    state = await shop_state(database, order_id)
    assert state.fiscal_profile == "checkbox" and not state.actions["handover"]
    assert len(restored.state["receipts"]) == 1
    assert not any(method == "POST" and path == "/api/v1/receipts/sell" for method,path,_ in restored.calls)


async def test_unknown_shift_and_z_lookup_original_uuid_and_close_alias(database, flags):
    verify = await call(database, service.register_command, verify_payload(confirm=True), str(uuid4()), "verify")
    await process(database, verify)
    register = await reg(database)
    opening = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "shift_open")
    flags.lose_after = "/api/v1/shifts"
    ack = await call(database, service.operation_command, opening.id, dto.RevisionCommand(expected_revision=opening.revision), str(uuid4()), "process")
    current = await call(database, service.read, "operation", opening.id)
    assert ack.status == current.status == "unknown"
    await call(database, service.operation_command, current.id, dto.ReconcileCommand(expected_revision=current.revision), str(uuid4()), "reconcile")
    register = await reg(database)
    assert register.shift_status == "open" and register.shift_id == opening.provider_uuid
    payload = register_payload(expected_revision=register.revision, confirm=True);key=str(uuid4())
    close = await call(database, service.register_command, payload, key, "shift_close")
    alias = await call(database, service.register_command, payload, key, "z")
    assert alias == close
    flags.lose_after = "/api/v1/shifts/close"
    await call(database, service.operation_command, close.id, dto.RevisionCommand(expected_revision=close.revision), str(uuid4()), "process")
    current = await call(database, service.read, "operation", close.id)
    await call(database, service.operation_command, close.id, dto.ReconcileCommand(expected_revision=current.revision), str(uuid4()), "reconcile")
    assert (await reg(database)).shift_status == "closed"
    assert [w[1] for w in flags.state["writes"]].count("/api/v1/shifts/close") == 1


async def test_unknown_x_requires_verified_candidate_and_never_reposts(database, flags):
    await open_shift(database)
    register = await reg(database)
    queued = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "x")
    flags.lose_after = "/api/v1/reports"
    await call(database, service.operation_command, queued.id, dto.RevisionCommand(expected_revision=queued.revision), str(uuid4()), "process")
    current = await call(database, service.read, "operation", queued.id)
    with pytest.raises(HTTPException) as error:
        await call(database, service.operation_command, queued.id, dto.ReconcileCommand(expected_revision=current.revision), str(uuid4()), "reconcile")
    assert error.value.detail["code"] == "checkbox_report_candidate_required"
    candidate = next(iter(flags.state["reports"]))
    result = await call(database, service.operation_command, queued.id,
        dto.ReconcileCommand(expected_revision=current.revision, candidate_report_uuid=candidate), str(uuid4()), "reconcile")
    assert result.acceptance_verified and result.result["fiscal_code"] is None
    assert [w[1] for w in flags.state["writes"]].count("/api/v1/reports") == 1


async def test_sale_original_shift_remains_valid_after_z_and_new_shift(database, flags):
    order_id, _, doc = await prepared(database)
    original = doc.shift_id
    register = await reg(database)
    close = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "z")
    await process(database, close)
    register = await reg(database)
    opening = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "shift_open")
    await process(database, opening)
    assert (await reg(database)).shift_id != original
    state = await shop_state(database, order_id)
    assert state.actions["handover"]
    state = await execute(database, order_id, ShopPhysicalCommand(expected_revision=state.revision, evidence="test pickup after shift"), "handover")
    state = await execute(database, order_id, ShopPhysicalCommand(expected_revision=state.revision, evidence="test complete"), "complete")
    assert state.completion.authority == "test"
    assert len(flags.state["receipts"]) == 1


async def test_register_fence_blocks_new_money_profile_alias_and_flagoff(database, flags, monkeypatch):
    await open_shift(database)
    order_id, _ = await seed(database, method="cash")
    register = await reg(database)
    pending = await call(database, service.register_command, register_payload(expected_revision=register.revision, confirm=True), str(uuid4()), "x")
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, manual(method="cash"), "manual_payment")
    assert error.value.detail["code"] == "checkbox_register_busy"
    alternate = deepcopy(settings.shop_checkbox_profile);alternate["id"] = "same-physical-register-new-name"
    monkeypatch.setattr(settings, "shop_checkbox_profile", alternate)
    with pytest.raises(HTTPException) as error:
        await call(database, service.register_command, verify_payload(confirm=True), str(uuid4()), "verify")
    assert error.value.detail["code"] == "checkbox_profile_conflict"
    monkeypatch.setattr(settings, "shop_checkbox_profile", flags.profile)
    await call(database, service.cancel_operation, pending.id, dto.EvidenceCommand(expected_revision=pending.revision, evidence="cancel undispatched"), str(uuid4()))
    # Bind an unpaid order through the policy (same durable binding as unpaid COD dispatch).
    async with database() as session:
        order = await shop_sales.load_order(session, order_id, lock=True)
        await service.payment_barrier(session, order)
        await session.commit()
    monkeypatch.setattr(settings, "shop_checkbox_enabled", False)
    with pytest.raises(HTTPException) as error:
        await execute(database, order_id, manual(method="cash"), "manual_payment")
    assert error.value.detail["code"] == "shop_checkbox_disabled"
    async with database() as session:
        assert not (await shop_sales.load_order(session, order_id)).payments


async def test_known_no_dispatch_rejection_unblocks_register_and_fresh_document(database, flags):
    _, doc = await queued_sale(database)
    # Wrong server-side tax metadata fails identity preflight before receipt POST.
    actual = flags.profile["taxes"]["1"]["rate"]
    flags.profile["taxes"]["1"]["rate"] = "7"
    ack = await call(database, service.operation_command, doc.operation.id, dto.RevisionCommand(expected_revision=doc.revision), str(uuid4()), "process")
    assert ack.status == "unknown"
    rejected = await call(database, service.read, "operation", doc.operation.id)
    assert rejected.status == "rejected" and not rejected.acceptance_verified
    assert not flags.state["receipts"] and (await reg(database)).pending_operation is None
    flags.profile["taxes"]["1"]["rate"] = actual
    fresh = await sale(database, doc.order_id)
    assert fresh.provider_uuid != doc.provider_uuid and fresh.acceptance_verified


@pytest.mark.parametrize("failure", ["duplicate", "overvalue", "wrong_tax", "crossprofile", "missing_quantity", "null_quantity", "missing_total", "null_total"])
async def test_raw_sql_return_allocation_and_profile_bindings_reject(database, flags, failure):
    order_id, _, doc = await prepared(database)
    state = await shop_state(database, order_id)
    await execute(database, order_id, ShopPhysicalCommand(expected_revision=state.revision, evidence="synthetic pickup"), "handover")
    line = dict(doc.snapshot["items"][0], quantity=1, total_minor=10000)
    lines = [line];register_id=flags.profile["id"]
    async with database() as session:
        if failure == "duplicate":
            lines = [line, deepcopy(line)]
        elif failure == "overvalue":
            line["total_minor"] += 100000
        elif failure == "wrong_tax":
            line["tax_codes"] = [999]
        elif failure.startswith("missing_"):
            line.pop("quantity" if failure.endswith("quantity") else "total_minor")
        elif failure.startswith("null_"):
            line["quantity" if failure.endswith("quantity") else "total_minor"] = None
        elif failure == "crossprofile":
            profile = deepcopy(flags.profile);profile["id"]="different-physical-profile";profile["register_id"]=str(uuid4())
            register_id=profile["id"]
            session.add(Register(id=register_id, profile=profile, profile_hash=service.digest(profile), authority="test",
                transport="mock_http", revision=1, shift_status="open", shift_id=doc.shift_id))
            await session.commit()
        session.add(FinancialReturn(order_id=order_id, sale_document_id=doc.id, register_id=register_id,
            shift_id=doc.shift_id, revision=1, amount_minor=sum(l.get("total_minor") or 10000 for l in lines), lines=lines,
            reason="synthetic invalid raw SQL allocation", operator_id=1, created_at=service.datetime.now(service.UTC)))
        with pytest.raises(DBAPIError):
            await session.commit()
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(FinancialReturn)) == 0


@pytest.mark.parametrize("failure", ["empty", "wrong_quantity", "no_ledger", "missing_quantity", "null_quantity", "missing_disposition", "null_disposition"])
async def test_raw_sql_physical_receipt_requires_exact_once_ledger(database, flags, failure):
    order_id, _, doc = await prepared(database)
    state = await shop_state(database, order_id)
    dispatched = await execute(database, order_id, ShopPhysicalCommand(expected_revision=state.revision, evidence="synthetic pickup"), "handover")
    line = doc.snapshot["items"][0]
    item = await call(database, service.order_command, order_id, dto.ReturnCommand(expected_revision=dispatched.revision,
        sale_document_id=doc.id, lines=[{"order_item_id":line["order_item_id"],"quantity":1}],reason="synthetic return"),str(uuid4()),"create_return")
    lines = [{"order_item_id":line["order_item_id"],"quantity":2 if failure=="wrong_quantity" else 1,"disposition":"restock"}]
    if failure == "empty":
        lines = []
    elif failure.startswith("missing_"):
        lines[0].pop(failure.removeprefix("missing_"))
    elif failure.startswith("null_"):
        lines[0][failure.removeprefix("null_")] = None
    async with database() as session:
        session.add(PhysicalReceipt(return_id=item.id, lines=lines, evidence="invalid synthetic physical SQL", operator_id=1,
            created_at=service.datetime.now(service.UTC)))
        with pytest.raises(DBAPIError):
            await session.commit()
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(PhysicalReceipt)) == 0


@pytest.mark.parametrize("failure", ["tax", "tender", "cash_fraction"])
async def test_payment_preflight_rejects_before_money_or_profile_binding(database, flags, monkeypatch, failure):
    profile = deepcopy(settings.shop_checkbox_profile)
    if failure == "tax":
        # Another valid configured product keeps the profile valid but not this order.
        profile["product_tax_codes"] = {"999":[1]}
    elif failure == "tender":
        profile["tenders"].pop("cash")
    if failure != "cash_fraction":
        monkeypatch.setattr(settings,"shop_checkbox_profile",profile)
        flags.profile=deepcopy(profile)
    await open_shift(database)
    order_id, product_id = await seed(database, method="cash")
    if failure=="cash_fraction":
        async with database() as session:
            order = await shop_sales.load_order(session, order_id)
            order.total_amount=Decimal("99.99");await session.commit()
    payment=manual(method="cash");payment.amount_minor=9999 if failure=="cash_fraction" else 10000
    with pytest.raises(HTTPException) as error:
        await execute(database,order_id,payment,"manual_payment")
    expected={"tax":"checkbox_tax_profile_invalid","tender":"checkbox_payment_mapping_invalid","cash_fraction":"checkbox_cash_rounding_unsupported"}
    assert error.value.detail["code"]==expected[failure]
    async with database() as session:
        assert not (await shop_sales.load_order(session,order_id)).payments
        assert await session.get(Binding,order_id) is None


async def test_lost_terminal_error_proof_allows_new_receipt_without_duplicate_money(database, flags):
    _,doc=await queued_sale(database)
    flags.reject_next_receipt=True;flags.lose_after="/api/v1/receipts/sell"
    await call(database,service.operation_command,doc.operation.id,dto.RevisionCommand(expected_revision=doc.revision),str(uuid4()),"process")
    unknown=await call(database,service.read,"operation",doc.operation.id)
    result=await call(database,service.operation_command,unknown.id,dto.ReconcileCommand(expected_revision=unknown.revision),str(uuid4()),"reconcile")
    assert result.status=="rejected" and not result.acceptance_verified
    fresh=await sale(database,doc.order_id)
    assert fresh.acceptance_verified and fresh.provider_uuid!=doc.provider_uuid
    async with database() as session:
        order=await shop_sales.load_order(session,doc.order_id)
        assert len(order.payments)==1
        assert await session.scalar(select(func.count()).select_from(Acceptance).where(Acceptance.operation_id==doc.operation.id))==0


async def test_payment_vs_z_race_cannot_close_over_unreceipted_money(database, flags):
    await open_shift(database)
    order_id,_=await seed(database,method="cash")
    register=await reg(database)
    start=asyncio.Event()
    async def money():
        await start.wait()
        return await execute(database,order_id,manual(method="cash"),"manual_payment")
    async def close():
        await start.wait()
        return await call(database,service.register_command,register_payload(expected_revision=register.revision,confirm=True),str(uuid4()),"z")
    tasks=[asyncio.create_task(money()),asyncio.create_task(close())];start.set()
    results=await asyncio.gather(*tasks,return_exceptions=True)
    assert sum(isinstance(r,HTTPException) and r.status_code==409 for r in results)==1,results
    async with database() as session:
        order=await shop_sales.load_order(session,order_id)
        pending=await session.scalar(select(Operation).where(Operation.status.in_(service.PENDING)))
        assert bool(order.payments) != bool(pending)
    assert not any(w[1]=="/api/v1/shifts/close" for w in flags.state["writes"])


async def test_same_key_concurrent_dispatch_and_revoked_replay(database, flags):
    _,doc=await queued_sale(database)
    key=str(uuid4());payload=dto.RevisionCommand(expected_revision=doc.revision)
    results=await asyncio.gather(*(call(database,service.operation_command,doc.operation.id,payload,key,"process") for _ in range(2)))
    assert results[0]==results[1] and results[0].status=="unknown"
    assert [w[1] for w in flags.state["writes"]].count("/api/v1/receipts/sell")==1
    async with database() as session:
        actor=await session.get(AdminUser,1);actor.is_superuser=False;actor.can_fiscalize_sales=False
        await session.commit()
    with pytest.raises(HTTPException) as error:
        await call(database,service.operation_command,doc.operation.id,payload,key,"process")
    assert error.value.status_code==403


@pytest.mark.parametrize("method,tender_type", [("cashOnDelivery","CASH"),("cardOnDelivery","CASHLESS")])
async def test_checkbox_policy_cod_dispatch_then_delivered_payment_receipt_complete(database,flags,method,tender_type):
    await open_shift(database)
    order_id,product_id=await seed(database,method=method,kind="nova_poshta")
    shipment_id,_=await attach(database,order_id)
    state=await shop_state(database,order_id)
    dispatched=await execute(database,order_id,ShopPhysicalCommand(expected_revision=state.revision,evidence="synthetic COD dispatch"),"handover")
    assert dispatched.fulfillment.authority=="test" and not dispatched.payment
    await delivered(database,shipment_id)
    paid_state=await execute(database,order_id,manual(revision=dispatched.revision,method="cod"),"manual_payment")
    doc=await sale(database,order_id)
    assert doc.snapshot["payment"]["type"]==tender_type
    state=await shop_state(database,order_id)
    completed=await execute(database,order_id,ShopPhysicalCommand(expected_revision=state.revision,evidence="synthetic COD complete"),"complete")
    assert completed.completion and completed.completion.authority=="test"
    current=await call(database,service.read,"order",order_id)
    assert not current.actions["create_return"] and "carrier_return_pending_live" in current.blocking_reasons
    register=await reg(database)
    z=await call(database,service.register_command,register_payload(expected_revision=register.revision,confirm=True),str(uuid4()),"z")
    await process(database,z)
    async with database() as session:
        product=await session.get(Product,product_id)
        assert product.stock_quantity==3 and product.reserved_quantity==0


@pytest.mark.parametrize("failure",["tax","tender","cash_fraction"])
async def test_unpaid_cod_preflight_never_dispatches_unrepresentable_order(database,flags,monkeypatch,failure):
    profile=deepcopy(settings.shop_checkbox_profile)
    if failure=="tax":profile["product_tax_codes"]={"999":[1]}
    if failure=="tender":profile["tenders"].pop("cashOnDelivery")
    monkeypatch.setattr(settings,"shop_checkbox_profile",profile);flags.profile=deepcopy(profile)
    await open_shift(database)
    order_id,product_id=await seed(database,method="cashOnDelivery",kind="nova_poshta")
    await attach(database,order_id)
    if failure=="cash_fraction":
        async with database() as session:
            order=await shop_sales.load_order(session,order_id);order.total_amount=Decimal("99.99");await session.commit()
    state=await shop_state(database,order_id)
    assert not state.actions["handover"]
    with pytest.raises(HTTPException):
        await execute(database,order_id,ShopPhysicalCommand(expected_revision=state.revision,evidence="must not dispatch"),"handover")
    async with database() as session:
        order=await shop_sales.load_order(session,order_id);product=await session.get(Product,product_id)
        assert not order.handovers and not order.payments
        assert product.stock_quantity==4 and product.reserved_quantity==1
        assert await session.get(Binding,order_id) is None


async def test_expected_profile_assertion_prevents_stale_create_on_new_register(database,flags,monkeypatch):
    old=verify_payload(confirm=True)
    profile=deepcopy(settings.shop_checkbox_profile);profile["id"]="new-profile";profile["register_id"]=str(uuid4())
    monkeypatch.setattr(settings,"shop_checkbox_profile",profile)
    with pytest.raises(HTTPException) as error:
        await call(database,service.register_command,old,str(uuid4()),"verify")
    assert error.value.detail["code"]=="checkbox_profile_conflict"
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Register))==0
        assert await session.scalar(select(func.count()).select_from(Operation))==0
    assert not flags.state["writes"]


async def test_discount_keeps_captured_unit_price_and_exact_partial_wire_value(database,flags):
    await open_shift(database)
    order_id,_=await seed(database,method="bank_transfer",quantity=2,stock=5)
    async with database() as session:
        order=await shop_sales.load_order(session,order_id);order.total_amount=Decimal("190.00");order.discount_amount=Decimal("10.00")
        await session.commit()
    payment=manual(method="bank_transfer");payment.amount_minor=19000
    await execute(database,order_id,payment,"manual_payment")
    doc=await sale(database,order_id)
    payload=next(w[2] for w in flags.state["writes"] if w[1]=="/api/v1/receipts/sell")
    assert payload["goods"][0]["good"]["price"]==10000
    assert payload["goods"][0]["quantity"]==2000 and payload["goods"][0]["discounts"][0]["value"]==1000
    state=await shop_state(database,order_id)
    state=await execute(database,order_id,ShopPhysicalCommand(expected_revision=state.revision,evidence="synthetic discounted pickup"),"handover")
    item=await call(database,service.order_command,order_id,dto.ReturnCommand(expected_revision=state.revision,sale_document_id=doc.id,
        lines=[{"order_item_id":doc.snapshot["items"][0]["order_item_id"],"quantity":1}],reason="partial discounted"),str(uuid4()),"create_return")
    assert item.amount_minor==9500 and item.lines[0]["unit_price_minor"]==10000


async def test_readiness_bootstrap_get_is_readonly_and_accepted_history_refuses_downgrade(database,flags,template_database):
    first=await call(database,service.read,"readiness")
    assert first.profile_id==flags.profile["id"] and first.register is None
    assert not flags.calls
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Register))==0
    await open_shift(database)
    from alembic import command
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    historical=ScriptDirectory.from_config(template_database[3]).get_revision("0125_shop_checkbox").module

    async def facts():
        async with database() as session:
            return {table:(await session.execute(text(
                f'SELECT row_to_json(t)::text FROM {table} t ORDER BY row_to_json(t)::text COLLATE "C"'
            ))).scalars().all() for table in historical.TABLES}

    before=await facts()
    assert all(before[table] for table in (
        "shop_checkbox_registers", "shop_checkbox_operations", "shop_checkbox_acceptances"))
    with pytest.raises(RuntimeError,match="^Canonical collation history incompatible: shop_checkbox_registers; no facts or hashes changed$"):
        await asyncio.to_thread(command.downgrade,template_database[3],"0124_shop_carrier_returns")
    async with database() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version"))=="0134_shop_canonical_collation"
    assert await facts()==before

    # Exercise the historical guard directly; do not bypass the newer guard
    # or alter the revision, canonicalizer, or accepted fixture history.
    def historical_guard(connection):
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError,match="^Accepted Shop Checkbox history prevents downgrade$"):
                historical.downgrade()
    async with database() as session:
        connection=await session.connection()
        await connection.run_sync(historical_guard)
        assert await session.scalar(text("SELECT version_num FROM alembic_version"))=="0134_shop_canonical_collation"
    assert await facts()==before


@pytest.mark.parametrize("verified",[False,True])
async def test_new_money_requires_verified_open_register(database,flags,verified):
    if verified:
        queued=await call(database,service.register_command,verify_payload(confirm=True),str(uuid4()),"verify")
        await process(database,queued)
    else:
        async with database() as session:
            await service.register(session,service.gateway(),create=True)
            await session.commit()
    order_id,_=await seed(database,method="cash")
    with pytest.raises(HTTPException) as error:
        await execute(database,order_id,manual(method="cash"),"manual_payment")
    assert error.value.detail["code"]==("checkbox_shift_closed" if verified else "checkbox_identity_required")
    async with database() as session:
        assert not (await shop_sales.load_order(session,order_id)).payments
        assert await session.get(Binding,order_id) is None


async def test_fiscal_only_profile_read_does_not_widen_b1_or_allow_master(database,flags):
    from app.models.booking import Master
    order_id,_=await seed(database,method="cash")
    async with database() as session:
        actor=await session.get(AdminUser,1)
        actor.is_superuser=False;actor.can_record_sales=False;actor.can_fiscalize_sales=True
        await session.commit()
    profile=await call(database,service.read,"profile",order_id)
    assert profile.fiscal_profile=="checkbox"
    assert not flags.calls
    with pytest.raises(HTTPException) as error:
        await shop_state(database,order_id)
    assert error.value.status_code==403
    async with database() as session:
        session.add(Master(admin_user_id=1,full_name="Synthetic restricted master",is_active=True));await session.commit()
    with pytest.raises(HTTPException) as error:
        await call(database,service.read,"profile",order_id)
    assert error.value.status_code==403


@pytest.mark.parametrize("failure",["null_proof","missing_binding","wrong_hash","contradictory_code"])
async def test_raw_rejection_audit_cannot_clear_unknown_without_exact_proof(database,flags,failure):
    _,doc=await queued_sale(database)
    flags.reject_next_receipt=True;flags.lose_after="/api/v1/receipts/sell"
    await call(database,service.operation_command,doc.operation.id,dto.RevisionCommand(expected_revision=doc.revision),str(uuid4()),"process")
    async with database() as session:
        op=await service.load_operation(session,doc.operation.id)
        proof=await service.gateway().get_receipt(op.provider_uuid,op.snapshot)
        assert proof["rejected"] is True
        if failure=="null_proof":proof=None
        elif failure=="missing_binding":proof.pop("shift_id")
        elif failure=="contradictory_code":proof["fiscal_code"]="TEST-EXISTING-FISCAL-CODE"
        evidence=Audit(operator_id=1,operation="provider_rejection",facts={"operation_id":op.id,
            "provider_uuid":op.provider_uuid,"snapshot_hash":op.snapshot_hash,"proof":proof,
            "proof_hash":"0"*64 if failure=="wrong_hash" else service.digest(proof),
            "proof_canonical":service.canonical_json(proof)},created_at=service.datetime.now(service.UTC))
        session.add(evidence);await session.flush([evidence]);op.status="rejected"
        with pytest.raises(DBAPIError):await session.commit()
        await session.rollback()
        assert (await service.load_operation(session,doc.operation.id)).status=="unknown"


@pytest.mark.parametrize("failure",["wrong_id","wrong_amount","wrong_profile","wrong_hash"])
async def test_raw_acceptance_requires_exact_dispatch_and_canonical_proof(database,flags,failure):
    _,doc=await queued_sale(database)
    flags.lose_after="/api/v1/receipts/sell"
    await call(database,service.operation_command,doc.operation.id,dto.RevisionCommand(expected_revision=doc.revision),str(uuid4()),"process")
    async with database() as session:
        op=await service.load_operation(session,doc.operation.id)
        proof=await service.gateway().get_receipt(op.provider_uuid,op.snapshot)
        if failure=="wrong_id":proof["id"]=str(uuid4())
        elif failure=="wrong_amount":proof["amount_minor"]+=1
        elif failure=="wrong_profile":proof["register_id"]=str(uuid4())
        session.add(Acceptance(operation_id=op.id,provider_id="receipt:"+op.provider_uuid,snapshot_hash=op.snapshot_hash,
            proof=proof,proof_canonical=service.canonical_json(proof),proof_hash="0"*64 if failure=="wrong_hash" else service.digest(proof),
            accepted_at=service.datetime.now(service.UTC)))
        with pytest.raises(DBAPIError):await session.commit()
        await session.rollback()
        assert (await service.load_operation(session,doc.operation.id)).status=="unknown"


async def test_permission_revoked_during_known_no_dispatch_still_saves_technical_rejection(database,flags,monkeypatch):
    _,doc=await queued_sale(database)
    boundary=asyncio.Event();revoked=asyncio.Event()
    flags.profile["taxes"]["1"]["rate"]="7"
    async def transport(request):
        if request.url.path=="/api/v1/cash-registers/info":
            boundary.set();await revoked.wait()
        return flags.handle(request)
    mock=httpx.MockTransport(transport)
    monkeypatch.setattr(checkbox_provider,"transport_factory",lambda:mock)
    async def revoke():
        await boundary.wait()
        async with database() as session:
            actor=await session.get(AdminUser,1);actor.is_superuser=False;actor.can_fiscalize_sales=False;actor.is_active=False
            await session.commit()
        revoked.set()
    key=str(uuid4());payload=dto.RevisionCommand(expected_revision=doc.revision)
    ack,_=await asyncio.gather(call(database,service.operation_command,doc.operation.id,payload,key,"process"),revoke())
    assert ack.status=="unknown" and not flags.state["receipts"]
    async with database() as session:
        op=await service.load_operation(session,doc.operation.id)
        assert op.status=="rejected" and not service.truth_valid(op)
        row=await service.register(session,register_id=flags.profile["id"])
        assert await service.pending(session,row) is None
        proof=await session.scalar(select(Audit).where(Audit.operation=="provider_no_dispatch"))
        assert proof.operator_id==1 and proof.facts["business_request_dispatched"] is False
    for fn,args in [(service.operation_command,(doc.operation.id,payload,key,"process")),
                    (service.register_command,(verify_payload(confirm=True),str(uuid4()),"verify"))]:
        with pytest.raises(HTTPException) as error:await call(database,fn,*args)
        assert error.value.status_code==403


async def test_selected_checkbox_flagoff_rejects_unbound_new_money(database,flags,monkeypatch):
    await open_shift(database)
    order_id,_=await seed(database,method="cash")
    monkeypatch.setattr(settings,"shop_checkbox_enabled",False)
    state=await shop_state(database,order_id)
    assert state.fiscal_profile=="checkbox" and not state.actions["record_manual_payment"]
    with pytest.raises(HTTPException) as error:
        await execute(database,order_id,manual(method="cash"),"manual_payment")
    assert error.value.detail["code"]=="shop_checkbox_disabled"
    async with database() as session:
        assert not (await shop_sales.load_order(session,order_id)).payments
        assert await session.get(Binding,order_id) is None


async def test_historical_unbound_money_cannot_be_adopted_by_service_or_raw_binding(database,flags,monkeypatch):
    profile=settings.shop_checkbox_profile
    monkeypatch.setattr(settings,"shop_checkbox_enabled",False)
    monkeypatch.setattr(settings,"shop_checkbox_profile",None)
    order_id,_=await seed(database,method="cash")
    await execute(database,order_id,manual(method="cash"),"manual_payment")
    monkeypatch.setattr(settings,"shop_checkbox_enabled",True)
    monkeypatch.setattr(settings,"shop_checkbox_profile",profile)
    await open_shift(database)
    state=await call(database,service.read,"order",order_id)
    assert not state.enabled and not state.actions["sale_preview"] and not state.actions["approve_sale"]
    assert "checkbox_history_unbound" in state.blocking_reasons
    before=len(flags.state["receipts"])
    with pytest.raises(HTTPException) as error:
        await call(database,service.order_command,order_id,dto.RevisionCommand(expected_revision=state.revision),str(uuid4()),"preview")
    assert error.value.detail["code"]=="checkbox_history_unbound"
    async with database() as session:
        row=await service.register(session,register_id=profile["id"])
        session.add(Binding(order_id=order_id,register_id=row.id,profile_hash=row.profile_hash,authority=row.authority,
            created_at=service.datetime.now(service.UTC)))
        with pytest.raises(DBAPIError):await session.commit()
        await session.rollback()
        assert await session.get(Binding,order_id) is None
        order=await shop_sales.load_order(session,order_id)
        assert len(order.payments)==1 and not order.handovers
        assert not order.shop_checkbox_documents
    assert len(flags.state["receipts"])==before
