from fastapi import APIRouter

from app.api.v1.routes import auth, brands, categories, orders, products, uploads

router = APIRouter()
router.include_router(auth.backoffice_router, prefix="/auth", tags=["backoffice:auth"])
router.include_router(products.backoffice_router, prefix="/products", tags=["backoffice:products"])
router.include_router(categories.backoffice_router, prefix="/categories", tags=["backoffice:categories"])
router.include_router(brands.backoffice_router, prefix="/brands", tags=["backoffice:brands"])
router.include_router(orders.backoffice_router, prefix="/orders", tags=["backoffice:orders"])
router.include_router(uploads.backoffice_router, prefix="/uploads", tags=["backoffice:uploads"])
