from __future__ import annotations

import asyncio
import mimetypes
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, request_id_context
from app.services.campaign_run_scheduler import run_campaign_scheduler
from app.services.sms_queue_scheduler import run_sms_queue_worker
from app.services.product_popularity import run_product_popularity_scheduler
from app.services.service_popularity import run_service_popularity_scheduler
from app.services.messaging import run_review_request_scheduler, run_sms_delivery_status_scheduler
from app.services.booking_funnel import run_booking_funnel_digest_scheduler
from app.services.waitlist_offers import run_waitlist_offer_scheduler
from app.services.customer_activity_notifications import run_customer_activity_notification_scheduler
from app.services.repeat_booking import run_repeat_booking_scheduler
from app.services.master_schedule_reminders import run_master_schedule_reminder_scheduler
from app.services.brevo_scheduler import run_brevo_sync_scheduler
from app.services.booking_completion import run_booking_completion_scheduler
from app.services.nova_poshta_directory import run_nova_poshta_directory_scheduler

configure_logging()

mimetypes.add_type("image/webp", ".webp")


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks: list[asyncio.Task[None]] = []
    if settings.booking_auto_completion_enabled:
        tasks.append(asyncio.create_task(
            run_booking_completion_scheduler(), name="booking-auto-completion-scheduler",
        ))
    if settings.sms_queue_worker_enabled and settings.sms_provider == "smsclub":
        tasks.append(asyncio.create_task(run_sms_queue_worker(), name="sms-queue-worker"))
    if settings.campaign_run_scheduler_enabled:
        tasks.append(asyncio.create_task(run_campaign_scheduler(), name="campaign-run-scheduler"))
    if settings.product_top_scheduler_enabled:
        tasks.append(asyncio.create_task(
            run_product_popularity_scheduler(),
            name="product-top-cache-scheduler",
        ))
    if settings.service_popularity_scheduler_enabled:
        tasks.append(asyncio.create_task(
            run_service_popularity_scheduler(),
            name="service-popularity-cache-scheduler",
        ))
    if settings.review_request_scheduler_enabled:
        tasks.append(asyncio.create_task(
            run_review_request_scheduler(),
            name="review-request-scheduler",
        ))
    if settings.sms_delivery_status_scheduler_enabled and settings.sms_provider == "smsclub":
        tasks.append(asyncio.create_task(
            run_sms_delivery_status_scheduler(),
            name="sms-delivery-status-scheduler",
        ))
    if settings.booking_funnel_digest_scheduler_enabled:
        tasks.append(asyncio.create_task(
            run_booking_funnel_digest_scheduler(),
            name="booking-funnel-weekly-digest-scheduler",
        ))
    if settings.waitlist_offer_scheduler_enabled:
        tasks.append(asyncio.create_task(
            run_waitlist_offer_scheduler(),
            name="waitlist-offer-scheduler",
        ))
    tasks.append(asyncio.create_task(
        run_customer_activity_notification_scheduler(),
        name="customer-activity-notification-scheduler",
    ))
    if settings.repeat_booking_scheduler_enabled:
        tasks.append(asyncio.create_task(
            run_repeat_booking_scheduler(),
            name="repeat-booking-scheduler",
        ))
    if settings.master_schedule_reminder_scheduler_enabled:
        tasks.append(asyncio.create_task(
            run_master_schedule_reminder_scheduler(),
            name="master-schedule-reminder-scheduler",
        ))
    if getattr(settings, "brevo_sync_enabled", False) and getattr(settings, "brevo_api_key", None):
        tasks.append(asyncio.create_task(
            run_brevo_sync_scheduler(),
            name="brevo-contact-sync-scheduler",
        ))
    if getattr(settings, "nova_poshta_directory_enabled", False) and getattr(settings, "nova_poshta_api_key", None):
        tasks.append(asyncio.create_task(
            run_nova_poshta_directory_scheduler(), name="nova-poshta-directory-scheduler",
        ))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id")
    if request_id:
        request_id_context.set_request_id(request_id)
    else:
        request_id = request_id_context.new_request_id()
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(api_router, prefix="/api/v1")
app.mount(settings.upload_url_prefix, StaticFiles(directory=settings.upload_dir, check_dir=False), name="media")
