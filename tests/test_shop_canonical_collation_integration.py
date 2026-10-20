"""Production-locale parity and atomic immutable-history migration guards."""
import asyncio
from datetime import UTC, datetime
import json
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, insert, text
from sqlalchemy.orm import Session, defer, noload
from app.models.order import Order
from app.models.product import Product
from app.models.category import Category
from app.schemas.order import OrderCreate
from app.services.order import OrderService
from decimal import Decimal
from sqlalchemy.exc import DBAPIError

from app.models.shop_payments import ShopPaymentIntent
from app.models.shop_checkbox import ShopCheckboxRegister
from app.models.shop_mono_checkbox import ShopMonoCheckboxObservation
from app.services.order_documents import digest
from tests.checkbox_http_stub import sample_profile
from tests.test_shop_fiscal_integration import (
    template_database, database, checkout_dependencies, flags,
)

pytestmark = pytest.mark.anyio
HEAD = "0134_shop_canonical_collation"
PREVIOUS = "0133_master_mono_live"


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def historical_order_loads():
    # These tests deliberately run the application fixture against schema 0133.
    # That schema cannot eagerly load the later, unrelated online-dispatch table.
    def omit_later_relation(state):
        if state.is_select and any(description.get("entity") is Order
                for description in getattr(state.statement, "column_descriptions", [])):
            state.statement = state.statement.options(noload(Order.shop_online_payment_request))
        entities = {description.get("entity") for description in getattr(state.statement, "column_descriptions", [])}
        if state.is_select and Product in entities:
            state.statement = state.statement.options(defer(Product.name_en), defer(Product.product_type_en))
        if state.is_select and Category in entities:
            state.statement = state.statement.options(defer(Category.name_en))
    event.listen(Session, "do_orm_execute", omit_later_relation)
    try:
        yield
    finally:
        event.remove(Session, "do_orm_execute", omit_later_relation)


async def seed(database, *, quantity=1, price="100.00", kind="pickup", method="bank_transfer", total=None):
    # Core INSERT omits nullable English columns absent in the historical schema.
    # Order pricing and reservation still pass through the real OrderService.
    async with database() as session:
        product_id = await session.scalar(insert(Product.__table__).values(
            name='<script>alert("synthetic")</script>', slug=uuid4().hex, price=Decimal(price),
            stock_quantity=10, reserved_quantity=0, allow_backorder=False).returning(Product.id))
        await session.commit()
        order = await OrderService().create_order(session, OrderCreate(
            firstName="Synthetic", phoneNumber="+380500001234", shippingMethod="pickup",
            paymentMethod=method, items=[{"productId": product_id, "quantity": quantity}]))
        order.fulfillment_method = kind
        if total is not None:
            order.total_amount = Decimal(total)
            order.discount_amount = order.subtotal_amount - Decimal(total)
        await session.commit()
        return order.id, product_id


async def migrate(template_database, target, *, down=False):
    await asyncio.to_thread(command.downgrade if down else command.upgrade, template_database[3], target)


async def state(database):
    async with database() as session:
        return await session.scalar(text("SELECT version_num FROM alembic_version"))


async def intent(database, *, mixed=False, legacy_hash=False):
    order_id, _ = await seed(database)
    profile = dict(id="collation-fixture", merchant_id="synthetic", environment="test",
                   external_fiscalization_disabled=True)
    if mixed:
        profile["tenders"] = {"cashOnDelivery": 1, "cashless": 2}
    async with database() as session:
        stored_hash = await session.scalar(text("SELECT encode(sha256(convert_to("
            "shop_payment_canonical(CAST(:value AS jsonb)),'UTF8')),'hex')"), {"value": json.dumps(profile)})
        if mixed:
            assert (stored_hash != digest(profile)) == legacy_hash
        row = ShopPaymentIntent(id=str(uuid4()), order_id=order_id, mode="prepayment", reference=str(uuid4()),
            amount_minor=10000, profile_id=profile["id"], profile=profile,
            profile_hash=stored_hash if legacy_hash else digest(profile), authority="test", transport="mock_http",
            operator_id=1, created_at=datetime.now(UTC))
        session.add(row)
        await session.commit()
        return row.id


async def fact(database, identifier):
    async with database() as session:
        return await session.scalar(text("SELECT row_to_json(p)::text FROM shop_payment_intents p WHERE id=:id"),
                                    {"id": identifier})


@pytest.mark.parametrize("value", [
    {"cashOnDelivery": 1, "cashless": 2},
    {"a_b": 1, "ab": 2, "aB": 3, "A": 4},
    {"\u044f": "\u0442\u043e\u0432\u0430\u0440", "\u0410": 2, "\u0457": True, "\u0404": None},
    {"nested": [{"cashless": {"a_b": 1, "ab": 2}, "cashOnDelivery": [3, 2, 1]}]},
    {"": {"_": [], "Z": {}, "z": [None, False, 0, -3]}},
])
async def test_recursive_python_parity_under_production_locale(database, value):
    async with database() as session:
        locale = await session.execute(text("SELECT datcollate,datctype FROM pg_database WHERE datname=current_database()"))
        assert tuple(locale.one()) == ("en_US.utf8", "en_US.utf8")
        canonical = await session.scalar(text("SELECT shop_payment_canonical(CAST(:value AS jsonb))"),
                                         {"value": json.dumps(value)})
    assert canonical == json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def test_compatible_immutable_intent_preserved_through_both_directions(database, template_database, checkout_dependencies):
    await migrate(template_database, PREVIOUS, down=True)
    identifier = await intent(database, legacy_hash=True)
    before = await fact(database, identifier)
    for target, down in ((HEAD, False), (PREVIOUS, True), (HEAD, False)):
        await migrate(template_database, target, down=down)
        assert await state(database) == target
        assert await fact(database, identifier) == before


