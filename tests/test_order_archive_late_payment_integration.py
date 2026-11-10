"""Archive a synthetic Shop order across Mono dispatch and late reconciliation."""

import asyncio
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.master_mono import MasterMonoCreateCandidate, MasterMonoPayment, MasterMonoReceipt
from app.models.order import Order, OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shop_mono_checkbox import ShopMonoCheckboxReceipt
from app.models.shop_payments import ShopPaymentEvidence
from app.models.shop_sales import ShopOperationAudit, ShopPaymentConfirmation
from app.schemas.order import OrderCancellationCommand
from app.schemas.master_mono import MasterMonoCommand
from app.schemas.shop_online_payment import OnlinePaymentRecovery
from app.schemas.shop_payments import VerifyInvoiceCommand
from app.services import master_mono, mono_checkbox_poll, order_archive, shop_online_payment, shop_payments
from app.services.monobank_provider import MonobankGateway
from tests.test_shop_fiscal_integration import anyio_backend, checkout_dependencies, database, template_database
from tests.test_master_mono_integration import invoke, mock_provider, prepared as pos_prepared
from tests.test_shop_online_payment_integration import online, prepared, start
from tests.test_shop_payment_resume_integration import create_order


pytestmark = pytest.mark.anyio
ADMIN = SimpleNamespace(id=1)


async def archive(database, order_id):
    async with database() as session:
        order = await session.get(Order, order_id)
        return await order_archive.cancel(session, ADMIN, order_id,
            OrderCancellationCommand(expected_revision=order.revision), str(uuid4()))


async def assert_late_payment(database, order_id, product_id):
    async with database() as session:
        order = await session.get(Order, order_id)
        product = await session.get(Product, product_id)
        audit = await session.scalar(select(ShopOperationAudit).where(
            ShopOperationAudit.order_id == order_id, ShopOperationAudit.operation == "owner_cancel_late_payment"))
        assert order.status == OrderStatus.cancelled and order.payment_method == "onlineCard"
        assert order.payment_status == "simulated_paid"
        assert product.stock_quantity == 4 and product.reserved_quantity == 1
        for model in (OrderPayment, ShopPaymentEvidence, ShopPaymentConfirmation):
            assert await session.scalar(select(func.count()).select_from(model).where(model.order_id == order_id)) == 1
        assert await session.scalar(select(func.count()).select_from(ShopMonoCheckboxReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.order_id == order_id, InventoryMovement.movement_type == InventoryMovementType.reservation_release,
        )) == 1
        assert audit.facts["invoice_id"] == "syntheticOnlineInvoice"
        assert audit.facts["proof"]["status"] == "success"
        assert audit.facts["proof"]["reference"] == order.shop_payment_intent.reference
        assert audit.facts["proof"]["amount_minor"] == order.shop_payment_intent.amount_minor
        result = await order_archive.read(session, ADMIN, order_id)
        assert result.late_payment_verified and result.reconciliation.payment


@pytest.mark.parametrize("after_dispatch_fence", [False, True])
async def test_owner_archive_races_dispatch_and_reconciles_paid_once(
    database, checkout_dependencies, online, monkeypatch, after_dispatch_fence,
):
    order_id, product_id, token = await prepared(database)
    await create_order(database, product_id)
    gateway = shop_online_payment.gateway()
    original = gateway.create_invoice
    paused, resume = asyncio.Event(), asyncio.Event()

    async def pause():
        paused.set()
        await resume.wait()

    async def delayed(*args, before_dispatch, **kwargs):
        if not after_dispatch_fence:
            await pause()

        async def after_fence():
            await before_dispatch()
            if after_dispatch_fence:
                await pause()

        return await original(*args, before_dispatch=after_fence, **kwargs)

    monkeypatch.setattr(gateway, "create_invoice", delayed)
    monkeypatch.setattr(shop_online_payment, "gateway", lambda: gateway)
    worker = asyncio.create_task(start(database, order_id, token))
    try:
        await asyncio.wait_for(paused.wait(), timeout=10)
        before = list(online["calls"])
        await archive(database, order_id)
        assert online["calls"] == before
    finally:
        resume.set()
        result = await worker

    assert not result.page_url and not result.actions.start and result.reason == "order_closed"
    assert online["posts"] == int(after_dispatch_fence)
    async with database() as session:
        order = await session.get(Order, order_id)
        binding = order.shop_payment_intent
        assert binding.invoice_id == ("syntheticOnlineInvoice" if after_dispatch_fence else None)
        assert order.status == OrderStatus.cancelled
        if not after_dispatch_fence:
            with pytest.raises(DBAPIError, match="Cannot bind an invoice after undispatched Shop cancellation"):
                await session.execute(text("UPDATE shop_payment_intents SET invoice_id='late-invoice' WHERE order_id=:id"),
                    {"id": order_id})
            await session.rollback()

    online["status"] = "success"
    async with asyncio.TaskGroup() as workers:
        polls = [workers.create_task(mono_checkbox_poll.poll_once(database)) for _ in range(2)]
    counts = [worker.result() for worker in polls]
    assert sum(row["checked"] for row in counts) == int(after_dispatch_fence), counts
    calls = list(online["calls"])
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 0, "blocked": 0}
    assert online["calls"] == calls
    assert not any(path.endswith("/fiscal-checks") for _, path in calls)
    if after_dispatch_fence:
        await assert_late_payment(database, order_id, product_id)


