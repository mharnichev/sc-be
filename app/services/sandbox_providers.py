"""Explicitly enabled, local provider-test integration; no live credential fallback.

Provider contracts (reviewed 2026-10-18):
https://monobank.ua/api-docs/acquiring/dev/test
https://monobank.ua/api-docs/acquiring/methods
https://monobank.ua/api-docs/acquiring/methods/ia/get--api--merchant--details
https://api.checkbox.ua/api/docs (cashier/me, cash-registers/info, receipts)
https://wiki.checkbox.ua/instructions/test-data

Monobank documents token provenance, not a runtime is_test probe. Fingerprint
and operator attestation are a local trust policy, never provider confirmation.
The caller must durably record dispatch before mutations; this adapter does not
retry POSTs. Receipt recovery always uses the previously allocated UUID.
"""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx

MONO = 'https://api.monobank.ua'
CHECKBOX = 'https://api.checkbox.ua'
_INVOICE_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')


class SandboxProviderError(Exception):
    """Safe machine code only: never expose upstream bodies or credentials."""

    def __init__(self, code: str, *, invoice_id: str | None = None):
        self.code = code
        self.invoice_id = invoice_id if isinstance(invoice_id, str) and _INVOICE_ID.fullmatch(invoice_id) else None
        super().__init__(code)


def _secret(value) -> str:
    return value.get_secret_value() if hasattr(value, 'get_secret_value') else ''


