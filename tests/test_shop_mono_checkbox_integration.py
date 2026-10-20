"""Direct receipts through migrated PG + real gateway MockTransport; no provider writes."""
import asyncio
import base64
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import OrderPayment
from app.models.shop_mono_checkbox import ShopMonoCheckboxReceipt as Receipt, ShopMonoCheckboxObservation as Observation
from app.models.shop_sales import ShopOperationAudit
from app.schemas import shop_payments as payments_dto
from app.schemas.shop_mono_checkbox import RefreshReceiptCommand
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import monobank_provider, shop_payments as payments, shop_sales as sales, shop_mono_checkbox as direct
from app.api.v1.routes import shop_payments as routes
from tests.test_shop_fiscal_integration import template_database, database, checkout_dependencies  # noqa: F401
from tests.test_shop_sales_integration import seed, execute, attach, delivered

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    for name, value in dict(app_env="test", shop_order_operations_enabled=True, shop_payments_enabled=True,
            shop_mono_checkbox_enabled=True, shop_fiscal_mock_enabled=False, shop_checkbox_enabled=False,
            shop_checkbox_profile=None, shop_monobank_live_transport_enabled=False, shop_checkout_enabled=False).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(sales, "fiscal_policy", sales.UnavailableFiscalPolicy())
    monkeypatch.setattr(settings, "shop_monobank_token", SecretStr("mono-checkbox-synthetic-only"))
    profile = dict(id="synthetic-direct", merchant_id="synthetic-owner", environment="test",
        token_sha256=sha256(b"mono-checkbox-synthetic-only").hexdigest(), external_fiscalization_disabled=False)
    monkeypatch.setattr(settings, "shop_monobank_profile", profile)
    check = dict(id=str(uuid4()), status="done", type="sale", fiscalizationSource="checkbox",
        taxUrl="https://cabinet.tax.gov.ua/cashregs/check?id=synthetic", file=base64.b64encode(b"%PDF-1.4\nsynthetic\n%%EOF").decode())
    state = dict(reference=None, checks=[check], calls=[], invoice_status="success", receipt_error=False, mutate=None)
    def handler(request):
        assert request.method == "GET"
        state["calls"].append(request.url.path)
        if request.url.path == "/api/merchant/details":
            return httpx.Response(200, json={"merchantId": profile["merchant_id"]})
        if request.url.path == "/api/merchant/invoice/fiscal-checks":
            if state["receipt_error"]:
                raise httpx.ReadTimeout("synthetic")
            if state["mutate"]:
                state["mutate"]()
            return httpx.Response(200, json={"checks":state["checks"]})
        now = datetime.now(UTC).isoformat()
        return httpx.Response(200, json=dict(invoiceId=request.url.params["invoiceId"], reference=state["reference"],
            ccy=980, amount=10000, finalAmount=10000, status=state["invoice_status"], createdDate=now, modifiedDate=now))
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(payments, "gateway", lambda: monobank_provider.MonobankGateway(transport=transport))
    return state


def assertion():
    profile = settings.shop_monobank_profile
    return dict(expected_profile_id=profile["id"], expected_profile_hash=direct.digest(profile))


async def call(database, function, *args, actor_id=1, **kwargs):
    async with database() as session:
        return await function(session, await session.get(AdminUser, actor_id), *args, **kwargs)


async def paid(database, flags, **kwargs):
    identifier, product_id = await seed(database, **kwargs)
    intent = await call(database, payments.command, identifier, payments_dto.InvoiceIntentCommand(expected_revision=1, **assertion()), str(uuid4()), "invoice_intent")
    flags["reference"] = intent.intent.reference
    result = await call(database, payments.command, identifier, payments_dto.VerifyInvoiceCommand(
        expected_revision=intent.revision, invoice_id="syntheticInvoice_" + uuid4().hex, **assertion()), str(uuid4()), "verify")
    assert result.payment
    return identifier, product_id, result


async def refresh(database, identifier, revision, *, key=None):
    return await call(database, direct.refresh, identifier, RefreshReceiptCommand(expected_revision=revision, **assertion()), key or str(uuid4()))


async def test_paid_receipt_import_handover_completion_pdf_and_once_only_stock(database, flags):
    identifier, product_id, payment = await paid(database, flags)
    assert not (await call(database, sales.read, identifier)).actions["handover"]
    key = str(uuid4())
    accepted = await refresh(database, identifier, payment.revision, key=key)
    assert accepted.accepted and accepted.paid and accepted.authority == "test"
    assert not accepted.structured_totals_verified and not accepted.reports_imported
    calls = len(flags["calls"])
    assert await refresh(database, identifier, payment.revision, key=key) == accepted
    assert len(flags["calls"]) == calls
    receipt_id, content = await call(database, direct.download_pdf, identifier)
    assert receipt_id == flags["checks"][0]["id"] and content.startswith(b"%PDF-")
    state = await call(database, sales.read, identifier)
    assert state.fiscal_profile == "mono_checkbox_external" and state.actions["handover"]
    assert state.order.fiscal_status == "simulated_mono_checkbox_external_accepted"
    handed = await execute(database, identifier, ShopPhysicalCommand(expected_revision=state.revision, evidence="Synthetic pickup"), "handover")
    assert handed.actions["complete"]
    completed = await execute(database, identifier, ShopPhysicalCommand(expected_revision=handed.revision, evidence="Synthetic completed"), "complete")
    assert completed.completion and completed.completion.authority == "test"
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.product_id == product_id, InventoryMovement.movement_type == InventoryMovementType.shipment)) == 1
        assert await session.scalar(select(func.count()).select_from(ShopOperationAudit).where(ShopOperationAudit.operation == "mono_checkbox_pdf_read")) == 1
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE shop_mono_checkbox_receipts SET receipt_id=receipt_id"))


