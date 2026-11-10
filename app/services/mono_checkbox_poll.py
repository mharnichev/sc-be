"""Bounded, opt-in reconciliation of existing invoices. Never creates bank invoices.

The scheduler uses the original operator's current permissions. Provider requests
are GET-only; existing ledgers own locking, provenance and idempotency.
"""
from app.core import error_reporting
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

from app.core.config import settings
from app.schemas.shop_mono_checkbox import RefreshReceiptCommand
from app.schemas.shop_payments import VerifyInvoiceCommand
from app.services import shop_payments as payments, shop_sales as sales, shop_mono_checkbox as direct, master_mono
from app.services import order_archive_payments

logger = logging.getLogger(__name__)


async def poll_once(session_factory):
    """One bounded provider batch, serialized across processes with a transaction lease."""
    if not settings.mono_checkbox_auto_enabled:
        return {"checked": 0, "blocked": 0}
    provider = payments.gateway()
    shop_available = provider.available and provider.external_checkbox and (
        provider.test_only and settings.app_env in {"local", "test"}
        or not provider.test_only and settings.app_env in {"production", "staging"}
        and settings.shop_online_payment_enabled and settings.shop_online_payment_link_confirmed)
    master_provider = master_mono.gateway()
    master_available = settings.master_mono_enabled and master_provider.available and master_provider.external_checkbox
    if not shop_available and not master_available:
        return {"checked": 0, "blocked": 0}
    checked = blocked = 0
    async with session_factory() as lease:
        if (shop_available and provider.test_only) or (master_available and master_provider.test_only):
            await sales.require_test_database(lease, "mono_auto_test_unsafe")
        if not await lease.scalar(text("SELECT pg_try_advisory_xact_lock(1396919372, 13)")):
            return {"checked": 0, "blocked": 0}
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=settings.mono_checkbox_poll_seconds)
        archive_cutoff = now - timedelta(seconds=max(settings.mono_checkbox_poll_seconds, 300))
        candidates = (await lease.execute(text("""
          WITH invoices AS (
            SELECT 'shop' AS kind,b.order_id,b.operator_id,b.created_at,o.status::text='cancelled' AS archived
            FROM shop_payment_intents b JOIN orders o ON o.id=b.order_id
            WHERE :shop_available AND b.mode='prepayment' AND b.invoice_id IS NOT NULL
              AND b.profile->>'external_fiscalization_disabled'='false'
              AND (o.status::text NOT IN ('completed','cancelled') OR (o.status::text='cancelled'
                AND EXISTS (SELECT 1 FROM shop_operation_audits a WHERE a.order_id=o.id AND a.operation='owner_cancel_order')
                AND NOT EXISTS (SELECT 1 FROM order_payments p WHERE p.order_id=o.id)))
            UNION ALL
            SELECT 'master',q.order_id,q.operator_id,COALESCE(b.created_at,q.created_at),o.status::text='cancelled'
            FROM master_mono_create_requests q JOIN orders o ON o.id=q.order_id
            LEFT JOIN master_mono_bindings b ON b.create_request_id=q.id
            WHERE :master_available AND NOT o.simulation
              AND (b.id IS NOT NULL AND o.status::text NOT IN ('completed','cancelled') OR (o.status::text='cancelled'
                AND EXISTS (SELECT 1 FROM shop_operation_audits a WHERE a.order_id=o.id AND a.operation='owner_cancel_order')
                AND NOT EXISTS (SELECT 1 FROM order_payments p WHERE p.order_id=o.id)
                AND NOT EXISTS (SELECT 1 FROM shop_operation_audits a WHERE a.order_id=o.id AND a.operation='owner_cancel_late_payment')
                AND (b.id IS NOT NULL OR EXISTS (SELECT 1 FROM master_mono_create_candidates c WHERE c.request_id=q.id)
                  OR EXISTS (SELECT 1 FROM shop_operation_audits a WHERE a.order_id=o.id AND a.operation='owner_cancel_pos_invoice'))))
          ) SELECT i.kind,i.order_id,i.operator_id FROM invoices i
          LEFT JOIN mono_checkbox_poll_state p ON p.kind=i.kind AND p.order_id=i.order_id
          WHERE p.last_attempt_at IS NULL OR p.last_attempt_at<=CASE WHEN i.archived
            THEN CAST(:archive_cutoff AS timestamptz) ELSE CAST(:cutoff AS timestamptz) END
          ORDER BY COALESCE(p.last_attempt_at,i.created_at),i.kind,i.order_id LIMIT :limit
        """), {"cutoff":cutoff, "archive_cutoff":archive_cutoff, "limit":settings.mono_checkbox_poll_batch_size,
            "shop_available":shop_available, "master_available":master_available})).mappings().all()
        for row in candidates:
            telemetry_token = error_reporting.poll_context.set(True)
            try:
                async with session_factory() as session:
                    actor = SimpleNamespace(id=row['operator_id'])
                    if row['kind'] == 'master':
                        from app.schemas.master_mono import MasterMonoCommand
                        order = await sales.load_order(session, row['order_id'])
                        if order.status.value == "cancelled":
                            await master_mono.refresh_archived(session, actor, order.id)
                            checked += 1
                            continue
                        await master_mono.refresh(session, actor, order.id,
                            MasterMonoCommand(expected_revision=order.revision), str(uuid4()))
                    else:
                        order = await sales.load_order(session, row['order_id'])
                        if order.status.value == "cancelled":
                            await order_archive_payments.refresh(session, actor, order)
                            checked += 1
                            continue
                        binding = order.shop_payment_intent
                        if not sales.canonical_payment(order):
                            from app.services.shop_payment_resolutions import effective_invoice
                            invoice, _ = await effective_invoice(session, binding)
                            result = await payments.command(session, actor, order.id, VerifyInvoiceCommand(
                                expected_revision=order.revision, expected_profile_id=binding.profile_id,
                                expected_profile_hash=binding.profile_hash, invoice_id=invoice), str(uuid4()), 'verify')
                            if not result.payment:
                                checked += 1
                                continue
                            order = await sales.load_order(session, order.id)
                            binding = order.shop_payment_intent
                        await direct.refresh(session, actor, order.id, RefreshReceiptCommand(
                            expected_revision=order.revision, expected_profile_id=binding.profile_id,
                            expected_profile_hash=binding.profile_hash), str(uuid4()))
                    checked += 1
            except HTTPException:
                # Current permissions, profile pins and revisions fail closed.
                blocked += 1
            except Exception as error:
                if not getattr(error, "_technical_reported", False):
                    error_reporting.report("poll", "poll_failed", order_id=row["order_id"], repeated=True)
                # Do not log tokens, provider payloads or bank error bodies.
                blocked += 1
                logger.warning("mono_checkbox_poll_failed order_id=%s", row['order_id'])
            finally:
                error_reporting.poll_context.reset(telemetry_token)
                await lease.execute(text("""INSERT INTO mono_checkbox_poll_state(kind,order_id,last_attempt_at)
                    VALUES(:kind,:order_id,:now) ON CONFLICT(kind,order_id)
                    DO UPDATE SET last_attempt_at=EXCLUDED.last_attempt_at"""),
                    {"kind":row['kind'],"order_id":row['order_id'],"now":datetime.now(UTC)})
        await lease.commit()
    return {"checked": checked, "blocked": blocked}


async def run_mono_checkbox_scheduler():
    from app.core.database import AsyncSessionLocal
    while True:
        try:
            await poll_once(AsyncSessionLocal)
        except asyncio.CancelledError:
            raise
        except Exception:
            error_reporting.report("poll", "poll_failed", repeated=True)
            logger.warning("mono_checkbox_poll_batch_failed")
        await asyncio.sleep(settings.mono_checkbox_poll_seconds)
