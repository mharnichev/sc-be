from __future__ import annotations

from datetime import date, datetime
from collections import OrderedDict

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.core.security import get_password_hash
from app.dependencies.auth import get_current_admin_user, get_current_master
from app.models.admin_user import AdminUser
from app.models.booking import BarberService, BaseService, Booking, BookingStatus, Master, MasterTimeBlock
from app.models.upload import Upload
from app.repositories.base import BaseRepository
from app.schemas.booking import (
    AdminMasterTimeBlockCreate,
    AvailableSlotResponse,
    BookingResponse,
    BarberServiceCreate,
    BarberServiceResponse,
    BarberServiceUpdate,
    BaseServiceCreate,
    BaseServiceResponse,
    BaseServiceUpdate,
    BookingStatusUpdate,
    MasterCreate,
    MasterResponse,
    MasterTimeBlockCreate,
    MasterTimeBlockResponse,
    MasterUpdate,
    PublicServiceCatalogBarberService,
    PublicServiceCatalogItem,
    PublicBookingCreate,
    SyncDefaultServicesResponse,
)
from app.dependencies.common import PaginationDep
from app.schemas.common import PaginatedResponse
from app.services.booking import KYIV_TZ, BookingServiceLayer
from app.services.uploads import delete_upload_file, save_image_upload

public_router = APIRouter()
backoffice_router = APIRouter()
service = BookingServiceLayer()
master_repo = BaseRepository(Master)
base_service_repo = BaseRepository(BaseService)
barber_service_repo = BaseRepository(BarberService)


