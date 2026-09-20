from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.schemas.auth import AdminUserResponse, BackofficeTokenResponse, RefreshTokenRequest
from app.services.auth import AuthService

backoffice_router = APIRouter()
auth_service = AuthService()


@backoffice_router.post("/login", response_model=BackofficeTokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeTokenResponse:
    user = await auth_service.authenticate(session, email=form_data.username, password=form_data.password)
    tokens = auth_service.issue_token_pair(user)
    return BackofficeTokenResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)


@backoffice_router.post("/refresh", response_model=BackofficeTokenResponse)
async def refresh(
    payload: RefreshTokenRequest,
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeTokenResponse:
    tokens = await auth_service.refresh_token_pair(session, payload.refresh_token)
    return BackofficeTokenResponse(access_token=tokens.access_token, refresh_token=tokens.refresh_token)


@backoffice_router.get("/me", response_model=AdminUserResponse)
async def me(
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUserResponse:
    response = AdminUserResponse.model_validate(current_user)
    if current_user.is_superuser:
        return response.model_copy(update={"role": "admin"})
    master = (
        await session.execute(select(Master).where(Master.admin_user_id == current_user.id))
    ).scalar_one_or_none()
    if master:
        return response.model_copy(update={"role": "barber", "master_id": master.id})
    return response
