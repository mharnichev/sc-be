from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import customer_tracking as ct
from app.schemas.shipment import RealShipmentAttach


class Session:
    def __init__(self, order=None, rows=()):
        self.order, self.rows, self.added = order, rows, []
        self.statements = []

    async def scalar(self, query):
        self.statements.append(query)
        if len(self.statements) == 1:
            return self.order
        return None

    async def scalars(self, query):
        self.statements.append(query)
        return SimpleNamespace(all=lambda: self.rows)


def shipment(**kw):
    values = dict(id=1, tracking_number='20450000000001', simulation=False, carrier='nova_poshta',
        status='requires_review', tracking_status='delivered', raw_status='9', last_checked_at=datetime.now(UTC),
        provider_event_at=datetime.now(UTC), last_attempt_status='success', return_of_shipment_id=None,
        shipment_items=[], tracking_events=[], next_poll_at=None)
    return SimpleNamespace(**(values | kw))


@pytest.mark.anyio
async def test_owner_queue_foreign_and_cooldown(monkeypatch):
    monkeypatch.setattr(ct.settings, 'np_tracking_enabled', True)
    monkeypatch.setattr(ct.settings, 'nova_poshta_api_key', 'FIXTURE-SECRET')
    session = Session()
    with pytest.raises(HTTPException) as error:
        await ct.queue_refresh(session, 9, 7)
    assert error.value.status_code == 404
    assert 'orders.customer_id = ' in str(session.statements[0])
    row = shipment(next_poll_at=datetime.now(UTC) + timedelta(hours=1))
    session = Session(SimpleNamespace(id=9), [row])
    response = await ct.queue_refresh(session, 9, 7)
    assert response['status'] == 'cooldown' and response['retry_after_seconds'] > 3500
    row.next_poll_at = None
    assert (await ct.queue_refresh(Session(SimpleNamespace(id=9), [row]), 9, 7))['status'] == 'queued'


def test_safe_projection_never_infers_legacy_receipt_or_item_delivery():
    row = shipment(tracking_status=None, last_checked_at=None)
    row.provider_payload = {'token': 'private', 'PhoneRecipient': 'private'}
    row.address_snapshot = {'private': 'private'}
    result = ct.project(row, detail=True)
    assert result.delivery_status == 'requires_review' and result.stale
    assert result.items is None and result.events == []
    assert 'private' not in result.model_dump_json()
    row.tracking_status = 'delivered'
    row.last_checked_at = datetime.now(UTC)
    row.last_attempt_status = 'timeout'
    result = ct.project(row)
    assert result.delivery_status == 'delivered' and result.stale and result.last_attempt_status == 'error'


@pytest.mark.anyio
async def test_return_allocation_is_bounded_to_parent(monkeypatch):
    parent = shipment(shipment_items=[SimpleNamespace(order_item_id=11, quantity=1)], active=True)
    order = SimpleNamespace(id=9, revision=1, simulation=False, fulfillment_method='nova_poshta',
        shipments=[parent], items=[SimpleNamespace(id=11, quantity=10)])
    async def load(*args, **kwargs):
        return order
    from app.services import local_sales
    monkeypatch.setattr(local_sales, 'load', load)
    actor = SimpleNamespace(is_superuser=True)
    # attach uses scalar only for duplicate lookup, return None.
    session = Session()
    payload = RealShipmentAttach(expected_revision=1, tracking_number='20450000000002',
        return_of_shipment_id=1, items=[{'order_item_id': 11, 'quantity': 2}])
    with pytest.raises(HTTPException) as error:
        await ct.attach_existing(session, actor, 9, payload)
    assert error.value.status_code == 422


@pytest.mark.anyio
async def test_refresh_disabled_without_credentials_after_owner_check(monkeypatch):
    monkeypatch.setattr(ct.settings, 'np_tracking_enabled', True)
    monkeypatch.setattr(ct.settings, 'nova_poshta_api_key', None)
    response = await ct.queue_refresh(Session(SimpleNamespace(id=9)), 9, 7)
    assert response['status'] == 'disabled'
