"""Authenticated SMS operation and campaign dispatch progress."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.sms_queue import SmsAccountThrottle, SmsQueueJob
from app.schemas.common import PaginatedResponse
from app.schemas.sms_queue import SmsAccountQueueProgress, SmsQueueConfiguration, SmsQueueJobResponse

backoffice_router = APIRouter(dependencies=[Depends(get_current_admin_user)])


def queue_configuration() -> SmsQueueConfiguration:
    return SmsQueueConfiguration(
        account_key=settings.sms_club_account_key,
        provider_requests_per_second=settings.sms_club_requests_per_second,
        campaign_recipients_per_minute_default=settings.sms_campaign_recipients_per_minute,
        batch_size=settings.sms_queue_batch_size,
        concurrency=settings.sms_queue_concurrency,
        worker_enabled=settings.sms_queue_worker_enabled,
    )


@backoffice_router.get("/sms-queue", response_model=SmsAccountQueueProgress)
async def inspect_sms_queue(session: AsyncSession = Depends(get_db_session)):
    rows = (await session.execute(select(SmsQueueJob.status, func.count()).where(
        SmsQueueJob.account_key == settings.sms_club_account_key,
    ).group_by(SmsQueueJob.status))).all()
    throttle = await session.get(SmsAccountThrottle, settings.sms_club_account_key)
    return SmsAccountQueueProgress(
        configuration=queue_configuration(), counts=dict(rows),
        next_request_at=throttle.next_request_at if throttle else None,
        cooldown_until=throttle.cooldown_until if throttle else None,
    )


@backoffice_router.get("/sms-queue/jobs", response_model=PaginatedResponse[SmsQueueJobResponse])
async def list_sms_jobs(
    state: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    statement = select(SmsQueueJob).where(SmsQueueJob.account_key == settings.sms_club_account_key)
    if state is not None:
        statement = statement.where(SmsQueueJob.status == state)
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    items = (await session.scalars(statement.order_by(SmsQueueJob.created_at.desc(), SmsQueueJob.id)
                                  .limit(page_size).offset((page - 1) * page_size))).all()
    return {"total": total, "items": items, "page": page, "page_size": page_size}


@backoffice_router.get("/sms-queue/jobs/{job_id}", response_model=SmsQueueJobResponse)
async def get_sms_job(job_id: str, session: AsyncSession = Depends(get_db_session)):
    item = await session.get(SmsQueueJob, job_id)
    if item is None or item.account_key != settings.sms_club_account_key:
        raise HTTPException(404, detail="SMS queue job not found")
    return item
