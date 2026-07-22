from __future__ import annotations

import enum

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin


class MasterReviewStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class MasterReview(TimestampMixin, Base):
    __tablename__ = "master_reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="master_reviews_rating_range"),
        UniqueConstraint("booking_id", name="uq_master_reviews_booking_id"),
        Index("ix_master_reviews_master_status_submitted", "master_id", "status", "submitted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MasterReviewStatus] = mapped_column(
        Enum(MasterReviewStatus),
        default=MasterReviewStatus.pending,
        nullable=False,
        index=True,
    )
    public_author_name: Mapped[str] = mapped_column(String(100), default="Verified client", nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    moderated_by: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    moderation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    booking = relationship("Booking")
    master = relationship("Master")
    customer = relationship("Customer")
    moderator = relationship("AdminUser")
    moderation_history = relationship(
        "MasterReviewModerationAudit",
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="MasterReviewModerationAudit.created_at",
    )


class MasterReviewModerationAudit(TimestampMixin, Base):
    __tablename__ = "master_review_moderation_audits"
    __table_args__ = (Index("ix_master_review_audits_review_created", "review_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("master_reviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    from_status: Mapped[MasterReviewStatus] = mapped_column(Enum(MasterReviewStatus), nullable=False)
    to_status: Mapped[MasterReviewStatus] = mapped_column(Enum(MasterReviewStatus), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    review = relationship("MasterReview", back_populates="moderation_history")
    actor = relationship("AdminUser")
