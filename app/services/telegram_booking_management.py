"""Customer-owned, versioned cancellation and atomic rescheduling in Telegram."""
from datetime import UTC, date, datetime, timedelta
import secrets

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.booking import Booking, BookingServiceItem, BookingStatus, Master
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.promotion import PromotionService
from app.services.master_notifications import cancelled_booking_telegram
from app.services.waitlist_offers import FreedBookingSlot, offer_freed_booking_slot

service = BookingServiceLayer()


def booking_management_markup(booking):
    return {'inline_keyboard': [[
        {'text': 'Перенести', 'callback_data': f'reschedule_booking:{booking.id}'},
        {'text': 'Скасувати', 'callback_data': f'cancel_booking:{booking.id}'},
    ]]}


def _version(booking):
    return [booking.master_id, booking.start_at.astimezone(UTC).isoformat(),
            booking.end_at.astimezone(UTC).isoformat()]


async def _owned_booking(session, routes, chat_id, contact, booking_id):
    bot = await routes._get_telegram_bot_session(session, chat_id)
    contact = await routes._telegram_contact_for_chat(session, chat_id=chat_id, telegram_contact=contact)
    if contact is None or str(contact.chat_id) != str(chat_id) or not routes._telegram_contact_allows_booking_flow(contact):
        return bot, None
    customer_id = await routes._telegram_booking_lookup_customer_id(session, bot_session=None, telegram_contact=contact)
    if customer_id is None:
        return bot, None
    booking = (await session.execute(select(Booking).where(
        Booking.id == booking_id, Booking.customer_id == customer_id,
        Booking.status == BookingStatus.confirmed, Booking.start_at > datetime.now(UTC),
    ).options(selectinload(Booking.master), selectinload(Booking.redirected_from_master),
        selectinload(Booking.service), selectinload(Booking.service_items).selectinload(BookingServiceItem.service)
    ).with_for_update())).scalar_one_or_none()
    return bot, booking


async def _send(telegram, routes, chat_id, body, markup=None):
    await routes._safe_send_telegram_message(telegram, destination=chat_id, body=body, reply_markup=markup)


async def _dates(telegram, session, routes, chat_id, bot, booking, start=None):
    start = max(start or datetime.now(KYIV_TZ).date(), datetime.now(KYIV_TZ).date())
    horizon = service.availability_horizon_end_date()
    end = min(start + timedelta(days=13), horizon)
    data = dict(bot.payload_json.get('booking_management', {}))
    data['dates_from'] = start.isoformat()
    data['dates_to'] = end.isoformat()
    data.pop('slots', None)
    data.pop('chosen_start', None)
    data.pop('confirmation', None)
    data['slot_token'] = secrets.token_hex(8)
    bot.payload_json = {**bot.payload_json, 'booking_management': data}
    await routes._commit_telegram_action(session)
    rows = []
    day = start
    while day <= end:
        if not service.is_closed_business_day(day):
            rows.append([{'text': routes._visit_date_button_text(day),
                'callback_data': f'manage_date:{data["slot_token"]}:{day:%Y%m%d}'}])
        day += timedelta(days=1)
    if end < horizon:
        rows.append([{'text': 'Наступні дати →', 'callback_data': f'manage_next:{data["slot_token"]}'}])
    rows.append([{'text': 'Залишити поточний запис', 'callback_data': f'manage_keep:{data["token"]}'}])
    await _send(telegram, routes, chat_id,
        'Оберіть нову дату. Поточний запис збережеться до підтвердження переносу.\n\n'+routes._booking_view_message(booking),
        {'inline_keyboard': rows})


async def _slots(session, booking, day):
    if not datetime.now(KYIV_TZ).date() <= day <= service.availability_horizon_end_date():
        raise HTTPException(409, "Date unavailable")
    master = await session.get(Master, booking.master_id)
    if master is None or not master.is_active:
        raise HTTPException(409, 'Master unavailable')
    start, end = service.day_bounds(day)
    facts = await service.load_calendar_range_data(session, [booking.master_id], start, end, exclude_booking_id=booking.id)
    # Duration and price belong to the saved visit, not today's service catalog.
    duration = booking.end_at.astimezone(UTC) - booking.start_at.astimezone(UTC)
    return [start for _, start, _ in service.iter_calendar_slots(facts, calendar_master_id=booking.master_id,
        start_date=day, end_date=day, duration=duration)]


