"""Retry only durable intents created by future blog subscription requests.

Each poll considers at most twenty due jobs; a busy email is skipped. Failures
use the coordinator's bounded retry delay. Historical subscriptions are never
discovered or enqueued here.
"""
from __future__ import annotations

import asyncio
import logging

from app.services.brevo import BREVO_SYNC_BATCH_SIZE, BREVO_SYNC_POLL_SECONDS, brevo_contact_sync_service

logger = logging.getLogger(__name__)


async def run_brevo_sync_scheduler() -> None:
    while True:
        await asyncio.sleep(BREVO_SYNC_POLL_SECONDS)
        try:
            if brevo_contact_sync_service.is_configured():
                await brevo_contact_sync_service.sync_pending(limit=BREVO_SYNC_BATCH_SIZE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Brevo sync scheduler iteration failed", extra={"error_type": type(exc).__name__})
