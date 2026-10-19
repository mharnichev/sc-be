"""OTP final admission on isolated PG; the HTTP transport stays in memory."""
from datetime import UTC,datetime,timedelta
import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.core.security import hash_otp_code
from app.models.customer_otp_code import CustomerOtpCode
from app.models.sms_queue import SmsQueueJob
from app.services.customer_auth import CustomerAuthService
from app.services.sms import SmsService
from app.services.sms_queue import SmsQueueService,SmsRequestContext,SmsTransportError
from tests.test_inventory_integration import anyio_backend,database
pytestmark=pytest.mark.anyio
PHONE='+380500001234'

async def challenge(database,*,sent=None,expires=None):
    now=datetime.now(UTC)
    async with database() as session:
        row=CustomerOtpCode(phone=PHONE,code_hash=hash_otp_code(PHONE,'123456'),sent_at=sent or now,expires_at=expires or now+timedelta(minutes=5))
        session.add(row);await session.commit();return row

@pytest.fixture
def transport(monkeypatch):
    calls=[]
    monkeypatch.setattr(settings,'sms_provider','smsclub');monkeypatch.setattr(settings,'sms_club_token','synthetic-r6-only')
    def record(self,url,payload,headers):
        calls.append((url,payload));return {'success_request':{'info':{'mock-otp-id':payload['phone'][0]}}}
    monkeypatch.setattr(SmsService,'_post_json',record)
    return calls

def queue(database):
    service=SmsService();q=SmsQueueService(session_factory=database,transport=service._execute_queue_job);service._queue=q;return q

async def enqueue(q,otp,*,legacy=False,phone=PHONE):
    context=SmsRequestContext(priority=0,idempotency_key=f'otp:{otp.id}',otp_record_id=None if legacy else otp.id)
    return await q.enqueue('send',{'phone':[phone.lstrip('+')],'message':'synthetic OTP 123456','src_addr':'Synthetic'},context=context,priority=0,expires_at=datetime.now(UTC)+timedelta(hours=1))

async def test_absolute_otp_expiry_is_pinned_and_two_workers_send_once(database,transport):
    otp=await challenge(database);a=queue(database);b=queue(database);job=await enqueue(a,otp)
    assert job.expires_at==otp.expires_at
    await asyncio.gather(a.process_one(),b.process_one())
    assert len(transport)==1
    assert (await a.get(job.id)).status=='accepted'
    await b.process_one();assert len(transport)==1

@pytest.mark.parametrize('change,reason',[('superseded','otp_superseded'),('verified','otp_inactive'),('expired','expired'),('missing','otp_missing'),('legacy','otp_legacy_context'),('identity','otp_identity_mismatch')])
async def test_stale_otp_never_crosses_http_boundary(database,transport,change,reason):
    otp=await challenge(database);q=queue(database);job=await enqueue(q,otp,legacy=change=='legacy',phone='+380500009999' if change=='identity' else PHONE)
    async with database() as session:
        row=await session.get(CustomerOtpCode,otp.id)
        if change=='superseded':
            session.add(CustomerOtpCode(phone=PHONE,code_hash=row.code_hash,sent_at=row.sent_at+timedelta(microseconds=1),expires_at=row.expires_at))
        elif change=='verified':row.verified_at=datetime.now(UTC)
        elif change=='expired':
            row.expires_at=datetime.now(UTC)-timedelta(seconds=1)
            saved=await session.get(SmsQueueJob,job.id);saved.expires_at=row.expires_at
        elif change=='missing':await session.delete(row)
        await session.commit()
    await q.process_one();assert transport==[]
    final=await q.get(job.id);assert final.status==('cancelled' if change=='expired' else 'skipped') and final.error_code==reason
    assert final.payload is None or final.payload=={}

async def test_latest_challenge_tie_uses_id_and_old_queue_stays_blocked(database,transport):
    now=datetime.now(UTC);old=await challenge(database,sent=now);new=await challenge(database,sent=now)
    q=queue(database);job=await enqueue(q,old);await q.process_one()
    assert transport==[] and (await q.get(job.id)).error_code=='otp_superseded'
    async with database() as session:
        assert (await CustomerAuthService()._latest_otp(session,PHONE)).id==new.id

async def test_uncertain_otp_is_not_automatically_sent_again(database,transport):
    otp=await challenge(database);q=queue(database);job=await enqueue(q,otp);calls=[]
    async def lost_reply(row):
        calls.append(row.id);raise SmsTransportError(503,'Synthetic response lost',code='timeout',ambiguous=True)
    q.transport=lost_reply;await q.process_one()
    assert (await q.get(job.id)).status=='uncertain'
    await queue(database).process_one();assert calls==[job.id] and transport==[]

@pytest.mark.parametrize('environment',['production','staging'])
async def test_stub_production_otp_fails_before_database_or_sender(monkeypatch,environment):
    monkeypatch.setattr(settings,'app_env',environment);monkeypatch.setattr(settings,'sms_provider','stub')
    with pytest.raises(HTTPException) as error:
        await CustomerAuthService().request_otp(None,PHONE)
    assert error.value.status_code==503 and error.value.detail['code']=='otp_delivery_unavailable'

async def test_provider_otp_without_challenge_context_is_rejected(monkeypatch):
    monkeypatch.setattr(settings,'app_env','production')
    with pytest.raises(HTTPException) as error:await SmsService().send_otp_code(PHONE,'123456')
    assert error.value.status_code==503
