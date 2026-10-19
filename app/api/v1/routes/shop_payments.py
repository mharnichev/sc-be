from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.shop_payments import (InvoiceIntentCommand, VerifyInvoiceCommand, ManualCODCommand,
    PaymentState, PaymentReadiness)
from app.services import shop_payments

backoffice_router = APIRouter(prefix="/shop-payments")


@backoffice_router.get("/readiness", response_model=PaymentReadiness)
async def readiness(actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_payments.readiness(session, actor)


@backoffice_router.get("/orders/{order_id}", response_model=PaymentState)
async def state(order_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_payments.read(session, actor, order_id)


@backoffice_router.post("/orders/{order_id}/invoice-intent", response_model=PaymentState)
async def invoice_intent(order_id: int, payload: InvoiceIntentCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_payments.command(session, actor, order_id, payload, key, "invoice_intent")


@backoffice_router.post("/orders/{order_id}/verify", response_model=PaymentState)
async def verify(order_id: int, payload: VerifyInvoiceCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_payments.command(session, actor, order_id, payload, key, "verify")


@backoffice_router.post("/orders/{order_id}/manual-cod", response_model=PaymentState)
async def manual_cod(order_id: int, payload: ManualCODCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_payments.command(session, actor, order_id, payload, key, "manual_cod")
