"""Persist service SMS consent and explicitly accepted website marketing SMS."""
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.messaging import ClientCommunicationPreference, ConsentStatus
from app.services.booking import KYIV_TZ

async def record_booking_sms_consent(
    session: AsyncSession, customer_id: int, booking_id: int, *, marketing_sms_consent: bool = False,
) -> None:
    preference = ClientCommunicationPreference
    values = {
        "transactional_consent": ConsentStatus.opted_in,
        "booking_sms_consent_at": datetime.now(KYIV_TZ),
        "booking_sms_consent_booking_id": booking_id,
    }
    if marketing_sms_consent:
        values["marketing_consent"] = ConsentStatus.opted_in
    insert_values = {"marketing_consent": ConsentStatus.unknown, **values}
    # Unique customer key serializes concurrent booking/opt-out updates. A new
    # booking never restores contact after an explicit refusal or blacklist.
    await session.execute(insert(preference).values(
        customer_id=customer_id, **insert_values,
    ).on_conflict_do_update(
        index_elements=[preference.customer_id], set_=values,
        where=(
            (preference.marketing_consent != ConsentStatus.opted_out)
            & (preference.transactional_consent != ConsentStatus.opted_out)
            & preference.do_not_contact.is_(False)
            & preference.blacklisted_at.is_(None)
        ),
    ))
