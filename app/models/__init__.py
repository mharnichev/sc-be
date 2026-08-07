from app.models.admin_user import AdminUser
from app.models.analytics import AnalyticsTrackingMarker
from app.models.blog import BlogSubscription, BlogSubscriptionEvent
from app.models.booking import (
    BarberService,
    BaseService,
    Booking,
    BookingServiceItem,
    Master,
    MasterAvailabilityWindow,
    MasterPosition,
    MasterTimeBlock,
)
from app.models.booking_funnel import BookingFunnelEvent, BookingFunnelWeeklyDigest
from app.models.booking_recovery import BookingRecoveryEvent
from app.models.brand import Brand
from app.models.category import Category
from app.models.customer import Customer
from app.models.customer_activity import CustomerActivityAccessToken
from app.models.customer_otp_code import CustomerOtpCode
from app.models.google_business_review_cache import GoogleBusinessReviewCache
from app.models.master_review import MasterReview, MasterReviewModerationAudit
from app.models.messaging import (
    Campaign,
    CampaignAudienceFilter,
    ChannelProviderConfig,
    ClientCommunicationPreference,
    MessageLog,
    MessageRecipient,
    MessageTemplate,
    ReviewFormOpenEvent,
    ReviewRequest,
    ReviewRequestEvent,
    TelegramBotSession,
    TelegramContact,
)
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.promotion import Promotion
from app.models.shop import (
    CustomerCartItem,
    CustomerWishlistItem,
    DeliveryCache,
    ProductImage,
    ProductReview,
    ProductReviewComment,
    ProductView,
)
from app.models.shop_promotion import ShopPromotion
from app.models.upload import Upload
from app.models.waitlist import WaitlistOffer, WaitlistRequest

all_models = [
    AdminUser,
    AnalyticsTrackingMarker,
    BlogSubscription,
    BlogSubscriptionEvent,
    Master,
    BaseService,
    BarberService,
    Booking,
    BookingServiceItem,
    MasterTimeBlock,
    MasterAvailabilityWindow,
    BookingFunnelEvent,
    BookingFunnelWeeklyDigest,
    BookingRecoveryEvent,
    Brand,
    Category,
    Customer,
    CustomerActivityAccessToken,
    CustomerOtpCode,
    GoogleBusinessReviewCache,
    MasterReview,
    MasterReviewModerationAudit,
    Campaign,
    MessageTemplate,
    MessageRecipient,
    MessageLog,
    ClientCommunicationPreference,
    TelegramContact,
    TelegramBotSession,
    ReviewFormOpenEvent,
    ReviewRequest,
    ReviewRequestEvent,
    ChannelProviderConfig,
    CampaignAudienceFilter,
    Product,
    ProductImage,
    CustomerCartItem,
    CustomerWishlistItem,
    ProductReview,
    ProductReviewComment,
    ProductView,
    DeliveryCache,
    ShopPromotion,
    Promotion,
    Order,
    OrderItem,
    Upload,
    WaitlistRequest,
    WaitlistOffer,
]
