from __future__ import annotations

import asyncio

from app.core.database import AsyncSessionLocal
from app.services.product_popularity import product_popularity_service


async def run() -> bool:
    async with AsyncSessionLocal() as session:
        return await product_popularity_service.refresh_if_due(session, force=True)


def main() -> None:
    refreshed = asyncio.run(run())
    print(f"refreshed={str(refreshed).lower()}")


if __name__ == "__main__":
    main()
