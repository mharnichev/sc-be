from fastapi import APIRouter

from app.api.v1.backoffice.router import router as backoffice_router
from app.api.v1.public.router import router as public_router

api_router = APIRouter()
api_router.include_router(public_router, prefix="/public")
api_router.include_router(backoffice_router, prefix="/backoffice")
