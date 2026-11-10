"""Close a single expired Mono POS reservation on isolated PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient, ReadError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes.master_mono import backoffice_router
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.master_mono import MasterMonoBinding, MasterMonoClosedConflict, MasterMonoCreateRequest, MasterMonoExpiredClosure
from app.models.order import Order, OrderPayment, OrderStatus
from app.models.product import Product
from app.schemas.master_mono import MasterMonoCommand, MasterMonoRecover
from app.schemas.order import POSOrderCreate
from app.services import local_sales, master_mono, master_mono_close, pos
from app.services.monobank_provider import MonobankGateway
from app.services.order_documents import digest
from tests.test_inventory_integration import checkout_dependencies
from tests.test_master_mono_integration import invoke, mock_provider, prepared, receipt
from tests.test_shop_fiscal_integration import database, template_database

pytestmark = pytest.mark.anyio


def command(revision):
    return MasterMonoCommand(expected_revision=revision)


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def unknown_sale(database, checkout_dependencies, mock_provider):
    mock_provider.update(page_url=None)
    product_id, order_id, state = await prepared(database, mock_provider)
    assert state.status == "creation_unknown" and state.invoice_id is None
    mock_provider.update(status="expired", final_amount=None)
    return product_id, order_id, state.revision


async def assert_stock(database, product_id, order_id, *, closed, reserved=1):
    async with database() as session:
        product = await session.get(Product, product_id)
        order = await session.get(Order, order_id)
        closure = await session.get(MasterMonoExpiredClosure, order_id)
        assert product.stock_quantity == 5 and product.reserved_quantity == reserved
        assert order.status == (OrderStatus.cancelled if closed else OrderStatus.pending)
        assert (closure is not None) is closed
        assert await session.scalar(select(func.count()).select_from(MasterMonoCreateRequest)) == 1
        moves = (await session.scalars(select(InventoryMovement).where(
            InventoryMovement.order_id == order_id,
            InventoryMovement.movement_type == InventoryMovementType.reservation_release,
        ))).all()
        assert len(moves) == int(closed)
        if closed:
            assert closure.actor_id == 1 and closure.proof["status"] == "expired"
            assert closure.bank_evidence_id == closure.proof["proof_hash"]
            assert closure.fiscal_proof["checks"] == []
            assert moves[0].on_hand_delta == 0 and moves[0].reserved_delta == -1


async def test_close_endpoint_replays_and_releases_only_its_own_unit(database, unknown_sale, mock_provider, monkeypatch):
    product_id, order_id, revision = unknown_sale
    async with database() as session:
        unrelated = await local_sales.prepare(session, await session.get(AdminUser, 3),
            POSOrderCreate(items=[{"product_id": product_id, "quantity": 1}], simulation=False), str(uuid4()))
    app = FastAPI()
    app.include_router(backoffice_router)
    async def db():
        async with database() as session:
            yield session
    async def actor():
        async with database() as session:
            return await session.get(AdminUser, 1)
    app.dependency_overrides.update({get_db_session: db, get_current_admin_user: actor})
    inspect = MonobankGateway.inspect_invoice
    reads = 0
    async def first_inspection_only(self, invoice):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise AssertionError("Committed replay must work while the provider is unavailable")
        return await inspect(self, invoice)
    monkeypatch.setattr(MonobankGateway, "inspect_invoice", first_inspection_only)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated.test") as client:
        url = f"/master-mono/orders/{order_id}/close-expired-unpaid"
        headers = {"Idempotency-Key": str(uuid4())}
        payload = command(revision).model_dump()
        async def lose_response():
            response = await client.post(url, headers=headers, json=payload)
            assert response.status_code == 200
            raise ReadError("Synthetic response loss after the request committed")
        lost, duplicate = await asyncio.gather(lose_response(), client.post(url, headers=headers, json=payload), return_exceptions=True)
        assert isinstance(lost, ReadError) and duplicate.status_code == 200
        assert duplicate.json() == {"order_id": order_id, "revision": revision + 1, "status": "cancelled"}
        assert duplicate.headers["cache-control"] == "private, no-store"
        calls = list(mock_provider["calls"])
        replay = await client.post(url, headers=headers, json=payload)
        assert replay.json() == duplicate.json() and mock_provider["calls"] == calls
        conflict = await client.post(url, headers={"Idempotency-Key": str(uuid4())}, json=payload)
        assert conflict.status_code == 409
        changed_proof = await client.post(url, headers=headers, json={**payload, "bank_confirmation_id": "different-record"})
        assert changed_proof.status_code == 422
        forged = await client.post(url, headers=headers, json={**payload, "proof": {"status": "expired"}})
        assert forged.status_code == 422
    assert reads == 1
    await assert_stock(database, product_id, order_id, closed=True, reserved=1)
    state = await invoke(database, master_mono.read, order_id)
    assert state.status == "cancelled" and not state.enabled and state.payment_url is None
    async with database() as session:
        assert (await session.get(Order, unrelated.id)).status == OrderStatus.pending
        projection = await pos.project(session, await local_sales.load(session, order_id))
        assert projection["order_status"] == "cancelled" and projection["payment"]["status"] == "expired"
        assert projection["receipt"]["status"] == "not_requested" and projection["handover"]["status"] == "not_ready"
        assert projection["actions"]["can_start_new"]
        assert not any(allowed for action, allowed in projection["actions"].items() if action != "can_start_new")
        with pytest.raises(DBAPIError, match="Closed POS inventory history is immutable"):
            await session.execute(text("DELETE FROM inventory_movements WHERE order_id=:id"), {"id": order_id})
        await session.rollback()
    assert mock_provider["post_calls"] == 1


async def test_concurrent_close_different_keys_apply_one_release(database, unknown_sale, mock_provider):
    product_id, order_id, revision = unknown_sale
    results = await asyncio.gather(*[
        invoke(database, master_mono_close.close, order_id, command(revision), str(uuid4()), actor_id=1)
        for _ in range(2)
    ], return_exceptions=True)
    conflicts = [result for result in results if isinstance(result, HTTPException)]
    assert len(conflicts) == 1 and conflicts[0].status_code == 409
    assert next(result for result in results if result not in conflicts).status == "cancelled"
    await assert_stock(database, product_id, order_id, closed=True, reserved=0)
    assert mock_provider["post_calls"] == 1


@pytest.mark.parametrize("case", ["active", "paid", "cancelled", "receipt", "wrong_amount", "wrong_reference", "wrong_profile", "wrong_time", "timeout"])
async def test_close_rejects_ambiguous_or_foreign_evidence(database, unknown_sale, mock_provider, monkeypatch, case):
    product_id, order_id, revision = unknown_sale
    mock_provider.update({
        "active": {"status": "created"},
        "paid": {"final_amount": mock_provider["amount"]},
        "cancelled": {"cancellations": [{"status": "success"}]},
        "receipt": {"checks": [receipt()]},
        "wrong_amount": {"amount": mock_provider["amount"] + 1},
        "wrong_reference": {"reference": "synthetic-foreign-dispatch"},
        "wrong_time": {"created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        "wrong_profile": {},
        "timeout": {"status_timeout": True},
    }[case])
    if case == "wrong_profile":
        inspect = MonobankGateway.inspect_invoice
        async def different_profile(self, invoice):
            proof = await inspect(self, invoice)
            proof["profile_id"] = "different-profile"
            proof["proof_hash"] = digest({key: value for key, value in proof.items() if key != "proof_hash"})
            return proof
        monkeypatch.setattr(MonobankGateway, "inspect_invoice", different_profile)
    with pytest.raises(HTTPException) as error:
        await invoke(database, master_mono_close.close, order_id, command(revision), str(uuid4()), actor_id=1)
    assert error.value.status_code == (503 if case == "timeout" else 409)
    await assert_stock(database, product_id, order_id, closed=False)
    assert mock_provider["post_calls"] == 1


async def test_close_rejects_ordinary_cancel_master_and_stale_revision(database, unknown_sale, mock_provider):
    product_id, order_id, revision = unknown_sale
    calls = list(mock_provider["calls"])
    with pytest.raises(HTTPException) as forbidden:
        await invoke(database, master_mono_close.close, order_id, command(revision), str(uuid4()))
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as stale:
        await invoke(database, master_mono_close.close, order_id, command(revision + 1), str(uuid4()), actor_id=1)
    assert stale.value.detail["code"] == "revision_conflict"
    async with database() as session:
        with pytest.raises(DBAPIError):
            await local_sales.OrderService().update_status(session, order_id, OrderStatus.cancelled, admin_user_id=1)
        request_id = await session.scalar(select(MasterMonoCreateRequest.id).where(MasterMonoCreateRequest.order_id == order_id))
        now = datetime.now(UTC)
        session.add(MasterMonoExpiredClosure(order_id=order_id, request_id=request_id, actor_id=2,
            operation_key="0" * 64, bank_evidence_id="0" * 64, expected_revision=revision,
            proof={}, fiscal_proof={}, fiscal_requested_at=now, created_at=now))
        with pytest.raises(DBAPIError, match="original unbound single-unit dispatch"):
            await session.flush()
        await session.rollback()
    await assert_stock(database, product_id, order_id, closed=False)
    assert mock_provider["calls"] == calls


async def test_close_rolls_back_if_stock_transition_is_missing(database, unknown_sale, monkeypatch):
    product_id, order_id, revision = unknown_sale
    original = local_sales.OrderService.update_status
    async def skip_transition(self, session, identifier, *args, **kwargs):
        return await session.get(Order, identifier)
    monkeypatch.setattr(local_sales.OrderService, "update_status", skip_transition)
    key = str(uuid4())
    with pytest.raises(DBAPIError, match="requires its atomic cancellation"):
        await invoke(database, master_mono_close.close, order_id, command(revision), key, actor_id=1)
    await assert_stock(database, product_id, order_id, closed=False)
    monkeypatch.setattr(local_sales.OrderService, "update_status", original)
    await invoke(database, master_mono_close.close, order_id, command(revision), key, actor_id=1)
    await assert_stock(database, product_id, order_id, closed=True, reserved=0)


async def test_close_rechecks_after_competing_paid_recovery(database, unknown_sale, mock_provider, monkeypatch):
    product_id, order_id, revision = unknown_sale
    inspect = MonobankGateway.inspect_invoice
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_inspect(self, invoice):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await inspect(self, invoice)

    monkeypatch.setattr(MonobankGateway, "inspect_invoice", waiting_inspect)
    mock_provider.update(status="success", final_amount=mock_provider["amount"])
    recovery = asyncio.create_task(invoke(database, master_mono.recover, order_id,
        MasterMonoRecover(expected_revision=revision, invoice_id=mock_provider["invoice_id"]), str(uuid4()), actor_id=1))
    await asyncio.wait_for(entered.wait(), timeout=5)
    close = asyncio.create_task(invoke(database, master_mono_close.close, order_id, command(revision), str(uuid4()), actor_id=1))
    release.set()
    await recovery
    with pytest.raises(HTTPException) as conflict:
        await close
    assert conflict.value.detail["code"] == "revision_conflict"
    await assert_stock(database, product_id, order_id, closed=False)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MasterMonoBinding)) == 1


async def test_recovery_cannot_read_or_bind_after_competing_close(database, unknown_sale, mock_provider, monkeypatch):
    product_id, order_id, revision = unknown_sale
    inspect = MonobankGateway.inspect_invoice
    entered, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def waiting_inspect(self, invoice):
        nonlocal reads
        reads += 1
        proof = await inspect(self, invoice)
        entered.set()
        await release.wait()
        return proof

    monkeypatch.setattr(MonobankGateway, "inspect_invoice", waiting_inspect)
    close = asyncio.create_task(invoke(database, master_mono_close.close, order_id, command(revision), str(uuid4()), actor_id=1))
    await asyncio.wait_for(entered.wait(), timeout=5)
    recovery = asyncio.create_task(invoke(database, master_mono.recover, order_id,
        MasterMonoRecover(expected_revision=revision, invoice_id=mock_provider["invoice_id"]), str(uuid4()), actor_id=1))
    release.set()
    await close
    with pytest.raises(HTTPException) as conflict:
        await recovery
    assert conflict.value.detail["code"] == "master_mono_sale_closed" and reads == 1
    await assert_stock(database, product_id, order_id, closed=True, reserved=0)


async def test_unknown_attempt_without_verified_candidate_keeps_reservation(database, checkout_dependencies, mock_provider):
    mock_provider["create_error"] = 500
    product_id, order_id, state = await prepared(database, mock_provider)
    calls = list(mock_provider["calls"])
    with pytest.raises(HTTPException) as conflict:
        await invoke(database, master_mono_close.close, order_id, command(state.revision), str(uuid4()), actor_id=1)
    assert conflict.value.detail["code"] == "master_mono_close_candidate_required"
    assert mock_provider["calls"] == calls
    await assert_stock(database, product_id, order_id, closed=False)


@pytest.mark.parametrize("case", ["paid_without_fiscal", "receipt_without_bank"])
async def test_late_contradiction_quarantines_even_if_other_read_fails(
    database, unknown_sale, mock_provider, monkeypatch, case,
):
    product_id, order_id, revision = unknown_sale
    close_key = str(uuid4())
    closed = await invoke(database, master_mono_close.close, order_id, command(revision), close_key, actor_id=1)
    recheck_key = str(uuid4())
    with pytest.raises(HTTPException) as denied:
        await invoke(database, master_mono_close.recheck, order_id, command(closed.revision), recheck_key)
    assert denied.value.status_code == 403
    if case == "paid_without_fiscal":
        mock_provider.update(status="success", final_amount=mock_provider["amount"])
        async def fiscal_offline(*args):
            from app.services.monobank_provider import MonobankError
            raise MonobankError("monobank_unavailable")
        monkeypatch.setattr(MonobankGateway, "get_fiscal_checks", fiscal_offline)
    else:
        mock_provider.update(status_timeout=True, checks=[receipt()])
    results = await asyncio.gather(*[
        invoke(database, master_mono_close.recheck, order_id, command(closed.revision), recheck_key, actor_id=1)
        for _ in range(2)
    ])
    assert results[0] == results[1] and results[0].status == "reconciliation_required"
    assert await invoke(database, master_mono_close.close, order_id, command(revision), close_key, actor_id=1) == closed
    state = await invoke(database, master_mono.read, order_id)
    assert state.status == "reconciliation_required" and not state.can_handover
    async with database() as session:
        rows = (await session.scalars(select(MasterMonoClosedConflict))).all()
        assert len(rows) == 1 and rows[0].actor_id == 1
        if case == "paid_without_fiscal":
            assert rows[0].proof["status"] == "success" and rows[0].proof["final_amount_minor"] == mock_provider["amount"]
            assert rows[0].fiscal_proof is None
        else:
            assert rows[0].proof is None and len(rows[0].fiscal_proof["checks"]) == 1
            assert set(rows[0].fiscal_proof["checks"][0]) == {"id", "status", "type", "fiscalizationSource"}
        assert not await session.scalar(select(func.count()).select_from(OrderPayment))
        assert not await session.scalar(select(func.count()).select_from(MasterMonoBinding))
        order = await local_sales.load(session, order_id)
        projection = await pos.project(session, order)
        assert projection["status"] == "requires_review" and projection["payment"]["status"] == "unknown"
        assert "master_mono_closed_conflict" in projection["blocking_reasons"]
        assert not any(projection["actions"].values())
        with pytest.raises(DBAPIError):
            await session.execute(text("DELETE FROM master_mono_closed_conflicts WHERE order_id=:id"), {"id": order_id})
        await session.rollback()
    assert mock_provider["post_calls"] == 1
    await assert_stock(database, product_id, order_id, closed=True, reserved=0)


async def test_late_clean_or_unverified_recheck_does_not_create_conflict(database, unknown_sale, mock_provider):
    product_id, order_id, revision = unknown_sale
    closed = await invoke(database, master_mono_close.close, order_id, command(revision), str(uuid4()), actor_id=1)
    observed = await invoke(database, master_mono_close.recheck, order_id, command(closed.revision), str(uuid4()), actor_id=1)
    assert observed.status == "cancelled"
    mock_provider.update(status="success", final_amount=mock_provider["amount"], reference="foreign")
    with pytest.raises(HTTPException) as error:
        await invoke(database, master_mono_close.recheck, order_id, command(closed.revision), str(uuid4()), actor_id=1)
    assert error.value.status_code == 503
    async with database() as session:
        assert not await session.scalar(select(func.count()).select_from(MasterMonoClosedConflict))
    await assert_stock(database, product_id, order_id, closed=True, reserved=0)


async def test_database_requires_late_contradiction_and_preserves_closure_on_downgrade(
    database, template_database, unknown_sale, mock_provider,
):
    from alembic import command as migration
    from alembic.script import ScriptDirectory

    config = template_database[3]
    async with database() as session:
        starting_revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert starting_revision == ScriptDirectory.from_config(config).get_current_head()

    product_id, order_id, revision = unknown_sale
    await invoke(database, master_mono_close.close, order_id, command(revision), str(uuid4()), actor_id=1)
    provider = master_mono.gateway()
    requested_at = datetime.now(UTC)
    proof = await provider.inspect_invoice(mock_provider["invoice_id"])
    fiscal = await provider.get_fiscal_checks(mock_provider["invoice_id"])
    async with database() as session:
        session.add(MasterMonoClosedConflict(
            operation_key="f" * 64, order_id=order_id, actor_id=1, proof=proof,
            fiscal_proof=fiscal, requested_at=requested_at, created_at=datetime.now(UTC),
        ))
        with pytest.raises(DBAPIError, match="Late POS evidence must contradict the closure"):
            await session.flush()
        await session.rollback()

    with pytest.raises(RuntimeError, match="Audited expired POS closures must be preserved"):
        await asyncio.to_thread(migration.downgrade, config, "0149_master_mono_opaque_route")
    async with database() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == starting_revision
        assert not await session.scalar(select(func.count()).select_from(MasterMonoClosedConflict))
    await assert_stock(database, product_id, order_id, closed=True, reserved=0)
