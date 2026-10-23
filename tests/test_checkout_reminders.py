"""No-network service reminder contracts in a disposable PostgreSQL schema."""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1.routes import checkout_reminders as routes
from app.core.config import settings
from app.core.database import Base, get_db_session
from app.models.checkout_reminder import CheckoutReminder
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.models.order import Order
from app.models.product import Product
from app.models.shop import CustomerCartItem
from app.schemas.checkout_reminder import CheckoutReminderCheckpoint
from app.schemas.order import OrderCreate
from app.services import checkout_reminders as service
from app.services.order import OrderService
from tests.test_inventory_integration import database, anyio_backend, checkout_dependencies  # noqa: F401


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    for name, value in {
        "app_env": "test", "shop_checkout_enabled": True, "checkout_reminder_enabled": True,
        "checkout_reminder_dry_run": True, "shared_ingress_limits_enabled": False,
        "checkout_reminder_live_enabled": False, "checkout_reminder_launch_approved": False,
        "checkout_reminder_logo_url": "https://shop.example.test/logo.png",
        "checkout_reminder_shop_url": "https://shop.example.test",
        "checkout_reminder_support_email": "support@example.com",
        "checkout_reminder_asset_origins": ["https://shop.example.test"],
        "checkout_reminder_media_origin": "",
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def renderer(monkeypatch):
    # Lifecycle tests cannot accidentally invoke SMTP or another external API.
    from app.services import checkout_reminder_email

    def build(**kwargs):
        message = EmailMessage()
        message.set_content(kwargs["locale"] + "\n" + kwargs["recovery_url"])
        return message

    monkeypatch.setattr(checkout_reminder_email, "build_checkout_reminder_message", build)


def payload(product_id, *, email="synthetic-checkout@example.com", locale="uk", quantity=1):
    return CheckoutReminderCheckpoint(email=email, locale=locale, 
        items=[{"product_id": product_id, "quantity": quantity}])


async def seed(database, *, customer=False):
    async with database() as session:
        product = Product(name="Synthetic checkout item", slug="synthetic-checkout-item",
            price=Decimal("120.00"), stock_quantity=5, reserved_quantity=0,
            volume_ml=100, image_url="https://shop.example.test/product.webp", allow_backorder=False)
        session.add(product)
        owner = None
        if customer:
            owner = Customer(name="Synthetic", phone="+380501112230", email="synthetic-checkout@example.com")
            session.add(owner)
        await session.commit()
        if owner:
            session.add(CustomerCartItem(customer_id=owner.id, product_id=product.id, quantity=1))
            await session.commit()
        return product.id, owner


async def request_reminder(database, product_id, *, key=None, customer=None, revision=None, **kwargs):
    key = key or str(uuid4())
    async with database() as session:
        await service.checkpoint(session, key, payload(product_id, **kwargs), customer, revision=revision)
    return key


async def due(database):
    async with database() as session:
        rows = (await session.execute(select(CheckoutReminder))).scalars().all()
        for row in rows:
            row.due_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()


async def record(database):
    async with database() as session:
        return (await session.execute(select(CheckoutReminder))).scalar_one()


def token_from(provider):
    message = next(iter(provider.messages.values()))[1]
    return re.search(r"#token=([\w-]+)", message.get_content()).group(1)


def test_closed_checkout_pilot_capabilities_require_actual_verified_owner(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "10")
    owner = SimpleNamespace(id=10, is_active=True, phone_verified_at=datetime.now(UTC))
    assert service.capabilities(owner)["enabled"] is True
    assert service.capabilities()["enabled"] is False
    for customer in (
        SimpleNamespace(id=11, is_active=True, phone_verified_at=datetime.now(UTC)),
        SimpleNamespace(id=10, is_active=False, phone_verified_at=datetime.now(UTC)),
        SimpleNamespace(id=10, is_active=True, phone_verified_at=None),
    ):
        assert service.capabilities(customer)["enabled"] is False
    monkeypatch.setattr(settings, "checkout_reminder_enabled", False)
    assert service.capabilities(owner)["enabled"] is False


def test_production_pilot_ready_without_global_checkout(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "10")
    monkeypatch.setattr(settings, "checkout_reminder_dry_run", False)
    monkeypatch.setattr(settings, "checkout_reminder_live_enabled", True)
    monkeypatch.setattr(settings, "checkout_reminder_launch_approved", True)
    monkeypatch.setattr(settings, "email_notifications_enabled", True)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_from_email", "sender@example.test")
    assert service.delivery_enabled() and service.scheduler_allowed()
    assert service.enabled(SimpleNamespace(id=10, is_active=True, phone_verified_at=datetime.now(UTC)))
    assert not service.enabled()
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "")
    assert not service.delivery_enabled()


def test_reminder_configuration_defaults_and_scoped_media_validation():
    from app.core.config import Settings
    for field in ("checkout_reminder_enabled", "checkout_reminder_scheduler_enabled",
                  "checkout_reminder_live_enabled", "checkout_reminder_launch_approved"):
        assert Settings.model_fields[field].default is False
    assert Settings.model_fields["checkout_reminder_dry_run"].default is True
    assert Settings.validate_reminder_media_origin("") == ""
    assert Settings.validate_reminder_media_origin("https://admin.soulcuts.com.ua/") == "https://admin.soulcuts.com.ua"
    for value in ("http://example.test", "https://user:secret@example.test", "https://example.test/media",
                  "https://example.test?x=1", "https://example.test#x", "//example.test", "https://example.test:bad"):
        with pytest.raises(ValueError):
            Settings.validate_reminder_media_origin(value)


