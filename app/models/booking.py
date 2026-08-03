from __future__ import annotations

import enum

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin
from app.models.promotion import Promotion
from app.models.upload import Upload


class BookingStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


class MasterPosition(str, enum.Enum):
    ambassador = "ambassador"
    senior_master = "senior_master"
    master = "master"


MASTER_POSITION_LABELS: dict[MasterPosition, dict[str, str]] = {
    MasterPosition.ambassador: {"uk": "Амбасадор", "en": "Ambassador"},
    MasterPosition.senior_master: {"uk": "Старший Майстер", "en": "Senior Master"},
    MasterPosition.master: {"uk": "Майстер", "en": "Master"},
}


class Master(TimestampMixin, Base):
    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        unique=True,
        nullable=True,
    )
    full_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    position: Mapped[MasterPosition] = mapped_column(
        Enum(MasterPosition),
        default=MasterPosition.master,
        nullable=False,
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    photo_upload_id: Mapped[int | None] = mapped_column(
        ForeignKey("uploads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    avatar_upload_id: Mapped[int | None] = mapped_column(
        ForeignKey("uploads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    booking_redirect_master_id: Mapped[int | None] = mapped_column(
        ForeignKey("masters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    show_on_master_block: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    admin_user = relationship("AdminUser")
    photo_upload = relationship(Upload, foreign_keys=[photo_upload_id])
    avatar_upload = relationship(Upload, foreign_keys=[avatar_upload_id])
    booking_redirect_master = relationship("Master", remote_side=[id], foreign_keys=[booking_redirect_master_id])
    services = relationship("BarberService", back_populates="master", cascade="all, delete-orphan")
    bookings = relationship("Booking", back_populates="master", foreign_keys="Booking.master_id")
    time_blocks = relationship("MasterTimeBlock", back_populates="master", cascade="all, delete-orphan")
    availability_windows = relationship("MasterAvailabilityWindow", back_populates="master", cascade="all, delete-orphan")

    @property
    def photo(self) -> Upload | None:
        return self.photo_upload

    @property
    def avatar(self) -> Upload | None:
        return self.avatar_upload

    @property
    def first_name_uk(self) -> str:
        return self.full_name

    @property
    def last_name_uk(self) -> str | None:
        return self.last_name

    @property
    def full_name_uk(self) -> str:
        return " ".join(part for part in (self.full_name, self.last_name) if part)

    @property
    def full_name_en(self) -> str | None:
        parts = [part for part in (self.first_name_en, self.last_name_en) if part]
        return " ".join(parts) if parts else None

    @property
    def position_uk(self) -> str:
        return MASTER_POSITION_LABELS[self.position or MasterPosition.master]["uk"]

    @property
    def position_en(self) -> str:
        return MASTER_POSITION_LABELS[self.position or MasterPosition.master]["en"]


class BaseService(TimestampMixin, Base):
    __tablename__ = "base_services"
    __table_args__ = (
        UniqueConstraint("name", name="uq_base_services_name"),
        Index("ix_base_services_popularity_sort", "popularity_rank", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    title_uk: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_uk: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    popularity_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    popularity_booking_count_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    popularity_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    barber_services = relationship("BarberService", back_populates="base_service")


class BarberService(TimestampMixin, Base):
    __tablename__ = "barber_services"

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), index=True)
    base_service_id: Mapped[int | None] = mapped_column(
        ForeignKey("base_services.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    title_uk: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_uk: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    master = relationship("Master", back_populates="services")
    base_service = relationship("BaseService", back_populates="barber_services")
    bookings = relationship("Booking", back_populates="service")
    booking_service_items = relationship("BookingServiceItem", back_populates="service")

    @property
    def barber_id(self) -> int:
        return self.master_id

    @property
    def source_type(self) -> str:
        return "base" if self.base_service_id is not None else "custom"


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="RESTRICT"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("barber_services.id", ondelete="RESTRICT"), index=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    redirected_from_master_id: Mapped[int | None] = mapped_column(
        ForeignKey("masters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_name: Mapped[str] = mapped_column(String(255))
    customer_phone: Mapped[str] = mapped_column(String(50))
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus),
        default=BookingStatus.confirmed,
        nullable=False,
        index=True,
    )
    sms_two_hour_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promotion_id: Mapped[int | None] = mapped_column(
        ForeignKey("promotions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    promotion_code_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    promotion_name_uk_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    promotion_name_en_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    promotion_discount_percent_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subtotal_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    promotion_discount_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_discount_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)

    master = relationship("Master", back_populates="bookings", foreign_keys=[master_id])
    redirected_from_master = relationship("Master", foreign_keys=[redirected_from_master_id])
    service = relationship("BarberService", back_populates="bookings")
    service_items = relationship(
        "BookingServiceItem",
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="BookingServiceItem.position",
    )
    customer = relationship("Customer", back_populates="bookings")
    promotion = relationship(Promotion, back_populates="bookings")

    @property
    def service_ids(self) -> list[int]:
        if self.service_items:
            return [item.service_id for item in self.service_items]
        return [self.service_id]

    @property
    def services(self) -> list[BarberService]:
        if self.service_items:
            return [item.service for item in self.service_items if item.service is not None]
        return [self.service] if self.service is not None else []

    @property
    def promotion_code(self) -> str | None:
        return self.promotion_code_snapshot

    @property
    def promotion_name_uk(self) -> str | None:
        return self.promotion_name_uk_snapshot

    @property
    def promotion_name_en(self) -> str | None:
        return self.promotion_name_en_snapshot

    @property
    def promotion_discount_percent(self) -> int | None:
        return self.promotion_discount_percent_snapshot

    @property
    def discount_amount(self) -> int | None:
        if self.promotion_discount_amount is None and self.manual_discount_amount is None:
            return None
        return int(self.promotion_discount_amount or 0) + int(self.manual_discount_amount or 0)


class BookingServiceItem(TimestampMixin, Base):
    __tablename__ = "booking_service_items"
    __table_args__ = (
        UniqueConstraint("booking_id", "service_id", name="uq_booking_service_items_booking_service"),
        UniqueConstraint("booking_id", "position", name="uq_booking_service_items_booking_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("barber_services.id", ondelete="RESTRICT"), index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    booking = relationship("Booking", back_populates="service_items")
    service = relationship("BarberService", back_populates="booking_service_items")


BookingService = BarberService


class MasterTimeBlock(TimestampMixin, Base):
    __tablename__ = "master_time_blocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    master = relationship("Master", back_populates="time_blocks")


class MasterAvailabilityWindow(TimestampMixin, Base):
    __tablename__ = "master_availability_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    master = relationship("Master", back_populates="availability_windows")


Index(
    "uq_barber_services_master_base_service",
    BarberService.master_id,
    BarberService.base_service_id,
    unique=True,
    postgresql_where=BarberService.base_service_id.is_not(None),
    sqlite_where=BarberService.base_service_id.is_not(None),
)
Index(
    "uq_barber_services_master_custom_name",
    BarberService.master_id,
    BarberService.name,
    unique=True,
    postgresql_where=BarberService.base_service_id.is_(None),
    sqlite_where=BarberService.base_service_id.is_(None),
)
