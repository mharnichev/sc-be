"""Recipient links confer an offer, never account authentication."""
from datetime import UTC, datetime
import hashlib
import secrets
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select
from app.core.config import settings
from app.models.campaign_offer import CampaignOffer, CampaignOfferOpen
from app.models.booking import Booking, BookingStatus, Master, BarberService
from app.models.customer import Customer
from app.models.promotion import PromotionDiscountType, Promotion
from app.services.segments import subtract_age


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CampaignOfferService:
    def render_variables(self, snapshot, token):
        from zoneinfo import ZoneInfo
        name = snapshot.get("master_name_for_message")
        if not name:
            raise ValueError("master_name_for_message is required")
        return {"master_name": name,
                "offer_expires_short": timestamp(snapshot["offer_expires_at"]).astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y"),
                "offer_link": f"{settings.public_site_url.rstrip('/')}/booking?offer_token={token}"}

    async def issue_offer(self, session, recipient, campaign_snapshot):
        existing = (await session.execute(select(CampaignOffer).where(CampaignOffer.recipient_id == recipient.id))).scalar_one_or_none()
        if existing:
            raise ValueError("Offer already issued; reuse recipient's persisted personalized message")
        token = secrets.token_urlsafe(32)
        offer = CampaignOffer(campaign_id=recipient.campaign_id, run_id=recipient.run_id,
            recipient_id=recipient.id, customer_id=recipient.customer_id,
            master_id=campaign_snapshot["offer_master_id"], promotion_id=campaign_snapshot["offer_promotion_id"],
            starts_at=timestamp(campaign_snapshot["offer_starts_at"]), expires_at=timestamp(campaign_snapshot["offer_expires_at"]),
            allowed_service_ids=list(campaign_snapshot["offer_service_ids"]), discount_percent=30,
            promotion_name_uk=campaign_snapshot.get("promotion_name_uk", "Новий майстер"),
            promotion_name_en=campaign_snapshot.get("promotion_name_en", "New Master"), token_hash=token_hash(token),
            conditions_snapshot={**campaign_snapshot, "issued_at": datetime.now(UTC).isoformat(), "eligibility": "latest_completed_or_imported_visit_3_to_12_calendar_months_no_future_pending_confirmed"})
        session.add(offer)
        await session.flush()
        return offer, token

    async def resolve(self, session, token, *, lock=False):
        stmt = select(CampaignOffer).where(CampaignOffer.token_hash == token_hash(token))
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        offer = (await session.execute(stmt)).scalar_one_or_none()
        if offer is None:
            raise HTTPException(404, detail={"code": "offer_not_found"})
        now = datetime.now(UTC)
        if not timestamp(offer.starts_at) <= now < timestamp(offer.expires_at):
            raise HTTPException(410, detail={"code": "offer_expired_or_not_started"})
        return offer

    async def current_restriction(self, session, offer, *, exclude_booking_id=None, at=None):
        from app.services.promotion import PromotionService
        customer = await session.get(Customer, offer.customer_id)
        if customer is None:
            return "customer_unavailable"
        now = at or datetime.now(UTC)
        latest = await PromotionService().customer_last_visit_at(session, customer)
        if latest is None or not subtract_age(now, 12, "months") <= timestamp(latest) <= subtract_age(now, 3, "months"):
            return "audience_no_longer_eligible"
        stmt = select(Booking.id).where(Booking.customer_id == customer.id,
            Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]), Booking.start_at >= now)
        if exclude_booking_id:
            stmt = stmt.where(Booking.id != exclude_booking_id)
        if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            return "future_booking"
        return None

    async def catalog_restriction(self, session, offer):
        master = await session.get(Master, offer.master_id)
        if master is None or not master.is_active:
            return "offer_master_unavailable"
        promotion = await session.get(Promotion, offer.promotion_id)
        if promotion is None or not promotion.is_active:
            return "offer_promotion_unavailable"
        active = (await session.execute(select(BarberService.id).where(
            BarberService.id.in_(offer.allowed_service_ids), BarberService.master_id == offer.master_id,
            BarberService.is_active.is_(True)))).scalars().all()
        if set(active) != set(offer.allowed_service_ids) or not active:
            return "offer_service_unavailable"
        return None

    async def send_eligibility(self, session, recipient, *, now=None):
        offer = (await session.execute(select(CampaignOffer).where(CampaignOffer.recipient_id == recipient.id))).scalar_one_or_none()
        if offer is None:
            return "offer_missing"
        at = now or datetime.now(UTC)
        if not timestamp(offer.starts_at) <= at < timestamp(offer.expires_at):
            return "offer_expired_or_not_started"
        if offer.consumed_booking_id:
            return "offer_consumed"
        return await self.catalog_restriction(session, offer) or await self.current_restriction(session, offer, at=at)

    async def apply(self, session, *, booking, customer, services, token=None, reserve=True, service_prices=None, preserve=False, validate_only=False):
        from app.services.promotion import PromotionService
        pricing = PromotionService()
        if customer is None:
            raise HTTPException(409, detail={"code": "offer_customer_required"})
        if reserve:
            await pricing.lock_customer(session, customer)
        if token:
            offer = await self.resolve(session, token, lock=reserve)
        else:
            stmt = select(CampaignOffer).where(CampaignOffer.id == booking.campaign_offer_id)
            if reserve:
                stmt = stmt.with_for_update().execution_options(populate_existing=True)
            offer = (await session.execute(stmt)).scalar_one()
        if customer.id != offer.customer_id or booking.master_id != offer.master_id:
            raise HTTPException(403, detail={"code": "offer_identity_or_master_mismatch"})
        if not services or any(item.id not in offer.allowed_service_ids or getattr(item, "is_active", True) is False for item in services):
            raise HTTPException(409, detail={"code": "offer_service_not_allowed"})
        if not timestamp(offer.starts_at) <= timestamp(booking.start_at) < timestamp(offer.expires_at):
            raise HTTPException(410, detail={"code": "offer_visit_outside_validity"})
        if not preserve:
            master = await session.get(Master, offer.master_id)
            if master is None or not master.is_active:
                raise HTTPException(409, detail={"code": "offer_master_unavailable"})
            promotion = await session.get(Promotion, offer.promotion_id)
            if promotion is None or not promotion.is_active:
                raise HTTPException(409, detail={"code": "offer_promotion_unavailable"})
            reason = await self.current_restriction(session, offer, exclude_booking_id=booking.id)
            if reason:
                raise HTTPException(409, detail={"code": reason})
        if offer.consumed_booking_id and offer.consumed_booking_id != booking.id:
            raise HTTPException(409, detail={"code": "offer_consumed"})
        reservation = select(Booking.id).where(Booking.campaign_offer_reservation_id == offer.id)
        if booking.id:
            reservation = reservation.where(Booking.id != booking.id)
        if (await session.execute(reservation.limit(1))).scalar_one_or_none() is not None:
            raise HTTPException(409, detail={"code": "offer_reserved"})
        if validate_only:
            return
        booking.campaign_offer_id = offer.id
        booking.campaign_id, booking.campaign_run_id, booking.campaign_recipient_id = offer.campaign_id, offer.run_id, offer.recipient_id
        if reserve and booking.status not in (BookingStatus.cancelled, BookingStatus.no_show):
            booking.campaign_offer_reservation_id = offer.id
        booking.first_visit_customer_id = None
        booking.subtotal_amount = pricing.subtotal_amount(services, service_prices)
        manual = int(booking.manual_discount_amount or 0) if preserve else 0
        booking.manual_discount_amount = manual
        booking.promotion_id = offer.promotion_id
        booking.promotion_code_snapshot = None
        booking.promotion_name_uk_snapshot, booking.promotion_name_en_snapshot = offer.promotion_name_uk, offer.promotion_name_en
        booking.promotion_discount_percent_snapshot = offer.discount_percent
        booking.promotion_application_mode_snapshot = "code"
        booking.promotion_eligibility_type_snapshot = "inactive_customers"
        booking.promotion_discount_amount = pricing.discount_amount(booking.subtotal_amount, SimpleNamespace(discount_type=PromotionDiscountType.percent, discount_percent=offer.discount_percent))
        booking.total_amount = pricing.total_amount(booking.subtotal_amount, booking.promotion_discount_amount + manual)

    async def sync_status(self, session, booking, new_status):
        from app.services.promotion import PromotionService
        customer = await session.get(Customer, booking.customer_id)
        if customer is None:
            raise HTTPException(409, detail={"code": "offer_customer_required"})
        await PromotionService().lock_customer(session, customer)
        offer = (await session.execute(select(CampaignOffer).where(CampaignOffer.id == booking.campaign_offer_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        if new_status in (BookingStatus.cancelled, BookingStatus.no_show):
            booking.campaign_offer_reservation_id = None
            return
        if offer.consumed_booking_id and offer.consumed_booking_id != booking.id:
            raise HTTPException(409, detail={"code": "offer_consumed"})
        reserved = (await session.execute(select(Booking.id).where(Booking.campaign_offer_reservation_id == offer.id, Booking.id != booking.id))).scalar_one_or_none()
        if reserved:
            raise HTTPException(409, detail={"code": "offer_reserved"})
        if not booking.campaign_offer_reservation_id and offer.consumed_booking_id != booking.id:
            if datetime.now(UTC) >= timestamp(offer.expires_at):
                raise HTTPException(410, detail={"code": "offer_expired"})
            reason = await self.current_restriction(session, offer, exclude_booking_id=booking.id)
            if reason:
                raise HTTPException(409, detail={"code": reason})
        booking.campaign_offer_reservation_id = offer.id
        if new_status == BookingStatus.completed:
            if offer.consumed_booking_id is None:
                offer.completed_at = datetime.now(UTC)
                offer.redeemed_discount_amount = int(booking.promotion_discount_amount or 0)
                offer.redeemed_revenue_amount = int(booking.total_amount or 0)
            offer.consumed_booking_id = booking.id

    async def context(self, session, token, *, event_id=None):
        offer = await self.resolve(session, token, lock=True)
        if event_id is None:
            offer.raw_link_requests += 1
        else:
            digest = token_hash(event_id)
            existing = (await session.execute(select(CampaignOfferOpen.id).where(CampaignOfferOpen.offer_id == offer.id, CampaignOfferOpen.event_hash == digest))).scalar_one_or_none()
            if existing is None:
                session.add(CampaignOfferOpen(offer_id=offer.id, event_hash=digest))
                offer.confirmed_open_count += 1
                offer.first_confirmed_open_at = offer.first_confirmed_open_at or datetime.now(UTC)
        reserved = (await session.execute(select(Booking.id).where(Booking.campaign_offer_reservation_id == offer.id).limit(1))).scalar_one_or_none()
        reason = None
        if not offer.consumed_booking_id and not reserved:
            reason = await self.catalog_restriction(session, offer) or await self.current_restriction(session, offer)
        entitlement = "consumed" if offer.consumed_booking_id else "reserved" if reserved else "unavailable" if reason else "available"
        return {"master_id": offer.master_id, "service_ids": offer.allowed_service_ids,
                "discount_percent": offer.discount_percent, "name_uk": offer.promotion_name_uk,
                "name_en": offer.promotion_name_en, "starts_at": offer.starts_at, "expires_at": offer.expires_at,
                "entitlement": entitlement}


campaign_offer_service = CampaignOfferService()
