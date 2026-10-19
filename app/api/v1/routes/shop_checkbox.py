from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.shop_checkbox import ShopCheckboxReturn
from app.schemas import shop_checkbox as dto
from app.services import shop_checkbox as service

backoffice_router = APIRouter(prefix="/shop-checkbox")


@backoffice_router.get("/readiness", response_model=dto.ReadinessView)
async def readiness(actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "readiness")


@backoffice_router.get("/register", response_model=dto.RegisterView)
async def register(actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "register")


@backoffice_router.post("/verify", response_model=dto.OperationView)
async def verify(payload: dto.VerifyCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.register_command(session, actor, payload, key, "verify")


@backoffice_router.post("/shift/{operation}", response_model=dto.OperationView)
async def shift(operation: str, payload: dto.RegisterCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    if operation not in {"open", "close"}:
        raise HTTPException(404)
    return await service.register_command(session, actor, payload, key, "shift_" + operation)


@backoffice_router.post("/reports/{kind}", response_model=dto.OperationView)
async def create_report(kind: str, payload: dto.RegisterCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    if kind not in {"x", "z"}:
        raise HTTPException(404)
    return await service.register_command(session, actor, payload, key, kind)


@backoffice_router.get("/operations/{identifier}", response_model=dto.OperationView)
async def operation(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "operation", identifier)


@backoffice_router.get("/reports/{identifier}", response_model=dto.OperationView)
async def report(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "report", identifier)


@backoffice_router.post("/operations/{identifier}/process", response_model=dto.OperationView)
async def process(identifier: int, payload: dto.RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.operation_command(session, actor, identifier, payload, key, "process")


@backoffice_router.post("/operations/{identifier}/reconcile", response_model=dto.OperationView)
async def reconcile(identifier: int, payload: dto.ReconcileCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.operation_command(session, actor, identifier, payload, key, "reconcile")


@backoffice_router.post("/operations/{identifier}/cancel", response_model=dto.OperationView)
async def cancel(identifier: int, payload: dto.EvidenceCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.cancel_operation(session, actor, identifier, payload, key)


@backoffice_router.get("/orders/{identifier}", response_model=dto.OrderFiscalView)
async def order(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "order", identifier)


@backoffice_router.get("/orders/{identifier}/profile", response_model=dto.ProfileView)
async def profile(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "profile", identifier)


@backoffice_router.post("/orders/{identifier}/sale-preview", response_model=dto.PreviewView)
async def sale_preview(identifier: int, payload: dto.RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.order_command(session, actor, identifier, payload, key, "preview")


@backoffice_router.post("/orders/{identifier}/sale-receipts", response_model=dto.DocumentView)
async def sale_approve(identifier: int, payload: dto.ApproveCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.order_command(session, actor, identifier, payload, key, "approve")


@backoffice_router.get("/previews/{identifier}", response_model=dto.PreviewView)
async def preview(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "preview", identifier)


@backoffice_router.get("/documents/{identifier}", response_model=dto.DocumentView)
async def document(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "document", identifier)


@backoffice_router.get("/documents/{identifier}/print", response_class=HTMLResponse)
async def print_document(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return HTMLResponse(await service.print_document(session, actor, identifier), headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@backoffice_router.post("/documents/{identifier}/process", response_model=dto.OperationView)
async def document_process(identifier: int, payload: dto.RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    doc = await service.load_document(session, identifier)
    return await service.operation_command(session, actor, doc.operation_id, payload, key, "process", document_id=identifier)


@backoffice_router.post("/documents/{identifier}/reconcile", response_model=dto.OperationView)
async def document_reconcile(identifier: int, payload: dto.ReconcileCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    doc = await service.load_document(session, identifier)
    return await service.operation_command(session, actor, doc.operation_id, payload, key, "reconcile", document_id=identifier)


@backoffice_router.post("/documents/{identifier}/cancel", response_model=dto.DocumentView)
async def document_cancel(identifier: int, payload: dto.EvidenceCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.cancel_operation(session, actor, identifier, payload, key, document=True)


@backoffice_router.post("/orders/{identifier}/returns", response_model=dto.ReturnView)
async def create_return(identifier: int, payload: dto.ReturnCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.order_command(session, actor, identifier, payload, key, "create_return")


@backoffice_router.get("/returns/{identifier}", response_model=dto.ReturnView)
async def financial_return(identifier: int, actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.read(session, actor, "return", identifier)


@backoffice_router.post("/returns/{identifier}/refund", response_model=dto.ReturnView)
async def refund(identifier: int, payload: dto.RefundCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.return_command(session, actor, identifier, payload, key, "refund")


@backoffice_router.post("/returns/{identifier}/physical-receipt", response_model=dto.ReturnView)
async def physical_receipt(identifier: int, payload: dto.ReceiptCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await service.return_command(session, actor, identifier, payload, key, "physical_receipt")


@backoffice_router.post("/returns/{identifier}/receipt-preview", response_model=dto.PreviewView)
async def return_preview(identifier: int, payload: dto.RevisionCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    item = await session.get(ShopCheckboxReturn, identifier)
    if not item:
        raise HTTPException(404)
    return await service.order_command(session, actor, item.order_id, payload, key, "preview", return_id=identifier)


@backoffice_router.post("/returns/{identifier}/receipts", response_model=dto.DocumentView)
async def return_approve(identifier: int, payload: dto.ApproveCommand, key: str = Header(alias="Idempotency-Key"),
    actor: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    item = await session.get(ShopCheckboxReturn, identifier)
    if not item:
        raise HTTPException(404)
    return await service.order_command(session, actor, item.order_id, payload, key, "approve", return_id=identifier)
