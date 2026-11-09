"""POS orchestration over the existing Mono and order ledgers."""
from datetime import UTC,date,datetime,time,timedelta
from types import SimpleNamespace
from uuid import UUID,uuid4
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Text,case,exists,func,or_,select
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.booking import Master
from app.models.brand import Brand
from app.models.order import Order,OrderItem,OrderStatus
from app.models.master_mono import MasterMonoCreateRequest, MasterMonoExpiredClosure
from app.models.pos import POSCheckoutIntent
from app.models.product import Product
from app.schemas.order import POSOrderCreate,OrderHandoverCreate
from app.schemas.master_mono import MasterMonoCommand
from app.schemas.pos import CART_MAX_ITEMS, CART_MAX_QUANTITY, CartCheckout, CartPromotion, CartQuote, CartQuoteItem
from app.services.monobank_provider import MonobankError, invoice_payload, valid_invoice_text
from app.services import local_sales,master_mono,sales_access
from app.services.order_documents import operation_key,claim_intent,digest,minor
from app.services.order import snapshot_image_url
from app.services.shop_promotion import shop_promotion_service


def fail(code,status=409,**fields):raise HTTPException(status,detail={'code':code,**fields})

def identifier(value):
    try:
        result=UUID(str(value))
        if result.version!=4:raise ValueError
        return str(result)
    except (ValueError,TypeError,AttributeError):fail('invalid_identifier',422)

async def seller(session,actor):
    current=await session.scalar(select(AdminUser).where(AdminUser.id==actor.id).with_for_update(read=True).execution_options(populate_existing=True))
    if not current or not current.is_active or not (current.is_superuser or current.can_record_sales and current.can_accept_qr_payments):fail('sale_permission_denied',403)
    return current

async def manager(session,actor):
    current=await session.scalar(select(AdminUser).where(AdminUser.id==actor.id).with_for_update(read=True).execution_options(populate_existing=True))
    if not current or not current.is_active or not current.is_superuser:fail('sale_permission_denied',403)
    return current

async def capabilities(session,actor):
    actor=await seller(session,actor)
    enabled=local_sales.live_available(session)
    return dict(mode='mono_checkbox',currency='UAH',max_quantity=1,can_search=True,can_start=enabled,
        cart_limits={'max_items': CART_MAX_ITEMS, 'max_quantity': CART_MAX_QUANTITY},
        can_view_admin_sales=actor.is_superuser,automatic_reconciliation_enabled=settings.mono_checkbox_auto_enabled,
        poll_after_ms=max(10000,settings.mono_checkbox_poll_seconds*1000),blocking_reasons=[] if enabled else ['master_mono_disabled'])


def product_options():return (selectinload(Product.brand),selectinload(Product.images),selectinload(Product.image_variants))

async def offers(session,rows):
    from app.services.catalog_visibility import CatalogVisibility
    if not rows:return []
    visibility=await CatalogVisibility.load(session)
    prices=await shop_promotion_service.price_products(session,rows,category_parents=visibility.category_parents())
    result=[]
    for p in rows:
        amount=minor(prices[p.id].price)
        reasons=[]
        if not p.is_active:reasons.append('product_unavailable')
        if p.available_quantity<1:reasons.append('out_of_stock')
        if amount<=0:reasons.append('unsupported_zero_total_pos')
        result.append(dict(id=p.id,name=p.name,brand_name=p.brand.name if p.brand else None,sku=p.sku,barcode=p.barcode,
            image_url=snapshot_image_url(p),variant_label=p.package_size or (f"{p.volume_ml} мл" if p.volume_ml else None),amount_minor=amount,currency='UAH',
            available_quantity=p.available_quantity,can_sell=not reasons,blocking_reasons=reasons))
    return result

async def product_offer(session,product_id,*,lock=False):
    stmt=select(Product).where(Product.id==product_id).options(*product_options()).execution_options(populate_existing=True)
    if lock:stmt=stmt.with_for_update()
    row=await session.scalar(stmt)
    if not row or not row.is_active:fail('product_unavailable',404)
    return (await offers(session,[row]))[0]

