"""Read-only operational triage. No recovery, resend or reserve release."""
from datetime import UTC,datetime,timedelta
from fastapi import HTTPException
from sqlalchemy import select,text
from app.models.admin_user import AdminUser
from app.core.config import settings
from app.services import sales_access

# Only registered application task labels, never arbitrary state or exception text.
WATCH_SCHEDULER_NAMES=frozenset({
    'checkout-reminder-scheduler',
    'mono-checkbox-payment-receipt-scheduler',
    'booking-auto-completion-scheduler','sms-queue-worker','campaign-run-scheduler',
    'product-top-cache-scheduler','service-popularity-cache-scheduler',
    'review-request-scheduler','sms-delivery-status-scheduler',
    'booking-funnel-weekly-digest-scheduler','waitlist-offer-scheduler',
    'customer-activity-notification-scheduler','repeat-booking-scheduler',
    'master-schedule-reminder-scheduler','brevo-contact-sync-scheduler',
    'nova-poshta-tracking-scheduler','nova-poshta-directory-scheduler',
})

# These are durable fact markers, independent of current feature flags.
TEST_ORDER = """(o.simulation OR
 EXISTS(SELECT 1 FROM order_shipments s WHERE s.order_id=o.id AND s.simulation) OR
 EXISTS(SELECT 1 FROM shop_fulfillment_facts f WHERE f.order_id=o.id AND f.authority='test') OR
 EXISTS(SELECT 1 FROM shop_completions c WHERE c.order_id=o.id AND c.authority='test') OR
 EXISTS(SELECT 1 FROM shop_payment_confirmations p WHERE p.order_id=o.id AND p.authority='test') OR
 EXISTS(SELECT 1 FROM shop_payment_intents p WHERE p.order_id=o.id AND (p.authority='test' OR p.profile->>'environment'='test')) OR
 EXISTS(SELECT 1 FROM shop_checkbox_bindings b JOIN shop_checkbox_registers r ON r.id=b.register_id WHERE b.order_id=o.id AND (b.authority='test' OR r.profile->>'environment'='test')) OR
 EXISTS(SELECT 1 FROM shop_fiscal_documents d WHERE d.order_id=o.id) OR
 EXISTS(SELECT 1 FROM shop_carrier_returns c WHERE c.order_id=o.id AND c.authority='test') OR
 EXISTS(SELECT 1 FROM shop_np_return_cases c WHERE c.order_id=o.id AND (c.test_context OR c.authority='test' OR c.profile->>'environment'='test')) OR
 EXISTS(SELECT 1 FROM shop_payment_resolution_cases c LEFT JOIN shop_payment_bank_closures b ON b.case_id=c.id WHERE c.order_id=o.id AND (c.test_context OR b.policy->>'environment'='test')))"""
