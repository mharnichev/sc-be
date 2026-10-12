"""Local HTTP smoke: real FastAPI webhook, PostgreSQL and booking domain; fake transport only."""
from datetime import datetime, timedelta
import httpx
import pytest
from sqlalchemy import select
from app.core.config import settings
from app.core.database import get_db_session
from app.main import app
from app.api.v1.routes import messaging as routes
from app.models.booking import Booking, BookingStatus, MasterAvailabilityWindow
from app.models.messaging import TelegramBotSession
from app.models.repeat_booking import RepeatBookingOffer, RepeatBookingOfferStatus
from app.services.repeat_booking import repeat_booking_service
from test_inventory_integration import database, anyio_backend  # noqa: F401
from test_telegram_booking_flow import seed
from test_telegram_connect import FakeTelegramMessageProvider


@pytest.mark.anyio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_http_booking_repeat_reschedule_and_cancel_smoke(database,monkeypatch,scheduled):
    FakeTelegramMessageProvider.sent=[]
    FakeTelegramMessageProvider.sent_photos=[]
    FakeTelegramMessageProvider.answered_callbacks=[]
    monkeypatch.setattr(settings,'telegram_webhook_secret','smoke-secret')
    monkeypatch.setattr(routes,'TelegramMessageProvider',FakeTelegramMessageProvider)
    monkeypatch.setattr(routes,'_schedule_booking_notifications',lambda *a: None)
    from app.services import telegram_booking_management as management
    async def noop(*a,**kw):
        return None
    monkeypatch.setattr(management,'offer_freed_booking_slot',noop)
    monkeypatch.setattr(routes.master_telegram_notification_service,'send_cancelled_booking_to_master',noop)
    async with database() as session:
        customer,master,_,services,contact=await seed(session)
        day=datetime.now(routes.KYIV_TZ).date()+timedelta(days=2)
        while routes.booking_service_layer.is_closed_business_day(day):
            day+=timedelta(days=1)
        start=datetime.combine(day,datetime.min.time(),tzinfo=routes.KYIV_TZ)+timedelta(hours=10)
        session.add(MasterAvailabilityWindow(master_id=master.id,start_at=start,end_at=start+timedelta(hours=5)))
        # A completed visit is the prefill source; new booking uses current catalog.
        source=Booking(master_id=master.id,service_id=services[0].id,customer_id=customer.id,
            customer_name=customer.name,customer_phone=customer.phone,start_at=start-timedelta(days=7),
            end_at=start-timedelta(days=7)+timedelta(minutes=30),status=BookingStatus.completed,
            completed_at=start-timedelta(days=7)+timedelta(minutes=30),total_amount=450)
        session.add(source)
        await session.flush()
        offer_token="s"*43
        if scheduled:
            session.add(RepeatBookingOffer(completed_booking_id=source.id,customer_id=customer.id,
                preferred_master_id=master.id,service_ids=[services[0].id],status=RepeatBookingOfferStatus.sent,
                token_hash=repeat_booking_service.hash_token(offer_token),scheduled_at=datetime.now(routes.KYIV_TZ),
                sent_at=datetime.now(routes.KYIV_TZ),expires_at=datetime.now(routes.KYIV_TZ)+timedelta(days=10)))
        await session.commit()
    async def dependency():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session]=dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local.test') as client:
            number=0
            async def send(text):
                nonlocal number
                number+=1
                update={'update_id':number}
                if ':' in text:
                    update['callback_query']={'id':str(number),'from':{'id':123},'data':text,
                        'message':{'chat':{'id':123, 'type': 'private'}}}
                else:
                    update['message']={'chat':{'id':123, 'type': 'private'},'from':{'id':123},'text':text}
                response=await client.post('/api/v1/public/telegram/webhook',
                    headers={'X-Telegram-Bot-Api-Secret-Token':'smoke-secret'},json=update)
                assert response.status_code==200,response.text
                return response.json()
            async def payload():
                async with database() as session:
                    bot=await session.scalar(select(TelegramBotSession).where(TelegramBotSession.chat_id=='123'))
                    return dict(bot.payload_json)
            response=await client.get('/openapi.json')
            assert response.status_code==200
            assert '/api/v1/public/telegram/webhook' in response.json()['paths']
            assert (await send('repeat_offer:'+offer_token if scheduled else 'Записатися знову'))['action']=='repeat_booking_start'
            data=await payload()
            assert data['selected_service_ids']==[services[0].id]
            assert 'selected_visit_time' not in data
            assert (await send(f'select_date:{day.isoformat()}'))['handled']
            data=await payload()
            assert (await send(f'select_time:{start.replace(tzinfo=None).isoformat()}|{data["slot_token"]}'))['action']=='select_time'
            data=await payload()
            callback='confirm_booking:'+data['confirmation_token']
            assert (await send(callback))['action']=='book',FakeTelegramMessageProvider.sent[-3:]
            data=await payload(); booking_id=data['booking_id']
            await send(callback)
            async with database() as session:
                records=(await session.execute(select(Booking).where(Booking.status==BookingStatus.confirmed))).scalars().all()
                assert len(records)==1 and records[0].id==booking_id
                assert records[0].total_amount==500
                if scheduled:
                    offer=await session.scalar(select(RepeatBookingOffer))
                    assert offer.status==RepeatBookingOfferStatus.booked and offer.result_booking_id==booking_id
                    assert offer.token_hash is None
                    assert (await send('repeat_offer:'+offer_token))['action']=='repeat_booking_offer_unavailable' 
            assert (await send(f'reschedule_booking:{booking_id}'))['action']=='booking_management_prompt'
            data=(await payload())['booking_management']
            await send(f'manage_date:{data["slot_token"]}:{day:%Y%m%d}')
            data=(await payload())['booking_management']
            index=next(i for i,v in enumerate(data['slots']) if datetime.fromisoformat(v).hour==12)
            await send(f'manage_slot:{data["slot_token"]}:{index}')
            data=(await payload())['booking_management']
            assert (await send('manage_confirm:'+data['confirmation']))['action']=='booking_management_done'
            await send(f'cancel_booking:{booking_id}')
            data=(await payload())['booking_management']
            async with database() as session:
                booking=await session.get(Booking,booking_id)
                assert booking.status==BookingStatus.confirmed and booking.start_at.astimezone(routes.KYIV_TZ).hour==12
            await send('manage_cancel:'+data['token'])
            async with database() as session:
                assert (await session.get(Booking,booking_id)).status==BookingStatus.cancelled
            unauthorized=await client.post('/api/v1/public/telegram/webhook',json={})
            assert unauthorized.status_code==401
    finally:
        app.dependency_overrides.pop(get_db_session,None)
