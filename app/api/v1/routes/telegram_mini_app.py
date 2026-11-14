import hmac
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes import booking_alternatives, bookings, messaging, waitlist
from app.core.config import settings
from app.core.database import get_db_session
from app.services import telegram_booking_management as management
from app.models.booking import Booking, BookingSource
from app.schemas.booking import PublicBookingCreate, PublicBookingQuoteRequest
from app.schemas.booking_alternatives import BookingAlternativesRequest
from app.schemas.waitlist import PublicWaitlistCreate

router = APIRouter(prefix="/telegram/booking-app")
PAGE = Path(__file__).with_name("telegram_mini_app.html")
FONT = Path(__file__).with_name("telegram_booking_cy_grotesk.woff2")
NO_STORE = {"Cache-Control": "no-store"}


@router.get("", include_in_schema=False)
async def page():
    if not settings.telegram_booking_mini_app_url:
        raise HTTPException(404)
    return FileResponse(PAGE, headers={**NO_STORE, "Referrer-Policy": "no-referrer"})


@router.get("/assets/cy-grotesk-600.woff2", include_in_schema=False)
async def font():
    if not settings.telegram_booking_mini_app_url:
        raise HTTPException(404)
    return FileResponse(FONT, media_type="font/woff2", headers={"Cache-Control": "public, max-age=86400"})


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
        "master_id": booking.master_id,
        "service_ids": [item.id for item in messaging._booking_services(booking)],
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
    history = await messaging._telegram_customer_bookings(
        session, customer_id=customer_id, booking_id=None, history=True,
    )
    now = datetime.now(UTC)
    dates = management._calendar_days(now.astimezone(management.KYIV_TZ).date(),
                                      management.service.availability_horizon_end_date())
    return {"visits": [card(item) for item in bookings if item.start_at > now],
            "history": [card(item) for item in history], "dates": dates}


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
    return [start for start in await management._slots(session, booking, day) if start != booking.start_at]


@router.get("/visits/{booking_id}/next")
async def next_slots(booking_id: int, owner: str = Depends(chat_id),
                     session: AsyncSession = Depends(get_db_session)):
    booking = await owned(session, owner, booking_id)
    today = datetime.now(management.KYIV_TZ).date()
    dates = management._calendar_days(today, management.service.availability_horizon_end_date())
    for day in dates[:7]:
        starts = [start for start in await management._slots(session, booking, day) if start != booking.start_at]
        if starts:
            return {"day": day, "slots": starts}
    return {"day": None, "slots": []}


class VisitVersion(BaseModel):
    version: tuple[int, str, str]


class MoveVisit(VisitVersion):
    start: AwareDatetime


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


@router.post("/visits/{booking_id}/cancel")
async def cancel(booking_id: int, choice: VisitVersion, background_tasks: BackgroundTasks,
                 owner: str = Depends(chat_id), session: AsyncSession = Depends(get_db_session)):
    booking = await owned(session, owner, booking_id, lock=True)
    if list(choice.version) != management._version(booking):
        raise HTTPException(409, "Запис змінився. Оновіть застосунок.")
    freed = await management.cancel_booking(session, booking)
    notification = management.cancelled_booking_telegram(booking)
    await session.commit()
    background_tasks.add_task(management.offer_freed_booking_slot, freed)
    background_tasks.add_task(
        messaging.master_telegram_notification_service.send_cancelled_booking_to_master, notification,
    )
    return {"id": booking_id}

class Selection(BaseModel):
    master_id: int = Field(gt=0, le=2**31 - 1)
    service_ids: list[Annotated[int, Field(gt=0, le=2**31 - 1)]] = Field(min_length=1, max_length=10)

    @field_validator("service_ids")
    @classmethod
    def unique_services(cls, ids):
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate services")
        return ids


class SearchSelection(Selection):
    day: date


class BookingChoice(Selection):
    start: AwareDatetime


class ConfirmBooking(BookingChoice):
    expected_total_amount: int = Field(ge=0, strict=True)


