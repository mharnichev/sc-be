"""Quick POS durable intent tests: disposable migrated PG and existing mono mock."""
import asyncio
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import func,select
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.product import Product
from app.models.order import Order,OrderHandover,OrderPayment,OrderIntent
from app.models.master_mono import MasterMonoCreateRequest
from app.models.pos import POSCheckoutIntent
from app.schemas.pos import CreateCheckout,RevisionCommand
from app.services import pos,master_mono
from tests.test_shop_fiscal_integration import template_database,database
from tests.test_inventory_integration import checkout_dependencies,add_product
from tests.test_master_mono_integration import mock_provider,receipt

pytestmark=pytest.mark.anyio

@pytest.fixture(scope='module')
def anyio_backend():return 'asyncio'

@pytest.fixture
async def sellers(database):
    async with database() as s:
        for ident in (2,3):
            s.add(AdminUser(id=ident,email=f'pos-{ident}@example.test',hashed_password='unused',is_active=True,is_superuser=False,can_record_sales=True,can_accept_qr_payments=True))
            s.add(Master(admin_user_id=ident,full_name=f'Synthetic seller {ident}'))
        await s.commit()

@pytest.fixture
async def product(database,sellers):return await add_product(database,stock=5)

async def call(database,fn,*args,actor_id=2,**kw):
    async with database() as s:
        return await fn(s,await s.get(AdminUser,actor_id),*args,**kw)

def payload(product,**changes):
    return CreateCheckout(intent_id=uuid4(),product_id=product.id,quantity=1,expected_amount_minor=10000,currency='UAH',**changes)

async def ready(database,state,view):
    state.update(status='success',checks=[receipt()])
    return await call(database,pos.refresh,view['checkout_id'],RevisionCommand(expected_revision=view['revision']),str(uuid4()))

async def test_same_intent_keys_lost_response_and_distinct_real_intents(database,product,checkout_dependencies,mock_provider):
    body=payload(product);key=str(uuid4())
    first=await call(database,pos.create,body,key)
    for retry in (key,str(uuid4())):
        again=await call(database,pos.create,body,retry)
        assert again['order_id']==first['order_id'] and again['checkout_id']==first['checkout_id']
    found=await call(database,pos.by_intent,str(body.intent_id))
    assert found['order_id']==first['order_id'] and mock_provider['post_calls']==1
    mock_provider['invoice_id']='synthetic_distinct_invoice'
    second=await call(database,pos.create,payload(product),str(uuid4()))
    assert second['order_id']!=first['order_id'] and mock_provider['post_calls']==2
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==2
        assert await s.scalar(select(func.count()).select_from(Order))==2

async def test_concurrent_tabs_same_intent_dispatch_once(database,product,checkout_dependencies,mock_provider):
    body=payload(product)
    results=await asyncio.gather(call(database,pos.create,body,str(uuid4())),call(database,pos.create,body,str(uuid4())),return_exceptions=True)
    assert all(isinstance(r,dict) for r in results),results
    assert len({r['order_id'] for r in results})==1
    assert mock_provider['post_calls']==1

async def test_price_change_rolls_back_all_and_explicit_confirmation(database,product,checkout_dependencies,mock_provider):
    body=payload(product).model_copy(update={'expected_amount_minor':9900})
    key=str(uuid4())
    with pytest.raises(HTTPException) as changed:await call(database,pos.create,body,key)
    assert changed.value.detail['code']=='price_changed'
    assert changed.value.detail['current_offer']['amount_minor']==10000
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(Order))==0
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==0
        assert await s.scalar(select(func.count()).select_from(OrderIntent))==0
        assert (await s.get(Product,product.id)).reserved_quantity==0
    assert mock_provider['post_calls']==0
    accepted=await call(database,pos.create,body.model_copy(update={'expected_amount_minor':10000}),key)
    assert accepted['amount_minor']==10000 and mock_provider['post_calls']==1

async def test_unknown_dispatch_never_reposts_or_clears_on_ui_close(database,product,checkout_dependencies,mock_provider):
    mock_provider['create_error']=500
    body=payload(product);view=await call(database,pos.create,body,str(uuid4()))
    assert view['status']=='requires_review' and view['payment']['qr_payload'] is None
    assert not view['actions']['can_resume'] and not view['actions']['can_start_new']
    again=await call(database,pos.create,body,str(uuid4()))
    assert again['order_id']==view['order_id'] and mock_provider['post_calls']==1
    async with database() as s:
        assert (await s.get(Product,product.id)).reserved_quantity==1
        assert await s.scalar(select(func.count()).select_from(MasterMonoCreateRequest))==1

async def test_paid_pending_receipt_hides_qr_then_handover_once(database,product,checkout_dependencies,mock_provider):
    view=await call(database,pos.create,payload(product),str(uuid4()))
    mock_provider.update(status='success',checks=[])
    paid=await call(database,pos.refresh,view['checkout_id'],RevisionCommand(expected_revision=view['revision']),str(uuid4()))
    assert paid['payment']['status']=='paid' and paid['status']=='paid_receipt_pending'
    assert paid['payment']['qr_payload'] is None and not paid['actions']['can_handover']
    assert not paid['actions']['can_start_new'] and mock_provider['post_calls']==1
    accepted=await ready(database,mock_provider,paid)
    assert accepted['actions']['can_handover'] and accepted['receipt']['status']=='accepted'
    key=str(uuid4());cmd=RevisionCommand(expected_revision=accepted['revision'])
    done=await call(database,pos.handover,accepted['checkout_id'],cmd,key)
    replay=await call(database,pos.handover,accepted['checkout_id'],cmd,key)
    assert done['handover']==replay['handover'] and done['handover']['actor_user_id']==2
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(OrderHandover))==1
        assert (await s.get(Product,product.id)).stock_quantity==4
        assert (await s.get(Product,product.id)).reserved_quantity==0
        row=await s.scalar(select(OrderHandover));assert row.evidence=='pos_customer_handover'
    assert mock_provider['post_calls']==1

async def test_admin_uses_same_real_flow_and_own_attribution(database,product,checkout_dependencies,mock_provider):
    caps=await call(database,pos.capabilities,actor_id=1)
    assert caps['mode']=='mono_checkbox' and caps['can_start']
    view=await call(database,pos.create,payload(product),str(uuid4()),actor_id=1)
    assert view['employee']['user_id']==1 and view['employee']['master_id'] is None
    async with database() as s:
        assert (await s.get(Order,view['order_id'])).created_by_user_id==1
        assert (await s.scalar(select(MasterMonoCreateRequest))).operator_id==1

async def test_permissions_foreign_scope_and_body_conflicts(database,product,checkout_dependencies,mock_provider):
    body=payload(product);key=str(uuid4());view=await call(database,pos.create,body,key)
    with pytest.raises(HTTPException) as foreign:await call(database,pos.state,view['checkout_id'],actor_id=3)
    assert foreign.value.status_code==404
    with pytest.raises(HTTPException):await call(database,pos.create,body.model_copy(update={'intent_id':uuid4()}),key)
    async with database() as s:
        actor=await s.get(AdminUser,2);actor.can_accept_qr_payments=False;await s.commit()
    with pytest.raises(HTTPException) as denied:await call(database,pos.create,body,key)
    assert denied.value.status_code==403 and mock_provider['post_calls']==1

async def test_last_unit_two_actors_one_reservation(database,product,checkout_dependencies,mock_provider):
    async with database() as s:
        row=await s.get(Product,product.id);row.stock_quantity=1;await s.commit()
    async def attempt(actor):
        try:return await call(database,pos.create,payload(product),str(uuid4()),actor_id=actor)
        except HTTPException as error:return error.status_code
    results=await asyncio.gather(attempt(2),attempt(3),return_exceptions=True)
    assert sum(isinstance(i,dict) for i in results)==1 and 409 in results,results
    assert mock_provider['post_calls']==1
    async with database() as s:assert (await s.get(Product,product.id)).reserved_quantity==1

