from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user, get_current_master
from app.dependencies.common import PaginationDep
from app.models.booking import BarberService, Booking, BookingServiceItem, BookingStatus, Master
from app.models.customer import Customer
from app.models.messaging import (
    Campaign,
    CampaignAudienceFilter,
    CampaignStatus,
    CampaignType,
    ClientCommunicationPreference,
    ConsentStatus,
    MessageChannel,
    MessageDeliveryStatus,
    MessageLog,
    MasterMessageDelivery,
    MasterScheduleReminder,
    MessagePurpose,
    MessageRecipient,
    MessageTemplate,
    TelegramBotSession,
    ReviewRequest,
    TelegramContact,
)
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.booking import PublicBookingCreate
from app.schemas.messaging import (
    AudienceCriteria,
    CampaignCreate,
    CampaignRecipient,
    CampaignResponse,
    CampaignUpdate,
    ClientCommunicationPreferenceUpdate,
    MessageLogResponse,
    MessageRecipientResponse,
    MessageTemplateCreate,
    MessageTemplateResponse,
    MessageTemplateUpdate,
    MessagingAnalyticsResponse,
    RenderPreviewRequest,
    RenderPreviewResponse,
    StartCampaignRequest,
    TestMessageRequest,
)
from app.services.booking import BookingServiceLayer, KYIV_TZ
from app.services.booking_sms_notifications import booking_sms_notification_service
from app.services.customer_activity_notifications import customer_activity_notification_service
from app.services.email_notifications import NewBookingEmail, email_notification_service
from app.services.master_notifications import (
    NewBookingTelegram,
    cancelled_booking_telegram,
    master_telegram_notification_service,
)
from app.services.messaging import MessagingService, TelegramMessageProvider

logger = logging.getLogger(__name__)

backoffice_router = APIRouter()
public_router = APIRouter()
service = MessagingService()
booking_service_layer = BookingServiceLayer()
campaign_repo = BaseRepository(Campaign)
template_repo = BaseRepository(MessageTemplate)
recipient_repo = BaseRepository(MessageRecipient)
log_repo = BaseRepository(MessageLog)


class AudienceRequest(BaseModel):
    rules: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=1000)


class CampaignStatusUpdate(BaseModel):
    status: CampaignStatus


class ManualCustomerMessageRequest(BaseModel):
    body: str = Field(min_length=1)
    channel: str = "telegram"


TELEGRAM_CUSTOMER_CONNECT_SCOPE = "telegram_customer_connect"
TELEGRAM_MASTER_CONNECT_SCOPE = "telegram_master_connect"
TELEGRAM_CUSTOMER_CONNECT_TOKEN_DAYS = 30
NEW_BOOKING_BOT_TEXTS = {
    "/booking",
    "new_booking",
    "new booking",
    "новий запис",
    "нова запис",
    "новая запись",
    "записатися",
    "записаться",
}
TELEGRAM_START_WELCOME_MESSAGE = (
    'Вітаємо, тепер записатися стало простіше! Для початку натисніть "Поділитись контактом" унизу.'
)
TELEGRAM_SHARE_CONTACT_BUTTON_TEXT = "Поділитись контактом"
TELEGRAM_CONTACT_SAVED_MESSAGE = "Контакт збережено.\n\nБудь ласка, оберіть потрібну дію:"
TELEGRAM_BOOKING_ACTION_BUTTONS = ("Майстер", "Послуги", "Дата і час", "Скасувати")
TELEGRAM_MASTER_ACTION_TEXTS = {"майстер", "мастер"}
TELEGRAM_SELECT_BUTTON_TEXT = "Обрати"
TELEGRAM_SELECT_MASTER_CALLBACK_PREFIX = "select_master:"
TELEGRAM_MASTER_SELECTED_STATE = "master_selected"
TELEGRAM_MASTER_SELECTED_ACTION_BUTTONS = ("Послуги", "Дата та час", "Скасувати")
TELEGRAM_SERVICES_ACTION_TEXTS = {"послуги", "услуги"}
TELEGRAM_SELECTING_SERVICES_STATE = "selecting_services"
TELEGRAM_SELECT_SERVICE_CALLBACK_PREFIX = "select_service:"
TELEGRAM_SERVICE_SELECTED_ACTION_BUTTONS = ("Дата та час", "Скасувати")
TELEGRAM_DATE_TIME_ACTION_TEXTS = {"дата та час", "дата і час", "дата и время"}
TELEGRAM_SELECTING_DATE_STATE = "selecting_date"
TELEGRAM_SELECT_DATE_CALLBACK_PREFIX = "select_date:"
TELEGRAM_SELECTING_TIME_STATE = "selecting_time"
TELEGRAM_SELECT_TIME_CALLBACK_PREFIX = "select_time:"
TELEGRAM_READY_TO_BOOK_STATE = "ready_to_book"
TELEGRAM_READY_TO_BOOK_ACTION_BUTTONS = ("Забронювати", "Скасувати")
TELEGRAM_BOOK_ACTION_TEXTS = {"забронювати", "забранювати", "забронировать"}
TELEGRAM_CANCEL_DRAFT_ACTION_TEXTS = {"скасувати", "отменить", "cancel"}
TELEGRAM_DRAFT_CANCELLED_MESSAGE = "Дію скасовано.\n\nБудь ласка, оберіть потрібну дію:"
TELEGRAM_BOOKED_STATE = "booked"
TELEGRAM_AFTER_BOOKING_ACTION_BUTTONS = ("Новий запис", "Перегляд записів")
TELEGRAM_VIEW_BOOKINGS_ACTION_TEXTS = {"перегляд записів", "просмотр записей"}
TELEGRAM_CANCEL_BOOKING_CALLBACK_PREFIX = "cancel_booking:"
TELEGRAM_BOOKING_CANCELLED_MESSAGE = "Запис скасовано."
TELEGRAM_MASTER_PHOTO_MAX_SIZE = (1280, 1280)
TELEGRAM_MASTER_PHOTO_JPEG_QUALITY = 88
TELEGRAM_MASTER_PHOTO_CACHE_FOLDER = "telegram/master-photos"
TELEGRAM_DATE_BUTTON_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
TELEGRAM_DATE_DETAIL_WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")
TELEGRAM_DATE_DETAIL_MONTHS = (
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)
TELEGRAM_MAX_VISIT_DATE_BUTTONS = 14


def _telegram_bot_username() -> str:
    if not settings.telegram_bot_username:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Telegram bot username is not configured")
    return settings.telegram_bot_username.removeprefix("@")


def _customer_connect_token(customer_id: int) -> str:
    expires_at = int((datetime.now(UTC) + timedelta(days=TELEGRAM_CUSTOMER_CONNECT_TOKEN_DAYS)).timestamp())
    signature = _customer_connect_signature(customer_id, expires_at)
    return f"c_{customer_id}_{expires_at}_{signature}"


def _master_connect_token(master_id: int) -> str:
    expires_at = int((datetime.now(UTC) + timedelta(days=TELEGRAM_CUSTOMER_CONNECT_TOKEN_DAYS)).timestamp())
    signature = _master_connect_signature(master_id, expires_at)
    return f"m_{master_id}_{expires_at}_{signature}"


def _customer_id_from_connect_token(token: str) -> int:
    parts = token.split("_", maxsplit=3)
    if len(parts) != 4 or parts[0] != "c":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram connect token")
    try:
        customer_id = int(parts[1])
        expires_at = int(parts[2])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram connect token subject") from exc
    if expires_at < int(datetime.now(UTC).timestamp()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Telegram connect token expired")
    expected_signature = _customer_connect_signature(customer_id, expires_at)
    if not hmac.compare_digest(parts[3], expected_signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram connect token signature")
    return customer_id


def _master_id_from_connect_token(token: str) -> int:
    parts = token.split("_", maxsplit=3)
    if len(parts) != 4 or parts[0] != "m":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram connect token")
    try:
        master_id = int(parts[1])
        expires_at = int(parts[2])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram connect token subject") from exc
    if expires_at < int(datetime.now(UTC).timestamp()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Telegram connect token expired")
    expected_signature = _master_connect_signature(master_id, expires_at)
    if not hmac.compare_digest(parts[3], expected_signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Telegram connect token signature")
    return master_id


def _customer_connect_signature(customer_id: int, expires_at: int) -> str:
    payload = f"{TELEGRAM_CUSTOMER_CONNECT_SCOPE}:{customer_id}:{expires_at}".encode("utf-8")
    digest = hmac.new(settings.secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode("ascii").rstrip("=")


def _master_connect_signature(master_id: int, expires_at: int) -> str:
    payload = f"{TELEGRAM_MASTER_CONNECT_SCOPE}:{master_id}:{expires_at}".encode("utf-8")
    digest = hmac.new(settings.secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode("ascii").rstrip("=")


def _telegram_start_token(update: dict[str, Any]) -> str | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0] != "/start" or len(parts) == 1:
        return None
    return parts[1].strip()


def _telegram_message(update: dict[str, Any]) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if isinstance(message, dict):
        return message
    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict) and isinstance(callback_query.get("message"), dict):
        return callback_query["message"]
    return None


def _telegram_from_user(update: dict[str, Any]) -> dict[str, Any] | None:
    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict) and isinstance(callback_query.get("from"), dict):
        return callback_query["from"]
    message = _telegram_message(update)
    if isinstance(message, dict) and isinstance(message.get("from"), dict):
        return message["from"]
    return None


def _telegram_message_text(update: dict[str, Any]) -> str | None:
    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        data = callback_query.get("data")
        if isinstance(data, str) and data.strip():
            return data.strip()
    message = _telegram_message(update)
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    return text.strip() if isinstance(text, str) else None


def _telegram_callback_query_id(update: dict[str, Any]) -> str | None:
    callback_query = update.get("callback_query")
    if not isinstance(callback_query, dict):
        return None
    callback_query_id = callback_query.get("id")
    return str(callback_query_id) if callback_query_id is not None else None


def _telegram_update_id(update: dict[str, Any]) -> int | None:
    update_id = update.get("update_id")
    return update_id if isinstance(update_id, int) else None


async def _telegram_update_already_processed(session: AsyncSession, update: dict[str, Any]) -> bool:
    update_id = _telegram_update_id(update)
    chat_id = _telegram_chat_id(update)
    if update_id is None or chat_id is None:
        return False
    last_update_id = (
        await session.execute(select(TelegramContact.last_update_id).where(TelegramContact.chat_id == chat_id))
    ).scalar_one_or_none()
    return last_update_id is not None and update_id <= last_update_id


def _is_plain_start_command(text: str | None) -> bool:
    return text.casefold() == "/start" if text else False


def _is_contact_message(update: dict[str, Any]) -> bool:
    return _telegram_contact_phone(update) is not None


def _telegram_contact_allows_booking_flow(telegram_contact: TelegramContact | None) -> bool:
    return bool(
        telegram_contact
        and (
            getattr(telegram_contact, "phone", None)
            or getattr(telegram_contact, "linked_customer_id", None)
        )
    )


def _is_master_action(text: str | None) -> bool:
    return text.casefold() in TELEGRAM_MASTER_ACTION_TEXTS if text else False


def _is_services_action(text: str | None) -> bool:
    return text.casefold() in TELEGRAM_SERVICES_ACTION_TEXTS if text else False


def _is_date_time_action(text: str | None) -> bool:
    return text.casefold() in TELEGRAM_DATE_TIME_ACTION_TEXTS if text else False


def _is_book_action(text: str | None) -> bool:
    return text.casefold() in TELEGRAM_BOOK_ACTION_TEXTS if text else False


def _is_cancel_draft_action(text: str | None) -> bool:
    return text.casefold() in TELEGRAM_CANCEL_DRAFT_ACTION_TEXTS if text else False


def _is_view_bookings_action(text: str | None) -> bool:
    return text.casefold() in TELEGRAM_VIEW_BOOKINGS_ACTION_TEXTS if text else False


def _selected_master_id_from_callback(text: str | None) -> int | None:
    if not text or not text.startswith(TELEGRAM_SELECT_MASTER_CALLBACK_PREFIX):
        return None
    try:
        return int(text.removeprefix(TELEGRAM_SELECT_MASTER_CALLBACK_PREFIX))
    except ValueError:
        return None


def _selected_service_id_from_callback(text: str | None) -> int | None:
    if not text or not text.startswith(TELEGRAM_SELECT_SERVICE_CALLBACK_PREFIX):
        return None
    try:
        return int(text.removeprefix(TELEGRAM_SELECT_SERVICE_CALLBACK_PREFIX))
    except ValueError:
        return None


def _selected_visit_date_from_callback(text: str | None) -> date | None:
    if not text or not text.startswith(TELEGRAM_SELECT_DATE_CALLBACK_PREFIX):
        return None
    try:
        return date.fromisoformat(text.removeprefix(TELEGRAM_SELECT_DATE_CALLBACK_PREFIX))
    except ValueError:
        return None


def _selected_visit_time_from_callback(text: str | None) -> datetime | None:
    if not text or not text.startswith(TELEGRAM_SELECT_TIME_CALLBACK_PREFIX):
        return None
    try:
        value = datetime.fromisoformat(text.removeprefix(TELEGRAM_SELECT_TIME_CALLBACK_PREFIX))
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=KYIV_TZ)
    return value.astimezone(KYIV_TZ)


def _cancel_booking_id_from_callback(text: str | None) -> int | None:
    if not text or not text.startswith(TELEGRAM_CANCEL_BOOKING_CALLBACK_PREFIX):
        return None
    try:
        return int(text.removeprefix(TELEGRAM_CANCEL_BOOKING_CALLBACK_PREFIX))
    except ValueError:
        return None


def _share_contact_reply_markup() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": TELEGRAM_SHARE_CONTACT_BUTTON_TEXT, "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def _booking_action_reply_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": TELEGRAM_BOOKING_ACTION_BUTTONS[0]}, {"text": TELEGRAM_BOOKING_ACTION_BUTTONS[1]}],
            [{"text": TELEGRAM_BOOKING_ACTION_BUTTONS[2]}, {"text": TELEGRAM_BOOKING_ACTION_BUTTONS[3]}],
        ],
        "resize_keyboard": True,
    }


