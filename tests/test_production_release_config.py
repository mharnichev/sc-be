"""Offline candidate checks: synthetic values, no live credentials or startup."""
import pytest
from pydantic import ValidationError
from app.core.config import Settings


def production(**extra):
    return Settings(_env_file=None, APP_ENV='production', DEBUG=False,
                    SECRET_KEY='synthetic-r5-config-only-secret-at-least-32',
                    **{'CORS_ORIGINS':'https://soulcuts.com.ua,https://admin.soulcuts.com.ua', **extra})


@pytest.mark.parametrize('origins', ['*', 'https://*.example.com', 'http://example.com',
    'https://[::1]', 'https://127.0.0.2', 'https://0.0.0.0', 'https://foo.localhost',
    'https://user:password@example.com', 'https://example.com/path',
    'https://example.com?key=value', 'https://example.com#fragment',
    'https://example.com:invalid', 'https://example.com:0', 'https://', ''])
def test_production_rejects_non_origin_or_insecure_cors(origins):
    with pytest.raises(ValidationError):
        production(CORS_ORIGINS=origins)


def test_synthetic_closed_production_candidate_has_no_provider_activation():
    candidate = production()
    for name in ('shop_checkout_enabled', 'shop_order_operations_enabled',
                 'shop_payments_enabled','shop_checkbox_enabled','shop_np_returns_enabled',
                 'shop_payment_resolutions_enabled','shop_monobank_live_transport_enabled',
                 'shop_checkbox_live_transport_enabled','shop_np_return_live_transport_enabled',
                 'provider_sandbox_enabled','sandbox_provider_network_enabled',
                 'shop_fiscal_mock_enabled','shop_carrier_returns_mock_enabled',
                 'checkbox_mock_enabled','np_mock_shipments_enabled','monobank_qr_mock_enabled'):
        assert getattr(candidate,name) is False, name
    assert candidate.shop_payment_resolution_manual_policy is None
    assert candidate.shop_monobank_profile is None
    assert candidate.shop_checkbox_profile is None
    assert candidate.shop_np_return_profile is None


def test_explicit_https_origin_port_is_supported():
    assert production(CORS_ORIGINS='https://api.example.com:8443').cors_origins == ['https://api.example.com:8443']
