"""Month overlap and redirect contract through actual admin HTTP and PostgreSQL."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.routes import bookings as routes
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.booking import (Master, BarberService, Booking, BookingStatus,
                                MasterTimeBlock, MasterAvailabilityWindow)
from tests.test_inventory_integration import anyio_backend, database

pytestmark = pytest.mark.anyio
KYIV = ZoneInfo('Europe/Kyiv')


async def test_all_calendar_endpoints_return_overlapping_intervals_and_resolve_alias(database):
    start = datetime(2026,11,1,tzinfo=KYIV)
    end = datetime(2026,12,1,tzinfo=KYIV)
    async with database() as session:
        target = Master(full_name='Synthetic physical resource')
        session.add(target)
        await session.flush()
        alias = Master(full_name='Synthetic source', booking_redirect_master_id=target.id)
        svc = BarberService(master_id=target.id,name='Synthetic calendar service',duration_minutes=30,price=100)
        session.add_all([alias,svc])
        await session.flush()
        expected = {}
        for model, key in [(MasterTimeBlock,'blocks'),(MasterAvailabilityWindow,'availability')]:
            spanning = model(master_id=target.id,start_at=start-timedelta(days=2),end_at=start+timedelta(days=2))
            session.add_all([spanning,model(master_id=target.id,start_at=start-timedelta(days=2),end_at=start),
                             model(master_id=target.id,start_at=end,end_at=end+timedelta(days=1))])
            await session.flush()
            expected[key] = spanning.id
        booking_ids = []
        for status in BookingStatus:
            booking=Booking(master_id=target.id,service_id=svc.id,customer_name='Synthetic customer',
                            customer_phone='+380500000001',start_at=start-timedelta(hours=1),
                            end_at=start+timedelta(hours=1),status=status)
            session.add(booking)
            await session.flush()
            booking_ids.append(booking.id)
        await session.commit()
        alias_id=alias.id
    app=FastAPI();app.include_router(routes.backoffice_router)
    app.dependency_overrides[get_current_admin_user]=lambda:SimpleNamespace(is_superuser=True)
    async def get_session():
        async with database() as session:yield session
    app.dependency_overrides[get_db_session]=get_session
    params=dict(date_from=start.isoformat(),date_to=end.isoformat(),master_id=alias_id)
    async with AsyncClient(transport=ASGITransport(app),base_url='http://synthetic.test') as client:
        for path,key in [('/calendar/time-blocks','blocks'),('/availability','availability')]:
            response=await client.get(path,params=params)
            assert response.status_code==200,response.text
            assert [row['id'] for row in response.json()]==[expected[key]]
        response=await client.get('/calendar/bookings',params=params)
        assert response.status_code==200,response.text
        assert {row['id'] for row in response.json()}==set(booking_ids)
        response=await client.get('/calendar/bookings',params=dict(params,status='confirmed'))
        assert response.status_code==200,response.text
        assert len(response.json())==1 and response.json()[0]['status']=='confirmed'
