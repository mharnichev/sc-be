"""R4: immutable reconciliation, explicit refunds, and once-only reserve release.

Provider status observations never create money. A foreign candidate correction
changes the effective lookup identity only; R2 still verifies acceptance separately.
"""
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, DBAPIError
from app.core.config import settings
from app.models.order import OrderIntent,OrderStatus,ProcurementStatus
from app.models.product import Product
from app.models.inventory import InventoryMovement,InventoryMovementType
from app.models.shop_payments import ShopPaymentIntent as PaymentIntent,ShopPaymentEvidence
from app.models.shop_sales import ShopOperationAudit
from app.models.shop_checkbox import ShopCheckboxBinding as Binding,ShopCheckboxReturn as FinancialReturn,ShopCheckboxRefundFact as Refund,ShopCheckboxDocument as Document
from app.models.shop_payment_resolutions import (ShopPaymentResolutionCase as Case,ShopPaymentResolutionObservation as Observation,
 ShopPaymentInvoiceCorrection as Correction,ShopPaymentBankClosure as Closure,ShopPaymentResolutionFinalization as Finalization,ShopPaymentFinancialException as ExceptionFact)
from app.schemas import shop_payment_resolutions as dto
from app.services import shop_sales as sales,shop_payments as payments,shop_checkbox as cb
from app.services.order_documents import digest,operation_key,claim_intent,minor
fail=sales.fail
canonical=payments.canonical


def cases(order):return order.__dict__.get('shop_payment_resolutions',[])
def test_context(order):
    return any(c.test_context or c.closure and c.closure.policy.get('environment')=='test' for c in cases(order))
def active(order):
    return any(c.exceptions or (not c.finalization and (c.kind=='paid_cancellation' or not sales.canonical_payment(order))) for c in cases(order))
def closed(order):return any(c.finalization for c in cases(order))
def payment_blocked(order):return any(c.kind=='paid_cancellation' or c.finalization or c.exceptions for c in cases(order))
def uuid_reference(value):
    try:
        compact=re.sub(r'^urn:uuid:','',value,flags=re.I).translate(str.maketrans('','','{}-'))
        return str(UUID(compact)) if re.fullmatch(r'[0-9a-fA-F]{32}',compact) else value
    except (ValueError,TypeError,AttributeError):return value

