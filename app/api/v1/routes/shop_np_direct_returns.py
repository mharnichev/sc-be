from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas import shop_np_direct_returns as dto
from app.services import shop_np_direct_returns as service

backoffice_router = APIRouter(prefix="/shop-np-direct-returns")


@backoffice_router.get("/orders/{order_id}", response_model=dto.ReturnView)
async def read(order_id: int, actor: AdminUser = Depends(get_current_admin_user),
               session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, order_id)


@backoffice_router.post("/orders/{order_id}/attach", response_model=dto.ReturnView)
async def attach(order_id: int, payload: dto.AttachCommand, key: str = Header(alias="Idempotency-Key"),
                 actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.command(session, actor, order_id, payload, key, "attach")


@backoffice_router.post("/orders/{order_id}/refresh", response_model=dto.ReturnView)
async def refresh(order_id: int, payload: dto.NPCommand, key: str = Header(alias="Idempotency-Key"),
                  actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.command(session, actor, order_id, payload, key, "refresh")


@backoffice_router.post("/orders/{order_id}/physical-receipt", response_model=dto.ReturnView)
async def receive(order_id: int, payload: dto.ReceiveCommand, key: str = Header(alias="Idempotency-Key"),
                  actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.command(session, actor, order_id, payload, key, "receive")
