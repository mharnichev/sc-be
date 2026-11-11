"""Populated PostgreSQL upgrade and forward recovery for owner archive."""

import asyncio
from uuid import uuid4

from alembic import command as migration
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.migration_readiness import require_current_schema
from app.models.order import OrderStatus
from app.services import mono_checkbox_poll
from tests.test_order_owner_archive_integration import command, stock
from tests.test_shop_fiscal_integration import anyio_backend, checkout_dependencies, database, template_database
from tests.test_shop_online_payment_integration import online, prepared, start
from tests.test_shop_payment_resume_integration import create_order

pytestmark = pytest.mark.anyio
PREVIOUS = "0152_shop_online_admin_cancel"
ARCHIVE = "0153_order_owner_archive"


async def snapshot(database):
    async with database() as session:
        return (await session.execute(text("""
            SELECT (SELECT version_num FROM alembic_version) AS revision,
                jsonb_build_object(
                    'payments', (SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id) FROM order_payments p),
                    'confirmations', (SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM shop_payment_confirmations c),
                    'evidence', (SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) FROM shop_payment_evidence e),
                    'receipts', (SELECT jsonb_agg(to_jsonb(r) ORDER BY r.id) FROM shop_mono_checkbox_receipts r)
                ) AS money,
                (SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) FROM orders o) AS orders,
                (SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id) FROM products p) AS products,
                (SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM inventory_movements m) AS movements,
                (SELECT jsonb_agg(to_jsonb(a) ORDER BY a.id) FROM shop_operation_audits a) AS audits
        """))).mappings().one()


async def test_populated_0152_upgrade_and_forward_recovery(database, template_database, checkout_dependencies, online):
    config = template_database[3]
    order_id, product_id, token = await prepared(database)
    peer_id, _ = await create_order(database, product_id)
    assert (await start(database, order_id, token)).status == "waiting"
    receipt_id = str(uuid4())
    online["status"] = "success"
    online["checks"] = [{"id": receipt_id, "status": "done", "type": "sale", "fiscalizationSource": "checkbox"}]
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 1, "blocked": 0}
    assert await stock(database, product_id, order_id) == (4, 2, OrderStatus.paid, 0)
    assert await stock(database, product_id, peer_id) == (4, 2, OrderStatus.pending, 0)
    # Seed with the running model's schema before rehearsing the older migration boundary.
    await asyncio.to_thread(migration.downgrade, config, PREVIOUS)
    before = dict(await snapshot(database))
    assert before.pop("revision") == PREVIOUS
    assert len(before["money"]["payments"]) == 1
    assert [receipt["receipt_id"] for receipt in before["money"]["receipts"]] == [receipt_id]
    provider_calls = list(online["calls"])

    script = ScriptDirectory.from_config(config)
    assert script.get_revision(ARCHIVE).down_revision == PREVIOUS
    await asyncio.to_thread(migration.upgrade, config, "head")
    upgraded = dict(await snapshot(database))
    assert upgraded.pop("revision") == script.get_current_head()
    assert upgraded == before

    current = await command(database, order_id)
    key = str(uuid4())
    result = await command(database, order_id, current.revision, key=key)
    assert result.archived and result.order_status == OrderStatus.cancelled
    assert result.payment_status == "simulated_paid"
    assert result.fiscal_status == "simulated_mono_checkbox_external_accepted"
    assert result.released_reservation_quantity == 1
    archived = await snapshot(database)
    assert archived["money"] == before["money"]
    assert archived["movements"][:-1] == before["movements"]
    assert archived["audits"][:-1] == before["audits"]
    assert archived["audits"][-1]["operation"] == "owner_cancel_order"

    with pytest.raises(RuntimeError, match="Audited owner order archives must be preserved"):
        await asyncio.to_thread(migration.downgrade, config, PREVIOUS)

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(require_current_schema, script)
        restarted = async_sessionmaker(engine, expire_on_commit=False)
        assert await command(restarted, order_id) == result
        assert await command(restarted, order_id, current.revision, key=key) == result
        assert await snapshot(restarted) == archived
        assert await stock(restarted, product_id, order_id) == (4, 1, OrderStatus.cancelled, 1)
        assert await stock(restarted, product_id, peer_id) == (4, 1, OrderStatus.pending, 0)
    finally:
        await engine.dispose()
    assert online["calls"] == provider_calls