async def test_manager_can_recover_known_invoice_after_lost_archived_reply(database, checkout_dependencies, online):
    order_id, product_id, token = await prepared(database)
    await create_order(database, product_id)
    online["unknown"] = True
    result = await start(database, order_id, token)
    assert result.reason == "invoice_creation_unknown" and online["posts"] == 1
    await archive(database, order_id)
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 0, "blocked": 0}
    async with database() as session:
        order = await session.get(Order, order_id)
        recovered = await shop_online_payment.recover(session, ADMIN, order_id,
            OnlinePaymentRecovery(expected_revision=order.revision, invoice_id="syntheticOnlineInvoice"), str(uuid4()))
    assert not recovered.page_url and recovered.reason == "order_closed"
    reference = online["reference"]
    online.update(status="success", reference=str(uuid4()))
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 0, "blocked": 1}
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0
        await session.execute(text("UPDATE mono_checkbox_poll_state SET last_attempt_at=now()-interval '1 day'"))
        await session.commit()
    online["reference"] = reference
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 1, "blocked": 0}
    assert online["posts"] == 1
    await assert_late_payment(database, order_id, product_id)


async def test_already_started_payment_verification_keeps_success_after_archive(
    database, checkout_dependencies, online, monkeypatch,
):
    order_id, product_id, token = await prepared(database)
    await create_order(database, product_id)
    await start(database, order_id, token)
    online["status"] = "success"
    original = MonobankGateway.get_invoice
    paused, resume = asyncio.Event(), asyncio.Event()

    async def delayed(self, *args, **kwargs):
        proof = await original(self, *args, **kwargs)
        paused.set()
        await resume.wait()
        return proof

    monkeypatch.setattr(MonobankGateway, "get_invoice", delayed)

    async def verify():
        async with database() as session:
            order = await session.get(Order, order_id)
            binding = order.shop_payment_intent
            return await shop_payments.command(session, ADMIN, order_id, VerifyInvoiceCommand(
                expected_revision=order.revision, expected_profile_id=binding.profile_id,
                expected_profile_hash=binding.profile_hash, invoice_id=binding.invoice_id), str(uuid4()), "verify")

    worker = asyncio.create_task(verify())
    try:
        await asyncio.wait_for(paused.wait(), timeout=10)
        await archive(database, order_id)
        calls = list(online["calls"])
        online["status"] = "reversed"
    finally:
        resume.set()
        result = await worker
    assert result.payment and not result.actions["verify_invoice"]
    assert online["calls"] == calls
    await assert_late_payment(database, order_id, product_id)


async def assert_pos_archived_payment(database, order_id, product_id, *, canonical):
    async with database() as session:
        order = await session.get(Order, order_id)
        product = await session.get(Product, product_id)
        assert order.status == OrderStatus.cancelled
        assert product.stock_quantity == 5 and product.reserved_quantity == 0
        for model in (OrderPayment, MasterMonoPayment):
            assert await session.scalar(select(func.count()).select_from(model).where(model.order_id == order_id)) == int(canonical)
        assert await session.scalar(select(func.count()).select_from(MasterMonoReceipt)) == 0
        audit = (await session.scalars(select(ShopOperationAudit).where(
            ShopOperationAudit.order_id == order_id, ShopOperationAudit.operation == "owner_cancel_late_payment"))).one()
        assert audit.facts["proof"]["status"] == "success" and audit.facts["channel"] == "backoffice_pos"
        state = await order_archive.read(session, ADMIN, order_id)
        assert state.late_payment_verified and state.reconciliation.payment


