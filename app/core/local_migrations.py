"""Keep the development database current before serving requests after reloads."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import subprocess
import sys

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def apply_local_migrations() -> None:
    logger.info("Applying local database migrations before API startup")
    # Use a separate interpreter so Alembic's logging configuration and synchronous
    # engine do not interfere with the API's event loop or logging handlers.
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "-c", str(PROJECT_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=PROJECT_ROOT,
        check=True,
    )