def test_scoped_media_origin_does_not_change_global_urls(monkeypatch):
    monkeypatch.setattr(settings, "public_api_base_url", None)
    monkeypatch.setattr(settings, "checkout_reminder_media_origin", "https://admin.soulcuts.com.ua")
    assert service.email_asset_url("/media/products/a.webp") == "https://admin.soulcuts.com.ua/media/products/a.webp"
    assert settings.public_api_base_url is None
    for value in ("http://example.test", "https://user:secret@example.test", "https://example.test/path"):
        monkeypatch.setattr(settings, "checkout_reminder_media_origin", value)
        assert service.email_asset_url("/media/products/a.webp") == ""


@pytest.mark.anyio
async def test_closed_pilot_dispatch_and_recovery_recheck_owner(database, renderer, monkeypatch):
    product_id, owner = await seed(database, customer=True)
    async with database() as session:
        stored = await session.get(Customer, owner.id)
        stored.phone_verified_at = datetime.now(UTC)
        await session.commit()
        owner.phone_verified_at = stored.phone_verified_at
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", str(owner.id))
    key = await request_reminder(database, product_id, customer=owner)
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    assert await service.process_due(session_factory=database, provider=provider, dry_run=False) == 1
    token = token_from(provider)
    async with database() as session:
        assert (await service.recover(session, token, owner))["items"]
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "999999")
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.recover(session, token, owner)
        assert error.value.status_code == 503
    monkeypatch.setattr(settings, "checkout_reminder_enabled", False)
    async with database() as session:
        assert await service.cancel(session, key, owner) == {"cancelled": True}
    assert (await record(database)).token_hash is None


@pytest.mark.anyio
@pytest.mark.parametrize("guest", [True, False])
async def test_closed_pilot_worker_suppresses_ineligible_existing_rows(database, renderer, monkeypatch, guest):
    product_id, owner = await seed(database, customer=not guest)
    await request_reminder(database, product_id, customer=owner)
    await due(database)
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "999999")
    provider = service.FakeCheckoutReminderProvider()
    assert await service.process_due(session_factory=database, provider=provider, dry_run=False) == 0
    assert not provider.messages
    row = await record(database)
    assert row.state == "suppressed" and row.email is None and row.snapshot == []


@pytest.mark.anyio
async def test_closed_pilot_rejects_guest_checkpoint_and_recovery_before_database(monkeypatch):
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "10")
    for operation in (
        service.checkpoint(None, "a" * 32, payload(1)),
        service.recover(None, "z" * 43),
    ):
        with pytest.raises(HTTPException) as error:
            await operation
        assert error.value.status_code == 503


@pytest.mark.anyio
async def test_pilot_capability_http_is_private_and_rejects_invalid_auth(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(settings, "shop_checkout_enabled", False)
    monkeypatch.setattr(settings, "shop_checkout_pilot_enabled", True)
    monkeypatch.setattr(settings, "shop_checkout_pilot_customer_ids", "10")
    application = FastAPI()
    application.include_router(routes.public_router)
    application.dependency_overrides[routes.get_optional_current_customer] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/checkout-reminders/capabilities")
        assert response.json() == {"enabled": False, "dry_run": True}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert (await client.get("/checkout-reminders/capabilities", headers={"Authorization": "Bearer invalid"})).status_code == 401
        application.dependency_overrides[routes.get_optional_current_customer] = lambda: SimpleNamespace(
            id=10, is_active=True, phone_verified_at=datetime.now(UTC))
        assert (await client.get("/checkout-reminders/capabilities")).json()["enabled"] is True


def test_deliberate_email_checkpoint_contract_and_item_bounds():
    valid = {"email": "synthetic@example.com", "locale": "en",
        "items": [{"product_id": 1, "quantity": 1}]}
    assert CheckoutReminderCheckpoint.model_validate(valid).email == "synthetic@example.com"
    for change in ({"consent": True}, {"consent": False}, {"email": None}, {"email": ""},
        {"items": []}, {"items": [{"product_id": 1, "quantity": 0}]},
        {"items": [{"product_id": 1, "quantity": 1}] * 2}, {"locale": "ru"}):
        with pytest.raises(ValidationError):
            CheckoutReminderCheckpoint.model_validate({**valid, **change})


def test_disabled_and_key_validation(monkeypatch):
    monkeypatch.setattr(settings, "checkout_reminder_enabled", False)
    assert service.enabled() is False
    with pytest.raises(HTTPException) as error:
        service.require_enabled()
    assert error.value.status_code == 503
    for key in ("", "short", "a@example.com", "bad?" * 12):
        with pytest.raises(HTTPException):
            service.key_digest(key)
    assert len(service.key_digest(str(uuid4()))) == 64


@pytest.mark.anyio
@pytest.mark.parametrize("locale", ["uk", "en"])
async def test_happy_path_one_send_recovery_is_read_only(database, renderer, locale):
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id, locale=locale)
    original = await record(database)
    assert original.locale == locale
    assert original.checkpoint_version == service.CHECKPOINT_VERSION
    assert original.management_hash != key
    assert len(original.snapshot) == 1
    assert await service.process_due(session_factory=database, provider=service.FakeCheckoutReminderProvider(), dry_run=False) == 0
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    assert await service.process_due(session_factory=database, provider=provider, dry_run=False) == 1
    assert await service.process_due(session_factory=database, provider=provider, dry_run=False) == 0
    assert len(provider.messages) == 1
    row = await record(database)
    assert row.state == "sent" and row.email == "synthetic-checkout@example.com"
    assert row.email_hash != row.email
    token = token_from(provider)
    assert row.token_hash != token
    async with database() as session:
        first = await service.recover(session, token)
        second = await service.recover(session, token)
        assert first == second
        assert first["locale"] == locale
        assert first["items"][0]["product_id"] == product_id
        assert first["items"][0]["product"].price == Decimal("120.00")
        assert first["changes"] == []
        assert first["checkout_key"] != token
        assert await session.scalar(select(CustomerCartItem.id)) is None
        assert await session.scalar(select(Order.id)) is None
    # Original management key still cancels after cross-device recovery.
    async with database() as session:
        await service.cancel(session, key)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.recover(session, token)
        assert error.value.status_code == 410


