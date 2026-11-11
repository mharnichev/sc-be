import json
from datetime import UTC, datetime, timedelta
from hmac import digest
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.routes.shop_external_fiscal_reports import backoffice_router
from app.core.database import get_db_session
from app.dependencies.auth import get_current_admin_user
from app.schemas.shop_mono_checkbox_refunds import DirectFiscalEntry, DirectFiscalReport
from app.services import mono_checkbox_poll as poll, shop_external_fiscal_reports as service
from tests.test_shop_fiscal_integration import checkout_dependencies, database, template_database  # noqa: F401
from tests.test_shop_online_payment_integration import anyio_backend, online, prepared, start  # noqa: F401

pytestmark = pytest.mark.anyio
KEY = b"isolated-fiscal-report-fixture-key-only"
HISTORICAL = datetime.now(UTC) - timedelta(days=30)


def response(payload, key=KEY):
    body = json.dumps(payload).encode()
    return httpx.Response(200, content=body, headers={
        "X-Synthetic-Report-Signature": digest(key, body, "sha256").hex(),
    })


def evidence(kind, identity, entries):
    return dict(**identity, kind=kind, authority="test", scope="provider_cash_register_shift",
                report_id=str(uuid4()), shift_id=str(uuid4()), register_id="synthetic-register",
                reported_at=HISTORICAL.isoformat(), currency="UAH",
                shift_sale_minor=30000, shift_return_minor=12000, receipts=entries)


