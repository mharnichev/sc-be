"""Run manually with: python -m app.utils.sync_nova_poshta [--force]."""
import argparse
import asyncio

from app.core.database import AsyncSessionLocal
from app.services.nova_poshta_directory import NovaPoshtaDirectory


async def sync(force: bool) -> None:
    async with AsyncSessionLocal() as session:
        changed = await NovaPoshtaDirectory().refresh(session, force=force)
        print("Nova Poshta directory refreshed" if changed else "Nova Poshta refresh skipped (fresh, busy, or missing key)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    asyncio.run(sync(parser.parse_args().force))
