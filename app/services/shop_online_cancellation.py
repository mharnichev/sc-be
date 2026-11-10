"""Order-locked Shop cancellation that never contacts payment providers."""
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.models.order import OrderStatus
from app.models.shop_sales import ShopOperationAudit
from app.schemas.shop_online_payment import OnlinePaymentCancellationState
from app.services import shop_sales as sales
from app.services.order import OrderService
from app.services.order_documents import claim_intent, digest, operation_key


async def view(session, order):
    attempt = order.shop_online_payment_request
    binding = order.shop_payment_intent
    if order.channel != "shop" or order.simulation or (order.payment_method != "onlineCard" and not attempt):
        raise HTTPException(404, "Shop online order not found")
    if order.status == OrderStatus.cancelled:
        status, reason = "cancelled", "order_cancelled"
    elif not settings.shop_order_operations_enabled:
        status, reason = "unavailable", "shop_order_operations_disabled"
    elif await session.scalar(select(func.shop_online_cancel_eligible(order.id))):
        try:
            await OrderService.validate_owned_reservations(session, order, order.items)
        except HTTPException:
            status, reason = "reconciliation_required", "reservation_ownership_unconfirmed"
        else:
            status, reason = "available", "invoice_not_dispatched" if attempt else "payment_not_started"
    elif order.payments or order.shop_payment_confirmation:
        status, reason = "reconciliation_required", "paid_order_requires_resolution"
    elif order.status not in {OrderStatus.pending, OrderStatus.confirmed}:
        status, reason = "unavailable", "order_closed"
    elif (binding or attempt) and not await session.scalar(select(func.shop_online_dispatch_pending(order.id))):
        status, reason = "reconciliation_required", (
            "invoice_reconciliation_required" if binding and binding.invoice_id else "invoice_dispatch_unconfirmed"
        )
    else:
        status, reason = "unavailable", "order_has_financial_or_fulfillment_facts"
    return OnlinePaymentCancellationState(order_id=order.id, revision=order.revision,
        order_status=order.status.value, status=status, reason=reason)


async def read(session, actor, order_id):
    order = await sales.load_order(session, order_id)
    await sales.current_actor(session, actor, "complete", order)
    return await view(session, order)


async def cancel(session, actor, order_id, payload, raw_key):
    try:
        order = await sales.load_order(session, order_id, lock=True)
        operator = await sales.current_actor(session, actor, "complete", order)
        state = await view(session, order)
        key = operation_key(raw_key, f"shop-online-cancel:{operator.id}:{order.id}")
        intent = await claim_intent(session, key, digest(payload.model_dump()))
        if intent.response_json is not None:
            result = OnlinePaymentCancellationState.model_validate(intent.response_json)
        elif state.status == "cancelled":
            result = state
        else:
            if payload.expected_revision != order.revision:
                sales.fail("revision_conflict", "Reload the current cancellation state")
            if state.status != "available":
                raise HTTPException(409, detail={"code": state.reason,
                    "message": "Order cannot be cancelled in its current state",
                    "cancellation": state.model_dump()})
            attempt = order.shop_online_payment_request
            session.add(ShopOperationAudit(order_id=order.id, operator_id=operator.id,
                operation="online_cancel_undispatched", request_key=key,
                facts={"request_id": attempt.id if attempt else None, "expected_revision": order.revision},
                created_at=datetime.now(UTC)))
            await session.flush()
            order = await OrderService().update_status(session, order.id, OrderStatus.cancelled,
                admin_user_id=operator.id, commit=False, online_cancel_command=True)
            result = await view(session, order)
        intent.order_id = order_id
        intent.response_json = result.model_dump()
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise
