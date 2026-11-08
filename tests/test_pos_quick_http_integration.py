"""Actual FastAPI wire/auth/strict DTO/privacy against disposable migrated PG."""
import asyncio
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select,text,func
from sqlalchemy.exc import DBAPIError
from app.api.v1.routes.master_mono import backoffice_router as master_mono_router
from app.api.v1.routes.pos import router
from app.core.database import get_db_session
from app.core.security import create_access_token
from app.models.admin_user import AdminUser
from app.models.brand import Brand
from app.models.product import Product
from app.models.pos import POSCheckoutIntent
from app.models.order import Order
from tests.test_master_mono_integration import receipt
from tests.test_pos_quick_checkout import database,template_database,checkout_dependencies,mock_provider,sellers,product,call,payload,ready
from app.services import pos

pytestmark=pytest.mark.anyio
@pytest.fixture(scope='module')
def anyio_backend():return 'asyncio'

@pytest.fixture
async def client(database):
    app=FastAPI();app.include_router(router,prefix='/api/v1/backoffice')
    app.include_router(master_mono_router, prefix='/api/v1/backoffice')
    async def dependency():
        async with database() as session:yield session
    app.dependency_overrides[get_db_session]=dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local.test') as c:yield c

def auth(actor=2,**extras):return {'Authorization':'Bearer '+create_access_token(str(actor)),**extras}
BASE='/api/v1/backoffice/pos'

async def test_full_wire_and_forbidden_input(client,database,product,checkout_dependencies,mock_provider):
    assert (await client.get(BASE+'/capabilities')).status_code==401
    for actor in (1,2):
        cap=await client.get(BASE+'/capabilities',headers=auth(actor));assert cap.status_code==200 and cap.json()['mode']=='mono_checkbox'
    body=payload(product).model_dump(mode='json');headers=auth(**{'Idempotency-Key':str(uuid4())})
    for extra in ({'actor_id':1},{'simulation':True},{'customer_name':'extra'},{'quantity':True}):
        denied=await client.post(BASE+'/checkouts',json={**body,**extra},headers=headers)
        assert denied.status_code==422 and denied.json()=={'detail':{'code':'invalid_command'}}
    made=await client.post(BASE+'/checkouts',json=body,headers=headers)
    assert made.status_code==201,made.text
    retried=await client.post(BASE+'/checkouts',json=body,headers=headers)
    assert retried.status_code==200 and retried.json()['order_id']==made.json()['order_id']
    view=made.json();assert len(view['items'])==1 and view['items'][0]['quantity']==1
    assert made.headers['Cache-Control']=='private, no-store'
    assert (await client.get(BASE+'/checkouts/'+view['checkout_id'],headers=auth(3))).status_code==404
    recovered=await client.get(BASE+'/checkouts/by-intent/'+body['intent_id'],headers=auth())
    assert recovered.json()['order_id']==view['order_id']
    invalid=await client.post(BASE+'/checkouts/'+view['checkout_id']+'/handover',json={'expected_revision':view['revision'],'reason':'sensitive-reason'},headers=headers)
    assert invalid.status_code==422 and 'sensitive' not in invalid.text
    assert mock_provider['post_calls']==1

async def test_search_brand_sku_barcode_literal_and_server_price(client,database,product,checkout_dependencies,mock_provider):
    async with database() as s:
        brand=Brand(name='SyntheticBrand',slug=uuid4().hex);s.add(brand);await s.flush()
        p=await s.get(Product,product.id);p.brand_id=brand.id;p.sku='POS_EXACT_17';p.barcode='0000000170001';p.package_size='100 ml';p.image_url='/media/products/synthetic.webp';await s.commit()
    for term in ('SyntheticBrand','POS_EXACT_17','0000000170001'):
        result=await client.get(BASE+'/products',params={'q':term},headers=auth())
        assert result.status_code==200 and result.json()['total']==1,result.text
        item=result.json()['items'][0]
        assert item['amount_minor']==10000 and item['barcode']=='0000000170001' and item['variant_label']=='100 ml'
        assert item['image_url'] and 'effective_shop_price' not in item
    wildcard=await client.get(BASE+'/products',params={'q':'%'},headers=auth())
    assert wildcard.json()['total']==0

