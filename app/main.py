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
from app.services.product_popularity import run_product_popularity_scheduler
from app.services.messaging import run_review_request_scheduler

configure_logging()

mimetypes.add_type("image/webp", ".webp")


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler_task: asyncio.Task[None] | None = None
    review_scheduler_task: asyncio.Task[None] | None = None
    if settings.product_top_scheduler_enabled:
        scheduler_task = asyncio.create_task(
            run_product_popularity_scheduler(),
            name="product-top-cache-scheduler",
        )
    if settings.review_request_scheduler_enabled:
        review_scheduler_task = asyncio.create_task(
            run_review_request_scheduler(),
            name="review-request-scheduler",
        )
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        if review_scheduler_task is not None:
            review_scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await review_scheduler_task


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
