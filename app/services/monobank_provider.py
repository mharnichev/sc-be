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
import logging
import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.core.config import settings
from app.services.order_documents import digest

logger = logging.getLogger(__name__)


class MonobankError(Exception):
    def __init__(self, code, *, stage=None, http_status=None, provider_code=None, invoice_candidate=None,
                 returned_page_url=None, url_rejection=None, url_shape=None):
        self.code = code
        self.stage = stage
        self.http_status = http_status
        self.provider_code = provider_code
        self.invoice_candidate = invoice_candidate
        self.returned_page_url = returned_page_url
        self.url_rejection = url_rejection
        self.url_shape = url_shape
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


def shop_iframe_page_url(invoice_id, value):
    """Validate the opaque returned Shop URL; invoice proof is checked separately."""
    _text(invoice_id, r"[A-Za-z0-9_-]{1,100}")
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or not value.startswith(("https://pay.mbnk.biz/", "https://pay.monobank.ua/"))
            or "\\" in value or "#" in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", value, re.IGNORECASE)):
        raise MonobankError("monobank_proof_invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc not in {"pay.mbnk.biz", "pay.monobank.ua"}:
        raise MonobankError("monobank_proof_invalid")
    if ("?" not in value and re.fullmatch(r"/[A-Za-z0-9_-]{1,100}", parsed.path)
            and parsed.path != "/" + invoice_id):
        raise MonobankError("monobank_proof_invalid")
    return value


MASTER_PAYMENT_URL_SOURCE = "mono_authenticated_create_v1"
MASTER_INVOICE_VALIDITY = 900
_LOCAL_PAYMENT_SUFFIXES = {"alt", "arpa", "corp", "example", "home", "internal", "intranet",
                           "invalid", "lan", "local", "localdomain", "localhost", "onion", "private", "test"}


def _public_payment_host(host):
    labels = host.split(".")
    return (len(host) <= 253 and len(labels) >= 2 and re.fullmatch(r"[a-z]{2,63}", labels[-1])
        and labels[-1] not in _LOCAL_PAYMENT_SUFFIXES
        and all(not label.startswith("xn--") and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels))


def master_returned_page_url(invoice_id, value, *, bank_selected=False):
    _text(invoice_id, r"[A-Za-z0-9_-]{1,100}")
    origin = "https://pay.mbnk.biz/"
    if value is None or value == "":
        raise MonobankError("monobank_proof_invalid", url_rejection="missing_url")
    if not isinstance(value, str):
        raise MonobankError("monobank_proof_invalid", url_rejection="invalid_url_type")
    if bank_selected:
        match = re.match(r"https://([^/]+)/", value, re.ASCII)
        if not match or not _public_payment_host(match[1]):
            raise MonobankError("monobank_proof_invalid", url_rejection="unsafe_payment_authority")
        origin = match[0]
    elif not value.startswith(origin):
        raise MonobankError("monobank_proof_invalid", url_rejection="unexpected_url_prefix")
    path, separator, query = value.removeprefix(origin).partition("?")
    # Keep the exact wire URL the browser can encode without serialization changes.
    # The ledger enforces the same policy before the payment URL becomes immutable.
    if len(value) > 240:
        reason = "url_too_long"
    elif re.search(r"[^!-~]|[#\\]|%(?:0[\da-f]|1[\da-f]|7f|5c)", value, re.ASCII | re.IGNORECASE):
        reason = "unsafe_url_character"
    elif re.search(r"%(?![0-9a-fA-F]{2})", value):
        reason = "malformed_escape"
    elif not (path or separator):
        reason = "empty_route"
    elif re.search(r'["<>`{}^|]', path) or re.search(r"[\"'<>]", query):
        reason = "browser_rewrites_url"
    elif re.search(r"(?:^|/)\.{1,2}(?:/|$)", path.lower().replace("%2e", ".")):
        reason = "dot_segment"
    else:
        return value
    raise MonobankError("monobank_proof_invalid", url_rejection=reason)


def _rejected_url_authority(value, parsed):
    # urllib can discard whitespace and controls. Never report a host recovered
    # by such normalization, or from an authority containing credentials.
    if "\\" in value or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return "unsafe_character", None
    authority = parsed.netloc
    if parsed.scheme not in {"http", "https"} or not authority:
        return "no_http_authority", None
    if "@" in authority:
        return "userinfo", None
    if not authority.isascii():
        return "non_ascii_authority", None
    if authority.startswith("["):
        _, _, port_suffix = authority.partition("]")
        if port_suffix and (not re.fullmatch(r":[0-9]{1,5}", port_suffix) or int(port_suffix[1:]) > 65535):
            return "malformed", None
        return "address_literal", None

    hostname, separator, port = authority.lower().partition(":")
    if separator and (not re.fullmatch(r"[0-9]{1,5}", port) or int(port) > 65535):
        return "malformed", None
    dns_name = hostname.removesuffix(".")
    labels = dns_name.split(".")
    if re.fullmatch(r"[0-9]+|0x[0-9a-f]*", labels[-1]):
        return "address_literal", None
    if (len(dns_name) > 253 or len(labels) < 2
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        return "malformed", None
    return ("dns_with_port" if separator else "dns"), hostname


def rejected_url_shape(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme if parsed.scheme in {"", "http", "https", "monobank"} else "other"
        known_host = parsed.hostname == "pay.mbnk.biz"
        path_segments = sum(bool(segment) for segment in parsed.path.split("/"))
        category, hostname = _rejected_url_authority(value, parsed)
    except ValueError:
        scheme, known_host, path_segments = None, False, None
        category, hostname = "malformed", None
    # This shape is stored only on the private dispatch intent. It never
    # authorizes a URL and must not contain any other part of the authority.
    result = {"length": len(value), "scheme": scheme or None, "known_host": known_host,
              "path_segments": path_segments, "has_query": "?" in value.partition("#")[0],
              "authority_category": category}
    if hostname is not None:
        result["hostname"] = hostname
    return result


def valid_invoice_text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def shop_invoice_icon(value):
    """Resolve only public product media from the stored order snapshot."""
    if not isinstance(value, str) or not 1 <= len(value) <= 1000:
        return None
    try:
        origin = urlsplit(settings.public_site_url)
    except ValueError:
        return None
    if (origin.scheme != "https" or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.path not in {"", "/"} or origin.query or origin.fragment):
        return None
    base = f"https://{origin.netloc}"
    path = value.removeprefix(base)
    prefix = settings.upload_url_prefix.rstrip("/") + "/products/"
    if not path.startswith(prefix):
        return None
    if not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.(?:webp|png|jpe?g)",
                        path[len(prefix):], re.IGNORECASE):
        return None
    return base + path


def invoice_payload(reference, amount_minor, basket, validity=900, *, max_validity=86400):
    """Validate a server-priced basket before claiming a one-shot dispatch."""
    try:
        if not isinstance(reference, str) or str(UUID(reference)) != reference:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MonobankError("monobank_proof_invalid") from None
    _integer(amount_minor, positive=True)
    if amount_minor > 2**53 - 1 or type(validity) is not int or not 60 <= validity <= max_validity:
        raise MonobankError("monobank_proof_invalid")
    if not isinstance(basket, list) or not 1 <= len(basket) <= 200:
        raise MonobankError("monobank_proof_invalid")
    normalized = []
    for item in basket:
        if not isinstance(item, dict) or set(item) - {"icon"} != {"name", "qty", "sum", "total", "code", "unit", "tax"}:
            raise MonobankError("monobank_proof_invalid")
        if "icon" in item and (not item["icon"] or shop_invoice_icon(item["icon"]) != item["icon"]):
            raise MonobankError("monobank_proof_invalid")
        for key, limit in (("name", 128), ("code", 100), ("unit", 16)):
            if not valid_invoice_text(item[key], limit):
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
    def __init__(self, config=settings, *, transport=None, create_capability="master"):
        self.create_capability = create_capability
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

    async def _get(self, path, *, params=None, keep_inflight=False):
        self._guard()
        allowed = {"/api/merchant/details", "/api/merchant/invoice/status", "/api/merchant/statement"}
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
        if not keep_inflight:
            self._guard()
        if response.status_code == 404:
            raise MonobankError("monobank_not_found", http_status=404)
        if response.status_code != 200:
            raise MonobankError("monobank_unavailable", http_status=response.status_code)
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

    async def deactivate_unpaid_shop_invoice(self, invoice_id, *, reference, amount_minor, before_dispatch=None):
        """Require matching unpaid status and an explicit 200 acknowledgement from Mono."""
        if self.create_capability != "shop":
            raise MonobankError("monobank_proof_invalid")
        proof = await self.inspect_invoice(invoice_id)
        if (
            proof["reference"] != reference
            or proof["amount_minor"] != amount_minor
            or proof["currency_code"] != 980
            or proof["status"] != "created"
            or proof["final_amount_minor"] not in (None, 0)
            or proof["has_cancellations"]
        ):
            raise MonobankError("monobank_invoice_not_removable")
        self._guard()
        if before_dispatch is not None:
            await before_dispatch()
        self._guard()
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(10, connect=5), verify=True,
            ) as client:
                response = await client.post(
                    "https://api.monobank.ua/api/merchant/invoice/remove",
                    json={"invoiceId": invoice_id},
                    headers={"X-Token": self.config.shop_monobank_token.get_secret_value()},
                )
        except httpx.HTTPError:
            raise MonobankError("monobank_invoice_removal_unconfirmed") from None
        body = response.content
        acknowledged = not body or (
            len(body) <= 1024 and re.fullmatch(rb"[ \t\r\n]*\{[ \t\r\n]*\}[ \t\r\n]*", body)
        )
        if response.status_code != 200 or not acknowledged:
            raise MonobankError(
                "monobank_invoice_removal_unconfirmed", http_status=response.status_code,
            )
        return {**proof, "removal_http_status": 200, "removed_at": datetime.now(UTC).isoformat()}

    async def invoice_candidates(self, reference, *, start, end):
        """Statement matches are leads only; an empty statement proves nothing."""
        if not (start.tzinfo and end.tzinfo and timedelta(0) < end - start <= timedelta(days=7)):
            raise MonobankError("monobank_proof_invalid")
        await self.readiness()
        data = await self._get("/api/merchant/statement",
            params={"from": int(start.timestamp()), "to": int(end.timestamp())})
        entries = data.get("list")
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise MonobankError("monobank_proof_invalid")
        matches = set()
        for item in entries:
            try:
                matches_reference = str(UUID(item.get("reference", ""))) == reference
            except (ValueError, AttributeError, TypeError):
                continue
            if matches_reference:
                matches.add(_text(item.get("invoiceId"), r"[A-Za-z0-9_-]{1,100}"))
        if len(matches) > 10:
            raise MonobankError("monobank_proof_invalid")
        return sorted(matches)

    async def readiness(self):
        data = await self._get("/api/merchant/details")
        if data.get("merchantId") != self.profile["merchant_id"]:
            raise MonobankError("monobank_profile_mismatch")
        return {"verified": True, "merchant_id": self.profile["merchant_id"], "profile_id": self.profile["id"],
            "authority": "test" if self.test_only else "provider", "transport": "mock_http" if self.transport is not None else "https"}

    def _create_guard(self):
        self._guard()
        flags = ("shop_online_payment_enabled", "shop_online_payment_invoice_create_enabled", "shop_online_payment_link_confirmed") if self.create_capability == "shop" else (
            "master_mono_enabled", "master_mono_invoice_create_enabled", "master_mono_checkbox_link_confirmed")
        if (self.create_capability not in {"shop", "master"} or not self.external_checkbox or self.profile["environment"] != "production"
                or any(getattr(self.config, flag, False) is not True for flag in flags)
                or (self.test_only and getattr(self.config, "app_env", None) not in {"local", "test"})
                or (not self.test_only and getattr(self.config, "app_env", None) not in {"production", "staging"})):
            raise MonobankError("monobank_invoice_create_disabled")

    async def create_invoice(self, reference, amount_minor, basket, validity=900, *, before_dispatch=None, presentation="redirect",
            on_dispatch=None, on_send=None, on_candidate=None):
        """One POST only. The caller must durably fence dispatch before invoking.

        Neither a transport error nor any HTTP response authorizes a retry:
        mono documents no idempotency guarantee for invoice/create.
        """
        if presentation not in {"redirect", "iframe"} or (presentation == "iframe" and self.create_capability != "shop"):
            raise MonobankError("monobank_invoice_create_disabled")
        self._create_guard()
        live_master = self.create_capability == "master" and not self.test_only
        payload = invoice_payload(reference, amount_minor, basket, MASTER_INVOICE_VALIDITY if live_master else validity,
            max_validity=30 * 86400 if self.create_capability == "shop" else 86400)
        if presentation == "iframe":
            payload["displayType"] = "iframe"
        try:
            identity = await self.readiness()
        except MonobankError as error:
            raise MonobankError(error.code, stage="merchant_readiness", http_status=error.http_status) from None
        if before_dispatch is not None:
            await before_dispatch()
        self._create_guard()
        try:
            async with httpx.AsyncClient(transport=self.transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(15, connect=5), verify=True) as client:
                if on_dispatch is not None:
                    await on_dispatch()
                self._create_guard()
                if on_send is not None:
                    on_send()
                response = await client.post("https://api.monobank.ua/api/merchant/invoice/create", json=payload,
                    headers={"X-Token": self.config.shop_monobank_token.get_secret_value(), "Accept": "application/json"})
        except httpx.HTTPError:
            raise MonobankError("monobank_invoice_create_unknown", stage="create_transport") from None
        if response.status_code != 200:
            provider_code = None
            if len(response.content) <= 100_000:
                try:
                    error_body = response.json()
                except ValueError:
                    error_body = None
                code = error_body.get("errCode") if isinstance(error_body, dict) else None
                if code in ("BAD_REQUEST", "FORBIDDEN", "NOT_FOUND", "TOO_MANY_REQUESTS", "EXTERNAL_ERROR", "INTERNAL_ERROR"):
                    provider_code = code
            raise MonobankError(
                "monobank_invoice_create_unknown", stage="create_http",
                http_status=response.status_code, provider_code=provider_code,
            )
        if len(response.content) > 100_000:
            raise MonobankError("monobank_invoice_create_unknown", stage="create_response_body", http_status=200)
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            invoice_id = _text(body.get("invoiceId"), r"[A-Za-z0-9_-]{1,100}")
        except (ValueError, TypeError, MonobankError):
            raise MonobankError("monobank_invoice_create_unknown", stage="create_response_body", http_status=200) from None
        if on_candidate is not None:
            await on_candidate(invoice_id)
        # Keep the acknowledgement of an already-authorized live master POST even after a pause.
        if not live_master:
            try:
                self._create_guard()
            except MonobankError as error:
                raise MonobankError(
                    error.code, stage="post_response_guard", http_status=response.status_code,
                ) from None
        iframe_fallback = False
        try:
            raw_url = body.get("pageUrl")
            if live_master:
                page_url = master_returned_page_url(invoice_id, raw_url, bank_selected=True)
            elif presentation == "iframe":
                try:
                    page_url = shop_iframe_page_url(invoice_id, raw_url)
                except (MonobankError, ValueError):
                    shape = rejected_url_shape(raw_url) or {}
                    hostname = shape.get("hostname", "").rstrip(".")
                    # Log only fixed public domains: subdomains may contain payment identifiers.
                    domain = next(
                        (root for root in ("mbnk.biz", "monobank.ua")
                         if hostname == root or hostname.endswith("." + root)),
                        None,
                    )
                    logger.warning("Shop Monobank iframe URL rejected: domain=%s", domain)
                    # The independently verified invoice can still use its canonical hosted page.
                    page_url = payment_page_url(invoice_id)
                    iframe_fallback = True
            else:
                try:
                    if not isinstance(raw_url, str):
                        raise ValueError
                    page_url = payment_page_url(invoice_id, raw_url)
                except (ValueError, MonobankError):
                    if self.create_capability != "shop":
                        raise
                    # Use only the canonical hosted link after verifying the existing invoice.
                    await self.get_invoice(invoice_id, reference=reference, amount_minor=amount_minor)
                    self._create_guard()
                    page_url = payment_page_url(invoice_id)
        except (ValueError, TypeError, MonobankError) as error:
            returned_page_url = None
            url_rejection = error.url_rejection if live_master and isinstance(error, MonobankError) else None
            if not live_master and presentation == "redirect" and self.create_capability == "master":
                try:
                    returned_page_url = master_returned_page_url(invoice_id, raw_url)
                except MonobankError as error:
                    url_rejection = error.url_rejection
            raise MonobankError(
                "monobank_invoice_create_unknown", stage="create_response_url",
                http_status=200, invoice_candidate=invoice_id, returned_page_url=returned_page_url,
                url_rejection=url_rejection,
                url_shape=rejected_url_shape(raw_url) if url_rejection else None,
            ) from None
        result = {**identity, "invoice_id": invoice_id, "page_url": page_url, "reference": reference,
            "amount_minor": amount_minor, "currency": "UAH"}
        if live_master:
            result.update(payment_url_source=MASTER_PAYMENT_URL_SOURCE, validity_seconds=payload["validity"])
        if presentation == "iframe":
            verification = await self.get_invoice(invoice_id, reference=reference, amount_minor=amount_minor)
            self._create_guard()
            result.update(display_type="iframe", invoice_verification=verification)
            if iframe_fallback:
                result["iframe_fallback"] = True
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


    async def inspect_invoice(self, invoice_id, *, preserve_inflight=False):
        """Inspect identity/status without claiming this invoice paid any order.

        Foreign references and amounts are retained minimally for reconciliation.
        No status, 404, or timeout proves irreversible financial closure.
        """
        _text(invoice_id, r"[A-Za-z0-9_-]{1,100}")
        identity = await self.readiness()
        # An already-authorized live master status read can still yield terminal evidence after a pause.
        data = await self._get("/api/merchant/invoice/status", params={"invoiceId": invoice_id},
            keep_inflight=preserve_inflight)
        reference = data.get("reference")
        returned_id = _text(data.get("invoiceId"), r"[A-Za-z0-9_-]{1,100}$")
        if ((returned_id != invoice_id and not preserve_inflight) or not isinstance(reference, str)
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
        result = {**identity, "invoice_id": returned_id, "reference": reference,
            "amount_minor": amount, "currency_code": currency, "currency": "UAH" if currency == 980 else None,
            "final_amount_minor": final, "status": status, "created_at": created.isoformat(),
            "modified_at": modified.isoformat(), "observed_at": observed.isoformat(),
            "has_cancellations": bool(cancellations)}
        result["proof_hash"] = digest(result)
        return result
