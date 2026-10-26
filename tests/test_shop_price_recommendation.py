from decimal import Decimal

import pytest

from app.utils.shop_price_recommendation import recommend_shop_price


@pytest.mark.parametrize("value,expected", [
    ("500", "499.00"), ("555", "549.00"), ("544", "539.00"),
    ("1000", "999.00"), ("1097", "1089.00"), ("499.99", "499.00"),
    ("499", None), ("115", None), ("145", None), ("0", None),
    ("-1", None), ("9", None), ("10", None), ("1.001", None),
    ("NaN", None), ("Infinity", None), ("100000000", None),
    ("50", "49.00"), ("50.01", None), ("99999999.99", "99999999.00"),
])
def test_recommendation(value, expected):
    actual = recommend_shop_price(Decimal(value))
    assert actual == (Decimal(expected) if expected else None)


def test_recommendations_never_increase_and_stay_inside_budget():
    for cents in range(1, 200001, 7):
        price = Decimal(cents) / 100
        suggested = recommend_shop_price(price)
        if suggested is None:
            continue
        assert 0 < suggested < price
        assert suggested % 10 == 9
        assert (price - suggested) * 100 <= price * 2
        assert recommend_shop_price(suggested) is None
