from __future__ import annotations

from fastapi import APIRouter, status

from app.schemas.feedback import FeedbackEmailRequest, FeedbackEmailResponse

public_router = APIRouter()


@public_router.post("/email", response_model=FeedbackEmailResponse, status_code=status.HTTP_202_ACCEPTED)
async def send_feedback_email(payload: FeedbackEmailRequest) -> FeedbackEmailResponse:
    return FeedbackEmailResponse(message="Feedback accepted")
