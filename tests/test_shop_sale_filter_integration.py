"""Sale membership uses real catalog SQL, promotion pricing and HTTP serialization."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.brand import Brand
from app.models.category import Category
from app.models.product import Product
from app.models.shop_promotion import ShopPromotion, ShopPromotionDiscountType, ShopPromotionTrigger
from app.services.shop_promotion import shop_promotion_service
from tests.test_catalog_assembly_http_integration import catalog, http, get
from tests.test_segments_integration import anyio_backend, database


@pytest.fixture
async def sale(http, database):
    async with database() as session:
        products = {p.id: p for p in (await session.execute(select(Product))).scalars()}
        now = datetime.now(UTC)
        for i, state, ids in [
            (302, 'active', [102, 104, 105, 108]),
            (303, 'expired', [103]),
            (304, 'future', [107]),
            (305, 'coupon', [106]),
            (306, 'disabled', [103]),
        ]:
            session.add(ShopPromotion(
                id=i, name=state, trigger=ShopPromotionTrigger.promocode if state == 'coupon' else ShopPromotionTrigger.automatic,
                code='SALEONLY' if state == 'coupon' else None,
                is_active=state != 'disabled',
                discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal('10'),
                starts_at=now + timedelta(days=1) if state == 'future' else None,
                ends_at=now - timedelta(days=1) if state == 'expired' else None,
                products=[products[pid] for pid in ids],
            ))
        # A retail comparison is not an actual public sale.
        products[103].recommended_retail_price = Decimal('999')
        await session.commit()
    return http


@pytest.mark.anyio
async def test_sale_pagination_visibility_and_serialized_prices(sale):
    first, _ = await get(sale, '/products?has_discount=true&limit=1')
    second, _ = await get(sale, '/products?has_discount=true&limit=1&offset=1')
    assert first['total'] == second['total'] == 2
    assert [first['items'][0]['id'], second['items'][0]['id']] == [101, 102]
    for item in first['items'] + second['items']:
        assert Decimal(item['price']) < Decimal(item['compare_at_price'])
        detail, _ = await get(sale, f"/products/{item['id']}")
        assert detail['price'] == item['price']
        assert detail['compare_at_price'] == item['compare_at_price']
    empty, _ = await get(sale, '/products?has_discount=true&offset=2')
    assert empty['total'] == 2 and empty['items'] == []


@pytest.mark.anyio
async def test_sale_excludes_future_expired_coupon_and_rrp(sale):
    data, _ = await get(sale, '/products?has_discount=false')
    assert {p['id'] for p in data['items']} == {103, 106, 107}
    assert all(p['compare_at_price'] is None for p in data['items'])
    default, _ = await get(sale, '/products')
    assert default['total'] == 5


@pytest.mark.anyio
async def test_sale_composes_with_category_brand_search_and_price(sale):
    for query in ['category_id=12&brand_id=31', 'category_slug=care&brand_slug=synthetic-brand', 'q=Alpha', 'priceMin=95&priceMax=105', 'price_min=95&price_max=105']:
        data, _ = await get(sale, '/products?has_discount=true&' + query)
        assert data['total'] == (2 if query.startswith('category') else 1)
    data, _ = await get(sale, '/products?has_discount=true&brand_id=999')
    assert data['total'] == 0


@pytest.mark.anyio
async def test_category_sale_facets_and_filters(sale):
    data, _ = await get(sale, '/categories/catalog/products?has_discount=true&brand=synthetic-brand&limit=1')
    assert data['total'] == 2 and data['items'][0]['id'] == 101
    facets, _ = await get(sale, '/categories/catalog/filters?has_discount=true')
    assert Decimal(facets['price']['min']) == Decimal('100')
    assert Decimal(facets['price']['max']) == Decimal('135')
    filtered, _ = await get(sale, '/categories/catalog/products?has_discount=true&priceMax=110')
    assert filtered['total'] == 1 and filtered['items'][0]['id'] == 101
    unavailable, _ = await get(sale, '/categories/catalog/products?has_discount=false')
    assert {p['id'] for p in unavailable['items']} == {103, 107}


@pytest.mark.anyio
@pytest.mark.parametrize('sort', ['newest', 'price_asc', 'price_desc'])
async def test_sale_availability_first(sale, sort):
    data, _ = await get(sale, '/products?has_discount=true&sort=' + sort)
    assert [p['id'] for p in data['items']] == [101, 102]


@pytest.mark.anyio
async def test_invalid_discount_rejected(sale):
    client, _ = sale
    for path in ['/products', '/categories/catalog/products', '/categories/catalog/filters']:
        assert (await client.get(path + '?has_discount=invalid')).status_code == 422

@pytest.mark.anyio
async def test_sale_price_range_and_pagination_validation(sale):
    client, _ = sale
    assert (await client.get('/products?has_discount=true&priceMin=110&priceMax=100')).status_code == 422
    data, _ = await get(sale, '/products?has_discount=true&page=2&page_size=1')
    assert data['total'] == 2 and data['items'][0]['id'] == 102
    data, _ = await get(sale, '/categories/catalog/products?has_discount=true&brand=absent')
    assert data['total'] == 0


@pytest.mark.anyio
@pytest.mark.parametrize('path', ['/products', '/categories/catalog/products'])
async def test_sale_and_non_sale_partition_the_unchanged_visible_catalog(sale, path):
    default, _ = await get(sale, path)
    discounted, _ = await get(sale, path + '?has_discount=true')
    regular, _ = await get(sale, path + '?has_discount=false')
    ids = lambda body: {item['id'] for item in body['items']}
    assert not ids(discounted) & ids(regular)
    assert ids(discounted) | ids(regular) == ids(default)
    assert discounted['total'] + regular['total'] == default['total']


@pytest.mark.anyio
@pytest.mark.parametrize('path', ['/products', '/categories/catalog/products'])
async def test_effective_price_alias_precedence_and_zero_bounds(sale, path):
    camel, _ = await get(sale, path + '?has_discount=true&priceMin=100&priceMax=100&price_min=135&price_max=135')
    assert camel['total'] == 1 and camel['items'][0]['id'] == 101
    zero, _ = await get(sale, path + '?has_discount=true&priceMin=0&price_min=999')
    assert zero['total'] == 2
    effective, _ = await get(sale, path + '?priceMax=110')
    assert effective['total'] == 1 and effective['items'][0]['id'] == 101
    client, _ = sale
    assert (await client.get(path + '?has_discount=false&priceMin=110&priceMax=100')).status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize('sort', ['newest', 'price_asc', 'price_desc'])
async def test_sale_stock_filter_and_reserved_stock_compose_before_pagination(sale, database, sort):
    for in_stock, expected in [('true', 101), ('false', 102)]:
        body, _ = await get(sale, f'/products?has_discount=true&in_stock={in_stock}&sort={sort}&limit=1')
        assert body['total'] == 1 and body['items'][0]['id'] == expected
        empty, _ = await get(sale, f'/products?has_discount=true&in_stock={in_stock}&sort={sort}&offset=1')
        assert empty['total'] == 1 and empty['items'] == []
    async with database() as session:
        alpha = await session.get(Product, 101)
        alpha.reserved_quantity = alpha.stock_quantity
        await session.commit()
    available, _ = await get(sale, '/products?has_discount=true&in_stock=true')
    assert available['total'] == 0 and available['items'] == []
    reserved, _ = await get(sale, '/products?has_discount=true&in_stock=false')
    assert reserved['total'] == 2
    assert all(not product['is_available_for_purchase'] for product in reserved['items'])


@pytest.mark.anyio
async def test_manual_shop_override_is_the_base_not_a_discount(sale, database):
    async with database() as session:
        alpha = await session.get(Product, 101)
        gamma = await session.get(Product, 103)
        alpha.shop_price = Decimal('160.00')
        gamma.shop_price = Decimal('80.00')
        await session.commit()
    discounted, _ = await get(sale, '/products?has_discount=true')
    alpha = next(item for item in discounted['items'] if item['id'] == 101)
    assert Decimal(alpha['price']) == Decimal('80.00')
    assert Decimal(alpha['compare_at_price']) == Decimal('160.00')
    regular, _ = await get(sale, '/products?has_discount=false')
    gamma = next(item for item in regular['items'] if item['id'] == 103)
    assert Decimal(gamma['price']) == Decimal('80.00')
    assert Decimal(gamma['base_price']) == Decimal('80.00')
    assert Decimal(gamma['recommended_retail_price']) == Decimal('999.00')
    assert gamma['compare_at_price'] is None


@pytest.mark.anyio
async def test_promotion_with_no_cent_rounded_saving_is_not_sale(sale, database):
    async with database() as session:
        gamma = await session.get(Product, 103)
        gamma.shop_price = Decimal('0.01')
        session.add(ShopPromotion(
            id=307, name='No rounded saving', trigger=ShopPromotionTrigger.automatic,
            discount_type=ShopPromotionDiscountType.percent, discount_value=Decimal('0.01'),
            products=[gamma],
        ))
        await session.commit()
    regular, _ = await get(sale, '/products?has_discount=false')
    gamma = next(item for item in regular['items'] if item['id'] == 103)
    assert Decimal(gamma['price']) == Decimal('0.01')
    assert gamma['compare_at_price'] is None
    discounted, _ = await get(sale, '/products?has_discount=true')
    assert {item['id'] for item in discounted['items']} == {101, 102}


@pytest.mark.anyio
@pytest.mark.parametrize(('scope', 'expected'), [
    ('ancestor', {101, 102, 103, 107}),
    ('parent_only', {101, 102}),
    ('brand', {101, 102, 103, 106, 107}),
])
async def test_sale_membership_uses_existing_automatic_promotion_scopes(sale, database, scope, expected):
    async with database() as session:
        parent = await session.get(Category, 11)
        brand = await session.get(Brand, 31)
        session.add(ShopPromotion(
            id=307, name='Scope regression', trigger=ShopPromotionTrigger.automatic,
            discount_type=ShopPromotionDiscountType.fixed_amount, discount_value=Decimal('1.00'),
            include_subcategories=scope != 'parent_only',
            categories=[] if scope == 'brand' else [parent],
            brands=[brand] if scope == 'brand' else [],
        ))
        await session.commit()
    body, _ = await get(sale, '/products?has_discount=true')
    assert {item['id'] for item in body['items']} == expected
    assert all(Decimal(item['price']) < Decimal(item['compare_at_price']) for item in body['items'])


@pytest.mark.anyio
async def test_sale_facet_counts_empty_ranges_and_base_subset_semantics(sale):
    facets, _ = await get(sale, '/categories/catalog/filters?has_discount=true')
    assert facets['filters']['brand']['values'] == [dict(slug='synthetic-brand', name='Synthetic brand', count=2)]
    assert {value['slug']: value['count'] for value in facets['filters']['color']['values']} == {'red': 1, 'blue': 1}
    unselected, _ = await get(sale, '/categories/catalog/filters?has_discount=true&Color=absent&priceMax=0&brand=absent')
    assert unselected == facets
    regular, _ = await get(sale, '/categories/catalog/filters?has_discount=false')
    assert Decimal(regular['price']['min']) == Decimal('120.00')
    assert Decimal(regular['price']['max']) == Decimal('180.00')
    assert regular['filters']['brand']['values'][0]['count'] == 2
    assert 'color' not in regular['filters']
    empty, _ = await get(sale, '/categories/empty/filters?has_discount=true')
    assert empty == dict(price=dict(min=None, max=None), filters={})


@pytest.mark.anyio
@pytest.mark.parametrize(('path', 'candidates'), [
    ('/products', {101, 102, 103, 106, 107}),
    ('/categories/catalog/products', {101, 102, 103, 107}),
])
async def test_sale_prices_the_candidate_set_once_before_slicing(sale, monkeypatch, path, candidates):
    original = shop_promotion_service.price_products
    calls = []

    async def price_once(session, products, **kwargs):
        calls.append({product.id for product in products})
        return await original(session, products, **kwargs)

    monkeypatch.setattr(shop_promotion_service, 'price_products', price_once)
    body, _ = await get(sale, path + '?has_discount=true&limit=1&offset=1')
    assert body['total'] == 2 and body['items'][0]['id'] == 102
    assert calls == [candidates]


@pytest.mark.anyio
async def test_sale_price_bounds_reject_invalid_numbers(sale):
    client, _ = sale
    for name in ['priceMin', 'priceMax', 'price_min', 'price_max']:
        for value in ['NaN', 'Infinity', '-1', '1000000001', '100.001']:
            response = await client.get('/products', params={'has_discount': 'true', name: value})
            assert response.status_code == 422
