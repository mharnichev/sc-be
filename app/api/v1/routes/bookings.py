from __future__ import annotations

from datetime import date, datetime, timedelta
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
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
from app.models.upload import Upload
from app.repositories.base import BaseRepository
from app.schemas.booking import (
    AdminMasterAvailabilityDaysCreate,
    AdminMasterAvailabilityWindowCreate,
    AdminMasterTimeBlockCreate,
    AdminMasterTimeBlockUpdate,
    AvailableSlotResponse,
    BookingBackofficeResponse,
    BookingResponse,
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
from app.services.email_notifications import NewBookingEmail, email_notification_service
from app.services.master_notifications import NewBookingTelegram, master_telegram_notification_service
from app.services.uploads import delete_upload_file, save_image_upload

public_router = APIRouter()
backoffice_router = APIRouter()
service = BookingServiceLayer()
master_repo = BaseRepository(Master)
base_service_repo = BaseRepository(BaseService)
barber_service_repo = BaseRepository(BarberService)


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
        "is_army_client": bool(service_payload_value(payload, base_service, "is_army_client")),
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
    for master in masters:
        master.services = [service for service in master.services if is_public_barber_service_active(service)]
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
    return [BarberServiceResponse.model_validate(item) for item in services]


def _catalog_key(service: BarberService) -> tuple[str, int | None, str, str | None, int, int, bool]:
    title_uk = getattr(service, "title_uk", None) or service.name
    title_en = getattr(service, "title_en", None)
    is_army_client = bool(getattr(service, "is_army_client", False))
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
        is_army_client,
    )


