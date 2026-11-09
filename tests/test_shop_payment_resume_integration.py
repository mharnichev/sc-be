"""Payment reminders and fresh browser resume through local API and fake providers."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import decode_token
from app.models.admin_user import AdminUser
from app.models.checkout_reminder import CheckoutReminder
from app.models.customer import Customer
from app.models.order import Order, OrderStatus
from app.schemas.order import OrderCreate
from app.schemas.shop_online_payment import OnlinePaymentRecovery
from app.services import checkout_reminders as reminders, shop_online_payment as payment, mono_checkbox_poll
from app.services.order import OrderService
from tests.test_shop_fiscal_integration import (
    anyio_backend, checkout_dependencies, database, template_database,
)
from tests.test_shop_online_payment_actions_integration import app_for
from tests.test_shop_online_payment_integration import online, start
from tests.test_shop_sales_integration import seed

pytestmark = pytest.mark.anyio


@pytest.fixture
def reminder_config(monkeypatch, online):
    for key, value in {
        "shop_checkout_enabled": True, "checkout_reminder_enabled": True,
        "checkout_reminder_dry_run": True, "shared_ingress_limits_enabled": False,
        "checkout_reminder_shop_url": "https://shop.example.test/shop/",
        "checkout_reminder_logo_url": "https://shop.example.test/logo.png",
        "checkout_reminder_support_email": "support@example.com",
    }.items():
        monkeypatch.setattr(settings, key, value)


async def create_order(database, product_id, *, checkout_key=None, owner=None):
    payload = OrderCreate(firstName="Synthetic", phoneNumber="+380500001234",
        email="synthetic-payment@example.com", shippingMethod="pickup", paymentMethod="onlineCard",
        items=[{"productId": product_id, "quantity": 1}])
    key = str(uuid4())
    async with database() as session:
        order = await OrderService().create_order(session, payload, idempotency_key=key, checkout_key=checkout_key, current_customer=owner)
        order_id, token = order.id, order.guest_payment_token
    async with database() as session:
        replay = await OrderService().create_order(session, payload, idempotency_key=key, checkout_key=checkout_key, current_customer=owner)
        assert replay.id == order_id
    return order_id, token


async def due(database, order_id):
    async with database() as session:
        rows = (await session.scalars(select(CheckoutReminder).where(
            CheckoutReminder.order_id == order_id))).all()
        payment_rows = [row for row in rows if row.checkpoint_version == reminders.PAYMENT_CHECKPOINT_VERSION]
        assert len(payment_rows) == 1
        payment_rows[0].due_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        return [row.state for row in rows]


async def test_payment_email_reopens_same_invoice_in_fresh_browser(
    database, checkout_dependencies, online, reminder_config, caplog, monkeypatch,
):
    _, product_id = await seed(database)
    order_id, original_token = await create_order(database, product_id, checkout_key="s" * 32)
    initial = await start(database, order_id, original_token)
    assert online["create_body"]["validity"] == 3 * 86400
    online["status"] = "created"
    assert sorted(await due(database, order_id)) == ["completed", "pending"]
    provider = reminders.FakeCheckoutReminderProvider()
    assert await reminders.process_due(session_factory=database, provider=provider, dry_run=False) == 1
    assert await reminders.process_due(session_factory=database, provider=provider, dry_run=False) == 0
    [(address, email)] = provider.messages.values()
    assert address == "synthetic-payment@example.com"
    assert address not in email.get_content()
    prefix = f"https://shop.example.test/shop/payment/{order_id}#token="
    token = next(line.removeprefix(prefix) for line in email.get_content().splitlines() if line.startswith(prefix))
    assert decode_token(token)["scope"] == "guest_order_payment"
    assert token not in caplog.text and address not in caplog.text

    two_days_later = datetime.now(UTC) + timedelta(days=2)

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return two_days_later

    monkeypatch.setattr(payment, "datetime", Future)
    monkeypatch.setattr(reminders, "now_utc", lambda: two_days_later)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(database)),
            base_url="https://api.example.test") as fresh:
        url = f"/orders/{order_id}/online-payment"
        headers = {"X-Order-Payment-Token": token}
        response = await fresh.get(url, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.json()["page_url"] == initial.page_url
        replay = await fresh.post(url, headers={**headers, "Idempotency-Key": str(uuid4())}, json={})
        assert replay.status_code == 200 and replay.json()["attempt_id"] == initial.attempt_id
        denied = await fresh.get(f"/orders/{order_id - 1}/online-payment", headers=headers)
        assert denied.status_code == 404 and denied.headers["referrer-policy"] == "no-referrer"
        with monkeypatch.context() as patch:
            patch.setattr(reminders, "now_utc", lambda: datetime.now(UTC) + timedelta(days=8))
            assert (await fresh.get(url, headers=headers)).status_code == 404
        async with database() as session:
            (await session.get(Order, order_id)).guest_tracking_revoked_at = datetime.now(UTC)
            await session.commit()
        assert (await fresh.get(url, headers=headers)).status_code == 404
    assert online["posts"] == 1


@pytest.mark.parametrize("terminal", ["cancelled", "unknown", "expired", "late_paid", "paid_before_poll", "processing"])
async def test_ineligible_order_does_not_send_or_redispatch(
    database, checkout_dependencies, online, reminder_config, terminal, monkeypatch,
):
    _, product_id = await seed(database)
    order_id, token = await create_order(database, product_id)
    if terminal == "cancelled":
        async with database() as session:
            (await session.get(Order, order_id)).status = OrderStatus.cancelled
            await session.commit()
    else:
        online["unknown"] = terminal == "unknown"
        await start(database, order_id, token)
        if terminal == "expired":
            future = datetime.now(UTC) + timedelta(days=4)

            class Future(datetime):
                @classmethod
                def now(cls, tz=None):
                    return future

            monkeypatch.setattr(payment, "datetime", Future)
        if terminal in {"late_paid", "paid_before_poll"}:
            online["status"] = "success"
        if terminal == "late_paid":
            await mono_checkbox_poll.poll_once(database)
        posts = online["posts"]
        result = await start(database, order_id, token)
        assert not result.actions.start and not result.actions.retry
        assert result.reason == {"expired": "invoice_expired", "unknown": "invoice_creation_unknown",
            "late_paid": "payment_verified_receipt_pending", "paid_before_poll": "payment_pending",
            "processing": "payment_pending"}[terminal]
        assert online["posts"] == posts
    await due(database, order_id)
    provider = reminders.FakeCheckoutReminderProvider()
    assert await reminders.process_due(session_factory=database, provider=provider, dry_run=False) == 0
    assert not provider.messages


async def test_account_email_requires_owner_and_dry_run_does_not_send(
    database, checkout_dependencies, online, reminder_config,
):
    _, product_id = await seed(database)
    async with database() as session:
        owner = Customer(name="Synthetic owner", phone="+380500001235", email="synthetic-payment@example.com")
        session.add(owner)
        await session.commit()
    order_id, token = await create_order(database, product_id, owner=owner)
    assert token is None
    await due(database, order_id)
    provider = reminders.FakeCheckoutReminderProvider()
    assert await reminders.process_due(session_factory=database, provider=provider, dry_run=False) == 1
    [(_, email)] = provider.messages.values()
    assert f"/shop/payment/{order_id}" in email.get_content() and "#token=" not in email.get_content()
    async with database() as session:
        assert (await payment.read(session, order_id, owner)).actions.start
        with pytest.raises(payment.HTTPException) as denied:
            await payment.read(session, order_id)
        assert denied.value.status_code == 404
    assert online["posts"] == 0
    another, _ = await create_order(database, product_id, owner=owner)
    await due(database, another)
    assert await reminders.process_due(session_factory=database, provider=provider, dry_run=True) == 1
    assert len(provider.messages) == 1


async def test_payment_email_admission_limits_distinct_orders_for_the_same_recipient(
    database, checkout_dependencies, online, reminder_config, monkeypatch,
):
    monkeypatch.setattr(settings, "shared_ingress_limits_enabled", True)
    _, product_id = await seed(database)
    first, _ = await create_order(database, product_id)
    second, _ = await create_order(database, product_id)
    await due(database, first)
    await due(database, second)
    provider = reminders.FakeCheckoutReminderProvider()
    assert await reminders.process_due(session_factory=database, provider=provider, dry_run=False) == 1
    assert len(provider.messages) == 1
    async with database() as session:
        later = await session.scalar(select(CheckoutReminder).where(CheckoutReminder.order_id == second))
        assert later.state == "pending" and later.attempts == 0
        assert later.due_at > datetime.now(UTC) + timedelta(hours=23)



async def test_recovered_paid_invoice_never_exposes_the_payment_page(
    database, checkout_dependencies, online, reminder_config,
):
    _, product_id = await seed(database)
    order_id, token = await create_order(database, product_id)
    online["unknown"] = True
    assert (await start(database, order_id, token)).status == "creation_unknown"
    online["status"] = "success"
    async with database() as session:
        order = await session.get(Order, order_id)
        actor = await session.get(AdminUser, 1)
        await payment.recover(session, actor, order_id, OnlinePaymentRecovery(
            expected_revision=order.revision, invoice_id="syntheticOnlineInvoice"), str(uuid4()))
    state = await start(database, order_id, token)
    assert state.status == "requires_review" and not state.actions.continue_payment
    assert state.page_url is None and online["posts"] == 1
