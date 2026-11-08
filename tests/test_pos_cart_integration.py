"""Exercise the POS cart through the mounted private API and isolated PostgreSQL."""
import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.models.admin_user import AdminUser
from app.models.category import Category
from app.models.order import Order

from app.models.product import Product
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
from tests.test_master_mono_integration import receipt
from tests.test_pos_quick_http_integration import BASE, auth, client, database, mock_provider, product, sellers, template_database

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def cart(database, product):
    async with database() as session:
        other = Product(name="Second POS product", slug="pos-cart-other", price=Decimal("20"), stock_quantity=5)
        session.add(other)
        await session.flush()
        session.add(ShopPromotion(
            name="Synthetic cart code", trigger=ShopPromotionTrigger.promocode, code="CART25",
            discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal("25"),
            products=[other], is_active=True, usage_limit=1,
        ))
        await session.commit()
        return [{"product_id": product.id, "quantity": 2}, {"product_id": other.id, "quantity": 3}]


async def quote(client, items, *, actor=2, promo_code=None):
    response = await client.post(BASE + "/cart/quote", headers=auth(actor), json={"items": items, "promo_code": promo_code})
    assert response.status_code == 200, response.text
    return response.json()


def checkout_body(cart, quote_result, *, promo_code=None):
    return {
        "items": cart, "promo_code": promo_code, "intent_id": str(uuid4()),
        "quote_revision": quote_result["quote_revision"], "currency": "UAH",
    }


async def checkout(client, body, actor=2, key=None):
    return await client.post(BASE + "/checkouts", json=body, headers=auth(actor, **{"Idempotency-Key": key or str(uuid4())}))


@pytest.mark.parametrize("actor", [1, 2])
async def test_cart_quote_checkout_and_lost_response(client, database, cart, mock_provider, actor):
    priced = await quote(client, cart, actor=actor, promo_code=" cart25 ")
    assert (priced["subtotal_minor"], priced["discount_minor"], priced["amount_minor"]) == (26000, 1500, 24500)
    line = priced["items"][1]
    assert line["promotion_name"] == "Synthetic cart code" and line["discount_source"] == "promo_code"
    assert (line["subtotal_minor"], line["discount_minor"], line["amount_minor"]) == (6000, 1500, 4500)
    body = checkout_body(cart, priced, promo_code="CART25")
    first, retry = await asyncio.gather(checkout(client, body, actor), checkout(client, body, actor))
    assert sorted([first.status_code, retry.status_code]) == [200, 201], (first.text, retry.text)
    view = first.json()
    assert view["order_id"] == retry.json()["order_id"]
    assert view["employee"]["user_id"] == actor
    assert (view["subtotal_minor"], view["discount_minor"], view["amount_minor"]) == (26000, 1500, 24500)
    assert sorted(line["qty"] for line in mock_provider["basket"]) == [2, 3]
    assert mock_provider["post_calls"] == 1 and mock_provider["amount"] == 24500
    recovered = await client.get(BASE + "/checkouts/by-intent/" + body["intent_id"], headers=auth(actor))
    assert recovered.json()["checkout_id"] == view["checkout_id"]
    mock_provider.update(status="success", checks=[])
    refreshed = await client.post(
        BASE + "/checkouts/" + view["checkout_id"] + "/refresh",
        headers=auth(actor, **{"Idempotency-Key": str(uuid4())}),
        json={"expected_revision": view["revision"]},
    )
    assert refreshed.status_code == 200, refreshed.text
    paid = refreshed.json()
    assert paid["payment"]["status"] == "paid" and paid["payment"]["qr_payload"] is None
    assert paid["receipt"]["status"] == "pending" and not paid["actions"]["can_handover"]
    mock_provider["checks"] = [receipt()]
    refreshed = await client.post(
        BASE + "/checkouts/" + view["checkout_id"] + "/refresh",
        headers=auth(actor, **{"Idempotency-Key": str(uuid4())}),
        json={"expected_revision": paid["revision"]},
    )
    assert refreshed.status_code == 200 and refreshed.json()["receipt"]["status"] == "accepted"
    assert mock_provider["post_calls"] == 1


