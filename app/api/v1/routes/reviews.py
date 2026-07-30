from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db_session
from app.core.rate_limit import privacy_safe_rate_key, review_rate_limiter
from app.dependencies.auth import get_current_admin_user, get_current_master
from app.dependencies.common import PaginationDep
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.master_review import MasterReviewStatus
from app.models.messaging import ReviewRequestStatus
from app.schemas.review import (
    AdminMasterReviewDetail,
    AdminMasterReviewsResponse,
    GoogleBusinessReviewsResponse,
    MasterRatingSummary,
    MasterRatingStatistics,
    PublicMasterReviewsResponse,
    PublicReviewRequestContext,
    ReviewAutomationSettings,
    ReviewAutomationSettingsUpdate,
    ReviewMetricsResponse,
    ReviewModerationRequest,
    ReviewRequestSettings,
    ReviewRequestSettingsUpdate,
    ReviewSubmission,
    ReviewSubmissionResponse,
)
from app.services.google_business_reviews import GoogleBusinessReviewsError, GoogleBusinessReviewsService
from app.services.booking import KYIV_TZ
from app.services.master_reviews import master_review_service, review_metrics_period_bounds

public_router = APIRouter()
backoffice_router = APIRouter()
service = GoogleBusinessReviewsService()


def prevent_private_review_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


def ensure_review_admin(current_user: AdminUser) -> None:
    if not current_user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can moderate reviews")


@public_router.get("/request", response_model=PublicReviewRequestContext)
async def validate_review_request(
    request: Request,
    response: Response,
    token: str = Header(..., alias="X-Review-Token", min_length=20, max_length=200),
    locale: Literal["uk", "en"] = Query(default="uk"),
    session: AsyncSession = Depends(get_db_session),
) -> PublicReviewRequestContext:
    prevent_private_review_caching(response)
    review_rate_limiter.check(
        privacy_safe_rate_key(request, "", "validate-ip"),
        limit=settings.review_validation_rate_limit * 2,
    )
    review_rate_limiter.check(
        privacy_safe_rate_key(request, token, "validate"),
        limit=settings.review_validation_rate_limit,
    )
    return await master_review_service.public_request_context(session, token, locale=locale)