ISSUES = """
WITH issues AS (
 SELECT 'pending_order' AS reason,o.id::text AS entity_id,o.id AS order_id,o.created_at AS since,
 CASE WHEN TEST_MARKER THEN 'test' ELSE 'unverified' END AS authority,
 COALESCE((SELECT sum(m.reserved_delta) FROM inventory_movements m WHERE m.order_id=o.id),0)::bigint AS reserved_units
 FROM orders o WHERE o.channel='shop' AND o.status::text IN ('pending','confirmed') AND o.created_at<:cutoff
 UNION ALL
 SELECT 'payment_without_sale',p.id::text,p.order_id,p.created_at,CASE WHEN TEST_MARKER THEN 'test' ELSE 'unverified' END,NULL::bigint
 FROM shop_payment_confirmations p JOIN orders o ON o.id=p.order_id JOIN order_payments m ON m.id=p.payment_id AND m.order_id=o.id
 WHERE p.created_at<:cutoff AND NOT EXISTS(SELECT 1 FROM shop_checkbox_documents d JOIN shop_checkbox_operations op ON op.id=d.operation_id JOIN shop_checkbox_acceptances a ON a.operation_id=op.id WHERE d.order_id=o.id AND d.kind='sale' AND op.status='accepted' AND a.proof::jsonb->'verified'='true'::jsonb AND a.proof::jsonb->'accepted'='true'::jsonb)
 AND NOT EXISTS(SELECT 1 FROM shop_fiscal_documents d WHERE d.order_id=o.id AND d.kind='sale' AND d.status='issued')
 UNION ALL
 SELECT CASE WHEN p.status='unknown' THEN 'fiscal_unknown' ELSE 'fiscal_stale' END,p.id::text,p.order_id,p.created_at,
 CASE WHEN p.authority='test' OR COALESCE(TEST_MARKER,false) THEN 'test' ELSE 'unverified' END,NULL::bigint
 FROM shop_checkbox_operations p LEFT JOIN orders o ON o.id=p.order_id
 WHERE p.status IN ('queued','pending','unknown') AND (p.status='unknown' OR p.created_at<:cutoff)
 UNION ALL
 SELECT CASE WHEN EXISTS(SELECT 1 FROM shop_payment_financial_exceptions e WHERE e.case_id=c.id) THEN 'financial_exception' ELSE 'payment_resolution_pending' END,
 c.id::text,c.order_id,c.created_at,CASE WHEN TEST_MARKER THEN 'test' ELSE 'unverified' END,NULL::bigint
 FROM shop_payment_resolution_cases c JOIN orders o ON o.id=c.order_id
 WHERE EXISTS(SELECT 1 FROM shop_payment_financial_exceptions e WHERE e.case_id=c.id)
 OR (NOT EXISTS(SELECT 1 FROM shop_payment_resolution_finalizations f WHERE f.case_id=c.id)
 AND (c.kind='paid_cancellation' OR NOT EXISTS(SELECT 1 FROM shop_payment_evidence e JOIN order_payments p ON p.id=e.payment_id WHERE e.order_id=o.id))
 AND c.created_at<:cutoff)
 UNION ALL
 SELECT 'tracking_stale',s.id::text,s.order_id,COALESCE(s.last_checked_at,s.created_at),
 CASE WHEN s.simulation OR TEST_MARKER THEN 'test' ELSE 'unverified' END,NULL::bigint
 FROM order_shipments s JOIN orders o ON o.id=s.order_id WHERE o.channel='shop' AND s.active
 AND COALESCE(s.tracking_status,'unknown') NOT IN ('delivered','refused','returned','cancelled','deleted')
 AND COALESCE(s.last_checked_at,s.created_at)<:cutoff
 UNION ALL
 SELECT 'np_return_pending',c.id::text,c.order_id,c.created_at,CASE WHEN TEST_MARKER THEN 'test' ELSE 'unverified' END,NULL::bigint
 FROM shop_np_return_cases c JOIN orders o ON o.id=c.order_id WHERE c.created_at<:cutoff AND
 (NOT EXISTS(SELECT 1 FROM shop_np_return_receipts r WHERE r.case_id=c.id) OR
  (c.financial_mode='unpaid_cod' AND NOT EXISTS(SELECT 1 FROM shop_np_unpaid_closures u WHERE u.case_id=c.id)) OR
  (c.financial_mode='paid' AND NOT EXISTS(SELECT 1 FROM shop_np_return_receipts r JOIN shop_checkbox_returns q ON q.carrier_receipt_id=r.id JOIN shop_checkbox_documents d ON d.return_id=q.id AND d.kind='return' JOIN shop_checkbox_operations op ON op.id=d.operation_id JOIN shop_checkbox_acceptances a ON a.operation_id=op.id WHERE r.case_id=c.id AND op.status='accepted' AND a.proof::jsonb->'verified'='true'::jsonb AND a.proof::jsonb->'accepted'='true'::jsonb)))
 UNION ALL
 SELECT CASE WHEN j.status='uncertain' THEN 'sms_uncertain' ELSE 'sms_queue_stale' END,j.id,NULL::integer,j.created_at,'unverified',NULL::bigint
 FROM sms_queue_jobs j WHERE j.account_key=:sms_account AND (j.status='uncertain' OR (j.status IN ('queued','dispatching') AND j.created_at<:cutoff))
)
""".replace('TEST_MARKER',TEST_ORDER)

async def snapshot(session,actor,*,limit=50,stale_minutes=60,process_state=None):
    if not 1<=limit<=100 or not 1<=stale_minutes<=10080:
        raise HTTPException(422,'Invalid watch bounds')
    current=await session.scalar(select(AdminUser).where(AdminUser.id==actor.id).execution_options(populate_existing=True))
    if not current or not current.is_active or not (current.is_superuser or current.can_record_sales and current.can_manage_fiscal_settings):
        raise HTTPException(403,'Shop operational and fiscal oversight permissions required')
    await sales_access.deny_master(session,current)
    now=datetime.now(UTC);params={'cutoff':now-timedelta(minutes=stale_minutes),'limit':limit,'sms_account':settings.sms_club_account_key}
    counts=dict((await session.execute(text(ISSUES+'SELECT reason,count(*) FROM issues GROUP BY reason'),params)).all())
    rows=(await session.execute(text(ISSUES+'SELECT * FROM issues ORDER BY since,reason,entity_id LIMIT :limit'),params)).mappings().all()
    # Never forward arbitrary state/exception objects or provider data.
    state=process_state or {}
    roles={'disabled','local','owner','follower'};states={'disabled','starting','running','standby','lost','stopping','stopped','failed'}
    errors={'owner_busy','owner_unavailable','owner_lost','worker_failed','worker_exited','startup_failed','shutdown_failed'}
    names=state.get('scheduler_names',[])
    safe_names=list(dict.fromkeys(name for name in names[:100]
        if isinstance(name,str) and name in WATCH_SCHEDULER_NAMES)) if isinstance(names,list) else []
    safe_state={'role':state.get('role') if state.get('role') in roles else 'unknown',
                'state':state.get('state') if state.get('state') in states else 'unknown',
                'last_error_code':state.get('last_error_code') if state.get('last_error_code') in errors else None,
                'scheduler_names':safe_names}
    return {'observed_at':now,'stale_minutes':stale_minutes,'counts':counts,'total':sum(counts.values()),
            'truncated':sum(counts.values())>limit,'issues':[dict(row) for row in rows],
            'background_process':safe_state,'automatic_actions':False}
