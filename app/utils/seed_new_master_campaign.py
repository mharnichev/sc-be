"""Explicit, idempotent draft initialization; never activates or sends a campaign."""
from __future__ import annotations

import asyncio
from sqlalchemy import select, text
from app.core.database import AsyncSessionLocal
from app.models.booking import BarberService, Master
from app.models.messaging import Campaign, CampaignStatus, CampaignType, MessageChannel, MessagePurpose, MessageTemplate
from app.models.promotion import Promotion, PromotionEligibilityType
from app.models.segment import CustomerSegment

TEMPLATE_NAME = "New Master — SMS uk"
CAMPAIGN_NAME = "New Master — Andrii"
SEGMENT_NAME = "Inactive 3–12 calendar months, no future bookings"
DEFAULT_BODY = "✂ Давно не бачились! У Soul Cuts −30% на візит до {{master_name}} за акцією «Новий майстер». До {{offer_expires_short}}. Запис: {{offer_link}}"
RULES = {"combine": "all", "conditions": [{"type": "last_visit_age", "min": 3, "max": 12,
          "unit": "calendar_months", "min_inclusive": True, "max_inclusive": True}],
         "exclusions": [{"type": "upcoming_booking", "present": True}]}


async def initialize(session) -> dict:
    # Serialize concurrent initializers without changing independently edited data.
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(733061430)"))
    master = await session.get(Master, 4)
    if master is None or master.full_name != "Андрій Віканов":
        raise ValueError("Verified seed identity master_id=4 / Андрій Віканов is absent; no seed data written")
    promotion = await session.scalar(select(Promotion).where(Promotion.code == "NEW_MASTER"))
    if promotion is None:
        promotion = Promotion(code="NEW_MASTER", name_en="New Master", name_uk="Новий майстер",
                              discount_percent=30, eligibility_type=PromotionEligibilityType.inactive_customers,
                              inactive_days=90, is_public=False, recipient_offer_only=True)
        session.add(promotion)
        await session.flush()
    elif not promotion.recipient_offer_only or promotion.discount_percent != 30:
        raise ValueError("NEW_MASTER exists with incompatible rules; preserve it and resolve before initialization")
    template = await session.scalar(select(MessageTemplate).where(MessageTemplate.name == TEMPLATE_NAME))
    if template is None:
        template = MessageTemplate(name=TEMPLATE_NAME, channel=MessageChannel.sms, language="uk", body=DEFAULT_BODY)
        session.add(template)
        await session.flush()
    segment = await session.scalar(select(CustomerSegment).where(CustomerSegment.name == SEGMENT_NAME))
    if segment is None:
        segment = CustomerSegment(name=SEGMENT_NAME, rules=RULES)
        session.add(segment)
        await session.flush()
    campaign = await session.scalar(select(Campaign).where(Campaign.name == CAMPAIGN_NAME))
    if campaign is None:
        ids = list((await session.scalars(select(BarberService.id).where(
            BarberService.master_id == master.id, BarberService.is_active.is_(True)))).all())
        campaign = Campaign(name=CAMPAIGN_NAME, type=CampaignType.re_engagement, status=CampaignStatus.draft,
                            channel=MessageChannel.sms, purpose=MessagePurpose.marketing, template_id=template.id,
                            timezone="Europe/Kyiv", offer_master_id=master.id, offer_promotion_id=promotion.id,
                            offer_service_ids=ids, master_name_for_message="Андрія Віканова",
                            sending_window={"start": "10:00", "end": "18:00", "days": list(range(7))},
                            sms_recipients_per_minute=20, marketing_frequency_days=7,
                            marketing_max_contacts=1, marketing_cap_days=7,
                            metadata_json={"segment_ids": [segment.id], "exclude_upcoming_booking": True,
                                           "exclude_returned_since_snapshot": True})
        session.add(campaign)
        await session.flush()
    return {"campaign_id": campaign.id, "promotion_id": promotion.id, "template_id": template.id,
            "segment_id": segment.id, "status": campaign.status.value, "validity_required": campaign.offer_expires_at is None}


async def main():
    async with AsyncSessionLocal() as session:
        result = await initialize(session)
        await session.commit()
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
