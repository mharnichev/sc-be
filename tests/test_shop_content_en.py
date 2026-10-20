from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.order import OrderItem
from app.models.product import Product
from app.schemas.product import ProductCreate, ProductUpdate, PublicProductResponse
from app.schemas.category import CategoryCreate, CategoryUpdate
from app.schemas.order import OrderItemResponse, CustomerOrderItemResponse


@pytest.mark.parametrize('schema', [ProductCreate, ProductUpdate])
def test_english_html_descriptions_follow_existing_safety_policy(schema):
    value = '<p onclick="bad()">Safe <strong>text</strong><script>alert(1)</script><a href="javascript:bad()">link</a></p>'
    result = schema(name='Товар', slug='item', price=100, description='Оригінал', description_en=value, short_description_en=value, ingredients_en='Aqua, Glycerin.')
    assert result.description == 'Оригінал'
    assert '<strong>text</strong>' in result.description_en
    for field in ['description_en', 'short_description_en']:
        assert 'onclick' not in getattr(result, field)
        assert 'script' not in getattr(result, field)
        assert 'javascript:' not in getattr(result, field)
    assert result.ingredients_en == 'Aqua, Glycerin.'


def test_optional_content_updates_distinguish_unset_and_clear():
    assert ProductUpdate().model_dump(exclude_unset=True) == {}
    assert ProductUpdate(description_en=None, short_description_en=None, ingredients_en=None).model_dump(exclude_unset=True) == dict(description_en=None, short_description_en=None, ingredients_en=None)
    assert CategoryCreate(name='Догляд', slug='care', description_en='Care').description_en == 'Care'
    assert CategoryUpdate().model_dump(exclude_unset=True) == {}
    assert CategoryUpdate(description_en=None).model_dump(exclude_unset=True) == {'description_en': None}


def test_public_response_sanitizes_stored_english_html_without_mutating_orm():
    values = {name: None for name, field in PublicProductResponse.model_fields.items() if field.is_required()}
    values.update(id=1, name='Товар', slug='item', price=100, is_active=True,
                  created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                  description_en='<p>Good</p><script>bad()</script>', short_description_en='<img src=x onerror=bad()>Short', ingredients_en='Aqua')
    response = PublicProductResponse.model_validate(values)
    assert response.description_en == '<p>Good</p>'
    assert response.short_description_en == 'Short'
    assert values['description_en'].endswith('</script>')


@pytest.mark.parametrize('schema', [OrderItemResponse, CustomerOrderItemResponse])
def test_order_translation_uses_only_loaded_catalog_fields_preserving_snapshot(schema):
    item = OrderItem(id=1, product_id=2, quantity=1, price=Decimal('10'), discount_amount=Decimal('0'), product_name='Історична назва')
    assert schema.model_validate(item).product_name_en is None
    product = Product(id=2, name='Поточна назва', name_en='Current English name')
    item.product = product
    response = schema.model_validate(item)
    assert response.product_name_en == 'Current English name'
    assert response.product_name == item.product_name == 'Історична назва'
    # Expired translation fields and unloaded relationships must not trigger SQL.
    product.__dict__.pop('name_en')
    assert schema.model_validate(item).product_name_en is None
    item.__dict__.pop('product')
    assert schema.model_validate(item).product_name_en is None
    item.product = None
    assert schema.model_validate(item).product_name_en is None
    assert 'product_name_en' not in OrderItem.__table__.columns


def test_shop_content_migration_adds_only_nullable_catalog_text_and_reverses(monkeypatch):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / 'alembic/versions/0138_shop_content_en.py'
    spec = importlib.util.spec_from_file_location('shop_content_en_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    added, removed = [], []
    monkeypatch.setattr(migration.op, 'add_column', lambda table, column: added.append((table, column)))
    monkeypatch.setattr(migration.op, 'drop_column', lambda table, column: removed.append((table, column)))
    migration.upgrade()
    assert migration.down_revision == '0137_shop_online_payment'
    assert [(table, column.name, column.nullable) for table, column in added] == [
        ('products', 'description_en', True), ('products', 'short_description_en', True),
        ('products', 'ingredients_en', True), ('categories', 'description_en', True)]
    migration.downgrade()
    assert removed == [(table, column.name) for table, column in reversed(added)]
