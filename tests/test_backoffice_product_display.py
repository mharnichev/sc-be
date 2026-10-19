from datetime import UTC,datetime
from decimal import Decimal
import pytest
from app.api.v1.routes.products import _backoffice_product_response
from app.api.v1.routes.inventory import _inventory_product_response
from app.models.product import Product
from app.models.shop import ProductImage,ProductImageVariant
from app.services.catalog_visibility import CatalogVisibility
NOW=datetime(2026,10,19,tzinfo=UTC)

def card():
    return Product(id=1,name='Synthetic',slug='synthetic',price=Decimal('10'),stock_quantity=1,reserved_quantity=0,allow_backorder=False,is_active=True,barcode=None,image_url='/original.webp',attributes_json={'image_urls':['/original.webp']},images=[],image_variants=[],created_at=NOW,updated_at=NOW)

def variant(source='/original.webp',**kw):
    args=dict(id=1,source_url=source,output_url='/processed.webp',status='succeeded',is_preferred=True)
    args.update(kw);return ProductImageVariant(**args)

@pytest.mark.parametrize('variants,expected',[( [],['/original.webp']),([variant()],['/processed.webp']),([variant(status='failed')],['/original.webp']),([variant(status='processing')],['/original.webp']),([variant(is_preferred=False)],['/original.webp']),([variant(output_url=None)],['/original.webp']),([variant(source='/unrelated.webp')],['/original.webp'])])
def test_display_requires_successful_preferred_variant_without_rewriting_originals(variants,expected):
    p=card();p.image_variants=variants
    for result in (_backoffice_product_response(p,CatalogVisibility.from_categories([])),_inventory_product_response(p)):
        assert result.image_url=='/original.webp'
        assert result.display_image_url==expected[0] and result.display_image_urls==expected
    assert p.image_url=='/original.webp' and p.attributes_json=={'image_urls':['/original.webp']}

def test_gallery_order_active_filter_and_original_management_urls_preserved():
    p=card();p.images=[ProductImage(id=2,product_id=1,image_url='/b.webp',sort_order=2,is_active=True,created_at=NOW,updated_at=NOW),ProductImage(id=1,product_id=1,image_url='/a.webp',sort_order=1,is_active=True,created_at=NOW,updated_at=NOW),ProductImage(id=3,product_id=1,image_url='/hidden.webp',sort_order=0,is_active=False,created_at=NOW,updated_at=NOW)]
    p.image_variants=[variant(source='/a.webp',output_url='/a-processed.webp')]
    r=_backoffice_product_response(p,CatalogVisibility.from_categories([]));assert r.display_image_urls==['/a-processed.webp','/b.webp'];assert r.display_image_url=='/a-processed.webp'
    assert [i.image_url for i in r.images]==['/hidden.webp','/a.webp','/b.webp']
    for image in p.images:image.is_active=False
    r=_backoffice_product_response(p,CatalogVisibility.from_categories([]));assert r.display_image_urls==[] and r.display_image_url is None
