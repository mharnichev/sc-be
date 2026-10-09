"""Delivery summary collapses events before pagination; full history stays available."""
import httpx
import pytest
from fastapi import FastAPI
from app.api.v1.routes.messaging import backoffice_router
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.messaging import MessageLog, MessageDeliveryStatus, MessageChannel
from tests.test_campaign_offers_integration import setup_offer
from tests.test_segments_integration import database, anyio_backend


@pytest.mark.anyio
async def test_delivery_summary_and_full_event_history(database):
    ids = await setup_offer(database)
    from app.models.campaign_offer import CampaignOffer
    async with database() as session:
        offer = await session.get(CampaignOffer, ids[-2])
        campaign_id = offer.campaign_id
        for state in [MessageDeliveryStatus.pending, MessageDeliveryStatus.sent,
                      MessageDeliveryStatus.delivered, MessageDeliveryStatus.delivered]:
            session.add(MessageLog(campaign_id=campaign_id, customer_id=offer.customer_id,
                                   recipient_id=offer.recipient_id, channel=MessageChannel.sms, status=state))
        # Unlinked historical records must not collapse into a single NULL group.
        for _ in range(2):
            session.add(MessageLog(campaign_id=campaign_id, customer_id=offer.customer_id,
                                   channel=MessageChannel.sms, status=MessageDeliveryStatus.failed))
        await session.commit()
    app = FastAPI()
    app.include_router(backoffice_router, prefix='/messaging')
    async def db():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_current_admin_user] = lambda: object()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        url = f'/messaging/campaigns/{campaign_id}/logs'
        summary = await client.get(url)
        assert summary.status_code == 200, summary.text
        assert summary.json()['total'] == 3
        linked = [row for row in summary.json()['items'] if row['recipient_id'] is not None]
        assert len(linked) == 1 and linked[0]['status'] == 'delivered'
        page = await client.get(url, params={'page': 2, 'page_size': 1})
        assert page.json()['total'] == 3
        assert len(page.json()['items']) == 1
        history = await client.get(url, params={'include_events': 'true'})
        assert history.json()['total'] == 6
        assert len(history.json()['items']) == 6
