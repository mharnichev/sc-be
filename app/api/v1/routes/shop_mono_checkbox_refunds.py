from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.shop_mono_checkbox_refunds import DirectRefundState, StartRefund
from app.services import shop_mono_checkbox_refunds as service

backoffice_router = APIRouter(prefix="/shop-direct-refunds/orders")


@backoffice_router.get("/{order_id}", response_model=DirectRefundState)
async def read(order_id: int, actor: AdminUser = Depends(get_current_admin_user),
               session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, order_id)


@backoffice_router.post("/{order_id}/start", response_model=DirectRefundState)
async def start(order_id: int, payload: StartRefund, key: str = Header(alias="Idempotency-Key"),
                actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.start(session, actor, order_id, payload, key)


@backoffice_router.post("/{order_id}/reconcile", response_model=DirectRefundState)
async def reconcile(order_id: int, actor: AdminUser = Depends(get_current_admin_user),
                    session: AsyncSession = Depends(get_db_session)):
    return await service.reconcile(session, actor, order_id)
