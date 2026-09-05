from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.utils.catalog_taxonomy import resolve_import_category_path
from app.utils.cleanup_catalog import build_plan, cleanup


def fixture_data() -> dict:
    def category(cid, name, parent=None, active=True):
        return dict(id=cid, name=name, slug=f'old-{cid}', parent_id=parent, is_active=active)
    return {
        'categories': [
            category(10, 'КОСМЕТИКА'), category(20, 'ДЛЯ БОРОДИ', 10),
            category(30, 'БАЛЬЗАМ', 20), category(40, 'ПІСЛЯ ГОЛІННЯ', 10),
            category(50, 'ОДЕКОЛОН', 40), category(60, 'НА ПРОДАЖ'),
            category(70, 'ПІСЛЯ ГОЛІННЯ', 60), category(80, 'ОДЕКОЛОН', 70),
            category(90, 'ВІД ПОРІЗІВ', 70), category(100, 'БРЕНДИ'),
            category(110, 'Reuzel', 100, False), category(120, 'БАЛЬЗАМ', 40),
        ],
        'brands': [dict(id=9, name='Reuzel')],
        'products': [
            dict(id=1, sku='850031020764', name='Reuzel beard balm', category_id=110, is_active=True),
            dict(id=2, sku='other', name='Cologne', category_id=80, is_active=True),
            dict(id=3, sku='cuts', name='Styptic', category_id=90, is_active=False),
            dict(id=4, sku='unrelated', name='Aftershave balm', category_id=120, is_active=True),
        ],
        'shop_promotion_categories': [
            dict(promotion_id=1, category_id=110), dict(promotion_id=2, category_id=80),
            dict(promotion_id=2, category_id=50),
        ],
        'shop_promotion_products': [dict(promotion_id=1, product_id=1)],
    }


def test_plan_merges_only_equivalent_paths_and_preserves_hidden_brand_subset():
    plan = build_plan(fixture_data())
    assert plan['categories_after'] == 7
    assert plan['delete_category_ids'] == [60, 70, 80, 100, 110]
    assert plan['product_updates'][0]['changes'] == {'category_id': 30, 'is_active': False}
    assert plan['product_updates'][1]['changes'] == {'category_id': 50}
    assert len(plan['product_updates']) == 2
    assert plan['category_updates'] == [{
        'id': 90, 'changes': {'slug': 'kosmetika-pislia-golinnia-vid-poriziv', 'parent_id': 40},
    }]
    assert plan['category_links'] == [(2, 50)]
    assert plan['product_links'] == [(1, 1)]


def test_unknown_brand_product_aborts_plan():
    snapshot = fixture_data()
    snapshot['products'][0]['sku'] = 'unreviewed'
    with pytest.raises(ValueError, match='missing reviewed category'):
        build_plan(snapshot)


def test_cleanup_backup_dry_run_and_idempotence(tmp_path: Path):
    engine = sa.create_engine('sqlite://')
    metadata = sa.MetaData()
    categories = sa.Table('categories', metadata,
        sa.Column('id', sa.Integer, primary_key=True), sa.Column('name', sa.String),
        sa.Column('slug', sa.String, unique=True), sa.Column('parent_id', sa.Integer),
        sa.Column('is_active', sa.Boolean), sa.Column('updated_at', sa.DateTime),
    )
    products = sa.Table('products', metadata,
        sa.Column('id', sa.Integer, primary_key=True), sa.Column('name', sa.String),
        sa.Column('sku', sa.String), sa.Column('category_id', sa.Integer),
        sa.Column('is_active', sa.Boolean), sa.Column('updated_at', sa.DateTime),
    )
    sa.Table('brands', metadata, sa.Column('id', sa.Integer, primary_key=True), sa.Column('name', sa.String))
    for name, column in [('shop_promotion_categories', 'category_id'), ('shop_promotion_products', 'product_id')]:
        sa.Table(name, metadata, sa.Column('promotion_id', sa.Integer, primary_key=True),
                 sa.Column(column, sa.Integer, primary_key=True))
    metadata.create_all(engine)
    snapshot = fixture_data()
    with engine.begin() as connection:
        for name, rows in snapshot.items():
            connection.execute(metadata.tables[name].insert(), rows)
        plan = cleanup(connection)
        assert connection.scalar(sa.select(sa.func.count()).select_from(categories)) == 12
        backup = tmp_path / 'catalog.json'
        applied = cleanup(connection, backup=backup)
        assert plan == applied
        assert len(json.loads(backup.read_text())['categories']) == 12
        assert connection.scalar(sa.select(sa.func.count()).select_from(categories)) == 7
        assert connection.scalar(sa.select(sa.func.count()).select_from(products)) == 4
        assert connection.execute(sa.select(products.c.category_id, products.c.is_active).where(products.c.id == 1)).one() == (30, False)
        second = cleanup(connection)
        assert second['delete_category_ids'] == second['category_updates'] == second['product_updates'] == []
        with pytest.raises(FileExistsError):
            cleanup(connection, backup=backup)
    engine.dispose()


@pytest.mark.parametrize('path,sku,extras,expected', [
    ('БРЕНДИ/Reuzel', '850031020764', [], 'КОСМЕТИКА/ДЛЯ БОРОДИ/БАЛЬЗАМ'),
    ('Reuzel', '850031020764', [], 'КОСМЕТИКА/ДЛЯ БОРОДИ/БАЛЬЗАМ'),
    ('НА ПРОДАЖ/ПІСЛЯ ГОЛІННЯ/ОДЕКОЛОН', 'other', [], 'КОСМЕТИКА/ПІСЛЯ ГОЛІННЯ/ОДЕКОЛОН'),
    ('НА ПРОДАЖ/ГРЕБНІ ТА ЩІТКИ', 'other', [], 'РОБОЧЕ МІСЦЕ/ПРОФЕСІЙНІ ГРЕБЕНІ ТА ЩІТКИ'),
    ('БРЕНДИ/Reuzel', 'new', ['БРЕНДИ/Reuzel', 'КОСМЕТИКА', 'КОСМЕТИКА/ДЛЯ БОРОДИ'], 'КОСМЕТИКА/ДЛЯ БОРОДИ'),
])
def test_import_canonical_paths(path, sku, extras, expected):
    assert resolve_import_category_path(path, sku=sku, brand_name='Reuzel', extra_paths=extras) == expected


@pytest.mark.parametrize('extras', [[], ['КОСМЕТИКА'], ['КОСМЕТИКА/ДЛЯ БОРОДИ', 'КОСМЕТИКА/ДЛЯ ВОЛОССЯ']])
def test_import_refuses_to_guess_category(extras):
    with pytest.raises(ValueError, match='requires a reviewed'):
        resolve_import_category_path('БРЕНДИ/Reuzel', sku='new', brand_name='Reuzel', extra_paths=extras)
