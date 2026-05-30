from fastapi import APIRouter

from app.api.v1.routes import auth, bookings, brands, categories, customers, messaging, orders, products, reviews, statistics, uploads

router = APIRouter()
router.include_router(auth.backoffice_router, prefix="/auth", tags=["backoffice:auth"])
router.include_router(products.backoffice_router, prefix="/products", tags=["backoffice:products"])
router.include_router(categories.backoffice_router, prefix="/categories", tags=["backoffice:categories"])
router.include_router(brands.backoffice_router, prefix="/brands", tags=["backoffice:brands"])
router.include_router(customers.backoffice_router, prefix="/customers", tags=["backoffice:customers"])
router.include_router(messaging.backoffice_router, prefix="/messaging", tags=["backoffice:messaging"])
router.include_router(orders.backoffice_router, prefix="/orders", tags=["backoffice:orders"])
router.include_router(uploads.backoffice_router, prefix="/uploads", tags=["backoffice:uploads"])
router.include_router(bookings.backoffice_router, tags=["backoffice:booking"])
router.include_router(reviews.backoffice_router, prefix="/reviews", tags=["backoffice:reviews"])
router.include_router(statistics.backoffice_router, tags=["backoffice:statistics"])
