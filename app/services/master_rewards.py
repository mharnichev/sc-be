"""Manual, private bookkeeping. No provider calls, jobs or sale mutation."""
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.fiscal import ReturnRecord
from app.models.master_rewards import RewardPolicy, RewardEntry, RewardSettlement, RewardAllocation, RewardAudit, RewardOperation
from app.models.order import Order, OrderItem, OrderStatus


def fail(code, status=409):
    raise HTTPException(status, detail={'code': code})


async def authorize(session, actor, *, management=True):
    current = await session.scalar(select(AdminUser).where(AdminUser.id == actor.id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if not current or not current.is_active:
        fail('access_denied', 403)
    if management:
        if not current.is_superuser:
            fail('access_denied', 403)
        return current, None
    master = await session.scalar(select(Master).where(Master.admin_user_id == current.id))
    if not master:
        fail('access_denied', 403)
    # Booking visibility and grants for NEW sales do not gate past earnings.
    return current, master.id


def minor(value):
    number = Decimal(value)
    if not number.is_finite():
        fail('sale_not_eligible')
    return int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def reward(base_minor, rate_bps):
    return int((Decimal(base_minor) * Decimal(rate_bps) / 10000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def net_item_bases(order):
    from app.services.fiscal_returns import net_lines
    items = [dict(order_item_id=i.id, total_minor=minor(i.total_price if i.total_price is not None else i.price * i.quantity)) for i in order.items]
    total = minor(order.total_amount)
    gross = sum(i['total_minor'] for i in items)
    if (not items or any(i.quantity <= 0 for i in order.items)
            or any(i['total_minor'] < 0 for i in items) or total <= 0 or gross < total):
        fail('sale_not_eligible')
    # Existing return/fiscal allocation: integer proportions, stable remainder ties.
    return net_lines({'items': items, 'additional_discount_minor': gross-total, 'amount_minor': total})


def audit(session, actor, operation, reason, facts):
    if not reason.strip():
        fail('invalid_command', 422)
    session.add(RewardAudit(actor_id=actor.id, operation=operation, reason=reason.strip(), facts=facts, created_at=datetime.now(UTC)))


async def policy(session):
    row = await session.get(RewardPolicy, 1)
    return {'revision': row.revision, 'rate_bps': row.rate_bps, 'currency': 'UAH'}


async def lock_orders(session, order_ids):
    # Match inventory/returns: ALL items first, then ALL orders, ascending IDs.
    await session.execute(select(OrderItem.id).where(OrderItem.order_id.in_(order_ids))
        .order_by(OrderItem.id).with_for_update())
    await session.execute(select(Order.id).where(Order.id.in_(order_ids))
        .order_by(Order.id).with_for_update())


async def load_order(session, order_id, *, lock=False):
    if lock:
        await lock_orders(session, [order_id])
    stmt = select(Order).options(selectinload(Order.items)).where(Order.id == order_id).execution_options(populate_existing=True)
    order = await session.scalar(stmt)
    if not order:
        fail('not_found', 404)
    return order


async def qualify(session, order):
    from app.services import master_mono, shop_sales
    if order.simulation or order.status != OrderStatus.completed or len(order.handovers) != 1 or order.handovers[0].simulation:
        fail('sale_not_eligible')
    if len(order.payments) != 1 or order.payments[0].simulation or order.payments[0].amount_minor != minor(order.total_amount):
        fail('sale_not_eligible')
    if order.channel == 'backoffice_pos':
        binding = order.__dict__.get('master_mono_binding')
        if (not binding or binding.authority != 'provider' or binding.transport != 'https'
                or not master_mono.canonical_payment(order) or not master_mono.accepted_receipt(order)):
            fail('sale_not_eligible')
    elif order.channel == 'shop':
        completion = shop_sales.canonical_completion(order)
        if not completion or completion.authority != 'production' or shop_sales.r2_test_context(order):
            fail('sale_not_eligible')
    else:
        fail('sale_not_eligible')
    # Conservative manual v1: any return case holds the WHOLE affected order.
    if any(order.__dict__.get(name) for name in ('shop_financial_returns', 'shop_carrier_returns', 'shop_np_returns', 'shop_checkbox_returns')):
        fail('sale_not_eligible')
    if await session.scalar(select(ReturnRecord.id).where(ReturnRecord.order_id == order.id).limit(1)):
        fail('sale_not_eligible')
    net_item_bases(order)
    return order.handovers[0]


async def candidates(session, order_id):
    order = await load_order(session, order_id)
    try:
        handover = await qualify(session, order)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return {'items': [], 'currency': 'UAH'}
    bases = net_item_bases(order)
    existing = set((await session.scalars(select(RewardEntry.order_item_id).where(RewardEntry.order_id == order_id))).all())
    return {'currency': 'UAH', 'items': [dict(order_id=order.id, order_item_id=i.id, order_revision=order.revision,
        product_id=i.product_id, product_name=i.product_name or 'Товар', quantity=i.quantity,
        base_minor=bases[i.id], sold_at=handover.created_at)
        for i in order.items if i.id not in existing and bases[i.id] > 0]}


async def entry_view(session, row):
    settlement_id = await session.scalar(select(RewardAllocation.settlement_id).where(RewardAllocation.entry_id == row.id))
    result = {name: getattr(row, name) for name in ('id', 'revision', 'order_id', 'order_item_id', 'master_id', 'master_name',
        'product_id', 'product_name', 'quantity', 'base_minor', 'rate_bps', 'reward_minor', 'sold_at', 'created_at', 'needs_review')}
    result.update(currency='UAH', settlement_id=settlement_id,
        status='transferred' if settlement_id else 'needs_review' if row.needs_review else 'pending')
    return jsonable_encoder(result)


async def settlement_view(session, row):
    ids = list((await session.scalars(select(RewardAllocation.entry_id).where(RewardAllocation.settlement_id == row.id).order_by(RewardAllocation.entry_id))).all())
    return jsonable_encoder({**{name: getattr(row, name) for name in ('id', 'master_id', 'master_name', 'total_minor', 'paid_at', 'created_at')},
        'entry_ids': ids, 'currency': 'UAH', 'status': 'transferred'})


def canonical_payload(payload):
    data = payload.model_dump(mode='json')
    if 'entries' in data:
        data['entries'] = sorted(data['entries'], key=lambda i: i['id'])
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


async def command(session, actor, operation, payload, raw_key, *, entry_id=None):
    try:
        actor, _ = await authorize(session, actor)
        try:
            key = str(UUID(raw_key))
        except (ValueError, TypeError, AttributeError):
            fail('invalid_command', 422)
        hashed = canonical_payload(payload)
        scope = f'{operation}:{entry_id}' if entry_id is not None else operation
        lock = int.from_bytes(hashlib.sha256(f'master-rewards:{actor.id}:{key}'.encode()).digest()[:8], 'big', signed=True)
        await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
        previous = await session.scalar(select(RewardOperation).where(RewardOperation.actor_id == actor.id, RewardOperation.key == key))
        if previous:
            if previous.operation != scope or previous.payload_hash != hashed:
                fail('idempotency_conflict')
            return previous.response
        if operation == 'policy':
            row = await session.scalar(select(RewardPolicy).where(RewardPolicy.id == 1).with_for_update().execution_options(populate_existing=True))
            if row.revision != payload.expected_revision:
                fail('revision_conflict')
            row.revision += 1
            row.rate_bps = payload.rate_bps
            await session.flush()
            response = await policy(session)
            audit(session, actor, operation, payload.reason, response)
        elif operation == 'entry':
            row = await session.scalar(select(RewardPolicy).where(RewardPolicy.id == 1).with_for_update(read=True).execution_options(populate_existing=True))
            if row.revision != payload.expected_policy_revision:
                fail('revision_conflict')
            if row.rate_bps is None:
                fail('policy_not_configured')
            order = await load_order(session, payload.order_id, lock=True)
            if order.revision != payload.expected_order_revision:
                fail('revision_conflict')
            handover = await qualify(session, order)
            item = next((i for i in order.items if i.id == payload.order_item_id), None)
            if not item:
                fail('not_found', 404)
            master = await session.scalar(select(Master).where(Master.id == payload.master_id).with_for_update(read=True).execution_options(populate_existing=True))
            beneficiary = await session.scalar(select(AdminUser).where(AdminUser.id == master.admin_user_id).with_for_update(read=True).execution_options(populate_existing=True)) if master else None
            if not master or not master.is_active or not beneficiary or not beneficiary.is_active:
                fail('not_found', 404)
            if await session.scalar(select(RewardEntry.id).where(RewardEntry.order_item_id == item.id)):
                fail('entry_exists')
            base = net_item_bases(order)[item.id]
            if base <= 0:
                fail('sale_not_eligible')
            entry = RewardEntry(order_id=order.id, order_item_id=item.id, handover_id=handover.id,
                master_id=master.id, beneficiary_user_id=beneficiary.id, master_name=master.full_name,
                product_id=item.product_id, product_name=item.product_name or 'Товар', quantity=item.quantity,
                base_minor=base, rate_bps=row.rate_bps, reward_minor=reward(base, row.rate_bps),
                policy_revision=row.revision, sold_at=handover.created_at, created_at=datetime.now(UTC),
                created_by=actor.id, needs_review=False, revision=1)
            session.add(entry)
            await session.flush()
            response = await entry_view(session, entry)
            audit(session, actor, operation, payload.reason, response)
        elif operation == 'review':
            row = await session.scalar(select(RewardEntry).where(RewardEntry.id == entry_id).with_for_update().execution_options(populate_existing=True))
            if not row:
                fail('not_found', 404)
            if row.revision != payload.expected_revision:
                fail('revision_conflict')
            row.needs_review = payload.needs_review
            row.revision += 1
            await session.flush()
            response = await entry_view(session, row)
            audit(session, actor, operation, payload.reason, response)
        elif operation == 'settlement':
            preview, rows = await preview_settlement(session, payload, lock=True)
            if preview['total_minor'] <= 0:
                fail('total_mismatch')
            if payload.paid_at > datetime.now(UTC):
                fail('invalid_command', 422)
            first = rows[0]
            settlement = RewardSettlement(master_id=first.master_id, beneficiary_user_id=first.beneficiary_user_id,
                master_name=first.master_name, total_minor=preview['total_minor'], paid_at=payload.paid_at,
                created_at=datetime.now(UTC), created_by=actor.id)
            session.add(settlement)
            await session.flush()
            session.add_all([RewardAllocation(settlement_id=settlement.id, entry_id=i.id) for i in rows])
            await session.flush()
            response = await settlement_view(session, settlement)
            audit(session, actor, operation, payload.reason, response)
        else:
            fail('invalid_command', 422)
        response = jsonable_encoder(response)
        session.add(RewardOperation(actor_id=actor.id, key=key, operation=scope, payload_hash=hashed, response=response))
        await session.commit()
        return response
    except IntegrityError:
        await session.rollback()
        fail('entry_not_available')
    except Exception:
        await session.rollback()
        raise


async def preview_settlement(session, payload, *, lock=False):
    requested = {i.id: i.expected_revision for i in payload.entries}
    order_ids = sorted(set((await session.scalars(select(RewardEntry.order_id).where(RewardEntry.id.in_(requested)))).all()))
    # Lock the complete multi-order item set before taking any order locks.
    if lock:
        await lock_orders(session, order_ids)
    for identifier in order_ids:
        order = await load_order(session, identifier)
        await qualify(session, order)
    stmt = select(RewardEntry).where(RewardEntry.id.in_(requested)).order_by(RewardEntry.id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    rows = list((await session.scalars(stmt)).all())
    allocated = await session.scalar(select(RewardAllocation.id).where(RewardAllocation.entry_id.in_(requested)).limit(1))
    if (len(rows) != len(requested) or allocated or any(i.master_id != payload.master_id or i.needs_review for i in rows)
            or len({i.beneficiary_user_id for i in rows}) != 1):
        fail('entry_not_available')
    if any(i.revision != requested[i.id] for i in rows):
        fail('revision_conflict')
    total = sum(i.reward_minor for i in rows)
    if total != payload.expected_total_minor:
        fail('total_mismatch')
    return dict(master_id=payload.master_id, entry_ids=sorted(requested), total_minor=total, currency='UAH'), rows


def period(stmt, column, date_from, date_to):
    if date_from and date_to and date_from > date_to:
        fail('invalid_command', 422)
    if date_from:
        stmt = stmt.where(column >= datetime.combine(date_from, time.min, ZoneInfo('Europe/Kyiv')))
    if date_to:
        if date_to == date.max:
            fail('invalid_command', 422)
        stmt = stmt.where(column < datetime.combine(date_to + timedelta(days=1), time.min, ZoneInfo('Europe/Kyiv')))
    return stmt


def scope(stmt, model, actor, own_master, master_id):
    if own_master is not None:
        return stmt.where(model.master_id == own_master, model.beneficiary_user_id == actor.id)
    return stmt.where(model.master_id == master_id) if master_id is not None else stmt


async def listing(session, actor, *, settlements=False, own=False, master_id=None, date_from=None, date_to=None, page=1, page_size=30):
    actor, own_master = await authorize(session, actor, management=not own)
    model = RewardSettlement if settlements else RewardEntry
    stmt = scope(select(model), model, actor, own_master, master_id)
    stmt = period(stmt, model.paid_at if settlements else model.sold_at, date_from, date_to)
    count = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(model.id.desc()).limit(page_size).offset((page-1)*page_size))).all()
    view = settlement_view if settlements else entry_view
    return dict(items=[await view(session, row) for row in rows], total=count, page=page, page_size=page_size)


async def summary(session, actor, *, own=False, master_id=None, date_from=None, date_to=None):
    actor, own_master = await authorize(session, actor, management=not own)
    stmt = period(scope(select(RewardEntry), RewardEntry, actor, own_master, master_id), RewardEntry.sold_at, date_from, date_to)
    rows = (await session.scalars(stmt)).all()
    allocation_ids = set((await session.scalars(select(RewardAllocation.entry_id).where(RewardAllocation.entry_id.in_([i.id for i in rows])))).all()) if rows else set()
    groups = {}
    orders = {}
    for row in rows:
        item = groups.setdefault(row.master_id, dict(master_id=row.master_id, master_name=row.master_name,
            sales_count=0, units=0, sales_net_minor=0, accrued_minor=0, transferred_minor=0, pending_minor=0, held_minor=0, balance_minor=0))
        orders.setdefault(row.master_id, set()).add(row.order_id)
        item['units'] += row.quantity
        item['sales_net_minor'] += row.base_minor
        item['accrued_minor'] += row.reward_minor
        item['transferred_minor' if row.id in allocation_ids else 'held_minor' if row.needs_review else 'pending_minor'] += row.reward_minor
    for key, item in groups.items():
        item['sales_count'] = len(orders[key])
        item['balance_minor'] = item['accrued_minor'] - item['transferred_minor']
    return {'currency': 'UAH', 'items': list(groups.values())}
