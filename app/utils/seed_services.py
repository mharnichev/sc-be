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

DEFAULT_BASE_SERVICE_TITLES_EN = {
    "Традиційне гоління": "Traditional shave",
    "Дитяча стрижка": "Kids haircut",
    "Стрижка машинкою+стрижка бороди": "Clipper cut + beard trim",
    "Стрижка+борода": "Haircut + beard trim",
    "Стрижка бороди": "Beard trim",
    "Стрижка машинкою": "Clipper cut",
    "Стрижка": "Haircut",
}

DEFAULT_BASE_SERVICE_DESCRIPTIONS_EN = {
    "Традиційне гоління": "Classic straight-razor shave with hot towel preparation and finishing care.",
    "Дитяча стрижка": "Haircut for children with a clean, comfortable finish.",
    "Стрижка машинкою+стрижка бороди": "Clipper haircut paired with beard shaping and contour cleanup.",
    "Стрижка+борода": "Complete haircut with beard shaping, edging, and styling.",
    "Стрижка бороди": "Beard shaping, length adjustment, and clean contouring.",
    "Стрижка машинкою": "Even clipper haircut with clean edges and neckline detail.",
    "Стрижка": "Classic haircut with shape, texture, and styling.",
}


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
            title_uk = name
            title_en = DEFAULT_BASE_SERVICE_TITLES_EN.get(name)
            description_en = DEFAULT_BASE_SERVICE_DESCRIPTIONS_EN.get(name)
            if name in existing_names:
                continue
            base_service = BaseService(
                name=name,
                title_uk=title_uk,
                title_en=title_en,
                description_en=description_en,
                duration_minutes=duration_minutes,
                price=price,
                is_active=True,
            )
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
