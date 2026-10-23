"""Stock priority is applied by the database before limiting a catalog page."""
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select

from app.services.catalog_assembly import product_order_clauses, product_stock_sort_key


@pytest.mark.parametrize('sort', [None, 'newest', 'name', 'price_asc', 'price_desc', 'popular'])
def test_stock_priority_before_pagination(sort):
    engine = create_engine('sqlite://')
    products = Table(
        'products', MetaData(),
        Column('id', Integer, primary_key=True), Column('stock_quantity', Integer),
        Column('reserved_quantity', Integer), Column('created_at', Integer),
        Column('name', Integer), Column('price', Integer), Column('shop_price', Integer),
        Column('is_top', Integer), Column('top_score', Integer),
    )
    products.metadata.create_all(engine)
    rows = [
        dict(id=1, stock_quantity=0, reserved_quantity=0),
        dict(id=2, stock_quantity=4, reserved_quantity=4),
        dict(id=3, stock_quantity=2, reserved_quantity=None),
        dict(id=4, stock_quantity=5, reserved_quantity=2),
        dict(id=5, stock_quantity=1, reserved_quantity=3),
    ]
    with engine.begin() as connection:
        connection.execute(products.insert(), [dict(row, created_at=row['id'], name=row['id'],
                           price=row['id'], is_top=1, top_score=row['id']) for row in rows])
        statement = select(products.c.id).order_by(*product_order_clauses(sort, None))
        ids = list(connection.execute(statement).scalars())
        assert set(ids[:2]) == {3, 4}
        assert set(ids[2:]) == {1, 2, 5}
        assert list(connection.execute(statement.limit(2)).scalars()) == ids[:2]
        assert list(connection.execute(statement.offset(2).limit(3)).scalars()) == ids[2:]
    assert {row['id'] for row in rows if not product_stock_sort_key(SimpleNamespace(**row))} == {3, 4}
    engine.dispose()