async def test_server_history_status_filter_total_and_read_only_details(database,product,checkout_dependencies,mock_provider):
    first=await call(database,pos.create,payload(product),str(uuid4()))
    paid=await ready(database,mock_provider,first)
    await call(database,pos.handover,paid['checkout_id'],RevisionCommand(expected_revision=paid['revision']),str(uuid4()))
    mock_provider.update(invoice_id='other_pending_fixture',status='created',checks=[])
    await call(database,pos.create,payload(product),str(uuid4()),actor_id=3)
    rows=await call(database,pos.sales,actor_id=1,order_status='completed',page_size=1)
    assert rows['total']==1 and rows['items'][0]['order_id']==first['order_id']
    assert rows['items'][0]['order_status']=='completed' and len(rows['items'][0]['items'])==1
    detail=await call(database,pos.sale_detail,first['order_id'],actor_id=1)
    assert not any(detail['actions'].values())
    with pytest.raises(HTTPException):await call(database,pos.sales,actor_id=2)

async def test_prepared_crash_resumes_same_order_without_duplicate(database,product,checkout_dependencies,mock_provider,monkeypatch):
    original=pos.ensure_started
    async def crash(*args):raise RuntimeError('synthetic process loss after order commit')
    monkeypatch.setattr(pos,'ensure_started',crash)
    body=payload(product);key=str(uuid4())
    with pytest.raises(RuntimeError):await call(database,pos.create,body,key)
    monkeypatch.setattr(pos,'ensure_started',original)
    recovered=await call(database,pos.by_intent,str(body.intent_id))
    assert recovered['status']=='prepared' and recovered['actions']['can_resume']
    assert mock_provider['post_calls']==0
    result=await call(database,pos.resume,recovered['checkout_id'],RevisionCommand(expected_revision=recovered['revision']),key)
    assert result['order_id']==recovered['order_id'] and mock_provider['post_calls']==1
    async with database() as s:assert await s.scalar(select(func.count()).select_from(Order))==1

async def test_unknown_recovery_attaches_existing_invoice_get_only(database,product,checkout_dependencies,mock_provider):
    from app.schemas.master_mono import MasterMonoRecover
    mock_provider['create_error']=500
    view=await call(database,pos.create,payload(product),str(uuid4()))
    command=MasterMonoRecover(expected_revision=view['revision'],invoice_id=mock_provider['invoice_id'])
    async with database() as s:
        with pytest.raises(HTTPException) as rejected:
            await master_mono.recover(s,await s.get(AdminUser,1),view['order_id'],command,str(uuid4()))
        assert rejected.value.detail['code']=='master_mono_recovery_url_required'
    mock_provider['status']='success'
    async with database() as s:
        await master_mono.recover(s,await s.get(AdminUser,1),view['order_id'],command,str(uuid4()))
    recovered=await call(database,pos.state,view['checkout_id'])
    assert recovered['payment']['invoice_id']==mock_provider['invoice_id']
    assert recovered['payment']['qr_payload'] is None and mock_provider['post_calls']==1

async def test_receipt_failure_after_received_payment_never_returns_qr(database,product,checkout_dependencies,mock_provider):
    view=await call(database,pos.create,payload(product),str(uuid4()))
    failed=receipt();failed['status']='failed'
    mock_provider.update(status='success',checks=[failed])
    result=await call(database,pos.refresh,view['checkout_id'],RevisionCommand(expected_revision=view['revision']),str(uuid4()))
    assert result['payment']['status']=='paid' and result['receipt']['status']=='failed'
    assert result['status']=='requires_review' and result['payment']['qr_payload'] is None
    assert not result['actions']['can_handover'] and not result['actions']['can_start_new']
    assert mock_provider['post_calls']==1

async def test_pre_dispatch_revoked_grant_blocks_post_and_preserves_fence(database,product,checkout_dependencies,mock_provider,monkeypatch):
    from app.services.monobank_provider import MonobankGateway
    original=MonobankGateway.readiness
    revoked=False
    async def readiness(gateway):
        nonlocal revoked
        result=await original(gateway)
        if not revoked:
            revoked=True
            async with database() as s:
                actor=await s.get(AdminUser,2);actor.can_accept_qr_payments=False;await s.commit()
        return result
    monkeypatch.setattr(MonobankGateway,'readiness',readiness)
    body=payload(product)
    with pytest.raises(HTTPException) as denied:await call(database,pos.create,body,str(uuid4()))
    assert denied.value.status_code==403 and mock_provider['post_calls']==0
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(MasterMonoCreateRequest))==1
        actor=await s.get(AdminUser,2);actor.can_accept_qr_payments=True;await s.commit()
    retry=await call(database,pos.create,body,str(uuid4()))
    assert retry['status']=='requires_review' and mock_provider['post_calls']==0

