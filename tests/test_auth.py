from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import decode_token
from app.models.admin_user import AdminUser
from app.services.auth import ADMIN_ACCESS_SCOPE, ADMIN_REFRESH_SCOPE, AuthService


class FakeSession:
    def __init__(self, user: AdminUser | None) -> None:
        self.user = user

    async def get(self, model, user_id: int):  # noqa: ANN001
        if model is AdminUser and self.user and self.user.id == user_id:
            return self.user
        return None


def test_backoffice_token_pair_uses_admin_and_refresh_scopes() -> None:
    user = AdminUser(id=123, email="admin@example.com", hashed_password="hash", is_active=True, is_superuser=True)
    tokens = AuthService().issue_token_pair(user)

    access_payload = decode_token(tokens.access_token)
    refresh_payload = decode_token(tokens.refresh_token)

    assert access_payload["sub"] == "123"
    assert access_payload["scope"] == ADMIN_ACCESS_SCOPE
    assert refresh_payload["sub"] == "123"
    assert refresh_payload["scope"] == ADMIN_REFRESH_SCOPE
    assert refresh_payload["exp"] > access_payload["exp"]


@pytest.mark.anyio
async def test_refresh_token_pair_preserves_original_session_expiry() -> None:
    user = AdminUser(id=123, email="admin@example.com", hashed_password="hash", is_active=True, is_superuser=True)
    session_expires_at = datetime.now(UTC) + timedelta(days=7)
    tokens = AuthService().issue_token_pair(user, session_expires_at=session_expires_at)

    refreshed = await AuthService().refresh_token_pair(FakeSession(user), tokens.refresh_token)

    original_refresh_payload = decode_token(tokens.refresh_token)
    refreshed_access_payload = decode_token(refreshed.access_token)
    refreshed_refresh_payload = decode_token(refreshed.refresh_token)

    assert refreshed_access_payload["scope"] == ADMIN_ACCESS_SCOPE
    assert refreshed_refresh_payload["scope"] == ADMIN_REFRESH_SCOPE
    assert refreshed_refresh_payload["exp"] == original_refresh_payload["exp"]
