"""Authenticated local mock fiscal controls. No public provider webhook."""
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.fiscal import FiscalDocument
from app.schemas.fiscal import (FiscalCommand, FiscalDeliveryCommand, FiscalDocumentResponse,
    FiscalPreviewCreate, FiscalPreviewResponse, FiscalProcessCommand, FiscalReadinessResponse,
    FiscalSubmissionCreate, MockFiscalInboxCreate, MockShiftCommand)
from app.services import fiscal

backoffice_router = APIRouter()


@backoffice_router.get('/integrations/readiness', response_model=FiscalReadinessResponse)
async def readiness(session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await fiscal.readiness(session, actor)


@backoffice_router.post('/integrations/checkbox/cash-registers/{register_id}/open-shift', response_model=FiscalReadinessResponse)
async def open_shift(register_id: str, payload: MockShiftCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.shift(session, actor, register_id, payload, key, opening=True)


@backoffice_router.post('/integrations/checkbox/cash-registers/{register_id}/close-shift', response_model=FiscalReadinessResponse)
async def close_shift(register_id: str, payload: MockShiftCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.shift(session, actor, register_id, payload, key, opening=False)


@backoffice_router.post('/orders/{order_id}/fiscal-preview', response_model=FiscalPreviewResponse)
async def preview(order_id: int, payload: FiscalPreviewCreate, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.create_preview(session, actor, order_id, payload, key)


@backoffice_router.get('/orders/{order_id}/fiscal-previews/{preview_id}', response_model=FiscalPreviewResponse)
async def get_preview(order_id: int, preview_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await fiscal.get_preview(session, actor, order_id, preview_id)


@backoffice_router.post('/orders/{order_id}/fiscal-submissions', response_model=FiscalDocumentResponse)
async def submit(order_id: int, payload: FiscalSubmissionCreate, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.submit(session, actor, order_id, payload, key)


@backoffice_router.get('/orders/{order_id}/fiscal-documents', response_model=list[FiscalDocumentResponse])
async def documents(order_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await fiscal.list_documents(session, actor, order_id)


@backoffice_router.get('/fiscal-documents/{document_id}', response_model=FiscalDocumentResponse)
async def document(document_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await fiscal.get_document(session, actor, document_id)


@backoffice_router.post('/fiscal-documents/{document_id}/process', response_model=FiscalDocumentResponse, status_code=202)
async def process(document_id: int, payload: FiscalProcessCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.command(session, actor, document_id, payload, key, 'process')


@backoffice_router.post('/fiscal-documents/{document_id}/cancel', response_model=FiscalDocumentResponse)
async def cancel(document_id: int, payload: FiscalCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.command(session, actor, document_id, payload, key, 'cancel')


@backoffice_router.post('/fiscal-documents/{document_id}/delivery', response_model=FiscalDocumentResponse)
async def delivery(document_id: int, payload: FiscalDeliveryCommand, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.command(session, actor, document_id, payload, key, 'delivery')


@backoffice_router.post('/integrations/checkbox/mock-inbox', response_model=FiscalDocumentResponse)
async def inbox(payload: MockFiscalInboxCreate, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user), key: str = Header(alias='Idempotency-Key')):
    return await fiscal.inbox(session, actor, payload, key)


@backoffice_router.get('/fiscal-documents/{document_id}/print', response_class=HTMLResponse)
async def printable(document_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    document = await session.get(FiscalDocument, document_id)
    await fiscal.require(session, actor, mutation=False, kind=document.kind if document else 'sale')
    if document is None:
        raise HTTPException(404, 'Fiscal document not found')
    if document.status != 'issued':
        fiscal.fail('receipt_not_issued', 'Only issued mock documents may be printed')
    await fiscal.validate_issued_document(session, document)
    return HTMLResponse(fiscal.print_html(document), headers={'Cache-Control': 'private, no-store',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"})