@pytest.mark.anyio
async def test_checkpoint_replays_do_not_write_or_extend_expiry(database):
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id)
    before = await record(database)
    await request_reminder(database, product_id, key=key)
    after = await record(database)
    assert after.id == before.id
    assert (after.updated_at, after.due_at, after.expires_at) == (before.updated_at, before.due_at, before.expires_at)


@pytest.mark.anyio
@pytest.mark.parametrize("suppression", ["do_not_contact", "blacklisted", "transactional", "inactive"])
async def test_guest_email_cannot_bypass_account_suppression(database, renderer, suppression):
    product_id, owner = await seed(database, customer=True)
    async with database() as session:
        preference = ClientCommunicationPreference(customer_id=owner.id)
        if suppression == "blacklisted":
            preference.blacklisted_at = datetime.now(UTC)
        elif suppression == "transactional":
            preference.transactional_consent = ConsentStatus.opted_out
        elif suppression == "inactive":
            saved = await session.get(Customer, owner.id)
            saved.is_active = False
        else:
            preference.do_not_contact = True
        session.add(preference)
        await session.commit()
    await request_reminder(database, product_id)
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    assert await service.process_due(session_factory=database, provider=provider, dry_run=False) == 0
    assert (await record(database)).state == "suppressed"
    assert not provider.messages


@pytest.mark.anyio
async def test_preference_changed_after_checkpoint_and_cart_removal_suppress(database, renderer):
    product_id, owner = await seed(database, customer=True)
    await request_reminder(database, product_id, customer=owner)
    async with database() as session:
        cart = await session.scalar(select(CustomerCartItem))
        await session.delete(cart)
        await session.commit()
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    assert not provider.messages
    assert (await record(database)).state == "suppressed"


@pytest.mark.anyio
async def test_completion_and_rollback_share_order_transaction(database, renderer):
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id)
    order_payload = OrderCreate(firstName="Synthetic", phoneNumber="+380501112230",
        items=[{"productId": product_id, "quantity": 1}])
    async with database() as session:
        await OrderService().create_order(session, order_payload, checkout_key=key, commit=False)
        await session.rollback()
    assert (await record(database)).state == "pending"
    async with database() as session:
        order = await OrderService().create_order(session, order_payload, checkout_key=key)
        order_id = order.id
    row = await record(database)
    assert row.state == "completed" and row.order_id == order_id and row.email is None
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    assert await service.process_due(session_factory=database, provider=provider, dry_run=False) == 0


@pytest.mark.anyio
async def test_cancel_before_checkpoint_and_complete_before_checkpoint_block_late_events(database):
    product_id, _ = await seed(database)
    key = str(uuid4())
    async with database() as session:
        await service.cancel(session, key)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.checkpoint(session, key, payload(product_id))
        assert error.value.status_code == 409


@pytest.mark.anyio
async def test_recovery_revalidates_price_variant_stock_and_isolated_account(database, renderer):
    product_id, owner = await seed(database, customer=True)
    await request_reminder(database, product_id, customer=owner)
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    token = token_from(provider)
    async with database() as session:
        product = await session.get(Product, product_id)
        product.price = Decimal("150.00")
        product.volume_ml = 200
        product.stock_quantity = 0
        await session.commit()
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.recover(session, token)
        assert error.value.status_code == 404
        recovered = await service.recover(session, token, owner)
        assert set(recovered["changes"]) == {f"price_changed:{product_id}", f"variant_changed:{product_id}", f"out_of_stock:{product_id}"}
        assert recovered["items"][0]["product"].is_available_for_purchase is False
        assert recovered["items"][0]["quantity"] == 1
    async with database() as session:
        product = await session.get(Product, product_id)
        product.is_active = False
        await session.commit()
    async with database() as session:
        result = await service.recover(session, token, owner)
        assert result["items"] == [] and result["changes"] == [f"unavailable:{product_id}"]


@pytest.mark.anyio
async def test_expiry_invalid_tokens_and_retention_purge(database, renderer):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    token = token_from(provider)
    async with database() as session:
        row = await session.scalar(select(CheckoutReminder))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        for invalid in (token, "z" * 43):
            with pytest.raises(HTTPException) as error:
                await service.recover(session, invalid)
            assert error.value.status_code == 410
        await service.purge(session)
    row = await record(database)
    assert row.state == "expired" and row.snapshot == [] and row.token_hash is None
    async with database() as session:
        await service.purge(session, now=datetime.now(UTC) + timedelta(days=8))
        assert await session.scalar(select(CheckoutReminder.id)) is None


@pytest.mark.anyio
async def test_parallel_workers_reserve_once_and_unknown_outcome_never_retries(database, renderer):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    await due(database)

    class UncertainProvider(service.FakeCheckoutReminderProvider):
        calls = 0

        async def send(self, **kwargs):
            self.calls += 1
            raise TimeoutError("Do not retain this synthetic destination or token")

    provider = UncertainProvider()
    await asyncio.gather(*[service.process_due(session_factory=database, provider=provider, dry_run=False) for _ in range(2)])
    assert provider.calls == 1
    row = await record(database)
    assert row.state == "uncertain" and row.last_error_code == "delivery_uncertain"
    await due(database)
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    assert provider.calls == 1