def manual_policy():
    p=settings.shop_payment_resolution_manual_policy
    if (not isinstance(p,dict) or set(p)!={'id','approved','environment','issuer','account','document_types'} or p.get('approved') is not True
        or p.get('environment') not in {'test','production'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',str(p.get('id','')))
        or any(not isinstance(p.get(k),str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}',p[k]) for k in ('issuer','account'))
        or not isinstance(p.get('document_types'),list) or not p['document_types']
        or any(not isinstance(t,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',t) for t in p['document_types'])):return None
    return p

async def actor_for(session,actor,operation='read'):
    actor=await sales.current_actor(session,actor,'read',SimpleNamespace(fulfillment_method=None))
    if operation=='close_unpaid' and not (actor.is_superuser or actor.can_manage_fiscal_settings):
        raise HTTPException(403,'Bank closure requires sales and fiscal settings permissions')
    return actor

async def provider_guard(session,provider):
    if not provider.available:fail("monobank_unavailable","Pinned bank inspection is unavailable",503)
    if provider.test_only:await sales.require_test_database(session,"resolution_test_unsafe")


async def guard(session,case=None):
    if not settings.shop_order_operations_enabled or not settings.shop_payment_resolutions_enabled:
        fail('shop_payment_resolutions_disabled','Payment resolutions are closed',503)
    if case and (case.test_context or case.closure and case.closure.policy.get('environment')=='test'):
        await sales.require_test_database(session,'resolution_test_unsafe')

def assertion(payload,profile):
    if not profile or payload.expected_profile_id!=profile['id'] or payload.expected_profile_hash!=digest(profile):
        fail('resolution_profile_conflict','Restore the pinned profile and reload the resolution')

def no_handover(order):
    if (order.handovers or order.shop_fulfillment_fact or order.shop_completion
        or any(s.handed_over_at or s.tracking_status not in {None,'unknown','created','not_found','preparing','cancelled','deleted'}
            or any(e.normalized_status not in {'unknown','created','not_found','preparing','cancelled','deleted'} for e in s.tracking_events)
            or any(e.normalized_status not in {'unknown','created','not_found','preparing','cancelled','deleted'} for e in s.events) for s in order.shipments)):
        fail('handover_or_carrier_progress','Existing handover or carrier progress requires the physical return workflow')

def eligible(order):
    if (not sales.eligible_order(order) or order.status in {OrderStatus.completed,OrderStatus.cancelled}
        or order.shop_carrier_returns or order.shop_np_returns or order.qr_payment_attempts
        or order.shop_fiscal_documents or order.shop_financial_returns or order.shop_checkbox_returns):
        fail('resolution_not_eligible','Use an eligible exclusive Shop order without another return history')
    no_handover(order)

def known_ids(case,binding):
    latest={o.invoice_id:o for o in case.observations}
    ours={o.invoice_id for o in case.observations if o.proof and uuid_reference(o.proof['reference'])==binding.reference}
    foreign={o.invoice_id for o in latest.values() if o.proof and uuid_reference(o.proof['reference'])!=binding.reference and o.invoice_id not in ours}
    foreign.update({case.correction.original_invoice_id} if case.correction else set())
    # All positively observed same-reference candidates remain documentary obligations.
    result={binding.invoice_id} if binding.invoice_id and binding.invoice_id not in foreign else set()
    for o in case.observations:
        if o.proof and uuid_reference(o.proof['reference'])==binding.reference:result.add(o.invoice_id)
    if case.correction:result.add(case.correction.replacement_invoice_id)
    return sorted(result)

def contradictory(case,binding):
    for o in case.observations:
        p=o.proof
        if p and uuid_reference(p['reference'])==binding.reference and (p['status']=='success' or p['has_cancellations']
            or p['final_amount_minor'] not in {None,0} or p['amount_minor']!=binding.amount_minor or p['currency']!='UAH'):
            return True
    return bool(case.exceptions)

async def effective_invoice(session,binding):
    correction=await session.scalar(select(Correction).where(Correction.intent_id==binding.id))
    return (correction.replacement_invoice_id if correction else binding.invoice_id),correction

async def financial_review_guard(session,order_id):
    if await session.scalar(select(ExceptionFact.id).join(Case, Case.id==ExceptionFact.case_id).where(Case.order_id==order_id)):
        fail("resolution_financial_review","Known bank money requires separate financial review")


async def cancellation_valid(session,item):
    if not item.cancellation_case_id:return False
    c=await session.get(Case,item.cancellation_case_id)
    return bool(c and c.kind=='paid_cancellation' and c.order_id==item.order_id and c.register_id==item.register_id
        and c.snapshot_hash==digest(c.snapshot) and not item.carrier_receipt_id)

async def paid_obligations(session,case):
    item=await session.scalar(select(FinancialReturn).where(FinancialReturn.cancellation_case_id==case.id))
    if not item:return None,None,None
    refund=await session.scalar(select(Refund).where(Refund.return_id==item.id))
    doc=await session.scalar(select(Document).where(Document.return_id==item.id,Document.kind=='return').order_by(Document.id.desc()))
    if doc:
        op=await cb.load_operation(session,doc.operation_id)
        if not cb.truth_valid(op):doc=None
    return item,refund,doc

async def case_view(session,actor,order,case):
    item,refund,return_doc=await paid_obligations(session,case)
    sale=await cb.sale_for(session,order.id)
    sale_issued=False
    if sale:sale_issued=cb.truth_valid(await cb.load_operation(session,sale.operation_id))
    accepted=sales.canonical_payment(order)
    if case.exceptions:status='financial_exception'
    elif case.finalization:status='closed_paid' if case.kind=='paid_cancellation' else 'closed_unpaid'
    elif case.kind=='paid_cancellation':
        status='awaiting_sale' if not sale_issued else 'awaiting_claim' if not item else 'awaiting_refund' if not refund else 'awaiting_return_receipt' if not return_doc else 'ready_to_finalize'
    else:status='payment_accepted' if accepted else 'corrected' if case.correction else 'review_required' if case.observations else 'inspection_required'
    profile=(await cb.register(session,register_id=case.register_id)).profile if case.kind=='paid_cancellation' else payments.gateway().profile
    current=profile==case.profile and digest(profile)==case.profile_hash if profile else False
    safe=not (case.test_context or case.closure and case.closure.policy.get('environment')=='test') or await sales.test_database_is_safe(session)
    enabled=settings.shop_payment_resolutions_enabled and settings.shop_order_operations_enabled and current and safe
    fresh=enabled and not case.exceptions and not case.finalization
    def fact(value):
        return {'id':value.id,**value.snapshot,'operator_id':value.operator_id,'created_at':value.created_at.isoformat(),'proof_hash':value.snapshot_hash} if value else None
    return dto.CaseView(id=case.id,kind=case.kind,order_id=order.id,revision=case.revision,order_revision=order.revision,
        profile_id=case.profile_id,profile_hash=case.profile_hash,protocol=case.protocol,
        test_context=bool(case.test_context or case.closure and case.closure.policy.get('environment')=='test'),status=status,reason=case.reason,snapshot=case.snapshot,
        known_invoice_ids=known_ids(case,order.shop_payment_intent) if case.kind=='invoice_review' else [],
        observations=[dict(id=o.id,invoice_id=o.invoice_id,error_code=o.error_code,proof=o.proof,proof_hash=o.proof_hash,operator_id=o.operator_id,created_at=o.created_at.isoformat()) for o in case.observations],
        correction=fact(case.correction),closure=fact(case.closure),finalization=fact(case.finalization),
        exceptions=[dict(id=e.id,observation_id=e.observation_id,reason=e.reason,amount_minor=e.amount_minor,currency=e.currency,operator_id=e.operator_id,created_at=e.created_at.isoformat()) for e in case.exceptions],
        financial_return_id=item.id if item else None,sale_document_id=sale.id if sale else None,
        manual_policy=manual_policy(),manual_policy_hash=digest(manual_policy()) if manual_policy() else None,
        actions={'inspect':bool(enabled and case.kind=='invoice_review'),
            'correct_invoice':bool(fresh and case.kind=='invoice_review' and not case.correction and not accepted),
            'financial_claim':bool(fresh and status=='awaiting_claim'),
            'close_unpaid':bool(fresh and case.kind=='invoice_review' and not accepted and manual_policy() and (actor.is_superuser or actor.can_manage_fiscal_settings)),
            'finalize_paid':bool(fresh and status=='ready_to_finalize'),
            **{'replay_'+k:bool(k!='close_unpaid' or actor.is_superuser or actor.can_manage_fiscal_settings) for k in ('open','inspect','correct_invoice','financial_claim','close_unpaid','finalize_paid')}},
        blocking_reasons=([] if enabled else ['resolution_profile_conflict' if not current else 'resolution_test_unsafe' if not safe else 'shop_payment_resolutions_disabled'])+(['resolution_financial_review'] if case.exceptions else []))

async def read(session,actor,resource,identifier):
    actor=await actor_for(session,actor)
    case=await session.get(Case,identifier,populate_existing=True) if resource=='case' else None
    if resource=='case' and not case:raise HTTPException(404,'Payment resolution not found')
    order=await sales.load_order(session,case.order_id if case else identifier)
    if case:return await case_view(session,actor,order,next(c for c in cases(order) if c.id==case.id))
    bound=await session.get(Binding,order.id);row=await cb.register(session,register_id=bound.register_id) if bound else None
    binding=order.shop_payment_intent
    reasons=[]
    try:eligible(order)
    except HTTPException as e:reasons.append(e.detail['code'])
    if active(order):reasons.append('resolution_active')
    enabled=settings.shop_payment_resolutions_enabled and settings.shop_order_operations_enabled
    if not enabled:reasons.append('shop_payment_resolutions_disabled')
    payment=sales.canonical_payment(order)
    return dto.OrderView(order_id=order.id,revision=order.revision,enabled=enabled,
        paid_profile_id=row.id if row else None,paid_profile_hash=row.profile_hash if row else None,
        invoice_profile_id=binding.profile_id if binding else None,invoice_profile_hash=binding.profile_hash if binding else None,
        cases=[await case_view(session,actor,order,c) for c in cases(order)],
        actions={'open_paid_cancellation':bool(not reasons and payment and row and not any(c.kind=='paid_cancellation' for c in cases(order))),
            'open_invoice_review':bool(not reasons and binding and binding.mode=='prepayment' and binding.invoice_id and not payment and not any(c.kind=='invoice_review' for c in cases(order))),
            'replay_open_paid_cancellation':True,'replay_open_invoice_review':True},blocking_reasons=reasons)

async def unresolved(session,register_id):
    result=[]
    for c in (await session.scalars(select(Case).where(Case.register_id==register_id))).all():
        order=await sales.load_order(session,c.order_id)
        if c.exceptions or not c.finalization and (c.kind=='paid_cancellation' or not sales.canonical_payment(order)):result.append(c.id)
    return result

async def release(session,order,case,actor):
    from app.services.order import OrderService
    await OrderService.validate_owned_reservations(session,order,order.items)
    products={p.id:p for p in (await session.scalars(select(Product).where(Product.id.in_(sorted({i.product_id for i in order.items}))).order_by(Product.id).with_for_update())).all()}
    plan=[]
    for i in order.items:
        qty=i.quantity_from_stock+i.quantity_received_for_order
        if qty:
            if products[i.product_id].reserved_quantity<qty:fail('resolution_obligations_pending','Owned reserves require reconciliation')
            products[i.product_id].reserved_quantity-=qty
            key=f'shop-resolution:{case.id}:item:{i.id}:reserve-release'
            session.add(InventoryMovement(product_id=i.product_id,movement_type=InventoryMovementType.reservation_release,quantity=qty,on_hand_delta=0,reserved_delta=-qty,
                order_id=order.id,order_item_id=i.id,performed_by_user_id=actor.id,reference_key=key,reason='Verified pre-handover resolution'))
            plan.append(dict(order_item_id=i.id,product_id=i.product_id,quantity=qty,reference_key=key))
        i.procurement_status=ProcurementStatus.not_required
    return plan

async def command(session,actor,identifier,payload,raw_key,operation):
    opening=operation in {'paid_cancellation','invoice_review'}
    actor_id=actor.id
    key=operation_key(raw_key,f'shop-resolution:{actor_id}:{operation}:{identifier}')
    request_hash=digest(payload.model_dump(mode='json'))
    try:
        actor=await actor_for(session,actor,operation)
        prior=await session.get(OrderIntent,key)
        if prior:
            if prior.payload_hash!=request_hash:fail('idempotency_conflict','Key already used with another body')
            if prior.response_json:
                if prior.response_json['test_context']:await sales.require_test_database(session,'resolution_test_unsafe')
                return dto.CaseView.model_validate(prior.response_json)
        case=None if opening else await session.get(Case,identifier,populate_existing=True)
        if not opening and not case:raise HTTPException(404,'Payment resolution not found')
        await guard(session,case)
        order_id=identifier if opening else case.order_id
        order=await sales.load_order(session,order_id)
        inspection=None;error=None
        if operation=='inspect':
            if case.kind!='invoice_review':fail('resolution_kind_mismatch','Only invoice review permits bank inspection')
            p=payments.gateway();await provider_guard(session,p);payments.pinned(order.shop_payment_intent,p);assertion(payload,p.profile)
            if (case.test_context or case.closure and case.closure.policy.get('environment')=='test') and not p.test_only:fail('resolution_profile_conflict','Test context cannot use production bank transport')
            await session.rollback()
            from app.services.monobank_provider import MonobankError
            try:inspection=await p.inspect_invoice(payload.invoice_id)
            except MonobankError as e:error=e.code
            actor=await actor_for(session,SimpleNamespace(id=actor_id),operation)
        order=await sales.load_order(session,order_id,lock=True)
        if not opening:case=await session.scalar(select(Case).where(Case.id==identifier).with_for_update().execution_options(populate_existing=True))
        await guard(session,case)
        command_intent=await claim_intent(session,key,request_hash)
        if command_intent.response_json:return dto.CaseView.model_validate(command_intent.response_json)
        if (order.revision if opening else case.revision)!=payload.expected_revision:fail('revision_conflict','Reload current resolution')
        binding=order.shop_payment_intent
        bound=await session.get(Binding,order.id)
        row=await cb.register(session,register_id=bound.register_id,lock=True) if bound else None
        if opening:
            eligible(order)
            if active(order) or any(c.kind==operation for c in cases(order)):fail('resolution_exists','An exclusive resolution already exists')
            payment=sales.canonical_payment(order)
            if operation=='paid_cancellation':
                if not payment or not row:fail('resolution_not_eligible','Canonical full payment and original R1 binding required')
                await cb.guard(session,cb.gateway(row.profile));await cb.binding(session,order,row)
                profile=row.profile;context=row.authority=='test' or sales.r2_test_context(order)
            else:
                if not binding or binding.mode!='prepayment' or not binding.invoice_id or order.payments:fail('resolution_not_eligible','An unpaid pinned invoice candidate is required')
                p=payments.gateway();await provider_guard(session,p);payments.pinned(binding,p)
                profile=binding.profile;context=binding.authority=='test' or profile.get('environment')=='test'
            assertion(payload,profile)
            if row:
                pending=await cb.pending(session,row)
                pending_doc=await session.scalar(select(Document).where(Document.operation_id==pending.id)) if pending else None
                if pending and not (operation=='paid_cancellation' and pending.kind=='sale' and pending.order_id==order.id
                    and pending_doc and pending_doc.kind=='sale' and pending_doc.payment_confirmation_id==payment.id):
                    fail('checkbox_register_busy','Resolve the existing register operation first')
            snap={'order_id':order.id,'amount_minor':minor(order.total_amount),'currency':'UAH','goods_in_custody':True,
                'reserved_lines':[{'order_item_id':i.id,'product_id':i.product_id,'quantity':i.quantity_from_stock+i.quantity_received_for_order} for i in sorted(order.items,key=lambda i:i.id)],
                'intent_id':binding.id if binding else None,'reference':binding.reference if binding else None,
                'original_invoice_id':binding.invoice_id if binding else None,'known_invoice_ids':[binding.invoice_id] if binding and binding.invoice_id else [],
                'payment_confirmation_id':payment.id if payment else None,'test_context':context}
            case=Case(order_id=order.id,kind=operation,intent_id=binding.id if binding else None,payment_confirmation_id=payment.id if payment else None,
                register_id=row.id if row else None,profile_id=profile['id'],profile=profile,profile_hash=digest(profile),protocol='full_sale_then_return' if operation=='paid_cancellation' else 'inspect_correct_or_bank_closure',
                test_context=context,revision=1,snapshot=snap,snapshot_hash=digest(snap),reason=payload.reason,operator_id=actor.id,created_at=datetime.now(UTC))
            session.add(case);order.revision+=1
            await session.flush()
        else:
            profile=row.profile if case.kind=='paid_cancellation' and row else payments.gateway().profile
            assertion(payload,profile)
            if profile!=case.profile:fail('resolution_profile_conflict','Restore immutable original profile')
            if case.kind=='invoice_review':
                payments.pinned(binding,payments.gateway())
            elif row:
                await cb.guard(session,cb.gateway(row.profile));await cb.binding(session,order,row)
            if case.exceptions and operation!='inspect':fail('resolution_financial_review','A financial exception requires separate review')
            if case.finalization and operation!='inspect':fail('resolution_obligations_pending','This resolution is already finalized')
            if operation!='inspect':no_handover(order)
            if operation=='inspect':
                await provider_guard(session,payments.gateway())
                if inspection:
                    proof=inspection
                    if (proof.get('verified') is not True or proof.get('invoice_id')!=payload.invoice_id or proof.get('profile_id')!=binding.profile_id
                        or proof.get('merchant_id')!=binding.profile['merchant_id'] or proof.get('authority')!=binding.authority or proof.get('transport')!=binding.transport
                        or proof.get('proof_hash')!=digest({k:v for k,v in proof.items() if k!='proof_hash'})):
                        fail('monobank_proof_invalid','Inspection lacks exact pinned authenticated provenance')
                else:proof=None
                o=Observation(case_id=case.id,invoice_id=payload.invoice_id,error_code=error,proof=proof,proof_canonical=canonical(proof),proof_hash=digest(proof),operator_id=actor.id,created_at=datetime.now(UTC))
                session.add(o);await session.flush()
                accepted=order.shop_payment_evidence
                recognized=bool(accepted and accepted.proof.get('invoice_id')==payload.invoice_id)
                if proof and uuid_reference(proof['reference'])==binding.reference and (proof['has_cancellations'] or proof['amount_minor']!=binding.amount_minor or proof['currency']!='UAH'
                    or proof['status']=='reversed' or case.finalization and proof['status']=='hold'
                    or proof['status']=='success' and (case.finalization and not recognized or accepted and not recognized or proof['final_amount_minor']!=binding.amount_minor)):
                    session.add(ExceptionFact(case_id=case.id,observation_id=o.id,reason='hold_after_closure' if case.finalization and proof['status']=='hold' else 'surplus_payment' if accepted and not recognized and proof['status']=='success' else 'late_success' if case.finalization and proof['status']=='success' else 'contradictory_bank_money',
                        amount_minor=proof['final_amount_minor'] or proof['amount_minor'],currency=proof['currency'],operator_id=actor.id,created_at=datetime.now(UTC)))
            elif operation=='correct_invoice':
                if case.kind!='invoice_review' or order.payments:fail('resolution_correction_invalid','Only an unpaid invoice review can correct a candidate')
                if case.correction:fail('resolution_correction_exists','Only one append-only correction is supported')
                old=next((o for o in case.observations if o.id==payload.foreign_observation_id),None)
                new=next((o for o in case.observations if o.id==payload.replacement_observation_id),None)
                if (not old or not new or not old.proof or not new.proof or old.invoice_id!=binding.invoice_id or old.invoice_id==new.invoice_id
                    or any(o.invoice_id==old.invoice_id and o.proof and uuid_reference(o.proof['reference'])==binding.reference for o in case.observations)
                    or any(o.invoice_id==old.invoice_id and o.id>old.id or o.invoice_id==new.invoice_id and o.id>new.id for o in case.observations)
                    or uuid_reference(old.proof['reference'])==binding.reference or new.proof['reference']!=binding.reference
                    or new.proof['currency']!='UAH' or new.proof['amount_minor']!=binding.amount_minor or new.proof['has_cancellations']
                    or datetime.fromisoformat(new.proof['created_at'])<sales.aware(binding.created_at)-timedelta(seconds=30)
                    or any(datetime.fromisoformat(o.proof['observed_at'])<datetime.now(UTC)-timedelta(minutes=10) for o in (old,new))
                    or contradictory(case,binding) and any(o.invoice_id!=new.invoice_id and o.proof and uuid_reference(o.proof['reference'])==binding.reference for o in case.observations)):
                    fail('resolution_correction_invalid','Fresh positive foreign-reference proof and exact replacement proof are required')
                snap=dict(intent_id=binding.id,original_invoice_id=old.invoice_id,replacement_invoice_id=new.invoice_id,foreign_observation_id=old.id,replacement_observation_id=new.id,reference=binding.reference,profile_hash=case.profile_hash)
                session.add(Correction(case_id=case.id,intent_id=binding.id,profile_id=binding.profile_id,original_invoice_id=old.invoice_id,replacement_invoice_id=new.invoice_id,
                    foreign_observation_id=old.id,replacement_observation_id=new.id,snapshot=snap,snapshot_hash=digest(snap),operator_id=actor.id,created_at=datetime.now(UTC)))
            elif operation=='financial_claim':
                if case.kind!='paid_cancellation' or not row:fail('resolution_kind_mismatch','Only paid full cancellation creates a financial claim')
                await cb.free_register(session,row);cb.open_register(row)
                if await session.scalar(select(FinancialReturn.id).where(FinancialReturn.order_id==order.id)):fail('resolution_exists','A financial claim already exists')
                sale=await cb.sale_for(session,order.id);op=await cb.issued(session,sale)
                if op.snapshot['amount_minor']!=case.snapshot['amount_minor']:fail('resolution_not_eligible','Exact original full sale required')
                item=FinancialReturn(order_id=order.id,sale_document_id=sale.id,register_id=row.id,shift_id=row.shift_id,revision=1,amount_minor=op.snapshot['amount_minor'],
                    lines=op.snapshot['items'],reason=payload.reason,operator_id=actor.id,created_at=datetime.now(UTC),cancellation_case_id=case.id)
                session.add(item)
            elif operation in {'close_unpaid','finalize_paid'}:
                if operation=='close_unpaid':
                    if case.kind!='invoice_review' or order.payments or order.shop_checkbox_documents:fail('resolution_closure_invalid','Unpaid closure cannot override financial facts')
                    policy=manual_policy()
                    if not policy:fail('resolution_closure_policy_unavailable','Approved bank closure documentary policy required')
                    if payload.expected_manual_policy_id!=policy['id'] or payload.expected_manual_policy_hash!=digest(policy):fail('resolution_profile_conflict','Reload the manual documentary policy')
                    if policy['environment']=='test':await sales.require_test_database(session,'resolution_test_unsafe')
                    e=payload.evidence
                    if (e.issuer!=policy['issuer'] or e.account!=policy['account'] or e.type not in policy['document_types'] or e.order_id!=order.id or e.intent_id!=binding.id
                        or e.reference!=binding.reference or sorted(e.known_invoice_ids)!=known_ids(case,binding)
                        or e.verified_at>datetime.now(UTC)+timedelta(seconds=30) or e.verified_at<sales.aware(binding.created_at)
                        or any(e.verified_at<o.created_at for o in case.observations) or contradictory(case,binding)):
                        fail('resolution_closure_invalid','Document must cover every known matching invoice without contradictory money history')
                    snap=e.model_dump(mode='json')
                    identity=digest([e.issuer.lower(),e.account.lower(),e.document_id.lower(),e.line_id.lower()])
                    session.add(Closure(case_id=case.id,policy=policy,policy_hash=digest(policy),identity=identity,snapshot=snap,snapshot_hash=digest(snap),operator_id=actor.id,created_at=datetime.now(UTC)))
                else:
                    if case.kind!='paid_cancellation':fail('resolution_kind_mismatch','Paid cancellation required')
                    item,refund,doc=await paid_obligations(session,case)
                    if not item or not refund or not doc:fail('resolution_obligations_pending','Full staff refund and accepted original-linked return cheque required')
                    if row:await cb.free_register(session,row)
                plan=await release(session,order,case,actor)
                snap=dict(case_id=case.id,order_id=order.id,kind=case.kind,case_hash=case.snapshot_hash,reserve_release=plan,test_context=case.test_context or operation=='close_unpaid' and policy['environment']=='test',
                    financial_return_id=item.id if operation=='finalize_paid' else None,return_document_id=doc.id if operation=='finalize_paid' else None)
                session.add(Finalization(case_id=case.id,order_id=order.id,snapshot=snap,snapshot_hash=digest(snap),operator_id=actor.id,created_at=datetime.now(UTC)))
                await session.flush()
                order.status=OrderStatus.cancelled
            else:raise ValueError('Unknown resolution operation')
            case.revision+=1;order.revision+=1
        intent=command_intent
        await session.flush()
        order=await sales.load_order(session,order.id)
        case=next(c for c in cases(order) if c.id==case.id)
        result=await case_view(session,actor,order,case)
        intent.order_id=order.id;intent.response_json=result.model_dump(mode='json')
        session.add(ShopOperationAudit(order_id=order.id,operator_id=actor.id,operation='r4_'+operation,request_key=key,facts={'case_id':case.id,'case_revision':case.revision},created_at=datetime.now(UTC)))
        await guard(session,case)
        current_profile=(await cb.register(session,register_id=case.register_id)).profile if case.kind=="paid_cancellation" else payments.gateway().profile
        assertion(payload,current_profile)
        if current_profile!=case.profile:fail("resolution_profile_conflict","Pinned profile changed before commit")
        if operation=="close_unpaid" and (manual_policy()!=policy):fail("resolution_profile_conflict","Bank documentary policy changed before commit")
        await session.commit()
        return result
    except IntegrityError:
        await session.rollback();fail('resolution_identity_conflict','Resolution, invoice, or document identity already belongs to another fact')
    except DBAPIError as error:
        await session.rollback()
        if "Inspection must preserve" in str(error.orig):fail("monobank_proof_invalid","Fresh nonconflicting inspection provenance required")
        if "Invoice identity belongs" in str(error.orig):fail("resolution_identity_conflict","Invoice already belongs to another immutable intent")
        raise
    except Exception:
        await session.rollback();raise


def payment_projection(order):
    existing=cases(order)
    if any(c.exceptions for c in existing):return ('simulated_' if test_context(order) else '')+'financial_exception'
    if any(c.finalization and c.kind=='paid_cancellation' for c in existing):return ('simulated_' if test_context(order) else '')+'refunded'
    if any(c.finalization and c.kind=='invoice_review' for c in existing):return 'unpaid'
    return None


def fulfillment_projection(order):
    existing=cases(order)
    prefix='simulated_' if test_context(order) else ''
    if any(c.finalization for c in existing):return prefix+'cancelled_before_handover'
    if any(c.kind=='paid_cancellation' for c in existing):return prefix+'cancellation_pending'
    return None
