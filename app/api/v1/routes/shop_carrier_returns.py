from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.shop_carrier_returns import CarrierCreate, CarrierReceive, CarrierClose, CarrierCaseView, CarrierStateView
from app.schemas.shop_fiscal import RevisionCommand
from app.services import shop_carrier_returns

backoffice_router = APIRouter(prefix="/orders/{order_id}/carrier-returns")


@backoffice_router.get("", response_model=CarrierStateView)
async def state(order_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_carrier_returns.read(session, actor, order_id)


@backoffice_router.post("", response_model=CarrierCaseView)
async def create(order_id: int, payload: CarrierCreate, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_carrier_returns.create(session, actor, order_id, payload, key)


@backoffice_router.get("/{case_id}", response_model=CarrierCaseView)
async def read(order_id: int, case_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_carrier_returns.read(session, actor, order_id, case_id)


@backoffice_router.post("/{case_id}/refresh", response_model=CarrierCaseView)
async def refresh(order_id: int, case_id: int, payload: RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_carrier_returns.command(session, actor, order_id, case_id, payload, key, "refresh")


@backoffice_router.post("/{case_id}/reconcile", response_model=CarrierCaseView)
async def reconcile(order_id: int, case_id: int, payload: RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_carrier_returns.command(session, actor, order_id, case_id, payload, key, "reconcile")


@backoffice_router.post("/{case_id}/receive", response_model=CarrierCaseView)
async def receive(order_id: int, case_id: int, payload: CarrierReceive, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_carrier_returns.command(session, actor, order_id, case_id, payload, key, "receive")


@backoffice_router.post("/{case_id}/close-unpaid", response_model=CarrierCaseView)
async def close_unpaid(order_id: int, case_id: int, payload: CarrierClose, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_carrier_returns.command(session, actor, order_id, case_id, payload, key, "close_unpaid")
