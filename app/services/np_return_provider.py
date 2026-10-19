"""Read-only, profile-pinned NP return verification; never creates shipments.

Easy Return linkage: https://novaposhta.ua/additional-services/easy-return/
Ordinary return linkage needs a separately recorded operator document. Tracking
never attests parcel contents, merchant payment, or fiscal acceptance.
"""
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
import re

import httpx

from app.core.config import settings
from app.services.np_tracking import STATUS_MAP, event_time
from app.services.order_documents import digest


class NPReturnError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise NPReturnError('np_return_proof_invalid')
    return value


class NPReturnGateway:
    def __init__(self, config=settings, *, transport=None):
        self.config, self.transport, self.profile = config, transport, None
        try:
            profile = dict(config.shop_np_return_profile or {})
            if set(profile) != {'id', 'token_sha256', 'environment', 'approved', 'manual_link_allowed'}:
                raise ValueError
            _text(profile['id'], r'[A-Za-z0-9_-]{1,64}')
            _text(profile['token_sha256'], r'[0-9a-f]{64}')
            if (profile['environment'] not in {'test', 'production'} or profile['approved'] is not True
                    or type(profile['manual_link_allowed']) is not bool):
                raise ValueError
            token = config.shop_np_return_api_key
            token = token.get_secret_value() if hasattr(token, 'get_secret_value') else ''
            if not token or not compare_digest(sha256(token.encode()).hexdigest(), profile['token_sha256']):
                raise ValueError
            if transport is not None and type(transport) is not httpx.MockTransport:
                raise ValueError
            self.profile = profile
        except (AttributeError, TypeError, ValueError, NPReturnError):
            pass

    @property
    def test_only(self):
        return self.transport is not None or not self.profile or self.profile['environment'] != 'production'

    @property
    def available(self):
        return bool(self.profile and (type(self.transport) is httpx.MockTransport or (
            self.config.shop_np_return_live_transport_enabled is True and self.profile['environment'] == 'production')))

    def _guard(self):
        current = NPReturnGateway(self.config, transport=self.transport)
        if not self.available or not current.available:
            raise NPReturnError('np_return_unconfigured')
        if current.profile != self.profile:
            raise NPReturnError('np_return_profile_mismatch')

    async def verify_pair(self, original_ttn, return_ttn, *, linkage_kind):
        self._guard()
        for number in (original_ttn, return_ttn):
            _text(number, r'[0-9]{14}')
        if not isinstance(linkage_kind, str) or original_ttn == return_ttn or linkage_kind not in {'easy_return', 'manual_document'}:
            raise NPReturnError('np_return_proof_invalid')
        if linkage_kind == 'manual_document' and self.profile['manual_link_allowed'] is not True:
            raise NPReturnError('np_return_manual_link_disabled')
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                    verify=True, timeout=httpx.Timeout(10, connect=5)) as client:
                response = await client.post('https://api.novaposhta.ua/v2.0/json/', json={
                    'apiKey': self.config.shop_np_return_api_key.get_secret_value(),
                    'modelName': 'TrackingDocumentGeneral', 'calledMethod': 'getStatusDocuments',
                    'methodProperties': {'Documents': [{'DocumentNumber': n} for n in (original_ttn, return_ttn)]},
                })
        except httpx.HTTPError:
            raise NPReturnError('np_return_unavailable') from None
        self._guard()
        if response.status_code != 200:
            raise NPReturnError('np_return_unavailable')
        if len(response.content) > 100_000:
            raise NPReturnError('np_return_proof_invalid')
        try:
            payload = response.json()
        except ValueError:
            raise NPReturnError('np_return_proof_invalid') from None
        if (not isinstance(payload, dict) or payload.get('success') is not True
                or not isinstance(payload.get('data'), list) or payload.get('errors')):
            raise NPReturnError('np_return_proof_invalid')
        now, rows, observations = datetime.now(UTC), {}, {}
        for row in payload['data']:
            if not isinstance(row, dict):
                raise NPReturnError('np_return_proof_invalid')
            number, code = row.get('Number'), row.get('StatusCode')
            if not isinstance(number, str) or number not in {original_ttn, return_ttn} or number in rows or type(code) not in {str, int}:
                raise NPReturnError('np_return_proof_invalid')
            code = str(code)
            if code not in STATUS_MAP:
                raise NPReturnError('np_return_proof_invalid')
            date = event_time(row)
            if date and date > now + timedelta(seconds=30):
                raise NPReturnError('np_return_proof_invalid')
            rows[number] = row
            observations[number] = {'number': number, 'raw_status': code, 'status': STATUS_MAP[code],
                'event_at': date.isoformat() if date else None}
        if set(rows) != {original_ttn, return_ttn}:
            raise NPReturnError('np_return_proof_invalid')
        link = rows[return_ttn].get('LightReturnNumber')
        if link not in (None, '', original_ttn) or (linkage_kind == 'easy_return' and link != original_ttn):
            raise NPReturnError('np_return_linkage_mismatch')
        automatic = linkage_kind == 'easy_return'
        proof = {'verified': True, 'profile_id': self.profile['id'],
            'authority': 'test' if self.test_only else 'provider',
            'transport': 'mock_http' if self.transport is not None else 'https',
            'original_ttn': original_ttn, 'return_ttn': return_ttn, 'linkage_kind': linkage_kind,
            'linkage_verified': automatic,
            'linkage_authority': ('test' if self.test_only else 'provider') if automatic else 'manual',
            'original': observations[original_ttn], 'reverse': observations[return_ttn],
            'observed_at': now.isoformat()}
        proof['proof_hash'] = digest(proof)
        return proof
