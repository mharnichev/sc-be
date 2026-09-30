"""Admin-only inventory workflow endpoints."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.dependencies.common import PaginationDep
from app.models.admin_user import AdminUser
from app.models.order import ProcurementStatus
from app.schemas.common import PaginatedResponse
from app.schemas.inventory import (
    CountCreateRequest,
    CountItemUpsertRequest,
    CountResponse,
    InventoryMovementResponse,
    InventoryProductResponse,
    ManualOperationRequest,
    MarkOrderedRequest,
    ProcurementQueueItemResponse,
    ProductInventorySettingsUpdate,
    ReceiptCreateRequest,
    ReceiptItemCreateRequest,
    ReceiptResponse,
)
from app.services.inventory import inventory_service

backoffice_router = APIRouter(dependencies=[Depends(get_current_admin_user)])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


@backoffice_router.get("/stock", response_model=PaginatedResponse[InventoryProductResponse])
async def list_stock(
    pagination: PaginationDep,
    search: str | None = Query(default=None, max_length=255),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[InventoryProductResponse]:
    items, total = await inventory_service.list_stock(
        session, page=pagination.page, page_size=pagination.page_size, search=search
    )
    return PaginatedResponse(total=total, page=pagination.page, page_size=pagination.page_size, items=items)


@backoffice_router.get("/products/barcode/{barcode}", response_model=InventoryProductResponse)
async def get_product_by_barcode(barcode: str, session: AsyncSession = Depends(get_db_session)) -> InventoryProductResponse:
    product = await inventory_service.get_product_by_barcode(session, barcode)
    if product is None:
        raise _not_found("Product not found")
    return product


@backoffice_router.patch("/products/{product_id}/settings", response_model=InventoryProductResponse)
async def update_product_settings(
    product_id: int,
    payload: ProductInventorySettingsUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> InventoryProductResponse:
    product = await inventory_service.update_product_settings(
        session, product_id, barcode=payload.barcode, allow_backorder=payload.allow_backorder
    )
    if product is None:
        raise _not_found("Product not found")
    return product


@backoffice_router.get("/procurement", response_model=list[ProcurementQueueItemResponse])
async def list_procurement_queue(
    statuses: list[ProcurementStatus] | None = Query(default=None, alias="status"),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProcurementQueueItemResponse]:
    return await inventory_service.list_procurement_queue(session, statuses)


@backoffice_router.post("/procurement/mark-ordered", status_code=status.HTTP_204_NO_CONTENT)
async def mark_procurement_ordered(
    payload: MarkOrderedRequest,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    await inventory_service.mark_ordered(session, payload.order_item_ids, current_user.id)


@backoffice_router.post("/receipts", response_model=ReceiptResponse, status_code=status.HTTP_201_CREATED)
async def create_receipt(
    payload: ReceiptCreateRequest,
    idempotency_key: IdempotencyKey,
    session: AsyncSession = Depends(get_db_session),
) -> ReceiptResponse:
    return await inventory_service.create_receipt(
        session, reason=payload.reason, comment=payload.comment, idempotency_key=idempotency_key
    )


@backoffice_router.get("/receipts/{receipt_id}", response_model=ReceiptResponse)
async def get_receipt(receipt_id: int, session: AsyncSession = Depends(get_db_session)) -> ReceiptResponse:
    receipt = await inventory_service.get_receipt(session, receipt_id)
    if receipt is None:
        raise _not_found("Receipt not found")
    return receipt


@backoffice_router.post("/receipts/{receipt_id}/items", response_model=ReceiptResponse)
async def add_receipt_item(
    receipt_id: int,
    payload: ReceiptItemCreateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> ReceiptResponse:
    if payload.product_id is None and payload.barcode is None:
        raise HTTPException(status_code=422, detail="product_id or barcode is required")
    return await inventory_service.add_receipt_item(
        session, receipt_id, product_id=payload.product_id, barcode=payload.barcode,
        quantity=payload.quantity, purchase_unit_cost=payload.purchase_unit_cost,
        batch_number=payload.batch_number, expiration_date=payload.expiration_date,
        allocations=[allocation.model_dump() for allocation in payload.allocations],
    )


@backoffice_router.post("/receipts/{receipt_id}/post", response_model=ReceiptResponse)
async def post_receipt(
    receipt_id: int,
    idempotency_key: IdempotencyKey,
    current_user: AdminUser = Depends(get_current_admin_user),
    session: AsyncSession = Depends(get_db_session),
) -> ReceiptResponse:
    return await inventory_service.post_receipt(session, receipt_id, current_user.id, idempotency_key)


async def _manual_operation(
    movement_type: str,
    payload: ManualOperationRequest,
    idempotency_key: str,
    current_user: AdminUser,
    session: AsyncSession,
) -> InventoryMovementResponse:
    product_id = payload.product_id
    if product_id is None:
        if payload.barcode is None:
            raise HTTPException(status_code=422, detail="product_id or barcode is required")
        product = await inventory_service.get_product_by_barcode(session, payload.barcode)
        if product is None:
            raise _not_found("Product not found")
        product_id = product.id
    return await inventory_service.manual_operation(
        session, movement_type=movement_type, product_id=product_id, quantity=payload.quantity,
        reason=payload.reason, admin_user_id=current_user.id, idempotency_key=idempotency_key,
        order_id=payload.order_id, order_item_id=payload.order_item_id,
    )


@backoffice_router.post("/operations/opening-balance", response_model=InventoryMovementResponse)
async def opening_balance(payload: ManualOperationRequest, idempotency_key: IdempotencyKey, current_user: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)) -> InventoryMovementResponse:
    return await _manual_operation("opening_balance", payload, idempotency_key, current_user, session)


@backoffice_router.post("/operations/receipt", response_model=InventoryMovementResponse)
async def manual_receipt(payload: ManualOperationRequest, idempotency_key: IdempotencyKey, current_user: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)) -> InventoryMovementResponse:
    return await _manual_operation("receipt", payload, idempotency_key, current_user, session)


@backoffice_router.post("/operations/customer-return", response_model=InventoryMovementResponse)
async def customer_return(payload: ManualOperationRequest, idempotency_key: IdempotencyKey, current_user: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)) -> InventoryMovementResponse:
    return await _manual_operation("customer_return", payload, idempotency_key, current_user, session)


@backoffice_router.post("/operations/write-off", response_model=InventoryMovementResponse)
async def write_off(payload: ManualOperationRequest, idempotency_key: IdempotencyKey, current_user: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)) -> InventoryMovementResponse:
    return await _manual_operation("write_off", payload, idempotency_key, current_user, session)


@backoffice_router.get("/products/{product_id}/movements", response_model=PaginatedResponse[InventoryMovementResponse])
async def list_movements(product_id: int, pagination: PaginationDep, session: AsyncSession = Depends(get_db_session)) -> PaginatedResponse[InventoryMovementResponse]:
    items, total = await inventory_service.list_movements(
        session, product_id, page=pagination.page, page_size=pagination.page_size
    )
    return PaginatedResponse(total=total, page=pagination.page, page_size=pagination.page_size, items=items)


@backoffice_router.post("/counts", response_model=CountResponse, status_code=status.HTTP_201_CREATED)
async def create_count(payload: CountCreateRequest, idempotency_key: IdempotencyKey, session: AsyncSession = Depends(get_db_session)) -> CountResponse:
    return await inventory_service.create_count(session, reason=payload.reason, idempotency_key=idempotency_key)


@backoffice_router.get("/counts/{count_id}", response_model=CountResponse)
async def get_count(count_id: int, session: AsyncSession = Depends(get_db_session)) -> CountResponse:
    count = await inventory_service.get_count(session, count_id)
    if count is None:
        raise _not_found("Inventory count not found")
    return count


@backoffice_router.put("/counts/{count_id}/items", response_model=CountResponse)
async def set_count_item(count_id: int, payload: CountItemUpsertRequest, session: AsyncSession = Depends(get_db_session)) -> CountResponse:
    if payload.product_id is None and payload.barcode is None:
        raise HTTPException(status_code=422, detail="product_id or barcode is required")
    return await inventory_service.set_count_item(
        session, count_id, product_id=payload.product_id, barcode=payload.barcode,
        counted_quantity=payload.counted_quantity,
    )


@backoffice_router.post("/counts/{count_id}/post", response_model=CountResponse)
async def post_count(count_id: int, idempotency_key: IdempotencyKey, current_user: AdminUser = Depends(get_current_admin_user), session: AsyncSession = Depends(get_db_session)) -> CountResponse:
    return await inventory_service.post_count(session, count_id, current_user.id, idempotency_key)
