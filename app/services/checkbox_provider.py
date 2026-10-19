"""Checkbox online wire client. No schedulers, delivery, acquiring or automatic retries.

Local verification injects httpx.MockTransport; it never produces provider authority.
The domain owns durable dispatch fences and verifies its actual database binding.
"""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from uuid import UUID

import httpx

from app.core.config import settings

HOST = "https://api.checkbox.ua"
transport_factory = None  # Explicit test-harness injection; not configurable via API/env.


class CheckboxProviderError(Exception):
    def __init__(self, code, *, dispatched=False):
        super().__init__(code)
        self.code, self.dispatched = code, dispatched


def bad(code="checkbox_proof_mismatch"):
    raise CheckboxProviderError(code)


def mapping(value):
    if not isinstance(value, dict):
        bad()
    return value


def uuid_string(value, *, version=None):
    if not isinstance(value, str):
        bad()
    try:
        parsed = UUID(value)
    except (ValueError, TypeError):
        bad()
    if (version is not None and parsed.version != version) or str(parsed) != value.lower():
        bad()
    return str(parsed)


def integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        bad()
    return value


def minor_number(value):
    """Provider monetary number in kopecks; never silently truncate fractions."""
    if type(value) not in (int, float, Decimal):
        bad()
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        bad()
    return int(number)


def instant(value):
    if not isinstance(value, str):
        bad()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        bad()
    if not parsed.tzinfo:
        bad()
    return parsed


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def normalized_profile(value):
    p = deepcopy(value)
    required = {"id", "version", "organization_id", "register_id", "cashier_id", "approved",
                "exclusive_register", "currency", "taxes", "product_tax_codes", "tenders", "phase_policy"}
    if not isinstance(p, dict) or set(p) != required or not isinstance(p.get("id"), str) or not p["id"].strip() or len(p["id"]) > 100:
        bad("checkbox_profile_unconfigured")
    integer(p["version"], minimum=1)
    for field in ("organization_id", "register_id", "cashier_id"):
        p[field] = uuid_string(p[field])
    if p["approved"] is not True or p["exclusive_register"] is not True or p["currency"] != "UAH":
        bad("checkbox_profile_unconfigured")
    if (not isinstance(p["taxes"], dict) or not p["taxes"] or not isinstance(p["product_tax_codes"], dict)
            or not p["product_tax_codes"] or not isinstance(p["tenders"], dict) or not p["tenders"]):
        bad("checkbox_tax_profile_invalid")
    for code, tax in p["taxes"].items():
        if not isinstance(code, str):
            bad("checkbox_tax_profile_invalid")
        try:
            if str(int(code)) != code:
                bad("checkbox_tax_profile_invalid")
        except ValueError:
            bad("checkbox_tax_profile_invalid")
        if not isinstance(tax, dict) or set(tax) != {"rate", "no_vat", "included", "label", "symbol"}:
            bad("checkbox_tax_profile_invalid")
        try:
            rate = Decimal(str(tax["rate"]))
        except (InvalidOperation, TypeError, ValueError):
            bad("checkbox_tax_profile_invalid")
        if (not rate.is_finite() or not 0 <= rate <= 100 or type(tax["no_vat"]) is not bool
                or tax["included"] is not True or not isinstance(tax["label"], str) or not tax["label"]
                or not isinstance(tax["symbol"], str) or not tax["symbol"] or tax["no_vat"] and rate != 0):
            bad("checkbox_tax_profile_invalid")
        tax["rate"] = str(rate.normalize())
    for product, codes in p["product_tax_codes"].items():
        if not isinstance(product, str) or not product.isdecimal() or not isinstance(codes, list) or len(codes) != 1 or any(type(c) is not int or str(c) not in p["taxes"] for c in codes):
            bad("checkbox_tax_profile_invalid")  # Compound/excise taxes deliberately unsupported.
    for key, tender in p["tenders"].items():
        if (not isinstance(key, str) or not key or not isinstance(tender, dict)
                or set(tender) != {"type", "label", "code"} or tender["type"] not in ("CASH", "CASHLESS")
                or not isinstance(tender["label"], str) or not tender["label"]
                or type(tender["code"]) is not int or tender["code"] < 0):
            bad("checkbox_payment_mapping_invalid")
    policy = p["phase_policy"]
    if (not isinstance(policy, dict) or set(policy) != {"pickup", "nova_poshta", "allow_cod_dispatch_without_receipt"}
            or policy["pickup"] != "paid_before_handover" or policy["nova_poshta"] != "cod_after_payment"
            or type(policy["allow_cod_dispatch_without_receipt"]) is not bool):
        bad("checkbox_profile_unconfigured")
    return p