async def cart_products(session, items, *, lock=False):
    product_ids = [item.product_id for item in items]
    stmt = (
        select(Product)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update()
    products = {product.id: product for product in (await session.scalars(stmt)).all()}
    subtotal = 0
    for item in items:
        product = products.get(item.product_id)
        if not product or not product.is_active:
            fail("product_unavailable", 404)
        if product.available_quantity < item.quantity:
            fail("out_of_stock")
        if not valid_invoice_text(product.name, 128):
            fail("master_mono_basket_invalid")
        subtotal += minor(product.effective_shop_price) * item.quantity
    # Order and line amounts are stored in NUMERIC(10,2).
    if subtotal > 9_999_999_999:
        fail("cart_amount_unsupported")
    return products


def price_cart(
    lines: list[CartQuoteItem], promo_code: str | None, promotion_id: int | None,
    *, options: list[dict] | None = None,
) -> CartQuote:
    lines = sorted(lines, key=lambda line: line.product_id)
    if promo_code and not any(line.promotion_code == promo_code for line in lines):
        fail("promo_not_applicable")
    if any(line.unit_minor <= 0 for line in lines):
        fail("unsupported_zero_total_pos", available_promotions=options or [])
    for line in lines:
        if line.promotion_id is not None:
            if line.promotion_code:
                line.discount_source = "promo_code"
            elif line.promotion_id == promotion_id:
                line.discount_source = "selected_promotion"
            else:
                line.discount_source = "automatic"
    subtotal = sum(line.subtotal_minor for line in lines)
    amount = sum(line.amount_minor for line in lines)
    return CartQuote(
        items=lines, subtotal_minor=subtotal, discount_minor=subtotal - amount,
        amount_minor=amount, applied_promo_code=promo_code, promotion_id=promotion_id,
        available_promotions=[CartPromotion.model_validate(option) for option in options or []],
        quote_revision=digest([promotion_id, [line.model_dump(mode="json") for line in lines]]),
    )


async def quote_cart(session, actor, payload):
    await seller(session, actor)
    products = await cart_products(session, payload.items)
    try:
        prices = await shop_promotion_service.price_products(
            session, list(products.values()), promo_code=payload.promo_code,
            validate_code_usage=bool(payload.promo_code), pos_promotion_id=payload.promotion_id,
        )
    except HTTPException as error:
        if payload.promo_code and error.status_code == 400:
            fail("promo_not_applicable")
        raise
    lines = [
        CartQuoteItem(
            product_id=item.product_id, quantity=item.quantity,
            base_unit_minor=minor(prices[item.product_id].base_price),
            unit_minor=minor(prices[item.product_id].price),
            promotion_id=prices[item.product_id].promotion_id,
            promotion_name=prices[item.product_id].promotion_name,
            promotion_code=prices[item.product_id].promotion_code,
        )
        for item in payload.items
    ]
    options = await shop_promotion_service.pos_promotion_options(session, list(products.values()))
    return price_cart(lines, payload.promo_code, payload.promotion_id, options=options)


async def prepare_cart(session, actor, payload, prepare_key):
    products = await cart_products(session, payload.items, lock=True)
    try:
        prepared = await local_sales.prepare(
            session, actor,
            POSOrderCreate(items=[item.model_dump() for item in payload.items], promo_code=payload.promo_code),
            prepare_key, commit=False, intent_scope="pos-quick-prepare", pos_promotion_id=payload.promotion_id,
        )
    except HTTPException as error:
        if payload.promo_code and error.status_code == 400:
            fail("promo_not_applicable")
        raise
    order = await local_sales.load(session, prepared.id)
    actual = price_cart([
        CartQuoteItem(
            product_id=item.product_id, quantity=item.quantity,
            base_unit_minor=minor(item.base_price), unit_minor=minor(item.price),
            promotion_id=item.shop_promotion_id, promotion_name=item.promotion_name,
            promotion_code=item.promotion_code,
        )
        for item in order.items
    ], payload.promo_code, payload.promotion_id)
    if actual.quote_revision != payload.quote_revision:
        options = await shop_promotion_service.pos_promotion_options(session, list(products.values()))
        actual.available_promotions = [CartPromotion.model_validate(option) for option in options]
        fail("price_changed", current_quote=actual.model_dump(mode="json"))
    # Reject unsupported fiscal lines before persisting an order and reservation.
    basket, _policy = master_mono.basket_for(order)
    try:
        invoice_payload(str(payload.intent_id), minor(order.total_amount), basket)
    except MonobankError:
        fail("master_mono_basket_invalid")
    return order


async def products(session,actor,*,q='',page=1,page_size=20):
    await seller(session,actor)
    stmt=select(Product).outerjoin(Brand).where(Product.is_active.is_(True))
    term=q.strip()
    if term:
        stmt=stmt.where(or_(*[field.icontains(term,autoescape=True) for field in (Product.name,Product.name_en,Product.sku,Product.barcode,Brand.name)]))
    total=await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows=list((await session.scalars(stmt.options(*product_options()).order_by(case((Product.barcode==term,0),else_=1),Product.name,Product.id).limit(page_size).offset((page-1)*page_size))).all())
    return dict(items=await offers(session,rows),total=total,page=page,page_size=page_size)

async def employee(session,user_id):
    actor=await session.get(AdminUser,user_id)
    linked=await session.scalar(select(Master).where(Master.admin_user_id==user_id)) if actor and not actor.is_superuser else None
    return dict(user_id=user_id,master_id=linked.id if linked else None,
        display_name=linked.full_name if linked else f'Адміністратор #{user_id}' if actor and actor.is_superuser else f'Працівник #{user_id}')

async def capture_items(session,order):
    rows=(await session.scalars(select(Product).where(Product.id.in_([i.product_id for i in order.items])).options(*product_options()))).all()
    mapping={p.id:p for p in rows}
    return [dict(order_item_id=i.id,product_id=i.product_id,name=i.product_name or mapping[i.product_id].name,
        sku=i.product_sku,barcode=mapping[i.product_id].barcode,image_url=i.product_image_url or snapshot_image_url(mapping[i.product_id]),variant_label=i.product_variant or mapping[i.product_id].package_size or (f"{mapping[i.product_id].volume_ml} мл" if mapping[i.product_id].volume_ml else None)) for i in order.items]

async def root_for(session,actor_id,value,*,intent=False):
    val=identifier(value)
    row=await session.scalar(select(POSCheckoutIntent).where(POSCheckoutIntent.actor_id==actor_id,
        POSCheckoutIntent.intent_id==val if intent else POSCheckoutIntent.id==val))
    if not row:fail('checkout_not_found',404)
    return row

async def project(session,order,root=None,*,history=False,actor=None,display=None):
    mono=master_mono.view(session,order)
    binding=order.__dict__.get('master_mono_binding');request=order.__dict__.get('master_mono_create_request')
    observations=order.__dict__.get('master_mono_observations',[])
    paid=master_mono.canonical_payment(order)
    receipt=master_mono.accepted_receipt(order)
    proof=observations[-1].proof.get('payment') if observations else None
    bank_status=None
    if binding and proof and master_mono.matching_identity(proof,binding) and proof.get('invoice_id')==binding.invoice_id:
        bank_status=proof.get('status')
    expired_closed = False
    quarantined = False
    if order.status == OrderStatus.cancelled and request and not binding:
        expired_closed = await session.get(MasterMonoExpiredClosure, order.id) is not None
        quarantined = expired_closed and await master_mono.has_closed_conflict(session, order.id)
    if quarantined:
        payment_status = 'unknown'
        mono.blocking_reasons.append('master_mono_closed_conflict')
    elif expired_closed:payment_status='expired'
    elif paid:payment_status='reversed' if bank_status=='reversed' else 'paid'
    elif bank_status in {'created','processing','hold','failure','expired','reversed'}:
        payment_status={'created':'pending','hold':'processing','failure':'failed'}.get(bank_status,bank_status)
    else:payment_status='pending' if binding and not observations and mono.payment_url else 'unknown' if request else 'not_started'
    receipt_status='requires_review' if quarantined else 'accepted' if receipt else 'failed' if 'receipt_failed' in mono.status else 'requires_review' if 'conflict' in mono.status or 'proof_invalid' in mono.status else 'pending' if paid else 'not_requested'
    done=bool(order.status==OrderStatus.completed and len(order.handovers)==1 and not order.handovers[0].simulation and paid)
    uncertain=payment_status in {'unknown','failed','expired','reversed'} or receipt_status in {'failed','requires_review'}
    if uncertain:workflow='requires_review'
    elif done:workflow='completed'
    elif mono.can_handover:workflow='ready_to_handover'
    elif paid:workflow='paid_receipt_pending'
    elif payment_status=='processing':workflow='payment_processing'
    elif binding:workflow='awaiting_payment'
    else:workflow='prepared'
    metadata=root.product_snapshot if root else await capture_items(session,order)
    snapshots={i['order_item_id']:i for i in metadata}
    items=[]
    for item in order.items:
        snap=snapshots[item.id]
        items.append({**{k:v for k,v in snap.items() if k!='order_item_id'},'quantity':item.quantity,
            'unit_minor':minor(item.price),'total_minor':minor(item.total_price if item.total_price is not None else item.price*item.quantity)})
    recovery = None
    if request and not binding and not order.simulation and actor and order.status in {OrderStatus.pending, OrderStatus.confirmed}:
        may_recover = actor.is_superuser or (
            actor.can_manage_fiscal_settings and not await sales_access.restricted_master(session, actor)
        )
        if may_recover:
            recovery = dict(reference=request.reference, requested_at=request.created_at, can_attach=mono.enabled)
    actions=dict(can_resume=bool(not history and mono.enabled and not request and not binding and workflow=='prepared'),
        can_refresh=bool(not history and mono.enabled and binding and order.status not in {OrderStatus.completed,OrderStatus.cancelled}),
        can_handover=bool(not history and mono.can_handover),
        can_recover_invoice=bool(not history and recovery and recovery['can_attach']),
        can_start_new=bool(not history and not quarantined and (workflow=='completed' or expired_closed)))
    if history:actions={key:False for key in actions}
    qr=mono.payment_url if not paid and payment_status=='pending' and mono.enabled else None
    hand=order.handovers[0] if order.handovers else None
    return jsonable_encoder(dict(checkout_id=root.id if root else None,intent_id=root.intent_id if root else None,
        order_id=order.id,order_status=order.status.value,revision=order.revision,created_at=order.created_at,
        employee=root.employee_snapshot if root else display or await employee(session,order.created_by_user_id),items=items,
        amount_minor=minor(order.total_amount),subtotal_minor=minor(order.subtotal_amount),
        discount_minor=minor(order.discount_amount),currency='UAH',status=workflow,
        payment=dict(provider='monobank',status=payment_status,invoice_id=binding.invoice_id if binding else None,
            confirmed_at=order.master_mono_payment.created_at if paid else None,qr_payload=None if history else qr),
        receipt=dict(issuer='checkbox',status=receipt_status,recorded_at=receipt.created_at if receipt else None,
            pdf_url=mono.receipt_pdf_path if not history else None),
        handover=dict(status='handed_over' if hand else 'ready' if actions['can_handover'] else 'not_ready',
            actor_user_id=hand.operator_id if hand else None,handed_over_at=hand.created_at if hand else None),
        actions=actions,invoice_recovery=recovery,poll_after_ms=max(10000,settings.mono_checkbox_poll_seconds*1000),blocking_reasons=mono.blocking_reasons))

async def state(session,actor,checkout_id):
    actor=await seller(session,actor);root=await root_for(session,actor.id,checkout_id)
    _,order=await master_mono.actor_order(session,actor,root.order_id)
    return await project(session,order,root,actor=actor)

async def by_intent(session,actor,intent_id):
    actor=await seller(session,actor);root=await root_for(session,actor.id,intent_id,intent=True)
    return await state(session,actor,root.id)

async def active(session,actor,*,page=1,page_size=20):
    actor=await seller(session,actor)
    stmt=select(POSCheckoutIntent).join(Order,Order.id==POSCheckoutIntent.order_id).where(POSCheckoutIntent.actor_id==actor.id,Order.status.notin_([OrderStatus.completed,OrderStatus.cancelled]))
    total=await session.scalar(select(func.count()).select_from(stmt.subquery()))
    roots=(await session.scalars(stmt.order_by(POSCheckoutIntent.created_at.desc(),POSCheckoutIntent.id).limit(page_size).offset((page-1)*page_size))).all()
    return dict(items=[await state(session,actor,r.id) for r in roots],total=total,page=page,page_size=page_size)

async def ensure_started(session,actor_id,checkout_id,key):
    root=await root_for(session,actor_id,checkout_id)
    order=await local_sales.load(session,root.order_id)
    if not order.__dict__.get('master_mono_create_request'):
        await master_mono.start(session,SimpleNamespace(id=actor_id),order.id,MasterMonoCommand(expected_revision=order.revision),key)
    return await state(session,SimpleNamespace(id=actor_id),checkout_id)

async def create(session,actor,payload,key):
    actor_id=actor.id
    created=False
    try:
        actor=await seller(session,actor)
        scope=operation_key(key,f'pos-quick-create:{actor_id}')
        body_hash=digest(payload.model_dump(mode='json'))
        alias=await claim_intent(session,scope,body_hash)
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f'pos:{actor_id}:{payload.intent_id}',0))))
        root=await session.scalar(select(POSCheckoutIntent).where(POSCheckoutIntent.actor_id==actor_id,POSCheckoutIntent.intent_id==str(payload.intent_id)))
        if root:
            if root.payload_hash!=body_hash:fail('idempotency_conflict')
        else:
            if not local_sales.live_available(session):fail('master_mono_disabled')
            prepare_key = digest(['quick-pos-order', str(payload.intent_id)])
            if isinstance(payload, CartCheckout):
                order = await prepare_cart(session, actor, payload, prepare_key)
            else:
                offer=await product_offer(session,payload.product_id,lock=True)
                if not offer['can_sell']:fail(offer['blocking_reasons'][0])
                prepared=await local_sales.prepare(session,actor,POSOrderCreate(items=[{'product_id':payload.product_id,'quantity':1}],simulation=False),
                    prepare_key,commit=False,expected_amount_minor=payload.expected_amount_minor,
                    intent_scope="pos-quick-prepare")
                order=await local_sales.load(session,prepared.id)
            created=True
            root=POSCheckoutIntent(id=str(uuid4()),actor_id=actor_id,intent_id=str(payload.intent_id),order_id=order.id,
                payload_hash=body_hash,kind='quick',employee_snapshot=await employee(session,actor_id),
                product_snapshot=await capture_items(session,order),created_at=datetime.now(UTC))
            session.add(root);await session.flush()
        if alias.order_id is not None and alias.order_id!=root.order_id:fail('idempotency_conflict')
        alias.order_id=root.order_id;alias.response_json={'pos_checkout_id':root.id}
        checkout_id=root.id
        await session.commit()
    except HTTPException as error:
        await session.rollback()
        if not isinstance(payload, CartCheckout) and isinstance(error.detail,dict) and error.detail.get('code')=='price_changed':
            await seller(session,SimpleNamespace(id=actor_id))
            fail('price_changed',current_offer=await product_offer(session,payload.product_id))
        if isinstance(error.detail,str) and error.detail.startswith('Insufficient stock'):fail('out_of_stock')
        raise
    except IntegrityError:
        await session.rollback();fail('idempotency_conflict')
    except Exception:
        await session.rollback();raise
    result=await ensure_started(session,actor_id,checkout_id,key)
    result["_created"]=created
    return result

