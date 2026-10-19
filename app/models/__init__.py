from app.models.nova_poshta import NovaPoshtaSettlement, NovaPoshtaBranch
from app.models.shop_ingress_limits import ShopIngressAdmission
from app.models.shop_payments import ShopPaymentIntent, ShopPaymentEvidence
from app.models.shop_np_returns import (ShopNPReturnCase, ShopNPReturnObservation, ShopNPReturnReceipt,
    ShopNPUnpaidClosure, ShopNPReturnAudit)
from app.models.shop_carrier_returns import (ShopCarrierReturn, ShopCarrierReturnAcceptance,
    ShopCarrierReturnReceipt, ShopCarrierUnpaidClosure)
from app.models.shop_fiscal import (ShopFiscalRegister, ShopFiscalPreview, ShopFiscalDocument,
    ShopFiscalAcceptance, ShopFinancialReturn, ShopRefundFact, ShopReturnReceipt, ShopFiscalReport, ShopFiscalAudit)
from app.models.campaign_offer import CampaignOffer, CampaignOfferOpen
from app.models.sms_queue import SmsQueueJob, SmsAccountThrottle
from app.models.segment import CustomerSegment
from app.models.campaign_run import CampaignRun
from app.models.admin_user import AdminUser
from app.models.analytics import AnalyticsTrackingMarker
from app.models.blog import BlogBrevoSyncJob, BlogSubscription, BlogSubscriptionEvent
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
from app.models.telegram_booking_funnel import TelegramBookingFunnelEvent
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
    MasterMessageDelivery,
    MasterScheduleReminder,
    ReviewFormOpenEvent,
    ReviewRequest,
    ReviewRequestEvent,
    TelegramBotSession,
    TelegramContact,
    TelegramDeliveryAttempt,
    TelegramWebhookEvent,
)
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.inventory import (
    InventoryCount,
    InventoryCountItem,
    InventoryMovement,
    InventoryReceipt,
    InventoryReceiptAllocation,
    InventoryReceiptItem,
    InventoryReceiptItemRequest,
)
from app.models.promotion import Promotion
from app.models.repeat_booking import RepeatBookingEvent, RepeatBookingOffer
from app.models.shop import (
    CustomerCartItem,
    CustomerWishlistItem,
    DeliveryCache,
    ProductImage,
    ProductImageVariant,
    ProductReview,
    ProductReviewComment,
    ProductView,
)
from app.models.shop_promotion import ShopPromotion
from app.models.upload import Upload
from app.models.waitlist import WaitlistOffer, WaitlistRequest

all_models = [
    NovaPoshtaSettlement,
    NovaPoshtaBranch,
    TelegramDeliveryAttempt,
    TelegramWebhookEvent,
    InventoryReceiptItemRequest,
    CampaignOffer,
    CampaignOfferOpen,
    SmsQueueJob,
    SmsAccountThrottle,
    CustomerSegment,
    CampaignRun,
    AdminUser,
    AnalyticsTrackingMarker,
    BlogSubscription,
    BlogSubscriptionEvent,
    BlogBrevoSyncJob,
    Master,
    BaseService,
    BarberService,
    Booking,
    BookingServiceItem,
    MasterTimeBlock,
    MasterAvailabilityWindow,
    TelegramBookingFunnelEvent,
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
    MasterMessageDelivery,
    MasterScheduleReminder,
    Product,
    InventoryMovement,
    InventoryReceipt,
    InventoryReceiptItem,
    InventoryReceiptAllocation,
    InventoryCount,
    InventoryCountItem,
    ProductImage,
    ProductImageVariant,
    CustomerCartItem,
    CustomerWishlistItem,
    ProductReview,
    ProductReviewComment,
    ProductView,
    DeliveryCache,
    ShopPromotion,
    Promotion,
    RepeatBookingOffer,
    RepeatBookingEvent,
    Order,
    OrderItem,
    Upload,
    WaitlistRequest,
    WaitlistOffer,
]

from app.models.shipment import Shipment, ShipmentEvent, MockCarrierRecord

from app.models.fiscal import MockCashRegister, FiscalPreview, FiscalDocument, FiscalJob, MockReceipt, FiscalEvent, FiscalInbox

from app.models.fiscal_automation import AutomationPreview, FiscalAutomation, AutomationCommand, MockAutomationRecord, AutomationEvent

from app.models.qr_payment import QRPaymentAttempt, QRProviderRecord, QRCommand, QREvent

from app.models.sandbox import SandboxRun

from app.models.mock_cod import MockCODConfirmation, MockShipmentReturnReceipt

from app.models.shop_sales import ShopPaymentConfirmation, ShopFulfillmentFact, ShopCompletion, ShopOperationAudit
from app.models.shop_checkbox import (ShopCheckboxRegister, ShopCheckboxBinding, ShopCheckboxOperation,
    ShopCheckboxAcceptance, ShopCheckboxPreview, ShopCheckboxDocument, ShopCheckboxReturn,
    ShopCheckboxRefundFact, ShopCheckboxPhysicalReceipt, ShopCheckboxAudit)

from app.models.shop_payment_resolutions import (ShopPaymentResolutionCase, ShopPaymentResolutionObservation, ShopPaymentInvoiceCorrection, ShopPaymentBankClosure, ShopPaymentResolutionFinalization, ShopPaymentFinancialException)
