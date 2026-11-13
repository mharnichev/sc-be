"""Merchant invoice creation contract using injected HTTP transport only."""
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.services.monobank_provider import MonobankGateway, MonobankError, invoice_payload, payment_page_url, rejected_url_shape
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


@pytest.mark.parametrize("mismatch", [None, {"amount": 20000}])
async def test_shop_redirect_uses_only_canonical_link_after_invoice_verification(mismatch):
    gateway, calls = widget_provider(
        page_url="https://pay.monobank.ua/widget/synthetic", mismatch=mismatch,
    )
    reference = str(uuid4())
    if mismatch:
        with pytest.raises(MonobankError) as error:
            await gateway.create_invoice(reference, 10000, basket(), presentation="redirect")
        assert error.value.stage == "create_response_url"
        assert error.value.invoice_candidate == "invoice_ABC"
    else:
        proof = await gateway.create_invoice(reference, 10000, basket(), presentation="redirect")
        assert proof["page_url"] == "https://pay.mbnk.biz/invoice_ABC"
        assert "display_type" not in proof
    assert sum(r.method == "POST" for r in calls) == 1
    assert sum(r.url.path == "/api/merchant/invoice/status" for r in calls) == 1


@pytest.mark.parametrize('raw_url', [
    'https://pay.mbnk.biz/widget/opaque-token?mode=frame&x=one%20two',
    'https://pay.mbnk.biz/widget?token=synthetic',
    'https://pay.mbnk.biz/invoice_other?mode=frame',
    'https://pay.monobank.ua/widget/synthetic',
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
    'https://pay.monobank.ua.evil.test/widget/token',
    'https://pay.monobank.ua:443/widget/token',
    'https://other.monobank.ua/widget/token',
    'http://pay.mbnk.biz/widget/token', 'https://pay.mbnk.biz.evil.test/widget/token',
    'https://pay.mbnk.biz@evil.test/widget/token', 'https://user@pay.mbnk.biz/widget/token',
    'https://pay.mbnk.biz:443/widget/token', 'https://pay.mbnk.biz/widget/token#fragment',
    'https://pay.mbnk.biz/widget/token#', 'https://pay.mbnk.biz/widget/\\token',
    'https://pay.mbnk.biz/widget/\ntoken', 'https://pay.mbnk.biz/widget/%0atoken',
    'https://pay.mbnk.biz/widget/%5ctoken', ' https://pay.mbnk.biz/widget/token',
    'https://pay.mbnk.biz/widget/' + 'x'*2048, 'javascript:alert(1)', None,
])
async def test_shop_iframe_falls_back_to_verified_invoice_without_using_unsafe_url(url):
    gateway, calls = widget_provider(page_url=url)
    reference = str(uuid4())
    proof = await gateway.create_invoice(reference, 10000, basket(), presentation='iframe')
    assert proof['iframe_fallback'] is True
    assert proof['page_url'] == payment_page_url('invoice_ABC')
    assert proof['invoice_verification']['reference'] == reference
    assert sum(r.method == 'POST' for r in calls) == 1
    assert any(r.url.path == '/api/merchant/invoice/status' for r in calls)


@pytest.mark.parametrize(("url", "domain"), [
    ("https://invoice-ABC.widget.mbnk.biz/frame/private-path?ref=private-query", "mbnk.biz"),
    ("https://invoice-ABC.widget.monobank.ua/frame/private-path?ref=private-query", "monobank.ua"),
    ("https://invoice-ABC.mbnk.biz.example.test/frame/private-path?ref=private-query", "None"),
])
async def test_shop_iframe_fallback_logs_only_fixed_bank_domains(url, domain, caplog):
    gateway, _ = widget_provider(page_url=url)
    proof = await gateway.create_invoice(str(uuid4()), 10000, basket(), presentation="iframe")

    assert proof["iframe_fallback"] is True
    warnings = [r.getMessage() for r in caplog.records if "Shop Monobank iframe URL rejected" in r.getMessage()]
    assert warnings == [f"Shop Monobank iframe URL rejected: domain={domain}"]


@pytest.mark.parametrize('mismatch', [
    {'invoiceId':'invoice_other'}, {'reference':'foreign-reference'}, {'amount':9999}, {'ccy':840},
])
@pytest.mark.parametrize('page_url', ['https://pay.mbnk.biz/widget?token=synthetic', 'https://untrusted.example/path'])
async def test_widget_create_cannot_bind_an_unverified_invoice_pair(mismatch, page_url):
    gateway, calls = widget_provider(mismatch=mismatch, page_url=page_url)
    with pytest.raises(MonobankError):
        await gateway.create_invoice(str(uuid4()), 10000, basket(), presentation='iframe')
    assert sum(r.method == 'POST' for r in calls) == 1
    assert any(r.url.path == '/api/merchant/invoice/status' for r in calls)


