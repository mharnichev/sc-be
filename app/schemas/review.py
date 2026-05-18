from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class GoogleBusinessReviewer(BaseModel):
    display_name: str | None = None
    profile_photo_url: str | None = None
    is_anonymous: bool = False


class GoogleBusinessReviewReply(BaseModel):
    comment: str | None = None
    update_time: datetime | None = None


class GoogleBusinessReview(BaseModel):
    review_id: str
    name: str | None = None
    reviewer: GoogleBusinessReviewer | None = None
    star_rating: int | None = None
    comment: str | None = None
    translations: dict[str, str] | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None
    review_reply: GoogleBusinessReviewReply | None = None


class GoogleBusinessReviewsResponse(BaseModel):
    average_rating: float | None = None
    total_review_count: int = 0
    fetched_at: datetime | None = None
    cache_expires_at: datetime | None = None
    stale: bool = False
    items: list[GoogleBusinessReview]
