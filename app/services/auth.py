from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_scoped_access_token, verify_password
from app.models.admin_user import AdminUser


class AuthService:
    async def authenticate(self, session: AsyncSession, email: str, password: str) -> AdminUser:
        result = await session.execute(select(AdminUser).where(AdminUser.email == email))
        user = result.scalar_one_or_none()
        if not user or not verify_password(password, user.hashed_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
        return user

    def issue_access_token(self, user: AdminUser) -> str:
        return create_scoped_access_token(
            subject=user.id,
            scope="admin",
            expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        )
