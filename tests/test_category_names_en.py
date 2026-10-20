from datetime import UTC, datetime
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.v1.routes.categories import _category_tree, _backoffice_category_response
from app.models.category import Category
from app.schemas.category import CategoryCreate, CategoryUpdate, CategoryResponse
from app.services.catalog_assembly import _category_path
from app.services.catalog_visibility import CatalogVisibility


@pytest.mark.parametrize('schema', [CategoryCreate, CategoryUpdate])
@pytest.mark.parametrize('name_en', ['x', 'x' * 256])
def test_category_english_name_write_limits(schema, name_en):
    with pytest.raises(ValidationError):
        schema(name='Догляд', slug='care', name_en=name_en)


def test_category_english_name_is_optional_and_can_be_set_or_cleared():
    assert CategoryCreate(name='Догляд', slug='care').name_en is None
    assert CategoryUpdate(name_en='Care').model_dump(exclude_unset=True) == {'name_en': 'Care'}
    assert CategoryUpdate(name_en=None).model_dump(exclude_unset=True) == {'name_en': None}
    assert CategoryUpdate().model_dump(exclude_unset=True) == {}


def test_tree_responses_and_product_breadcrumbs_preserve_both_names_and_hierarchy():
    now = datetime.now(UTC)
    categories = [Category(id=1, name='Догляд', name_en='Care', slug='care', is_active=True,
                           parent_id=None, created_at=now, updated_at=now),
                  Category(id=2, name='Волосся', name_en='Hair', slug='hair', is_active=True,
                           parent_id=1, created_at=now, updated_at=now)]
    tree = _category_tree(categories, {2})
    assert (tree[0].id, tree[0].name, tree[0].name_en) == (1, 'Догляд', 'Care')
    assert (tree[0].children[0].parent_id, tree[0].children[0].name_en) == (1, 'Hair')
    assert CategoryResponse.model_validate(categories[1]).name_en == 'Hair'
    visibility = CatalogVisibility.from_categories(categories)
    assert _backoffice_category_response(categories[1], visibility).name_en == 'Hair'
    path = _category_path(2, {category.id: category for category in categories})
    assert [(item.id, item.name, item.name_en, item.slug) for item in path] == [
        (1, 'Догляд', 'Care', 'care'), (2, 'Волосся', 'Hair', 'hair')]


def test_category_name_migration_is_nullable_and_reversible(monkeypatch):
    path = Path(__file__).parents[1] / 'alembic/versions/0136_category_names_en.py'
    spec = importlib.util.spec_from_file_location('category_names_en_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    added, removed = [], []
    monkeypatch.setattr(migration.op, 'add_column', lambda table, column: added.append((table, column)))
    monkeypatch.setattr(migration.op, 'drop_column', lambda table, column: removed.append((table, column)))
    migration.upgrade()
    assert migration.down_revision == '0135_product_names_en'
    assert [(table, column.name, column.type.length, column.nullable) for table, column in added] == [('categories', 'name_en', 255, True)]
    migration.downgrade()
    assert removed == [('categories', 'name_en')]


@pytest.mark.parametrize('backoffice', [False, True])
def test_category_list_search_matches_english_name_without_changing_visibility(monkeypatch, backoffice):
    import asyncio
    import sqlite3
    from sqlalchemy.dialects import sqlite
    from app.api.v1.routes import categories as routes
    from app.dependencies.common import PaginationParams

    categories = [Category(id=1, name='Догляд', name_en='Hair care', is_active=True),
                  Category(id=2, name='Прихований', name_en='Hair care', is_active=False)]
    visibility = CatalogVisibility.from_categories(categories)
    database = sqlite3.connect(':memory:')
    database.execute('CREATE TABLE categories (id INTEGER, name TEXT, name_en TEXT, is_active BOOLEAN)')
    database.executemany('INSERT INTO categories VALUES (?, ?, ?, ?)', [(c.id, c.name, c.name_en, c.is_active) for c in categories])
    selected = []

    async def load(_):
        return visibility

    async def list_rows(_, *, stmt, page, page_size):
        query = stmt.with_only_columns(Category.id)
        sql = str(query.compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
        selected.extend(row[0] for row in database.execute(sql))
        return [], len(selected)

    monkeypatch.setattr(routes.CatalogVisibility, 'load', load)
    monkeypatch.setattr(routes.repo, 'list', list_rows)
    try:
        if backoffice:
            result = asyncio.run(routes.backoffice_list_categories(PaginationParams(), is_active=None, parent_id=None, search='hair', session=object()))
        else:
            result = asyncio.run(routes.list_categories(PaginationParams(), search='hair', session=object()))
        assert selected == ([1, 2] if backoffice else [1])
        assert result.total == len(selected)
    finally:
        database.close()
