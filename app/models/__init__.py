from app.models.admin_user import AdminUser
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
from app.models.brand import Brand
from app.models.category import Category
from app.models.customer import Customer
from app.models.customer_otp_code import CustomerOtpCode
from app.models.google_business_review_cache import GoogleBusinessReviewCache
from app.models.messaging import (
    Campaign,
    CampaignAudienceFilter,
    ChannelProviderConfig,
    ClientCommunicationPreference,
    MessageLog,
    MessageRecipient,
    MessageTemplate,
    ReviewRequest,
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

all_models = [
    AdminUser,
    BlogSubscription,
    BlogSubscriptionEvent,
    Master,
    BaseService,
    BarberService,
    Booking,
    BookingServiceItem,
    MasterTimeBlock,
    MasterAvailabilityWindow,
    Brand,
    Category,
    Customer,
    CustomerOtpCode,
    GoogleBusinessReviewCache,
    Campaign,
    MessageTemplate,
    MessageRecipient,
    MessageLog,
    ClientCommunicationPreference,
    TelegramContact,
    TelegramBotSession,
    ReviewRequest,
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
]
