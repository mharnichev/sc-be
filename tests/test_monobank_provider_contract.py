"""Real serializers/parsers over MockTransport; never open a network socket."""
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import SecretStr
import pytest

from app.services.monobank_provider import MonobankError, MonobankGateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def configuration():
    return SimpleNamespace(shop_monobank_profile={"id": "shop-test", "merchant_id": "merchant1",
        "token_sha256": sha256(b"synthetic-only").hexdigest(), "environment": "test",
        "external_fiscalization_disabled": True}, shop_monobank_token=SecretStr("synthetic-only"),
        shop_monobank_live_transport_enabled=False)


def invoice(reference):
    return {"invoiceId": "invoice_1", "reference": reference, "amount": 10000, "finalAmount": 10000,
        "ccy": 980, "status": "success", "createdDate": "2026-01-01T00:00:00Z",
        "modifiedDate": "2026-01-01T00:01:00Z", "cancelList": [],
        "paymentInfo": {"maskedPan": "never-persist-this", "rrn": "unnecessary"}}


def gateway(data, *, config=None, merchant="merchant1", response_code=200):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.monobank.ua" and request.url.scheme == "https"
        assert request.headers["X-Token"] == "synthetic-only"
        if request.url.path == "/api/merchant/details":
            return httpx.Response(200, json={"merchantId": merchant})
        assert request.url.path == "/api/merchant/invoice/status"
        assert dict(request.url.params) == {"invoiceId": "invoice_1"}
        return httpx.Response(response_code, json=data)
    return MonobankGateway(config or configuration(), transport=httpx.MockTransport(handler)), calls


async def test_exact_success_has_test_authority_and_minimal_proof():
    reference = str(uuid4())
    provider, calls = gateway(invoice(reference))
    result = await provider.get_invoice("invoice_1", reference=reference, amount_minor=10000)
    assert result["status"] == "success" and result["authority"] == "test"
    assert result["transport"] == "mock_http" and result["currency"] == "UAH"
    assert len(calls) == 2 and len(result["proof_hash"]) == 64
    assert "paymentInfo" not in result and "synthetic-only" not in str(result)


@pytest.mark.parametrize("status", ["created", "processing", "hold", "failure", "expired", "reversed"])
async def test_non_success_is_never_accepted(status):
    reference = str(uuid4()); data = invoice(reference); data["status"] = status
    provider, _ = gateway(data)
    result = await provider.get_invoice("invoice_1", reference=reference, amount_minor=10000)
    assert result["status"] == status and result["accepted_at"] is None


@pytest.mark.parametrize("patch", [
    {"invoiceId": "other"}, {"reference": "other"}, {"ccy": 840}, {"ccy": 980.0},
    {"amount": 9999}, {"amount": True}, {"amount": 10000.0}, {"finalAmount": 9999},
    {"finalAmount": None}, {"status": "unknown"}, {"createdDate": None},
    {"modifiedDate": "2026-01-01T00:01:00"}, {"modifiedDate": "2000-01-01T00:00:00Z"},
    {"modifiedDate": "2099-01-01T00:00:00Z"}, {"cancelList": None},
    {"cancelList": [{"status": "processing", "amount": 1}]},
])
async def test_mismatched_or_ambiguous_money_rejected(patch):
    reference = str(uuid4()); data = invoice(reference); data.update(patch)
    provider, _ = gateway(data)
    with pytest.raises(MonobankError):
        await provider.get_invoice("invoice_1", reference=reference, amount_minor=10000)


async def test_foreign_merchant_stops_before_invoice():
    reference = str(uuid4()); provider, calls = gateway(invoice(reference), merchant="other")
    with pytest.raises(MonobankError, match="monobank_profile_mismatch"):
        await provider.get_invoice("invoice_1", reference=reference, amount_minor=10000)
    assert len(calls) == 1


@pytest.mark.parametrize("patch", [
    {"environment": "other"}, {"token_sha256": "0" * 64}, {"external_fiscalization_disabled": False},
    {"id": ""}, {"merchant_id": ""}, {"extra": True},
])
async def test_profile_fail_closed_before_http(patch):
    config = configuration(); config.shop_monobank_profile.update(patch)
    provider, calls = gateway({}, config=config)
    assert not provider.available
    with pytest.raises(MonobankError, match="monobank_unconfigured"):
        await provider.readiness()
    assert not calls


async def test_test_profile_cannot_enable_real_network():
    config = configuration(); config.shop_monobank_live_transport_enabled = True
    assert not MonobankGateway(config).available
    config.shop_monobank_profile["environment"] = "production"
    assert MonobankGateway(config).available
    config.shop_monobank_live_transport_enabled = False
    assert not MonobankGateway(config).available


@pytest.mark.parametrize("code", [301, 302, 400, 403, 404, 429, 500])
async def test_errors_no_retry_or_redirect(code):
    reference = str(uuid4()); provider, calls = gateway({}, response_code=code)
    with pytest.raises(MonobankError):
        await provider.get_invoice("invoice_1", reference=reference, amount_minor=10000)
    assert len(calls) == 2


async def test_timeout_only_one_get_no_mutation():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-only", request=request)
    provider = MonobankGateway(configuration(), transport=httpx.MockTransport(handler))
    with pytest.raises(MonobankError, match="monobank_unavailable"):
        await provider.readiness()
    assert len(calls) == 1


async def test_profile_change_during_response_cannot_accept():
    config = configuration()
    def handler(request):
        config.shop_monobank_profile = {**config.shop_monobank_profile, "id": "changed"}
        return httpx.Response(200, json={"merchantId": "merchant1"})
    provider = MonobankGateway(config, transport=httpx.MockTransport(handler))
    with pytest.raises(MonobankError, match="monobank_profile_mismatch"):
        await provider.readiness()


async def test_mock_never_claims_provider_even_with_production_profile():
    config = configuration(); config.shop_monobank_profile["environment"] = "production"
    config.shop_monobank_live_transport_enabled = True
    provider, _ = gateway({}, config=config)
    result = await provider.readiness()
    assert result["authority"] == "test" and result["transport"] == "mock_http"


async def test_custom_transport_is_not_an_approved_mock():
    class OtherTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise AssertionError("Must never execute")
    provider = MonobankGateway(configuration(), transport=OtherTransport())
    assert not provider.available
    with pytest.raises(MonobankError, match="monobank_unconfigured"):
        await provider.readiness()


@pytest.mark.parametrize("body", [b"not json", b"[]", b"null", b"x" * 100001])
async def test_bad_response_has_safe_error(body):
    def handler(request):
        return httpx.Response(200, content=body)
    provider = MonobankGateway(configuration(), transport=httpx.MockTransport(handler))
    with pytest.raises(MonobankError, match="monobank_proof_invalid"):
        await provider.readiness()


async def test_shared_fixture_pending_then_success_has_no_bank_mutation():
    from tests.monobank_http_stub import MonobankHTTPStub
    config = configuration()
    stub = MonobankHTTPStub(merchant_id="merchant1")
    provider = MonobankGateway(config, transport=httpx.MockTransport(stub.handle))
    reference = str(uuid4()); identifier = stub.register(reference, 10000)
    pending = await provider.get_invoice(identifier, reference=reference, amount_minor=10000)
    assert pending["status"] == "created" and pending["accepted_at"] is None
    stub.update(identifier, status="success")
    paid = await provider.get_invoice(identifier, reference=reference, amount_minor=10000)
    assert paid["status"] == "success" and paid["authority"] == "test"
    assert len(stub.calls) == 4 and all(method == "GET" for method, _ in stub.calls)