def _master_selected_reply_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": TELEGRAM_MASTER_SELECTED_ACTION_BUTTONS[0]}, {"text": TELEGRAM_MASTER_SELECTED_ACTION_BUTTONS[1]}],
            [{"text": TELEGRAM_MASTER_SELECTED_ACTION_BUTTONS[2]}],
        ],
        "resize_keyboard": True,
    }


def _service_selected_reply_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": TELEGRAM_SERVICE_SELECTED_ACTION_BUTTONS[0]}],
            [{"text": TELEGRAM_SERVICE_SELECTED_ACTION_BUTTONS[1]}],
        ],
        "resize_keyboard": True,
    }


def _ready_to_book_reply_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": TELEGRAM_READY_TO_BOOK_ACTION_BUTTONS[0]}],
            [{"text": TELEGRAM_READY_TO_BOOK_ACTION_BUTTONS[1]}],
        ],
        "resize_keyboard": True,
    }


def _after_booking_reply_markup() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": TELEGRAM_AFTER_BOOKING_ACTION_BUTTONS[0]}],
            [{"text": TELEGRAM_AFTER_BOOKING_ACTION_BUTTONS[1]}],
        ],
        "resize_keyboard": True,
    }


def _master_line(master: Master) -> str:
    name = _master_display_name(master)
    position = getattr(master, "position_uk", None) or ""
    phone = master.phone or ""
    return f"{name} - {position}\n\n{phone}".rstrip()


def _master_display_name(master: Master) -> str:
    return (
        getattr(master, "full_name_uk", None)
        or getattr(master, "full_name", None)
        or "Майстер"
    )


def _master_photo_url(master: Master) -> str | None:
    raw_url = getattr(master, "photo_url", None) or getattr(master, "avatar_url", None)
    if not raw_url:
        return None
    upload = _master_photo_upload(master)
    base_url = settings.public_api_base_url or settings.public_site_url
    if upload is not None and base_url:
        return urljoin(f"{base_url.rstrip('/')}/", f"api/v1/public/telegram/master-photo/{master.id}.jpg")
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    if not base_url:
        return None
    return urljoin(f"{base_url.rstrip('/')}/", raw_url.lstrip("/"))


def _master_photo_upload(master: Master) -> Any | None:
    for upload_attr in ("photo_upload", "avatar_upload"):
        upload = getattr(master, upload_attr, None)
        if upload is not None and getattr(upload, "file_path", None):
            return upload
    return None


def _safe_upload_path(upload: Any) -> Path | None:
    file_path = getattr(upload, "file_path", None)
    if not file_path:
        return None

    upload_root = Path(settings.upload_dir)
    if not upload_root.is_absolute():
        upload_root = Path.cwd() / upload_root
    upload_root = upload_root.resolve()

    source_path = Path(str(file_path))
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    source_path = source_path.resolve()

    try:
        source_path.relative_to(upload_root)
    except ValueError:
        logger.warning("Master photo file path is outside upload directory", extra={"file_path": str(source_path)})
        return None
    if not source_path.is_file():
        return None
    return source_path


def _telegram_master_photo_cache_path(master_id: int, upload: Any, source_path: Path) -> Path:
    upload_id = getattr(upload, "id", None) or "file"
    stat = source_path.stat()
    digest = hashlib.sha256(f"{source_path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")).hexdigest()[:16]
    return Path(settings.upload_dir) / TELEGRAM_MASTER_PHOTO_CACHE_FOLDER / f"master-{master_id}-{upload_id}-{digest}.jpg"


def _ensure_telegram_master_photo(master_id: int, upload: Any, source_path: Path) -> Path:
    cache_path = _telegram_master_photo_cache_path(master_id, upload, source_path)
    if cache_path.is_file():
        return cache_path

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError("Pillow is required to prepare Telegram master photos") from exc

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with Image.open(source_path) as image:
            try:
                image.seek(0)
            except EOFError:
                pass
            image = ImageOps.exif_transpose(image)
            image.thumbnail(TELEGRAM_MASTER_PHOTO_MAX_SIZE)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.save(temp_path, "JPEG", quality=TELEGRAM_MASTER_PHOTO_JPEG_QUALITY, optimize=True)
    except UnidentifiedImageError as exc:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("Master photo file is not a supported image") from exc
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    temp_path.replace(cache_path)
    return cache_path


async def _local_telegram_master_photo(master: Master) -> Path | None:
    upload = _master_photo_upload(master)
    if upload is None:
        return None
    source_path = _safe_upload_path(upload)
    if source_path is None:
        return None
    try:
        return await asyncio.to_thread(_ensure_telegram_master_photo, master.id, upload, source_path)
    except Exception as exc:
        logger.warning(
            "Telegram master photo preparation failed",
            extra={"master_id": master.id, "error": str(exc)},
        )
        return None


def _master_reply_markup(master: Master) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{TELEGRAM_SELECT_BUTTON_TEXT} {_master_display_name(master)}",
                    "callback_data": f"select_master:{master.id}",
                }
            ]
        ]
    }


def _service_button_text(service_item: BarberService) -> str:
    return getattr(service_item, "title_uk", None) or service_item.name


def _service_price_text(service_item: BarberService) -> str:
    price = getattr(service_item, "price", None)
    if price is None:
        return ""
    if isinstance(price, Decimal):
        return str(int(price)) if price == price.to_integral_value() else format(price.normalize(), "f")
    return str(price).strip()


def _service_select_icon(service_item: BarberService) -> str:
    text = _service_button_text(service_item).casefold()
    if "бород" in text or "beard" in text:
        return "🧔"
    if "стриж" in text or "haircut" in text:
        return "🙂"
    return "💈"


def _service_select_button_text(service_item: BarberService, *, selected: bool = False) -> str:
    icon = "✅" if selected else _service_select_icon(service_item)
    label = f"{icon} {_service_button_text(service_item)}"
    price = _service_price_text(service_item)
    return f"{label} · {price} грн" if price else label


def _services_reply_markup(
    services: list[BarberService],
    selected_service_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    if not services:
        return None
    selected = set(selected_service_ids or [])
    return {
        "inline_keyboard": [
            [
                {
                    "text": _service_select_button_text(service_item, selected=service_item.id in selected),
                    "callback_data": f"{TELEGRAM_SELECT_SERVICE_CALLBACK_PREFIX}{service_item.id}",
                }
            ]
            for service_item in services
        ]
    }


def _visit_date_button_text(visit_date: date) -> str:
    return f"{visit_date:%d.%m} {TELEGRAM_DATE_BUTTON_WEEKDAYS[visit_date.weekday()]}"


def _visit_dates_reply_markup(visit_dates: list[date]) -> dict[str, Any] | None:
    if not visit_dates:
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": _visit_date_button_text(visit_date),
                    "callback_data": f"{TELEGRAM_SELECT_DATE_CALLBACK_PREFIX}{visit_date.isoformat()}",
                }
            ]
            for visit_date in visit_dates
        ]
    }


def _time_slot_button_text(slot_start: datetime) -> str:
    return slot_start.astimezone(KYIV_TZ).strftime("%H:%M")


def _time_slot_callback_data(slot_start: datetime) -> str:
    return f"{TELEGRAM_SELECT_TIME_CALLBACK_PREFIX}{slot_start.astimezone(KYIV_TZ).replace(tzinfo=None).isoformat()}"


def _time_slots_reply_markup(slots: list[Any]) -> dict[str, Any] | None:
    if not slots:
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": _time_slot_button_text(slot.start_at),
                    "callback_data": _time_slot_callback_data(slot.start_at),
                }
            ]
            for slot in slots
        ]
    }


def _visit_time_summary(visit_time: datetime) -> str:
    local_time = visit_time.astimezone(KYIV_TZ)
    return f"{local_time:%d.%m %H:%M}, {TELEGRAM_DATE_BUTTON_WEEKDAYS[local_time.weekday()]}."


def _visit_time_detail(visit_time: datetime) -> str:
    local_time = visit_time.astimezone(KYIV_TZ)
    return (
        f"{TELEGRAM_DATE_DETAIL_WEEKDAYS[local_time.weekday()]} "
        f"{local_time.day} {TELEGRAM_DATE_DETAIL_MONTHS[local_time.month - 1]} - {local_time:%H:%M}"
    )


def _local_kyiv_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KYIV_TZ)
    return value.astimezone(KYIV_TZ)


def _booking_details_message(master: Master, services: list[BarberService], visit_time: datetime) -> str:
    master_name = getattr(master, "full_name_uk", None) or master.full_name
    master_position = getattr(master, "position_uk", None) or ""
    service_lines = [
        f"{_service_button_text(service_item)}. Майстер {master_name} ({service_item.price} грн)"
        for service_item in services
    ]
    return (
        f"Ви обрали час: {_visit_time_summary(visit_time)}\n\n\n"
        "Деталі запису\n\n"
        f"{_visit_time_detail(visit_time)}\n\n"
        f"{master_name} - {master_position}\n\n"
        f"{chr(10).join(service_lines)}\n\n\n"
        "Будь ласка, оберіть потрібну дію:"
    )