async def test_canonical_retail_price_real_promotions_and_volume(database,product,mock_provider):
    from app.models.shop_promotion import ShopPromotion,ShopPromotionDiscountType,ShopPromotionTrigger
    async with database() as s:
        p=await s.get(Product,product.id);p.price=Decimal('10.00');p.shop_price=Decimal('20.00');p.recommended_retail_price=Decimal('99.00');p.volume_ml=100
        promotion=ShopPromotion(name='Synthetic automatic',discount_type=ShopPromotionDiscountType.percent,
            discount_value=Decimal('10'),trigger=ShopPromotionTrigger.automatic,is_active=True,products=[p])
        s.add(promotion);await s.commit()
    offers=await call(database,pos.products,q='')
    offer=next(row for row in offers['items'] if row['id']==product.id)
    assert offer['amount_minor']==1800 and offer['variant_label']=='100 мл'
    body=payload(product).model_copy(update={'expected_amount_minor':1800})
    result=await call(database,pos.create,body,str(uuid4()))
    assert result['amount_minor']==1800 and result['items'][0]['unit_minor']==1800
    assert mock_provider['amount']==1800


async def test_existing_real_multi_item_adoption_is_link_only(database,product,checkout_dependencies,mock_provider,monkeypatch):
    import httpx
    from app.core.config import settings
    from app.schemas.order import POSOrderCreate
    from app.schemas.master_mono import MasterMonoCommand
    from app.schemas.pos import AdoptCheckout
    from app.services import local_sales
    # Intercept only the wire client: retain actual production authority guards/parser.
    transport=master_mono.gateway().transport
    client=httpx.AsyncClient
    factory=mock_provider['gateway_factory']
    def offline_client(*args,**kwargs):
        assert kwargs.get('transport') is None
        return client(*args,**{**kwargs,'transport':transport})
    monkeypatch.setattr(httpx,'AsyncClient',offline_client)
    monkeypatch.setattr(master_mono,'gateway',lambda:factory())
    monkeypatch.setattr(settings,'app_env','production')
    monkeypatch.setattr(settings,'master_monobank_live_transport_enabled',True)
    async with database() as s:
        other=Product(name='Second synthetic product',slug='second-synthetic-pos',price=Decimal('100.00'),stock_quantity=3,reserved_quantity=0,is_active=True)
        s.add(other);await s.commit()
        order=await local_sales.prepare(s,await s.get(AdminUser,2),POSOrderCreate(items=[
            {'product_id':product.id,'quantity':2},{'product_id':other.id,'quantity':1}],simulation=False),str(uuid4()))
        order_id,revision=order.id,order.revision
    await call(database,master_mono.start,order_id,MasterMonoCommand(expected_revision=revision),str(uuid4()))
    before=await call(database,pos.sale_detail,order_id,actor_id=1)
    assert before['source']=='existing_mono_pos' and before['checkout_id'] is None
    assert sorted(i['quantity'] for i in before['items'])==[1,2]
    assert not any(before['actions'].values())
    requests=list(mock_provider['calls'])
    mine = await call(database, pos.my_sales)
    assert mine["total"] == 1
    legacy = mine["items"][0]
    assert legacy["order_id"] == order_id and legacy["source"] == "existing_mono_pos"
    assert legacy["checkout_id"] is legacy["intent_id"] is None
    assert await call(database, pos.my_sales, actor_id=3) == {"items": [], "total": 0, "page": 1, "page_size": 20}
    body=AdoptCheckout(intent_id=uuid4(),order_id=order_id)
    with pytest.raises(HTTPException) as foreign:await call(database,pos.adopt,body,str(uuid4()),actor_id=3)
    assert foreign.value.status_code==404
    linked=await call(database,pos.adopt,body,str(uuid4()))
    replay=await call(database,pos.adopt,body,str(uuid4()))
    assert linked['order_id']==order_id and replay['checkout_id']==linked['checkout_id']
    assert linked['payment']['invoice_id']==mock_provider['invoice_id']
    assert sorted(i['quantity'] for i in linked['items'])==[1,2]
    assert mock_provider['calls']==requests and mock_provider['post_calls']==1
    async with database() as s:
        assert await s.scalar(select(func.count()).select_from(Order))==1
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==1
        assert (await s.get(Product,product.id)).reserved_quantity==2


