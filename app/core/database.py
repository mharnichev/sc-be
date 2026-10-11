from __future__ import annotations

from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy import DateTime, MetaData, event, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, with_loader_criteria

from app.core.config import settings

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    metadata = metadata


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OperationalSession(Session):
    """Application sessions consistently hide archived bookings."""


@event.listens_for(OperationalSession, "do_orm_execute")
def exclude_archived_bookings(execute_state) -> None:
    """Operational ORM reads omit archived visits, including aggregates and aliases.

    Internal history/audit reads can opt in with include_archived_bookings=True.
    Core SQL/text and already-loaded identity-map objects require explicit handling.
    """
    if not execute_state.is_select or execute_state.execution_options.get("include_archived_bookings", False):
        return
    # Lazy import avoids the model's dependency on Base during module initialization.
    from app.models.booking import Booking

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(Booking, lambda booking: booking.archived_at.is_(None), include_aliases=True)
    )


engine = create_async_engine(
    settings.sqlalchemy_database_uri,
    echo=settings.debug,
    future=True,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, sync_session_class=OperationalSession, expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
