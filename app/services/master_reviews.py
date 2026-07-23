from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Literal

from fastapi import HTTPException, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.booking import Booking, BookingServiceItem, BookingStatus, Master
from app.models.master_review import MasterReview, MasterReviewModerationAudit, MasterReviewStatus
from app.models.messaging import (
    Campaign,
    CampaignStatus,
    CampaignType,
    MessageChannel,
    ReviewRequest,
    ReviewRequestEvent,
    ReviewRequestStatus,
)
from app.schemas.review import (
    AdminMasterReviewDetail,
    AdminMasterReviewListItem,
    MasterRatingSummary,
    MasterRatingStatistics,
    ModerationAuditResponse,
    PublicMasterReview,
    PublicReviewRequestContext,
    ReviewAutomationSettings,
    ReviewAutomationSettingsUpdate,
    ReviewMetricsResponse,
    ReviewMasterContext,
    ReviewRequestSettings,
    ReviewRequestSettingsUpdate,
    ReviewRequestEventResponse,
    ReviewSubmission,
    ReviewSubmissionResponse,
)
from app.services.booking import KYIV_TZ


UNAVAILABLE_REVIEW_REQUEST = "Review request is unavailable"
EXCLUSION_RULE_KEYS = {
    "master_id": "master_ids",
    "service_id": "service_ids",
    "customer_id": "customer_ids",
}
MAX_REVIEW_METRICS_RANGE_DAYS = 366


@dataclass(frozen=True)
class ApprovedReviewAggregate:
    average_rating: Decimal | None
    review_count: int


@dataclass(frozen=True)
class ReviewOperationalCounts:
    moderation_backlog: int
    failed_deliveries: int


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ)


def review_metrics_period_bounds(
    date_from: date | None,
    date_to: date | None,
) -> tuple[datetime | None, datetime | None]:
    if (date_from is None) != (date_to is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from and date_to must be provided together",
        )
    if date_from is None or date_to is None:
        return None, None
    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must not be after date_to",
        )
    inclusive_days = (date_to - date_from).days + 1
    if inclusive_days > MAX_REVIEW_METRICS_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Date range must not exceed {MAX_REVIEW_METRICS_RANGE_DAYS} inclusive days",
        )
    return (
        datetime.combine(date_from, time.min, tzinfo=KYIV_TZ),
        datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=KYIV_TZ),
    )


def review_token_hash(token: str) -> str:
    return hmac.new(settings.secret_key.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_review_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, review_token_hash(token)


def sanitize_review_comment(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    cleaned = "".join(
        character
        for character in normalized
        if character in {"\n", "\r", "\t"} or not unicodedata.category(character).startswith("C")
    ).strip()
    if not cleaned:
        return None
    if len(cleaned) > settings.review_comment_max_length:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Comment must be at most {settings.review_comment_max_length} characters",
        )
    return cleaned


def mask_customer_name(name: str | None) -> str:
    normalized = (name or "").strip()
    if not normalized:
        return "Verified client"
    return f"{normalized[0]}***"


def public_author_name(name: str | None) -> str:
    if not settings.review_public_author_names_enabled:
        return "Verified client"
    first_name = (name or "").strip().split(maxsplit=1)[0]
    return first_name[:100] or "Verified client"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=KYIV_TZ) if value.tzinfo is None else value.astimezone(KYIV_TZ)


def parse_exclusion_rules(values: Iterable[str]) -> dict[str, list[int]]:
    exclusions: dict[str, set[int]] = {value: set() for value in EXCLUSION_RULE_KEYS.values()}
    for raw_value in values:
        rule, separator, identifier = raw_value.strip().partition(":")
        if not separator or rule not in EXCLUSION_RULE_KEYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Exclusions must use master_id:ID, service_id:ID, or customer_id:ID",
            )
        try:
            parsed_identifier = int(identifier)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Exclusion IDs must be positive integers",
            ) from exc
        if parsed_identifier <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Exclusion IDs must be positive integers",
            )
        exclusions[EXCLUSION_RULE_KEYS[rule]].add(parsed_identifier)
    return {key: sorted(items) for key, items in exclusions.items() if items}