async def _show_slots(telegram, session, routes, chat_id, bot, booking, day):
    try:
        starts = await _slots(session, booking, day)
    except HTTPException:
        starts = []
    data = dict(bot.payload_json['booking_management'])
    data['slot_token'] = secrets.token_hex(8)
    data['slots'] = [item.isoformat() for item in starts]
    data['day'] = day.isoformat()
    data.pop('chosen_start', None)
    data.pop('confirmation', None)
    bot.payload_json = {**bot.payload_json, 'booking_management': data}
    await routes._commit_telegram_action(session)
    rows = [[{'text': datetime.fromisoformat(item).astimezone(KYIV_TZ).strftime('%H:%M'),
        'callback_data': f'manage_slot:{data["slot_token"]}:{index}'}] for index, item in enumerate(data['slots'])]
    rows.append([{'text': 'Обрати іншу дату', 'callback_data': f'manage_dates:{data["token"]}'}])
    rows.append([{'text': 'Залишити поточний запис', 'callback_data': f'manage_keep:{data["token"]}'}])
    await _send(telegram, routes, chat_id,
        f'📅 {routes._visit_date_button_text(day)}\n\n'+('Оберіть новий час.' if starts else 'Немає вільного часу. Поточний запис збережено.'),
        {'inline_keyboard': rows})


