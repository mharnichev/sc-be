from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.booking import BaseService

DEFAULT_BASE_SERVICES = (
    ("Традиційне гоління", 30, 800),
    ("Дитяча стрижка", 60, 900),
    ("Стрижка машинкою+стрижка бороди", 60, 1300),
    ("Стрижка+борода", 90, 1500),
    ("Стрижка бороди", 30, 600),
    ("Стрижка машинкою", 30, 700),
    ("Стрижка", 60, 900),
)


async def seed_base_services(session: AsyncSession | None = None) -> int:
    owns_session = session is None
    if session is None:
        session = AsyncSessionLocal()

    try:
        created = 0
        existing_names = {
            name
            for name in (
                await session.execute(select(BaseService.name).where(BaseService.name.in_([item[0] for item in DEFAULT_BASE_SERVICES])))
            ).scalars().all()
        }
        for name, duration_minutes, price in DEFAULT_BASE_SERVICES:
            if name in existing_names:
                continue
            base_service = BaseService(name=name, duration_minutes=duration_minutes, price=price, is_active=True)
            session.add(base_service)
            await session.flush()
            created += 1
        if owns_session:
            await session.commit()
        elif created:
            await session.flush()
        return created
    finally:
        if owns_session:
            await session.close()


if __name__ == "__main__":
    count = asyncio.run(seed_base_services())
    print(f"Created base services: {count}")
