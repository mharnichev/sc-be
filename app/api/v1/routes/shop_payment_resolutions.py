from fastapi import APIRouter,Depends,Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas import shop_payment_resolutions as dto
from app.services import shop_payment_resolutions as service
backoffice_router=APIRouter(prefix="/shop-payment-resolutions")

@backoffice_router.get("/orders/{identifier}",response_model=dto.OrderView)
async def read_orders(identifier:int,actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.read(session,actor,"order",identifier)

@backoffice_router.get("/cases/{identifier}",response_model=dto.CaseView)
async def read_cases(identifier:int,actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.read(session,actor,"case",identifier)

@backoffice_router.post("/orders/{identifier}/paid-cancellation",response_model=dto.CaseView)
async def paid_cancellation(identifier:int,payload:dto.OpenCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"paid_cancellation")

@backoffice_router.post("/orders/{identifier}/invoice-review",response_model=dto.CaseView)
async def invoice_review(identifier:int,payload:dto.OpenCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"invoice_review")

@backoffice_router.post("/cases/{identifier}/inspect",response_model=dto.CaseView)
async def inspect(identifier:int,payload:dto.InspectCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"inspect")

@backoffice_router.post("/cases/{identifier}/correct-invoice",response_model=dto.CaseView)
async def correct_invoice(identifier:int,payload:dto.CorrectCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"correct_invoice")

@backoffice_router.post("/cases/{identifier}/financial-claim",response_model=dto.CaseView)
async def financial_claim(identifier:int,payload:dto.ClaimCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"financial_claim")

@backoffice_router.post("/cases/{identifier}/close-unpaid",response_model=dto.CaseView)
async def close_unpaid(identifier:int,payload:dto.CloseUnpaidCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"close_unpaid")

@backoffice_router.post("/cases/{identifier}/finalize-paid",response_model=dto.CaseView)
async def finalize_paid(identifier:int,payload:dto.FinalizeCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"finalize_paid")
