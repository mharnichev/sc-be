"""Durable Shop failure diagnostics with the real PostgreSQL schema and fake Mono."""

import asyncio

import pytest
from fastapi import HTTPException

from app.models.order import Order, OrderStatus
from app.models.shop_sales import ShopOperationAudit
from app.schemas.order import OrderSummaryResponse
from app.services import shop_online_payment as payment
from app.services.monobank_provider import MonobankError
from tests.test_shop_fiscal_integration import (
    anyio_backend, checkout_dependencies, database, template_database,
)
from tests.test_shop_online_payment_integration import online, prepared, start

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "failure,code,missing,review,posts",
    [
        ("readiness", "merchant_readiness_failed", ["merchant_readiness", "invoice_id"], False, 0),
        ("lost_reply", "invoice_result_unconfirmed", ["invoice_id", "bank_dispatch_result"], True, 1),
        ("old_record", "invoice_id_missing", ["invoice_id", "bank_dispatch_result"], True, 1),
        ("invalid_marker", "invoice_id_missing", ["invoice_id", "bank_dispatch_result"], True, 1),
    ],
)
async def test_missing_invoice_marks_order_payment_failed_with_safe_details(
    database, checkout_dependencies, online, monkeypatch, failure, code, missing, review, posts,
):
    order_id, _, token = await prepared(database)
    if failure == "readiness":
        def fail_readiness():
            raise MonobankError("monobank_unavailable")

        online["readiness_hook"] = fail_readiness
    else:
        online["unknown"] = True
    if failure in {"old_record", "invalid_marker"}:
        async def old_behavior(*args):
            pass

        monkeypatch.setattr(payment, "record_start_failure", old_behavior)
    async with database() as session:
        if failure in {"old_record", "invalid_marker"}:
            add = session.add

            def add_legacy(obj):
                if isinstance(obj, ShopOperationAudit) and obj.operation == "online_dispatch_prepared":
                    if failure == "old_record":
                        return
                    assert obj.facts["request_id"] == 1
                    obj.facts = {**obj.facts, "request_id": True}
                add(obj)

            monkeypatch.setattr(session, "add", add_legacy)
        result = await payment.start(session, order_id, None, token, "synthetic-failure-test-idempotency-key")
    expected = {"code": code, "missing": missing, "reconciliation_required": review}
    assert result.payment_status == "failed"
    assert result.payment_failure.model_dump() == expected
    assert result.page_url is None and not result.actions.start and not result.actions.continue_payment
    assert online["posts"] == posts

    async with database() as session:
        reopened = await payment.read(session, order_id, None, token)
        order = await payment.sales.load_order(session, order_id)
        listing = OrderSummaryResponse.model_validate(order)
    assert reopened.payment_failure.model_dump() == expected
    assert listing.payment_status == "failed" and listing.payment_failure.model_dump() == expected
    assert order.status == OrderStatus.pending
    assert (await start(database, order_id, token)).payment_failure.model_dump() == expected
    assert online["posts"] == posts


async def test_unstarted_and_bound_invoice_are_not_failed(database, checkout_dependencies, online):
    order_id, _, token = await prepared(database)
    async with database() as session:
        order = await session.get(Order, order_id)
        assert order.payment_status == "unpaid" and order.payment_failure is None
    result = await start(database, order_id, token)
    assert result.status == "waiting" and result.payment_failure is None
    assert online["posts"] == 1



async def test_concurrent_read_does_not_mark_in_flight_request_failed(
    database, checkout_dependencies, online, monkeypatch,
):
    order_id, _, token = await prepared(database)
    provider = payment.gateway()
    readiness = provider.readiness
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed_readiness():
        started.set()
        await finish.wait()
        return await readiness()

    monkeypatch.setattr(provider, "readiness", delayed_readiness)
    monkeypatch.setattr(payment, "gateway", lambda: provider)
    task = asyncio.create_task(start(database, order_id, token))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with database() as session:
            state = await payment.read(session, order_id, None, token)
        assert state.payment_failure is None and state.page_url is None
    finally:
        finish.set()
        result = await task
    assert result.status == "waiting" and online["posts"] == 1



async def test_failure_after_verified_bank_reply_names_the_missing_binding(
    database, checkout_dependencies, online, monkeypatch,
):
    order_id, _, token = await prepared(database)
    guard = payment.guard
    rejected = False

    async def fail_final_check(session, order, *, create=False):
        nonlocal rejected
        if online["posts"] and not rejected:
            rejected = True
            raise HTTPException(503, "synthetic final guard failure")
        return await guard(session, order, create=create)

    monkeypatch.setattr(payment, "guard", fail_final_check)
    result = await start(database, order_id, token)
    assert result.payment_failure.model_dump() == {
        "code": "invoice_binding_unconfirmed", "missing": ["verified_invoice_binding"],
        "reconciliation_required": True,
    }
    assert result.page_url is None and online["posts"] == 1
    assert (await start(database, order_id, token)).payment_failure == result.payment_failure
    assert online["posts"] == 1
