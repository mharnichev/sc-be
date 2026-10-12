"""Draft-bound Telegram recovery menus; availability stays in existing services."""
from __future__ import annotations

from datetime import date

from app.services.booking import KYIV_TZ


def recovery_context(bot_session, desired_date: date) -> dict:
    payload = bot_session.payload_json or {}
    return {
        "master_id": bot_session.selected_master_id,
        "service_ids": sorted(payload.get("selected_service_ids", [])),
        "desired_date": desired_date.isoformat(),
        "attempt_id": payload.get("telegram_funnel_attempt_id"),
    }


def recovery_is_current(bot_session, token: str) -> bool:
    if bot_session is None or bot_session.state not in {"selecting_date", "selecting_time"}:
        return False
    payload = bot_session.payload_json or {}
    recovery = payload.get("booking_recovery") or {}
    context = recovery.get("context") or {}
    try:
        desired_date = date.fromisoformat(payload.get("selected_visit_date", ""))
    except (TypeError, ValueError):
        return False
    return bool(token and token == recovery.get("token")
                and context == recovery_context(bot_session, desired_date))


def recovery_markup(response, token: str) -> dict:
    rows = []
    for index, offer in enumerate([*response.same_master, *response.other_masters]):
        start = offer.start_at.astimezone(KYIV_TZ)
        label = ("Ваш майстер" if offer in response.same_master else offer.master.name)
        rows.append([{"text": f"{label}: {start:%d.%m %H:%M} · {offer.price} грн",
                      "callback_data": f"recover_slot:{token}:{index}"}])
    rows.extend([
        [{"text": "📅 Обрати іншу дату", "callback_data": "Обрати іншу дату"}],
        [{"text": "Повідомити про вільний час", "callback_data": f"recover_waitlist:{token}"}],
    ])
    return {"inline_keyboard": rows}
