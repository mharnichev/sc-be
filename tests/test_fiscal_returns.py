"""Exact allocation is independent from runtime, catalog and payment transports."""
from app.services.fiscal_returns import net_lines
from app.schemas.fiscal_returns import ReturnCreate, MockRefundCreate, MockReportCreate
from pydantic import ValidationError
import pytest


def state(amounts, discount):
    return {'items': [{'order_item_id': key, 'total_minor': value} for key, value in amounts],
        'additional_discount_minor': discount, 'amount_minor': sum(v for _, v in amounts) - discount}


def test_largest_remainder_ties_use_sorted_original_line_id():
    original = state([(8, 3), (2, 3), (5, 3)], 2)
    assert net_lines(original) == {2: 2, 5: 2, 8: 3}
    assert sum(net_lines(original).values()) == original['amount_minor']


def test_unequal_proportions_and_zero_minor_partial_are_exact():
    original = state([(1, 100), (2, 200), (3, 300)], 5)
    assert net_lines(original) == {1: 99, 2: 198, 3: 298}
    net, qty = 1, 3
    partials = [net * (previous + 1) // qty - net * previous // qty for previous in range(qty)]
    assert partials == [0, 0, 1] and sum(partials) == net


def test_money_cannot_be_submitted_and_reason_evidence_must_be_meaningful():
    with pytest.raises(ValidationError):
        ReturnCreate(parent_sale_id=1, items=[{'order_item_id': 1, 'quantity': 1}], reason='test', amount_minor=1)
    with pytest.raises(ValidationError):
        MockRefundCreate(evidence=' ')
    with pytest.raises(ValidationError):
        MockReportCreate(kind='z', expected_shift_revision=1, confirm=False)