async def resume(session,actor,checkout_id,payload,key):
    actor=await seller(session,actor);actor_id=actor.id;root=await root_for(session,actor_id,checkout_id)
    order_id=root.order_id
    await master_mono.start(session,SimpleNamespace(id=actor_id),order_id,MasterMonoCommand(expected_revision=payload.expected_revision),key)
    return await state(session,SimpleNamespace(id=actor_id),checkout_id)

async def refresh(session,actor,checkout_id,payload,key):
    actor=await seller(session,actor);actor_id=actor.id;root=await root_for(session,actor_id,checkout_id)
    await master_mono.refresh(session,SimpleNamespace(id=actor_id),root.order_id,MasterMonoCommand(expected_revision=payload.expected_revision),key)
    return await state(session,SimpleNamespace(id=actor_id),checkout_id)

async def handover(session,actor,checkout_id,payload,key):
    actor=await seller(session,actor);actor_id=actor.id;root=await root_for(session,actor_id,checkout_id)
    await local_sales.command(session,SimpleNamespace(id=actor_id),root.order_id,
        OrderHandoverCreate(expected_revision=payload.expected_revision,evidence='pos_customer_handover'),key,handover=True)
    return await state(session,SimpleNamespace(id=actor_id),checkout_id)

async def adopt(session,actor,payload,key):
    try:
        actor=await seller(session,actor);actor_id=actor.id
        alias=await claim_intent(session,operation_key(key,f'pos-quick-adopt:{actor_id}'),digest(payload.model_dump(mode='json')))
        _,order=await master_mono.actor_order(session,actor,payload.order_id,lock=True)
        request=order.__dict__.get('master_mono_create_request')
        if order.simulation or not request or request.authority!='provider' or request.transport!='https':fail('master_mono_request_required')
        existing=await session.scalar(select(POSCheckoutIntent).where(POSCheckoutIntent.order_id==order.id))
        if not existing:
            if await session.scalar(select(POSCheckoutIntent.id).where(POSCheckoutIntent.actor_id==actor_id,POSCheckoutIntent.intent_id==str(payload.intent_id))):fail('idempotency_conflict')
            existing=POSCheckoutIntent(id=str(uuid4()),actor_id=actor_id,intent_id=str(payload.intent_id),order_id=order.id,
                payload_hash=digest(payload.model_dump(mode='json')),kind='adopt',employee_snapshot=await employee(session,actor_id),
                product_snapshot=await capture_items(session,order),created_at=datetime.now(UTC))
            session.add(existing);await session.flush()
        if alias.order_id is not None and alias.order_id!=order.id:fail('idempotency_conflict')
        alias.order_id=order.id;alias.response_json={'pos_checkout_id':existing.id};checkout_id=existing.id
        await session.commit()
        return await state(session,SimpleNamespace(id=actor_id),checkout_id)
    except IntegrityError:
        await session.rollback();fail('idempotency_conflict')
    except Exception:
        await session.rollback();raise


