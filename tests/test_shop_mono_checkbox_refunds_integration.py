"""Direct refund dispatch and evidence using disposable localhost PG and HTTP mocks."""
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.order import OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shop_checkbox import ShopCheckboxDocument
from app.models.shop_fiscal import ShopFiscalDocument
from app.schemas.shop_mono_checkbox_refunds import StartRefund
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import mono_checkbox_poll as poll, shop_mono_checkbox_refunds as refunds, shop_sales as sales
from app.services.order_documents import digest
from tests.test_shop_fiscal_integration import checkout_dependencies, database, template_database  # noqa: F401
from tests.test_shop_online_payment_integration import anyio_backend, online, start as start_payment  # noqa: F401
from tests.test_shop_sales_integration import execute, seed

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("kind", ["full", "partial"])
async def test_timeout_reconciles_exact_bank_and_linked_receipt_once(
        database, checkout_dependencies, online, monkeypatch, kind):
    order_id, product_id = await seed(database, method="onlineCard", quantity=2)
    async with database() as session:
        order = await sales.load_order(session, order_id)
        token = order.guest_payment_token
    await start_payment(database, order_id, token)
    sale_id = str(uuid4())
    online.update(status="success", checks=[
        dict(id=sale_id, status="done", type="sale", fiscalizationSource="checkbox")
    ])
    assert await poll.poll_once(database) == {"checked": 1, "blocked": 0}
    actor = SimpleNamespace(id=1)
    async with database() as session:
        order = await sales.load_order(session, order_id)
        revision = order.revision
    selection = {} if kind == "full" else {0: 1}
    payload = StartRefund(expected_revision=revision, kind=kind, quantities=selection, goods_in_custody=True)
    calls = []
    bank = {}
    proof = {}
    receipt_id = str(uuid4())

    def handle(request):
        if request.method == "POST":
            assert request.url.path == "/api/merchant/invoice/cancel"
            calls.append(json.loads(request.content))
            raise httpx.ReadTimeout("synthetic lost response", request=request)
        assert request.url.path == "/api/merchant/invoice/status"
        assert request.url.params["invoiceId"] == "syntheticOnlineInvoice"
        now = datetime.now(UTC).isoformat()
        return httpx.Response(200, json={
            "invoiceId": "syntheticOnlineInvoice", "reference": online["reference"], "ccy": 980,
            "amount": online["amount"], "status": "success", "finalAmount": online["amount"],
            "modifiedDate": now, "cancelList": [], **bank,
        })

    async def linked_receipt(snapshot, ext_ref):
        if not proof:
            return None
        return {
            "invoice_id": snapshot["invoice_id"], "original_receipt_id": sale_id, "ext_ref": ext_ref,
            "amount_minor": snapshot["amount_minor"], "currency": "UAH", "issuer": "checkbox",
            "profile_hash": snapshot["profile_hash"], "merchant_id": snapshot["merchant_id"],
            "items_hash": digest(snapshot["items"]), "type": "return", "status": "done",
            "receipt_id": receipt_id, **proof,
        }

    monkeypatch.setattr(refunds, "gateway", lambda: refunds.MockRefundGateway(httpx.MockTransport(handle), linked_receipt))

    async def dispatch(key):
        async with database() as session:
            return await refunds.start(session, actor, order_id, payload, key)

    async def reconcile():
        async with database() as session:
            return await refunds.reconcile(session, actor, order_id)

    key = str(uuid4())
    with pytest.raises(HTTPException) as unavailable:
        await dispatch(key)
    assert unavailable.value.detail["code"] == "direct_refunds_unavailable" and not calls
    monkeypatch.setattr(settings, "shop_mono_checkbox_refunds_enabled", True)
    async with database() as session:
        ready = await refunds.read(session, actor, order_id)
    assert ready.can_start and ready.refundable_items[0].basket_index == 0
    assert ready.refundable_items[0].quantity == 2
    first, duplicate = await asyncio.gather(dispatch(key), dispatch(key))
    assert first.ext_ref == duplicate.ext_ref == calls[0]["extRef"]
    assert len(calls) == 1
    assert first.reason == "dispatch_outcome_unknown" and first.can_reconcile and not first.can_start
    assert sum(item["total"] for item in calls[0]["items"]) == first.amount_minor
    if kind == "full":
        assert "amount" not in calls[0]
    else:
        assert calls[0]["amount"] == 10000
    with pytest.raises(HTTPException) as competing:
        await dispatch(str(uuid4()))
    assert competing.value.detail["code"] == "direct_refund_already_claimed"
    with pytest.raises(HTTPException) as handover:
        await execute(database, order_id, ShopPhysicalCommand(expected_revision=first.revision, evidence="Synthetic"),
                      "handover")
    assert handover.value.detail["code"] == "direct_refund_active"

    # Even a valid receipt cannot prove that the bank refunded this operation.
    proof["status"] = "done"
    receipt_only = await reconcile()
    assert receipt_only.return_receipt_id == receipt_id and not receipt_only.bank_confirmed
    assert receipt_only.finalized_at is None
    remaining = online["amount"] - first.amount_minor
    timestamp = datetime.now(UTC).isoformat()
    bank.update(status="success" if remaining else "reversed", finalAmount=remaining, cancelList=[
        dict(status="success", amount=first.amount_minor, ccy=980, extRef=uuid4().hex,
             createdDate=timestamp, modifiedDate=timestamp)
    ])
    assert not (await reconcile()).bank_confirmed
    bank["cancelList"][0]["extRef"] = first.ext_ref
    proof["original_receipt_id"] = str(uuid4())
    bank_only = await reconcile()
    assert bank_only.bank_confirmed and bank_only.finalized_at is None
    assert bank_only.reason == "linked_return_receipt_required"
    if kind == "full":
        async with database() as session:
            with pytest.raises(DBAPIError, match="exact owned reserve releases"):
                await session.execute(text(
                    "UPDATE shop_mono_checkbox_refunds SET finalized_at=now() WHERE order_id=:order_id"
                ), {"order_id": order_id})
            await session.rollback()

    del proof["original_receipt_id"]
    closed, replay = await asyncio.gather(reconcile(), reconcile())
    assert closed == replay
    assert closed.reason == kind + "_refund_finalized"
    assert closed.finalized_at is not None and closed.return_receipt_id == receipt_id
    assert len(calls) == 1
    assert closed.report.issuer == "mono_checkbox_external"
    assert closed.report.net_minor == remaining
    assert [(entry.kind, entry.receipt_id, entry.amount_minor) for entry in closed.report.entries] == [
        ("sale", sale_id, online["amount"]), ("return", receipt_id, first.amount_minor),
    ]
    assert closed.report.entries[1].original_receipt_id == sale_id
    async with database() as session:
        projection = await sales.read(session, actor, order_id)
        assert projection.external_fiscal_report == closed.report
        if kind == "partial":
            assert "direct_refund_partial_remainder_requires_review" in projection.blocking_reasons
            assert not projection.actions["handover"]
        order = await sales.load_order(session, order_id)
        product = await session.get(Product, product_id)
        assert order.status == (OrderStatus.cancelled if kind == "full" else OrderStatus.paid)
        assert product.stock_quantity == 4
        assert product.reserved_quantity == (0 if kind == "full" else 2)
        releases = await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.order_id == order_id,
            InventoryMovement.movement_type == InventoryMovementType.reservation_release,
        ))
        assert releases == (1 if kind == "full" else 0)
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        for internal in (ShopCheckboxDocument, ShopFiscalDocument):
            assert await session.scalar(select(func.count()).select_from(internal)) == 0
