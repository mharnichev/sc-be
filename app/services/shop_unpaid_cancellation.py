"""Close a pinned Shop invoice before making any inventory changes."""

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.config import settings
from app.models.order import OrderStatus
from app.models.shop_sales import ShopOperationAudit
from app.services import shop_payments, shop_sales as sales
from app.services.monobank_provider import MonobankError, MonobankGateway
from app.services.order import OrderService
from app.services.order_documents import digest, minor
from app.services.shop_payment_resolutions import creation_candidate


async def cancel(session, actor, order_id, payload, *, bank_only=False):
    actor_id = actor.id
    try:
        while True:
            order = await sales.load_order(session, order_id, lock=True)
            operator = await sales.current_actor(session, SimpleNamespace(id=actor_id), "complete", order)
            if not operator.is_superuser and not operator.can_manage_fiscal_settings:
                raise HTTPException(403, "Bank cancellation requires fiscal settings permission")
            if not settings.shop_order_operations_enabled:
                sales.fail("shop_order_operations_disabled", "Shop order operations are closed", 503)
            binding = order.shop_payment_intent
            request = order.shop_online_payment_request
            if not binding or not request or request.intent_id != binding.id or binding.amount_minor != minor(order.total_amount):
                sales.fail("invoice_reconciliation_required", "An exact Shop invoice binding is required")
            candidate = await creation_candidate(session, order)
            invoice_id = binding.invoice_id or candidate
            if not invoice_id or (candidate and candidate != invoice_id):
                sales.fail("invoice_reconciliation_required", "The invoice identity needs review")

            identity = {
                "intent_id": binding.id, "request_id": request.id, "invoice_id": invoice_id,
                "reference": binding.reference, "amount_minor": binding.amount_minor,
            }
            dispatch_key = digest(["online_unpaid_dispatched", binding.id])
            removed_key = digest(["online_unpaid_removed", binding.id])
            removed = await session.scalar(select(ShopOperationAudit).where(
                ShopOperationAudit.order_id == order.id, ShopOperationAudit.request_key == removed_key,
                ShopOperationAudit.operation == "online_unpaid_removed",
            ))
            expired_key = digest(["online_unpaid_expired", binding.id])
            expired = await session.scalar(select(ShopOperationAudit).where(
                ShopOperationAudit.order_id == order.id, ShopOperationAudit.request_key == expired_key,
                ShopOperationAudit.operation == "online_unpaid_expired",
            ))
            if expired and any(expired.facts.get(field) != value for field, value in {
                **identity, "status": "expired", "currency_code": 980,
                "final_amount_minor": 0, "has_cancellations": False,
            }.items()):
                sales.fail("invoice_reconciliation_required", "Stored expiry proof does not match the order")
            if removed and any(
                removed.facts.get(field) != value
                for field, value in {**identity, "removal_http_status": 200}.items()
            ):
                sales.fail("invoice_reconciliation_required", "Stored bank proof does not match the order")

            if binding.authority == "test":
                await sales.require_test_database(session, "unpaid_removal_test_unsafe")
            closure = removed or expired
            invoice_status = "deactivated" if removed else "expired"
            if closure and order.status == OrderStatus.cancelled:
                await session.commit()
                return {"order_id": order_id, "invoice_status": invoice_status, "order_status": "cancelled"}
            if order.revision != payload.expected_revision:
                sales.fail("revision_conflict", "Reload the current order")
            if not await session.scalar(select(func.shop_mono_unpaid_eligible(order.id, bank_only))):
                sales.fail("invoice_reconciliation_required", "Current order facts require reconciliation")
            if bank_only and not order.owner_archive:
                sales.fail("invoice_reconciliation_required", "Bank-only cancellation requires an audited archive")
            if not bank_only:
                await OrderService.validate_owned_reservations(session, order, order.items)
            if closure:
                session.add(ShopOperationAudit(
                    order_id=order_id, operator_id=actor_id, operation="online_unpaid_finalize",
                    request_key=digest(["online_unpaid_finalize", binding.id, order.revision]),
                    facts={"removal_audit_id": closure.id, "expected_revision": order.revision},
                    created_at=datetime.now(UTC),
                ))
                await session.flush()
                await OrderService().update_status(
                    session, order.id, OrderStatus.cancelled, admin_user_id=operator.id,
                    commit=False, online_cancel_command=True,
                )
                await session.commit()
                return {"order_id": order_id, "invoice_status": invoice_status, "order_status": "cancelled"}

            previously_dispatched = await session.scalar(select(ShopOperationAudit.id).where(
                ShopOperationAudit.request_key == dispatch_key))

            gateway = MonobankGateway(create_capability="shop")
            shop_payments.pinned(binding, gateway)
            if not gateway.available:
                sales.fail("monobank_unavailable", "Pinned bank transport required", 503)
            if gateway.test_only:
                await sales.require_test_database(session, "unpaid_removal_test_unsafe")
            try:
                inspection = await gateway.inspect_invoice(invoice_id)
            except MonobankError:
                code = "invoice_removal_unconfirmed" if previously_dispatched else "invoice_removal_preflight_failed"
                sales.fail(code, "Bank status could not be verified")
            if (
                inspection["invoice_id"] != invoice_id
                or inspection["reference"] != identity["reference"]
                or inspection["amount_minor"] != identity["amount_minor"]
                or inspection["currency_code"] != 980
                or inspection["final_amount_minor"] not in (None, 0)
                or inspection["has_cancellations"]
            ):
                sales.fail("invoice_reconciliation_required", "Bank status does not match an unpaid Shop invoice")
            if inspection["status"] == "expired":
                session.add(ShopOperationAudit(
                    order_id=order_id, operator_id=actor_id, operation="online_unpaid_expired",
                    request_key=expired_key, facts={
                        **identity, "status": "expired", "currency_code": 980,
                        "final_amount_minor": 0, "has_cancellations": False,
                        "observed_at": inspection["observed_at"], "proof_hash": inspection["proof_hash"],
                    }, created_at=datetime.now(UTC),
                ))
                await session.flush()
                continue
            if previously_dispatched:
                sales.fail("invoice_removal_unconfirmed", "A bank request may already have been sent. Reconcile before retrying")
            if inspection["status"] != "created":
                sales.fail("invoice_removal_preflight_failed", "Only a created unpaid invoice can be removed")

            dispatched = False

            async def mark_dispatch():
                nonlocal dispatched
                session.add(ShopOperationAudit(
                    order_id=order_id, operator_id=actor_id, operation="online_unpaid_dispatched",
                    request_key=dispatch_key, facts=identity, created_at=datetime.now(UTC),
                ))
                await session.commit()
                dispatched = True

            try:
                proof = await gateway.deactivate_unpaid_shop_invoice(
                    invoice_id, reference=identity["reference"], amount_minor=identity["amount_minor"],
                    before_dispatch=mark_dispatch,
                )
            except MonobankError:
                if not dispatched:
                    sales.fail("invoice_removal_preflight_failed", "Bank preflight failed. No removal was sent")
                sales.fail("invoice_removal_unconfirmed", "Bank did not confirm deactivation. Reconcile before retrying")

            if proof.get("removal_http_status") != 200 or not proof.get("removed_at"):
                sales.fail("invoice_removal_unconfirmed", "Bank did not confirm deactivation. Reconcile before retrying")
            session.add(ShopOperationAudit(
                order_id=order_id, operator_id=actor_id, operation="online_unpaid_removed",
                request_key=removed_key,
                facts={**identity, "removal_http_status": 200, "removed_at": proof["removed_at"]},
                created_at=datetime.now(UTC),
            ))
            await session.commit()
    except Exception:
        await session.rollback()
        raise
