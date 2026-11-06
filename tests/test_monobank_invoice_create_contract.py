"""Merchant invoice creation contract using injected HTTP transport only."""
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.services.monobank_provider import MonobankGateway, MonobankError, invoice_payload, payment_page_url
from app.services.order_documents import digest
from tests.test_monobank_provider_contract import configuration, anyio_backend

pytestmark = pytest.mark.anyio


def basket():
    return [dict(name="Шампунь", qty=2, sum=5000, total=10000, code="product-8", unit="шт.", tax=[0])]


def provider(*, body=None, status=200, merchant="merchant1", timeout=False):
    config = configuration()
    config.app_env = "test"
    config.shop_mono_checkbox_enabled = True
    config.master_mono_enabled = True
    config.master_mono_invoice_create_enabled = True
    config.master_mono_checkbox_link_confirmed = True
    config.shop_monobank_profile.update(environment="production", external_fiscalization_disabled=False)
    calls = []
    response = body if body is not None else dict(invoiceId="invoice_ABC", pageUrl="https://pay.mbnk.biz/invoice_ABC")
    def handler(request):
        calls.append(request)
        assert request.url.host == "api.monobank.ua"
        if request.method == "GET":
            assert request.url.path == "/api/merchant/details"
            return httpx.Response(200, json={"merchantId":merchant})
        assert request.method == "POST" and request.url.path == "/api/merchant/invoice/create"
        if timeout:
            raise httpx.ReadTimeout("synthetic accepted response loss")
        return httpx.Response(status, json=response)
    return MonobankGateway(config, transport=httpx.MockTransport(handler)), calls, config


async def test_exact_wire_basket_and_minimal_bound_response():
    import json
    gateway, calls, _ = provider(body=dict(invoiceId="invoice_ABC", pageUrl="https://pay.mbnk.biz/invoice_ABC", cardToken="private-unused"))
    reference = str(uuid4())
    result = await gateway.create_invoice(reference,10000,basket())
    assert [request.method for request in calls] == ["GET","POST"]
    assert json.loads(calls[1].content) == invoice_payload(reference,10000,basket())
    assert calls[1].headers['x-token'] == 'synthetic-only'
    assert result['invoice_id'] == 'invoice_ABC' and result['page_url'] == 'https://pay.mbnk.biz/invoice_ABC'
    assert result['authority'] == 'test' and result['transport'] == 'mock_http'
    assert result['reference'] == reference and result['amount_minor'] == 10000
    assert 'cardToken' not in result and result['proof_hash'] == digest({k:v for k,v in result.items() if k!='proof_hash'})


@pytest.mark.parametrize('flag',['master_mono_enabled','master_mono_invoice_create_enabled','master_mono_checkbox_link_confirmed'])
async def test_each_explicit_gate_prevents_all_http(flag):
    gateway,calls,config = provider()
    setattr(config,flag,False)
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()),10000,basket())
    assert calls == []


async def test_foreign_merchant_never_receives_creation():
    gateway,calls,_ = provider(merchant='foreign-owner')
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()),10000,basket())
    assert [r.method for r in calls] == ['GET']


@pytest.mark.parametrize('status',[301,400,403,404,429,500,503])
async def test_any_uncertain_http_result_never_retries_or_follows_redirect(status):
    gateway,calls,_ = provider(status=status)
    with pytest.raises(MonobankError, match='monobank_invoice_create_unknown'):
        await gateway.create_invoice(str(uuid4()),10000,basket())
    assert sum(r.method=='POST' for r in calls) == 1


async def test_lost_response_does_not_retry():
    gateway,calls,_ = provider(timeout=True)
    with pytest.raises(MonobankError, match='monobank_invoice_create_unknown'):
        await gateway.create_invoice(str(uuid4()),10000,basket())
    assert sum(r.method=='POST' for r in calls) == 1


@pytest.mark.parametrize('url',[
    'http://pay.mbnk.biz/invoice_ABC','https://pay.mbnk.biz.evil.test/invoice_ABC',
    'https://pay.mbnk.biz@evil.test/invoice_ABC','https://pay.mbnk.biz/invoice_other',
    'https://pay.mbnk.biz:443/invoice_ABC','https://pay.mbnk.biz/invoice_ABC?q=1',
    'https://pay.mbnk.biz/invoice_ABC#x','javascript:alert(1)',None,
])
async def test_untrusted_or_mismatched_payment_url_is_unknown(url):
    gateway,calls,_ = provider(body=dict(invoiceId='invoice_ABC',pageUrl=url))
    with pytest.raises(MonobankError, match='monobank_invoice_create_unknown'):
        await gateway.create_invoice(str(uuid4()),10000,basket())
    assert sum(r.method=='POST' for r in calls) == 1


@pytest.mark.parametrize('patch',[
    {'qty':True},{'qty':0},{'sum':0},{'total':1},{'tax':[]},{'tax':[True]},
    {'tax':[0,0]},{'tax':[[0]]},{'name':'bad\nline'},{'code':' '},{'unit':'x'*17},
])
async def test_invalid_server_basket_rejected_before_any_http(patch):
    gateway,calls,_=provider()
    item=deepcopy(basket());item[0].update(patch)
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()),10000,item)
    assert calls == []