async def dispatch_telegram_booking_management(telegram, session, *, chat_id, text,
        telegram_contact, callback_query_id, background_tasks=None):
    from app.api.v1.routes import messaging as routes
    prefixes = ('cancel_booking:', 'reschedule_booking:', 'manage_')
    if not isinstance(text, str) or not text.startswith(prefixes):
        return None
    await routes._safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
    action, _, value = text.partition(':')
    bot = await routes._get_telegram_bot_session(session, chat_id)
    opening = action in {'cancel_booking', 'reschedule_booking'}
    data = (bot.payload_json or {}).get('booking_management', {}) if bot else {}
    try:
        booking_id = int(value) if opening else int(data['booking_id'])
        if not 1 <= booking_id <= 2**31 - 1:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        await _send(telegram, routes, chat_id, 'Ця кнопка застаріла. Відкрийте «Мої записи».', routes._after_booking_reply_markup())
        return {'ok': True, 'handled': True, 'action': 'booking_management_stale'}
    bot, booking = await _owned_booking(session, routes, chat_id, telegram_contact, booking_id)
    if booking is None or bot is None:
        await _send(telegram, routes, chat_id, 'Запис недоступний. Відкрийте «Мої записи».', routes._after_booking_reply_markup())
        return {'ok': True, 'handled': True, 'action': 'booking_management_unavailable'}
    if opening:
        data = {'token': secrets.token_hex(8), 'booking_id': booking.id, 'version': _version(booking),
                'mode': 'cancel' if action == 'cancel_booking' else 'reschedule'}
        bot.payload_json = {**(bot.payload_json or {}), 'booking_management': data}
        if action == 'reschedule_booking':
            await _dates(telegram, session, routes, chat_id, bot, booking)
        else:
            await routes._commit_telegram_action(session)
            await _send(telegram, routes, chat_id, 'Скасувати цей запис?\n\n'+routes._booking_view_message(booking),
                {'inline_keyboard': [
                    [{'text': 'Перенести замість скасування', 'callback_data': f'reschedule_booking:{booking.id}'}],
                    [{'text': 'Так, скасувати', 'callback_data': f'manage_cancel:{data["token"]}'}],
                    [{'text': 'Залишити запис', 'callback_data': f'manage_keep:{data["token"]}'}],
                ]})
        return {'ok': True, 'handled': True, 'action': 'booking_management_prompt'}
    parts = value.split(':')
    expected = data.get('slot_token') if action in {'manage_date', 'manage_slot', 'manage_next'} else data.get('confirmation') if action == 'manage_confirm' else data.get('token')
    if not expected or parts[0] != expected or data.get('version') != _version(booking):
        await _send(telegram, routes, chat_id, 'Ця кнопка застаріла. Відкрийте актуальний запис.', routes._after_booking_reply_markup())
        return {'ok': True, 'handled': True, 'action': 'booking_management_stale'}
    if action == 'manage_keep':
        bot.payload_json = {key: val for key, val in bot.payload_json.items() if key != 'booking_management'}
        await routes._commit_telegram_action(session)
        await _send(telegram, routes, chat_id, 'Ваш поточний запис збережено.', routes._after_booking_reply_markup())
    elif action == 'manage_cancel' and data.get('mode') == 'cancel':
        freed = FreedBookingSlot(booking.master_id, booking.start_at, booking.end_at, booking.id, booking.redirected_from_master_id or booking.master_id)
        await PromotionService().sync_first_visit_status(session, booking, BookingStatus.cancelled)
        booking.status = BookingStatus.cancelled
        booking.cancelled_at = datetime.now(UTC)
        booking.completed_at = None
        bot.payload_json = {key: val for key, val in bot.payload_json.items() if key != 'booking_management'}
        await routes._commit_telegram_action(session)
        if background_tasks is not None:
            background_tasks.add_task(offer_freed_booking_slot, freed)
            background_tasks.add_task(routes.master_telegram_notification_service.send_cancelled_booking_to_master, cancelled_booking_telegram(booking))
        await _send(telegram, routes, chat_id, routes.TELEGRAM_BOOKING_CANCELLED_MESSAGE, routes._after_booking_reply_markup())
    elif data.get('mode') != 'reschedule':
        return {'ok': True, 'handled': True, 'action': 'booking_management_stale'}
    elif action in {'manage_dates', 'manage_next'}:
        start = date.fromisoformat(data['dates_to'])+timedelta(days=1) if action == 'manage_next' else None
        if start is not None and start > service.availability_horizon_end_date():
            return {'ok': True, 'handled': True, 'action': 'booking_management_stale'}
        await _dates(telegram, session, routes, chat_id, bot, booking, start)
    elif action == 'manage_date':
        try:
            day = datetime.strptime(parts[1], '%Y%m%d').date()
            assert date.fromisoformat(data['dates_from']) <= day <= date.fromisoformat(data['dates_to'])
        except (ValueError, IndexError, AssertionError):
            return {'ok': True, 'handled': True, 'action': 'booking_management_stale'}
        await _show_slots(telegram, session, routes, chat_id, bot, booking, day)
    elif action == 'manage_slot':
        try:
            index = int(parts[1])
            if index < 0:
                raise ValueError
            chosen = data['slots'][index]
        except (ValueError, IndexError, KeyError):
            return {'ok': True, 'handled': True, 'action': 'booking_management_stale'}
        data = {**data, 'chosen_start': chosen, 'confirmation': secrets.token_hex(8)}
        bot.payload_json = {**bot.payload_json, 'booking_management': data}
        await routes._commit_telegram_action(session)
        new_start = datetime.fromisoformat(chosen)
        new_end = new_start.astimezone(UTC)+(booking.end_at.astimezone(UTC)-booking.start_at.astimezone(UTC))
        await _send(telegram, routes, chat_id,
            'Підтвердіть перенесення:\n'+routes._booking_view_message(booking)+f'\n\nНовий час: {routes._booking_time_range(new_start, new_end)}\nЦіна та послуги збережуться.',
            {'inline_keyboard': [[{'text': 'Підтвердити перенос', 'callback_data': f'manage_confirm:{data["confirmation"]}'}],
                                [{'text': 'Залишити поточний запис', 'callback_data': f'manage_keep:{data["token"]}'}]]})
    elif action == 'manage_confirm':
        start = datetime.fromisoformat(data['chosen_start']).astimezone(KYIV_TZ)
        end = (start.astimezone(UTC)+(booking.end_at.astimezone(UTC)-booking.start_at.astimezone(UTC))).astimezone(KYIV_TZ)
        try:
            master = await session.get(Master, booking.master_id, with_for_update=True, populate_existing=True)
            if master is None or not master.is_active:
                raise HTTPException(409, "Master unavailable")
            service.ensure_valid_interval(start, end)
            service.ensure_not_past(start)
            service.ensure_availability_horizon(start, end)
            service.ensure_within_working_hours(start, end)
            await service.ensure_slot_available(session, booking.master_id, start, end, exclude_booking_id=booking.id)
            # Availability can change between preview and confirmation.
            await service.ensure_booking_within_availability(session, booking.master_id, start, end)
        except HTTPException:
            await _send(telegram, routes, chat_id, 'Новий час уже недоступний. Поточний запис збережено. Оберіть інший час.')
            await _show_slots(telegram, session, routes, chat_id, bot, booking, start.date())
            return {'ok': True, 'handled': True, 'action': 'reschedule_conflict'}
        freed = FreedBookingSlot(booking.master_id, booking.start_at, booking.end_at, booking.id, booking.redirected_from_master_id or booking.master_id)
        booking.start_at, booking.end_at = start, end
        booking.sms_two_hour_reminder_sent_at = None
        bot.payload_json = {key: val for key, val in bot.payload_json.items() if key != 'booking_management'}
        await routes._commit_telegram_action(session)
        if background_tasks is not None:
            background_tasks.add_task(offer_freed_booking_slot, freed)
            routes._schedule_booking_notifications(background_tasks, booking)
        await _send(telegram, routes, chat_id, 'Запис перенесено.\n\n'+routes._booking_view_message(booking), routes._after_booking_reply_markup())
    else:
        return {'ok': True, 'handled': True, 'action': 'booking_management_stale'}
    return {'ok': True, 'handled': True, 'action': 'booking_management_done'}