def build_receipt_payload(provider_uuid, snapshot, profile):
    """Exact kopecks/thousandths, per-line value discounts, no delivery metadata."""
    identifier = uuid_string(provider_uuid, version=4)
    mapping(snapshot)
    if snapshot.get("kind") not in ("sale", "return") or snapshot.get("currency") != "UAH":
        bad()
    amount = integer(snapshot.get("amount_minor"), minimum=1)
    goods, seen = [], set()
    if not isinstance(snapshot.get("items"), list):
        bad()
    for line in snapshot["items"]:
        mapping(line)
        item = integer(line.get("order_item_id"), minimum=1)
        if item in seen:
            bad()
        seen.add(item)
        quantity = integer(line.get("quantity"), minimum=1)
        price = integer(line.get("unit_price_minor"))
        total = integer(line.get("total_minor"))
        codes = profile["product_tax_codes"].get(str(line.get("product_id")))
        if codes is None or codes != line.get("tax_codes"):
            bad("checkbox_tax_profile_invalid")
        if not isinstance(line.get("name"), str) or not line["name"].strip() or total > price * quantity:
            bad()
        row = {"good": {"code": f"SC-{item}", "name": line["name"], "price": price, "tax": codes},
               "quantity": quantity * 1000, "is_return": snapshot["kind"] == "return"}
        discount = price * quantity - total
        if discount:
            row["discounts"] = [{"type": "DISCOUNT", "mode": "VALUE", "value": discount,
                                 "tax_code": codes[0], "name": "Знижка"}]
        goods.append(row)
    if not goods or sum(line["total_minor"] for line in snapshot["items"]) != amount:
        bad()
    payment = snapshot.get("payment")
    if (not isinstance(payment, dict) or set(payment) != {"type", "label", "code", "value"}
            or {k: payment[k] for k in ("type", "label", "code")} not in profile["tenders"].values()
            or type(payment.get("value")) is not int or payment["value"] != amount):
        bad("checkbox_payment_mapping_invalid")
    result = {"id": identifier, "goods": goods, "payments": [deepcopy(payment)], "rounding": False}
    # Cash rounding is not implemented. Whole hryvnia amounts need no rounding.
    if payment["type"] == "CASH" and amount % 100:
        bad("checkbox_cash_rounding_unsupported")
    if snapshot["kind"] == "return":
        result["related_receipt_id"] = uuid_string(snapshot.get("related_receipt_id"))
    elif snapshot.get("related_receipt_id") is not None:
        bad()
    return result


def _secret(value):
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value or ""