@pytest.mark.anyio
async def test_definitive_rejection_retries_bounded_same_delivery_key(database, renderer):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)

    class RejectedProvider(service.FakeCheckoutReminderProvider):
        keys = []

        async def send(self, **kwargs):
            self.keys.append(kwargs["delivery_key"])
            raise service.DefinitiveReminderRejection()

    provider = RejectedProvider()
    for attempt in range(3):
        await due(database)
        await service.process_due(session_factory=database, provider=provider, dry_run=False)
        assert (await record(database)).attempts == attempt + 1
    row = await record(database)
    assert row.state == "cancelled" and row.email is None
    assert len(provider.keys) == 3 and len(set(provider.keys)) == 1


@pytest.mark.anyio
async def test_live_transport_requires_approval_and_dry_run_never_dispatch(database, renderer):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    await due(database)
    with pytest.raises(RuntimeError, match="checkout_reminder_live_not_approved"):
        await service.process_due(session_factory=database, provider=object(), dry_run=False)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=True)
    assert not provider.messages
    assert (await record(database)).state == "dry_run"


@pytest.mark.anyio
async def test_api_contract_private_headers_and_no_get_token_consumption(database):
    product_id, _ = await seed(database)
    app = FastAPI()
    app.include_router(routes.public_router)

    async def db():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        capability = await client.get("/checkout-reminders/capabilities")
        assert capability.json() == {"enabled": True, "dry_run": True}
        assert capability.headers["cache-control"] == "no-store"
        response = await client.post("/checkout-reminders/checkpoint", json=payload(product_id).model_dump(),
            headers={"X-Checkout-Key": str(uuid4())})
        assert response.status_code == 200
        assert response.json() == {"accepted": True}
        assert response.headers["referrer-policy"] == "no-referrer"
        assert (await client.get("/checkout-reminders/recover?token=" + "z" * 43)).status_code == 405
        assert (await client.post("/checkout-reminders/recover", json={"token": "z" * 43})).status_code == 410


def test_production_and_staging_capabilities_fail_closed(monkeypatch):
    for environment in ('production', 'staging'):
        monkeypatch.setattr(settings, 'app_env', environment)
        assert service.enabled() is False


def test_relative_media_resolution_rejects_foreign_urls(monkeypatch):
    monkeypatch.setattr(settings, 'public_api_base_url', 'https://api.example.test/api/v1')
    assert service.email_asset_url('/media/products/synthetic.webp') == 'https://api.example.test/media/products/synthetic.webp'
    for unsafe in ('//evil.test/media/a.webp', '/other/a.webp', '/media/a?tracking=1', '/media/a#fragment', '/media/\\evil', '/media/ bad'):
        assert service.email_asset_url(unsafe) == ''
    monkeypatch.setattr(settings, 'public_api_base_url', 'https://user:secret@api.example.test')
    assert service.email_asset_url('/media/products/a.webp') == ''


@pytest.mark.anyio
@pytest.mark.parametrize("scoped", [False, True])
async def test_real_renderer_receives_resolved_upload_image(database, monkeypatch, scoped):
    product_id, _ = await seed(database)
    monkeypatch.setattr(settings, 'public_api_base_url', None if scoped else 'https://api.example.test/api/v1')
    monkeypatch.setattr(settings, 'checkout_reminder_media_origin', 'https://api.example.test' if scoped else '')
    async with database() as session:
        product = await session.get(Product, product_id)
        product.image_url = '/media/products/synthetic.webp'
        await session.commit()
    await request_reminder(database, product_id)
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    message = next(iter(provider.messages.values()))[1]
    assert 'src="https://api.example.test/media/products/synthetic.webp"' in message.get_body(preferencelist=('html',)).get_content()


@pytest.mark.anyio
async def test_guest_new_suppression_revokes_sent_recovery(database, renderer):
    product_id, owner = await seed(database, customer=True)
    await request_reminder(database, product_id)  # Guest: no account ownership association.
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    token = token_from(provider)
    row = await record(database)
    assert row.customer_id is None
    async with database() as session:
        assert (await service.recover(session, token))['items']
        session.add(ClientCommunicationPreference(customer_id=owner.id, do_not_contact=True))
        await session.commit()
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.recover(session, token)
        assert error.value.status_code == 410


@pytest.mark.anyio
async def test_recovered_key_completes_original_checkout_atomically(database, renderer):
    product_id, _ = await seed(database)
    original_key = await request_reminder(database, product_id)
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    async with database() as session:
        recovery = await service.recover(session, token_from(provider))
    assert recovery['checkout_key'] != original_key
    async with database() as session:
        order = await OrderService().create_order(session, OrderCreate(firstName='Synthetic', phoneNumber='+380501112230',
            items=[{'productId': product_id, 'quantity': 1}]), checkout_key=recovery['checkout_key'])
        order_id = order.id
    row = await record(database)
    assert row.state == 'completed' and row.order_id == order_id
    assert row.management_hash == service.key_digest(original_key)
    assert row.email is None and row.snapshot == []


@pytest.mark.anyio
async def test_disabled_delivery_still_purges_expired_contact(database, monkeypatch):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    async with database() as session:
        row = await session.scalar(select(CheckoutReminder))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    monkeypatch.setattr(settings, 'checkout_reminder_enabled', False)
    assert await service.process_due(session_factory=database) == 0
    row = await record(database)
    assert row.state == 'expired' and row.email is None and row.snapshot == []


