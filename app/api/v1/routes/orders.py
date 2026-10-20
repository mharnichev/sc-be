from __future__ import annotations

from datetime import UTC, datetime

from fastapi.responses import HTMLResponse
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db_session
from app.core.config import settings
from app.core.rate_limit import SlidingWindowRateLimiter, privacy_safe_rate_key
from app.dependencies.auth import get_current_admin_user, get_current_customer, get_optional_current_customer
from app.dependencies.common import PaginationDep
from app.models.admin_user import AdminUser
from app.models.order import Order, OrderItem, PreliminaryDocument
from app.models.product import Product
from app.models.customer import Customer
from app.repositories.base import BaseRepository
from app.schemas.common import PaginatedResponse
from app.schemas.order import (
    BackofficeOrderResponse,
    POSOrderCreate, OrderPaymentCreate, OrderHandoverCreate,
    CustomerOrderResponse,
    OrderCreate,
    OrderFulfillmentResponse,
    PublicOrderResponse,
    PreliminaryDocumentResponse,
    PreliminaryDocumentCreate,
    document_response,
    OrderStatusUpdate,
    OrderSummaryResponse,
)
from app.schemas.shipment import (ShipmentCreate, ShipmentCommand, ShipmentFixture,
    GuestTrackingResponse, MockShipmentFixtureResponse, RealShipmentAttach, TrackingRefreshResponse)
from app.schemas.mock_cod import (MockCODPaymentCreate, MockReturnShipmentCreate, MockParcelReceiptCreate,
    MockSettlementResponse)
from app.services import local_sales, sales_access
from app.services.order import OrderService
from app.services.shop_ingress_limits import admit_shop_ingress
from app.services.order_documents import digest, operation_key, make_document, token_document_id, printable

public_router = APIRouter()
backoffice_router = APIRouter()
documents_router = APIRouter()
repo = BaseRepository(Order)
service = OrderService()
tracking_rate_limiter = SlidingWindowRateLimiter()


@public_router.get("/me", response_model=PaginatedResponse[CustomerOrderResponse])
async def list_my_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[CustomerOrderResponse]:
    base = select(Order).where(Order.customer_id == current_customer.id)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.options(
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.images),
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.image_variants),
    ).order_by(Order.created_at.desc(), Order.id.desc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    orders = (await session.execute(stmt)).scalars().all()
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[CustomerOrderResponse.from_order(order) for order in orders],
    )


@public_router.get("/me/{order_id}", response_model=CustomerOrderResponse)
async def get_my_order(
    order_id: int,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> CustomerOrderResponse:
    stmt = select(Order).options(
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.images),
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.image_variants),
    ).where(
        Order.id == order_id, Order.customer_id == current_customer.id,
    )
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return CustomerOrderResponse.from_order(order, detail=True)


async def require_shop_checkout_enabled() -> None:
    if not settings.shop_checkout_enabled and not (
        settings.shop_checkout_pilot_enabled and settings.checkout_pilot_customer_ids
    ):
        raise HTTPException(status_code=503, detail={
            "code": "shop_checkout_closed",
            "message": "Shop checkout is temporarily unavailable",
        })


async def admit_checkout_source(request: Request) -> None:
    await admit_shop_ingress(request, "checkout")


@public_router.post("", response_model=PublicOrderResponse, status_code=status.HTTP_201_CREATED,
                    dependencies=[Depends(require_shop_checkout_enabled), Depends(admit_checkout_source)])
