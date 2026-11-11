"""Compare the local order projection with authenticated synthetic shift evidence."""
from datetime import UTC, datetime, timedelta
from hmac import compare_digest, digest

import httpx
from pydantic import TypeAdapter

from app.core.config import settings
from app.schemas.shop_external_fiscal_reports import (
    ExternalFiscalReconciliation,
    ProviderReportComparison,
    ReportKind,
    SyntheticReportBatch,
    SyntheticReportEvidence,
)
from app.schemas.shop_mono_checkbox_refunds import DirectFiscalReport
from app.services import shop_mono_checkbox as direct, shop_mono_checkbox_refunds as refunds, shop_sales as sales


_REPORT_BODY = TypeAdapter(SyntheticReportBatch | SyntheticReportEvidence)


class MockReportGateway:
    def __init__(self, transport: httpx.MockTransport, key: bytes):
        if type(transport) is not httpx.MockTransport or type(key) is not bytes or len(key) < 32:
            raise TypeError("External report gateway requires an isolated HTTP mock and synthetic signing key")
        self._transport = transport
        self._key = key

    async def fetch(self, kind: ReportKind, identity: dict[str, str]) -> list[SyntheticReportEvidence]:
        async with httpx.AsyncClient(
            transport=self._transport, base_url="https://checkbox-report.invalid",
            follow_redirects=False, trust_env=False, timeout=5,
        ) as client:
            response = await client.get(f"/synthetic/external-fiscal-reports/{kind}", params=identity)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.content
        signature = response.headers.get("X-Synthetic-Report-Signature", "")
        if len(body) > 65536 or not compare_digest(signature.encode(), digest(self._key, body, "sha256").hex().encode()):
            raise ValueError("Invalid synthetic report signature or size")
        parsed = _REPORT_BODY.validate_json(body)
        return parsed.reports if isinstance(parsed, SyntheticReportBatch) else [parsed]


def gateway() -> MockReportGateway | None:
    return None


async def compare(
    provider: MockReportGateway,
    kind: ReportKind,
    identity: dict[str, str],
    local: DirectFiscalReport,
    receipt_causal_bounds: dict[str, datetime],
) -> ProviderReportComparison:
    try:
        reports = await provider.fetch(kind, identity)
    except httpx.HTTPError:
        return ProviderReportComparison(status="unavailable")
    except ValueError:
        return ProviderReportComparison(status="invalid")
    if not reports:
        return ProviderReportComparison(status="missing")

    expected = {entry.receipt_id: (entry.kind, entry.original_receipt_id, entry.amount_minor) for entry in local.entries}
    receipts = [entry for report in reports for entry in report.receipts]
    observed = {
        str(entry.receipt_id): (
            entry.kind, str(entry.original_receipt_id) if entry.original_receipt_id else None, entry.amount_minor
        )
        for entry in receipts
    }
    latest_report_time = datetime.now(UTC) + timedelta(seconds=30)
    if (
        len({report.report_id for report in reports}) != len(reports)
        or len({report.shift_id for report in reports}) != len(reports)
        or len(observed) != len(receipts)
        or any(expected.get(identifier) != facts for identifier, facts in observed.items())
        or any(
            report.kind != kind
            or report.reported_at > latest_report_time
            or any(getattr(report, field) != value for field, value in identity.items())
            or any(
                str(entry.receipt_id) in receipt_causal_bounds
                and report.reported_at < receipt_causal_bounds[str(entry.receipt_id)]
                for entry in report.receipts
            )
            or sum(entry.amount_minor for entry in report.receipts if entry.kind == "sale") > report.shift_sale_minor
            or sum(entry.amount_minor for entry in report.receipts if entry.kind == "return") > report.shift_return_minor
            for report in reports
        )
    ):
        return ProviderReportComparison(status="conflict")
    missing = [entry.receipt_id for entry in local.entries if entry.receipt_id not in observed]
    return ProviderReportComparison(
        status="partial" if missing else "matched",
        evidence=reports[0] if len(reports) == 1 else None,
        reports=reports,
        chronology_checked=bool(observed) and all(identifier in receipt_causal_bounds for identifier in observed),
        missing_receipt_ids=missing,
    )


async def read(session, actor, order_id):
    order = await sales.load_order(session, order_id)
    await sales.current_actor(session, actor, "read", order)
    applicable = direct.has_direct_facts(order)
    sale = direct.accepted_receipt(order) if applicable else None
    refund = await refunds.record(session, order_id) if applicable else None
    local = refunds.report(order, refund) if sale else None
    refund_status = "none"
    if refund:
        refund_status = "finalized" if local and len(local.entries) == 2 else "unresolved"

    x = z = ProviderReportComparison(status="not_applicable" if not applicable else "unavailable")
    provider = gateway()
    revision, refund_reason = order.revision, refund.reason if refund else None
    if (
        local and sale.authority == "test" and sale.proof.get("transport") == "mock_http"
        and settings.shop_order_operations_enabled and settings.shop_mono_checkbox_enabled
        and type(provider) is MockReportGateway and await sales.test_database_is_safe(session)
    ):
        identity = {
            "invoice_id": sale.invoice_id,
            "profile_hash": sale.profile_hash,
            "merchant_id": sale.snapshot["merchant_id"],
        }
        # Local import/finalization can follow a historical provider shift.
        # Only use causal lower bounds, never those local observation times.
        causal_bounds = {}
        try:
            paid_at = datetime.fromisoformat(order.shop_payment_evidence.proof.get("accepted_at"))
        except (TypeError, ValueError):
            paid_at = None
        if paid_at and paid_at.tzinfo:
            causal_bounds[sale.receipt_id] = paid_at
        if refund_status == "finalized":
            causal_bounds[refund.return_receipt_id] = sales.aware(refund.created_at)
        await session.rollback()
        x = await compare(provider, "x", identity, local, causal_bounds)
        z = await compare(provider, "z", identity, local, causal_bounds)
    return ExternalFiscalReconciliation(
        order_id=order_id,
        revision=revision,
        local_status="projected" if local else "sale_unresolved" if applicable else "not_applicable",
        local_projection=local,
        refund_status=refund_status,
        refund_reason=refund_reason,
        provider_x=x,
        provider_z=z,
    )
