from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,UUID4,computed_field,field_validator,model_validator
from app.schemas.master_mono import MasterPaymentURLSource

from app.schemas.shop_promotion import SHOP_PROMOTION_CODE_PATTERN, normalize_shop_promotion_code


CART_MAX_ITEMS = 100
CART_MAX_QUANTITY = 100


class Command(BaseModel):
    model_config=ConfigDict(extra='forbid')

class CreateCheckout(Command):
    intent_id:UUID
    product_id:int=Field(gt=0,strict=True)
    quantity:int=Field(ge=1,le=1,strict=True)
    expected_amount_minor:int=Field(gt=0,strict=True)
    currency:Literal['UAH']
    @field_validator('intent_id')
    @classmethod
    def random_intent(cls,value):
        if value.version!=4:raise ValueError('UUIDv4 required')
        return value

class CartItem(Command):
    product_id: int = Field(gt=0, le=2147483647, strict=True)
    quantity: int = Field(ge=1, le=CART_MAX_QUANTITY, strict=True)


class CartQuoteRequest(Command):
    items: list[CartItem] = Field(min_length=1, max_length=CART_MAX_ITEMS)
    promo_code: str | None = Field(default=None, min_length=3, max_length=50)
    promotion_id: int | None = Field(default=None, gt=0, le=2147483647, strict=True)

    @model_validator(mode="after")
    def one_choice(self):
        if self.promotion_id is not None and self.promo_code is not None:
            raise ValueError("Select one promotion or enter one promo code")
        return self

    @field_validator("items")
    @classmethod
    def unique_products(cls, items: list[CartItem]) -> list[CartItem]:
        if len({item.product_id for item in items}) != len(items):
            raise ValueError("Duplicate products are not allowed")
        return sorted(items, key=lambda item: item.product_id)

    @field_validator("promo_code", mode="before")
    @classmethod
    def normalize_code(cls, value):
        if isinstance(value, str):
            value = normalize_shop_promotion_code(value)
            if not SHOP_PROMOTION_CODE_PATTERN.fullmatch(value):
                raise ValueError("Invalid promo code")
        return value


class CartCheckout(CartQuoteRequest):
    intent_id: UUID4
    currency: Literal["UAH"]
    quote_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class CartQuoteItem(BaseModel):
    product_id: int
    quantity: int
    base_unit_minor: int
    unit_minor: int
    promotion_id: int | None
    promotion_name: str | None
    promotion_code: str | None
    discount_source: Literal["automatic", "selected_promotion", "promo_code"] | None = None

    @computed_field
    @property
    def subtotal_minor(self) -> int:
        return self.base_unit_minor * self.quantity

    @computed_field
    @property
    def amount_minor(self) -> int:
        return self.unit_minor * self.quantity

    @computed_field
    @property
    def discount_minor(self) -> int:
        return self.subtotal_minor - self.amount_minor


class CartPromotion(BaseModel):
    id: int
    name: str
    product_ids: list[int]


class CartQuote(BaseModel):
    items: list[CartQuoteItem]
    currency: Literal["UAH"] = "UAH"
    subtotal_minor: int
    discount_minor: int
    amount_minor: int
    applied_promo_code: str | None
    promotion_id: int | None
    available_promotions: list[CartPromotion] = Field(default_factory=list)
    quote_revision: str


class AdoptCheckout(Command):
    intent_id:UUID
    order_id:int=Field(gt=0,strict=True)
    _intent=field_validator('intent_id')(CreateCheckout.random_intent.__func__)

class RevisionCommand(Command):
    expected_revision:int=Field(ge=1,strict=True)

class ProductOffer(BaseModel):
    id:int
    name:str
    brand_name:str|None
    sku:str|None
    barcode:str|None
    image_url:str|None
    variant_label:str|None
    amount_minor:int
    currency:Literal['UAH']='UAH'
    available_quantity:int
    can_sell:bool
    blocking_reasons:list[str]

class Employee(BaseModel):
    user_id:int
    master_id:int|None
    display_name:str

class CheckoutItem(BaseModel):
    product_id:int
    name:str
    sku:str|None=None
    barcode:str|None=None
    image_url:str|None=None
    variant_label:str|None=None
    quantity:int
    unit_minor:int
    total_minor:int

class Payment(BaseModel):
    provider:Literal['monobank']='monobank'
    status:Literal['not_started','pending','processing','paid','failed','expired','reversed','unknown']
    invoice_id:str|None
    confirmed_at:datetime|None
    qr_payload:str|None
    payment_url_source: MasterPaymentURLSource | None = None
    payment_url_expires_at: datetime | None = None

class Receipt(BaseModel):
    issuer:Literal['checkbox']='checkbox'
    status:Literal['not_requested','pending','accepted','failed','requires_review']
    recorded_at:datetime|None
    pdf_url:str|None

class Handover(BaseModel):
    status:Literal['not_ready','ready','handed_over']
    actor_user_id:int|None
    handed_over_at:datetime|None

class Actions(BaseModel):
    can_resume:bool
    can_refresh:bool
    can_verify_invoice:bool=False
    can_handover:bool
    can_recover_invoice:bool
    can_start_new:bool

Workflow=Literal['prepared','creating_invoice','awaiting_payment','payment_processing','paid_receipt_pending','ready_to_handover','completed','requires_review']
OrderStatus=Literal['pending','confirmed','paid','completed','cancelled']

class InvoiceRecovery(BaseModel):
    reference: UUID
    requested_at: datetime
    can_attach: bool


class CheckoutView(BaseModel):
    checkout_id:str|None
    intent_id:str|None
    order_id:int
    order_status:OrderStatus
    revision:int
    created_at:datetime
    employee:Employee
    items:list[CheckoutItem]
    amount_minor:int
    subtotal_minor: int
    discount_minor: int
    currency:Literal['UAH']='UAH'
    status:Workflow
    payment:Payment
    receipt:Receipt
    handover:Handover
    actions:Actions
    invoice_recovery: InvoiceRecovery | None = None
    poll_after_ms:int
    blocking_reasons:list[str]

class SaleSummary(BaseModel):
    order_id:int
    checkout_id:str|None
    intent_id:str|None
    created_at:datetime
    employee:Employee
    items:list[CheckoutItem]
    amount_minor:int
    currency:Literal['UAH']='UAH'
    order_status:OrderStatus
    status:Workflow
    payment_status:str
    receipt_status:str
    handover_status:str
    source:Literal['quick_pos','existing_mono_pos']

class SaleDetail(CheckoutView):
    can_check_expired_close: bool = False
    payment_status:str
    receipt_status:str
    handover_status:str
    source:Literal['quick_pos','existing_mono_pos']

class CartLimits(BaseModel):
    max_items: int
    max_quantity: int


class Capabilities(BaseModel):
    mode:Literal['mono_checkbox']
    currency:Literal['UAH']
    max_quantity:Literal[1]
    cart_limits: CartLimits
    can_search:bool
    can_start:bool
    can_view_admin_sales:bool
    automatic_reconciliation_enabled:bool
    poll_after_ms:int
    blocking_reasons:list[str]
