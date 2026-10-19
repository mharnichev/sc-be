"""Read-only monobank invoice verification. No charging or invoice creation.

Wire contract: https://api.monobank.ua/docs/acquiring.html
Test transport is injected by the server harness; configuration cannot select it.
The merchant endpoint has no runtime is_test flag: environment is an explicitly
approved, token-fingerprint-pinned configuration, never a provider assertion.
"""
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
import re
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
            if profile["environment"] not in {"production", "test"} or profile["external_fiscalization_disabled"] is not True:
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
        if path not in {"/api/merchant/details", "/api/merchant/invoice/status"}:
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
        if len(response.content) > 100_000:
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
