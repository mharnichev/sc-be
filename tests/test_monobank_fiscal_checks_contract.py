"""Read-only direct Checkbox fiscal checks; no bank/tax requests or real tokens."""
import base64
from uuid import uuid4

import httpx
import pytest

from app.services.monobank_provider import MonobankError, MonobankGateway
from app.services.order_documents import digest
from tests.test_monobank_provider_contract import configuration, anyio_backend

pytestmark = pytest.mark.anyio


def check(**patch):
    return {"id": str(uuid4()), "status": "done", "type": "sale",
            "fiscalizationSource": "checkbox", **patch}


def provider(body, *, merchant="merchant1", status=200, config=None):
    config = config or configuration()
    config.shop_mono_checkbox_enabled = True
    config.shop_monobank_profile["external_fiscalization_disabled"] = False
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert request.method == "GET" and request.url.host == "api.monobank.ua"
        if request.url.path == "/api/merchant/details":
            return httpx.Response(200, json={"merchantId": merchant})
        assert request.url.path == "/api/merchant/invoice/fiscal-checks"
        assert dict(request.url.params) == {"invoiceId": "invoice_1"}
        return httpx.Response(status, json=body)
    return MonobankGateway(config, transport=httpx.MockTransport(handler)), calls


async def test_direct_check_has_invoice_association_not_invented_totals():
    row = check(file=base64.b64encode(b"%PDF-1.7\nsynthetic-only").decode(),
                taxUrl="https://cabinet.tax.gov.ua/cashregs/check?id=synthetic")
    gateway, calls = provider({"checks": [row]})
    result = await gateway.get_fiscal_checks("invoice_1")
    assert result["checks"] == [row]
    assert result["invoice_id"] == "invoice_1" and result["merchant_id"] == "merchant1"
    assert result["authority"] == "test" and result["transport"] == "mock_http"
    assert result["verification_scope"] == "provider_invoice_association"
    assert result["proof_hash"] == digest({k: v for k, v in result.items() if k != "proof_hash"})
    assert "amount_minor" not in result and "synthetic-only" not in str(result)
    assert len(calls) == 2


@pytest.mark.parametrize("status", ["new", "process", "failed"])
async def test_pending_and_failed_status_are_retained_not_promoted(status):
    gateway, _ = provider({"checks": [check(status=status)]})
    assert (await gateway.get_fiscal_checks("invoice_1"))["checks"][0]["status"] == status


async def test_empty_list_is_pending_and_minimal_metadata_is_valid():
    gateway, _ = provider({"checks": []})
    assert (await gateway.get_fiscal_checks("invoice_1"))["checks"] == []


@pytest.mark.parametrize("patch", [
    {"id": "wrong"}, {"id": None}, {"status": []}, {"status": "success"},
    {"type": "payment"}, {"fiscalizationSource": {}}, {"fiscalizationSource": "unknown"},
    {"taxUrl": "https://evil.example/cashregs/check"},
    {"taxUrl": "http://cabinet.tax.gov.ua/cashregs/check"},
    {"taxUrl": "https://user@cabinet.tax.gov.ua/cashregs/check"},
    {"taxUrl": "https://[broken/cashregs/check"}, {"taxUrl": []},
    {"file": "not-base64!"}, {"file": base64.b64encode(b"<script>bad</script>").decode()},
    {"file": 42}, {"statusDescription": "secret\nnewline"}, {"statusDescription": "x" * 1001},
])
async def test_malformed_check_fails_with_safe_provider_error(patch):
    gateway, _ = provider({"checks": [check(**patch)]})
    with pytest.raises(MonobankError, match="monobank_proof_invalid"):
        await gateway.get_fiscal_checks("invoice_1")


@pytest.mark.parametrize("body", [{}, {"checks": None}, {"checks": {}}, {"checks": [None]},
                                 {"checks": [check()] * 2}])
async def test_bad_envelopes_and_duplicate_ids_fail_closed(body):
    gateway, _ = provider(body)
    with pytest.raises(MonobankError, match="monobank_proof_invalid"):
        await gateway.get_fiscal_checks("invoice_1")


async def test_other_fiscal_source_and_returns_are_explicit_for_domain_to_reject():
    rows = [check(fiscalizationSource="monopay"), check(type="return")]
    gateway, _ = provider({"checks": rows})
    assert (await gateway.get_fiscal_checks("invoice_1"))["checks"] == rows


async def test_internal_fiscal_mode_cannot_query_external_checks():
    def handler(request):
        raise AssertionError("No HTTP allowed")
    gateway = MonobankGateway(configuration(), transport=httpx.MockTransport(handler))
    with pytest.raises(MonobankError, match="monobank_external_fiscalization_disabled"):
        await gateway.get_fiscal_checks("invoice_1")


async def test_flag_off_and_profile_change_reject_before_acceptance():
    config = configuration()
    gateway, calls = provider({"checks": [check()]}, config=config)
    config.shop_mono_checkbox_enabled = False
    with pytest.raises(MonobankError, match="monobank_unconfigured"):
        await gateway.get_fiscal_checks("invoice_1")
    assert calls == []


async def test_foreign_merchant_and_http_error_never_create_anything():
    gateway, calls = provider({"checks": [check()]}, merchant="other")
    with pytest.raises(MonobankError, match="monobank_profile_mismatch"):
        await gateway.get_fiscal_checks("invoice_1")
    assert len(calls) == 1
    gateway, calls = provider({}, status=404)
    with pytest.raises(MonobankError, match="monobank_not_found"):
        await gateway.get_fiscal_checks("invoice_1")
    assert len(calls) == 2


async def test_no_extra_provider_fields_are_persisted():
    row = check(maskedPan="not-needed", unrelated="private-data")
    gateway, _ = provider({"checks": [row], "token": "never-persist"})
    result = await gateway.get_fiscal_checks("invoice_1")
    assert "maskedPan" not in result["checks"][0] and "token" not in result
