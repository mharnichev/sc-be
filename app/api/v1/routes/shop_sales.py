from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.shop_sales import ManualShopPayment, ProviderShopPayment, ShopPhysicalCommand, ShopOperationResponse
from app.services import shop_sales

backoffice_router = APIRouter(prefix="/orders/{order_id}/shop-operations")


@backoffice_router.get("", response_model=ShopOperationResponse)
async def state(order_id: int, actor: AdminUser = Depends(get_current_admin_user),
                session: AsyncSession = Depends(get_db_session)):
    return await shop_sales.read(session, actor, order_id)


@backoffice_router.post("/payments/manual", response_model=ShopOperationResponse)
async def manual_payment(order_id: int, payload: ManualShopPayment,
    key: str = Header(alias="Idempotency-Key"), actor: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session)):
    return await shop_sales.command(session, actor, order_id, payload, key, "manual_payment")


@backoffice_router.post("/payments/provider", response_model=ShopOperationResponse)
async def provider_payment(order_id: int, payload: ProviderShopPayment,
    key: str = Header(alias="Idempotency-Key"), actor: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session)):
    return await shop_sales.command(session, actor, order_id, payload, key, "provider_payment")


@backoffice_router.post("/handover", response_model=ShopOperationResponse)
async def handover(order_id: int, payload: ShopPhysicalCommand,
    key: str = Header(alias="Idempotency-Key"), actor: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session)):
    return await shop_sales.command(session, actor, order_id, payload, key, "handover")


@backoffice_router.post("/complete", response_model=ShopOperationResponse)
async def complete(order_id: int, payload: ShopPhysicalCommand,
    key: str = Header(alias="Idempotency-Key"), actor: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session)):
    return await shop_sales.command(session, actor, order_id, payload, key, "complete")