@pytest.mark.parametrize("status", ["new", "process", "failed", "unavailable"])
async def test_pending_failed_unavailable_keep_paid_block_stock_and_recover(database, flags, status):
    identifier, _, payment = await paid(database, flags)
    if status == "unavailable":
        flags["receipt_error"] = True
    else:
        flags["checks"][0]["status"] = status
    pending = await refresh(database, identifier, payment.revision)
    assert pending.paid and not pending.accepted
    state = await call(database, sales.read, identifier)
    assert not state.actions["handover"] and not state.actions["complete"]
    with pytest.raises(HTTPException):
        await execute(database, identifier, ShopPhysicalCommand(expected_revision=state.revision, evidence="Must block"), "handover")
    flags["receipt_error"] = False
    flags["checks"][0]["status"] = "done"
    recovered = await refresh(database, identifier, pending.revision)
    assert recovered.accepted and recovered.paid


@pytest.mark.parametrize("kind", ["empty", "multiple", "monopay", "return"])
async def test_non_unique_sale_or_return_never_authorizes(database, flags, kind):
    identifier, _, payment = await paid(database, flags)
    if kind == "empty":
        flags["checks"] = []
    elif kind == "multiple":
        other = deepcopy(flags["checks"][0]); other["id"] = str(uuid4()); flags["checks"].append(other)
    elif kind == "monopay":
        flags["checks"][0]["fiscalizationSource"] = "monopay"
    else:
        flags["checks"][0]["type"] = "return"
    result = await refresh(database, identifier, payment.revision)
    assert result.paid and not result.accepted
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt)) == 0


async def test_reversal_after_import_blocks_and_keeps_immutable_historical_paid_fact(database, flags):
    identifier, _, payment = await paid(database, flags)
    receipt = await refresh(database, identifier, payment.revision)
    flags["invoice_status"] = "reversed"
    latest = await refresh(database, identifier, receipt.revision)
    assert latest.paid and not latest.accepted and latest.status == "mono_checkbox_payment_reconciliation_required"
    assert not (await call(database, sales.read, identifier)).actions["handover"]
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt)) == 1


async def test_internal_issuer_rejected_and_flags_or_owner_change_fail_closed(database, flags, monkeypatch):
    identifier, _, payment = await paid(database, flags)
    async with database() as session:
        order = await sales.load_order(session, identifier)
        with pytest.raises(HTTPException) as error:
            direct.deny_internal_issuer(order)
        assert error.value.detail["code"] == "mono_checkbox_external_issuer_required"
    monkeypatch.setattr(settings, "shop_checkbox_enabled", True)
    with pytest.raises(HTTPException) as error:
        await refresh(database, identifier, payment.revision)
    assert error.value.detail["code"] == "mono_checkbox_fiscal_mode_conflict"
    monkeypatch.setattr(settings, "shop_checkbox_enabled", False)
    profile = deepcopy(settings.shop_monobank_profile); profile["merchant_id"] = "different-owner"
    monkeypatch.setattr(settings, "shop_monobank_profile", profile)
    with pytest.raises(HTTPException):
        await refresh(database, identifier, payment.revision)


async def test_same_receipt_cannot_bind_another_paid_order(database, flags):
    first, _, first_payment = await paid(database, flags)
    assert (await refresh(database, first, first_payment.revision)).accepted
    second, _, second_payment = await paid(database, flags)
    with pytest.raises(HTTPException) as error:
        await refresh(database, second, second_payment.revision)
    assert error.value.detail["code"] == "mono_checkbox_receipt_conflict"
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 2


async def test_same_refresh_key_concurrent_acceptance_one_observation(database, flags):
    identifier, _, payment = await paid(database, flags)
    key = str(uuid4())
    results = await asyncio.gather(refresh(database, identifier, payment.revision, key=key), refresh(database, identifier, payment.revision, key=key))
    assert results[0] == results[1] and results[0].accepted
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt)) == 1
        assert await session.scalar(select(func.count()).select_from(Observation)) == 1


