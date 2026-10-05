from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.schemas.campaign_analytics import CampaignOfferAnalytics
from app.services.campaign_analytics import offer_analytics

backoffice_router = APIRouter(dependencies=[Depends(get_current_admin_user)])


@backoffice_router.get("/campaign-offer-analytics", response_model=CampaignOfferAnalytics,
    summary="Observed recipient-offer engagement and completed-visit revenue")
async def analytics(campaign_id: int | None = Query(None, gt=0), run_id: int | None = Query(None, gt=0),
                    master_id: int | None = Query(None, gt=0), promotion_id: int | None = Query(None, gt=0),
                    date_from: AwareDatetime | None = Query(None), date_to: AwareDatetime | None = Query(None),
                    session: AsyncSession = Depends(get_db_session)):
    if date_from is not None and date_to is not None and date_from >= date_to:
        raise HTTPException(422, detail="date_from must precede date_to")
    return await offer_analytics(session, campaign_id=campaign_id, run_id=run_id,
                                 master_id=master_id, promotion_id=promotion_id, date_from=date_from, date_to=date_to)