@pytest.mark.anyio
@pytest.mark.parametrize('missing', ['email'])
async def test_due_record_without_email_never_sends(database, renderer, missing):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    async with database() as session:
        row = await session.scalar(select(CheckoutReminder))
        setattr(row, missing, None)
        await session.commit()
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    assert not provider.messages
    assert (await record(database)).state == 'suppressed'


@pytest.mark.anyio
async def test_email_and_source_budget_share_durable_hashed_admission(database, monkeypatch):
    from types import SimpleNamespace
    from app.models.shop_ingress_limits import ShopIngressAdmission
    from app.services.shop_ingress_limits import admit_checkout_reminder_email, admit_shop_ingress
    monkeypatch.setattr(settings, 'shared_ingress_limits_enabled', True)
    monkeypatch.setattr(settings, 'checkout_reminder_source_limit', 1)
    await admit_checkout_reminder_email('synthetic-budget@example.com', session_factory=database)
    with pytest.raises(HTTPException) as error:
        await admit_checkout_reminder_email('SYNTHETIC-BUDGET@example.com', session_factory=database)
    assert error.value.status_code == 429
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.33'))
    await admit_shop_ingress(request, 'reminder', session_factory=database)
    with pytest.raises(HTTPException) as error:
        await admit_shop_ingress(request, 'reminder', session_factory=database)
    assert error.value.status_code == 429
    async with database() as session:
        rows = (await session.execute(select(ShopIngressAdmission))).scalars().all()
        assert len(rows) == 2
        assert all(len(row.source_key) == 64 and '@' not in row.source_key for row in rows)


@pytest.mark.anyio
async def test_order_completion_wins_concurrent_worker_without_send(database, renderer):
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id)
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    async with database() as session:
        await OrderService().create_order(session, OrderCreate(firstName='Synthetic', phoneNumber='+380501112230',
            items=[{'productId': product_id, 'quantity': 1}]), checkout_key=key, commit=False)
        assert await service.process_due(session_factory=database, provider=provider, dry_run=False) == 0
        await session.commit()
    assert not provider.messages
    assert (await record(database)).state == 'completed'


@pytest.mark.anyio
async def test_server_cart_mutation_before_final_dispatch_is_detected(database, renderer, monkeypatch):
    product_id, owner = await seed(database, customer=True)
    await request_reminder(database, product_id, customer=owner)
    await due(database)
    original_matches = service.cart_matches
    mutated = False

    async def mutate_before_final_fence(session, row, *, lock=False):
        nonlocal mutated
        if lock and not mutated:
            mutated = True
            async with database() as other:
                # Same advisory lock as the real customer cart delete endpoint.
                from sqlalchemy import func
                await other.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f'shop-cart:{owner.id}', 0))))
                cart = await other.scalar(select(CustomerCartItem))
                await other.delete(cart)
                await other.commit()
        return await original_matches(session, row, lock=lock)

    monkeypatch.setattr(service, 'cart_matches', mutate_before_final_fence)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    assert mutated and not provider.messages
    assert (await record(database)).state == 'suppressed'


@pytest.mark.anyio
async def test_additive_migration_extends_shared_budgets_and_downgrades_safely(database):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.services.shop_ingress_limits import admit_checkout_reminder_email
    from app.models.shop_ingress_limits import ShopIngressAdmission
    migration_path = Path(__file__).resolve().parents[1] / 'alembic/versions/0142_checkout_reminders.py'
    spec = importlib.util.spec_from_file_location('reminder_migration_test', migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate_up(connection):
        operations = Operations(MigrationContext.configure(connection, opts={"target_metadata": Base.metadata}))
        operations.drop_table('checkout_reminders')
        operations.drop_constraint(operations.f('ck_shop_ingress_admissions_action'), 'shop_ingress_admissions', type_='check')
        operations.create_check_constraint(operations.f('ck_shop_ingress_admissions_action'), 'shop_ingress_admissions',
            "action IN ('otp_send', 'otp_verify', 'checkout', 'feedback', 'review', 'profile', 'search', 'cart')")
        migration.op = operations
        migration.upgrade()
        from sqlalchemy import inspect
        names = {item['name'] for item in inspect(connection).get_check_constraints('checkout_reminders')}
        assert names == {'ck_checkout_reminders_checkout_reminder_state', 'ck_checkout_reminders_checkout_reminder_attempts'}

    async with database() as session:
        connection = await session.connection()
        await connection.run_sync(migrate_up)
        await session.commit()
    # Exercise the actual new action against the migrated database constraint.
    from app.core.config import settings as config
    original = config.shared_ingress_limits_enabled
    try:
        config.shared_ingress_limits_enabled = True
        await admit_checkout_reminder_email('synthetic-migration@example.com', session_factory=database)
    finally:
        config.shared_ingress_limits_enabled = original

    def migrate_down(connection):
        migration.op = Operations(MigrationContext.configure(connection, opts={"target_metadata": Base.metadata}))
        migration.downgrade()

    async with database() as session:
        assert await session.scalar(select(ShopIngressAdmission.id)) is not None
        connection = await session.connection()
        await connection.run_sync(migrate_down)
        await session.commit()
        assert await session.scalar(select(ShopIngressAdmission.id)) is None


def test_live_policy_approval_gates_runtime_without_network(monkeypatch):
    monkeypatch.setattr(settings, 'app_env', 'production')
    monkeypatch.setattr(settings, 'checkout_reminder_dry_run', False)
    monkeypatch.setattr(settings, 'checkout_reminder_live_enabled', True)
    monkeypatch.setattr(settings, 'checkout_reminder_launch_approved', True)
    monkeypatch.setattr(settings, 'email_notifications_enabled', True)
    monkeypatch.setattr(settings, 'smtp_host', 'smtp.example.invalid')
    monkeypatch.setattr(settings, 'smtp_from_email', 'synthetic@example.com')
    assert service.live_ready() and service.enabled() and service.scheduler_allowed()
    assert service.capabilities() == {'enabled': True, 'dry_run': False}
    for gate in ('checkout_reminder_live_enabled', 'checkout_reminder_launch_approved', 'email_notifications_enabled'):
        monkeypatch.setattr(settings, gate, False)
        assert not service.live_ready() and not service.enabled() and not service.scheduler_allowed()
        monkeypatch.setattr(settings, gate, True)


@pytest.mark.anyio
async def test_permanent_rejection_cancels_without_retry(database, renderer):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    await due(database)

    class PermanentFakeProvider(service.FakeCheckoutReminderProvider):
        calls = 0

        async def send(self, **kwargs):
            self.calls += 1
            raise service.PermanentReminderRejection()

    provider = PermanentFakeProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    await due(database)
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    row = await record(database)
    assert provider.calls == 1 and row.state == 'cancelled' and row.attempts == 1
    assert row.email is None and row.token_hash is None
    assert row.last_error_code == 'provider_permanent_rejection'


@pytest.mark.anyio
async def test_configuration_error_preserves_pending_reminder(database, renderer, monkeypatch):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    await due(database)
    monkeypatch.setattr(settings, 'checkout_reminder_shop_url', '')
    with pytest.raises(ValueError, match='checkout_reminder_url_config'):
        await service.process_due(session_factory=database, provider=service.FakeCheckoutReminderProvider(), dry_run=False)
    row = await record(database)
    assert row.state == 'pending' and row.attempts == 0 and row.token_hash is None


@pytest.mark.anyio
async def test_historical_order_replay_also_completes_provided_checkout(database):
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id)
    order_key = str(uuid4())
    order_payload = OrderCreate(firstName='Synthetic', phoneNumber='+380501112230',
        items=[{'productId': product_id, 'quantity': 1}])
    async with database() as session:
        initial = await OrderService().create_order(session, order_payload, idempotency_key=order_key)
        order_id = initial.id
    assert (await record(database)).state == 'pending'
    for _ in range(2):
        async with database() as session:
            replayed = await OrderService().create_order(session, order_payload, idempotency_key=order_key, checkout_key=key)
            assert replayed.id == order_id
    row = await record(database)
    assert row.state == 'completed' and row.order_id == order_id


