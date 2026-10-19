"""Migrated disposable PostgreSQL only. No provider, HTTP transport or lifespan."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.routes import shop_fiscal as routes, shop_sales as sales_routes
from app.core.config import Settings, settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.inventory import InventoryMovement
from app.models.order import OrderIntent, OrderPayment, OrderStatus
from app.models.product import Product
from app.models.shop_fiscal import (ShopFiscalRegister, ShopFiscalDocument, ShopFiscalAcceptance, ShopFiscalPreview,
    ShopFiscalReport, ShopFinancialReturn, ShopRefundFact, ShopReturnReceipt, ShopFiscalAudit)
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
from app.schemas.order import OrderCreate
from app.schemas.shop_sales import ManualShopPayment, ShopPhysicalCommand
from app.schemas.shop_fiscal import (RevisionCommand, ShiftCommand, ApproveCommand, ReturnCommand,
    RefundCommand, ReceiptCommand, EvidenceCommand)
from app.services import shop_fiscal as service, shop_sales
from app.services.order import OrderService
from tests.test_shop_sales_integration import attach, delivered

pytestmark = pytest.mark.anyio


@pytest.fixture
def checkout_dependencies(monkeypatch):
    # Keep real ShopPromotion pricing; only catalog presentation is isolated.
    async def visibility(_cls, _session):
        return SimpleNamespace(product_states=lambda products: {
            item.id: SimpleNamespace(is_effectively_visible=True) for item in products}, category_parents=lambda: {})
    monkeypatch.setattr("app.services.order.CatalogVisibility.load", classmethod(visibility))


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
async def template_database():
    raw = os.environ.get("INVENTORY_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Isolated local PostgreSQL URL required")
    parsed = make_url(raw)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"} and "test" in parsed.database
    name = "shop_fiscal_test_template_" + uuid4().hex
    admin = create_async_engine(raw, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    previous = settings.database_url
    settings.database_url = parsed.set(database=name).render_as_string(hide_password=False)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        assert len(ScriptDirectory.from_config(config).get_heads()) == 1
        await asyncio.to_thread(alembic_command.upgrade, config, "head")
        await asyncio.to_thread(alembic_command.downgrade, config, "0122_shop_order_operations")
        await asyncio.to_thread(alembic_command.upgrade, config, "head")
        yield parsed, name, admin, config
    finally:
        settings.database_url = previous
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}"'))
        await admin.dispose()


@pytest.fixture
async def database(template_database):
    parsed, template, admin, config = template_database
    name = "shop_fiscal_test_" + uuid4().hex
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    url = parsed.set(database=name).render_as_string(hide_password=False)
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    previous = settings.database_url
    settings.database_url = url
    try:
        async with sessions() as session:
            session.add(AdminUser(email="shop-fiscal@example.test", hashed_password="unused", is_active=True, is_superuser=True))
            await session.commit()
        yield sessions
    finally:
        settings.database_url = previous
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}"'))


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setattr(settings, "shop_fiscal_mock_enabled", True)
    monkeypatch.setattr(settings, "shop_order_operations_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(shop_sales, "fiscal_policy", shop_sales.UnavailableFiscalPolicy())
    monkeypatch.setattr(service, "adapter", service.LocalShopMockAdapter())


async def invoke(database, function, *args, actor_id=1, **kwargs):
    async with database() as session:
        return await function(session, await session.get(AdminUser, actor_id), *args, **kwargs)


async def state(database, order_id):
    return await invoke(database, shop_sales.read, order_id)


async def seed(database, *, quantity=1, price="100.00", kind="pickup", method="bank_transfer", promotion=False, total=None):
    async with database() as session:
        product = Product(name='<script>alert("synthetic")</script>', slug=uuid4().hex, price=Decimal(price),
            stock_quantity=10, reserved_quantity=0, allow_backorder=False)
        session.add(product)
        if promotion:
            session.add(ShopPromotion(name="Synthetic actual promotion", trigger=ShopPromotionTrigger.automatic,
                discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal("10"), priority=1,
                is_active=True, applies_to_all_products=True))
        await session.commit()
        order = await OrderService().create_order(session, OrderCreate(firstName="Synthetic", phoneNumber="+380500001234",
            shippingMethod="pickup", paymentMethod=method, items=[{"productId": product.id, "quantity": quantity}]))
        order.fulfillment_method = kind
        if total is not None:
            order.total_amount, order.discount_amount = Decimal(total), order.subtotal_amount - Decimal(total)
        await session.commit()
        return order.id, product.id


async def open_shift(database):
    current = await invoke(database, service.read, "register")
    if current.shift_status == "open":
        return current
    return await invoke(database, service.shift_command, ShiftCommand(expected_shift_revision=current.shift_revision, confirm=True), str(uuid4()), "open")


async def pay(database, order_id, method="bank_transfer"):
    current = await state(database, order_id)
    return await invoke(database, shop_sales.command, order_id, ManualShopPayment(expected_revision=current.revision,
        amount_minor=int(Decimal(str(current.order.total_amount)) * 100), currency="UAH", method=method,
        external_reference=uuid4().hex, evidence="Synthetic staff verification"), str(uuid4()), "manual_payment")


async def receipt(database, order_id, *, return_id=None):
    current = await invoke(database, service.read, "return", return_id) if return_id else await state(database, order_id)
    preview = await invoke(database, service.order_command, order_id, RevisionCommand(expected_revision=current.revision),
        str(uuid4()), "preview", return_id=return_id)
    document = await invoke(database, service.order_command, order_id, ApproveCommand(expected_revision=current.revision,
        preview_id=preview.id, confirm=True), str(uuid4()), "approve", return_id=return_id)
    acknowledgment = await invoke(database, service.document_command, document.id,
        RevisionCommand(expected_revision=document.revision), str(uuid4()), "process")
    assert acknowledgment.status == "unknown"
    return await invoke(database, service.read, "document", document.id)


async def handover(database, order_id):
    current = await state(database, order_id)
    return await invoke(database, shop_sales.command, order_id, ShopPhysicalCommand(expected_revision=current.revision,
        evidence="Synthetic handover"), str(uuid4()), "handover")


async def prepared(database, *, quantity=1, total=None):
    await open_shift(database)
    order_id, product_id = await seed(database, quantity=quantity, total=total)
    await pay(database, order_id)
    document = await receipt(database, order_id)
    await handover(database, order_id)
    return order_id, product_id, document


async def create_return(database, order_id, document, quantity=1, key=None):
    current = await state(database, order_id)
    return await invoke(database, service.order_command, order_id, ReturnCommand(expected_revision=current.revision,
        sale_document_id=document.id, lines=[{"order_item_id": document.snapshot["items"][0]["order_item_id"], "quantity": quantity}],
        reason="Synthetic partial return"), key or str(uuid4()), "create_return")


async def refund(database, row, *, reference=None):
    return await invoke(database, service.return_command, row.id, RefundCommand(expected_revision=row.revision,
        external_reference=reference or uuid4().hex, evidence="Synthetic TEST staff refund; no bank operation"), str(uuid4()), "refund")


async def receive(database, row, disposition="restock", actor_id=1):
    return await invoke(database, service.return_command, row.id, ReceiptCommand(expected_revision=row.revision,
        lines=[{"order_item_id": i["order_item_id"], "quantity": i["quantity"], "disposition": disposition} for i in row.lines],
        evidence="Synthetic physical return"), str(uuid4()), "physical_receipt", actor_id=actor_id)


async def counts(database):
    async with database() as session:
        return {model.__tablename__: await session.scalar(select(func.count()).select_from(model)) for model in (
            ShopFiscalRegister, ShopFiscalPreview, ShopFiscalDocument, ShopFiscalAcceptance, ShopFiscalAudit,
            ShopFinancialReturn, ShopRefundFact, ShopReturnReceipt, ShopFiscalReport, InventoryMovement, OrderIntent)}


async def test_sale_bound_policy_stages_stock_once_and_escaped_print(database, checkout_dependencies):
    await open_shift(database)
    order_id, product_id = await seed(database)
    paid = await pay(database, order_id)
    assert "sale_receipt_required" in paid.blocking_reasons and not paid.actions["handover"]
    with pytest.raises(HTTPException) as blocked:
        await handover(database, order_id)
    assert blocked.value.detail["code"] == "sale_receipt_required"
    document = await receipt(database, order_id)
    assert document.status == "issued" and document.authority == "test" and not document.is_sent_dps
    dispatched = await handover(database, order_id)
    assert dispatched.authority == "test" and dispatched.actions["complete"]
    before = await counts(database)
    completed = await invoke(database, shop_sales.command, order_id, ShopPhysicalCommand(expected_revision=dispatched.revision,
        evidence="Synthetic close"), str(uuid4()), "complete")
    assert completed.stage == "completed" and completed.order.fulfillment_status == "simulated_fulfilled"
    assert completed.order.fiscal_status == "simulated_fiscal_issued"
    assert completed.order.receipt_delivery_status == "simulated_local_print_available"
    assert (await counts(database))["inventory_movements"] == before["inventory_movements"]
    html = await invoke(database, service.print_document, document.id)
    assert "<script>" not in html and "&lt;script&gt;" in html and "ДПС" in html
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (9, 0)


async def test_partial_refund_receipt_and_restock_independent_exact_cents(database, checkout_dependencies):
    order_id, product_id, sale = await prepared(database, quantity=3, total="100.00")
    amounts = []
    for index in range(3):
        row = await create_return(database, order_id, sale)
        amounts.append(row.amount_minor)
        before = await counts(database)
        row = await refund(database, row)
        await receipt(database, order_id, return_id=row.id)
        assert (await counts(database))["inventory_movements"] == before["inventory_movements"]
        row = await invoke(database, service.read, "return", row.id)
        row = await receive(database, row)
        assert row.physical_receipt and row.refund
    assert amounts == [3333, 3333, 3334]
    with pytest.raises(HTTPException) as error:
        await create_return(database, order_id, sale)
    assert error.value.detail["code"] == "return_quantity_exceeded"
    current = await invoke(database, service.read, "register")
    result = await invoke(database, service.shift_command, ShiftCommand(expected_shift_revision=current.shift_revision, confirm=True), str(uuid4()), "z")
    assert result.totals["sales_minor"] == result.totals["returns_minor"] == 10000
    assert result.totals["net_minor"] == 0
    async with database() as session:
        product = await session.get(Product, product_id)
        assert (product.stock_quantity, product.reserved_quantity) == (10, 0)


async def test_actual_shop_promotion_is_not_discounted_twice(database, checkout_dependencies):
    await open_shift(database)
    order_id, _ = await seed(database, promotion=True, quantity=2)
    paid = await pay(database, order_id)
    assert paid.payment.amount_minor == 18000
    document = await receipt(database, order_id)
    assert document.snapshot["additional_discount_minor"] == 0
    assert sum(i["total_minor"] for i in document.snapshot["items"]) == 18000


async def test_lost_response_acceptance_independent_commit_and_flagoff_recovery(database, checkout_dependencies, monkeypatch):
    await open_shift(database)
    order_id, _ = await seed(database)
    paid = await pay(database, order_id)
    preview = await invoke(database, service.order_command, order_id, RevisionCommand(expected_revision=paid.revision), str(uuid4()), "preview")
    document = await invoke(database, service.order_command, order_id, ApproveCommand(expected_revision=paid.revision,
        preview_id=preview.id, confirm=True), str(uuid4()), "approve")
    class LostResponse(service.LocalShopMockAdapter):
        async def submit(self, *args, **kwargs):
            await super().submit(*args, **kwargs)
            raise TimeoutError("Synthetic response lost after independent commit")
    monkeypatch.setattr(service, "adapter", LostResponse())
    key, body = str(uuid4()), RevisionCommand(expected_revision=1)
    first, second = await asyncio.gather(*[invoke(database, service.document_command, document.id, body, key, "process") for _ in range(2)])
    assert first.model_dump() == second.model_dump() and first.status == "unknown"
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(ShopFiscalAcceptance)) == 1
    monkeypatch.setattr(settings, "shop_fiscal_mock_enabled", False)
    monkeypatch.setattr(service, "adapter", service.LocalShopMockAdapter())  # emulate a fresh worker process
    replay = await invoke(database, service.document_command, document.id, body, key, "process")
    assert replay.model_dump() == first.model_dump()
    unknown = await invoke(database, service.read, "document", document.id)
    assert unknown.actions["reconcile"] and not unknown.actions["process"]
    issued = await invoke(database, service.document_command, document.id, RevisionCommand(expected_revision=unknown.revision), str(uuid4()), "reconcile")
    assert issued.status == "issued"
    assert (await counts(database))["shop_fiscal_acceptances"] == 1


async def test_dispatch_before_provider_crash_is_unknown_lookup_only(database, checkout_dependencies, monkeypatch):
    await open_shift(database)
    order_id, _ = await seed(database)
    paid = await pay(database, order_id)
    preview = await invoke(database, service.order_command, order_id, RevisionCommand(expected_revision=paid.revision), str(uuid4()), "preview")
    document = await invoke(database, service.order_command, order_id, ApproveCommand(expected_revision=paid.revision, preview_id=preview.id, confirm=True), str(uuid4()), "approve")
    class BeforeAcceptance(service.LocalShopMockAdapter):
        calls = 0
        async def submit(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("Synthetic failure before provider accepted")
    provider = BeforeAcceptance()
    monkeypatch.setattr(service, "adapter", provider)
    unknown = await invoke(database, service.document_command, document.id, RevisionCommand(expected_revision=1), str(uuid4()), "process")
    again = await invoke(database, service.document_command, document.id, RevisionCommand(expected_revision=unknown.revision), str(uuid4()), "process")
    assert provider.calls == 1 and again.status == "unknown"
    assert (await counts(database))["shop_fiscal_acceptances"] == 0
    register = await invoke(database, service.read, "register")
    assert document.id in register.reconciliation.unknown_document_ids and not register.actions["z"]


@pytest.mark.parametrize("method", ["cashOnDelivery", "cardOnDelivery"])
async def test_cod_phase_policy_dispatch_unpaid_complete_after_explicit_payment_receipt(database, checkout_dependencies, method):
    await open_shift(database)
    order_id, _ = await seed(database, kind="nova_poshta", method=method)
    shipment_id, _ = await attach(database, order_id)
    dispatched = await handover(database, order_id)
    assert dispatched.stage == "dispatched" and dispatched.payment is None
    await delivered(database, shipment_id)
    paid = await pay(database, order_id, "cod")
    assert not paid.actions["complete"]
    await receipt(database, order_id)
    ready = await state(database, order_id)
    completed = await invoke(database, shop_sales.command, order_id, ShopPhysicalCommand(expected_revision=ready.revision, evidence="Synthetic COD close"), str(uuid4()), "complete")
    assert completed.stage == "completed" and completed.authority == "test"
    fiscal = await invoke(database, service.read, "order", order_id)
    assert not fiscal.actions["create_return"] and "carrier_receipt_required" in fiscal.blocking_reasons
    before = await counts(database)
    with pytest.raises(HTTPException) as error:
        await create_return(database, order_id, fiscal.sale_document)
    assert error.value.detail["code"] == "carrier_receipt_required" and await counts(database) == before


async def test_pre_handover_return_zero_amount_and_duplicate_quantities_no_claim(database, checkout_dependencies):
    await open_shift(database)
    order_id, _ = await seed(database, quantity=3, total="0.01")
    await pay(database, order_id)
    sale = await receipt(database, order_id)
    before = await counts(database)
    with pytest.raises(HTTPException) as error:
        await create_return(database, order_id, sale)
    assert error.value.detail["code"] == "pre_handover_return_blocked" and await counts(database) == before
    await handover(database, order_id)
    before = await counts(database)
    with pytest.raises(HTTPException) as error:
        await create_return(database, order_id, sale)
    assert error.value.detail["code"] == "unsupported_zero_total" and await counts(database) == before


async def test_shift_close_blocks_unallocated_money_and_unresolved_returns(database, checkout_dependencies):
    await open_shift(database)
    order_id, _ = await seed(database)
    await pay(database, order_id)
    current = await invoke(database, service.read, "register")
    for operation in ("close", "z"):
        with pytest.raises(HTTPException) as error:
            await invoke(database, service.shift_command, ShiftCommand(expected_shift_revision=current.shift_revision, confirm=True), str(uuid4()), operation)
        assert error.value.detail["code"] == "shift_unsettled"
    x = await invoke(database, service.shift_command, ShiftCommand(expected_shift_revision=current.shift_revision, confirm=True), str(uuid4()), "x")
    assert len(x.reconciliation.unreceipted_payment_ids) == 1
    sale = await receipt(database, order_id)
    await handover(database, order_id)
    row = await create_return(database, order_id, sale)
    row = await refund(database, row)
    await receipt(database, order_id, return_id=row.id)
    current = await invoke(database, service.read, "register")
    assert row.id in current.reconciliation.unresolved_return_ids and not current.actions["close"]


async def test_cross_shift_return_preserves_original_z_and_has_own_shift(database, checkout_dependencies):
    order_id, _, sale = await prepared(database)
    current = await invoke(database, service.read, "register")
    z = await invoke(database, service.shift_command, ShiftCommand(expected_shift_revision=current.shift_revision, confirm=True), str(uuid4()), "z")
    opened = await open_shift(database)
    row = await create_return(database, order_id, sale)
    assert row.shift_revision == opened.shift_revision and row.shift_revision != sale.shift_revision
    row = await refund(database, row)
    await receipt(database, order_id, return_id=row.id)
    row = await invoke(database, service.read, "return", row.id)
    await receive(database, row, "not_restock")
    second = await invoke(database, service.shift_command, ShiftCommand(expected_shift_revision=opened.shift_revision, confirm=True), str(uuid4()), "z")
    assert second.totals["sales_minor"] == 0 and second.totals["returns_minor"] == 10000
    assert (await invoke(database, service.read, "report", z.id)).model_dump() == z.model_dump()


async def test_refund_global_reference_race_and_restock_race_exactly_once(database, checkout_dependencies):
    order_id, _, sale = await prepared(database, quantity=2)
    rows = [await create_return(database, order_id, sale) for _ in range(2)]
    reference = uuid4().hex
    outcomes = await asyncio.gather(*(refund(database, row, reference=reference) for row in rows), return_exceptions=True)
    assert sum(not isinstance(v, Exception) for v in outcomes) == 1
    loser = next(v for v in outcomes if isinstance(v, HTTPException))
    assert loser.status_code == 409 and loser.detail["code"] == "external_reference_used"
    row = await invoke(database, service.read, "return", rows[0].id)
    body = ReceiptCommand(expected_revision=row.revision, lines=[{"order_item_id": i["order_item_id"], "quantity": i["quantity"], "disposition":"restock"} for i in row.lines], evidence="synthetic")
    key = str(uuid4())
    first, second = await asyncio.gather(*(invoke(database, service.return_command, row.id, body, key, "physical_receipt") for _ in range(2)))
    assert first.model_dump() == second.model_dump()
    assert (await counts(database))["shop_return_receipts"] == 1


async def test_read_permissions_redacted_register_and_inventory_only_receive(database, checkout_dependencies):
    order_id, _, sale = await prepared(database)
    row = await create_return(database, order_id, sale)
    current = await invoke(database, service.read, "register")
    report = await invoke(database, service.shift_command, ShiftCommand(expected_shift_revision=current.shift_revision, confirm=True), str(uuid4()), "x")
    async with database() as session:
        staff = AdminUser(email="inventory-only@example.test", hashed_password="unused", is_active=True, can_post_inventory=True)
        session.add(staff)
        await session.commit()
        actor_id = staff.id
    redacted = await invoke(database, service.read, "register", actor_id=actor_id)
    assert redacted.reconciliation is None and redacted.reports == []
    with pytest.raises(HTTPException) as forbidden:
        await invoke(database, service.read, "report", report.id, actor_id=actor_id)
    assert forbidden.value.status_code == 403
    received = await receive(database, row, actor_id=actor_id)
    assert received.physical_receipt
    with pytest.raises(HTTPException) as forbidden:
        await invoke(database, service.return_command, row.id, RefundCommand(expected_revision=received.revision, external_reference=uuid4().hex, evidence="synthetic"), str(uuid4()), "refund", actor_id=actor_id)
    assert forbidden.value.status_code == 403


async def test_default_closed_production_guard_and_immutable_raw_sql(database, checkout_dependencies, monkeypatch, template_database):
    assert Settings(_env_file=None).shop_fiscal_mock_enabled is False
    monkeypatch.setattr(settings, "app_env", "production")
    before = await counts(database)
    with pytest.raises(HTTPException) as unsafe:
        await open_shift(database)
    assert unsafe.value.detail["code"] == "shop_fiscal_test_unsafe" and await counts(database) == before
    monkeypatch.setattr(settings, "app_env", "test")
    order_id, _, sale = await prepared(database)
    async with database() as session:
        with pytest.raises(Exception, match="immutable|cannot be changed"):
            await session.execute(text("UPDATE shop_fiscal_acceptances SET snapshot_hash='tampered'"))
        await session.rollback()
        with pytest.raises(Exception, match="cannot be changed"):
            await session.execute(text("UPDATE shop_fiscal_documents SET snapshot_hash='tampered' WHERE id=:id"), {"id": sale.id})
        await session.rollback()
    parsed, _, _, config = template_database
    with pytest.raises(RuntimeError, match="Accepted Shop fiscal history"):
        await asyncio.to_thread(alembic_command.downgrade, config, "0122_shop_order_operations")


@pytest.mark.parametrize("model,payload", [
    (RevisionCommand, {"expected_revision":True}),
    (ShiftCommand, {"expected_shift_revision":1,"confirm":1}),
    (ApproveCommand, {"expected_revision":1,"preview_id":1,"confirm":1}),
    (RefundCommand, {"expected_revision":1,"external_reference":" ","evidence":"x"}),
    (RevisionCommand, {"expected_revision":1,"amount_minor":100,"outcome":"accepted"}),
])
async def test_strict_commands_reject_client_proof_and_boolean_numbers(model, payload):
    with pytest.raises(ValidationError):
        model(**payload)


async def test_actual_http_no_get_mutation_and_closed_replay_permissions(database, checkout_dependencies, monkeypatch):
    app = FastAPI()
    app.include_router(routes.backoffice_router, prefix="/api/v1/backoffice")
    app.include_router(sales_routes.backoffice_router, prefix="/api/v1/backoffice")
    async def db():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await session.get(AdminUser, 1)
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = await counts(database)
        for _ in range(2):
            response = await client.get("/api/v1/backoffice/shop-fiscal/register")
            assert response.status_code == 200 and response.json()["shift_status"] == "closed"
        assert await counts(database) == before
        key, body = str(uuid4()), {"expected_shift_revision":1,"confirm":True}
        first = await client.post("/api/v1/backoffice/shop-fiscal/shift/open", headers={"Idempotency-Key":key}, json=body)
        assert first.status_code == 200
        monkeypatch.setattr(settings, "shop_fiscal_mock_enabled", False)
        replay = await client.post("/api/v1/backoffice/shop-fiscal/shift/open", headers={"Idempotency-Key":key}, json=body)
        assert replay.json() == first.json()
        changed = await client.post("/api/v1/backoffice/shop-fiscal/shift/open", headers={"Idempotency-Key":key}, json={**body,"expected_shift_revision":2})
        assert changed.status_code == 409 and changed.json()["detail"]["code"] == "idempotency_conflict"
        new = await client.post("/api/v1/backoffice/shop-fiscal/shift/open", headers={"Idempotency-Key":str(uuid4())}, json=body)
        assert new.status_code == 503
        async with database() as session:
            current = await session.get(AdminUser, 1)
            current.is_superuser = False
            await session.commit()
        revoked = await client.post("/api/v1/backoffice/shop-fiscal/shift/open", headers={"Idempotency-Key":key}, json=body)
        assert revoked.status_code == 403


@pytest.mark.parametrize("operation", ["close", "z"])
@pytest.mark.parametrize("flag", [True, False])
async def test_payment_holds_shared_register_until_commit_before_close(database, checkout_dependencies, monkeypatch, operation, flag):
    opened = await open_shift(database)
    order_id, _ = await seed(database)
    monkeypatch.setattr(settings, "shop_fiscal_mock_enabled", flag)
    held, release = asyncio.Event(), asyncio.Event()
    original = service.payment_barrier
    async def barrier(session):
        await original(session)
        held.set()
        await release.wait()
    monkeypatch.setattr(service, "payment_barrier", barrier)
    payment = asyncio.create_task(pay(database, order_id))
    await asyncio.wait_for(held.wait(), 5)
    # The payment already owns the same register row. Re-enable only to allow
    # closing new work; the payment acquired its lock while the flag was false.
    monkeypatch.setattr(settings, "shop_fiscal_mock_enabled", True)
    closing = asyncio.create_task(invoke(database, service.shift_command,
        ShiftCommand(expected_shift_revision=opened.shift_revision, confirm=True), str(uuid4()), operation))
    await asyncio.sleep(0.03)
    assert not closing.done()
    release.set()
    paid = await asyncio.wait_for(payment, 10)
    assert paid.payment
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(closing, 10)
    assert error.value.detail["code"] == "shift_unsettled"


async def test_pending_approval_serializes_with_z_and_cancel_never_reuses_preview(database, checkout_dependencies, monkeypatch):
    opened = await open_shift(database)
    order_id, _ = await seed(database)
    paid = await pay(database, order_id)
    preview = await invoke(database, service.order_command, order_id, RevisionCommand(expected_revision=paid.revision), str(uuid4()), "preview")
    held, release = asyncio.Event(), asyncio.Event()
    original = service.register
    async def barrier(session, **kwargs):
        row = await original(session, **kwargs)
        if asyncio.current_task().get_name() == "approval-race" and kwargs.get("lock"):
            held.set()
            await release.wait()
        return row
    monkeypatch.setattr(service, "register", barrier)
    approval = asyncio.create_task(invoke(database, service.order_command, order_id,
        ApproveCommand(expected_revision=paid.revision, preview_id=preview.id, confirm=True), str(uuid4()), "approve"), name="approval-race")
    await asyncio.wait_for(held.wait(), 5)
    closing = asyncio.create_task(invoke(database, service.shift_command,
        ShiftCommand(expected_shift_revision=opened.shift_revision, confirm=True), str(uuid4()), "z"))
    await asyncio.sleep(0.03)
    assert not closing.done()
    release.set()
    document = await asyncio.wait_for(approval, 10)
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(closing, 10)
    assert error.value.detail["code"] == "shift_unsettled"
    cancelled = await invoke(database, service.document_command, document.id,
        EvidenceCommand(expected_revision=document.revision, evidence="Synthetic cancellation before dispatch"), str(uuid4()), "cancel")
    assert cancelled.status == "cancelled" and not cancelled.dispatched_at
    current = await state(database, order_id)
    with pytest.raises(HTTPException) as error:
        await invoke(database, service.order_command, order_id,
            ApproveCommand(expected_revision=current.revision, preview_id=preview.id, confirm=True), str(uuid4()), "approve")
    assert error.value.detail["code"] in {"preview_stale", "preview_consumed"}
    replacement = await receipt(database, order_id)
    assert replacement.id != document.id and replacement.status == "issued"
    assert (await counts(database))["shop_fiscal_acceptances"] == 1


async def test_wrong_order_return_preview_and_acceptance_bindings_reject(database, checkout_dependencies, monkeypatch):
    first_id, _, first_sale = await prepared(database)
    second_id, _ = await seed(database)
    await pay(database, second_id)
    _, _, foreign_sale = await prepared(database)
    before = await counts(database)
    with pytest.raises(HTTPException) as error:
        await create_return(database, first_id, foreign_sale)
    assert error.value.detail["code"] == "sale_receipt_required" and await counts(database) == before
    async with database() as session:
        with pytest.raises(Exception, match="committed Shop dispatch fence"):
            session.add(ShopFiscalAcceptance(provider_uuid=str(uuid4()), document_id=first_sale.id,
                snapshot_hash="0"*64, accepted_at=datetime.now(UTC)))
            await session.commit()
        await session.rollback()
    assert await counts(database) == before
    row = await create_return(database, first_id, first_sale)
    row = await refund(database, row)
    current_second = await state(database, second_id)
    preview = await invoke(database, service.order_command, first_id, RevisionCommand(expected_revision=row.revision), str(uuid4()), "preview", return_id=row.id)
    before = await counts(database)
    with pytest.raises(HTTPException) as error:
        await invoke(database, service.order_command, second_id,
            ApproveCommand(expected_revision=current_second.revision, preview_id=preview.id, confirm=True), str(uuid4()), "approve")
    assert error.value.detail["code"] == "preview_stale" and await counts(database) == before


async def test_actual_bind_database_mismatch_rejected_not_just_settings(database, monkeypatch):
    actual = database.kw["bind"].url
    misleading = create_async_engine(actual.set(database="misleading_test_" + uuid4().hex), connect_args={"database":actual.database})
    try:
        async with async_sessionmaker(misleading, expire_on_commit=False)() as session:
            actor = await session.get(AdminUser, 1)
            with pytest.raises(HTTPException) as error:
                await service.shift_command(session, actor, ShiftCommand(expected_shift_revision=1, confirm=True), str(uuid4()), "open")
            assert error.value.detail["code"] == "shop_fiscal_test_unsafe"
    finally:
        await misleading.dispose()


async def test_permissions_refresh_master_denial_and_replay_after_revocation(database, checkout_dependencies):
    await open_shift(database)
    order_id, _ = await seed(database)
    paid = await pay(database, order_id)
    async with database() as session:
        staff = AdminUser(email="fiscal-staff@example.test", hashed_password="unused", is_active=True, can_fiscalize_sales=True)
        session.add(staff)
        await session.flush()
        actor_id = staff.id
        master = Master(admin_user_id=actor_id, full_name="Synthetic restricted master", is_active=True)
        session.add(master)
        await session.commit()
        master_id = master.id
    body, key = RevisionCommand(expected_revision=paid.revision), str(uuid4())
    with pytest.raises(HTTPException) as error:
        await invoke(database, service.order_command, order_id, body, key, "preview", actor_id=actor_id)
    assert error.value.status_code == 403
    async with database() as session:
        master = await session.get(Master, master_id)
        master.admin_user_id = None
        await session.commit()
    accepted = await invoke(database, service.order_command, order_id, body, key, "preview", actor_id=actor_id)
    async with database() as stale:
        actor = await stale.get(AdminUser, actor_id)
        async with database() as session:
            current = await session.get(AdminUser, actor_id)
            current.can_fiscalize_sales = False
            await session.commit()
        with pytest.raises(HTTPException) as error:
            await service.order_command(stale, actor, order_id, body, key, "preview")
        assert error.value.status_code == 403
    assert accepted.id


async def test_large_money_refund_uses_bigint_without_overflow(database, checkout_dependencies):
    await open_shift(database)
    order_id, _ = await seed(database, price="30000000.00")
    paid = await pay(database, order_id)
    assert paid.payment.amount_minor == 3000000000
    sale = await receipt(database, order_id)
    await handover(database, order_id)
    row = await create_return(database, order_id, sale)
    row = await refund(database, row)
    assert row.amount_minor == row.refund.amount_minor == 3000000000


async def test_preview_expiry_and_duplicate_return_lines_leave_zero_effects(database, checkout_dependencies):
    await open_shift(database)
    order_id, _ = await seed(database)
    paid = await pay(database, order_id)
    # Immutable expired fixture inserted as an approval snapshot; no UPDATE bypass.
    async with database() as session:
        order = await shop_sales.load_order(session, order_id)
        register = await service.register(session)
        snapshot = service.sale_snapshot(order, register)
        preview = ShopFiscalPreview(order_id=order_id, return_id=None, actor_id=1, kind="sale", expected_revision=paid.revision,
            shift_revision=register.shift_revision, snapshot=snapshot, snapshot_hash=service.digest(snapshot), expires_at=datetime.now(UTC)-timedelta(seconds=1))
        session.add(preview)
        await session.commit()
        preview_id = preview.id
    before = await counts(database)
    with pytest.raises(HTTPException) as error:
        await invoke(database, service.order_command, order_id,
            ApproveCommand(expected_revision=paid.revision, preview_id=preview_id, confirm=True), str(uuid4()), "approve")
    assert error.value.detail["code"] == "preview_expired" and await counts(database) == before
    sale = await receipt(database, order_id)
    await handover(database, order_id)
    before = await counts(database)
    current = await state(database, order_id)
    item_id = sale.snapshot["items"][0]["order_item_id"]
    with pytest.raises(HTTPException) as error:
        await invoke(database, service.order_command, order_id, ReturnCommand(expected_revision=current.revision,
            sale_document_id=sale.id, lines=[{"order_item_id":item_id,"quantity":1}]*2, reason="synthetic"), str(uuid4()), "create_return")
    assert error.value.detail["code"] == "return_quantity_exceeded" and await counts(database) == before