def _booking_time_range(start_at: datetime, end_at: datetime) -> str:
    local_start = _local_kyiv_datetime(start_at)
    local_end = _local_kyiv_datetime(end_at)
    return (
        f"{TELEGRAM_DATE_DETAIL_WEEKDAYS[local_start.weekday()]} "
        f"{local_start.day} {TELEGRAM_DATE_DETAIL_MONTHS[local_start.month - 1]} "
        f"{local_start:%H:%M} - {local_end:%H:%M}"
    )


def _booking_master_name(booking: Booking) -> str:
    master = getattr(booking, "redirected_from_master", None) or getattr(booking, "master", None)
    if master is None:
        return ""
    return getattr(master, "full_name_uk", None) or getattr(master, "full_name", "")


def _booking_services(booking: Booking) -> list[BarberService]:
    services = list(getattr(booking, "services", []) or [])
    if not services and getattr(booking, "service", None) is not None:
        services = [booking.service]
    return services


def _booking_service_summary(service_item: BarberService, master_name: str) -> str:
    return f"{_service_button_text(service_item)}. Майстер {master_name} ({service_item.price} грн)"


def _booking_view_message(booking: Booking) -> str:
    master_name = _booking_master_name(booking)
    services = _booking_services(booking)
    service_text = ", ".join(_booking_service_summary(service_item, master_name) for service_item in services)
    total_price = sum(getattr(service_item, "price", 0) or 0 for service_item in services)
    return (
        f"Ім‘я майстра: {master_name}\n"
        f"Послуги: {service_text}\n"
        f"Час зустрічі: {_booking_time_range(booking.start_at, booking.end_at)}\n"
        f"Коментар: {booking.customer_comment or ''}\n"
        f"Загальна вартість: {total_price} грн"
    )


def _booking_cancel_reply_markup(booking: Booking) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Скасувати", "callback_data": f"{TELEGRAM_CANCEL_BOOKING_CALLBACK_PREFIX}{booking.id}"}]
        ]
    }


def _booking_service_names(booking: Booking) -> str:
    return ", ".join(_service_button_text(service_item) for service_item in _booking_services(booking))


def _should_send_booking_notifications(booking: Booking) -> bool:
    start_at = _local_kyiv_datetime(booking.start_at)
    return start_at > datetime.now(KYIV_TZ)


def _schedule_booking_notifications(
    background_tasks: BackgroundTasks | None,
    booking: Booking,
) -> None:
    if background_tasks is None or not _should_send_booking_notifications(booking):
        return

    master = getattr(booking, "redirected_from_master", None) or booking.master
    master_name = (getattr(master, "full_name_uk", None) or getattr(master, "full_name", "")) if master is not None else ""
    service_name = _booking_service_names(booking)
    background_tasks.add_task(
        email_notification_service.send_new_booking_to_master,
        NewBookingEmail(
            booking_id=booking.id,
            master_name=master_name,
            master_email=getattr(master, "email", None) if master is not None else None,
            service_name=service_name,
            customer_name=booking.customer_name,
            customer_phone=booking.customer_phone,
            customer_comment=booking.customer_comment,
            start_at=booking.start_at,
            end_at=booking.end_at,
        ),
    )
    background_tasks.add_task(
        master_telegram_notification_service.send_new_booking_to_master,
        NewBookingTelegram(
            booking_id=booking.id,
            master_id=getattr(master, "id", None) if master is not None else None,
            master_name=master_name,
            telegram_chat_id=getattr(master, "telegram_chat_id", None) if master is not None else None,
            service_name=service_name,
            customer_name=booking.customer_name,
            customer_phone=booking.customer_phone,
            customer_comment=booking.customer_comment,
            start_at=booking.start_at,
            end_at=booking.end_at,
        ),
    )
    background_tasks.add_task(
        customer_activity_notification_service.send_booking_confirmation,
        booking.id,
    )


async def _send_master_list(telegram: TelegramMessageProvider, session: AsyncSession, chat_id: str) -> None:
    masters = (
        await session.execute(
            select(Master)
            .options(selectinload(Master.photo_upload), selectinload(Master.avatar_upload))
            .where(Master.is_active.is_(True))
            .order_by(Master.full_name.asc())
        )
    ).scalars().all()
    masters = list(masters)
    if not masters:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Наразі немає доступних майстрів.",
        )
        return

    for master in masters:
        body = _master_line(master)
        reply_markup = _master_reply_markup(master)
        photo_path = await _local_telegram_master_photo(master)
        photo_url = _master_photo_url(master)
        sent_photo = False
        if photo_path is not None:
            sent_photo = await _safe_send_telegram_photo(
                telegram,
                destination=chat_id,
                photo_path=photo_path,
                caption=body,
                reply_markup=reply_markup,
            )
        if not sent_photo and photo_url:
            sent_photo = await _safe_send_telegram_photo(
                telegram,
                destination=chat_id,
                photo_url=photo_url,
                caption=body,
                reply_markup=reply_markup,
            )
        if not sent_photo:
            await _safe_send_telegram_message(
                telegram,
                destination=chat_id,
                body=body,
                reply_markup=reply_markup,
            )


async def _get_telegram_bot_session(session: AsyncSession, chat_id: str) -> TelegramBotSession | None:
    return (
        await session.execute(select(TelegramBotSession).where(TelegramBotSession.chat_id == chat_id))
    ).scalar_one_or_none()


async def _upsert_telegram_bot_session(
    session: AsyncSession,
    *,
    chat_id: str,
    telegram_contact: TelegramContact | None = None,
    selected_master_id: int | None = None,
    state: str = "idle",
) -> TelegramBotSession:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is None:
        bot_session = TelegramBotSession(chat_id=chat_id)
        session.add(bot_session)

    telegram_contact_id = getattr(telegram_contact, "id", None)
    linked_customer_id = getattr(telegram_contact, "linked_customer_id", None)
    if telegram_contact_id is not None:
        bot_session.telegram_contact_id = telegram_contact_id
    if linked_customer_id is not None:
        bot_session.linked_customer_id = linked_customer_id
    if selected_master_id is not None:
        bot_session.selected_master_id = selected_master_id
    bot_session.state = state
    bot_session.last_seen_at = datetime.now(UTC)
    await session.flush()
    return bot_session


def _reset_telegram_booking_session(bot_session: TelegramBotSession, *, state: str) -> None:
    bot_session.selected_master_id = None
    bot_session.selected_service_id = None
    bot_session.payload_json = {}
    bot_session.state = state
    bot_session.last_seen_at = datetime.now(UTC)


async def _handle_new_booking_action(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    telegram_contact: TelegramContact | None,
    callback_query_id: str | None,
) -> bool:
    await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
    contact = await _telegram_contact_for_chat(session, chat_id=chat_id, telegram_contact=telegram_contact)
    if contact is None or not getattr(contact, "phone", None):
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body=TELEGRAM_START_WELCOME_MESSAGE,
            reply_markup=_share_contact_reply_markup(),
        )
        return False

    bot_session = await _upsert_telegram_bot_session(
        session,
        chat_id=chat_id,
        telegram_contact=contact,
        state="booking_started",
    )
    _reset_telegram_booking_session(bot_session, state="booking_started")
    await session.commit()
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body="Будь ласка, оберіть потрібну дію:",
        reply_markup=_booking_action_reply_markup(),
    )
    return True


async def _handle_cancel_draft_action(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is not None:
        _reset_telegram_booking_session(bot_session, state="draft_cancelled")
        await session.commit()
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body=TELEGRAM_DRAFT_CANCELLED_MESSAGE,
        reply_markup=_after_booking_reply_markup(),
    )
    return True


async def _handle_master_selection(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    master_id: int,
    telegram_contact: TelegramContact | None,
    callback_query_id: str | None,
) -> bool:
    await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
    master = (
        await session.execute(
            select(Master).where(
                Master.id == master_id,
                Master.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if master is None:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Обраного майстра не знайдено. Будь ласка, оберіть майстра ще раз.",
        )
        return False

    await _upsert_telegram_bot_session(
        session,
        chat_id=chat_id,
        telegram_contact=telegram_contact,
        selected_master_id=master.id,
        state=TELEGRAM_MASTER_SELECTED_STATE,
    )
    await session.commit()
    master_name = getattr(master, "full_name_uk", None) or master.full_name
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body=f"Ви обрали майстра: {master_name}.\n\nБудь ласка, оберіть потрібну дію:",
        reply_markup=_master_selected_reply_markup(),
    )
    return True


async def _handle_services_action(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is None or not bot_session.selected_master_id:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть майстра.",
            reply_markup=_booking_action_reply_markup(),
        )
        return False

    services = (
        await session.execute(
            select(BarberService)
            .where(
                BarberService.master_id == bot_session.selected_master_id,
                BarberService.is_active.is_(True),
            )
            .order_by(BarberService.name.asc())
        )
    ).scalars().all()
    bot_session.state = TELEGRAM_SELECTING_SERVICES_STATE
    selected_service_ids = bot_session.payload_json.get("selected_service_ids", []) if bot_session.payload_json else []
    bot_session.payload_json = {
        **(bot_session.payload_json or {}),
        "selected_service_ids": selected_service_ids,
    }
    bot_session.last_seen_at = datetime.now(UTC)
    await session.commit()
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body="Оберіть одну або більше послуг:",
        reply_markup=_services_reply_markup(list(services), selected_service_ids),
    )
    return True


async def _handle_service_selection(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    service_id: int,
    callback_query_id: str | None,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is None or not bot_session.selected_master_id:
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Спочатку оберіть майстра")
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть майстра.",
            reply_markup=_booking_action_reply_markup(),
        )
        return False

    service_item = (
        await session.execute(
            select(BarberService).where(
                BarberService.id == service_id,
                BarberService.master_id == bot_session.selected_master_id,
                BarberService.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if service_item is None:
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Послугу не знайдено")
        return False

    selected_service_ids = list((bot_session.payload_json or {}).get("selected_service_ids", []))
    if service_id in selected_service_ids:
        selected_service_ids = [item for item in selected_service_ids if item != service_id]
        callback_text = "Послугу прибрано"
    else:
        selected_service_ids.append(service_id)
        callback_text = "Послугу додано"

    bot_session.selected_service_id = selected_service_ids[0] if selected_service_ids else None
    bot_session.payload_json = {
        **(bot_session.payload_json or {}),
        "selected_service_ids": selected_service_ids,
    }
    bot_session.state = TELEGRAM_SELECTING_SERVICES_STATE
    bot_session.last_seen_at = datetime.now(UTC)
    await session.commit()
    await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text=callback_text)
    if service_id in selected_service_ids:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Будь ласка, оберіть потрібну дію:",
            reply_markup=_service_selected_reply_markup(),
        )
    return True


async def _available_visit_dates(
    session: AsyncSession,
    *,
    master_id: int,
    service_ids: list[int],
) -> list[date]:
    visit_dates: list[date] = []
    current_date = datetime.now(KYIV_TZ).date()
    horizon_end = booking_service_layer.availability_horizon_end_date()
    while current_date <= horizon_end and len(visit_dates) < TELEGRAM_MAX_VISIT_DATE_BUTTONS:
        slots = await _available_visit_slots(session, master_id=master_id, service_ids=service_ids, visit_date=current_date)
        if slots:
            visit_dates.append(current_date)
        current_date += timedelta(days=1)
    return visit_dates


async def _available_visit_slots(
    session: AsyncSession,
    *,
    master_id: int,
    service_ids: list[int],
    visit_date: date,
) -> list[Any]:
    return await booking_service_layer.get_available_slots(
        session,
        master_id=master_id,
        service_id=None,
        service_ids=service_ids,
        target_date=visit_date,
    )


async def _handle_date_time_action(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is None or not bot_session.selected_master_id:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть майстра.",
            reply_markup=_booking_action_reply_markup(),
        )
        return False

    selected_service_ids = list((bot_session.payload_json or {}).get("selected_service_ids", []))
    if not selected_service_ids:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть послугу.",
            reply_markup=_master_selected_reply_markup(),
        )
        return False

    visit_dates = await _available_visit_dates(
        session,
        master_id=bot_session.selected_master_id,
        service_ids=selected_service_ids,
    )
    bot_session.state = TELEGRAM_SELECTING_DATE_STATE
    bot_session.last_seen_at = datetime.now(UTC)
    await session.commit()
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body="Оберіть дату візиту" if visit_dates else "Наразі немає доступних дат.",
        reply_markup=_visit_dates_reply_markup(visit_dates),
    )
    return True