def format_exclusion_rules(exclusions: dict[str, object]) -> list[str]:
    rules: list[str] = []
    for rule, key in EXCLUSION_RULE_KEYS.items():
        values = exclusions.get(key)
        if not isinstance(values, list):
            continue
        rules.extend(f"{rule}:{identifier}" for identifier in sorted(_positive_integers(values)))
    return rules


def _positive_integers(values: Iterable[object]) -> set[int]:
    result: set[int] = set()
    for value in values:
        try:
            identifier = int(value)
        except (TypeError, ValueError):
            continue
        if identifier > 0:
            result.add(identifier)
    return result


class MasterReviewService:
    @staticmethod
    def master_context(master: Master) -> ReviewMasterContext:
        return ReviewMasterContext(
            id=master.id,
            full_name=master.full_name_uk,
            full_name_uk=master.full_name_uk,
            full_name_en=master.full_name_en,
            first_name_uk=master.first_name_uk,
            last_name_uk=master.last_name_uk,
        )

    async def get_request_by_token(self, session: AsyncSession, token: str) -> ReviewRequest:
        if not token or len(token) > 200:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNAVAILABLE_REVIEW_REQUEST)
        request_item = (
            await session.execute(
                select(ReviewRequest)
                .options(
                    selectinload(ReviewRequest.appointment)
                    .selectinload(Booking.service_items)
                    .selectinload(BookingServiceItem.service),
                    selectinload(ReviewRequest.appointment).selectinload(Booking.service),
                    selectinload(ReviewRequest.appointment).selectinload(Booking.customer),
                    selectinload(ReviewRequest.master),
                    selectinload(ReviewRequest.events),
                )
                .where(ReviewRequest.token_hash == review_token_hash(token))
            )
        ).scalar_one_or_none()
        if request_item is None or request_item.expires_at is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNAVAILABLE_REVIEW_REQUEST)
        if _aware(request_item.expires_at) <= now_kyiv():
            if request_item.status not in {ReviewRequestStatus.expired, ReviewRequestStatus.submitted}:
                self.transition_request(request_item, ReviewRequestStatus.expired, reason="token_expired")
                await session.commit()
            raise HTTPException(status_code=status.HTTP_410_GONE, detail=UNAVAILABLE_REVIEW_REQUEST)
        if request_item.status not in {
            ReviewRequestStatus.sent,
            ReviewRequestStatus.delivered,
            ReviewRequestStatus.submitted,
        }:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNAVAILABLE_REVIEW_REQUEST)
        return request_item

    async def public_request_context(
        self,
        session: AsyncSession,
        token: str,
        *,
        locale: Literal["uk", "en"] = "uk",
    ) -> PublicReviewRequestContext:
        request_item = await self.get_request_by_token(session, token)
        booking = request_item.appointment
        master = request_item.master or booking.master
        services = list(booking.services)
        if locale == "en":
            master_name = master.full_name_en or master.full_name_uk
            service_names = [item.title_en or item.title_uk or item.name for item in services]
        else:
            master_name = master.full_name_uk
            service_names = [item.title_uk or item.name for item in services]
        return PublicReviewRequestContext(
            state="submitted" if request_item.status == ReviewRequestStatus.submitted else "available",
            master_id=master.id,
            master_name=master_name,
            master_photo_url=master.photo_url or master.avatar_url,
            visit_date=booking.start_at,
            service_names=service_names,
            expires_at=request_item.expires_at,
        )

    async def submit(
        self,
        session: AsyncSession,
        token: str,
        payload: ReviewSubmission,
    ) -> ReviewSubmissionResponse:
        request_item = await self.get_request_by_token(session, token)
        if request_item.status == ReviewRequestStatus.submitted or request_item.review_id is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review was already submitted")
        booking = request_item.appointment
        if (
            booking.status != BookingStatus.completed
            or booking.customer_id is None
            or booking.customer_id != request_item.customer_id
            or booking.master_id != request_item.master_id
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNAVAILABLE_REVIEW_REQUEST)

        submitted_at = now_kyiv()
        review = MasterReview(
            booking_id=booking.id,
            master_id=booking.master_id,
            customer_id=booking.customer_id,
            rating=payload.rating,
            comment=sanitize_review_comment(payload.comment),
            status=MasterReviewStatus.pending,
            public_author_name=public_author_name(getattr(booking.customer, "name", None)),
            submitted_at=submitted_at,
        )
        session.add(review)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Review was already submitted") from exc
        request_item.review_id = review.id
        request_item.reviewed_at = submitted_at
        self.transition_request(request_item, ReviewRequestStatus.submitted)
        await session.commit()
        return ReviewSubmissionResponse(submitted_at=submitted_at)

    def transition_request(
        self,
        request_item: ReviewRequest,
        new_status: ReviewRequestStatus,
        *,
        channel: MessageChannel | None = None,
        reason: str | None = None,
    ) -> None:
        request_item.status = new_status
        request_item.failure_reason = reason if new_status == ReviewRequestStatus.failed else None
        request_item.events.append(
            ReviewRequestEvent(status=new_status, channel=channel or request_item.channel, reason=reason)
        )

    async def rating_summary(self, session: AsyncSession, master_id: int) -> MasterRatingSummary:
        if await session.get(Master, master_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
        approved_row = (
            await session.execute(
                select(func.avg(MasterReview.rating), func.count(MasterReview.id)).where(
                    MasterReview.master_id == master_id,
                    MasterReview.status == MasterReviewStatus.approved,
                )
            )
        ).one()
        pending_count = int(
            (
                await session.execute(
                    select(func.count(MasterReview.id)).where(
                        MasterReview.master_id == master_id,
                        MasterReview.status == MasterReviewStatus.pending,
                    )
                )
            ).scalar_one()
            or 0
        )
        distribution_rows = (
            await session.execute(
                select(MasterReview.rating, func.count(MasterReview.id))
                .where(
                    MasterReview.master_id == master_id,
                    MasterReview.status == MasterReviewStatus.approved,
                )
                .group_by(MasterReview.rating)
            )
        ).all()
        average = float(approved_row[0]) if approved_row[0] is not None else None
        return MasterRatingSummary(
            master_id=master_id,
            average_rating=round(average, 1) if average is not None else None,
            approved_review_count=int(approved_row[1] or 0),
            pending_review_count=pending_count,
            rating_distribution={rating: dict(distribution_rows).get(rating, 0) for rating in range(1, 6)},
        )

    async def rating_statistics(self, session: AsyncSession, master_id: int) -> MasterRatingStatistics:
        summary = await self.rating_summary(session, master_id)
        master = await session.get(Master, master_id)
        return MasterRatingStatistics(
            master_id=master_id,
            master=self.master_context(master) if master is not None else None,
            approved_average_rating=summary.average_rating,
            approved_review_count=summary.approved_review_count,
            pending_review_count=summary.pending_review_count,
            rating_distribution=summary.rating_distribution,
        )

    async def all_rating_statistics(self, session: AsyncSession) -> list[MasterRatingStatistics]:
        master_ids = (await session.execute(select(Master.id).order_by(Master.id.asc()))).scalars().all()
        return [await self.rating_statistics(session, master_id) for master_id in master_ids]

    async def approved_rating_aggregates(
        self,
        session: AsyncSession,
        master_ids: Iterable[int],
    ) -> dict[int, ApprovedReviewAggregate]:
        """Return approved-only rating aggregates in one query for dashboard consumers."""
        ids = sorted(_positive_integers(master_ids))
        if not ids:
            return {}
        rows = (
            await session.execute(
                select(
                    MasterReview.master_id,
                    func.avg(MasterReview.rating),
                    func.count(MasterReview.id),
                )
                .where(
                    MasterReview.master_id.in_(ids),
                    MasterReview.status == MasterReviewStatus.approved,
                )
                .group_by(MasterReview.master_id)
            )
        ).all()
        return {
            int(row[0]): ApprovedReviewAggregate(
                average_rating=(
                    Decimal(str(row[1])).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                    if row[1] is not None
                    else None
                ),
                review_count=int(row[2] or 0),
            )
            for row in rows
        }

    async def dashboard_operational_counts(
        self,
        session: AsyncSession,
        *,
        master_id: int | None = None,
    ) -> ReviewOperationalCounts:
        """Reuse review-domain statuses for dashboard signals without loading review content."""
        pending_stmt = select(func.count(MasterReview.id)).where(
            MasterReview.status == MasterReviewStatus.pending
        )
        failed_stmt = select(func.count(ReviewRequest.id)).where(
            ReviewRequest.status == ReviewRequestStatus.failed
        )
        if master_id is not None:
            pending_stmt = pending_stmt.where(MasterReview.master_id == master_id)
            failed_stmt = failed_stmt.where(ReviewRequest.master_id == master_id)
        row = (
            await session.execute(
                select(
                    pending_stmt.scalar_subquery(),
                    failed_stmt.scalar_subquery(),
                )
            )
        ).one()
        return ReviewOperationalCounts(
            moderation_backlog=int(row[0] or 0),
            failed_deliveries=int(row[1] or 0),
        )

    async def public_reviews(
        self,
        session: AsyncSession,
        master_id: int,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[PublicMasterReview], int]:
        filters = (MasterReview.master_id == master_id, MasterReview.status == MasterReviewStatus.approved)
        total = int((await session.execute(select(func.count(MasterReview.id)).where(*filters))).scalar_one() or 0)
        items = (
            await session.execute(
                select(MasterReview)
                .where(*filters)
                .order_by(MasterReview.published_at.desc(), MasterReview.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return [
            PublicMasterReview(
                id=item.id,
                rating=item.rating,
                comment=item.comment,
                author_name=item.public_author_name,
                published_at=item.published_at or item.submitted_at,
            )
            for item in items
        ], total

    async def admin_reviews(
        self,
        session: AsyncSession,
        *,
        page: int,
        page_size: int,
        review_status: MasterReviewStatus | None = None,
        master_id: int | None = None,
        rating: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        request_status: ReviewRequestStatus | None = None,
    ) -> tuple[list[AdminMasterReviewListItem], int]:
        filters = []
        if review_status is not None:
            filters.append(MasterReview.status == review_status)
        if master_id is not None:
            filters.append(MasterReview.master_id == master_id)
        if rating is not None:
            filters.append(MasterReview.rating == rating)
        if date_from is not None:
            filters.append(MasterReview.submitted_at >= date_from)
        if date_to is not None:
            filters.append(MasterReview.submitted_at <= date_to)
        stmt = select(MasterReview).options(
            selectinload(MasterReview.master),
            selectinload(MasterReview.customer),
        )
        count_stmt = select(func.count(MasterReview.id))
        if request_status is not None:
            stmt = stmt.join(ReviewRequest, ReviewRequest.review_id == MasterReview.id).where(
                ReviewRequest.status == request_status
            )
            count_stmt = count_stmt.join(ReviewRequest, ReviewRequest.review_id == MasterReview.id).where(
                ReviewRequest.status == request_status
            )
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)
        reviews = (
            await session.execute(
                stmt.order_by(MasterReview.submitted_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        requests = await self._requests_by_booking(session, [item.booking_id for item in reviews])
        return [self._admin_item(item, requests[item.booking_id]) for item in reviews], int(
            (await session.execute(count_stmt)).scalar_one() or 0
        )

    async def admin_review_detail(self, session: AsyncSession, review_id: int) -> AdminMasterReviewDetail:
        review = (
            await session.execute(
                select(MasterReview)
                .options(
                    selectinload(MasterReview.master),
                    selectinload(MasterReview.customer),
                    selectinload(MasterReview.moderation_history),
                )
                .where(MasterReview.id == review_id)
            )
        ).scalar_one_or_none()
        if review is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
        request_item = (
            await session.execute(
                select(ReviewRequest)
                .options(selectinload(ReviewRequest.events))
                .where(ReviewRequest.appointment_id == review.booking_id)
            )
        ).scalar_one()
        base = self._admin_item(review, request_item).model_dump()
        return AdminMasterReviewDetail(
            **base,
            moderation_reason=review.moderation_reason,
            published_at=review.published_at,
            request_scheduled_at=request_item.scheduled_at,
            request_sent_at=request_item.sent_at,
            request_delivered_at=request_item.delivered_at,
            request_expires_at=request_item.expires_at,
            request_failure_reason=request_item.failure_reason,
            moderation_history=[
                ModerationAuditResponse(
                    id=item.id,
                    from_status=item.from_status,
                    to_status=item.to_status,
                    action=item.to_status,
                    actor_id=item.actor_id,
                    actor_display_name=f"Admin #{item.actor_id}" if item.actor_id is not None else None,
                    reason=item.reason,
                    created_at=item.created_at,
                    occurred_at=item.created_at,
                )
                for item in review.moderation_history
            ],
            request_history=[
                ReviewRequestEventResponse(
                    id=item.id,
                    status=item.status,
                    state=item.status,
                    channel=item.channel,
                    reason=item.reason,
                    failure_reason=item.reason,
                    created_at=item.created_at,
                    occurred_at=item.created_at,
                )
                for item in request_item.events
            ],
        )

    async def moderate(
        self,
        session: AsyncSession,
        review_id: int,
        *,
        new_status: MasterReviewStatus,
        actor_id: int,
        reason: str | None,
    ) -> AdminMasterReviewDetail:
        if new_status not in {MasterReviewStatus.approved, MasterReviewStatus.rejected}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid moderation action")
        review = await session.get(MasterReview, review_id)
        if review is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
        previous = review.status
        moderated_at = now_kyiv()
        review.status = new_status
        review.moderated_at = moderated_at
        review.moderated_by = actor_id
        review.moderation_reason = sanitize_review_comment(reason)
        review.published_at = moderated_at if new_status == MasterReviewStatus.approved else None
        session.add(
            MasterReviewModerationAudit(
                review_id=review.id,
                actor_id=actor_id,
                from_status=previous,
                to_status=new_status,
                reason=review.moderation_reason,
            )
        )
        await session.commit()
        return await self.admin_review_detail(session, review_id)

    async def automation_campaign(self, session: AsyncSession) -> Campaign:
        campaign = (
            await session.execute(
                select(Campaign)
                .options(selectinload(Campaign.template))
                .where(Campaign.type == CampaignType.post_visit_review_request)
                .order_by(Campaign.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review automation campaign is not configured")
        return campaign

    async def automation_settings(self, session: AsyncSession) -> ReviewAutomationSettings:
        campaign = await self.automation_campaign(session)
        metadata = campaign.metadata_json or {}
        return ReviewAutomationSettings(
            enabled=campaign.status == CampaignStatus.active,
            delay_minutes=(
                campaign.review_delay_minutes
                if campaign.review_delay_minutes is not None
                else settings.review_request_delay_minutes
            ),
            primary_channel=MessageChannel(metadata.get("primary_channel", MessageChannel.telegram.value)),
            fallback_channel=(
                MessageChannel(metadata["fallback_channel"]) if metadata.get("fallback_channel") else None
            ),
            quiet_hours_from=str(metadata.get("quiet_hours_from") or settings.review_quiet_hours_from),
            quiet_hours_to=str(metadata.get("quiet_hours_to") or settings.review_quiet_hours_to),
            frequency_cap_days=int(metadata.get("frequency_cap_days", settings.review_frequency_cap_days)),
            submitted_frequency_cap_days=int(
                metadata.get(
                    "submitted_frequency_cap_days",
                    settings.review_submitted_frequency_cap_days,
                )
            ),
            exclusions=dict(metadata.get("exclusions") or {}),
            template_preview=(campaign.template.body if campaign.template else metadata.get("message_body")),
        )

    async def update_automation_settings(
        self,
        session: AsyncSession,
        payload: ReviewAutomationSettingsUpdate,
    ) -> ReviewAutomationSettings:
        campaign = await self.automation_campaign(session)
        data = {
            key: value
            for key, value in payload.model_dump(exclude_unset=True).items()
            if value is not None or key == "fallback_channel"
        }
        if "enabled" in data:
            campaign.status = CampaignStatus.active if data.pop("enabled") else CampaignStatus.paused
        if "delay_minutes" in data:
            campaign.review_delay_minutes = data.pop("delay_minutes")
        metadata = dict(campaign.metadata_json or {})
        for key, value in data.items():
            metadata[key] = value.value if isinstance(value, MessageChannel) else value
        campaign.metadata_json = metadata
        await session.commit()
        return await self.automation_settings(session)

    async def request_settings(self, session: AsyncSession) -> ReviewRequestSettings:
        campaign = await self.automation_campaign(session)
        metadata = dict(campaign.metadata_json or {})
        return ReviewRequestSettings(
            enabled=campaign.status == CampaignStatus.active,
            delay_minutes=(
                campaign.review_delay_minutes
                if campaign.review_delay_minutes is not None
                else settings.review_request_delay_minutes
            ),
            primary_channel="sms",
            sms_fallback_enabled=False,
            quiet_hours_enabled=bool(metadata.get("quiet_hours_enabled", True)),
            quiet_hours_from=str(metadata.get("quiet_hours_from") or settings.review_quiet_hours_from),
            quiet_hours_to=str(metadata.get("quiet_hours_to") or settings.review_quiet_hours_to),
            frequency_cap_count=1,
            frequency_cap_days=int(metadata.get("frequency_cap_days", settings.review_frequency_cap_days)),
            submitted_frequency_cap_days=int(
                metadata.get(
                    "submitted_frequency_cap_days",
                    settings.review_submitted_frequency_cap_days,
                )
            ),
            exclusions=format_exclusion_rules(dict(metadata.get("exclusions") or {})),
            template_preview=(campaign.template.body if campaign.template else str(metadata.get("message_body") or "")),
            updated_at=campaign.updated_at,
        )

    async def update_request_settings(
        self,
        session: AsyncSession,
        payload: ReviewRequestSettingsUpdate,
    ) -> ReviewRequestSettings:
        campaign = await self.automation_campaign(session)
        campaign.status = CampaignStatus.active if payload.enabled else CampaignStatus.paused
        campaign.review_delay_minutes = payload.delay_minutes
        metadata = dict(campaign.metadata_json or {})
        metadata.update(
            {
                "primary_channel": MessageChannel.sms.value,
                "fallback_channel": None,
                "quiet_hours_enabled": payload.quiet_hours_enabled,
                "quiet_hours_from": payload.quiet_hours_from,
                "quiet_hours_to": payload.quiet_hours_to,
                "frequency_cap_count": payload.frequency_cap_count,
                "frequency_cap_days": payload.frequency_cap_days,
                "submitted_frequency_cap_days": payload.submitted_frequency_cap_days,
                "exclusions": parse_exclusion_rules(payload.exclusions),
            }
        )
        campaign.metadata_json = metadata
        await session.commit()
        return await self.request_settings(session)

    async def metrics(
        self,
        session: AsyncSession,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        master_id: int | None = None,
    ) -> ReviewMetricsResponse:
        booking_filters = [Booking.status == BookingStatus.completed]
        if period_start is not None:
            booking_filters.append(Booking.start_at >= period_start)
        if period_end is not None:
            booking_filters.append(Booking.start_at < period_end)
        if master_id is not None:
            booking_filters.append(Booking.master_id == master_id)

        eligible = int(
            (
                await session.execute(
                    select(func.count(Booking.id)).where(
                        Booking.customer_id.is_not(None),
                        *booking_filters,
                    )
                )
            ).scalar_one()
            or 0
        )
        request_counts = dict(
            (
                await session.execute(
                    select(ReviewRequest.status, func.count(ReviewRequest.id))
                    .join(Booking, Booking.id == ReviewRequest.appointment_id)
                    .where(*booking_filters)
                    .group_by(ReviewRequest.status)
                )
            ).all()
        )
        request_history_row = (
            await session.execute(
                select(
                    func.count(ReviewRequest.id),
                    func.count(ReviewRequest.sent_at),
                    func.count(ReviewRequest.delivered_at),
                )
                .join(Booking, Booking.id == ReviewRequest.appointment_id)
                .where(*booking_filters)
            )
        ).one()
        review_row = (
            await session.execute(
                select(
                    func.count(MasterReview.id),
                    func.sum(case((MasterReview.status == MasterReviewStatus.approved, 1), else_=0)),
                    func.avg(case((MasterReview.status == MasterReviewStatus.approved, MasterReview.rating))),
                    func.avg(
                        case(
                            (
                                MasterReview.moderated_at.is_not(None),
                                func.extract("epoch", MasterReview.moderated_at - MasterReview.submitted_at) / 3600,
                            )
                        )
                    ),
                    func.sum(
                        case(
                            (
                                (MasterReview.status == MasterReviewStatus.pending) & (MasterReview.rating <= 2),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                )
                .join(Booking, Booking.id == MasterReview.booking_id)
                .where(*booking_filters)
            )
        ).one()
        submitted = int(review_row[0] or 0)
        sent_total = int(request_history_row[1] or 0)
        moderation_time_hours = round(float(review_row[3]), 2) if review_row[3] is not None else None
        approved = int(review_row[1] or 0)
        conversion_rate = round((submitted / sent_total * 100) if sent_total else 0.0, 2)
        rating_statistics = await self._rating_statistics_for_booking_scope(
            session,
            booking_filters=booking_filters,
        )
        response_date_from = period_start.astimezone(KYIV_TZ).date() if period_start is not None else None
        response_date_to = (
            period_end.astimezone(KYIV_TZ).date() - timedelta(days=1)
            if period_end is not None
            else None
        )
        cohort_definition = (
            "Completed bookings scheduled within the inclusive Europe/Kyiv date range; "
            "request and review funnel values use those same booking IDs."
            if response_date_from is not None
            else "All completed bookings; request and review funnel values use their associated booking IDs."
        )
        return ReviewMetricsResponse(
            date_from=response_date_from,
            date_to=response_date_to,
            cohort_definition=cohort_definition,
            eligible_completed_visits=eligible,
            scheduled=int(request_history_row[0] or 0),
            sent=sent_total,
            delivered=int(request_history_row[2] or 0),
            submitted=submitted,
            expired=int(request_counts.get(ReviewRequestStatus.expired, 0)),
            failed=int(request_counts.get(ReviewRequestStatus.failed, 0)),
            approved=approved,
            conversion_rate=conversion_rate,
            average_approved_rating=round(float(review_row[2]), 1) if review_row[2] is not None else None,
            moderation_time_hours=moderation_time_hours,
            low_rating_pending_count=int(review_row[4] or 0),
            requests_scheduled=int(request_history_row[0] or 0),
            requests_sent=sent_total,
            requests_delivered=int(request_history_row[2] or 0),
            review_form_opens=None,
            submitted_reviews=submitted,
            approved_reviews=approved,
            review_conversion_rate=conversion_rate,
            average_moderation_time_minutes=(
                round(moderation_time_hours * 60, 1) if moderation_time_hours is not None else None
            ),
            average_rating_by_master=rating_statistics,
        )

    async def _rating_statistics_for_booking_scope(
        self,
        session: AsyncSession,
        *,
        booking_filters: list[object],
    ) -> list[MasterRatingStatistics]:
        rating_rows = (
            await session.execute(
                select(
                    MasterReview.master_id,
                    MasterReview.status,
                    MasterReview.rating,
                    func.count(MasterReview.id),
                )
                .join(Booking, Booking.id == MasterReview.booking_id)
                .where(*booking_filters)
                .group_by(MasterReview.master_id, MasterReview.status, MasterReview.rating)
            )
        ).all()
        if not rating_rows:
            return []

        master_ids = sorted({int(row[0]) for row in rating_rows})
        masters = (
            await session.execute(
                select(Master).where(Master.id.in_(master_ids)).order_by(Master.id.asc())
            )
        ).scalars().all()
        master_by_id = {master.id: master for master in masters}
        aggregates: dict[int, dict[str, object]] = {}
        for raw_master_id, review_status, raw_rating, raw_count in rating_rows:
            current_master_id = int(raw_master_id)
            count = int(raw_count or 0)
            rating = int(raw_rating)
            aggregate = aggregates.setdefault(
                current_master_id,
                {
                    "approved_count": 0,
                    "approved_rating_total": 0,
                    "pending_count": 0,
                    "distribution": {},
                },
            )
            if review_status == MasterReviewStatus.approved:
                aggregate["approved_count"] = int(aggregate["approved_count"]) + count
                aggregate["approved_rating_total"] = int(aggregate["approved_rating_total"]) + rating * count
                distribution = aggregate["distribution"]
                if isinstance(distribution, dict):
                    distribution[rating] = count
            elif review_status == MasterReviewStatus.pending:
                aggregate["pending_count"] = int(aggregate["pending_count"]) + count

        result: list[MasterRatingStatistics] = []
        for current_master_id in master_ids:
            aggregate = aggregates[current_master_id]
            approved_count = int(aggregate["approved_count"])
            average_rating = (
                round(int(aggregate["approved_rating_total"]) / approved_count, 1)
                if approved_count
                else None
            )
            master = master_by_id.get(current_master_id)
            result.append(
                MasterRatingStatistics(
                    master_id=current_master_id,
                    master=self.master_context(master) if master is not None else None,
                    approved_average_rating=average_rating,
                    approved_review_count=approved_count,
                    pending_review_count=int(aggregate["pending_count"]),
                    rating_distribution={
                        rating: int(aggregate["distribution"].get(rating, 0))
                        for rating in range(1, 6)
                    },
                )
            )
        return result

    async def _requests_by_booking(
        self, session: AsyncSession, booking_ids: Iterable[int]
    ) -> dict[int, ReviewRequest]:
        ids = list(booking_ids)
        if not ids:
            return {}
        rows = (
            await session.execute(select(ReviewRequest).where(ReviewRequest.appointment_id.in_(ids)))
        ).scalars().all()
        return {item.appointment_id: item for item in rows}

    def _admin_item(self, review: MasterReview, request_item: ReviewRequest) -> AdminMasterReviewListItem:
        master = review.master
        return AdminMasterReviewListItem(
            id=review.id,
            booking_reference=f"SC-{review.booking_id}",
            master_id=review.master_id,
            master_name=master.full_name_uk,
            master=self.master_context(master),
            customer_display_name=mask_customer_name(getattr(review.customer, "name", None)),
            rating=review.rating,
            comment=review.comment,
            text=review.comment,
            status=review.status,
            moderation_status=review.status,
            submitted_at=review.submitted_at,
            moderated_at=review.moderated_at,
            request_status=request_item.status,
            request_state=request_item.status,
            request_channel=request_item.channel,
            requested_at=request_item.scheduled_at or request_item.created_at,
        )


master_review_service = MasterReviewService()