async def test_admin_quick_sale_existing_poller_and_stale_reversal_block(database,product,checkout_dependencies,mock_provider,monkeypatch):
    from app.core.config import settings
    from app.services import local_sales
    from app.services.mono_checkbox_poll import poll_once
    monkeypatch.setattr(settings,'mono_checkbox_auto_enabled',True)
    first=await call(database,pos.create,payload(product),str(uuid4()),actor_id=1)
    mock_provider.update(status='success',checks=[receipt()])
    assert await poll_once(database)=={'checked':1,'blocked':0}
    accepted=await call(database,pos.state,first['checkout_id'],actor_id=1)
    assert accepted['status']=='ready_to_handover' and accepted['actions']['can_handover']
    class StaleClock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.now(tz)+timedelta(seconds=31)
    monkeypatch.setattr(local_sales,'datetime',StaleClock)
    mock_provider['status']='reversed'
    with pytest.raises(HTTPException):
        await call(database,pos.handover,accepted['checkout_id'],RevisionCommand(expected_revision=accepted['revision']),str(uuid4()),actor_id=1)
    latest=await call(database,pos.state,first['checkout_id'],actor_id=1)
    assert latest['payment']['status']=='reversed' and latest['status']=='requires_review'
    assert not latest['actions']['can_handover'] and latest['payment']['qr_payload'] is None
    async with database() as s:
        assert (await s.get(Product,product.id)).stock_quantity==5
        assert await s.scalar(select(func.count()).select_from(OrderHandover))==0
    assert mock_provider['post_calls']==1