def _uuid(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise SandboxProviderError('sandbox_invalid_identity') from None


def _integer(value, *, positive=False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise SandboxProviderError('sandbox_invalid_amount')
    return value


def _invoice_id(value) -> str:
    if not isinstance(value, str) or not _INVOICE_ID.fullmatch(value):
        raise SandboxProviderError('sandbox_invalid_invoice')
    return value


def _date(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise SandboxProviderError('sandbox_invalid_provider_date') from None


class SandboxProviders:
    def __init__(self, config, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    def _configured(self):
        c = self.config
        if c.app_env not in ('local', 'test') or c.provider_sandbox_enabled is not True:
            raise SandboxProviderError('provider_sandbox_disabled')
        if c.sandbox_provider_network_enabled is not True:
            raise SandboxProviderError('sandbox_provider_network_disabled')
        token = _secret(c.sandbox_monobank_token)
        fingerprint = c.sandbox_monobank_token_sha256
        if (not token or c.sandbox_monobank_test_token_confirmed is not True
                or c.sandbox_monobank_token_source != 'api.monobank.ua'
                or not isinstance(fingerprint, str)
                or not re.fullmatch('[0-9a-f]{64}', fingerprint)
                or not compare_digest(sha256(token.encode()).hexdigest(), fingerprint)):
            raise SandboxProviderError('sandbox_monobank_provenance_required')
        if not all(_secret(getattr(c, name)) for name in (
                'sandbox_checkbox_login', 'sandbox_checkbox_password', 'sandbox_checkbox_license_key')):
            raise SandboxProviderError('sandbox_checkbox_credentials_required')
        for name in ('sandbox_checkbox_cash_register_id', 'sandbox_checkbox_cashier_id',
                     'sandbox_checkbox_organization_id'):
            if _uuid(getattr(c, name)) != getattr(c, name):
                raise SandboxProviderError('sandbox_invalid_identity')

    async def _request(self, method, host, path, *, headers=None, body=None, params=None,
                       unknown_code=None, missing=False):
        # All URLs originate in code, never provider responses or user input.
        self._configured()
        if host not in (MONO, CHECKBOX) or not path.startswith('/api/'):
            raise SandboxProviderError('sandbox_endpoint_forbidden')
        safe_headers = {'Accept': 'application/json', 'X-Client-Name': 'Soulcuts-local-sandbox',
                        'X-Client-Version': '1', **(headers or {})}
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                    follow_redirects=False, timeout=httpx.Timeout(10, connect=5),
                    limits=httpx.Limits(max_connections=2, max_keepalive_connections=0)) as client:
                response = await client.request(method, host + path, headers=safe_headers,
                                                json=body, params=params)
        except httpx.HTTPError:
            raise SandboxProviderError(unknown_code or 'sandbox_provider_unavailable') from None
        if missing and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            # A write may have reached the provider even when its response is an
            # error. Never turn an uncertain mutation into permission to retry.
            code = unknown_code if method == 'POST' and response.status_code >= 500 else None
            raise SandboxProviderError(code or 'sandbox_provider_rejected')
        if len(response.content) > 2_000_000:
            raise SandboxProviderError(unknown_code or 'sandbox_invalid_provider_response')
        try:
            data = response.json()
        except ValueError:
            raise SandboxProviderError(unknown_code or 'sandbox_invalid_provider_response') from None
        if not isinstance(data, dict):
            raise SandboxProviderError(unknown_code or 'sandbox_invalid_provider_response')
        return data

    async def _verified(self):
        """Probe register first, then authenticate and prove the actual cashier."""
        self._configured()
        c = self.config
        register = await self._request('GET', CHECKBOX, '/api/v1/cash-registers/info',
            headers={'X-License-Key': _secret(c.sandbox_checkbox_license_key)})
        if (register.get('is_test') is not True
                or register.get('id') != c.sandbox_checkbox_cash_register_id
                or register.get('organization_id') != c.sandbox_checkbox_organization_id):
            raise SandboxProviderError('sandbox_checkbox_register_unverified')
        auth = await self._request('POST', CHECKBOX, '/api/v1/cashier/signin', body={
            'login': _secret(c.sandbox_checkbox_login), 'password': _secret(c.sandbox_checkbox_password)})
        access_token = auth.get('access_token')
        if not isinstance(access_token, str) or not access_token or len(access_token) > 8192:
            raise SandboxProviderError('sandbox_checkbox_auth_failed')
        headers = {'Authorization': 'Bearer ' + access_token}
        cashier = await self._request('GET', CHECKBOX, '/api/v1/cashier/me', headers=headers)
        if (cashier.get('is_test') is not True or cashier.get('id') != c.sandbox_checkbox_cashier_id
                or not isinstance(cashier.get('organization'), dict)
                or cashier['organization'].get('id') != c.sandbox_checkbox_organization_id):
            raise SandboxProviderError('sandbox_checkbox_cashier_unverified')
        return headers

    async def _shift(self, headers):
        shift = await self._request('GET', CHECKBOX, '/api/v1/cashier/shift', headers=headers, missing=True)
        c = self.config
        if not shift or shift.get('status') != 'OPENED':
            raise SandboxProviderError('checkbox_test_shift_required')
        # The published ShiftWithCashRegisterModel has no is_test/cashier.
        # Test identity comes from the fresh cashier/register proofs and this
        # authenticated cashier's current shift; reject contradictory optional
        # fields rather than pretending they are guaranteed by the schema.
        if (('is_test' in shift and shift['is_test'] is not True)
                or not isinstance(shift.get('cash_register'), dict)
                or shift['cash_register'].get('id') != c.sandbox_checkbox_cash_register_id
                or ('cashier' in shift and (not isinstance(shift['cashier'], dict)
                    or shift['cashier'].get('id') != c.sandbox_checkbox_cashier_id))):
            raise SandboxProviderError('sandbox_checkbox_shift_unverified')
        return _uuid(shift.get('id'))

    async def readiness(self, *, require_open_shift=True):
        c = self.config
        result = {'available': False, 'blocking_reasons': [], 'verification': {
            'monobank_provider_confirmed': False,
            'monobank_policy': 'operator_attested_test_token_with_pinned_sha256',
            'monobank_token_sha256': c.sandbox_monobank_token_sha256,
            'checkbox_provider_confirmed': False,
        }, 'checkbox_cash_register_id': c.sandbox_checkbox_cash_register_id,
           'checkbox_cashier_id': c.sandbox_checkbox_cashier_id,
           'checkbox_organization_id': c.sandbox_checkbox_organization_id}
        try:
            headers = await self._verified()
            result['verification']['checkbox_provider_confirmed'] = True
            if require_open_shift:
                result['verification']['shift_id'] = await self._shift(headers)
            result['available'] = True
        except SandboxProviderError as error:
            result['blocking_reasons'] = [error.code]
        return result

    async def ensure_test_shift(self):
        return {'shift_id': await self._shift(await self._verified())}

    async def create_invoice(self, *, reference, amount_minor, expires_seconds):
        _uuid(reference)
        _integer(amount_minor, positive=True)
        if type(expires_seconds) is not int or not 60 <= expires_seconds <= 3600:
            raise SandboxProviderError('sandbox_invalid_expiry')
        await self._shift(await self._verified())
        data = await self._request('POST', MONO, '/api/merchant/invoice/create',
            headers={'X-Token': _secret(self.config.sandbox_monobank_token)},
            body={'amount': amount_minor, 'ccy': 980, 'paymentType': 'debit',
                  'validity': expires_seconds, 'merchantPaymInfo': {'reference': reference}},
            unknown_code='invoice_create_unknown')
        try:
            invoice_id = _invoice_id(data.get('invoiceId'))
        except SandboxProviderError:
            raise SandboxProviderError('invoice_create_unknown') from None
        url = data.get('pageUrl')
        try:
            parts = urlsplit(url)
            valid = (isinstance(url, str) and parts.scheme == 'https'
                and parts.hostname in ('pay.mbnk.biz', 'pay.monobank.ua') and parts.port in (None, 443)
                and not parts.username and not parts.password and not parts.query and not parts.fragment
                and re.fullmatch(r'/[A-Za-z0-9_-]{1,200}', parts.path) is not None
                and not any(char.isspace() for char in url) and '\\' not in url)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise SandboxProviderError('invoice_create_unknown', invoice_id=invoice_id)
        return {'invoice_id': invoice_id, 'payment_url': url, 'status': 'waiting'}

    async def get_invoice(self, invoice_id):
        _invoice_id(invoice_id)
        await self._verified()
        data = await self._request('GET', MONO, '/api/merchant/invoice/status',
            headers={'X-Token': _secret(self.config.sandbox_monobank_token)}, params={'invoiceId': invoice_id})
        if data.get('invoiceId') != invoice_id or type(data.get('ccy')) is not int or data['ccy'] != 980:
            raise SandboxProviderError('sandbox_invoice_mismatch')
        amount = _integer(data.get('amount'), positive=True)
        status = {'created': 'waiting', 'processing': 'processing', 'hold': 'processing',
                  'success': 'paid', 'failure': 'declined', 'expired': 'expired',
                  'reversed': 'cancelled'}.get(data.get('status'), 'unknown')
        if status == 'paid' and (type(data.get('finalAmount')) is not int or data['finalAmount'] != amount):
            raise SandboxProviderError('sandbox_invoice_mismatch')
        return {'invoice_id': invoice_id, 'status': status, 'amount_minor': amount, 'currency': 'UAH',
                'reference': data.get('reference'),
                'accepted_at': _date(data.get('modifiedDate')) if status == 'paid' else None}

    def _receipt(self, data, receipt_uuid):
        c = self.config
        # Receipt responses may omit is_test. Both callers freshly verify the pinned
        # test cashier/register before I/O; receipt IDs and no-DPS evidence remain mandatory.
        if (data.get('id') != receipt_uuid or ('is_test' in data and data['is_test'] is not True)
                or data.get('organization_id') != c.sandbox_checkbox_organization_id
                or data.get('cash_register_id') != c.sandbox_checkbox_cash_register_id
                or data.get('cashier_id') != c.sandbox_checkbox_cashier_id
                or data.get('is_sent_dps') is not False):
            raise SandboxProviderError('sandbox_receipt_unverified')
        status = {'DONE': 'issued', 'ERROR': 'error', 'CANCELLED': 'error',
                  'CREATED': 'pending'}.get(data.get('status'))
        if status is None:
            raise SandboxProviderError('sandbox_invalid_receipt_status')
        amount = _integer(data.get('total_sum'), positive=True)
        if _integer(data.get('total_payment'), positive=True) != amount:
            raise SandboxProviderError('sandbox_receipt_amount_mismatch')
        return {'receipt_uuid': receipt_uuid, 'status': status, 'is_test': True, 'is_sent_dps': False,
            'amount_minor': amount, 'total': amount, 'currency': 'UAH',
            'cash_register_id': data['cash_register_id'], 'cashier_id': data['cashier_id'],
            'organization_id': data['organization_id'],
            'shift_id': data.get('shift_id'),
            'accepted_at': _date(data.get('fiscal_date')) if status == 'issued' else None}

    async def get_receipt(self, receipt_uuid):
        receipt_uuid = _uuid(receipt_uuid)
        headers = await self._verified()
        data = await self._request('GET', CHECKBOX, '/api/v1/receipts/' + receipt_uuid,
                                   headers=headers, missing=True)
        return None if data is None else self._receipt(data, receipt_uuid)

    async def create_receipt(self, receipt_uuid, *, snapshot, invoice_id):
        receipt_uuid = _uuid(receipt_uuid)
        _invoice_id(invoice_id)
        amount = _integer(snapshot.get('amount_minor'), positive=True)
        if snapshot.get('currency') != 'UAH' or not isinstance(snapshot.get('items'), list):
            raise SandboxProviderError('sandbox_invalid_snapshot')
        goods = []
        for item in snapshot['items']:
            quantity = _integer(item.get('quantity'), positive=True)
            price = _integer(item.get('price_minor'))
            if _integer(item.get('total_minor')) != price * quantity:
                raise SandboxProviderError('sandbox_invalid_snapshot')
            name = item.get('name')
            if not isinstance(name, str) or not name.strip() or len(name) > 500:
                raise SandboxProviderError('sandbox_invalid_snapshot')
            goods.append({'good': {'code': str(item.get('product_id')), 'name': name, 'price': price},
                          'quantity': quantity * 1000, 'is_return': False})
        additional_discount = sum(x['good']['price'] * x['quantity'] // 1000 for x in goods) - amount
        if (not goods or additional_discount < 0 or
                _integer(snapshot.get('additional_discount_minor', 0)) != additional_discount):
            raise SandboxProviderError('sandbox_invalid_snapshot')
        body = {'id': receipt_uuid, 'goods': goods,
                'payments': [{'type': 'CASHLESS', 'value': amount, 'label': 'Картка'}]}
        if additional_discount:
            body['discounts'] = [{'type': 'DISCOUNT', 'mode': 'VALUE', 'value': additional_discount}]
        headers = await self._verified()
        shift_id = await self._shift(headers)
        pinned = snapshot.get('sandbox', {})
        if not isinstance(pinned, dict) or any(pinned.get(key) != expected for key, expected in (
            ('checkbox_cash_register_id', self.config.sandbox_checkbox_cash_register_id),
            ('checkbox_cashier_id', self.config.sandbox_checkbox_cashier_id),
            ('checkbox_organization_id', self.config.sandbox_checkbox_organization_id))):
            raise SandboxProviderError('sandbox_invalid_snapshot')
        if _uuid(pinned.get('checkbox_shift_id')) != shift_id:
            raise SandboxProviderError('sandbox_checkbox_shift_changed')
        invoice = await self.get_invoice(invoice_id)
        if (invoice['status'] != 'paid' or invoice['amount_minor'] != amount
                or not pinned.get('reference') or invoice['reference'] != pinned['reference']):
            raise SandboxProviderError('sandbox_invoice_mismatch')
        data = await self._request('POST', CHECKBOX, '/api/v1/receipts/sell', headers=headers,
            body=body,
            unknown_code='receipt_create_unknown')
        result = self._receipt(data, receipt_uuid)
        if result['amount_minor'] != amount or result['shift_id'] != shift_id:
            raise SandboxProviderError('sandbox_receipt_amount_mismatch')
        return result


def get_adapter():
    # Lazy import keeps pure adapter tests independent of dotenv/app startup.
    from app.core.config import settings
    return SandboxProviders(settings)
