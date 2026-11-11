"""Direct NP return: inspected contents, one bank claim and one report entry."""
import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select

from app.api.v1.routes import orders
from app.core.config import settings
from app.core.database import get_db_session
from app.core.security import create_scoped_token
from app.dependencies.auth import get_current_customer
from app.models.admin_user import AdminUser
from app.models.customer import Customer
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.product import Product
from app.schemas.shop_mono_checkbox_refunds import StartRefund
from app.schemas.shop_np_direct_returns import AttachCommand, ReceiveCommand
from app.schemas.shop_sales import ShopPhysicalCommand
from app.services import customer_returns, mono_checkbox_poll as poll, shop_mono_checkbox_refunds as refunds
from app.services import shop_online_payment
from app.services import shop_np_direct_returns as physical, shop_sales as sales
from app.services.np_return_provider import NPReturnGateway
from app.services.order_documents import digest
from tests.np_return_http_stub import MockNPReturn
from tests.test_shop_fiscal_integration import checkout_dependencies, database, template_database  # noqa: F401
from tests.test_shop_online_payment_integration import anyio_backend, online, start as start_payment  # noqa: F401
from tests.test_shop_sales_integration import attach as attach_outbound, execute, seed


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["full", "partial"])
async def test_post_dispatch_refund_uses_inspected_quantities_and_never_moves_stock(
        database, checkout_dependencies, online, monkeypatch, kind):
    actor = SimpleNamespace(id=1)
    order_id, product_id = await seed(database, kind="nova_poshta", method="onlineCard", quantity=2)
    owner = None
    async with database() as session:
        order = await sales.load_order(session, order_id)
        token = order.guest_payment_token
        tracking_token = order.guest_tracking_url.rsplit("/", 1)[-1]
        if kind == "full":
            customer = Customer(phone="+380501115501")
            session.add(customer)
            await session.flush()
            owner = SimpleNamespace(id=customer.id)
            order.customer_id = owner.id
            await session.commit()
    if owner:
        async with database() as session:
            await shop_online_payment.start(session, order_id, owner, None, str(uuid4()))
    else:
        await start_payment(database, order_id, token)

    app = FastAPI()
    app.include_router(orders.public_router, prefix="/api/v1/public/orders")

    async def session_override():
        async with database() as session:
            yield session

    async def mock_customer(identifier: int = Header(alias="X-Test-Customer")):
        return SimpleNamespace(id=identifier)

    app.dependency_overrides[get_db_session] = session_override
    app.dependency_overrides[get_current_customer] = mock_customer
    prefix = "/api/v1/public/orders"
    owner_path = f"{prefix}/me/{order_id}"
    guest_path = f"{prefix}/tracking/{tracking_token}"

    async def request(path, *, identifier=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-test") as client:
            return await client.get(path, headers={"X-Test-Customer": str(identifier)} if identifier else {})

    async def customer_state():
        response = await request(owner_path, identifier=owner.id) if owner else await request(guest_path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        return response.json()["direct_return"]

    assert await customer_state() is None
    original_receipt = str(uuid4())
    online.update(status="success", checks=[
        dict(id=original_receipt, status="done", type="sale", fiscalizationSource="checkbox")
    ])
    assert (await poll.poll_once(database))["checked"] == 1
    shipment_id, original_ttn = await attach_outbound(database, order_id)
    async with database() as session:
        order = await sales.load_order(session, order_id)
        revision, item_id = order.revision, order.items[0].id
    dispatched = await execute(database, order_id, ShopPhysicalCommand(expected_revision=revision,
        evidence="Synthetic direct Nova Poshta dispatch"), "handover")
    reverse_ttn = str(uuid4().int % 10**14).zfill(14)
    scan = datetime.now(UTC).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M:%S")
    np_token = "synthetic-direct-return"
    np_profile = dict(id="np-p1-test", token_sha256=sha256(np_token.encode()).hexdigest(),
                      environment="test", approved=True, manual_link_allowed=True)
    np = MockNPReturn({
        original_ttn: dict(Number=original_ttn, StatusCode="9", DateScan=scan),
        reverse_ttn: dict(Number=reverse_ttn, StatusCode="9", DateScan=scan, LightReturnNumber=original_ttn),
    })
    monkeypatch.setattr(settings, "shop_np_returns_enabled", True)
    monkeypatch.setattr(settings, "shop_np_return_profile", np_profile)
    monkeypatch.setattr(settings, "shop_np_return_api_key", SecretStr(np_token))
    monkeypatch.setattr(settings, "shop_np_return_live_transport_enabled", False)
    monkeypatch.setattr(physical, "gateway", lambda: NPReturnGateway(transport=np.transport))
    profile = dict(expected_profile_id=np_profile["id"], expected_profile_hash=digest(np_profile))

    async def command(payload, operation, key=None):
        async with database() as session:
            return await physical.command(session, actor, order_id, payload, key or str(uuid4()), operation)

    attached = await command(AttachCommand(expected_revision=dispatched.revision, **profile,
        original_shipment_id=shipment_id, return_ttn=reverse_ttn,
        lines=[dict(order_item_id=item_id, quantity=2)]), "attach")
    waiting = await customer_state()
    assert waiting["inspection_status"] == "awaiting_inspection" and waiting["received_quantity"] is None

    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=1)

    with monkeypatch.context() as stale:
        stale.setattr(customer_returns, "datetime", LaterClock)
        state = await customer_state()
        assert state["inspection_status"] == "awaiting_confirmation" and state["tracking_stale"]

    count = 2 if kind == "full" else 1
    intake = ReceiveCommand(expected_revision=attached.revision, **profile, contents_verified=True,
        evidence="Counted parcel contents and separated damage",
        lines=[dict(order_item_id=item_id, received_quantity=count, restock_quantity=1, damaged_quantity=count-1)])
    key = str(uuid4())
    accepted = await command(intake, "receive", key)
    assert await command(intake, "receive", key) == accepted
    assert accepted.receipt.quantities == {str(item_id): count}
    received = await customer_state()
    assert received["inspection_status"] == "received" and received["received_quantity"] == count
    assert received["money_status"] == received["return_receipt_status"] == "not_started"
    async with database() as session:
        order = await sales.load_order(session, order_id)
        original_status = order.status
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (3, 0)
        operator = await session.get(AdminUser, actor.id)
        operator.is_superuser = operator.can_post_inventory = operator.can_manage_shipments = False
        operator.can_record_sales = True
        await session.commit()

    requests = []
    bank = {}
    return_receipt = str(uuid4())

    def mock_bank(request):
        if request.method == "POST":
            import json
            assert request.url.path == "/api/merchant/invoice/cancel"
            requests.append(json.loads(request.content))
            raise httpx.ReadTimeout("Synthetic lost cancellation response", request=request)
        assert request.url.path == "/api/merchant/invoice/status"
        return httpx.Response(200, json={
            "invoiceId": "syntheticOnlineInvoice", "reference": online["reference"], "ccy": 980,
            "amount": 20000, "status": "success", "finalAmount": 20000,
            "modifiedDate": datetime.now(UTC).isoformat(), "cancelList": [], **bank,
        })

    expose_receipt = False

    async def linked(snapshot, ext_ref):
        if not expose_receipt:
            return None
        return {
            "invoice_id": snapshot["invoice_id"], "original_receipt_id": original_receipt,
            "ext_ref": ext_ref, "amount_minor": snapshot["amount_minor"], "currency": "UAH",
            "issuer": "checkbox", "profile_hash": snapshot["profile_hash"], "merchant_id": snapshot["merchant_id"],
            "items_hash": digest(snapshot["items"]), "type": "return", "status": "done",
            "receipt_id": return_receipt,
        }

    monkeypatch.setattr(settings, "shop_mono_checkbox_refunds_enabled", True)
    monkeypatch.setattr(refunds, "gateway", lambda: refunds.MockRefundGateway(httpx.MockTransport(mock_bank), linked))
    async with database() as session:
        ready = await refunds.read(session, actor, order_id)
    assert ready.can_start and ready.kind == kind and ready.amount_minor == count * 10000
    assert ready.carrier_receipt_id == accepted.receipt.id

    async def start(payload, key):
        async with database() as session:
            return await refunds.start(session, actor, order_id, payload, key)

    payload = StartRefund(expected_revision=ready.revision, kind=kind, goods_in_custody=True,
                          carrier_receipt_id=ready.carrier_receipt_id)
    with pytest.raises(HTTPException) as error:
        await start(payload.model_copy(update={"quantities": {0: 2}}), str(uuid4()))
    assert error.value.detail["code"] == "direct_refund_carrier_quantities"
    key = str(uuid4())
    started, replay = await asyncio.gather(start(payload, key), start(payload, key))
    assert started.ext_ref == replay.ext_ref and len(requests) == 1
    assert sum(line["qty"] for line in requests[0]["items"]) == count
    assert requests[0].get("amount") == (10000 if kind == "partial" else None)
    pending = await customer_state()
    assert pending["money_status"] == pending["return_receipt_status"] == "pending"
    assert pending["refunded_amount_minor"] is None and pending["financial_stale"]
    if kind == "partial":
        expose_receipt = True
        async with database() as session:
            await refunds.reconcile(session, actor, order_id)
        receipt_only = await customer_state()
        assert receipt_only["money_status"] == "pending"
        assert receipt_only["return_receipt_status"] == "confirmed"
        assert receipt_only["refunded_amount_minor"] is None
        expose_receipt = False
    now = datetime.now(UTC).isoformat()
    bank.update(status="reversed" if kind == "full" else "success", finalAmount=20000-count*10000, cancelList=[
        dict(extRef=started.ext_ref, amount=count*10000, status="success", ccy=980,
             createdDate=now, modifiedDate=now)
    ])
    async with database() as session:
        await refunds.reconcile(session, actor, order_id)
    money_only = await customer_state()
    assert money_only["money_status"] == "confirmed"
    assert money_only["return_receipt_status"] == ("confirmed" if kind == "partial" else "pending")
    assert money_only["refunded_amount_minor"] == count * 10000
    expose_receipt = True
    for _ in range(2):
        async with database() as session:
            finalized = await refunds.reconcile(session, actor, order_id)
            assert finalized.reason == f"carrier_{kind}_refund_finalized"
            assert finalized.report.net_minor == 20000-count*10000
            assert [entry.kind for entry in finalized.report.entries] == ["sale", "return"]
            assert finalized.report.entries[1].original_receipt_id == original_receipt
    async with database() as session:
        order = await sales.load_order(session, order_id)
        product = await session.get(Product, product_id)
        projection = await sales.read(session, actor, order_id)
        assert projection.external_fiscal_report == finalized.report
        assert projection.external_fiscal_report.issuer == "mono_checkbox_external"
        assert order.status == original_status
        assert [(shipment.id, shipment.tracking_number) for shipment in order.shipments] == [(shipment_id, original_ttn)]
        assert (product.stock_quantity, product.reserved_quantity) == (3, 0)
        assert await session.scalar(select(func.count()).select_from(InventoryMovement).where(
            InventoryMovement.order_id == order_id,
            InventoryMovement.movement_type == InventoryMovementType.customer_return)) == 1
    assert len(requests) == 1
    with monkeypatch.context() as stale:
        stale.setattr(customer_returns, "datetime", LaterClock)
        public = await customer_state()
    assert public == {
        "simulation": True, "inspection_status": "received", "received_quantity": count,
        "tracking_stale": False, "refund_kind": kind, "money_status": "confirmed",
        "refunded_amount_minor": count * 10000, "currency": "UAH",
        "return_receipt_status": "confirmed", "financial_stale": False,
    }
    assert (await request(owner_path, identifier=(owner.id if owner else 1) + 100)).status_code == 404
    if owner:
        assert (await request(guest_path)).status_code == 404
    else:
        for scope, expiry in [("guest_order_payment", datetime.now(UTC) + timedelta(days=1)),
                              ("guest_order_tracking", datetime.now(UTC) - timedelta(seconds=1))]:
            invalid = create_scoped_token(order_id, scope, expiry)
            assert (await request(f"{prefix}/tracking/{invalid}")).status_code == 404
        async with database() as session:
            order = await sales.load_order(session, order_id)
            order.guest_tracking_revoked_at = datetime.now(UTC)
            await session.commit()
        assert (await request(guest_path)).status_code == 404
