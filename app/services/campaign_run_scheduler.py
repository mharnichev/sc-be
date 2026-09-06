"""Opt-in scheduler for manually configured campaign runs only."""
from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.services.campaign_runs import campaign_run_service

logger = logging.getLogger(__name__)


async def run_campaign_scheduler() -> None:
    while True:
        await asyncio.sleep(settings.campaign_run_scheduler_interval_seconds)
        try:
            async with AsyncSessionLocal() as session:
                await campaign_run_service.process_due_runs(session)
                await campaign_run_service.process_run_messages(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Campaign run scheduler iteration failed")
