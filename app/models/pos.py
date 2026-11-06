"""Durable quick-POS identity; financial facts remain in existing order ledgers."""
from datetime import datetime
from sqlalchemy import DateTime,ForeignKey,JSON,String,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from app.core.database import Base

class POSCheckoutIntent(Base):
    __tablename__='pos_checkout_intents'
    __table_args__=(UniqueConstraint('actor_id','intent_id',name='uq_pos_actor_intent'),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    actor_id:Mapped[int]=mapped_column(ForeignKey('admin_users.id',ondelete='RESTRICT'),index=True)
    intent_id:Mapped[str]=mapped_column(String(36))
    order_id:Mapped[int]=mapped_column(ForeignKey('orders.id',ondelete='RESTRICT'),unique=True)
    payload_hash:Mapped[str]=mapped_column(String(64))
    kind:Mapped[str]=mapped_column(String(16))
    employee_snapshot:Mapped[dict]=mapped_column(JSON)
    product_snapshot:Mapped[list]=mapped_column(JSON)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
