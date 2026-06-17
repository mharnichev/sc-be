from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_scoped_token, decode_token, verify_password
from app.models.admin_user import AdminUser


ADMIN_ACCESS_SCOPE = "admin"
ADMIN_REFRESH_SCOPE = "admin_refresh"


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str


class AuthService:
    async def authenticate(self, session: AsyncSession, email: str, password: str) -> AdminUser:
        result = await session.execute(select(AdminUser).where(AdminUser.email == email))
        user = result.scalar_one_or_none()
        if not user or not verify_password(password, user.hashed_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
        return user

    def issue_token_pair(self, user: AdminUser, *, session_expires_at: datetime | None = None) -> TokenPair:
        session_expires_at = session_expires_at or (
            datetime.now(UTC) + timedelta(days=settings.backoffice_refresh_token_expire_days)
        )
        access_expires_at = min(
            datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes),
            session_expires_at,
        )
        return TokenPair(
            access_token=create_scoped_token(
                subject=user.id,
                scope=ADMIN_ACCESS_SCOPE,
                expires_at=access_expires_at,
            ),
            refresh_token=create_scoped_token(
                subject=user.id,
                scope=ADMIN_REFRESH_SCOPE,
                expires_at=session_expires_at,
            ),
        )

    def issue_access_token(self, user: AdminUser) -> str:
        return self.issue_token_pair(user).access_token

    async def refresh_token_pair(self, session: AsyncSession, refresh_token: str) -> TokenPair:
        try:
            payload = decode_token(refresh_token)
        except JWTError:
            raise self._credentials_exception()

        if payload.get("scope") != ADMIN_REFRESH_SCOPE:
            raise self._credentials_exception()

        try:
            user_id = int(payload.get("sub"))
            session_expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        except (TypeError, ValueError, KeyError):
            raise self._credentials_exception()

        user = await session.get(AdminUser, user_id)
        if not user or not user.is_active:
            raise self._credentials_exception()

        return self.issue_token_pair(user, session_expires_at=session_expires_at)

    def _credentials_exception(self) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