@pytest.mark.parametrize(("mode", "actor_id"), [
    ("canonical", 2), ("opaque", 2), ("opaque", 1), ("foreign_reference", 2),
    ("already_paid", 2), ("untrusted_url", 2), ("short_opaque", 2), ("normalized_path", 2),
    ("missing_url", 2), ("bad_http", 2),
])
async def test_production_wire_checkout_verifies_opaque_url_without_reposting(
    database, product, mock_provider, monkeypatch, caplog, mode, actor_id,
):
    import json
    import logging
    from datetime import datetime, UTC

    import httpx
    from fastapi import FastAPI

    from app.api.v1.routes.pos import router
    from app.core.config import settings
    from app.core.database import get_db_session
    from app.core.logging import MonobankQueryFilter
    from app.core.security import create_access_token
    from app.models.master_mono import MasterMonoBinding
    from app.models.order import OrderItem
    from app.schemas.master_mono import MasterMonoRecover

    invoice_id = "p2_synthetic_provider"
    url = "https://pay.mbnk.biz/" + invoice_id
    private_value = "sensitive-provider-data-never-log"
    opaque_url = "https://pay.mbnk.biz/i/pay?session=" + private_value + "%2Btoken%3D"
    short_url = "https://pay.mbnk.biz/" + private_value
    returned_urls = {
        "canonical": url,
        "untrusted_url": "https://foreign.invalid/" + private_value,
        "short_opaque": short_url,
        "foreign_reference": short_url,
        "normalized_path": "https://pay.mbnk.biz/i/%2e%2e/pay?session=" + private_value,
    }
    post_paths = []
    get_paths = []
    captured = {}
    exercise_recovery_race = mode == "opaque" and actor_id == 2
    async_client = httpx.AsyncClient
    gateway_factory = mock_provider["gateway_factory"]
    # Isolate capture from the application's lazy dictConfig during tests.
    httpx_logger = logging.Logger("httpx", logging.INFO)
    httpx_logger.addFilter(MonobankQueryFilter())
    httpx_logger.addHandler(caplog.handler)
    monkeypatch.setattr(httpx._client, "logger", httpx_logger)
    checkout_logger = logging.Logger(master_mono.__name__, logging.WARNING)
    checkout_logger.addHandler(caplog.handler)
    monkeypatch.setattr(master_mono, "logger", checkout_logger)
    token = settings.master_monobank_token
    monkeypatch.setattr(settings, "shop_monobank_token", token)
    monkeypatch.setattr(settings, "master_monobank_token", None)
    monkeypatch.setattr(settings, "master_mono_fiscal_policy", {"id": "synthetic-policy", "tax_codes": [8], "unit": "шт."})
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "master_monobank_live_transport_enabled", True)

    async def wire(request):
        assert request.url.host == "api.monobank.ua"
        assert request.headers["X-Token"] == token.get_secret_value()
        if request.method == "GET":
            get_paths.append(request.url.path)
            if request.url.path == "/api/merchant/details":
                return httpx.Response(200, json={
                    "merchantId": settings.master_monobank_profile["merchant_id"], "merchantName": private_value,
                })
            assert request.url.path == "/api/merchant/invoice/status"
            assert request.url.params["invoiceId"] == invoice_id
            if exercise_recovery_race and not captured.get("recover_started"):
                captured["recover_started"] = True
                with pytest.raises(HTTPException) as blocked:
                    await call(database, master_mono.recover, captured["order_id"],
                               MasterMonoRecover(expected_revision=2, invoice_id=invoice_id), str(uuid4()), actor_id=1)
                assert blocked.value.detail["code"] == "master_mono_recovery_url_required"
            return httpx.Response(200, json={
                "invoiceId": invoice_id, "reference": "different-reference" if mode == "foreign_reference" else captured["reference"],
                "amount": 54900, "ccy": 980, "finalAmount": 54900,
                "status": "success" if mode == "already_paid" else "created",
                "createdDate": captured["date"], "modifiedDate": datetime.now(UTC).isoformat(),
            })
        assert request.method == "POST" and request.url.path == "/api/merchant/invoice/create"
        post_paths.append(request.url.path)
        body = json.loads(request.content)
        assert body["amount"] == 54900 and body["ccy"] == 980 and body["paymentType"] == "debit"
        captured["reference"] = body["merchantPaymInfo"]["reference"]
        captured["date"] = datetime.now(UTC).isoformat()
        assert body["merchantPaymInfo"]["basketOrder"] == [{
            "name": "Бальзам для бороди Test 100 мл", "qty": 1, "sum": 54900,
            "total": 54900, "code": str(product.id), "unit": "шт.", "tax": [8],
        }]
        # At the actual HTTP boundary the one-shot claim is committed and
        # before_dispatch has rolled back all its business locks.
        async with database() as independent:
            request_row = await independent.scalar(select(MasterMonoCreateRequest))
            assert request_row.reference == captured["reference"] and request_row.authority == "provider"
            captured["order_id"] = request_row.order_id
            assert await independent.scalar(
                select(OrderItem.id).where(OrderItem.order_id == request_row.order_id).with_for_update(nowait=True)
            )
            order = await independent.scalar(select(Order).where(Order.id == request_row.order_id).with_for_update(nowait=True))
            assert order.revision == 2
        if mode == "bad_http":
            return httpx.Response(400, json={"errCode": "BAD_REQUEST", "errText": private_value})
        if mode == "missing_url":
            return httpx.Response(200, json={"invoiceId": invoice_id})
        return httpx.Response(200, json={"invoiceId": invoice_id, "pageUrl": returned_urls.get(mode, opaque_url)})

    def offline_client(*args, **kwargs):
        assert kwargs.get("transport") is None
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False and kwargs["verify"] is True
        return async_client(*args, **{**kwargs, "transport": httpx.MockTransport(wire)})

    monkeypatch.setattr(httpx, "AsyncClient", offline_client)
    monkeypatch.setattr(master_mono, "gateway", lambda: gateway_factory())
    async with database() as session:
        item = await session.get(Product, product.id)
        item.price = Decimal("549")
        item.name = "Бальзам для бороди Test 100 мл"
        await session.commit()

    body = payload(product).model_copy(update={"expected_amount_minor": 54900})
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/backoffice")

    async def dependency():
        async with database() as session:
            yield session

    app.dependency_overrides[get_db_session] = dependency
    auth = {"Authorization": "Bearer " + create_access_token(str(actor_id))}
    path = "/api/v1/backoffice/pos/checkouts"
    async with async_client(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        response = await client.post(path, json=body.model_dump(mode="json"), headers={**auth, "Idempotency-Key": str(uuid4())})
        retry = await client.post(path, json=body.model_dump(mode="json"), headers={**auth, "Idempotency-Key": str(uuid4())})
        assert response.status_code == 201 and retry.status_code == 200
        assert response.headers["Cache-Control"] == retry.headers["Cache-Control"] == "private, no-store"
        first, replay = response.json(), retry.json()
    assert replay["checkout_id"] == first["checkout_id"] and len(post_paths) == 1
    assert first["order_id"] == captured["order_id"]
    url_rejections = {
        "untrusted_url": "unexpected_url_prefix",
        "normalized_path": "dot_segment", "missing_url": "missing_url",
    }
    invalid_url_modes = set(url_rejections)
    failed = mode in {"foreign_reference", "bad_http"} | invalid_url_modes
    expected_url = {"canonical": url, "short_opaque": short_url}.get(mode, opaque_url)
    if failed:
        assert first["payment"]["invoice_id"] is None and first["payment"]["qr_payload"] is None
        assert first["payment"]["status"] == "unknown"
    else:
        assert first["payment"]["invoice_id"] == invoice_id
        assert first["payment"]["qr_payload"] == (None if mode == "already_paid" else expected_url)
        assert first["payment"]["status"] == ("unknown" if mode == "already_paid" else "pending")
    assert first["receipt"]["status"] == "not_requested"
    assert get_paths == ["/api/merchant/details"] + (
        ["/api/merchant/details", "/api/merchant/invoice/status"]
        if mode in {"opaque", "short_opaque", "foreign_reference", "already_paid"} | invalid_url_modes else []
    ) + (["/api/merchant/details", "/api/merchant/invoice/status"] if exercise_recovery_race else [])
    async with database() as session:
        binding = await session.scalar(select(MasterMonoBinding))
        assert (binding is None) == failed
        if binding:
            assert binding.page_url == expected_url and binding.operator_id == actor_id
            if mode != "canonical":
                assert binding.create_proof["returned_page_verified"] is True
                assert binding.create_proof["page_url"] == expected_url and "observed_at" in binding.create_proof
        records = (await session.scalars(select(OrderIntent).where(OrderIntent.order_id == first["order_id"]))).all()
        diagnostics = [
            row.response_json["dispatch_failure"] for row in records
            if row.response_json and "dispatch_failure" in row.response_json
        ]
        if mode == "bad_http":
            assert diagnostics == [{
                "stage": "create_http", "code": "monobank_invoice_create_unknown",
                "http_status": 400, "provider_code": "BAD_REQUEST",
            }]
        elif mode == "foreign_reference":
            assert len(diagnostics) == 1
            assert diagnostics[0]["stage"] == "create_url_verification"
            assert diagnostics[0]["invoice_candidate"] == invoice_id
            assert diagnostics[0]["candidate_identity_verified"] is False
        elif mode in invalid_url_modes:
            assert len(diagnostics) == 1
            assert diagnostics[0]["stage"] == "create_response_url"
            assert diagnostics[0]["invoice_candidate"] == invoice_id
            assert diagnostics[0]["candidate_identity_verified"] is True
            assert diagnostics[0]["url_rejection"] == url_rejections[mode]
            assert f"url_rejection={url_rejections[mode]}" in caplog.text
            if mode == "missing_url":
                assert "url_shape" not in diagnostics[0]
            else:
                known_host = mode == "normalized_path"
                assert diagnostics[0]["url_shape"] == {
                    "length": len(returned_urls[mode]), "scheme": "https", "known_host": known_host,
                    "path_segments": 3 if known_host else 1, "has_query": known_host,
                }
            assert private_value not in json.dumps(diagnostics)
            assert "dispatch_failure" not in first and "dispatch_failure" not in replay
        else:
            assert diagnostics == []
    assert "HTTP Request: POST https://api.monobank.ua/api/merchant/invoice/create" in caplog.text
    assert private_value not in caplog.text and invoice_id not in caplog.text
    assert token.get_secret_value() not in caplog.text

    if mode == "short_opaque":
        from alembic import command
        from alembic.config import Config

        with pytest.raises(RuntimeError, match="short POS payment routes exist"):
            await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "0148_master_mono_returned_url")
        latest = await call(database, pos.state, first["checkout_id"], actor_id=actor_id)
        assert latest["payment"]["qr_payload"] == short_url

    if mode == "opaque":
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.exc import DBAPIError

        from app.services.order_documents import digest

        async with database() as session:
            bound = await session.scalar(select(MasterMonoBinding))
            request = await session.get(MasterMonoCreateRequest, bound.create_request_id)
            forged = {**bound.create_proof, "observed_at": None, "created_at": None}
            forged["proof_hash"] = digest({key: value for key, value in forged.items() if key != "proof_hash"})
            session.add(master_mono.binding_for(request, forged))
            with pytest.raises(DBAPIError, match="Live master invoice requires exact immutable dispatch"):
                await session.flush()
            await session.rollback()

        with pytest.raises(RuntimeError, match="populated ledger cannot downgrade"):
            await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "0147_pos_checkout_intents")
        latest = await call(database, pos.state, first["checkout_id"], actor_id=actor_id)
        assert latest["payment"]["qr_payload"] == opaque_url


