"""Conservative price-ending suggestion, independent of promotions and RRP."""
from decimal import Decimal, InvalidOperation

MAX_PRICE = Decimal("99999999.99")


def recommend_shop_price(price: Decimal) -> Decimal | None:
    """Suggest the closest lower whole-UAH 9-ending, losing at most 2%.

    This is an optional merchandising policy, not a claim of optimal pricing.
    None means leave the entered price alone, including invalid/zero prices.
    """
    try:
        if not price.is_finite() or not Decimal("0") < price <= MAX_PRICE:
            return None
        if price != price.quantize(Decimal("0.01")):
            return None
        whole = int(price)
        candidate = Decimal(((whole + 1) // 10) * 10 - 1)
        if candidate <= 0 or candidate >= price or (price - candidate) * 100 > price * 2:
            return None
        return candidate.quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
