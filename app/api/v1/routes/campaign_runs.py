"""Authenticated run inspection and explicit, idempotent campaign launch."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.campaign_run import CampaignRun
from app.models.messaging import MessageRecipient
from app.schemas.campaign_run import (
    CampaignAudiencePreviewResponse,
    CampaignRunCreate,
    CampaignRunDetail,
    CampaignRunMemberResponse,
    CampaignRunResponse,
)
from app.schemas.common import PaginatedResponse
from app.schemas.sms_queue import SmsQueueProgress, CancelUnsentResponse
from app.services.campaign_runs import campaign_run_service
from app.services.messaging import MessagingService

backoffice_router = APIRouter(dependencies=[Depends(get_current_admin_user)])
messaging_service = MessagingService()


async def get_run(session: AsyncSession, campaign_id: int, run_id: int) -> CampaignRun:
    run = await session.get(CampaignRun, run_id)
    if run is None or run.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Campaign run not found")
    return run


@backoffice_router.post("/campaigns/{campaign_id}/audience-preview", response_model=CampaignAudiencePreviewResponse)
async def preview_campaign_audience(
    campaign_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    campaign = await messaging_service.get_campaign(session, campaign_id)
    return await campaign_run_service.preview(session, campaign, page=page, page_size=page_size)


@backoffice_router.post("/campaigns/{campaign_id}/runs", response_model=CampaignRunResponse, status_code=201)
async def launch_campaign_run(
    campaign_id: int,
    payload: CampaignRunCreate,
    session: AsyncSession = Depends(get_db_session),
):
    campaign = await messaging_service.get_campaign(session, campaign_id)
    return await campaign_run_service.launch(
        session, campaign, scheduled_at=payload.scheduled_at, idempotency_key=payload.idempotency_key
    )


@backoffice_router.get("/campaigns/{campaign_id}/runs", response_model=PaginatedResponse[CampaignRunResponse])
async def list_campaign_runs(
    campaign_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    await messaging_service.get_campaign(session, campaign_id)
    predicate = CampaignRun.campaign_id == campaign_id
    total = await session.scalar(select(func.count()).select_from(CampaignRun).where(predicate))
    items = (await session.scalars(select(CampaignRun).where(predicate).order_by(CampaignRun.id.desc())
                                  .offset((page - 1) * page_size).limit(page_size))).all()
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@backoffice_router.get("/campaigns/{campaign_id}/runs/{run_id}", response_model=CampaignRunDetail)
async def inspect_campaign_run(
    campaign_id: int, run_id: int, session: AsyncSession = Depends(get_db_session),
):
    run = await get_run(session, campaign_id, run_id)
    rows = (await session.execute(select(MessageRecipient.status, func.count(MessageRecipient.id))
                                  .where(MessageRecipient.run_id == run_id)
                                  .group_by(MessageRecipient.status))).all()
    result = CampaignRunDetail.model_validate(run)
    result.delivery_counts = {row[0].value: row[1] for row in rows}
    return result


@backoffice_router.get("/campaigns/{campaign_id}/runs/{run_id}/members", response_model=PaginatedResponse[CampaignRunMemberResponse])
async def inspect_campaign_run_members(
    campaign_id: int,
    run_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    await get_run(session, campaign_id, run_id)
    predicate = MessageRecipient.run_id == run_id
    total = await session.scalar(select(func.count()).select_from(MessageRecipient).where(predicate))
    items = (await session.scalars(select(MessageRecipient).where(predicate).order_by(MessageRecipient.customer_id)
                                  .offset((page - 1) * page_size).limit(page_size))).all()
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@backoffice_router.get("/campaigns/{campaign_id}/runs/{run_id}/queue", response_model=SmsQueueProgress)
async def inspect_run_queue(campaign_id: int, run_id: int, session: AsyncSession = Depends(get_db_session)):
    from app.services.campaign_dispatch import campaign_dispatch_service
    run = await get_run(session, campaign_id, run_id)
    campaign = await messaging_service.get_campaign(session, campaign_id)
    return await campaign_dispatch_service.progress(session, campaign, run=run)


@backoffice_router.get("/campaigns/{campaign_id}/queue", response_model=SmsQueueProgress)
async def inspect_campaign_queue(campaign_id: int, session: AsyncSession = Depends(get_db_session)):
    from app.services.campaign_dispatch import campaign_dispatch_service
    campaign = await messaging_service.get_campaign(session, campaign_id)
    return await campaign_dispatch_service.progress(session, campaign)


@backoffice_router.post("/campaigns/{campaign_id}/runs/{run_id}/cancel-unsent", response_model=CancelUnsentResponse)
async def cancel_run_unsent(campaign_id: int, run_id: int, session: AsyncSession = Depends(get_db_session)):
    run = await get_run(session, campaign_id, run_id)
    cancelled = await campaign_run_service.cancel_run_unsent(session, run)
    return {"run_id": run.id, "cancelled": cancelled, "status": run.status}