async def test_database_identity_immutable_and_source_not_editable(database,product,checkout_dependencies,mock_provider):
    view=await call(database,pos.create,payload(product),str(uuid4()))
    async with database() as s:
        for statement in ('UPDATE pos_checkout_intents SET actor_id=1','DELETE FROM pos_checkout_intents'):
            with pytest.raises(DBAPIError):await s.execute(text(statement))
            await s.rollback()
        assert await s.scalar(select(func.count()).select_from(POSCheckoutIntent))==1
        assert (await s.get(Order,view['order_id'])).created_by_user_id==2

async def test_filter_wire_counts_and_date_validation(client,database,product,checkout_dependencies,mock_provider):
    await call(database,pos.create,payload(product),str(uuid4()))
    rows=await client.get(BASE+'/sales',params={'order_status':'pending','page_size':1,'employee_user_id':2},headers=auth(1))
    assert rows.status_code==200 and rows.json()['total']==1 and rows.json()['items'][0]['order_status']=='pending',rows.text
    assert (await client.get(BASE+'/sales',params={'order_status':'ready_to_handover'},headers=auth(1))).status_code==422
    invalid=await client.get(BASE+'/sales',params={'date_from':'2026-11-10','date_to':'2026-11-01'},headers=auth(1))
    assert invalid.status_code==422
    assert (await client.get(BASE+'/sales',headers=auth())).status_code==403


@pytest.mark.parametrize("owner, cancelled", [(1, False), (2, False), (2, True)])
async def test_manager_recovers_existing_invoice_from_sale_detail(
    client, database, product, checkout_dependencies, mock_provider, owner, cancelled, monkeypatch,
):
    from app.core.config import settings
    from app.services.mono_checkbox_poll import poll_once
    mock_provider["create_error"] = 500
    made = await client.post(
        BASE + "/checkouts", json=payload(product).model_dump(mode="json"),
        headers=auth(owner, **{"Idempotency-Key": str(uuid4())}),
    )
    assert made.status_code == 201, made.text
    sale = made.json()
    assert sale["payment"]["status"] == "unknown" and sale["payment"]["qr_payload"] is None
    assert sale["actions"]["can_recover_invoice"] is (owner == 1)
    if owner == 2:
        assert sale["invoice_recovery"] is None
        assert (await client.get(BASE + "/checkouts/" + sale["checkout_id"], headers=auth(1))).status_code == 404

    history_url = BASE + "/sales/" + str(sale["order_id"])
    assert (await client.get(history_url, headers=auth(2))).status_code == 403
    before_reads = list(mock_provider["calls"])
    detail = await client.get(history_url, headers=auth(1))
    assert detail.status_code == 200 and detail.headers["Cache-Control"] == "private, no-store"
    history = detail.json()
    assert mock_provider["calls"] == before_reads
    assert not any(history["actions"].values())
    recovery = history["invoice_recovery"]
    assert set(recovery) == {"reference", "requested_at", "can_attach"}
    assert recovery["can_attach"] and recovery["reference"] == mock_provider["reference"]
    assert datetime.fromisoformat(recovery["requested_at"]).tzinfo is not None

    # A paid or cancelled invoice must never be offered for payment after attachment.
    mock_provider["status"] = "created" if cancelled else "success"
    if cancelled:
        mock_provider["cancellations"] = [{"status": "success"}]
    recovery_url = f"/api/v1/backoffice/master-mono/orders/{sale['order_id']}/recover-invoice"
    command = {"expected_revision": history["revision"], "invoice_id": mock_provider["invoice_id"]}
    key = str(uuid4())
    denied = await client.post(recovery_url, json=command, headers=auth(2, **{"Idempotency-Key": key}))
    assert denied.status_code == 403 and mock_provider["calls"] == before_reads
    attached, concurrent = await asyncio.gather(*(
        client.post(recovery_url, json=command, headers=auth(1, **{"Idempotency-Key": key}))
        for _ in range(2)
    ))
    assert attached.status_code == concurrent.status_code == 200, (attached.text, concurrent.text)
    assert attached.headers["Cache-Control"] == concurrent.headers["Cache-Control"] == "private, no-store"
    assert attached.json()["revision"] == concurrent.json()["revision"] == history["revision"] + 1
    assert not attached.json()["paid"] and attached.json()["payment_url"] is None
    assert mock_provider["post_calls"] == 1
    assert all(method == "GET" for method, _ in mock_provider["calls"][len(before_reads):])
    current = (await client.get(history_url, headers=auth(1))).json()
    assert current["invoice_recovery"] is None and not any(current["actions"].values())
    assert current["employee"]["user_id"] == owner
    assert current["payment"]["status"] == "unknown" and current["receipt"]["status"] == "not_requested"
    own = (await client.get(BASE + "/checkouts/" + sale["checkout_id"], headers=auth(owner))).json()
    assert own["actions"]["can_refresh"] and own["payment"]["qr_payload"] is None
    replay = await client.post(recovery_url, json=command, headers=auth(1, **{"Idempotency-Key": key}))
    assert replay.status_code == 200 and replay.json()["revision"] == current["revision"]
    conflict = await client.post(
        recovery_url, json={**command, "expected_revision": current["revision"]},
        headers=auth(1, **{"Idempotency-Key": key}),
    )
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    if cancelled:
        assert mock_provider["post_calls"] == 1
        return

    monkeypatch.setattr(settings, "mono_checkbox_auto_enabled", True)
    mock_provider["checks"] = [receipt()]
    assert await poll_once(database) == {"checked": 1, "blocked": 0}
    verified = (await client.get(history_url, headers=auth(1))).json()
    assert verified["payment"]["status"] == "paid" and verified["receipt"]["status"] == "accepted"
    assert not any(verified["actions"].values())
    assert mock_provider["post_calls"] == 1