async def test_opaque_url_browser_parity_in_gateway_and_ledger(database):
    from sqlalchemy import text

    from app.services.monobank_provider import MonobankError, master_returned_page_url

    cases = [
        ("/i/pay?session=synthetic%2Btoken%3D", True),
        ("/synthetic_invoice?session=opaque%27data", True),
        ("/?route=opaque", True),
        ("/i/opaque'path?session=token%27data", True),
        ("/i/%2e%2e/pay?session=synthetic", False),
        ("/i/.%2E/pay", False),
        ("/i/./pay", False),
        ("/i/pay?session=bad%zz", False),
        ("/i/pay?session=bad%", False),
        ("/i/pay?session=raw'apostrophe", False),
        ("/i/pay?session=raw-юнікод", False),
        ("/i/{path}?session=token", False),
        ("/i/a^b?session=token", False),
        ("/i/a|b?session=token", False),
        ("/i/a%5Eb%7C?session=token", True),
        ("/i/pay?session=token%5c", False),
        ("/i/pay?session=token%0a", False),
        ("/", False),
        ("/different-opaque-route?session=synthetic", True),
    ]
    sql = text("""
        SELECT master_mono_returned_url_valid(:invoice, :url, jsonb_build_object(
            'returned_page_verified', true, 'page_url', CAST(:url AS text),
            'created_at', '2026-11-08T12:00:00Z', 'observed_at', '2026-11-08T12:00:01Z'
        ))
    """)
    async with database() as session:
        for suffix, accepted in cases:
            raw = "https://pay.mbnk.biz" + suffix
            if accepted:
                assert master_returned_page_url("synthetic_invoice", raw) == raw
            else:
                with pytest.raises(MonobankError, match="monobank_proof_invalid"):
                    master_returned_page_url("synthetic_invoice", raw)
            assert await session.scalar(sql, {"invoice": "synthetic_invoice", "url": raw}) is accepted, suffix

        status_only = text("""
            SELECT master_mono_returned_url_valid('synthetic_invoice', :url, jsonb_build_object(
                'status', CAST(:status AS text), 'has_cancellations', CAST(:cancelled AS boolean),
                'created_at', '2026-11-08T12:00:00Z', 'observed_at', '2026-11-08T12:00:01Z'
            ))
        """)
        for state, cancelled, accepted in [("created", False, False), ("created", True, True), ("success", False, True)]:
            assert await session.scalar(status_only, {"url": None, "status": state, "cancelled": cancelled}) is accepted
        canonical = "https://pay.mbnk.biz/synthetic_invoice"
        assert await session.scalar(status_only, {"url": canonical, "status": "created", "cancelled": False}) is False
        assert await session.scalar(sql, {"invoice": "synthetic_invoice", "url": canonical}) is True