async def test_cart_stale_price_rolls_back_entire_order(client, database, cart, mock_provider):
    body = checkout_body(cart, await quote(client, cart))
    key = str(uuid4())
    async with database() as session:
        first = await session.get(Product, cart[0]["product_id"])
        second = await session.get(Product, cart[1]["product_id"])
        # The total is unchanged, but the fiscal line amounts change.
        first.price += Decimal("3")
        second.price -= Decimal("2")
        await session.commit()
    stale = await checkout(client, body, key=key)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "price_changed", stale.text
    current = stale.json()["detail"]["current_quote"]
    assert current["amount_minor"] == 26000 and current["quote_revision"] != body["quote_revision"]
    absent = await client.get(BASE + "/checkouts/by-intent/" + body["intent_id"], headers=auth())
    assert absent.status_code == 404 and mock_provider["post_calls"] == 0
    async with database() as session:
        for line in cart:
            assert (await session.get(Product, line["product_id"])).reserved_quantity == 0
    accepted = await checkout(client, {**body, "quote_revision": current["quote_revision"]}, key=key)
    assert accepted.status_code == 201 and mock_provider["post_calls"] == 1


async def test_cart_stock_contention_and_uncertain_dispatch(client, database, cart, mock_provider):
    async with database() as session:
        item = await session.get(Product, cart[0]["product_id"])
        item.stock_quantity = 2
        await session.commit()
    prices = await quote(client, cart)
    first, second = (checkout_body(cart, prices) for _ in range(2))
    mock_provider["create_error"] = 500
    responses = await asyncio.gather(checkout(client, first), checkout(client, second, actor=3))
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    winner = next(i for i, response in enumerate(responses) if response.status_code == 201)
    result = responses[winner].json()
    assert result["status"] == "requires_review" and result["payment"]["qr_payload"] is None
    actor, body = ((2, first), (3, second))[winner]
    retry = await checkout(client, body, actor=actor)
    assert retry.status_code == 200 and retry.json()["checkout_id"] == result["checkout_id"]
    resume = await client.post(
        BASE + "/checkouts/" + result["checkout_id"] + "/resume",
        headers=auth(actor, **{"Idempotency-Key": str(uuid4())}),
        json={"expected_revision": result["revision"]},
    )
    assert resume.status_code == 200 and resume.json()["status"] == "requires_review"
    assert mock_provider["post_calls"] == 1


async def test_promotion_repriced_and_revoked_between_quote_and_checkout(client, database, cart, mock_provider):
    body = checkout_body(cart, await quote(client, cart, promo_code="CART25"), promo_code="CART25")
    async with database() as session:
        promo = await session.scalar(select(ShopPromotion).where(ShopPromotion.code == "CART25"))
        promo.discount_value = Decimal("15")
        await session.commit()
    stale = await checkout(client, body)
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "price_changed"
    current = stale.json()["detail"]["current_quote"]
    assert current["discount_minor"] == 900 and mock_provider["post_calls"] == 0
    async with database() as session:
        promo = await session.scalar(select(ShopPromotion).where(ShopPromotion.code == "CART25"))
        promo.is_active = False
        await session.commit()
    denied = await checkout(client, {**body, "quote_revision": current["quote_revision"]})
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "promo_not_applicable"
    assert mock_provider["post_calls"] == 0


async def test_quote_validation_role_and_anonymous_code_limit(client, database, cart, mock_provider):
    for items in ([], [cart[0], cart[0]], [{**cart[0], "quantity": True}], [{**cart[0], "quantity": 101}]):
        invalid = await client.post(BASE + "/cart/quote", headers=auth(), json={"items": items})
        assert invalid.status_code == 422
    async with database() as session:
        promo = await session.scalar(select(ShopPromotion).where(ShopPromotion.code == "CART25"))
        promo.usage_limit_per_customer = 1
        seller = await session.get(AdminUser, 3)
        seller.can_accept_qr_payments = False
        await session.commit()
    rejected = await client.post(BASE + "/cart/quote", headers=auth(), json={"items": cart, "promo_code": "CART25"})
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "promo_not_applicable"
    body = checkout_body(cart, await quote(client, cart))
    denied_quote = await client.post(BASE + "/cart/quote", headers=auth(3), json={"items": cart})
    assert denied_quote.status_code == 403 and (await checkout(client, body, actor=3)).status_code == 403
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
    assert mock_provider["post_calls"] == 0


