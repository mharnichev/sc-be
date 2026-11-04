"""Actual HTTP + PostgreSQL; all data synthetic and schema-isolated."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.api.v1.routes import booking_funnel as routes
from app.core.database import get_db_session
from app.models.booking import BarberService, Master
from app.models.booking_funnel import BookingFunnelEvent
from app.models.booking_funnel_observation import BookingFunnelObservation as Observation
from app.services.booking_funnel_observation import observation_metrics
from tests.test_inventory_integration import anyio_backend, database

pytestmark = pytest.mark.anyio


@pytest.fixture
async def http(database, monkeypatch):
    monkeypatch.setattr(routes.settings, "booking_funnel_event_rate_limit", 10000)
    monkeypatch.setattr(routes, "observation_ingress_limiter", routes.SlidingWindowRateLimiter())
    monkeypatch.setattr(routes, "booking_funnel_rate_limiter", routes.SlidingWindowRateLimiter())
    async with database() as session:
        m = Master(full_name="Synthetic diagnostic master", is_active=True)
        session.add(m)
        await session.flush()
        services = [BarberService(master_id=m.id, name=f"Synthetic service {i}", duration_minutes=30, price=100) for i in range(2)]
        session.add_all(services)
        await session.commit()
        context = dict(master_id=m.id, service_ids=[s.id for s in services], target_date="2026-11-03",
                       duration_minutes=60, trigger="initial", anonymous_session_id="synthetic-attempt-001")
    app = FastAPI()
    app.include_router(routes.public_router)
    async def get_session():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = get_session
    async with AsyncClient(transport=ASGITransport(app), base_url="http://synthetic.test") as client:
        yield client, context


def payload(context, kind="availability_result", **kw):
    result = dict(context, event_id="synthetic-event-001", event_type=kind)
    if kind != "time_step_viewed":
        result['availability_request_id'] = "synthetic-request-001"
    if kind == "availability_result": result['slot_count'] = 0
    if kind == "availability_failed": result['error_code'] = "timeout"
    if kind == "availability_cancelled": result['cancel_reason'] = "superseded"
    result.update(kw)
    return result


async def send(client, data):
    return await client.post('/booking-funnel/observations', json=data)


async def test_same_id_retry_semantic_retry_conflict_and_privacy(http, database):
    client, ctx = http
    body = payload(ctx)
    assert (await send(client, body)).json()['status'] == 'recorded'
    assert (await send(client, body)).json()['status'] == 'duplicate'
    assert (await send(client, dict(body, event_id="synthetic-event-002"))).json()['status'] == 'duplicate'
    assert (await send(client, dict(body, slot_count=1))).status_code == 409
    assert (await send(client, dict(body, event_id='synthetic-event-002', availability_request_id='another-request-001'))).status_code == 409
    assert (await send(client, payload(ctx, 'availability_failed', event_id="synthetic-event-003"))).status_code == 409
    async with database() as session:
        rows = (await session.execute(select(Observation))).scalars().all()
        assert len(rows) == 1
        assert rows[0].anonymous_session_hash != ctx['anonymous_session_id']
        assert rows[0].request_id_hash != body['availability_request_id']
        assert rows[0].event_id_hash != body['event_id']
        assert (await session.execute(select(func.count()).select_from(BookingFunnelEvent))).scalar_one() == 0


@pytest.mark.parametrize('kind', ['time_step_viewed','availability_requested','availability_result','availability_failed','availability_cancelled'])
async def test_each_event_kind_and_no_historical_no_slot(http, database, kind):
    client, ctx = http
    response = await send(client, payload(ctx, kind))
    assert response.status_code == 202, response.text
    async with database() as session:
        assert (await session.execute(select(func.count()).select_from(BookingFunnelEvent))).scalar_one() == 0


@pytest.mark.parametrize('override', [
    {'metadata': {'phone': '+380500000000'}}, {'error': 'https://secret.test/?token=private'},
    {'slot_count': -1}, {'slot_count': True}, {'slot_count': 5001},
    {'error_code': 'raw error'}, {'cancel_reason': 'superseded'},
    {'availability_request_id': None}, {'duration_minutes': True}, {'service_ids': [True]},
    {'service_ids': []}, {'service_ids': [0]}, {'master_id': 0}, {'trigger':'query-string'},
])
async def test_invalid_or_arbitrary_context_is_not_persisted(http, database, override):
    client, ctx = http
    assert (await send(client, payload(ctx, **override))).status_code == 422
    async with database() as session:
        assert (await session.execute(select(func.count()).select_from(Observation))).scalar_one() == 0


async def test_context_validation_normalization_and_attempt_isolation(http):
    client, ctx = http
    assert (await send(client, payload(ctx, service_ids=[99999]))).status_code == 422
    assert (await send(client, payload(ctx, service_ids=list(reversed(ctx['service_ids'])) * 2))).status_code == 202
    # Same request ID under another attempt is an independent logical request.
    assert (await send(client, payload(ctx, anonymous_session_id='synthetic-attempt-002', event_id='synthetic-event-002'))).json()['status'] == 'recorded'
    assert (await send(client, payload(ctx, 'availability_requested', duration_minutes=30, event_id='synthetic-event-003'))).status_code == 409


async def test_concurrent_retries_one_row_and_out_of_order_delivery(http, database):
    client, ctx = http
    responses = await asyncio.gather(*[send(client, payload(ctx)) for _ in range(4)])
    assert [r.status_code for r in responses] == [202]*4
    assert sorted(r.json()['status'] for r in responses) == ['duplicate']*3 + ['recorded']
    # Lost request delivery can be retried after the terminal result.
    assert (await send(client, payload(ctx, 'availability_requested', event_id='synthetic-event-request'))).status_code == 202
    async with database() as session:
        metrics = await observation_metrics(session, start=datetime(2020,1,1,tzinfo=UTC), end=datetime(2030,1,1,tzinfo=UTC))
        assert metrics['event_counts']['availability_result'] == 1
        assert metrics['initial_empty_results'] == 1
        assert metrics['terminals_without_request'] == 0


async def test_period_receipt_time_half_open_and_cross_midnight_request(http, database):
    client, ctx = http
    await send(client, payload(ctx, 'availability_requested', event_id='synthetic-request-event'))
    await send(client, payload(ctx))
    start = datetime(2026,10,24,21,tzinfo=UTC)  # Kyiv midnight before DST change
    end = datetime(2026,10,25,22,tzinfo=UTC)    # next Kyiv midnight (25-hour day)
    async with database() as session:
        await session.execute(update(Observation).where(Observation.event_type=='availability_requested').values(occurred_at=start-timedelta(seconds=1)))
        await session.execute(update(Observation).where(Observation.event_type=='availability_result').values(occurred_at=start))
        await session.commit()
        metrics = await observation_metrics(session,start=start,end=end)
        assert metrics['event_counts']['availability_requested'] == 0
        assert metrics['event_counts']['availability_result'] == 1
        assert metrics['terminals_without_request'] == 0
        await session.execute(update(Observation).where(Observation.event_type=='availability_result').values(occurred_at=end))
        await session.commit()
        assert (await observation_metrics(session,start=start,end=end))['event_counts']['availability_result'] == 0


async def test_missing_request_is_diagnostic_not_failure(http, database):
    client, ctx = http
    await send(client, payload(ctx, 'availability_cancelled'))
    async with database() as session:
        metrics = await observation_metrics(session,start=datetime(2020,1,1,tzinfo=UTC),end=datetime(2030,1,1,tzinfo=UTC))
        assert metrics['terminals_without_request'] == 1
        assert metrics['event_counts']['availability_cancelled'] == 1
        assert metrics['event_counts']['availability_failed'] == 0


async def test_cohort_loss_rankings_are_separate_from_event_period_diagnostics(http, database):
    from app.models.booking_funnel import BookingFunnelEventType as Kind, BookingFunnelEventSource
    from app.schemas.booking_funnel import FUNNEL_STEP_TYPES
    from app.services.booking_funnel import BookingFunnelService
    client, ctx = http
    start=datetime(2026,11,1,tzinfo=UTC)
    end=start+timedelta(days=1)
    async with database() as session:
        for index in range(100):
            for kind,count in zip(FUNNEL_STEP_TYPES,[100,50,40,30,10,10]):
                if index<count:
                    session.add(BookingFunnelEvent(
                        event_id_hash=f'synthetic-cohort-{index}-{kind.value}',
                        anonymous_session_hash=f'synthetic-attempt-{index}',master_id=ctx['master_id'],
                        event_type=kind,source=BookingFunnelEventSource.client,occurred_at=start,
                    ))
        # Later success with no in-period start is not a new denominator/cohort.
        session.add(BookingFunnelEvent(event_id_hash='synthetic-orphan',anonymous_session_hash='orphan-attempt',
                                      event_type=Kind.booking_success,source=BookingFunnelEventSource.server,occurred_at=start))
        await session.commit()
        aggregate=await BookingFunnelService().aggregate(session,start=start,end=end,include_latest_digest=False)
        diag=aggregate.selection_diagnostics
        assert diag.largest_absolute_loss.from_step == Kind.booking_start
        assert diag.largest_absolute_loss.count == 50
        assert diag.largest_percentage_loss.from_step == Kind.slot_selected
        assert str(diag.largest_percentage_loss.drop_off_percent) == '66.67'
        assert diag.observation_incomplete is True
        assert aggregate.overall_conversion.started == 100
        assert aggregate.overall_conversion.succeeded == 10
        assert aggregate.observation_diagnostics.scope == 'event_period'
        assert not any(aggregate.observation_diagnostics.event_counts.values())


async def test_retry_stays_duplicate_after_options_removed(http, database):
    from sqlalchemy import delete
    client, ctx = http
    body=payload(ctx)
    assert (await send(client,body)).json()['status']=='recorded'
    async with database() as session:
        await session.execute(delete(BarberService).where(BarberService.master_id==ctx['master_id']))
        await session.commit()
    assert (await send(client,body)).json()['status']=='duplicate'
    assert (await send(client,dict(body,event_id='new-delivery-identity'))).json()['status']=='duplicate'


async def test_client_budget_survives_attempt_rotation_and_preserves_retries(http, database, monkeypatch):
    client,ctx=http
    monkeypatch.setattr(routes.settings,"booking_funnel_event_rate_limit",3)
    body=payload(ctx)
    assert (await send(client,body)).status_code==202
    assert (await send(client,body)).json()['status']=='duplicate'
    assert (await send(client,dict(body,event_id='rotating-event-002',anonymous_session_id='rotating-attempt-002'))).status_code==202
    for index in range(3,7):
        response=await client.post('/booking-funnel/observations',json=dict(body,event_id=f'rotating-event-{index:03}',anonymous_session_id=f'rotating-attempt-{index:03}'),headers={'X-Forwarded-For':f'192.0.2.{index}'})
        assert response.status_code==429
    assert len(routes.booking_funnel_rate_limiter._entries)==2
    assert len(routes.observation_ingress_limiter._entries)==1
    assert all(len(key)==64 for key in routes.observation_ingress_limiter._entries)
    async with AsyncClient(transport=ASGITransport(app=client._transport.app,client=('192.0.2.100',1234)),base_url='http://synthetic.test') as other:
        assert (await send(other,dict(body,event_id='another-client-event',anonymous_session_id='another-client-attempt'))).status_code==202
    async with database() as session:
        assert (await session.execute(select(func.count()).select_from(Observation))).scalar_one()==3


async def test_timeout_breakdown_is_separate_from_cancellation(http, database):
    client,ctx=http
    await send(client,payload(ctx,'availability_failed'))
    await send(client,payload(ctx,'availability_cancelled',event_id='cancelled-other-event',availability_request_id='cancelled-other-request'))
    async with database() as session:
        metrics=await observation_metrics(session,start=datetime(2020,1,1,tzinfo=UTC),end=datetime(2030,1,1,tzinfo=UTC))
        assert metrics['event_counts']['availability_failed']==1
        assert metrics['event_counts']['availability_cancelled']==1
        assert metrics['failure_counts']==dict(timeout=1,network=0,http_4xx=0,http_5xx=0,invalid_response=0)