async def test_owner_archived_bound_pos_keeps_canonical_late_payment(database, checkout_dependencies, mock_provider, monkeypatch):
    monkeypatch.setattr(settings, "mono_checkbox_auto_enabled", True)
    product_id, order_id, _ = await pos_prepared(database, mock_provider)
    await archive(database, order_id)
    mock_provider["status"] = "success"
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 1, "blocked": 0}
    calls = list(mock_provider["calls"])
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 0, "blocked": 0}
    assert mock_provider["calls"] == calls and mock_provider["post_calls"] == 1
    assert not any(path.endswith("/fiscal-checks") for _, path in calls)
    await assert_pos_archived_payment(database, order_id, product_id, canonical=True)


@pytest.mark.parametrize("archive_before_create_reply", [False, True])
async def test_pos_capture_and_archive_keep_known_invoice_and_get_only_paid_fact(
    database, checkout_dependencies, mock_provider, monkeypatch, archive_before_create_reply,
):
    monkeypatch.setattr(settings, "mono_checkbox_auto_enabled", True)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "master_monobank_live_transport_enabled", True)
    monkeypatch.setattr(master_mono, "gateway", lambda: mock_provider["gateway_factory"]())
    product_id, order_id, initial = await pos_prepared(database, mock_provider, start=False)
    invoice = "syntheticArchivedPOS"
    paid = False
    paused, resume = asyncio.Event(), asyncio.Event()
    calls = []
    fields = {}

    async def wire(request):
        assert request.url.host == "api.monobank.ua"
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/details"):
            return httpx.Response(200, json={"merchantId": settings.master_monobank_profile["merchant_id"]})
        if request.method == "POST":
            assert request.url.path.endswith("/invoice/create")
            body = json.loads(request.content)
            fields.update(reference=body["merchantPaymInfo"]["reference"], amount=body["amount"],
                createdDate=datetime.now(UTC).isoformat())
            if archive_before_create_reply:
                paused.set()
                await resume.wait()
            return httpx.Response(200, json={"invoiceId": invoice, "pageUrl": "https://pay.mbnk.biz/i/pay?opaque=fixture"})
        assert request.url.path.endswith("/invoice/status") and request.url.params["invoiceId"] == invoice
        if not archive_before_create_reply and not paid:
            paused.set()
            await resume.wait()
        return httpx.Response(200, json={**fields, "invoiceId": invoice, "ccy": 980, "cancelList": [],
            "finalAmount": fields["amount"], "status": "success" if paid else "processing",
            "modifiedDate": datetime.now(UTC).isoformat()})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs:
        client(*args, **{**kwargs, "transport": httpx.MockTransport(wire)}))
    worker = asyncio.create_task(invoke(database, master_mono.start, order_id,
        MasterMonoCommand(expected_revision=initial.revision), str(uuid4())))
    try:
        await asyncio.wait_for(paused.wait(), timeout=10)
        await archive(database, order_id)
    finally:
        resume.set()
        result = await worker
    assert result.status == "cancelled" and not result.payment_url
    async with database() as session:
        candidate = await session.scalar(select(MasterMonoCreateCandidate))
        audit = await session.scalar(select(ShopOperationAudit).where(
            ShopOperationAudit.order_id == order_id, ShopOperationAudit.operation == "owner_cancel_pos_invoice"))
        if archive_before_create_reply:
            assert candidate is None and audit.facts["invoice_id"] == invoice and "page_url" not in audit.facts
        else:
            assert audit is None and candidate.invoice_id == invoice
    paid = True
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 1, "blocked": 0}
    checked = list(calls)
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 0, "blocked": 0}
    assert calls == checked and sum(method == "POST" for method, _ in calls) == 1
    await assert_pos_archived_payment(database, order_id, product_id, canonical=False)
