from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.inventory import (
    InventoryCount,
    InventoryCountItem,
    InventoryCountStatus,
    InventoryMovement,
    InventoryMovementType,
    InventoryReceipt,
    InventoryReceiptAllocation,
    InventoryReceiptItem,
    InventoryReceiptStatus,
)
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
from app.models.product import Product


def normalize_barcode(value: str | None) -> str | None:
    """Return the canonical scanner value used for storage and exact lookup."""
    if value is None:
        return None
    normalized = "".join(value.split()).upper()
    if not normalized:
        return None
    if len(normalized) > 64:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Barcode is too long")
    return normalized


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


class InventoryService:
    @staticmethod
    async def _locked_product(session: AsyncSession, product_id: int) -> Product:
        product = (
            await session.execute(
                select(Product)
                .where(Product.id == product_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if product is None:
            raise _not_found("Product not found")
        return product

    @staticmethod
    async def _product_from_identifier(
        session: AsyncSession,
        *,
        product_id: int | None,
        barcode: str | None,
        lock: bool = False,
    ) -> Product:
        if (product_id is None) == (barcode is None):
            raise _bad_request("Provide exactly one of product_id or barcode")
        if product_id is not None:
            if lock:
                return await InventoryService._locked_product(session, product_id)
            product = await session.get(Product, product_id)
        else:
            normalized = normalize_barcode(barcode)
            if normalized is None:
                raise _bad_request("Barcode is required")
            stmt = select(Product).where(Product.barcode == normalized)
            if lock:
                stmt = stmt.with_for_update().execution_options(populate_existing=True)
            product = (await session.execute(stmt)).scalar_one_or_none()
        if product is None:
            raise _not_found("Product not found")
        return product

    @staticmethod
    def _movement(
        *,
        product: Product,
        movement_type: InventoryMovementType,
        quantity: int,
        on_hand_delta: int,
        reserved_delta: int,
        reason: str | None,
        admin_user_id: int | None = None,
        order_id: int | None = None,
        order_item_id: int | None = None,
        receipt_id: int | None = None,
        inventory_count_id: int | None = None,
        reference_key: str | None = None,
    ) -> InventoryMovement:
        return InventoryMovement(
            product_id=product.id,
            movement_type=movement_type,
            quantity=quantity,
            on_hand_delta=on_hand_delta,
            reserved_delta=reserved_delta,
            order_id=order_id,
            order_item_id=order_item_id,
            receipt_id=receipt_id,
            inventory_count_id=inventory_count_id,
            reason=reason,
            performed_by_user_id=admin_user_id,
            reference_key=reference_key,
        )

    async def list_stock(
        self,
        session: AsyncSession,
        *,
        page: int,
        page_size: int,
        search: str | None = None,
    ) -> tuple[list[Product], int]:
        stmt = select(Product).order_by(Product.name.asc(), Product.id.asc())
        if search and search.strip():
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(Product.name.ilike(pattern), Product.sku.ilike(pattern), Product.barcode.ilike(pattern))
            )
        total = int((await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))) or 0)
        products = list(
            (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))).scalars().all()
        )
        return products, total

    async def get_product_by_barcode(self, session: AsyncSession, barcode: str) -> Product:
        normalized = normalize_barcode(barcode)
        if normalized is None:
            raise _bad_request("Barcode is required")
        product = (
            await session.execute(select(Product).where(Product.barcode == normalized))
        ).scalar_one_or_none()
        if product is None:
            raise _not_found("Product not found")
        return product

    async def update_product_settings(
        self,
        session: AsyncSession,
        product_id: int,
        *,
        barcode: str | None,
        allow_backorder: bool,
    ) -> Product:
        product = await self._locked_product(session, product_id)
        normalized = normalize_barcode(barcode)
        if normalized is not None:
            duplicate = (
                await session.execute(
                    select(Product.id).where(Product.barcode == normalized, Product.id != product.id)
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise _conflict("Barcode is already assigned to another product")
        product.barcode = normalized
        product.allow_backorder = allow_backorder
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _conflict("Barcode is already assigned to another product") from exc
        except Exception:
            await session.rollback()
            raise
        await session.refresh(product)
        return product

    async def list_procurement_queue(
        self,
        session: AsyncSession,
        statuses: Iterable[ProcurementStatus] | None = None,
    ) -> list[dict[str, Any]]:
        wanted = tuple(statuses or (ProcurementStatus.to_order, ProcurementStatus.ordered))
        stmt = (
            select(OrderItem, Product)
            .join(Product, Product.id == OrderItem.product_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                OrderItem.procurement_status.in_(wanted),
                OrderItem.quantity_to_order > OrderItem.quantity_received_for_order,
                Order.status.not_in((OrderStatus.cancelled, OrderStatus.completed)),
            )
            .order_by(Product.name.asc(), OrderItem.order_id.asc(), OrderItem.id.asc())
        )
        grouped: dict[int, dict[str, Any]] = {}
        for order_item, product in (await session.execute(stmt)).all():
            remaining = order_item.quantity_to_order - order_item.quantity_received_for_order
            group = grouped.setdefault(
                product.id,
                {
                    "product_id": product.id,
                    "product_name": product.name,
                    "sku": product.sku,
                    "barcode": product.barcode,
                    "total_quantity_required": 0,
                    "requirements": [],
                },
            )
            group["total_quantity_required"] += remaining
            group["requirements"].append(
                {
                    "order_id": order_item.order_id,
                    "order_item_id": order_item.id,
                    "quantity_required": remaining,
                    "quantity_ordered": order_item.quantity_to_order,
                    "quantity_received": order_item.quantity_received_for_order,
                    "procurement_status": order_item.procurement_status,
                }
            )
        return list(grouped.values())

    async def mark_ordered(
        self,
        session: AsyncSession,
        order_item_ids: list[int],
        admin_user_id: int,
    ) -> list[OrderItem]:
        if not order_item_ids or len(order_item_ids) != len(set(order_item_ids)):
            raise _bad_request("order_item_ids must be a non-empty unique list")
        items = list(
            (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.id.in_(sorted(order_item_ids)))
                    .order_by(OrderItem.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if len(items) != len(order_item_ids):
            raise _not_found("One or more order items were not found")
        for item in items:
            if item.quantity_to_order <= item.quantity_received_for_order:
                raise _conflict(f"Order item {item.id} has no outstanding procurement quantity")
            if item.procurement_status not in (ProcurementStatus.to_order, ProcurementStatus.ordered):
                raise _conflict(f"Order item {item.id} cannot be marked ordered")
            item.procurement_status = ProcurementStatus.ordered
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return items

    async def create_receipt(
        self,
        session: AsyncSession,
        *,
        reason: str,
        comment: str | None,
        idempotency_key: str | None,
    ) -> InventoryReceipt:
        reason = reason.strip()
        if not reason:
            raise _bad_request("Reason is required")
        if idempotency_key:
            existing = (
                await session.execute(
                    select(InventoryReceipt).where(InventoryReceipt.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return await self.get_receipt(session, existing.id)
        receipt = InventoryReceipt(reason=reason, comment=comment, idempotency_key=idempotency_key)
        session.add(receipt)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if idempotency_key:
                existing = (
                    await session.execute(
                        select(InventoryReceipt).where(InventoryReceipt.idempotency_key == idempotency_key)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return await self.get_receipt(session, existing.id)
            raise
        except Exception:
            await session.rollback()
            raise
        await session.refresh(receipt)
        return await self.get_receipt(session, receipt.id)

    async def get_receipt(self, session: AsyncSession, receipt_id: int) -> InventoryReceipt:
        receipt = (
            await session.execute(
                select(InventoryReceipt)
                .options(
                    selectinload(InventoryReceipt.items).selectinload(InventoryReceiptItem.allocations),
                    selectinload(InventoryReceipt.items).selectinload(InventoryReceiptItem.product),
                )
                .where(InventoryReceipt.id == receipt_id)
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise _not_found("Receipt not found")
        return receipt

    async def add_receipt_item(
        self,
        session: AsyncSession,
        receipt_id: int,
        *,
        product_id: int | None,
        barcode: str | None,
        quantity: int,
        purchase_unit_cost: Decimal,
        batch_number: str | None,
        expiration_date: date | None,
        allocations: list[dict[str, int]],
    ) -> InventoryReceipt:
        receipt = (
            await session.execute(
                select(InventoryReceipt).where(InventoryReceipt.id == receipt_id).with_for_update()
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise _not_found("Receipt not found")
        if receipt.status != InventoryReceiptStatus.draft:
            raise _conflict("Posted receipts cannot be edited")
        identified_product = await self._product_from_identifier(
            session, product_id=product_id, barcode=barcode, lock=False
        )
        if quantity <= 0:
            raise _bad_request("Quantity must be positive")

        allocation_total = sum(item["quantity"] for item in allocations)
        if allocation_total > quantity:
            raise _bad_request("Allocated quantity cannot exceed receipt item quantity")
        if len({item["order_item_id"] for item in allocations}) != len(allocations):
            raise _bad_request("Each order item may be allocated only once per receipt line")

        order_items: dict[int, OrderItem] = {}
        if allocations:
            ids = sorted(item["order_item_id"] for item in allocations)
            locked = (
                await session.execute(
                    select(OrderItem)
                    .where(OrderItem.id.in_(ids))
                    .order_by(OrderItem.id)
                    .with_for_update()
                )
            ).scalars().all()
            order_items = {item.id: item for item in locked}
            if len(order_items) != len(ids):
                raise _not_found("One or more order items were not found")
            for allocation in allocations:
                order_item = order_items[allocation["order_item_id"]]
                if allocation["quantity"] <= 0:
                    raise _bad_request("Allocation quantity must be positive")
                if order_item.product_id != identified_product.id:
                    raise _bad_request("Receipt allocation product does not match the order item")
                if order_item.procurement_status != ProcurementStatus.ordered:
                    raise _conflict("Receipt allocations require an ordered procurement item")
                remaining = order_item.quantity_to_order - order_item.quantity_received_for_order
                already_allocated = int(
                    (
                        await session.scalar(
                            select(func.coalesce(func.sum(InventoryReceiptAllocation.quantity), 0))
                            .join(
                                InventoryReceiptItem,
                                InventoryReceiptItem.id == InventoryReceiptAllocation.receipt_item_id,
                            )
                            .where(
                                InventoryReceiptItem.receipt_id == receipt.id,
                                InventoryReceiptAllocation.order_item_id == order_item.id,
                            )
                        )
                    )
                    or 0
                )
                if allocation["quantity"] + already_allocated > remaining:
                    raise _conflict("Receipt allocation exceeds the outstanding order requirement")

        # Match receipt posting and order fulfillment lock order: requirements
        # first, then the product balance row. This avoids cross-receipt
        # deadlocks while still refreshing the balance before it can be used.
        product = await self._locked_product(session, identified_product.id)
        if barcode is not None and product.barcode != normalize_barcode(barcode):
            raise _conflict("Barcode assignment changed while editing the receipt")

        existing = (
            await session.execute(
                select(InventoryReceiptItem).where(
                    InventoryReceiptItem.receipt_id == receipt.id,
                    InventoryReceiptItem.product_id == product.id,
                    InventoryReceiptItem.purchase_unit_cost == purchase_unit_cost,
                    InventoryReceiptItem.batch_number.is_(None)
                    if batch_number is None
                    else InventoryReceiptItem.batch_number == batch_number,
                    InventoryReceiptItem.expiration_date.is_(None)
                    if expiration_date is None
                    else InventoryReceiptItem.expiration_date == expiration_date,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = InventoryReceiptItem(
                receipt_id=receipt.id,
                product_id=product.id,
                quantity=quantity,
                purchase_unit_cost=purchase_unit_cost,
                batch_number=batch_number,
                expiration_date=expiration_date,
            )
            session.add(existing)
            await session.flush()
        else:
            existing.quantity += quantity
        for allocation in allocations:
            receipt_allocation = (
                await session.execute(
                    select(InventoryReceiptAllocation).where(
                        InventoryReceiptAllocation.receipt_item_id == existing.id,
                        InventoryReceiptAllocation.order_item_id == allocation["order_item_id"],
                    )
                )
            ).scalar_one_or_none()
            if receipt_allocation is None:
                session.add(
                    InventoryReceiptAllocation(
                        receipt_item_id=existing.id,
                        order_item_id=allocation["order_item_id"],
                        quantity=allocation["quantity"],
                    )
                )
            else:
                receipt_allocation.quantity += allocation["quantity"]
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return await self.get_receipt(session, receipt.id)

    async def post_receipt(
        self,
        session: AsyncSession,
        receipt_id: int,
        admin_user_id: int,
        idempotency_key: str | None,
    ) -> InventoryReceipt:
        receipt = (
            await session.execute(
                select(InventoryReceipt)
                .options(
                    selectinload(InventoryReceipt.items).selectinload(InventoryReceiptItem.allocations),
                    selectinload(InventoryReceipt.items).selectinload(InventoryReceiptItem.product),
                )
                .where(InventoryReceipt.id == receipt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise _not_found("Receipt not found")
        if receipt.status == InventoryReceiptStatus.posted:
            return receipt
        if not receipt.items:
            raise _bad_request("A receipt must contain at least one item")
        if idempotency_key:
            other = (
                await session.execute(
                    select(InventoryReceipt.id).where(
                        InventoryReceipt.posted_idempotency_key == idempotency_key,
                        InventoryReceipt.id != receipt.id,
                    )
                )
            ).scalar_one_or_none()
            if other is not None:
                raise _conflict("Idempotency key is already in use")
            receipt.posted_idempotency_key = idempotency_key

        allocation_ids = sorted(
            {allocation.order_item_id for item in receipt.items for allocation in item.allocations}
        )
        order_items: dict[int, OrderItem] = {}
        orders: dict[int, Order] = {}
        if allocation_ids:
            locked_items = list(
                (
                    await session.execute(
                        select(OrderItem)
                        .where(OrderItem.id.in_(allocation_ids))
                        .order_by(OrderItem.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            order_items = {item.id: item for item in locked_items}
            order_ids = sorted({item.order_id for item in locked_items})
            orders = {
                order.id: order
                for order in (
                    await session.execute(
                        select(Order).where(Order.id.in_(order_ids)).order_by(Order.id).with_for_update()
                    )
                ).scalars()
            }
        product_ids = sorted({item.product_id for item in receipt.items})
        products = {
            product.id: product
            for product in (
                await session.execute(
                    select(Product)
                    .where(Product.id.in_(product_ids))
                    .order_by(Product.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }

        for receipt_item in receipt.items:
            allocated = sum(allocation.quantity for allocation in receipt_item.allocations)
            if allocated > receipt_item.quantity:
                raise _conflict("Receipt allocations exceed the received quantity")
            for allocation in receipt_item.allocations:
                order_item = order_items[allocation.order_item_id]
                order = orders[order_item.order_id]
                if order.status in (OrderStatus.cancelled, OrderStatus.completed):
                    raise _conflict("Cannot allocate a receipt to a terminal order")
                if order_item.procurement_status != ProcurementStatus.ordered:
                    raise _conflict("Receipt allocations require an ordered procurement item")
                remaining = order_item.quantity_to_order - order_item.quantity_received_for_order
                if allocation.quantity > remaining:
                    raise _conflict("Receipt allocation exceeds the outstanding order requirement")

        for receipt_item in receipt.items:
            product = products[receipt_item.product_id]
            product.stock_quantity += receipt_item.quantity
            session.add(
                self._movement(
                    product=product,
                    movement_type=InventoryMovementType.receipt,
                    quantity=receipt_item.quantity,
                    on_hand_delta=receipt_item.quantity,
                    reserved_delta=0,
                    receipt_id=receipt.id,
                    reason=receipt.reason,
                    admin_user_id=admin_user_id,
                    reference_key=f"receipt:{receipt.id}:item:{receipt_item.id}",
                )
            )
            for allocation in receipt_item.allocations:
                order_item = order_items[allocation.order_item_id]
                order_item.quantity_received_for_order += allocation.quantity
                order_item.procurement_status = (
                    ProcurementStatus.received
                    if order_item.quantity_received_for_order == order_item.quantity_to_order
                    else ProcurementStatus.ordered
                )
                product.reserved_quantity += allocation.quantity
                session.add(
                    self._movement(
                        product=product,
                        movement_type=InventoryMovementType.reservation,
                        quantity=allocation.quantity,
                        on_hand_delta=0,
                        reserved_delta=allocation.quantity,
                        order_id=order_item.order_id,
                        order_item_id=order_item.id,
                        receipt_id=receipt.id,
                        reason="Allocated supplier receipt to customer order",
                        admin_user_id=admin_user_id,
                        reference_key=f"receipt:{receipt.id}:allocation:{allocation.id}",
                    )
                )

        receipt.status = InventoryReceiptStatus.posted
        receipt.posted_at = datetime.now(timezone.utc)
        receipt.posted_by_user_id = admin_user_id
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _conflict("Receipt posting idempotency key is already in use") from exc
        except Exception:
            await session.rollback()
            raise
        return await self.get_receipt(session, receipt.id)

    async def manual_operation(
        self,
        session: AsyncSession,
        *,
        movement_type: InventoryMovementType,
        product_id: int,
        quantity: int,
        reason: str,
        admin_user_id: int,
        idempotency_key: str | None,
        order_id: int | None = None,
        order_item_id: int | None = None,
    ) -> InventoryMovement:
        if not isinstance(movement_type, InventoryMovementType):
            try:
                movement_type = InventoryMovementType(movement_type)
            except ValueError as exc:
                raise _bad_request("Unsupported manual inventory operation") from exc
        if quantity <= 0:
            raise _bad_request("Quantity must be positive")
        reason = reason.strip()
        if not reason:
            raise _bad_request("Reason is required")
        allowed = {
            InventoryMovementType.opening_balance,
            InventoryMovementType.receipt,
            InventoryMovementType.customer_return,
            InventoryMovementType.write_off,
        }
        if movement_type not in allowed:
            raise _bad_request("Unsupported manual inventory operation")
        if idempotency_key:
            existing = (
                await session.execute(
                    select(InventoryMovement).where(InventoryMovement.reference_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        product = await self._locked_product(session, product_id)
        if movement_type == InventoryMovementType.write_off:
            if product.stock_quantity - product.reserved_quantity < quantity:
                raise _conflict("Write-off exceeds available stock")
            on_hand_delta = -quantity
        else:
            on_hand_delta = quantity
        product.stock_quantity += on_hand_delta
        movement = self._movement(
            product=product,
            movement_type=movement_type,
            quantity=quantity,
            on_hand_delta=on_hand_delta,
            reserved_delta=0,
            reason=reason,
            admin_user_id=admin_user_id,
            order_id=order_id,
            order_item_id=order_item_id,
            reference_key=idempotency_key,
        )
        session.add(movement)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if idempotency_key:
                existing = (
                    await session.execute(
                        select(InventoryMovement).where(InventoryMovement.reference_key == idempotency_key)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return existing
            raise
        except Exception:
            await session.rollback()
            raise
        await session.refresh(movement)
        return movement

    async def list_movements(
        self,
        session: AsyncSession,
        product_id: int,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[InventoryMovement], int]:
        if await session.get(Product, product_id) is None:
            raise _not_found("Product not found")
        base = select(InventoryMovement).where(InventoryMovement.product_id == product_id)
        total = int((await session.scalar(select(func.count()).select_from(base.subquery()))) or 0)
        items = list(
            (
                await session.execute(
                    base.order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return items, total

    async def create_count(
        self,
        session: AsyncSession,
        *,
        reason: str,
        idempotency_key: str | None,
    ) -> InventoryCount:
        reason = reason.strip()
        if not reason:
            raise _bad_request("Reason is required")
        if idempotency_key:
            existing = (
                await session.execute(
                    select(InventoryCount).where(InventoryCount.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return await self.get_count(session, existing.id)
        count = InventoryCount(reason=reason, idempotency_key=idempotency_key)
        session.add(count)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if idempotency_key:
                existing = (
                    await session.execute(
                        select(InventoryCount).where(InventoryCount.idempotency_key == idempotency_key)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return await self.get_count(session, existing.id)
            raise
        except Exception:
            await session.rollback()
            raise
        await session.refresh(count)
        return await self.get_count(session, count.id)

    async def get_count(self, session: AsyncSession, count_id: int) -> InventoryCount:
        count = (
            await session.execute(
                select(InventoryCount)
                .options(selectinload(InventoryCount.items).selectinload(InventoryCountItem.product))
                .where(InventoryCount.id == count_id)
            )
        ).scalar_one_or_none()
        if count is None:
            raise _not_found("Inventory count not found")
        return count

    async def set_count_item(
        self,
        session: AsyncSession,
        count_id: int,
        *,
        product_id: int | None,
        barcode: str | None,
        counted_quantity: int,
    ) -> InventoryCount:
        if counted_quantity < 0:
            raise _bad_request("Counted quantity cannot be negative")
        count = (
            await session.execute(
                select(InventoryCount).where(InventoryCount.id == count_id).with_for_update()
            )
        ).scalar_one_or_none()
        if count is None:
            raise _not_found("Inventory count not found")
        if count.status != InventoryCountStatus.draft:
            raise _conflict("Posted inventory counts cannot be edited")
        product = await self._product_from_identifier(
            session, product_id=product_id, barcode=barcode, lock=True
        )
        item = (
            await session.execute(
                select(InventoryCountItem).where(
                    InventoryCountItem.inventory_count_id == count.id,
                    InventoryCountItem.product_id == product.id,
                )
            )
        ).scalar_one_or_none()
        if item is None:
            session.add(
                InventoryCountItem(
                    inventory_count_id=count.id,
                    product_id=product.id,
                    expected_quantity=product.stock_quantity,
                    counted_quantity=counted_quantity,
                )
            )
        else:
            item.counted_quantity = counted_quantity
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        return await self.get_count(session, count.id)

    async def post_count(
        self,
        session: AsyncSession,
        count_id: int,
        admin_user_id: int,
        idempotency_key: str | None,
    ) -> InventoryCount:
        count = (
            await session.execute(
                select(InventoryCount)
                .options(selectinload(InventoryCount.items).selectinload(InventoryCountItem.product))
                .where(InventoryCount.id == count_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if count is None:
            raise _not_found("Inventory count not found")
        if count.status == InventoryCountStatus.posted:
            return count
        if not count.items:
            raise _bad_request("Inventory count must contain at least one item")
        if idempotency_key:
            other = (
                await session.execute(
                    select(InventoryCount.id).where(
                        InventoryCount.posted_idempotency_key == idempotency_key,
                        InventoryCount.id != count.id,
                    )
                )
            ).scalar_one_or_none()
            if other is not None:
                raise _conflict("Idempotency key is already in use")
            count.posted_idempotency_key = idempotency_key

        product_ids = sorted(item.product_id for item in count.items)
        products = {
            product.id: product
            for product in (
                await session.execute(
                    select(Product)
                    .where(Product.id.in_(product_ids))
                    .order_by(Product.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        for count_item in count.items:
            product = products[count_item.product_id]
            if count_item.counted_quantity < product.reserved_quantity:
                raise _conflict(
                    f"Counted quantity for product {product.id} is below its reserved quantity"
                )

        for count_item in count.items:
            product = products[count_item.product_id]
            count_item.expected_quantity = product.stock_quantity
            delta = count_item.counted_quantity - product.stock_quantity
            if delta:
                product.stock_quantity = count_item.counted_quantity
                session.add(
                    self._movement(
                        product=product,
                        movement_type=InventoryMovementType.inventory_adjustment,
                        quantity=abs(delta),
                        on_hand_delta=delta,
                        reserved_delta=0,
                        inventory_count_id=count.id,
                        reason=count.reason,
                        admin_user_id=admin_user_id,
                        reference_key=f"count:{count.id}:item:{count_item.id}",
                    )
                )
        count.status = InventoryCountStatus.posted
        count.posted_at = datetime.now(timezone.utc)
        count.posted_by_user_id = admin_user_id
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _conflict("Count posting idempotency key is already in use") from exc
        except Exception:
            await session.rollback()
            raise
        return await self.get_count(session, count.id)


inventory_service = InventoryService()
