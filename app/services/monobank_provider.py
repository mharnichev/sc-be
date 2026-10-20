"""Monobank verification and explicitly gated merchant invoice creation.

Wire contract: https://api.monobank.ua/docs/acquiring.html
Test transport is injected by the server harness; configuration cannot select it.
The merchant endpoint has no runtime is_test flag: environment is an explicitly
approved, token-fingerprint-pinned configuration, never a provider assertion.
"""
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
import base64
import binascii
import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.core.config import settings
from app.services.order_documents import digest


class MonobankError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise MonobankError("monobank_proof_invalid")
    return value


def _integer(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise MonobankError("monobank_proof_invalid")
    return value


def _date(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        raise MonobankError("monobank_proof_invalid") from None


def payment_page_url(invoice_id, value=None):
    """Only the documented mono payment host and this exact invoice identity."""
    _text(invoice_id, r"[A-Za-z0-9_-]{1,100}")
    expected = "https://pay.mbnk.biz/" + invoice_id
    if value is not None and value != expected:
        raise MonobankError("monobank_proof_invalid")
    return expected


def invoice_payload(reference, amount_minor, basket, validity=900):
    """Validate a server-priced basket before claiming a one-shot dispatch."""
    try:
        if not isinstance(reference, str) or str(UUID(reference)) != reference:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MonobankError("monobank_proof_invalid") from None
    _integer(amount_minor, positive=True)
    if amount_minor > 2**53 - 1 or type(validity) is not int or not 60 <= validity <= 86400:
        raise MonobankError("monobank_proof_invalid")
    if not isinstance(basket, list) or not 1 <= len(basket) <= 200:
        raise MonobankError("monobank_proof_invalid")
    normalized = []
    for item in basket:
        if not isinstance(item, dict) or set(item) != {"name", "qty", "sum", "total", "code", "unit", "tax"}:
            raise MonobankError("monobank_proof_invalid")
        for key, limit in (("name", 128), ("code", 100), ("unit", 16)):
            value = item[key]
            if (not isinstance(value, str) or not value or value != value.strip() or len(value) > limit
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise MonobankError("monobank_proof_invalid")
        quantity, price, total = (_integer(item[key], positive=True) for key in ("qty", "sum", "total"))
        tax = item["tax"]
        if (quantity > 10000 or price > 2**53 - 1 or total != quantity * price
                or not isinstance(tax, list) or not 1 <= len(tax) <= 10
                or any(type(code) is not int or not 0 <= code <= 99 for code in tax)
                or len(set(tax)) != len(tax)):
            raise MonobankError("monobank_proof_invalid")
        normalized.append({**item, "tax": list(tax)})
    if sum(item["total"] for item in normalized) != amount_minor:
        raise MonobankError("monobank_proof_invalid")
    return {"amount": amount_minor, "ccy": 980, "paymentType": "debit", "validity": validity,
        "merchantPaymInfo": {"reference": reference, "destination": "Soul Cuts — товари", "basketOrder": normalized}}


class MonobankGateway:
    def __init__(self, config=settings, *, transport=None):
        self.config = config
        self.transport = transport
        self.profile = None
        try:
            profile = dict(config.shop_monobank_profile or {})
            if set(profile) != {"id", "merchant_id", "token_sha256", "environment", "external_fiscalization_disabled"}:
                raise ValueError
            _text(profile["id"], r"[A-Za-z0-9_-]{1,64}")
            _text(profile["merchant_id"], r"[A-Za-z0-9_-]{1,100}")
            _text(profile["token_sha256"], r"[0-9a-f]{64}")
            external_disabled = profile["external_fiscalization_disabled"]
            if (profile["environment"] not in {"production", "test"}
                    or type(external_disabled) is not bool
                    or (not external_disabled and getattr(config, "shop_mono_checkbox_enabled", False) is not True)):
                raise ValueError
            token = config.shop_monobank_token
            token = token.get_secret_value() if hasattr(token, "get_secret_value") else ""
            if not token or not compare_digest(sha256(token.encode()).hexdigest(), profile["token_sha256"]):
                raise ValueError
            if transport is not None and type(transport) is not httpx.MockTransport:
                raise ValueError
            self.profile = profile
        except (AttributeError, TypeError, ValueError, MonobankError):
            pass

    @property
    def external_checkbox(self):
        return bool(self.profile and self.profile["external_fiscalization_disabled"] is False)

    @property
    def test_only(self):
        return self.transport is not None or not self.profile or self.profile["environment"] != "production"

    @property
    def available(self):
        return bool(self.profile and (type(self.transport) is httpx.MockTransport or (
            self.config.shop_monobank_live_transport_enabled is True and self.profile["environment"] == "production")))

    def _guard(self):
        # Reconstruct to recheck the currently configured token/profile and flag.
        current = MonobankGateway(self.config, transport=self.transport)
        if not self.available or not current.available:
            raise MonobankError("monobank_unconfigured")
        if current.profile != self.profile:
            raise MonobankError("monobank_profile_mismatch")

    async def _get(self, path, *, params=None):
        self._guard()
        allowed = {"/api/merchant/details", "/api/merchant/invoice/status"}
        if self.external_checkbox:
            allowed.add("/api/merchant/invoice/fiscal-checks")
        if path not in allowed:
            raise MonobankError("monobank_proof_invalid")
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(10, connect=5), verify=True) as client:
                response = await client.get("https://api.monobank.ua" + path, params=params,
                    headers={"X-Token": self.config.shop_monobank_token.get_secret_value(), "Accept": "application/json"})
        except httpx.HTTPError:
            raise MonobankError("monobank_unavailable") from None
        self._guard()
        if response.status_code == 404:
            raise MonobankError("monobank_not_found")
        if response.status_code != 200:
            raise MonobankError("monobank_unavailable")
        limit = 2_000_000 if path == "/api/merchant/invoice/fiscal-checks" else 100_000
        if len(response.content) > limit:
            raise MonobankError("monobank_proof_invalid")
        try:
            data = response.json()
        except ValueError:
            raise MonobankError("monobank_proof_invalid") from None
        if not isinstance(data, dict):
            raise MonobankError("monobank_proof_invalid")
        return data

    async def readiness(self):
        data = await self._get("/api/merchant/details")
        if data.get("merchantId") != self.profile["merchant_id"]:
            raise MonobankError("monobank_profile_mismatch")
        return {"verified": True, "merchant_id": self.profile["merchant_id"], "profile_id": self.profile["id"],
            "authority": "test" if self.test_only else "provider", "transport": "mock_http" if self.transport is not None else "https"}

    def _create_guard(self):
        self._guard()
        if (not self.external_checkbox or self.profile["environment"] != "production"
                or any(getattr(self.config, flag, False) is not True for flag in (
                    "master_mono_enabled", "master_mono_invoice_create_enabled", "master_mono_checkbox_link_confirmed"))
                or (self.test_only and getattr(self.config, "app_env", None) not in {"local", "test"})
                or (not self.test_only and getattr(self.config, "app_env", None) not in {"production", "staging"})):
            raise MonobankError("monobank_invoice_create_disabled")

    async def create_invoice(self, reference, amount_minor, basket, validity=900):
        """One POST only. The caller must durably fence dispatch before invoking.

        Neither a transport error nor any HTTP response authorizes a retry:
        mono documents no idempotency guarantee for invoice/create.
        """
        self._create_guard()
        payload = invoice_payload(reference, amount_minor, basket, validity)
        identity = await self.readiness()
        self._create_guard()
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(15, connect=5), verify=True) as client:
                response = await client.post("https://api.monobank.ua/api/merchant/invoice/create", json=payload,
                    headers={"X-Token": self.config.shop_monobank_token.get_secret_value(), "Accept": "application/json"})
        except httpx.HTTPError:
            raise MonobankError("monobank_invoice_create_unknown") from None
        self._create_guard()
        if response.status_code != 200 or len(response.content) > 100_000:
            raise MonobankError("monobank_invoice_create_unknown")
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            invoice_id = _text(body.get("invoiceId"), r"[A-Za-z0-9_-]{1,100}")
            page_url = payment_page_url(invoice_id, body.get("pageUrl"))
            if not isinstance(body.get("pageUrl"), str):
                raise ValueError
        except (ValueError, TypeError, MonobankError):
            raise MonobankError("monobank_invoice_create_unknown") from None
        result = {**identity, "invoice_id": invoice_id, "page_url": page_url, "reference": reference,
            "amount_minor": amount_minor, "currency": "UAH"}
        result["proof_hash"] = digest(result)
        return result

    async def get_invoice(self, invoice_id, *, reference, amount_minor):
        _text(invoice_id, r"[A-Za-z0-9_-]{1,100}")
        try:
            if not isinstance(reference, str) or str(UUID(reference)) != reference:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise MonobankError("monobank_proof_invalid") from None
        _integer(amount_minor, positive=True)
        identity = await self.readiness()
        data = await self._get("/api/merchant/invoice/status", params={"invoiceId": invoice_id})
        if (data.get("invoiceId") != invoice_id or data.get("reference") != reference
                or type(data.get("ccy")) is not int or data["ccy"] != 980
                or _integer(data.get("amount"), positive=True) != amount_minor):
            raise MonobankError("monobank_proof_invalid")
        status = data.get("status")
        if status not in {"created", "processing", "hold", "success", "failure", "expired", "reversed"}:
            raise MonobankError("monobank_proof_invalid")
        created, modified = _date(data.get("createdDate")), _date(data.get("modifiedDate"))
        if created > modified or modified > datetime.now(UTC) + timedelta(seconds=30):
            raise MonobankError("monobank_proof_invalid")
        cancellations = data.get("cancelList", [])
        if not isinstance(cancellations, list) or cancellations:
            # Partial/processing/reversed money needs explicit reconciliation.
            raise MonobankError("monobank_reversed")
        if status == "success" and _integer(data.get("finalAmount"), positive=True) != amount_minor:
            raise MonobankError("monobank_proof_invalid")
        result = {**identity, "invoice_id": invoice_id, "reference": reference, "amount_minor": amount_minor,
            "currency": "UAH", "status": status, "created_at": created.isoformat(),
            "modified_at": modified.isoformat(), "accepted_at": modified.isoformat() if status == "success" else None}
        result["proof_hash"] = digest(result)
        return result

    async def get_fiscal_checks(self, invoice_id):
        """Read provider-associated checks; never infer receipt totals from status.

        This API has no structured goods/tax/amount fields. Consumers must bind
        it to the independently verified invoice and retain that evidence limit.
        A bank payment receipt is deliberately not used as a fiscal check.
        """
        self._guard()
        if not self.external_checkbox:
            raise MonobankError("monobank_external_fiscalization_disabled")
        _text(invoice_id, r"[A-Za-z0-9_-]{1,100}")
        identity = await self.readiness()
        data = await self._get("/api/merchant/invoice/fiscal-checks", params={"invoiceId": invoice_id})
        checks = data.get("checks")
        if not isinstance(checks, list) or len(checks) > 100:
            raise MonobankError("monobank_proof_invalid")
        normalized, identifiers = [], set()
        for check in checks:
            if not isinstance(check, dict):
                raise MonobankError("monobank_proof_invalid")
            try:
                identifier = str(UUID(check["id"]))
            except (ValueError, TypeError, KeyError, AttributeError):
                raise MonobankError("monobank_proof_invalid") from None
            if (identifier != check["id"] or identifier in identifiers
                    or any(not isinstance(check.get(k), str) for k in ("status", "type", "fiscalizationSource"))
                    or check.get("status") not in {"new", "process", "done", "failed"}
                    or check.get("type") not in {"sale", "return"}
                    or check.get("fiscalizationSource") not in {"checkbox", "monopay"}):
                raise MonobankError("monobank_proof_invalid")
            identifiers.add(identifier)
            row = {k: check[k] for k in ("id", "status", "type", "fiscalizationSource")}
            description = check.get("statusDescription")
            if description is not None:
                if (not isinstance(description, str) or len(description) > 1000
                        or any(ord(c) < 32 or ord(c) == 127 for c in description)):
                    raise MonobankError("monobank_proof_invalid")
                row["statusDescription"] = description
            tax_url = check.get("taxUrl")
            if tax_url:
                if not isinstance(tax_url, str) or len(tax_url) > 2048:
                    raise MonobankError("monobank_proof_invalid")
                try:
                    parsed = urlsplit(tax_url)
                except ValueError:
                    raise MonobankError("monobank_proof_invalid") from None
                if (parsed.scheme != "https" or parsed.netloc != "cabinet.tax.gov.ua"
                        or not parsed.path.startswith("/cashregs/")
                        or any(ord(c) < 33 or ord(c) == 127 for c in tax_url)):
                    raise MonobankError("monobank_proof_invalid")
                row["taxUrl"] = tax_url
            elif tax_url is not None and tax_url != "":
                raise MonobankError("monobank_proof_invalid")
            file = check.get("file")
            if file:
                if not isinstance(file, str) or len(file) > 1_400_000:
                    raise MonobankError("monobank_proof_invalid")
                try:
                    pdf = base64.b64decode(file, validate=True)
                except (ValueError, binascii.Error):
                    raise MonobankError("monobank_proof_invalid") from None
                if not pdf.startswith(b"%PDF-") or len(pdf) > 1_000_000:
                    raise MonobankError("monobank_proof_invalid")
                row["file"] = file
            elif file is not None and file != "":
                raise MonobankError("monobank_proof_invalid")
            normalized.append(row)
        result = {**identity, "invoice_id": invoice_id, "checks": normalized,
                  "verification_scope": "provider_invoice_association"}
        result["proof_hash"] = digest(result)
        return result


    async def inspect_invoice(self, invoice_id):
        """Inspect identity/status without claiming this invoice paid any order.

        Foreign references and amounts are retained minimally for reconciliation.
        No status, 404, or timeout proves irreversible financial closure.
        """
        _text(invoice_id, r"[A-Za-z0-9_-]{1,100}")
        identity = await self.readiness()
        data = await self._get("/api/merchant/invoice/status", params={"invoiceId": invoice_id})
        reference = data.get("reference")
        if (data.get("invoiceId") != invoice_id or not isinstance(reference, str)
                or not reference or reference != reference.strip() or len(reference) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in reference)):
            raise MonobankError("monobank_proof_invalid")
        amount = _integer(data.get("amount"), positive=True)
        currency = _integer(data.get("ccy"), positive=True)
        if currency > 999:
            raise MonobankError("monobank_proof_invalid")
        status = data.get("status")
        if not isinstance(status, str) or status not in {"created", "processing", "hold", "success", "failure", "expired", "reversed"}:
            raise MonobankError("monobank_proof_invalid")
        created, modified = _date(data.get("createdDate")), _date(data.get("modifiedDate"))
        observed = datetime.now(UTC)
        if created > modified or modified > observed + timedelta(seconds=30):
            raise MonobankError("monobank_proof_invalid")
        cancellations = data.get("cancelList", [])
        if not isinstance(cancellations, list):
            raise MonobankError("monobank_proof_invalid")
        final = data.get("finalAmount")
        if final is not None:
            final = _integer(final)
        elif status == "success":
            raise MonobankError("monobank_proof_invalid")
        result = {**identity, "invoice_id": invoice_id, "reference": reference,
            "amount_minor": amount, "currency_code": currency, "currency": "UAH" if currency == 980 else None,
            "final_amount_minor": final, "status": status, "created_at": created.isoformat(),
            "modified_at": modified.isoformat(), "observed_at": observed.isoformat(),
            "has_cancellations": bool(cancellations)}
        result["proof_hash"] = digest(result)
        return result
