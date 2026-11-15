"""Read Checkbox evidence for receipts already associated with the Mono invoice."""
import asyncio
import json
from datetime import UTC, datetime

import httpx

from app.core.config import settings
from app.schemas.master_mono import MasterMonoDpsReceipt, MasterMonoDpsState
from app.services import master_mono_refunds
from app.services.checkbox_provider import HOST, CheckboxProviderError, instant, mapping, uuid_string


def provider_text(value, limit):
    if isinstance(value, str) and 0 < len(value) <= limit:
        return "".join(char if char.isprintable() else " " for char in value)
    return None


def receipt_evidence(data, identifier, register, amount, sale_id):
    data = mapping(data)
    cash_register = mapping(mapping(data.get("shift")).get("cash_register"))
    if (data.get("id") != identifier or cash_register.get("id") != register
            or data.get("type") != ("RETURN" if sale_id else "SELL")
            or data.get("related_receipt_id") != sale_id
            or type(data.get("total_sum")) is not int or data["total_sum"] != amount):
        raise CheckboxProviderError("checkbox_identity_mismatch")
    if data.get("is_test") is True or cash_register.get("is_test") is True:
        raise CheckboxProviderError("checkbox_identity_mismatch")
    transaction = mapping(data.get("transaction") if data.get("transaction") is not None else {})
    sent = data.get("is_sent_dps")
    if sent is not None and type(sent) is not bool:
        raise CheckboxProviderError("checkbox_proof_mismatch")
    sent_at = instant(data["sent_dps_at"]) if data.get("sent_dps_at") else None
    fiscal_at = instant(data["fiscal_date"]) if data.get("fiscal_date") else None
    response_status = provider_text(transaction.get("response_status"), 100)
    response_id = provider_text(transaction.get("response_id"), 128)
    error = provider_text(transaction.get("response_error_message"), 1000)
    code = provider_text(data.get("fiscal_code"), 128)
    checkbox_status = provider_text(data.get("status"), 50)
    status = "unconfirmed"
    if (sent is True and checkbox_status == "DONE" and transaction.get("status") == "DONE"
            and response_status == "OK" and response_id and code and sent_at and fiscal_at
            and not transaction.get("offline_id")):
        status = "accepted"
    elif (checkbox_status == "ERROR" and transaction.get("status") == "ERROR"
            and response_status and response_status != "OK" and not code and not transaction.get("offline_id")):
        status = "rejected"
    return MasterMonoDpsReceipt(
        receipt_id=identifier, status=status, checked_at=datetime.now(UTC),
        checkbox_status=checkbox_status, fiscal_code=code, is_sent_dps=sent, sent_dps_at=sent_at,
        response_status=response_status, response_id=response_id, response_error_message=error,
    )


def client(token):
    return httpx.AsyncClient(
        base_url=HOST, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "X-Client-Name": "Soulcuts-POS", "X-Client-Version": "1.0",
        }, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(5, connect=3),
    )


async def lookup(checkbox, identifier, register, amount, sale_id=None):
    if identifier is None:
        return MasterMonoDpsReceipt(status="awaiting_receipt")
    status = "unavailable"
    try:
        identifier = uuid_string(identifier)
        async with asyncio.timeout(8):
            async with checkbox.stream("GET", f"/api/v1/receipts/{identifier}") as response:
                if response.status_code == 404:
                    return MasterMonoDpsReceipt(receipt_id=identifier, status="not_found", checked_at=datetime.now(UTC))
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise CheckboxProviderError("checkbox_proof_mismatch")
        return receipt_evidence(json.loads(body), identifier, register, amount, sale_id)
    except (CheckboxProviderError, ValueError, RecursionError):
        status = "mismatch"
    except (httpx.HTTPError, TimeoutError):
        pass
    return MasterMonoDpsReceipt(receipt_id=identifier, status=status, checked_at=datetime.now(UTC))


async def read(session, actor, order_id):
    _, order, refund = await master_mono_refunds.context(session, actor, order_id)
    original = master_mono_refunds.original(order)
    sale_id = original["sale_receipt_id"] if original else None
    return_id = refund.receipt_id if refund and refund.bank_proof and refund.snapshot == original else None
    await session.rollback()
    token = settings.master_mono_checkbox_read_token
    try:
        register = uuid_string(settings.master_mono_checkbox_register_id)
    except CheckboxProviderError:
        register = None
    if not sale_id or not token or not token.get_secret_value() or not register:
        return MasterMonoDpsState(
            sale=MasterMonoDpsReceipt(receipt_id=sale_id, status="unconfigured" if sale_id else "awaiting_receipt"),
            refund=MasterMonoDpsReceipt(receipt_id=return_id, status="unconfigured" if return_id else "awaiting_receipt"),
        )
    amount = original["amount_minor"]
    async with client(token.get_secret_value()) as checkbox:
        sale, returned = await asyncio.gather(
            lookup(checkbox, sale_id, register, amount),
            lookup(checkbox, return_id, register, amount, sale_id),
        )
    return MasterMonoDpsState(sale=sale, refund=returned)
