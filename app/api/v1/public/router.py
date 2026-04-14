from fastapi import APIRouter

from app.api.v1.routes import brands, categories, health, orders, products

router = APIRouter()
router.include_router(health.public_router, tags=["public:health"])
router.include_router(products.public_router, prefix="/products", tags=["public:products"])
router.include_router(categories.public_router, prefix="/categories", tags=["public:categories"])
router.include_router(brands.public_router, prefix="/brands", tags=["public:brands"])
router.include_router(orders.public_router, prefix="/orders", tags=["public:orders"])
