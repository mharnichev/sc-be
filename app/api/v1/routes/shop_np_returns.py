from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas import shop_np_returns as dto
from app.services import shop_np_returns as service

backoffice_router=APIRouter(prefix="/shop-np-returns")


@backoffice_router.get("/readiness",response_model=dto.ReadinessView)
async def readiness(actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.read(session,actor,"readiness")


@backoffice_router.get("/orders/{identifier}",response_model=dto.OrderView)
async def order(identifier:int,actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.read(session,actor,"order",identifier)


@backoffice_router.get("/cases/{identifier}",response_model=dto.CaseView)
async def case(identifier:int,actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.read(session,actor,"case",identifier)


@backoffice_router.post("/orders/{identifier}/attach",response_model=dto.CaseView)
async def attach(identifier:int,payload:dto.AttachCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"attach")


@backoffice_router.post("/cases/{identifier}/refresh",response_model=dto.CaseView)
async def refresh(identifier:int,payload:dto.NPCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"refresh")


@backoffice_router.post("/cases/{identifier}/physical-receipt",response_model=dto.CaseView)
async def receive(identifier:int,payload:dto.ReceiveCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"receive")


@backoffice_router.post("/cases/{identifier}/close-unpaid",response_model=dto.CaseView)
async def close(identifier:int,payload:dto.CloseCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"close_unpaid")


@backoffice_router.post("/cases/{identifier}/financial-claim",response_model=dto.CaseView)
async def claim(identifier:int,payload:dto.ClaimCommand,key:str=Header(alias="Idempotency-Key"),actor:AdminUser=Depends(get_current_admin_user),session:AsyncSession=Depends(get_db_session)):
    return await service.command(session,actor,identifier,payload,key,"financial_claim")
