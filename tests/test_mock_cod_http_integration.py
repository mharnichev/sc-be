"""Real JWT/HTTP boundaries for local mock carrier settlement."""
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select

from app.api.v1.routes import orders
from app.core.database import get_db_session
from app.core.security import create_access_token
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.mock_cod import MockCODConfirmation
from tests.test_inventory_integration import anyio_backend, database, checkout_dependencies
from tests.test_mock_cod_integration import enable_mock, delivered, cod_payload
from tests.test_shop_checkout_http_integration import local_network_only

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures('local_network_only')]


async def test_cod_http_jwt_permissions_validation_and_exact_replay(database, checkout_dependencies):
    order, sid, _ = await delivered(database, checkout_dependencies)
    async with database() as session:
        session.add_all([
            AdminUser(id=2, email='cod-reader@example.test', hashed_password='unused', is_active=True),
            AdminUser(id=3, email='cod-master@example.test', hashed_password='unused', is_active=True,
                can_record_sales=True, can_post_inventory=True, can_manage_shipments=True),
        ])
        await session.flush()
        session.add(Master(full_name='Isolated master', admin_user_id=3))
        await session.commit()
    app = FastAPI()
    app.include_router(orders.backoffice_router, prefix='/orders')
    async def sessions():
        async with database() as session:
            yield session
    app.dependency_overrides[get_db_session] = sessions
    def auth(user):
        return {'Authorization': 'Bearer '+create_access_token(str(user))}
    path = f'/orders/{order.id}/shipments/{sid}'
    body = cod_payload(order).model_dump(mode='json')
    key = str(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://mock.test') as client:
        assert (await client.get(path+'/settlement')).status_code == 401
        for denied in (2, 3):
            assert (await client.get(path+'/settlement', headers=auth(denied))).status_code == 403
            assert (await client.post(path+'/cod-payment', json=body,
                headers={**auth(denied), 'Idempotency-Key': str(uuid4())})).status_code == 403
        summary = await client.get(path+'/settlement', headers=auth(1))
        assert summary.status_code == 200 and summary.json()['amount_minor'] == 10000
        for invalid in ({**body, 'amount_minor': 1}, {**body, 'confirm': False}):
            assert (await client.post(path+'/cod-payment', json=invalid,
                headers={**auth(1), 'Idempotency-Key': str(uuid4())})).status_code == 422
        response = await client.post(path+'/cod-payment', json=body, headers={**auth(1), 'Idempotency-Key': key})
        assert response.status_code == 200, response.text
        result = response.json()
        replay = await client.post(path+'/cod-payment', json=body, headers={**auth(1), 'Idempotency-Key': key})
        assert replay.status_code == 200 and replay.json() == result
        duplicate = await client.post(path+'/cod-payment', json={**body, 'expected_revision': result['revision']},
            headers={**auth(1), 'Idempotency-Key': str(uuid4())})
        assert duplicate.status_code == 409 and duplicate.json()['detail']['code'] == 'cod_payment_exists'
        assert (await client.get(f'/orders/{order.id}/shipments/{sid+999}/settlement', headers=auth(1))).status_code == 404
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(MockCODConfirmation)) == 1


@pytest.mark.parametrize('item_only', [False, True])
async def test_historical_manual_return_can_replay_but_not_create_new_credit(database, checkout_dependencies, item_only):
    from app.models.inventory import InventoryMovement, MovementType
    from app.models.product import Product
    from app.services.inventory import InventoryService
    order, _, pid = await delivered(database, checkout_dependencies)
    key = 'historical-'+uuid4().hex
    link = None if item_only else order.id
    async with database() as session:
        # Immutable fact recorded by the pre-TTN workflow. The new guard must
        # preserve recovery of this old command without accepting another credit.
        movement = InventoryMovement(product_id=pid, movement_type=MovementType.customer_return,
            quantity=1, on_hand_delta=1, reserved_delta=0, order_id=link, order_item_id=order.items[0].id,
            reason='Historical physical receipt', performed_by_user_id=1, reference_key=key)
        session.add(movement)
        (await session.get(Product, pid)).stock_quantity += 1
        await session.commit()
        previous_id = movement.id
    async with database() as session:
        request = dict(movement_type=MovementType.customer_return, product_id=pid, quantity=1,
            reason='Historical physical receipt', admin_user_id=1, order_id=link, order_item_id=order.items[0].id)
        recovered = await InventoryService().manual_operation(session, idempotency_key=key, **request)
        assert recovered.id == previous_id
        with pytest.raises(HTTPException) as denied:
            await InventoryService().manual_operation(session, idempotency_key='new-'+uuid4().hex, **request)
        assert denied.value.status_code == 409
        assert (await session.get(Product, pid)).stock_quantity == 5
