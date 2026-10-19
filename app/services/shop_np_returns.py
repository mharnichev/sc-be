"""Attach existing reverse parcels; carrier delivery and content receipt are distinct."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.order import OrderIntent, OrderStatus
from app.models.product import Product
from app.models.inventory import InventoryMovement, InventoryMovementType
from app.models.shipment import Shipment, ShipmentItem
from app.models.shop_np_returns import (ShopNPReturnCase as Case, ShopNPReturnObservation as Observation,
    ShopNPReturnReceipt as Receipt, ShopNPUnpaidClosure as Closure, ShopNPReturnAudit as Audit)
from app.models.shop_checkbox import ShopCheckboxReturn as FinancialReturn
from app.schemas import shop_np_returns as dto
from app.services import shop_sales as sales, shop_fiscal, sales_access
from app.services.order_documents import claim_intent, operation_key, digest

fail = sales.fail


def gateway():
    from app.services.np_return_provider import NPReturnGateway
    return NPReturnGateway()


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)


def cases(order):
    return order.__dict__.get("shop_np_returns",[])


def test_context(order):
    return any(c.test_context or c.authority=="test" or c.profile.get("environment")=="test" for c in cases(order))


def valid_case(case,order=None):
    return bool(case and case.snapshot_hash==digest(case.snapshot) and case.profile_hash==digest(case.profile)
        and case.snapshot.get("order_id")==case.order_id and case.snapshot.get("original_shipment_id")==case.original_shipment_id
        and case.snapshot.get("return_ttn")==case.return_ttn and case.snapshot.get("original_ttn")==case.original_ttn
        and case.snapshot.get("lines")==case.lines and case.snapshot.get("authority")==case.authority
        and case.snapshot.get("test_context") is case.test_context and (not order or case.order_id==order.id))


def valid_receipt(receipt,case):
    return bool(valid_case(case) and receipt and receipt.case_id==case.id and receipt.snapshot_hash==digest(receipt.snapshot)
        and receipt.snapshot==dict(case_id=case.id,case_hash=case.snapshot_hash,observation_id=receipt.observation_id,
            lines=receipt.lines,contents_verified=True,test_context=case.test_context))


def financial_for(order,case):
    receipt=case.__dict__.get("receipt")
    return next((r for r in order.__dict__.get("shop_checkbox_returns",[]) if receipt and r.carrier_receipt_id==receipt.id),None)


def paid_resolution(order,case):
    from app.services.shop_checkbox import truth_valid
    item=financial_for(order,case)
    if not item or not valid_receipt(case.__dict__.get("receipt"),case) or not item.__dict__.get("refund_fact"):
        return None
    return next((d for d in item.__dict__.get("documents",[]) if d.kind=="return" and truth_valid(d.operation)),None)


def active_case(order):
    return any(not c.__dict__.get("closure") and not paid_resolution(order,c) for c in cases(order))


def projection(order):
    if not cases(order):
        return None
    c=cases(order)[-1]
    prefix="simulated_" if c.test_context else ""
    return prefix+("np_refusal_closed" if c.__dict__.get("closure") else "np_return_fiscalized" if paid_resolution(order,c)
        else "np_return_received" if c.__dict__.get("receipt") else "np_return_pending")


async def actor_for(session,actor,operation="read"):
    current=await session.scalar(select(AdminUser).where(AdminUser.id==actor.id).with_for_update(read=True).execution_options(populate_existing=True))
    if not current or not current.is_active:
        raise HTTPException(403,"Active Shop operator required")
    await sales_access.deny_master(session,current)
    fields={"attach":["can_manage_shipments"],"refresh":["can_manage_shipments"],
        "receive":["can_manage_shipments","can_post_inventory"],"close_unpaid":["can_manage_shipments","can_record_sales"],
        "financial_claim":["can_record_sales"]}.get(operation)
    allowed=current.is_superuser or (all(getattr(current,p,False) for p in fields) if fields else any(getattr(current,p,False)
        for p in ("can_manage_shipments","can_record_sales","can_post_inventory","can_fiscalize_sales","can_fiscalize_returns","can_manage_fiscal_settings")))
    if not allowed:
        raise HTTPException(403,"Current Nova Poshta return permissions required")
    return current


async def guard(session,provider,case=None):
    if not settings.shop_np_returns_enabled or not settings.shop_order_operations_enabled:
        fail("shop_np_returns_disabled","Nova Poshta return operations are closed",503)
    if not provider.available:
        fail("np_return_unavailable","An approved read-only carrier profile is required",503)
    if provider.test_only or case and case.test_context:
        await sales.require_test_database(session,"np_return_test_unsafe")
    if case and (case.profile_hash!=digest(provider.profile) or case.profile!=provider.profile
            or case.authority!=("test" if provider.test_only else "provider")
            or case.transport!=("mock_http" if provider.test_only else "https")):
        fail("np_return_profile_conflict","Restore the exact original return profile and transport")


def assertion(payload,provider,case=None):
    profile=case.profile if case else provider.profile
    if not profile or payload.expected_profile_id!=profile["id"] or payload.expected_profile_hash!=digest(profile):
        fail("np_return_profile_conflict","Reload the current return profile")


async def readiness(session,actor):
    await actor_for(session,actor)
    p=gateway();reasons=[]
    enabled=settings.shop_np_returns_enabled and settings.shop_order_operations_enabled
    if not enabled:reasons.append("shop_np_returns_disabled")
    if not p.available:reasons.append("np_return_unavailable")
    safe=not p.test_only or await sales.test_database_is_safe(session)
    if not safe:reasons.append("np_return_test_unsafe")
    return dto.ReadinessView(enabled=enabled,available=bool(p.available and safe),profile_id=p.profile["id"] if p.profile else None,
        profile_hash=digest(p.profile) if p.profile else None,authority=("test" if p.test_only else "provider") if p.available else "none",
        transport=("mock_http" if p.test_only else "https") if p.available else "unavailable",
        manual_link_allowed=bool(p.profile and p.profile["manual_link_allowed"]),blocking_reasons=reasons)


async def eligible(session,order):
    from app.services.shop_payment_resolutions import financial_review_guard
    await financial_review_guard(session,order.id)
    from app.services import shop_checkbox as cb
    if not sales.eligible_order(order) or order.fulfillment_method!="nova_poshta" or order.status==OrderStatus.cancelled:
        fail("np_return_shipment_required","A dispatched non-simulated Nova Poshta Shop order is required")
    if order.shop_carrier_returns or order.shop_fiscal_documents or order.shop_financial_returns or order.qr_payment_attempts:
        fail("np_return_history_conflict","Legacy, B3, QR and local fiscal histories cannot be adopted")
    if not cases(order) and await session.scalar(select(InventoryMovement.id).where(InventoryMovement.movement_type==InventoryMovementType.customer_return,
            (InventoryMovement.order_id==order.id)|(InventoryMovement.order_item_id.in_([i.id for i in order.items]))).limit(1)):
        fail("np_return_history_conflict","Existing returned-stock history requires separate reconciliation")
    fulfillment=sales.canonical_fulfillment(order)
    if not fulfillment or fulfillment.kind!="nova_poshta":
        fail("np_return_shipment_required","Canonical full Nova Poshta dispatch is required")
    shipment=next((s for s in order.shipments if s.id==fulfillment.shipment_id),None)
    if len(order.shipments)!=1:
        fail("np_return_history_conflict","Only the sole original outbound parcel can enter this return workflow")
    if (not shipment or shipment.simulation or not shipment.active or shipment.source!="attach" or shipment.carrier!="nova_poshta"
            or shipment.return_of_shipment_id or shipment.tracking_number!=shipment.provider_reference):
        fail("np_return_shipment_required","The exact original attached carrier parcel is required")
    allocations=list((await session.scalars(select(ShipmentItem).where(ShipmentItem.shipment_id==shipment.id))).all())
    if {a.order_item_id:a.quantity for a in allocations}!={i.id:i.quantity for i in order.items}:
        fail("np_return_shipment_mismatch","Original parcel must cover the exact dispatched order")
    bound=order.shop_checkbox_binding
    if not bound:
        fail("np_return_history_conflict","The original dispatch requires its original Checkbox binding")
    row=await cb.register(session,register_id=bound.register_id)
    await cb.binding(session,order,row)
    payment=sales.canonical_payment(order)
    if payment:
        sale=await cb.sale_for(session,order.id)
        if not sale:
            fail("np_return_sale_required","An accepted original Checkbox sale is required")
        operation=await cb.issued(session,sale)
        if sale.payment_confirmation_id!=payment.id or operation.register_id!=row.id:
            fail("np_return_sale_required","The sale must identify this exact order, payment and register")
        return shipment,fulfillment,row,"paid",sale
    if order.payments or order.shop_payment_intent or not sales.is_cod_order(order) or order.shop_completion or order.shop_checkbox_documents or order.shop_checkbox_returns:
        fail("np_return_unpaid_facts_conflict","Unpaid refusal requires COD without money, intents or fiscal documents")
    return shipment,fulfillment,row,"unpaid_cod",None


def date(value):
    try:
        parsed=datetime.fromisoformat(value)
        if parsed.tzinfo is None:raise ValueError
        return parsed
    except (ValueError,TypeError):
        fail("np_return_proof_invalid","Carrier proof requires a dated UTC observation")


def validate_proof(proof,provider,original_ttn,return_ttn,kind,mode,*,previous=None,dispatched_at=None):
    if (not isinstance(proof,dict) or proof.get("verified") is not True or proof.get("profile_id")!=provider.profile["id"]
            or proof.get("authority")!=("test" if provider.test_only else "provider")
            or proof.get("transport")!=("mock_http" if provider.test_only else "https")
            or proof.get("original_ttn")!=original_ttn or proof.get("return_ttn")!=return_ttn or proof.get("linkage_kind")!=kind
            or proof.get("linkage_verified") is not (kind=="easy_return")
            or proof.get("linkage_authority")!=("manual" if kind=="manual_document" else proof["authority"])
            or proof.get("proof_hash")!=digest({k:v for k,v in proof.items() if k!="proof_hash"})):
        fail("np_return_proof_invalid","Carrier proof does not bind the exact parcel pair and profile")
    for key,number in (("original",original_ttn),("reverse",return_ttn)):
        value=proof.get(key)
        if not isinstance(value,dict) or value.get("number")!=number or value.get("status") in {None,"unknown","requires_review"}:
            fail("np_return_proof_invalid","Unknown carrier statuses require reconciliation")
    observed=date(proof.get("observed_at"))
    if not datetime.now(UTC)-timedelta(seconds=min(settings.np_tracking_stale_seconds,7200))<=observed<=datetime.now(UTC)+timedelta(seconds=30):
        fail("np_return_tracking_stale","A fresh carrier verification is required")
    for name in ("original","reverse"):
        current=proof[name]
        if current.get("event_at") and date(current["event_at"])>observed+timedelta(seconds=30):
            fail("np_return_proof_invalid","Carrier event cannot follow the verified observation")
        if previous:
            older=previous[-1].proof[name]
            if older.get("event_at") and (not current.get("event_at") or date(current["event_at"])<date(older["event_at"])
                    or date(current["event_at"])==date(older["event_at"]) and current["raw_status"]!=older["raw_status"]):
                fail("np_return_proof_invalid","Carrier events cannot regress or conflict at the same timestamp")
    if mode=="unpaid_cod" and (not proof["original"].get("event_at") or dispatched_at and
            date(proof["original"]["event_at"])<sales.aware(dispatched_at)-timedelta(seconds=30)):
        fail("np_return_refusal_required","An unpaid refusal requires a dated original event after dispatch")
    if mode=="unpaid_cod" and proof["original"].get("raw_status")!="103":
        earlier=previous and any(o.proof.get("original",{}).get("raw_status")=="103" for o in previous)
        if proof["original"].get("raw_status")!="102" or not earlier:
            fail("np_return_refusal_required","Unpaid closure requires an authoritative original refusal observation")
    return proof


def receipt_ready(observation,case):
    if not observation or observation.proof_hash!=digest(observation.proof) or observation.proof_canonical!=canonical(observation.proof):
        fail("np_return_proof_invalid","An exact verified carrier observation is required")
    proof=observation.proof
    observed=date(proof.get("observed_at"))
    if observed<datetime.now(UTC)-timedelta(seconds=min(settings.np_tracking_stale_seconds,7200)) or observed>datetime.now(UTC)+timedelta(seconds=30):
        fail("np_return_tracking_stale","Refresh carrier tracking before receiving contents")
    reverse=proof.get("reverse",{})
    if reverse.get("raw_status") not in {"9","10","11"} or reverse.get("status")!="delivered" or not reverse.get("event_at"):
        fail("np_return_delivery_required","A dated delivered reverse parcel is required before content inspection")
    event=date(reverse["event_at"])
    if event>observed+timedelta(seconds=30) or event<date(case.snapshot["dispatched_at"])-timedelta(seconds=30):
        fail("np_return_proof_invalid","Reverse delivery must follow the original dispatch")


async def observation(session,case,proof,actor):
    item=Observation(case_id=case.id,proof=proof,proof_canonical=canonical(proof),proof_hash=digest(proof),
        operator_id=actor.id,created_at=datetime.now(UTC))
    session.add(item);await session.flush()
    return item


def receipt_view(receipt):
    return {k:getattr(receipt,k) for k in ("id","case_id","observation_id","lines","evidence","snapshot_hash","operator_id")}|{"contents_verified":True,"created_at":receipt.created_at.isoformat()}


def cash_return_policy(sale_snapshot,lines):
    if sale_snapshot["payment"]["type"]=="CASH" and sum(line["total_minor"] for line in lines)%100:
        fail("checkbox_cash_rounding_unsupported","This partial cash return requires an unsupported rounding policy")


async def physical_for(session,item):
    if not item.carrier_receipt_id:return None
    receipt=await session.get(Receipt,item.carrier_receipt_id)
    case=await session.get(Case,receipt.case_id) if receipt else None
    if not valid_receipt(receipt,case) or case.order_id!=item.order_id or case.financial_mode!="paid" or item.lines!=case.lines:
        fail("np_return_receipt_required","The financial claim requires its exact canonical parcel receipt")
    return receipt


async def case_view(session,actor,order,case):
    ready=await readiness(session,actor);reasons=list(ready.blocking_reasons)
    p=gateway()
    try:
        await guard(session,p,case)
    except HTTPException as error:
        if error.detail["code"] not in reasons:reasons.append(error.detail["code"])
    receipt,closure=case.receipt,case.closure
    item=financial_for(order,case);doc=paid_resolution(order,case)
    obs=case.observations[-1] if case.observations else None
    receive_reasons=[]
    try:receipt_ready(obs,case)
    except HTTPException as error:receive_reasons.append(error.detail["code"])
    enabled=not reasons and not closure and not doc
    permissions={}
    for op in ("refresh","receive","close_unpaid","financial_claim"):
        fields={"refresh":["can_manage_shipments"],"receive":["can_manage_shipments","can_post_inventory"],
            "close_unpaid":["can_record_sales","can_manage_shipments"],"financial_claim":["can_record_sales"]}[op]
        permissions[op]=actor.is_superuser or all(getattr(actor,k,False) for k in fields)
    actions={"refresh":bool(enabled and permissions["refresh"] and not receipt),
        "receive":bool(enabled and permissions["receive"] and not receipt and not receive_reasons),
        "close_unpaid":bool(enabled and permissions["close_unpaid"] and case.financial_mode=="unpaid_cod" and receipt),
        "financial_claim":bool(enabled and permissions["financial_claim"] and case.financial_mode=="paid" and receipt and not item)}
    actions.update({"replay_"+k:v for k,v in permissions.items()})
    status="closed_unpaid" if closure else "closed_paid" if doc else "financial_claimed" if item else "received" if receipt else "arrived" if not receive_reasons else "awaiting_return"
    return dto.CaseView(id=case.id,order_id=order.id,revision=case.revision,order_revision=order.revision,profile_id=case.profile_id,
        profile_hash=case.profile_hash,authority=case.authority,transport=case.transport,test_context=case.test_context,
        original_shipment_id=case.original_shipment_id,original_ttn=case.original_ttn,return_ttn=case.return_ttn,
        financial_mode=case.financial_mode,linkage_kind=case.linkage_kind,linkage_authority="manual" if case.linkage_kind=="manual_document" else case.authority,
        manual_link=case.manual_link,sale_document_id=case.sale_document_id,status=status,lines=case.lines,
        observation={"id":obs.id,"proof_hash":obs.proof_hash,"proof":obs.proof,"created_at":obs.created_at.isoformat()} if obs else None,
        receipt=receipt_view(receipt) if receipt else None,
        closure=({k:getattr(closure,k) for k in ("id","case_id","order_id","receipt_id","evidence","snapshot_hash","operator_id")}|{"created_at":closure.created_at.isoformat()}) if closure else None,
        financial_return_id=item.id if item else None,financial_document_id=doc.id if doc else None,
        financial_remaining_lines=case.lines if case.financial_mode=="paid" and receipt and not item else [],actions=actions,
        blocking_reasons=list(dict.fromkeys(reasons+([] if receipt else receive_reasons))))


async def read(session,actor,resource,identifier=None):
    actor=await actor_for(session,actor)
    if resource=="readiness":return await readiness(session,actor)
    case=await session.get(Case,identifier) if resource=="case" else None
    if resource=="case" and not case:raise HTTPException(404,"Nova Poshta return case not found")
    order=await sales.load_order(session,case.order_id if case else identifier)
    if case:return await case_view(session,actor,order,next(c for c in cases(order) if c.id==case.id))
    ready=await readiness(session,actor);reasons=list(ready.blocking_reasons)
    shipment=mode=sale=None;lines=[]
    try:
        shipment,_,_,mode,sale=await eligible(session,order)
        if sale:
            lines=sale.operation.snapshot["items"]
        else:lines=[dict(order_item_id=i.id,product_id=i.product_id,name=i.product_name,quantity=i.quantity) for i in order.items]
    except HTTPException as error:reasons.append(error.detail["code"])
    existing=cases(order)
    if existing:reasons.append("np_return_case_exists")
    enabled=not reasons
    return dto.OrderView(order_id=order.id,revision=order.revision,enabled=enabled,readiness=ready,
        original_shipment_id=shipment.id if shipment else None,original_ttn=shipment.tracking_number if shipment else None,
        financial_mode=mode,eligible_lines=lines,cases=[await case_view(session,actor,order,c) for c in existing],
        actions={"attach":bool(enabled and (actor.is_superuser or actor.can_manage_shipments)),
            "replay_attach":bool(actor.is_superuser or actor.can_manage_shipments)},blocking_reasons=list(dict.fromkeys(reasons)))


async def unresolved(session,register_id):
    rows=list((await session.scalars(select(Case).where(Case.register_id==register_id))).all())
    unresolved_ids=[]
    for case in rows:
        order=await sales.load_order(session,case.order_id)
        current=next(c for c in cases(order) if c.id==case.id)
        if not current.closure and not paid_resolution(order,current):unresolved_ids.append(case.id)
    return unresolved_ids


async def command(session,actor,identifier,payload,raw_key,operation):
    from app.services import shop_checkbox as cb
    from app.services.np_return_provider import NPReturnError
    actor_id=actor.id
    key=operation_key(raw_key,f"shop-np-return:{actor_id}:{operation}:{identifier}")
    request_hash=digest(payload.model_dump(mode="json"))
    try:
        actor=await actor_for(session,actor,operation)
        previous=await session.get(OrderIntent,key)
        if previous:
            if previous.payload_hash!=request_hash:fail("idempotency_conflict","Key already used for another request")
            if previous.response_json:
                if previous.response_json["test_context"]:await sales.require_test_database(session,"np_return_test_unsafe")
                return dto.CaseView.model_validate(previous.response_json)
        p=gateway();await guard(session,p)
        case=None if operation=="attach" else await session.get(Case,identifier)
        if operation!="attach" and not case:raise HTTPException(404,"Nova Poshta return case not found")
        assertion(payload,p,case)
        if case:await guard(session,p,case)
        order_id=identifier if operation=="attach" else case.order_id
        order=await sales.load_order(session,order_id)
        if operation=="attach":
            shipment,fulfillment,row,mode,sale=await eligible(session,order)
            if shipment.id!=payload.original_shipment_id:fail("np_return_shipment_mismatch","Select the canonical original dispatch")
            if cases(order):fail("np_return_case_exists","Only one reverse parcel is supported for this outbound parcel")
            if order.revision!=payload.expected_revision:fail("revision_conflict","Reload order")
            original_ttn,return_ttn,kind=shipment.tracking_number,payload.return_ttn,payload.linkage_kind
            manual=payload.manual_link
            if manual and (manual.order_id!=order.id or manual.original_shipment_id!=shipment.id
                    or manual.original_ttn!=original_ttn or manual.return_ttn!=return_ttn):
                fail("np_return_manual_link_invalid","Documentary linkage must identify this exact order and parcel pair")
            if p.test_only is False and (row.authority=="test" or sales.r2_test_context(order) or fulfillment.authority=="test"):
                fail("np_return_profile_conflict","Test order facts cannot use a production carrier transport")
        else:
            original_ttn,return_ttn,kind,mode=case.original_ttn,case.return_ttn,case.linkage_kind,case.financial_mode
        proof=None
        if operation in {"attach","refresh"}:
            await session.rollback()
            try:proof=await p.verify_pair(original_ttn,return_ttn,linkage_kind=kind)
            except NPReturnError as error:fail(error.code,"Carrier lookup could not verify the exact original and reverse parcels")
            actor=await actor_for(session,SimpleNamespace(id=actor_id),operation)
        order=await sales.load_order(session,order_id,lock=True)
        if operation!="attach":
            case=await session.scalar(select(Case).where(Case.id==identifier).with_for_update().execution_options(populate_existing=True))
            await guard(session,gateway(),case)
            if not valid_case(case,order):fail("np_return_proof_invalid","Stored return binding requires reconciliation")
        else:await guard(session,gateway())
        command_intent=await claim_intent(session,key,request_hash)
        if command_intent.response_json:return dto.CaseView.model_validate(command_intent.response_json)
        if (order.revision if operation=="attach" else case.revision)!=payload.expected_revision:
            fail("revision_conflict","Reload the current return resource")
        if operation=="attach":
            shipment,fulfillment,row,mode,sale=await eligible(session,order)
            if shipment.id!=payload.original_shipment_id or shipment.tracking_number!=original_ttn:
                fail("np_return_shipment_mismatch","Original dispatch changed during lookup")
            row=await cb.register(session,register_id=row.id,lock=True)
            await cb.free_register(session,row)
            if cases(order):fail("np_return_case_exists","An existing reverse parcel already owns this dispatch")
            validate_proof(proof,p,original_ttn,return_ttn,kind,mode,dispatched_at=fulfillment.created_at)
            requested={line.order_item_id:line.quantity for line in payload.lines}
            if len(requested)!=len(payload.lines):fail("np_return_quantity_mismatch","Return lines must be distinct")
            if mode=="paid":
                if order.shop_checkbox_returns:fail("np_return_history_conflict","Existing financial return allocations cannot be adopted")
                lines=shop_fiscal.allocate_return_lines(sale.operation.snapshot,[line.model_dump() for line in payload.lines],{})
                if sum(line["total_minor"] for line in lines)<=0:fail("unsupported_zero_total","A parcel financial claim must have positive exact value")
                cash_return_policy(sale.operation.snapshot,lines)
            else:
                if requested!={i.id:i.quantity for i in order.items}:fail("np_return_unpaid_full_required","Unpaid refusal requires the complete original order")
                lines=[dict(order_item_id=i.id,product_id=i.product_id,name=i.product_name,quantity=i.quantity) for i in sorted(order.items,key=lambda i:i.id)]
            await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("np-return-ttn:"+return_ttn,0))))
            if original_ttn==return_ttn or await session.scalar(select(Shipment.id).where((Shipment.tracking_number==return_ttn)|(Shipment.provider_reference==return_ttn))):
                fail("np_return_history_conflict","A tracked or original shipment number cannot be adopted as a reverse parcel")
            context=p.test_only or row.authority=="test" or sales.r2_test_context(order) or fulfillment.authority=="test"
            if context and row.authority!="test":fail("checkbox_profile_conflict","A test carrier return cannot enter a production fiscal binding")
            state=dict(order_id=order.id,original_shipment_id=shipment.id,fulfillment_id=fulfillment.id,
                original_ttn=original_ttn,return_ttn=return_ttn,financial_mode=mode,sale_document_id=sale.id if sale else None,
                register_id=row.id,register_profile_hash=row.profile_hash,lines=lines,profile_id=p.profile["id"],profile_hash=digest(p.profile),
                authority="test" if p.test_only else "provider",transport="mock_http" if p.test_only else "https",test_context=bool(context),
                linkage_kind=kind,manual_link=payload.manual_link.model_dump(mode="json") if payload.manual_link else None,
                dispatched_at=sales.aware(fulfillment.created_at).isoformat())
            case=Case(**{k:v for k,v in state.items() if k!="dispatched_at"},profile=p.profile,snapshot=state,snapshot_hash=digest(state),
                reason=payload.reason,revision=1,operator_id=actor.id,created_at=datetime.now(UTC))
            session.add(case);await session.flush();await observation(session,case,proof,actor)
            order.revision+=1
        else:
            row=await cb.register(session,register_id=case.register_id,lock=True)
            if case.closure or paid_resolution(order,case):fail("np_return_closed","The return is already resolved")
            if operation=="refresh":
                if case.receipt:fail("np_return_receipt_exists","Carrier proof is already consumed by a physical receipt")
                validate_proof(proof,p,original_ttn,return_ttn,kind,mode,previous=case.observations,dispatched_at=date(case.snapshot["dispatched_at"]))
                await observation(session,case,proof,actor)
            elif operation=="receive":
                if case.receipt:fail("np_return_receipt_exists","Parcel contents are already recorded")
                obs=case.observations[-1] if case.observations else None
                receipt_ready(obs,case)
                requested={l.order_item_id:l for l in payload.lines}
                if len(requested)!=len(payload.lines) or {k:v.quantity for k,v in requested.items()}!={l["order_item_id"]:l["quantity"] for l in case.lines}:
                    fail("np_return_quantity_mismatch","Content inspection must cover every selected parcel line exactly once")
                lines=[l.model_dump() for l in sorted(payload.lines,key=lambda l:l.order_item_id)]
                snapshot=dict(case_id=case.id,case_hash=case.snapshot_hash,observation_id=obs.id,lines=lines,contents_verified=True,test_context=case.test_context)
                receipt=Receipt(case_id=case.id,observation_id=obs.id,lines=lines,snapshot=snapshot,snapshot_hash=digest(snapshot),
                    evidence=payload.evidence,operator_id=actor.id,created_at=datetime.now(UTC))
                session.add(receipt);await session.flush()
                products={p.id:p for p in (await session.scalars(select(Product).where(Product.id.in_(sorted({l["product_id"] for l in case.lines})))
                    .order_by(Product.id).with_for_update().execution_options(populate_existing=True))).all()}
                for line in case.lines:
                    shipped=await session.scalar(select(func.coalesce(func.sum(-InventoryMovement.on_hand_delta),0)).where(
                        InventoryMovement.order_id==order.id,InventoryMovement.order_item_id==line["order_item_id"],InventoryMovement.product_id==line["product_id"],
                        InventoryMovement.movement_type==InventoryMovementType.shipment,
                        InventoryMovement.reference_key==f'order:{order.id}:item:{line["order_item_id"]}:shipment'))
                    restored=await session.scalar(select(func.coalesce(func.sum(InventoryMovement.on_hand_delta),0)).where(
                        InventoryMovement.order_id==order.id,InventoryMovement.order_item_id==line["order_item_id"],InventoryMovement.product_id==line["product_id"],
                        InventoryMovement.movement_type==InventoryMovementType.customer_return))
                    if restored+line["quantity"]>shipped:fail("shipped_quantity_exceeded","The parcel exceeds the original shipment ledger")
                    if requested[line["order_item_id"]].disposition=="restock":
                        products[line["product_id"]].stock_quantity+=line["quantity"]
                        session.add(InventoryMovement(product_id=line["product_id"],movement_type=InventoryMovementType.customer_return,
                            quantity=line["quantity"],on_hand_delta=line["quantity"],reserved_delta=0,order_id=order.id,order_item_id=line["order_item_id"],
                            performed_by_user_id=actor.id,reference_key=f'shop-np-return:{receipt.id}:{line["order_item_id"]}',reason=payload.evidence))
                order.revision+=1
            elif operation=="close_unpaid":
                if case.financial_mode!="unpaid_cod":fail("np_return_unpaid_required","Only an unpaid full refusal can close without fiscal money")
                if not valid_receipt(case.receipt,case):fail("np_return_receipt_required","Record the exact full parcel contents first")
                if order.payments or order.shop_payment_intent or order.qr_payment_attempts or order.shop_checkbox_returns or order.shop_checkbox_documents or order.shop_completion:
                    fail("np_return_unpaid_facts_conflict","Unpaid closure cannot have money, intents or fiscal documents")
                state=dict(case_id=case.id,order_id=order.id,receipt_id=case.receipt.id,case_hash=case.snapshot_hash,receipt_hash=case.receipt.snapshot_hash,test_context=case.test_context)
                session.add(Closure(case_id=case.id,order_id=order.id,receipt_id=case.receipt.id,snapshot=state,snapshot_hash=digest(state),
                    evidence=payload.evidence,operator_id=actor.id,created_at=datetime.now(UTC)))
                await session.flush();order.status=OrderStatus.cancelled;order.revision+=1
            elif operation=="financial_claim":
                if case.financial_mode!="paid":fail("np_return_sale_required","Unpaid refusal cannot create a financial return")
                if not valid_receipt(case.receipt,case):fail("np_return_receipt_required","Receive exact parcel contents before claiming money")
                if financial_for(order,case):fail("np_return_claim_exists","The exact parcel already has its financial claim")
                original=await cb.issued(session,await cb.load_document(session,case.sale_document_id))
                cash_return_policy(original.snapshot,case.lines)
                provider=cb.gateway(row.profile);await cb.guard(session,provider)
                await cb.free_register(session,row);cb.open_register(row);await cb.binding(session,order,row)
                if case.test_context and row.authority!="test":fail("checkbox_profile_conflict","Test parcel facts cannot become production fiscal proof")
                session.add(FinancialReturn(order_id=order.id,sale_document_id=case.sale_document_id,register_id=row.id,shift_id=row.shift_id,
                    amount_minor=sum(l["total_minor"] for l in case.lines),lines=case.lines,reason=payload.reason,revision=1,
                    carrier_receipt_id=case.receipt.id,operator_id=actor.id,created_at=datetime.now(UTC)))
                order.revision+=1
            else:raise ValueError("Unsupported Nova Poshta return command")
            case.revision+=1
        session.add(Audit(case_id=case.id,request_key=key,operation=operation,facts={"case_revision":case.revision,"order_revision":order.revision},operator_id=actor.id,created_at=datetime.now(UTC)))
        await session.flush()
        order=await sales.load_order(session,order_id)
        case=next(c for c in cases(order) if c.id==case.id)
        result=await case_view(session,actor,order,case)
        command_intent.response_json=result.model_dump(mode="json");command_intent.order_id=order.id
        await guard(session,gateway(),case)
        if not settings.shop_np_returns_enabled or not settings.shop_order_operations_enabled:fail("shop_np_returns_disabled","Return operations closed during admission",503)
        await session.commit();return result
    except IntegrityError:
        await session.rollback();fail("np_return_case_exists","Parcel, claim or immutable receipt identity is already in use")
    except Exception:
        await session.rollback();raise
