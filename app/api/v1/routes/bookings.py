from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal, get_db_session
from app.core.security import get_password_hash
from app.dependencies.auth import get_current_admin_user, get_current_master, get_optional_admin_user
from app.models.admin_user import AdminUser
from app.models.booking import (
    BarberService,
    BaseService,
    Booking,
    BookingServiceItem,
    BookingStatus,
    Master,
    MasterAvailabilityWindow,
    MasterTimeBlock,
)
from app.models.customer import Customer
from app.models.upload import Upload
from app.models.waitlist import WaitlistOffer, WaitlistOfferStatus
from app.repositories.base import BaseRepository
from app.schemas.booking import (
    AdminMasterAvailabilityDaysCreate,
    AdminMasterAvailabilityWindowCreate,
    AdminMasterTimeBlockCreate,
    AdminMasterTimeBlockUpdate,
    AdminBookingCreate,
    AdminBookingUpdate,
    AvailableSlotResponse,
    BookingBackofficeResponse,
    BookingResponse,
    CalendarCapacityRangeResponse,
    CalendarHoldResponse,
    BarberServiceCreate,
    BarberServiceResponse,
    BarberServiceUpdate,
    BaseServiceCreate,
    BaseServiceResponse,
    BaseServiceUpdate,
    BookingStatusUpdate,
    BookingUpdate,
    MasterBackofficeResponse,
    MasterAvailabilityDaysCreate,
    MasterAvailabilityWindowCreate,
    MasterAvailabilityWindowResponse,
    MasterCreate,
    MasterResponse,
    MasterTimeBlockCreate,
    MasterTimeBlockResponse,
    MasterUpdate,
    PublicServiceCatalogBarberService,
    PublicServiceCatalogItem,
    PublicBookingCreate,
    SyncDefaultServicesResponse,
    sync_service_text_data,
)
from app.dependencies.common import PaginationDep
from app.schemas.common import PaginatedResponse
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.customer_activity import customer_activity_service, set_browser_session_cookie
from app.services.customer_activity_notifications import customer_activity_notification_service
from app.services.email_notifications import NewBookingEmail, email_notification_service
from app.services.master_notifications import NewBookingTelegram, master_telegram_notification_service
from app.services.repeat_booking import repeat_booking_service
from app.services.uploads import delete_upload_file, save_image_upload
from app.services.waitlist_offers import FreedBookingSlot, offer_freed_booking_slot

public_router = APIRouter()
backoffice_router = APIRouter()
service = BookingServiceLayer()
master_repo = BaseRepository(Master)
base_service_repo = BaseRepository(BaseService)
barber_service_repo = BaseRepository(BarberService)
MAX_CALENDAR_RANGE_DAYS = 31
logger = logging.getLogger(__name__)


async def set_booking_browser_session(
    response: Response,
    *,
    customer_id: int,
    booking_id: int,
) -> bool:
    """Persist the convenience capability without risking the committed booking."""
    try:
        async with AsyncSessionLocal() as browser_session:
            browser_token, browser_expires_at = await customer_activity_service.create_browser_session(
                browser_session,
                customer_id,
                source_booking_id=booking_id,
            )
            await browser_session.commit()
    except Exception:
        logger.exception(
            "Customer activity browser session creation failed",
            extra={"booking_id": booking_id},
        )
        return False

    set_browser_session_cookie(response, browser_token, expires_at=browser_expires_at)
    return True


async def list_public_catalog_promotions(session: AsyncSession):
    return await service.promotion_service.list_active_public_catalog_promotions(
        session,
        at=datetime.now(tz=KYIV_TZ),
    )


def apply_booking_status_update(booking: Booking, new_status: BookingStatus) -> None:
    booking.status = new_status
    now = datetime.now(KYIV_TZ)
    if new_status == BookingStatus.cancelled:
        booking.cancelled_at = now
        booking.completed_at = None
    elif new_status == BookingStatus.completed:
        booking.completed_at = now
        booking.cancelled_at = None
    else:
        booking.cancelled_at = None
        booking.completed_at = None


def booking_owner_filter(master: Master):
    if getattr(master, "booking_redirect_master_id", None) is None:
        return Booking.master_id == master.id
    return or_(
        Booking.redirected_from_master_id == master.id,
        and_(
            Booking.master_id == master.id,
            Booking.redirected_from_master_id.is_(None),
        ),
    )


def booking_belongs_to_master(booking: Booking, master: Master) -> bool:
    if getattr(master, "booking_redirect_master_id", None) is None:
        return booking.master_id == master.id
    redirected_from_master_id = booking.redirected_from_master_id
    return redirected_from_master_id == master.id or (
        redirected_from_master_id is None and booking.master_id == master.id
    )


def ensure_bounded_calendar_range(date_from: datetime, date_to: datetime) -> tuple[datetime, datetime]:
    start_at, end_at = service.ensure_valid_interval(date_from, date_to)
    if end_at - start_at > timedelta(days=MAX_CALENDAR_RANGE_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Calendar range cannot exceed {MAX_CALENDAR_RANGE_DAYS} days",
        )
    return start_at, end_at


def freed_slot_snapshot(booking: Booking, *, keep_source: bool = True) -> FreedBookingSlot:
    return FreedBookingSlot(
        master_id=booking.master_id,
        start_at=booking.start_at,
        end_at=booking.end_at,
        source_booking_id=booking.id if keep_source else None,
        source_master_id=booking.redirected_from_master_id or booking.master_id,
    )


def schedule_waitlist_offer(
    background_tasks: BackgroundTasks | None,
    slot: FreedBookingSlot | None,
) -> None:
    if background_tasks is not None and slot is not None:
        background_tasks.add_task(offer_freed_booking_slot, slot)


def ensure_booking_editable(booking: Booking) -> None:
    if booking.status == BookingStatus.completed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Completed bookings cannot be modified")


def booking_response_options():
    booking_service_items = selectinload(Booking.service_items).selectinload(BookingServiceItem.service)
    return (
        selectinload(Booking.customer),
        selectinload(Booking.redirected_from_master),
        selectinload(Booking.service).selectinload(BarberService.base_service),
        booking_service_items,
        booking_service_items.selectinload(BarberService.base_service),
    )


def should_send_booking_notifications(booking: Booking) -> bool:
    start_at = booking.start_at
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=KYIV_TZ)
    else:
        start_at = start_at.astimezone(KYIV_TZ)
    return start_at > datetime.now(KYIV_TZ)


def master_response_options():
    return (
        selectinload(Master.services).selectinload(BarberService.base_service),
        selectinload(Master.photo_upload),
        selectinload(Master.avatar_upload),
    )


def public_barber_service_filter():
    return (
        BarberService.is_active.is_(True),
        or_(
            BarberService.base_service_id.is_(None),
            BarberService.base_service.has(BaseService.is_active.is_(True)),
        ),
    )


def is_public_barber_service_active(service: BarberService) -> bool:
    base_service = getattr(service, "base_service", None)

    return service.is_active and (
        service.base_service_id is None
        or base_service is None
        or bool(base_service.is_active)
    )


def ensure_superuser(current_user: AdminUser) -> None:
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only superusers can manage booking settings")


