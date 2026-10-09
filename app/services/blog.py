from __future__ import annotations

import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.blog import BlogBrevoSyncJob, BlogSubscription, BlogSubscriptionEvent, BlogSubscriptionEventType, BlogSubscriptionStatus
from app.schemas.blog import (
    BlogSubscriptionAnalyticsResponse,
    BlogSubscriptionDailyStats,
    BlogSubscriptionEventResponse,
    BlogSubscriptionLanguageStats,
    BlogSubscriptionReasonStats,
    BlogSubscriptionSourceStats,
)
from app.services.brevo import brevo_contact_sync_service


class BlogSubscriptionService:
    def normalize_email(self, email: str) -> str:
        return email.strip().lower()

    async def _enqueue_brevo_sync(self, session: AsyncSession, subscription_id: int) -> None:
        now = datetime.now(UTC)
        await session.execute(insert(BlogBrevoSyncJob).values(
            subscription_id=subscription_id, desired_version=1, synced_version=0,
            attempts=0, next_attempt_at=now,
        ).on_conflict_do_update(index_elements=[BlogBrevoSyncJob.subscription_id], set_={
            "desired_version": BlogBrevoSyncJob.desired_version + 1,
            "attempt_version": None, "attempts": 0, "last_attempt_at": None,
            "next_attempt_at": now, "last_error": None, "updated_at": now,
        }))

    async def subscribe(
        self,
        session: AsyncSession,
        data: dict[str, Any],
        *,
        subscriber_ip: str | None = None,
        user_agent: str | None = None,
    ) -> BlogSubscription:
        now = datetime.now(UTC)
        email = self.normalize_email(str(data["email"]))
        subscription = await self.get_by_email(session, email, lock=True)
        is_new_subscription = subscription is None

        if subscription is None:
            subscription = BlogSubscription(
                email=email,
                unsubscribe_token=self._new_unsubscribe_token(),
                first_subscribed_at=now,
                subscribed_at=now,
            )
            session.add(subscription)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                constraint_name = getattr(exc.orig.__cause__, "constraint_name", None)
                if getattr(exc.orig, "sqlstate", None) != "23505" or constraint_name != "uq_blog_subscriptions_email":
                    raise
                subscription = await self.get_by_email(session, email, lock=True)
                if subscription is None:
                    raise
                is_new_subscription = False

        was_unsubscribed = subscription.status == BlogSubscriptionStatus.unsubscribed
        if was_unsubscribed:
            proof = data.get("confirmation_token")
            if not isinstance(proof, str) or not secrets.compare_digest(proof.encode("utf-8"), subscription.unsubscribe_token.encode("utf-8")):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A valid personal confirmation token is required to resubscribe")

        subscription.status = BlogSubscriptionStatus.subscribed
        subscription.subscribed_at = now
        subscription.unsubscribed_at = None
        subscription.unsubscribe_reason = None
        subscription.subscriber_ip = subscriber_ip
        subscription.user_agent = user_agent
        for field in (
            "name",
            "source",
            "language",
            "referrer",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "metadata_json",
        ):
            value = data.get(field)
            if value is not None:
                setattr(subscription, field, value)

        await session.flush()
        if is_new_subscription or was_unsubscribed:
            session.add(
                BlogSubscriptionEvent(
                    subscription_id=subscription.id,
                    event_type=BlogSubscriptionEventType.resubscribed
                    if was_unsubscribed
                    else BlogSubscriptionEventType.subscribed,
                    source=subscription.source,
                    occurred_at=now,
                    subscriber_ip=subscriber_ip,
                    user_agent=user_agent,
                    metadata_json={
                        "utm_source": subscription.utm_source,
                        "utm_medium": subscription.utm_medium,
                        "utm_campaign": subscription.utm_campaign,
                        "referrer": subscription.referrer,
                    },
                )
            )
        await self._enqueue_brevo_sync(session, subscription.id)
        await session.commit()
        await brevo_contact_sync_service.sync_subscribed_contact(subscription)
        await session.refresh(subscription)
        return subscription

    async def unsubscribe(
        self,
        session: AsyncSession,
        *,
        email: str | None = None,
        token: str | None = None,
        reason: str | None = None,
        subscriber_ip: str | None = None,
        user_agent: str | None = None,
    ) -> BlogSubscription:
        if not token:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A personal unsubscribe token is required")
        subscription = await self.get_by_token(session, token, lock=True)
        if subscription is not None and email is not None and subscription.email != self.normalize_email(email):
            subscription = None
        if subscription is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog subscription not found")

        if subscription.status != BlogSubscriptionStatus.unsubscribed:
            subscription.status = BlogSubscriptionStatus.unsubscribed
            subscription.unsubscribed_at = datetime.now(UTC)
            subscription.unsubscribe_reason = reason
            session.add(
                BlogSubscriptionEvent(
                    subscription_id=subscription.id,
                    event_type=BlogSubscriptionEventType.unsubscribed,
                    source=subscription.source,
                    occurred_at=subscription.unsubscribed_at,
                    subscriber_ip=subscriber_ip,
                    user_agent=user_agent,
                    metadata_json={"reason": reason} if reason else {},
                )
            )
        elif reason and not subscription.unsubscribe_reason:
            subscription.unsubscribe_reason = reason

        await self._enqueue_brevo_sync(session, subscription.id)
        await session.commit()
        await brevo_contact_sync_service.sync_unsubscribed_contact(subscription)
        await session.refresh(subscription)
        return subscription

    async def get_by_email(self, session: AsyncSession, email: str, *, lock: bool = False) -> BlogSubscription | None:
        statement = select(BlogSubscription).where(BlogSubscription.email == self.normalize_email(email))
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await session.execute(statement)).scalar_one_or_none()

    async def get_by_token(self, session: AsyncSession, token: str | None, *, lock: bool = False) -> BlogSubscription | None:
        if not token:
            return None
        statement = select(BlogSubscription).where(BlogSubscription.unsubscribe_token == token)
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return (await session.execute(statement)).scalar_one_or_none()

    async def analytics(
        self,
        session: AsyncSession,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> BlogSubscriptionAnalyticsResponse:
        now = datetime.now(UTC)
        period_end = period_end or now
        period_start = period_start or period_end - timedelta(days=30)
        if period_start.utcoffset() is None or period_end.utcoffset() is None:
            raise HTTPException(status_code=422, detail="Analytics timestamps must include a timezone")
        if period_start > period_end:
            raise HTTPException(status_code=422, detail="period_start must not be after period_end")
        kyiv = ZoneInfo("Europe/Kyiv")
        if (period_end.astimezone(kyiv).date() - period_start.astimezone(kyiv).date()).days + 1 > 366:
            raise HTTPException(status_code=422, detail="Analytics period must not exceed 366 inclusive calendar days")

        total_subscribers = await self._count_subscriptions(session)
        active_subscribers = await self._count_subscriptions(session, BlogSubscriptionStatus.subscribed)
        unsubscribed_subscribers = await self._count_subscriptions(session, BlogSubscriptionStatus.unsubscribed)

        event_rows = (
            await session.execute(
                select(BlogSubscriptionEvent.event_type, func.count())
                .where(BlogSubscriptionEvent.occurred_at >= period_start, BlogSubscriptionEvent.occurred_at <= period_end)
                .group_by(BlogSubscriptionEvent.event_type)
            )
        ).all()
        event_counts = {event_type: count for event_type, count in event_rows}
        subscribe_events = int(
            event_counts.get(BlogSubscriptionEventType.subscribed, 0)
            + event_counts.get(BlogSubscriptionEventType.resubscribed, 0)
        )
        unsubscribe_events = int(event_counts.get(BlogSubscriptionEventType.unsubscribed, 0))

        return BlogSubscriptionAnalyticsResponse(
            period_start=period_start,
            period_end=period_end,
            total_subscribers=total_subscribers,
            active_subscribers=active_subscribers,
            unsubscribed_subscribers=unsubscribed_subscribers,
            subscribe_events=subscribe_events,
            unsubscribe_events=unsubscribe_events,
            net_growth=subscribe_events - unsubscribe_events,
            unsubscribe_rate=round(unsubscribe_events / subscribe_events, 4) if subscribe_events else 0.0,
            events=[
                BlogSubscriptionEventResponse(event_type=event_type, count=int(count))
                for event_type, count in event_rows
            ],
            by_date=await self._daily_stats(session, period_start, period_end),
            by_source=await self._source_stats(session, period_start, period_end),
            by_language=await self._language_stats(session),
            unsubscribe_reasons=await self._unsubscribe_reasons(session),
            current_subscriber_counts={"total": total_subscribers, "active": active_subscribers, "unsubscribed": unsubscribed_subscribers},
            period_event_counts={event_type.value: int(count) for event_type, count in event_rows},
            metric_definitions={
                "current_subscriber_counts": "Current subscription state, not a historical period-end snapshot",
                "period_event_counts": "Events within the inclusive timestamp bounds; repeated state changes are separate events",
                "unsubscribe_rate": "unsubscribe_events / (subscribed + resubscribed events), or 0 when denominator is 0; event ratio, not cohort churn",
                "by_date": "Europe/Kyiv calendar date of each event",
                "by_source": "active_subscribers is current state; subscribe_events and unsubscribe_events are period events",
                "by_language_and_unsubscribe_reasons": "Current subscription state, independent of selected event period",
            },
        )

    async def _count_subscriptions(
        self,
        session: AsyncSession,
        status_filter: BlogSubscriptionStatus | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(BlogSubscription)
        if status_filter is not None:
            stmt = stmt.where(BlogSubscription.status == status_filter)
        return int((await session.execute(stmt)).scalar_one())

    async def _daily_stats(
        self,
        session: AsyncSession,
        period_start: datetime,
        period_end: datetime,
    ) -> list[BlogSubscriptionDailyStats]:
        calendar_date = func.date(func.timezone("Europe/Kyiv", BlogSubscriptionEvent.occurred_at))
        rows = (
            await session.execute(
                select(calendar_date, BlogSubscriptionEvent.event_type, func.count())
                .where(BlogSubscriptionEvent.occurred_at >= period_start, BlogSubscriptionEvent.occurred_at <= period_end)
                .group_by(calendar_date, BlogSubscriptionEvent.event_type)
                .order_by(calendar_date)
            )
        ).all()
        grouped: dict[str, dict[str, int]] = defaultdict(lambda: {"subscribed": 0, "unsubscribed": 0})
        for raw_date, event_type, count in rows:
            key = str(raw_date)
            if event_type in {BlogSubscriptionEventType.subscribed, BlogSubscriptionEventType.resubscribed}:
                grouped[key]["subscribed"] += int(count)
            elif event_type == BlogSubscriptionEventType.unsubscribed:
                grouped[key]["unsubscribed"] += int(count)
        return [
            BlogSubscriptionDailyStats(
                date=date,
                subscribed=counts["subscribed"],
                unsubscribed=counts["unsubscribed"],
                net_growth=counts["subscribed"] - counts["unsubscribed"],
            )
            for date, counts in grouped.items()
        ]

    async def _source_stats(
        self,
        session: AsyncSession,
        period_start: datetime,
        period_end: datetime,
    ) -> list[BlogSubscriptionSourceStats]:
        subscription_source = func.coalesce(BlogSubscription.source, "unknown")
        active_rows = (
            await session.execute(
                select(subscription_source, func.count())
                .where(BlogSubscription.status == BlogSubscriptionStatus.subscribed)
                .group_by(subscription_source)
            )
        ).all()
        event_source = func.coalesce(BlogSubscriptionEvent.source, "unknown")
        event_rows = (
            await session.execute(
                select(event_source, BlogSubscriptionEvent.event_type, func.count())
                .where(BlogSubscriptionEvent.occurred_at >= period_start, BlogSubscriptionEvent.occurred_at <= period_end)
                .group_by(event_source, BlogSubscriptionEvent.event_type)
            )
        ).all()

        sources: dict[str, dict[str, int]] = defaultdict(
            lambda: {"active_subscribers": 0, "subscribe_events": 0, "unsubscribe_events": 0}
        )
        for source, count in active_rows:
            sources[str(source)]["active_subscribers"] = int(count)
        for source, event_type, count in event_rows:
            key = str(source)
            if event_type in {BlogSubscriptionEventType.subscribed, BlogSubscriptionEventType.resubscribed}:
                sources[key]["subscribe_events"] += int(count)
            elif event_type == BlogSubscriptionEventType.unsubscribed:
                sources[key]["unsubscribe_events"] += int(count)
        return [
            BlogSubscriptionSourceStats(source=source, **counts)
            for source, counts in sorted(sources.items(), key=lambda item: item[1]["active_subscribers"], reverse=True)
        ]

    async def _language_stats(self, session: AsyncSession) -> list[BlogSubscriptionLanguageStats]:
        subscription_language = func.coalesce(BlogSubscription.language, "unknown")
        rows = (
            await session.execute(
                select(subscription_language, func.count())
                .where(BlogSubscription.status == BlogSubscriptionStatus.subscribed)
                .group_by(subscription_language)
                .order_by(func.count().desc())
            )
        ).all()
        return [
            BlogSubscriptionLanguageStats(language=str(language), active_subscribers=int(count))
            for language, count in rows
        ]

    async def _unsubscribe_reasons(self, session: AsyncSession) -> list[BlogSubscriptionReasonStats]:
        unsubscribe_reason = func.coalesce(BlogSubscription.unsubscribe_reason, "unknown")
        rows = (
            await session.execute(
                select(unsubscribe_reason, func.count())
                .where(BlogSubscription.status == BlogSubscriptionStatus.unsubscribed)
                .group_by(unsubscribe_reason)
                .order_by(func.count().desc())
            )
        ).all()
        return [BlogSubscriptionReasonStats(reason=str(reason), count=int(count)) for reason, count in rows]

    def _new_unsubscribe_token(self) -> str:
        return secrets.token_urlsafe(32)