@pytest.mark.anyio
@pytest.mark.parametrize('recipient',['synthetic-checkout@example.com','\u0442\u0435\u0441\u0442@example.com'])
async def test_invalid_live_smtp_config_preserves_pending_before_claim(database, renderer, monkeypatch, recipient):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id, email=recipient)
    await due(database)
    monkeypatch.setattr(settings, 'checkout_reminder_live_enabled', True)
    monkeypatch.setattr(settings, 'checkout_reminder_launch_approved', True)
    monkeypatch.setattr(settings, 'email_notifications_enabled', True)
    monkeypatch.setattr(settings, 'smtp_host', 'smtp.example.invalid')
    monkeypatch.setattr(settings, 'smtp_from_email', 'invalid-mailbox')

    def forbidden_connection(*args, **kwargs):
        raise AssertionError('Invalid configuration must never open SMTP')

    from app.services import checkout_reminder_smtp
    monkeypatch.setattr(checkout_reminder_smtp.smtplib, 'SMTP', forbidden_connection)
    with pytest.raises(ValueError, match='reminder_smtp_address_invalid'):
        await service.process_due(session_factory=database, dry_run=False)
    row = await record(database)
    assert row.state == 'pending' and row.attempts == 0 and row.token_hash is None
    assert row.email==recipient and row.snapshot


@pytest.mark.anyio
async def test_unsupported_recipient_does_not_starve_following_due_reminder(database, renderer, monkeypatch):
    from app.services import checkout_reminder_smtp
    product_id,_=await seed(database)
    await request_reminder(database,product_id,email='\u0442\u0435\u0441\u0442@example.com')
    await request_reminder(database,product_id,email='supported@example.com')
    await due(database)
    for name,value in {
        'checkout_reminder_live_enabled':True,'checkout_reminder_launch_approved':True,
        'email_notifications_enabled':True,'smtp_host':'smtp.example.invalid',
        'smtp_from_email':'service@example.com',
    }.items():
        monkeypatch.setattr(settings,name,value)
    accepted=[]
    async def local_send(self,*,delivery_key,email,message):
        self._validate(email,delivery_key,message)
        accepted.append(email)
    def forbidden_connection(*args,**kwargs):
        raise AssertionError('Regression must never connect to SMTP')
    monkeypatch.setattr(checkout_reminder_smtp.smtplib,'SMTP',forbidden_connection)
    monkeypatch.setattr(checkout_reminder_smtp.CheckoutReminderSMTPProvider,'send',local_send)
    assert await service.process_due(session_factory=database,dry_run=False)==1
    async with database() as session:
        rejected,sent=(await session.execute(select(CheckoutReminder).order_by(CheckoutReminder.id))).scalars().all()
        assert rejected.state=='cancelled' and rejected.attempts==0
        assert rejected.last_error_code=='recipient_unsupported'
        assert rejected.email is None and rejected.email_hash is None and rejected.snapshot==[]
        assert rejected.payload_hash is None and rejected.token_hash is None and rejected.recovery_management_hash is None
        assert sent.state=='sent' and sent.attempts==1
    assert accepted==['supported@example.com']
    assert await service.process_due(session_factory=database,dry_run=False)==0
    assert accepted==['supported@example.com']


