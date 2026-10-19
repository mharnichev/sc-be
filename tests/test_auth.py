from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.routes import auth as auth_routes
from app.core.security import decode_token
from app.dependencies.auth import get_current_master
from app.models.admin_user import AdminUser
from app.services.auth import ADMIN_ACCESS_SCOPE, ADMIN_REFRESH_SCOPE, AuthService


class FakeSession:
    def __init__(self, user: AdminUser | None, master=None) -> None:
        self.user = user
        self.master = master
        self.statements = []

    async def get(self, model, user_id: int):  # noqa: ANN001
        if model is AdminUser and self.user and self.user.id == user_id:
            return self.user
        return None

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.master)


def current_user(*, is_superuser: bool = False, can_post_inventory: bool = False, can_record_sales: bool = False, can_manage_shipments: bool = False, can_fiscalize_sales: bool = False, can_fiscalize_returns: bool = False, can_manage_fiscal_settings: bool = False) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=42,
        email="admin@example.com",
        is_active=True,
        is_superuser=is_superuser,
        can_post_inventory=can_post_inventory,
        can_record_sales=can_record_sales,
        can_manage_shipments=can_manage_shipments,
        can_fiscalize_sales=can_fiscalize_sales,
        can_fiscalize_returns=can_fiscalize_returns,
        can_manage_fiscal_settings=can_manage_fiscal_settings,
        created_at=now,
        updated_at=now,
    )


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


@pytest.mark.anyio
async def test_get_current_master_allows_inactive_linked_master() -> None:
    master = SimpleNamespace(id=7, is_active=False)
    session = FakeSession(None, master=master)

    resolved = await get_current_master(current_user=SimpleNamespace(id=42), session=session)

    assert resolved is master
    query = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "masters.admin_user_id = 42" in query
    assert "masters.is_active" not in query.partition("WHERE")[2]


@pytest.mark.anyio
async def test_get_current_master_rejects_unlinked_user() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_current_master(current_user=SimpleNamespace(id=42), session=FakeSession(None))

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("master_is_active", [True, False])
async def test_auth_me_returns_barber_linkage_for_active_or_inactive_master(master_is_active: bool) -> None:
    session = FakeSession(None, master=SimpleNamespace(id=7, is_active=master_is_active))

    response = await auth_routes.me(current_user=current_user(), session=session)

    assert response.role == "barber"
    assert response.master_id == 7
    assert response.can_post_inventory is False
    query = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "masters.admin_user_id = 42" in query
    assert "masters.is_active" not in query.partition("WHERE")[2]


@pytest.mark.anyio
async def test_auth_me_returns_admin_role_for_unlinked_superuser() -> None:
    response = await auth_routes.me(
        current_user=current_user(is_superuser=True),
        session=FakeSession(None),
    )

    assert response.role == "admin"
    assert response.master_id is None


@pytest.mark.anyio
async def test_auth_me_returns_admin_role_for_linked_superuser() -> None:
    response = await auth_routes.me(
        current_user=current_user(is_superuser=True),
        session=FakeSession(None, master=SimpleNamespace(id=7, is_active=False)),
    )

    assert response.role == "admin"
    assert response.master_id is None


@pytest.mark.anyio
async def test_auth_me_returns_no_role_for_unlinked_non_superuser() -> None:
    response = await auth_routes.me(
        current_user=current_user(is_superuser=False),
        session=FakeSession(None),
    )

    assert response.role is None
    assert response.master_id is None


@pytest.mark.anyio
@pytest.mark.parametrize("is_superuser,can_post_inventory,expected", [
    (False, False, False), (False, True, True), (True, False, True),
])
async def test_auth_me_returns_effective_inventory_permission(is_superuser, can_post_inventory, expected) -> None:
    response = await auth_routes.me(
        current_user=current_user(is_superuser=is_superuser, can_post_inventory=can_post_inventory),
        session=FakeSession(None),
    )
    assert response.can_post_inventory is expected


@pytest.mark.anyio
@pytest.mark.parametrize("is_superuser,can_record_sales,expected", [
    (False, False, False), (False, True, True), (True, False, True),
])
async def test_auth_me_returns_effective_sales_permission(is_superuser, can_record_sales, expected) -> None:
    response = await auth_routes.me(
        current_user=current_user(is_superuser=is_superuser, can_record_sales=can_record_sales),
        session=FakeSession(None),
    )
    assert response.can_record_sales is expected


@pytest.mark.anyio
@pytest.mark.parametrize("is_superuser,can_manage_shipments,expected", [
    (False, False, False), (False, True, True), (True, False, True), (True, True, True),
])
async def test_auth_me_returns_effective_shipment_permission(is_superuser, can_manage_shipments, expected):
    response = await auth_routes.me(
        current_user=current_user(is_superuser=is_superuser, can_manage_shipments=can_manage_shipments),
        session=FakeSession(None),
    )
    assert response.can_manage_shipments is expected


@pytest.mark.anyio
@pytest.mark.parametrize("superuser, sales, settings, expected", [(False, False, False, (False, False)), (False, True, False, (True, False)), (False, False, True, (False, True)), (True, False, False, (True, True))])
async def test_me_exposes_effective_fiscal_permissions(superuser, sales, settings, expected):
    user = current_user(is_superuser=superuser, can_fiscalize_sales=sales, can_manage_fiscal_settings=settings)
    result = await auth_routes.me(current_user=user, session=FakeSession(None))
    assert (result.can_fiscalize_sales, result.can_manage_fiscal_settings) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("is_superuser,can_fiscalize_returns,expected", [
    (False, False, False), (False, True, True), (True, False, True), (True, True, True),
])
async def test_auth_me_returns_effective_return_permission(is_superuser, can_fiscalize_returns, expected):
    response = await auth_routes.me(
        current_user=current_user(is_superuser=is_superuser, can_fiscalize_returns=can_fiscalize_returns),
        session=FakeSession(None),
    )
    assert response.can_fiscalize_returns is expected
