"""Exact allocation to immutable original bank baskets after carrier inspection."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.shop_mono_checkbox_refunds import carrier_items


def test_carrier_quantities_use_original_price_buckets_and_exclude_unreceived_units():
    order = SimpleNamespace(items=[SimpleNamespace(id=91, product_id=42, quantity=3),
                                   SimpleNamespace(id=92, product_id=43, quantity=1)])
    basket = [
        {"code": "42", "qty": 2, "sum": 333, "total": 666},
        {"code": "42", "qty": 1, "sum": 334, "total": 334},
        {"code": "43", "qty": 1, "sum": 200, "total": 200},
    ]
    assert carrier_items(order, basket, {"quantities": {"91": 1}}) == [
        {"code": "42", "qty": 1, "sum": 333, "total": 333},
    ]
    assert carrier_items(order, basket, {"quantities": {"91": 3, "92": 1}}) == basket
    assert basket[0]["qty"] == 2
    for quantities in ({"91": 4}, {"100": 1}):
        with pytest.raises(HTTPException) as error:
            carrier_items(order, basket, {"quantities": quantities})
        assert error.value.detail["code"] == "direct_refund_basket_invalid"
