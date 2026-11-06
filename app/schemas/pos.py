from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,field_validator

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
    can_handover:bool
    can_recover_invoice:bool
    can_start_new:bool

Workflow=Literal['prepared','creating_invoice','awaiting_payment','payment_processing','paid_receipt_pending','ready_to_handover','completed','requires_review']
OrderStatus=Literal['pending','confirmed','paid','completed','cancelled']

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
    currency:Literal['UAH']='UAH'
    status:Workflow
    payment:Payment
    receipt:Receipt
    handover:Handover
    actions:Actions
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
    payment_status:str
    receipt_status:str
    handover_status:str
    source:Literal['quick_pos','existing_mono_pos']

class Capabilities(BaseModel):
    mode:Literal['mono_checkbox']
    currency:Literal['UAH']
    max_quantity:Literal[1]
    can_search:bool
    can_start:bool
    can_view_admin_sales:bool
    automatic_reconciliation_enabled:bool
    poll_after_ms:int
    blocking_reasons:list[str]
