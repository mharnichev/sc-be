"""Receipt commands: originals stay immutable; quantity/cost deltas are explicit."""
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import selectinload

from app.models.inventory import (InventoryMovement, InventoryMovementType, InventoryReceipt,
    InventoryReceiptAllocation, InventoryReceiptChange, InventoryReceiptItem, InventoryReceiptStatus)
from app.models.order import Order, OrderItem, OrderStatus, ProcurementStatus
from app.models.product import Product
from app.schemas.inventory import ReceiptResponse


def fail(code, message):
    raise HTTPException(code, detail=message)


def money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def snapshot(item):
    return dict(product_id=item.product_id,quantity=item.quantity,
                purchase_unit_cost=format(money(item.purchase_unit_cost),'f'),
                batch_number=item.batch_number,
                expiration_date=item.expiration_date.isoformat() if item.expiration_date else None,
                allocations=sorted([dict(order_item_id=a.order_item_id,quantity=a.quantity)
                                    for a in item.allocations],key=lambda a:a['order_item_id']))


def weighted_receipt_cost(items, product_id, overrides):
    rows=[overrides.get(item.id) or dict(quantity=item.quantity,purchase_unit_cost=item.purchase_unit_cost)
          for item in items if item.product_id==product_id]
    quantity=sum(row['quantity'] for row in rows)
    return money(sum(Decimal(row['purchase_unit_cost'])*row['quantity'] for row in rows)/quantity) if quantity else None


async def latest_corrections(session, receipt_ids):
    if not receipt_ids:
        return {}
    latest=(select(func.max(InventoryReceiptChange.id)).where(
        InventoryReceiptChange.receipt_id.in_(receipt_ids),
        InventoryReceiptChange.operation=='posted_correction',
    ).group_by(InventoryReceiptChange.receipt_id,InventoryReceiptChange.target_item_id))
    changes=(await session.execute(select(InventoryReceiptChange).where(InventoryReceiptChange.id.in_(latest)))).scalars().all()
    return {(c.receipt_id,c.target_item_id):c for c in changes}


async def project_receipts(session, receipts):
    if not receipts:
        return []
    ids=[r.id for r in receipts]
    # Refresh revision, lines and correction projection under one parent read
    # lock. Every writer takes that parent FOR UPDATE before any line/audit write.
    # Product reads stay unlocked, avoiding a reversed product/receipt lock order.
    locked=(await session.execute(select(InventoryReceipt).where(InventoryReceipt.id.in_(ids))
        .options(selectinload(InventoryReceipt.items).selectinload(InventoryReceiptItem.allocations),
                 selectinload(InventoryReceipt.items).selectinload(InventoryReceiptItem.product))
        .order_by(InventoryReceipt.id).with_for_update(read=True)
        .execution_options(populate_existing=True))).scalars().all()
    by_id={r.id:r for r in locked}
    if len(by_id)!=len(set(ids)):
        fail(404,'Receipt disappeared during projection')
    receipts=[by_id[identifier] for identifier in ids]
    corrections=await latest_corrections(session,[r.id for r in receipts if r.status==InventoryReceiptStatus.posted])
    result=[]
    for receipt in receipts:
        response=ReceiptResponse.model_validate(receipt)
        original=Decimal('0.00');effective=Decimal('0.00')
        for line in response.items:
            change=corrections.get((receipt.id,line.id))
            current=change.after if change else dict(quantity=line.quantity,purchase_unit_cost=line.purchase_unit_cost)
            line.effective_quantity=current['quantity']
            line.effective_purchase_unit_cost=Decimal(current['purchase_unit_cost'])
            line.effective_purchase_value=money(line.effective_quantity*line.effective_purchase_unit_cost)
            line.last_correction_id=change.id if change else None
            original+=line.quantity*line.purchase_unit_cost
            effective+=line.effective_purchase_value
        response.original_purchase_value=money(original)
        response.effective_purchase_value=money(effective)
        result.append(response)
    return result


