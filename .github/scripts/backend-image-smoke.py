"""Synthetic CI data only, inside an internal Docker network."""
import asyncio
from datetime import UTC, datetime
import sys

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.admin_user import AdminUser
from app.models.master_rewards import RewardAudit, RewardEntry, RewardSettlement


async def main(stage):
    url = make_url(settings.database_url)
    assert settings.app_env == 'test' and url.host == 'rewards-db' and url.database == 'rewards_ci'
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            if stage == 'seed':
                assert await session.scalar(text('SELECT version_num FROM alembic_version')) == '0143_auto_checkout_checkpoint'
                session.add(AdminUser(email='synthetic-image-smoke@example.com', hashed_password='unused', is_active=True, is_superuser=True))
                await session.commit()
                return
            assert stage in {'check-upgrade', 'check-history'}
            assert await session.scalar(text('SELECT version_num FROM alembic_version')) == '0144_master_rewards'
            actor = await session.scalar(select(AdminUser).where(AdminUser.email == 'synthetic-image-smoke@example.com'))
            assert actor is not None
            policy = (await session.execute(text('SELECT revision, rate_bps FROM master_reward_policy WHERE id=1'))).one()
            assert policy == (0, None)
            assert await session.scalar(select(func.count()).select_from(RewardEntry)) == 0
            assert await session.scalar(select(func.count()).select_from(RewardSettlement)) == 0
            if stage == 'check-upgrade':
                assert await session.scalar(select(func.count()).select_from(RewardAudit)) == 0
                session.add(RewardAudit(actor_id=actor.id, operation='ci-history-marker', reason='synthetic-history', facts={}, created_at=datetime.now(UTC)))
                await session.commit()
                try:
                    await session.execute(update(RewardAudit).values(reason='must-not-change'))
                    await session.commit()
                except DBAPIError:
                    await session.rollback()
                else:
                    raise AssertionError('History update unexpectedly allowed')
            rows = (await session.scalars(select(RewardAudit))).all()
            assert len(rows) == 1 and rows[0].reason == 'synthetic-history'
    finally:
        await engine.dispose()


asyncio.run(main(sys.argv[1]))
