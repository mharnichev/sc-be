"""Payload identity and unique-key races against isolated opt-in PostgreSQL."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.models.admin_user import AdminUser
from app.models.inventory import InventoryCount, InventoryMovement, InventoryMovementType, InventoryReceipt
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.services.inventory import InventoryService
from tests.test_inventory_integration import anyio_backend, database  # noqa: F401


pytestmark = pytest.mark.anyio
CONFLICT_DETAIL = "Idempotency key is already in use for a different operation"
MANUAL_TYPES = (
    InventoryMovementType.opening_balance,
    InventoryMovementType.receipt,
    InventoryMovementType.customer_return,
    InventoryMovementType.write_off,
)


async def bounded(operation):
    return await asyncio.wait_for(operation, timeout=5)


async def seed_context(database):
    """All alternate payload IDs refer to valid, synthetic foreign-key rows."""
    async with database() as session:
        products = [Product(
            name="Synthetic idempotency product", slug=f"idempotency-{uuid4().hex}",
            price=Decimal("100.00"), stock_quantity=20, reserved_quantity=0,
        ) for _ in range(2)]
        session.add_all(products)
        session.add(AdminUser(
            id=2, email=f"idempotency-{uuid4().hex}@example.test", hashed_password="not-used",
            is_active=True, is_superuser=True,
        ))
        await bounded(session.flush())
        orders = []
        for product in products:
            order = Order(
                customer_name="Synthetic idempotency customer", customer_phone="+380500000001",
                total_amount=Decimal("100.00"),
            )
            order.items = [OrderItem(
                product_id=product.id, quantity=1, quantity_from_stock=1, quantity_to_order=0,
                price=Decimal("100.00"),
            )]
            session.add(order)
            orders.append(order)
        await bounded(session.commit())
        return {
            "products": [product.id for product in products],
            "orders": [order.id for order in orders],
            "items": [order.items[0].id for order in orders],
        }


def manual_payload(product_id, **overrides):
    payload = dict(
        movement_type=InventoryMovementType.receipt, product_id=product_id,
        quantity=2, reason="Stock operation", admin_user_id=1,
        idempotency_key="historical-manual-key", order_id=None, order_item_id=None,
    )
    payload.update(overrides)
    return payload


async def snapshot(database):
    async with database() as session:
        state = {}
        for model in (InventoryMovement, InventoryReceipt, InventoryCount):
            result = await bounded(session.execute(select(model.__table__).order_by(model.id)))
            state[model.__tablename__] = [tuple(row) for row in result.all()]
        result = await bounded(session.execute(
            select(Product.id, Product.stock_quantity, Product.reserved_quantity).order_by(Product.id)
        ))
        state["stock"] = [tuple(row) for row in result.all()]
        return state


async def create_document(service, session, kind, **payload):
    if kind == "receipt":
        return await bounded(service.create_receipt(session, **payload))
    return await bounded(service.create_count(session, **payload))


@pytest.mark.parametrize("movement_type", MANUAL_TYPES)
async def test_manual_identical_normalized_replay_keeps_one_stock_delta(database, movement_type):
    context = await seed_context(database)
    payload = manual_payload(context["products"][0], movement_type=movement_type, reason="  Stock operation \n")
    service = InventoryService()
    async with database() as session:
        original = await bounded(service.manual_operation(session, **payload))
        original_id = original.id
    before = await snapshot(database)
    async with database() as session:
        replayed = await bounded(service.manual_operation(session, **{**payload, "reason": "Stock operation"}))
        assert replayed.id == original_id
        assert replayed.reference_key == payload["idempotency_key"]
        assert replayed.reason == "Stock operation"
    assert await snapshot(database) == before
    delta = -2 if movement_type == InventoryMovementType.write_off else 2
    assert before["stock"] == [(context["products"][0], 20 + delta, 0), (context["products"][1], 20, 0)]
    assert len(before["inventory_movements"]) == 1


@pytest.mark.parametrize("field", [
    "product_id", "movement_type", "quantity", "reason", "admin_user_id", "order_id", "order_item_id",
])
async def test_manual_key_reuse_rejects_each_changed_operation_field(database, field):
    context = await seed_context(database)
    payload = manual_payload(
        context["products"][0], order_id=context["orders"][0], order_item_id=context["items"][0],
    )
    alternatives = dict(
        product_id=context["products"][1], movement_type=InventoryMovementType.write_off,
        quantity=3, reason="Different operation", admin_user_id=2,
        order_id=context["orders"][1], order_item_id=context["items"][1],
    )
    service = InventoryService()
    async with database() as session:
        await bounded(service.manual_operation(session, **payload))
    before = await snapshot(database)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await bounded(service.manual_operation(session, **{**payload, field: alternatives[field]}))
        assert (error.value.status_code, error.value.detail) == (409, CONFLICT_DETAIL)
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert await snapshot(database) == before


@pytest.mark.parametrize("on_hand_delta,reserved_delta", [(3, 0), (2, 1)])
async def test_manual_replay_rejects_matching_fields_with_incompatible_ledger_deltas(
    database, on_hand_delta, reserved_delta,
):
    context = await seed_context(database)
    payload = manual_payload(context["products"][0], idempotency_key="order:synthetic:internal-key")
    async with database() as session:
        product = await bounded(session.get(Product, payload["product_id"]))
        product.stock_quantity += on_hand_delta
        product.reserved_quantity += reserved_delta
        session.add(InventoryMovement(
            product_id=product.id, movement_type=payload["movement_type"], quantity=payload["quantity"],
            reason=payload["reason"], performed_by_user_id=payload["admin_user_id"],
            reference_key=payload["idempotency_key"], on_hand_delta=on_hand_delta,
            reserved_delta=reserved_delta,
        ))
        await bounded(session.commit())
    before = await snapshot(database)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await bounded(InventoryService().manual_operation(session, **payload))
        assert (error.value.status_code, error.value.detail) == (409, CONFLICT_DETAIL)
    assert await snapshot(database) == before


@pytest.mark.parametrize("kind", ["receipt", "count"])
async def test_document_identical_normalized_replay_preserves_original_creation_fields(database, kind):
    service = InventoryService()
    payload = dict(reason="  Stock operation \n", idempotency_key="historical-document-key")
    if kind == "receipt":
        payload["comment"] = " Keep this comment exactly \n"
    async with database() as session:
        original = await create_document(service, session, kind, **payload)
        original_id = original.id
    before = await snapshot(database)
    async with database() as session:
        replayed = await create_document(service, session, kind, **{**payload, "reason": "Stock operation"})
        assert replayed.id == original_id
        assert replayed.idempotency_key == payload["idempotency_key"]
        assert replayed.reason == "Stock operation"
        if kind == "receipt":
            assert replayed.comment == payload["comment"]
    assert await snapshot(database) == before


@pytest.mark.parametrize("kind,field,original_value,retry_value", [
    ("receipt", "reason", "Stock operation", "Different operation"),
    ("count", "reason", "Stock operation", "Different operation"),
    ("receipt", "comment", "Exact comment", "Different comment"),
    ("receipt", "comment", "Exact comment", " Exact comment "),
    ("receipt", "comment", None, ""),
    ("receipt", "comment", "", None),
])
async def test_document_key_reuse_rejects_changed_creation_payload(
    database, kind, field, original_value, retry_value,
):
    service = InventoryService()
    payload = dict(reason="Stock operation", idempotency_key="document-key")
    if kind == "receipt":
        payload["comment"] = "Exact comment"
    payload[field] = original_value
    async with database() as session:
        await create_document(service, session, kind, **payload)
    before = await snapshot(database)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await create_document(service, session, kind, **{**payload, field: retry_value})
        assert (error.value.status_code, error.value.detail) == (409, CONFLICT_DETAIL)
        assert await bounded(session.scalar(text("SELECT 1"))) == 1
    assert await snapshot(database) == before


async def test_manual_receipt_and_count_keep_independent_historical_key_namespaces(database):
    context = await seed_context(database)
    service = InventoryService()
    key = "unchanged-historical-key"
    payload = manual_payload(context["products"][0], idempotency_key=key)
    async with database() as session:
        movement = await bounded(service.manual_operation(session, **payload))
        receipt = await create_document(service, session, "receipt", reason="Receipt reason", comment=None, idempotency_key=key)
        count = await create_document(service, session, "count", reason="Count reason", idempotency_key=key)
        ids = (movement.id, receipt.id, count.id)
    before = await snapshot(database)
    async with database() as session:
        movement = await bounded(service.manual_operation(session, **payload))
        receipt = await create_document(service, session, "receipt", reason="Receipt reason", comment=None, idempotency_key=key)
        count = await create_document(service, session, "count", reason="Count reason", idempotency_key=key)
        assert (movement.id, receipt.id, count.id) == ids
        assert (movement.reference_key, receipt.idempotency_key, count.idempotency_key) == (key, key, key)
    assert await snapshot(database) == before


def synchronize_missing_lookups(monkeypatch, sessions, model):
    """Both real SELECTs miss before the winner commits; release the loser afterward."""
    ready = asyncio.Event()
    winner_committed = asyncio.Event()
    observations = []
    rollbacks = [0, 0]
    key_column = "reference_key" if model is InventoryMovement else "idempotency_key"

    for index, session in enumerate(sessions):
        execute = session.execute
        rollback = session.rollback

        def install(index, session, execute, rollback):
            intercepted = False

            async def execute_with_barrier(statement, *args, **kwargs):
                nonlocal intercepted
                result = await execute(statement, *args, **kwargs)
                descriptions = getattr(statement, "column_descriptions", ())
                if not intercepted and descriptions and descriptions[0].get("entity") is model:
                    criteria = getattr(statement, "whereclause", None)
                    if criteria is not None and key_column in str(criteria):
                        intercepted = True
                        frozen = result.freeze()
                        assert frozen().scalar_one_or_none() is None
                        observations.append(index)
                        if len(observations) == 2:
                            ready.set()
                        await bounded(ready.wait())
                        if index == 1:
                            await bounded(winner_committed.wait())
                        return frozen()
                return result

            async def record_rollback():
                rollbacks[index] += 1
                return await rollback()

            monkeypatch.setattr(session, "execute", execute_with_barrier)
            monkeypatch.setattr(session, "rollback", record_rollback)

        install(index, session, execute, rollback)

    commit = sessions[0].commit

    async def commit_winner():
        result = await commit()
        winner_committed.set()
        return result

    monkeypatch.setattr(sessions[0], "commit", commit_winner)
    return observations, rollbacks


@pytest.mark.parametrize("kind", ["manual", "receipt", "count"])
@pytest.mark.parametrize("identical", [True, False], ids=["identical", "mismatched"])
async def test_concurrent_missing_key_recovers_winner_and_rolls_back_loser(
    database, monkeypatch, kind, identical,
):
    context = await seed_context(database)
    service = InventoryService()
    model = {"manual": InventoryMovement, "receipt": InventoryReceipt, "count": InventoryCount}[kind]
    if kind == "manual":
        payload = manual_payload(context["products"][0], idempotency_key="concurrent-key")
        retry = {**payload, "quantity": 2 if identical else 3, "reason": " Stock operation "}
    else:
        payload = dict(reason="Stock operation", idempotency_key="concurrent-key")
        if kind == "receipt":
            payload["comment"] = "Exact comment"
        retry = {**payload, "reason": " Stock operation " if identical else "Different operation"}

    async with database() as winner_session, database() as loser_session:
        observations, rollbacks = synchronize_missing_lookups(
            monkeypatch, (winner_session, loser_session), model,
        )

        async def attempt(session, values):
            try:
                if kind == "manual":
                    return await bounded(service.manual_operation(session, **values))
                return await create_document(service, session, kind, **values)
            except HTTPException as error:
                return error

        winner, loser = await asyncio.wait_for(asyncio.gather(
            attempt(winner_session, payload), attempt(loser_session, retry),
        ), timeout=10)
        assert sorted(observations) == [0, 1]
        assert not isinstance(winner, HTTPException)
        if identical:
            assert not isinstance(loser, HTTPException)
            assert loser.id == winner.id
        else:
            assert isinstance(loser, HTTPException)
            assert (loser.status_code, loser.detail) == (409, CONFLICT_DETAIL)
        assert rollbacks == [0, 1]
        assert loser_session.is_active
        assert await bounded(loser_session.scalar(text("SELECT 1"))) == 1
        await bounded(loser_session.commit())

    state = await snapshot(database)
    assert len(state[model.__tablename__]) == 1
    expected_stock = 22 if kind == "manual" else 20
    assert state["stock"] == [(context["products"][0], expected_stock, 0), (context["products"][1], 20, 0)]
    if kind != "manual":
        assert state["inventory_movements"] == []
    async with database() as session:
        persisted = await bounded(session.get(model, winner.id))
        assert persisted.reason == "Stock operation"
        if kind == "manual":
            assert (persisted.quantity, persisted.on_hand_delta, persisted.reserved_delta) == (2, 2, 0)
        elif kind == "receipt":
            assert persisted.comment == "Exact comment"


@pytest.mark.parametrize("identical", [True, False], ids=["identical", "mismatched"])
async def test_concurrent_scarce_write_off_checks_winner_before_insufficient_stock(
    database, monkeypatch, identical,
):
    context = await seed_context(database)
    product_id = context["products"][0]
    async with database() as session:
        product = await bounded(session.get(Product, product_id))
        product.stock_quantity = 2
        await bounded(session.commit())

    payload = manual_payload(
        product_id, movement_type=InventoryMovementType.write_off, idempotency_key="scarce-write-off-key",
    )
    retry = {**payload, "quantity": 2 if identical else 3, "reason": " Stock operation "}
    service = InventoryService()
    async with database() as winner_session, database() as loser_session:
        observations, _rollbacks = synchronize_missing_lookups(
            monkeypatch, (winner_session, loser_session), InventoryMovement,
        )

        async def attempt(session, values):
            try:
                return await bounded(service.manual_operation(session, **values))
            except HTTPException as error:
                return error

        winner, loser = await asyncio.wait_for(asyncio.gather(
            attempt(winner_session, payload), attempt(loser_session, retry),
        ), timeout=10)
        assert sorted(observations) == [0, 1]
        assert not isinstance(winner, HTTPException)
        if identical:
            assert not isinstance(loser, HTTPException)
            assert loser.id == winner.id
        else:
            assert isinstance(loser, HTTPException)
            assert (loser.status_code, loser.detail) == (409, CONFLICT_DETAIL)
        assert loser_session.is_active
        assert await bounded(loser_session.scalar(text("SELECT 1"))) == 1
        await bounded(loser_session.commit())

    state = await snapshot(database)
    assert state["stock"] == [(product_id, 0, 0), (context["products"][1], 20, 0)]
    assert len(state["inventory_movements"]) == 1
    async with database() as session:
        persisted = await bounded(session.get(InventoryMovement, winner.id))
        assert (
            persisted.movement_type, persisted.quantity, persisted.on_hand_delta,
            persisted.reserved_delta, persisted.reason,
        ) == (InventoryMovementType.write_off, 2, -2, 0, "Stock operation")
