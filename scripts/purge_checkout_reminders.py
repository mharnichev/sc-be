"""Explicit retention-only maintenance; never dispatches a message.

Run from sc-be: python -m scripts.purge_checkout_reminders --purge
Without --purge this command prints usage and makes no database connection.
"""
from __future__ import annotations

import argparse
import asyncio

from app.core.database import AsyncSessionLocal, engine
from app.services.checkout_reminders import purge


async def main():
    try:
        async with AsyncSessionLocal() as session:
            await purge(session)
        print("Checkout reminder retention maintenance completed")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purge", action="store_true", help="Erase expired reminder contact data and old tombstones")
    args = parser.parse_args()
    if args.purge:
        asyncio.run(main())
    else:
        parser.print_help()