@pytest.mark.anyio
@pytest.mark.parametrize('missing', ['checkpoint_at', 'checkpoint_version'])
async def test_checkpoint_metadata_is_observational_not_an_optin_gate(database, renderer, missing):
    product_id, _ = await seed(database)
    await request_reminder(database, product_id)
    async with database() as session:
        row = await session.scalar(select(CheckoutReminder))
        setattr(row, missing, None)
        await session.commit()
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    assert len(provider.messages) == 1
    assert (await record(database)).state == 'sent'


@pytest.mark.anyio
async def test_versioned_invalidation_rearms_same_checkout_without_new_email_admission(database, renderer, monkeypatch):
    product_id, _ = await seed(database)
    admissions = []

    async def admit(email):
        admissions.append(email)

    monkeypatch.setattr(service, 'admit_checkout_reminder_email', admit)
    key = await request_reminder(database, product_id, revision=1)
    original = await record(database)
    async with database() as session:
        await service.cancel(session, key, revision=2)
    cancelled = await record(database)
    assert cancelled.state == 'cancelled' and cancelled.email is None and cancelled.snapshot == []
    assert cancelled.email_hash == original.email_hash
    async with database() as session:
        await service.checkpoint(session, key, payload(product_id, quantity=9), revision=1)
    assert (await record(database)).state == 'cancelled'
    await request_reminder(database, product_id, key=key, quantity=2, revision=3)
    updated = await record(database)
    assert updated.id == original.id and updated.state == 'pending' and updated.checkpoint_revision == 3
    assert updated.snapshot[0]['quantity'] == 2 and updated.expires_at == original.expires_at
    assert len(admissions) == 1
    async with database() as session:
        await service.cancel(session, key, revision=2)
        await service.checkpoint(session, key, payload(product_id, quantity=9), revision=3)
    assert (await record(database)).snapshot[0]['quantity'] == 2
    await request_reminder(database, product_id, key=key, quantity=2, revision=4)
    newer = await record(database)
    assert newer.checkpoint_revision == 4 and newer.due_at == updated.due_at
    assert newer.expires_at == original.expires_at
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    async with database() as session:
        await service.cancel(session, key, revision=5)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.checkpoint(session, key, payload(product_id), revision=6)
        assert error.value.status_code == 409
    assert len(provider.messages) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('state', ['completed', 'sent', 'dry_run', 'uncertain', 'attempting', 'suppressed', 'expired'])
async def test_terminal_states_cannot_be_rearmed_through_cancel(database, state):
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id, revision=1)
    async with database() as session:
        row = await session.scalar(select(CheckoutReminder))
        row.state = state
        await session.commit()
    async with database() as session:
        await service.cancel(session, key, revision=2)
    closed = await record(database)
    assert closed.state == ('cancelled' if state == 'attempting' else state)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.checkpoint(session, key, payload(product_id), revision=3)
        assert error.value.status_code == 409


@pytest.mark.anyio
async def test_versioned_cancel_before_checkpoint_fences_stale_events(database):
    product_id, _ = await seed(database)
    key = str(uuid4())
    async with database() as session:
        await service.cancel(session, key, revision=2)
    cancelled = await record(database)
    async with database() as session:
        await service.checkpoint(session, key, payload(product_id), revision=1)
    assert (await record(database)).state == 'cancelled'
    await request_reminder(database, product_id, key=key, revision=3)
    row = await record(database)
    assert row.state == 'pending' and row.checkpoint_revision == 3 and row.expires_at == cancelled.expires_at
    # An unversioned legacy checkpoint cannot overwrite a versioned snapshot.
    async with database() as session:
        await service.checkpoint(session, key, payload(product_id, quantity=9))
    assert (await record(database)).snapshot[0]['quantity'] == 1


@pytest.mark.anyio
async def test_legacy_cancel_remains_terminal_even_for_newer_versioned_events(database):
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id, revision=1)
    async with database() as session:
        await service.cancel(session, key)
    assert (await record(database)).checkpoint_revision == 0
    async with database() as session:
        await service.cancel(session, key, revision=2)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.checkpoint(session, key, payload(product_id), revision=3)
        assert error.value.status_code == 409


@pytest.mark.anyio
async def test_concurrent_checkpoint_and_cancel_keep_highest_deliberate_revision(database):
    product_id, _ = await seed(database)
    key = str(uuid4())

    async def save(revision, quantity):
        async with database() as session:
            await service.checkpoint(session, key, payload(product_id, quantity=quantity), revision=revision)

    async def invalidate():
        async with database() as session:
            await service.cancel(session, key, revision=2)

    await asyncio.gather(save(1, 1), invalidate(), save(3, 3))
    row = await record(database)
    assert row.state == 'pending' and row.checkpoint_revision == 3
    assert row.snapshot[0]['quantity'] == 3


@pytest.mark.anyio
async def test_checkpoint_revision_api_bounds_and_consent_free_body(database):
    product_id, _ = await seed(database)
    app = FastAPI()
    app.include_router(routes.public_router)

    async def db():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    headers = {'X-Checkout-Key': str(uuid4())}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for invalid in ('0', '-1', '2147483648', 'not-an-integer'):
            invalid_headers = {**headers, 'X-Checkout-Revision': invalid}
            assert (await client.post('/checkout-reminders/checkpoint', json=payload(product_id).model_dump(), headers=invalid_headers)).status_code == 422
            assert (await client.post('/checkout-reminders/cancel', headers=invalid_headers)).status_code == 422
        result = await client.post('/checkout-reminders/checkpoint', json=payload(product_id).model_dump(),
            headers={**headers, 'X-Checkout-Revision': '1'})
        assert result.status_code == 200
        assert (await record(database)).checkpoint_revision == 1
        old_body = {**payload(product_id).model_dump(), 'consent': True}
        assert (await client.post('/checkout-reminders/checkpoint', json=old_body, headers=headers)).status_code == 422


