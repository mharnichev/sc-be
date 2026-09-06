"""Reusable dynamic audiences; writes never enqueue delivery."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.segment import CustomerSegment, SegmentStatus
from app.schemas.segment import (
    SegmentCreate, SegmentList, SegmentPreviewRequest, SegmentPreviewResponse,
    SegmentRead, SegmentRules, SegmentUpdate,
)
from app.services.segments import segment_service

backoffice_router = APIRouter(dependencies=[Depends(get_current_admin_user)])


async def get_segment(session: AsyncSession, segment_id: int, *, lock: bool = False) -> CustomerSegment:
    statement = select(CustomerSegment).where(CustomerSegment.id == segment_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    segment = await session.scalar(statement)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    return segment


@backoffice_router.get("", response_model=SegmentList)
async def list_segments(
    status: SegmentStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1000000),
    session: AsyncSession = Depends(get_db_session),
):
    statement = select(CustomerSegment)
    if status is not None:
        statement = statement.where(CustomerSegment.status == status)
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    items = (await session.scalars(statement.order_by(CustomerSegment.id).limit(limit).offset(offset))).all()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@backoffice_router.post("", response_model=SegmentRead, status_code=201)
async def create_segment(payload: SegmentCreate, session: AsyncSession = Depends(get_db_session)):
    segment = CustomerSegment(**payload.model_dump(mode="json"), revision=1, status=SegmentStatus.active)
    session.add(segment)
    await session.commit()
    await session.refresh(segment)
    return segment


@backoffice_router.post("/preview", response_model=SegmentPreviewResponse)
async def preview_segment(payload: SegmentPreviewRequest, session: AsyncSession = Depends(get_db_session)):
    return await segment_service.preview(
        session, payload.rules, evaluated_at=payload.evaluated_at, limit=payload.limit, offset=payload.offset,
    )


@backoffice_router.get("/{segment_id}", response_model=SegmentRead)
async def retrieve_segment(segment_id: int, session: AsyncSession = Depends(get_db_session)):
    return await get_segment(session, segment_id)


@backoffice_router.patch("/{segment_id}", response_model=SegmentRead)
async def update_segment(segment_id: int, payload: SegmentUpdate, session: AsyncSession = Depends(get_db_session)):
    segment = await get_segment(session, segment_id, lock=True)
    if segment.status == SegmentStatus.archived:
        raise HTTPException(status_code=409, detail="Archived segments cannot be edited")
    if segment.revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="Segment revision conflict; retrieve the current revision")
    for key, value in payload.model_dump(mode="json", exclude_unset=True, exclude={"expected_revision"}).items():
        setattr(segment, key, value)
    segment.revision += 1
    await session.commit()
    await session.refresh(segment)
    return segment


@backoffice_router.post("/{segment_id}/archive", response_model=SegmentRead)
async def archive_segment(segment_id: int, session: AsyncSession = Depends(get_db_session)):
    segment = await get_segment(session, segment_id, lock=True)
    if segment.status != SegmentStatus.archived:
        segment.status = SegmentStatus.archived
        segment.archived_at = datetime.now(ZoneInfo("Europe/Kyiv"))
        segment.revision += 1
        await session.commit()
        await session.refresh(segment)
    return segment


@backoffice_router.get("/{segment_id}/members", response_model=SegmentPreviewResponse)
async def segment_members(
    segment_id: int,
    evaluated_at: AwareDatetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1000000),
    session: AsyncSession = Depends(get_db_session),
):
    segment = await get_segment(session, segment_id)
    return await segment_service.preview(
        session, SegmentRules.model_validate(segment.rules), evaluated_at=evaluated_at, limit=limit, offset=offset,
    )
