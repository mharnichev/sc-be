"""Physical direct Nova Poshta return checks using isolated PostgreSQL and HTTP mocks."""
import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.product import Product
from app.schemas import shop_np_direct_returns as dto
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import mono_checkbox_poll as poll, shop_np_direct_returns as service, shop_sales as sales
from app.services.np_return_provider import NPReturnGateway
from app.services.order_documents import digest
from tests.np_return_http_stub import MockNPReturn
from tests.test_shop_fiscal_integration import checkout_dependencies, database, template_database  # noqa: F401
from tests.test_shop_online_payment_integration import anyio_backend, online, start as start_payment  # noqa: F401
from tests.test_shop_sales_integration import attach as outbound, execute, seed

pytestmark = pytest.mark.anyio


def scan(value=None):
    return (value or datetime.now(UTC)).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M:%S")


async def seed_dispatched(database, monkeypatch, online, *, quantity=2, original_status="9", reverse_status="9"):
    monkeypatch.setattr(settings, "shop_np_returns_enabled", True)
    monkeypatch.setattr(settings, "shop_np_return_live_transport_enabled", False)
    monkeypatch.setattr(settings, "shop_np_return_api_key", SecretStr("synthetic-direct-np"))
    profile = dict(id="isolated-direct-np", token_sha256=sha256(b"synthetic-direct-np").hexdigest(),
                   environment="test", approved=True, manual_link_allowed=True)
    monkeypatch.setattr(settings, "shop_np_return_profile", profile)
    order_id, product_id = await seed(database, kind="nova_poshta", method="onlineCard", quantity=quantity)
    async with database() as session:
        order = await sales.load_order(session, order_id)
        token = order.guest_payment_token
    await start_payment(database, order_id, token)
    sale_receipt_id = str(uuid4())
    online.update(status="success", checks=[
        dict(id=sale_receipt_id, status="done", type="sale", fiscalizationSource="checkbox")
    ])
    assert await poll.poll_once(database) == {"checked": 1, "blocked": 0}
    shipment_id, original_ttn = await outbound(database, order_id)
    async with database() as session:
        order = await sales.load_order(session, order_id)
        revision, item_id = order.revision, order.items[0].id
    dispatched = await execute(database, order_id, ShopPhysicalCommand(expected_revision=revision,
                                                                        evidence="Synthetic outbound"), "handover")
    return_ttn = str(uuid4().int % 10**14).zfill(14)
    stub = MockNPReturn({
        original_ttn: dict(Number=original_ttn, StatusCode=original_status, DateScan=scan()),
        return_ttn: dict(Number=return_ttn, StatusCode=reverse_status, DateScan=scan(), LightReturnNumber=original_ttn),
    })
    monkeypatch.setattr(service, "gateway", lambda: NPReturnGateway(transport=stub.transport))
    return dict(order_id=order_id, product_id=product_id, item_id=item_id, quantity=quantity, shipment_id=shipment_id,
                original_ttn=original_ttn, return_ttn=return_ttn, revision=dispatched.revision, stub=stub,
                sale_receipt_id=sale_receipt_id)


def profile_assertions():
    profile = settings.shop_np_return_profile
    return dict(expected_profile_id=profile["id"], expected_profile_hash=digest(profile))


def attachment(data, *, manual=False):
    linkage = {}
    if manual:
        linkage = dict(linkage_kind="manual_document", manual_link=dict(
            source="Synthetic documented refusal", document_id="refusal-1", line_id=uuid4().hex,
            order_id=data["order_id"], original_shipment_id=data["shipment_id"], original_ttn=data["original_ttn"],
            return_ttn=data["return_ttn"], verified=True,
        ))
    return dto.AttachCommand(
        expected_revision=data["revision"], **profile_assertions(), original_shipment_id=data["shipment_id"],
        return_ttn=data["return_ttn"], lines=[dict(order_item_id=data["item_id"], quantity=data["quantity"])],
        **linkage,
    )


async def command(database, order_id, payload, operation, key=None):
    async with database() as session:
        return await service.command(session, SimpleNamespace(id=1), order_id, payload, key or str(uuid4()), operation)


def inspection(data, attached, *, received, restock):
    return dto.ReceiveCommand(expected_revision=attached.revision, **profile_assertions(), contents_verified=True,
                              evidence="Synthetic separate parcel inspection", lines=[dict(
                                  order_item_id=data["item_id"], received_quantity=received, restock_quantity=restock,
                                  damaged_quantity=received - restock,
                              )])


async def record_receipt(database, data, *, received=2, restock=1):
    attached = await command(database, data["order_id"], attachment(data), "attach")
    return await command(database, data["order_id"], inspection(data, attached, received=received, restock=restock),
                         "receive")