def master_response_options():
    return (
        selectinload(Master.services).selectinload(BarberService.base_service),
        selectinload(Master.photo_upload),
        selectinload(Master.avatar_upload),
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


@public_router.get("/masters", response_model=list[MasterResponse])
async def list_public_masters(session: AsyncSession = Depends(get_db_session)) -> list[MasterResponse]:
    stmt = (
        select(Master)
        .options(*master_response_options())
        .where(Master.is_active.is_(True))
        .order_by(Master.full_name.asc())
    )
    masters = (await session.execute(stmt)).scalars().unique().all()
    return [MasterResponse.model_validate(master) for master in masters]


@public_router.get("/services", response_model=list[BarberServiceResponse])
async def list_public_services(session: AsyncSession = Depends(get_db_session)) -> list[BarberServiceResponse]:
    stmt = (
        select(BarberService)
        .options(selectinload(BarberService.base_service))
        .where(BarberService.is_active.is_(True))
        .order_by(BarberService.name.asc())
    )
    services = (await session.execute(stmt)).scalars().all()
    return [BarberServiceResponse.model_validate(item) for item in services]


def _catalog_key(service: BarberService) -> tuple[str, int | None, str, int, int]:
    if service.base_service_id is not None:
        source_key = f"base:{service.base_service_id}"
    else:
        source_key = f"custom:{service.name.strip().casefold()}"
    return source_key, service.base_service_id, service.name, service.duration_minutes, service.price


@public_router.get("/service-catalog", response_model=list[PublicServiceCatalogItem])
async def list_public_service_catalog(session: AsyncSession = Depends(get_db_session)) -> list[PublicServiceCatalogItem]:
    stmt = (
        select(BarberService)
        .options(selectinload(BarberService.base_service))
        .where(BarberService.is_active.is_(True))
        .order_by(BarberService.name.asc(), BarberService.price.asc(), BarberService.duration_minutes.asc(), BarberService.id.asc())
    )
    services = (await session.execute(stmt)).scalars().all()
    grouped: OrderedDict[tuple[str, int | None, str, int, int], list[BarberService]] = OrderedDict()
    for item in services:
        grouped.setdefault(_catalog_key(item), []).append(item)

    catalog: list[PublicServiceCatalogItem] = []
    for index, ((source_key, base_service_id, name, duration_minutes, price), items) in enumerate(grouped.items(), start=1):
        source_type = "base" if base_service_id is not None else "custom"
        catalog.append(
            PublicServiceCatalogItem(
                catalog_id=f"{source_key}:{duration_minutes}:{price}:{index}",
                base_service_id=base_service_id,
                source_type=source_type,
                name=name,
                description=next((item.description for item in items if item.description), None),
                duration_minutes=duration_minutes,
                price=price,
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
    service_id: int = Query(),
    session: AsyncSession = Depends(get_db_session),
) -> list[AvailableSlotResponse]:
    return await service.get_available_slots(session, master_id=master_id, service_id=service_id, target_date=date_)


@public_router.post("/bookings", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_public_booking(
    payload: PublicBookingCreate,
    session: AsyncSession = Depends(get_db_session),
) -> BookingResponse:
    booking = await service.create_public_booking(session, payload)
    return BookingResponse.model_validate(booking)


@backoffice_router.get("/masters/me/calendar", response_model=list[BookingResponse])
async def get_my_calendar(
    date_from: datetime = Query(),
    date_to: datetime = Query(),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[BookingResponse]:
    start_at, end_at = service.ensure_valid_interval(date_from, date_to)
    stmt = (
        select(Booking)
        .where(Booking.master_id == current_master.id, Booking.start_at < end_at, Booking.end_at > start_at)
        .order_by(Booking.start_at.asc())
    )
    bookings = (await session.execute(stmt)).scalars().all()
    return [BookingResponse.model_validate(item) for item in bookings]


@backoffice_router.get("/masters/me/bookings", response_model=list[BookingResponse])
async def list_my_bookings(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> list[BookingResponse]:
    stmt = select(Booking).where(Booking.master_id == current_master.id).order_by(Booking.start_at.asc())
    if date_from:
        stmt = stmt.where(Booking.end_at > service.normalize_datetime(date_from))
    if date_to:
        stmt = stmt.where(Booking.start_at < service.normalize_datetime(date_to))
    if booking_status:
        stmt = stmt.where(Booking.status == booking_status)
    bookings = (await session.execute(stmt)).scalars().all()
    return [BookingResponse.model_validate(item) for item in bookings]


@backoffice_router.patch("/masters/me/bookings/{booking_id}/status", response_model=BookingResponse)
async def update_my_booking_status(
    booking_id: int,
    payload: BookingStatusUpdate,
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> BookingResponse:
    if payload.status == BookingStatus.pending:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pending status is only used at creation")
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.master_id != current_master.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot modify another master's booking")
    booking.status = payload.status
    booking.cancelled_at = datetime.now(KYIV_TZ) if payload.status == BookingStatus.cancelled else None
    await session.commit()
    await session.refresh(booking)
    return BookingResponse.model_validate(booking)


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


@backoffice_router.get("/masters", response_model=PaginatedResponse[MasterResponse])
async def admin_list_masters(
    pagination: PaginationDep,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[MasterResponse]:
    ensure_superuser(current_user)
    stmt = (
        select(Master)
        .options(*master_response_options())
        .order_by(Master.created_at.desc())
    )
    items, total = await master_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[MasterResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[MasterResponse.model_validate(item) for item in items],
    )


@backoffice_router.post("/masters", response_model=MasterResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_master(
    payload: MasterCreate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterResponse:
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
    return MasterResponse.model_validate(master)


@backoffice_router.put("/masters/{master_id}", response_model=MasterResponse)
async def admin_update_master(
    master_id: int,
    payload: MasterUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterResponse:
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
    return MasterResponse.model_validate(master)


@backoffice_router.post("/masters/{master_id}/photo", response_model=MasterResponse)
async def admin_upload_master_photo(
    master_id: int,
    file: UploadFile = File(...),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterResponse:
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
    return MasterResponse.model_validate(master)


@backoffice_router.post("/masters/{master_id}/avatar", response_model=MasterResponse)
async def admin_upload_master_avatar(
    master_id: int,
    file: UploadFile = File(...),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterResponse:
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
    return MasterResponse.model_validate(master)


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
    item = BaseService(**payload.model_dump())
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
    updated = await base_service_repo.update(session, item, payload.model_dump(exclude_unset=True))
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
    name = payload.name if payload.name is not None else getattr(base_service, "name", None)
    duration_minutes = (
        payload.duration_minutes if payload.duration_minutes is not None else getattr(base_service, "duration_minutes", None)
    )
    price = payload.price if payload.price is not None else getattr(base_service, "price", None)
    description = payload.description if payload.description is not None else getattr(base_service, "description", None)
    await ensure_no_duplicate_barber_service(
        session,
        barber_id=barber_id,
        base_service_id=payload.base_service_id,
        name=name,
    )
    item = BarberService(
        master_id=barber_id,
        base_service_id=payload.base_service_id,
        name=name,
        duration_minutes=duration_minutes,
        price=price,
        description=description,
        is_active=payload.is_active,
    )
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
    data = payload.model_dump(exclude_unset=True)
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


@backoffice_router.get("/bookings", response_model=PaginatedResponse[BookingResponse])
async def admin_list_bookings(
    pagination: PaginationDep,
    master_id: int | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BookingResponse]:
    if not current_user.is_superuser:
        await get_linked_master_for_user(session, current_user)
    stmt = select(Booking).order_by(Booking.start_at.asc())
    if master_id is not None:
        stmt = stmt.where(Booking.master_id == master_id)
    if date_from is not None:
        stmt = stmt.where(Booking.end_at > service.normalize_datetime(date_from))
    if date_to is not None:
        stmt = stmt.where(Booking.start_at < service.normalize_datetime(date_to))
    if booking_status is not None:
        stmt = stmt.where(Booking.status == booking_status)
    items, total = await BaseRepository(Booking).list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[BookingResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[BookingResponse.model_validate(item) for item in items],
    )


@backoffice_router.patch("/bookings/{booking_id}/status", response_model=BookingResponse)
async def admin_update_booking_status(
    booking_id: int,
    payload: BookingStatusUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BookingResponse:
    if payload.status == BookingStatus.pending:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pending status is only used at creation")
    booking = await session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if not current_user.is_superuser:
        master = await get_linked_master_for_user(session, current_user)
        if booking.master_id != master.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot update another master's booking")
    booking.status = payload.status
    booking.cancelled_at = datetime.now(KYIV_TZ) if payload.status == BookingStatus.cancelled else None
    await session.commit()
    await session.refresh(booking)
    return BookingResponse.model_validate(booking)


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
