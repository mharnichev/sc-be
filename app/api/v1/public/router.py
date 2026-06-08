from fastapi import APIRouter

from app.api.v1.routes import blog, bookings, brands, categories, customers, health, messaging, orders, products, reviews

router = APIRouter()
router.include_router(health.public_router, tags=["public:health"])
router.include_router(products.public_router, prefix="/products", tags=["public:products"])
router.include_router(categories.public_router, prefix="/categories", tags=["public:categories"])
router.include_router(brands.public_router, prefix="/brands", tags=["public:brands"])
router.include_router(orders.public_router, prefix="/orders", tags=["public:orders"])
router.include_router(customers.public_router, prefix="/customers", tags=["public:customers"])
router.include_router(bookings.public_router, tags=["public:booking"])
router.include_router(reviews.public_router, prefix="/reviews", tags=["public:reviews"])
router.include_router(messaging.public_router, tags=["public:messaging"])
router.include_router(blog.public_router, prefix="/blog", tags=["public:blog"])
