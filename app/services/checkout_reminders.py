"""One automatic service reminder after a deliberate email checkpoint for one persisted checkout.

Live SMTP is separately gated and disabled by default. SMTP cannot prove that
an interrupted send was rejected, so unknown outcomes are quarantined, not retried.
No account access, browsing history, promotion campaigns, or account-cart writes.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.checkout_reminder import CheckoutReminder
from app.models.customer import Customer
from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.models.product import Product
from app.models.shop import CustomerCartItem
from app.models.shop_ingress_limits import ShopIngressAdmission
from app.services.catalog_assembly import build_shop_product_response, product_image_urls
from app.services.catalog_visibility import CatalogVisibility
from app.services.shop_promotion import shop_promotion_service
from app.services.shop_ingress_limits import admit_checkout_reminder_email

logger = logging.getLogger(__name__)
CHECKPOINT_VERSION = "email-checkpoint-v1"
ACTIVE = {"pending", "retry"}
RECOVERABLE = {"attempting", "sent", "uncertain", "dry_run"}


def digest(value: str, purpose: str) -> str:
    return hmac.new(settings.secret_key.encode(), f"checkout-reminder:{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def key_digest(key: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
        raise HTTPException(422, detail={"code": "checkout_key_invalid"})
    return digest(key, "management")


def live_ready() -> bool:
    # This is a global operator launch gate, never a per-customer opt-in.
    return bool(settings.checkout_reminder_live_enabled
        and settings.checkout_reminder_launch_approved
        and settings.email_notifications_enabled and settings.smtp_host and settings.smtp_from_email
        and settings.checkout_reminder_shop_url and settings.checkout_reminder_logo_url
        and settings.checkout_reminder_support_email)


def scheduler_allowed() -> bool:
    if settings.checkout_reminder_dry_run:
        return settings.app_env not in {"production", "staging"}
    return live_ready()


def checkout_allowed(customer=None) -> bool:
    return bool(settings.shop_checkout_enabled or (
        settings.shop_checkout_pilot_enabled
        and customer is not None
        and customer.is_active
        and customer.phone_verified_at is not None
        and customer.id in settings.checkout_pilot_customer_ids
    ))


def delivery_enabled() -> bool:
    if not settings.checkout_reminder_enabled or not (
        settings.shop_checkout_enabled or (
            settings.shop_checkout_pilot_enabled and settings.checkout_pilot_customer_ids
        )
    ):
        return False
    if settings.app_env in {"production", "staging"}:
        return not settings.checkout_reminder_dry_run and live_ready()
    return settings.checkout_reminder_dry_run or live_ready()


def enabled(customer=None) -> bool:
    return delivery_enabled() and checkout_allowed(customer)


def capabilities(customer=None):
    return {"enabled": enabled(customer), "dry_run": settings.checkout_reminder_dry_run}


def require_enabled(customer=None) -> None:
    if not enabled(customer):
        raise HTTPException(503, detail={"code": "checkout_reminder_disabled"})


async def owner_checkout_allowed(session, row) -> bool:
    if settings.shop_checkout_enabled:
        return True
    if row.customer_id is None:
        return False
    owner = (await session.execute(select(Customer).where(Customer.id == row.customer_id)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    return checkout_allowed(owner)


def now_utc() -> datetime:
    return datetime.now(UTC)


def public_asset_origin():
    try:
        scoped = settings.checkout_reminder_media_origin
        value = scoped or settings.public_api_base_url or ""
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or re.search(r"[\x00-\x20\x7f\\]", value)
                or (scoped and parsed.path not in {"", "/"})):
            return None
        # Validate the port before constructing an origin; preserve explicit port.
        port = parsed.port
        return f"https://{parsed.hostname.lower()}" + (f":{port}" if port not in (None, 443) else "")
    except ValueError:
        return None


def email_asset_url(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        return ""
    if value.startswith("/"):
        # Only this API's upload namespace may be resolved against its public
        # origin. Protocol-relative, arbitrary paths, query/fragment URLs fail.
        origin = public_asset_origin()
        prefix = settings.upload_url_prefix.rstrip("/") + "/"
        if (not origin or value.startswith("//") or not value.startswith(prefix)
                or urlsplit(value).query or urlsplit(value).fragment):
            return ""
        return origin + value
    return value


async def cart_matches(session, row, *, lock=False):
    if row.customer_id is None:
        return True
    if lock:
        await session.execute(select(func.pg_advisory_xact_lock(
            func.hashtextextended(f"shop-cart:{row.customer_id}", 0))))
    current = (await session.execute(select(CustomerCartItem.product_id, CustomerCartItem.quantity)
        .where(CustomerCartItem.customer_id == row.customer_id))).all()
    return sorted(current) == sorted((item["product_id"], item["quantity"]) for item in row.snapshot)


def clear_contact(row, *, clear_items=False, preserve_email_hash=False):
    row.email = None
    if not preserve_email_hash:
        row.email_hash = None
    if clear_items:
        row.snapshot = []
        row.payload_hash = None
        row.token_hash = None
        row.recovery_management_hash = None


async def managed_row(session, key: str):
    hashed = key_digest(key)
    # Serialize insert/update/cancel/complete even before a row exists. No raw key
    # is sent to PostgreSQL or logged by the ORM.
    lock = int.from_bytes(bytes.fromhex(hashed)[:8], "big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(lock)))
    row = (await session.execute(select(CheckoutReminder)
        .where(or_(CheckoutReminder.management_hash == hashed,
            CheckoutReminder.recovery_management_hash == hashed)).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    return hashed, row


def assert_scope(row, customer):
    owner = customer.id if customer is not None else None
    if row.customer_id is not None and row.customer_id != owner:
        raise HTTPException(404, detail={"code": "checkout_reminder_unavailable"})


async def suppressed(session, row, *, email=None) -> bool:
    address = email if email is not None else row.email
    conditions = []
    if row.customer_id is not None:
        conditions.append(Customer.id == row.customer_id)
    if address:
        conditions.append(func.lower(Customer.email) == address.lower())
    if not conditions:
        return False
    # Email comparison only suppresses; it never associates a guest checkout
    # with an account or exposes whether an address belongs to a customer.
    rows = (await session.execute(select(Customer, ClientCommunicationPreference)
        .outerjoin(ClientCommunicationPreference, ClientCommunicationPreference.customer_id == Customer.id)
        .where(or_(*conditions)))).all()
    return any(not customer.is_active or (preference is not None and (
        preference.do_not_contact or preference.blacklisted_at is not None
        or preference.transactional_consent == ConsentStatus.opted_out
    )) for customer, preference in rows)


async def products_snapshot(session, items, *, customer_id=None, strict=False, locale="uk"):
    ids = [item.product_id if hasattr(item, "product_id") else item["product_id"] for item in items]
    products = (await session.execute(select(Product).where(Product.id.in_(ids)).options(
        selectinload(Product.images), selectinload(Product.image_variants),
        selectinload(Product.category), selectinload(Product.brand)))).scalars().all()
    by_id = {product.id: product for product in products}
    visibility = await CatalogVisibility.load(session)
    prices = await shop_promotion_service.price_products(session, products,
        category_parents=visibility.category_parents(), customer_id=customer_id)
    snapshot, recovered, changes = [], [], []
    for item in items:
        product_id = item.product_id if hasattr(item, "product_id") else item["product_id"]
        quantity = item.quantity if hasattr(item, "quantity") else item["quantity"]
        product = by_id.get(product_id)
        if product is None or not visibility.product_state(product).is_effectively_visible:
            if strict:
                raise HTTPException(409, detail={"code": "checkout_product_unavailable"})
            changes.append(f"unavailable:{product_id}")
            continue
        available = product.available_quantity >= quantity or product.allow_backorder
        if not available:
            changes.append(f"out_of_stock:{product_id}")
        state = visibility.product_state(product)
        response = build_shop_product_response(product, categories=visibility.categories_by_id,
            pricing=prices[product_id], visibility_state=state, is_available_for_purchase=available)
        unit = "ml" if locale == "en" else "мл"
        variant = f"{product.volume_ml} {unit}" if product.volume_ml else (product.package_size or "")
        images = product_image_urls(product)
        canonical = {"product_id": product_id, "quantity": quantity, "name": product.name,
            "variant": variant, "image_url": email_asset_url(images[0]) if images else "", "price": str(prices[product_id].price)}
        if isinstance(item, dict):
            if item.get("price") != canonical["price"]:
                changes.append(f"price_changed:{product_id}")
            if item.get("variant") != variant:
                changes.append(f"variant_changed:{product_id}")
        snapshot.append(canonical)
        recovered.append({"product_id": product_id, "quantity": quantity, "variant": variant, "product": response})
    return snapshot, recovered, changes


def validate_revision(revision):
    if revision is not None and (type(revision) is not int or not 1 <= revision <= 2147483647):
        raise HTTPException(422, detail={"code": "checkout_revision_invalid"})


def stale_revision(row, revision):
    if revision is None:
        return row.checkpoint_revision > 0
    return revision <= row.checkpoint_revision


async def checkpoint(session, key, payload, customer=None, *, revision=None):
    require_enabled(customer)
    validate_revision(revision)
    hashed, row = await managed_row(session, key)
    now = now_utc()
    if row is not None:
        assert_scope(row, customer)
        if stale_revision(row, revision):
            await session.commit()
            return {"accepted": True}
        rearmable = (row.state == "cancelled" and row.attempts == 0 and row.order_id is None
            and row.checkpoint_revision > 0 and revision is not None)
        if row.expires_at <= now or (row.state not in ACTIVE and not rearmable):
            raise HTTPException(409, detail={"code": "checkout_reminder_closed"})
    normalized_email = str(payload.email).lower()
    email_hash = digest(normalized_email, "email")
    canonical_payload = {"email": normalized_email, "locale": payload.locale,
        "items": sorted([item.model_dump() for item in payload.items], key=lambda item: item["product_id"])}
    payload_hash = digest(json.dumps(canonical_payload, sort_keys=True), "checkpoint")
    ownership_changed = row is not None and row.customer_id is None and customer is not None
    if row is not None and row.payload_hash == payload_hash and not ownership_changed:
        # A genuinely newer deliberate event advances its fence without resetting
        # the reminder timer. Same-revision and legacy duplicate events do no write.
        if revision is not None:
            row.checkpoint_revision = revision
            row.checkpoint_at = now
            row.checkpoint_version = CHECKPOINT_VERSION
        await session.commit()
        return {"accepted": True}
    if row is None or row.email_hash != email_hash:
        await admit_checkout_reminder_email(normalized_email)
    snapshot, _, _ = await products_snapshot(session, payload.items,
        customer_id=customer.id if customer else None, strict=True, locale=payload.locale)
    if row is None:
        row = CheckoutReminder(management_hash=hashed, customer_id=customer.id if customer else None,
            due_at=now, expires_at=now + timedelta(hours=settings.checkout_reminder_ttl_hours),
            snapshot=[], attempts=0, state="pending", locale=payload.locale,
            checkpoint_revision=revision or 0)
        session.add(row)
    if row.customer_id is None and customer is not None:
        # A real authenticated checkpoint may claim its prior guest capability;
        # an email match alone never establishes account ownership.
        row.customer_id = customer.id
    row.state = "pending"
    row.email = normalized_email
    row.email_hash = email_hash
    row.locale = payload.locale
    row.checkpoint_at = now
    row.checkpoint_version = CHECKPOINT_VERSION
    row.checkpoint_revision = revision or 0
    row.snapshot = snapshot
    row.payload_hash = payload_hash
    row.due_at = min(now + timedelta(seconds=settings.checkout_reminder_delay_seconds), row.expires_at)
    row.token_hash = None
    row.recovery_management_hash = None
    if await suppressed(session, row):
        row.state = "suppressed"
        clear_contact(row, clear_items=True)
    await session.commit()
    return {"accepted": True}


async def cancel(session, key, customer=None, *, revision=None):
    # Cancellation remains available after an operator disables the feature.
    validate_revision(revision)
    hashed, row = await managed_row(session, key)
    now = now_utc()
    if row is None:
        row = CheckoutReminder(management_hash=hashed, customer_id=customer.id if customer else None,
            state="cancelled", locale="uk", snapshot=[], attempts=0, due_at=now,
            checkpoint_revision=revision or 0,
            expires_at=now + timedelta(hours=settings.checkout_reminder_ttl_hours))
        session.add(row)
    else:
        assert_scope(row, customer)
        if ((revision is not None and revision <= row.checkpoint_revision)
                or (row.state == "cancelled" and row.checkpoint_revision == 0)):
            await session.commit()
            return {"cancelled": True}
        # A legacy unversioned cancel remains terminal; a newer deliberate event
        # may only rearm a versioned, never-attempted cart invalidation.
        row.checkpoint_revision = revision or 0
        if row.state == "attempting":
            # Reservation is already durable, but dispatch may not yet have won
            # its final fence. Cancel it and preserve the no-rearm attempt fact.
            row.state = "cancelled"
            row.attempts = max(row.attempts, 1)
        rearmable = (revision is not None and row.attempts == 0 and row.order_id is None
            and row.state in {"pending", "retry", "cancelled"} and row.expires_at > now)
        if row.state in {"pending", "retry", "cancelled"}:
            row.state = "cancelled"
        # Retain only this HMAC for reusing the existing address admission when
        # the same concrete checkout is deliberately checkpointed again.
        clear_contact(row, clear_items=True, preserve_email_hash=rearmable)
    await session.commit()
    return {"cancelled": True}


async def bind_order(session, key, payload, customer=None, *, replay_order_id=None):
    """Lock before product locks; returned row is completed by the same order commit."""
    hashed, row = await managed_row(session, key)
    now = now_utc()
    if row is None:
        row = CheckoutReminder(management_hash=hashed, customer_id=customer.id if customer else None,
            state="completed", locale="uk", snapshot=[], attempts=0, due_at=now,
            expires_at=now + timedelta(hours=settings.checkout_reminder_ttl_hours))
        session.add(row)
    else:
        assert_scope(row, customer)
        # Completion closes the exact browser checkout regardless of product
        # edits since the explicit checkpoint; never reject a legitimate order
        # merely because its previous saved snapshot became stale.
        if row.order_id is not None and row.order_id != replay_order_id:
            # Historical idempotent replay is handled by the caller. Reusing a
            # completed checkout key for a new order is an explicit conflict.
            raise HTTPException(409, detail={"code": "checkout_already_completed"})
    return row


def complete_order(row, order_id):
    row.order_id = order_id
    row.state = "completed"
    clear_contact(row, clear_items=True)


async def recover(session, token, customer=None):
    require_enabled(customer)
    row = (await session.execute(select(CheckoutReminder).where(
        CheckoutReminder.token_hash == digest(token, "recovery")))).scalar_one_or_none()
    if row is None or row.expires_at <= now_utc() or row.state not in RECOVERABLE:
        raise HTTPException(410, detail={"code": "checkout_recovery_unavailable"})
    assert_scope(row, customer)
    if not await owner_checkout_allowed(session, row):
        raise HTTPException(410, detail={"code": "checkout_recovery_unavailable"})
    if await suppressed(session, row):
        raise HTTPException(410, detail={"code": "checkout_recovery_unavailable"})
    _, items, changes = await products_snapshot(session, row.snapshot, customer_id=row.customer_id, locale=row.locale)
    return {"items": items, "locale": row.locale, "changes": changes,
        "checkout_key": digest(token, "completion")}


from app.services.checkout_reminder_smtp import (
    CheckoutReminderSMTPProvider, DefinitiveReminderRejection, PermanentReminderRejection,
    UnsupportedReminderRecipient,
)


class FakeCheckoutReminderProvider:
    is_fake = True

    def __init__(self):
        self.messages = {}

    async def send(self, *, delivery_key, email, message):
        self.messages.setdefault(delivery_key, (email, message))


async def purge(session, now=None):
    now = now or now_utc()
    expired = (await session.execute(select(CheckoutReminder).where(
        CheckoutReminder.expires_at <= now, CheckoutReminder.state != "expired")
        .with_for_update(skip_locked=True).limit(100))).scalars().all()
    for row in expired:
        row.state = "expired"
        clear_contact(row, clear_items=True)
        row.checkpoint_at = None
        row.checkpoint_version = None
    await session.execute(delete(CheckoutReminder).where(
        CheckoutReminder.expires_at <= now - timedelta(days=settings.checkout_reminder_retention_days)))
    await session.execute(delete(ShopIngressAdmission).where(
        ShopIngressAdmission.action == "reminder_email",
        ShopIngressAdmission.admitted_at <= now - timedelta(days=1)))
    await session.execute(delete(ShopIngressAdmission).where(
        ShopIngressAdmission.action == "reminder",
        ShopIngressAdmission.admitted_at <= now - timedelta(seconds=settings.source_limit_window_seconds)))
    await session.commit()


async def process_due(*, session_factory=AsyncSessionLocal, provider=None, dry_run=None, limit=20):
    # Retention is independent of the delivery feature switch.
    async with session_factory() as session:
        await purge(session)
    if not delivery_enabled():
        return 0
    dry_run = settings.checkout_reminder_dry_run if dry_run is None else dry_run
    if not dry_run and (provider is None or getattr(provider, "is_fake", False) is not True):
        if not live_ready():
            raise RuntimeError("checkout_reminder_live_not_approved")
        if provider is None:
            provider = CheckoutReminderSMTPProvider()
        elif not isinstance(provider, CheckoutReminderSMTPProvider):
            raise RuntimeError("checkout_reminder_live_provider_unavailable")
    from app.services.checkout_reminder_email import build_checkout_reminder_message
    processed = 0
    for _ in range(min(limit, 100)):
        async with session_factory() as session:
            row = (await session.execute(select(CheckoutReminder).where(
                CheckoutReminder.state.in_(ACTIVE), CheckoutReminder.due_at <= now_utc(),
                CheckoutReminder.expires_at > now_utc()).order_by(CheckoutReminder.due_at, CheckoutReminder.id)
                .with_for_update(skip_locked=True).limit(1))).scalar_one_or_none()
            if row is None:
                break
            if not row.email or not await owner_checkout_allowed(session, row) or await suppressed(session, row):
                row.state = "suppressed"
                clear_contact(row, clear_items=True)
                await session.commit()
                continue
            if not await cart_matches(session, row):
                row.state = "suppressed"
                clear_contact(row, clear_items=True)
                await session.commit()
                continue
            snapshot, _, _ = await products_snapshot(session, row.snapshot, customer_id=row.customer_id, locale=row.locale)
            if not snapshot or len(snapshot) != len(row.snapshot):
                row.state = "cancelled"
                clear_contact(row, clear_items=True)
                await session.commit()
                continue
            row.snapshot = snapshot
            token = secrets.token_urlsafe(32)
            shop_url = settings.checkout_reminder_shop_url.rstrip("/")
            origin = urlsplit(shop_url)
            if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment:
                raise ValueError("checkout_reminder_url_config")
            if not settings.checkout_reminder_logo_url or not settings.checkout_reminder_support_email:
                raise ValueError("checkout_reminder_brand_config")
            message = build_checkout_reminder_message(locale=row.locale, items=snapshot,
                recovery_url=shop_url + "/checkout/recover#token=" + token,
                logo_url=settings.checkout_reminder_logo_url,
                support_email=settings.checkout_reminder_support_email,
                allowed_origins=[f"{origin.scheme}://{origin.netloc}",
                    *([public_asset_origin()] if public_asset_origin() else []),
                    *settings.checkout_reminder_asset_origins])
            if not dry_run and isinstance(provider, CheckoutReminderSMTPProvider):
                # A known configuration failure has not contacted SMTP and must
                # not consume the durable attempt or become delivery uncertainty.
                try:
                    provider._validate(row.email, f"checkout-reminder:{row.id}", message)
                except UnsupportedReminderRecipient:
                    row.state = "cancelled"
                    row.last_error_code = "recipient_unsupported"
                    clear_contact(row, clear_items=True)
                    await session.commit()
                    continue
            row.token_hash = digest(token, "recovery")
            row.recovery_management_hash = key_digest(digest(token, "completion"))
            # Freeze one logical attempt before provider interaction. Crash after
            # this commit is uncertain and never eligible for automatic retry.
            row.state = "attempting"
            row.attempts += 1
            row.last_error_code = None
            row_id, email, expires_at = row.id, row.email, row.expires_at
            await session.commit()
            # Completion/cancellation may have won between reservation and this
            # fence. Hold the row lock through fake transport to linearize both.
            row = (await session.execute(select(CheckoutReminder).where(CheckoutReminder.id == row_id)
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if row is None or row.state != "attempting":
                await session.commit()
                continue
            if (expires_at <= now_utc() or not delivery_enabled()
                    or not await owner_checkout_allowed(session, row) or await suppressed(session, row)
                    or not await cart_matches(session, row, lock=True)):
                row.state = "suppressed"
                clear_contact(row, clear_items=True)
                await session.commit()
                continue
            try:
                if not dry_run:
                    await provider.send(delivery_key=f"checkout-reminder:{row_id}", email=email, message=message)
            except PermanentReminderRejection:
                row.state = "cancelled"
                row.last_error_code = "provider_permanent_rejection"
                clear_contact(row, clear_items=True)
            except DefinitiveReminderRejection:
                row.state = "retry" if row.attempts < 3 else "cancelled"
                row.last_error_code = "provider_rejected"
                row.due_at = now_utc() + timedelta(minutes=5 * 2 ** row.attempts)
                row.token_hash = None
                if row.state == "cancelled":
                    clear_contact(row, clear_items=True)
            except Exception:
                row.state = "uncertain"
                row.last_error_code = "delivery_uncertain"
                # Keep the address only through the original recovery TTL so
                # subsequent guest email suppression can revoke this capability.
            else:
                row.state = "dry_run" if dry_run else "sent"
                row.sent_at = now_utc()
                # Required for guest suppression rechecks until recovery expiry.
                # Completion/cancellation/expiry erase it immediately.
            await session.commit()
            logger.info("checkout_reminder state=%s", row.state)
            processed += 1
    return processed


async def run_checkout_reminder_scheduler():
    # Default configuration starts no reminder worker. Live admission requires
    # separate global launch approval, transport, and runtime settings.
    if not scheduler_allowed():
        return
    while True:
        await asyncio.sleep(settings.checkout_reminder_poll_seconds)
        try:
            await process_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("checkout_reminder_iteration_failed")