@public_router.post("/request", response_model=ReviewSubmissionResponse, status_code=status.HTTP_201_CREATED)
async def submit_review(
    payload: ReviewSubmission,
    request: Request,
    response: Response,
    token: str = Header(..., alias="X-Review-Token", min_length=20, max_length=200),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewSubmissionResponse:
    prevent_private_review_caching(response)
    review_rate_limiter.check(
        privacy_safe_rate_key(request, "", "submit-ip"),
        limit=settings.review_submission_rate_limit * 2,
    )
    review_rate_limiter.check(
        privacy_safe_rate_key(request, token, "submit"),
        limit=settings.review_submission_rate_limit,
    )
    return await master_review_service.submit(session, token, payload)


@public_router.post("/request/open", status_code=status.HTTP_204_NO_CONTENT)
async def record_review_form_open(
    request: Request,
    response: Response,
    token: str = Header(..., alias="X-Review-Token", min_length=20, max_length=200),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    prevent_private_review_caching(response)
    review_rate_limiter.check(
        privacy_safe_rate_key(request, "", "form-open-ip"),
        limit=settings.review_validation_rate_limit * 2,
    )
    review_rate_limiter.check(
        privacy_safe_rate_key(request, token, "form-open"),
        limit=settings.review_validation_rate_limit,
    )
    await master_review_service.record_form_open(session, token)


@public_router.get("/masters/{master_id}/summary", response_model=MasterRatingSummary)
async def get_public_master_rating_summary(
    master_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> MasterRatingSummary:
    return await master_review_service.rating_summary(session, master_id)


@public_router.get("/masters/{master_id}", response_model=PublicMasterReviewsResponse)
async def get_public_master_reviews(
    master_id: int,
    pagination: PaginationDep,
    session: AsyncSession = Depends(get_db_session),
) -> PublicMasterReviewsResponse:
    items, total = await master_review_service.public_reviews(
        session,
        master_id,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return PublicMasterReviewsResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=items,
    )


@public_router.get("", response_model=GoogleBusinessReviewsResponse)
async def list_reviews(
    session: AsyncSession = Depends(get_db_session),
) -> GoogleBusinessReviewsResponse:
    try:
        return await service.get_reviews(session)
    except GoogleBusinessReviewsError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@backoffice_router.post("/refresh", response_model=GoogleBusinessReviewsResponse)
async def refresh_reviews(
    _: object = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> GoogleBusinessReviewsResponse:
    try:
        return await service.get_reviews(session, force_refresh=True)
    except GoogleBusinessReviewsError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@backoffice_router.get("", response_model=AdminMasterReviewsResponse)
async def list_master_reviews(
    pagination: PaginationDep,
    review_status: MasterReviewStatus | None = Query(default=None, alias="status"),
    moderation_status: MasterReviewStatus | None = Query(default=None),
    master_id: int | None = Query(default=None),
    rating: int | None = Query(default=None, ge=1, le=5),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    request_status: ReviewRequestStatus | None = Query(default=None),
    request_state: ReviewRequestStatus | None = Query(default=None),
    submitted_from: date | None = Query(default=None),
    submitted_to: date | None = Query(default=None),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminMasterReviewsResponse:
    ensure_review_admin(current_user)
    items, total = await master_review_service.admin_reviews(
        session,
        page=pagination.page,
        page_size=pagination.page_size,
        review_status=review_status or moderation_status,
        master_id=master_id,
        rating=rating,
        date_from=date_from or (datetime.combine(submitted_from, time.min, tzinfo=KYIV_TZ) if submitted_from else None),
        date_to=date_to or (datetime.combine(submitted_to, time.max, tzinfo=KYIV_TZ) if submitted_to else None),
        request_status=request_status or request_state,
    )
    return AdminMasterReviewsResponse(
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=items,
    )


@backoffice_router.get(
    "/metrics",
    response_model=ReviewMetricsResponse,
    summary="Review funnel metrics",
    description=(
        "Admin-only review funnel. When date_from/date_to are supplied, all values use the cohort of bookings "
        "scheduled within the inclusive Europe/Kyiv calendar-date range. Both dates are required together."
    ),
    responses={
        403: {"description": "The authenticated Backoffice user is not a superuser."},
        422: {"description": "The optional date range is incomplete, invalid or exceeds 366 inclusive days."},
    },
)
async def get_review_metrics(
    date_from: date | None = Query(
        default=None,
        description="First included Europe/Kyiv booking calendar date.",
        examples=["2026-06-01"],
    ),
    date_to: date | None = Query(
        default=None,
        description="Last included Europe/Kyiv booking calendar date.",
        examples=["2026-06-30"],
    ),
    master_id: int | None = Query(
        default=None,
        ge=1,
        description="Optionally limit the booking cohort and review metrics to one master.",
    ),
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewMetricsResponse:
    ensure_review_admin(current_user)
    period_start, period_end = review_metrics_period_bounds(date_from, date_to)
    return await master_review_service.metrics(
        session,
        period_start=period_start,
        period_end=period_end,
        master_id=master_id,
    )


@backoffice_router.get("/automation/settings", response_model=ReviewAutomationSettings)
async def get_review_automation_settings(
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewAutomationSettings:
    ensure_review_admin(current_user)
    return await master_review_service.automation_settings(session)


@backoffice_router.patch("/automation/settings", response_model=ReviewAutomationSettings)
async def patch_review_automation_settings(
    payload: ReviewAutomationSettingsUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewAutomationSettings:
    ensure_review_admin(current_user)
    return await master_review_service.update_automation_settings(session, payload)


@backoffice_router.get("/request-settings", response_model=ReviewRequestSettings)
async def get_review_request_settings(
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewRequestSettings:
    ensure_review_admin(current_user)
    return await master_review_service.request_settings(session)


@backoffice_router.patch("/request-settings", response_model=ReviewRequestSettings)
async def patch_review_request_settings(
    payload: ReviewRequestSettingsUpdate,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewRequestSettings:
    ensure_review_admin(current_user)
    return await master_review_service.update_request_settings(session, payload)


@backoffice_router.get("/masters/me/summary", response_model=MasterRatingSummary)
async def get_my_master_review_summary(
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> MasterRatingSummary:
    return await master_review_service.rating_summary(session, current_master.id)


@backoffice_router.get("/masters/me/statistics", response_model=MasterRatingStatistics)
async def get_my_master_review_statistics(
    current_master: Master = Depends(get_current_master),
    session: AsyncSession = Depends(get_db_session),
) -> MasterRatingStatistics:
    return await master_review_service.rating_statistics(session, current_master.id)


@backoffice_router.get("/masters/statistics", response_model=list[MasterRatingStatistics])
async def get_all_master_review_statistics(
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[MasterRatingStatistics]:
    ensure_review_admin(current_user)
    return await master_review_service.all_rating_statistics(session)


@backoffice_router.get("/masters/{master_id}/summary", response_model=MasterRatingSummary)
async def get_admin_master_review_summary(
    master_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterRatingSummary:
    if not current_user.is_superuser:
        master = await get_current_master(current_user, session)
        if master.id != master_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view another master's ratings")
    return await master_review_service.rating_summary(session, master_id)


@backoffice_router.get("/masters/{master_id}/statistics", response_model=MasterRatingStatistics)
async def get_admin_master_review_statistics(
    master_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> MasterRatingStatistics:
    if not current_user.is_superuser:
        master = await get_current_master(current_user, session)
        if master.id != master_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot view another master's ratings")
    return await master_review_service.rating_statistics(session, master_id)


@backoffice_router.get("/{review_id}", response_model=AdminMasterReviewDetail)
async def get_master_review_detail(
    review_id: int,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminMasterReviewDetail:
    ensure_review_admin(current_user)
    return await master_review_service.admin_review_detail(session, review_id)


@backoffice_router.post("/{review_id}/approve", response_model=AdminMasterReviewDetail)
async def approve_master_review(
    review_id: int,
    payload: ReviewModerationRequest | None = None,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminMasterReviewDetail:
    ensure_review_admin(current_user)
    return await master_review_service.moderate(
        session,
        review_id,
        new_status=MasterReviewStatus.approved,
        actor_id=current_user.id,
        reason=payload.reason if payload is not None else None,
    )


@backoffice_router.post("/{review_id}/reject", response_model=AdminMasterReviewDetail)
async def reject_master_review(
    review_id: int,
    payload: ReviewModerationRequest,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> AdminMasterReviewDetail:
    ensure_review_admin(current_user)
    return await master_review_service.moderate(
        session,
        review_id,
        new_status=MasterReviewStatus.rejected,
        actor_id=current_user.id,
        reason=payload.reason,
    )