async def test_legacy_hash_incompatible_with_python_refuses_upgrade_atomically(database, template_database, checkout_dependencies):
    await migrate(template_database, PREVIOUS, down=True)
    identifier = await intent(database, mixed=True, legacy_hash=True)
    before = await fact(database, identifier)
    with pytest.raises(RuntimeError, match="history incompatible: shop_payment_intents"):
        await migrate(template_database, HEAD)
    assert await state(database) == PREVIOUS
    assert await fact(database, identifier) == before
    async with database() as session:
        assert await session.scalar(text("SELECT shop_payment_canonical(CAST(:value AS jsonb))"),
            {"value": '{"cashless":1,"cashOnDelivery":2}'}) == '{"cashless":1,"cashOnDelivery":2}'


async def test_new_mixed_case_immutable_fact_refuses_downgrade_atomically(database, template_database, checkout_dependencies):
    identifier = await intent(database, mixed=True)
    starting_head = await state(database)
    before = await fact(database, identifier)
    with pytest.raises(RuntimeError, match="history incompatible: shop_payment_intents"):
        await migrate(template_database, PREVIOUS, down=True)
    assert await state(database) == starting_head
    assert await fact(database, identifier) == before
    async with database() as session:
        assert await session.scalar(text("SELECT shop_payment_canonical(CAST(:value AS jsonb))"),
            {"value": '{"cashless":1,"cashOnDelivery":2}'}) == '{"cashOnDelivery":2,"cashless":1}'
    for sql in ("UPDATE shop_payment_intents SET profile_hash=:hash WHERE id=:id",
                "DELETE FROM shop_payment_intents WHERE id=:id"):
        async with database() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(sql), {"id": identifier, "hash": "0" * 64})
                await session.commit()
    assert await fact(database, identifier) == before


async def test_existing_checkbox_python_hash_preserved_on_upgrade_not_rewritten(database, template_database):
    await migrate(template_database, PREVIOUS, down=True)
    profile = sample_profile()
    async with database() as session:
        session.add(ShopCheckboxRegister(id=profile["id"], profile=profile, profile_hash=digest(profile),
                    authority="test", transport="mock_http", revision=1, shift_status="closed"))
        await session.commit()
        before = await session.scalar(text("SELECT row_to_json(r)::text FROM shop_checkbox_registers r"))
    await migrate(template_database, HEAD)
    async with database() as session:
        assert await session.scalar(text("SELECT row_to_json(r)::text FROM shop_checkbox_registers r")) == before
    with pytest.raises(RuntimeError, match="history incompatible: shop_checkbox_registers"):
        await migrate(template_database, PREVIOUS, down=True)
    assert await state(database) == HEAD


async def test_embedded_proof_hash_is_audited_not_only_outer_hash(database, template_database, checkout_dependencies):
    await migrate(template_database, PREVIOUS, down=True)
    order_id, _ = await seed(database)
    proof = {"payment": {"verified": True, "proof_hash": "0" * 64}, "fiscal": None}
    async with database() as session:
        session.add(ShopMonoCheckboxObservation(order_id=order_id, status="pending", proof=proof,
            proof_hash=digest(proof), operator_id=1, created_at=datetime.now(UTC)))
        await session.commit()
        before = await session.scalar(text("SELECT row_to_json(o)::text FROM shop_mono_checkbox_observations o"))
    with pytest.raises(RuntimeError, match="history incompatible: shop_mono_checkbox_observations"):
        await migrate(template_database, HEAD)
    assert await state(database) == PREVIOUS
    async with database() as session:
        assert await session.scalar(text("SELECT row_to_json(o)::text FROM shop_mono_checkbox_observations o")) == before


async def test_existing_np_case_observation_receipt_closure_preserved(database, template_database, checkout_dependencies, monkeypatch):
    from tests import test_shop_np_returns_integration as np
    await migrate(template_database, PREVIOUS, down=True)
    np_flags = np.flags.__wrapped__(monkeypatch)
    monkeypatch.setattr(np, "seed", seed)
    _, case, _ = await np.attach_case(database, np_flags, paid=False)
    case, _ = await np.receive(database, case)
    payload = np.dto.CloseCommand(expected_revision=case.revision, **np.assertion(), confirm=True,
                                 evidence="Synthetic migration preservation")
    result = await np.cb.call(database, np.service.command, case.id, payload, str(uuid4()), "close_unpaid")
    assert result.status == "closed_unpaid" and result.closure
    tables = ("shop_np_return_cases", "shop_np_return_observations", "shop_np_return_receipts", "shop_np_unpaid_closures")
    async def snapshots():
        async with database() as session:
            return {table: await session.scalar(text(f"SELECT jsonb_agg(to_jsonb(t) ORDER BY t.id)::text FROM {table} t"))
                    for table in tables}
    before = await snapshots()
    assert all(before.values())
    await migrate(template_database, HEAD)
    assert await state(database) == HEAD
    assert await snapshots() == before
