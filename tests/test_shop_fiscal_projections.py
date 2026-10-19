"""Projection trust boundaries: test receipt availability is never delivery proof."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace as Row

import pytest

from app.models.order import Order
from app.services.order_documents import digest
from app.services.shop_fiscal import fiscal_projection


def document(identifier=1, *, kind="sale", amount=10000):
    state = {"document_kind": "shop_mock_fiscal_receipt", "authority": "test",
             "is_sent_dps": False, "payment_confirmation_id": 7, "amount_minor": amount}
    row = Row(id=identifier, kind=kind, status="issued", payment_confirmation_id=7,
              provider_uuid=f"test-uuid-{identifier}", snapshot=state, snapshot_hash=digest(state))
    row.acceptance = Row(document_id=identifier, provider_uuid=row.provider_uuid, snapshot_hash=row.snapshot_hash)
    return row


@pytest.fixture
def order():
    snapshot = {"order_id": 1, "payment_id": 2, "amount_minor": 10000, "currency": "UAH",
                "method": "cash", "authority": "manual", "namespace": "shop_manual",
                "external_reference": "TEST-REFERENCE", "source": "manual"}
    confirmation = Row(id=7, order_id=1, payment_id=2, source="manual", authority="manual",
                       snapshot=snapshot, snapshot_hash=digest(snapshot), namespace="shop_manual",
                       external_reference="TEST-REFERENCE", operator_id=3)
    payment = Row(id=2, simulation=False, amount_minor=10000, method="cash", source="shop_manual",
                  evidence="TEST-REFERENCE", operator_id=3)
    return Row(id=1, channel="shop", simulation=False, total_amount=Decimal("100.00"),
               payments=[payment], shop_payment_confirmation=confirmation,
               shop_fiscal_documents=[document()], shop_financial_returns=[])


@pytest.mark.parametrize("invalid", ["missing", "document", "uuid", "hash", "snapshot", "authority", "dps", "payment"])
def test_issued_projection_rejects_unbound_receipt_proof(order, invalid):
    row = order.shop_fiscal_documents[0]
    if invalid == "missing":
        row.acceptance = None
    elif invalid == "document":
        row.acceptance.document_id = 99
    elif invalid == "uuid":
        row.acceptance.provider_uuid = "another-operation"
    elif invalid == "hash":
        row.acceptance.snapshot_hash = "0" * 64
    elif invalid == "snapshot":
        row.snapshot["amount_minor"] += 1
    elif invalid in {"authority", "dps"}:
        row.snapshot["authority" if invalid == "authority" else "is_sent_dps"] = "production" if invalid == "authority" else True
        row.snapshot_hash = row.acceptance.snapshot_hash = digest(row.snapshot)
    else:
        row.payment_confirmation_id = 99
    assert fiscal_projection(order) == "simulated_fiscal_invalid"
    assert Order.receipt_delivery_status.fget(order) == "simulated_pending_document"


def test_test_receipt_is_only_local_print_available(order):
    assert fiscal_projection(order) == "simulated_fiscal_issued"
    assert Order.fiscal_status.fget(order) == "simulated_fiscal_issued"
    assert Order.receipt_delivery_status.fget(order) == "simulated_local_print_available"


@pytest.mark.parametrize("returned,expected", [(5000, "partial_return"), (10000, "returned")])
def test_return_projection_requires_issued_return_acceptance(order, returned, expected):
    order.shop_financial_returns = [Row(amount_minor=returned)]
    row = document(2, kind="return", amount=returned)
    order.shop_fiscal_documents.append(row)
    row.acceptance = None
    assert fiscal_projection(order) == "simulated_fiscal_return_pending"
    row.acceptance = Row(document_id=2, provider_uuid=row.provider_uuid, snapshot_hash=row.snapshot_hash)
    assert fiscal_projection(order) == f"simulated_fiscal_{expected}"
    # Original sale remains printable; no projection claims email/SMS delivery.
    assert Order.receipt_delivery_status.fget(order) == "simulated_local_print_available"


def test_legacy_and_missing_canonical_payment_keep_existing_fallback(order):
    order.shop_payment_confirmation = None
    assert fiscal_projection(order) is None
    assert Order.fiscal_status.fget(order) == "not_requested"
    assert Order.receipt_delivery_status.fget(order) == "pending_document"
    order.channel = None
    assert Order.fiscal_status.fget(order) == "legacy_unknown"


def test_return_fact_does_not_mutate_original_sale_projection_input(order):
    original = deepcopy(order.shop_fiscal_documents[0].snapshot)
    order.shop_financial_returns = [Row(amount_minor=5000)]
    assert fiscal_projection(order) == "simulated_fiscal_return_pending"
    assert order.shop_fiscal_documents[0].snapshot == original
