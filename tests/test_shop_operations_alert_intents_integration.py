"""Shop watch queue and local-only alert intents on migrated PostgreSQL."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.api.v1.routes import shop_operations_watch as routes
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.schemas.shop_mono_checkbox_refunds import StartRefund
from app.services import mono_checkbox_poll, shop_mono_checkbox_refunds, shop_operations_watch, shop_sales
from app.services.order_documents import digest
from tests.test_shop_fiscal_integration import database, template_database  # noqa: F401
from tests.test_shop_online_payment_integration import anyio_backend, online, prepared, start  # noqa: F401
from tests.test_inventory_integration import checkout_dependencies  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.fixture
def stale_watch(monkeypatch):
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(hours=2)

    monkeypatch.setattr(shop_operations_watch, "datetime", Future)


async def test_payment_and_receipt_queue_records_each_canonical_incident_once(
    database, checkout_dependencies, online, stale_watch, monkeypatch,
):
    order_id, _, token = await prepared(database)
    await start(database, order_id, token)
    monkeypatch.setattr(settings, "mono_checkbox_poll_seconds", 0)
    app = FastAPI()
    app.include_router(routes.backoffice_router)

    async def db():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=1)
    path = "/shop-operations-watch"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        async def queue():
            result = await client.get(path)
            assert result.status_code == 200, result.text
            return {row["stage"]: row for row in result.json()["issues"] if row.get("stage")}

        async def enqueue():
            result = await client.post(path + "/alert-intents")
            assert result.status_code == 200, result.text
            return result.json()

        payment = (await queue())["payment_pending"]
        assert payment["reason"] == "payment_stale" and payment["order_id"] == order_id
        async with database() as session:
            assert await session.scalar(text("SELECT count(*) FROM shop_operations_alert_intents")) == 0
        before = online["calls"][:]
        first, second = await asyncio.gather(enqueue(), enqueue())
        assert sorted(item["created"] for item in (first, second)) == [0, 1]
        assert (first["intents"] or second["intents"]) == [payment]
        assert online["calls"] == before

        online["status"] = "success"
        assert (await mono_checkbox_poll.poll_once(database))["checked"] == 1
        receipt = (await queue())["sale_receipt_pending"]
        assert receipt["reason"] == "payment_without_sale" and receipt["order_id"] == order_id
        assert await enqueue() == {"created": 1, "intents": [receipt]}
        assert await enqueue() == {"created": 0, "intents": []}

        online["checks"] = [dict(
            id=str(uuid4()), status="done", type="sale", fiscalizationSource="checkbox",
        )]
        assert (await mono_checkbox_poll.poll_once(database))["checked"] == 1
        assert await queue() == {}
        assert await enqueue() == {"created": 0, "intents": []}

        online["checks"][0]["id"] = str(uuid4())
        assert (await mono_checkbox_poll.poll_once(database))["checked"] == 1
        reopened = (await queue())["sale_receipt_pending"]
        assert reopened["entity_id"] == receipt["entity_id"]
        assert reopened["since"] > receipt["since"]
        assert await enqueue() == {"created": 1, "intents": [reopened]}
        assert (await mono_checkbox_poll.poll_once(database))["checked"] == 1
        assert (await queue())["sale_receipt_pending"] == reopened
        assert await enqueue() == {"created": 0, "intents": []}
        async with database() as session:
            assert await session.scalar(text("SELECT count(*) FROM shop_operations_alert_intents")) == 3

        monkeypatch.setattr(settings, "app_env", "production")
        assert (await client.post(path + "/alert-intents")).status_code == 409



async def test_refund_queue_stage_progression_keeps_one_intent(
    database, checkout_dependencies, online, stale_watch, monkeypatch,
):
    actor = SimpleNamespace(id=1)
    order_id, _, token = await prepared(database)
    await start(database, order_id, token)
    sale_id = str(uuid4())
    online.update(status="success", checks=[
        dict(id=sale_id, status="done", type="sale", fiscalizationSource="checkbox"),
    ])
    assert (await mono_checkbox_poll.poll_once(database))["checked"] == 1
    monkeypatch.setattr(settings, "shop_mono_checkbox_refunds_enabled", True)

    bank_cancellations = []
    receipt_id = str(uuid4())
    return_receipt_ready = False

    def handle(request):
        if request.method == "POST":
            assert request.url.path == "/api/merchant/invoice/cancel"
            raise httpx.ReadTimeout("synthetic lost response", request=request)
        assert request.url.path == "/api/merchant/invoice/status"
        return httpx.Response(200, json={
            "invoiceId": "syntheticOnlineInvoice", "reference": online["reference"], "ccy": 980,
            "amount": online["amount"],
            "status": "reversed" if bank_cancellations else "success",
            "finalAmount": 0 if bank_cancellations else online["amount"],
            "modifiedDate": datetime.now(UTC).isoformat(), "cancelList": bank_cancellations,
        })

    async def linked_receipt(snapshot, ext_ref):
        if not return_receipt_ready:
            return None
        return {
            "invoice_id": snapshot["invoice_id"], "original_receipt_id": sale_id, "ext_ref": ext_ref,
            "amount_minor": snapshot["amount_minor"], "currency": "UAH", "issuer": "checkbox",
            "profile_hash": snapshot["profile_hash"], "merchant_id": snapshot["merchant_id"],
            "items_hash": digest(snapshot["items"]), "type": "return", "status": "done", "receipt_id": receipt_id,
        }

    monkeypatch.setattr(shop_mono_checkbox_refunds, "gateway", lambda: shop_mono_checkbox_refunds.MockRefundGateway(
        httpx.MockTransport(handle), linked_receipt,
    ))
    async with database() as session:
        order = await shop_sales.load_order(session, order_id)
        refund = await shop_mono_checkbox_refunds.start(session, actor, order_id, StartRefund(
            expected_revision=order.revision, kind="full", goods_in_custody=True,
        ), str(uuid4()))
    async with database() as session:
        result = await shop_operations_watch.record_alert_intents(session, actor)
        assert result["created"] == 1
        bank_pending, = result["intents"]
        assert bank_pending["reason"] == "refund_stale"
        assert bank_pending["stage"] == "refund_bank_pending"

    now = datetime.now(UTC).isoformat()
    bank_cancellations.append(dict(status="success", amount=online["amount"], ccy=980,
        extRef=refund.ext_ref, createdDate=now, modifiedDate=now))
    async with database() as session:
        bank_only = await shop_mono_checkbox_refunds.reconcile(session, actor, order_id)
        assert bank_only.bank_confirmed and bank_only.finalized_at is None
        result = await shop_operations_watch.snapshot(session, actor)
        receipt_pending, = [row for row in result["issues"] if row["reason"] == "refund_stale"]
        assert receipt_pending == {**bank_pending, "stage": "refund_receipt_pending"}
        assert (await shop_operations_watch.record_alert_intents(session, actor))["created"] == 0

    return_receipt_ready = True
    async with database() as session:
        finalized = await shop_mono_checkbox_refunds.reconcile(session, actor, order_id)
        assert finalized.finalized_at is not None
        assert "refund_stale" not in (await shop_operations_watch.snapshot(session, actor))["counts"]
        assert (await shop_operations_watch.record_alert_intents(session, actor))["created"] == 0
        assert await session.scalar(text("SELECT count(*) FROM shop_operations_alert_intents")) == 1


@pytest.mark.parametrize("received", [0, 1])
async def test_carrier_return_intent_tracks_actual_intake_without_duplicate(
    database, checkout_dependencies, online, stale_watch, monkeypatch, received,
):
    from tests import test_shop_np_direct_returns_integration as carrier

    actor = SimpleNamespace(id=1)
    data = await carrier.seed_dispatched(database, monkeypatch, online)
    order_id = data["order_id"]
    attached = await carrier.command(database, order_id, carrier.attachment(data), "attach")
    async with database() as session:
        intake, = (await shop_operations_watch.record_alert_intents(session, actor))["intents"]
        assert intake["reason"] == "np_direct_return_pending"
        assert intake["stage"] == "carrier_intake_pending"
        assert intake["order_id"] == order_id

    await carrier.command(database, order_id, carrier.inspection(
        data, attached, received=received, restock=received,
    ), "receive")
    async with database() as session:
        result = await shop_operations_watch.snapshot(session, actor)
        backlog = [row for row in result["issues"] if row["reason"] == "np_direct_return_pending"]
        assert backlog == ([{**intake, "stage": "refund_not_started"}] if received else [])
        assert (await shop_operations_watch.record_alert_intents(session, actor))["created"] == 0


async def test_financial_exception_alert_uses_first_exception_event(
    database, checkout_dependencies, monkeypatch,
):
    from tests import test_shop_payment_resolutions_integration as resolution

    actor = SimpleNamespace(id=1)
    flags = resolution.flags.__wrapped__(monkeypatch)
    _, _, state, case, invoice = await resolution.invoice_case(database, flags, monkeypatch)
    case = await resolution.inspect(database, case, invoice)
    case = await resolution.close_unpaid(database, case, state)
    async with database() as session:
        assert (await shop_operations_watch.record_alert_intents(session, actor))["created"] == 0

    flags[0].update(invoice, status="success")
    case = await resolution.inspect(database, case, invoice)
    async with database() as session:
        exception, = (await shop_operations_watch.record_alert_intents(session, actor))["intents"]
        assert exception["reason"] == "financial_exception"
        assert exception["stage"] == "financial_exception_open"
        assert exception["since"] == datetime.fromisoformat(case.exceptions[0]["created_at"])
        assert exception["entity_id"] == str(case.id)
        assert (await shop_operations_watch.record_alert_intents(session, actor))["created"] == 0


async def test_known_pre_dispatch_failure_keeps_only_pending_order_triage(
    database, checkout_dependencies, online, stale_watch, monkeypatch,
):
    actor = SimpleNamespace(id=1)
    order_id, _, token = await prepared(database)
    online["readiness_hook"] = lambda: monkeypatch.setattr(settings, "shop_online_payment_link_confirmed", False)
    payment = await start(database, order_id, token)
    assert payment.status == "creation_failed" and online["posts"] == 0

    async with database() as session:
        result = await shop_operations_watch.snapshot(session, actor)
        assert [row["reason"] for row in result["issues"] if row["order_id"] == order_id] == ["pending_order"]
        assert (await shop_operations_watch.record_alert_intents(session, actor))["created"] == 0
