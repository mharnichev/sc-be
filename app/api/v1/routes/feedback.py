from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.rate_limit import feedback_rate_limiter, privacy_safe_rate_key
from app.dependencies.auth import get_optional_current_customer
from app.models.customer import Customer
from app.models.order import Order
from app.schemas.feedback import FeedbackEmailRequest, FeedbackEmailResponse
from app.services.email_notifications import FeedbackEmail, email_notification_service

logger = logging.getLogger(__name__)

public_router = APIRouter()


@public_router.post("/email", response_model=FeedbackEmailResponse, status_code=status.HTTP_202_ACCEPTED)
async def send_feedback_email(
    payload: FeedbackEmailRequest,
    request: Request,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> FeedbackEmailResponse:
    feedback_rate_limiter.check(
        privacy_safe_rate_key(request, "source", "shop-feedback-source"),
        limit=10,
        window_seconds=60,
    )
    feedback_rate_limiter.check(
        privacy_safe_rate_key(request, str(payload.email).lower(), "shop-feedback"),
        limit=5,
        window_seconds=60,
    )
    if payload.order_id is not None:
        if current_customer is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        owned_order = await session.scalar(select(Order.id).where(
            Order.id == payload.order_id,
            Order.customer_id == current_customer.id,
        ))
        if owned_order is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    try:
        await email_notification_service.send_feedback(FeedbackEmail(
            name=payload.name,
            email=str(payload.email),
            topic=payload.topic,
            text=payload.text,
            order_id=payload.order_id,
        ))
    except Exception as exc:
        logger.warning("Shop contact request delivery failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Contact request could not be delivered",
        ) from exc
    return FeedbackEmailResponse(message="Feedback accepted")
