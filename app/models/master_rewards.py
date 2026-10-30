"""Private manual accounting. Never attach these tables to public ORM projections."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class RewardPolicy(Base):
    __tablename__ = 'master_reward_policy'
    __table_args__ = (CheckConstraint('id = 1 AND revision >= 0 AND (rate_bps IS NULL OR rate_bps BETWEEN 0 AND 10000)', name='valid'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_bps: Mapped[int | None] = mapped_column(Integer)


class RewardEntry(Base):
    __tablename__ = 'master_reward_entries'
    __table_args__ = (CheckConstraint('quantity > 0 AND base_minor > 0 AND reward_minor >= 0 AND rate_bps BETWEEN 0 AND 10000 AND revision > 0', name='valid'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey('order_items.id', ondelete='RESTRICT'), unique=True)
    handover_id: Mapped[int] = mapped_column(ForeignKey('order_handovers.id', ondelete='RESTRICT'))
    master_id: Mapped[int] = mapped_column(ForeignKey('masters.id', ondelete='RESTRICT'), index=True)
    beneficiary_user_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'), index=True)
    master_name: Mapped[str] = mapped_column(String(255))
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id', ondelete='RESTRICT'))
    product_name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    base_minor: Mapped[int] = mapped_column(BigInteger)
    rate_bps: Mapped[int] = mapped_column(Integer)
    reward_minor: Mapped[int] = mapped_column(BigInteger)
    policy_revision: Mapped[int] = mapped_column(Integer)
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class RewardSettlement(Base):
    __tablename__ = 'master_reward_settlements'
    __table_args__ = (CheckConstraint('total_minor > 0', name='positive'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey('masters.id', ondelete='RESTRICT'), index=True)
    beneficiary_user_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'), index=True)
    master_name: Mapped[str] = mapped_column(String(255))
    total_minor: Mapped[int] = mapped_column(BigInteger)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))


class RewardAllocation(Base):
    __tablename__ = 'master_reward_allocations'
    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey('master_reward_settlements.id', ondelete='RESTRICT'), index=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey('master_reward_entries.id', ondelete='RESTRICT'), unique=True)


class RewardAudit(Base):
    __tablename__ = 'master_reward_audits'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    operation: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    facts: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RewardOperation(Base):
    __tablename__ = 'master_reward_operations'
    __table_args__ = (UniqueConstraint('actor_id', 'key', name='uq_master_reward_operation_actor_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_users.id', ondelete='RESTRICT'))
    key: Mapped[str] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(32))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