async def test_read_pdf_http_current_permissions_and_audit(database, flags):
    identifier, _, payment = await paid(database, flags)
    await refresh(database, identifier, payment.revision)
    app = FastAPI(); app.include_router(routes.backoffice_router)
    actor_id = 1
    async def actor():
        async with database() as session:
            return await session.get(AdminUser, actor_id)
    async def session_override():
        async with database() as session:
            yield session
    app.dependency_overrides[get_current_admin_user] = actor
    app.dependency_overrides[get_db_session] = session_override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        response = await client.get(f"/shop-payments/orders/{identifier}/external-receipt/pdf")
        assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
        assert "attachment" in response.headers["content-disposition"] and "sandbox" in response.headers["content-security-policy"]
        async with database() as session:
            denied = AdminUser(email="denied@example.test", hashed_password="unused", is_active=True, is_superuser=False, can_record_sales=False)
            session.add(denied); await session.commit(); actor_id = denied.id
        assert (await client.get(f"/shop-payments/orders/{identifier}/external-receipt")).status_code == 403
        assert (await client.get(f"/shop-payments/orders/{identifier}/external-receipt/pdf")).status_code == 403


async def test_pdf_late_arrival_keeps_original_receipt_immutable_and_observations_small(database, flags):
    file = flags["checks"][0].pop("file")
    identifier, _, payment = await paid(database, flags)
    first = await refresh(database, identifier, payment.revision)
    assert first.accepted and not first.receipt["pdf_available"]
    flags["checks"][0]["file"] = file
    latest = await refresh(database, identifier, first.revision)
    assert latest.accepted and latest.receipt["pdf_available"]
    assert latest.receipt["proof_hash"] == first.receipt["proof_hash"]
    assert (await call(database, direct.download_pdf, identifier))[1].startswith(b"%PDF-")
    async with database() as session:
        rows = (await session.scalars(select(Observation))).all()
        assert all("file" not in row.proof["fiscal"]["checks"][0] for row in rows)


async def test_linked_master_denied_on_read_refresh_pdf_despite_sales_permissions(database, flags):
    from app.models.booking import Master
    identifier, _, payment = await paid(database, flags)
    await refresh(database, identifier, payment.revision)
    async with database() as session:
        actor = AdminUser(email="master@example.test", hashed_password="unused", is_active=True, is_superuser=False,
            can_record_sales=True, can_post_inventory=True)
        session.add(actor); await session.flush()
        session.add(Master(admin_user_id=actor.id, full_name="Synthetic linked master", is_active=True))
        await session.commit(); actor_id = actor.id
    for function, args in ((direct.read, (identifier,)), (direct.download_pdf, (identifier,)),
            (direct.refresh, (identifier, RefreshReceiptCommand(expected_revision=payment.revision + 1, **assertion()), str(uuid4())))):
        with pytest.raises(HTTPException) as error:
            await call(database, function, *args, actor_id=actor_id)
        assert error.value.status_code == 403


async def test_direct_prepayment_carrier_dispatch_and_delivery_complete(database, flags):
    identifier, _, payment = await paid(database, flags, kind="nova_poshta", method="card")
    receipt = await refresh(database, identifier, payment.revision)
    shipment_id, _ = await attach(database, identifier)
    handed = await execute(database, identifier, ShopPhysicalCommand(expected_revision=receipt.revision, evidence="Synthetic carrier dispatch"), "handover")
    with pytest.raises(HTTPException):
        await execute(database, identifier, ShopPhysicalCommand(expected_revision=handed.revision, evidence="Not yet delivered"), "complete")
    await delivered(database, shipment_id)
    complete = await execute(database, identifier, ShopPhysicalCommand(expected_revision=handed.revision, evidence="Delivered"), "complete")
    assert complete.completion and complete.authority == "test"


@pytest.mark.parametrize("table", ["shop_checkbox_operations", "shop_checkbox_bindings", "shop_fiscal_documents"])
async def test_database_prevents_internal_issuer_for_pinned_direct_mode(database, flags, table):
    identifier, _, _ = await paid(database, flags)
    async with database() as session:
        with pytest.raises(DBAPIError, match="External mono Checkbox order cannot use an internal receipt issuer"):
            await session.execute(text(f"INSERT INTO {table} (order_id) VALUES (:id)"), {"id":identifier})


async def test_receipt_uuid_cannot_be_reused_after_profile_rename(database, flags, monkeypatch):
    first, _, payment = await paid(database, flags)
    assert (await refresh(database, first, payment.revision)).accepted
    profile = deepcopy(settings.shop_monobank_profile)
    profile["id"] = "renamed-synthetic-profile"
    monkeypatch.setattr(settings, "shop_monobank_profile", profile)
    second, _, payment = await paid(database, flags)
    with pytest.raises(HTTPException) as error:
        await refresh(database, second, payment.revision)
    assert error.value.detail["code"] == "mono_checkbox_receipt_conflict"
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 2


async def test_global_direct_mode_does_not_change_unbound_pos_projections(flags):
    from app.models.order import Order
    order = Order(channel="backoffice_pos", simulation=False)
    assert direct.projection(order) is None
    assert order.fiscal_status == "not_requested"
    assert order.receipt_delivery_status == "pending_document"
