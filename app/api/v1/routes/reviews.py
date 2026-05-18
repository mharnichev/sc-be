from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.schemas.review import GoogleBusinessReviewsResponse
from app.services.google_business_reviews import GoogleBusinessReviewsError, GoogleBusinessReviewsService

public_router = APIRouter()
backoffice_router = APIRouter()
service = GoogleBusinessReviewsService()


@public_router.get("", response_model=GoogleBusinessReviewsResponse)
async def list_reviews(
    session: AsyncSession = Depends(get_db_session),
) -> GoogleBusinessReviewsResponse:
    try:
        return await service.get_reviews(session)
    except GoogleBusinessReviewsError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@backoffice_router.post("/refresh", response_model=GoogleBusinessReviewsResponse)
async def refresh_reviews(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> GoogleBusinessReviewsResponse:
    try:
        return await service.get_reviews(session, force_refresh=True)
    except GoogleBusinessReviewsError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
