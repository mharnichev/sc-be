"""Verified returning-client entry points into the ordinary Telegram booking flow."""
from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.models.booking import Booking, BookingStatus
from app.models.repeat_booking import RepeatBookingEventType, RepeatBookingOfferStatus
from app.services.repeat_booking import repeat_booking_service
from app.services.telegram_booking_funnel import record_telegram_step

REPEAT_BOOKING_TEXT = "Записатися знову"
REPEAT_BOOKING_PREFIX = "repeat_booking:"
REPEAT_OFFER_PREFIX = "repeat_offer:"


def is_telegram_repeat_action(text: str | None) -> bool:
    return bool(text and (text.strip().casefold() == REPEAT_BOOKING_TEXT.casefold()
                         or text.startswith((REPEAT_BOOKING_PREFIX, REPEAT_OFFER_PREFIX))))


async def validate_telegram_repeat_booking(session, bot_session):
    """Validate without a row lock; creation locks the master before final attribution."""
    offer_id = (bot_session.payload_json or {}).get("repeat_booking_offer_id")
    if offer_id is None:
        return None
    if type(offer_id) is not int or not bot_session.linked_customer_id:
        raise HTTPException(401, "Invalid or expired repeat booking link")
    return await repeat_booking_service.telegram_offer(
        session, customer_id=bot_session.linked_customer_id, offer_id=offer_id, for_update=False,
    )


async def attribute_telegram_repeat_booking(
    session, bot_session, booking, *, requested_master_id: int, requested_service_ids: list[int],
):
    """Consume after normal creation flush, in the SAME booking transaction/savepoint."""
    offer_id = (bot_session.payload_json or {}).get("repeat_booking_offer_id")
    if offer_id is None:
        return None
    if type(offer_id) is not int or not bot_session.linked_customer_id:
        raise HTTPException(401, "Invalid or expired repeat booking link")
    return await repeat_booking_service.attribute_telegram_booking(
        session, offer_id=offer_id, customer_id=bot_session.linked_customer_id,
        booking=booking, requested_master_id=requested_master_id,
        requested_service_ids=requested_service_ids,
    )


