from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.master_mono import MasterMonoCommand, MasterMonoState, MasterMonoRecover
from app.services import master_mono

backoffice_router = APIRouter(prefix="/master-mono/orders")


@backoffice_router.get("/{order_id}", response_model=MasterMonoState)
async def state(order_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await master_mono.read(session, actor, order_id)


@backoffice_router.post("/{order_id}/start", response_model=MasterMonoState)
async def start(order_id: int, payload: MasterMonoCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await master_mono.start(session, actor, order_id, payload, key)


@backoffice_router.post("/{order_id}/refresh", response_model=MasterMonoState)
async def refresh(order_id: int, payload: MasterMonoCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await master_mono.refresh(session, actor, order_id, payload, key)


@backoffice_router.get("/{order_id}/receipt/pdf")
async def pdf(order_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    receipt_id, content = await master_mono.download_pdf(session, actor, order_id)
    return Response(content=content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="receipt-{receipt_id}.pdf"',
        "Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store"})


@backoffice_router.post("/{order_id}/recover-invoice", response_model=MasterMonoState)
async def recover(order_id: int, payload: MasterMonoRecover, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await master_mono.recover(session, actor, order_id, payload, key)