async def _handle_date_selection(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    visit_date: date,
    callback_query_id: str | None,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is None or not bot_session.selected_master_id:
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Спочатку оберіть майстра")
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть майстра.",
            reply_markup=_booking_action_reply_markup(),
        )
        return False

    selected_service_ids = list((bot_session.payload_json or {}).get("selected_service_ids", []))
    if not selected_service_ids:
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Спочатку оберіть послугу")
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть послугу.",
            reply_markup=_master_selected_reply_markup(),
        )
        return False

    slots = await _available_visit_slots(
        session,
        master_id=bot_session.selected_master_id,
        service_ids=selected_service_ids,
        visit_date=visit_date,
    )
    bot_session.state = TELEGRAM_SELECTING_TIME_STATE
    bot_session.payload_json = {
        **(bot_session.payload_json or {}),
        "selected_visit_date": visit_date.isoformat(),
    }
    bot_session.last_seen_at = datetime.now(UTC)
    await session.commit()
    await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body="Оберіть час візиту" if slots else "Наразі немає доступного часу на цю дату.",
        reply_markup=_time_slots_reply_markup(list(slots)),
    )
    return True


async def _handle_time_selection(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    visit_time: datetime,
    callback_query_id: str | None,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is None or not bot_session.selected_master_id:
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Спочатку оберіть майстра")
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть майстра.",
            reply_markup=_booking_action_reply_markup(),
        )
        return False

    selected_service_ids = list((bot_session.payload_json or {}).get("selected_service_ids", []))
    if not selected_service_ids:
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Спочатку оберіть послугу")
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть послугу.",
            reply_markup=_master_selected_reply_markup(),
        )
        return False

    master = (
        await session.execute(
            select(Master).where(
                Master.id == bot_session.selected_master_id,
                Master.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if master is None:
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Майстра не знайдено")
        return False

    services = (
        await session.execute(
            select(BarberService).where(
                BarberService.master_id == bot_session.selected_master_id,
                BarberService.id.in_(selected_service_ids),
                BarberService.is_active.is_(True),
            )
        )
    ).scalars().all()
    service_by_id = {service_item.id: service_item for service_item in services}
    selected_services = [service_by_id[service_id] for service_id in selected_service_ids if service_id in service_by_id]
    if len(selected_services) != len(selected_service_ids):
        await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id, text="Послугу не знайдено")
        return False

    bot_session.state = TELEGRAM_READY_TO_BOOK_STATE
    bot_session.payload_json = {
        **(bot_session.payload_json or {}),
        "selected_visit_time": visit_time.astimezone(KYIV_TZ).isoformat(),
    }
    bot_session.last_seen_at = datetime.now(UTC)
    await session.commit()
    await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body=_booking_details_message(master, selected_services, visit_time),
        reply_markup=_ready_to_book_reply_markup(),
    )
    return True


async def _telegram_contact_for_chat(
    session: AsyncSession,
    *,
    chat_id: str,
    telegram_contact: TelegramContact | None,
) -> TelegramContact | None:
    if telegram_contact is not None:
        return telegram_contact
    return (
        await session.execute(select(TelegramContact).where(TelegramContact.chat_id == chat_id))
    ).scalar_one_or_none()


async def _telegram_booking_customer_details(
    session: AsyncSession,
    telegram_contact: TelegramContact | None,
) -> tuple[str, str, str | None] | None:
    customer = None
    if telegram_contact is not None and telegram_contact.linked_customer_id is not None:
        customer = await session.get(Customer, telegram_contact.linked_customer_id)

    customer_name = ""
    customer_phone = None
    customer_email = None
    if customer is not None:
        customer_name = " ".join(part for part in [customer.name, customer.surname] if part).strip()
        customer_phone = customer.phone
        customer_email = customer.email

    if not customer_name and telegram_contact is not None:
        customer_name = " ".join(
            part for part in [telegram_contact.first_name, telegram_contact.last_name] if part
        ).strip()
    if not customer_name and telegram_contact is not None:
        customer_name = telegram_contact.username or ""
    if not customer_name:
        customer_name = "Telegram клієнт"

    if customer_phone is None and telegram_contact is not None:
        customer_phone = telegram_contact.phone

    if not customer_phone:
        return None
    return customer_name, customer_phone, customer_email


async def _telegram_booking_lookup_customer_id(
    session: AsyncSession,
    *,
    bot_session: TelegramBotSession | None,
    telegram_contact: TelegramContact | None,
) -> int | None:
    if telegram_contact is not None and telegram_contact.linked_customer_id is not None:
        return telegram_contact.linked_customer_id
    if bot_session is not None and bot_session.linked_customer_id is not None:
        return bot_session.linked_customer_id
    return await _customer_id_by_phone(session, getattr(telegram_contact, "phone", None))


async def _telegram_customer_bookings(
    session: AsyncSession,
    *,
    customer_id: int | None,
    booking_id: int | None,
) -> list[Booking]:
    now = datetime.now(KYIV_TZ)
    booking_service_items = selectinload(Booking.service_items).selectinload(BookingServiceItem.service)
    stmt = (
        select(Booking)
        .options(
            selectinload(Booking.master),
            selectinload(Booking.redirected_from_master),
            selectinload(Booking.service).selectinload(BarberService.base_service),
            booking_service_items,
            booking_service_items.selectinload(BarberService.base_service),
        )
        .where(
            Booking.status == BookingStatus.confirmed,
            Booking.end_at >= now,
        )
        .order_by(Booking.start_at.asc())
    )
    if customer_id is not None:
        stmt = stmt.where(Booking.customer_id == customer_id)
    elif booking_id is not None:
        stmt = stmt.where(Booking.id == booking_id)
    else:
        return []
    return list((await session.execute(stmt)).scalars().all())


async def _handle_view_bookings_action(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    telegram_contact: TelegramContact | None,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    contact = await _telegram_contact_for_chat(session, chat_id=chat_id, telegram_contact=telegram_contact)
    customer_id = await _telegram_booking_lookup_customer_id(
        session,
        bot_session=bot_session,
        telegram_contact=contact,
    )
    payload_json = bot_session.payload_json if bot_session is not None and bot_session.payload_json else {}
    booking_id = payload_json.get("booking_id")
    bookings = await _telegram_customer_bookings(
        session,
        customer_id=customer_id,
        booking_id=booking_id if isinstance(booking_id, int) else None,
    )
    if not bookings:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="У вас немає активних записів.",
            reply_markup=_after_booking_reply_markup(),
        )
        return False

    if bot_session is not None:
        bot_session.state = "viewing_bookings"
        bot_session.last_seen_at = datetime.now(UTC)
        await session.commit()

    for booking in bookings:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body=_booking_view_message(booking),
            reply_markup=_booking_cancel_reply_markup(booking),
        )
    return True


async def _telegram_cancellable_booking(
    session: AsyncSession,
    *,
    booking_id: int,
    customer_id: int | None,
    session_booking_id: int | None,
) -> Booking | None:
    if customer_id is None and session_booking_id != booking_id:
        return None

    stmt = select(Booking).where(
        Booking.id == booking_id,
        Booking.status == BookingStatus.confirmed,
        Booking.end_at >= datetime.now(KYIV_TZ),
    ).options(
        selectinload(Booking.master),
        selectinload(Booking.redirected_from_master),
        selectinload(Booking.service),
        selectinload(Booking.service_items).selectinload(BookingServiceItem.service),
    )
    if customer_id is not None:
        stmt = stmt.where(Booking.customer_id == customer_id)
    return (await session.execute(stmt)).scalar_one_or_none()


def _friendly_booking_error_message(exc: HTTPException) -> str:
    detail = str(exc.detail)
    if "overlaps an existing booking" in detail or "blocked interval" in detail:
        return "Цей час вже недоступний. Будь ласка, оберіть інший час."
    if "outside master's open availability" in detail:
        return "Обраний час вже недоступний для цього майстра. Будь ласка, оберіть інший час."
    if "Slot is in the past" in detail:
        return "Цей час вже минув. Будь ласка, оберіть іншу дату та час."
    if "Master does not provide this service" in detail or "Service not found" in detail:
        return "Послуга недоступна для цього майстра. Будь ласка, оберіть послуги ще раз."
    if "Master not found" in detail:
        return "Майстра не знайдено. Будь ласка, оберіть майстра ще раз."
    return "Не вдалося створити запис. Будь ласка, спробуйте ще раз."


async def _handle_cancel_booking_callback(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    booking_id: int,
    telegram_contact: TelegramContact | None,
    callback_query_id: str | None,
    background_tasks: BackgroundTasks | None,
) -> bool:
    await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
    bot_session = await _get_telegram_bot_session(session, chat_id)
    contact = await _telegram_contact_for_chat(session, chat_id=chat_id, telegram_contact=telegram_contact)
    customer_id = await _telegram_booking_lookup_customer_id(
        session,
        bot_session=bot_session,
        telegram_contact=contact,
    )
    payload_json = bot_session.payload_json if bot_session is not None and bot_session.payload_json else {}
    session_booking_id = payload_json.get("booking_id")
    booking = await _telegram_cancellable_booking(
        session,
        booking_id=booking_id,
        customer_id=customer_id,
        session_booking_id=session_booking_id if isinstance(session_booking_id, int) else None,
    )
    if booking is None:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Запис не знайдено або вже скасовано.",
        )
        return False

    booking.status = BookingStatus.cancelled
    booking.cancelled_at = datetime.now(KYIV_TZ)
    booking.completed_at = None
    if bot_session is not None:
        bot_session.state = "booking_cancelled"
        bot_session.last_seen_at = datetime.now(UTC)
    await session.commit()
    if background_tasks is not None:
        background_tasks.add_task(
            master_telegram_notification_service.send_cancelled_booking_to_master,
            cancelled_booking_telegram(booking),
        )
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body=TELEGRAM_BOOKING_CANCELLED_MESSAGE,
    )
    return True