async def get_linked_master_for_user(session: AsyncSession, current_user: AdminUser) -> Master:
    master = (
        await session.execute(
            select(Master).where(Master.admin_user_id == current_user.id, Master.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if not master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Current user is not linked to a master")
    return master


async def ensure_master_exists(session: AsyncSession, master_id: int) -> Master:
    master = await session.get(Master, master_id)
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Barber not found")
    return master


async def ensure_booking_redirect_master_valid(
    session: AsyncSession,
    *,
    source_master_id: int | None,
    redirect_master_id: int | None,
) -> None:
    if redirect_master_id is None:
        return
    if source_master_id is not None and redirect_master_id == source_master_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Master cannot redirect bookings to itself")

    redirect_master = await session.get(Master, redirect_master_id)
    if not redirect_master:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Redirect master not found")
    if not redirect_master.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Redirect master must be active")

    visited_master_ids = {source_master_id} if source_master_id is not None else set()
    current_master = redirect_master
    while True:
        if current_master.id in visited_master_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking redirect cannot create a cycle")
        visited_master_ids.add(current_master.id)
        next_master_id = current_master.booking_redirect_master_id
        if next_master_id is None:
            return
        if next_master_id in visited_master_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking redirect cannot create a cycle")
        current_master = await session.get(Master, next_master_id)
        if current_master is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Redirect master not found")
        if not current_master.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Redirect master must be active")


async def ensure_redirect_change_has_no_future_state(
    session: AsyncSession,
    *,
    source_master_id: int,
) -> None:
    now = datetime.now(KYIV_TZ)
    checks = (
        (
            "bookings",
            select(Booking.id).where(
                or_(
                    Booking.master_id == source_master_id,
                    Booking.redirected_from_master_id == source_master_id,
                ),
                Booking.status == BookingStatus.confirmed,
                Booking.end_at > now,
            ),
        ),
        (
            "availability windows",
            select(MasterAvailabilityWindow.id).where(
                MasterAvailabilityWindow.master_id == source_master_id,
                MasterAvailabilityWindow.end_at > now,
            ),
        ),
        (
            "time blocks",
            select(MasterTimeBlock.id).where(
                MasterTimeBlock.master_id == source_master_id,
                MasterTimeBlock.end_at > now,
            ),
        ),
        (
            "waitlist holds",
            select(WaitlistOffer.id).where(
                or_(
                    WaitlistOffer.source_master_id == source_master_id,
                    and_(
                        WaitlistOffer.source_master_id.is_(None),
                        WaitlistOffer.master_id == source_master_id,
                    ),
                ),
                WaitlistOffer.status.in_(
                    (
                        WaitlistOfferStatus.pending,
                        WaitlistOfferStatus.sent,
                        WaitlistOfferStatus.delivered,
                    )
                ),
                WaitlistOffer.end_at > now,
            ),
        ),
    )
    for label, stmt in checks:
        existing_id = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        if existing_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot change booking redirect while future {label} exist for this master",
            )


async def resolve_calendar_master(session: AsyncSession, master: Master) -> Master:
    if getattr(master, "booking_redirect_master_id", None) is None:
        return master
    _, booking_master = await service.resolve_booking_master(session, master.id)
    return booking_master


async def resolve_backoffice_calendar_master_id(session: AsyncSession, master_id: int | None) -> int | None:
    if master_id is None:
        return None
    _, booking_master = await service.resolve_booking_master(session, master_id)
    return booking_master.id


async def ensure_can_manage_barber_services(
    session: AsyncSession,
    current_user: AdminUser,
    barber_id: int,
) -> Master:
    master = await ensure_master_exists(session, barber_id)
    if current_user.is_superuser:
        return master
    linked_master = await get_linked_master_for_user(session, current_user)
    if linked_master.id != barber_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another barber's services")
    return master


async def get_active_base_service(session: AsyncSession, base_service_id: int | None) -> BaseService | None:
    if base_service_id is None:
        return None
    base_service = await session.get(BaseService, base_service_id)
    if not base_service or not base_service.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Base service not found")
    return base_service


async def get_upload(session: AsyncSession, upload_id: int | None) -> Upload | None:
    if upload_id is None:
        return None
    upload = await session.get(Upload, upload_id)
    if not upload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Photo upload not found")
    return upload


async def apply_master_upload_data(session: AsyncSession, data: dict) -> None:
    for upload_field, url_field in (
        ("photo_upload_id", "photo_url"),
        ("avatar_upload_id", "avatar_url"),
    ):
        if upload_field not in data:
            continue
        upload = await get_upload(session, data[upload_field])
        data[url_field] = upload.file_url if upload else None


async def cleanup_unreferenced_uploads(session: AsyncSession, upload_ids: set[int]) -> list[str]:
    file_paths: list[str] = []
    for upload_id in upload_ids:
        still_used = (
            await session.execute(
                select(Master.id)
                .where(
                    or_(
                        Master.photo_upload_id == upload_id,
                        Master.avatar_upload_id == upload_id,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if still_used is not None:
            continue

        upload = await session.get(Upload, upload_id)
        if upload is None:
            continue
        file_paths.append(upload.file_path)
        await session.delete(upload)

    return file_paths


async def cleanup_replaced_master_uploads(
    session: AsyncSession,
    master: Master,
    old_upload_ids: set[int | None],
) -> list[str]:
    upload_ids = {
        upload_id
        for upload_id in old_upload_ids
        if upload_id is not None and upload_id not in {master.photo_upload_id, master.avatar_upload_id}
    }
    if not upload_ids:
        return []

    await session.flush()
    return await cleanup_unreferenced_uploads(session, upload_ids)


async def cleanup_master_images(session: AsyncSession, master: Master) -> list[str]:
    upload_ids = {
        upload_id
        for upload_id in (master.photo_upload_id, master.avatar_upload_id)
        if upload_id is not None
    }
    if not upload_ids:
        return []

    master.photo_upload_id = None
    master.photo_url = None
    master.avatar_upload_id = None
    master.avatar_url = None
    await session.flush()

    return await cleanup_unreferenced_uploads(session, upload_ids)


async def ensure_no_duplicate_barber_service(
    session: AsyncSession,
    *,
    barber_id: int,
    base_service_id: int | None,
    name: str | None,
    exclude_service_id: int | None = None,
) -> None:
    if base_service_id is not None:
        stmt = select(BarberService).where(
            BarberService.master_id == barber_id,
            BarberService.base_service_id == base_service_id,
        )
        if exclude_service_id is not None:
            stmt = stmt.where(BarberService.id != exclude_service_id)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Barber service for this base service already exists",
            )
        return

    if name is not None:
        stmt = select(BarberService).where(
            BarberService.master_id == barber_id,
            BarberService.base_service_id.is_(None),
            BarberService.name == name,
        )
        if exclude_service_id is not None:
            stmt = stmt.where(BarberService.id != exclude_service_id)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Custom barber service with this name already exists",
            )


def ensure_barber_service_update_allowed(current_user: AdminUser, item: BarberService, data: dict) -> None:
    if current_user.is_superuser:
        return
    if "base_service_id" in data:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can link barber services to base services")


def ensure_master_service_update_allowed(item: BarberService, data: dict) -> None:
    if "base_service_id" in data:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can link barber services to base services")


def service_payload_value(payload: BarberServiceCreate, base_service: BaseService | None, field_name: str):
    value = getattr(payload, field_name)
    if value is not None:
        return value
    return getattr(base_service, field_name, None)


def build_barber_service_data(barber_id: int, payload: BarberServiceCreate, base_service: BaseService | None) -> dict:
    name = service_payload_value(payload, base_service, "name")
    title_uk = service_payload_value(payload, base_service, "title_uk")
    title_en = service_payload_value(payload, base_service, "title_en")
    description = service_payload_value(payload, base_service, "description")
    description_uk = service_payload_value(payload, base_service, "description_uk")
    description_en = service_payload_value(payload, base_service, "description_en")

    data = {
        "master_id": barber_id,
        "base_service_id": payload.base_service_id,
        "name": name,
        "title_uk": title_uk,
        "title_en": title_en,
        "description": description,
        "description_uk": description_uk,
        "description_en": description_en,
        "duration_minutes": service_payload_value(payload, base_service, "duration_minutes"),
        "price": service_payload_value(payload, base_service, "price"),
        "is_active": payload.is_active,
    }
    return sync_service_text_data(data)


@public_router.get("/masters", response_model=list[MasterResponse])
async def list_public_masters(session: AsyncSession = Depends(get_db_session)) -> list[MasterResponse]:
    stmt = (
        select(Master)
        .options(*master_response_options())
        .where(Master.is_active.is_(True))
        .order_by(Master.full_name.asc())
    )
    masters = (await session.execute(stmt)).scalars().unique().all()
    promotions = await list_public_catalog_promotions(session)
    for master in masters:
        master.services = [item for item in master.services if is_public_barber_service_active(item)]
        service.promotion_service.annotate_public_promotions(master.services, promotions)
    return [MasterResponse.model_validate(master) for master in masters]


@public_router.get("/services", response_model=list[BarberServiceResponse])
async def list_public_services(session: AsyncSession = Depends(get_db_session)) -> list[BarberServiceResponse]:
    stmt = (
        select(BarberService)
        .options(selectinload(BarberService.base_service))
        .where(*public_barber_service_filter())
        .order_by(BarberService.name.asc())
    )
    services = (await session.execute(stmt)).scalars().all()
    services = [service for service in services if is_public_barber_service_active(service)]
    promotions = await list_public_catalog_promotions(session)
    service.promotion_service.annotate_public_promotions(services, promotions)
    return [BarberServiceResponse.model_validate(item) for item in services]


def _catalog_key(service: BarberService) -> tuple[str, int | None, str, str | None, int, int]:
    title_uk = getattr(service, "title_uk", None) or service.name
    title_en = getattr(service, "title_en", None)
    if service.base_service_id is not None:
        source_key = f"base:{service.base_service_id}"
    else:
        source_key = f"custom:{title_uk.strip().casefold()}:{(title_en or '').strip().casefold()}"
    return (
        source_key,
        service.base_service_id,
        title_uk,
        title_en,
        service.duration_minutes,
        service.price,
    )


@public_router.get("/service-catalog", response_model=list[PublicServiceCatalogItem])
async def list_public_service_catalog(session: AsyncSession = Depends(get_db_session)) -> list[PublicServiceCatalogItem]:
    stmt = (
        select(BarberService)
        .outerjoin(BaseService, BarberService.base_service_id == BaseService.id)
        .options(selectinload(BarberService.base_service))
        .where(*public_barber_service_filter())
        .order_by(
            BaseService.popularity_rank.asc().nulls_last(),
            BarberService.name.asc(),
            BarberService.price.asc(),
            BarberService.duration_minutes.asc(),
            BarberService.id.asc(),
        )
    )
    services = (await session.execute(stmt)).scalars().all()
    services = [service for service in services if is_public_barber_service_active(service)]
    promotions = await list_public_catalog_promotions(session)
    service.promotion_service.annotate_public_promotions(services, promotions)
    grouped: OrderedDict[
        tuple[str, int | None, str, str | None, int, int],
        list[BarberService],
    ] = OrderedDict()
    for item in services:
        grouped.setdefault(_catalog_key(item), []).append(item)

    catalog: list[PublicServiceCatalogItem] = []
    for index, (
        (source_key, base_service_id, title_uk, title_en, duration_minutes, price),
        items,
    ) in enumerate(grouped.items(), start=1):
        source_type = "base" if base_service_id is not None else "custom"
        name = next((item.name for item in items if item.name), title_uk)
        active_promotion = next(
            (getattr(item, "active_promotion", None) for item in items if getattr(item, "active_promotion", None)),
            None,
        )
        catalog.append(
            PublicServiceCatalogItem(
                catalog_id=f"{source_key}:{duration_minutes}:{price}:{index}",
                base_service_id=base_service_id,
                source_type=source_type,
                name=name,
                title_uk=title_uk,
                title_en=title_en,
                description=next((item.description for item in items if item.description), None),
                description_uk=next(
                    (getattr(item, "description_uk", None) for item in items if getattr(item, "description_uk", None)),
                    None,
                ),
                description_en=next(
                    (getattr(item, "description_en", None) for item in items if getattr(item, "description_en", None)),
                    None,
                ),
                duration_minutes=duration_minutes,
                price=price,
                active_promotion=active_promotion,
                barber_ids=sorted({item.master_id for item in items}),
                barber_service_ids=[item.id for item in items],
                barber_services=[PublicServiceCatalogBarberService.model_validate(item) for item in items],
            )
        )
    return catalog


@public_router.get("/masters/{master_id}/available-slots", response_model=list[AvailableSlotResponse])
async def get_available_slots(
    master_id: int,
    date_: date = Query(alias="date"),
    service_id: int | None = Query(default=None),
    service_ids: list[int] | None = Query(default=None),
    duration_minutes: int | None = Query(default=None, gt=0, le=720),
    session: AsyncSession = Depends(get_db_session),
) -> list[AvailableSlotResponse]:
    return await service.get_available_slots(
        session,
        master_id=master_id,
        service_id=service_id,
        service_ids=service_ids,
        duration_minutes=duration_minutes,
        target_date=date_,
    )


@public_router.post("/bookings", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_public_booking(
    payload: PublicBookingCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    current_user: AdminUser | None = Depends(get_optional_admin_user),
    session: AsyncSession = Depends(get_db_session),
    x_repeat_booking_token: str | None = Header(default=None),
) -> BookingResponse:
    create_kwargs = {
        "allow_past": bool(current_user and current_user.is_superuser),
        "allow_private_promotions": bool(current_user and current_user.is_superuser),
        "record_funnel_success": True,
    }
    if payload.promotion_code:
        create_kwargs["promotion_code"] = payload.promotion_code
    if isinstance(x_repeat_booking_token, str) and x_repeat_booking_token:
        create_kwargs["repeat_booking_token"] = x_repeat_booking_token
    booking = await service.create_public_booking(
        session,
        payload,
        **create_kwargs,
    )
    booking = (
        await session.execute(
            select(Booking)
            .options(selectinload(Booking.master), *booking_response_options())
            .where(Booking.id == booking.id)
        )
    ).scalar_one()
    await set_booking_browser_session(
        response,
        customer_id=booking.customer_id,
        booking_id=booking.id,
    )
    if should_send_booking_notifications(booking):
        notification_master = booking.redirected_from_master or booking.master
        service_name = ", ".join(item.name for item in booking.services) or booking.service.name
        background_tasks.add_task(
            email_notification_service.send_new_booking_to_master,
            NewBookingEmail(
                booking_id=booking.id,
                master_name=notification_master.full_name,
                master_email=notification_master.email,
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
                master_id=notification_master.id,
                master_name=notification_master.full_name,
                telegram_chat_id=notification_master.telegram_chat_id,
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
    return BookingResponse.model_validate(booking)


@backoffice_router.get("/masters/me/calendar", response_model=list[BookingBackofficeResponse])
async def get_my_calendar(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[BookingBackofficeResponse]:
    start_at, end_at = ensure_bounded_calendar_range(date_from, date_to)
    stmt = (
        select(Booking)
        .options(*booking_response_options())
        .where(booking_owner_filter(current_master), Booking.start_at < end_at, Booking.end_at > start_at)
        .order_by(Booking.start_at.asc())
    )
    bookings = (await session.execute(stmt)).scalars().all()
    return [BookingBackofficeResponse.model_validate(item) for item in bookings]


@backoffice_router.get("/masters/me/calendar-holds", response_model=list[CalendarHoldResponse])
async def get_my_calendar_holds(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[CalendarHoldResponse]:
    start_at, end_at = ensure_bounded_calendar_range(date_from, date_to)
    calendar_master = await resolve_calendar_master(session, current_master)
    holds = await service.list_active_waitlist_holds(
        session,
        master_id=calendar_master.id,
        start_at=start_at,
        end_at=end_at,
    )
    return [CalendarHoldResponse.model_validate(item) for item in holds]


@backoffice_router.get(
    "/masters/me/calendar-capacity",
    response_model=list[CalendarCapacityRangeResponse],
)
async def get_my_calendar_capacity(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[CalendarCapacityRangeResponse]:
    start_at, end_at = ensure_bounded_calendar_range(date_from, date_to)
    calendar_master = await resolve_calendar_master(session, current_master)
    bookings = (
        await session.execute(
            select(Booking)
            .where(
                Booking.master_id == calendar_master.id,
                Booking.status == BookingStatus.confirmed,
                Booking.start_at < end_at,
                Booking.end_at > start_at,
            )
            .order_by(Booking.start_at.asc())
        )
    ).scalars().all()
    return [CalendarCapacityRangeResponse.model_validate(item) for item in bookings]


@backoffice_router.get("/masters/me/bookings", response_model=list[BookingBackofficeResponse])
async def list_my_bookings(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[BookingBackofficeResponse]:
    stmt = (
        select(Booking)
        .options(*booking_response_options())
        .where(booking_owner_filter(current_master))
        .order_by(Booking.start_at.asc())
    )
    if date_from:
        stmt = stmt.where(Booking.end_at > service.normalize_datetime(date_from))
    if date_to:
        stmt = stmt.where(Booking.start_at < service.normalize_datetime(date_to))
    if booking_status:
        stmt = stmt.where(Booking.status == booking_status)
    bookings = (await session.execute(stmt)).scalars().all()
    return [BookingBackofficeResponse.model_validate(item) for item in bookings]


@backoffice_router.get("/masters/me/services", response_model=list[BarberServiceResponse])
async def list_my_services(
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[BarberServiceResponse]:
    stmt = (
        select(BarberService)
        .options(selectinload(BarberService.base_service))
        .where(BarberService.master_id == current_master.id)
        .order_by(BarberService.id.asc())
    )
    items = (await session.execute(stmt)).scalars().all()
    return [BarberServiceResponse.model_validate(item) for item in items]


@backoffice_router.patch("/masters/me/services/{service_id}", response_model=BarberServiceResponse)
async def update_my_service(
    service_id: int,
    payload: BarberServiceUpdate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> BarberServiceResponse:
    item = await barber_service_repo.get(session, service_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if item.master_id != current_master.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another master's service")
    data = sync_service_text_data(payload.model_dump(exclude_unset=True))
    ensure_master_service_update_allowed(item, data)
    await ensure_no_duplicate_barber_service(
        session,
        barber_id=current_master.id,
        base_service_id=item.base_service_id,
        name=data.get("name", item.name),
        exclude_service_id=item.id,
    )
    updated = await barber_service_repo.update(session, item, data)
    if updated.base_service_id is not None:
        await session.refresh(updated, attribute_names=["base_service"])
    return BarberServiceResponse.model_validate(updated)


@backoffice_router.patch("/masters/me/bookings/{booking_id}/status", response_model=BookingBackofficeResponse)
async def update_my_booking_status(
    booking_id: int,
    payload: BookingStatusUpdate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
    background_tasks: BackgroundTasks = None,
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not booking_belongs_to_master(booking, current_master):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another master's booking")
    ensure_booking_editable(booking)
    if booking.status != BookingStatus.confirmed and payload.status == BookingStatus.confirmed:
        await service.ensure_booking_within_availability(
            session,
            booking.master_id,
            booking.start_at,
            booking.end_at,
        )
        await service.ensure_slot_available(
            session,
            booking.master_id,
            booking.start_at,
            booking.end_at,
            exclude_booking_id=booking.id,
        )
    freed_slot = (
        freed_slot_snapshot(booking)
        if booking.status == BookingStatus.confirmed and payload.status == BookingStatus.cancelled
        else None
    )
    apply_booking_status_update(booking, payload.status)
    if payload.status == BookingStatus.completed:
        await repeat_booking_service.mark_repeat_visit_completed(session, booking)
    await session.commit()
    schedule_waitlist_offer(background_tasks, freed_slot)
    booking = (
        await session.execute(
            select(Booking)
            .options(*booking_response_options())
            .where(Booking.id == booking_id)
        )
    ).scalar_one()
    return BookingBackofficeResponse.model_validate(booking)


@backoffice_router.patch("/masters/me/bookings/{booking_id}", response_model=BookingBackofficeResponse)
async def update_my_booking(
    booking_id: int,
    payload: BookingUpdate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
    background_tasks: BackgroundTasks = None,
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not booking_belongs_to_master(booking, current_master):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another master's booking")
    ensure_booking_editable(booking)
    original_slot = freed_slot_snapshot(booking) if booking.status == BookingStatus.confirmed else None
    original_start_at = booking.start_at
    original_end_at = booking.end_at

    selected_services = None
    if payload.service_ids is not None:
        requested_master = await service.get_active_master_with_services(session, current_master.id)
        booking_master = await service.get_active_master_with_services(session, booking.master_id)
        selected_services = await service.resolve_booking_services_for_master(
            session,
            requested_master,
            booking_master,
            payload.service_ids,
        )
    start_at = payload.start_at if payload.start_at is not None else booking.start_at
    if payload.end_at is not None:
        end_at = payload.end_at
    elif selected_services is not None:
        duration_minutes = sum(item.duration_minutes for item in selected_services)
        end_at = start_at + timedelta(minutes=duration_minutes)
    else:
        end_at = booking.end_at
    start_at, end_at = service.ensure_valid_interval(start_at, end_at)
    service.ensure_not_past(start_at)
    service.ensure_within_working_hours(start_at, end_at)
    await service.ensure_booking_within_availability(session, booking.master_id, start_at, end_at)
    await service.ensure_slot_available(session, booking.master_id, start_at, end_at, exclude_booking_id=booking.id)

    booking.start_at = start_at
    booking.end_at = end_at
    if selected_services is not None:
        await service.update_booking_services(session, booking, selected_services)
        service_prices = {item.id: int(item.price) for item in selected_services}
        customer = None
        if booking.promotion_code and booking.customer_id is not None:
            customer = await session.get(Customer, booking.customer_id)
        await service.promotion_service.apply_to_booking(
            session,
            booking=booking,
            promotion_code=booking.promotion_code,
            customer=customer,
            services=selected_services,
            service_prices=service_prices,
            at=booking.start_at,
            allow_private_promotions=True,
        )
    await session.commit()
    schedule_waitlist_offer(
        background_tasks,
        original_slot
        if original_slot is not None
        and (original_start_at != booking.start_at or original_end_at != booking.end_at)
        else None,
    )
    booking = (
        await session.execute(
            select(Booking)
            .options(*booking_response_options())
            .where(Booking.id == booking_id)
        )
    ).scalar_one()
    return BookingBackofficeResponse.model_validate(booking)


@backoffice_router.delete("/masters/me/bookings/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_booking(
    booking_id: int,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
    background_tasks: BackgroundTasks = None,
) -> None:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not booking_belongs_to_master(booking, current_master):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another master's booking")
    ensure_booking_editable(booking)
    freed_slot = (
        freed_slot_snapshot(booking, keep_source=False)
        if booking.status == BookingStatus.confirmed
        else None
    )
    await session.delete(booking)
    await session.commit()
    schedule_waitlist_offer(background_tasks, freed_slot)


@backoffice_router.post("/masters/me/time-blocks", response_model=MasterTimeBlockResponse, status_code=status.HTTP_201_CREATED)
async def create_my_time_block(
    payload: MasterTimeBlockCreate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> MasterTimeBlockResponse:
    calendar_master = await resolve_calendar_master(session, current_master)
    block = await service.create_time_block(session, calendar_master, payload)
    return MasterTimeBlockResponse.model_validate(block)


@backoffice_router.get("/masters/me/time-blocks", response_model=list[MasterTimeBlockResponse])
async def list_my_time_blocks(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterTimeBlockResponse]:
    calendar_master = await resolve_calendar_master(session, current_master)
    stmt = (
        select(MasterTimeBlock)
        .where(MasterTimeBlock.master_id == calendar_master.id)
        .order_by(MasterTimeBlock.start_at.asc())
    )
    if (date_from is None) != (date_to is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from and date_to must be provided together",
        )
    if date_from is not None and date_to is not None:
        start_at, end_at = ensure_bounded_calendar_range(date_from, date_to)
        stmt = stmt.where(
            MasterTimeBlock.end_at > start_at,
            MasterTimeBlock.start_at < end_at,
        )
    blocks = (await session.execute(stmt)).scalars().all()
    return [MasterTimeBlockResponse.model_validate(item) for item in blocks]


@backoffice_router.delete("/masters/me/time-blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_time_block(
    block_id: int,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    calendar_master = await resolve_calendar_master(session, current_master)
    block = await session.get(MasterTimeBlock, block_id)
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Time block not found")
    if block.master_id != calendar_master.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another master's time block")
    await session.delete(block)
    await session.commit()


@backoffice_router.get("/masters/me/availability", response_model=list[MasterAvailabilityWindowResponse])
async def list_my_availability(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterAvailabilityWindowResponse]:
    start_at, end_at = service.ensure_valid_interval(date_from, date_to)
    calendar_master = await resolve_calendar_master(session, current_master)
    windows = await service.list_availability_windows(session, calendar_master.id, start_at, end_at)
    return [MasterAvailabilityWindowResponse.model_validate(item) for item in windows]


@backoffice_router.post(
    "/masters/me/availability/days",
    response_model=list[MasterAvailabilityWindowResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_my_availability_days(
    payload: MasterAvailabilityDaysCreate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterAvailabilityWindowResponse]:
    calendar_master = await resolve_calendar_master(session, current_master)
    windows = await service.create_availability_days(session, calendar_master, payload.dates)
    return [MasterAvailabilityWindowResponse.model_validate(item) for item in windows]


@backoffice_router.post(
    "/masters/me/availability/windows",
    response_model=MasterAvailabilityWindowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_availability_window(
    payload: MasterAvailabilityWindowCreate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> MasterAvailabilityWindowResponse:
    calendar_master = await resolve_calendar_master(session, current_master)
    window = await service.create_availability_window(session, calendar_master, payload)
    return MasterAvailabilityWindowResponse.model_validate(window)


@backoffice_router.delete("/masters/me/availability/{window_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_availability_window(
    window_id: int,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    calendar_master = await resolve_calendar_master(session, current_master)
    window = await session.get(MasterAvailabilityWindow, window_id)
    if not window:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Availability window not found")
    if window.master_id != calendar_master.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another master's availability")
    await service.delete_availability_window(session, window, allow_booked=False)


@backoffice_router.get("/masters", response_model=PaginatedResponse[MasterBackofficeResponse])
async def admin_list_masters(
    pagination: PaginationDep,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MasterBackofficeResponse]:
    ensure_superuser(current_user)
    stmt = (
        select(Master)
        .options(*master_response_options())
        .order_by(Master.created_at.desc())
    )
    items, total = await master_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[MasterBackofficeResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MasterBackofficeResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("/masters", response_model=MasterBackofficeResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_master(
    payload: MasterCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterBackofficeResponse:
    ensure_superuser(current_user)
    if payload.password and payload.admin_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use either password for a new barber account or admin_user_id for an existing account",
        )
    if payload.password and not payload.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email is required when creating a barber login account",
        )
    if payload.password and payload.email:
        existing_user = (
            await session.execute(select(AdminUser).where(AdminUser.email == payload.email))
        ).scalar_one_or_none()
        if existing_user:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Admin user with this email already exists")
        admin_user = AdminUser(
            email=payload.email,
            hashed_password=get_password_hash(payload.password),
            is_active=payload.is_active,
            is_superuser=False,
        )
        session.add(admin_user)
        await session.flush()
        payload.admin_user_id = admin_user.id
    await ensure_booking_redirect_master_valid(
        session,
        source_master_id=None,
        redirect_master_id=payload.booking_redirect_master_id,
    )
    data = payload.model_dump(exclude={"service_ids", "password"})
    await apply_master_upload_data(session, data)
    master = Master(**data)
    session.add(master)
    await session.flush()
    await service.copy_active_base_services_to_master(session, master)
    await session.commit()
    stmt = (
        select(Master)
        .options(*master_response_options())
        .where(Master.id == master.id)
    )
    master = (await session.execute(stmt)).scalar_one()
    return MasterBackofficeResponse.model_validate(master)


@backoffice_router.put("/masters/{master_id}", response_model=MasterBackofficeResponse)
async def admin_update_master(
    master_id: int,
    payload: MasterUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterBackofficeResponse:
    ensure_superuser(current_user)
    stmt = (
        select(Master)
        .options(*master_response_options())
        .where(Master.id == master_id)
    )
    master = (await session.execute(stmt)).scalar_one_or_none()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    data = payload.model_dump(exclude_unset=True, exclude={"service_ids"})
    redirect_changed = (
        "booking_redirect_master_id" in data
        and data["booking_redirect_master_id"] != master.booking_redirect_master_id
    )
    if redirect_changed:
        await ensure_booking_redirect_master_valid(
            session,
            source_master_id=master_id,
            redirect_master_id=data["booking_redirect_master_id"],
        )
        await ensure_redirect_change_has_no_future_state(
            session,
            source_master_id=master_id,
        )
    await apply_master_upload_data(session, data)
    old_upload_ids = {
        getattr(master, upload_field)
        for upload_field in ("photo_upload_id", "avatar_upload_id")
        if upload_field in data and getattr(master, upload_field) != data[upload_field]
    }
    for key, value in data.items():
        setattr(master, key, value)
    if payload.service_ids is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use /barbers/{barber_id}/services to manage barber services",
        )
    image_file_paths = await cleanup_replaced_master_uploads(session, master, old_upload_ids)
    await session.commit()
    for file_path in image_file_paths:
        delete_upload_file(file_path)
    master = (await session.execute(stmt)).scalar_one()
    return MasterBackofficeResponse.model_validate(master)


@backoffice_router.post("/masters/{master_id}/photo", response_model=MasterBackofficeResponse)
async def admin_upload_master_photo(
    master_id: int,
    file: UploadFile = File(...),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterBackofficeResponse:
    ensure_superuser(current_user)
    master = await master_repo.get(session, master_id)
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    old_upload_id = master.photo_upload_id
    upload_data = await save_image_upload(file, folder="barbers")
    upload = Upload(**upload_data)
    session.add(upload)
    await session.flush()

    master.photo_upload_id = upload.id
    master.photo_url = upload.file_url
    image_file_paths = await cleanup_replaced_master_uploads(session, master, {old_upload_id})
    await session.commit()
    for file_path in image_file_paths:
        delete_upload_file(file_path)

    stmt = (
        select(Master)
        .options(*master_response_options())
        .where(Master.id == master_id)
    )
    master = (await session.execute(stmt)).scalar_one()
    return MasterBackofficeResponse.model_validate(master)


@backoffice_router.post("/masters/{master_id}/avatar", response_model=MasterBackofficeResponse)
async def admin_upload_master_avatar(
    master_id: int,
    file: UploadFile = File(...),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterBackofficeResponse:
    ensure_superuser(current_user)
    master = await master_repo.get(session, master_id)
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    old_upload_id = master.avatar_upload_id
    upload_data = await save_image_upload(file, folder="barbers/avatars")
    upload = Upload(**upload_data)
    session.add(upload)
    await session.flush()

    master.avatar_upload_id = upload.id
    master.avatar_url = upload.file_url
    image_file_paths = await cleanup_replaced_master_uploads(session, master, {old_upload_id})
    await session.commit()
    for file_path in image_file_paths:
        delete_upload_file(file_path)

    stmt = (
        select(Master)
        .options(*master_response_options())
        .where(Master.id == master_id)
    )
    master = (await session.execute(stmt)).scalar_one()
    return MasterBackofficeResponse.model_validate(master)


@backoffice_router.delete("/masters/{master_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_master(
    master_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    ensure_superuser(current_user)
    master = await master_repo.get(session, master_id)
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    has_bookings = (
        await session.execute(select(Booking.id).where(Booking.master_id == master_id).limit(1))
    ).scalar_one_or_none()
    image_file_paths = await cleanup_master_images(session, master)
    if has_bookings is not None:
        master.is_active = False
    else:
        await session.delete(master)
    await session.commit()
    for file_path in image_file_paths:
        delete_upload_file(file_path)


@backoffice_router.get("/admin/services", response_model=list[BaseServiceResponse])
async def admin_list_base_services(
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[BaseServiceResponse]:
    ensure_superuser(current_user)
    stmt = select(BaseService).order_by(BaseService.id.asc())
    items = (await session.execute(stmt)).scalars().all()
    return [BaseServiceResponse.model_validate(item) for item in items]


@backoffice_router.post("/admin/services", response_model=BaseServiceResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_base_service(
    payload: BaseServiceCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BaseServiceResponse:
    ensure_superuser(current_user)
    item = BaseService(**sync_service_text_data(payload.model_dump()))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return BaseServiceResponse.model_validate(item)


@backoffice_router.get("/admin/services/{service_id}", response_model=BaseServiceResponse)
async def admin_get_base_service(
    service_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BaseServiceResponse:
    ensure_superuser(current_user)
    item = await base_service_repo.get(session, service_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return BaseServiceResponse.model_validate(item)


@backoffice_router.patch("/admin/services/{service_id}", response_model=BaseServiceResponse)
async def admin_update_base_service(
    service_id: int,
    payload: BaseServiceUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BaseServiceResponse:
    ensure_superuser(current_user)
    item = await base_service_repo.get(session, service_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    updated = await base_service_repo.update(session, item, sync_service_text_data(payload.model_dump(exclude_unset=True)))
    return BaseServiceResponse.model_validate(updated)


@backoffice_router.delete("/admin/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_base_service(
    service_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    ensure_superuser(current_user)
    item = await base_service_repo.get(session, service_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    item.is_active = False
    await session.commit()


@backoffice_router.get("/barbers/{barber_id}/services", response_model=list[BarberServiceResponse])
async def list_barber_services(
    barber_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[BarberServiceResponse]:
    await ensure_can_manage_barber_services(session, current_user, barber_id)
    stmt = (
        select(BarberService)
        .options(selectinload(BarberService.base_service))
        .where(BarberService.master_id == barber_id)
        .order_by(BarberService.id.asc())
    )
    items = (await session.execute(stmt)).scalars().all()
    return [BarberServiceResponse.model_validate(item) for item in items]


@backoffice_router.get("/barbers/{barber_id}/services/{service_id}", response_model=BarberServiceResponse)
async def get_barber_service(
    barber_id: int,
    service_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BarberServiceResponse:
    await ensure_can_manage_barber_services(session, current_user, barber_id)
    stmt = (
        select(BarberService)
        .options(selectinload(BarberService.base_service))
        .where(BarberService.id == service_id, BarberService.master_id == barber_id)
    )
    item = (await session.execute(stmt)).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return BarberServiceResponse.model_validate(item)


@backoffice_router.post("/barbers/{barber_id}/services", response_model=BarberServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_barber_service(
    barber_id: int,
    payload: BarberServiceCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BarberServiceResponse:
    await ensure_can_manage_barber_services(session, current_user, barber_id)
    base_service = await get_active_base_service(session, payload.base_service_id)
    data = build_barber_service_data(barber_id, payload, base_service)
    await ensure_no_duplicate_barber_service(
        session,
        barber_id=barber_id,
        base_service_id=payload.base_service_id,
        name=data["name"],
    )
    item = BarberService(**data)
    if base_service is not None:
        item.base_service = base_service
    session.add(item)
    await session.commit()
    await session.refresh(item)
    if item.base_service_id is not None:
        await session.refresh(item, attribute_names=["base_service"])
    return BarberServiceResponse.model_validate(item)


@backoffice_router.patch("/barbers/{barber_id}/services/{service_id}", response_model=BarberServiceResponse)
async def update_barber_service(
    barber_id: int,
    service_id: int,
    payload: BarberServiceUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BarberServiceResponse:
    await ensure_can_manage_barber_services(session, current_user, barber_id)
    item = await barber_service_repo.get(session, service_id)
    if not item or item.master_id != barber_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    data = sync_service_text_data(payload.model_dump(exclude_unset=True))
    ensure_barber_service_update_allowed(current_user, item, data)
    if "base_service_id" in data:
        await get_active_base_service(session, data["base_service_id"])
    await ensure_no_duplicate_barber_service(
        session,
        barber_id=barber_id,
        base_service_id=data.get("base_service_id", item.base_service_id),
        name=data.get("name", item.name),
        exclude_service_id=item.id,
    )
    updated = await barber_service_repo.update(session, item, data)
    if updated.base_service_id is not None:
        await session.refresh(updated, attribute_names=["base_service"])
    return BarberServiceResponse.model_validate(updated)


@backoffice_router.delete("/barbers/{barber_id}/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_barber_service(
    barber_id: int,
    service_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    await ensure_can_manage_barber_services(session, current_user, barber_id)
    item = await barber_service_repo.get(session, service_id)
    if not item or item.master_id != barber_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    item.is_active = False
    await session.commit()


@backoffice_router.post("/admin/barbers/{barber_id}/services/sync-defaults", response_model=SyncDefaultServicesResponse)
async def admin_sync_default_barber_services(
    barber_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> SyncDefaultServicesResponse:
    ensure_superuser(current_user)
    await ensure_master_exists(session, barber_id)
    created_count = await service.sync_default_services_for_barber(session, barber_id)
    await session.commit()
    return SyncDefaultServicesResponse(barber_id=barber_id, created_count=created_count)


@backoffice_router.get("/bookings", response_model=PaginatedResponse[BookingBackofficeResponse])
async def admin_list_bookings(
    pagination: PaginationDep,
    master_id: int | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BookingBackofficeResponse]:
    ownership_filter = None
    if not current_user.is_superuser:
        linked_master = await get_linked_master_for_user(session, current_user)
        if master_id is not None and master_id != linked_master.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view another master's bookings")
        ownership_filter = booking_owner_filter(linked_master)
    else:
        master_id = await resolve_backoffice_calendar_master_id(session, master_id)
        if master_id is not None:
            ownership_filter = Booking.master_id == master_id
    stmt = select(Booking).options(*booking_response_options()).order_by(Booking.start_at.asc())
    if ownership_filter is not None:
        stmt = stmt.where(ownership_filter)
    if date_from is not None:
        stmt = stmt.where(Booking.end_at > service.normalize_datetime(date_from))
    if date_to is not None:
        stmt = stmt.where(Booking.start_at < service.normalize_datetime(date_to))
    if booking_status is not None:
        stmt = stmt.where(Booking.status == booking_status)
    items, total = await BaseRepository(Booking).list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[BookingBackofficeResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[BookingBackofficeResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("/bookings", response_model=BookingBackofficeResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_booking(
    payload: AdminBookingCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BookingBackofficeResponse:
    ensure_superuser(current_user)
    create_kwargs = {
        "allow_past": True,
        "allow_private_promotions": True,
        "require_availability": False,
        "require_working_hours": False,
        "allow_duration_override": True,
    }
    promotion_code = getattr(payload, "promotion_code", None)
    if promotion_code:
        create_kwargs["promotion_code"] = promotion_code
    booking = await service.create_public_booking(
        session,
        payload,
        **create_kwargs,
    )
    booking = (
        await session.execute(
            select(Booking)
            .options(*booking_response_options())
            .where(Booking.id == booking.id)
        )
    ).scalar_one()
    return BookingBackofficeResponse.model_validate(booking)


@backoffice_router.patch("/bookings/{booking_id}/status", response_model=BookingBackofficeResponse)
async def admin_update_booking_status(
    booking_id: int,
    payload: BookingStatusUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
    background_tasks: BackgroundTasks = None,
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not current_user.is_superuser:
        master = await get_linked_master_for_user(session, current_user)
        if not booking_belongs_to_master(booking, master):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot update another master's booking")
    ensure_booking_editable(booking)
    if booking.status != BookingStatus.confirmed and payload.status == BookingStatus.confirmed:
        await service.ensure_slot_available(
            session,
            booking.master_id,
            booking.start_at,
            booking.end_at,
            exclude_booking_id=booking.id,
        )
    freed_slot = (
        freed_slot_snapshot(booking)
        if booking.status == BookingStatus.confirmed and payload.status == BookingStatus.cancelled
        else None
    )
    apply_booking_status_update(booking, payload.status)
    if payload.status == BookingStatus.completed:
        await repeat_booking_service.mark_repeat_visit_completed(session, booking)
    await session.commit()
    schedule_waitlist_offer(background_tasks, freed_slot)
    booking = (
        await session.execute(
            select(Booking)
            .options(*booking_response_options())
            .where(Booking.id == booking_id)
        )
    ).scalar_one()
    return BookingBackofficeResponse.model_validate(booking)


@backoffice_router.patch("/bookings/{booking_id}", response_model=BookingBackofficeResponse)
async def admin_update_booking(
    booking_id: int,
    payload: AdminBookingUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
    background_tasks: BackgroundTasks = None,
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    original_slot = freed_slot_snapshot(booking) if booking.status == BookingStatus.confirmed else None
    original_start_at = booking.start_at
    original_end_at = booking.end_at
    fields_set = getattr(payload, "model_fields_set", set())
    service_prices_requested = "service_prices" in fields_set
    promotion_requested = "promotion_code" in fields_set
    pricing_requested = service_prices_requested or promotion_requested
    pricing_recalculation_requested = pricing_requested or payload.service_ids is not None
    schedule_requested = payload.start_at is not None or payload.end_at is not None or payload.service_ids is not None
    if pricing_requested and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can update booking prices and promotions",
        )
    if not current_user.is_superuser:
        master = await get_linked_master_for_user(session, current_user)
        if not booking_belongs_to_master(booking, master):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot update another master's booking")
    if schedule_requested or not pricing_requested:
        ensure_booking_editable(booking)

    if pricing_recalculation_requested:
        booking = (
            await session.execute(
                select(Booking)
                .options(*booking_response_options())
                .where(Booking.id == booking_id)
            )
        ).scalar_one()

    selected_services = None
    if payload.service_ids is not None:
        master = await service.get_active_master_with_services(session, booking.master_id)
        selected_services = await service.get_active_services(session, payload.service_ids)
        service.ensure_master_provides_services(master, [item.id for item in selected_services])
    if schedule_requested:
        start_at = payload.start_at if payload.start_at is not None else booking.start_at
        if payload.end_at is not None:
            end_at = payload.end_at
        elif selected_services is not None:
            duration_minutes = sum(item.duration_minutes for item in selected_services)
            end_at = start_at + timedelta(minutes=duration_minutes)
        else:
            end_at = booking.end_at
        start_at, end_at = service.ensure_valid_interval(start_at, end_at)
        service.ensure_not_past(start_at)
        if current_user.is_superuser:
            service.ensure_within_open_business_days(start_at, end_at)
        else:
            service.ensure_within_working_hours(start_at, end_at)
            await service.ensure_booking_within_availability(session, booking.master_id, start_at, end_at)
        await service.ensure_slot_available(session, booking.master_id, start_at, end_at, exclude_booking_id=booking.id)

        booking.start_at = start_at
        booking.end_at = end_at
    if selected_services is not None:
        service_prices = None
        if pricing_requested:
            requested_prices = getattr(payload, "service_prices", None)
            if requested_prices is not None:
                service_prices = {item.service_id: item.price_amount for item in requested_prices}
        await service.update_booking_services(session, booking, selected_services, service_prices=service_prices)

    if pricing_recalculation_requested:
        pricing_services = selected_services or booking.services
        if not pricing_services:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Booking services are required")

        requested_prices = getattr(payload, "service_prices", None)
        if requested_prices is not None:
            service_prices = {item.service_id: item.price_amount for item in requested_prices}
            expected_service_ids = {item.id for item in pricing_services}
            if set(service_prices) != expected_service_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="service_prices must match booking services",
                )
        elif selected_services is not None:
            service_prices = {item.id: int(item.price) for item in pricing_services}
        else:
            service_prices = booking.service_prices

        if selected_services is None and requested_prices is not None:
            service_items_by_id = {item.service_id: item for item in booking.service_items}
            for service_id, price_amount in service_prices.items():
                service_items_by_id[service_id].price_amount = price_amount

        promotion_code = (
            getattr(payload, "promotion_code", None)
            if promotion_requested
            else booking.promotion_code
        )
        await service.promotion_service.apply_to_booking(
            session,
            booking=booking,
            promotion_code=promotion_code,
            customer=booking.customer,
            services=pricing_services,
            service_prices=service_prices,
            at=booking.start_at,
            allow_private_promotions=True,
        )
    await session.commit()
    schedule_waitlist_offer(
        background_tasks,
        original_slot
        if original_slot is not None
        and (original_start_at != booking.start_at or original_end_at != booking.end_at)
        else None,
    )
    booking = (
        await session.execute(
            select(Booking)
            .options(*booking_response_options())
            .where(Booking.id == booking_id)
        )
    ).scalar_one()
    return BookingBackofficeResponse.model_validate(booking)


@backoffice_router.delete("/bookings/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_booking(
    booking_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
    background_tasks: BackgroundTasks = None,
) -> None:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not current_user.is_superuser:
        master = await get_linked_master_for_user(session, current_user)
        if not booking_belongs_to_master(booking, master):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another master's booking")
    ensure_booking_editable(booking)
    freed_slot = (
        freed_slot_snapshot(booking, keep_source=False)
        if booking.status == BookingStatus.confirmed
        else None
    )
    await session.delete(booking)
    await session.commit()
    schedule_waitlist_offer(background_tasks, freed_slot)


@backoffice_router.get("/availability", response_model=list[MasterAvailabilityWindowResponse])
async def admin_list_availability(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    master_id: int | None = Query(default=None),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterAvailabilityWindowResponse]:
    ensure_superuser(current_user)
    start_at, end_at = service.ensure_valid_interval(date_from, date_to)
    stmt = (
        select(MasterAvailabilityWindow)
        .where(
            MasterAvailabilityWindow.start_at < end_at,
            MasterAvailabilityWindow.end_at > start_at,
        )
        .order_by(MasterAvailabilityWindow.start_at.asc())
    )
    if master_id is not None:
        master_id = await resolve_backoffice_calendar_master_id(session, master_id)
        stmt = stmt.where(MasterAvailabilityWindow.master_id == master_id)
    windows = (await session.execute(stmt)).scalars().all()
    return [MasterAvailabilityWindowResponse.model_validate(item) for item in windows]


@backoffice_router.get("/calendar-holds", response_model=list[CalendarHoldResponse])
async def admin_list_calendar_holds(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    master_id: int | None = Query(default=None),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[CalendarHoldResponse]:
    ensure_superuser(current_user)
    start_at, end_at = ensure_bounded_calendar_range(date_from, date_to)
    calendar_master_id = await resolve_backoffice_calendar_master_id(session, master_id)
    holds = await service.list_active_waitlist_holds(
        session,
        master_id=calendar_master_id,
        start_at=start_at,
        end_at=end_at,
    )
    return [CalendarHoldResponse.model_validate(item) for item in holds]


@backoffice_router.get("/calendar/bookings", response_model=list[BookingBackofficeResponse])
async def admin_list_calendar_bookings(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    master_id: int | None = Query(default=None),
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[BookingBackofficeResponse]:
    ensure_superuser(current_user)
    start_at, end_at = ensure_bounded_calendar_range(date_from, date_to)
    calendar_master_id = await resolve_backoffice_calendar_master_id(session, master_id)
    stmt = (
        select(Booking)
        .options(*booking_response_options())
        .where(Booking.start_at < end_at, Booking.end_at > start_at)
        .order_by(Booking.start_at.asc())
    )
    if calendar_master_id is not None:
        stmt = stmt.where(Booking.master_id == calendar_master_id)
    if booking_status is not None:
        stmt = stmt.where(Booking.status == booking_status)
    bookings = (await session.execute(stmt)).scalars().all()
    return [BookingBackofficeResponse.model_validate(item) for item in bookings]


@backoffice_router.get("/calendar/time-blocks", response_model=list[MasterTimeBlockResponse])
async def admin_list_calendar_time_blocks(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    master_id: int | None = Query(default=None),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterTimeBlockResponse]:
    ensure_superuser(current_user)
    start_at, end_at = ensure_bounded_calendar_range(date_from, date_to)
    calendar_master_id = await resolve_backoffice_calendar_master_id(session, master_id)
    stmt = (
        select(MasterTimeBlock)
        .where(MasterTimeBlock.start_at < end_at, MasterTimeBlock.end_at > start_at)
        .order_by(MasterTimeBlock.start_at.asc())
    )
    if calendar_master_id is not None:
        stmt = stmt.where(MasterTimeBlock.master_id == calendar_master_id)
    blocks = (await session.execute(stmt)).scalars().all()
    return [MasterTimeBlockResponse.model_validate(item) for item in blocks]


@backoffice_router.post(
    "/availability/days",
    response_model=list[MasterAvailabilityWindowResponse],
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_availability_days(
    payload: AdminMasterAvailabilityDaysCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterAvailabilityWindowResponse]:
    ensure_superuser(current_user)
    master = await session.get(Master, payload.master_id)
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    calendar_master = await resolve_calendar_master(session, master)
    windows = await service.create_availability_days(session, calendar_master, payload.dates)
    return [MasterAvailabilityWindowResponse.model_validate(item) for item in windows]


@backoffice_router.post(
    "/availability/windows",
    response_model=MasterAvailabilityWindowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_availability_window(
    payload: AdminMasterAvailabilityWindowCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterAvailabilityWindowResponse:
    ensure_superuser(current_user)
    master = await session.get(Master, payload.master_id)
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    calendar_master = await resolve_calendar_master(session, master)
    window = await service.create_availability_window(session, calendar_master, payload)
    return MasterAvailabilityWindowResponse.model_validate(window)


@backoffice_router.delete("/availability/{window_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_availability_window(
    window_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    ensure_superuser(current_user)
    window = await session.get(MasterAvailabilityWindow, window_id)
    if not window:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Availability window not found")
    await service.delete_availability_window(session, window, allow_booked=True)


@backoffice_router.get("/time-blocks", response_model=PaginatedResponse[MasterTimeBlockResponse])
async def admin_list_time_blocks(
    pagination: PaginationDep,
    master_id: int | None = Query(default=None),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MasterTimeBlockResponse]:
    ensure_superuser(current_user)
    stmt = select(MasterTimeBlock).order_by(MasterTimeBlock.start_at.asc())
    if master_id is not None:
        master_id = await resolve_backoffice_calendar_master_id(session, master_id)
        stmt = stmt.where(MasterTimeBlock.master_id == master_id)
    if date_from is not None:
        stmt = stmt.where(MasterTimeBlock.end_at > service.normalize_datetime(date_from))
    if date_to is not None:
        stmt = stmt.where(MasterTimeBlock.start_at < service.normalize_datetime(date_to))
    items, total = await BaseRepository(MasterTimeBlock).list(
        session,
        stmt=stmt,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return PaginatedResponse[MasterTimeBlockResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MasterTimeBlockResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("/time-blocks", response_model=MasterTimeBlockResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_time_block(
    payload: AdminMasterTimeBlockCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterTimeBlockResponse:
    ensure_superuser(current_user)
    master = await session.get(Master, payload.master_id)
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    calendar_master = await resolve_calendar_master(session, master)
    block = await service.create_time_block(session, calendar_master, payload)
    return MasterTimeBlockResponse.model_validate(block)


@backoffice_router.patch("/time-blocks/{block_id}", response_model=MasterTimeBlockResponse)
async def admin_update_time_block(
    block_id: int,
    payload: AdminMasterTimeBlockUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterTimeBlockResponse:
    ensure_superuser(current_user)
    block = await session.get(MasterTimeBlock, block_id)
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Time block not found")

    data = payload.model_dump(exclude_unset=True)
    if "master_id" in data:
        master = await session.get(Master, data["master_id"])
        if not master:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        calendar_master = await resolve_calendar_master(session, master)
        block.master_id = calendar_master.id

    start_at = data.get("start_at", block.start_at)
    end_at = data.get("end_at", block.end_at)
    start_at, end_at = service.ensure_valid_interval(start_at, end_at)
    block.start_at = start_at
    block.end_at = end_at
    if "reason" in data:
        block.reason = data["reason"]

    await session.commit()
    await session.refresh(block)
    return MasterTimeBlockResponse.model_validate(block)


@backoffice_router.delete("/time-blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_time_block(
    block_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    ensure_superuser(current_user)
    block = await session.get(MasterTimeBlock, block_id)
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Time block not found")
    await session.delete(block)
    await session.commit()