async def test_recovery_metadata_respects_existing_profile_guard(client, product, checkout_dependencies, mock_provider, monkeypatch):
    from app.core.config import settings

    mock_provider["create_error"] = 500
    response = await client.post(
        BASE + "/checkouts", json=payload(product).model_dump(mode="json"),
        headers=auth(1, **{"Idempotency-Key": str(uuid4())}),
    )
    assert response.status_code == 201
    sale = response.json()
    detail_url = BASE + "/sales/" + str(sale["order_id"])
    monkeypatch.setattr(settings, "master_mono_invoice_create_enabled", False)
    assert (await client.get(detail_url, headers=auth(1))).json()["invoice_recovery"]["can_attach"]
    monkeypatch.setattr(settings, "master_mono_checkbox_link_confirmed", False)
    detail = (await client.get(detail_url, headers=auth(1))).json()
    assert not detail["invoice_recovery"]["can_attach"]
    assert "master_mono_disabled" in detail["blocking_reasons"]
    own = (await client.get(BASE + "/checkouts/" + sale["checkout_id"], headers=auth(1))).json()
    assert not own["actions"]["can_recover_invoice"]
    assert mock_provider["post_calls"] == 1



@pytest.mark.parametrize("failure, status, code", [
    ("monobank_unavailable", 503, "master_mono_recovery_unavailable"),
    ("monobank_proof_invalid", 409, "master_mono_recovery_mismatch"),
    ("monobank_not_found", 404, "master_mono_recovery_invoice_not_found"),
])
async def test_recovery_provider_failure_preserves_unbound_sale(
    client, product, checkout_dependencies, mock_provider, monkeypatch, failure, status, code,
):
    from app.services.monobank_provider import MonobankError, MonobankGateway

    mock_provider["create_error"] = 500
    made = await client.post(
        BASE + "/checkouts", json=payload(product).model_dump(mode="json"),
        headers=auth(1, **{"Idempotency-Key": str(uuid4())}),
    )
    assert made.status_code == 201
    sale = made.json()

    async def inspect_fails(self, invoice_id):
        raise MonobankError(failure)

    monkeypatch.setattr(MonobankGateway, "inspect_invoice", inspect_fails)
    result = await client.post(
        f"/api/v1/backoffice/master-mono/orders/{sale['order_id']}/recover-invoice",
        json={"expected_revision": sale["revision"], "invoice_id": mock_provider["invoice_id"]},
        headers=auth(1, **{"Idempotency-Key": str(uuid4())}),
    )
    assert result.status_code == status and result.json()["detail"]["code"] == code
    detail = (await client.get(BASE + "/sales/" + str(sale["order_id"]), headers=auth(1))).json()
    assert detail["revision"] == sale["revision"] and detail["invoice_recovery"]["can_attach"]
    assert detail["payment"]["invoice_id"] is None and mock_provider["post_calls"] == 1


