from datetime import UTC, datetime
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from app.core.config import settings
from app.models.order import Order, OrderStatus
from app.models.shipment import Shipment
from app.schemas.shipment import ShipmentCreate, ShipmentCommand, ShipmentAddress
from app.services import shipments


@pytest.mark.parametrize('env,flag,url,expected', [
    ('test', True, 'postgresql://x@127.0.0.1/sc_stage3_test', True),
    ('local', True, 'postgresql://x@localhost/local_test', True),
    ('production', True, 'postgresql://x@127.0.0.1/local_test', False),
    ('test', False, 'postgresql://x@127.0.0.1/local_test', False),
    ('test', True, 'postgresql://x@remote.test/local_test', False),
    ('test', True, 'postgresql://x@127.0.0.1/production', False),
    ('test', True, 'sqlite:///local_test', False),
])
def test_mock_guard_uses_actual_session_database(monkeypatch, env, flag, url, expected):
    monkeypatch.setattr(settings, 'app_env', env)
    monkeypatch.setattr(settings, 'np_mock_shipments_enabled', flag)
    session = SimpleNamespace(bind=SimpleNamespace(url=make_url(url)))
    assert shipments.available(session) is expected


@pytest.mark.parametrize('amount', [True, 1.5, '100', -1, 10000000000])
def test_cod_strict_minor_units(amount):
    with pytest.raises(ValidationError):
        ShipmentCreate(expected_revision=1, packaging={'weight_kg': .5, 'length_cm': 10,
            'width_cm': 10, 'height_cm': 10, 'places': 1, 'payer': 'sender',
            'declared_value_minor': 100, 'cod_amount_minor': amount})


@pytest.mark.parametrize('weight', [float('inf'), float('nan'), 0, -1])
def test_weight_finite_positive(weight):
    with pytest.raises(ValidationError):
        ShipmentCreate(expected_revision=1, packaging={'weight_kg': weight, 'length_cm': 10,
            'width_cm': 10, 'height_cm': 10, 'places': 1, 'payer': 'sender',
            'declared_value_minor': 100, 'cod_amount_minor': 0})


def test_legacy_payment_unknown_and_known_cod_handover_unpaid():
    order = Order(channel='shop', status=OrderStatus.paid, simulation=False)
    order.payments, order.shipments = [], []
    assert order.payment_status == 'legacy_unknown'
    order.status = OrderStatus.completed
    assert order.payment_status == 'legacy_unknown'
    order.shipments = [Shipment(simulation=True, handed_over_at=datetime.now(UTC))]
    assert order.payment_status == 'unpaid'
    order.channel = None
    assert order.payment_status == 'legacy_unknown'


def test_unverified_numeric_tracking_and_incomplete_address_denied():
    with pytest.raises(ValidationError):
        ShipmentCreate(expected_revision=1, source='attach', tracking_number='20450000000000', packaging={})
    with pytest.raises(ValidationError):
        ShipmentAddress(shippingCityRef='11111111-1111-4111-8111-111111111111', cityLabel='City')
