from __future__ import annotations

import asyncio
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.shop_request_size import ShopRequestSizeMiddleware
from app.services.shop_ingress_limits import ingress_engine
from app.core.background_workers import BackgroundWorkers, worker_state
from app.core.logging import configure_logging, request_id_context
from app.core.local_migrations import apply_local_migrations
from app.core.sentry import init_sentry
from app.core import error_reporting
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
from app.services.mono_checkbox_poll import run_mono_checkbox_scheduler
from app.services.np_tracking import run_np_tracking_scheduler
from app.services.nova_poshta_directory import run_nova_poshta_directory_scheduler
from app.services.booking_completion import run_booking_completion_scheduler

configure_logging()
init_sentry("api")

mimetypes.add_type("image/webp", ".webp")


@asynccontextmanager
async def lifespan(application: FastAPI):
    if getattr(settings, "app_env", None) in {"local", "development"}:
        await apply_local_migrations()
    if not getattr(settings, "background_workers_enabled", True):
        if application is not None:
            application.state.background_workers = worker_state()
        try:
            yield
        finally:
            await ingress_engine.dispose()
        return
    schedulers = []
    if settings.booking_auto_completion_enabled:
        schedulers.append(("booking-auto-completion-scheduler", run_booking_completion_scheduler))
    if settings.sms_queue_worker_enabled and settings.sms_provider == "smsclub":
        schedulers.append(("sms-queue-worker", run_sms_queue_worker))
    if settings.campaign_run_scheduler_enabled:
        schedulers.append(("campaign-run-scheduler", run_campaign_scheduler))
    if settings.product_top_scheduler_enabled:
        schedulers.append(("product-top-cache-scheduler", run_product_popularity_scheduler))
    if settings.service_popularity_scheduler_enabled:
        schedulers.append(("service-popularity-cache-scheduler", run_service_popularity_scheduler))
    if settings.review_request_scheduler_enabled:
        schedulers.append(("review-request-scheduler", run_review_request_scheduler))
    if settings.sms_delivery_status_scheduler_enabled and settings.sms_provider == "smsclub":
        schedulers.append(("sms-delivery-status-scheduler", run_sms_delivery_status_scheduler))
    if settings.booking_funnel_digest_scheduler_enabled:
        schedulers.append(("booking-funnel-weekly-digest-scheduler", run_booking_funnel_digest_scheduler))
    if settings.waitlist_offer_scheduler_enabled:
        schedulers.append(("waitlist-offer-scheduler", run_waitlist_offer_scheduler))
    if getattr(settings, "customer_activity_notification_scheduler_enabled", True):
        schedulers.append(("customer-activity-notification-scheduler", run_customer_activity_notification_scheduler))
    if settings.repeat_booking_scheduler_enabled:
        schedulers.append(("repeat-booking-scheduler", run_repeat_booking_scheduler))
    if settings.master_schedule_reminder_scheduler_enabled:
        schedulers.append(("master-schedule-reminder-scheduler", run_master_schedule_reminder_scheduler))
    if getattr(settings, "brevo_sync_enabled", False) and getattr(settings, "brevo_api_key", None):
        schedulers.append(("brevo-contact-sync-scheduler", run_brevo_sync_scheduler))
    if settings.np_tracking_enabled and settings.nova_poshta_api_key:
        schedulers.append(("nova-poshta-tracking-scheduler", run_np_tracking_scheduler))
    if getattr(settings, "nova_poshta_directory_enabled", False) and getattr(settings, "nova_poshta_api_key", None):
        schedulers.append(("nova-poshta-directory-scheduler", run_nova_poshta_directory_scheduler))
    if getattr(settings, "mono_checkbox_auto_enabled", False):
        schedulers.append(("mono-checkbox-payment-receipt-scheduler", run_mono_checkbox_scheduler))
    if getattr(settings, "checkout_reminder_scheduler_enabled", False):
        from app.services.checkout_reminders import run_checkout_reminder_scheduler, scheduler_allowed
        if scheduler_allowed():
            schedulers.append(("checkout-reminder-scheduler", run_checkout_reminder_scheduler))
    workers = BackgroundWorkers(
        schedulers, app_env=getattr(settings, "app_env", None),
        create_task=asyncio.create_task,
        heartbeat_seconds=getattr(settings, "background_owner_heartbeat_seconds", 5),
    )
    if application is not None:
        application.state.background_workers = workers.state
    try:
        async with workers:
            yield
    finally:
        await ingress_engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(ShopRequestSizeMiddleware)

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
    try:
        response = await call_next(request)
    except Exception as error:
        route = request.scope.get("route")
        path = getattr(route, "path", "")
        step = "order_create" if request.method == "POST" and path.endswith("/orders") else "request"
        error_reporting.report_exception(step, error)
        raise
    response.headers["x-request-id"] = request_id
    return response


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(api_router, prefix="/api/v1")
app.mount(settings.upload_url_prefix, StaticFiles(directory=settings.upload_dir, check_dir=False), name="media")
