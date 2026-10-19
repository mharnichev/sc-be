"""Authenticated, explicitly local mock automation. No provider webhook route."""
from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.fiscal import FiscalCommand
from app.schemas.fiscal_automation import (AutomationCancel, AutomationCreate, AutomationListResponse,
    AutomationPreviewCreate, AutomationPreviewResponse, AutomationResponse)
from app.services import fiscal_automation

backoffice_router = APIRouter()


@backoffice_router.post('/orders/{order_id}/fiscal-automation-preview', response_model=AutomationPreviewResponse)
async def preview(order_id: int, payload: AutomationPreviewCreate, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal_automation.create_preview(session, actor, order_id, payload, key)


@backoffice_router.get('/orders/{order_id}/fiscal-automation-previews/{preview_id}', response_model=AutomationPreviewResponse)
async def get_preview(order_id: int, preview_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await fiscal_automation.get_preview(session, actor, order_id, preview_id)


@backoffice_router.post('/orders/{order_id}/fiscal-automations', response_model=AutomationResponse)
async def create(order_id: int, payload: AutomationCreate, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal_automation.create(session, actor, order_id, payload, key)


@backoffice_router.get('/orders/{order_id}/fiscal-automations', response_model=AutomationListResponse)
async def projects(order_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await fiscal_automation.list_projects(session, actor, order_id)


@backoffice_router.get('/fiscal-automations/{project_id}', response_model=AutomationResponse)
async def project(project_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await fiscal_automation.get(session, actor, project_id)


@backoffice_router.post('/fiscal-automations/{project_id}/cancel', response_model=AutomationResponse)
async def cancel(project_id: int, payload: AutomationCancel, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal_automation.command(session, actor, project_id, payload, key, 'cancel')


@backoffice_router.post('/fiscal-automations/{project_id}/process', response_model=AutomationResponse)
async def process(project_id: int, payload: FiscalCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal_automation.command(session, actor, project_id, payload, key, 'process')


@backoffice_router.post('/fiscal-automations/{project_id}/reconcile', response_model=AutomationResponse)
async def reconcile(project_id: int, payload: FiscalCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal_automation.command(session, actor, project_id, payload, key, 'reconcile')


@backoffice_router.post('/fiscal-automations/{project_id}/trigger', response_model=AutomationResponse)
async def trigger(project_id: int, payload: FiscalCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal_automation.command(session, actor, project_id, payload, key, 'trigger')


@backoffice_router.post('/fiscal-automations/{project_id}/materialize', response_model=AutomationResponse)
async def materialize(project_id: int, payload: FiscalCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal_automation.command(session, actor, project_id, payload, key, 'materialize')
