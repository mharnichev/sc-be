"""Repeat capability consumption is part of the ordinary booking transaction."""
from datetime import datetime, timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import select,func
from app.api.v1.routes import messaging as routes
from app.models.booking import Booking,BookingSource
from app.models.messaging import TelegramBotSession
from app.services import telegram_return_booking as returning
from test_inventory_integration import database,anyio_backend  # noqa: F401
from test_telegram_booking_flow import seed
from test_telegram_connect import FakeTelegramMessageProvider


@pytest.mark.anyio
async def test_rejected_repeat_attribution_rolls_back_flushed_booking(database,monkeypatch):
    from app.services import telegram_inbox
    async def noop(*a,**kw): return None
    monkeypatch.setattr(telegram_inbox,'send_event_reply',noop)
    monkeypatch.setattr(returning,'validate_telegram_repeat_booking',noop)
    async def reject(*a,**kw): raise HTTPException(409,'Repeat offer changed')
    monkeypatch.setattr(returning,'attribute_telegram_repeat_booking',reject)
    FakeTelegramMessageProvider.sent=[]
    async with database() as session:
        customer,master,_,services,contact=await seed(session)
        start=datetime.now(routes.KYIV_TZ)+timedelta(days=2)
        bot=TelegramBotSession(chat_id='123',telegram_contact_id=contact.id,linked_customer_id=customer.id,
            selected_master_id=master.id,state=routes.TELEGRAM_READY_TO_BOOK_STATE,
            payload_json={'selected_service_ids':[services[0].id], 'selected_visit_date':start.date().isoformat(),
                'selected_visit_time':start.isoformat(),'confirmation_token':'a'*32,'repeat_booking_offer_id':987})
        session.add(bot);await session.commit()
        async def create(db,payload,**kw):
            booking=Booking(master_id=master.id,service_id=services[0].id,customer_id=customer.id,
                customer_name=customer.name,customer_phone=customer.phone,start_at=start,
                end_at=start+timedelta(minutes=30),source=BookingSource.telegram)
            db.add(booking);await db.flush();return booking
        monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',create)
        assert not await routes._handle_booking_confirmation(FakeTelegramMessageProvider(),session,
            chat_id='123',telegram_contact=contact,background_tasks=None,confirmation_token='a'*32)
        assert await session.scalar(select(func.count()).select_from(Booking))==0
        assert bot.state!=routes.TELEGRAM_BOOKED_STATE


@pytest.mark.anyio
async def test_confirmation_after_midnight_reopens_current_dates(database,monkeypatch):
    from app.services import telegram_inbox
    async def noop(*a,**kw): return None
    monkeypatch.setattr(telegram_inbox,'send_event_reply',noop)
    async def past(*a,**kw): raise HTTPException(400,'Slot is in the past')
    monkeypatch.setattr(routes.booking_service_layer,'create_public_booking',past)
    tomorrow=datetime.now(routes.KYIV_TZ).date()+timedelta(days=1)
    async def dates(*a,**kw): return [tomorrow]
    monkeypatch.setattr(routes,'_available_visit_dates',dates)
    FakeTelegramMessageProvider.sent=[]
    async with database() as session:
        customer,master,_,services,contact=await seed(session)
        start=datetime.now(routes.KYIV_TZ)-timedelta(days=1)
        bot=TelegramBotSession(chat_id='123',telegram_contact_id=contact.id,linked_customer_id=customer.id,
            selected_master_id=master.id,state=routes.TELEGRAM_READY_TO_BOOK_STATE,
            payload_json={'selected_service_ids':[services[0].id],'selected_visit_date':start.date().isoformat(),
                          'selected_visit_time':start.isoformat(),'confirmation_token':'a'*32,'slot_token':'b'*16})
        session.add(bot);await session.commit()
        assert not await routes._handle_booking_confirmation(FakeTelegramMessageProvider(),session,
            chat_id='123',telegram_contact=contact,background_tasks=None,confirmation_token='a'*32)
        assert bot.state==routes.TELEGRAM_SELECTING_DATE_STATE
        assert 'selected_visit_time' not in bot.payload_json and 'confirmation_token' not in bot.payload_json
        assert bot.payload_json['date_page']['after_date']==tomorrow.isoformat()
        assert await session.scalar(select(func.count()).select_from(Booking))==0
        assert 'Крок 3' in FakeTelegramMessageProvider.sent[-1][1]
