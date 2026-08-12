from fastapi import APIRouter

from app.api.v1.routes import (
    blog,
    booking_funnel,
    booking_alternatives,
    booking_recovery,
    bookings,
    brands,
    categories,
    customers,
    customer_activity,
    delivery,
    feedback,
    health,
    messaging,
    orders,
    products,
    reviews,
    repeat_booking,
    shop_promotions,
    waitlist,
    waitlist_offers,
)

router = APIRouter()
router.include_router(health.public_router, tags=["public:health"])
router.include_router(products.public_router, prefix="/products", tags=["public:products"])
router.include_router(categories.public_router, prefix="/categories", tags=["public:categories"])
router.include_router(brands.public_router, prefix="/brands", tags=["public:brands"])
router.include_router(orders.public_router, prefix="/orders", tags=["public:orders"])
router.include_router(
    shop_promotions.public_router,
    prefix="/shop-promotions",
    tags=["public:shop-promotions"],
)
router.include_router(delivery.public_router, prefix="/delivery", tags=["public:delivery"])
router.include_router(feedback.public_router, prefix="/feedback", tags=["public:feedback"])
router.include_router(customers.public_router, prefix="/customers", tags=["public:customers"])
router.include_router(customer_activity.public_router, tags=["public:customer-activity"])
router.include_router(bookings.public_router, tags=["public:booking"])
router.include_router(booking_funnel.public_router, tags=["public:booking-funnel"])
router.include_router(booking_alternatives.public_router, tags=["public:booking-recovery"])
router.include_router(booking_recovery.public_router, tags=["public:booking-recovery"])
router.include_router(waitlist.public_router, tags=["public:waitlist"])
router.include_router(waitlist_offers.public_router, tags=["public:waitlist"])
router.include_router(reviews.public_router, prefix="/reviews", tags=["public:reviews"])
router.include_router(repeat_booking.public_router, tags=["public:repeat-booking"])
router.include_router(messaging.public_router, tags=["public:messaging"])
router.include_router(blog.public_router, prefix="/blog", tags=["public:blog"])
