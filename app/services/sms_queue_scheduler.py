"""Persistent SMSClub queue workers; all concurrency admission is in PostgreSQL."""
import asyncio
import logging

from app.core.config import settings
from app.services.sms_queue import SmsQueueService

logger = logging.getLogger(__name__)


async def run_sms_queue_worker() -> None:
    queue = SmsQueueService()
    while True:
        try:
            remaining = settings.sms_queue_batch_size
            while remaining:
                width = min(remaining, settings.sms_queue_concurrency)
                outcomes = await asyncio.gather(*(queue.process_one() for _ in range(width)))
                remaining -= width
                if not any(outcomes):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log provider payloads, OTPs or bound SQL message bodies.
            logger.error("SMS queue worker iteration failed", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(settings.sms_queue_poll_seconds)