async def create_order(
    payload: OrderCreate,
    request: Request,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PublicOrderResponse:
    if current_customer is None and request.headers.get("authorization") is not None:
        raise HTTPException(status_code=401, detail="Invalid or expired customer authentication")
    if not settings.shop_checkout_enabled and not (
        settings.shop_checkout_pilot_enabled
        and current_customer is not None
        and current_customer.is_active
        and current_customer.phone_verified_at is not None
        and current_customer.id in settings.checkout_pilot_customer_ids
    ):
        raise HTTPException(status_code=503, detail={
            "code": "shop_checkout_closed",
            "message": "Shop checkout is temporarily unavailable",
        })
    order = await service.create_order(session, payload, current_customer=current_customer, idempotency_key=idempotency_key)
    if getattr(order, "_checkout_response", None) is not None:
        return PublicOrderResponse.model_validate(order._checkout_response)
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    result = await session.execute(stmt)
    refreshed = result.scalar_one()
    return PublicOrderResponse.model_validate(refreshed)


@backoffice_router.get("", response_model=PaginatedResponse[OrderSummaryResponse])
async def list_orders(
    pagination: PaginationDep,
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[OrderSummaryResponse]:
    stmt = select(Order).options(selectinload(Order.items)).order_by(Order.created_at.desc())
    if await sales_access.restricted_master(session, current_admin):
        local_sales.require_permission(current_admin)
        if not current_admin.can_accept_qr_payments:
            raise HTTPException(403, "Sales and QR payment permissions required")
        stmt = stmt.where(Order.created_by_user_id == current_admin.id,
            Order.channel == "backoffice_pos")
    items, total = await repo.list(session, stmt=stmt, page=pagination.page, page_size=pagination.page_size)
    return PaginatedResponse[OrderSummaryResponse](
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        items=[OrderSummaryResponse.model_validate(item) for item in items],
    )


@backoffice_router.get("/pos-capabilities")
async def pos_capabilities(current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await local_sales.capabilities(session, current_admin)


@backoffice_router.post("", response_model=BackofficeOrderResponse, status_code=201)
async def prepare_pos_order(payload: POSOrderCreate, idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await local_sales.prepare(session, current_admin, payload, idempotency_key)


@backoffice_router.post("/{order_id}/payments", response_model=BackofficeOrderResponse)
async def record_order_payment(order_id: int, payload: OrderPaymentCreate, idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await local_sales.command(session, current_admin, order_id, payload, idempotency_key)


@backoffice_router.post("/{order_id}/handover", response_model=BackofficeOrderResponse)
async def record_order_handover(order_id: int, payload: OrderHandoverCreate, idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    return await local_sales.command(session, current_admin, order_id, payload, idempotency_key, handover=True)


@backoffice_router.get("/{order_id}/fulfillment", response_model=OrderFulfillmentResponse)
async def get_order_fulfillment(
    order_id: int,
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrderFulfillmentResponse:
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    order = (await session.execute(stmt)).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    await sales_access.own_sale(session, current_admin, order)
    return OrderFulfillmentResponse.model_validate(order)


@backoffice_router.get("/{order_id}", response_model=BackofficeOrderResponse)
async def get_order(
    order_id: int,
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeOrderResponse:
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return await local_sales.response(order, session, current_admin)


@backoffice_router.patch("/{order_id}/status", response_model=BackofficeOrderResponse)
async def update_order_status(
    order_id: int,
    payload: OrderStatusUpdate,
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> BackofficeOrderResponse:
    await sales_access.deny_master(session, current_admin)
    await service.update_status(
        session,
        order_id,
        payload.status,
        admin_user_id=current_admin.id,
    )
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    result = await session.execute(stmt)
    refreshed = result.scalar_one()
    response = BackofficeOrderResponse.model_validate(refreshed)
    response.preliminary_documents = [document_response(d, admin=True) for d in refreshed.preliminary_documents]
    return response


def document_html(document: PreliminaryDocument) -> HTMLResponse:
    return HTMLResponse(printable(document), headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
    })


@documents_router.get("/{token}", response_class=HTMLResponse)
async def guest_document(
    token: str, session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    document = await session.get(PreliminaryDocument, token_document_id(token))
    if document is None or document.revoked_at or not document.guest_expires_at:
        raise HTTPException(404, "Document not found")
    expiry = document.guest_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= datetime.now(UTC):
        raise HTTPException(404, "Document not found")
    return document_html(document)


@public_router.get("/me/{order_id}/preliminary-documents/{document_id}/print", response_class=HTMLResponse)
async def customer_document(
    order_id: int, document_id: int,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    document = (await session.execute(
        select(PreliminaryDocument).join(Order, Order.id == PreliminaryDocument.order_id)
        .where(PreliminaryDocument.id == document_id, Order.id == order_id,
               Order.customer_id == current_customer.id)
    )).scalar_one_or_none()
    if document is None:
        raise HTTPException(404, "Document not found")
    return document_html(document)


@backoffice_router.get("/{order_id}/preliminary-documents/{document_id}/print", response_class=HTMLResponse)
async def admin_document(
    order_id: int, document_id: int,
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    await sales_access.own_sale(session, current_admin, await local_sales.load(session, order_id))
    document = (await session.execute(
        select(PreliminaryDocument).where(
            PreliminaryDocument.id == document_id, PreliminaryDocument.order_id == order_id,
        )
    )).scalar_one_or_none()
    if document is None:
        raise HTTPException(404, "Document not found")
    return document_html(document)


@backoffice_router.post("/{order_id}/preliminary-documents", response_model=PreliminaryDocumentResponse, status_code=201)
async def create_preliminary_document(
    order_id: int, payload: PreliminaryDocumentCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> PreliminaryDocumentResponse:
    await sales_access.deny_master(session, current_admin)
    key = operation_key(idempotency_key, f"preliminary:{current_admin.id}:{order_id}")
    request_hash = digest(payload.model_dump())
    order = (await session.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, "Order not found")
    existing = (await session.execute(select(PreliminaryDocument).where(PreliminaryDocument.operation_key == key))).scalar_one_or_none()
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(409, detail={"code": "idempotency_conflict", "message": "Key already used for another request"})
        return document_response(existing, admin=True)
    if order.revision != payload.expected_revision:
        raise HTTPException(409, detail={"code": "revision_conflict", "message": "Reload the order before creating a document"})
    version = (await session.scalar(select(func.max(PreliminaryDocument.version)).where(PreliminaryDocument.order_id == order_id)) or 0) + 1
    document = make_document(order, version=version, operation=key, request_hash=request_hash)
    session.add(document)
    order.revision += 1
    await session.commit()
    return document_response(document, admin=True)


@public_router.get("/tracking/{token}", response_model=GuestTrackingResponse)
async def guest_order_tracking(token: str, session: AsyncSession = Depends(get_db_session)):
    from app.services.shipments import guest_tracking
    return await guest_tracking(session, token)


@backoffice_router.post("/{order_id}/mock-shipment-fixtures", response_model=MockShipmentFixtureResponse)
async def create_mock_shipment_fixture(order_id: int, payload: ShipmentFixture,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.shipments import fixture
    return await fixture(session, current_admin, order_id, payload, idempotency_key)


@backoffice_router.post("/{order_id}/shipments", response_model=BackofficeOrderResponse, status_code=201)
async def create_order_shipment(order_id: int, payload: ShipmentCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.shipments import create
    return await create(session, current_admin, order_id, payload, idempotency_key)


@backoffice_router.post("/{order_id}/shipments/{shipment_id}/refresh", response_model=BackofficeOrderResponse)
async def refresh_order_shipment(order_id: int, shipment_id: int, payload: ShipmentCommand,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.shipments import command
    return await command(session, current_admin, order_id, shipment_id, payload, idempotency_key, 'refresh')


@backoffice_router.post("/{order_id}/shipments/{shipment_id}/cancel", response_model=BackofficeOrderResponse)
async def cancel_order_shipment(order_id: int, shipment_id: int, payload: ShipmentCommand,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.shipments import command
    return await command(session, current_admin, order_id, shipment_id, payload, idempotency_key, 'cancel')


@backoffice_router.post("/{order_id}/shipments/{shipment_id}/handover", response_model=BackofficeOrderResponse)
async def handover_order_shipment(order_id: int, shipment_id: int, payload: ShipmentCommand,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.shipments import command
    return await command(session, current_admin, order_id, shipment_id, payload, idempotency_key, 'handover')


@backoffice_router.post("/{order_id}/shipments/{shipment_id}/label", response_model=BackofficeOrderResponse)
async def prepare_order_shipment_label(order_id: int, shipment_id: int, payload: ShipmentCommand,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.shipments import command
    return await command(session, current_admin, order_id, shipment_id, payload, idempotency_key, 'label')


@backoffice_router.get("/{order_id}/shipments/{shipment_id}/label", response_class=HTMLResponse)
async def print_order_shipment_label(order_id: int, shipment_id: int,
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    await sales_access.deny_master(session, current_admin)
    from app.models.shipment import Shipment
    from app.services.shipments import allowed, label_html
    if not allowed(current_admin):
        raise HTTPException(403, 'Shipment permission required')
    shipment = (await session.execute(select(Shipment).where(Shipment.id == shipment_id, Shipment.order_id == order_id))).scalar_one_or_none()
    if not shipment or not shipment.simulation or not shipment.tracking_number or shipment.status in ('unknown', 'cancelled'):
        raise HTTPException(404, 'Label not found')
    return HTMLResponse(label_html(shipment), headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"})


@public_router.post("/me/{order_id}/shipments/refresh", response_model=TrackingRefreshResponse)
async def refresh_my_shipments(order_id: int, request: Request,
    current_customer: Customer = Depends(get_current_customer),
    session: AsyncSession = Depends(get_db_session)):
    from app.services.customer_tracking import queue_refresh
    tracking_rate_limiter.check(privacy_safe_rate_key(request, str(current_customer.id), "tracking"),
                                limit=10, window_seconds=60)
    return await queue_refresh(session, order_id, current_customer.id)


@backoffice_router.post("/{order_id}/real-shipments", status_code=201)
async def attach_real_shipment(order_id: int, payload: RealShipmentAttach,
    current_admin: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session)):
    await sales_access.deny_master(session, current_admin)
    from app.services.customer_tracking import attach_existing
    return await attach_existing(session, current_admin, order_id, payload)


@backoffice_router.get('/{order_id}/shipments/{shipment_id}/settlement', response_model=MockSettlementResponse)
async def mock_shipment_settlement(order_id: int, shipment_id: int,
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.mock_cod import settlement
    return await settlement(session, current_admin, order_id, shipment_id)


@backoffice_router.post('/{order_id}/shipments/{shipment_id}/cod-payment', response_model=BackofficeOrderResponse)
async def confirm_mock_cod_payment(order_id: int, shipment_id: int, payload: MockCODPaymentCreate,
    idempotency_key: str = Header(alias='Idempotency-Key'),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.mock_cod import confirm_cod
    return await confirm_cod(session, current_admin, order_id, shipment_id, payload, idempotency_key)


@backoffice_router.post('/{order_id}/shipments/{shipment_id}/return-shipment', response_model=BackofficeOrderResponse)
async def create_mock_return_shipment(order_id: int, shipment_id: int, payload: MockReturnShipmentCreate,
    idempotency_key: str = Header(alias='Idempotency-Key'),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.mock_cod import create_return_shipment
    return await create_return_shipment(session, current_admin, order_id, shipment_id, payload, idempotency_key)


@backoffice_router.post('/{order_id}/shipments/{shipment_id}/return-receipt', response_model=BackofficeOrderResponse)
async def receive_mock_return_parcel(order_id: int, shipment_id: int, payload: MockParcelReceiptCreate,
    idempotency_key: str = Header(alias='Idempotency-Key'),
    current_admin: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)):
    from app.services.mock_cod import receive_return
    return await receive_return(session, current_admin, order_id, shipment_id, payload, idempotency_key)