async def test_one_use_promotion_cannot_be_redeemed_twice(client, database, cart, mock_provider):
    from sqlalchemy.orm import selectinload

    async with database() as session:
        promo = await session.scalar(
            select(ShopPromotion).where(ShopPromotion.code == "CART25").options(selectinload(ShopPromotion.products))
        )
        promo.products.append(await session.get(Product, cart[0]["product_id"]))
        await session.commit()
    first_items, second_items = [cart[0]], [cart[1]]
    first = checkout_body(first_items, await quote(client, first_items, promo_code="CART25"), promo_code="CART25")
    second = checkout_body(second_items, await quote(client, second_items, actor=3, promo_code="CART25"), promo_code="CART25")
    responses = await asyncio.gather(checkout(client, first), checkout(client, second, actor=3))
    assert sorted(response.status_code for response in responses) == [201, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["detail"]["code"] == "promo_not_applicable"
    assert mock_provider["post_calls"] == 1

@pytest.mark.parametrize("change", ["promotion", "category"])
async def test_checkout_reprices_automatic_promo_after_waiting_for_code(client, database, cart, mock_provider, change):
    async with database() as session:
        first_product = await session.get(Product, cart[0]["product_id"])
        scope = {"products": [first_product]}
        category_id = None
        if change == "category":
            ancestor = Category(name="Promo ancestor", slug="promo-ancestor")
            session.add(ancestor)
            await session.flush()
            child = Category(name="Promo child", slug="promo-child", parent_id=ancestor.id)
            session.add(child)
            await session.flush()
            category_id = child.id
            first_product.category_id = category_id
            scope = {"categories": [ancestor], "include_subcategories": True}
        automatic = ShopPromotion(
            name="Synthetic automatic", trigger=ShopPromotionTrigger.automatic,
            discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal("20"),
            is_active=True, **scope,
        )
        session.add(automatic)
        await session.flush()
        automatic_id = automatic.id
        await session.commit()

    body = checkout_body(cart, await quote(client, cart, promo_code="CART25"), promo_code="CART25")
    async with database() as holder:
        await holder.scalar(select(ShopPromotion).where(ShopPromotion.code == "CART25").with_for_update())
        async with asyncio.TaskGroup() as group:
            pending = group.create_task(checkout(client, body))
            try:
                async with database() as observer, asyncio.timeout(10):
                    while not await observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event_type='Lock')"
                    )):
                        # pg_stat_activity snapshots are transaction-scoped.
                        await observer.rollback()
                        await asyncio.sleep(0.02)
                if change == "promotion":
                    automatic = await holder.get(ShopPromotion, automatic_id)
                    automatic.discount_value = Decimal("10")
                else:
                    category = await holder.get(Category, category_id)
                    category.parent_id = None
                await holder.commit()
            finally:
                await holder.rollback()
    response = pending.result()
    assert response.status_code == 409 and response.json()["detail"]["code"] == "price_changed", response.text
    expected_discount = 3500 if change == "promotion" else 1500
    assert response.json()["detail"]["current_quote"]["discount_minor"] == expected_discount
    assert mock_provider["post_calls"] == 0


async def test_quote_rejects_unsupported_fiscal_name(client, database, cart, mock_provider):
    async with database() as session:
        product = await session.get(Product, cart[0]["product_id"])
        product.name = "x" * 129
        await session.commit()
    response = await client.post(BASE + "/cart/quote", headers=auth(), json={"items": cart})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "master_mono_basket_invalid"
    assert mock_provider["post_calls"] == 0