async def _handle_booking_confirmation(
    telegram: TelegramMessageProvider,
    session: AsyncSession,
    *,
    chat_id: str,
    telegram_contact: TelegramContact | None,
    background_tasks: BackgroundTasks | None,
) -> bool:
    bot_session = await _get_telegram_bot_session(session, chat_id)
    if bot_session is None or not bot_session.selected_master_id:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть майстра.",
            reply_markup=_booking_action_reply_markup(),
        )
        return False

    selected_service_ids = list((bot_session.payload_json or {}).get("selected_service_ids", []))
    selected_visit_time = (bot_session.payload_json or {}).get("selected_visit_time")
    if not selected_service_ids or not selected_visit_time:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Спочатку оберіть послугу, дату та час.",
            reply_markup=_master_selected_reply_markup(),
        )
        return False

    try:
        start_at = datetime.fromisoformat(selected_visit_time)
    except ValueError:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Не вдалося визначити час запису. Будь ласка, оберіть дату та час ще раз.",
            reply_markup=_service_selected_reply_markup(),
        )
        return False

    contact = await _telegram_contact_for_chat(session, chat_id=chat_id, telegram_contact=telegram_contact)
    customer_details = await _telegram_booking_customer_details(session, contact)
    if customer_details is None:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Не вдалося визначити телефон клієнта. Будь ласка, поділіться контактом ще раз.",
            reply_markup=_share_contact_reply_markup(),
        )
        return False
    customer_name, customer_phone, customer_email = customer_details

    try:
        created_booking = await booking_service_layer.create_public_booking(
            session,
            PublicBookingCreate(
                master_id=bot_session.selected_master_id,
                service_ids=selected_service_ids,
                customer_name=customer_name,
                customer_phone=customer_phone,
                customer_email=customer_email,
                start_at=start_at,
            ),
        )
    except HTTPException as exc:
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body=_friendly_booking_error_message(exc),
            reply_markup=_service_selected_reply_markup(),
        )
        return False

    booking = created_booking
    if background_tasks is not None:
        booking_service_items = selectinload(Booking.service_items).selectinload(BookingServiceItem.service)
        booking = (
            await session.execute(
                select(Booking)
                .options(
                    selectinload(Booking.master),
                    selectinload(Booking.service),
                    booking_service_items,
                )
                .where(Booking.id == created_booking.id)
            )
        ).scalar_one()
        _schedule_booking_notifications(background_tasks, booking)
    booking_customer_id = getattr(booking, "customer_id", None)
    if booking_customer_id is not None:
        bot_session.linked_customer_id = booking_customer_id
        if contact is not None:
            contact.linked_customer_id = booking_customer_id
    bot_session.state = TELEGRAM_BOOKED_STATE
    bot_session.payload_json = {
        **(bot_session.payload_json or {}),
        "booking_id": booking.id,
    }
    bot_session.last_seen_at = datetime.now(UTC)
    await session.commit()
    await _safe_send_telegram_message(
        telegram,
        destination=chat_id,
        body=f"Запис здійснено успішно! Номер замовлення: {booking.id}.\n\n\nБудь ласка, оберіть потрібну дію:",
        reply_markup=_after_booking_reply_markup(),
    )
    return True


def _telegram_chat_id(update: dict[str, Any]) -> str | None:
    message = _telegram_message(update)
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    return str(chat_id) if chat_id is not None else None


def _telegram_contact_phone(update: dict[str, Any]) -> str | None:
    message = _telegram_message(update)
    if not isinstance(message, dict):
        return None
    contact = message.get("contact")
    if not isinstance(contact, dict):
        return None
    phone_number = contact.get("phone_number")
    return str(phone_number).strip() if phone_number else None


def _phone_candidates(phone: str) -> list[str]:
    normalized = phone.strip()
    digits = "".join(char for char in normalized if char.isdigit())
    candidates = [normalized]
    if digits:
        candidates.extend([digits, f"+{digits}"])
        if len(digits) == 9:
            candidates.append(f"+380{digits}")
        if len(digits) == 10 and digits.startswith("0"):
            candidates.append(f"+38{digits}")
        if digits.startswith("00") and len(digits) > 2:
            candidates.append(f"+{digits[2:]}")
    return list(dict.fromkeys(item for item in candidates if item))


async def _link_customer_telegram_preference(
    session: AsyncSession,
    *,
    customer_id: int,
    chat_id: str,
) -> ClientCommunicationPreference:
    preference = await service.get_preference(session, customer_id)
    if preference is None:
        preference = ClientCommunicationPreference(customer_id=customer_id)
        session.add(preference)
    preference.telegram_chat_id = chat_id
    preference.transactional_consent = ConsentStatus.opted_in
    return preference


async def _customer_id_by_phone(session: AsyncSession, phone: str | None) -> int | None:
    if not phone:
        return None
    customer = (
        await session.execute(
            select(Customer).where(Customer.phone.in_(_phone_candidates(phone))).order_by(Customer.id.asc()).limit(1)
        )
    ).scalar_one_or_none()
    return customer.id if customer else None


async def _upsert_telegram_contact_from_update(
    session: AsyncSession,
    update: dict[str, Any],
) -> TelegramContact | None:
    chat_id = _telegram_chat_id(update)
    if not chat_id:
        return None
    contact = (
        await session.execute(select(TelegramContact).where(TelegramContact.chat_id == chat_id))
    ).scalar_one_or_none()
    if contact is None:
        contact = TelegramContact(chat_id=chat_id)
        session.add(contact)

    user = _telegram_from_user(update) or {}
    phone = _telegram_contact_phone(update)
    if user.get("id") is not None:
        contact.telegram_user_id = str(user["id"])
    contact.username = user.get("username") or contact.username
    contact.first_name = user.get("first_name") or contact.first_name
    contact.last_name = user.get("last_name") or contact.last_name
    contact.language_code = user.get("language_code") or contact.language_code
    contact.phone = phone or contact.phone
    contact.last_update_id = update.get("update_id") if isinstance(update.get("update_id"), int) else contact.last_update_id
    contact.last_seen_at = datetime.now(UTC)
    contact.raw_update = update

    customer_id = await _customer_id_by_phone(session, phone)
    if customer_id is not None:
        contact.linked_customer_id = customer_id
        await _link_customer_telegram_preference(session, customer_id=customer_id, chat_id=chat_id)
    return contact


def _booking_link() -> str:
    return f"{settings.public_site_url.rstrip('/')}/#booking"


def _booking_link_message() -> str:
    return f"Для нового запису відкрийте онлайн-форму: {_booking_link()}"


def _unsupported_bot_command_message() -> str:
    return (
        "Ця команда поки недоступна в Telegram. "
        f"Для запису скористайтесь онлайн-формою: {_booking_link()}"
    )


async def _safe_answer_callback_query(
    telegram: TelegramMessageProvider,
    *,
    callback_query_id: str | None,
    text: str | None = None,
) -> None:
    if not callback_query_id:
        return
    try:
        await telegram.answer_callback_query(callback_query_id=callback_query_id, text=text)
    except Exception as exc:
        logger.warning("Telegram callback answer failed", extra={"error": str(exc)})