@pytest.mark.parametrize("value,category,hostname", [
    ("https://CHECKOUT.Example.Test/synthetic-private-path?session=synthetic-token", "dns", "checkout.example.test"),
    ("https://PAY.MBNK.BIZ./synthetic-private-path", "dns", "pay.mbnk.biz."),
    ("https://Checkout.Example.Test:8443/synthetic-private-path", "dns_with_port", "checkout.example.test"),
    ("https://checkout.example.test:/synthetic-private-path", "malformed", None),
    ("https://checkout.example.test:65536/synthetic-private-path", "malformed", None),
    ("https://checkout.example.test:synthetic-token/synthetic-private-path", "malformed", None),
    ("https://user:synthetic-password@checkout.example.test/synthetic-private-path", "userinfo", None),
    ("https://%70ay.mbnk.biz/synthetic-private-path", "malformed", None),
    ("https://checkout..example.test/synthetic-private-path", "malformed", None),
    ("https://example.test\\@other.test/synthetic-private-path", "unsafe_character", None),
    ("https://check\tout.example.test/synthetic-private-path", "unsafe_character", None),
    ("https://checkout.example.test/synthetic-private-path\x00", "unsafe_character", None),
    ("https://оплата.example.test/synthetic-private-path", "non_ascii_authority", None),
    ("https://[2001:db8::1]:443/synthetic-private-path", "address_literal", None),
    ("https://192.0.2.1/synthetic-private-path", "address_literal", None),
    ("https://0x7f.0.0.1/synthetic-private-path", "address_literal", None),
    ("https://0x7f000001/synthetic-private-path", "address_literal", None),
    ("https://example.123/synthetic-private-path", "address_literal", None),
    ("https://[broken/synthetic-private-path", "malformed", None),
    ("https://[::1]synthetic-token/synthetic-private-path", "malformed", None),
    ("https://[::1]:synthetic-token/synthetic-private-path", "malformed", None),
    ("https://[::1]:65536/synthetic-private-path", "malformed", None),
    ("https:///synthetic-private-path", "no_http_authority", None),
    ("monobank://pay/synthetic-private-path", "no_http_authority", None),
])
def test_rejected_url_diagnostic_retains_only_safe_authority(value, category, hostname):
    shape = rejected_url_shape(value)
    assert shape["authority_category"] == category
    assert shape.get("hostname") == hostname
    assert ("hostname" in shape) == (hostname is not None)
    assert set(shape) == {"length", "scheme", "known_host", "path_segments", "has_query", "authority_category"} | (
        {"hostname"} if hostname is not None else set()
    )
    assert not any(marker in str(shape) for marker in ("synthetic-private-path", "synthetic-token", "synthetic-password", ":8443"))


@pytest.mark.parametrize('image', [
    '/media/products/example.webp',
    'https://soulcuts.com.ua/media/products/example.webp',
])
def test_shop_invoice_sends_public_product_icon(image, monkeypatch):
    from app.services.monobank_provider import settings, shop_invoice_icon

    monkeypatch.setattr(settings, 'public_site_url', 'https://soulcuts.com.ua')
    icon = shop_invoice_icon(image)
    assert icon == 'https://soulcuts.com.ua/media/products/example.webp'
    items = [{**basket()[0], 'icon': icon}]
    assert invoice_payload(str(uuid4()), 10000, items)['merchantPaymInfo']['basketOrder'] == items


@pytest.mark.parametrize('image', [
    '//foreign.example/media/products/example.webp',
    'https://foreign.example/media/products/example.webp',
    '/media/products/../secret.jpg',
    '/media/products/example.webp?secret=value',
    '/media/products/example.svg',
])
def test_shop_invoice_ignores_nonpublic_product_icon(image):
    from app.services.monobank_provider import shop_invoice_icon

    assert shop_invoice_icon(image) is None


@pytest.mark.parametrize(
    "status, outcome, dispatched, code",
    [
        ("created", 200, True, None),
        ("created", "empty_object", True, None),
        ("created", "formatted_object", True, None),
        ("created", "array", True, "monobank_invoice_removal_unconfirmed"),
        ("created", 404, True, "monobank_invoice_removal_unconfirmed"),
        ("created", 400, True, "monobank_invoice_removal_unconfirmed"),
        ("created", "timeout", True, "monobank_invoice_removal_unconfirmed"),
        ("created", "body", True, "monobank_invoice_removal_unconfirmed"),
        ("hold", 200, False, "monobank_invoice_not_removable"),
        ("success", 200, False, "monobank_invoice_not_removable"),
        ("foreign", 200, False, "monobank_invoice_not_removable"),
    ],
)
async def test_deactivate_unpaid_shop_invoice(status, outcome, dispatched, code):
    from datetime import UTC, datetime
    import json

    _, _, config = provider()
    reference = str(uuid4())
    requests = []

    def handler(request):
        path = request.url.path
        requests.append(path)
        if path == "/api/merchant/details":
            return httpx.Response(200, json={"merchantId": config.shop_monobank_profile["merchant_id"]})
        if path == "/api/merchant/invoice/status":
            now = datetime.now(UTC).isoformat()
            return httpx.Response(200, json={
                "invoiceId": "invoice_ABC", "reference": str(uuid4()) if status == "foreign" else reference,
                "amount": 10000, "ccy": 980, "status": "created" if status == "foreign" else status,
                "createdDate": now, "modifiedDate": now, "finalAmount": 10000 if status == "success" else None,
            })
        assert path == "/api/merchant/invoice/remove"
        assert request.method == "POST" and json.loads(request.content) == {"invoiceId": "invoice_ABC"}
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if outcome == "empty_object":
            return httpx.Response(200, json={})
        if outcome == "formatted_object":
            return httpx.Response(200, content=b" { \n } ")
        if outcome == "array":
            return httpx.Response(200, json=[])
        if outcome == "body":
            return httpx.Response(200, json={"status": "unknown"})
        return httpx.Response(outcome)

    gateway = MonobankGateway(config, transport=httpx.MockTransport(handler), create_capability="shop")
    if code:
        with pytest.raises(MonobankError) as error:
            await gateway.deactivate_unpaid_shop_invoice("invoice_ABC", reference=reference, amount_minor=10000)
        assert error.value.code == code
    else:
        proof = await gateway.deactivate_unpaid_shop_invoice("invoice_ABC", reference=reference, amount_minor=10000)
        assert proof["removal_http_status"] == 200 and proof["reference"] == reference
    assert requests.count("/api/merchant/invoice/remove") == int(dispatched)