async def test_legacy_prepare_cannot_pin_cart_checkout_price(client, database, cart, mock_provider):
    from app.schemas.order import POSOrderCreate
    from app.services import local_sales
    from app.services.order_documents import digest

    body = checkout_body(cart, await quote(client, cart))
    async with database() as session:
        for line in cart:
            product = await session.get(Product, line["product_id"])
            product.stock_quantity = 10
        await session.commit()
        actor = await session.get(AdminUser, 2)
        await local_sales.prepare(
            session, actor, POSOrderCreate(items=cart),
            digest(["quick-pos-order", body["intent_id"]]),
        )
        product = await session.get(Product, cart[0]["product_id"])
        product.price += Decimal("5")
        await session.commit()
    response = await checkout(client, body)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "price_changed", response.text
    assert response.json()["detail"]["current_quote"]["amount_minor"] == 27000
    assert mock_provider["post_calls"] == 0


async def test_choose_eligible_automatic_promotion_without_exposing_codes(client, database, cart, mock_provider):
    async with database() as session:
        first, second = [await session.get(Product, line["product_id"]) for line in cart]

        def automatic(name, percent, product, *, active=True):
            return ShopPromotion(
                name=name, trigger=ShopPromotionTrigger.automatic,
                discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal(percent),
                products=[product], is_active=active,
            )

        best = automatic("Best for first", "20", first)
        selected = automatic("Selected for first", "10", first)
        unaffected = automatic("Best for second", "10", second)
        inactive = automatic("Inactive", "50", first, active=False)
        session.add_all([best, selected, unaffected, inactive])
        await session.flush()
        eligible_ids = {best.id, selected.id, unaffected.id}
        selected_id = selected.id
        code_id = await session.scalar(select(ShopPromotion.id).where(ShopPromotion.code == "CART25"))
        await session.commit()

    default = await quote(client, cart)
    assert default["amount_minor"] == 21400
    options = default["available_promotions"]
    assert {option["id"] for option in options} == eligible_ids
    assert all(set(option) == {"id", "name", "product_ids"} for option in options)
    assert next(option for option in options if option["id"] == selected_id)["product_ids"] == [cart[0]["product_id"]]
    for promotion_id, promo_code, status in ((code_id, None, 409), (selected_id, "CART25", 422)):
        response = await client.post(BASE + "/cart/quote", headers=auth(), json={
            "items": cart, "promotion_id": promotion_id, "promo_code": promo_code,
        })
        assert response.status_code == status, response.text

    response = await client.post(BASE + "/cart/quote", headers=auth(), json={"items": cart, "promotion_id": selected_id})
    assert response.status_code == 200, response.text
    chosen = response.json()
    assert chosen["promotion_id"] == selected_id and chosen["amount_minor"] == 23400
    assert [line["unit_minor"] for line in chosen["items"]] == [9000, 1800]
    assert [line["discount_source"] for line in chosen["items"]] == ["selected_promotion", "automatic"]
    assert [line["discount_minor"] for line in chosen["items"]] == [2000, 600]
    body = {**checkout_body(cart, chosen), "promotion_id": selected_id}
    stale = await checkout(client, {**body, "quote_revision": default["quote_revision"]})
    assert stale.status_code == 409 and stale.json()["detail"]["current_quote"]["promotion_id"] == selected_id
    assert mock_provider["post_calls"] == 0
    accepted = await checkout(client, body)
    assert accepted.status_code == 201 and accepted.json()["discount_minor"] == 2600
    assert mock_provider["amount"] == 23400 and mock_provider["post_calls"] == 1


async def test_selected_automatic_promotion_removed_after_quote(client, database, cart, mock_provider):
    async with database() as session:
        product = await session.get(Product, cart[0]["product_id"])
        promo = ShopPromotion(
            name="Removed before checkout", trigger=ShopPromotionTrigger.automatic,
            discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal("10"),
            products=[product], is_active=True,
        )
        session.add(promo)
        await session.flush()
        promotion_id = promo.id
        await session.commit()
    response = await client.post(BASE + "/cart/quote", headers=auth(), json={"items": cart, "promotion_id": promotion_id})
    assert response.status_code == 200, response.text
    async with database() as session:
        promo = await session.get(ShopPromotion, promotion_id)
        promo.is_active = False
        await session.commit()
    denied = await checkout(client, {**checkout_body(cart, response.json()), "promotion_id": promotion_id})
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "promotion_not_applicable"
    assert mock_provider["post_calls"] == 0