async def customer_details(session, owner):
    contact = await messaging._telegram_contact_for_chat(session, chat_id=owner, telegram_contact=None)
    details = await messaging._telegram_booking_customer_details(session, contact)
    if details is None:
        raise HTTPException(409, "Поділіться номером телефону в чаті з ботом.")
    return contact, details


@router.get("/catalog")
async def catalog(owner: str = Depends(chat_id), session: AsyncSession = Depends(get_db_session)):
    await customer_details(session, owner)
    masters = await bookings.list_public_masters(session)
    return [{"id": master.id, "name": master.full_name_uk or master.full_name,
             "services": [{"id": item.id, "name": item.title_uk or item.name,
                           "price": item.price, "duration": item.duration_minutes,
                           "key": f"base:{item.base_service_id}" if item.base_service_id else
                           json.dumps([item.title_uk or item.name, item.title_en, item.duration_minutes, item.price])}
                          for item in master.services]} for master in masters if master.services]


@router.post("/search")
async def search(choice: SearchSelection, owner: str = Depends(chat_id),
                 session: AsyncSession = Depends(get_db_session)):
    await customer_details(session, owner)
    return await management.service.get_available_slots(
        session, master_id=choice.master_id, service_id=None, service_ids=choice.service_ids, target_date=choice.day,
    )


@router.post("/alternatives")
async def alternatives(choice: SearchSelection, owner: str = Depends(chat_id),
                       session: AsyncSession = Depends(get_db_session)):
    await customer_details(session, owner)
    selected = await management.service.get_active_services(session, choice.service_ids)
    try:
        payload = BookingAlternativesRequest(
            **choice.model_dump(exclude={"day"}), desired_date=choice.day,
            duration_minutes=sum(item.duration_minutes for item in selected),
        )
    except ValidationError:
        raise HTTPException(422, "Перевірте майстра та обрані послуги.") from None
    result = await booking_alternatives.service.find_page(session, payload)
    return result.other_masters


@router.post("/quote")
async def quote(choice: BookingChoice, owner: str = Depends(chat_id),
                session: AsyncSession = Depends(get_db_session)):
    _, (_, phone, email) = await customer_details(session, owner)
    return await bookings.quote_public_booking(PublicBookingQuoteRequest(
        **choice.model_dump(exclude={"start"}), start_at=choice.start, customer_phone=phone, customer_email=email,
    ), session)


@router.post("/create", status_code=201)
async def create(choice: ConfirmBooking, background_tasks: BackgroundTasks, owner: str = Depends(chat_id),
                 session: AsyncSession = Depends(get_db_session)):
    contact, (name, phone, email) = await customer_details(session, owner)
    try:
        payload = PublicBookingCreate(
            **choice.model_dump(exclude={"start"}), start_at=choice.start,
            customer_name=name, customer_phone=phone, customer_email=email,
        )
    except ValidationError:
        raise HTTPException(422, "Перевірте обрані послуги та контакт у боті.") from None
    created = await management.service.create_public_booking(session, payload, source=BookingSource.telegram, commit=False)
    booking = (await session.execute(select(Booking).where(Booking.id == created.id).options(
        selectinload(Booking.master), *bookings.booking_response_options(),
    ))).scalar_one()
    contact.linked_customer_id = booking.customer_id
    result = card(booking)
    await session.commit()
    messaging._schedule_booking_notifications(background_tasks, booking)
    return result


class WaitlistChoice(SearchSelection):
    notification_consent: bool = Field(strict=True)


@router.post("/waitlist", status_code=201)
async def join_waitlist(choice: WaitlistChoice, background_tasks: BackgroundTasks,
                        owner: str = Depends(chat_id), session: AsyncSession = Depends(get_db_session)):
    _, (name, phone, _) = await customer_details(session, owner)
    try:
        payload = PublicWaitlistCreate(
            customer_name=name, customer_phone=phone, service_ids=choice.service_ids,
            preferred_master_id=choice.master_id, desired_date=choice.day,
            notification_consent=choice.notification_consent,
        )
    except ValidationError:
        raise HTTPException(422, "Перевірте обрані послуги та контакт у боті.") from None
    return await waitlist.create_waitlist_request(payload, background_tasks, session)
