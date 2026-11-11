from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.shop_external_fiscal_reports import ExternalFiscalReconciliation
from app.services import shop_external_fiscal_reports as service

backoffice_router = APIRouter(prefix="/shop-external-fiscal-reports/orders")


@backoffice_router.get("/{order_id}", response_model=ExternalFiscalReconciliation)
async def read(
    order_id: int,
    actor: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    return await service.read(session, actor, order_id)
