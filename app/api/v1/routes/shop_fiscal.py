from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.shop_fiscal import ShopFinancialReturn
from app.schemas.shop_fiscal import (RevisionCommand, ShiftCommand, ApproveCommand, ReturnCommand,
    RefundCommand, ReceiptCommand, EvidenceCommand, OrderFiscalView, RegisterView, PreviewView,
    DocumentView, ReturnView, ReportView)
from app.services import shop_fiscal

backoffice_router = APIRouter(prefix="/shop-fiscal")


@backoffice_router.get("/register", response_model=RegisterView)
async def register(actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.read(session, actor, "register")


@backoffice_router.post("/shift/{operation}", response_model=RegisterView)
async def shift(operation: str, payload: ShiftCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    if operation not in {"open", "close"}:
        raise HTTPException(404)
    return await shop_fiscal.shift_command(session, actor, payload, key, operation)


@backoffice_router.post("/reports/{kind}", response_model=ReportView)
async def report(kind: str, payload: ShiftCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    if kind not in {"x", "z"}:
        raise HTTPException(404)
    return await shop_fiscal.shift_command(session, actor, payload, key, kind)


@backoffice_router.get("/reports/{resource_id}", response_model=ReportView)
async def read_report(resource_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.read(session, actor, "report", resource_id)


@backoffice_router.get("/orders/{order_id}", response_model=OrderFiscalView)
async def order(order_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.read(session, actor, "order", order_id)


@backoffice_router.post("/orders/{order_id}/sale-preview", response_model=PreviewView)
async def sale_preview(order_id: int, payload: RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.order_command(session, actor, order_id, payload, key, "preview")


@backoffice_router.post("/orders/{order_id}/sale-receipts", response_model=DocumentView)
async def sale_approve(order_id: int, payload: ApproveCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.order_command(session, actor, order_id, payload, key, "approve")


@backoffice_router.get("/previews/{resource_id}", response_model=PreviewView)
async def read_preview(resource_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.read(session, actor, "preview", resource_id)


@backoffice_router.get("/documents/{resource_id}", response_model=DocumentView)
async def read_document(resource_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.read(session, actor, "document", resource_id)


@backoffice_router.get("/documents/{resource_id}/print", response_class=HTMLResponse)
async def print_document(resource_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return HTMLResponse(await shop_fiscal.print_document(session, actor, resource_id), headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@backoffice_router.post("/documents/{resource_id}/process", response_model=DocumentView)
async def process(resource_id: int, payload: RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.document_command(session, actor, resource_id, payload, key, "process")


@backoffice_router.post("/documents/{resource_id}/reconcile", response_model=DocumentView)
async def reconcile(resource_id: int, payload: RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.document_command(session, actor, resource_id, payload, key, "reconcile")


@backoffice_router.post("/documents/{resource_id}/cancel", response_model=DocumentView)
async def cancel(resource_id: int, payload: EvidenceCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.document_command(session, actor, resource_id, payload, key, "cancel")


@backoffice_router.post("/orders/{order_id}/returns", response_model=ReturnView)
async def create_return(order_id: int, payload: ReturnCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.order_command(session, actor, order_id, payload, key, "create_return")


@backoffice_router.get("/returns/{resource_id}", response_model=ReturnView)
async def read_return(resource_id: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.read(session, actor, "return", resource_id)


async def return_order(session, resource_id):
    row = await session.get(ShopFinancialReturn, resource_id)
    if not row:
        raise HTTPException(404, "Return not found")
    return row.order_id


@backoffice_router.post("/returns/{resource_id}/receipt-preview", response_model=PreviewView)
async def return_preview(resource_id: int, payload: RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.order_command(session, actor, await return_order(session, resource_id), payload, key, "preview", return_id=resource_id)


@backoffice_router.post("/returns/{resource_id}/receipts", response_model=DocumentView)
async def return_approve(resource_id: int, payload: ApproveCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.order_command(session, actor, await return_order(session, resource_id), payload, key, "approve", return_id=resource_id)


@backoffice_router.post("/returns/{resource_id}/refund", response_model=ReturnView)
async def refund(resource_id: int, payload: RefundCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.return_command(session, actor, resource_id, payload, key, "refund")


@backoffice_router.post("/returns/{resource_id}/physical-receipt", response_model=ReturnView)
async def physical_receipt(resource_id: int, payload: ReceiptCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await shop_fiscal.return_command(session, actor, resource_id, payload, key, "physical_receipt")