@pytest.mark.parametrize(("change", "status"), [
    ("none", "matched"), ("omit_return", "partial"), ("amount", "conflict"), ("link", "conflict"),
    ("profile", "conflict"), ("duplicate", "conflict"), ("wrong_kind", "conflict"),
    ("shift_total", "conflict"), ("numeric_string", "invalid"), ("unsigned", "invalid"),
    ("unavailable", "unavailable"), ("absent", "missing"),
    ("before_receipt_evidence", "conflict"), ("future", "conflict"),
])
async def test_exact_authenticated_report_membership(change, status):
    sale_id, return_id = str(uuid4()), str(uuid4())
    identity = dict(invoice_id="invoice-1", merchant_id="merchant-1", profile_hash="a" * 64)
    rows = [
        dict(kind="sale", receipt_id=sale_id, original_receipt_id=None, amount_minor=20000),
        dict(kind="return", receipt_id=return_id, original_receipt_id=sale_id, amount_minor=10000),
    ]
    local = DirectFiscalReport(authority="test", net_minor=10000, entries=[
        DirectFiscalEntry(**row, structured_totals_verified=(row["kind"] == "return")) for row in rows
    ])
    payload = evidence("z", identity, rows)
    if change == "omit_return":
        rows.pop()
    elif change == "amount":
        rows[1]["amount_minor"] = 9999
    elif change == "link":
        rows[1]["original_receipt_id"] = str(uuid4())
    elif change == "profile":
        payload["profile_hash"] = "b" * 64
    elif change == "duplicate":
        rows[1] = rows[0]
    elif change == "wrong_kind":
        payload["kind"] = "x"
    elif change == "shift_total":
        payload["shift_return_minor"] = 9999
    elif change == "numeric_string":
        rows[0]["amount_minor"] = "20000"
    elif change == "before_receipt_evidence":
        payload["reported_at"] = (HISTORICAL - timedelta(seconds=1)).isoformat()
    elif change == "future":
        payload["reported_at"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "checkbox-report.invalid"
        assert request.url.path == "/synthetic/external-fiscal-reports/z"
        assert dict(request.url.params) == identity
        if change == "absent":
            return httpx.Response(404)
        if change == "unavailable":
            raise httpx.ReadTimeout("isolated mock timeout")
        return response(payload, key=b"wrong-key" if change == "unsigned" else KEY)

    gateway = service.MockReportGateway(httpx.MockTransport(handler), KEY)
    result = await service.compare(
        gateway, "z", identity, local, {sale_id: HISTORICAL - timedelta(days=1), return_id: HISTORICAL}
    )
    assert result.status == status
    if status in {"matched", "partial"}:
        assert result.evidence.shift_sale_minor == 30000 and result.chronology_checked
        assert local.net_minor == 10000
        assert list(map(str, result.missing_receipt_ids)) == ([return_id] if status == "partial" else [])
    else:
        assert result.evidence is None


async def test_endpoint_local_projection_and_independent_report(database, checkout_dependencies, online, monkeypatch):
    order_id, _, token = await prepared(database)
    await start(database, order_id, token)
    sale_id = str(uuid4())
    payment_event_at = datetime.now(UTC).isoformat()
    online.update(status="success", payment_event_at=payment_event_at, checks=[
        dict(id=sale_id, status="done", type="sale", fiscalizationSource="checkbox")
    ])
    api = FastAPI()
    api.include_router(backoffice_router)
    api.dependency_overrides[get_current_admin_user] = lambda: SimpleNamespace(id=1)

    async def session():
        async with database() as db:
            yield db

    api.dependency_overrides[get_db_session] = session
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/z"):
            return httpx.Response(404)
        payload = evidence("x", dict(request.url.params), [
            dict(kind="sale", receipt_id=sale_id, original_receipt_id=None, amount_minor=10000)
        ])
        payload["reported_at"] = payment_event_at
        return response(payload)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        path = f"/shop-external-fiscal-reports/orders/{order_id}"
        unresolved = (await client.get(path)).json()
        assert unresolved["local_status"] == "sale_unresolved"
        assert unresolved["local_projection"] is None
        assert await poll.poll_once(database) == {"checked": 1, "blocked": 0}
        provider_calls = online["calls"][:]

        disabled = (await client.get(path)).json()
        assert disabled["provider_x"]["status"] == disabled["provider_z"]["status"] == "unavailable"
        assert disabled["local_projection"]["net_minor"] == 10000

        monkeypatch.setattr(service, "gateway", lambda: service.MockReportGateway(httpx.MockTransport(handler), KEY))
        report = await client.get(path)
        assert report.status_code == 200
        report = report.json()
        assert report["local_projection"] == disabled["local_projection"]
        assert report["local_projection"]["entries"][0]["structured_totals_verified"] is False
        assert report["provider_x"]["status"] == "matched" and report["provider_x"]["chronology_checked"]
        assert report["provider_x"]["evidence"]["authority"] == "test"
        assert report["provider_z"]["status"] == "missing"
        assert calls == ["/synthetic/external-fiscal-reports/x", "/synthetic/external-fiscal-reports/z"]
        assert online["calls"] == provider_calls
        assert report["revision"] == disabled["revision"]
        assert report["refund_status"] == "none"

        async def unsafe_database(_session):
            return False

        monkeypatch.setattr(service.sales, "test_database_is_safe", unsafe_database)
        blocked = (await client.get(path)).json()
        assert blocked["local_projection"] == report["local_projection"]
        assert blocked["provider_x"]["status"] == blocked["provider_z"]["status"] == "unavailable"
        assert len(calls) == 2


@pytest.mark.parametrize("kind", ["x", "z"])
@pytest.mark.parametrize("conflict", [None, "shift_id", "report_id", "receipt"])
async def test_separate_sale_and_return_shifts(kind, conflict):
    sale_id, return_id = str(uuid4()), str(uuid4())
    identity = dict(invoice_id="invoice-1", merchant_id="merchant-1", profile_hash="a" * 64)
    sale = dict(kind="sale", receipt_id=sale_id, original_receipt_id=None, amount_minor=20000)
    returned = dict(kind="return", receipt_id=return_id, original_receipt_id=sale_id, amount_minor=10000)
    local = DirectFiscalReport(authority="test", net_minor=10000, entries=[
        DirectFiscalEntry(**sale, structured_totals_verified=False),
        DirectFiscalEntry(**returned, structured_totals_verified=True),
    ])
    first = evidence(kind, identity, [sale])
    first.update(shift_sale_minor=20000, shift_return_minor=0)
    second = evidence(kind, identity, [returned])
    return_time = HISTORICAL + timedelta(days=1)
    second.update(shift_sale_minor=0, shift_return_minor=10000, reported_at=return_time.isoformat())
    if conflict == "receipt":
        second.update(receipts=[sale, returned], shift_sale_minor=20000)
    elif conflict:
        second[conflict] = first[conflict]

    gateway = service.MockReportGateway(httpx.MockTransport(
        lambda _: response(dict(reports=[first, second]))
    ), KEY)
    result = await service.compare(gateway, kind, identity, local, {sale_id: HISTORICAL, return_id: return_time})
    assert result.evidence is None
    assert result.missing_receipt_ids == []
    if conflict:
        assert result.status == "conflict"
        assert result.reports == []
    else:
        assert result.status == "matched"
        assert [str(report.shift_id) for report in result.reports] == [first["shift_id"], second["shift_id"]]
        assert [str(report.receipts[0].receipt_id) for report in result.reports] == [sale_id, return_id]


async def test_signed_report_without_causal_bound_does_not_claim_chronology():
    sale_id = str(uuid4())
    identity = dict(invoice_id="invoice-1", merchant_id="merchant-1", profile_hash="a" * 64)
    sale = dict(kind="sale", receipt_id=sale_id, original_receipt_id=None, amount_minor=20000)
    local = DirectFiscalReport(authority="test", net_minor=20000, entries=[
        DirectFiscalEntry(**sale, structured_totals_verified=False),
    ])
    gateway = service.MockReportGateway(httpx.MockTransport(lambda _: response(evidence("z", identity, [sale]))), KEY)
    result = await service.compare(gateway, "z", identity, local, {})
    assert result.status == "matched" and not result.chronology_checked