async def dispatch_telegram_return_booking(
    telegram, session, *, chat_id: str, text: str | None, telegram_contact,
    callback_query_id: str | None, background_tasks=None,
) -> dict | None:
    """Called after the webhook's existing verified-contact gate. Never creates a booking."""
    if not is_telegram_repeat_action(text):
        return None
    # Route helpers are loaded only on invocation to avoid the route/service import cycle.
    from app.api.v1.routes import messaging as ui

    await ui._safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
    contact = await ui._telegram_contact_for_chat(
        session, chat_id=chat_id, telegram_contact=telegram_contact,
    )
    if (contact is None or str(contact.chat_id) != str(chat_id)
            or not ui._telegram_contact_allows_booking_flow(contact)):
        await ui._safe_send_telegram_message(
            telegram, destination=chat_id, body=ui.TELEGRAM_START_WELCOME_MESSAGE,
            reply_markup=ui._share_contact_reply_markup(),
        )
        return {"ok": True, "handled": True, "action": "repeat_booking_contact_required"}
    # Do not fall back to a possibly stale customer association on the old draft.
    customer_id = await ui._telegram_booking_lookup_customer_id(
        session, bot_session=None, telegram_contact=contact,
    )
    offer = None
    source = None
    if text.startswith(REPEAT_OFFER_PREFIX):
        try:
            if customer_id is None:
                raise HTTPException(401, "Invalid or expired repeat booking link")
            offer = await repeat_booking_service.telegram_offer(
                session, customer_id=customer_id, token=text.removeprefix(REPEAT_OFFER_PREFIX),
            )
        except HTTPException:
            await ui._commit_telegram_action(session)
            await ui._safe_send_telegram_message(
                telegram, destination=chat_id,
                body="Ця пропозиція вже недоступна. Натисніть «Записатися знову» або «Новий запис».",
                reply_markup=ui._after_booking_reply_markup(),
            )
            return {"ok": True, "handled": True, "action": "repeat_booking_offer_unavailable"}
        if offer.opened_at is None:
            offer.opened_at = repeat_booking_service.now()
            await repeat_booking_service.record_event(session, offer, RepeatBookingEventType.link_opened)
        if offer.started_at is None:
            offer.started_at = repeat_booking_service.now()
            await repeat_booking_service.record_event(session, offer, RepeatBookingEventType.booking_started)
        offer.status = RepeatBookingOfferStatus.started
        master_id, service_ids = offer.preferred_master_id, list(offer.service_ids)
    else:
        stmt = (select(Booking).execution_options(include_archived_bookings=True)
                .options(*repeat_booking_service._booking_load_options())
                .where(Booking.customer_id == customer_id, Booking.status == BookingStatus.completed,
                       Booking.completed_at.is_not(None))
                .order_by(Booking.completed_at.desc(), Booking.start_at.desc(), Booking.id.desc()).limit(1))
        if text.startswith(REPEAT_BOOKING_PREFIX):
            raw_id = text.removeprefix(REPEAT_BOOKING_PREFIX)
            if (not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 10
                    or not 1 <= int(raw_id) <= 2**31 - 1):
                return await _unavailable_source(telegram, chat_id)
            stmt = stmt.where(Booking.id == int(raw_id))
        if customer_id is not None:
            source = (await session.execute(stmt)).scalar_one_or_none()
        if source is None and text.startswith(REPEAT_BOOKING_PREFIX):
            return await _unavailable_source(telegram, chat_id)
        master_id = source.public_master_id if source else None
        service_ids = repeat_booking_service.snapshot_service_ids(source) if source else []

    bot_session = await ui._upsert_telegram_bot_session(
        session, chat_id=chat_id, telegram_contact=contact, state="booking_started",
    )
    ui._reset_telegram_booking_session(bot_session, state="booking_started")
    # Replacement removes all date/time, confirmation, recovery, and stale booking tokens.
    bot_session.payload_json = {"telegram_funnel_attempt_id": str(uuid4())}
    bot_session.linked_customer_id = customer_id
    if offer is not None:
        bot_session.payload_json = {**bot_session.payload_json, "repeat_booking_offer_id": offer.id}
    await record_telegram_step(session, bot_session, "booking_start")
    await record_telegram_step(session, bot_session, "contact_entered")
    master, services, active = await repeat_booking_service._active_context(session, master_id, service_ids)
    if not active or not services:
        await ui._commit_telegram_action(session)
        await ui._safe_send_telegram_message(
            telegram, destination=chat_id,
            body=("Попередній майстер або послуги вже недоступні. Оберіть майстра та послуги для нового запису."
                  if source is not None or offer is not None
                  else "У вас ще немає завершених візитів. Оберіть майстра для нового запису."),
            reply_markup=ui._booking_action_reply_markup(),
        )
        await ui._send_master_list(telegram, session, chat_id)
        return {"ok": True, "handled": True, "action": "repeat_booking_fallback"}
    bot_session.selected_master_id = master.id
    bot_session.selected_service_id = services[0].id
    bot_session.state = ui.TELEGRAM_SELECTING_SERVICES_STATE
    bot_session.payload_json = {**bot_session.payload_json, "selected_service_ids": service_ids}
    await record_telegram_step(session, bot_session, "master_selected")
    await record_telegram_step(session, bot_session, "service_selected")
    await ui._commit_telegram_action(session)
    summary = "\n".join(f"✓ {ui._service_button_text(item)} — {item.price} грн" for item in services)
    await ui._safe_send_telegram_message(
        telegram, destination=chat_id,
        body=(f"Записатися знову до {ui._master_display_name(master)}\n\n{summary}"
              f"\n\nВартість: {sum(item.price for item in services)} грн"
              f"\nТривалість: {sum(item.duration_minutes for item in services)} хв"
              "\nОберіть нову дату та час візиту."),
        reply_markup=ui._service_selected_reply_markup(),
    )
    await ui._handle_date_time_action(telegram, session, chat_id=chat_id)
    return {"ok": True, "handled": True, "action": "repeat_booking_start"}


async def _unavailable_source(telegram, chat_id):
    from app.api.v1.routes import messaging as ui
    await ui._safe_send_telegram_message(
        telegram, destination=chat_id,
        body="Завершений візит не знайдено. Натисніть «Записатися знову» або «Новий запис».",
        reply_markup=ui._after_booking_reply_markup(),
    )
    return {"ok": True, "handled": True, "action": "repeat_booking_unavailable"}
