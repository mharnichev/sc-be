"""Synthetic provider contracts: MockTransport only; no dotenv, DB or internet."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from app.services.sandbox_providers import SandboxProviderError, SandboxProviders

REGISTER = '11111111-1111-4111-8111-111111111111'
CASHIER = '22222222-2222-4222-8222-222222222222'
ORG = '33333333-3333-4333-8333-333333333333'
SHIFT = '44444444-4444-4444-8444-444444444444'
RECEIPT = '55555555-5555-4555-8555-555555555555'
REFERENCE = '66666666-6666-4666-8666-666666666666'
TOKEN = 'synthetic-test-token'
INVOICE = 'p2_testInvoice'


def configuration(**changes):
    values = dict(app_env='test', provider_sandbox_enabled=True,
        sandbox_provider_network_enabled=True, sandbox_monobank_token=SecretStr(TOKEN),
        sandbox_monobank_token_sha256=sha256(TOKEN.encode()).hexdigest(),
        sandbox_monobank_token_source='api.monobank.ua', sandbox_monobank_test_token_confirmed=True,
        sandbox_checkbox_login=SecretStr('synthetic-cashier'),
        sandbox_checkbox_password=SecretStr('synthetic-password'),
        sandbox_checkbox_license_key=SecretStr('synthetic-license'),
        sandbox_checkbox_cash_register_id=REGISTER, sandbox_checkbox_cashier_id=CASHIER,
        sandbox_checkbox_organization_id=ORG)
    values.update(changes)
    return SimpleNamespace(**values)


def snapshot():
    return {'amount_minor': 2500, 'currency': 'UAH', 'items': [{
        'product_id': 1, 'name': 'Synthetic shampoo', 'quantity': 2,
        'price_minor': 1250, 'total_minor': 2500}], 'sandbox': {
        'checkbox_cash_register_id': REGISTER, 'checkbox_cashier_id': CASHIER,
        'checkbox_organization_id': ORG, 'checkbox_shift_id': SHIFT, 'reference': REFERENCE}}


class Provider:
    def __init__(self):
        self.calls = []
        self.register = {'id': REGISTER, 'organization_id': ORG, 'is_test': True}
        self.cashier = {'id': CASHIER, 'organization': {'id': ORG}, 'is_test': True}
        self.shift = {'id': SHIFT, 'status': 'OPENED', 'is_test': True,
            'cash_register': {'id': REGISTER}, 'cashier': {'id': CASHIER}}
        self.invoice = {'invoiceId': INVOICE, 'status': 'success', 'ccy': 980,
            'amount': 2500, 'finalAmount': 2500, 'reference': REFERENCE,
            'modifiedDate': '2026-10-18T12:00:00Z'}
        self.created = {'invoiceId': INVOICE, 'pageUrl': 'https://pay.mbnk.biz/' + INVOICE}
        self.receipt = {'id': RECEIPT, 'status': 'DONE', 'is_test': True,
            'organization_id': ORG, 'cash_register_id': REGISTER, 'cashier_id': CASHIER,
            'shift_id': SHIFT, 'is_sent_dps': False, 'total_sum': 2500, 'total_payment': 2500,
            'fiscal_date': '2026-10-18T12:01:00Z'}
        self.override = {}

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path in self.override:
            value = self.override[path]
            if isinstance(value, Exception):
                raise value
            return httpx.Response(value, headers={'Location': 'https://untrusted.example/'}, request=request)
        result = {
            '/api/v1/cash-registers/info': self.register,
            '/api/v1/cashier/signin': {'access_token': 'synthetic-access'},
            '/api/v1/cashier/me': self.cashier,
            '/api/v1/cashier/shift': self.shift,
            '/api/merchant/invoice/create': self.created,
            '/api/merchant/invoice/status': self.invoice,
            '/api/v1/receipts/' + RECEIPT: self.receipt,
            '/api/v1/receipts/sell': self.receipt,
        }.get(path)
        assert result is not None, 'Unexpected provider endpoint: ' + path
        return httpx.Response(200, json=result, request=request)

    def adapter(self, **config):
        return SandboxProviders(configuration(**config), transport=httpx.MockTransport(self))


def run(coro):
    return asyncio.run(coro)


def test_configuration_defaults_disable_network_and_redact_dedicated_secrets(monkeypatch):
    from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource
    monkeypatch.setattr(DotEnvSettingsSource, '_read_env_files', lambda self: {})
    monkeypatch.setattr(EnvSettingsSource, '_load_env_vars', lambda self: {})
    from app.core.config import Settings
    configured = Settings(_env_file=None, SANDBOX_MONOBANK_TOKEN=TOKEN,
        SANDBOX_CHECKBOX_PASSWORD='synthetic-config-password')
    assert configured.provider_sandbox_enabled is False
    assert configured.sandbox_provider_network_enabled is False
    assert configured.sandbox_monobank_test_token_confirmed is False
    assert TOKEN not in repr(configured) and 'synthetic-config-password' not in repr(configured)
    assert configured.sandbox_monobank_token.get_secret_value() == TOKEN


@pytest.mark.parametrize('changes,code', [
    ({'provider_sandbox_enabled': False}, 'provider_sandbox_disabled'),
    ({'sandbox_provider_network_enabled': False}, 'sandbox_provider_network_disabled'),
    ({'app_env': 'production'}, 'provider_sandbox_disabled'),
    ({'sandbox_monobank_test_token_confirmed': False}, 'sandbox_monobank_provenance_required'),
    ({'sandbox_monobank_token_source': 'web.monobank.ua'}, 'sandbox_monobank_provenance_required'),
    ({'sandbox_monobank_token_sha256': 'a' * 64}, 'sandbox_monobank_provenance_required'),
    ({'sandbox_monobank_token': 'unprotected-string'}, 'sandbox_monobank_provenance_required'),
])
def test_unconfigured_never_contacts_provider(changes, code):
    provider = Provider()
    result = run(provider.adapter(**changes).readiness())
    assert result['blocking_reasons'] == [code] and not result['available']
    assert provider.calls == []


@pytest.mark.parametrize('value', [False, None, 'true', 'false', 1, 0])
@pytest.mark.parametrize('target', ['register', 'cashier', 'shift'])
def test_missing_or_non_boolean_test_flag_blocks_payment(target, value):
    provider = Provider()
    getattr(provider, target)['is_test'] = value
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().create_invoice(reference=REFERENCE, amount_minor=2500, expires_seconds=600))
    assert all(request.url.host != 'api.monobank.ua' for request in provider.calls)


@pytest.mark.parametrize('target,key,value', [
    ('register', 'id', CASHIER), ('register', 'organization_id', CASHIER),
    ('cashier', 'id', REGISTER), ('cashier', 'organization', {'id': REGISTER}),
    ('shift', 'cash_register', {'id': CASHIER}), ('shift', 'cashier', {'id': REGISTER}),
    ('shift', 'cashier', None), ('shift', 'cash_register', []),
])
def test_identity_mismatch_blocks_invoice(target, key, value):
    provider = Provider()
    getattr(provider, target)[key] = value
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().create_invoice(reference=REFERENCE, amount_minor=2500, expires_seconds=600))
    assert all(request.url.host != 'api.monobank.ua' for request in provider.calls)


def test_readiness_candid_provenance_and_preopened_shift():
    provider = Provider()
    result = run(provider.adapter().readiness())
    assert result['available']
    assert result['verification']['monobank_provider_confirmed'] is False
    assert result['verification']['monobank_token_sha256'] == sha256(TOKEN.encode()).hexdigest()
    assert result['verification']['checkbox_provider_confirmed'] is True
    assert result['verification']['shift_id'] == SHIFT
    provider.shift['status'] = 'CLOSED'
    assert run(provider.adapter().readiness())['blocking_reasons'] == ['checkbox_test_shift_required']
    recovery = run(provider.adapter().readiness(require_open_shift=False))
    assert recovery['available'] and recovery['verification']['checkbox_provider_confirmed'] is True
    assert 'shift_id' not in recovery['verification']
    assert not any('/shifts' in request.url.path and request.method == 'POST' for request in provider.calls)


def test_documented_current_shift_without_optional_fields_is_verified_by_cashier_and_register():
    provider = Provider()
    del provider.shift['is_test'], provider.shift['cashier']
    assert run(provider.adapter().ensure_test_shift()) == {'shift_id': SHIFT}


def test_invoice_contract_and_strict_return_url():
    provider = Provider()
    result = run(provider.adapter().create_invoice(reference=REFERENCE, amount_minor=2500, expires_seconds=600))
    assert result['invoice_id'] == INVOICE
    request = provider.calls[-1]
    assert request.url == 'https://api.monobank.ua/api/merchant/invoice/create'
    assert request.headers['X-Token'] == TOKEN
    assert json.loads(request.content) == {'amount': 2500, 'ccy': 980, 'paymentType': 'debit',
        'validity': 600, 'merchantPaymInfo': {'reference': REFERENCE}}


@pytest.mark.parametrize('url', [
    'http://pay.mbnk.biz/' + INVOICE, 'https://pay.mbnk.biz.evil.example/' + INVOICE,
    'https://user:pass@pay.mbnk.biz/' + INVOICE, 'https://pay.mbnk.biz:8443/' + INVOICE,
    'https://pay.mbnk.biz/nested/path', 'https://pay.mbnk.biz/' + INVOICE + '?redirect=evil',
    'https://pay.mbnk.biz/' + INVOICE + '#fragment', 'javascript:alert(1)', None,
])
def test_invoice_bad_return_url_stays_unknown(url):
    provider = Provider()
    provider.created['pageUrl'] = url
    with pytest.raises(SandboxProviderError, match='invoice_create_unknown') as caught:
        run(provider.adapter().create_invoice(reference=REFERENCE, amount_minor=2500, expires_seconds=600))
    assert caught.value.invoice_id == INVOICE
    assert len([r for r in provider.calls if r.url.host == 'api.monobank.ua']) == 1


def test_invoice_timeout_has_no_automatic_retry_or_replacement():
    provider = Provider()
    provider.override['/api/merchant/invoice/create'] = httpx.ReadTimeout('secret upstream message')
    with pytest.raises(SandboxProviderError, match='^invoice_create_unknown$'):
        run(provider.adapter().create_invoice(reference=REFERENCE, amount_minor=2500, expires_seconds=600))
    assert len([r for r in provider.calls if r.url.path.endswith('/invoice/create')]) == 1


def test_redirect_is_never_followed():
    provider = Provider()
    provider.override['/api/v1/cash-registers/info'] = 307
    result = run(provider.adapter().readiness())
    assert result['blocking_reasons'] == ['sandbox_provider_rejected']
    assert len(provider.calls) == 1


@pytest.mark.parametrize('changes', [{'invoiceId': 'other'}, {'ccy': '980'}, {'ccy': True},
    {'finalAmount': 2400}, {'finalAmount': None}, {'modifiedDate': '2026-10-18T12:00:00'}])
def test_invoice_cannot_accept_mismatched_money_identity_or_date(changes):
    provider = Provider()
    provider.invoice.update(changes)
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().get_invoice(INVOICE))


def test_hold_is_not_payment():
    provider = Provider()
    provider.invoice['status'] = 'hold'
    result = run(provider.adapter().get_invoice(INVOICE))
    assert result['status'] == 'processing' and result['accepted_at'] is None


def test_provider_acceptance_is_normalized_to_utc_before_persistence():
    provider = Provider()
    provider.invoice['modifiedDate'] = '2026-10-18T15:00:00+03:00'
    result = run(provider.adapter().get_invoice(INVOICE))
    assert result['accepted_at'].isoformat() == '2026-10-18T12:00:00+00:00'


def test_receipt_exact_units_no_delivery_and_test_artifact():
    provider = Provider()
    result = run(provider.adapter().create_receipt(RECEIPT, snapshot=snapshot(), invoice_id=INVOICE))
    assert result['status'] == 'issued' and result['is_test'] is True and result['amount_minor'] == 2500
    assert result['is_sent_dps'] is False
    assert (result['cash_register_id'], result['cashier_id'], result['organization_id']) == (REGISTER, CASHIER, ORG)
    request = provider.calls[-1]
    body = json.loads(request.content)
    assert body == {'id': RECEIPT, 'goods': [{'good': {'code': '1', 'name': 'Synthetic shampoo',
        'price': 1250}, 'quantity': 2000, 'is_return': False}],
        'payments': [{'type': 'CASHLESS', 'value': 2500, 'label': 'Картка'}]}
    assert request.url.path == '/api/v1/receipts/sell'
    assert all('delivery' not in json.loads(r.content) for r in provider.calls if r.content)


def test_reopened_shift_cannot_receive_original_sale_receipt():
    provider = Provider()
    provider.shift['id'] = REGISTER
    with pytest.raises(SandboxProviderError, match='sandbox_checkbox_shift_changed'):
        run(provider.adapter().create_receipt(RECEIPT, snapshot=snapshot(), invoice_id=INVOICE))
    assert all(r.url.path != '/api/v1/receipts/sell' for r in provider.calls)


@pytest.mark.parametrize('changes', [{'status': 'created'}, {'amount': 2600, 'finalAmount': 2600},
    {'reference': REGISTER}])
def test_receipt_requires_same_paid_invoice(changes):
    provider = Provider()
    provider.invoice.update(changes)
    with pytest.raises(SandboxProviderError, match='sandbox_invoice_mismatch'):
        run(provider.adapter().create_receipt(RECEIPT, snapshot=snapshot(), invoice_id=INVOICE))
    assert all(r.url.path != '/api/v1/receipts/sell' for r in provider.calls)


@pytest.mark.parametrize('changes', [{'is_test': False}, {'is_test': 'true'}, {'id': REGISTER},
    {'organization_id': REGISTER}, {'cash_register_id': CASHIER}, {'cashier_id': REGISTER},
    {'is_sent_dps': True}, {'total_sum': 2400}, {'total_payment': 2400}, {'total_sum': True}])
def test_receipt_mismatches_never_become_issued(changes):
    provider = Provider()
    provider.receipt.update(changes)
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().get_receipt(RECEIPT))


def test_receipt_timeout_recovery_gets_same_uuid_without_resubmit():
    provider = Provider()
    provider.override['/api/v1/receipts/sell'] = httpx.ReadTimeout('secret')
    adapter = provider.adapter()
    with pytest.raises(SandboxProviderError, match='receipt_create_unknown'):
        run(adapter.create_receipt(RECEIPT, snapshot=snapshot(), invoice_id=INVOICE))
    assert run(adapter.get_receipt(RECEIPT))['receipt_uuid'] == RECEIPT
    assert len([r for r in provider.calls if r.url.path == '/api/v1/receipts/sell']) == 1


def test_receipt_missing_only_404_is_none():
    provider = Provider()
    provider.override['/api/v1/receipts/' + RECEIPT] = 404
    assert run(provider.adapter().get_receipt(RECEIPT)) is None
    provider.override['/api/v1/receipts/' + RECEIPT] = 500
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().get_receipt(RECEIPT))


def test_order_discount_requires_exact_snapshot_mapping_before_dispatch():
    provider = Provider()
    state = deepcopy(snapshot())
    state['amount_minor'] = 2400
    with pytest.raises(SandboxProviderError, match='sandbox_invalid_snapshot'):
        run(provider.adapter().create_receipt(RECEIPT, snapshot=state, invoice_id=INVOICE))
    assert provider.calls == []


def test_order_discount_maps_to_global_absolute_value_without_changing_item_price():
    provider = Provider()
    state = deepcopy(snapshot())
    state.update(amount_minor=2400, additional_discount_minor=100)
    provider.invoice.update(amount=2400, finalAmount=2400)
    provider.receipt.update(total_sum=2400, total_payment=2400)
    result = run(provider.adapter().create_receipt(RECEIPT, snapshot=state, invoice_id=INVOICE))
    assert result['amount_minor'] == 2400
    body = json.loads(provider.calls[-1].content)
    assert body['discounts'] == [{'type': 'DISCOUNT', 'mode': 'VALUE', 'value': 100}]
    assert body['goods'][0]['good']['price'] == 1250


@pytest.mark.parametrize('identity', ['../escape', 'http://evil.example', 'bad/identity'])
def test_provider_identifiers_cannot_change_paths(identity):
    provider = Provider()
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().get_invoice(identity))
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().get_receipt(identity))
    assert provider.calls == []


def test_invoice_payment_path_is_opaque_and_independent_of_invoice_id():
    provider = Provider()
    provider.created['pageUrl'] = 'https://pay.mbnk.biz/opaque_payment-123'
    result = run(provider.adapter().create_invoice(reference=REFERENCE, amount_minor=2500, expires_seconds=600))
    assert result['invoice_id'] == INVOICE
    assert result['payment_url'] == provider.created['pageUrl']


def test_current_monobank_payment_origin_is_supported():
    provider = Provider()
    provider.created['pageUrl'] = 'https://pay.monobank.ua/opaque_payment-123'
    result = run(provider.adapter().create_invoice(reference=REFERENCE, amount_minor=2500, expires_seconds=600))
    assert result['payment_url'] == provider.created['pageUrl']


def test_receipt_without_redundant_test_flag_requires_verified_test_identities():
    provider = Provider()
    del provider.receipt['is_test']
    result = run(provider.adapter().get_receipt(RECEIPT))
    assert result['is_test'] is True and result['is_sent_dps'] is False
    provider.register['is_test'] = False
    with pytest.raises(SandboxProviderError):
        run(provider.adapter().get_receipt(RECEIPT))
