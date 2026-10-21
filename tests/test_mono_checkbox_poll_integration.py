"""Automatic reconciliation against migrated local PG and injected GET mocks."""
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, func, text

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import OrderPayment
from app.models.shop_mono_checkbox import ShopMonoCheckboxReceipt
from app.schemas import shop_payments as dto
from app.services import shop_payments as payments, mono_checkbox_poll as poll
from tests.test_shop_mono_checkbox_integration import (
    flags, database, template_database, checkout_dependencies, seed, call, assertion,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def auto_enabled(monkeypatch):
    monkeypatch.setattr(settings, "mono_checkbox_auto_enabled", True)


async def pending(database, flags):
    identifier, _ = await seed(database)
    intent = await call(database, payments.command, identifier,
        dto.InvoiceIntentCommand(expected_revision=1, **assertion()), str(uuid4()), "invoice_intent")
    flags['reference'] = intent.intent.reference
    flags['invoice_status'] = 'processing'
    await call(database, payments.command, identifier,
        dto.VerifyInvoiceCommand(expected_revision=intent.revision, invoice_id='syntheticAutoInvoice', **assertion()), str(uuid4()), 'verify')
    return identifier


async def due(database):
    async with database() as session:
        await session.execute(text("UPDATE mono_checkbox_poll_state SET last_attempt_at=now()-interval '2 hours'"))
        await session.commit()


async def test_background_payment_then_late_receipt_once_only_and_durable_cooldown(database, flags):
    await pending(database, flags)
    assert await poll.poll_once(database) == {'checked':1,'blocked':0}
    before = len(flags['calls'])
    assert await poll.poll_once(database) == {'checked':0,'blocked':0}
    assert len(flags['calls']) == before
    flags['invoice_status'] = 'success'
    flags['checks'][0]['status'] = 'process'
    await due(database)
    assert await poll.poll_once(database) == {'checked':1,'blocked':0}
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(ShopMonoCheckboxReceipt)) == 0
    flags['checks'][0]['status'] = 'done'
    await due(database)
    assert await poll.poll_once(database) == {'checked':1,'blocked':0}
    await due(database)
    assert await poll.poll_once(database) == {'checked':1,'blocked':0}
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 1
        assert await session.scalar(select(func.count()).select_from(ShopMonoCheckboxReceipt)) == 1


async def test_concurrent_batches_share_lease_and_cooldown(database, flags):
    await pending(database, flags)
    results = await asyncio.gather(poll.poll_once(database), poll.poll_once(database))
    assert sum(result['checked'] for result in results) == 1


async def test_revoked_operator_no_provider_reads_and_no_payments(database, flags):
    await pending(database, flags)
    async with database() as session:
        actor = await session.get(AdminUser, 1)
        actor.is_active = False
        await session.commit()
    before = len(flags['calls'])
    assert await poll.poll_once(database) == {'checked':0,'blocked':1}
    assert len(flags['calls']) == before
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(OrderPayment)) == 0


async def test_disabled_or_production_never_polls(database, flags, monkeypatch):
    await pending(database, flags)
    before = len(flags['calls'])
    monkeypatch.setattr(settings,'mono_checkbox_auto_enabled',False)
    assert await poll.poll_once(database) == {'checked':0,'blocked':0}
    monkeypatch.setattr(settings,'mono_checkbox_auto_enabled',True)
    monkeypatch.setattr(settings,'app_env','production')
    assert await poll.poll_once(database) == {'checked':0,'blocked':0}
    assert len(flags['calls']) == before
