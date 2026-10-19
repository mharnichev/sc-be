from fastapi import APIRouter
from app.api.v1.routes import shop_checkbox
from app.api.v1.routes import shop_payments
from app.api.v1.routes import shop_np_returns, shop_payment_resolutions

from app.api.v1.routes import (
    auth,
    blog,
    booking_recovery,
    bookings,
    brands,
    categories,
    customers,
    inventory,
    messaging,
    segments,
    campaign_runs,
    campaign_analytics,
    sms_queue,
    orders,
    products,
    promotions,
    reviews,
    repeat_booking,
    shop_promotions,
    statistics,
    uploads,
)

router = APIRouter()
router.include_router(shop_payment_resolutions.backoffice_router,tags=["backoffice:shop-payment-resolutions"])
router.include_router(shop_np_returns.backoffice_router,tags=["backoffice:shop-np-returns"])
router.include_router(shop_payments.backoffice_router, tags=["backoffice:shop-payments"])
router.include_router(shop_checkbox.backoffice_router, tags=["backoffice:shop-checkbox"])
router.include_router(auth.backoffice_router, prefix="/auth", tags=["backoffice:auth"])
router.include_router(products.backoffice_router, prefix="/products", tags=["backoffice:products"])
router.include_router(promotions.backoffice_router, prefix="/promotions", tags=["backoffice:promotions"])
router.include_router(
    shop_promotions.backoffice_router,
    prefix="/shop-promotions",
    tags=["backoffice:shop-promotions"],
)
router.include_router(categories.backoffice_router, prefix="/categories", tags=["backoffice:categories"])
router.include_router(brands.backoffice_router, prefix="/brands", tags=["backoffice:brands"])
router.include_router(customers.backoffice_router, prefix="/customers", tags=["backoffice:customers"])
router.include_router(inventory.backoffice_router, prefix="/inventory", tags=["backoffice:inventory"])
router.include_router(messaging.backoffice_router, prefix="/messaging", tags=["backoffice:messaging"])
router.include_router(orders.backoffice_router, prefix="/orders", tags=["backoffice:orders"])
router.include_router(uploads.backoffice_router, prefix="/uploads", tags=["backoffice:uploads"])
router.include_router(bookings.backoffice_router, tags=["backoffice:booking"])
router.include_router(reviews.backoffice_router, prefix="/reviews", tags=["backoffice:reviews"])
router.include_router(statistics.backoffice_router, tags=["backoffice:statistics"])
router.include_router(booking_recovery.backoffice_router, tags=["backoffice:booking-recovery"])
router.include_router(repeat_booking.backoffice_router, tags=["backoffice:repeat-booking"])
router.include_router(blog.backoffice_router, prefix="/blog", tags=["backoffice:blog"])

router.include_router(segments.backoffice_router, prefix="/segments", tags=["backoffice:segments"])
router.include_router(campaign_runs.backoffice_router, prefix="/messaging", tags=["backoffice:campaign-runs"])

router.include_router(sms_queue.backoffice_router, prefix="/messaging", tags=["backoffice:sms-queue"])

router.include_router(campaign_analytics.backoffice_router, prefix="/messaging", tags=["backoffice:campaign-analytics"])

from app.api.v1.routes import fiscal
router.include_router(fiscal.backoffice_router, tags=['backoffice:fiscal'])

from app.api.v1.routes import fiscal_returns
router.include_router(fiscal_returns.backoffice_router, tags=['mock:fiscal-returns'])

from app.api.v1.routes import fiscal_automation
router.include_router(fiscal_automation.backoffice_router, tags=['mock:fiscal-automation'])

from app.api.v1.routes import qr_payment
router.include_router(qr_payment.backoffice_router, tags=['mock:monobank-qr'])

from app.api.v1.routes import shop_sales
router.include_router(shop_sales.backoffice_router, tags=["backoffice:shop-operations"])

from app.api.v1.routes import shop_fiscal
router.include_router(shop_fiscal.backoffice_router, tags=["backoffice:shop-fiscal-test"])

from app.api.v1.routes import shop_carrier_returns
router.include_router(shop_carrier_returns.backoffice_router, tags=["backoffice:shop-carrier-return-test"])

from app.api.v1.routes.shop_operations_watch import backoffice_router as shop_operations_watch_router
router.include_router(shop_operations_watch_router, tags=["Shop operations watch"])
