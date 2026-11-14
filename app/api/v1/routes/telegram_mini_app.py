import hmac
import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes import messaging
from app.core.config import settings
from app.core.database import get_db_session
from app.services import telegram_booking_management as management

router = APIRouter(prefix="/telegram/booking-app")
PAGE = Path(__file__).with_name("telegram_mini_app.html")
NO_STORE = {"Cache-Control": "no-store"}


@router.get("", include_in_schema=False)
async def page():
    if not settings.telegram_booking_mini_app_url:
        raise HTTPException(404)
    return FileResponse(PAGE, headers={**NO_STORE, "Referrer-Policy": "no-referrer"})


async def chat_id(response: Response, init_data: str = Header(alias="X-Telegram-Init-Data", default="")) -> str:
    response.headers.update(NO_STORE)
    token = settings.telegram_bot_token
    if not token or not settings.telegram_booking_mini_app_url:
        raise HTTPException(404)
    try:
        pairs = parse_qsl(init_data, strict_parsing=True, max_num_fields=24)
        fields = dict(pairs)
        signature = fields.pop("hash")
        if len(fields) + 1 != len(pairs):
            raise ValueError
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        key = hmac.digest(b"WebAppData", token.encode(), "sha256")
        expected = hmac.digest(key, check.encode(), "sha256").hex()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
        age = datetime.now(UTC).timestamp() - int(fields["auth_date"])
        if not -30 <= age <= 3600:
            raise ValueError
        user_id = json.loads(fields["user"])["id"]
        if type(user_id) is not int or user_id <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HTTPException(401, "Відкрийте застосунок знову в Telegram.") from None
    return str(user_id)


def card(booking):
    return {
        "id": booking.id,
        "when": messaging._booking_time_range(booking.start_at, booking.end_at),
        "master": messaging._booking_master_name(booking),
        "services": [messaging._service_button_text(item) for item in messaging._booking_services(booking)],
        "price": booking.total_amount,
        "version": management._version(booking),
    }


@router.get("/visits")
async def visits(owner: str = Depends(chat_id), session: AsyncSession = Depends(get_db_session)):
    contact = await messaging._telegram_contact_for_chat(session, chat_id=owner, telegram_contact=None)
    customer_id = await messaging._telegram_booking_lookup_customer_id(
        session, bot_session=None, telegram_contact=contact,
    ) if contact else None
    bookings = await messaging._telegram_customer_bookings(session, customer_id=customer_id, booking_id=None)
    now = datetime.now(UTC)
    dates = management._calendar_days(now.astimezone(management.KYIV_TZ).date(),
                                      management.service.availability_horizon_end_date())
    return {"visits": [card(item) for item in bookings if item.start_at > now], "dates": dates}


async def owned(session, owner, booking_id, *, lock=False):
    if not 1 <= booking_id <= 2**31 - 1:
        raise HTTPException(404, "Запис недоступний.")
    _, booking = await management._owned_booking(session, messaging, owner, None, booking_id, lock=lock)
    if booking is None:
        raise HTTPException(404, "Запис недоступний.")
    return booking


@router.get("/visits/{booking_id}/slots")
async def slots(booking_id: int, day: date, owner: str = Depends(chat_id),
                session: AsyncSession = Depends(get_db_session)):
    booking = await owned(session, owner, booking_id)
    return await management._slots(session, booking, day)


class MoveVisit(BaseModel):
    start: AwareDatetime
    version: tuple[int, str, str]


@router.post("/visits/{booking_id}/move")
async def move(booking_id: int, choice: MoveVisit, background_tasks: BackgroundTasks,
               owner: str = Depends(chat_id), session: AsyncSession = Depends(get_db_session)):
    booking = await owned(session, owner, booking_id, lock=True)
    if list(choice.version) != management._version(booking):
        raise HTTPException(409, "Запис змінився. Оновіть застосунок.")
    day = choice.start.astimezone(management.KYIV_TZ).date()
    try:
        if choice.start == booking.start_at or choice.start not in await management._slots(session, booking, day):
            raise HTTPException(409)
        freed = await management.reschedule_booking(session, booking, choice.start)
    except HTTPException:
        raise HTTPException(409, "Цей час уже недоступний. Поточний запис збережено.") from None
    result = card(booking)
    await session.commit()
    background_tasks.add_task(management.offer_freed_booking_slot, freed)
    messaging._schedule_booking_notifications(background_tasks, booking)
    return result
