from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep
from app.models.blog import BlogSubscription, BlogSubscriptionEvent, BlogSubscriptionEventType, BlogSubscriptionStatus
from app.repositories.base import BaseRepository
from app.schemas.blog import (
    BlogSubscriptionAnalyticsResponse,
    BlogSubscriptionBackofficeResponse,
    BlogSubscriptionCreate,
    BlogSubscriptionEventBackofficeResponse,
    BlogSubscriptionPublicResponse,
    BlogSubscriptionUnsubscribeRequest,
)
from app.schemas.common import PaginatedResponse
from app.services.blog import BlogSubscriptionService

public_router = APIRouter()
backoffice_router = APIRouter()
repo = BaseRepository(BlogSubscription)
event_repo = BaseRepository(BlogSubscriptionEvent)
service = BlogSubscriptionService()


def _request_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", maxsplit=1)[0].strip()
    return request.client.host if request.client else None


def _public_response(subscription: BlogSubscription) -> BlogSubscriptionPublicResponse:
    return BlogSubscriptionPublicResponse(
        email=subscription.email,
        status=subscription.status,
        is_subscribed=subscription.status == BlogSubscriptionStatus.subscribed,
        subscribed_at=subscription.subscribed_at,
        unsubscribed_at=subscription.unsubscribed_at,
        unsubscribe_token=subscription.unsubscribe_token,
    )


@public_router.post("/subscriptions", response_model=BlogSubscriptionPublicResponse, status_code=status.HTTP_201_CREATED)
async def subscribe_to_blog(
    payload: BlogSubscriptionCreate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionPublicResponse:
    subscription = await service.subscribe(
        session,
        payload.model_dump(),
        subscriber_ip=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _public_response(subscription)


@public_router.post("/subscribe", response_model=BlogSubscriptionPublicResponse, status_code=status.HTTP_201_CREATED)
async def subscribe_to_blog_alias(
    payload: BlogSubscriptionCreate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionPublicResponse:
    return await subscribe_to_blog(payload, request, session)


@public_router.post("/unsubscribe", response_model=BlogSubscriptionPublicResponse)
async def unsubscribe_from_blog(
    payload: BlogSubscriptionUnsubscribeRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionPublicResponse:
    subscription = await service.unsubscribe(
        session,
        email=str(payload.email) if payload.email is not None else None,
        token=payload.token,
        reason=payload.reason,
        subscriber_ip=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _public_response(subscription)


@public_router.delete("/subscriptions", response_model=BlogSubscriptionPublicResponse)
async def delete_blog_subscription(
    payload: BlogSubscriptionUnsubscribeRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionPublicResponse:
    return await unsubscribe_from_blog(payload, request, session)


@public_router.delete("/subscriptions/{token}", response_model=BlogSubscriptionPublicResponse)
async def delete_blog_subscription_by_token(
    token: str,
    request: Request,
    reason: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionPublicResponse:
    subscription = await service.unsubscribe(
        session,
        token=token,
        reason=reason,
        subscriber_ip=_request_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return _public_response(subscription)


@public_router.get("/subscriptions/status", response_model=BlogSubscriptionPublicResponse)
async def get_blog_subscription_status(
    email: str | None = Query(default=None),
    token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionPublicResponse:
    subscription = await service.get_by_token(session, token) if token else None
    if subscription is None and email is not None:
        subscription = await service.get_by_email(session, email)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog subscription not found")
    return _public_response(subscription)


@backoffice_router.get("/subscriptions", response_model=PaginatedResponse[BlogSubscriptionBackofficeResponse])
async def list_blog_subscriptions(
    pagination: PaginationDep,
    status_filter: BlogSubscriptionStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None),
    source: str | None = Query(default=None),
    language: str | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BlogSubscriptionBackofficeResponse]:
    stmt = select(BlogSubscription).order_by(BlogSubscription.created_at.desc())
    if status_filter is not None:
        stmt = stmt.where(BlogSubscription.status == status_filter)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(BlogSubscription.email.ilike(like), BlogSubscription.name.ilike(like)))
    if source:
        stmt = stmt.where(BlogSubscription.source == source)
    if language:
        stmt = stmt.where(BlogSubscription.language == language)
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[BlogSubscriptionBackofficeResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/subscriptions/{subscription_id}", response_model=BlogSubscriptionBackofficeResponse)
async def get_blog_subscription(
    subscription_id: int,
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionBackofficeResponse:
    subscription = await repo.get(session, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blog subscription not found")
    return BlogSubscriptionBackofficeResponse.model_validate(subscription)


@backoffice_router.get("/statistics", response_model=BlogSubscriptionAnalyticsResponse)
async def get_blog_subscription_statistics(
    period_start: datetime | None = Query(default=None),
    period_end: datetime | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BlogSubscriptionAnalyticsResponse:
    return await service.analytics(session, period_start=period_start, period_end=period_end)


@backoffice_router.get("/events", response_model=PaginatedResponse[BlogSubscriptionEventBackofficeResponse])
async def list_blog_subscription_events(
    pagination: PaginationDep,
    subscription_id: int | None = Query(default=None),
    event_type: BlogSubscriptionEventType | None = Query(default=None),
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[BlogSubscriptionEventBackofficeResponse]:
    stmt = select(BlogSubscriptionEvent).order_by(BlogSubscriptionEvent.occurred_at.desc())
    if subscription_id is not None:
        stmt = stmt.where(BlogSubscriptionEvent.subscription_id == subscription_id)
    if event_type is not None:
        stmt = stmt.where(BlogSubscriptionEvent.event_type == event_type)
    items, total = await event_repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[BlogSubscriptionEventBackofficeResponse.model_validate(item) for item in items],
    )
