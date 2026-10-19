"""Authenticated local simulation and explicit provider sandbox controls."""
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.models.admin_user import AdminUser
from app.models.fiscal import FiscalDocument
from app.schemas.qr_payment import QRCreate, QRCommandCreate, QRSimulate, QRAttemptResponse
from app.services import qr_payment, fiscal

backoffice_router = APIRouter()


@backoffice_router.get('/integrations/monobank-qr/readiness')
async def readiness(session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await qr_payment.readiness(session, actor)


@backoffice_router.post('/orders/{order_id}/qr-payment-attempts', response_model=QRAttemptResponse, status_code=201)
async def create(order_id: int, payload: QRCreate, key: str = Header(alias='Idempotency-Key'),
    session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await qr_payment.create(session, actor, order_id, payload, key)


@backoffice_router.get('/orders/{order_id}/qr-payment-attempts', response_model=list[QRAttemptResponse])
async def attempts(order_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await qr_payment.list_attempts(session, actor, order_id)


@backoffice_router.get('/qr-payment-attempts/{attempt_id}', response_model=QRAttemptResponse)
async def attempt(attempt_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await qr_payment.get(session, actor, attempt_id)


@backoffice_router.post('/qr-payment-attempts/{attempt_id}/refresh', response_model=QRAttemptResponse)
async def refresh(attempt_id: int, payload: QRCommandCreate, key: str = Header(alias='Idempotency-Key'),
    session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await qr_payment.command(session, actor, attempt_id, payload, key, 'refresh')


@backoffice_router.post('/qr-payment-attempts/{attempt_id}/cancel', response_model=QRAttemptResponse)
async def cancel(attempt_id: int, payload: QRCommandCreate, key: str = Header(alias='Idempotency-Key'),
    session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await qr_payment.command(session, actor, attempt_id, payload, key, 'cancel')


@backoffice_router.post('/qr-payment-attempts/{attempt_id}/simulate', response_model=QRAttemptResponse)
async def simulate(attempt_id: int, payload: QRSimulate, key: str = Header(alias='Idempotency-Key'),
    session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    return await qr_payment.command(session, actor, attempt_id, payload, key, 'simulate')


@backoffice_router.get('/qr-payment-attempts/{attempt_id}/receipt', response_class=HTMLResponse)
async def receipt(attempt_id: int, session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    await qr_payment.get(session, actor, attempt_id)
    document = await session.scalar(select(FiscalDocument).where(FiscalDocument.qr_attempt_id == attempt_id))
    if not document:
        raise HTTPException(404, 'Issued QR receipt not found')
    await fiscal.validate_issued_document(session, document)
    if document.snapshot.get('transport') == 'provider_sandbox':
        from app.services.sandbox import validate, receipt_summary
        row = await session.get(qr_payment.QRPaymentAttempt, attempt_id)
        run = await validate(session, row)
        html = receipt_summary(document, run)
    else:
        html = fiscal.print_html(document)
    return HTMLResponse(html, headers={'Cache-Control': 'private, no-store',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"})


@backoffice_router.get('/integrations/provider-sandbox/readiness')
async def sandbox_readiness(session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    from app.services import sandbox
    return await sandbox.readiness(session, actor)


@backoffice_router.post('/orders/{order_id}/sandbox-qr-payment-attempts', response_model=QRAttemptResponse, status_code=201)
async def sandbox_create(order_id: int, payload: QRCreate, key: str = Header(alias='Idempotency-Key'),
    session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    from app.services import sandbox
    return await sandbox.create(session, actor, order_id, payload, key)


@backoffice_router.post('/qr-payment-attempts/{attempt_id}/sandbox-refresh', response_model=QRAttemptResponse)
async def sandbox_refresh(attempt_id: int, payload: QRCommandCreate, key: str = Header(alias='Idempotency-Key'),
    session: AsyncSession = Depends(get_db_session), actor: AdminUser = Depends(get_current_admin_user)):
    from app.services import sandbox
    return await sandbox.refresh(session, actor, attempt_id, payload, key)
