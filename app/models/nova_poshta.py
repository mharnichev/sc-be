"""Locally searchable, atomically refreshed Nova Poshta directories."""
from __future__ import annotations

from sqlalchemy import Boolean, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NovaPoshtaSettlement(Base):
    __tablename__ = "nova_poshta_settlements"

    ref: Mapped[str] = mapped_column(String(36), primary_key=True)
    city_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    search_text: Mapped[str] = mapped_column(Text)
    popular_rank: Mapped[int] = mapped_column(Integer, default=100)
    delivery_available: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON)


class NovaPoshtaBranch(Base):
    __tablename__ = "nova_poshta_branches"

    ref: Mapped[str] = mapped_column(String(36), primary_key=True)
    settlement_ref: Mapped[str] = mapped_column(String(36), index=True)
    number: Mapped[str] = mapped_column(String(32))
    sort_number: Mapped[int] = mapped_column(Integer, default=0)
    search_text: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
