"""Append-only bank reconciliation and pre-handover cancellation facts."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column,relationship
from app.core.database import Base

class ShopPaymentResolutionCase(Base):
    __tablename__='shop_payment_resolution_cases'
    __table_args__=(UniqueConstraint('order_id','kind',name='uq_payment_resolution_kind'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    order_id:Mapped[int]=mapped_column(ForeignKey('orders.id',ondelete='RESTRICT'))
    kind:Mapped[str]=mapped_column(String(24))
    intent_id:Mapped[str|None]=mapped_column(ForeignKey('shop_payment_intents.id',ondelete='RESTRICT'),nullable=True)
    payment_confirmation_id:Mapped[int|None]=mapped_column(ForeignKey('shop_payment_confirmations.id',ondelete='RESTRICT'),nullable=True)
    register_id:Mapped[str|None]=mapped_column(ForeignKey('shop_checkbox_registers.id',ondelete='RESTRICT'),nullable=True)
    profile_id:Mapped[str]=mapped_column(String(100))
    profile:Mapped[dict]=mapped_column(JSON)
    profile_hash:Mapped[str]=mapped_column(String(64))
    protocol:Mapped[str]=mapped_column(String(40))
    test_context:Mapped[bool]
    revision:Mapped[int]=mapped_column(default=1)
    snapshot:Mapped[dict]=mapped_column(JSON)
    snapshot_hash:Mapped[str]=mapped_column(String(64))
    reason:Mapped[str]=mapped_column(String(500))
    operator_id:Mapped[int]=mapped_column(ForeignKey('admin_users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    observations=relationship('ShopPaymentResolutionObservation',lazy='selectin',order_by='ShopPaymentResolutionObservation.id')
    correction=relationship('ShopPaymentInvoiceCorrection',lazy='selectin',uselist=False)
    closure=relationship('ShopPaymentBankClosure',lazy='selectin',uselist=False)
    finalization=relationship('ShopPaymentResolutionFinalization',lazy='selectin',uselist=False)
    exceptions=relationship('ShopPaymentFinancialException',lazy='selectin',order_by='ShopPaymentFinancialException.id')

class ShopPaymentResolutionObservation(Base):
    __tablename__='shop_payment_resolution_observations'
    id:Mapped[int]=mapped_column(primary_key=True)
    case_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_cases.id',ondelete='RESTRICT'))
    invoice_id:Mapped[str]=mapped_column(String(100))
    error_code:Mapped[str|None]=mapped_column(String(64),nullable=True)
    proof:Mapped[dict|None]=mapped_column(JSON,nullable=True)
    proof_canonical:Mapped[str]=mapped_column(Text)
    proof_hash:Mapped[str]=mapped_column(String(64))
    operator_id:Mapped[int]=mapped_column(ForeignKey('admin_users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))

class ShopPaymentInvoiceCorrection(Base):
    __tablename__='shop_payment_invoice_corrections'
    id:Mapped[int]=mapped_column(primary_key=True)
    case_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_cases.id',ondelete='RESTRICT'),unique=True)
    intent_id:Mapped[str]=mapped_column(ForeignKey('shop_payment_intents.id',ondelete='RESTRICT'),unique=True)
    profile_id:Mapped[str]=mapped_column(String(64))
    original_invoice_id:Mapped[str]=mapped_column(String(100))
    replacement_invoice_id:Mapped[str]=mapped_column(String(100))
    foreign_observation_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_observations.id',ondelete='RESTRICT'))
    replacement_observation_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_observations.id',ondelete='RESTRICT'))
    snapshot:Mapped[dict]=mapped_column(JSON)
    snapshot_hash:Mapped[str]=mapped_column(String(64))
    operator_id:Mapped[int]=mapped_column(ForeignKey('admin_users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    __table_args__=(UniqueConstraint('profile_id','replacement_invoice_id',name='uq_corrected_invoice'),)

class ShopPaymentBankClosure(Base):
    __tablename__='shop_payment_bank_closures'
    id:Mapped[int]=mapped_column(primary_key=True)
    case_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_cases.id',ondelete='RESTRICT'),unique=True)
    policy:Mapped[dict]=mapped_column(JSON)
    policy_hash:Mapped[str]=mapped_column(String(64))
    identity:Mapped[str]=mapped_column(String(64),unique=True)
    snapshot:Mapped[dict]=mapped_column(JSON)
    snapshot_hash:Mapped[str]=mapped_column(String(64))
    operator_id:Mapped[int]=mapped_column(ForeignKey('admin_users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))

class ShopPaymentResolutionFinalization(Base):
    __tablename__='shop_payment_resolution_finalizations'
    id:Mapped[int]=mapped_column(primary_key=True)
    case_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_cases.id',ondelete='RESTRICT'),unique=True)
    order_id:Mapped[int]=mapped_column(ForeignKey('orders.id',ondelete='RESTRICT'),unique=True)
    snapshot:Mapped[dict]=mapped_column(JSON)
    snapshot_hash:Mapped[str]=mapped_column(String(64))
    operator_id:Mapped[int]=mapped_column(ForeignKey('admin_users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))

class ShopPaymentFinancialException(Base):
    __tablename__='shop_payment_financial_exceptions'
    id:Mapped[int]=mapped_column(primary_key=True)
    case_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_cases.id',ondelete='RESTRICT'))
    observation_id:Mapped[int]=mapped_column(ForeignKey('shop_payment_resolution_observations.id',ondelete='RESTRICT'),unique=True)
    reason:Mapped[str]=mapped_column(String(64))
    amount_minor:Mapped[int]
    currency:Mapped[str|None]=mapped_column(String(3),nullable=True)
    operator_id:Mapped[int]=mapped_column(ForeignKey('admin_users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