async def test_mismatched_total_and_missing_fiscal_data_fail_before_any_http():
    gateway,calls,_=provider()
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()),9999,basket())
    item=basket();item[0].pop('tax')
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()),10000,item)
    assert calls == []


async def test_configuration_revoked_during_readiness_prevents_post():
    gateway,calls,config=provider()
    async def readiness():
        config.master_mono_invoice_create_enabled=False
        return {'verified':True}
    gateway.readiness=readiness
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()),10000,basket())
    assert calls == []


def test_canonical_payment_page_is_derived_without_untrusted_redirects():
    assert payment_page_url('invoice_ABC') == 'https://pay.mbnk.biz/invoice_ABC'


def widget_provider(*, page_url='https://pay.mbnk.biz/widget/opaque-token?mode=frame', mismatch=None):
    import json
    from datetime import UTC, datetime
    _, _, config = provider()
    config.shop_online_payment_enabled = True
    config.shop_online_payment_invoice_create_enabled = True
    config.shop_online_payment_link_confirmed = True
    calls = []
    sent = {}
    def handler(request):
        calls.append(request)
        if request.url.path == '/api/merchant/details':
            return httpx.Response(200, json={'merchantId': config.shop_monobank_profile['merchant_id']})
        if request.method == 'POST':
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={'invoiceId': 'invoice_ABC', 'pageUrl': page_url})
        assert request.url.path == '/api/merchant/invoice/status'
        now = datetime.now(UTC).isoformat()
        data = dict(invoiceId='invoice_ABC', reference=sent['merchantPaymInfo']['reference'],
                    amount=sent['amount'], ccy=980, status='processing', createdDate=now, modifiedDate=now)
        data.update(mismatch or {})
        return httpx.Response(200, json=data)
    return MonobankGateway(config, transport=httpx.MockTransport(handler), create_capability='shop'), calls


@pytest.mark.parametrize('raw_url', [
    'https://pay.mbnk.biz/widget/opaque-token?mode=frame&x=one%20two',
    'https://pay.mbnk.biz/widget?token=synthetic',
    'https://pay.mbnk.biz/invoice_other?mode=frame',
])
async def test_shop_iframe_preserves_returned_url_and_get_verifies_the_same_invoice(raw_url):
    import json
    gateway, calls = widget_provider(page_url=raw_url)
    reference = str(uuid4())
    proof = await gateway.create_invoice(reference, 10000, basket(), presentation='iframe')
    assert [r.method for r in calls] == ['GET', 'POST', 'GET', 'GET']
    assert json.loads(calls[1].content) == {**invoice_payload(reference, 10000, basket()), 'displayType': 'iframe'}
    assert proof['page_url'] == raw_url and proof['display_type'] == 'iframe'
    assert proof['invoice_verification']['invoice_id'] == proof['invoice_id'] == 'invoice_ABC'
    assert proof['invoice_verification']['reference'] == reference
    assert proof['proof_hash'] == digest({k:v for k,v in proof.items() if k != 'proof_hash'})
    with pytest.raises(MonobankError):
        payment_page_url('invoice_ABC', raw_url)


async def test_master_gateway_rejects_iframe_before_any_http():
    gateway, calls, _ = provider()
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()), 10000, basket(), presentation='iframe')
    assert calls == []


@pytest.mark.parametrize('url', [
    'https://pay.mbnk.biz/invoice_other',
    'http://pay.mbnk.biz/widget/token', 'https://pay.mbnk.biz.evil.test/widget/token',
    'https://pay.mbnk.biz@evil.test/widget/token', 'https://user@pay.mbnk.biz/widget/token',
    'https://pay.mbnk.biz:443/widget/token', 'https://pay.mbnk.biz/widget/token#fragment',
    'https://pay.mbnk.biz/widget/token#', 'https://pay.mbnk.biz/widget/\\token',
    'https://pay.mbnk.biz/widget/\ntoken', 'https://pay.mbnk.biz/widget/%0atoken',
    'https://pay.mbnk.biz/widget/%5ctoken', ' https://pay.mbnk.biz/widget/token',
    'https://pay.mbnk.biz/widget/' + 'x'*2048, 'javascript:alert(1)', None,
])
async def test_shop_iframe_rejects_unsafe_returned_urls_without_second_post(url):
    gateway, calls = widget_provider(page_url=url)
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()), 10000, basket(), presentation='iframe')
    assert sum(r.method == 'POST' for r in calls) == 1


@pytest.mark.parametrize('mismatch', [
    {'invoiceId':'invoice_other'}, {'reference':'foreign-reference'}, {'amount':9999}, {'ccy':840},
])
async def test_widget_create_cannot_bind_an_unverified_invoice_pair(mismatch):
    gateway, calls = widget_provider(mismatch=mismatch)
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()), 10000, basket(), presentation='iframe')
    assert sum(r.method == 'POST' for r in calls) == 1
    assert any(r.url.path == '/api/merchant/invoice/status' for r in calls)