class CheckboxGateway:
    def __init__(self, profile=None, *, login="", password="", license_key="", transport=None, live_enabled=False):
        self.profile, self.blocking_reasons = None, []
        self._login, self._password, self._license = _secret(login), _secret(password), _secret(license_key)
        self._transport = transport
        self.test_only = transport is not None
        self.transport = "mock_http" if self.test_only else "live"
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            self.blocking_reasons.append("checkbox_transport_invalid")
        try:
            self.profile = normalized_profile(profile)
        except CheckboxProviderError as error:
            self.blocking_reasons.append(error.code)
        if not all((self._login, self._password, self._license)):
            self.blocking_reasons.append("checkbox_credentials_required")
        if not self.test_only and not live_enabled:
            self.blocking_reasons.append("checkbox_live_transport_disabled")
        if self.test_only and settings.app_env not in {"local", "test"}:
            self.blocking_reasons.append("checkbox_test_unsafe")
        self.available = not self.blocking_reasons
        if not self.available:
            self.transport = "unavailable"

    def _guard(self):
        if not self.available:
            raise CheckboxProviderError(self.blocking_reasons[0])
        if self.test_only and settings.app_env not in {"local", "test"}:
            bad("checkbox_test_unsafe")

    async def _request(self, method, path, *, headers=None, body=None, missing=False, fiscal_write=False):
        self._guard()
        if not path.startswith("/api/v1/") or "?" in path or ".." in path:
            bad()
        safe_headers = {"X-Client-Name": "Soulcuts-Shop", "X-Client-Version": "1.0", "Accept": "application/json", **(headers or {})}
        try:
            async with httpx.AsyncClient(transport=self._transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(10, connect=5), limits=httpx.Limits(max_connections=2, max_keepalive_connections=0)) as client:
                response = await client.request(method, HOST + path, headers=safe_headers,
                    **({"json": body} if body is not None else {}))
        except httpx.HTTPError:
            raise CheckboxProviderError("checkbox_provider_unavailable", dispatched=fiscal_write) from None
        if missing and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise CheckboxProviderError("checkbox_provider_rejected", dispatched=fiscal_write)
        if len(response.content) > 2_000_000:
            raise CheckboxProviderError("checkbox_proof_mismatch", dispatched=fiscal_write)
        try:
            data = response.json()
        except ValueError:
            raise CheckboxProviderError("checkbox_proof_mismatch", dispatched=fiscal_write) from None
        if not isinstance(data, (dict, list)):
            raise CheckboxProviderError("checkbox_proof_mismatch", dispatched=fiscal_write)
        return data

    def _base(self, resource, identifier, status):
        return {"resource": resource, "id": identifier, "status": status, "verified": True, "accepted": False,
            "organization_id": self.profile["organization_id"], "register_id": self.profile["register_id"],
            "cashier_id": self.profile["cashier_id"], "authority": "test" if self.test_only else "provider", "transport": self.transport}

    @staticmethod
    def _after_write(parser, *args):
        try:
            return parser(*args)
        except CheckboxProviderError as error:
            error.dispatched = True
            raise

    def _taxes(self, rows, *, selected=None):
        if not isinstance(rows, list):
            bad("checkbox_tax_profile_invalid")
        indexed = {}
        for tax in rows:
            if not isinstance(tax, dict) or type(tax.get("code")) is not int or tax["code"] in indexed:
                bad("checkbox_tax_profile_invalid")
            if tax.get("is_gambling") not in (None, False) or tax.get("type") not in (None, "INCLUDED"):
                bad("checkbox_tax_profile_invalid")
            indexed[tax["code"]] = tax
        for code in selected if selected is not None else [int(c) for c in self.profile["taxes"]]:
            expected, actual = self.profile["taxes"].get(str(code)), indexed.get(code)
            if not expected or not actual:
                bad("checkbox_tax_profile_invalid")
            try:
                same_rate = Decimal(str(actual.get("rate"))) == Decimal(expected["rate"])
                no_extra = actual.get("extra_rate") is None or Decimal(str(actual["extra_rate"])) == 0
            except InvalidOperation:
                bad("checkbox_tax_profile_invalid")
            if (not same_rate or not no_extra or actual.get("no_vat") is not expected["no_vat"]
                    or actual.get("included") is not expected["included"] or actual.get("label") != expected["label"]
                    or actual.get("symbol") != expected["symbol"]):
                bad("checkbox_tax_profile_invalid")
        return indexed

    async def _identity(self):
        register = await self._request("GET", "/api/v1/cash-registers/info", headers={"X-License-Key": self._license})
        if (not isinstance(register, dict) or register.get("id") != self.profile["register_id"]
                or register.get("organization_id") != self.profile["organization_id"]
                or register.get("is_test") is not self.test_only
                or register.get("offline_mode") is not False or register.get("stay_offline") is not False):
            bad("checkbox_identity_mismatch")
        auth = await self._request("POST", "/api/v1/cashier/signin", body={"login": self._login, "password": self._password})
        token = auth.get("access_token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not token or len(token) > 8192:
            bad("checkbox_identity_mismatch")
        headers = {"Authorization": "Bearer " + token, "X-License-Key": self._license}
        cashier = await self._request("GET", "/api/v1/cashier/me", headers=headers)
        if (not isinstance(cashier, dict) or cashier.get("id") != self.profile["cashier_id"]
                or cashier.get("is_test") is not self.test_only or mapping(cashier.get("organization")).get("id") != self.profile["organization_id"]):
            bad("checkbox_identity_mismatch")
        taxes = await self._request("GET", "/api/v1/cashier/tax", headers=headers)
        self._taxes(taxes)
        return headers

    async def verify_identity(self):
        await self._identity()
        return {**self._base("identity", self.profile["register_id"], "VERIFIED"), "accepted": True, "is_test": self.test_only}

    @staticmethod
    def _transaction_done(transaction):
        return bool(isinstance(transaction, dict) and transaction.get("status") == "DONE"
                    and transaction.get("response_status") == "OK" and transaction.get("response_id")
                    and not transaction.get("offline_id"))

    def _shift(self, data, identifier):
        if (not isinstance(data, dict) or data.get("id") != identifier
                or mapping(data.get("cash_register")).get("id") != self.profile["register_id"]
                or mapping(data.get("cashier")).get("id") != self.profile["cashier_id"]
                or "is_test" in data and data["is_test"] is not self.test_only):
            bad()
        status = data.get("status")
        if status not in ("CREATED", "OPENING", "OPENED", "CLOSING", "CLOSED"):
            bad()
        result = self._base("shift", identifier, status)
        result.update(shift_id=identifier, is_sent_dps=False if self.test_only else None, z_report=None)
        transaction = data.get("closing_transaction" if status == "CLOSED" else "initial_transaction")
        result["accepted"] = status in {"OPENED", "CLOSED"} and self._transaction_done(transaction)
        if not self.test_only and result["accepted"]:
            result["is_sent_dps"] = True
        if status == "CLOSED" and result["accepted"]:
            if not isinstance(data.get("z_report"), dict):
                bad()
            result["z_report"] = self._report(data["z_report"], identifier)
            if not result["z_report"]["is_z_report"]:
                bad()
        return result

    async def open_shift(self, provider_uuid):
        identifier = uuid_string(provider_uuid, version=4);headers = await self._identity()
        current = await self._request("GET", "/api/v1/cashier/shift", headers=headers, missing=True)
        if current and mapping(current).get("status") in ("CREATED", "OPENING", "OPENED", "CLOSING"):
            bad("checkbox_shift_conflict")
        data = await self._request("POST", "/api/v1/shifts", headers=headers, body={"id": identifier}, fiscal_write=True)
        return self._after_write(self._shift, data, identifier)

    async def get_shift(self, provider_uuid):
        identifier = uuid_string(provider_uuid);headers = await self._identity()
        data = await self._request("GET", "/api/v1/shifts/" + identifier, headers=headers, missing=True)
        return self._shift(data, identifier) if data else None

    async def close_shift(self, original_shift_uuid):
        identifier = uuid_string(original_shift_uuid);headers = await self._identity()
        current = await self._request("GET", "/api/v1/cashier/shift", headers=headers, missing=True)
        if not current or current.get("id") != identifier or self._shift(current, identifier)["status"] != "OPENED":
            bad("checkbox_shift_conflict")
        data = await self._request("POST", "/api/v1/shifts/close", headers=headers, fiscal_write=True)
        return self._after_write(self._shift, data, identifier)

    def _receipt(self, data, identifier, snapshot):
        expected = build_receipt_payload(identifier, snapshot, self.profile)
        if not isinstance(data, dict) or data.get("id") != identifier or data.get("type") not in ("SELL", "RETURN"):
            bad()
        if snapshot["kind"] == "sale" and data["type"] != "SELL":
            bad()
        shift_id = uuid_string(snapshot.get("shift_id"))
        for field, expected_id in (("shift_id",shift_id),("organization_id",self.profile["organization_id"]),
                ("cash_register_id",self.profile["register_id"]),("cashier_id",self.profile["cashier_id"])):
            if field in data and data[field] != expected_id:
                bad("checkbox_identity_mismatch")
        if "is_test" in data and data["is_test"] is not self.test_only:
            bad("checkbox_identity_mismatch")
        self._shift(data.get("shift"), shift_id)
        if (data.get("related_receipt_id") != expected.get("related_receipt_id") or data.get("is_created_offline") is not False
                or data.get("total_sum") != snapshot["amount_minor"] or type(data.get("total_sum")) is not int
                or data.get("total_payment") != snapshot["amount_minor"] or data.get("round_sum") != 0
                or data.get("total_rest", data.get("rest")) != 0 or data.get("discounts", [])):
            bad()
        goods = data.get("goods")
        if not isinstance(goods, list) or len(goods) != len(expected["goods"]):
            bad()
        observed_items, observed_taxes = [], {}
        for row, wanted in zip(goods, expected["goods"]):
            row = mapping(row)
            actual = mapping(row.get("good"))
            if (any(actual.get(k) != wanted["good"][k] for k in ("code", "name", "price"))
                    or type(actual.get("price")) is not int or row.get("quantity") != wanted["quantity"]
                    or type(row.get("quantity")) is not int or row.get("is_return") is not wanted["is_return"]):
                bad()
            tax_rows = self._taxes(row.get("taxes"), selected=wanted["good"]["tax"])
            if set(tax_rows) != set(wanted["good"]["tax"]):
                bad("checkbox_tax_profile_invalid")
            discounts = row.get("discounts", [])
            expected_discount = sum(d["value"] for d in wanted.get("discounts", []))
            if not isinstance(discounts, list) or len(discounts) != len(wanted.get("discounts", [])):
                bad()
            for d in discounts:
                mapping(d)
                if (d.get("type") != "DISCOUNT" or d.get("mode") != "VALUE"
                        or type(d.get("value")) is not int or d["value"] != expected_discount
                        or (d.get("tax_codes") or [d.get("tax_code")]) != wanted["good"]["tax"]):
                    bad()
            item_total = wanted["good"]["price"] * wanted["quantity"] // 1000 - expected_discount
            if row.get("sum") != item_total or type(row.get("sum")) is not int:
                bad()
            for code, tax in tax_rows.items():
                expected_tax = self.profile["taxes"][str(code)]
                rate = Decimal(expected_tax["rate"])
                value = int((Decimal(item_total) * rate / (100 + rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                if minor_number(tax.get("value")) != value or tax.get("extra_value", 0) not in (None, 0):
                    bad("checkbox_tax_profile_invalid")
                aggregate = observed_taxes.setdefault(code, {"code": code, **expected_tax, "value": 0, "turnover_minor": 0})
                aggregate["value"] += value
                aggregate["turnover_minor"] += item_total
            observed_items.append({"code": actual["code"], "name": actual["name"], "unit_price_minor": actual["price"],
                "quantity": row["quantity"] // 1000, "total_minor": item_total, "tax_codes": list(tax_rows), "is_return": row["is_return"]})
        aggregate_taxes = self._taxes(data.get("taxes"), selected=list(observed_taxes))
        if set(aggregate_taxes) != set(observed_taxes):
            bad("checkbox_tax_profile_invalid")
        for code, aggregate in aggregate_taxes.items():
            if minor_number(aggregate.get("value")) != observed_taxes[code]["value"] or aggregate.get("extra_value", 0) not in (None, 0):
                bad("checkbox_tax_profile_invalid")
        payments = data.get("payments")
        if not isinstance(payments, list) or len(payments) != 1 or any(mapping(payments[0]).get(k) != v or type(payments[0].get(k)) is not type(v) for k,v in snapshot["payment"].items()):
            bad()
        status = data.get("status")
        if status not in ("CREATED", "PENDING", "DONE", "ERROR"):
            bad()
        result = self._base("receipt", identifier, status)
        result.update(kind=snapshot["kind"], shift_id=shift_id, currency="UAH", amount_minor=data["total_sum"],
            items=observed_items, payment=deepcopy(snapshot["payment"]), related_receipt_id=expected.get("related_receipt_id"),
            taxes=sorted(observed_taxes.values(), key=lambda t:t["code"]),
            fiscal_code=data.get("fiscal_code"), is_sent_dps=data.get("is_sent_dps"), created_at=data.get("created_at"), snapshot_hash=fingerprint(snapshot))
        transaction = data.get("transaction")
        result["rejected"] = bool(status == "ERROR" and isinstance(transaction, dict)
            and transaction.get("status") == "ERROR" and not transaction.get("offline_id")
            and transaction.get("response_status") != "OK"
            and not data.get("fiscal_code") and data.get("is_sent_dps") is False)
        result.update(is_created_offline=False,
            transaction_status=transaction.get("status") if isinstance(transaction, dict) else None,
            transaction_offline=bool(transaction.get("offline_id")) if isinstance(transaction, dict) else None,
            transaction_response_accepted=transaction.get("response_status") == "OK" if isinstance(transaction, dict) else None)
        if status == "DONE":
            if (not self._transaction_done(data.get("transaction")) or not isinstance(data.get("fiscal_code"), str)
                    or not data["fiscal_code"] or data.get("is_sent_dps") is not (not self.test_only)
                    or self.test_only and not data["fiscal_code"].startswith("TEST")):
                bad()
            instant(data.get("fiscal_date"));result["accepted"] = True
        return result

    async def create_receipt(self, provider_uuid, snapshot):
        payload = build_receipt_payload(provider_uuid, snapshot, self.profile);headers = await self._identity()
        shift = await self._request("GET", "/api/v1/cashier/shift", headers=headers, missing=True)
        if not shift or shift.get("id") != snapshot.get("shift_id") or self._shift(shift, snapshot["shift_id"])["status"] != "OPENED":
            bad("checkbox_shift_conflict")
        data = await self._request("POST", "/api/v1/receipts/sell", headers=headers, body=payload, fiscal_write=True)
        return self._after_write(self._receipt, data, provider_uuid, snapshot)

    async def get_receipt(self, provider_uuid, snapshot):
        identifier = uuid_string(provider_uuid);headers = await self._identity()
        data = await self._request("GET", "/api/v1/receipts/" + identifier, headers=headers, missing=True)
        if data:
            mapping(data)
            for field in ("shift_id","organization_id","cash_register_id","cashier_id"):
                if field not in data:
                    bad("checkbox_identity_mismatch")
            if data.get("shift") is None:
                sid = uuid_string(data.get("shift_id"))
                data["shift"] = await self._request("GET", "/api/v1/shifts/" + sid, headers=headers)
        return self._receipt(data, identifier, snapshot) if data else None

    def _report(self, data, shift_id):
        if (not isinstance(data, dict) or uuid_string(data.get("shift_id")) != shift_id
                or type(data.get("is_z_report")) is not bool or data.get("transaction_fail") is not False):
            bad()
        identifier = uuid_string(data.get("id"));instant(data.get("created_at"))
        payments = data.get("payments")
        if not isinstance(payments, list):
            bad()
        for p in payments:
            if (not isinstance(p, dict) or p.get("type") not in ("CASH", "CASHLESS")
                    or not isinstance(p.get("label"), str)):
                bad()
            integer(p.get("code"))
            for field in ("sell_sum", "return_sum"):
                integer(p.get(field))
            for field in ("service_in", "service_out", "cash_withdrawal", "cash_withdrawal_commission"):
                integer(p.get(field, 0))
            if any(p.get(field, 0) for field in ("service_in", "service_out", "cash_withdrawal", "cash_withdrawal_commission")):
                bad("checkbox_external_activity")
        for field in ("cash_withdrawal_receipts_count", "transfers_count", "transfers_sum", "currency_exchange_count", "sales_round_up", "sales_round_down", "returns_round_up", "returns_round_down", "extra_charge_sum"):
            if data.get(field, 0) != 0:
                bad("checkbox_external_activity")
        tax_rows = data.get("taxes")
        indexed = self._taxes(tax_rows, selected=[t.get("code") for t in tax_rows] if isinstance(tax_rows, list) and all(isinstance(t, dict) for t in tax_rows) else None)
        normalized_taxes = []
        for code, tax in indexed.items():
            normalized_taxes.append({"code":code, **self.profile["taxes"][str(code)],
                **{field:minor_number(tax.get(field)) for field in ("sell_sum","return_sum","sales_turnover","returns_turnover")}})
        sales, returns = sum(p["sell_sum"] for p in payments), sum(p["return_sum"] for p in payments)
        result = self._base("report", identifier, "DONE")
        result.update(accepted=True, shift_id=shift_id, is_z_report=data["is_z_report"], created_at=data["created_at"],
            totals={"sales_minor":sales,"returns_minor":returns,"net_minor":sales-returns,
                    "sale_count":integer(data.get("sell_receipts_count")),"return_count":integer(data.get("return_receipts_count"))},
            payments=sorted([{k:p.get(k) for k in ("type","label","code","sell_sum","return_sum")} for p in payments],
                key=lambda p:(p["type"],p["label"],p["code"])),
            taxes=sorted(normalized_taxes,key=lambda t:t["code"]), fiscal_code=data.get("fiscal_code"), is_sent_dps=False if self.test_only or not data["is_z_report"] else None)
        if data["is_z_report"]:
            if not isinstance(data.get("fiscal_code"), str) or not data["fiscal_code"]:
                bad()
            result["is_sent_dps"] = not self.test_only
        return result

    async def create_x_report(self):
        headers = await self._identity()
        shift = await self._request("GET", "/api/v1/cashier/shift", headers=headers, missing=True)
        if not shift:
            bad("checkbox_shift_closed")
        sid = uuid_string(shift.get("id"))
        if self._shift(shift, sid)["status"] != "OPENED":
            bad("checkbox_shift_closed")
        data = await self._request("POST", "/api/v1/reports", headers=headers, fiscal_write=True)
        return self._after_write(self._report, data, sid)

    async def get_report(self, candidate_uuid):
        identifier = uuid_string(candidate_uuid);headers = await self._identity()
        data = await self._request("GET", "/api/v1/reports/" + identifier, headers=headers, missing=True)
        if not data:
            return None
        if data.get("id") != identifier:
            bad()
        sid = uuid_string(data.get("shift_id"))
        shift = await self._request("GET", "/api/v1/shifts/" + sid, headers=headers)
        self._shift(shift, sid)
        return self._report(data, sid)


def get_gateway(profile_snapshot=None):
    profile = profile_snapshot if profile_snapshot is not None else getattr(settings, "shop_checkbox_profile", None)
    transport = transport_factory() if transport_factory is not None else None
    return CheckboxGateway(profile, login=getattr(settings,"shop_checkbox_login",None),
        password=getattr(settings,"shop_checkbox_password",None), license_key=getattr(settings,"shop_checkbox_license_key",None),
        transport=transport, live_enabled=getattr(settings,"shop_checkbox_live_transport_enabled",False))
