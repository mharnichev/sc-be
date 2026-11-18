"""A signed notification schedules an authenticated GET against the pinned invoice."""
import base64
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from sqlalchemy import select, text

from app.core.config import settings
from app.models.master_mono import MasterMonoBinding
from app.models.shop_payments import ShopPaymentIntent
from app.services import master_mono, shop_payments
from app.services.checkbox_provider import CheckboxProviderError, instant

PATH = "/api/v1/public/master-mono/webhook"
SHOP_PATH = "/api/v1/public/shop-mono/webhook"


def public_key(encoded):
    try:
        key = serialization.load_pem_public_key(base64.b64decode(encoded or "", validate=True))
    except (ValueError, UnsupportedAlgorithm):
        return None
    return key if isinstance(key, ec.EllipticCurvePublicKey) else None


def callback_url(url, encoded_key, *, path=PATH):
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                and not parts.query and not parts.fragment and parts.path == path
                and not any(char.isspace() or ord(char) < 32 for char in url)
                and public_key(encoded_key)):
            return url
    except ValueError:
        pass
    return None


async def receive(session, request, *, shop=False):
    key = public_key(settings.shop_mono_webhook_public_key if shop else settings.master_mono_webhook_public_key)
    enabled = settings.shop_online_payment_enabled and settings.shop_online_payment_link_confirmed if shop else settings.master_mono_enabled
    if not enabled or not key:
        raise HTTPException(503, "Mono webhook unavailable")
    signature = request.headers.get("X-Sign", "")
    if not signature or len(signature) > 512:
        raise HTTPException(401, "Invalid Mono signature")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 64_000:
            raise HTTPException(413, "Mono notification too large")
    try:
        key.verify(base64.b64decode(signature, validate=True), bytes(body), ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature):
        raise HTTPException(401, "Invalid Mono signature") from None
    try:
        event = json.loads(body)
        invoice_id = event.get("invoiceId") if isinstance(event, dict) else None
        if not isinstance(invoice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", invoice_id):
            raise ValueError
        modified = instant(event.get("modifiedDate"))
        if modified > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError
    except (ValueError, CheckboxProviderError, RecursionError):
        raise HTTPException(400, "Invalid Mono notification") from None

    if shop:
        provider = shop_payments.gateway()
        bindings = (await session.scalars(select(ShopPaymentIntent).where(
            ShopPaymentIntent.invoice_id == invoice_id, ShopPaymentIntent.mode == "prepayment",
            ShopPaymentIntent.profile_id == (provider.profile or {}).get("id"),
        ))).all()
        if not bindings:
            return
        if len(bindings) != 1:
            raise HTTPException(400, "Mono invoice identity mismatch")
        binding = bindings[0]
        await shop_payments.guard(session, provider)
        shop_payments.pinned(binding, provider)
    else:
        binding = await session.scalar(select(MasterMonoBinding).where(MasterMonoBinding.invoice_id == invoice_id))
        if binding is None:
            return
        master_mono.guard(session, binding)
    if (event.get("reference") != binding.reference or type(event.get("amount")) is not int
            or event["amount"] != binding.amount_minor or event.get("ccy") != 980
            or modified < binding.created_at - timedelta(seconds=30)):
        raise HTTPException(400, "Mono invoice identity mismatch")
    table = "shop_mono_webhook_signals" if shop else "master_mono_webhook_signals"
    await session.execute(text(f"""
        INSERT INTO {table}(order_id, modified_at, received_at)
        VALUES(:order_id, :modified, :received)
        ON CONFLICT (order_id) DO UPDATE SET modified_at = EXCLUDED.modified_at, received_at = EXCLUDED.received_at
        WHERE {table}.modified_at < EXCLUDED.modified_at
    """), {"order_id": binding.order_id, "modified": modified, "received": datetime.now(UTC)})
    await session.commit()
