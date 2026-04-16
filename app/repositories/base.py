from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    def __init__(self, model: type[ModelT]):
        self.model = model

    async def get(self, session: AsyncSession, entity_id: int) -> ModelT | None:
        return await session.get(self.model, entity_id)

    async def list(
        self,
        session: AsyncSession,
        *,
        stmt: Select[tuple[ModelT]] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[ModelT], int]:
        statement = stmt if stmt is not None else select(self.model)
        count_stmt = select(func.count()).select_from(statement.order_by(None).subquery())
        total = (await session.execute(count_stmt)).scalar_one()
        result = await session.execute(statement.offset((page - 1) * page_size).limit(page_size))
        return result.scalars().all(), total

    async def create(self, session: AsyncSession, data: dict[str, Any]) -> ModelT:
        instance = self.model(**data)
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        return instance

    async def update(self, session: AsyncSession, instance: ModelT, data: dict[str, Any]) -> ModelT:
        for key, value in data.items():
            setattr(instance, key, value)
        await session.commit()
        await session.refresh(instance)
        return instance

    async def delete(self, session: AsyncSession, instance: ModelT) -> None:
        await session.delete(instance)
        await session.commit()
