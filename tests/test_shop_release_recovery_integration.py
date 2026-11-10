"""The Shop recovery state stays usable with 0152 and immutable payment history."""
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models.product import Product
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_online_payment import OnlinePaymentCancellationCommand
from app.services import mono_checkbox_poll, shop_online_cancellation, shop_online_payment
from tests.test_shop_fiscal_integration import anyio_backend, checkout_dependencies, database, template_database
from tests.test_shop_online_admin_cancel_integration import ADMIN
from tests.test_shop_online_payment_integration import online, prepared
from tests.test_shop_payment_resume_integration import create_order

pytestmark = pytest.mark.anyio


async def test_populated_recovery_keeps_links_reserves_and_audits(database, checkout_dependencies, online, monkeypatch):
    paid, product_id, paid_token = await prepared(database)

    async def begin(order_id, token, label, *, v3=False):
        online["invoice_id"] = f"invoice{label}"
        online["page_url"] = f"https://pay.mbnk.biz/widget/{label}"
        async with database() as session:
            if v3:
                add = session.add

                def add_legacy_v3(obj):
                    if isinstance(obj, ShopOperationAudit):
                        if obj.operation in {"online_dispatch_prepared", "online_dispatch_started"}:
                            return
                        if obj.operation == "online_presentation_selected":
                            obj.facts = {**obj.facts, "schema_version": 3}
                    add(obj)

                monkeypatch.setattr(session, "add", add_legacy_v3)
            return await shop_online_payment.start(session, order_id, None, token, str(uuid4()), presentation="iframe")

    await begin(paid, paid_token, "paid")
    online["status"] = "success"
    online["checks"] = [{"id": str(uuid4()), "status": "done", "type": "sale", "fiscalizationSource": "checkbox"}]
    assert await mono_checkbox_poll.poll_once(database) == {"checked": 1, "blocked": 0}
    online.update(status="processing", checks=[])

    older, older_token = await create_order(database, product_id)
    fresh, fresh_token = await create_order(database, product_id)
    failed, failed_token = await create_order(database, product_id)
    cancelled, _, _ = await prepared(database)
    assert (await begin(older, older_token, "old", v3=True)).embed_url.endswith("/old")
    assert (await begin(fresh, fresh_token, "new")).embed_url.endswith("/new")
    online["unknown"] = True
    assert (await begin(failed, failed_token, "failed")).payment_status == "failed"
    online["unknown"] = False

    async with database() as session:
        state = await shop_online_cancellation.read(session, ADMIN, cancelled)
        done = await shop_online_cancellation.cancel(session, ADMIN, cancelled,
            OnlinePaymentCancellationCommand(expected_revision=state.revision), str(uuid4()))
        assert done.status == "cancelled"

    async def snapshot():
        async with database() as session:
            values = (await session.execute(text("""
                SELECT (SELECT version_num FROM alembic_version),
                    (SELECT count(*) FROM shop_online_payment_requests),
                    (SELECT count(*) FROM order_payments),
                    (SELECT count(*) FROM shop_mono_checkbox_receipts),
                    (SELECT count(*) FROM inventory_movements WHERE movement_type::text='reservation_release'),
                    (SELECT jsonb_agg(to_jsonb(a) ORDER BY a.id) FROM shop_operation_audits a),
                    (SELECT jsonb_agg(to_jsonb(m) ORDER BY m.id) FROM inventory_movements m)
            """))).one()
            product = await session.get(Product, product_id)
            return values, product.stock_quantity, product.reserved_quantity

    before = await snapshot()
    assert before[0][1:5] == (4, 1, 1, 1)
    assert before[1:] == (4, 4)
    calls = list(online["calls"])

    async with database() as session:
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(text("""
                UPDATE shop_operation_audits SET facts=facts WHERE operation='online_cancel_undispatched'
            """))
        await session.rollback()

    for identifier, token, label in [(older, older_token, "old"), (fresh, fresh_token, "new")]:
        async with database() as session:
            current = await shop_online_payment.read(session, identifier, None, token)
            assert current.status == "waiting" and current.embed_url.endswith(f"/{label}")
            replay = await shop_online_payment.start(session, identifier, None, token, str(uuid4()), presentation="iframe")
            assert replay == current
    async with database() as session:
        accepted = await shop_online_payment.read(session, paid, None, paid_token)
        failure = await shop_online_payment.read(session, failed, None, failed_token)
        assert accepted.status == "accepted" and accepted.paid and accepted.page_url is None
        assert failure.payment_status == "failed" and failure.payment_failure.reconciliation_required
        assert failure.page_url is None and failure.payment_failure.missing == ["invoice_id", "bank_dispatch_result"]
        assert (await shop_online_cancellation.read(session, ADMIN, cancelled)).status == "cancelled"
    assert online["calls"] == calls and online["posts"] == 4
    assert await snapshot() == before