@pytest.mark.parametrize("actor", [1, 2])
async def test_unknown_sale_remains_discoverable_during_another_explicit_sale(
    client, database, product, checkout_dependencies, mock_provider, actor,
):
    from app.models.master_mono import MasterMonoBinding, MasterMonoCreateRequest

    async with database() as session:
        item = await session.get(Product, product.id)
        item.stock_quantity = 2
        await session.commit()

    async def create(body, key):
        return await client.post(
            BASE + "/checkouts", json=body, headers=auth(actor, **{"Idempotency-Key": key}),
        )

    async def read(path, **params):
        response = await client.get(BASE + path, headers=auth(actor), params=params)
        assert response.status_code == 200, response.text
        return response.json()

    original_body, original_key = payload(product).model_dump(mode="json"), str(uuid4())
    mock_provider["create_error"] = 500
    original = await create(original_body, original_key)
    assert original.status_code == 201, original.text
    parked = original.json()
    parked_path = "/checkouts/" + parked["checkout_id"]
    intent_path = "/checkouts/by-intent/" + original_body["intent_id"]
    assert parked["payment"]["status"] == "unknown" and parked["payment"]["invoice_id"] is None
    assert parked["payment"]["qr_payload"] is None and "creation_unknown" in parked["blocking_reasons"]
    assert not parked["actions"]["can_start_new"] and not parked["actions"]["can_resume"]

    async def original_facts():
        async with database() as session:
            request = await session.scalar(select(MasterMonoCreateRequest).where(
                MasterMonoCreateRequest.order_id == parked["order_id"],
            ))
            root = await session.get(POSCheckoutIntent, parked["checkout_id"])
            order = await session.get(Order, parked["order_id"])
            return (request.id, request.reference, request.operator_id, root.intent_id,
                    order.created_by_user_id, order.revision, order.status)

    saved_facts = await original_facts()
    calls_before_navigation = list(mock_provider["calls"])
    # Independent HTTP reads model repeated selection and reload without any command.
    first_open, reloaded, active_before = await asyncio.gather(
        read(parked_path), read(intent_path), read("/checkouts/active"),
    )
    assert first_open == reloaded == parked
    assert active_before["total"] == 1 and active_before["items"] == [parked]
    assert mock_provider["calls"] == calls_before_navigation

    # Two explicit new intents compete for the unit left after the first reserve.
    mock_provider.update(create_error=None, invoice_id="synthetic_distinct_sale")
    one, two = await asyncio.gather(
        create(payload(product).model_dump(mode="json"), str(uuid4())),
        create(payload(product).model_dump(mode="json"), str(uuid4())),
    )
    assert sorted([one.status_code, two.status_code]) == [201, 409], (one.text, two.text)
    winner, loser = (one, two) if one.status_code == 201 else (two, one)
    assert loser.json()["detail"]["code"] == "out_of_stock"
    other = winner.json()
    assert other["order_id"] != parked["order_id"] and other["intent_id"] != parked["intent_id"]
    assert other["payment"]["invoice_id"] == "synthetic_distinct_sale"
    assert mock_provider["post_calls"] == 2

    before_reads = list(mock_provider["calls"])
    front, back = await asyncio.gather(
        read("/checkouts/active", page=1, page_size=1),
        read("/checkouts/active", page=2, page_size=1),
    )
    assert front["total"] == back["total"] == 2
    assert [front["items"][0]["checkout_id"], back["items"][0]["checkout_id"]] == [
        other["checkout_id"], parked["checkout_id"],
    ]
    assert await read(parked_path) == await read(intent_path) == parked
    assert (await client.get(BASE + parked_path, headers=auth(3))).status_code == 404
    replay = await create(original_body, original_key)
    assert replay.status_code == 200 and replay.json() == parked
    assert mock_provider["calls"] == before_reads

    async def command(operation, revision):
        return await client.post(
            BASE + "/checkouts/" + other["checkout_id"] + "/" + operation,
            json={"expected_revision": revision}, headers=auth(actor, **{"Idempotency-Key": str(uuid4())}),
        )

    mock_provider.update(status="success", checks=[receipt()])
    paid = await command("refresh", other["revision"])
    assert paid.status_code == 200 and paid.json()["actions"]["can_handover"]
    completed = await command("handover", paid.json()["revision"])
    assert completed.status_code == 200 and completed.json()["actions"]["can_start_new"]
    assert (await read("/checkouts/active"))["items"] == [parked]
    assert await original_facts() == saved_facts
    async with database() as session:
        item = await session.get(Product, product.id)
        assert (item.stock_quantity, item.reserved_quantity) == (1, 1)
        assert await session.scalar(select(func.count()).select_from(MasterMonoCreateRequest)) == 2
        assert await session.scalar(select(func.count()).select_from(MasterMonoBinding)) == 1
    assert mock_provider["post_calls"] == 2
