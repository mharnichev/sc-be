"""NP tracking serializers/parsers, injected transport only; no carrier actions."""
from hashlib import sha256
import json
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from app.services.np_return_provider import NPReturnGateway, NPReturnError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def configuration():
    return SimpleNamespace(shop_np_return_profile={'id': 'np-test', 'token_sha256': sha256(b'synthetic-only').hexdigest(),
        'environment': 'test', 'approved': True, 'manual_link_allowed': True},
        shop_np_return_api_key=SecretStr('synthetic-only'), shop_np_return_live_transport_enabled=False)


ORIGINAL, REVERSE = '20450000000001', '20450000000002'


def payload():
    return {'success': True, 'errors': [], 'data': [
        {'Number': ORIGINAL, 'StatusCode': '103', 'DateScan': '2026-01-01 12:00:00', 'RecipientPhone': 'private'},
        {'Number': REVERSE, 'StatusCode': '9', 'DateScan': '2026-01-02 12:00:00', 'LightReturnNumber': ORIGINAL}]}


def gateway(data=None, config=None, status=200):
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url) == 'https://api.novaposhta.ua/v2.0/json/'
        assert request.method == 'POST'
        assert json.loads(request.content) == {'apiKey': 'synthetic-only', 'modelName': 'TrackingDocumentGeneral',
            'calledMethod': 'getStatusDocuments', 'methodProperties': {'Documents': [
                {'DocumentNumber': ORIGINAL}, {'DocumentNumber': REVERSE}]}}
        return httpx.Response(status, json=data if data is not None else payload())
    return NPReturnGateway(config or configuration(), transport=httpx.MockTransport(handler)), calls


async def test_readonly_exact_pair_minimal_proof():
    provider, calls = gateway()
    proof = await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')
    assert len(calls) == 1 and proof['authority'] == 'test' and proof['transport'] == 'mock_http'
    assert proof['linkage_verified'] is True and proof['linkage_authority'] == 'test'
    assert proof['reverse']['status'] == 'delivered'  # physical contents remain unconfirmed
    assert proof['reverse']['event_at'] == '2026-01-02T10:00:00+00:00'
    assert len(proof['proof_hash']) == 64
    assert 'private' not in str(proof) and 'synthetic-only' not in str(proof)


async def test_manual_link_never_claims_provider_attestation():
    data = payload(); del data['data'][1]['LightReturnNumber']
    provider, _ = gateway(data)
    proof = await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='manual_document')
    assert proof['linkage_verified'] is False and proof['linkage_authority'] == 'manual'


@pytest.mark.parametrize('kind', ['easy_return', 'manual_document'])
async def test_explicit_foreign_lineage_cannot_be_overridden(kind):
    data = payload(); data['data'][1]['LightReturnNumber'] = '20450000000003'
    provider, _ = gateway(data)
    with pytest.raises(NPReturnError, match='np_return_linkage_mismatch'):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind=kind)


@pytest.mark.parametrize('patch', [
    {'success': False}, {'success': 1}, {'errors': ['failure']}, {'data': []}, {'data': None},
])
async def test_invalid_envelope(patch):
    data = payload(); data.update(patch); provider, _ = gateway(data)
    with pytest.raises(NPReturnError):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')


@pytest.mark.parametrize('patch', [
    {'Number': ORIGINAL}, {'Number': '20450000000003'}, {'Number': []}, {'Number': None}, {'StatusCode': True}, {'StatusCode': 9.0},
    {'StatusCode': '999'}, {'DateScan': '2099-01-01 12:00:00'}, {'LightReturnNumber': None},
])
async def test_ambiguous_rows_rejected(patch):
    data = payload(); data['data'][1].update(patch); provider, _ = gateway(data)
    with pytest.raises(NPReturnError):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')


async def test_missing_date_remains_unqualified():
    data = payload(); del data['data'][1]['DateScan']; provider, _ = gateway(data)
    assert (await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return'))['reverse']['event_at'] is None


@pytest.mark.parametrize('patch', [
    {'approved': False}, {'approved': 1}, {'environment': 'sandbox'}, {'manual_link_allowed': 1},
    {'token_sha256': '0' * 64}, {'id': 'bad id'}, {'extra': True},
])
async def test_profile_pin_fail_closed(patch):
    config = configuration(); config.shop_np_return_profile.update(patch)
    provider, calls = gateway(config=config)
    assert not provider.available
    with pytest.raises(NPReturnError):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')
    assert not calls


async def test_live_off_and_production_mock_still_test():
    config = configuration(); config.shop_np_return_profile['environment'] = 'production'
    assert not NPReturnGateway(config).available
    provider, _ = gateway(config=config)
    assert provider.test_only
    assert (await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return'))['authority'] == 'test'


async def test_runtime_profile_change_and_manual_disabled_stop_before_io():
    config = configuration(); provider, calls = gateway(config=config)
    config.shop_np_return_profile['id'] = 'changed'
    with pytest.raises(NPReturnError, match='np_return_profile_mismatch'):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')
    assert not calls
    config = configuration(); config.shop_np_return_profile['manual_link_allowed'] = False
    provider, calls = gateway(config=config)
    with pytest.raises(NPReturnError, match='np_return_manual_link_disabled'):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='manual_document')
    assert not calls


@pytest.mark.parametrize('status', [302, 404, 429, 500])
async def test_no_redirect_or_retry(status):
    provider, calls = gateway(status=status)
    with pytest.raises(NPReturnError, match='np_return_unavailable'):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')
    assert len(calls) == 1


@pytest.mark.parametrize('original,reverse,kind', [
    (ORIGINAL, ORIGINAL, 'easy_return'), ('１２３４５６７８９０１２３４', REVERSE, 'easy_return'),
    (ORIGINAL, 'https://example.test', 'easy_return'), (ORIGINAL, REVERSE, 'create_return'),
])
async def test_invalid_request_never_contacts_provider(original, reverse, kind):
    provider, calls = gateway()
    with pytest.raises(NPReturnError):
        await provider.verify_pair(original, reverse, linkage_kind=kind)
    assert not calls


async def test_arbitrary_transport_rejected():
    class OtherTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise AssertionError('must never run')
    provider = NPReturnGateway(configuration(), transport=OtherTransport())
    assert not provider.available
    with pytest.raises(NPReturnError):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')


async def test_token_rotation_during_io_rejects_response():
    config = configuration()
    def handler(request):
        config.shop_np_return_api_key = SecretStr('rotated')
        return httpx.Response(200, json=payload())
    provider = NPReturnGateway(config, transport=httpx.MockTransport(handler))
    with pytest.raises(NPReturnError, match='np_return_unconfigured'):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')


async def test_timeout_has_only_safe_error():
    def handler(request):
        raise httpx.ReadTimeout('private-body-secret')
    provider = NPReturnGateway(configuration(), transport=httpx.MockTransport(handler))
    with pytest.raises(NPReturnError, match='^np_return_unavailable$'):
        await provider.verify_pair(ORIGINAL, REVERSE, linkage_kind='easy_return')