def history_query():
    real=exists(select(MasterMonoCreateRequest.id).where(MasterMonoCreateRequest.order_id==Order.id,
        MasterMonoCreateRequest.authority=='provider',MasterMonoCreateRequest.transport=='https'))
    return select(Order,POSCheckoutIntent).outerjoin(POSCheckoutIntent,POSCheckoutIntent.order_id==Order.id).where(
        Order.channel=='backoffice_pos',Order.simulation.is_(False),or_(POSCheckoutIntent.id.is_not(None),real))

def dates(stmt,date_from,date_to):
    if date_from and date_to and date_from>date_to:fail('invalid_period',422)
    if date_from:stmt=stmt.where(Order.created_at>=datetime.combine(date_from,time.min,ZoneInfo('Europe/Kyiv')))
    if date_to:
        if date_to==date.max:fail('invalid_period',422)
        stmt=stmt.where(Order.created_at<datetime.combine(date_to+timedelta(days=1),time.min,ZoneInfo('Europe/Kyiv')))
    return stmt

async def sales(session,actor,*,q='',employee_user_id=None,order_status=None,date_from=None,date_to=None,page=1,page_size=20):
    await manager(session,actor)
    stmt=dates(history_query(),date_from,date_to)
    if employee_user_id is not None:stmt=stmt.where(Order.created_by_user_id==employee_user_id)
    if order_status is not None:stmt=stmt.where(Order.status==OrderStatus(order_status))
    term=q.strip()
    if term:
        matches=[POSCheckoutIntent.employee_snapshot.cast(Text).icontains(term,autoescape=True),POSCheckoutIntent.product_snapshot.cast(Text).icontains(term,autoescape=True),
            exists(select(OrderItem.id).where(OrderItem.order_id==Order.id,or_(OrderItem.product_name.icontains(term,autoescape=True),OrderItem.product_sku.icontains(term,autoescape=True))))]
        if term.isdigit() and len(term)<=9:matches.append(Order.id==int(term))
        matches.append(exists(select(Master.id).where(Master.admin_user_id==Order.created_by_user_id,Master.full_name.icontains(term,autoescape=True))))
        stmt=stmt.where(or_(*matches))
    total=await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows=(await session.execute(stmt.options(selectinload(Order.items)).order_by(Order.created_at.desc(),Order.id.desc()).limit(page_size).offset((page-1)*page_size))).all()
    result=[]
    for order,root in rows:
        view=await project(session,order,root,history=True)
        result.append({**{key:view[key] for key in ('order_id','checkout_id','intent_id','created_at','employee','items','amount_minor','currency','order_status','status')},
            'payment_status':view['payment']['status'],'receipt_status':view['receipt']['status'],'handover_status':view['handover']['status'],
            'source':'quick_pos' if root and root.kind=='quick' else 'existing_mono_pos'})
    return dict(items=result,total=total,page=page,page_size=page_size)

async def sale_detail(session,actor,order_id):
    actor=await manager(session,actor)
    pair=(await session.execute(history_query().where(Order.id==order_id).options(selectinload(Order.items)))) .one_or_none()
    if not pair:fail('sale_not_found',404)
    order,root=pair
    view=await project(session,order,root,history=True,actor=actor)
    view['source']='quick_pos' if root and root.kind=='quick' else 'existing_mono_pos'
    view.update(payment_status=view['payment']['status'],receipt_status=view['receipt']['status'],handover_status=view['handover']['status'])
    return view