@public_router.get("/service-catalog", response_model=list[PublicServiceCatalogItem])
async def list_public_service_catalog(session: AsyncSession = Depends(get_db_session)) -> list[PublicServiceCatalogItem]:
    stmt = (
        select(BarberService)
        .options(selectinload(BarberService.base_service))
        .where(*public_barber_service_filter())
        .order_by(
            BarberService.name.asc(),
            BarberService.price.asc(),
            BarberService.duration_minutes.asc(),
            BarberService.id.asc(),
        )
    )
    services = (await session.execute(stmt)).scalars().all()
    services = [service for service in services if is_public_barber_service_active(service)]
    grouped: OrderedDict[
        tuple[str, int | None, str, str | None, int, int, bool],
        list[BarberService],
    ] = OrderedDict()
    for item in services:
        grouped.setdefault(_catalog_key(item), []).append(item)

    catalog: list[PublicServiceCatalogItem] = []
    for index, (
        (source_key, base_service_id, title_uk, title_en, duration_minutes, price, is_army_client),
        items,
    ) in enumerate(grouped.items(), start=1):
        source_type = "base" if base_service_id is not None else "custom"
        name = next((item.name for item in items if item.name), title_uk)
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
                is_army_client=is_army_client,
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
    current_user: AdminUser | None = Depends(get_optional_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BookingResponse:
    booking = await service.create_public_booking(
        session,
        payload,
        allow_past=bool(current_user and current_user.is_superuser),
    )
    booking = (
        await session.execute(
            select(Booking)
            .options(selectinload(Booking.master), *booking_response_options())
            .where(Booking.id == booking.id)
        )
    ).scalar_one()
    if should_send_booking_notifications(booking):
        service_name = ", ".join(item.name for item in booking.services) or booking.service.name
        background_tasks.add_task(
            email_notification_service.send_new_booking_to_master,
            NewBookingEmail(
                booking_id=booking.id,
                master_name=booking.master.full_name,
                master_email=booking.master.email,
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
                master_name=booking.master.full_name,
                telegram_chat_id=booking.master.telegram_chat_id,
                service_name=service_name,
                customer_name=booking.customer_name,
                customer_phone=booking.customer_phone,
                customer_comment=booking.customer_comment,
                start_at=booking.start_at,
                end_at=booking.end_at,
            ),
        )
    return BookingResponse.model_validate(booking)


@backoffice_router.get("/masters/me/calendar", response_model=list[BookingBackofficeResponse])
async def get_my_calendar(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[BookingBackofficeResponse]:
    start_at, end_at = service.ensure_valid_interval(date_from, date_to)
    stmt = (
        select(Booking)
        .options(*booking_response_options())
        .where(Booking.master_id == current_master.id, Booking.start_at < end_at, Booking.end_at > start_at)
        .order_by(Booking.start_at.asc())
    )
    bookings = (await session.execute(stmt)).scalars().all()
    return [BookingBackofficeResponse.model_validate(item) for item in bookings]


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
        .where(Booking.master_id == current_master.id)
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
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.master_id != current_master.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another master's booking")
    ensure_booking_editable(booking)
    apply_booking_status_update(booking, payload.status)
    await session.commit()
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
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.master_id != current_master.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another master's booking")
    ensure_booking_editable(booking)

    selected_services = None
    if payload.service_ids is not None:
        current_master = await service.get_active_master_with_services(session, current_master.id)
        selected_services = await service.get_active_services(session, payload.service_ids)
        service.ensure_master_provides_services(current_master, [item.id for item in selected_services])
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
    await service.ensure_booking_within_availability(session, current_master.id, start_at, end_at)
    await service.ensure_slot_available(session, current_master.id, start_at, end_at, exclude_booking_id=booking.id)

    booking.start_at = start_at
    booking.end_at = end_at
    if selected_services is not None:
        await service.update_booking_services(session, booking, selected_services)
    await session.commit()
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
) -> None:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.master_id != current_master.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another master's booking")
    ensure_booking_editable(booking)
    await session.delete(booking)
    await session.commit()


@backoffice_router.post("/masters/me/time-blocks", response_model=MasterTimeBlockResponse, status_code=status.HTTP_201_CREATED)
async def create_my_time_block(
    payload: MasterTimeBlockCreate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> MasterTimeBlockResponse:
    block = await service.create_time_block(session, current_master, payload)
    return MasterTimeBlockResponse.model_validate(block)


@backoffice_router.get("/masters/me/time-blocks", response_model=list[MasterTimeBlockResponse])
async def list_my_time_blocks(
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterTimeBlockResponse]:
    stmt = select(MasterTimeBlock).where(MasterTimeBlock.master_id == current_master.id).order_by(MasterTimeBlock.start_at.asc())
    blocks = (await session.execute(stmt)).scalars().all()
    return [MasterTimeBlockResponse.model_validate(item) for item in blocks]


@backoffice_router.delete("/masters/me/time-blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_time_block(
    block_id: int,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    block = await session.get(MasterTimeBlock, block_id)
    if not block:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Time block not found")
    if block.master_id != current_master.id:
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
    windows = await service.list_availability_windows(session, current_master.id, start_at, end_at)
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
    windows = await service.create_availability_days(session, current_master, payload.dates)
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
    window = await service.create_availability_window(session, current_master, payload)
    return MasterAvailabilityWindowResponse.model_validate(window)


@backoffice_router.delete("/masters/me/availability/{window_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_availability_window(
    window_id: int,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    window = await session.get(MasterAvailabilityWindow, window_id)
    if not window:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Availability window not found")
    if window.master_id != current_master.id:
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
    if "booking_redirect_master_id" in data:
        await ensure_booking_redirect_master_valid(
            session,
            source_master_id=master_id,
            redirect_master_id=data["booking_redirect_master_id"],
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
    if not current_user.is_superuser:
        linked_master = await get_linked_master_for_user(session, current_user)
        if master_id is not None and master_id != linked_master.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view another master's bookings")
        master_id = linked_master.id
    stmt = select(Booking).options(*booking_response_options()).order_by(Booking.start_at.asc())
    if master_id is not None:
        stmt = stmt.where(Booking.master_id == master_id)
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
    payload: PublicBookingCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BookingBackofficeResponse:
    ensure_superuser(current_user)
    booking = await service.create_public_booking(
        session,
        payload,
        allow_past=True,
        require_availability=False,
        require_working_hours=False,
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
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not current_user.is_superuser:
        master = await get_linked_master_for_user(session, current_user)
        if booking.master_id != master.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot update another master's booking")
    ensure_booking_editable(booking)
    apply_booking_status_update(booking, payload.status)
    await session.commit()
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
    payload: BookingUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BookingBackofficeResponse:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not current_user.is_superuser:
        master = await get_linked_master_for_user(session, current_user)
        if booking.master_id != master.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot update another master's booking")
    ensure_booking_editable(booking)

    selected_services = None
    if payload.service_ids is not None:
        master = await service.get_active_master_with_services(session, booking.master_id)
        selected_services = await service.get_active_services(session, payload.service_ids)
        service.ensure_master_provides_services(master, [item.id for item in selected_services])
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
        await service.update_booking_services(session, booking, selected_services)
    await session.commit()
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
) -> None:
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not current_user.is_superuser:
        master = await get_linked_master_for_user(session, current_user)
        if booking.master_id != master.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete another master's booking")
    ensure_booking_editable(booking)
    await session.delete(booking)
    await session.commit()


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
        stmt = stmt.where(MasterAvailabilityWindow.master_id == master_id)
    windows = (await session.execute(stmt)).scalars().all()
    return [MasterAvailabilityWindowResponse.model_validate(item) for item in windows]


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
    windows = await service.create_availability_days(session, master, payload.dates)
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
    window = await service.create_availability_window(session, master, payload)
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
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MasterTimeBlockResponse]:
    ensure_superuser(current_user)
    stmt = select(MasterTimeBlock).order_by(MasterTimeBlock.start_at.asc())
    if master_id is not None:
        stmt = stmt.where(MasterTimeBlock.master_id == master_id)
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
    block = await service.create_time_block(session, master, payload)
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
        block.master_id = data["master_id"]

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
