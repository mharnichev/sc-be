"""Explicit mock-only return, reporting and capability read endpoints."""
from html import escape
from fastapi import APIRouter, Depends, Header
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.schemas.fiscal import FiscalPreviewCreate, FiscalPreviewResponse
from app.schemas.fiscal_returns import (ReturnCreate, ReturnResponse, MockRefundCreate, ReturnReceiptCreate,
    MockReportCreate, MockReportResponse, ReconciliationResponse, ReconciliationAckCreate,
    ReconciliationAckResponse, FiscalShareCreate, FiscalShareRevoke, FiscalShareResponse, PublicMockFiscalDocument)
from app.services import fiscal_returns as service

backoffice_router = APIRouter()
public_router = APIRouter()
Session = Depends(get_db_session)
Actor = Depends(get_current_admin_user)
Key = Header(alias='Idempotency-Key')


@backoffice_router.get('/orders/{order_id}/returns', response_model=list[ReturnResponse])
async def returns(order_id: int, session: AsyncSession = Session, actor: AdminUser = Actor):
    return await service.list_returns(session, actor, order_id)


@backoffice_router.post('/orders/{order_id}/returns', response_model=ReturnResponse)
async def create_return(order_id: int, payload: ReturnCreate, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.create_return(session, actor, order_id, payload, key)


@backoffice_router.post('/returns/{return_id}/refunds', response_model=ReturnResponse)
async def refund(return_id: int, payload: MockRefundCreate, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.record_refund(session, actor, return_id, payload, key)


@backoffice_router.post('/returns/{return_id}/receipts', response_model=ReturnResponse)
async def receipt(return_id: int, payload: ReturnReceiptCreate, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.record_receipt(session, actor, return_id, payload, key)


@backoffice_router.post('/returns/{return_id}/fiscal-preview', response_model=FiscalPreviewResponse)
async def preview(return_id: int, payload: FiscalPreviewCreate, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.create_preview(session, actor, return_id, payload, key)


@backoffice_router.get('/integrations/checkbox/reconciliation', response_model=ReconciliationResponse)
async def reconciliation(session: AsyncSession = Session, actor: AdminUser = Actor):
    return await service.reconciliation(session, actor)


@backoffice_router.post('/integrations/checkbox/reconciliation/acknowledgements', response_model=ReconciliationAckResponse)
async def acknowledge(payload: ReconciliationAckCreate, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.acknowledge(session, actor, payload, key)


@backoffice_router.get('/integrations/checkbox/cash-registers/{register_id}/reports', response_model=list[MockReportResponse])
async def reports(register_id: str, session: AsyncSession = Session, actor: AdminUser = Actor):
    return await service.list_reports(session, actor, register_id)


@backoffice_router.post('/integrations/checkbox/cash-registers/{register_id}/reports', response_model=MockReportResponse)
async def report(register_id: str, payload: MockReportCreate, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.report(session, actor, register_id, payload, key)


@backoffice_router.post('/fiscal-documents/{document_id}/share', response_model=FiscalShareResponse)
async def share(document_id: int, payload: FiscalShareCreate, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.share_document(session, actor, document_id, payload, key)


@backoffice_router.post('/fiscal-documents/{document_id}/revoke-share', response_model=FiscalShareResponse)
async def revoke(document_id: int, payload: FiscalShareRevoke, session: AsyncSession = Session, actor: AdminUser = Actor, key: str = Key):
    return await service.share_document(session, actor, document_id, payload, key, revoke=True)


PUBLIC_HEADERS = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'"}


@public_router.get('/mock-fiscal-documents/{token}', response_model=PublicMockFiscalDocument)
async def public_document(token: str, session: AsyncSession = Session):
    from fastapi.responses import JSONResponse
    result = await service.public_document(session, token)
    return JSONResponse(result.model_dump(mode='json'), headers=PUBLIC_HEADERS)


@public_router.get('/mock-fiscal-documents/{token}/print', response_class=HTMLResponse)
async def public_print(token: str, session: AsyncSession = Session):
    result = await service.public_document(session, token)
    rows = ''.join('<tr><td>' + escape(item.name or '') + '</td><td>' + str(item.quantity) + '</td><td>'
        + f'{item.total_minor / 100:.2f}' + '</td></tr>' for item in result.items)
    return HTMLResponse('<!doctype html><html lang="uk"><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
        '<title>MOCK Checkbox</title><h1>' + escape(result.notice) + '</h1><p>' + escape(result.kind) + '</p><table>' + rows
        + '</table><p>UAH ' + f'{result.amount_minor / 100:.2f}' + '</p></html>', headers=PUBLIC_HEADERS)