async def mutate_receipt(session, receipt_id, item_id, *, operation, payload, actor_id, idempotency_key):
    from app.services.inventory import inventory_service
    if not idempotency_key.strip() or len(idempotency_key)>128:
        fail(422,'Idempotency-Key must contain 1..128 nonblank characters')
    canonical=payload.model_dump(mode='json')
    if 'purchase_unit_cost' in canonical:
        canonical['purchase_unit_cost']=format(money(payload.purchase_unit_cost),'f')
    if 'allocations' in canonical:
        canonical['allocations'].sort(key=lambda a:a['order_item_id'])
    digest=sha256(json.dumps(dict(operation=operation,item_id=item_id,payload=canonical),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    try:
        receipt=(await session.execute(select(InventoryReceipt).where(InventoryReceipt.id==receipt_id)
            .options(selectinload(InventoryReceipt.items).selectinload(InventoryReceiptItem.allocations))
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if receipt is None:fail(404,'Receipt not found')
        prior=(await session.execute(select(InventoryReceiptChange).where(
            InventoryReceiptChange.receipt_id==receipt_id,InventoryReceiptChange.idempotency_key==idempotency_key))).scalar_one_or_none()
        if prior is not None:
            if prior.payload_hash!=digest:fail(409,'receipt_idempotency_conflict')
            await session.commit()
            return await inventory_service.get_receipt(session,receipt_id)
        if receipt.revision!=payload.expected_revision:fail(409,'receipt_revision_conflict')
        expected_status=InventoryReceiptStatus.posted if operation=='posted_correction' else InventoryReceiptStatus.draft
        if receipt.status!=expected_status:fail(409,'Use corrections for posted receipts; direct edits require a draft')
        item=next((line for line in receipt.items if line.id==item_id),None)
        if item is None:fail(404,'Receipt item not found in this receipt')
        before=snapshot(item)
        movement_id=None;stock_delta=0
        if operation=='posted_correction':
            latest=await latest_corrections(session,[receipt_id])
            previous=latest.get((receipt_id,item_id))
            if previous is not None:
                before=dict(previous.after)
            after=dict(before,quantity=payload.quantity,purchase_unit_cost=format(money(payload.purchase_unit_cost),'f'))
            if after['quantity']==before['quantity'] and after['purchase_unit_cost']==before['purchase_unit_cost']:
                fail(409,'receipt_no_change')
            allocated=sum(a.quantity for a in item.allocations)
            if after['quantity']<allocated:fail(409,'receipt_allocated_quantity_protected')
            product=await inventory_service._locked_product(session,item.product_id)
            stock_delta=after['quantity']-before['quantity']
            if stock_delta<0:
                if product.stock_quantity-product.reserved_quantity < -stock_delta:
                    fail(409,'receipt_insufficient_free_stock')
                original=(await session.execute(select(InventoryMovement.id).where(
                    InventoryMovement.reference_key==f'receipt:{receipt_id}:item:{item_id}'))).scalar_one_or_none()
                if original is None:fail(409,'receipt_original_movement_missing')
                unsafe=(await session.execute(select(InventoryMovement.id).where(
                    InventoryMovement.product_id==product.id,InventoryMovement.id>original,
                    or_(InventoryMovement.on_hand_delta<0,InventoryMovement.reserved_delta>0),
                    ~(func.coalesce(InventoryMovement.receipt_id==receipt_id,False)&func.coalesce(
                        InventoryMovement.reference_key.like('receipt-correction:%')|
                        InventoryMovement.reference_key.like(f'receipt:{receipt_id}:allocation:%'),False)),
                ).limit(1))).scalar_one_or_none()
                if unsafe is not None:fail(409,'receipt_downstream_stock_activity')
            if not 0<=product.stock_quantity+stock_delta<=2147483647:
                fail(409,'receipt_stock_quantity_out_of_range')
            before['product_purchase_unit_cost']=format(product.purchase_unit_cost,'f') if product.purchase_unit_cost is not None else None
            before['product_cost_source_receipt_id']=product.purchase_cost_receipt_id
            # Pre-feature products have no source marker. Establish one only
            # on an explicit correction of the demonstrably latest posted receipt;
            # never infer cost from sale prices or overwrite an unowned manual cost.
            if product.purchase_cost_receipt_id is None and product.purchase_unit_cost is None:
                newest=await session.scalar(select(InventoryReceipt.id)
                    .join(InventoryReceiptItem,InventoryReceiptItem.receipt_id==InventoryReceipt.id)
                    .where(InventoryReceipt.status==InventoryReceiptStatus.posted,
                           InventoryReceiptItem.product_id==product.id)
                    .order_by(InventoryReceipt.posted_at.desc().nullslast(),InventoryReceipt.id.desc()).limit(1))
                if newest==receipt_id:
                    product.purchase_cost_receipt_id=receipt_id
            if product.purchase_cost_receipt_id==receipt_id:
                overrides={line.id:latest[(receipt_id,line.id)].after for line in receipt.items if (receipt_id,line.id) in latest}
                overrides[item.id]=after
                product.purchase_unit_cost=weighted_receipt_cost(receipt.items,product.id,overrides)
            after['product_purchase_unit_cost']=format(product.purchase_unit_cost,'f') if product.purchase_unit_cost is not None else None
            after['product_cost_source_receipt_id']=product.purchase_cost_receipt_id
            if stock_delta:
                product.stock_quantity+=stock_delta
                movement=inventory_service._movement(product=product,movement_type=InventoryMovementType.inventory_adjustment,
                    quantity=abs(stock_delta),on_hand_delta=stock_delta,reserved_delta=0,receipt_id=receipt_id,
                    reason=payload.reason,admin_user_id=actor_id,
                    reference_key=f'receipt-correction:{receipt_id}:{sha256(idempotency_key.encode()).hexdigest()}')
                session.add(movement);await session.flush();movement_id=movement.id
        else:
            new_allocations=canonical.get('allocations',[])
            affected_ids=sorted({a.order_item_id for a in item.allocations}|{a['order_item_id'] for a in new_allocations})
            locked=list((await session.execute(select(OrderItem).where(OrderItem.id.in_(affected_ids))
                .order_by(OrderItem.id).with_for_update().execution_options(populate_existing=True))).scalars().all()) if affected_ids else []
            order_items={row.id:row for row in locked}
            orders={row.id:row for row in (await session.execute(select(Order).where(Order.id.in_(sorted({r.order_id for r in locked})))
                .order_by(Order.id).with_for_update().execution_options(populate_existing=True))).scalars().all()} if locked else {}
            await inventory_service._locked_product(session,item.product_id)
            if operation=='draft_delete':
                after={}
                await session.delete(item)
            else:
                for allocation in new_allocations:
                    requirement=order_items.get(allocation['order_item_id'])
                    if requirement is None:fail(404,'Allocation order item not found')
                    if requirement.product_id!=item.product_id:fail(422,'Allocation product mismatch')
                    if orders[requirement.order_id].status in (OrderStatus.cancelled,OrderStatus.completed):fail(409,'Allocation order is terminal')
                    if requirement.procurement_status!=ProcurementStatus.ordered:fail(409,'Allocation requires ordered procurement')
                    others=(await session.scalar(select(func.coalesce(func.sum(InventoryReceiptAllocation.quantity),0))
                        .join(InventoryReceiptItem,InventoryReceiptItem.id==InventoryReceiptAllocation.receipt_item_id)
                        .where(InventoryReceiptItem.receipt_id==receipt_id,InventoryReceiptItem.id!=item_id,
                               InventoryReceiptAllocation.order_item_id==requirement.id))) or 0
                    if others+allocation['quantity']>requirement.quantity_to_order-requirement.quantity_received_for_order:
                        fail(409,'Allocation exceeds outstanding order requirement')
                for other in receipt.items:
                    if other.id!=item.id and (other.product_id,other.batch_number,other.expiration_date,other.purchase_unit_cost)==(
                        item.product_id,item.batch_number,item.expiration_date,payload.purchase_unit_cost):
                        fail(409,'Edit would duplicate another receipt lot line')
                after=dict(before,quantity=payload.quantity,purchase_unit_cost=format(money(payload.purchase_unit_cost),'f'),allocations=new_allocations)
                if before==after:fail(409,'receipt_no_change')
                item.quantity=payload.quantity;item.purchase_unit_cost=payload.purchase_unit_cost
                # Flush removal before reinsert to preserve the per-line unique constraint.
                for allocation in list(item.allocations):await session.delete(allocation)
                await session.flush()
                for allocation in new_allocations:
                    session.add(InventoryReceiptAllocation(receipt_item_id=item.id,**allocation))
        receipt.revision+=1
        before_value=Decimal(before['purchase_unit_cost'])*before['quantity']
        after_value=Decimal(after.get('purchase_unit_cost','0'))*after.get('quantity',0)
        session.add(InventoryReceiptChange(receipt_id=receipt_id,target_item_id=item_id,actor_id=actor_id,
            operation=operation,idempotency_key=idempotency_key,payload_hash=digest,reason=payload.reason,
            applied_revision=receipt.revision,before=before,after=after,on_hand_delta=stock_delta,
            purchase_value_delta=money(after_value-before_value),movement_id=movement_id))
        await session.commit()
    except Exception:
        await session.rollback();raise
    return await inventory_service.get_receipt(session,receipt_id)