@pytest.mark.anyio
async def test_0143_renames_existing_dev_schema_without_historical_enrollment(database):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    base = Path(__file__).resolve().parents[1] / 'alembic/versions'

    def load(filename):
        spec = importlib.util.spec_from_file_location(filename[:-3], base / filename)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    original = load('0142_checkout_reminders.py')
    automatic = load('0143_auto_checkout_checkpoint.py')
    checkpoint_at = datetime.now(UTC)
    expires_at = checkpoint_at + timedelta(hours=72)

    def upgrade_from_0142(connection):
        operations = Operations(MigrationContext.configure(connection, opts={'target_metadata': Base.metadata}))
        operations.drop_table('checkout_reminders')
        original.op = operations
        original.upgrade()
        connection.execute(text("INSERT INTO checkout_reminders (management_hash,email,locale,consent_at,consent_version,snapshot,state,due_at,expires_at) VALUES (:key,:email,'en',:checkpoint,'checkout-service-v1','[]','pending',:checkpoint,:expiry)"),
            {'key': 'a' * 64, 'email': 'synthetic-migration@example.com', 'checkpoint': checkpoint_at, 'expiry': expires_at})
        automatic.op = operations
        automatic.upgrade()
        columns = {column['name'] for column in inspect(connection).get_columns('checkout_reminders')}
        assert {'checkpoint_at', 'checkpoint_version', 'checkpoint_revision'} <= columns
        assert 'consent_at' not in columns and 'consent_version' not in columns
        constraints = {constraint['name'] for constraint in inspect(connection).get_check_constraints('checkout_reminders')}
        assert 'ck_checkout_reminders_checkout_reminder_revision' in constraints

    async with database() as session:
        connection = await session.connection()
        await connection.run_sync(upgrade_from_0142)
        await session.commit()
        rows = (await session.execute(select(CheckoutReminder))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.email == 'synthetic-migration@example.com' and row.checkpoint_revision == 0
        assert row.checkpoint_at == checkpoint_at and row.expires_at == expires_at
        assert row.checkpoint_version == 'checkout-service-v1' and row.state == 'pending'

    def downgrade_to_0142(connection):
        automatic.op = Operations(MigrationContext.configure(connection, opts={'target_metadata': Base.metadata}))
        automatic.downgrade()
        columns = {column['name'] for column in inspect(connection).get_columns('checkout_reminders')}
        assert {'consent_at', 'consent_version'} <= columns
        assert 'checkpoint_at' not in columns and 'checkpoint_revision' not in columns
        legacy = connection.execute(text('SELECT consent_at, consent_version FROM checkout_reminders')).one()
        assert legacy.consent_at == checkpoint_at and legacy.consent_version == 'checkout-service-v1'

    async with database() as session:
        connection = await session.connection()
        await connection.run_sync(downgrade_to_0142)
        await session.commit()


def test_reminder_migrations_extend_current_production_chain():
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision("0142_checkout_reminders").down_revision == "0141_product_shop_price"
    assert scripts.get_revision("0143_auto_checkout_checkpoint").down_revision == "0142_checkout_reminders"


@pytest.mark.anyio
async def test_authenticated_rearm_binds_prior_guest_checkout_to_account(database, renderer):
    from types import SimpleNamespace
    product_id, owner = await seed(database, customer=True)
    key = await request_reminder(database, product_id, revision=1)
    assert (await record(database)).customer_id is None
    async with database() as session:
        await service.cancel(session, key, revision=2)
    await request_reminder(database, product_id, key=key, customer=owner, revision=3)
    assert (await record(database)).customer_id == owner.id
    await due(database)
    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=database, provider=provider, dry_run=False)
    token = token_from(provider)
    async with database() as session:
        for outsider in (None, SimpleNamespace(id=owner.id + 1)):
            with pytest.raises(HTTPException) as error:
                await service.recover(session, token, outsider)
            assert error.value.status_code == 404
        recovered = await service.recover(session, token, owner)
        assert recovered['items'][0]['product_id'] == product_id


@pytest.mark.anyio
async def test_cancel_after_durable_reservation_blocks_final_dispatch(database, renderer):
    from contextlib import asynccontextmanager
    product_id, _ = await seed(database)
    key = await request_reminder(database, product_id, revision=1)
    await due(database)
    cancelled = False

    @asynccontextmanager
    async def worker_sessions():
        nonlocal cancelled
        async with database() as session:
            original_commit = session.commit

            async def commit_then_cancel():
                nonlocal cancelled
                await original_commit()
                reserved = any(isinstance(row, CheckoutReminder) and row.state == 'attempting'
                    for row in session.identity_map.values())
                if reserved and not cancelled:
                    cancelled = True
                    async with database() as cancellation_session:
                        await service.cancel(cancellation_session, key, revision=2)

            session.commit = commit_then_cancel
            yield session

    provider = service.FakeCheckoutReminderProvider()
    await service.process_due(session_factory=worker_sessions, provider=provider, dry_run=False)
    row = await record(database)
    assert cancelled and not provider.messages
    assert row.state == 'cancelled' and row.attempts == 1 and row.email is None
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await service.checkpoint(session, key, payload(product_id), revision=3)
        assert error.value.status_code == 409
