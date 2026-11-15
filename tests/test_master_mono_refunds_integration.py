import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command as alembic
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.models.inventory import InventoryMovement, MovementType
from app.models.order import OrderStatus
from app.models.product import Product
from app.schemas.master_mono import MasterMonoCommand, MasterMonoRefundCommand
from app.schemas.order import OrderHandoverCreate
from app.services import local_sales, master_mono, master_mono_refunds as refunds, pos
from tests.test_inventory_integration import checkout_dependencies
from tests.test_master_mono_integration import invoke, mock_provider, prepared, receipt
from tests.test_shop_fiscal_integration import database, template_database

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def bank(mock_provider, monkeypatch):
    monkeypatch.setattr(settings, "master_mono_refunds_enabled", True)
    original = master_mono.gateway()
    bank = mock_provider
    bank["refund_posts"] = 0

    def mark_refunded(ext_ref):
        date = datetime.now(UTC).isoformat()
        bank.update(status="reversed", final_amount=0, cancellations=[{
            "status": "success", "extRef": ext_ref, "amount": bank["amount"], "ccy": 980,
            "createdDate": date, "modifiedDate": date,
        }])

    def handle(request):
        if request.url.path != "/api/merchant/invoice/cancel":
            return original.transport.handler(request)
        payload = json.loads(request.content)
        assert payload["invoiceId"] == bank["invoice_id"]
        assert payload["items"] == bank["basket"]
        assert "amount" not in payload
        bank["refund_posts"] += 1
        mark_refunded(payload["extRef"])
        raise httpx.ReadTimeout("Refund accepted but response lost", request=request)

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(master_mono, "gateway", lambda: bank["gateway_factory"](transport=transport))
    bank["mark_refunded"] = mark_refunded
    return bank


async def paid(database, bank):
    product_id, order_id, started = await prepared(database, bank)
    bank.update(status="success", checks=[receipt()])
    accepted = await invoke(database, master_mono.refresh, order_id,
                            MasterMonoCommand(expected_revision=started.revision), str(uuid4()))
    assert accepted.receipt_accepted
    return product_id, order_id, accepted


async def test_parallel_refund_lost_ack_late_receipt_and_once_only_stock(database, checkout_dependencies, bank):
    product_id, order_id, accepted = await paid(database, bank)
    handed_over = await invoke(database, lambda *args: local_sales.command(*args, handover=True), order_id,
        OrderHandoverCreate(expected_revision=accepted.revision, evidence="Given to customer"), str(uuid4()))
    command = MasterMonoRefundCommand(expected_revision=handed_over.revision, reason="Customer returned goods")
    with pytest.raises(HTTPException) as denied:
        await invoke(database, refunds.start, order_id, command, str(uuid4()))
    assert denied.value.status_code == 403
    await asyncio.gather(*(invoke(database, refunds.start, order_id, command, str(uuid4()), actor_id=1)
                           for _ in range(2)))
    assert bank["refund_posts"] == 1
    state = await invoke(database, refunds.read, order_id, actor_id=1)
    assert (state.money_status, state.receipt_status, state.stock_status) == ("refunded", "pending", "awaiting_goods")
    bank["checks"].append({**receipt(), "type": "return"})
    state = await invoke(database, refunds.reconcile, order_id, actor_id=1)
    assert state.receipt_status == "issued"
    receive = MasterMonoCommand(expected_revision=state.revision)
    for _ in range(2):
        assert (await invoke(database, refunds.receive, order_id, receive, actor_id=1)).stock_status == "received"
    async with database() as session:
        assert (await session.get(Product, product_id)).stock_quantity == 5
        rows = (await session.scalars(select(InventoryMovement).where(
            InventoryMovement.order_id == order_id, InventoryMovement.movement_type == MovementType.customer_return,
        ))).all()
        assert len(rows) == 1 and rows[0].on_hand_delta == 1


async def test_external_refund_before_handover_releases_reserve(database, checkout_dependencies, bank):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    await asyncio.to_thread(alembic.downgrade, config, "0162_shop_balance_edits")
    product_id, order_id, accepted = await paid(database, bank)
    await asyncio.to_thread(alembic.upgrade, config, "0163_master_mono_refunds")
    bank["mark_refunded"]("merchant-cabinet")
    bank["checks"].append({**receipt(), "type": "return"})
    result = await invoke(database, refunds.start, order_id,
        MasterMonoRefundCommand(expected_revision=accepted.revision, reason="Returned in Mono cabinet"),
        str(uuid4()), actor_id=1)
    assert bank["refund_posts"] == 0
    assert (result.money_status, result.receipt_status, result.stock_status) == ("refunded", "issued", "not_required")
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (5, 0)
        order = await local_sales.load(session, order_id)
        assert order.status == OrderStatus.cancelled
        checkout = await pos.project(session, order)
        assert checkout["payment"]["status"] == "reversed"
        assert checkout["actions"]["can_start_new"] and not checkout["actions"]["can_handover"]


async def test_unknown_dispatch_can_reconcile_later_external_refund(database, checkout_dependencies, bank, monkeypatch):
    from app.services.monobank_provider import MonobankError, MonobankGateway

    async def disconnect_before_acceptance(*args):
        raise MonobankError("monobank_refund_unknown")

    _, order_id, accepted = await paid(database, bank)
    monkeypatch.setattr(MonobankGateway, "refund_master_invoice", disconnect_before_acceptance)
    result = await invoke(database, refunds.start, order_id,
        MasterMonoRefundCommand(expected_revision=accepted.revision, reason="Lost response"),
        str(uuid4()), actor_id=1)
    assert result.money_status == "requires_review"
    with pytest.raises(HTTPException) as blocked:
        await invoke(database, lambda *args: local_sales.command(*args, handover=True), order_id,
            OrderHandoverCreate(expected_revision=result.revision, evidence="Cannot hand over"), str(uuid4()))
    assert blocked.value.detail["code"] == "master_mono_refund_in_progress"
    bank["mark_refunded"]("external-after-unknown")
    bank["checks"].append({**receipt(), "type": "return"})
    result = await invoke(database, refunds.reconcile, order_id, actor_id=1)
    assert (result.money_status, result.receipt_status) == ("refunded", "issued")