async def _safe_send_telegram_message(
    telegram: TelegramMessageProvider,
    *,
    destination: str,
    body: str,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    try:
        await telegram.send_message(destination=destination, body=body, reply_markup=reply_markup)
    except Exception as exc:
        logger.warning("Telegram message send failed", extra={"destination": destination, "error": str(exc)})
        return False
    return True


async def _safe_send_telegram_photo(
    telegram: TelegramMessageProvider,
    *,
    destination: str,
    photo_url: str | None = None,
    photo_path: Path | None = None,
    caption: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    try:
        await telegram.send_photo(
            destination=destination,
            photo_url=photo_url,
            photo_path=photo_path,
            caption=caption,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.warning(
            "Telegram photo send failed",
            extra={
                "destination": destination,
                "photo_url": photo_url,
                "has_local_photo": photo_path is not None,
                "error": str(exc),
            },
        )
        return False
    return True


def campaign_recipient(campaign: Campaign) -> CampaignRecipient:
    raw = str((campaign.metadata_json or {}).get("recipient") or "").lower()
    if raw in {CampaignRecipient.master.value, "barber"}:
        return CampaignRecipient.master
    return CampaignRecipient.customer


def campaign_write_data(
    payload: CampaignCreate | CampaignUpdate,
    *,
    campaign: Campaign | None = None,
) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=isinstance(payload, CampaignUpdate), exclude={"audience", "recipient"})
    current_recipient = campaign_recipient(campaign) if campaign is not None else CampaignRecipient.customer
    metadata = dict(
        data.get("metadata_json")
        if isinstance(data.get("metadata_json"), dict)
        else (campaign.metadata_json if campaign is not None else {})
    )
    if "recipient" in payload.model_fields_set and payload.recipient is not None:
        recipient = payload.recipient
    else:
        legacy_recipient = str(metadata.get("recipient") or "").lower()
        recipient = CampaignRecipient.master if legacy_recipient in {"master", "barber"} else current_recipient
    metadata["recipient"] = recipient.value
    if recipient == CampaignRecipient.master:
        requested_channel = data.get("channel")
        effective_channel = requested_channel or (campaign.channel if campaign is not None else None)
        if effective_channel not in {MessageChannel.telegram, MessageChannel.email}:
            data["channel"] = MessageChannel.telegram
        metadata.pop("fallback_to_sms", None)
    data["metadata_json"] = metadata
    return data


def reject_master_sms_campaign(data: dict[str, Any]) -> None:
    recipient = str((data.get("metadata_json") or {}).get("recipient") or "").lower()
    if recipient in {CampaignRecipient.master.value, "barber"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Master campaigns cannot use SMS",
        )


def campaign_recipient_filter(recipient: CampaignRecipient):
    stored_recipient = func.lower(func.coalesce(Campaign.metadata_json["recipient"].as_string(), "customer"))
    if recipient == CampaignRecipient.master:
        return stored_recipient.in_(("master", "barber"))
    return ~stored_recipient.in_(("master", "barber"))


def campaign_response(
    campaign: Campaign,
    delivery_counts: tuple[int, int] = (0, 0),
) -> CampaignResponse:
    data = CampaignResponse.model_validate(campaign)
    data.recipient = campaign_recipient(campaign)
    data.audience = service.audience_from_campaign(campaign)
    if campaign.template is not None:
        data.template_name = campaign.template.name
    data.template_body = service.campaign_message_body(campaign)
    data.sent_count, data.failed_count = delivery_counts
    return data


async def campaign_delivery_counts(
    session: AsyncSession,
    campaign_ids: list[int],
) -> dict[int, tuple[int, int]]:
    if not campaign_ids:
        return {}
    counts: dict[int, list[int]] = {campaign_id: [0, 0] for campaign_id in campaign_ids}
    for model in (MessageRecipient, MasterMessageDelivery):
        rows = (
            await session.execute(
                select(model.campaign_id, model.status, func.count(model.id))
                .where(model.campaign_id.in_(campaign_ids))
                .group_by(model.campaign_id, model.status)
            )
        ).all()
        for campaign_id, delivery_status, total in rows:
            if delivery_status in {MessageDeliveryStatus.sent, MessageDeliveryStatus.delivered}:
                counts[campaign_id][0] += total
            elif delivery_status == MessageDeliveryStatus.failed:
                counts[campaign_id][1] += total
    reminder_rows = (
        await session.execute(
            select(
                MasterScheduleReminder.campaign_id,
                func.count(MasterScheduleReminder.initial_sent_at),
                func.count(MasterScheduleReminder.follow_up_sent_at),
                func.count(MasterScheduleReminder.last_error),
            )
            .where(MasterScheduleReminder.campaign_id.in_(campaign_ids))
            .group_by(MasterScheduleReminder.campaign_id)
        )
    ).all()
    for campaign_id, initial_sent, follow_up_sent, failed in reminder_rows:
        counts[campaign_id][0] += initial_sent + follow_up_sent
        counts[campaign_id][1] += failed
    return {campaign_id: (values[0], values[1]) for campaign_id, values in counts.items()}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rules_to_audience(rules: list[dict[str, Any]], limit: int | None = None) -> AudienceCriteria:
    criteria: dict[str, Any] = {
        "all_clients": False,
        "barber_ids": [],
        "service_ids": [],
    }
    for rule in rules:
        rule_type = rule.get("type")
        if rule_type == "all_clients":
            criteria["all_clients"] = True
        elif rule_type == "selected_barber" and rule.get("barber_id"):
            criteria["barber_ids"].append(int(rule["barber_id"]))
        elif rule_type == "selected_service" and rule.get("service_id"):
            criteria["service_ids"].append(int(rule["service_id"]))
        elif rule_type == "visited_date_range":
            criteria["visited_from"] = _parse_datetime(rule.get("date_from"))
            criteria["visited_to"] = _parse_datetime(rule.get("date_to"))
        elif rule_type == "inactive_clients" and rule.get("inactive_days"):
            criteria["inactive_days"] = int(rule["inactive_days"])
        elif rule_type == "first_time_clients":
            criteria["first_time_clients"] = True
        elif rule_type == "vip_clients":
            criteria["vip_clients"] = True
        elif rule_type == "birthday_this_month":
            criteria["birthday_month"] = datetime.now().month
    if limit is not None:
        criteria["limit"] = limit
    if not criteria["all_clients"] and not any(
        [
            criteria["barber_ids"],
            criteria["service_ids"],
            criteria.get("visited_from"),
            criteria.get("visited_to"),
            criteria.get("inactive_days"),
            criteria.get("first_time_clients"),
            criteria.get("vip_clients"),
            criteria.get("birthday_month"),
        ]
    ):
        criteria["all_clients"] = True
    return AudienceCriteria.model_validate(criteria)


def _temporary_campaign(audience: AudienceCriteria, purpose: MessagePurpose = MessagePurpose.marketing) -> Campaign:
    campaign = Campaign(
        name="Audience preview",
        type=CampaignType.manual,
        status=CampaignStatus.draft,
        channel=MessageChannel.telegram,
        purpose=purpose,
        timezone="Europe/Kyiv",
    )
    campaign.audience_filter = CampaignAudienceFilter(criteria=audience.model_dump(mode="json", exclude_none=True))
    return campaign


async def _preference_map(session: AsyncSession, customer_ids: list[int]) -> dict[int, ClientCommunicationPreference]:
    if not customer_ids:
        return {}
    rows = (
        await session.execute(
            select(ClientCommunicationPreference).where(ClientCommunicationPreference.customer_id.in_(customer_ids))
        )
    ).scalars().all()
    return {item.customer_id: item for item in rows}


def _customer_name(customer: Customer) -> str:
    return " ".join(part for part in [customer.name, customer.surname] if part).strip() or customer.phone


async def _audience_estimate(session: AsyncSession, customers: list[Customer]) -> dict[str, int]:
    preferences = await _preference_map(session, [customer.id for customer in customers])
    missing_chat_id = 0
    opted_out = 0
    eligible = 0
    for customer in customers:
        preference = preferences.get(customer.id)
        allowed, _ = service.communication_allowed(preference, MessagePurpose.marketing)
        if not preference or not preference.telegram_chat_id:
            missing_chat_id += 1
        if preference and (preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out):
            opted_out += 1
        if allowed and preference and preference.telegram_chat_id:
            eligible += 1
    return {
        "total": len(customers),
        "eligible": eligible,
        "missing_chat_id": missing_chat_id,
        "opted_out": opted_out,
        "excluded": len(customers) - eligible,
    }


@backoffice_router.post("/templates", response_model=MessageTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_message_template(
    payload: MessageTemplateCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.create_template(session, payload.model_dump())
    return MessageTemplateResponse.model_validate(template)


@backoffice_router.get("/templates", response_model=PaginatedResponse[MessageTemplateResponse])
async def list_message_templates(
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MessageTemplateResponse]:
    stmt = select(MessageTemplate).order_by(MessageTemplate.created_at.desc())
    items, total = await template_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MessageTemplateResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("/templates/{template_id}/duplicate", response_model=MessageTemplateResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_message_template(
    template_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.get_template(session, template_id)
    clone = await service.create_template(
        session,
        {
            "name": f"{template.name} copy {int(datetime.now().timestamp())}",
            "channel": template.channel,
            "language": template.language,
            "body": template.body,
            "is_active": template.is_active,
        },
    )
    return MessageTemplateResponse.model_validate(clone)


@backoffice_router.get("/templates/{template_id}", response_model=MessageTemplateResponse)
async def get_message_template(
    template_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.get_template(session, template_id)
    return MessageTemplateResponse.model_validate(template)


@backoffice_router.put("/templates/{template_id}", response_model=MessageTemplateResponse)
async def update_message_template(
    template_id: int,
    payload: MessageTemplateUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    template = await service.get_template(session, template_id)
    updated = await service.update_template(session, template, payload.model_dump(exclude_unset=True))
    return MessageTemplateResponse.model_validate(updated)


@backoffice_router.patch("/templates/{template_id}", response_model=MessageTemplateResponse)
async def patch_message_template(
    template_id: int,
    payload: MessageTemplateUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessageTemplateResponse:
    return await update_message_template(template_id, payload, _, session)


@backoffice_router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message_template(
    template_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    template = await service.get_template(session, template_id)
    await template_repo.delete(session, template)


@backoffice_router.get("/dashboard")
async def get_messaging_dashboard(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    analytics = await service.analytics(session)
    status_counts = dict(
        (
            await session.execute(
                select(Campaign.status, func.count(Campaign.id)).group_by(Campaign.status)
            )
        ).all()
    )
    recent_campaigns = (
        await session.execute(select(Campaign).order_by(Campaign.updated_at.desc()).limit(5))
    ).scalars().all()
    return {
        "active_campaigns": status_counts.get(CampaignStatus.active, 0),
        "scheduled_campaigns": 0,
        "messages_sent": analytics["sent_count"],
        "failed_messages": analytics["failed_count"],
        "delivery_rate": analytics["delivery_rate"],
        "review_requests_sent": analytics["review_request_sent_count"],
        "recent_activity": [
            {
                "id": campaign.id,
                "campaign_id": campaign.id,
                "title": campaign.name,
                "description": f"{campaign.channel.value} · {campaign.type.value}",
                "status": campaign.status.value,
                "created_at": campaign.updated_at,
            }
            for campaign in recent_campaigns
        ],
    }


@backoffice_router.get("/settings")
async def get_messaging_settings(
    _: object = Depends(get_current_admin_user),
) -> dict[str, Any]:
    return {
        "telegram_bot_status": "connected" if settings.telegram_bot_token else "offline",
        "default_review_links": {
            "google": settings.messaging_default_review_url or "",
            "instagram": "",
            "internal": "",
            "custom": "",
        },
        "default_template_ids": {campaign_type.value: None for campaign_type in CampaignType},
        "quiet_hours_from": "20:00",
        "quiet_hours_to": "10:00",
        "default_rate_limit": settings.messaging_batch_size,
        "default_timezone": "Europe/Kyiv",
        "opt_out_text": "Напишіть STOP, щоб відписатися.",
        "test_recipient_chat_id": None,
        "multi_location_enabled": False,
    }


@backoffice_router.patch("/settings")
@backoffice_router.put("/settings")
async def update_messaging_settings(
    payload: dict[str, Any],
    _: object = Depends(get_current_admin_user),
) -> dict[str, Any]:
    settings_payload = await get_messaging_settings(_)
    settings_payload.update(payload)
    return settings_payload


@backoffice_router.post("/audience/estimate")
async def estimate_messaging_audience(
    payload: AudienceRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    audience = _rules_to_audience(payload.rules)
    campaign = _temporary_campaign(audience)
    customers = list(await service.calculate_recipients(session, campaign))
    return await _audience_estimate(session, customers)


@backoffice_router.post("/audience/preview")
async def preview_messaging_recipients(
    payload: AudienceRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    audience = _rules_to_audience(payload.rules, payload.limit)
    campaign = _temporary_campaign(audience)
    customers = list(await service.calculate_recipients(session, campaign))
    preferences = await _preference_map(session, [customer.id for customer in customers])
    rows = []
    for customer in customers:
        preference = preferences.get(customer.id)
        allowed, reason = service.communication_allowed(preference, MessagePurpose.marketing)
        rows.append(
            {
                "id": customer.id,
                "name": _customer_name(customer),
                "phone": customer.phone,
                "telegram_chat_id": preference.telegram_chat_id if preference else None,
                "marketing_consent": service.has_marketing_consent(preference),
                "opt_out": bool(preference and (preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out)),
                "preferred_language": preference.preferred_language if preference else None,
                "eligible": bool(allowed and preference and preference.telegram_chat_id),
                "exclusion_reason": None if allowed else reason,
            }
        )
    return rows


@backoffice_router.post("/campaigns", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    data = campaign_write_data(payload)
    campaign = await service.create_campaign(session, data, payload.audience)
    return campaign_response(campaign)


@backoffice_router.get("/campaigns", response_model=PaginatedResponse[CampaignResponse])
async def list_campaigns(
    pagination: PaginationDep,
    status_filter: CampaignStatus | None = Query(default=None, alias="status"),
    type_filter: CampaignType | None = Query(default=None, alias="type"),
    channel_filter: MessageChannel | None = Query(default=None, alias="channel"),
    recipient_filter: CampaignRecipient | None = Query(default=None, alias="recipient"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    barber_id: int | None = Query(default=None, ge=1),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CampaignResponse]:
    stmt = (
        select(Campaign)
        .options(selectinload(Campaign.audience_filter), selectinload(Campaign.template))
        .order_by(Campaign.created_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(Campaign.status == status_filter)
    if type_filter is not None:
        stmt = stmt.where(Campaign.type == type_filter)
    if channel_filter is not None:
        stmt = stmt.where(Campaign.channel == channel_filter)
    if recipient_filter is not None:
        stmt = stmt.where(campaign_recipient_filter(recipient_filter))
    if date_from is not None:
        stmt = stmt.where(Campaign.scheduled_at >= datetime.combine(date_from, datetime.min.time(), tzinfo=KYIV_TZ))
    if date_to is not None:
        next_day = date_to + timedelta(days=1)
        stmt = stmt.where(Campaign.scheduled_at < datetime.combine(next_day, datetime.min.time(), tzinfo=KYIV_TZ))
    if barber_id is not None:
        stmt = stmt.join(CampaignAudienceFilter, CampaignAudienceFilter.campaign_id == Campaign.id).where(
            CampaignAudienceFilter.criteria.cast(JSONB).contains({"barber_ids": [barber_id]})
        )
    items, total = await campaign_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    counts = await campaign_delivery_counts(session, [item.id for item in items])
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[campaign_response(item, counts.get(item.id, (0, 0))) for item in items],
    )


@backoffice_router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    counts = await campaign_delivery_counts(session, [campaign.id])
    return campaign_response(campaign, counts.get(campaign.id, (0, 0)))


@backoffice_router.get("/sms-campaigns", response_model=PaginatedResponse[CampaignResponse])
async def list_sms_campaigns(
    pagination: PaginationDep,
    status_filter: CampaignStatus | None = Query(default=None, alias="status"),
    recipient_filter: CampaignRecipient | None = Query(default=None, alias="recipient"),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CampaignResponse]:
    stmt = (
        select(Campaign)
        .options(selectinload(Campaign.audience_filter), selectinload(Campaign.template))
        .where(Campaign.channel == MessageChannel.sms)
        .order_by(Campaign.created_at.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(Campaign.status == status_filter)
    if recipient_filter is not None:
        stmt = stmt.where(campaign_recipient_filter(recipient_filter))
    items, total = await campaign_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    counts = await campaign_delivery_counts(session, [item.id for item in items])
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[campaign_response(item, counts.get(item.id, (0, 0))) for item in items],
    )


@backoffice_router.post("/sms-campaigns", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_sms_campaign(
    payload: CampaignCreate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    data = campaign_write_data(payload)
    reject_master_sms_campaign(data)
    data["channel"] = MessageChannel.sms
    campaign = await service.create_campaign(session, data, payload.audience)
    return campaign_response(campaign)


@backoffice_router.get("/sms-campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_sms_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    if campaign.channel != MessageChannel.sms:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SMS campaign not found")
    return campaign_response(campaign)


@backoffice_router.put("/sms-campaigns/{campaign_id}", response_model=CampaignResponse)
async def update_sms_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    if campaign.channel != MessageChannel.sms:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SMS campaign not found")
    data = campaign_write_data(payload, campaign=campaign)
    reject_master_sms_campaign(data)
    data["channel"] = MessageChannel.sms
    updated = await service.update_campaign(session, campaign, data, payload.audience)
    return campaign_response(updated)


@backoffice_router.patch("/sms-campaigns/{campaign_id}", response_model=CampaignResponse)
async def patch_sms_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    return await update_sms_campaign(campaign_id, payload, _, session)


@backoffice_router.put("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    data = campaign_write_data(payload, campaign=campaign)
    updated = await service.update_campaign(session, campaign, data, payload.audience)
    return campaign_response(updated)


@backoffice_router.patch("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def patch_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    return await update_campaign(campaign_id, payload, _, session)


@backoffice_router.post("/campaigns/{campaign_id}/duplicate", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    clone = await service.create_campaign(
        session,
        {
            "name": f"{campaign.name} copy",
            "type": campaign.type,
            "status": CampaignStatus.draft,
            "channel": campaign.channel,
            "purpose": campaign.purpose,
            "template_id": campaign.template_id,
            "scheduled_at": campaign.scheduled_at,
            "timezone": campaign.timezone,
            "review_delay_minutes": campaign.review_delay_minutes,
            "follow_up_delay_days": campaign.follow_up_delay_days,
            "review_platform": campaign.review_platform,
            "review_url": campaign.review_url,
            "discount_code": campaign.discount_code,
            "location_key": campaign.location_key,
            "metadata_json": campaign.metadata_json,
        },
        service.audience_from_campaign(campaign),
    )
    return campaign_response(clone)


@backoffice_router.patch("/campaigns/{campaign_id}/status", response_model=CampaignResponse)
async def update_campaign_status(
    campaign_id: int,
    payload: CampaignStatusUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    campaign.status = payload.status
    await session.commit()
    return campaign_response(await service.get_campaign(session, campaign_id))


@backoffice_router.post("/campaigns/{campaign_id}/send-test")
async def send_campaign_test_message(
    campaign_id: int,
    payload: dict[str, str | None],
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    campaign = await service.get_campaign(session, campaign_id)
    chat_id = payload.get("recipient") or payload.get("chat_id")
    if not chat_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="recipient is required")
    body = service.campaign_message_body(campaign)
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Campaign has no message body")
    provider = service.providers.get(campaign.channel)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"No provider configured for channel {campaign.channel.value}")
    result = await provider.send_message(destination=chat_id, body=body)
    return {"sent": True, "provider_message_id": result.provider_message_id, "provider_response": result.raw_response}


@backoffice_router.delete("/campaigns/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    campaign = await service.get_campaign(session, campaign_id)
    await campaign_repo.delete(session, campaign)


@backoffice_router.post("/campaigns/{campaign_id}/enable", response_model=CampaignResponse)
async def enable_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    campaign.status = CampaignStatus.active
    await session.commit()
    return campaign_response(await service.get_campaign(session, campaign_id))


@backoffice_router.post("/campaigns/{campaign_id}/disable", response_model=CampaignResponse)
async def disable_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    campaign = await service.get_campaign(session, campaign_id)
    campaign.status = CampaignStatus.paused
    await session.commit()
    return campaign_response(await service.get_campaign(session, campaign_id))


@backoffice_router.post("/campaigns/{campaign_id}/pause", response_model=CampaignResponse)
async def pause_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    return await disable_campaign(campaign_id, _, session)


@backoffice_router.post("/campaigns/{campaign_id}/resume", response_model=CampaignResponse)
async def resume_campaign(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> CampaignResponse:
    return await enable_campaign(campaign_id, _, session)


@backoffice_router.post("/campaigns/{campaign_id}/start")
async def start_manual_campaign(
    campaign_id: int,
    payload: StartCampaignRequest,
    background_tasks: BackgroundTasks,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int | str]:
    campaign = await service.get_campaign(session, campaign_id)
    if campaign.status not in {CampaignStatus.active, CampaignStatus.draft}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only draft or active campaigns can be started")
    campaign.status = CampaignStatus.active
    enqueued = await service.enqueue_campaign_recipients(session, campaign, payload.scheduled_at)
    background_tasks.add_task(_process_pending_messages_background)
    return {"campaign_id": campaign_id, "enqueued": enqueued, "status": campaign.status.value}


async def _process_pending_messages_background() -> None:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as background_session:
        await service.process_pending_messages(background_session)


@backoffice_router.get("/campaigns/{campaign_id}/recipients", response_model=PaginatedResponse[MessageRecipientResponse])
async def get_campaign_recipients(
    campaign_id: int,
    pagination: PaginationDep,
    calculate: bool = Query(default=False),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MessageRecipientResponse]:
    campaign = await service.get_campaign(session, campaign_id)
    if calculate:
        customers = await service.calculate_recipients(session, campaign)
        body = service.campaign_message_body(campaign)
        items = [
            MessageRecipientResponse(
                id=customer.id,
                campaign_id=campaign_id,
                customer_id=customer.id,
                appointment_id=None,
                waitlist_request_id=None,
                waitlist_offer_id=None,
                channel=campaign.channel,
                status="pending",
                idempotency_key=service.build_idempotency_key(campaign_id, customer.id),
                scheduled_at=campaign.scheduled_at,
                sent_at=None,
                delivered_at=None,
                delivery_status_checked_at=None,
                rendered_message=(
                    (await service.render_for_customer(session, body, customer, campaign))[0]
                    if body
                    else None
                ),
                attempts=0,
                next_retry_at=None,
                last_error=None,
                provider_message_id=None,
                created_at=campaign.created_at,
                updated_at=campaign.updated_at,
            )
            for customer in customers[(pagination.page - 1) * pagination.page_size : pagination.page * pagination.page_size]
        ]
        return PaginatedResponse(total=len(customers), page=pagination.page, page_size=pagination.page_size, items=items)

    stmt = (
        select(MessageRecipient)
        .where(MessageRecipient.campaign_id == campaign_id)
        .order_by(MessageRecipient.created_at.desc())
    )
    recipients, total = await recipient_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MessageRecipientResponse.model_validate(item) for item in recipients],
    )


@backoffice_router.get("/campaigns/{campaign_id}/logs", response_model=PaginatedResponse[MessageLogResponse])
async def get_campaign_logs(
    campaign_id: int,
    pagination: PaginationDep,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MessageLogResponse]:
    await service.get_campaign(session, campaign_id)
    stmt = select(MessageLog).where(MessageLog.campaign_id == campaign_id).order_by(MessageLog.created_at.desc())
    logs, total = await log_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MessageLogResponse.model_validate(item) for item in logs],
    )


@backoffice_router.post("/campaigns/{campaign_id}/retry-failed")
async def retry_failed_campaign_messages(
    campaign_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    await service.get_campaign(session, campaign_id)
    return {"queued": await service.retry_failed(session, campaign_id)}


@backoffice_router.get("/analytics", response_model=MessagingAnalyticsResponse)
async def get_messaging_analytics(
    campaign_id: int | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MessagingAnalyticsResponse:
    return MessagingAnalyticsResponse.model_validate(await service.analytics(session, campaign_id))


@backoffice_router.post("/preview", response_model=RenderPreviewResponse)
async def preview_message(
    payload: RenderPreviewRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> RenderPreviewResponse:
    customer = await session.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    campaign = await service.get_campaign(session, payload.campaign_id) if payload.campaign_id else None
    appointment = None
    if payload.appointment_id is not None:
        appointment = (
            await session.execute(
                select(Booking)
                .options(selectinload(Booking.master), selectinload(Booking.service))
                .where(Booking.id == payload.appointment_id)
            )
        ).scalar_one_or_none()
        if appointment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    if payload.body is not None:
        body = payload.body
    elif payload.template_id is not None:
        body = (await service.get_template(session, payload.template_id)).body
    elif campaign:
        body = service.campaign_message_body(campaign)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No message body available")
    if body is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No message body available")
    rendered, variables = await service.render_for_customer(
        session,
        body,
        customer,
        campaign,
        appointment,
        payload.extra_variables,
    )
    return RenderPreviewResponse(rendered_message=rendered, variables=variables)


@backoffice_router.post("/test-message")
async def send_test_message(
    payload: TestMessageRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    body = payload.body
    campaign = await service.get_campaign(session, payload.campaign_id) if payload.campaign_id else None
    channel = campaign.channel if campaign is not None else MessageChannel.telegram
    if body is None and payload.template_id is not None:
        template = await service.get_template(session, payload.template_id)
        body = template.body
        channel = template.channel
    if body is None and campaign is not None:
        body = service.campaign_message_body(campaign)
    if body is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="body, template_id or campaign_id is required")
    customer = await session.get(Customer, payload.customer_id) if payload.customer_id else None
    if customer is not None:
        body, _ = await service.render_for_customer(session, body, customer, campaign)
    else:
        service.validate_template_body(body)
    provider = service.providers.get(channel)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"No provider configured for channel {channel.value}")
    result = await provider.send_message(destination=payload.chat_id, body=body)
    return {"sent": True, "provider_message_id": result.provider_message_id, "provider_response": result.raw_response}


@backoffice_router.get("/customers/{customer_id}/preferences")
async def get_customer_communication_preferences(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    preference = await service.get_preference(session, customer_id)
    logs = (
        await session.execute(
            select(MessageLog).where(MessageLog.customer_id == customer_id).order_by(MessageLog.created_at.desc()).limit(20)
        )
    ).scalars().all()
    review_requests = (
        await session.execute(
            select(ReviewRequest)
            .where(ReviewRequest.customer_id == customer_id)
            .order_by(ReviewRequest.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "telegram_chat_id": preference.telegram_chat_id if preference else None,
        "telegram_status": "connected" if preference and preference.telegram_chat_id else "missing",
        "marketing_consent": service.has_marketing_consent(preference),
        "opt_out": bool(preference and (preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out)),
        "preferred_language": (preference.preferred_language if preference else None) or "uk",
        "message_history": [
            {
                "id": log.id,
                "client_id": customer_id,
                "client_name": _customer_name(customer),
                "phone": customer.phone,
                "telegram_status": log.status.value,
                "sent_at": log.created_at,
                "failure_reason": log.error_reason,
            }
            for log in logs
        ],
        "review_requests": [
            {
                "id": item.id,
                "client_id": customer_id,
                "client_name": _customer_name(customer),
                "phone": customer.phone,
                "telegram_status": "sent" if item.sent_at else "queued",
                "sent_at": item.sent_at,
                "failure_reason": None,
            }
            for item in review_requests
        ],
    }


@backoffice_router.get("/customers/{customer_id}/telegram-connect-link")
async def get_customer_telegram_connect_link(
    customer_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    token = _customer_connect_token(customer_id)
    bot_username = _telegram_bot_username()
    return {
        "customer_id": customer_id,
        "bot_username": bot_username,
        "connect_link": f"https://t.me/{bot_username}?start={token}",
        "expires_in_days": TELEGRAM_CUSTOMER_CONNECT_TOKEN_DAYS,
    }


@backoffice_router.get("/masters/me/telegram-connect-link")
async def get_my_master_telegram_connect_link(
    current_master: Master = Depends(get_current_master),
) -> dict[str, object]:
    token = _master_connect_token(current_master.id)
    bot_username = _telegram_bot_username()
    return {
        "master_id": current_master.id,
        "bot_username": bot_username,
        "connect_link": f"https://t.me/{bot_username}?start={token}",
        "expires_in_days": TELEGRAM_CUSTOMER_CONNECT_TOKEN_DAYS,
        "telegram_connected": bool(current_master.telegram_chat_id),
    }


@backoffice_router.post("/customers/{customer_id}/messages")
async def send_customer_manual_message(
    customer_id: int,
    payload: ManualCustomerMessageRequest,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    preference = await service.get_preference(session, customer_id)
    if payload.channel != "telegram":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only Telegram is currently supported")
    if preference is None or not preference.telegram_chat_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer has no Telegram chat_id")
    result = await TelegramMessageProvider().send_message(destination=preference.telegram_chat_id, body=payload.body)
    return {"sent": True, "provider_message_id": result.provider_message_id, "provider_response": result.raw_response}


@public_router.get("/telegram/master-photo/{master_id}.jpg", response_class=FileResponse)
async def get_telegram_master_photo(
    master_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> FileResponse:
    master = (
        await session.execute(
            select(Master)
            .options(selectinload(Master.photo_upload), selectinload(Master.avatar_upload))
            .where(Master.id == master_id, Master.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if master is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    if not (getattr(master, "photo_url", None) or getattr(master, "avatar_url", None)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master photo not found")

    upload = _master_photo_upload(master)
    if upload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master photo not found")

    source_path = _safe_upload_path(upload)
    if source_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master photo file not found")

    try:
        photo_path = await asyncio.to_thread(_ensure_telegram_master_photo, master_id, upload, source_path)
    except RuntimeError as exc:
        logger.warning("Telegram master photo preparation failed", extra={"master_id": master_id, "error": str(exc)})
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Master photo is unavailable") from exc

    return FileResponse(
        photo_path,
        media_type="image/jpeg",
        filename=f"master-{master_id}.jpg",
    )


@backoffice_router.put("/customers/{customer_id}/preferences")
async def update_customer_communication_preferences(
    customer_id: int,
    payload: ClientCommunicationPreferenceUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    preference = await service.upsert_preference(session, customer_id, payload.model_dump(exclude_unset=True))
    return {
        "customer_id": preference.customer_id,
        "telegram_chat_id": preference.telegram_chat_id,
        "preferred_language": preference.preferred_language,
        "marketing_consent": preference.marketing_consent == ConsentStatus.opted_in,
        "transactional_consent": preference.transactional_consent.value,
        "do_not_contact": preference.do_not_contact,
        "repeat_booking_opt_out": preference.repeat_booking_opt_out,
        "opt_out": preference.do_not_contact or preference.marketing_consent == ConsentStatus.opted_out,
        "opted_out_at": preference.opted_out_at,
    }


@public_router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    background_tasks: BackgroundTasks = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram webhook secret")

    update = await request.json()
    if not isinstance(update, dict):
        return {"ok": True, "handled": False}

    if await _telegram_update_already_processed(session, update):
        return {"ok": True, "handled": True, "action": "duplicate_update"}

    telegram_contact = await _upsert_telegram_contact_from_update(session, update)
    if isinstance(telegram_contact, TelegramContact):
        await session.commit()

    token = _telegram_start_token(update)
    chat_id = _telegram_chat_id(update)
    text = _telegram_message_text(update)
    callback_query_id = _telegram_callback_query_id(update)
    telegram = TelegramMessageProvider()

    if chat_id and _is_contact_message(update):
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body=TELEGRAM_CONTACT_SAVED_MESSAGE,
            reply_markup=_booking_action_reply_markup(),
        )
        return {"ok": True, "handled": True, "action": "contact_saved"}

    if chat_id and _is_plain_start_command(text):
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body=TELEGRAM_START_WELCOME_MESSAGE,
            reply_markup=_share_contact_reply_markup(),
        )
        return {"ok": True, "handled": True, "action": "start_share_contact"}

    if token and chat_id and token.startswith("m_"):
        master_id = _master_id_from_connect_token(token)
        master = await session.get(Master, master_id)
        if master is None or not getattr(master, "is_active", True):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        master.telegram_chat_id = chat_id
        await session.commit()
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body=(
                f"Telegram підключено для майстра {_master_display_name(master)}. "
                "Тепер ви отримуватимете сповіщення про нові записи."
            ),
        )
        return {"ok": True, "handled": True, "master_id": master_id, "telegram_chat_id": chat_id}

    if token and chat_id:
        customer_id = _customer_id_from_connect_token(token)
        preference = await service.upsert_preference(
            session,
            customer_id,
            {
                "telegram_chat_id": chat_id,
                "transactional_consent": ConsentStatus.opted_in,
            },
        )
        if telegram_contact is not None:
            telegram_contact.linked_customer_id = customer_id
            await session.commit()
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body="Telegram підключено. Тепер ми зможемо надсилати вам повідомлення про записи.",
        )
        return {"ok": True, "handled": True, "customer_id": customer_id, "telegram_chat_id": preference.telegram_chat_id}

    if chat_id and not _telegram_contact_allows_booking_flow(telegram_contact):
        await _safe_answer_callback_query(
            telegram,
            callback_query_id=callback_query_id,
            text='Спочатку натисніть "Поділитись контактом"',
        )
        await _safe_send_telegram_message(
            telegram,
            destination=chat_id,
            body=TELEGRAM_START_WELCOME_MESSAGE,
            reply_markup=_share_contact_reply_markup(),
        )
        return {"ok": True, "handled": True, "action": "contact_required"}

    cancel_booking_id = _cancel_booking_id_from_callback(text)
    if chat_id and cancel_booking_id is not None:
        handled = await _handle_cancel_booking_callback(
            telegram,
            session,
            chat_id=chat_id,
            booking_id=cancel_booking_id,
            telegram_contact=telegram_contact,
            callback_query_id=callback_query_id,
            background_tasks=background_tasks,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "cancel_booking" if handled else "cancel_booking_failed",
        }

    selected_visit_time = _selected_visit_time_from_callback(text)
    if chat_id and selected_visit_time is not None:
        handled = await _handle_time_selection(
            telegram,
            session,
            chat_id=chat_id,
            visit_time=selected_visit_time,
            callback_query_id=callback_query_id,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "select_time" if handled else "select_time_failed",
        }

    selected_visit_date = _selected_visit_date_from_callback(text)
    if chat_id and selected_visit_date is not None:
        handled = await _handle_date_selection(
            telegram,
            session,
            chat_id=chat_id,
            visit_date=selected_visit_date,
            callback_query_id=callback_query_id,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "select_date" if handled else "select_date_failed",
        }

    selected_service_id = _selected_service_id_from_callback(text)
    if chat_id and selected_service_id is not None:
        handled = await _handle_service_selection(
            telegram,
            session,
            chat_id=chat_id,
            service_id=selected_service_id,
            callback_query_id=callback_query_id,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "select_service" if handled else "select_service_failed",
        }

    selected_master_id = _selected_master_id_from_callback(text)
    if chat_id and selected_master_id is not None:
        handled = await _handle_master_selection(
            telegram,
            session,
            chat_id=chat_id,
            master_id=selected_master_id,
            telegram_contact=telegram_contact,
            callback_query_id=callback_query_id,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "select_master" if handled else "select_master_not_found",
        }

    if chat_id and text and text.casefold() in NEW_BOOKING_BOT_TEXTS:
        handled = await _handle_new_booking_action(
            telegram,
            session,
            chat_id=chat_id,
            telegram_contact=telegram_contact,
            callback_query_id=callback_query_id,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "new_booking_start" if handled else "new_booking_needs_contact",
        }

    if chat_id and _is_services_action(text):
        handled = await _handle_services_action(telegram, session, chat_id=chat_id)
        return {
            "ok": True,
            "handled": True,
            "action": "list_services" if handled else "list_services_without_master",
        }

    if chat_id and _is_date_time_action(text):
        handled = await _handle_date_time_action(telegram, session, chat_id=chat_id)
        return {
            "ok": True,
            "handled": True,
            "action": "list_visit_dates" if handled else "list_visit_dates_missing_context",
        }

    if chat_id and _is_book_action(text):
        handled = await _handle_booking_confirmation(
            telegram,
            session,
            chat_id=chat_id,
            telegram_contact=telegram_contact,
            background_tasks=background_tasks,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "book" if handled else "book_failed",
        }

    if chat_id and _is_view_bookings_action(text):
        handled = await _handle_view_bookings_action(
            telegram,
            session,
            chat_id=chat_id,
            telegram_contact=telegram_contact,
        )
        return {
            "ok": True,
            "handled": True,
            "action": "view_bookings" if handled else "view_bookings_empty",
        }

    if chat_id and _is_cancel_draft_action(text):
        handled = await _handle_cancel_draft_action(telegram, session, chat_id=chat_id)
        return {
            "ok": True,
            "handled": True,
            "action": "cancel_draft" if handled else "cancel_draft_failed",
        }

    if chat_id and _is_master_action(text):
        await _send_master_list(telegram, session, chat_id)
        return {"ok": True, "handled": True, "action": "list_masters"}

    if not token or not chat_id:
        if chat_id and text:
            await _safe_answer_callback_query(telegram, callback_query_id=callback_query_id)
            await _safe_send_telegram_message(telegram, destination=chat_id, body=_unsupported_bot_command_message())
            return {"ok": True, "handled": True, "action": "unsupported_command_fallback"}
        return {"ok": True, "handled": False}

    return {"ok": True, "handled": False}


@backoffice_router.patch("/customers/{customer_id}/preferences")
async def patch_customer_communication_preferences(
    customer_id: int,
    payload: ClientCommunicationPreferenceUpdate,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await update_customer_communication_preferences(customer_id, payload, _, session)


@backoffice_router.post("/jobs/process-pending")
async def process_pending_messages(
    limit: int | None = Query(default=None, ge=1, le=1000),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    return {"processed": await service.process_pending_messages(session, limit)}


@backoffice_router.post("/jobs/create-review-requests")
async def create_review_requests(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    return {"created": await service.create_review_requests_for_completed_appointments(session)}


@backoffice_router.post("/jobs/sync-sms-delivery-statuses")
async def sync_sms_delivery_statuses(
    limit: int | None = Query(default=None, ge=1, le=100),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    return {"updated": await service.sync_sms_delivery_statuses(session, limit)}


@backoffice_router.post("/jobs/create-appointment-reminders")
async def create_appointment_reminders(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    return {"created": await service.create_appointment_reminders_for_upcoming_bookings(session)}


@backoffice_router.post("/jobs/send-booking-sms-reminders")
async def send_booking_sms_reminders(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    return {"sent": await booking_sms_notification_service.send_due_booking_reminders(session)}