async def test_inspected_received_and_restock_quantities_are_distinct_and_replay_once(
        database, checkout_dependencies, online, monkeypatch):
    data = await seed_dispatched(database, monkeypatch, online, quantity=3)
    order_id = data["order_id"]
    first_case_read = asyncio.Event()
    resume_case_read = asyncio.Event()
    real_case_for = service.case_for

    async def pause_case_read(session, identifier):
        if asyncio.current_task().get_name() == "delayed-attach" and not first_case_read.is_set():
            first_case_read.set()
            await resume_case_read.wait()
        return await real_case_for(session, identifier)

    monkeypatch.setattr(service, "case_for", pause_case_read)
    attach_payload = attachment(data)
    attach_key = str(uuid4())
    delayed = asyncio.create_task(command(database, order_id, attach_payload, "attach", attach_key),
                                  name="delayed-attach")
    await asyncio.wait_for(first_case_read.wait(), timeout=10)
    try:
        attached = await command(database, order_id, attach_payload, "attach", attach_key)
    finally:
        resume_case_read.set()
    assert await delayed == attached
    assert attached.status == "arrived" and attached.can_receive
    assert attached.observation["original"]["raw_status"] == "9"
    assert attached.receipt is None
    payload = inspection(data, attached, received=2, restock=1)
    key = str(uuid4())
    received = await command(database, order_id, payload, "receive", key)
    assert received.receipt.quantities == {str(data["item_id"]): 2}
    assert received.receipt.lines[0].damaged_quantity == 1
    assert received.financial_refund_path
    assert await command(database, order_id, payload, "receive", key) == received
    async with database() as session:
        order = await sales.load_order(session, order_id)
        basis = await service.financial_basis(session, order, received.receipt.id)
        assert basis["quantities"] == {str(data["item_id"]): 2}
        assert basis["proof_hash"] == received.receipt.proof_hash
        assert (await session.get(Product, data["product_id"])).stock_quantity == 2
        rows = list(await session.scalars(select(InventoryMovement).where(
            InventoryMovement.order_id == order_id,
            InventoryMovement.movement_type == InventoryMovementType.customer_return,
        )))
        assert len(rows) == 1 and rows[0].quantity == 1
        assert len(data["stub"].calls) == 2
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE shop_np_direct_return_receipts SET quantities='{}'"))
        await session.rollback()
        forged = InventoryMovement(
            product_id=data["product_id"], movement_type=InventoryMovementType.inventory_adjustment,
            quantity=1, on_hand_delta=1, reserved_delta=0, order_id=order_id, order_item_id=data["item_id"],
            reference_key=service.movement_key(received.receipt.id, data["item_id"] + 1),
        )
        session.add(forged)
        with pytest.raises(DBAPIError, match="restock namespace is reserved"):
            await session.flush()
        await session.rollback()


async def test_refusal_requires_dated_proof_then_verified_reverse_delivery(database, checkout_dependencies, online,
                                                                            monkeypatch):
    data = await seed_dispatched(database, monkeypatch, online, original_status="103", reverse_status="4")
    order_id = data["order_id"]
    stub = data["stub"]
    original, reverse = stub.rows[data["original_ttn"]], stub.rows[data["return_ttn"]]
    reverse.pop("LightReturnNumber")
    with pytest.raises(HTTPException) as error:
        await command(database, order_id, attachment(data), "attach")
    assert error.value.detail["code"] == "np_return_linkage_mismatch"
    original["DateScan"] = scan(datetime.now(UTC) - timedelta(minutes=10))
    with pytest.raises(HTTPException) as error:
        await command(database, order_id, attachment(data, manual=True), "attach")
    assert error.value.detail["code"] == "np_direct_return_original_proof"
    original["DateScan"] = scan()
    reverse.pop("DateScan")
    attached = await command(database, order_id, attachment(data, manual=True), "attach")
    assert attached.status == "awaiting_return" and not attached.can_receive
    with pytest.raises(HTTPException) as error:
        await command(database, order_id, inspection(data, attached, received=2, restock=1), "receive")
    assert error.value.detail["code"] == "np_return_delivery_required"
    reverse.update(StatusCode="9", DateScan=scan())
    refresh = dto.NPCommand(expected_revision=attached.revision, **profile_assertions())
    arrived = await command(database, order_id, refresh, "refresh")
    assert arrived.can_receive and arrived.observation["linkage_authority"] == "manual"
    empty = await command(database, order_id, inspection(data, arrived, received=0, restock=0), "receive")
    assert empty.receipt.quantities == {} and empty.financial_refund_path is None
    async with database() as session:
        order = await sales.load_order(session, order_id)
        with pytest.raises(HTTPException) as error:
            await service.financial_basis(session, order, empty.receipt.id)
        assert error.value.detail["code"] == "np_direct_return_no_received_goods"
