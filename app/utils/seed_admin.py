from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.security import get_password_hash
from app.models.admin_user import AdminUser


async def seed_admin(email: str, password: str, is_superuser: bool = True) -> None:
    async with AsyncSessionLocal() as session:
        assert isinstance(session, AsyncSession)
        result = await session.execute(select(AdminUser).where(AdminUser.email == email))
        existing = result.scalar_one_or_none()
        if existing:
            print(f"Admin user already exists: {email}")
            return

        user = AdminUser(
            email=email,
            hashed_password=get_password_hash(password),
            is_active=True,
            is_superuser=is_superuser,
        )
        session.add(user)
        await session.commit()
        print(f"Created admin user: {email}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the initial admin user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(seed_admin(args.email, args.password, is_superuser=True))
