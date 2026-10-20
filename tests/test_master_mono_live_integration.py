"""Lost acknowledgement, recovery, checkout ownership and live role contracts."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes import master_mono as routes
from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.master_mono import MasterMonoCreateRequest, MasterMonoBinding
from app.models.order import Order, OrderHandover
from app.models.product import Product
from app.schemas.master_mono import MasterMonoCommand, MasterMonoRecover
from app.schemas.order import POSOrderCreate, OrderHandoverCreate
from app.services import local_sales, master_mono as service
from tests.test_inventory_integration import add_product, checkout_dependencies
from tests.test_shop_fiscal_integration import template_database, database
from tests.test_master_mono_integration import mock_provider, invoke, prepared, receipt

pytestmark = pytest.mark.anyio


async def test_committed_handover_replays_after_freshness_expires_without_bank_reads(database, checkout_dependencies, mock_provider, monkeypatch):
    from app.models.order import OrderPayment
    _, identifier, started = await prepared(database, mock_provider)
    mock_provider.update(status="success", checks=[receipt()])
    accepted = await invoke(database, service.refresh, identifier,
        MasterMonoCommand(expected_revision=started.revision), str(uuid4()))
    key = str(uuid4())
    payload = OrderHandoverCreate(expected_revision=accepted.revision, evidence="handover response lost")
    async with database() as session:
        actor = await session.get(AdminUser, 2)
        completed = await local_sales.command(session, actor, identifier, payload, key, handover=True)
    real_datetime = datetime
    class Later(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(minutes=2)
    monkeypatch.setattr(service, "datetime", Later)
    def unavailable():
        raise AssertionError("A known committed handover must not consult the bank")
    monkeypatch.setattr(service, "gateway", unavailable)
    async with database() as session:
        actor = await session.get(AdminUser, 2)
        assert await local_sales.command(session, actor, identifier, payload, key, handover=True) == completed
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        actor.can_accept_qr_payments = False
        await session.commit()
        with pytest.raises(HTTPException) as denied:
            await local_sales.command(session, actor, identifier, payload, key, handover=True)
        assert denied.value.status_code == 403


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def extra_product(database, *, stock):
    async with database() as session:
        product = Product(name="Extra fiscal product", slug="extra-"+uuid4().hex, price=Decimal("100.00"),
            stock_quantity=stock, reserved_quantity=0, is_active=True, allow_backorder=True)
        session.add(product)
        await session.commit()
        return product


async def test_unknown_creation_is_durable_and_cannot_dispatch_with_new_key(database, checkout_dependencies, mock_provider):
    mock_provider["create_error"] = 500
    _, order_id, unknown = await prepared(database, mock_provider)
    assert unknown.status == "creation_unknown" and unknown.invoice_id is None and unknown.payment_url is None
    assert mock_provider["post_calls"] == 1
    mock_provider["create_error"] = None
    # These are fresh sessions (the equivalent of browser retries and a worker restart).
    for _ in range(2):
        result = await invoke(database, service.start, order_id, MasterMonoCommand(expected_revision=1), str(uuid4()))
        assert result.status == "creation_unknown" and not result.can_handover
    assert mock_provider["post_calls"] == 1
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MasterMonoCreateRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(MasterMonoBinding)) == 0
        for sql in ("UPDATE orders SET total_amount=1 WHERE id=:id", "UPDATE orders SET status='cancelled' WHERE id=:id",
                    "UPDATE order_items SET quantity_from_stock=0,quantity_to_order=quantity WHERE order_id=:id",
                    "UPDATE orders SET created_by_user_id=3 WHERE id=:id"):
            with pytest.raises(DBAPIError):
                await session.execute(text(sql), {"id":order_id})
            await session.rollback()


async def test_manager_recovers_pending_invoice_without_impersonation_or_post(database, checkout_dependencies, mock_provider):
    mock_provider["create_error"] = 500
    _, order_id, unknown = await prepared(database, mock_provider)
    command = MasterMonoRecover(expected_revision=unknown.revision, invoice_id=mock_provider["invoice_id"])
    with pytest.raises(HTTPException) as denied:
        await invoke(database, service.recover, order_id, command, str(uuid4()))
    assert denied.value.status_code == 403
    recovered = await invoke(database, service.recover, order_id, command, str(uuid4()), actor_id=1)
    assert recovered.invoice_id == mock_provider["invoice_id"] and recovered.payment_url == "https://pay.mbnk.biz/"+mock_provider["invoice_id"]
    assert not recovered.paid and not recovered.can_handover and mock_provider["post_calls"] == 1
    async with database() as session:
        binding = await session.scalar(select(MasterMonoBinding).where(MasterMonoBinding.order_id==order_id))
        assert binding.operator_id == 2
    mock_provider.update(status="success", checks=[receipt()])
    accepted = await invoke(database, service.refresh, order_id, MasterMonoCommand(expected_revision=recovered.revision), str(uuid4()))
    assert accepted.can_handover
    with pytest.raises(HTTPException) as denied:
        await invoke(database, service.read, order_id, actor_id=1)
    assert denied.value.status_code == 404


@pytest.mark.parametrize("mismatch", ["reference", "amount", "created_at"])
async def test_recovery_requires_exact_reference_amount_and_creation_time(database, checkout_dependencies, mock_provider, mismatch):
    mock_provider["create_error"] = 500
    _, order_id, unknown = await prepared(database, mock_provider)
    mock_provider[mismatch] = {"reference":"foreign-order", "amount":10001,
        "created_at":(datetime.now(UTC)-timedelta(days=1)).isoformat()}[mismatch]
    with pytest.raises(HTTPException) as error:
        await invoke(database, service.recover, order_id,
            MasterMonoRecover(expected_revision=unknown.revision, invoice_id=mock_provider["invoice_id"]), str(uuid4()), actor_id=1)
    assert error.value.detail["code"] == "master_mono_recovery_mismatch"
    assert mock_provider["post_calls"] == 1


async def test_simulate_route_removed_and_historical_sale_cannot_start(database, checkout_dependencies, mock_provider):
    _, order_id, started = await prepared(database, mock_provider)
    app = FastAPI()
    app.include_router(routes.backoffice_router)
    async def db():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await session.get(AdminUser, 2)
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = actor
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated.test") as client:
        response = await client.post(f"/master-mono/orders/{order_id}/simulate", json={"expected_revision":started.revision,"outcome":"paid"})
        assert response.status_code == 404
    product = await extra_product(database, stock=2)
    async with database() as session:
        # Historical simulation remains readable; no new master simulations can be prepared.
        settings.local_sales_simulation_enabled = True
        old = await local_sales.prepare(session, await session.get(AdminUser,1),
            POSOrderCreate(items=[{"product_id":product.id,"quantity":1}],simulation=True),str(uuid4()))
        old_order = await session.get(Order, old.id)
        old_order.created_by_user_id = 2
        await session.commit()
    legacy = await invoke(database, service.read, old.id)
    assert not legacy.enabled and "legacy_simulation" in legacy.blocking_reasons and not legacy.payment_url
    with pytest.raises(HTTPException) as denied:
        await invoke(database, service.start, old.id, MasterMonoCommand(expected_revision=old.revision), str(uuid4()))
    assert denied.value.detail["code"] == "legacy_simulation"


async def test_master_capabilities_and_shortage_block_before_create(database, checkout_dependencies, mock_provider):
    _, order_id, _ = await prepared(database, mock_provider)
    product = await extra_product(database, stock=0)
    async with database() as session:
        actor = await session.get(AdminUser,2)
        capabilities = await local_sales.capabilities(session, actor)
        assert capabilities["mode"] == "mono_checkbox" and capabilities["live_qr_available"] and not capabilities["simulation_available"]
        with pytest.raises(HTTPException):
            await local_sales.prepare(session, actor, POSOrderCreate(items=[{"product_id":product.id,"quantity":1}],simulation=True),str(uuid4()))
        with pytest.raises(HTTPException) as error:
            await local_sales.prepare(session, actor, POSOrderCreate(items=[{"product_id":product.id,"quantity":1}],simulation=False),str(uuid4()))
        assert error.value.status_code == 409 and "Insufficient stock" in error.value.detail
    assert mock_provider["post_calls"] == 1


async def test_concurrent_start_creates_one_invoice_and_handover_is_once(database, checkout_dependencies, mock_provider):
    _, order_id, initial = await prepared(database,mock_provider,start=False)
    assert mock_provider["post_calls"] == 0
    results = await asyncio.gather(*[invoke(database,service.start,order_id,
        MasterMonoCommand(expected_revision=initial.revision),str(uuid4())) for _ in range(2)])
    started = await invoke(database,service.read,order_id)
    assert started.invoice_id and all(r.order_id == order_id for r in results) and mock_provider["post_calls"] == 1
    mock_provider.update(status="success",checks=[receipt()])
    accepted = await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=started.revision),str(uuid4()))
    key = str(uuid4())
    payload = OrderHandoverCreate(expected_revision=accepted.revision,evidence="owned live handover")
    async with database() as session:
        actor = await session.get(AdminUser,2)
        await local_sales.command(session,actor,order_id,payload,key,handover=True)
        await local_sales.command(session,actor,order_id,payload,key,handover=True)
        assert await session.scalar(select(func.count()).select_from(OrderHandover).where(OrderHandover.order_id==order_id)) == 1


async def test_https_local_environment_is_disabled_before_dispatch_claim(database, checkout_dependencies, mock_provider, monkeypatch):
    _, order_id, _ = await prepared(database, mock_provider)
    product = await extra_product(database, stock=1)
    async with database() as session:
        order = await local_sales.prepare(session, await session.get(AdminUser,2),
            POSOrderCreate(items=[{"product_id":product.id,"quantity":1}],simulation=False),str(uuid4()))
    monkeypatch.setattr(settings,"master_monobank_live_transport_enabled",True)
    monkeypatch.setattr(settings,"app_env","local")
    monkeypatch.setattr(service,"gateway",mock_provider["gateway_factory"])
    with pytest.raises(HTTPException) as error:
        await invoke(database,service.start,order.id,MasterMonoCommand(expected_revision=order.revision),str(uuid4()))
    assert error.value.detail["code"] == "master_mono_disabled"
    async with database() as session:
        assert await session.scalar(select(MasterMonoCreateRequest).where(MasterMonoCreateRequest.order_id==order.id)) is None
    assert mock_provider["post_calls"] == 1


async def test_discount_cent_allocation_has_exact_integer_basket(mock_provider):
    from types import SimpleNamespace
    order = SimpleNamespace(total_amount=Decimal("2.98"),items=[SimpleNamespace(id=1,product_id=41,
        quantity=3,quantity_from_stock=3,quantity_to_order=0,quantity_received_for_order=0,
        price=Decimal("1.00"),total_price=Decimal("3.00"),product_name="Approved good")])
    basket, policy = service.basket_for(order)
    assert sum(line["total"] for line in basket) == 298
    assert sum(line["qty"] for line in basket) == 3
    assert all(line["total"] == line["qty"]*line["sum"] and line["tax"] == [7] for line in basket)
    assert policy == settings.master_mono_fiscal_policy


async def test_stale_observation_refreshes_before_handover_and_reversal_blocks(database, checkout_dependencies, mock_provider, monkeypatch):
    product_id, order_id, started = await prepared(database,mock_provider)
    mock_provider.update(status="success",checks=[receipt()])
    accepted = await invoke(database,service.refresh,order_id,MasterMonoCommand(expected_revision=started.revision),str(uuid4()))
    class StaleClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz)+timedelta(seconds=31)
    monkeypatch.setattr(local_sales,"datetime",StaleClock)
    mock_provider["status"] = "reversed"
    calls = len(mock_provider["calls"])
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await local_sales.command(session,await session.get(AdminUser,2),order_id,
                OrderHandoverCreate(expected_revision=accepted.revision,evidence="stale evidence"),str(uuid4()),handover=True)
        assert error.value.detail["code"] == "master_mono_receipt_required"
        await session.rollback()
        assert (await session.get(Product,product_id)).stock_quantity == 5
        assert await session.scalar(select(func.count()).select_from(OrderHandover)) == 0
    assert len(mock_provider["calls"]) > calls and mock_provider["post_calls"] == 1
    latest = await invoke(database,service.read,order_id)
    assert latest.status == "reversed" and not latest.can_handover


async def test_live_dispatch_history_prevents_downgrade(database, template_database, checkout_dependencies, mock_provider):
    from alembic import command
    _, order_id, _ = await prepared(database,mock_provider)
    config = template_database[3]
    with pytest.raises(DBAPIError) as error:
        await asyncio.to_thread(command.downgrade,config,"0132_mono_checkbox_poll")
    assert "populated ledger cannot downgrade" in str(error.value)
    async with database() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "0134_shop_canonical_collation"
        assert await session.scalar(select(MasterMonoCreateRequest.id).where(MasterMonoCreateRequest.order_id==order_id))


@pytest.mark.parametrize("master_token_mode", ["missing", "empty", "dedicated"])
async def test_master_bank_configuration_does_not_select_shop_external_fiscal_policy(monkeypatch, master_token_mode):
    from hashlib import sha256
    from pydantic import SecretStr
    from app.services import shop_mono_checkbox, shop_sales
    shared_token = "synthetic-shared-bank-token"
    token = "synthetic-dedicated-master-token" if master_token_mode == "dedicated" else shared_token
    master_profile = dict(id="master-production", merchant_id="master-merchant", environment="production",
        token_sha256=sha256(token.encode()).hexdigest(), external_fiscalization_disabled=False)
    shop_profile = dict(id="shop-internal", merchant_id="shop-merchant", environment="production",
        token_sha256=sha256(shared_token.encode()).hexdigest(), external_fiscalization_disabled=True)
    for name, value in dict(app_env="production", master_mono_enabled=True, master_mono_invoice_create_enabled=True,
            master_mono_checkbox_link_confirmed=True, master_monobank_live_transport_enabled=True,
            master_monobank_profile=master_profile, master_monobank_token=None if master_token_mode == "missing" else
                SecretStr("") if master_token_mode == "empty" else SecretStr(token),
            shop_monobank_profile=shop_profile, shop_monobank_token=SecretStr(shared_token),
            shop_mono_checkbox_enabled=False, shop_checkbox_enabled=False, shop_fiscal_mock_enabled=False).items():
        monkeypatch.setattr(settings,name,value)
    existing_policy = shop_sales.effective_fiscal_policy()
    assert not shop_mono_checkbox.selected() and not isinstance(existing_policy,shop_mono_checkbox.DirectFiscalPolicy)
    provider = service.gateway()
    assert provider.available and provider.external_checkbox and provider.profile == master_profile
    assert provider.config.shop_monobank_token.get_secret_value() == token
    provider._create_guard()  # Pure configuration check; never opens an HTTP client.
    assert settings.shop_monobank_profile == shop_profile and not settings.shop_mono_checkbox_enabled
    assert not shop_mono_checkbox.selected() and shop_sales.effective_fiscal_policy() is existing_policy
    monkeypatch.setattr(settings,"master_monobank_profile",None)
    assert not service.gateway().available  # A master merchant is always explicitly configured.


async def test_master_create_flag_revoked_during_readiness_prevents_post(monkeypatch):
    from hashlib import sha256
    from pydantic import SecretStr
    from httpx import MockTransport, Response
    from app.services.monobank_provider import MonobankError
    token = "synthetic-revocation-test-token"
    profile = dict(id="master-revocation", merchant_id="master-revocation-merchant", environment="production",
        token_sha256=sha256(token.encode()).hexdigest(), external_fiscalization_disabled=False)
    for name, value in dict(app_env="test", master_mono_enabled=True, master_mono_invoice_create_enabled=True,
            master_mono_checkbox_link_confirmed=True, master_monobank_profile=profile,
            master_monobank_token=SecretStr(token), master_monobank_live_transport_enabled=False).items():
        monkeypatch.setattr(settings,name,value)
    methods = []
    def handler(request):
        methods.append(request.method)
        assert request.method == "GET" and request.url.path == "/api/merchant/details"
        monkeypatch.setattr(settings,"master_mono_invoice_create_enabled",False)
        return Response(200,json={"merchantId":profile["merchant_id"]})
    provider = service.gateway(transport=MockTransport(handler))
    basket = [dict(name="Reserved good",qty=1,sum=100,total=100,code="1",unit="шт.",tax=[7])]
    with pytest.raises(MonobankError) as error:
        await provider.create_invoice(str(uuid4()),100,basket)
    assert error.value.code == "monobank_invoice_create_disabled"
    assert methods == ["GET"]
