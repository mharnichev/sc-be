from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep
from app.models.upload import Upload
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.upload import UploadCreate, UploadResponse

backoffice_router = APIRouter()
repo = BaseRepository(Upload)


@backoffice_router.get("", response_model=PaginatedResponse[UploadResponse])
async def list_uploads(
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[UploadResponse]:
    stmt = select(Upload).order_by(Upload.id.desc())
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[UploadResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[UploadResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def create_upload(
    payload: UploadCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> UploadResponse:
    upload = await repo.create(session, payload.model_dump())
    return UploadResponse.model_validate(upload)


@backoffice_router.get("/{upload_id}", response_model=UploadResponse)
async def get_upload(
    upload_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> UploadResponse:
    upload = await repo.get(session, upload_id)
    if not upload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")
    return UploadResponse.model_validate(upload)
